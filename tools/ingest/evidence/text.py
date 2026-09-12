"""Native text extraction: reading the text a file already carries.

This module answers "does this file say, in its own bytes, what it contains?"
for the formats that embed real text rather than pixels -- plain text and
markdown, PDFs with an embedded text layer, and Word's `.docx` XML format. It
never asks a model to *read* anything; a model only sees pixels, via
`raster.py`. Everything here is deterministic, in-repo code operating on
`EvidenceSource`, which is why it is allowed to take a `Path` at all (see
evidence/README-equivalent reasoning in the binding spec, invariant 4): these
extractors stream from disk because a multi-hundred-page PDF or a multi-gigabyte
log file must not be read into memory whole.

Return-value shape (read this before wiring an orchestrator against it):
    `backends/__init__.py` (owned by another worker) defines the real
    `TextExtractor` Protocol and its `ExtractionResult` dataclass. This module
    does not import that module, to avoid a work-in-progress dependency, and
    implements classes with the same *shape* instead: `name`, `version`,
    `supports(source)`, and

        extract(self, source, *, max_chars) -> tuple[
            NativeText | None,
            StructureEvidence | None,
            tuple[EvidenceWarning, ...],
            str,                          # status: "ok" | "partial" | "unavailable" | "failed"
        ]

    An orchestrator adapts this 4-tuple into `ExtractionResult`. The tuple
    order is (native_text, structure, warnings, status) throughout this file.

Discipline every extractor here follows, matching `media/probe.py`:

* **Never raises.** Every failure mode -- missing optional dependency,
  corrupt container, one unreadable page in an otherwise good PDF, a binary
  file mistaken for text -- degrades to a warning and a status, never an
  exception. A single bad file must not abort a batch.
* **Read-only.** Every extractor opens its source with `open(path, "rb")` or
  `zipfile.ZipFile(path)` in read mode. Nothing here writes a byte anywhere.
* **Bounded work.** A hostile or merely huge file (a 2 GiB log, a PDF with a
  thousand pages, a zip bomb disguised as a `.docx`) is capped before it is
  fully read, not after.
* **Sanitize on the way out.** Every string that will end up inside a
  `NativeText` passes through `sanitize_extracted_text` with the caller's
  character cap. This is a display-safety and storage-hygiene measure, not a
  prompt-injection defense -- see `types.py` for why that framing matters.
* **Omit, don't default.** A fact that cannot be established (a heading level
  a format does not support, a page number a format does not have) is left
  off the object rather than filled in with a placeholder.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
import zipfile

from tools.ingest.evidence.types import (
    EvidenceSource,
    EvidenceWarning,
    Heading,
    NativeText,
    StructureEvidence,
    TextSection,
    WARN_BACKEND_UNAVAILABLE,
    WARN_MALFORMED,
    WARN_PARTIAL,
    WARN_TOO_LARGE,
    WARN_TRUNCATED,
    WARN_UNSUPPORTED,
    sanitize_extracted_text,
)
from tools.ingest.media.probe import MEDIA_DOCUMENT, MEDIA_TEXT, TEXT_SUFFIXES, pypdf_available

_STAGE = "native_text"

# A single stray mis-encoded byte is normal in real-world text; a binary blob
# decoded with errors="replace" is mostly U+FFFD. The ratio threshold is
# chosen to catch the latter without flagging the former. Samples shorter than
# this are not judged at all -- there is not enough signal to tell "one bad
# byte in a two-byte file" from "genuinely malformed".
_REPLACEMENT_CHAR = "�"
_MALFORMED_REPLACEMENT_RATIO = 0.05
_MALFORMED_MIN_SAMPLE = 32


# --- 1. plain text / markdown ---------------------------------------------


_MARKDOWN_SUFFIXES = frozenset({".md", ".markdown"})

# Deliberately not a full CommonMark parser: this only needs to recover
# headings as structural evidence, not render the document. ATX headings
# ("# Title") and setext headings (a line underlined with "===" or "---")
# are the two forms worth the trouble; the setext case is guarded by
# requiring a real preceding non-blank line so an ordinary "---" horizontal
# rule (which is preceded by a blank line) is not mistaken for one.
_ATX_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_SETEXT_UNDERLINE_RE = re.compile(r"^(=+|-+)\s*$")


def _is_markdown(source: EvidenceSource) -> bool:
    if source.path.suffix.lower() in _MARKDOWN_SUFFIXES:
        return True
    mime = (source.mime or "").split(";", 1)[0].strip().lower()
    return mime == "text/markdown"


def _extract_markdown_headings(text: str) -> tuple[Heading, ...]:
    headings: list[Heading] = []
    previous_nonblank: str | None = None
    for line in text.split("\n"):
        atx = _ATX_HEADING_RE.match(line)
        if atx:
            heading_text = atx.group(2).strip()
            if heading_text:
                headings.append(Heading(text=heading_text, level=len(atx.group(1))))
            previous_nonblank = None
            continue
        underline = _SETEXT_UNDERLINE_RE.match(line)
        if underline and previous_nonblank:
            level = 1 if underline.group(1).startswith("=") else 2
            headings.append(Heading(text=previous_nonblank.strip(), level=level))
            previous_nonblank = None
            continue
        previous_nonblank = line if line.strip() else None
    return tuple(headings)


class PlainTextExtractor:
    """Reads text/markdown/source files, bounded by bytes read, not by size on disk."""

    name = "plain-text"
    version = "1"

    def supports(self, source: EvidenceSource) -> bool:
        if source.media_kind == MEDIA_TEXT:
            return True
        if source.path.suffix.lower() in TEXT_SUFFIXES:
            return True
        mime = (source.mime or "").split(";", 1)[0].strip().lower()
        return mime.startswith("text/")

    def extract(
        self, source: EvidenceSource, *, max_chars: int
    ) -> tuple[NativeText | None, StructureEvidence | None, tuple[EvidenceWarning, ...], str]:
        try:
            return self._extract(source, max_chars=max_chars)
        except Exception as exc:  # noqa: BLE001 - never raises, see module docstring
            return (
                None,
                None,
                (EvidenceWarning(code=WARN_MALFORMED, stage=_STAGE, detail=f"unexpected failure: {type(exc).__name__}: {exc}"),),
                "failed",
            )

    def _extract(
        self, source: EvidenceSource, *, max_chars: int
    ) -> tuple[NativeText | None, StructureEvidence | None, tuple[EvidenceWarning, ...], str]:
        # Read at most 4 bytes per requested char, from the head of the file
        # only, so a multi-gigabyte log cannot be pulled into memory just to
        # decide it is text. UTF-8 is at most 4 bytes per character, so this
        # is never short of max_chars worth of content when the file is
        # genuinely that short.
        max_bytes = max_chars * 4
        try:
            with open(source.path, "rb") as handle:
                raw = handle.read(max_bytes)
        except OSError as exc:
            return (
                None,
                None,
                (EvidenceWarning(code=WARN_MALFORMED, stage=_STAGE, detail=f"could not read file: {exc}"),),
                "failed",
            )

        if b"\x00" in raw:
            return (
                None,
                None,
                (EvidenceWarning(code=WARN_MALFORMED, stage=_STAGE, detail="NUL byte in head; treated as binary, not text"),),
                "failed",
            )

        decoded = raw.decode("utf-8", errors="replace")
        if len(decoded) >= _MALFORMED_MIN_SAMPLE:
            ratio = decoded.count(_REPLACEMENT_CHAR) / len(decoded)
            if ratio > _MALFORMED_REPLACEMENT_RATIO:
                return (
                    None,
                    None,
                    (
                        EvidenceWarning(
                            code=WARN_MALFORMED,
                            stage=_STAGE,
                            detail=f"head is {ratio:.0%} replacement characters; not valid utf-8 text",
                        ),
                    ),
                    "failed",
                )

        sanitized, truncated = sanitize_extracted_text(decoded, max_chars=max_chars)

        headings: tuple[Heading, ...] = ()
        if _is_markdown(source):
            headings = _extract_markdown_headings(sanitized)
        structure = StructureEvidence(headings=headings) if headings else None

        native = NativeText(
            text=sanitized,
            sections=(TextSection(text=sanitized, label="document"),),
            extractor=self.name,
            extractor_version=self.version,
            char_count=len(sanitized),
            truncated=truncated,
        )
        warnings: tuple[EvidenceWarning, ...] = ()
        if truncated:
            warnings = (EvidenceWarning(code=WARN_TRUNCATED, stage=_STAGE, detail=f"capped at {max_chars} chars"),)
        return native, structure, warnings, "ok"


# --- 2. PDF embedded text ---------------------------------------------------


# A PDF with thousands of pages is read up to this many and no further; the
# remainder is reported as partial rather than silently dropped. This bounds
# worst-case work independently of the character cap, which bounds output
# size -- a PDF of huge near-blank pages could otherwise pass the char cap
# late while still costing an unbounded number of `extract_text()` calls.
MAX_TEXT_PAGES = 200


def has_adequate_embedded_text(source: EvidenceSource, *, min_chars: int = 60) -> bool:
    """Cheap check: does this PDF already carry enough embedded text to skip OCR?

    Sampling is capped at the first 3 pages deliberately -- this exists so a
    router-adjacent caller can decide whether OCR is worth scheduling without
    doing the full, potentially 200-page extraction first. Never raises: any
    failure reads as "not adequate", which is the safe default (it means OCR
    still gets a chance rather than being silently skipped).
    """
    if not pypdf_available():
        return False
    try:
        from pypdf import PdfReader

        with open(source.path, "rb") as handle:
            reader = PdfReader(handle)
            if reader.is_encrypted:
                return False
            pages = reader.pages
            total = 0
            for index in range(min(len(pages), 3)):
                try:
                    total += len((pages[index].extract_text() or "").strip())
                except Exception:  # noqa: BLE001 - one bad page just contributes nothing
                    continue
                if total >= min_chars:
                    return True
            return total >= min_chars
    except Exception:  # noqa: BLE001 - never raises; "not adequate" is the safe answer
        return False


class PdfTextExtractor:
    """Reads a PDF's own embedded text layer via pypdf. No OCR, no rasterization."""

    name = "pdf-embedded-text"
    version = "1"

    def supports(self, source: EvidenceSource) -> bool:
        return source.media_kind == MEDIA_DOCUMENT and source.path.suffix.lower() == ".pdf"

    def extract(
        self, source: EvidenceSource, *, max_chars: int
    ) -> tuple[NativeText | None, StructureEvidence | None, tuple[EvidenceWarning, ...], str]:
        try:
            return self._extract(source, max_chars=max_chars)
        except Exception as exc:  # noqa: BLE001 - never raises, see module docstring
            return (
                None,
                None,
                (EvidenceWarning(code=WARN_MALFORMED, stage=_STAGE, detail=f"pdf unreadable: {type(exc).__name__}: {exc}"),),
                "failed",
            )

    def _extract(
        self, source: EvidenceSource, *, max_chars: int
    ) -> tuple[NativeText | None, StructureEvidence | None, tuple[EvidenceWarning, ...], str]:
        if not pypdf_available():
            return (
                None,
                None,
                (EvidenceWarning(code=WARN_BACKEND_UNAVAILABLE, stage=_STAGE, detail="pypdf not installed"),),
                "unavailable",
            )
        try:
            from pypdf import PdfReader
        except ImportError as exc:  # pragma: no cover - find_spec said otherwise
            return (
                None,
                None,
                (EvidenceWarning(code=WARN_BACKEND_UNAVAILABLE, stage=_STAGE, detail=f"pypdf import failed: {exc}"),),
                "unavailable",
            )

        with open(source.path, "rb") as handle:
            reader = PdfReader(handle)

            # Encrypted PDFs are never decrypted here, even with an empty
            # user password: guessing at decryption is outside this
            # extractor's job, and pypdf raises trying to touch `.pages` on
            # an encrypted, non-decrypted reader anyway.
            if reader.is_encrypted:
                return (
                    None,
                    None,
                    (EvidenceWarning(code=WARN_UNSUPPORTED, stage=_STAGE, detail="encrypted pdf; decryption not attempted"),),
                    "failed",
                )

            pages = reader.pages
            page_count = len(pages)
            read_limit = min(page_count, MAX_TEXT_PAGES)

            sections: list[TextSection] = []
            total_chars = 0
            truncated = False
            pages_with_errors = 0

            for index in range(read_limit):
                if total_chars >= max_chars:
                    truncated = True
                    break
                try:
                    page_text = pages[index].extract_text() or ""
                except Exception:  # noqa: BLE001 - one corrupt page does not abort the pdf
                    pages_with_errors += 1
                    continue

                sanitized, page_truncated = sanitize_extracted_text(page_text, max_chars=max_chars - total_chars)
                if sanitized:
                    sections.append(TextSection(text=sanitized, page=index + 1, label="page"))
                    total_chars += len(sanitized)
                if page_truncated:
                    truncated = True
                    break

            warnings: list[EvidenceWarning] = []
            if page_count > MAX_TEXT_PAGES:
                warnings.append(
                    EvidenceWarning(
                        code=WARN_PARTIAL,
                        stage=_STAGE,
                        detail=f"pdf has {page_count} pages; only the first {MAX_TEXT_PAGES} were read",
                    )
                )
            if pages_with_errors:
                warnings.append(
                    EvidenceWarning(
                        code=WARN_PARTIAL,
                        stage=_STAGE,
                        detail=f"{pages_with_errors} page(s) failed to extract text and were skipped",
                    )
                )
            if truncated:
                warnings.append(EvidenceWarning(code=WARN_TRUNCATED, stage=_STAGE, detail=f"capped at {max_chars} chars"))

            # Headings are not inferred from a PDF: a heading in a PDF is a
            # font-size/position judgement call this extractor has no basis
            # to make honestly, so page_count is the only structural fact
            # recorded.
            structure = StructureEvidence(page_count=page_count)
            full_text = "\n\n".join(section.text for section in sections)
            native = NativeText(
                text=full_text,
                sections=tuple(sections),
                extractor=self.name,
                extractor_version=self.version,
                char_count=len(full_text),
                truncated=truncated,
            )

            if pages_with_errors and not sections:
                status = "failed"
            elif pages_with_errors:
                status = "partial"
            else:
                status = "ok"
            return native, structure, tuple(warnings), status


# --- 3. docx (Office Open XML) ---------------------------------------------


# SECURITY: this extractor reads exactly two zip members, `word/document.xml`
# and `docProps/core.xml`, using `zipfile` and `xml.etree.ElementTree` only.
# It never reads `word/vbaProject.bin` (macros), `word/embeddings/*` (OLE
# objects), or any `_rels/*.rels` file (external/remote relationship
# targets), and never calls `extractall()`. A `.docx` is a zip archive an
# attacker fully controls the contents of; this extractor's job is to read
# two specific, well-known XML members off it and nothing else.
#
# Two independent caps guard against zip-bomb and billion-laughs style
# attacks: `MAX_DOCX_ZIP_ENTRIES` bounds the central directory itself (cheap
# to read but not free to iterate, per the same reasoning as
# `media/probe.py::_probe_zip`), and `MAX_DOCX_XML_BYTES` bounds
# `document.xml`. That bound is checked twice -- against the *declared*
# uncompressed size in the zip's central directory before anything is
# decompressed, and again as a hard stop on the actual bytes read -- because
# a crafted archive's declared size and its real decompressed size need not
# agree. Entity resolution is left at the stdlib `ElementTree`/`expat`
# default rather than hand-rolled, because recent `expat` already bounds
# general entity expansion; the byte cap on the input keeps whatever is fed
# to it small regardless.
MAX_DOCX_XML_BYTES = 25_000_000
MAX_DOCX_ZIP_ENTRIES = 5000
# core.xml is a small metadata part; this cap exists purely so a
# pathological one cannot cost more than a rounding error's worth of work.
_MAX_DOCX_CORE_XML_BYTES = 1_000_000

_DOCX_SUFFIXES = frozenset({".docx", ".doc"})
_NS_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_NS_DC = "{http://purl.org/dc/elements/1.1/}"
_HEADING_STYLE_RE = re.compile(r"^Heading(\d+)$", re.IGNORECASE)


def _read_zip_member_bounded(archive: zipfile.ZipFile, info: zipfile.ZipInfo, *, max_bytes: int) -> bytes | None:
    """Read one zip member, refusing anything past `max_bytes` either way.

    Declared size is checked first without decompressing anything; the
    actual read is then itself capped at `max_bytes + 1` so a declared size
    that understates the truth cannot be used to smuggle a larger payload
    through the check.
    """
    if info.file_size > max_bytes:
        return None
    with archive.open(info) as handle:
        data = handle.read(max_bytes + 1)
    if len(data) > max_bytes:
        return None
    return data


def _paragraph_text_and_heading(paragraph: ET.Element) -> tuple[str, Heading | None]:
    pieces: list[str] = []
    for node in paragraph.iter():
        tag = node.tag
        if tag == f"{_NS_W}t":
            if node.text:
                pieces.append(node.text)
        elif tag == f"{_NS_W}tab":
            pieces.append("\t")
        elif tag in (f"{_NS_W}br", f"{_NS_W}cr"):
            pieces.append("\n")
    text = "".join(pieces)

    heading: Heading | None = None
    style = paragraph.find(f"{_NS_W}pPr/{_NS_W}pStyle")
    if style is not None:
        style_id = style.get(f"{_NS_W}val", "")
        match = _HEADING_STYLE_RE.match(style_id)
        if match and text.strip():
            heading = Heading(text=text.strip(), level=int(match.group(1)))
    return text, heading


def _read_core_properties(archive: zipfile.ZipFile, infos_by_name: dict[str, zipfile.ZipInfo]) -> str:
    """Return a small "Title: ...\nAuthor: ..." block, or "" if unavailable.

    Metadata is a bonus, not a requirement: any failure here is swallowed
    rather than failing the whole extraction, because the document body is
    the evidence that actually matters.
    """
    info = infos_by_name.get("docProps/core.xml")
    if info is None:
        return ""
    try:
        data = _read_zip_member_bounded(archive, info, max_bytes=_MAX_DOCX_CORE_XML_BYTES)
        if data is None:
            return ""
        root = ET.fromstring(data)
    except Exception:  # noqa: BLE001 - metadata is optional
        return ""

    title = root.findtext(f"{_NS_DC}title") or ""
    author = root.findtext(f"{_NS_DC}creator") or ""
    lines = []
    if title.strip():
        lines.append(f"Title: {title.strip()}")
    if author.strip():
        lines.append(f"Author: {author.strip()}")
    return "\n".join(lines)


class DocxTextExtractor:
    """Reads `.docx` text and headings straight from its zip/XML parts.

    Legacy binary `.doc` is recognised (so a caller gets an informative
    warning rather than silence) but never parsed: it is a pre-XML binary
    format with no safe, dependency-free reader available here, and this
    extractor never falls back to another library to cover it.
    """

    name = "docx-xml"
    version = "1"

    def supports(self, source: EvidenceSource) -> bool:
        return source.media_kind == MEDIA_DOCUMENT and source.path.suffix.lower() in _DOCX_SUFFIXES

    def extract(
        self, source: EvidenceSource, *, max_chars: int
    ) -> tuple[NativeText | None, StructureEvidence | None, tuple[EvidenceWarning, ...], str]:
        try:
            return self._extract(source, max_chars=max_chars)
        except Exception as exc:  # noqa: BLE001 - never raises, see module docstring
            return (
                None,
                None,
                (EvidenceWarning(code=WARN_MALFORMED, stage=_STAGE, detail=f"docx unreadable: {type(exc).__name__}: {exc}"),),
                "failed",
            )

    def _extract(
        self, source: EvidenceSource, *, max_chars: int
    ) -> tuple[NativeText | None, StructureEvidence | None, tuple[EvidenceWarning, ...], str]:
        suffix = source.path.suffix.lower()
        if suffix == ".doc":
            return (
                None,
                None,
                (EvidenceWarning(code=WARN_UNSUPPORTED, stage=_STAGE, detail="legacy binary .doc is not parsed"),),
                "unavailable",
            )
        if suffix != ".docx":
            return (
                None,
                None,
                (EvidenceWarning(code=WARN_UNSUPPORTED, stage=_STAGE, detail=f"unsupported suffix {suffix!r}"),),
                "unavailable",
            )

        try:
            archive = zipfile.ZipFile(source.path)
        except zipfile.BadZipFile as exc:
            return (
                None,
                None,
                (EvidenceWarning(code=WARN_MALFORMED, stage=_STAGE, detail=f"not a valid zip container: {exc}"),),
                "failed",
            )
        except OSError as exc:
            return (
                None,
                None,
                (EvidenceWarning(code=WARN_MALFORMED, stage=_STAGE, detail=f"could not open file: {exc}"),),
                "failed",
            )

        with archive:
            infos = archive.infolist()
            if len(infos) > MAX_DOCX_ZIP_ENTRIES:
                return (
                    None,
                    None,
                    (
                        EvidenceWarning(
                            code=WARN_TOO_LARGE,
                            stage=_STAGE,
                            detail=f"zip has {len(infos)} entries, cap is {MAX_DOCX_ZIP_ENTRIES}",
                        ),
                    ),
                    "failed",
                )
            infos_by_name = {info.filename: info for info in infos}
            doc_info = infos_by_name.get("word/document.xml")
            if doc_info is None:
                return (
                    None,
                    None,
                    (EvidenceWarning(code=WARN_MALFORMED, stage=_STAGE, detail="word/document.xml missing from archive"),),
                    "failed",
                )

            xml_bytes = _read_zip_member_bounded(archive, doc_info, max_bytes=MAX_DOCX_XML_BYTES)
            if xml_bytes is None:
                return (
                    None,
                    None,
                    (
                        EvidenceWarning(
                            code=WARN_TOO_LARGE,
                            stage=_STAGE,
                            detail=f"document.xml exceeds the {MAX_DOCX_XML_BYTES} byte cap",
                        ),
                    ),
                    "failed",
                )

            try:
                root = ET.fromstring(xml_bytes)
            except ET.ParseError as exc:
                return (
                    None,
                    None,
                    (EvidenceWarning(code=WARN_MALFORMED, stage=_STAGE, detail=f"document.xml parse failed: {exc}"),),
                    "failed",
                )

            paragraphs: list[str] = []
            headings: list[Heading] = []
            for paragraph in root.iter(f"{_NS_W}p"):
                text, heading = _paragraph_text_and_heading(paragraph)
                paragraphs.append(text)
                if heading is not None:
                    headings.append(heading)

            body_text = "\n".join(paragraphs)
            metadata_text = _read_core_properties(archive, infos_by_name)

        sections: list[TextSection] = []
        remaining = max_chars
        truncated = False
        if metadata_text:
            sanitized_meta, meta_truncated = sanitize_extracted_text(metadata_text, max_chars=remaining)
            if sanitized_meta:
                sections.append(TextSection(text=sanitized_meta, label="metadata"))
                remaining -= len(sanitized_meta)
            truncated = truncated or meta_truncated

        sanitized_body, body_truncated = sanitize_extracted_text(body_text, max_chars=max(remaining, 0))
        if sanitized_body:
            sections.append(TextSection(text=sanitized_body, label="document"))
        truncated = truncated or body_truncated

        full_text = "\n\n".join(section.text for section in sections)
        structure = StructureEvidence(headings=tuple(headings)) if headings else None
        native = NativeText(
            text=full_text,
            sections=tuple(sections),
            extractor=self.name,
            extractor_version=self.version,
            char_count=len(full_text),
            truncated=truncated,
        )
        warnings: tuple[EvidenceWarning, ...] = ()
        if truncated:
            warnings = (EvidenceWarning(code=WARN_TRUNCATED, stage=_STAGE, detail=f"capped at {max_chars} chars"),)
        return native, structure, warnings, "ok"
