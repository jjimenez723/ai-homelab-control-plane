"""Load and merge the human-editable `config/ingest.yaml`.

Defaults live in code so the tool works with no config file at all (useful for
tests and for `--config` pointed at a minimal fixture); `config/ingest.yaml`
only needs to override what differs on a given machine.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError as exc:  # pragma: no cover - exercised by the CLI environment
    raise SystemExit("PyYAML is required; run `uv pip install -r requirements-social-video.txt`") from exc


DEFAULT_CONFIG_PATH = Path("config/ingest.yaml")

DEFAULTS: dict[str, Any] = {
    "t7": {
        "volume_uuid": None,
        "storage_root_override": None,
        "inbox_relative_path": "00-inbox/organized",
        "system_relative_path": "90-system",
    },
    "sources": {
        "desktop": "~/Desktop",
        "documents": "~/Documents",
        "downloads": "~/Downloads",
        "sd_volume_uuids": [],
        # Screenshots are a first-class source for *naming*, not for vacuuming.
        # There is no default path on purpose: macOS puts screenshots wherever
        # `com.apple.screencapture location` says, which is often ~/Desktop.
        # Guessing would mean silently walking the Desktop, so an unconfigured
        # screenshots source fails loudly instead. `organize doctor` reports the
        # configured location without ever applying it.
        "screenshots": {
            "path": None,
            "enabled": False,
        },
    },
    "settle_seconds": 30,
    "downloads_watcher_min_settle_seconds": 1200,
    "naming": {
        "enabled": True,
        # Naming applies to these source classes. Desktop and Documents are
        # absent by design: this phase does not touch them automatically at all.
        "source_classes": ["screenshots", "downloads", "sd"],
        # Below this, a proposal is recorded and ignored rather than applied.
        "min_confidence": 0.55,
        "max_filename_length": 120,
        # Extensions never renamed regardless of how poor the name looks:
        # renaming these breaks something that depends on the name.
        "protected_extensions": [".app", ".dmg", ".pkg", ".mpkg", ".framework", ".kext"],
        # Words the owner's own projects/clients use. Present in a filename,
        # they are strong evidence the name is already meaningful.
        "vocabulary": [],
    },
    "triage": {
        "enabled": True,
        # Nothing is ingested automatically until this is switched on, and even
        # then only above min_ingest_confidence. Everything else is reported.
        "auto_ingest": False,
        "min_ingest_confidence": 0.85,
        # Treated as disposable-for-Angel on sight. They stay on the Mac.
        "installer_extensions": [".dmg", ".pkg", ".mpkg", ".iso", ".exe", ".msi", ".deb", ".rpm", ".appimage"],
        "ignore_extensions": [".torrent", ".crdownload", ".part", ".download", ".tmp"],
        "document_extensions": [".pdf", ".docx", ".doc", ".pages", ".key", ".pptx", ".xlsx", ".numbers", ".csv", ".rtf"],
        # Below this many bytes a file is assumed to be a fragment or an icon.
        "min_interesting_bytes": 4096,
    },
    "automation": {
        "enabled": False,
        "state_relative_path": ".state/file-intelligence",
        # Absolute override, for a drill or a test. Normally unset.
        "state_dir": None,
    },
    "exclusions": {
        "dir_names": [
            "node_modules",
            ".venv",
            "venv",
            "env",
            ".tox",
            "__pycache__",
            ".mypy_cache",
            ".pytest_cache",
            ".git",
        ],
        "bundle_extensions": [
            ".app",
            ".bundle",
            ".framework",
            ".plugin",
            ".kext",
            ".photoslibrary",
            ".xcodeproj",
            ".xcworkspace",
        ],
        "incomplete_download_suffixes": [".crdownload", ".download", ".part"],
        "keep_local_marker": ".keep-local",
        "extra_paths": [],
    },
    "media": {
        "heic_to_jpeg": {
            "enabled": True,
            "preserve_original": True,
            "jpeg_quality": 100,
            "preserve_metadata": True,
            "preserve_icc_profile": True,
            "normalize_orientation": True,
        }
    },
    "evidence": {
        # Perception only: native text, OCR, a vision description, layout.
        # This layer never renames, moves, deletes, or ingests anything --
        # it measures a file's bytes and caches the result for a separate,
        # later decision to read. See the evidence-layer spec for the full
        # invariant (FACTS != INTERPRETATIONS != DECISIONS).
        "enabled": True,
        # Files larger than this are skipped outright -- nothing rasterized,
        # no backend invoked -- rather than silently truncated mid-page.
        "max_bytes": 104_857_600,
        # A PDF/document past this many pages has only its first pages
        # perceived; the rest is left unread rather than raising.
        "max_pages": 20,
        "raster_dpi": 200,
        # Decompression-bomb guard for rasterized pages, mirroring
        # `media.probe.MAX_DECODE_PIXELS`'s reasoning for source images.
        "raster_max_pixels": 40_000_000,
        # Extracted text is bounded before it is ever stored; see
        # `evidence/types.py::sanitize_extracted_text`.
        "max_text_chars": 20_000,
        "ocr": {
            "enabled": True,
            # "null" performs no OCR at all -- the safe default until an
            # operator opts into "tesseract" on a machine that has it.
            "backend": "null",
            "timeout_seconds": 30,
            "max_blocks": 2000,
        },
        "vision": {
            "enabled": True,
            # "heuristic" makes no model call; it is a deterministic,
            # offline description built from measured facts. "null" performs
            # no vision perception at all.
            "backend": "heuristic",
            "timeout_seconds": 30,
            "video_frame_count": 5,
            # Collapse near-identical sampled frames to one representative
            # before OCR/vision run, and sample fewer frames from a very
            # short clip. Both are deterministic; see
            # `evidence/backends/ffmpeg_video.py` for the exact algorithm.
            "frame_dedupe": True,
            "frame_duration_aware": True,
        },
        "transcription": {
            # Off by default, like every other optional inference seam: it
            # needs a whisper.cpp binary and a multi-hundred-megabyte model
            # the repository does not ship (D-058).
            "enabled": False,
            "backend": "null",  # "null" | "whisper-cpp"
            # Unset means "look in the Angel whisper cache", resolved through
            # `tools/models/storage.py` at call time -- never a machine path
            # baked into code.
            "model_path": None,
            "language": "en",
            # Only the first N seconds of a long recording are transcribed.
            # Evidence about a file, not a subtitle track.
            "max_audio_seconds": 600,
            "timeout_seconds": 300,
        },
    },
    "backup": {
        # This is intentionally constrained in code to the one dedicated
        # Google Drive repository for this machine. It is not a general
        # arbitrary-restic-repository setting.
        "repository": "rclone:gdrive-backup:AI-Homelab-Backups/T7-restic",
        "keychain_service": "ai-homelab-t7-restic",
        "rclone_keychain_service": "ai-homelab-rclone-config",
        "retention": {
            "enabled": False,
            "keep_daily": 7,
            "keep_weekly": 4,
            "keep_monthly": 6,
            "keep_yearly": 0,
        },
        # A relative path on the T7 for the monthly, isolated restore drill.
        # null disables the drill until an operator chooses a durable marker.
        "restore_drill_relative_path": None,
        # The only default exclusions are incomplete transfer artifacts and
        # macOS volume metadata that is either unreadable or meaningless in a
        # restore. `.Spotlight-V100` is SIP-protected: restic cannot open it
        # even as the owner, which otherwise makes every backup exit 3 and be
        # logged as a failure. Do not add valuable data classes here without
        # an explicit decision.
        "exclude_patterns": [
            "90-system/staging/**",
            "40-ai/model-cache/**",
            "**/*.partial",
            ".Spotlight-V100",
            ".fseventsd",
            ".TemporaryItems",
            ".Trashes",
        ],
    },
}


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


@dataclass(frozen=True)
class HeicToJpegConfig:
    enabled: bool = True
    preserve_original: bool = True
    jpeg_quality: int = 100
    preserve_metadata: bool = True
    preserve_icc_profile: bool = True
    normalize_orientation: bool = True


@dataclass(frozen=True)
class MediaConfig:
    heic_to_jpeg: HeicToJpegConfig = field(default_factory=HeicToJpegConfig)


@dataclass(frozen=True)
class ExclusionsConfig:
    dir_names: frozenset[str]
    bundle_extensions: frozenset[str]
    incomplete_download_suffixes: frozenset[str]
    keep_local_marker: str
    extra_paths: tuple[Path, ...]


@dataclass(frozen=True)
class T7Config:
    volume_uuid: str | None
    storage_root_override: Path | None
    inbox_relative_path: str
    system_relative_path: str


@dataclass(frozen=True)
class ScreenshotsConfig:
    """Where screenshots live, if the owner has said so.

    `enabled` without `path` is a configuration error, not a cue to search.
    """

    path: Path | None = None
    enabled: bool = False


@dataclass(frozen=True)
class SourcesConfig:
    desktop: Path
    documents: Path
    downloads: Path
    sd_volume_uuids: frozenset[str]
    screenshots: ScreenshotsConfig = field(default_factory=ScreenshotsConfig)


@dataclass(frozen=True)
class NamingConfig:
    enabled: bool = True
    source_classes: frozenset[str] = frozenset({"screenshots", "downloads", "sd"})
    min_confidence: float = 0.55
    max_filename_length: int = 120
    protected_extensions: frozenset[str] = frozenset({".app", ".dmg", ".pkg", ".mpkg", ".framework", ".kext"})
    vocabulary: tuple[str, ...] = ()


@dataclass(frozen=True)
class TriageConfig:
    enabled: bool = True
    auto_ingest: bool = False
    min_ingest_confidence: float = 0.85
    installer_extensions: frozenset[str] = frozenset()
    ignore_extensions: frozenset[str] = frozenset()
    document_extensions: frozenset[str] = frozenset()
    min_interesting_bytes: int = 4096


# Backend names the evidence layer knows how to build. Anything else in
# config is a typo or a not-yet-implemented backend, and is refused at load
# time rather than silently falling back to a default the operator didn't ask
# for -- the same posture as the screenshots-path guard above.
VALID_OCR_BACKENDS = frozenset({"null", "tesseract"})
VALID_VISION_BACKENDS = frozenset({"null", "heuristic", "ollama"})
VALID_TRANSCRIPTION_BACKENDS = frozenset({"null", "whisper-cpp"})

# The hard ceiling on sampled video frames, independent of what an operator
# asks for. Every frame is a separate `ffmpeg` run plus (up to) one vision
# call and one OCR pass, so an unbounded count turns one file into an
# arbitrarily long stall in a batch that has a whole volume left to scan.
# Deduplication (`evidence.vision.frame_dedupe`) reduces the *backend* calls
# below this; it does not raise the ceiling on sampling.
MAX_VIDEO_FRAME_COUNT = 12


@dataclass(frozen=True)
class OCRConfig:
    enabled: bool = True
    backend: str = "null"  # "null" | "tesseract"
    timeout_seconds: int = 30
    max_blocks: int = 2000


@dataclass(frozen=True)
class VisionConfig:
    enabled: bool = True
    backend: str = "heuristic"  # "null" | "heuristic" | "ollama"
    timeout_seconds: int = 30
    # Ollama is opt-in and loopback-only by default. Bounds apply before any
    # bytes leave this process, even when an operator changes the endpoint.
    endpoint: str = "http://127.0.0.1:11434"
    model: str = "qwen3-vl:8b"
    max_image_bytes: int = 8_000_000
    max_prompt_chars: int = 4_000
    # How many frames a video gets sampled into for vision/OCR (evenly spaced
    # across its duration). The ROI knob: more frames catches a subject that
    # only appears partway through a clip, at the cost of one backend call
    # (and, if OCR is enabled, one OCR pass) per frame.
    video_frame_count: int = 5
    # Collapse near-identical sampled frames (average-hash, see
    # `evidence/backends/ffmpeg_video.py::deduplicate_frames`) so a static
    # clip costs one backend call rather than `video_frame_count` calls
    # describing the same picture.
    frame_dedupe: bool = True
    # Sample fewer frames from a clip too short for `video_frame_count`
    # distinct moments to exist in.
    frame_duration_aware: bool = True


@dataclass(frozen=True)
class TranscriptionConfig:
    """Speech-to-text: the third perception seam (D-058).

    Disabled by default. `model_path` unset means "resolve the Angel whisper
    cache through `tools/models/storage.py`", which only answers on a machine
    whose UUID-verified T7 is mounted -- so the default is a *policy*, not a
    path, and no machine-specific location appears in this repository.
    """

    enabled: bool = False
    backend: str = "null"  # "null" | "whisper-cpp"
    model_path: Path | None = None
    language: str = "en"
    max_audio_seconds: int = 600
    timeout_seconds: int = 300


@dataclass(frozen=True)
class EvidenceConfig:
    """Perception only -- see `tools/ingest/evidence/__init__.py`.

    Nothing under this key is permitted to change a naming, retention, or
    ingestion decision; it governs what gets measured and cached, not what
    happens to a file.
    """

    enabled: bool = True
    max_bytes: int = 104_857_600
    max_pages: int = 20
    raster_dpi: int = 200
    raster_max_pixels: int = 40_000_000
    max_text_chars: int = 20_000
    ocr: OCRConfig = field(default_factory=OCRConfig)
    vision: VisionConfig = field(default_factory=VisionConfig)
    transcription: TranscriptionConfig = field(default_factory=TranscriptionConfig)


@dataclass(frozen=True)
class AutomationConfig:
    enabled: bool = False
    state_relative_path: str = ".state/file-intelligence"
    # An absolute state directory, for a drill or a test that must not write
    # into the developer's checkout. `state_relative_path` stays relative-only
    # so a stray config value can never point run state at `/`.
    state_dir_override: Path | None = None


@dataclass(frozen=True)
class BackupRetentionConfig:
    enabled: bool = False
    keep_daily: int = 7
    keep_weekly: int = 4
    keep_monthly: int = 6
    keep_yearly: int = 0


@dataclass(frozen=True)
class BackupConfig:
    repository: str = "rclone:gdrive-backup:AI-Homelab-Backups/T7-restic"
    keychain_service: str = "ai-homelab-t7-restic"
    rclone_keychain_service: str = "ai-homelab-rclone-config"
    retention: BackupRetentionConfig = field(default_factory=BackupRetentionConfig)
    restore_drill_relative_path: str | None = None
    exclude_patterns: tuple[str, ...] = (
        "90-system/staging/**",
        "40-ai/model-cache/**",
        "**/*.partial",
        ".Spotlight-V100",
        ".fseventsd",
        ".TemporaryItems",
        ".Trashes",
    )


@dataclass(frozen=True)
class IngestConfig:
    t7: T7Config
    sources: SourcesConfig
    settle_seconds: int
    downloads_watcher_min_settle_seconds: int
    exclusions: ExclusionsConfig
    media: MediaConfig
    naming: NamingConfig = field(default_factory=NamingConfig)
    triage: TriageConfig = field(default_factory=TriageConfig)
    evidence: EvidenceConfig = field(default_factory=EvidenceConfig)
    automation: AutomationConfig = field(default_factory=AutomationConfig)
    backup: BackupConfig = field(default_factory=BackupConfig)
    config_path: Path | None = None

    @property
    def repo_root(self) -> Path:
        return Path(__file__).resolve().parents[2]

    @property
    def state_dir(self) -> Path:
        if self.automation.state_dir_override is not None:
            return self.automation.state_dir_override
        return self.repo_root / self.automation.state_relative_path

    @property
    def local_ledger_path(self) -> Path:
        """The Mac-side ledger. Deliberately not on the T7 -- naming has to keep
        working with Angel unplugged (D-021)."""
        return self.state_dir / "local-ledger.sqlite"

    def effective_settle_seconds(self, *, automated: bool) -> int:
        """The settle window a run must honor.

        An automated run always uses at least the watcher threshold. Taking the
        max rather than simply switching means lowering one value in config can
        never quietly shorten the window an unattended job waits.
        """
        if not automated:
            return self.settle_seconds
        return max(self.settle_seconds, self.downloads_watcher_min_settle_seconds)


def _expand(raw: str) -> Path:
    return Path(raw).expanduser()


def load_config(path: Path | None = None) -> IngestConfig:
    """Load config, merging over built-in defaults. Missing file -> pure defaults."""
    raw: dict[str, Any] = {}
    resolved_path: Path | None = None
    candidate = path if path is not None else DEFAULT_CONFIG_PATH
    if candidate.exists():
        resolved_path = candidate
        loaded = yaml.safe_load(candidate.read_text(encoding="utf-8")) or {}
        if not isinstance(loaded, dict):
            raise ValueError(f"{candidate}: expected a YAML mapping at the top level")
        raw = loaded
    elif path is not None:
        raise FileNotFoundError(f"config file not found: {path}")

    merged = _deep_merge(DEFAULTS, raw)

    t7_raw = merged["t7"]
    t7 = T7Config(
        volume_uuid=t7_raw.get("volume_uuid"),
        storage_root_override=(
            _expand(t7_raw["storage_root_override"]) if t7_raw.get("storage_root_override") else None
        ),
        inbox_relative_path=t7_raw["inbox_relative_path"],
        system_relative_path=t7_raw["system_relative_path"],
    )

    sources_raw = merged["sources"]
    screenshots_raw = sources_raw.get("screenshots") or {}
    if not isinstance(screenshots_raw, dict):
        raise ValueError("sources.screenshots must be a mapping with `path` and `enabled`")
    screenshots = ScreenshotsConfig(
        path=_expand(screenshots_raw["path"]) if screenshots_raw.get("path") else None,
        enabled=bool(screenshots_raw.get("enabled", False)),
    )
    if screenshots.enabled and screenshots.path is None:
        # Fail here rather than fall back to ~/Desktop: an unconfigured
        # screenshots source that quietly resolves to the Desktop would put the
        # whole Desktop under automatic evaluation.
        raise ValueError(
            "sources.screenshots.enabled is true but sources.screenshots.path is not set; "
            "set the screenshot folder explicitly (`organize doctor` reports where macOS is "
            "currently saving them)"
        )
    sources = SourcesConfig(
        desktop=_expand(sources_raw["desktop"]),
        documents=_expand(sources_raw["documents"]),
        downloads=_expand(sources_raw["downloads"]),
        sd_volume_uuids=frozenset(sources_raw.get("sd_volume_uuids") or []),
        screenshots=screenshots,
    )

    naming_raw = merged["naming"]
    naming = NamingConfig(
        enabled=bool(naming_raw["enabled"]),
        source_classes=frozenset(naming_raw["source_classes"]),
        min_confidence=float(naming_raw["min_confidence"]),
        max_filename_length=int(naming_raw["max_filename_length"]),
        protected_extensions=frozenset(ext.lower() for ext in naming_raw["protected_extensions"]),
        vocabulary=tuple(str(word) for word in naming_raw.get("vocabulary") or ()),
    )
    if not 0.0 <= naming.min_confidence <= 1.0:
        raise ValueError("naming.min_confidence must be between 0 and 1")
    if not 8 <= naming.max_filename_length <= 200:
        raise ValueError("naming.max_filename_length must be between 8 and 200")

    triage_raw = merged["triage"]
    triage = TriageConfig(
        enabled=bool(triage_raw["enabled"]),
        auto_ingest=bool(triage_raw["auto_ingest"]),
        min_ingest_confidence=float(triage_raw["min_ingest_confidence"]),
        installer_extensions=frozenset(ext.lower() for ext in triage_raw["installer_extensions"]),
        ignore_extensions=frozenset(ext.lower() for ext in triage_raw["ignore_extensions"]),
        document_extensions=frozenset(ext.lower() for ext in triage_raw["document_extensions"]),
        min_interesting_bytes=int(triage_raw["min_interesting_bytes"]),
    )
    if not 0.0 <= triage.min_ingest_confidence <= 1.0:
        raise ValueError("triage.min_ingest_confidence must be between 0 and 1")

    evidence_raw = merged["evidence"]
    ocr_raw = evidence_raw["ocr"]
    vision_raw = evidence_raw["vision"]
    if ocr_raw["backend"] not in VALID_OCR_BACKENDS:
        raise ValueError(
            f"evidence.ocr.backend must be one of {sorted(VALID_OCR_BACKENDS)}, got {ocr_raw['backend']!r}"
        )
    if vision_raw["backend"] not in VALID_VISION_BACKENDS:
        raise ValueError(
            f"evidence.vision.backend must be one of {sorted(VALID_VISION_BACKENDS)}, "
            f"got {vision_raw['backend']!r}"
        )
    transcription_raw = evidence_raw["transcription"]
    if transcription_raw["backend"] not in VALID_TRANSCRIPTION_BACKENDS:
        raise ValueError(
            f"evidence.transcription.backend must be one of {sorted(VALID_TRANSCRIPTION_BACKENDS)}, "
            f"got {transcription_raw['backend']!r}"
        )
    ocr = OCRConfig(
        enabled=bool(ocr_raw["enabled"]),
        backend=str(ocr_raw["backend"]),
        timeout_seconds=int(ocr_raw["timeout_seconds"]),
        max_blocks=int(ocr_raw["max_blocks"]),
    )
    if not 1 <= ocr.timeout_seconds <= 600:
        raise ValueError("evidence.ocr.timeout_seconds must be between 1 and 600")
    if ocr.max_blocks <= 0:
        raise ValueError("evidence.ocr.max_blocks must be a positive integer")

    vision = VisionConfig(
        enabled=bool(vision_raw["enabled"]),
        backend=str(vision_raw["backend"]),
        timeout_seconds=int(vision_raw["timeout_seconds"]),
        endpoint=str(vision_raw.get("endpoint", "http://127.0.0.1:11434")),
        model=str(vision_raw.get("model", "qwen3-vl:8b")),
        max_image_bytes=int(vision_raw.get("max_image_bytes", 8_000_000)),
        max_prompt_chars=int(vision_raw.get("max_prompt_chars", 4_000)),
        video_frame_count=int(vision_raw.get("video_frame_count", 5)),
        frame_dedupe=bool(vision_raw.get("frame_dedupe", True)),
        frame_duration_aware=bool(vision_raw.get("frame_duration_aware", True)),
    )
    if not 1 <= vision.timeout_seconds <= 600:
        raise ValueError("evidence.vision.timeout_seconds must be between 1 and 600")
    if vision.max_image_bytes <= 0 or vision.max_image_bytes > 50_000_000:
        raise ValueError("evidence.vision.max_image_bytes must be between 1 and 50000000")
    if not 100 <= vision.max_prompt_chars <= 100_000:
        raise ValueError("evidence.vision.max_prompt_chars must be between 100 and 100000")
    if not 1 <= vision.video_frame_count <= MAX_VIDEO_FRAME_COUNT:
        raise ValueError(
            f"evidence.vision.video_frame_count must be between 1 and {MAX_VIDEO_FRAME_COUNT}"
        )

    model_path_raw = transcription_raw.get("model_path")
    transcription = TranscriptionConfig(
        enabled=bool(transcription_raw["enabled"]),
        backend=str(transcription_raw["backend"]),
        model_path=_expand(str(model_path_raw)) if model_path_raw else None,
        language=str(transcription_raw.get("language", "en")),
        max_audio_seconds=int(transcription_raw.get("max_audio_seconds", 600)),
        timeout_seconds=int(transcription_raw.get("timeout_seconds", 300)),
    )
    if not 1 <= transcription.max_audio_seconds <= 36_000:
        raise ValueError("evidence.transcription.max_audio_seconds must be between 1 and 36000")
    if not 1 <= transcription.timeout_seconds <= 3_600:
        raise ValueError("evidence.transcription.timeout_seconds must be between 1 and 3600")
    if transcription.model_path is not None and not transcription.model_path.is_absolute():
        raise ValueError("evidence.transcription.model_path must be an absolute path")
    if len(transcription.language) > 16:
        raise ValueError("evidence.transcription.language must be a short language tag")

    evidence = EvidenceConfig(
        enabled=bool(evidence_raw["enabled"]),
        max_bytes=int(evidence_raw["max_bytes"]),
        max_pages=int(evidence_raw["max_pages"]),
        raster_dpi=int(evidence_raw["raster_dpi"]),
        raster_max_pixels=int(evidence_raw["raster_max_pixels"]),
        max_text_chars=int(evidence_raw["max_text_chars"]),
        ocr=ocr,
        vision=vision,
        transcription=transcription,
    )
    if evidence.max_bytes <= 0:
        raise ValueError("evidence.max_bytes must be a positive integer")
    if not 1 <= evidence.max_pages <= 10_000:
        raise ValueError("evidence.max_pages must be between 1 and 10000")
    if not 1 <= evidence.raster_dpi <= 1200:
        raise ValueError("evidence.raster_dpi must be between 1 and 1200")
    if evidence.raster_max_pixels <= 0:
        raise ValueError("evidence.raster_max_pixels must be a positive integer")
    if evidence.max_text_chars <= 0:
        raise ValueError("evidence.max_text_chars must be a positive integer")

    automation_raw = merged["automation"]
    override_raw = automation_raw.get("state_dir")
    state_dir_override = _expand(str(override_raw)) if override_raw else None
    if state_dir_override is not None:
        if not state_dir_override.is_absolute():
            raise ValueError("automation.state_dir must be an absolute path")
        if state_dir_override == Path(state_dir_override.root):
            raise ValueError("automation.state_dir may not be a filesystem root")
    automation = AutomationConfig(
        enabled=bool(automation_raw["enabled"]),
        state_relative_path=str(automation_raw["state_relative_path"]),
        state_dir_override=state_dir_override,
    )
    if Path(automation.state_relative_path).is_absolute() or ".." in Path(automation.state_relative_path).parts:
        raise ValueError("automation.state_relative_path must be a relative path inside the checkout")

    excl_raw = merged["exclusions"]
    exclusions = ExclusionsConfig(
        dir_names=frozenset(excl_raw["dir_names"]),
        bundle_extensions=frozenset(ext.lower() for ext in excl_raw["bundle_extensions"]),
        incomplete_download_suffixes=frozenset(s.lower() for s in excl_raw["incomplete_download_suffixes"]),
        keep_local_marker=excl_raw["keep_local_marker"],
        extra_paths=tuple(_expand(p) for p in excl_raw.get("extra_paths") or []),
    )

    heic_raw = merged["media"]["heic_to_jpeg"]
    if not heic_raw.get("preserve_original", True):
        # Not a real knob: docs/13 and D-016 require the HEIC/HEIF original to
        # remain the untouched archival asset. Refuse to start rather than
        # silently ignore an operator's expectation that this would delete it.
        raise ValueError(
            "media.heic_to_jpeg.preserve_original may not be set to false: "
            "the original HEIC/HEIF is never deleted by this tool"
        )
    media = MediaConfig(
        heic_to_jpeg=HeicToJpegConfig(
            enabled=bool(heic_raw["enabled"]),
            preserve_original=bool(heic_raw["preserve_original"]),
            jpeg_quality=int(heic_raw["jpeg_quality"]),
            preserve_metadata=bool(heic_raw["preserve_metadata"]),
            preserve_icc_profile=bool(heic_raw["preserve_icc_profile"]),
            normalize_orientation=bool(heic_raw["normalize_orientation"]),
        )
    )

    backup_raw = merged["backup"]
    retention_raw = backup_raw["retention"]
    retention = BackupRetentionConfig(
        enabled=bool(retention_raw.get("enabled", False)),
        keep_daily=int(retention_raw["keep_daily"]),
        keep_weekly=int(retention_raw["keep_weekly"]),
        keep_monthly=int(retention_raw["keep_monthly"]),
        keep_yearly=int(retention_raw["keep_yearly"]),
    )
    if any(value < 0 for value in (retention.keep_daily, retention.keep_weekly, retention.keep_monthly, retention.keep_yearly)):
        raise ValueError("backup retention values may not be negative")
    backup = BackupConfig(
        repository=str(backup_raw["repository"]),
        keychain_service=str(backup_raw["keychain_service"]),
        rclone_keychain_service=str(backup_raw["rclone_keychain_service"]),
        retention=retention,
        restore_drill_relative_path=(
            str(backup_raw["restore_drill_relative_path"])
            if backup_raw.get("restore_drill_relative_path")
            else None
        ),
        exclude_patterns=tuple(str(pattern) for pattern in backup_raw["exclude_patterns"]),
    )

    return IngestConfig(
        t7=t7,
        sources=sources,
        settle_seconds=int(merged["settle_seconds"]),
        downloads_watcher_min_settle_seconds=int(merged["downloads_watcher_min_settle_seconds"]),
        exclusions=exclusions,
        media=media,
        naming=naming,
        triage=triage,
        evidence=evidence,
        automation=automation,
        backup=backup,
        config_path=resolved_path,
    )
