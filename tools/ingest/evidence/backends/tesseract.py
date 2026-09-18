"""Optional, offline OCR via the `tesseract` CLI.

One of exactly two modules in the evidence package permitted to use
`subprocess` -- `EVIDENCE_SUBPROCESS_JUSTIFICATIONS` in
`tests/ingest/test_mutation_authority.py` names both this module and
`backends/ffmpeg_video.py`, and fails the build for any third. Every other
module under `tools/ingest/evidence/` must stay free of processes and
sockets; these two are where "run an external offline binary, with a hard
timeout and no shell" is allowed to live, because tesseract is the only OCR
engine this project ships that needs no Python package, only a system
binary, and confining the exception to justified, reviewed files keeps it
auditable.

Detection follows `media/probe.py`'s pattern for optional tooling
(`ffprobe_available`/`_probe_av`): `tesseract_version()` is checked once per
process via `shutil.which` plus a short-timeout `--version` call, and a
missing or broken binary degrades to `None` rather than raising anywhere.

Recognition writes NO temporary file. `tesseract stdin stdout tsv` is invoked
with the PNG bytes fed on its stdin and the TSV read back from its stdout --
verified to work on this machine before this module was written (see the
worker report; if a future machine's tesseract build does not support the
`stdin`/`stdout` pseudo-filenames, this backend must report itself
unavailable rather than fall back to a temp file, because zero filesystem
writes is an invariant of the whole evidence layer, not a preference).

`TesseractOCRBackend.version` folds in the detected tesseract version string
(`f"1+{tesseract_version()}"`), so upgrading the system's tesseract binary
naturally invalidates any evidence cached under the old version -- the same
reasoning `probe.py::PROBE_VERSION` documents for its own cache-busting.
"""

from __future__ import annotations

import shutil
import subprocess
from functools import lru_cache

from tools.ingest.evidence.backends import OCRResult
from tools.ingest.evidence.types import (
    EvidenceWarning,
    OCRBlock,
    Polygon,
    RasterImage,
    WARN_BACKEND_UNAVAILABLE,
    WARN_MALFORMED,
    WARN_TIMEOUT,
    WARN_TRUNCATED,
    sanitize_extracted_text,
)

# Short: this only ever runs `tesseract --version`, which does no image work.
_VERSION_PROBE_TIMEOUT_SECONDS = 10

# The TSV header tesseract's `tsv` output config emits. Looked up by name
# rather than assumed by position, so a future tesseract that reorders or
# adds columns still parses correctly.
_TSV_COLUMNS = (
    "level", "page_num", "block_num", "par_num", "line_num", "word_num",
    "left", "top", "width", "height", "conf", "text",
)

# `level == 5` is tesseract's word level (1=page, 2=block, 3=paragraph,
# 4=line, 5=word). Only word rows carry recognized text.
_TSV_WORD_LEVEL = 5

# A single OCR word is never a whole document; this is generous headroom
# against a pathological single "word" tesseract somehow reports as huge,
# not a realistic limit in normal use.
_MAX_WORD_TEXT_CHARS = 10_000


@lru_cache(maxsize=1)
def tesseract_version() -> str | None:
    """The installed tesseract's version string, or None if unusable.

    Cached for the life of the process: a batch run probes many files, and
    "is tesseract installed" cannot change partway through one run. Callers
    (and tests) that need to simulate a different answer patch this function
    rather than the environment, matching `probe.py::ffprobe_available`.
    """
    if shutil.which("tesseract") is None:
        return None
    try:
        completed = subprocess.run(
            ["tesseract", "--version"],
            capture_output=True,
            timeout=_VERSION_PROBE_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    text = (completed.stdout or completed.stderr).decode("utf-8", "replace").strip()
    if not text:
        return None
    first_line = text.splitlines()[0]
    parts = first_line.split()
    # Expected shape: "tesseract 5.5.3". Fall back to the raw first line
    # rather than None so an unexpected banner still yields *a* distinct,
    # cache-busting version string instead of masquerading as "absent".
    if len(parts) >= 2 and parts[0].lower() == "tesseract":
        return parts[1]
    return first_line


def _first_line(raw: bytes) -> str:
    text = raw.decode("utf-8", "replace").strip()
    return text.splitlines()[0] if text else ""


def _parse_tsv(
    tsv_text: str, *, page: int, engine_version: str
) -> tuple[list[OCRBlock], list[EvidenceWarning]]:
    """Turn tesseract's TSV into line-level `OCRBlock`s.

    Rows are grouped by (`block_num`, `par_num`, `line_num`) rather than kept
    as one block per word: a line is the unit a later naming/reading stage can
    actually use ("Invoice Total: $1,204.50"), whereas individual words lose
    all phrasing and would have to be re-joined downstream anyway, with no
    principled way to recover spacing or punctuation adjacency.

    `page` comes from the caller's `RasterImage.page`, not the TSV's own
    `page_num` column: tesseract is invoked once per already-rasterized page,
    so its `page_num` is always `1` for this single-image call and carries no
    information about where that page sits in the source document.
    """
    warnings: list[EvidenceWarning] = []
    lines = tsv_text.split("\n")
    if not lines or not lines[0].strip():
        return [], [EvidenceWarning(code=WARN_MALFORMED, stage="ocr", detail="tesseract produced no TSV output")]

    header = lines[0].rstrip("\r").split("\t")
    try:
        column_index = {name: header.index(name) for name in _TSV_COLUMNS}
    except ValueError:
        return [], [
            EvidenceWarning(
                code=WARN_MALFORMED, stage="ocr", detail=f"unexpected tesseract TSV header: {header!r}"
            )
        ]

    # key -> list of word dicts, in first-seen order so line assembly below
    # does not depend on dict ordering being meaningful (it also happens to
    # be, on CPython, but the explicit `order` list keeps that assumption out).
    words_by_line: dict[tuple[int, int, int], list[dict]] = {}
    order: list[tuple[int, int, int]] = []
    malformed_rows = 0

    for raw_line in lines[1:]:
        if not raw_line.strip():
            continue
        fields = raw_line.rstrip("\r").split("\t")
        if len(fields) < len(_TSV_COLUMNS):
            malformed_rows += 1
            continue
        try:
            level = int(fields[column_index["level"]])
        except ValueError:
            malformed_rows += 1
            continue
        if level != _TSV_WORD_LEVEL:
            continue  # page/block/paragraph/line summary rows carry no text
        try:
            confidence = float(fields[column_index["conf"]])
        except ValueError:
            malformed_rows += 1
            continue
        if confidence < 0:
            continue  # tesseract's "no confidence" sentinel, not a real word
        text = fields[column_index["text"]]
        if not text.strip():
            continue
        try:
            block_num = int(fields[column_index["block_num"]])
            par_num = int(fields[column_index["par_num"]])
            line_num = int(fields[column_index["line_num"]])
            word_num = int(fields[column_index["word_num"]])
            left = int(float(fields[column_index["left"]]))
            top = int(float(fields[column_index["top"]]))
            width = int(float(fields[column_index["width"]]))
            height = int(float(fields[column_index["height"]]))
        except ValueError:
            malformed_rows += 1
            continue

        key = (block_num, par_num, line_num)
        if key not in words_by_line:
            words_by_line[key] = []
            order.append(key)
        words_by_line[key].append(
            {
                "word_num": word_num,
                "left": left,
                "top": top,
                "width": width,
                "height": height,
                "conf": confidence,
                "text": text,
            }
        )

    blocks: list[OCRBlock] = []
    for key in order:
        words = sorted(words_by_line[key], key=lambda w: w["word_num"])
        clean_words: list[tuple[str, dict]] = []
        for word in words:
            clean_text, _ = sanitize_extracted_text(word["text"], max_chars=_MAX_WORD_TEXT_CHARS)
            if clean_text:
                clean_words.append((clean_text, word))
        if not clean_words:
            continue

        line_text = " ".join(text for text, _ in clean_words)
        left = min(word["left"] for _, word in clean_words)
        top = min(word["top"] for _, word in clean_words)
        right = max(word["left"] + word["width"] for _, word in clean_words)
        bottom = max(word["top"] + word["height"] for _, word in clean_words)
        mean_confidence = sum(word["conf"] for _, word in clean_words) / len(clean_words) / 100.0

        blocks.append(
            OCRBlock(
                text=line_text,
                polygon=Polygon.from_bbox(left, top, right, bottom),
                page=page,
                confidence=max(0.0, min(1.0, mean_confidence)),
                reading_order=0,  # the orchestrator assigns real order
                engine="tesseract",
                engine_version=engine_version,
            )
        )

    if malformed_rows:
        warnings.append(
            EvidenceWarning(
                code=WARN_MALFORMED,
                stage="ocr",
                detail=f"{malformed_rows} tesseract TSV row(s) could not be parsed and were skipped",
            )
        )

    return blocks, warnings


class TesseractOCRBackend:
    """OCR via a locally installed `tesseract` binary. No Python dependency."""

    name = "tesseract-ocr"

    def __init__(self, *, max_blocks: int = 2000) -> None:
        self._max_blocks = max_blocks

    @property
    def version(self) -> str:
        detected = tesseract_version()
        return f"1+{detected}" if detected else "1+absent"

    def available(self) -> bool:
        return tesseract_version() is not None

    def recognize(self, image: RasterImage, *, timeout_seconds: int) -> OCRResult:
        if not self.available():
            return OCRResult(
                blocks=(),
                warnings=(
                    EvidenceWarning(
                        code=WARN_BACKEND_UNAVAILABLE,
                        stage="ocr",
                        detail="tesseract binary not found on PATH",
                    ),
                ),
                status="unavailable",
            )

        try:
            completed = subprocess.run(
                ["tesseract", "stdin", "stdout", "tsv"],
                input=image.png_bytes,
                capture_output=True,
                timeout=timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return OCRResult(
                blocks=(),
                warnings=(
                    EvidenceWarning(
                        code=WARN_TIMEOUT, stage="ocr", detail=f"tesseract timed out after {timeout_seconds}s"
                    ),
                ),
                status="failed",
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return OCRResult(
                blocks=(),
                warnings=(
                    EvidenceWarning(
                        code=WARN_BACKEND_UNAVAILABLE,
                        stage="ocr",
                        detail=f"tesseract could not be executed: {type(exc).__name__}: {exc}",
                    ),
                ),
                status="unavailable",
            )

        if completed.returncode != 0:
            return OCRResult(
                blocks=(),
                warnings=(
                    EvidenceWarning(
                        code=WARN_MALFORMED,
                        stage="ocr",
                        detail=f"tesseract exited {completed.returncode}: {_first_line(completed.stderr)}",
                    ),
                ),
                status="failed",
            )

        try:
            tsv_text = completed.stdout.decode("utf-8", "replace")
        except Exception as exc:  # noqa: BLE001 - defence in depth; bytes.decode with "replace" should not raise
            return OCRResult(
                blocks=(),
                warnings=(
                    EvidenceWarning(
                        code=WARN_MALFORMED,
                        stage="ocr",
                        detail=f"tesseract output could not be decoded: {type(exc).__name__}: {exc}",
                    ),
                ),
                status="failed",
            )

        try:
            blocks, warnings = _parse_tsv(tsv_text, page=image.page, engine_version=self.version)
        except Exception as exc:  # noqa: BLE001 - never raise out of a backend
            return OCRResult(
                blocks=(),
                warnings=(
                    EvidenceWarning(
                        code=WARN_MALFORMED,
                        stage="ocr",
                        detail=f"tesseract TSV parsing failed unexpectedly: {type(exc).__name__}: {exc}",
                    ),
                ),
                status="failed",
            )

        if len(blocks) > self._max_blocks:
            blocks = blocks[: self._max_blocks]
            warnings.append(
                EvidenceWarning(
                    code=WARN_TRUNCATED, stage="ocr", detail=f"OCR blocks capped at {self._max_blocks}"
                )
            )

        malformed = any(w.code == WARN_MALFORMED for w in warnings)
        if malformed and not blocks:
            status = "failed"
        elif malformed:
            status = "partial"
        else:
            status = "ok"

        return OCRResult(blocks=tuple(blocks), warnings=tuple(warnings), status=status)
