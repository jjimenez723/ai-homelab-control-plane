"""Turning a file into in-memory PNG bytes for a model-facing backend.

`OCRBackend` and `VisionBackend` receive pixels, never a path (spec
invariant 4) -- there is nothing in a `RasterImage` for a model-facing
backend to reach the filesystem with. This module is the only place PDF
pages get rasterized and the only place an arbitrary image gets normalized
into that shape; both entry points write nothing to disk and never mutate
the file they read.

This is an Angel-native reimplementation, not a port. PaddleOCR's
`check_and_read` inspects a very different problem (reading whichever of a
long list of scientific/medical formats OpenCV can decode) and is not used or
copied here. The only idea worth keeping from that neighbourhood is
"rasterize a PDF page at a bounded resolution instead of the naive `dpi ->
pixels` conversion, so a physically large page cannot demand an unbounded
allocation" -- which is implemented below directly against `pypdfium2`.

`pypdfium2` is an optional dependency, detected the same way
`media/probe.py::pypdf_available` detects pypdf: `importlib.util.find_spec`,
cached with `@lru_cache(maxsize=1)` because the answer cannot change within a
run. Its absence degrades to an empty result and a
`backend_unavailable` warning, never an exception -- a machine without it
still catalogues everything else about a PDF; it simply cannot be OCRed or
shown to a vision backend from its rasterized pages.
"""

from __future__ import annotations

from functools import lru_cache
from importlib.util import find_spec

from tools.ingest.evidence.preprocess import (
    alpha_to_color,
    apply_exif_orientation,
    downscale_to_fit,
    open_image_safely,
    to_png_bytes,
)
from tools.ingest.evidence.types import (
    EvidenceSource,
    EvidenceWarning,
    RasterImage,
    WARN_BACKEND_UNAVAILABLE,
    WARN_MALFORMED,
    WARN_PARTIAL,
    WARN_TRUNCATED,
)

_STAGE = "raster"


@lru_cache(maxsize=1)
def pdfium_available() -> bool:
    try:
        return find_spec("pypdfium2") is not None
    except (ImportError, ValueError):  # pragma: no cover - broken install
        return False


def rasterize_pdf_pages(
    source: EvidenceSource, *, dpi: int, max_pages: int, max_pixels: int
) -> tuple[list[RasterImage], tuple[EvidenceWarning, ...]]:
    """Render up to `max_pages` pages of a PDF to in-memory PNG bytes.

    `dpi` is a request, not a promise: a page whose rendered size at `dpi`
    would exceed `max_pixels` (a 60x40in architectural plan at 200dpi is
    ~288 megapixels) has its scale reduced to fit the budget instead, and
    that page's warning names it explicitly rather than silently degrading
    quality. A single page that fails to open or render is skipped with a
    warning; it does not abort the rest of the document. Nothing is ever
    written to disk -- `PdfBitmap.to_pil()` decodes straight into memory.
    """
    if not pdfium_available():
        return [], (EvidenceWarning(code=WARN_BACKEND_UNAVAILABLE, stage=_STAGE, detail="pypdfium2 not installed"),)

    import pypdfium2 as pdfium  # imported lazily: optional dependency

    warnings: list[EvidenceWarning] = []

    try:
        document = pdfium.PdfDocument(str(source.path))
    except Exception as exc:  # noqa: BLE001 - corrupt/truncated pdfs raise widely
        return (
            [],
            (EvidenceWarning(code=WARN_MALFORMED, stage=_STAGE, detail=f"pdf could not be opened: {type(exc).__name__}: {exc}"),),
        )

    images: list[RasterImage] = []
    try:
        try:
            page_count = len(document)
        except Exception as exc:  # noqa: BLE001
            return (
                [],
                (EvidenceWarning(code=WARN_MALFORMED, stage=_STAGE, detail=f"pdf page count unreadable: {type(exc).__name__}: {exc}"),),
            )

        read_count = min(page_count, max_pages)
        if page_count > max_pages:
            warnings.append(
                EvidenceWarning(
                    code=WARN_TRUNCATED,
                    stage=_STAGE,
                    detail=f"pdf has {page_count} pages; rasterizing only the first {max_pages}",
                )
            )

        for index in range(read_count):
            try:
                page = document.get_page(index)
            except Exception as exc:  # noqa: BLE001 - one bad page does not abort the pdf
                warnings.append(
                    EvidenceWarning(code=WARN_PARTIAL, stage=_STAGE, detail=f"page {index + 1} could not be opened: {type(exc).__name__}: {exc}")
                )
                continue

            try:
                width_pt, height_pt = page.get_size()
                scale = dpi / 72.0
                projected_pixels = (width_pt * scale) * (height_pt * scale)
                if projected_pixels > max_pixels > 0:
                    # The renderer rounds width and height to whole pixels
                    # independently, so the exact ratio can land a handful of
                    # pixels over budget after rounding (two dimensions each
                    # rounded up is a larger product than the unrounded
                    # target). The small safety factor absorbs that without
                    # visibly affecting output quality.
                    scale = scale * ((max_pixels / projected_pixels) ** 0.5) * 0.995
                    warnings.append(
                        EvidenceWarning(
                            code=WARN_PARTIAL,
                            stage=_STAGE,
                            detail=f"page {index + 1} rendered below the requested {dpi} dpi to fit the {max_pixels}-pixel budget",
                        )
                    )

                bitmap = page.render(scale=scale)
                try:
                    pil_image = bitmap.to_pil()
                finally:
                    bitmap.close()
            except Exception as exc:  # noqa: BLE001 - one bad page does not abort the pdf
                warnings.append(
                    EvidenceWarning(code=WARN_PARTIAL, stage=_STAGE, detail=f"page {index + 1} could not be rendered: {type(exc).__name__}: {exc}")
                )
                continue
            finally:
                try:
                    page.close()
                except Exception:  # noqa: BLE001 - best-effort cleanup
                    pass

            images.append(
                RasterImage(
                    png_bytes=to_png_bytes(pil_image),
                    width=pil_image.width,
                    height=pil_image.height,
                    page=index + 1,
                    source_sha256=source.sha256,
                )
            )
    finally:
        try:
            document.close()
        except Exception:  # noqa: BLE001 - best-effort cleanup
            pass

    return images, tuple(warnings)


def rasterize_image(
    source: EvidenceSource, *, max_pixels: int, max_edge: int | None = None
) -> tuple[RasterImage | None, tuple[EvidenceWarning, ...]]:
    """Normalize an arbitrary image file into PNG bytes for a model backend.

    Applies, in order: the decompression-bomb guard (refuses to decode a
    canvas over `max_pixels`), EXIF-orientation correction, alpha
    flattening, and an optional further downscale to `max_edge`. A file that
    cannot be safely decoded at all -- corrupt, truncated, or an oversized
    declared canvas -- returns `(None, warnings)` rather than raising.
    """
    image = open_image_safely(source.path, max_pixels=max_pixels)
    if image is None:
        return (
            None,
            (EvidenceWarning(code=WARN_MALFORMED, stage=_STAGE, detail="image could not be safely decoded (unreadable, corrupt, or exceeds the pixel budget)"),),
        )

    try:
        image = apply_exif_orientation(image)
        image = alpha_to_color(image)
        image, resized = downscale_to_fit(image, max_pixels=max_pixels, max_edge=max_edge)
        png_bytes = to_png_bytes(image)
    except Exception as exc:  # noqa: BLE001 - never raises past this boundary
        return (
            None,
            (EvidenceWarning(code=WARN_MALFORMED, stage=_STAGE, detail=f"image could not be encoded: {type(exc).__name__}: {exc}"),),
        )

    warnings: tuple[EvidenceWarning, ...] = ()
    if resized:
        warnings = (EvidenceWarning(code=WARN_PARTIAL, stage=_STAGE, detail="image downscaled to fit the configured pixel/edge budget"),)

    raster = RasterImage(png_bytes=png_bytes, width=image.width, height=image.height, page=1, source_sha256=source.sha256)
    return raster, warnings
