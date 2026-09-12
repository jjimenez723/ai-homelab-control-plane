"""The absent-backend, honestly.

`NullOCRBackend` and `NullVisionBackend` are the default OCR and vision
backends (`config.ocr.backend == "null"`, and the only OCR default -- see
`tools/ingest/config.py`). They are not a placeholder to be deleted once a
real backend exists; they are the correct answer when no backend is
configured or reachable, and they are first-class code for exactly one
reason: an absent backend must produce *recorded evidence of its absence* --
a warning, a status, a stable identity to cache against -- rather than
silently producing nothing and leaving a caller unable to tell "we looked and
found nothing" apart from "we never looked".

Both `available()` calls return `False` unconditionally and never raise;
there is nothing to detect.
"""

from __future__ import annotations

from tools.ingest.evidence.backends import OCRResult, VisionResult
from tools.ingest.evidence.types import EvidenceWarning, RasterImage, WARN_BACKEND_UNAVAILABLE


class NullOCRBackend:
    name = "null-ocr"
    version = "1"

    def available(self) -> bool:
        return False

    def recognize(self, image: RasterImage, *, timeout_seconds: int) -> OCRResult:
        return OCRResult(
            blocks=(),
            warnings=(
                EvidenceWarning(
                    code=WARN_BACKEND_UNAVAILABLE,
                    stage="ocr",
                    detail="no OCR backend is configured (backend=null)",
                ),
            ),
            status="unavailable",
        )


class NullVisionBackend:
    name = "null-vision"
    version = "1"

    def available(self) -> bool:
        return False

    def describe(self, image: RasterImage, *, timeout_seconds: int) -> VisionResult:
        return VisionResult(
            visual=None,
            warnings=(
                EvidenceWarning(
                    code=WARN_BACKEND_UNAVAILABLE,
                    stage="vision",
                    detail="no vision backend is configured (backend=null)",
                ),
            ),
            status="unavailable",
        )
