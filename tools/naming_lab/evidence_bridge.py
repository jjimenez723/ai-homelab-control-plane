"""The Lab's evidence bridge: production naming evidence + OCR/vision perception.

This is the one deliberate, reviewed place in the repo that combines
`tools.ingest.naming` (production naming evidence) with
`tools.ingest.evidence` (OCR/native-text/vision perception) -- see
DECISIONS.md D-054. Everywhere else, `tests/ingest/test_evidence_security.py`
has an AST guard that forbids a module under `tools/ingest/naming/` or
`tools/ingest/triage/` from importing `tools.ingest.evidence`, because that
combination is exactly how a model's perception would end up deciding a
rename instead of merely being recorded (D-029/D-030). `tools/naming_lab/`
is neither of those packages, and nothing here ever renames a file: every
result is human-reviewed evaluation data, not a production decision.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from pathlib import Path

from tools.ingest.config import EvidenceConfig
from tools.ingest.evidence.backends import OCRBackend, VisionBackend, build_ocr_backend, build_vision_backend
from tools.ingest.evidence.extract import collect_evidence
from tools.ingest.evidence.types import EvidenceSource, FileEvidence, VisualEvidence
from tools.ingest.hashing import sha256_file
from tools.ingest.media.probe import probe_file
from tools.ingest.naming.evidence import NamingEvidence, gather_evidence
from tools.naming_lab.dates import SemanticDateCandidate, derive_semantic_date
from tools.naming_lab.document_dates import extract_document_dates


@dataclass(frozen=True)
class LabEvidence:
    """Everything the Lab knows about one file: perception plus a derived date.

    Carries no callable and, like `NamingEvidence`/`FileEvidence` themselves,
    is inert data -- the Lab's runner reads it, nothing mutates a filesystem
    with it.
    """

    sha256: str
    naming_evidence: NamingEvidence
    file_evidence: FileEvidence | None
    semantic_date: SemanticDateCandidate | None
    document_date_candidates: tuple[SemanticDateCandidate, ...]

    @property
    def combined_text(self) -> str:
        """Native text and OCR text, concatenated, for the UI's evidence panel."""
        if self.file_evidence is None:
            return ""
        native = self.file_evidence.native_text.text if self.file_evidence.native_text else ""
        ocr = self.file_evidence.ocr_text
        return "\n".join(part for part in (native, ocr) if part)

    @property
    def visual(self) -> VisualEvidence | None:
        """The vision backend's description of the file, if any was run.

        For a photo, screenshot, or a sampled video frame -- the one place a
        naming prompt or a human reviewer should look for "what does this
        actually show," as opposed to `combined_text`'s OCR/native text. For
        a video this is the *first* sampled frame's description; see
        `visual_frames` for all of them.
        """
        return self.file_evidence.visual if self.file_evidence is not None else None

    @property
    def visual_frames(self) -> tuple[VisualEvidence, ...]:
        """Every vision description the backend produced, in sampling order.

        Length 1 for a photo/screenshot, one entry per sampled frame for a
        video (`evidence.vision.video_frame_count` of them, fewer if some
        frames failed to extract).
        """
        return self.file_evidence.visual_frames if self.file_evidence is not None else ()


def gather_lab_evidence(
    path: Path,
    *,
    vocabulary: tuple[str, ...] = (),
    evidence_config: EvidenceConfig | None = None,
    ocr_backend: OCRBackend | None = None,
    vision_backend: VisionBackend | None = None,
) -> LabEvidence:
    """Measure one file for the Lab.

    Always gathers production `NamingEvidence` (`tools.ingest.naming.evidence
    .gather_evidence`, unchanged). When `evidence_config.enabled` (the same
    knob production's own `config/ingest.yaml` uses), also runs OCR/native-text
    perception through `collect_evidence` -- the same orchestrator production
    would use if it ever called this package -- and derives a semantic date
    from the result.

    Backends default to `tools.ingest.evidence.backends.build_ocr_backend`/
    `build_vision_backend`, so the only engines that can ever run are the ones
    already shipped for production evidence: `null`, `tesseract` (offline
    CLI), `heuristic` (no model), or `ollama` (loopback-only, already
    rejects non-local endpoints in `OllamaLocalClient`). No cloud/frontier
    model is ever reachable from here. `ocr_backend`/`vision_backend` are
    injectable, mirroring `collect_evidence` itself, so tests can substitute
    the deterministic fakes in `tests/ingest/evidence_fakes.py` instead of
    depending on tesseract/Ollama being installed.
    """
    evidence_config = evidence_config or EvidenceConfig()
    naming_evidence = gather_evidence(path, source_class="naming-lab", vocabulary=vocabulary)
    content_sha256 = sha256_file(path)

    file_evidence: FileEvidence | None = None
    document_candidates: tuple[SemanticDateCandidate, ...] = ()
    if evidence_config.enabled:
        media_facts = probe_file(path)
        source = EvidenceSource(
            path=path, sha256=content_sha256, mime=naming_evidence.mime,
            media_kind=naming_evidence.media_kind, size=naming_evidence.size,
        )
        file_evidence = collect_evidence(
            source=source, facts=media_facts.facts,
            screenshot_by_name=naming_evidence.screenshot_by_name,
            config=evidence_config,
            now=dt.datetime.now(dt.timezone.utc).isoformat(),
            ocr_backend=ocr_backend if ocr_backend is not None else build_ocr_backend(evidence_config.ocr),
            vision_backend=vision_backend if vision_backend is not None else build_vision_backend(evidence_config.vision),
        )
        native = file_evidence.native_text.text if file_evidence.native_text else ""
        text = "\n".join(part for part in (native, file_evidence.ocr_text) if part)
        document_candidates = extract_document_dates(text)

    semantic_date = derive_semantic_date(
        document_candidates=document_candidates,
        capture_date=naming_evidence.capture_date,
        original_filename=naming_evidence.original_filename,
    )

    return LabEvidence(
        sha256=content_sha256,
        naming_evidence=naming_evidence,
        file_evidence=file_evidence,
        semantic_date=semantic_date,
        document_date_candidates=document_candidates,
    )
