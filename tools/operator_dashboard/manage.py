"""Explicit local corpus/index/freeze/export/evaluation operations."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from tools.operator_dashboard.canary import (
    AssetDescriptor, CorpusStore, FrozenCanaryStore, TruthStore,
    export_training_jsonl, frozen_canary_report,
)


def _descriptor_payload(raw: list[dict]) -> tuple[list[AssetDescriptor], dict[str, Path]]:
    descriptors: list[AssetDescriptor] = []
    local_paths: dict[str, Path] = {}
    for item in raw:
        row = dict(item)
        local_path = row.pop("local_path", None)
        descriptor = AssetDescriptor(**row)
        descriptors.append(descriptor)
        if local_path:
            local_paths[descriptor.content_sha256] = Path(str(local_path))
    return descriptors, local_paths


def _predictions(path: Path) -> dict[str, dict]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    values = raw.values() if isinstance(raw, dict) else raw
    if not isinstance(raw, (dict, list)):
        raise ValueError("predictions JSON must be a list or mapping")
    output: dict[str, dict] = {}
    for item in values:
        digest = str(item["content_sha256"])
        output[digest] = dict(item)
    return output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="angel-operator-manage")
    parser.add_argument("--state", required=True, type=Path)
    actions = parser.add_subparsers(dest="action", required=True)

    init = actions.add_parser("init-corpus")
    init.add_argument("--corpus-id", required=True)
    init.add_argument("--kind", choices=("coherent_client", "general_file_intelligence"), required=True)
    init.add_argument("--purpose", required=True)

    imported = actions.add_parser("import-descriptors")
    imported.add_argument("--corpus-id", required=True)
    imported.add_argument("--json", required=True, type=Path)

    results = actions.add_parser("import-model-results")
    results.add_argument("--corpus-id", required=True)
    results.add_argument("--json", required=True, type=Path)

    freeze = actions.add_parser("freeze-gate3")
    freeze.add_argument("--corpus-id", required=True)
    freeze.add_argument("--canary-id", required=True)
    freeze.add_argument("--seed", required=True)

    export = actions.add_parser("export-training")
    export.add_argument("--corpus-id", required=True)
    export.add_argument("--out", required=True, type=Path)

    evaluate = actions.add_parser("evaluate-gate3")
    evaluate.add_argument("--corpus-id", required=True)
    evaluate.add_argument("--canary-id", required=True)
    evaluate.add_argument("--predictions", required=True, type=Path)
    evaluate.add_argument("--run-identity", required=True)
    evaluate.add_argument("--out", required=True, type=Path)

    args = parser.parse_args(argv)
    state = args.state.expanduser().resolve()
    canaries = FrozenCanaryStore(state / "canaries")

    if args.action == "init-corpus":
        CorpusStore(state / "corpora", args.corpus_id).create(corpus_kind=args.kind, purpose=args.purpose)
        return 0

    corpus = CorpusStore(state / "corpora", args.corpus_id)
    if args.action == "import-descriptors":
        raw = json.loads(args.json.read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            raise ValueError("descriptor JSON must be a list")
        descriptors, local_paths = _descriptor_payload(raw)
        print(corpus.import_descriptors(descriptors, local_paths=local_paths or None))
        return 0
    if args.action == "import-model-results":
        print(corpus.import_model_results(json.loads(args.json.read_text(encoding="utf-8"))))
        return 0
    if args.action == "freeze-gate3":
        frozen = corpus.freeze_gate3(canaries, canary_id=args.canary_id, seed=args.seed)
        print(frozen.manifest_sha256)
        return 0

    truth_store = TruthStore(state / "truth" / args.corpus_id)
    if args.action == "export-training":
        print(export_training_jsonl(truth_store.all(), args.out, canaries=canaries))
        return 0

    report = frozen_canary_report(
        canaries=canaries,
        canary_id=args.canary_id,
        truth_store=truth_store,
        predictions=_predictions(args.predictions),
        run_identity=args.run_identity,
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    print(args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
