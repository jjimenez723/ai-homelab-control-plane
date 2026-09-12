"""In-memory image preprocessing for the evidence layer.

Every function here takes a Pillow `Image` (or, for `open_image_safely`, a
path or raw bytes) and returns a Pillow `Image` or PNG `bytes`. Nothing in
this module accepts a destination path, nothing calls `.save()` against a
path on disk, and nothing leaves a temp file behind -- pixels come in, pixels
or bytes go out. That is not a style preference here: the evidence layer's
central safety property is zero filesystem writes (spec invariant 1), and
this module is one of the two places (with `raster.py`) that touch decoded
image data at all.

Two of these functions -- `alpha_to_color` and `binarize` -- exist in
PaddleOCR under the names `alpha_to_color` and `binarize_img`. Only the
*idea* is borrowed (flatten transparency before OCR; binarize with an
adaptive threshold before OCR): each is a handful of lines built directly
against Pillow's own API, using a different algorithm (Otsu's method,
computed from Pillow's own 256-bin histogram, in place of whatever PaddleOCR
uses), with no line of upstream code copied. This is too small and too
mechanical a transformation to be a derivative work of anything, so it
carries no PaddleOCR attribution -- unlike `reading_order.py` and
`perspective.py`, which do adapt named upstream functions and are marked
accordingly.

`binarize`'s Otsu implementation is pure Python over a 256-entry histogram
(no numpy: the evidence package avoids it everywhere, see spec §13).

Perspective cropping is deliberately not here -- that is `perspective.py`,
owned separately, and it is the one module in this package allowed to carry
PaddleOCR-derived geometry code.
"""

from __future__ import annotations

import io

from PIL import Image, ImageOps

try:
    import pillow_heif

    # Registered here too, independently of media/probe.py and media/heic.py,
    # so this module can decode a HEIC handed to it even if nothing else in
    # the process has imported those modules yet. Registration is idempotent.
    pillow_heif.register_heif_opener()
except ImportError:  # pragma: no cover - pillow-heif ships in requirements-ingest.txt
    pass


def apply_exif_orientation(image: Image.Image) -> Image.Image:
    """Return `image` rotated/flipped to match its EXIF Orientation tag.

    Delegates to Pillow's own `ImageOps.exif_transpose`, which both applies
    the transform and normalizes the tag away, so nothing downstream sees a
    sideways photograph and nothing re-applies the rotation a second time.
    Malformed EXIF is common enough in the wild (see `media/probe.py`'s own
    defensive EXIF reads) that a parse failure here returns the image
    unrotated rather than raising.
    """
    try:
        transposed = ImageOps.exif_transpose(image)
    except Exception:  # noqa: BLE001 - malformed EXIF varies too widely to enumerate
        return image
    return transposed if transposed is not None else image


def alpha_to_color(image: Image.Image, *, background: tuple[int, int, int] = (255, 255, 255)) -> Image.Image:
    """Flatten transparency onto a solid background, returning an RGB image.

    A model-facing backend receives PNG bytes with no guarantee anything
    downstream understands alpha the way the pipeline intends -- and OCR in
    particular tends to see straight through a transparent background to
    whatever noise is beneath it. Compositing onto white (by default) removes
    that ambiguity. Images that are already opaque are only converted to RGB
    if they are not already in it (a paletted or grayscale image, say);
    nothing is recomposited unnecessarily.
    """
    has_alpha = image.mode in ("RGBA", "LA") or (image.mode == "P" and "transparency" in image.info)
    if has_alpha:
        rgba = image.convert("RGBA")
        canvas = Image.new("RGB", rgba.size, background)
        canvas.paste(rgba, mask=rgba.split()[-1])
        return canvas
    if image.mode != "RGB":
        return image.convert("RGB")
    return image


def _otsu_threshold(histogram: list[int]) -> int:
    """Otsu's method over a 256-bin grayscale histogram, in pure Python.

    Finds the threshold that maximizes between-class variance -- the
    standard formulation, computed as a single pass with running sums rather
    than recomputing class means from scratch at every candidate threshold.
    An all-empty or single-valued histogram falls back to a neutral midpoint
    threshold rather than raising or picking an arbitrary bin.
    """
    total = sum(histogram)
    if total == 0:
        return 128

    weighted_total = sum(level * count for level, count in enumerate(histogram))
    weight_background = 0
    sum_background = 0.0
    best_threshold = 0
    best_variance = -1.0

    for level, count in enumerate(histogram):
        weight_background += count
        if weight_background == 0:
            continue
        weight_foreground = total - weight_background
        if weight_foreground == 0:
            break
        sum_background += level * count
        mean_background = sum_background / weight_background
        mean_foreground = (weighted_total - sum_background) / weight_foreground
        variance_between = weight_background * weight_foreground * (mean_background - mean_foreground) ** 2
        if variance_between > best_variance:
            best_variance = variance_between
            best_threshold = level

    return best_threshold


def binarize(image: Image.Image, *, threshold: int | None = None) -> Image.Image:
    """Return a black/white ("L" mode, values 0 or 255) version of `image`.

    `threshold` is honoured verbatim when given; otherwise it is computed
    per-image with Otsu's method, which tends to cope far better than a fixed
    threshold with scans that vary in exposure or paper colour.
    """
    gray = image.convert("L")
    cutoff = threshold if threshold is not None else _otsu_threshold(gray.histogram())
    lookup_table = [255 if level > cutoff else 0 for level in range(256)]
    return gray.point(lookup_table)


def downscale_to_fit(
    image: Image.Image, *, max_pixels: int, max_edge: int | None = None
) -> tuple[Image.Image, bool]:
    """Shrink `image` to fit a pixel budget and/or a longest-edge bound.

    Only ever shrinks -- a small input is returned unchanged, never upscaled.
    Returns `(image, changed)` so a caller can tell whether anything actually
    happened without comparing sizes itself.
    """
    width, height = image.size
    scale = 1.0
    if max_pixels > 0 and width * height > max_pixels:
        scale = min(scale, (max_pixels / (width * height)) ** 0.5)
    if max_edge is not None:
        longest = max(width, height)
        if longest > max_edge:
            scale = min(scale, max_edge / longest)

    if scale >= 1.0:
        return image, False

    new_size = (max(1, round(width * scale)), max(1, round(height * scale)))
    return image.resize(new_size, Image.LANCZOS), True


def open_image_safely(path_or_bytes, *, max_pixels: int) -> Image.Image | None:
    """Open an image and return a fully-decoded, detached copy, or None.

    Mirrors `media/probe.py::thumbnail_png`'s decompression-bomb guard
    exactly: the declared canvas size is checked *before* `.load()` is ever
    called, because decoding the pixels is the expensive step a hostile file
    can weaponise. `.copy()` is taken while the source is still open so the
    returned image survives past this function -- Pillow's documentation
    does not guarantee an `Image` remains usable after its backing file is
    closed, even once `.load()` has run.

    Never raises: an unreadable path, an unidentifiable format, a truncated
    file, or a canvas over `max_pixels` all return None.
    """
    try:
        source = io.BytesIO(path_or_bytes) if isinstance(path_or_bytes, (bytes, bytearray)) else path_or_bytes
        with Image.open(source) as opened:
            width, height = opened.size
            if width * height > max_pixels:
                return None
            opened.load()
            return opened.copy()
    except Exception:  # noqa: BLE001 - callers get None, never an exception
        return None


def to_png_bytes(image: Image.Image) -> bytes:
    """Encode `image` as PNG bytes. Never touches the filesystem."""
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()
