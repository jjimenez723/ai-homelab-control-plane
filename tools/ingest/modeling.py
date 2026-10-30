"""Safe, proposal-only media analysis backed by a local Ollama model.

The model layer is deliberately downstream of verified ingestion. It can write
catalog proposals and profile observations, but it never moves, renames, or
deletes source files. Human corrections are stored separately and are included
in future prompts as durable profile memory.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import io
import json
import shutil
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tools.ingest.catalog import Asset, Catalog
from tools.ingest.config import AIConfig, _is_loopback_url
from tools.ingest.archive_profile import (
    EMPTY_ARCHIVE_PROFILE,
    ArchiveProfile,
    normalize_archive_category,
)


SYSTEM_PROMPT = """You are Angel's local media-analysis component.

Analyze the supplied image or representative video frames and return only one valid JSON object. You propose
metadata; you do not perform file operations. Never identify an unknown person
by real-world identity. Only assign a person_profile_id from the supplied,
human-maintained profiles. If the person is not a clear match, use null.

Keep person matching separate from duplicate detection: a duplicate image is
not evidence that two people are the same. Do not invent profile IDs, dates,
clients, or facts that are not visible or present in the input context.

Required JSON shape:
{
  "summary": "short factual description",
  "description": "factual description of what is shown",
  "capture_date": "YYYY-MM-DD or null; use supplied camera/context evidence only",
  "archive_category": "one category from the supplied archive vocabulary or null",
  "archive_nested_path": [],
  "archive_confidence": 0.0,
  "archive_evidence": "brief visible/context evidence",
  "keywords": ["lowercase", "searchable", "keywords"],
  "tags": ["short", "lowercase", "tags"],
  "people": [
    {"person_profile_id": "known-id-or-null", "confidence": 0.0, "evidence": "brief visual evidence"}
  ],
  "animals": [
    {"animal_type": "dog", "count": 1, "confidence": 0.0, "evidence": "brief visual evidence"}
  ],
  "filename_proposal": {
    "subject": "short descriptive subject or null",
    "variant": "wide, portrait, detail, etc. or null",
    "confidence": 0.0
  }
}

Confidence is calibrated conservatively. Use 0.95 only for an obvious match;
use null or a low confidence when the image is ambiguous.
Return one people entry per visible person even when no known profile matches.
For animals, return one entry per visible animal type and use count for multiple
animals of the same type. Use an empty list when none are visible.

Choose archive_category only from the exact supplied archive vocabulary or
return null. When no vocabulary is supplied, always return null. Do not output
a prefix, invent a category, folder, or folder ID. The deterministic resolver,
not the model, chooses the prefix, Pics/Vids route, and destination folder.
Unknown or low-confidence categories remain review.

Filename proposals must be useful for searching later. Make `subject` a specific
2-6 word lowercase phrase describing the central visible subject, action, or
object; include species, body part, procedure, or distinctive equipment when
clearly visible. Do not use generic subjects such as "photo", "image", "scene",
"medical", or "surgery" by themselves. Do not invent details to make a name
sound specific. Use `variant` only for a visible framing or sequence distinction
such as "detail", "wide", "portrait", "front", "side", "before", or "after";
otherwise use null. Never include a file extension or path.
"""


class ModelError(RuntimeError):
    """A model request or response could not be used safely."""


@dataclass(frozen=True)
class ModelResponse:
    payload: dict[str, Any]
    model: str
    prompt_sha: str


@dataclass(frozen=True)
class AnalysisResult:
    asset_id: str
    model: str | None
    model_version: str
    prompt_sha: str
    proposal_ids: tuple[int, ...]
    exact_duplicate_ids: tuple[str, ...]
    people_matched: int
    people_detected: int = 0
    animals_detected: int = 0


@dataclass(frozen=True)
class PreflightAnalysis:
    """Proposal-only analysis for a source file that is not on T7 yet.

    The source remains untouched. The CLI persists the returned metadata in
    the preflight catalog, but this object itself has no file-operation power.
    """

    source_path: str
    payload: dict[str, Any]
    model: str = ""
    prompt_sha: str = ""


def _clamp_confidence(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, min(1.0, number))


def _parse_json_object(content: str) -> dict[str, Any]:
    cleaned = content.strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        cleaned = "\n".join(lines).strip()
    try:
        value = json.loads(cleaned)
    except json.JSONDecodeError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start < 0 or end <= start:
            raise ModelError("model response was not a JSON object")
        try:
            value = json.loads(cleaned[start : end + 1])
        except json.JSONDecodeError as exc:
            raise ModelError("model response contained invalid JSON") from exc
    if not isinstance(value, dict):
        raise ModelError("model response must be a JSON object")
    return value


def normalize_payload(
    payload: dict[str, Any],
    archive_profile: ArchiveProfile = EMPTY_ARCHIVE_PROFILE,
) -> dict[str, Any]:
    """Keep only the small, stable schema that downstream code understands."""
    people: list[dict[str, Any]] = []
    raw_people = payload.get("people") if isinstance(payload.get("people"), list) else []
    for raw in raw_people:
        if not isinstance(raw, dict):
            continue
        profile_id = raw.get("person_profile_id")
        people.append(
            {
                "person_profile_id": str(profile_id).strip() if profile_id else None,
                "confidence": _clamp_confidence(raw.get("confidence")),
                "evidence": str(raw.get("evidence") or "").strip()[:500],
            }
        )
    animals: list[dict[str, Any]] = []
    raw_animals = payload.get("animals") if isinstance(payload.get("animals"), list) else []
    for raw in raw_animals:
        if not isinstance(raw, dict):
            continue
        animal_type = raw.get("animal_type") or raw.get("species") or raw.get("type") or raw.get("kind")
        animal_type = str(animal_type or "").strip().lower()[:80]
        if not animal_type:
            continue
        try:
            count = int(raw.get("count", 1))
        except (TypeError, ValueError):
            count = 1
        animals.append(
            {
                "animal_type": animal_type,
                "count": max(1, min(50, count)),
                "confidence": _clamp_confidence(raw.get("confidence")),
                "evidence": str(raw.get("evidence") or "").strip()[:500],
            }
        )
    raw_filename = payload.get("filename_proposal")
    filename = raw_filename if isinstance(raw_filename, dict) else {}
    tags = payload.get("tags") if isinstance(payload.get("tags"), list) else []
    summary = str(payload.get("summary") or "").strip()[:1000]
    description = str(payload.get("description") or summary).strip()[:2000]
    capture_date_raw = str(payload.get("capture_date") or "").strip()
    try:
        capture_date = dt.date.fromisoformat(capture_date_raw).isoformat() if capture_date_raw else None
    except ValueError:
        capture_date = None
    return {
        "summary": summary,
        "description": description,
        "capture_date": capture_date,
        "archive_category": normalize_archive_category(payload.get("archive_category"), archive_profile),
        "archive_nested_path": [
            str(part).strip()[:160]
            for part in (payload.get("archive_nested_path") if isinstance(payload.get("archive_nested_path"), list) else [])
            if str(part).strip()
        ][:4],
        "archive_confidence": _clamp_confidence(payload.get("archive_confidence")),
        "archive_evidence": str(payload.get("archive_evidence") or "").strip()[:500],
        "keywords": sorted({str(tag).strip().lower() for tag in (payload.get("keywords") or []) if str(tag).strip()})[:40],
        "tags": sorted({str(tag).strip().lower() for tag in tags if str(tag).strip()})[:30],
        "people": people[:20],
        "animals": animals[:20],
        "filename_proposal": {
            "subject": str(filename.get("subject") or "").strip()[:120] or None,
            "variant": str(filename.get("variant") or "").strip()[:80] or None,
            "confidence": _clamp_confidence(filename.get("confidence")),
        },
    }


class OllamaClient:
    """Small dependency-free client for Ollama's native chat API."""

    def __init__(self, config: AIConfig, archive_profile: ArchiveProfile = EMPTY_ARCHIVE_PROFILE):
        if not _is_loopback_url(config.base_url):
            raise ModelError(
                "Ollama endpoint must target a loopback address; remote/cloud model routing is not implemented"
            )
        self.config = config
        self.archive_profile = archive_profile

    @staticmethod
    def _executable(name: str) -> str | None:
        """Find a media tool in PATH, with the usual Homebrew fallbacks."""
        found = shutil.which(name)
        if found:
            return found
        for candidate in (f"/opt/homebrew/bin/{name}", f"/usr/local/bin/{name}"):
            if Path(candidate).is_file():
                return candidate
        return None

    @classmethod
    def _video_frame_bytes(cls, video_path: Path, sample_count: int) -> list[bytes]:
        ffmpeg = cls._executable("ffmpeg")
        ffprobe = cls._executable("ffprobe")
        if ffmpeg is None or ffprobe is None:
            raise ModelError("video analysis requires ffmpeg and ffprobe")

        duration = 0.0
        try:
            probe = subprocess.run(
                [
                    ffprobe,
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration",
                    "-of",
                    "default=noprint_wrappers=1:nokey=1",
                    str(video_path),
                ],
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
            duration = float(probe.stdout.strip() or 0.0)
        except (OSError, ValueError, subprocess.SubprocessError):
            # Some camera containers have incomplete duration metadata. A
            # start-frame fallback still gives the model useful evidence.
            duration = 0.0

        if duration > 0:
            # Avoid the first/last transition frames and sample evenly across
            # the clip so a short action sequence is represented fairly.
            positions = [duration * (index + 0.5) / sample_count for index in range(sample_count)]
        else:
            positions = [0.0]

        frames: list[bytes] = []
        for position in positions:
            try:
                extracted = subprocess.run(
                    [
                        ffmpeg,
                        "-hide_banner",
                        "-loglevel",
                        "error",
                        "-ss",
                        f"{position:.3f}",
                        "-i",
                        str(video_path),
                        "-frames:v",
                        "1",
                        "-vf",
                        "scale=1600:1600:force_original_aspect_ratio=decrease",
                        "-f",
                        "image2pipe",
                        "-vcodec",
                        "mjpeg",
                        "pipe:1",
                    ],
                    capture_output=True,
                    check=False,
                    timeout=60,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                raise ModelError(f"could not extract a frame from {video_path}: {exc}") from exc
            if extracted.returncode == 0 and extracted.stdout:
                frames.append(extracted.stdout)
        if not frames:
            raise ModelError(f"could not extract any frames from {video_path}")
        return frames

    def _proxy_bytes(self, media_path: Path) -> bytes:
        """Create a bounded JPEG proxy/contact sheet; never send originals."""
        try:
            from PIL import Image
        except ImportError as exc:  # pragma: no cover - dependency setup issue
            raise ModelError("Pillow is required to create a model-analysis proxy") from exc

        try:
            if media_path.suffix.lower() in {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm", ".mts", ".m2ts"}:
                frame_bytes = self._video_frame_bytes(media_path, self.config.video_sample_count)
                frames = [Image.open(io.BytesIO(raw)).convert("RGB") for raw in frame_bytes]
                try:
                    tile_width, tile_height = 800, 450
                    columns = 2 if len(frames) > 1 else 1
                    rows = (len(frames) + columns - 1) // columns
                    sheet = Image.new("RGB", (tile_width * columns, tile_height * rows), (24, 24, 24))
                    for index, frame in enumerate(frames):
                        frame.thumbnail((tile_width, tile_height))
                        left = (index % columns) * tile_width + (tile_width - frame.width) // 2
                        top = (index // columns) * tile_height + (tile_height - frame.height) // 2
                        sheet.paste(frame, (left, top))
                    output = io.BytesIO()
                    sheet.save(output, format="JPEG", quality=88, optimize=True)
                    return output.getvalue()
                finally:
                    for frame in frames:
                        frame.close()

            with Image.open(media_path) as image:
                image = image.convert("RGB")
                image.thumbnail((1600, 1600))
                output = io.BytesIO()
                image.save(output, format="JPEG", quality=88, optimize=True)
                return output.getvalue()
        except Exception as exc:  # noqa: BLE001 - normalize codec errors for the CLI
            if isinstance(exc, ModelError):
                raise
            raise ModelError(f"could not create a JPEG proxy for {media_path}: {exc}") from exc

    def _user_prompt(self, *, asset: Asset, profiles: list[dict[str, Any]]) -> str:
        profile_context = [
            {
                "profile_id": profile["profile_id"],
                "scope": profile.get("scope", "global"),
                "display_name": profile["display_name"],
                "aliases": profile.get("aliases", []),
                "notes": profile.get("notes", ""),
                "confirmed_observations": profile.get("observation_count", 0),
                "confirmed_examples": profile.get("confirmed_examples", [])[:12],
            }
            for profile in profiles
        ]
        archive_vocabulary = {
            category: {"keywords": list(entry.get("keywords", []))}
            for category, entry in self.archive_profile.categories.items()
        }
        archive_line = (
            f"Archive category vocabulary (choose exactly one of "
            f"{json.dumps(list(self.archive_profile.canonical_categories))} or null): "
            f"{json.dumps(archive_vocabulary, sort_keys=True)}\n"
            if archive_vocabulary
            else "Archive category vocabulary: none supplied; archive_category must be null.\n"
        )
        return (
            f"Asset ID: {asset.asset_id}\n"
            f"Original filename: {asset.original_filename}\n"
            f"{archive_line}"
            f"Known person profiles (human-maintained; empty means no known profiles): "
            f"{json.dumps(profile_context, sort_keys=True)}\n"
            "Detect people and animals independently of profile matching. Return the required JSON object."
        )

    def prompt_sha(self, *, asset: Asset, profiles: list[dict[str, Any]]) -> str:
        user_prompt = self._user_prompt(asset=asset, profiles=profiles)
        return hashlib.sha256((self.config.prompt_version + SYSTEM_PROMPT + user_prompt).encode("utf-8")).hexdigest()

    def _request_content(self, request: urllib.request.Request) -> str:
        try:
            with urllib.request.urlopen(request, timeout=self.config.timeout_seconds) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            raise ModelError(f"Ollama returned HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise ModelError(f"Ollama is unavailable at {self.config.base_url}: {exc.reason}") from exc
        try:
            envelope = json.loads(raw)
            if "message" in envelope:
                content = envelope["message"]["content"]
            else:
                content = envelope["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            raise ModelError("Ollama returned an unexpected response shape") from exc
        if isinstance(content, list):
            content = "".join(str(part.get("text", "")) if isinstance(part, dict) else str(part) for part in content)
        return str(content)

    def analyze_image(self, image_path: Path, *, asset: Asset, profiles: list[dict[str, Any]]) -> ModelResponse:
        if not image_path.is_file():
            raise ModelError(f"asset file not found: {image_path}")
        image_bytes = self._proxy_bytes(image_path)
        image_data = base64.b64encode(image_bytes).decode("ascii")
        user_prompt = self._user_prompt(asset=asset, profiles=profiles)
        prompt_sha = self.prompt_sha(asset=asset, profiles=profiles)
        body = {
            "model": self.config.model,
            "stream": False,
            # Qwen 3.5's hidden reasoning is useful for open-ended chat but
            # makes this bounded metadata task needlessly slow. The response
            # schema and visual evidence are already constrained by the prompt.
            "think": False,
            "format": "json",
            "options": {"temperature": self.config.temperature},
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": user_prompt,
                    "images": [image_data],
                },
            ],
        }
        request = urllib.request.Request(
            f"{self.config.base_url}/api/chat",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        content = self._request_content(request)
        try:
            payload = normalize_payload(_parse_json_object(content), self.archive_profile)
        except ModelError as first_error:
            # Small local VLMs occasionally ignore JSON mode. Retry only this
            # format failure so ordinary model/API failures remain bounded and
            # so a transient malformed answer does not discard the image.
            retry_body = dict(body)
            retry_body["messages"] = [
                body["messages"][0],
                {
                    "role": "user",
                    "content": (
                        user_prompt
                        + "\nYour previous answer was unusable. Return ONLY one JSON object: "
                        "start with { and end with }. Do not include markdown or explanation."
                    ),
                    "images": [image_data],
                },
            ]
            retry_request = urllib.request.Request(
                f"{self.config.base_url}/api/chat",
                data=json.dumps(retry_body).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                payload = normalize_payload(
                    _parse_json_object(self._request_content(retry_request)), self.archive_profile
                )
            except ModelError as retry_error:
                raise ModelError(f"{first_error}; retry failed: {retry_error}") from retry_error
        return ModelResponse(payload=payload, model=self.config.model, prompt_sha=prompt_sha)


class ModelOrchestrator:
    """Run analysis for one verified asset and persist proposal-only output."""

    def __init__(self, config: AIConfig, archive_profile: ArchiveProfile = EMPTY_ARCHIVE_PROFILE):
        self.config = config
        self.client = OllamaClient(config, archive_profile) if config.provider == "ollama" else None

    def analyze_source_image(
        self,
        image_path: Path,
        *,
        source_id: str,
        profiles: list[dict[str, Any]],
    ) -> PreflightAnalysis:
        if not self.config.enabled:
            raise ModelError("local model analysis is disabled in ingest.yaml")
        if self.client is None:
            raise ModelError(f"unsupported model provider: {self.config.provider}")
        if not image_path.is_file():
            raise ModelError(f"source file not found: {image_path}")
        asset = Asset({"asset_id": source_id, "original_filename": image_path.name})
        response = self.client.analyze_image(image_path, asset=asset, profiles=profiles)
        return PreflightAnalysis(
            source_path=str(image_path),
            payload=response.payload,
            model=response.model,
            prompt_sha=response.prompt_sha,
        )

    def analyze_asset(self, catalog: Catalog, asset: Asset) -> AnalysisResult:
        duplicate_ids = tuple(
            duplicate.asset_id
            for duplicate in catalog.find_assets_by_sha256(asset.sha256 or "", exclude_asset_id=asset.asset_id)
        ) if asset.sha256 else ()
        proposal_ids: list[int] = []

        if not self.config.enabled:
            if duplicate_ids:
                duplicate_value = json.dumps(duplicate_ids)
                if not catalog.has_proposal(asset.asset_id, field="exact_duplicate_of", value=duplicate_value):
                    proposal_ids.append(
                        catalog.record_proposal(
                            asset.asset_id,
                            field="exact_duplicate_of",
                            value=duplicate_value,
                            confidence=1.0,
                            model="deterministic-sha256",
                            model_version="sha256-v1",
                            prompt_sha="not-applicable",
                        )
                    )
            return AnalysisResult(asset.asset_id, None, self.config.prompt_version, "disabled", tuple(proposal_ids), duplicate_ids, 0)
        if self.client is None:
            raise ModelError(f"unsupported model provider: {self.config.provider}")
        if not asset.final_path:
            raise ModelError("asset has no verified final path")
        if not asset.sha256:
            raise ModelError("asset has no SHA-256; only verified assets can be analyzed")
        profiles = catalog.profile_context(scope=self.config.profile_scope)
        prompt_sha = self.client.prompt_sha(asset=asset, profiles=profiles)
        run_started = catalog.begin_analysis_run(
            asset_id=asset.asset_id,
            stage="media-analysis",
            input_sha256=asset.sha256,
            model=self.config.model,
            model_version=self.config.prompt_version,
            prompt_sha=prompt_sha,
        )
        if not run_started:
            return AnalysisResult(asset.asset_id, self.config.model, self.config.prompt_version, prompt_sha, (), duplicate_ids, 0)
        if duplicate_ids:
            duplicate_value = json.dumps(duplicate_ids)
            if not catalog.has_proposal(asset.asset_id, field="exact_duplicate_of", value=duplicate_value):
                proposal_ids.append(
                    catalog.record_proposal(
                        asset.asset_id,
                        field="exact_duplicate_of",
                        value=duplicate_value,
                        confidence=1.0,
                        model="deterministic-sha256",
                        model_version="sha256-v1",
                        prompt_sha="not-applicable",
                    )
                )
        try:
            response = self.client.analyze_image(Path(asset.final_path), asset=asset, profiles=profiles)
        except Exception:
            catalog.finish_analysis_run(
                asset_id=asset.asset_id,
                stage="media-analysis",
                input_sha256=asset.sha256,
                model=self.config.model,
                model_version=self.config.prompt_version,
                prompt_sha=prompt_sha,
                status="failed",
            )
            raise
        payload = response.payload
        proposal_records = [
            catalog.record_proposal(
                asset.asset_id,
                field="summary",
                value=payload["summary"],
                confidence=None,
                model=response.model,
                model_version=self.config.prompt_version,
                prompt_sha=response.prompt_sha,
            ),
            catalog.record_proposal(
                asset.asset_id,
                field="tags",
                value=json.dumps(payload["tags"]),
                confidence=None,
                model=response.model,
                model_version=self.config.prompt_version,
                prompt_sha=response.prompt_sha,
            ),
            catalog.record_proposal(
                asset.asset_id,
                field="filename_proposal",
                value=json.dumps(payload["filename_proposal"], sort_keys=True),
                confidence=payload["filename_proposal"]["confidence"],
                model=response.model,
                model_version=self.config.prompt_version,
                prompt_sha=response.prompt_sha,
            ),
            catalog.record_proposal(
                asset.asset_id,
                field="description",
                value=payload["description"],
                confidence=None,
                model=response.model,
                model_version=self.config.prompt_version,
                prompt_sha=response.prompt_sha,
            ),
            catalog.record_proposal(
                asset.asset_id,
                field="archive_category",
                value=json.dumps(
                    {
                        "category": payload["archive_category"],
                        "confidence": payload["archive_confidence"],
                        "evidence": payload["archive_evidence"],
                        "keywords": payload["keywords"],
                    },
                    sort_keys=True,
                ),
                confidence=payload["archive_confidence"],
                model=response.model,
                model_version=self.config.prompt_version,
                prompt_sha=response.prompt_sha,
            ),
        ]
        if payload.get("capture_date"):
            proposal_records.append(
                catalog.record_proposal(
                    asset.asset_id,
                    field="capture_date",
                    value=payload["capture_date"],
                    confidence=None,
                    model=response.model,
                    model_version=self.config.prompt_version,
                    prompt_sha=response.prompt_sha,
                )
            )
        proposal_ids.extend(proposal_records)
        known_profiles = {profile["profile_id"] for profile in profiles}
        matched = 0
        for person in payload["people"]:
            profile_id = person["person_profile_id"]
            if profile_id and profile_id in known_profiles:
                catalog.record_person_observation(
                    asset.asset_id,
                    profile_id=profile_id,
                    confidence=person["confidence"],
                    source="model",
                    evidence=person["evidence"],
                    model=response.model,
                    model_version=self.config.prompt_version,
                )
                matched += 1
            else:
                proposal_ids.append(
                    catalog.record_proposal(
                        asset.asset_id,
                        field="person_observation",
                        value=json.dumps(person, sort_keys=True),
                        confidence=person["confidence"],
                        model=response.model,
                        model_version=self.config.prompt_version,
                        prompt_sha=response.prompt_sha,
                    )
                )
        for animal in payload["animals"]:
            proposal_ids.append(
                catalog.record_proposal(
                    asset.asset_id,
                    field="animal_observation",
                    value=json.dumps(animal, sort_keys=True),
                    confidence=animal["confidence"],
                    model=response.model,
                    model_version=self.config.prompt_version,
                    prompt_sha=response.prompt_sha,
                )
            )
        catalog.finish_analysis_run(
            asset_id=asset.asset_id,
            stage="media-analysis",
            input_sha256=asset.sha256,
            model=self.config.model,
            model_version=self.config.prompt_version,
            prompt_sha=response.prompt_sha,
            status="succeeded",
        )
        return AnalysisResult(
            asset.asset_id,
            response.model,
            self.config.prompt_version,
            response.prompt_sha,
            tuple(proposal_ids),
            duplicate_ids,
            matched,
            len(payload["people"]),
            sum(animal["count"] for animal in payload["animals"]),
        )
