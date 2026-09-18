"""Native text extraction for spreadsheets, presentations, and CSV/TSV.

This module extends `tools/ingest/evidence/text.py`'s native-text seam
(D-029: evidence is perception, never authority) to three more formats that
carry their own text rather than pixels: Office Open XML spreadsheets
(`.xlsx`, and macro-enabled `.xlsm`, read identically), Office Open XML
presentations (`.pptx`), and delimited text (`.csv`, `.tsv`). It is not a new
decision (D-029 already covers "native text, no OCR" for office documents;
see `docs/17-file-evidence.md`), and it makes no new one: everything read
here is untrusted, inert data -- see `types.py::sanitize_extracted_text` and
the module docstring on `text.py` for the invariants this file inherits
verbatim:

* **Never raises.** Every failure mode degrades to a warning and a status.
* **Read-only.** Every extractor opens its source with `zipfile.ZipFile(path)`
  (or a bounded `open(path, "rb")` for CSV/TSV) in read mode. Nothing here
  writes a byte anywhere.
* **Bounded work.** A hostile or merely huge file is capped before it is
  fully read, not after.
* **Sanitize on the way out.** Every string passes through
  `sanitize_extracted_text` with the caller's character cap.
* **Omit, don't default.** A sheet or slide this module cannot honestly
  resolve is skipped with a warning, never guessed at.

Return-value shape matches `text.py` exactly (see its docstring for the full
rationale): each extractor is `name`, `version`, `supports(source)`, and

    extract(self, source, *, max_chars) -> tuple[
        NativeText | None, StructureEvidence | None,
        tuple[EvidenceWarning, ...], str,   # status
    ]

SECURITY: `.xlsx`/`.xlsm`/`.pptx` are zip archives an attacker fully
controls the contents of. This module reads a small, fixed set of
well-known parts and nothing else:

* `XlsxTextExtractor` reads `xl/workbook.xml`, `xl/_rels/workbook.xml.rels`,
  `xl/sharedStrings.xml`, and `xl/worksheets/sheetN.xml`. It never reads
  `xl/vbaProject.bin` (VBA macros -- present in `.xlsm`), `xl/media/*`,
  `xl/embeddings/*`, or any other `_rels/*.rels` file. Cell **formulas**
  (`<f>`) are never read at all, only cached **values** (`<v>`) and shared
  or inline strings -- there is no formula evaluator here, and this module
  does not become one by omission: it simply never looks at formula text.
* `PptxTextExtractor` reads `ppt/presentation.xml`,
  `ppt/_rels/presentation.xml.rels`, and `ppt/slides/slideN.xml`. It never
  reads `ppt/notesSlides/*` (speaker notes are deliberately excluded, not
  merely unread by accident), `ppt/media/*`, `ppt/embeddings/*`, or any
  per-slide `_rels` file (so an external or embedded object a slide links to
  is never followed).
* Neither ever calls `extractall()`.

As with `DocxTextExtractor`, entry count is capped
(`MAX_OFFICE_ZIP_ENTRIES`) before the central directory is iterated in full,
and every member this module does read is bounded twice: against its
*declared* uncompressed size before anything is decompressed
(`MAX_OFFICE_XML_BYTES` / `_MAX_RELS_BYTES`), and again as a hard stop on the
bytes actually read, because a crafted archive's declared size and its real
decompressed size need not agree. Entity resolution is left at the stdlib
`ElementTree`/`expat` default, exactly as in `text.py`, because recent
`expat` already bounds general entity expansion (a billion-laughs payload
raises `ParseError` rather than expanding) and the byte cap on the input
keeps whatever reaches it small regardless.

`CsvTextExtractor` is plain-text bounded reading (see `PlainTextExtractor`)
plus `csv.Sniffer` to guess the delimiter; it never shells out and never
imports a third-party CSV/Excel library.
"""

from __future__ import annotations

import csv
import io
import re
import xml.etree.ElementTree as ET
import zipfile

from tools.ingest.evidence.types import (
    EvidenceSource,
    EvidenceWarning,
    NativeText,
    StructureEvidence,
    TextSection,
    WARN_MALFORMED,
    WARN_PARTIAL,
    WARN_TOO_LARGE,
    WARN_TRUNCATED,
    sanitize_extracted_text,
)
from tools.ingest.media.probe import MEDIA_DOCUMENT

_STAGE = "native_text"

# Mirrors `text.py`'s malformed-binary heuristic exactly (see its comment):
# a genuinely binary file decoded with errors="replace" is mostly U+FFFD.
_REPLACEMENT_CHAR = "�"
_MALFORMED_REPLACEMENT_RATIO = 0.05
_MALFORMED_MIN_SAMPLE = 32

# -- shared zip-container guards, mirroring text.py's DocxTextExtractor -----

MAX_OFFICE_ZIP_ENTRIES = 5000
# Declared-uncompressed-size cap for the XML parts this module reads
# (workbook.xml, sharedStrings.xml, one worksheet, presentation.xml, one
# slide). Same value and same reasoning as `text.py::MAX_DOCX_XML_BYTES`.
MAX_OFFICE_XML_BYTES = 25_000_000
# Relationship files are small, fixed-shape metadata; this cap exists so a
# pathological one cannot cost more than a rounding error's worth of work,
# matching `text.py`'s `_MAX_DOCX_CORE_XML_BYTES`.
_MAX_RELS_BYTES = 1_000_000

_NS_PACKAGE_REL = "{http://schemas.openxmlformats.org/package/2006/relationships}"


def _read_zip_member_bounded(archive: zipfile.ZipFile, info: zipfile.ZipInfo, *, max_bytes: int) -> bytes | None:
    """Read one zip member, refusing anything past `max_bytes` either way.

    Identical in shape to `text.py::_read_zip_member_bounded`, duplicated
    (rather than imported) so this module has no dependency on `text.py` --
    see the module docstring on keeping the office extractors self-contained
    in one file. Declared size is checked first without decompressing
    anything; the actual read is then itself capped at `max_bytes + 1` so a
    declared size that understates the truth cannot smuggle a larger payload
    through the check.
    """
    if info.file_size > max_bytes:
        return None
    with archive.open(info) as handle:
        data = handle.read(max_bytes + 1)
    if len(data) > max_bytes:
        return None
    return data


def _parse_relationships(
    archive: zipfile.ZipFile, infos_by_name: dict[str, zipfile.ZipInfo], rels_name: str
) -> dict[str, str]:
    """Return `{relationship id: target}` from an OOXML `.rels` part, or `{}`.

    Any failure -- the part is absent, oversized, or malformed -- degrades to
    an empty mapping rather than raising; callers treat an unresolved sheet
    or slide as "skip with a warning", never as "guess".
    """
    info = infos_by_name.get(rels_name)
    if info is None:
        return {}
    try:
        data = _read_zip_member_bounded(archive, info, max_bytes=_MAX_RELS_BYTES)
        if data is None:
            return {}
        root = ET.fromstring(data)
    except Exception:  # noqa: BLE001 - relationship resolution is best-effort
        return {}
    mapping: dict[str, str] = {}
    for rel in root.iter(f"{_NS_PACKAGE_REL}Relationship"):
        rid, target = rel.get("Id"), rel.get("Target")
        if rid and target:
            mapping[rid] = target
    return mapping


def _resolve_part(base_dir: str, target: str) -> str:
    """Join a relationship `Target` against the directory of the part that referenced it."""
    if target.startswith("/"):
        return target.lstrip("/")
    return f"{base_dir}/{target}"


def _open_zip(source_path) -> tuple[zipfile.ZipFile | None, EvidenceWarning | None]:
    try:
        return zipfile.ZipFile(source_path), None
    except zipfile.BadZipFile as exc:
        return None, EvidenceWarning(code=WARN_MALFORMED, stage=_STAGE, detail=f"not a valid zip container: {exc}")
    except OSError as exc:
        return None, EvidenceWarning(code=WARN_MALFORMED, stage=_STAGE, detail=f"could not open file: {exc}")


# --- 1. xlsx / xlsm (Office Open XML spreadsheet) ---------------------------

_XLSX_SUFFIXES = frozenset({".xlsx", ".xlsm"})
_NS_SS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_NS_R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"

# "First N sheets" / "first N rows per sheet": these bound the *evidence*
# this module produces, independent of the security byte caps above -- a
# workbook with a thousand sheets or a million-row sheet still costs at most
# one bounded XML parse per part, but only this much of it becomes text.
MAX_SHEETS = 50
MAX_ROWS_PER_SHEET = 200


def _sheet_cell_text(cell: ET.Element, shared_strings: list[str]) -> str:
    cell_type = cell.get("t")
    if cell_type == "inlineStr":
        inline = cell.find(f"{_NS_SS}is")
        if inline is None:
            return ""
        return "".join(node.text or "" for node in inline.iter(f"{_NS_SS}t"))

    # Deliberately never `cell.find(f"{_NS_SS}f")`: a formula's source text
    # is not a value, and this module has no formula evaluator to run it
    # through. Only the cached `<v>` the file's own author computed is read.
    value_el = cell.find(f"{_NS_SS}v")
    raw = value_el.text if value_el is not None else None
    if raw is None:
        return ""
    if cell_type == "s":
        try:
            index = int(raw)
        except ValueError:
            return ""
        return shared_strings[index] if 0 <= index < len(shared_strings) else ""
    return raw


class XlsxTextExtractor:
    """Reads `.xlsx`/`.xlsm` sheet names and a bounded sample of rows.

    `.xlsm` (macro-enabled) is treated identically to `.xlsx`: the only
    difference between the two formats is the optional presence of
    `xl/vbaProject.bin`, which this extractor never opens either way. Macros
    are never executed, inspected, or referenced.
    """

    name = "xlsx-xml"
    version = "1"

    def supports(self, source: EvidenceSource) -> bool:
        return source.media_kind == MEDIA_DOCUMENT and source.path.suffix.lower() in _XLSX_SUFFIXES

    def extract(
        self, source: EvidenceSource, *, max_chars: int
    ) -> tuple[NativeText | None, StructureEvidence | None, tuple[EvidenceWarning, ...], str]:
        try:
            return self._extract(source, max_chars=max_chars)
        except Exception as exc:  # noqa: BLE001 - never raises, see module docstring
            return (
                None,
                None,
                (EvidenceWarning(code=WARN_MALFORMED, stage=_STAGE, detail=f"xlsx unreadable: {type(exc).__name__}: {exc}"),),
                "failed",
            )

    def _extract(
        self, source: EvidenceSource, *, max_chars: int
    ) -> tuple[NativeText | None, StructureEvidence | None, tuple[EvidenceWarning, ...], str]:
        archive, open_warning = _open_zip(source.path)
        if archive is None:
            return None, None, (open_warning,), "failed"

        with archive:
            infos = archive.infolist()
            if len(infos) > MAX_OFFICE_ZIP_ENTRIES:
                return (
                    None,
                    None,
                    (EvidenceWarning(code=WARN_TOO_LARGE, stage=_STAGE, detail=f"zip has {len(infos)} entries, cap is {MAX_OFFICE_ZIP_ENTRIES}"),),
                    "failed",
                )
            infos_by_name = {info.filename: info for info in infos}

            workbook_info = infos_by_name.get("xl/workbook.xml")
            if workbook_info is None:
                return (
                    None,
                    None,
                    (EvidenceWarning(code=WARN_MALFORMED, stage=_STAGE, detail="xl/workbook.xml missing from archive"),),
                    "failed",
                )
            workbook_bytes = _read_zip_member_bounded(archive, workbook_info, max_bytes=MAX_OFFICE_XML_BYTES)
            if workbook_bytes is None:
                return (
                    None,
                    None,
                    (EvidenceWarning(code=WARN_TOO_LARGE, stage=_STAGE, detail=f"workbook.xml exceeds the {MAX_OFFICE_XML_BYTES} byte cap"),),
                    "failed",
                )
            try:
                workbook_root = ET.fromstring(workbook_bytes)
            except ET.ParseError as exc:
                return (
                    None,
                    None,
                    (EvidenceWarning(code=WARN_MALFORMED, stage=_STAGE, detail=f"workbook.xml parse failed: {exc}"),),
                    "failed",
                )

            declared_sheets = [
                (sheet_el.get("name") or "", sheet_el.get(f"{_NS_R}id"))
                for sheet_el in workbook_root.iter(f"{_NS_SS}sheet")
            ]
            rels = _parse_relationships(archive, infos_by_name, "xl/_rels/workbook.xml.rels")

            shared_strings: list[str] = []
            shared_info = infos_by_name.get("xl/sharedStrings.xml")
            if shared_info is not None:
                shared_bytes = _read_zip_member_bounded(archive, shared_info, max_bytes=MAX_OFFICE_XML_BYTES)
                if shared_bytes is not None:
                    try:
                        shared_root = ET.fromstring(shared_bytes)
                        for si in shared_root.iter(f"{_NS_SS}si"):
                            shared_strings.append("".join(node.text or "" for node in si.iter(f"{_NS_SS}t")))
                    except ET.ParseError:
                        pass  # shared strings are a bonus; a bad pool degrades to empty cells, not failure

            sections: list[TextSection] = []
            remaining = max_chars
            truncated = False
            unresolved_sheets = 0  # no relationship/part could be found for this sheet
            too_large_sheets = 0  # declared or actual size exceeded MAX_OFFICE_XML_BYTES
            malformed_sheets = 0  # part existed and was in bounds, but did not parse

            sheets_to_read = declared_sheets[:MAX_SHEETS]
            for sheet_name, rid in sheets_to_read:
                if remaining <= 0:
                    truncated = True
                    break
                target = rels.get(rid) if rid else None
                part_name = _resolve_part("xl", target) if target else None
                sheet_info = infos_by_name.get(part_name) if part_name else None
                if sheet_info is None:
                    unresolved_sheets += 1
                    continue

                sheet_bytes = _read_zip_member_bounded(archive, sheet_info, max_bytes=MAX_OFFICE_XML_BYTES)
                if sheet_bytes is None:
                    too_large_sheets += 1
                    continue
                try:
                    sheet_root = ET.fromstring(sheet_bytes)
                except ET.ParseError:
                    malformed_sheets += 1
                    continue

                row_lines: list[str] = []
                for row_index, row_el in enumerate(sheet_root.iter(f"{_NS_SS}row")):
                    if row_index >= MAX_ROWS_PER_SHEET:
                        break
                    cells = [_sheet_cell_text(cell, shared_strings) for cell in row_el.findall(f"{_NS_SS}c")]
                    row_lines.append("\t".join(cells))

                sheet_text = f"{sheet_name}\n" + "\n".join(row_lines) if sheet_name else "\n".join(row_lines)
                sanitized, section_truncated = sanitize_extracted_text(sheet_text, max_chars=remaining)
                if sanitized:
                    sections.append(TextSection(text=sanitized, label="sheet"))
                    remaining -= len(sanitized)
                if section_truncated:
                    truncated = True
                    break

            warnings: list[EvidenceWarning] = []
            if len(declared_sheets) > MAX_SHEETS:
                warnings.append(EvidenceWarning(code=WARN_PARTIAL, stage=_STAGE, detail=f"workbook has {len(declared_sheets)} sheets; only the first {MAX_SHEETS} were read"))
            if unresolved_sheets:
                warnings.append(EvidenceWarning(code=WARN_PARTIAL, stage=_STAGE, detail=f"{unresolved_sheets} sheet(s) could not be resolved to a worksheet part and were skipped"))
            if too_large_sheets:
                warnings.append(EvidenceWarning(code=WARN_TOO_LARGE, stage=_STAGE, detail=f"{too_large_sheets} sheet(s) exceeded the {MAX_OFFICE_XML_BYTES} byte cap and were skipped"))
            if malformed_sheets:
                warnings.append(EvidenceWarning(code=WARN_MALFORMED, stage=_STAGE, detail=f"{malformed_sheets} sheet(s) failed to parse and were skipped"))
            if truncated:
                warnings.append(EvidenceWarning(code=WARN_TRUNCATED, stage=_STAGE, detail=f"capped at {max_chars} chars"))

        bad_sheets = unresolved_sheets + too_large_sheets + malformed_sheets
        full_text = "\n\n".join(section.text for section in sections)
        native = NativeText(
            text=full_text, sections=tuple(sections), extractor=self.name, extractor_version=self.version,
            char_count=len(full_text), truncated=truncated,
        )
        if bad_sheets and not sections:
            status = "failed"
        elif bad_sheets:
            status = "partial"
        else:
            status = "ok"
        return native, None, tuple(warnings), status


# --- 2. pptx (Office Open XML presentation) ---------------------------------

_NS_P = "{http://schemas.openxmlformats.org/presentationml/2006/main}"
_NS_A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
_TITLE_PLACEHOLDER_TYPES = frozenset({"title", "ctrTitle"})
_SLIDE_FALLBACK_RE = re.compile(r"^ppt/slides/slide(\d+)\.xml$")

MAX_SLIDES = 200


def _shape_text_lines(shape: ET.Element) -> str:
    lines = []
    for paragraph in shape.findall(f".//{_NS_A}p"):
        line = "".join(node.text or "" for node in paragraph.iter(f"{_NS_A}t"))
        lines.append(line)
    return "\n".join(lines)


def _is_title_shape(shape: ET.Element) -> bool:
    placeholder = shape.find(f"{_NS_P}nvSpPr/{_NS_P}nvPr/{_NS_P}ph")
    return placeholder is not None and (placeholder.get("type") in _TITLE_PLACEHOLDER_TYPES)


def _slide_title_and_body(slide_root: ET.Element) -> tuple[str, str]:
    """Title-placeholder text (if any) and the rest of the slide's shape text.

    Notes are structurally excluded: this only ever looks at the slide part
    itself (`ppt/slides/slideN.xml`), never `ppt/notesSlides/*`.
    """
    title = ""
    body_parts: list[str] = []
    for shape in slide_root.iter(f"{_NS_P}sp"):
        text = _shape_text_lines(shape)
        if not text.strip():
            continue
        if not title and _is_title_shape(shape):
            title = text
        else:
            body_parts.append(text)
    return title, "\n".join(body_parts)


def _ordered_slide_parts(archive: zipfile.ZipFile, infos_by_name: dict[str, zipfile.ZipInfo]) -> list[str]:
    """Slide part names in presentation order, or a best-effort fallback.

    Primary path resolves `ppt/presentation.xml`'s `<p:sldIdLst>` through
    `ppt/_rels/presentation.xml.rels`, the same relationship-based approach
    `XlsxTextExtractor` uses for sheet order. If either part is absent or
    fails to parse, this falls back to every `ppt/slides/slideN.xml` entry
    sorted by its numeric filename -- a strong convention in practice, and
    strictly a fallback, never the primary source of truth.
    """
    pres_info = infos_by_name.get("ppt/presentation.xml")
    if pres_info is not None:
        pres_bytes = _read_zip_member_bounded(archive, pres_info, max_bytes=MAX_OFFICE_XML_BYTES)
        if pres_bytes is not None:
            try:
                pres_root = ET.fromstring(pres_bytes)
                rels = _parse_relationships(archive, infos_by_name, "ppt/_rels/presentation.xml.rels")
                ordered: list[str] = []
                for slide_id in pres_root.iter(f"{_NS_P}sldId"):
                    rid = slide_id.get(f"{_NS_R}id")
                    target = rels.get(rid) if rid else None
                    if target:
                        part = _resolve_part("ppt", target)
                        if part in infos_by_name:
                            ordered.append(part)
                if ordered:
                    return ordered
            except ET.ParseError:
                pass

    fallback: list[tuple[int, str]] = []
    for filename in infos_by_name:
        match = _SLIDE_FALLBACK_RE.match(filename)
        if match:
            fallback.append((int(match.group(1)), filename))
    return [name for _, name in sorted(fallback)]


class PptxTextExtractor:
    """Reads `.pptx` slide titles and body text, in slide order. No notes."""

    name = "pptx-xml"
    version = "1"

    def supports(self, source: EvidenceSource) -> bool:
        return source.media_kind == MEDIA_DOCUMENT and source.path.suffix.lower() == ".pptx"

    def extract(
        self, source: EvidenceSource, *, max_chars: int
    ) -> tuple[NativeText | None, StructureEvidence | None, tuple[EvidenceWarning, ...], str]:
        try:
            return self._extract(source, max_chars=max_chars)
        except Exception as exc:  # noqa: BLE001 - never raises, see module docstring
            return (
                None,
                None,
                (EvidenceWarning(code=WARN_MALFORMED, stage=_STAGE, detail=f"pptx unreadable: {type(exc).__name__}: {exc}"),),
                "failed",
            )

    def _extract(
        self, source: EvidenceSource, *, max_chars: int
    ) -> tuple[NativeText | None, StructureEvidence | None, tuple[EvidenceWarning, ...], str]:
        archive, open_warning = _open_zip(source.path)
        if archive is None:
            return None, None, (open_warning,), "failed"

        with archive:
            infos = archive.infolist()
            if len(infos) > MAX_OFFICE_ZIP_ENTRIES:
                return (
                    None,
                    None,
                    (EvidenceWarning(code=WARN_TOO_LARGE, stage=_STAGE, detail=f"zip has {len(infos)} entries, cap is {MAX_OFFICE_ZIP_ENTRIES}"),),
                    "failed",
                )
            infos_by_name = {info.filename: info for info in infos}

            slide_parts = _ordered_slide_parts(archive, infos_by_name)
            if not slide_parts:
                return (
                    None,
                    None,
                    (EvidenceWarning(code=WARN_MALFORMED, stage=_STAGE, detail="no slides could be resolved in this presentation"),),
                    "failed",
                )

            sections: list[TextSection] = []
            remaining = max_chars
            truncated = False
            unresolved_slides = 0  # no part found under this slide's resolved name
            too_large_slides = 0  # declared or actual size exceeded MAX_OFFICE_XML_BYTES
            malformed_slides = 0  # part existed and was in bounds, but did not parse
            total_slides = len(slide_parts)
            slides_to_read = slide_parts[:MAX_SLIDES]

            for slide_index, part_name in enumerate(slides_to_read, start=1):
                if remaining <= 0:
                    truncated = True
                    break
                slide_info = infos_by_name.get(part_name)
                if slide_info is None:
                    unresolved_slides += 1
                    continue
                slide_bytes = _read_zip_member_bounded(archive, slide_info, max_bytes=MAX_OFFICE_XML_BYTES)
                if slide_bytes is None:
                    too_large_slides += 1
                    continue
                try:
                    slide_root = ET.fromstring(slide_bytes)
                except ET.ParseError:
                    malformed_slides += 1
                    continue

                title, body = _slide_title_and_body(slide_root)
                combined = f"{title}\n\n{body}".strip() if title else body
                if not combined.strip():
                    continue
                sanitized, section_truncated = sanitize_extracted_text(combined, max_chars=remaining)
                if sanitized:
                    sections.append(TextSection(text=sanitized, page=slide_index, label="slide"))
                    remaining -= len(sanitized)
                if section_truncated:
                    truncated = True
                    break

            warnings: list[EvidenceWarning] = []
            if total_slides > MAX_SLIDES:
                warnings.append(EvidenceWarning(code=WARN_PARTIAL, stage=_STAGE, detail=f"presentation has {total_slides} slides; only the first {MAX_SLIDES} were read"))
            if unresolved_slides:
                warnings.append(EvidenceWarning(code=WARN_PARTIAL, stage=_STAGE, detail=f"{unresolved_slides} slide(s) could not be resolved to a slide part and were skipped"))
            if too_large_slides:
                warnings.append(EvidenceWarning(code=WARN_TOO_LARGE, stage=_STAGE, detail=f"{too_large_slides} slide(s) exceeded the {MAX_OFFICE_XML_BYTES} byte cap and were skipped"))
            if malformed_slides:
                warnings.append(EvidenceWarning(code=WARN_MALFORMED, stage=_STAGE, detail=f"{malformed_slides} slide(s) failed to parse and were skipped"))
            if truncated:
                warnings.append(EvidenceWarning(code=WARN_TRUNCATED, stage=_STAGE, detail=f"capped at {max_chars} chars"))

        bad_slides = unresolved_slides + too_large_slides + malformed_slides
        full_text = "\n\n".join(section.text for section in sections)
        native = NativeText(
            text=full_text, sections=tuple(sections), extractor=self.name, extractor_version=self.version,
            char_count=len(full_text), truncated=truncated,
        )
        if bad_slides and not sections:
            status = "failed"
        elif bad_slides:
            status = "partial"
        else:
            status = "ok"
        return native, None, tuple(warnings), status


# --- 3. csv / tsv -----------------------------------------------------------

_CSV_SUFFIXES = frozenset({".csv", ".tsv"})
_CSV_SNIFF_SAMPLE_CHARS = 8192
_CSV_SNIFF_DELIMITERS = ",;\t|"

# "First N rows": bounds the evidence, not the read -- the byte cap below is
# what bounds the actual work, matching `text.py::MAX_TEXT_PAGES`'s
# reasoning for PDFs.
MAX_CSV_ROWS = 500


class CsvTextExtractor:
    """Reads a CSV/TSV's header and a bounded sample of rows.

    The delimiter is sniffed with `csv.Sniffer`, falling back to comma if
    sniffing fails (a one-column file, an unusual delimiter, or too short a
    sample to tell). Encoding is tolerant the same way
    `PlainTextExtractor` is: `errors="replace"` rather than a raise, with the
    same replacement-character ratio heuristic to recognise a binary file
    mistaken for text.
    """

    name = "csv-text"
    version = "1"

    def supports(self, source: EvidenceSource) -> bool:
        return source.path.suffix.lower() in _CSV_SUFFIXES

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
        # Same bound as `PlainTextExtractor`: at most 4 bytes per requested
        # char (UTF-8's worst case), read from the head only.
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

        if not raw:
            native = NativeText(text="", sections=(), extractor=self.name, extractor_version=self.version, char_count=0, truncated=False)
            return native, None, (), "ok"

        if b"\x00" in raw:
            return (
                None,
                None,
                (EvidenceWarning(code=WARN_MALFORMED, stage=_STAGE, detail="NUL byte in head; treated as binary, not csv"),),
                "failed",
            )

        decoded = raw.decode("utf-8-sig", errors="replace")
        if len(decoded) >= _MALFORMED_MIN_SAMPLE:
            ratio = decoded.count(_REPLACEMENT_CHAR) / len(decoded)
            if ratio > _MALFORMED_REPLACEMENT_RATIO:
                return (
                    None,
                    None,
                    (EvidenceWarning(code=WARN_MALFORMED, stage=_STAGE, detail=f"head is {ratio:.0%} replacement characters; not valid utf-8 text"),),
                    "failed",
                )

        try:
            dialect = csv.Sniffer().sniff(decoded[:_CSV_SNIFF_SAMPLE_CHARS], delimiters=_CSV_SNIFF_DELIMITERS)
            delimiter = dialect.delimiter
        except csv.Error:
            delimiter = "\t" if source.path.suffix.lower() == ".tsv" else ","

        rows: list[str] = []
        row_count_seen = 0
        row_truncated = False
        try:
            reader = csv.reader(io.StringIO(decoded), delimiter=delimiter)
            for row in reader:
                row_count_seen += 1
                if len(rows) >= MAX_CSV_ROWS:
                    row_truncated = True
                    break
                rows.append(delimiter.join(row))
        except csv.Error as exc:
            return (
                None,
                None,
                (EvidenceWarning(code=WARN_MALFORMED, stage=_STAGE, detail=f"csv parse failed: {exc}"),),
                "failed",
            )

        body = "\n".join(rows)
        sanitized, char_truncated = sanitize_extracted_text(body, max_chars=max_chars)

        warnings: list[EvidenceWarning] = []
        if row_truncated:
            warnings.append(EvidenceWarning(code=WARN_PARTIAL, stage=_STAGE, detail=f"more than {MAX_CSV_ROWS} rows; only the first {MAX_CSV_ROWS} were read"))
        if char_truncated:
            warnings.append(EvidenceWarning(code=WARN_TRUNCATED, stage=_STAGE, detail=f"capped at {max_chars} chars"))

        native = NativeText(
            text=sanitized,
            sections=(TextSection(text=sanitized, label="table"),) if sanitized else (),
            extractor=self.name,
            extractor_version=self.version,
            char_count=len(sanitized),
            truncated=row_truncated or char_truncated,
        )
        return native, None, tuple(warnings), "ok"
