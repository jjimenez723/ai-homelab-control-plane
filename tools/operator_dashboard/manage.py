"""Explicit local corpus/index/freeze/export operations; no source bytes move."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from tools.operator_dashboard.canary import AssetDescriptor, CorpusStore, FrozenCanaryStore, TruthStore, export_training_jsonl


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="angel-operator-manage")
    parser.add_argument("--state", required=True, type=Path)
    actions = parser.add_subparsers(dest="action", required=True)
    init = actions.add_parser("init-corpus"); init.add_argument("--corpus-id", required=True); init.add_argument("--kind", choices=("coherent_client", "general_file_intelligence"), required=True); init.add_argument("--purpose", required=True)
    imported = actions.add_parser("import-descriptors"); imported.add_argument("--corpus-id", required=True); imported.add_argument("--json", required=True, type=Path)
    results = actions.add_parser("import-model-results"); results.add_argument("--corpus-id", required=True); results.add_argument("--json", required=True, type=Path)
    freeze = actions.add_parser("freeze-gate3"); freeze.add_argument("--corpus-id", required=True); freeze.add_argument("--canary-id", required=True); freeze.add_argument("--seed", required=True)
    export = actions.add_parser("export-training"); export.add_argument("--corpus-id", required=True); export.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)
    state = args.state.expanduser().resolve()
    if args.action == "init-corpus":
        CorpusStore(state / "corpora", args.corpus_id).create(corpus_kind=args.kind, purpose=args.purpose); return 0
    corpus = CorpusStore(state / "corpora", args.corpus_id)
    if args.action == "import-descriptors":
        raw = json.loads(args.json.read_text(encoding="utf-8")); descriptors = [AssetDescriptor(**item) for item in raw]
        print(corpus.import_descriptors(descriptors)); return 0
    if args.action == "import-model-results":
        print(corpus.import_model_results(json.loads(args.json.read_text(encoding="utf-8")))); return 0
    if args.action == "freeze-gate3":
        frozen = corpus.freeze_gate3(FrozenCanaryStore(state / "canaries"), canary_id=args.canary_id, seed=args.seed)
        print(frozen.manifest_sha256); return 0
    print(export_training_jsonl(TruthStore(state / "truth" / args.corpus_id).all(), args.out)); return 0


if __name__ == "__main__":
    raise SystemExit(main())
