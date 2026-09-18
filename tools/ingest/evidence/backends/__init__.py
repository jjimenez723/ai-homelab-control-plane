"""Protocol seams for perception backends, and the registries that build them.

Four separate `Protocol`s, deliberately not one god-model interface:
`TextExtractor` (native document text), `OCRBackend` (pixels to words),
`VisionBackend` (pixels to a scene description), and `TranscriptionBackend`
(audio samples to speech). A real system eventually plugs in four quite
different kinds of engine here -- a library that parses a file format, a CLI
OCR engine, a vision model, a speech model -- and giving them separate,
narrow contracts means swapping one never has to touch the others' callers.

The structural distinction that matters is *what each seam is handed*:

* A `TextExtractor` is trusted in-repo deterministic code (a PDF/DOCX/plain
  text parser), not a model. It receives an `EvidenceSource`, which carries a
  real filesystem `Path`, because it must be able to stream a large file
  rather than hold the whole thing in memory.
* An `OCRBackend`, `VisionBackend`, or `TranscriptionBackend` may one day be
  a model -- today a CLI tool or a hand-written heuristic, but the seam has
  to be safe for whatever fills it next. It receives only a `RasterImage`
  (in-memory PNG bytes, width, height, page number) or an `AudioClip`
  (in-memory 16 kHz mono PCM WAV bytes, a duration). No `Path`, no file
  handle, nothing to mutate the filesystem with. This is exactly the
  reasoning that makes `NamingEvidence.thumbnail_png`
  (`tools/ingest/naming/evidence.py`) bytes rather than a path, applied one
  layer further out.

A backend must never raise. Every method here, including `available()`,
returns a plain result carrying warnings and a status instead of propagating
an exception -- the same "never raises" contract as `media/probe.py::probe_file`,
because these run over a whole volume of unknown files and one bad input must
degrade, not abort a batch.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from tools.ingest.config import OCRConfig, TranscriptionConfig, VisionConfig
from tools.ingest.evidence.types import (
    AudioClip,
    EvidenceSource,
    EvidenceWarning,
    NativeText,
    OCRBlock,
    RasterImage,
    StructureEvidence,
    TranscriptEvidence,
    VisualEvidence,
)


class TextExtractor(Protocol):
    """Reads a file format's own text, deterministically. No inference."""

    name: str
    version: str

    def supports(self, source: EvidenceSource) -> bool:
        """Whether this extractor applies to `source`, e.g. by MIME or extension."""
        ...

    def extract(
        self, source: EvidenceSource, *, max_chars: int
    ) -> tuple[NativeText | None, StructureEvidence | None, tuple[EvidenceWarning, ...], str]:
        """Read `source.path` read-only and return whatever text/structure it holds.

        Returns `(native_text, structure, warnings, status)` rather than the
        `ExtractionResult` below, deliberately: the extractors live in
        `tools/ingest/evidence/text.py`, and having them import this module for
        a return type would close the import cycle that `text_extractors()`'s
        lazy import exists to avoid. A plain tuple keeps the extractor modules
        free of any dependency on the seam that describes them, and
        `ExtractionResult` remains the orchestrator's normalized form.
        """
        ...


class OCRBackend(Protocol):
    """Recognises text within an image. Receives pixels, never a path."""

    name: str
    version: str

    def available(self) -> bool:
        """Whether this backend can currently run (tool installed, etc.)."""
        ...

    def recognize(self, image: RasterImage, *, timeout_seconds: int) -> "OCRResult":
        ...


class VisionBackend(Protocol):
    """Describes what an image shows, in general terms. Receives pixels only."""

    name: str
    version: str

    def available(self) -> bool:
        ...

    def describe(self, image: RasterImage, *, timeout_seconds: int) -> "VisionResult":
        ...


class TranscriptionBackend(Protocol):
    """Recognises speech within audio. Receives samples, never a path.

    `transcribe` is handed an `AudioClip` -- in-memory WAV bytes at a fixed
    16 kHz mono, normalized by `backends/ffmpeg_audio.py` so an engine never
    has to resample and two engines can never disagree about what they were
    shown.
    """

    name: str
    version: str

    def available(self) -> bool:
        """Whether this backend can currently run (binary and model present)."""
        ...

    def transcribe(self, audio: AudioClip, *, timeout_seconds: int) -> "TranscriptionResult":
        ...


@dataclass(frozen=True)
class ExtractionResult:
    native_text: NativeText | None
    structure: StructureEvidence | None = None
    warnings: tuple[EvidenceWarning, ...] = ()
    status: str = "ok"


@dataclass(frozen=True)
class OCRResult:
    # `reading_order` on each block may be a placeholder (0) here; the
    # orchestrator (`extract.py`) is what actually calls
    # `reading_order.assign_reading_order` across a page's blocks.
    blocks: tuple[OCRBlock, ...] = ()
    warnings: tuple[EvidenceWarning, ...] = ()
    status: str = "ok"


@dataclass(frozen=True)
class VisionResult:
    visual: VisualEvidence | None = None
    warnings: tuple[EvidenceWarning, ...] = ()
    status: str = "ok"


@dataclass(frozen=True)
class TranscriptionResult:
    transcript: TranscriptEvidence | None = None
    warnings: tuple[EvidenceWarning, ...] = ()
    status: str = "ok"


def build_ocr_backend(config: OCRConfig) -> OCRBackend:
    """Construct the configured `OCRBackend`.

    Imports are deferred into each branch so that, e.g., importing this module
    does not require the tesseract backend's module to be importable when
    tesseract support is not being used. Config loading (`tools/ingest/
    config.py`) already validates `config.backend` against `VALID_OCR_BACKENDS`
    at load time; the `ValueError` here is the second line of defence for a
    config object built by hand (as tests do) rather than loaded from YAML.
    """
    if config.backend == "null":
        from tools.ingest.evidence.backends.null import NullOCRBackend

        return NullOCRBackend()
    if config.backend == "tesseract":
        from tools.ingest.evidence.backends.tesseract import TesseractOCRBackend

        # `max_blocks` is not part of the `OCRBackend.recognize()` call shape
        # (see the protocol above) because it is a backend policy, not a
        # per-call parameter -- it is fixed for the life of the backend, the
        # same way `timeout_seconds` is fixed per call.
        return TesseractOCRBackend(max_blocks=config.max_blocks)
    raise ValueError(f"unknown OCR backend {config.backend!r}")


def build_vision_backend(config: VisionConfig) -> VisionBackend:
    """Construct the configured `VisionBackend`. See `build_ocr_backend`."""
    if config.backend == "null":
        from tools.ingest.evidence.backends.null import NullVisionBackend

        return NullVisionBackend()
    if config.backend == "heuristic":
        from tools.ingest.evidence.backends.heuristic import HeuristicVisionBackend

        return HeuristicVisionBackend()
    if config.backend == "ollama":
        from tools.ingest.evidence.backends.ollama import OllamaVisionBackend

        return OllamaVisionBackend(config)
    raise ValueError(f"unknown vision backend {config.backend!r}")


def build_transcription_backend(config: TranscriptionConfig) -> TranscriptionBackend:
    """Construct the configured `TranscriptionBackend`. See `build_ocr_backend`.

    `"null"` is the default and stays the repository-safe one: whisper.cpp is
    an optional system binary plus a model file the repository does not ship
    (D-058), detected rather than required.
    """
    if config.backend == "null":
        from tools.ingest.evidence.backends.transcription import NullTranscriptionBackend

        return NullTranscriptionBackend()
    if config.backend == "whisper-cpp":
        from tools.ingest.evidence.backends.transcription import WhisperCppBackend

        return WhisperCppBackend(config)
    raise ValueError(f"unknown transcription backend {config.backend!r}")


def text_extractors() -> tuple[TextExtractor, ...]:
    """The native-text extractors, in priority order.

    Imported lazily from `tools.ingest.evidence.text` to avoid an import cycle
    (that module is a sibling built by a different slice of this work, and
    package `__init__` modules importing each other's submodules eagerly is
    exactly how such cycles happen). Order matters: `extract.py` uses the
    first extractor whose `supports()` returns True.
    """
    from tools.ingest.evidence.office import CsvTextExtractor, PptxTextExtractor, XlsxTextExtractor
    from tools.ingest.evidence.text import DocxTextExtractor, PdfTextExtractor, PlainTextExtractor

    # CsvTextExtractor precedes PlainTextExtractor deliberately: `.csv`/`.tsv`
    # already satisfy PlainTextExtractor.supports() (media_kind is text), and
    # "first extractor whose supports() returns True wins" means order alone
    # decides which one actually handles a CSV.
    return (
        CsvTextExtractor(),
        PlainTextExtractor(),
        PdfTextExtractor(),
        DocxTextExtractor(),
        XlsxTextExtractor(),
        PptxTextExtractor(),
    )
