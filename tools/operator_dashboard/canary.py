"""Human truth, frozen-canary manifests, and safe future-training exports.

This module deliberately does not import Naming Lab seeds.  A canary has a
different persisted type and all portable records are content-addressed; local
paths live only in a separate convenience index.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json
import random
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable, Literal

from tools.operator_dashboard.taxonomy import (
    DOCUMENT_MEDIA_TYPES, LABEL_SCHEMA_VERSION, NAMING_ACTIONS, TAXONOMY_VERSION,
    normalize_tags, validate_reason_codes,
)
from tools.semantic.evaluation import CanaryRecord, evaluate
from tools.semantic.manifests import ManifestRegistry

Split = Literal["development", "training_candidate", "validation", "frozen_canary", "general_holdout"]
CANARY_SIZE = 100
FROZEN_SPLITS = frozenset({"frozen_canary", "general_holdout"})
TRAINING_SPLITS = frozenset({"development", "training_candidate"})


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _check_sha256(value: str) -> str:
    value = value.lower()
    if len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise ValueError("content_sha256 must be a 64-character lowercase hex digest")
    return value


@dataclasses.dataclass(frozen=True)
class AssetDescriptor:
    """Safe index metadata; it never contains a source path or source bytes."""

    content_sha256: str
    original_filename: str
    media_type: str
    evidence_flags: tuple[str, ...] = ()
    orientation: str = "unknown"
    evidence_strength: str = "unknown"
    split: Split = "development"

    def __post_init__(self) -> None:
        object.__setattr__(self, "content_sha256", _check_sha256(self.content_sha256))
        object.__setattr__(self, "evidence_flags", tuple(self.evidence_flags))
        if not all(isinstance(flag, str) for flag in self.evidence_flags):
            raise ValueError("evidence_flags must contain only strings")
        if self.media_type not in DOCUMENT_MEDIA_TYPES:
            raise ValueError(f"unsupported document/media type: {self.media_type}")
        if self.split not in {"development", "training_candidate", "validation", "frozen_canary", "general_holdout"}:
            raise ValueError(f"unsupported split: {self.split}")
        if "/" in self.original_filename or "\\" in self.original_filename:
            raise ValueError("original_filename must be a basename, not a path")

    @property
    def asset_id(self) -> str:
        return f"sha256:{self.content_sha256}"

    @property
    def stratum(self) -> str:
        flags = ",".join(sorted(self.evidence_flags)) or "no-extra-evidence"
        return f"{self.media_type}|{self.orientation}|{self.evidence_strength}|{flags}"


@dataclasses.dataclass(frozen=True)
class HumanTruth:
    """Model-neutral reviewer truth. Once locked, it is append-only."""

    content_sha256: str
    corpus_id: str
    split: Split
    naming_action: str
    preferred_filename: str | None
    accepted_filenames: tuple[str, ...]
    required_semantics: tuple[str, ...]
    forbidden_semantics: tuple[str, ...]
    semantic_date: dict[str, Any] | None
    client_id: str | None
    project_id: str | None
    subject: str | None
    tags: tuple[str, ...]
    media_type: str
    review_required: bool
    human_confidence: float
    additional_context_required: bool
    reason_codes: tuple[str, ...]
    reviewer_notes: str | None
    annotator_id: str
    created_at: str
    label_schema_version: str = LABEL_SCHEMA_VERSION
    taxonomy_version: str = TAXONOMY_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "content_sha256", _check_sha256(self.content_sha256))
        if self.split not in {"development", "training_candidate", "validation", "frozen_canary", "general_holdout"}:
            raise ValueError(f"unsupported split: {self.split}")
        if self.naming_action not in NAMING_ACTIONS:
            raise ValueError(f"unsupported naming action: {self.naming_action}")
        if self.naming_action == "RENAME" and not self.preferred_filename:
            raise ValueError("RENAME needs preferred_filename")
        if not 0 <= self.human_confidence <= 1:
            raise ValueError("human_confidence must be 0..1")
        if self.media_type not in DOCUMENT_MEDIA_TYPES:
            raise ValueError("invalid media_type")
        object.__setattr__(self, "accepted_filenames", tuple(dict.fromkeys(self.accepted_filenames)))
        object.__setattr__(self, "tags", normalize_tags(self.tags))
        object.__setattr__(self, "reason_codes", validate_reason_codes(self.reason_codes))

    @property
    def asset_id(self) -> str:
        return f"sha256:{self.content_sha256}"


@dataclasses.dataclass(frozen=True)
class FrozenCanary:
    canary_id: str
    corpus_id: str
    selection_seed: str
    selection_rules: str
    candidate_pool_count: int
    candidate_pool_sha256: str
    entries: tuple[AssetDescriptor, ...]
    manifest_sha256: str
    frozen_at: str


class CanaryError(ValueError):
    pass


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class CorpusStore:
    """Local metadata index for one coherent or heterogeneous review corpus.

    Importing descriptors neither copies nor opens source bytes.  The optional
    path index is deliberately separate from portable corpus records, so a
    rename/move can be refreshed without changing asset identity or truth.
    """

    def __init__(self, root: Path, corpus_id: str):
        self.root = Path(root) / corpus_id
        self.corpus_id = corpus_id
        self.metadata_path = self.root / "corpus.json"
        self.assets_path = self.root / "assets.jsonl"
        self.path_index_path = self.root / "local-path-index.json"
        self.model_results_path = self.root / "model-results.jsonl"

    def create(self, *, corpus_kind: Literal["coherent_client", "general_file_intelligence"], purpose: str) -> None:
        if corpus_kind not in {"coherent_client", "general_file_intelligence"}:
            raise CanaryError("corpus_kind must be coherent_client or general_file_intelligence")
        if self.metadata_path.exists():
            raise CanaryError(f"corpus already exists: {self.corpus_id}")
        self.root.mkdir(parents=True)
        self.metadata_path.write_text(json.dumps({"corpus_id": self.corpus_id, "corpus_kind": corpus_kind,
            "purpose": purpose, "created_at": _now(), "schema_version": LABEL_SCHEMA_VERSION}, indent=2, sort_keys=True), encoding="utf-8")

    def metadata(self) -> dict[str, Any]:
        if not self.metadata_path.exists():
            raise CanaryError(f"unknown corpus: {self.corpus_id}")
        return json.loads(self.metadata_path.read_text(encoding="utf-8"))

    def all_assets(self) -> tuple[AssetDescriptor, ...]:
        if not self.assets_path.exists():
            return ()
        return tuple(AssetDescriptor(**json.loads(line)) for line in self.assets_path.read_text(encoding="utf-8").splitlines() if line.strip())

    def import_descriptors(self, assets: Iterable[AssetDescriptor], *, local_paths: dict[str, Path] | None = None) -> int:
        metadata = self.metadata()
        sealed = metadata.get("sealed_canary_id")
        existing = {asset.content_sha256: asset for asset in self.all_assets()}
        added = 0
        with self.assets_path.open("a", encoding="utf-8") as handle:
            for asset in assets:
                previous = existing.get(asset.content_sha256)
                if previous is not None:
                    if previous != asset:
                        raise CanaryError("a content identity already has conflicting descriptor metadata")
                    continue
                if sealed:
                    raise CanaryError(f"corpus is sealed by frozen canary {sealed}; descriptor membership cannot change")
                handle.write(json.dumps(dataclasses.asdict(asset), sort_keys=True) + "\n")
                existing[asset.content_sha256] = asset
                added += 1
        if local_paths:
            index = json.loads(self.path_index_path.read_text(encoding="utf-8")) if self.path_index_path.exists() else {}
            for digest, path in local_paths.items():
                checked = _check_sha256(digest)
                if checked not in existing:
                    raise CanaryError("local path identity is not part of this corpus")
                resolved = Path(path).expanduser().resolve()
                if not resolved.is_file():
                    raise CanaryError(f"local path is not a readable file: {resolved}")
                if _sha256_file(resolved) != checked:
                    raise CanaryError(f"local path bytes do not match content identity: {resolved}")
                index[checked] = str(resolved)
            self.path_index_path.write_text(json.dumps(index, indent=2, sort_keys=True), encoding="utf-8")
        return added

    def local_path_for(self, content_sha256: str) -> Path | None:
        if not self.path_index_path.exists():
            return None
        raw = json.loads(self.path_index_path.read_text(encoding="utf-8")).get(_check_sha256(content_sha256))
        return Path(raw) if raw else None

    def import_model_results(self, results: Iterable[dict[str, Any]]) -> int:
        """Index bounded model outputs by content identity, never paths or bytes."""
        known = {asset.content_sha256 for asset in self.all_assets()}
        rows = []
        for result in results:
            digest = _check_sha256(str(result.get("content_sha256", "")))
            if digest not in known:
                raise CanaryError("model result is not part of this corpus")
            rows.append({"content_sha256": digest, "client_id": result.get("client_id"),
                         "project_id": result.get("project_id"), "tags": list(result.get("tags", ())),
                         "client_confidence": float(result.get("client_confidence", 0)),
                         "project_confidence": float(result.get("project_confidence", 0))})
        with self.model_results_path.open("a", encoding="utf-8") as handle:
            for row in rows: handle.write(json.dumps(row, sort_keys=True) + "\n")
        return len(rows)

    def model_result_for(self, content_sha256: str) -> dict[str, Any] | None:
        if not self.model_results_path.exists():
            return None
        digest = _check_sha256(content_sha256)
        found = None
        for line in self.model_results_path.read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            if row["content_sha256"] == digest: found = row
        return found

    def freeze_gate3(self, canaries: "FrozenCanaryStore", *, canary_id: str, seed: str) -> "FrozenCanary":
        metadata = self.metadata()
        if metadata.get("sealed_canary_id"):
            raise CanaryError(f"corpus is already sealed by {metadata['sealed_canary_id']}")
        assets = self.all_assets()
        if metadata["corpus_kind"] != "coherent_client":
            raise CanaryError("Gate 3 canary must come from a coherent_client corpus, not the general-file corpus")
        if len(assets) > 500:
            raise CanaryError("Gate 3 coherent corpus may contain at most 500 indexed assets")
        frozen = canaries.freeze(
            canary_id=canary_id, corpus_id=self.corpus_id, assets=assets, seed=seed,
            selection_rules="round-robin strata: media_type, orientation, evidence_strength, evidence_flags",
        )
        metadata.update({
            "sealed_canary_id": frozen.canary_id,
            "sealed_at": frozen.frozen_at,
            "candidate_pool_count": frozen.candidate_pool_count,
            "candidate_pool_sha256": frozen.candidate_pool_sha256,
        })
        self.metadata_path.write_text(json.dumps(metadata, indent=2, sort_keys=True), encoding="utf-8")
        return frozen


def _inventory(assets: Iterable[AssetDescriptor]) -> tuple[tuple[AssetDescriptor, ...], str]:
    unique: dict[str, AssetDescriptor] = {}
    for asset in assets:
        previous = unique.get(asset.content_sha256)
        if previous is not None and previous != asset:
            raise CanaryError("candidate pool contains conflicting descriptors for one content identity")
        unique[asset.content_sha256] = asset
    ordered = tuple(sorted(unique.values(), key=lambda item: item.content_sha256))
    digest = hashlib.sha256(
        _canonical_json([dataclasses.asdict(item) for item in ordered]).encode("utf-8")
    ).hexdigest()
    return ordered, digest


def deterministic_select(assets: Iterable[AssetDescriptor], *, seed: str, count: int = CANARY_SIZE) -> tuple[AssetDescriptor, ...]:
    """Round-robin deterministic strata sample, never first-N filename ordering."""
    eligible = [asset for asset in assets if asset.split not in FROZEN_SPLITS]
    unique = {asset.content_sha256: asset for asset in eligible}
    if len(unique) < count:
        raise CanaryError(f"corpus has {len(unique)} eligible assets; need at least {count} to freeze a canary")
    groups: dict[str, list[AssetDescriptor]] = defaultdict(list)
    for asset in unique.values():
        groups[asset.stratum].append(asset)
    ordered: list[list[AssetDescriptor]] = []
    for stratum in sorted(groups):
        values = sorted(groups[stratum], key=lambda item: item.content_sha256)
        random.Random(f"{seed}:{stratum}").shuffle(values)
        ordered.append(values)
    selected: list[AssetDescriptor] = []
    cursor = 0
    while len(selected) < count:
        group = ordered[cursor % len(ordered)]
        if group:
            selected.append(group.pop(0))
        cursor += 1
    return tuple(sorted(selected, key=lambda item: item.content_sha256))


class FrozenCanaryStore:
    """Separate on-disk type for immutable canary membership and locks."""

    def __init__(self, root: Path):
        self.root = Path(root)

    def _directory(self, canary_id: str) -> Path:
        if not canary_id.replace("-", "").replace("_", "").isalnum():
            raise CanaryError("unsafe canary id")
        return self.root / canary_id

    def freeze(self, *, canary_id: str, corpus_id: str, assets: Iterable[AssetDescriptor], seed: str, selection_rules: str) -> FrozenCanary:
        directory = self._directory(canary_id)
        manifest_path = directory / "manifest.json"
        if manifest_path.exists():
            raise CanaryError(f"canary already frozen: {canary_id}")
        candidate_pool, pool_digest = _inventory(assets)
        selected = deterministic_select(candidate_pool, seed=seed)
        frozen = tuple(dataclasses.replace(asset, split="frozen_canary") for asset in selected)
        body = {"canary_id": canary_id, "corpus_id": corpus_id, "selection_seed": seed,
                "selection_rules": selection_rules, "candidate_pool_count": len(candidate_pool),
                "candidate_pool_sha256": pool_digest,
                "entries": [dataclasses.asdict(item) for item in frozen], "frozen_at": _now()}
        digest = hashlib.sha256(_canonical_json(body).encode()).hexdigest()
        directory.mkdir(parents=True)
        manifest_path.write_text(json.dumps({**body, "manifest_sha256": digest}, indent=2, sort_keys=True), encoding="utf-8")
        return FrozenCanary(
            canary_id=canary_id, corpus_id=corpus_id, selection_seed=seed,
            selection_rules=selection_rules, candidate_pool_count=len(candidate_pool),
            candidate_pool_sha256=pool_digest, entries=frozen,
            manifest_sha256=digest, frozen_at=body["frozen_at"],
        )

    def load(self, canary_id: str) -> FrozenCanary:
        path = self._directory(canary_id) / "manifest.json"
        if not path.exists():
            raise CanaryError(f"unknown frozen canary: {canary_id}")
        data = json.loads(path.read_text(encoding="utf-8"))
        digest = data.pop("manifest_sha256")
        if hashlib.sha256(_canonical_json(data).encode()).hexdigest() != digest:
            raise CanaryError("canary manifest digest mismatch")
        data["entries"] = tuple(AssetDescriptor(**item) for item in data["entries"])
        if len(data["entries"]) != CANARY_SIZE:
            raise CanaryError(f"frozen Gate 3 canary must contain exactly {CANARY_SIZE} entries")
        return FrozenCanary(manifest_sha256=digest, **data)

    def contains(self, content_sha256: str) -> bool:
        digest = _check_sha256(content_sha256)
        for manifest in self.root.glob("*/manifest.json"):
            canary = self.load(manifest.parent.name)
            if any(item.content_sha256 == digest for item in canary.entries):
                return True
        return False

    def members(self, canary_id: str) -> frozenset[str]:
        return frozenset(item.content_sha256 for item in self.load(canary_id).entries)


def assert_not_frozen_for_development(content_sha256: str, canaries: FrozenCanaryStore) -> None:
    if canaries.contains(content_sha256):
        raise CanaryError("frozen canary content may not become a Naming Lab seed, prompt example, or training item")


class TruthStore:
    """Append-only truth locks plus append-only audited correction events."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "truth-events.jsonl"
        self.corrections_path = self.root / "truth-corrections.jsonl"

    def _original(self, content_sha256: str) -> HumanTruth | None:
        digest = _check_sha256(content_sha256)
        if not self.path.exists():
            return None
        for line in self.path.read_text(encoding="utf-8").splitlines():
            event = json.loads(line)
            if event["content_sha256"] == digest:
                return truth_from_json(event)
        return None

    def locked(self, content_sha256: str) -> HumanTruth | None:
        current = self._original(content_sha256)
        if current is None or not self.corrections_path.exists():
            return current
        digest = _check_sha256(content_sha256)
        for line in self.corrections_path.read_text(encoding="utf-8").splitlines():
            event = json.loads(line)
            if event["content_sha256"] != digest:
                continue
            if event["prior_truth_sha256"] != _truth_sha256(current):
                raise CanaryError("truth correction chain digest mismatch")
            current = truth_from_json(event["replacement"])
        return current

    def all(self) -> tuple[HumanTruth, ...]:
        if not self.path.exists():
            return ()
        rows = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            current = self.locked(json.loads(line)["content_sha256"])
            if current is not None:
                rows.append(current)
        return tuple(rows)

    def lock(self, truth: HumanTruth, *, registry: ManifestRegistry | None = None) -> HumanTruth:
        if self._original(truth.content_sha256) is not None:
            raise CanaryError("ground truth is locked; use an audited correction event rather than overwrite it")
        if registry is not None:
            _validate_identity(truth, registry)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(truth_to_json(truth), sort_keys=True) + "\n")
        return truth

    def correct(self, replacement: HumanTruth, *, actor_id: str, reason: str,
                registry: ManifestRegistry | None = None) -> HumanTruth:
        current = self.locked(replacement.content_sha256)
        if current is None:
            raise CanaryError("cannot correct truth before an original lock exists")
        if replacement.corpus_id != current.corpus_id:
            raise CanaryError("a correction cannot move truth to a different corpus")
        if not actor_id.strip() or not reason.strip():
            raise CanaryError("correction actor and reason are required")
        if registry is not None:
            _validate_identity(replacement, registry)
        event = {
            "content_sha256": replacement.content_sha256,
            "actor_id": actor_id.strip(),
            "reason": reason.strip(),
            "prior_truth_sha256": _truth_sha256(current),
            "replacement": truth_to_json(replacement),
            "created_at": _now(),
        }
        with self.corrections_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, sort_keys=True) + "\n")
        return replacement


def _validate_identity(truth: HumanTruth, registry: ManifestRegistry) -> None:
    if truth.client_id is not None and registry.client(truth.client_id) is None:
        raise CanaryError(f"unknown client manifest id: {truth.client_id}")
    if truth.project_id is not None:
        project = registry.project(truth.project_id)
        if project is None:
            raise CanaryError(f"unknown project manifest id: {truth.project_id}")
        if truth.client_id is None:
            raise CanaryError("a canonical project requires its canonical client")
        if project.client_id != truth.client_id:
            raise CanaryError("project does not belong to the selected canonical client")


def truth_to_json(truth: HumanTruth) -> dict[str, Any]:
    return {key: (list(value) if isinstance(value, tuple) else value) for key, value in dataclasses.asdict(truth).items()}


def truth_from_json(data: dict[str, Any]) -> HumanTruth:
    tuple_fields = {"accepted_filenames", "required_semantics", "forbidden_semantics", "tags", "reason_codes"}
    return HumanTruth(**{key: tuple(value) if key in tuple_fields else value for key, value in data.items()})


def _truth_sha256(truth: HumanTruth) -> str:
    return hashlib.sha256(_canonical_json(truth_to_json(truth)).encode("utf-8")).hexdigest()


def blind_review_payload(asset: AssetDescriptor, *, truth: HumanTruth | None, model_result: dict[str, Any] | None,
                         evidence: dict[str, Any], reveal_model_comparison: bool = True) -> dict[str, Any]:
    """Never serializes model output before lock or when holdout blindness is required."""
    payload = {"asset": dataclasses.asdict(asset), "evidence": evidence, "ground_truth_locked": truth is not None}
    if truth is not None:
        payload["truth"] = truth_to_json(truth)
        if reveal_model_comparison:
            payload["model_comparison"] = model_result
        else:
            payload["model_comparison_withheld"] = True
    return payload


def export_training_jsonl(truths: Iterable[HumanTruth], destination: Path, *,
                          canaries: FrozenCanaryStore) -> int:
    rows = list(truths)
    unsafe = [row.content_sha256 for row in rows
              if row.split in FROZEN_SPLITS or canaries.contains(row.content_sha256)]
    if unsafe:
        raise CanaryError(f"training export refuses frozen/holdout identities: {unsafe[:3]!r}")
    allowed = [row for row in rows if row.split in TRAINING_SPLITS]
    with Path(destination).open("w", encoding="utf-8") as handle:
        for row in allowed:
            handle.write(json.dumps({"schema_version": row.label_schema_version, "taxonomy_version": row.taxonomy_version,
                                     "asset_id": row.asset_id, "content_sha256": row.content_sha256,
                                     "supervision": truth_to_json(row)}, sort_keys=True) + "\n")
    return len(allowed)


def canary_records_from_truth(truths: Iterable[HumanTruth], predictions: dict[str, dict[str, Any]]) -> tuple[CanaryRecord, ...]:
    """Low-level adapter; Gate 3 claims must use frozen_canary_report()."""
    rows = []
    for truth in truths:
        predicted = predictions.get(truth.content_sha256, {})
        rows.append(CanaryRecord(
            asset_id=truth.asset_id, expected_client_id=truth.client_id, expected_project_id=truth.project_id,
            expected_tags=frozenset(truth.tags), predicted_client_id=predicted.get("client_id"),
            predicted_project_id=predicted.get("project_id"), predicted_tags=frozenset(predicted.get("tags", ())),
            client_confidence=float(predicted.get("client_confidence", 0)),
            project_confidence=float(predicted.get("project_confidence", 0)),
        ))
    return tuple(rows)



def frozen_canary_report(*, canaries: FrozenCanaryStore, canary_id: str,
                         truth_store: TruthStore, predictions: dict[str, dict[str, Any]],
                         run_identity: str) -> dict[str, Any]:
    """Evaluate exactly one verified frozen manifest and record its identity."""
    if not run_identity.strip():
        raise CanaryError("run_identity is required for a Gate 3 evaluation")
    canary = canaries.load(canary_id)
    members = {item.content_sha256 for item in canary.entries}
    extras = set(predictions) - members
    if extras:
        raise CanaryError(f"predictions contain {len(extras)} identities outside frozen canary")
    truths: list[HumanTruth] = []
    missing: list[str] = []
    for item in canary.entries:
        truth = truth_store.locked(item.content_sha256)
        if truth is None:
            missing.append(item.content_sha256)
            continue
        if truth.corpus_id != canary.corpus_id:
            raise CanaryError("frozen truth corpus_id does not match canary corpus_id")
        truths.append(dataclasses.replace(truth, split="frozen_canary"))
    if missing:
        raise CanaryError(f"frozen canary has {len(missing)} unlabeled identities")
    report = evaluate(canary_records_from_truth(truths, predictions))
    return {
        "canary_id": canary.canary_id,
        "canary_manifest_sha256": canary.manifest_sha256,
        "corpus_id": canary.corpus_id,
        "candidate_pool_count": canary.candidate_pool_count,
        "candidate_pool_sha256": canary.candidate_pool_sha256,
        "run_identity": run_identity.strip(),
        "evaluated_at": _now(),
        "metrics": report.as_dict(),
    }
