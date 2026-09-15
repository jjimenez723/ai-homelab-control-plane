"""Declarative campaign manifest: the smallest schema that captures provenance.

A manifest names *what* to measure (suites, splits, candidates, sample
count, thresholds) and nothing about *how* the runner executes it -- that
split is what lets `tools/campaign/runner.py` change concurrency or
checkpointing strategy without the manifest format moving.

``campaign_id`` is the human identity; ``manifest_fingerprint()`` is the
mechanical one. `tools/campaign/runner.py` records the fingerprint the first
time a campaign_id is run and refuses to silently resume under a changed
manifest (`ManifestDrift`) -- changing a meaningful input is required to
produce a new identity (this task's own brief), and the enforcement point is
here: a campaign_id is not allowed to mean two different things across runs.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from tools.campaign.fingerprint import Candidate, PromptRef, sha256_of
from tools.campaign.suites import UnknownSuite, suite as lookup_suite
from tools.prompts.registry import UnknownPrompt, load_default_registry

MANIFEST_SCHEMA_VERSION = 1

DEFAULT_SAMPLES_PER_ITEM = 5
DEFAULT_TIMEOUT_SECONDS = 60
DEFAULT_CONCURRENCY = 1


@dataclass(frozen=True)
class SuiteThreshold:
    min_pass_rate: float = 0.0
    min_confidence: float = 0.0
    max_timeout_rate: float = 1.0
    max_provider_error_rate: float = 1.0
    max_p95_latency_seconds: float | None = None


@dataclass(frozen=True)
class SuiteSpec:
    suite_id: str
    split: str
    threshold: SuiteThreshold


@dataclass(frozen=True)
class CampaignManifest:
    campaign_id: str
    description: str
    suites: tuple[SuiteSpec, ...]
    candidates: tuple[Candidate, ...]
    samples_per_item: int
    timeout_seconds: int
    concurrency: int

    def manifest_fingerprint(self) -> str:
        payload: dict[str, Any] = {
            "campaign_id": self.campaign_id,
            "samples_per_item": self.samples_per_item,
            "timeout_seconds": self.timeout_seconds,
            "suites": sorted([
                {"suite_id": s.suite_id, "split": s.split,
                 "threshold": vars(s.threshold)}
                for s in self.suites
            ], key=lambda d: d["suite_id"]),
            "candidates": sorted([
                {**c.candidate_identity(), "name": c.name,
                 "prompts": {k: vars(v) for k, v in c.prompts.items()}}
                for c in self.candidates
            ], key=lambda d: d["name"]),
        }
        return sha256_of(payload)


class ManifestError(ValueError):
    """The manifest file is structurally invalid."""


def _require(mapping: dict[str, Any], key: str, where: str) -> Any:
    if key not in mapping:
        raise ManifestError(f"{where}: missing required field {key!r}")
    return mapping[key]


def _load_candidate(raw: dict[str, Any], *, index: int) -> Candidate:
    where = f"candidates[{index}]"
    if not isinstance(raw, dict):
        raise ManifestError(f"{where} must be a mapping")
    name = str(_require(raw, "name", where))
    prompts_raw = _require(raw, "prompts", where)
    if not isinstance(prompts_raw, dict) or not prompts_raw:
        raise ManifestError(f"{where}.prompts must be a non-empty mapping of suite_id -> prompt ref")
    prompts: dict[str, PromptRef] = {}
    registry = load_default_registry()
    for suite_id, prompt_raw in prompts_raw.items():
        if not isinstance(prompt_raw, dict):
            raise ManifestError(f"{where}.prompts[{suite_id!r}] must be a mapping")
        prompt_id = str(_require(prompt_raw, "prompt_id", f"{where}.prompts[{suite_id!r}]"))
        prompt_version = str(_require(prompt_raw, "prompt_version", f"{where}.prompts[{suite_id!r}]"))
        try:
            registry.get(prompt_id, prompt_version)
        except UnknownPrompt as exc:
            raise ManifestError(f"{where}.prompts[{suite_id!r}]: {exc}") from exc
        prompts[suite_id] = PromptRef(prompt_id, prompt_version)
    return Candidate(
        name=name,
        model=str(_require(raw, "model", where)),
        runtime=str(_require(raw, "runtime", where)),
        endpoint=str(raw.get("endpoint", "http://127.0.0.1:11434")),
        tool_schema_version=str(raw.get("tool_schema_version", "none")),
        prompts=prompts,
        model_digest=raw.get("model_digest"),
        quantization=raw.get("quantization"),
        sampling=dict(raw.get("sampling") or {}),
        augmentation_version=str(raw.get("augmentation_version", "none")),
    )


def _load_suite_spec(raw: dict[str, Any], *, index: int) -> SuiteSpec:
    where = f"suites[{index}]"
    if not isinstance(raw, dict):
        raise ManifestError(f"{where} must be a mapping")
    suite_id = str(_require(raw, "id", where))
    try:
        definition = lookup_suite(suite_id)
    except UnknownSuite as exc:
        raise ManifestError(f"{where}: {exc}") from exc
    split = str(raw.get("split", "validation"))
    if split not in ("dev", "validation", "frozen_holdout"):
        raise ManifestError(f"{where}.split must be dev/validation/frozen_holdout, got {split!r}")
    if not definition.by_split(split):
        raise ManifestError(f"{where}: suite {suite_id!r} has no tasks in split {split!r}")
    threshold_raw = raw.get("threshold") or {}
    threshold = SuiteThreshold(
        min_pass_rate=float(threshold_raw.get("min_pass_rate", 0.0)),
        min_confidence=float(threshold_raw.get("min_confidence", 0.0)),
        max_timeout_rate=float(threshold_raw.get("max_timeout_rate", 1.0)),
        max_provider_error_rate=float(threshold_raw.get("max_provider_error_rate", 1.0)),
        max_p95_latency_seconds=threshold_raw.get("max_p95_latency_seconds"),
    )
    return SuiteSpec(suite_id=suite_id, split=split, threshold=threshold)


def load_manifest(path: Path) -> CampaignManifest:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    if raw.get("version") != MANIFEST_SCHEMA_VERSION:
        raise ManifestError(
            f"unsupported campaign manifest schema version: expected "
            f"{MANIFEST_SCHEMA_VERSION}, got {raw.get('version')!r}"
        )
    campaign_id = str(_require(raw, "campaign_id", "manifest"))
    suites_raw = _require(raw, "suites", "manifest")
    candidates_raw = _require(raw, "candidates", "manifest")
    if not isinstance(suites_raw, list) or not suites_raw:
        raise ManifestError("manifest.suites must be a non-empty list")
    if not isinstance(candidates_raw, list) or not candidates_raw:
        raise ManifestError("manifest.candidates must be a non-empty list")

    suites = tuple(_load_suite_spec(item, index=i) for i, item in enumerate(suites_raw))
    candidates = tuple(_load_candidate(item, index=i) for i, item in enumerate(candidates_raw))

    names = [c.name for c in candidates]
    if len(names) != len(set(names)):
        raise ManifestError(f"manifest.candidates has duplicate name(s): {sorted({n for n in names if names.count(n) > 1})}")
    for spec in suites:
        missing = [c.name for c in candidates if spec.suite_id not in c.prompts]
        if missing:
            raise ManifestError(
                f"suite {spec.suite_id!r} is requested but candidate(s) {missing} have no pinned prompt for it"
            )

    samples_per_item = int(raw.get("samples_per_item", DEFAULT_SAMPLES_PER_ITEM))
    if samples_per_item < 1:
        raise ManifestError("manifest.samples_per_item must be >= 1")

    return CampaignManifest(
        campaign_id=campaign_id,
        description=str(raw.get("description", "")),
        suites=suites,
        candidates=candidates,
        samples_per_item=samples_per_item,
        timeout_seconds=int(raw.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)),
        concurrency=int(raw.get("concurrency", DEFAULT_CONCURRENCY)),
    )
