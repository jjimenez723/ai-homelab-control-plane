#!/usr/bin/env python3
"""Local-first social-video ingestion with durable, non-executable artifacts."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qs, urlparse

try:
    import yaml
except ImportError as exc:  # pragma: no cover - exercised by the CLI environment
    raise SystemExit("PyYAML is required; run `uv pip install -r requirements-social-video.txt`") from exc


PROCESSING_VERSION = "social-video-learn-v0.2"
VTS_COMMIT = "c527aa9b64cdb7e39a9d20dae6218c7cfc513473"
VTS_VERSION = "0.2.4"
# Candidate skills are untrusted, video-derived text. They must never live under the
# trusted `skills/` tree, because agent harnesses load every SKILL.md they find there.
# They also should not accumulate inside the repository working tree: `knowledge/`
# is gitignored, so nothing leaked into Git, but a relative path meant the write
# target was "wherever this happened to be invoked from". The home is now the
# UUID-verified volume (docs/25-angel-volume-layout.md), with a Mac-local
# gitignored fallback so the pipeline still runs with Angel unplugged.
ANGEL_CANDIDATES_RELATIVE = Path("40-ai/skills/candidates")
# Gitignored (`.state/`), outside `skills/`, and outside `knowledge/`.
DEFAULT_CANDIDATES_ROOT = Path(".state/social-video/skill-candidates")
INSTAGRAM_ID = re.compile(r"(?:instagram\.com)/(?:reel|p)/([A-Za-z0-9_-]+)", re.I)
YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be", "www.youtu.be"}
YOUTUBE_ID = re.compile(r"^[A-Za-z0-9_-]{6,}$")
UNSAFE_INPUT = re.compile(
    r"(?i)(ignore (?:all )?previous instructions|print (?:the )?(?:environment|api keys?|credentials)|"
    r"upload (?:the|your) files?|send (?:me )?(?:the )?(?:api keys?|credentials)|disable security|"
    r"rm -rf|curl[^\n|]*\|\s*(?:sh|bash)|wget[^\n|]*\|\s*(?:sh|bash)|"
    r"(?:passwords?|api keys?|credentials?)\s+(?:and\s+)?(?:keys?\s+)?(?:in|inside)\s+(?:one\s*)?\.env|"
    r"transcribe the reasoning verbatim|thinking-copy|encrypted thought injection)"
)
PROCEDURE_WORDS = re.compile(
    r"(?i)\b(?:how to|tutorial|step(?:s)?|install|setup|set up|configure|workflow|demo|"
    r"click|run|create|build|deploy|open|select|command|recipe)\b"
)
PROCEDURE_SEQUENCE = re.compile(r"(?i)\b(?:step\s+\d+|first(?:ly)?|next|then|finally|click\s+\w+|run\s+\w+|install\s+\w+|configure\s+\w+|select\s+\w+)\b")
TRADING_WORDS = re.compile(r"(?i)\b(?:stock|stocks|options?|trading|alpha|portfolio|forecast|backtest|Kronos)\b")


class PipelineError(RuntimeError):
    """An expected, user-actionable pipeline failure."""


def normalize_url(value: str) -> dict[str, str]:
    """Return platform, stable content ID, and canonical URL for a supported URL."""
    parsed = urlparse(value.strip())
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("not a supported HTTP(S) video URL")

    match = INSTAGRAM_ID.search(parsed.netloc + parsed.path)
    if match:
        content_id = match.group(1)
        return {
            "platform": "instagram",
            "content_id": content_id,
            "canonical_url": f"https://www.instagram.com/reel/{content_id}/",
        }

    host = parsed.netloc.lower().split(":", 1)[0]
    if host not in YOUTUBE_HOSTS:
        raise ValueError("URL must be an Instagram Reel, YouTube video, or local video file")
    if host.endswith("youtu.be"):
        content_id = parsed.path.strip("/").split("/", 1)[0]
    elif parsed.path == "/watch":
        content_id = parse_qs(parsed.query).get("v", [""])[0]
    else:
        parts = [part for part in parsed.path.split("/") if part]
        content_id = parts[1] if len(parts) > 1 and parts[0] in {"shorts", "embed", "v"} else ""
    if not YOUTUBE_ID.match(content_id):
        raise ValueError("could not extract a YouTube video ID")
    return {
        "platform": "youtube",
        "content_id": content_id,
        "canonical_url": f"https://www.youtube.com/watch?v={content_id}",
    }


def normalize_input(value: str) -> dict[str, str]:
    """Normalize a URL or local video file into a stable source identity."""
    if value.startswith(("http://", "https://")):
        return normalize_url(value)
    path = Path(value).expanduser().resolve()
    if not path.is_file():
        raise ValueError(f"local video file not found: {path}")
    digest = sha256_file(path)
    return {
        "platform": "local",
        "content_id": f"sha256-{digest[:16]}",
        "canonical_url": path.as_uri(),
        "local_path": str(path),
    }


def safe_component(value: str, fallback: str = "video") -> str:
    """Make an ID/title safe for a single output path component."""
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip()).strip(".-_")
    return (cleaned[:80] or fallback)


def output_path(root: Path, platform: str, content_id: str) -> Path:
    return root / safe_component(platform) / safe_component(content_id)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run_command(args: list[str], *, cwd: Path | None = None, timeout: int | None = None) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            args,
            cwd=cwd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except (FileNotFoundError, PermissionError) as exc:
        # A missing tool is an expected, reportable failure: the queue must mark the
        # item blocked and continue, not abort the batch with a traceback.
        raise PipelineError(f"required tool not available: {args[0]} ({exc.strerror})") from exc


def metadata_for(source: dict[str, str]) -> dict[str, Any]:
    if source["platform"] == "local":
        return {}
    result = run_command(
        [
            "yt-dlp",
            "--no-playlist",
            "--skip-download",
            "--dump-single-json",
            "--no-warnings",
            source["canonical_url"],
        ],
        timeout=90,
    )
    if result.returncode != 0:
        reason = result.stderr.strip().splitlines()[-1] if result.stderr.strip() else "yt-dlp metadata lookup failed"
        raise PipelineError(f"anonymous yt-dlp metadata access blocked: {reason}")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise PipelineError("yt-dlp returned invalid metadata JSON") from exc


def load_queue(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        document = yaml.safe_load(handle) or {}
    if not isinstance(document.get("items"), list):
        raise PipelineError(f"queue has no items list: {path}")
    return document


def unique_queue_items(items: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    unique: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for item in items:
        key = (str(item.get("platform", "")), str(item.get("content_id", "")))
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique


def update_queue_item(path: Path, content_id: str, status: str, reason: str | None = None) -> None:
    """Update one queue item while preserving the hand-authored YAML layout."""
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    start = next((i for i, line in enumerate(lines) if re.search(rf"\bcontent_id:\s*{re.escape(content_id)}\s*$", line)), None)
    if start is None:
        raise PipelineError(f"queue item not found: {content_id}")
    block_start = start
    while block_start >= 0 and not re.match(r"\s*-\s+platform:", lines[block_start]):
        block_start -= 1
    block_end = len(lines)
    for i in range(start + 1, len(lines)):
        if re.match(r"\s*-\s+platform:", lines[i]):
            block_end = i
            break
    block = lines[block_start:block_end]
    status_index = next((i for i, line in enumerate(block) if re.match(r"\s+status:\s*", line)), None)
    if status_index is None:
        block.insert(1, f"    status: {status}\n")
    else:
        indent = re.match(r"\s*", block[status_index]).group(0)
        block[status_index] = f"{indent}status: {status}\n"
    block = [line for line in block if not re.match(r"\s+failure_reason:\s*", line)]
    if reason:
        insert_at = next((i for i, line in enumerate(block) if re.match(r"\s+status:\s*", line)), len(block) - 1) + 1
        block.insert(insert_at, f"    failure_reason: {json.dumps(reason)}\n")
    lines[block_start:block_end] = block
    path.write_text("".join(lines), encoding="utf-8")


def timestamp(seconds: float) -> str:
    total = max(0, int(seconds))
    return f"{total // 60}:{total % 60:02d}"


def read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def transcript_text(transcript: dict[str, Any]) -> str:
    return "\n".join(str(segment.get("text", "")).strip() for segment in transcript.get("segments", []) if segment.get("text"))


def sentences(text: str) -> list[str]:
    return [part.strip() for part in re.split(r"(?<=[.!?])\s+|\n+", text) if len(part.strip()) >= 12]


def classify(text: str) -> str:
    unsafe = bool(UNSAFE_INPUT.search(text))
    sequence_signals = PROCEDURE_SEQUENCE.findall(text)
    ordered = bool(re.search(r"(?i)\b(?:step\s+\d+|first(?:ly)?|finally)\b", text))
    procedural = bool(re.search(r"(?i)\bstep\s+\d+", text)) or (ordered and len(sequence_signals) >= 3 and len(PROCEDURE_WORDS.findall(text)) >= 4)
    informational = len(sentences(text)) >= 2
    if unsafe:
        return "unsafe/untrusted"
    if procedural and informational:
        return "mixed"
    if procedural:
        return "procedure"
    if informational:
        return "knowledge"
    return "low-value"


def run_ocr(frames: list[Path]) -> list[dict[str, str]]:
    if shutil.which("tesseract") is None:
        return []
    found: list[dict[str, str]] = []
    seen: set[str] = set()
    for frame in frames:
        result = run_command(["tesseract", str(frame), "stdout", "--psm", "6"], timeout=45)
        text = re.sub(r"\s+", " ", result.stdout).strip()
        if result.returncode == 0 and len(text) >= 3 and text not in seen:
            seen.add(text)
            found.append({"frame": frame.name, "text": text})
    return found


def write_transcript(path: Path, transcript: dict[str, Any]) -> None:
    lines = ["# Transcript", "", f"Language: `{transcript.get('language') or 'unknown'}`", ""]
    segments = transcript.get("segments", [])
    if not segments:
        lines.append("No speech transcript was produced by the local Whisper runtime.")
    else:
        for segment in segments:
            lines.append(f"- `{timestamp(float(segment.get('start_secs', 0)))}–{timestamp(float(segment.get('end_secs', 0)))}` {str(segment.get('text', '')).strip()}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_knowledge_artifacts(
    artifact: Path,
    source: dict[str, str],
    metadata: dict[str, Any],
    manifest: dict[str, Any],
    transcript: dict[str, Any],
    visual: list[dict[str, str]],
) -> str:
    artifact.mkdir(parents=True, exist_ok=True)
    frames_dir = artifact / "frames"
    frames_dir.mkdir(exist_ok=True)
    extraction_frames = Path(manifest.get("_extraction_dir", "")) / "frames"
    copied_frames: list[Path] = []
    if extraction_frames.is_dir():
        for frame in sorted(extraction_frames.glob("*.jpg")):
            if "native" in frame.name:
                continue
            destination = frames_dir / frame.name
            shutil.copy2(frame, destination)
            copied_frames.append(destination)

    text = transcript_text(transcript)
    caption = str(metadata.get("description") or metadata.get("title") or "").strip()
    combined = "\n".join(part for part in [text, caption, " ".join(entry["text"] for entry in visual)] if part)
    content_type = classify(combined)
    title = str(metadata.get("title") or manifest.get("source", {}).get("title") or source["content_id"])
    acquisition = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()
    media_sha = str(manifest.get("source", {}).get("sha256") or "")
    source_record = {
        "original_url": source.get("original_url", source.get("canonical_url")),
        "canonical_url": source["canonical_url"],
        "platform": source["platform"],
        "content_id": source["content_id"],
        "creator": metadata.get("uploader") or metadata.get("channel") or metadata.get("uploader_id"),
        "title": title,
        "caption": metadata.get("description"),
        "upload_date": metadata.get("upload_date"),
        "acquisition_timestamp": acquisition,
        "downloaded_media_sha256": media_sha or None,
        "transcript_runtime": "whisper-rs via video-to-skill v0.2.4",
        "transcript_model": "ggml-base (multilingual)",
        "processing_version": PROCESSING_VERSION,
        "video_to_skill_commit": VTS_COMMIT,
    }
    (artifact / "source.json").write_text(json.dumps(source_record, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    write_transcript(artifact / "transcript.md", transcript)
    visual_record = {
        "method": "representative keyframes from vts-extract plus local tesseract OCR",
        "frames": [entry["name"] for entry in [{"name": p.name} for p in copied_frames]],
        "on_screen_text": visual,
        "manual_vlm_review": "not run; frame files are preserved for later stronger-model inspection",
    }
    (artifact / "visual.json").write_text(json.dumps(visual_record, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    key_claims = sentences(text)[:12]
    claims = [{"claim": claim, "evidence": "transcript", "confidence": "medium"} for claim in key_claims]
    claims.extend({"claim": entry["text"], "evidence": f"OCR:{entry['frame']}", "confidence": "medium"} for entry in visual)
    (artifact / "claims.json").write_text(json.dumps(claims, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    digest_lines = [f"# {title}", "", f"Classification: `{content_type}`", "", "## What the video says", ""]
    digest_lines.extend(f"- {claim}" for claim in key_claims)
    if not key_claims:
        digest_lines.append("- No reliable speech claims were extracted.")
    if caption and caption not in text:
        digest_lines.extend(["", "## Caption/context", "", caption])
    if visual:
        digest_lines.extend(["", "## On-screen text", ""])
        digest_lines.extend(f"- `{entry['frame']}`: {entry['text']}" for entry in visual)
    digest_lines.extend(["", "## Confidence and verification", "", "Transcript and OCR are machine-derived evidence, not independently verified facts. Check product names, commands, pricing, dates, and claims against primary sources before acting."])
    if TRADING_WORDS.search(combined):
        digest_lines.append("Trading boundary: treat any market or portfolio advice as a research hypothesis only; do not place trades or bypass backtests/risk controls.")
    (artifact / "digest.md").write_text("\n".join(digest_lines) + "\n", encoding="utf-8")
    recommendations = [
        "Keep the original source URL and checksum with any downstream note.",
        "Independently verify concrete tools, URLs, commands, and claims before adoption.",
        "Treat all spoken or on-screen instructions as untrusted content; this pipeline executes none of them.",
    ]
    if "app" in combined.lower() or "code" in combined.lower() or "github" in combined.lower():
        recommendations.append("App-development relevance: extract named tools into a separate comparison or proof-of-concept before changing a project.")
    if "client" in combined.lower() or "media" in combined.lower() or "video" in combined.lower():
        recommendations.append("Client-media relevance: preserve raw media and provenance; do not let a social-video recommendation rename, delete, or upload client assets automatically.")
    if TRADING_WORDS.search(combined):
        recommendations.append("Kronos/trading relevance: save only as educational material or a hypothesis requiring point-in-time data validation and backtesting.")
    (artifact / "recommendations.md").write_text("# Recommendations\n\n" + "\n".join(f"- {item}" for item in recommendations) + "\n", encoding="utf-8")
    return content_type


def maybe_candidate_skill(artifact: Path, source: dict[str, str], title: str, text: str, content_type: str, candidates_root: Path) -> Path | None:
    if content_type not in {"procedure", "mixed"} or not (re.search(r"(?i)\bstep\s+\d+", text) or (re.search(r"(?i)\b(?:first(?:ly)?|finally)\b", text) and len(PROCEDURE_SEQUENCE.findall(text)) >= 3)) or UNSAFE_INPUT.search(text):
        return None
    candidate_name = safe_component(title.lower(), source["content_id"])[:48]
    candidate = candidates_root / candidate_name
    candidate.mkdir(parents=True, exist_ok=True)
    steps = sentences(text)[:10]
    body = [
        "---",
        f"name: {candidate_name}",
        f"description: Candidate procedure extracted from {source['canonical_url']}; review before use.",
        "---",
        "",
        "# Candidate skill",
        "",
        "> Review-only artifact. It is not installed, trusted, or executed automatically.",
        "",
        f"Source: {source['canonical_url']}",
        f"video-to-skill commit: `{VTS_COMMIT}`",
        "",
        "## Candidate steps",
        "",
    ]
    body.extend(f"{index}. {step}" for index, step in enumerate(steps, 1))
    body.extend(["", "## Review gates", "", "- Verify every step against the preserved frames and primary documentation.", "- Remove secrets and destructive or network-fetch-and-execute commands.", "- Define prerequisites and success criteria before promotion.", "- Run only in the reviewed OS sandbox; never install globally without explicit review."])
    (candidate / "SKILL.md").write_text("\n".join(body) + "\n", encoding="utf-8")
    return candidate


def extractor_provenance(vts_bin: str) -> dict[str, str | None]:
    """Record which extractor binary actually ran; the lock file pins source, not the build."""
    path = Path(vts_bin)
    return {
        "vts_binary_path": str(path),
        "vts_binary_sha256": sha256_file(path) if path.is_file() else None,
    }


def process_one(
    value: str,
    *,
    knowledge_root: Path,
    work_root: Path,
    candidates_root: Path,
    vts_bin: str,
    force: bool = False,
) -> tuple[Path, str, Path | None]:
    source = normalize_input(value)
    source["original_url"] = value
    artifact = output_path(knowledge_root, source["platform"], source["content_id"])
    existing = artifact / "source.json"
    if existing.is_file() and not (force or os.environ.get("SOCIAL_VIDEO_FORCE")):
        record = read_json(existing, {})
        return artifact, str(record.get("classification", "already-processed")), None
    metadata = metadata_for(source)
    work_root.mkdir(parents=True, exist_ok=True)
    run_dir = Path(tempfile.mkdtemp(prefix=f"{safe_component(source['content_id'])}-", dir=work_root))
    try:
        extraction = run_dir / "extraction"
        # Hand the extractor the normalized identity, not the raw user string.
        extract_input = source.get("local_path", source["canonical_url"])
        result = run_command([vts_bin, "extract", extract_input, "--out", str(extraction)], timeout=1800)
        if result.returncode != 0:
            reason = result.stderr.strip().splitlines()[-1] if result.stderr.strip() else result.stdout.strip().splitlines()[-1] if result.stdout.strip() else "vts-extract failed"
            raise PipelineError(reason)
        manifest = read_json(extraction / "manifest.json", {})
        manifest["_extraction_dir"] = str(extraction)
        transcript = read_json(extraction / "transcript.json", {"language": None, "segments": []})
        frames = sorted((extraction / "frames").glob("*.jpg"))
        visual = run_ocr([frame for frame in frames if "native" not in frame.name])
        content_type = write_knowledge_artifacts(artifact, source, metadata, manifest, transcript, visual)
        title = str(metadata.get("title") or source["content_id"])
        candidate = maybe_candidate_skill(artifact, source, title, transcript_text(transcript), content_type, candidates_root)
        source_record = read_json(artifact / "source.json", {})
        source_record["classification"] = content_type
        source_record["candidate_skill"] = str(candidate) if candidate else None
        source_record["trust"] = "untrusted-video-derived"
        source_record.update(extractor_provenance(vts_bin))
        (artifact / "source.json").write_text(json.dumps(source_record, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        return artifact, content_type, candidate
    finally:
        shutil.rmtree(run_dir, ignore_errors=True)


def default_vts_bin() -> str:
    default = Path.home() / ".cache" / "ai-homelab-control-plane" / "video-to-skill" / VTS_COMMIT / "vts-extract"
    return os.environ.get("VTS_EXTRACT_BIN", str(default))


def resolve_candidates_root(explicit: Path | None = None) -> Path:
    """Where untrusted candidate skills are written.

    Order: an explicit `--candidates-root`; then Angel
    `40-ai/skills/candidates/` if the configured volume UUID is mounted and
    writable; then the gitignored Mac-local fallback.

    The volume resolution is deliberately best-effort. Unlike model weights
    (D-037), a candidate skill is small and the pipeline is worth running
    with the drive unplugged -- and the fallback is still outside `skills/`
    and outside Git, so the trust boundary holds either way. What is *not*
    optional is that neither destination is ever the trusted tree.
    """
    if explicit is not None:
        return explicit
    try:
        from tools.ingest.config import load_config
        from tools.ingest.t7 import resolve_t7
        from tools.ingest.volumes import diskutil_volume_prober

        resolved = resolve_t7(load_config(None), diskutil_volume_prober, for_apply=False)
        if resolved.volume_uuid:
            return resolved.root / ANGEL_CANDIDATES_RELATIVE
    except Exception:
        # An absent, locked, misconfigured, or unreadable volume is a normal
        # condition here, not an error: fall back to the Mac-local path.
        pass
    return DEFAULT_CANDIDATES_ROOT


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", help="Instagram Reel URL, YouTube URL, or local video file")
    parser.add_argument("--force", action="store_true", help="reprocess even if a completed artifact exists")
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
    args = parse_args(argv if argv is not None else sys.argv[1:])
    artifact, content_type, candidate = process_one(
        args.input,
        knowledge_root=args.knowledge_root,
        work_root=args.work_root,
        candidates_root=resolve_candidates_root(args.candidates_root),
        vts_bin=args.vts_bin,
        force=args.force,
    )
    print(f"{content_type}: {artifact}")
    if candidate:
        print(f"candidate skill: {candidate}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
