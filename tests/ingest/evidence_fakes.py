"""Deterministic, offline stand-ins for the evidence layer's backend seams.

Every fake here exists to make one specific claim checkable rather than
merely plausible: that OCR is never invoked when native text already answers
the question (a call counter), that a backend lying about its own
`available()` costs nothing (`available_raises`), that a backend handing back
attacker-authored or simply broken data cannot climb any higher than a
`FileEvidence` field (`HostileOCRBackend`, `GarbageOCRBackend`). None of these
talk to a network, a subprocess, or the filesystem beyond the handful of
fixture builders (`make_text_pdf`, `make_scanned_pdf`, `make_docx`,
`make_tilted_rect_image`) that create the input files a test starts from --
and even those only ever write brand-new files under a caller-supplied path,
never touch anything that already exists.

`make_text_pdf` is reused verbatim from a hand-verified, dependency-free PDF
writer: no `pypdf`/`reportlab` round trip is needed to prove a real embedded
text layer exists, because this module builds the PDF's own object/xref
structure directly.
"""

from __future__ import annotations

import hashlib
import math
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Sequence
from xml.sax.saxutils import escape as _xml_escape

from tools.ingest.evidence.backends import ExtractionResult, OCRResult, VisionResult
from tools.ingest.evidence.types import OCRBlock, Polygon

# -- fake backends ------------------------------------------------------------


class FakeOCRBackend:
    """An OCR engine a test fully controls, including how it lies.

    Every `recognize()` call is recorded (by the raster page it was handed)
    so a test can assert what was *not* run -- the only way "an embedded-text
    PDF never reaches OCR" is provable rather than merely asserted. `available`
    calls are counted separately: several tests need to know whether an
    "unavailable" backend costs a full recognition attempt (it must not) and
    whether an unavailable result gets cached (it must not, so a backend that
    later becomes installed is re-asked rather than reading back a stale
    absence -- see `extract.py`'s comment on `_vision_stage`/`_ocr_stage`).
    """

    def __init__(
        self,
        *,
        name: str = "fake-ocr",
        version: str = "1",
        blocks_by_page: dict[int, Sequence[OCRBlock]] | None = None,
        available: bool = True,
        available_raises: bool = False,
        raises: BaseException | None = None,
        status: str = "ok",
        warnings: tuple = (),
    ) -> None:
        self.name = name
        self.version = version
        self._blocks_by_page = dict(blocks_by_page or {})
        self._available = available
        self._available_raises = available_raises
        self._raises = raises
        self._status = status
        self._warnings = warnings
        self.calls: list[int] = []  # one entry per `recognize()` call, the page number
        self.available_calls = 0

    def available(self) -> bool:
        self.available_calls += 1
        if self._available_raises:
            raise RuntimeError(f"{self.name}.available() exploded")
        return self._available

    def recognize(self, image: Any, *, timeout_seconds: int) -> OCRResult:
        self.calls.append(image.page)
        if self._raises is not None:
            raise self._raises
        blocks = tuple(self._blocks_by_page.get(image.page, ()))
        return OCRResult(blocks=blocks, warnings=tuple(self._warnings), status=self._status)


class FakeVisionBackend:
    """A vision backend a test fully controls. See `FakeOCRBackend`."""

    def __init__(
        self,
        *,
        name: str = "fake-vision",
        version: str = "1",
        model: str = "fake-model",
        model_version: str = "1",
        visual: Any = None,
        available: bool = True,
        available_raises: bool = False,
        raises: BaseException | None = None,
        status: str = "ok",
        warnings: tuple = (),
    ) -> None:
        self.name = name
        self.version = version
        self.model = model
        self.model_version = model_version
        self._visual = visual
        self._available = available
        self._available_raises = available_raises
        self._raises = raises
        self._status = status
        self._warnings = warnings
        self.calls = 0
        self.available_calls = 0

    def available(self) -> bool:
        self.available_calls += 1
        if self._available_raises:
            raise RuntimeError(f"{self.name}.available() exploded")
        return self._available

    def describe(self, image: Any, *, timeout_seconds: int) -> VisionResult:
        self.calls += 1
        if self._raises is not None:
            raise self._raises
        return VisionResult(visual=self._visual, warnings=tuple(self._warnings), status=self._status)


class UnavailableOCRBackend(FakeOCRBackend):
    """An OCR engine that is honestly not installed. `available()` -> False,
    never raises, mirrors `NullOCRBackend`'s contract with a test-controlled
    name/version so cache-key tests can tell it apart from the real null."""

    def __init__(self, *, name: str = "unavailable-ocr", version: str = "1") -> None:
        super().__init__(name=name, version=version, available=False)


class UnavailableVisionBackend(FakeVisionBackend):
    """See `UnavailableOCRBackend`."""

    def __init__(self, *, name: str = "unavailable-vision", version: str = "1") -> None:
        super().__init__(name=name, version=version, available=False)


class ExplodingOCRBackend(FakeOCRBackend):
    """Raises out of `recognize()` on every call: the worst case for a
    backend that claimed to be available and then failed anyway."""

    def __init__(self, *, name: str = "exploding-ocr", version: str = "1", exc: BaseException | None = None) -> None:
        super().__init__(name=name, version=version, raises=exc or RuntimeError("ocr backend exploded"))


class ExplodingVisionBackend(FakeVisionBackend):
    """See `ExplodingOCRBackend`."""

    def __init__(self, *, name: str = "exploding-vision", version: str = "1", exc: BaseException | None = None) -> None:
        super().__init__(name=name, version=version, raises=exc or RuntimeError("vision backend exploded"))


class GarbageOCRBackend:
    """Returns structurally garbage blocks: wrong types, NaN confidence,
    degenerate/negative coordinates, and far more blocks than any real page
    could contain. Every value here is something a broken or hostile OCR
    engine could hand back; `extract.py`'s normalization step is what has to
    survive it, not this fixture. `page_value` defaults to a real page number
    (0, i.e. "fall through to the raster page") so a test can isolate the
    *other* garbage dimensions from the one field that is not yet defended --
    see `test_evidence_extract.py`'s `expectedFailure` case.
    """

    name = "garbage-ocr"
    version = "1"

    def __init__(
        self,
        *,
        block_count: int = 10_000,
        page_value: Any = 0,
        text_value: Any = 12345,
    ) -> None:
        self._block_count = block_count
        self._page_value = page_value
        self._text_value = text_value
        self.calls: list[int] = []

    def available(self) -> bool:
        return True

    def recognize(self, image: Any, *, timeout_seconds: int) -> OCRResult:
        self.calls.append(image.page)
        garbage = SimpleNamespace(
            text=self._text_value,  # not a string
            polygon=Polygon(points=((-5, -5), (-5, -5))),  # <3 points, negative: invalid
            page=self._page_value,
            confidence=float("nan"),
            engine=None,
            engine_version=None,
        )
        blocks = tuple(garbage for _ in range(self._block_count))
        return OCRResult(blocks=blocks, status="ok")


class HostileOCRBackend:
    """Emits attacker-authored strings, one block per payload, all on page 1.

    Used to prove that prompt-injection text surviving OCR is captured as
    inert data -- present, sanitized, and powerless -- rather than obeyed or
    silently dropped. See `test_evidence_security.py`.
    """

    name = "hostile-ocr"
    version = "1"

    def __init__(self, payloads: Sequence[str]) -> None:
        self._payloads = list(payloads)
        self.calls: list[int] = []

    def available(self) -> bool:
        return True

    def recognize(self, image: Any, *, timeout_seconds: int) -> OCRResult:
        self.calls.append(image.page)
        blocks = tuple(
            OCRBlock(
                text=text,
                polygon=Polygon.from_bbox(0, index * 20, 200, index * 20 + 18),
                page=image.page,
                confidence=0.9,
                reading_order=0,
                engine=self.name,
                engine_version=self.version,
            )
            for index, text in enumerate(self._payloads)
        )
        return OCRResult(blocks=blocks, status="ok")


# -- fixture builders -----------------------------------------------------


def make_text_pdf(path: Path, lines, pages: int = 1) -> Path:
    """A minimal, valid PDF with a real embedded text layer. No dependencies.

    Writes the PDF's object/xref/trailer structure directly, so this exists
    independently of `pypdf`, `reportlab`, or any other PDF library -- the
    same reasoning `fi_helpers.py::make_png` applies to Pillow: the fixture
    must not depend on the very thing (here, a *different* library from the
    one under test) it is proving behaviour about.
    """
    objects = []

    def add(body):
        objects.append(body)
        return len(objects)

    font = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    page_ids, content_ids = [], []
    for _ in range(pages):
        stream = b"BT /F1 14 Tf 72 720 Td 16 TL\n"
        for line in lines:
            esc = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            stream += b"(" + esc.encode("latin-1", "replace") + b") Tj T*\n"
        stream += b"ET\n"
        content_ids.append(add(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"endstream"))
        page_ids.append(None)
    pages_id = len(objects) + pages + 1
    for index, content_id in enumerate(content_ids):
        page_ids[index] = add(
            b"<< /Type /Page /Parent %d 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 %d 0 R >> >> /Contents %d 0 R >>"
            % (pages_id, font, content_id)
        )
    kids = b" ".join(b"%d 0 R" % pid for pid in page_ids)
    real_pages_id = add(b"<< /Type /Pages /Kids [" + kids + b"] /Count %d >>" % pages)
    catalog = add(b"<< /Type /Catalog /Pages %d 0 R >>" % real_pages_id)

    out = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for offset in offsets[1:]:
        out += b"%010d 00000 n \n" % offset
    out += b"trailer\n<< /Size %d /Root %d 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, catalog, xref)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bytes(out))
    return path


def make_scanned_pdf(path: Path, pages: int = 1) -> Path:
    """An image-only PDF with no embedded text layer.

    What a flatbed scan looks like to a text extractor: `pypdf` finds nothing
    to extract, so the router must fall through to r4 (native attempt + OCR)
    rather than r3. Each page is a blank white canvas -- the pixel content
    does not matter to this fixture, only that no text object exists.
    """
    from PIL import Image

    path.parent.mkdir(parents=True, exist_ok=True)
    images = [Image.new("RGB", (1200, 1600), (255, 255, 255)) for _ in range(max(pages, 1))]
    first, rest = images[0], images[1:]
    if rest:
        first.save(path, "PDF", save_all=True, append_images=rest)
    else:
        first.save(path, "PDF")
    return path


def make_docx(path: Path, paragraphs: Sequence[str], headings: Sequence[tuple[str, int]] = ()) -> Path:
    """A minimal, valid `.docx` (Office Open XML), built directly as a zip of
    hand-written XML parts -- no `python-docx` dependency, because the
    extractor under test (`DocxTextExtractor`) reads exactly these parts and
    nothing else, and this fixture should not depend on a library capable of
    producing more than that.

    `headings` is `(text, level)` pairs, emitted as their own paragraphs
    styled `HeadingN`, ahead of the plain `paragraphs`.
    """
    import zipfile

    body_parts = []
    for text, level in headings:
        body_parts.append(
            f'<w:p><w:pPr><w:pStyle w:val="Heading{level}"/></w:pPr>'
            f"<w:r><w:t>{_xml_escape(text)}</w:t></w:r></w:p>"
        )
    for text in paragraphs:
        body_parts.append(f"<w:p><w:r><w:t>{_xml_escape(text)}</w:t></w:r></w:p>")

    document_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:body>" + "".join(body_parts) + "</w:body></w:document>"
    ).encode("utf-8")
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        "</Types>"
    ).encode("utf-8")
    root_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
        'Target="word/document.xml"/></Relationships>'
    ).encode("utf-8")

    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", root_rels)
        archive.writestr("word/document.xml", document_xml)
    return path


def make_tilted_rect_image(
    path: Path,
    angle: float,
    *,
    size: tuple[int, int] = (400, 400),
    rect_size: tuple[int, int] = (160, 60),
    color: tuple[int, int, int] = (200, 30, 30),
    background: tuple[int, int, int] = (255, 255, 255),
) -> tuple[Path, tuple[tuple[int, int], ...]]:
    """A synthetic image containing one filled rectangle, rotated `angle`
    degrees about its own centre, plus the exact quad it was drawn at.

    The corners are computed analytically and handed back so a test can build
    the matching `Polygon` and drive `perspective.rotate_crop` against a quad
    whose true content (a solid, known colour) is verifiable, rather than
    trusting a separately-computed rotation to agree with what was drawn.
    Corner order is `[TL, TR, BR, BL]` of the *unrotated* rectangle, rotated
    together -- exactly what `rotate_crop`/`crop_width_height` expect.
    """
    from PIL import Image, ImageDraw

    width, height = rect_size
    half_w, half_h = width / 2.0, height / 2.0
    local = [(-half_w, -half_h), (half_w, -half_h), (half_w, half_h), (-half_w, half_h)]
    cx, cy = size[0] / 2.0, size[1] / 2.0
    theta = math.radians(angle)
    cos_t, sin_t = math.cos(theta), math.sin(theta)
    corners = tuple(
        (int(round(x * cos_t - y * sin_t + cx)), int(round(x * sin_t + y * cos_t + cy))) for x, y in local
    )

    image = Image.new("RGB", size, background)
    draw = ImageDraw.Draw(image)
    draw.polygon(list(corners), fill=color)
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path)
    return path, corners


# -- misc test helpers --------------------------------------------------------


def content_hash_multiset(root: Path) -> Counter:
    """The multiset of every file's SHA-256 under `root`, ignoring names.

    A prompt-injection test has to prove a hostile run changed nothing about
    the *bytes* on disk even though the naming layer may legitimately rename a
    file. Comparing file names before/after cannot express that; comparing
    the multiset of content hashes can, because a rename changes which name a
    hash is filed under but not how many times each hash appears.
    """
    counts: Counter = Counter()
    for candidate in root.rglob("*"):
        if candidate.is_file():
            counts[hashlib.sha256(candidate.read_bytes()).hexdigest()] += 1
    return counts


__all__ = [
    "FakeOCRBackend",
    "FakeVisionBackend",
    "UnavailableOCRBackend",
    "UnavailableVisionBackend",
    "ExplodingOCRBackend",
    "ExplodingVisionBackend",
    "GarbageOCRBackend",
    "HostileOCRBackend",
    "make_text_pdf",
    "make_scanned_pdf",
    "make_docx",
    "make_tilted_rect_image",
    "content_hash_multiset",
    "ExtractionResult",
]
