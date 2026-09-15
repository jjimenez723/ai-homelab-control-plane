"""Measured certifications: what makes a configuration production-eligible.

D-042 says production eligibility belongs to a configuration, not a model
name:

    model + quant + runtime + prompt_version + tool_schema_version + capability

D-044 adds that the measurement behind it must be a production-capability
benchmark, not a raw-model baseline. Before this module the router had no
certification concept at all -- `_candidates()` selected on tier, declared
capability, and context length, which is parameter-count reasoning wearing
a registry's clothes.

Three properties this file exists to enforce, each of them a fail-closed
default:

* **A certification is a claim about a pinned configuration.** If the
  runtime, quantization, prompt version, or tool-schema version the router
  is about to use differs from the one that was measured, the certification
  does not apply. A prompt change is a capability change (docs/20 §4).
* **Certifications expire.** A measurement has a date; hardware, runtimes,
  and model tags move underneath it. An expired certification is not
  eligibility, and the router says so rather than quietly still using it.
* **Only measured evidence certifies.** `status: active` requires a suite,
  a suite version, a threshold, and a measured pass rate at or above it.
  A record that does not carry its evidence is refused at load time, not
  at routing time.

Nothing here runs a benchmark. It records the result of one and answers
"may this configuration be routed this task class, right now?".
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import yaml

from tools.models.task_classes import UnknownTaskClass, suite_for, task_class

CERTIFICATION_SCHEMA_VERSION = 1

STATUSES = {"active", "provisional", "revoked"}
# Only "active" is production-eligible. "provisional" records a measured run
# that has not met its threshold or has not been reviewed; it exists so a
# result can be kept without becoming permission.
ELIGIBLE_STATUSES = {"active"}


@dataclass(frozen=True)
class Certification:
    """One measured claim: this configuration, this capability, this evidence."""

    model: str
    task_class: str
    suite: str
    suite_version: str
    prompt_id: str
    prompt_version: str
    tool_schema_version: str
    runtime: str
    quantization: str | None
    threshold: float
    measured_pass_rate: float
    measured_samples: int
    p95_latency_seconds: float | None
    peak_rss_bytes: int | None
    evaluated_at: dt.date
    expires_at: dt.date
    status: str
    known_failure_modes: tuple[str, ...] = ()
    notes: str | None = None

    def is_expired(self, today: dt.date) -> bool:
        return today > self.expires_at

    def meets_threshold(self) -> bool:
        return self.measured_pass_rate >= self.threshold

    def configuration_matches(self, *, runtime: str, quantization: str | None,
                              prompt_version: str | None, tool_schema_version: str | None) -> bool:
        """Whether the configuration about to be routed is the measured one.

        ``None`` for a prompt or tool-schema version means the caller has
        not pinned one. That is treated as a mismatch, not a wildcard: an
        unpinned prompt is exactly the ambiguity docs/20 §5 exists to
        prevent, where a score change cannot be attributed to the model or
        the prompt.
        """
        return (
            runtime == self.runtime
            and quantization == self.quantization
            and prompt_version == self.prompt_version
            and tool_schema_version == self.tool_schema_version
        )


@dataclass(frozen=True)
class Ineligible:
    """Why a configuration was not eligible. Recorded, never swallowed."""

    model: str
    task_class: str
    reason: str


class CertificationRegistry:
    """Answers eligibility questions; never decides that nothing is required."""

    def __init__(self, certifications: Iterable[Certification]):
        self._records = tuple(certifications)

    def all(self) -> tuple[Certification, ...]:
        return self._records

    def for_model(self, model: str) -> tuple[Certification, ...]:
        return tuple(record for record in self._records if record.model == model)

    def check(self, *, model: str, task_class_name: str, runtime: str,
              quantization: str | None, prompt_version: str | None,
              tool_schema_version: str | None, today: dt.date | None = None,
              ) -> tuple[Certification | None, Ineligible | None]:
        """Return the certification that permits this route, or why none does.

        The negative result is as important as the positive one: an
        autonomous router has to be able to log *why* it declined to use a
        backend, or an operator cannot tell "nothing is certified" from
        "the certified prompt version moved".
        """
        today = today or dt.date.today()
        suite = suite_for(task_class_name)
        if suite is None:
            return None, Ineligible(model, task_class_name, "no ACB-v1 suite gates this task class yet")

        candidates = [r for r in self._records if r.model == model and r.task_class == task_class_name]
        if not candidates:
            return None, Ineligible(model, task_class_name, "no certification recorded")

        reasons: list[str] = []
        for record in candidates:
            if record.status not in ELIGIBLE_STATUSES:
                reasons.append(f"status is {record.status!r}")
                continue
            if record.is_expired(today):
                reasons.append(f"expired {record.expires_at.isoformat()}")
                continue
            if not record.meets_threshold():
                reasons.append(f"measured {record.measured_pass_rate:.3f} below threshold {record.threshold:.3f}")
                continue
            if not record.configuration_matches(
                runtime=runtime, quantization=quantization,
                prompt_version=prompt_version, tool_schema_version=tool_schema_version,
            ):
                reasons.append(
                    "configuration differs from the measured one "
                    f"(certified runtime={record.runtime} quant={record.quantization} "
                    f"prompt={record.prompt_version} tools={record.tool_schema_version})"
                )
                continue
            return record, None
        return None, Ineligible(model, task_class_name, "; ".join(reasons) or "no eligible certification")


def _require(item: dict[str, Any], key: str, index: int) -> Any:
    if key not in item:
        raise ValueError(f"certifications[{index}] missing required field {key!r}")
    return item[key]


def _as_date(value: Any, field: str, index: int) -> dt.date:
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    try:
        return dt.date.fromisoformat(str(value))
    except ValueError as exc:
        raise ValueError(f"certifications[{index}].{field} is not an ISO date: {value!r}") from exc


def load_certifications(path: Path) -> CertificationRegistry:
    """Load and validate a certification file, refusing unevidenced records."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if raw.get("version") != CERTIFICATION_SCHEMA_VERSION:
        raise ValueError(
            f"unsupported certification schema version: expected {CERTIFICATION_SCHEMA_VERSION}, "
            f"got {raw.get('version')!r}"
        )
    entries = raw.get("certifications")
    if entries is None:
        entries = []
    if not isinstance(entries, list):
        raise ValueError("certifications must be a list")

    records: list[Certification] = []
    for index, item in enumerate(entries):
        if not isinstance(item, dict):
            raise ValueError(f"certifications[{index}] must be a mapping")

        name = str(_require(item, "task_class", index))
        try:
            declared = task_class(name)
        except UnknownTaskClass as exc:
            raise ValueError(f"certifications[{index}]: {exc}") from exc
        if declared.suite is None:
            raise ValueError(
                f"certifications[{index}] certifies {name!r}, which no ACB-v1 suite gates yet; "
                "build the suite before recording a certification against it"
            )

        suite = str(item.get("suite", declared.suite))
        if suite != declared.suite:
            raise ValueError(
                f"certifications[{index}] suite {suite!r} does not match the suite for "
                f"task class {name!r} ({declared.suite!r})"
            )

        status = str(item.get("status", "provisional"))
        if status not in STATUSES:
            raise ValueError(f"certifications[{index}].status is invalid: {status!r}")

        threshold = float(_require(item, "threshold", index))
        pass_rate = float(_require(item, "measured_pass_rate", index))
        samples = int(_require(item, "measured_samples", index))
        if not 0.0 <= threshold <= 1.0 or not 0.0 <= pass_rate <= 1.0:
            raise ValueError(f"certifications[{index}]: threshold and measured_pass_rate must be in [0, 1]")
        if samples <= 0:
            raise ValueError(f"certifications[{index}].measured_samples must be positive")
        if status == "active" and pass_rate < threshold:
            raise ValueError(
                f"certifications[{index}] is active but measured {pass_rate} below threshold {threshold}; "
                "record it as provisional rather than promoting it"
            )

        evaluated_at = _as_date(_require(item, "evaluated_at", index), "evaluated_at", index)
        expires_at = _as_date(_require(item, "expires_at", index), "expires_at", index)
        if expires_at <= evaluated_at:
            raise ValueError(f"certifications[{index}].expires_at must be after evaluated_at")

        records.append(Certification(
            model=str(_require(item, "model", index)),
            task_class=name,
            suite=suite,
            suite_version=str(_require(item, "suite_version", index)),
            prompt_id=str(_require(item, "prompt_id", index)),
            prompt_version=str(_require(item, "prompt_version", index)),
            tool_schema_version=str(_require(item, "tool_schema_version", index)),
            runtime=str(_require(item, "runtime", index)),
            quantization=item.get("quantization"),
            threshold=threshold,
            measured_pass_rate=pass_rate,
            measured_samples=samples,
            p95_latency_seconds=item.get("p95_latency_seconds"),
            peak_rss_bytes=item.get("peak_rss_bytes"),
            evaluated_at=evaluated_at,
            expires_at=expires_at,
            status=status,
            known_failure_modes=tuple(item.get("known_failure_modes") or ()),
            notes=item.get("notes"),
        ))
    return CertificationRegistry(records)


def empty_registry() -> CertificationRegistry:
    """A registry that certifies nothing.

    Angel's real state today: no suite exists, so no configuration is
    certified. Routing a certification-gated task class against this
    registry produces an explicit escalation, which is the correct and
    honest outcome -- not an error, and not a silent success.
    """
    return CertificationRegistry(())
