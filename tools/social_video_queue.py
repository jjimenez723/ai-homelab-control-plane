#!/usr/bin/env python3
"""Platform-aware queue runner for the social-video learning pipeline."""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path
from typing import Any

try:
    from tools.social_video_learn import (
        DEFAULT_CANDIDATES_ROOT,
        PipelineError,
        default_vts_bin,
        load_queue,
        output_path,
        process_one,
        resolve_candidates_root,
        safe_component,
        unique_queue_items,
        update_queue_item,
    )
except ImportError:  # direct script execution from tools/
    from social_video_learn import (  # type: ignore
        DEFAULT_CANDIDATES_ROOT,
        PipelineError,
        default_vts_bin,
        load_queue,
        output_path,
        process_one,
        resolve_candidates_root,
        safe_component,
        unique_queue_items,
        update_queue_item,
    )


def process_queue(queue_path: Path, **kwargs: Any) -> list[tuple[str, str, str]]:
    document = load_queue(queue_path)
    outcomes: list[tuple[str, str, str]] = []

    for item in unique_queue_items(document["items"]):
        platform = safe_component(str(item.get("platform", "unknown")).strip().lower(), "unknown")
        content_id = str(item["content_id"])
        value = str(item["url"])
        completed_marker = output_path(
            kwargs["knowledge_root"], platform, content_id
        ) / "source.json"

        if str(item.get("status")) == "complete" and completed_marker.is_file() and not kwargs.get("force"):
            outcomes.append((content_id, "skipped", "already complete"))
            continue

        update_queue_item(queue_path, content_id, "processing")
        try:
            artifact, content_type, candidate = process_one(value, **kwargs)
            update_queue_item(queue_path, content_id, "complete")
            detail = f"{content_type}; {artifact}"
            if candidate:
                detail += f"; candidate {candidate}"
            outcomes.append((content_id, "complete", detail))
        except (PipelineError, ValueError, subprocess.TimeoutExpired) as exc:
            reason = str(exc)[:500]
            update_queue_item(queue_path, content_id, "blocked", reason)
            outcomes.append((content_id, "blocked", reason))

    return outcomes


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--queue", type=Path, required=True)
    parser.add_argument("--force", action="store_true", help="reprocess items even if a completed artifact exists")
    parser.add_argument("--knowledge-root", type=Path, default=Path("knowledge/social-video"))
    parser.add_argument("--work-root", type=Path, default=Path("work/social-video"))
    parser.add_argument(
        "--candidates-root",
        type=Path,
        default=None,
        help="Where untrusted candidate skills go. Default: Angel 40-ai/skills/candidates/ "
             f"when the configured volume is mounted, else {DEFAULT_CANDIDATES_ROOT}.",
    )
    parser.add_argument("--vts-bin", default=default_vts_bin())
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    outcomes = process_queue(
        args.queue,
        knowledge_root=args.knowledge_root,
        work_root=args.work_root,
        candidates_root=resolve_candidates_root(args.candidates_root),
        vts_bin=args.vts_bin,
        force=args.force,
    )
    for content_id, status, detail in outcomes:
        print(f"{content_id}: {status}: {detail}")
    return 0 if all(status in {"complete", "skipped"} for _, status, _ in outcomes) else 2


if __name__ == "__main__":
    raise SystemExit(main())
