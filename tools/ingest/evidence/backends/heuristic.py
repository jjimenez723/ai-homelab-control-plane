"""The honest offline stand-in for a real vision model.

`HeuristicVisionBackend` is written in exactly the spirit of
`naming/proposer.py`'s `DeterministicProposer`: it says only what it can
actually measure, at honestly low confidence, and says `unknown` rather than
inventing a scene a person would have to un-learn later. Where
`DeterministicProposer` measures a file's own metadata (EXIF, a PDF's title),
this backend measures pixels -- dimensions, colour distribution, flatness,
greyscale-ness -- and nothing else.

That "nothing else" is load-bearing, not incidental. `VisionBackend`
(`tools/ingest/evidence/backends/__init__.py`) exists specifically so a
future real model receives pixels and nothing that could let it (or this
stand-in) cheat by reading the filename, EXIF, or the deterministic `facts`
dict a probe already produced. This backend never receives any of those --
`describe()` takes only a `RasterImage`, which carries PNG bytes and nothing
else -- so there is nothing here to accidentally consult.

It never asserts `IMAGE_KIND_RENDERING`, `IMAGE_KIND_DIAGRAM`,
`IMAGE_KIND_CHART`, or `IMAGE_KIND_MEME`. None of those are things pixel
statistics can actually tell apart from an ordinary photo or a UI capture --
a rendered 3D scene and a photo can have identical colour-distribution
statistics -- and asserting one anyway would be exactly the failure this
whole design exists to avoid: a confident, wrong, machine-authored fact that
looks exactly as trustworthy as a true one. Where the evidence does not
clearly support one of the few kinds below, it answers `IMAGE_KIND_UNKNOWN`.
"""

from __future__ import annotations

import io
from collections import Counter

from PIL import Image

from tools.ingest.evidence.backends import VisionResult
from tools.ingest.evidence.types import (
    IMAGE_KIND_ORDINARY_PHOTO,
    IMAGE_KIND_SCANNED_DOCUMENT,
    IMAGE_KIND_UI_CAPTURE,
    IMAGE_KIND_UNKNOWN,
    EvidenceWarning,
    RasterImage,
    VisualEvidence,
    WARN_MALFORMED,
    sanitize_extracted_text,
)

CONFIDENCE_UI_CAPTURE = 0.6
CONFIDENCE_SCANNED_DOCUMENT = 0.55
CONFIDENCE_ORDINARY_PHOTO = 0.55
CONFIDENCE_UNKNOWN = 0.3

# Same reasoning and the same order of magnitude as `media/probe.py`'s
# `MAX_DECODE_PIXELS`: a small file can declare an enormous canvas, and
# decoding it would be the attack. This backend runs over rasterized pages and
# crops that are already bounded by `config.raster_max_pixels`, so this guard
# should never actually fire in the normal pipeline; it exists for whatever
# calls this backend directly with untrusted bytes.
_MAX_DECODE_PIXELS = 80_000_000

# The heuristics below are statistics of the whole image, not per-pixel
# detail, so measuring a small downscaled sample gives the same answer as the
# full image at a fraction of the cost -- important because this runs over a
# whole volume of images.
_SAMPLE_MAX_EDGE = 256

# A screenshot/UI capture is flat-fill and low-palette: a handful of colours
# covering most of the image, with long runs of bit-identical neighbouring
# pixels (axis-aligned fills, text on a solid background). A photo essentially
# never looks like this.
_UI_MAX_DISTINCT_COLOURS = 64
_UI_MIN_DOMINANT_FRACTION = 0.25
_UI_MIN_FLAT_RUN_FRACTION = 0.30

# A scanned document is near-greyscale and overwhelmingly light (paper), with
# only a small dark fraction (ink/lines).
_GREYSCALE_MAX_CHANNEL_SPREAD = 8.0
_SCAN_MIN_LIGHT_FRACTION = 0.80
_SCAN_MAX_DARK_FRACTION = 0.20

# An ordinary photo has a broad, continuous colour palette: real-world lens
# and sensor noise means adjacent pixels are rarely bit-identical, and the
# number of distinct sampled colours is large.
_PHOTO_MIN_DISTINCT_COLOURS = 2000


class HeuristicVisionBackend:
    name = "heuristic-vision"
    version = "1"
    model = "deterministic-heuristics"
    model_version = "1"

    def available(self) -> bool:
        # Needs only Pillow, which the rest of this package already requires
        # unconditionally (see `perspective.py`).
        return True

    def describe(self, image: RasterImage, *, timeout_seconds: int) -> VisionResult:
        try:
            with Image.open(io.BytesIO(image.png_bytes)) as opened:
                width, height = opened.size
                if width * height > _MAX_DECODE_PIXELS:
                    return VisionResult(
                        visual=None,
                        warnings=(
                            EvidenceWarning(
                                code=WARN_MALFORMED,
                                stage="vision",
                                detail=f"image {width}x{height} exceeds the decode-size guard; skipped",
                            ),
                        ),
                        status="failed",
                    )
                opened.load()
                decoded = opened.convert("RGB")
        except Exception as exc:  # noqa: BLE001 - untrusted bytes, never raise
            return VisionResult(
                visual=None,
                warnings=(
                    EvidenceWarning(
                        code=WARN_MALFORMED,
                        stage="vision",
                        detail=f"heuristic vision backend could not decode image: {type(exc).__name__}: {exc}",
                    ),
                ),
                status="failed",
            )

        try:
            width, height = decoded.size
            measurements = _measure(decoded)
            kind, confidence = _classify(measurements)
            description = _describe(width, height, measurements)
            # Only the screenshot-ish kind is allowed a context beyond "":
            # "screen capture" is as specific as pixel statistics can honestly
            # support -- nothing about *which* app or page.
            context = "screen capture" if kind == IMAGE_KIND_UI_CAPTURE else ""

            description, _ = sanitize_extracted_text(description, max_chars=500)
            context, _ = sanitize_extracted_text(context, max_chars=100)

            return VisionResult(
                visual=VisualEvidence(
                    image_kind=kind,
                    description=description,
                    entities=(),  # a heuristic cannot recognise objects
                    context=context,
                    confidence=confidence,
                    backend=self.name,
                    model=self.model,
                    model_version=self.model_version,
                ),
                warnings=(),
                status="ok",
            )
        except Exception as exc:  # noqa: BLE001 - never raise out of a backend
            return VisionResult(
                visual=None,
                warnings=(
                    EvidenceWarning(
                        code=WARN_MALFORMED,
                        stage="vision",
                        detail=f"heuristic vision backend failed unexpectedly: {type(exc).__name__}: {exc}",
                    ),
                ),
                status="failed",
            )


def _measure(decoded: "Image.Image") -> dict:
    """Cheap pixel statistics: the only inputs anything below may use."""
    sample = decoded.copy()
    sample.thumbnail((_SAMPLE_MAX_EDGE, _SAMPLE_MAX_EDGE), Image.BILINEAR)
    sample_width, sample_height = sample.size
    # `Image.getdata()` is deprecated in Pillow 12 in favour of
    # `get_flattened_data()`, which does not exist in older Pillow. Reaching
    # for the new name when it is there keeps the warning out of every test
    # run without pinning this module to one Pillow generation.
    flatten = getattr(sample, "get_flattened_data", sample.getdata)
    pixels = list(flatten())
    total = len(pixels) or 1

    counts = Counter(pixels)
    distinct_colours = len(counts)
    _dominant_colour, dominant_count = counts.most_common(1)[0]
    dominant_fraction = dominant_count / total

    channel_spread = sum(max(p) - min(p) for p in pixels) / total
    dark_fraction = sum(1 for p in pixels if sum(p) / 3 < 64) / total
    light_fraction = sum(1 for p in pixels if sum(p) / 3 > 200) / total

    # A cheap proxy for "long flat UI-style runs" vs. photo/scan texture:
    # the fraction of horizontally adjacent sampled pixels that are exactly
    # identical, plus the mean magnitude of the difference where they are not.
    if sample_width > 1 and sample_height > 0:
        pixel_at = sample.load()
        identical = 0
        diff_total = 0
        diff_count = 0
        for y in range(sample_height):
            for x in range(sample_width - 1):
                a = pixel_at[x, y]
                b = pixel_at[x + 1, y]
                delta = abs(a[0] - b[0]) + abs(a[1] - b[1]) + abs(a[2] - b[2])
                diff_total += delta
                diff_count += 1
                if delta == 0:
                    identical += 1
        flat_run_fraction = identical / diff_count if diff_count else 0.0
        mean_local_variance = diff_total / diff_count if diff_count else 0.0
    else:
        flat_run_fraction = 0.0
        mean_local_variance = 0.0

    return {
        "distinct_colours": distinct_colours,
        "dominant_fraction": dominant_fraction,
        "channel_spread": channel_spread,
        "dark_fraction": dark_fraction,
        "light_fraction": light_fraction,
        "flat_run_fraction": flat_run_fraction,
        "mean_local_variance": mean_local_variance,
        "is_greyscale": channel_spread < _GREYSCALE_MAX_CHANNEL_SPREAD,
    }


def _classify(m: dict) -> tuple[str, float]:
    """Map measurements to one of the few kinds this heuristic may assert.

    Order matters: a screenshot of a scanned-document viewer, or a mostly
    white UI, could plausibly satisfy more than one rule, so the checks run
    from the most specific pixel signature (flat UI fills) to the least
    (broad continuous colour), falling through to `unknown` when nothing
    clears its bar.
    """
    if (
        m["distinct_colours"] <= _UI_MAX_DISTINCT_COLOURS
        and m["dominant_fraction"] >= _UI_MIN_DOMINANT_FRACTION
        and m["flat_run_fraction"] >= _UI_MIN_FLAT_RUN_FRACTION
    ):
        return IMAGE_KIND_UI_CAPTURE, CONFIDENCE_UI_CAPTURE

    if (
        m["is_greyscale"]
        and m["light_fraction"] >= _SCAN_MIN_LIGHT_FRACTION
        and m["dark_fraction"] <= _SCAN_MAX_DARK_FRACTION
    ):
        return IMAGE_KIND_SCANNED_DOCUMENT, CONFIDENCE_SCANNED_DOCUMENT

    if m["distinct_colours"] >= _PHOTO_MIN_DISTINCT_COLOURS and not m["is_greyscale"]:
        return IMAGE_KIND_ORDINARY_PHOTO, CONFIDENCE_ORDINARY_PHOTO

    return IMAGE_KIND_UNKNOWN, CONFIDENCE_UNKNOWN


def _describe(width: int, height: int, m: dict) -> str:
    """A factual sentence built only from measurements -- no interpretation."""
    parts = [f"{width}x{height} image", f"with {m['distinct_colours']} distinct sampled colours"]
    if m["flat_run_fraction"] >= _UI_MIN_FLAT_RUN_FRACTION:
        parts.append("and large flat regions")
    elif m["is_greyscale"]:
        parts.append("and a near-greyscale palette")
    else:
        parts.append("and a broad, continuous colour palette")
    return " ".join(parts)
