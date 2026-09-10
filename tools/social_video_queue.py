#!/usr/bin/env python3
"""Platform-aware queue runner for the social-video learning pipeline."""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path
from typing import Any

try:
    from tools.social_video_learn import (
        PipelineError,
        load_queue,
        output_path,
        process_one,
        safe_component,
        unique_queue_items,
        update_queue_item,
    )
except ImportError:  # direct script execution from tools/
    from social_video_learn import (  # type: ignore
        PipelineError,
        load_queue,
        output_path,
        process_one,
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

        if str(item.get("status")) == "complete" and completed_marker.is_file():
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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--queue", type=Path, required=True)
    parser.add_argument("--knowledge-root", type=Path, default=Path("knowledge/social-video"))
    parser.add_argument("--work-root", type=Path, default=Path("work/social-video"))
    parser.add_argument("--candidates-root", type=Path, default=Path("skills/candidates"))
    parser.add_argument("--vts-bin", default=None)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    kwargs: dict[str, Any] = {
        "knowledge_root": args.knowledge_root,
        "work_root": args.work_root,
        "candidates_root": args.candidates_root,
    }
    if args.vts_bin:
        kwargs["vts_bin"] = args.vts_bin
    else:
        from pathlib import Path as _Path
        try:
            from tools.social_video_learn import VTS_COMMIT
        except ImportError:
            from social_video_learn import VTS_COMMIT  # type: ignore
        kwargs["vts_bin"] = str(
            _Path.home()
            / ".cache"
            / "ai-homelab-control-plane"
            / "video-to-skill"
            / VTS_COMMIT
            / "vts-extract"
        )

    outcomes = process_queue(args.queue, **kwargs)
    for content_id, status, detail in outcomes:
        print(f"{content_id}: {status}: {detail}")
    return 0 if all(status in {"complete", "skipped"} for _, status, _ in outcomes) else 2


if __name__ == "__main__":
    raise SystemExit(main())
