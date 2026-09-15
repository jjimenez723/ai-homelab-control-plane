"""Work units: campaign + candidate + suite + item + sample, and how one runs.

A work unit's identity is exactly the tuple this task's brief names:
``campaign + candidate fingerprint + suite + item + sample index``. Deriving
the id from the *configuration* fingerprint (not the candidate name alone)
means a manifest edit that changes what is measured produces new work-unit
ids automatically -- resumability and drift-detection are the same
mechanism, not two.
"""

from __future__ import annotations

import platform
import socket
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from tools.bench.suite import SuiteTask
from tools.campaign.fingerprint import Candidate, configuration_fingerprint
from tools.campaign.manifest import CampaignManifest, SuiteSpec
from tools.campaign.store import ResultRecord, RESULT_RECORD_SCHEMA_VERSION
from tools.campaign.suites import prompt_input, suite as lookup_suite, verifier as lookup_verifier
from tools.models.ollama_local import OllamaTextProvider
from tools.prompts.registry import load_default_registry
from tools.prompts.renderer import render


@dataclass(frozen=True)
class WorkUnit:
    work_unit_id: str
    campaign_id: str
    manifest_fingerprint: str
    candidate: Candidate
    configuration_fingerprint: str
    suite_id: str
    suite_version: str
    split: str
    item: SuiteTask
    sample_index: int
    timeout_seconds: int


def _work_unit_id(campaign_id: str, configuration_fp: str, suite_id: str, item_id: str, sample_index: int) -> str:
    return f"{campaign_id}:{configuration_fp}:{suite_id}:{item_id}:sample{sample_index}"


def enumerate_work_units(manifest: CampaignManifest, *, candidates: tuple[Candidate, ...] | None = None) -> list[WorkUnit]:
    """Enumerate work for the source manifest, optionally using runtime-resolved candidates.

    Runtime resolution supplies exact model digest/quantization discovered from the
    local provider after an approved model is staged. The source manifest fingerprint
    remains the campaign identity; the per-work-unit configuration fingerprint uses
    the resolved candidate, so a different model digest can never silently reuse an
    earlier model's work-unit ids.
    """
    manifest_fp = manifest.manifest_fingerprint()
    units: list[WorkUnit] = []
    candidate_set = candidates if candidates is not None else manifest.candidates
    for spec in manifest.suites:
        definition = lookup_suite(spec.suite_id)
        items = definition.by_split(spec.split)
        for candidate in candidate_set:
            config_fp = configuration_fingerprint(
                candidate, suite_id=spec.suite_id, suite_version=definition.suite_version)
            for item in items:
                for sample_index in range(manifest.samples_per_item):
                    unit_id = _work_unit_id(manifest.campaign_id, config_fp, spec.suite_id,
                                            item.id, sample_index)
                    units.append(WorkUnit(
                        work_unit_id=unit_id, campaign_id=manifest.campaign_id,
                        manifest_fingerprint=manifest_fp, candidate=candidate,
                        configuration_fingerprint=config_fp, suite_id=spec.suite_id,
                        suite_version=definition.suite_version, split=spec.split, item=item,
                        sample_index=sample_index, timeout_seconds=manifest.timeout_seconds,
                    ))
    return units


def build_provider(candidate: Candidate) -> OllamaTextProvider:
    sampling = candidate.sampling or {}
    base_seed = sampling.get("seed")
    return OllamaTextProvider(
        candidate.endpoint, candidate.model,
        num_predict=int(sampling.get("num_predict", OllamaTextProvider.DEFAULT_NUM_PREDICT)),
        num_ctx=sampling.get("num_ctx"),
        seed=base_seed,
        think=bool(sampling.get("think", False)),
        temperature=float(sampling.get("temperature", 0.0)),
    )


_INFRA_FAILURE_STATUS = {
    TimeoutError: "timeout",
    ConnectionError: "provider_unavailable",
}


def execute_work_unit(unit: WorkUnit, provider: OllamaTextProvider) -> ResultRecord:
    """Render, call the provider, verify, and classify -- never raises.

    Every branch returns a `ResultRecord`; a provider/runtime failure is
    recorded as an infrastructure status, never silently treated as a wrong
    answer (the same discipline `tools.bench.harness.run` and
    `tools.bench.ablation.run_condition` already apply, generalised across
    suites and candidates rather than one corpus and one experiment).
    """
    started_at = datetime.now(timezone.utc).isoformat()
    started = time.perf_counter()
    host = socket.gethostname()
    registry = load_default_registry()
    prompt_ref = unit.candidate.prompts[unit.suite_id]
    prompt_version = registry.get(prompt_ref.prompt_id, prompt_ref.prompt_version)
    task_text, extra = prompt_input(unit.suite_id, unit.item)
    rendered = render(prompt_version, task_text=task_text, extra=extra)

    sampling = dict(unit.candidate.sampling or {})
    # The same seed is used for every sample of an item, on the same
    # provider instance -- this suite's repeated sampling measures whatever
    # variance the runtime actually has (D-049: a seed is recorded for
    # repeatability, never claimed as proof of bit-exact determinism), it
    # does not manufacture variance by changing the seed per sample.
    seed = sampling.get("seed")
    seed = int(seed) if seed is not None else None
    num_predict = int(sampling.get("num_predict", OllamaTextProvider.DEFAULT_NUM_PREDICT))

    common: dict[str, Any] = dict(
        schema_version=RESULT_RECORD_SCHEMA_VERSION, work_unit_id=unit.work_unit_id,
        campaign_id=unit.campaign_id, manifest_fingerprint=unit.manifest_fingerprint,
        candidate_name=unit.candidate.name, configuration_fingerprint=unit.configuration_fingerprint,
        suite_id=unit.suite_id, suite_version=unit.suite_version, split=unit.split,
        item_id=unit.item.id, scenario=unit.item.scenario, sample_index=unit.sample_index,
        prompt_id=rendered.prompt_id, prompt_version=rendered.version, prompt_sha256=rendered.sha256,
        tool_schema_version=rendered.tool_schema_version, seed=seed, started_at=started_at, host=host,
    )

    try:
        response = provider.generate(
            rendered.text, num_predict=num_predict, timeout_seconds=unit.timeout_seconds,
        )
    except (TimeoutError, ConnectionError) as exc:
        status = _INFRA_FAILURE_STATUS.get(type(exc), "provider_unavailable")
        return ResultRecord(**common, status=status, passed=None, detail={},
                            wall_seconds=time.perf_counter() - started,
                            input_tokens=None, output_tokens=None,
                            error=f"{type(exc).__name__}: {exc}")
    except ValueError as exc:
        return ResultRecord(**common, status="provider_error", passed=None, detail={},
                            wall_seconds=time.perf_counter() - started,
                            input_tokens=None, output_tokens=None,
                            error=f"{type(exc).__name__}: {exc}")
    except Exception as exc:  # noqa: BLE001 -- an unexpected exception is still an infra fact, never silence it
        return ResultRecord(**common, status="provider_error", passed=None, detail={},
                            wall_seconds=time.perf_counter() - started,
                            input_tokens=None, output_tokens=None,
                            error=f"unexpected {type(exc).__name__}: {exc}")

    wall = time.perf_counter() - started
    output = response.get("output", "") if isinstance(response, dict) else str(response)
    output_tokens = response.get("output_tokens") if isinstance(response, dict) else None
    input_tokens = response.get("input_tokens") if isinstance(response, dict) else None
    truncated = bool(output_tokens is not None and output_tokens >= num_predict)

    try:
        verify = lookup_verifier(unit.suite_id)
        passed, detail = verify(unit.item, output)
    except Exception as exc:  # noqa: BLE001 -- an oracle crash is a verifier fact, not a model fact
        return ResultRecord(**common, status="verifier_error", passed=None,
                            detail={"error": f"{type(exc).__name__}: {exc}"},
                            wall_seconds=wall, input_tokens=input_tokens, output_tokens=output_tokens,
                            error=f"verifier raised: {type(exc).__name__}: {exc}")

    if not passed and isinstance(detail, dict) and detail.get("failure") == "non_string_output":
        status = "invalid_output"
    elif not passed and truncated:
        status = "truncated"
    elif passed:
        status = "verified_pass"
    else:
        status = "verified_fail"

    return ResultRecord(**common, status=status, passed=passed, detail=dict(detail or {}),
                        wall_seconds=wall, input_tokens=input_tokens, output_tokens=output_tokens, error=None)
