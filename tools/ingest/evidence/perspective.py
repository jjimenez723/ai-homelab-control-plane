# Portions of this file are adapted from PaddleOCR, which is licensed under the
# Apache License, Version 2.0.
#
#   Copyright (c) 2020 PaddlePaddle Authors. All Rights Reserved.
#   Upstream: https://github.com/PaddlePaddle/PaddleOCR
#   Upstream file: tools/infer/utility.py
#   Upstream function(s): get_rotate_crop_image(), get_minarea_rect_crop()
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# Modified by the Angel control plane: reimplemented with Pillow and a
# pure-Python homography solve in place of OpenCV, with degenerate geometry
# returning None instead of raising, and no dependency on numpy or cv2.

"""Turn a detected quadrilateral into an upright rectangular crop.

A text or figure detector reports a region as an arbitrary quad (or, via
`min_area_quad`, an arbitrary point cloud) in source-image pixel coordinates.
Recognition -- OCR or a vision backend -- wants a small upright raster of just
that region. This module is PaddleOCR's `get_rotate_crop_image()` and
`get_minarea_rect_crop()`, ported off OpenCV: `cv2.getPerspectiveTransform` /
`cv2.warpPerspective` become a hand-solved 8-coefficient projective transform
fed to `PIL.Image.transform`, and `cv2.minAreaRect` becomes a pure-Python
convex hull plus rotating calipers.

DEGENERATE GEOMETRY NEVER RAISES, which is a deliberate divergence from
upstream. Upstream `assert`s exactly four points and then divides by the crop
width and height it just computed, so a zero-area quad is a crash. This module
runs over a whole volume of unknown, possibly hostile or malformed files --
same exposure as `media/probe.py::probe_file` -- and a single bad polygon must
degrade to `None`, not abort a batch. Every public function here returns
`None` (never raises) for: fewer than the required number of points, negative
or non-finite coordinates, zero-area geometry, and a polygon wholly outside the
image being cropped.

No numpy, no cv2. Pillow only, and no filesystem access -- everything here is
image-in, image-or-bytes-out.
"""

from __future__ import annotations

import io
import math
from collections.abc import Sequence

from PIL import Image

from tools.ingest.evidence.types import Point, Polygon

# Upstream's `dst_img_height / dst_img_width >= 1.5` rule.
ROTATE_ASPECT_RATIO = 1.5

# Below this, a coordinate system is treated as singular/degenerate rather than
# trusting float division near zero to behave.
_EPSILON = 1e-9


def _finite_pair(point: Sequence[float]) -> bool:
    try:
        x, y = point
        return math.isfinite(x) and math.isfinite(y)
    except (TypeError, ValueError):
        return False


def _all_finite(points: Sequence[Sequence[float]]) -> bool:
    return all(_finite_pair(point) for point in points)


def _polygon_area(points: Sequence[Sequence[float]]) -> float:
    """Shoelace formula. Works for any simple (non-self-intersecting) polygon."""
    n = len(points)
    if n < 3:
        return 0.0
    total = 0.0
    for i in range(n):
        x1, y1 = points[i]
        x2, y2 = points[(i + 1) % n]
        total += x1 * y2 - x2 * y1
    return abs(total) / 2.0


def _dist(a: Sequence[float], b: Sequence[float]) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def crop_width_height(points: Sequence[tuple[float, float]]) -> tuple[int, int]:
    """Upstream `get_rotate_crop_image`'s width/height computation, verbatim.

    `points` is `[TL, TR, BR, BL]`. Width is the larger of the top and bottom
    edge lengths; height is the larger of the left and right edge lengths.
    Both are `int()`-TRUNCATED, not rounded -- a faithful port truncates.

    This is a small pure-math step, not a public safety boundary: it assumes a
    caller has already validated `points` as a real quad (see `rotate_crop`,
    which does that validation before calling this). Fewer than four points
    still cannot raise here -- it degrades to `(0, 0)`, which every caller
    already treats as "nothing to crop" -- but the meaningful degenerate-input
    contract (returning `None`) belongs to the functions that hand back an
    image.
    """
    if len(points) < 4:
        return (0, 0)
    p0, p1, p2, p3 = points[0], points[1], points[2], points[3]
    width = int(max(_dist(p0, p1), _dist(p2, p3)))
    height = int(max(_dist(p0, p3), _dist(p1, p2)))
    return width, height


def _solve_linear_system(matrix: list[list[float]], rhs: list[float]) -> list[float] | None:
    """Gauss-Jordan elimination with partial pivoting. `None` if singular.

    Pure Python in place of `numpy.linalg.solve`, per the module's ban on
    numpy. `matrix` is square; `len(rhs)` equations in that many unknowns.
    """
    n = len(rhs)
    if n == 0 or len(matrix) != n or any(len(row) != n for row in matrix):
        return None
    # NOTE: deliberately not `_all_finite`, which expects a sequence of 2-element
    # (x, y) points -- an 8-element matrix row would fail to unpack there and
    # be (incorrectly) treated as non-finite on every call.
    if any(not math.isfinite(v) for row in matrix for v in row):
        return None
    if any(not math.isfinite(v) for v in rhs):
        return None

    augmented = [list(row) + [value] for row, value in zip(matrix, rhs)]

    try:
        for col in range(n):
            pivot_row = max(range(col, n), key=lambda r: abs(augmented[r][col]))
            pivot_value = augmented[pivot_row][col]
            if abs(pivot_value) < _EPSILON:
                return None  # singular system: no unique perspective transform
            augmented[col], augmented[pivot_row] = augmented[pivot_row], augmented[col]
            augmented[col] = [v / pivot_value for v in augmented[col]]
            for row in range(n):
                if row == col:
                    continue
                factor = augmented[row][col]
                if factor:
                    augmented[row] = [
                        a - factor * b for a, b in zip(augmented[row], augmented[col])
                    ]
        return [augmented[row][n] for row in range(n)]
    except (ArithmeticError, ValueError):  # pragma: no cover - defence in depth
        return None


def perspective_coefficients(
    src_quad: Sequence[tuple[float, float]], dst_rect: Sequence[tuple[float, float]]
) -> tuple[float, ...] | None:
    """Solve for the 8 coefficients of `PIL.Image.transform(..., PERSPECTIVE, ...)`.

    Pillow maps DESTINATION pixels back to SOURCE pixels: for each output
    pixel `(x, y)`, the sampled source pixel is
    `((a*x + b*y + c) / (g*x + h*y + 1), (d*x + e*y + f) / (g*x + h*y + 1))`.
    To make the output rectangle `dst_rect` display the source quad
    `src_quad`, the transform must therefore be solved taking `dst_rect`'s
    corners as the `(x, y)` inputs and `src_quad`'s corresponding corners as
    the `(u, v)` outputs -- the reverse of what a naive reading of "src to
    dst" suggests. Getting this backwards produces a transform that samples
    the wrong region entirely, silently.

    Both sequences must have exactly 4 points, corresponding by position.
    Returns `None` if the system is singular (degenerate quad) rather than
    raising.
    """
    if len(src_quad) != 4 or len(dst_rect) != 4:
        return None
    if not _all_finite(src_quad) or not _all_finite(dst_rect):
        return None

    matrix: list[list[float]] = []
    rhs: list[float] = []
    for (u, v), (x, y) in zip(src_quad, dst_rect):
        matrix.append([x, y, 1, 0, 0, 0, -x * u, -y * u])
        rhs.append(u)
        matrix.append([0, 0, 0, x, y, 1, -x * v, -y * v])
        rhs.append(v)

    solution = _solve_linear_system(matrix, rhs)
    if solution is None:
        return None
    return tuple(solution)


def _degenerate_quad(points: Sequence[tuple[float, float]], image_size: tuple[int, int] | None) -> bool:
    if len(points) != 4:
        return True
    if not _all_finite(points):
        return True
    if any(x < 0 or y < 0 for x, y in points):
        return True
    if _polygon_area(points) < _EPSILON:
        return True
    if image_size is not None:
        width, height = image_size
        xs = [p[0] for p in points]
        ys = [p[1] for p in points]
        # "Wholly outside" only -- a box that merely straddles an edge (common
        # for detections near the border) is not degenerate.
        if max(xs) < 0 or min(xs) > width or max(ys) < 0 or min(ys) > height:
            return True
    return False


def rotate_crop(image: Image.Image, polygon: Polygon) -> Image.Image | None:
    """Upstream `get_rotate_crop_image`, reimplemented on Pillow.

    Solves the exact perspective transform mapping the quad onto an upright
    `width x height` rectangle, resamples with bicubic interpolation, and
    rotates 90 degrees counter-clockwise when the result is much taller than
    it is wide (`height / width >= ROTATE_ASPECT_RATIO`), matching upstream's
    `np.rot90` step exactly for a 4-sided polygon.

    Interpolation note: upstream also fills any sampled point outside the
    source image with `cv2.BORDER_REPLICATE` (repeat the edge pixel). Pillow's
    `Image.transform` has no equivalent border mode; it samples zero/black
    outside the source. In practice this is a non-issue here, because the
    destination rectangle's size is derived directly from the quad's own edge
    lengths, so the transform is (barring detector noise) an into-bounds
    resampling of the quad's own interior -- out-of-bounds sampling is limited
    to sub-pixel edge effects, not a visible border.

    Direction of `transpose(Image.ROTATE_90)` was verified empirically (not
    assumed from documentation) by rotating a 4x2 test image with a distinct
    marker pixel in each corner: the corner that was top-right before the
    call becomes top-left after it, which is a counter-clockwise turn as
    displayed -- matching upstream's `np.rot90(k=1)`.

    Returns `None` for degenerate input (see module docstring) instead of
    raising.
    """
    try:
        points = polygon.points
        if _degenerate_quad(points, image.size):
            return None

        width, height = crop_width_height(points)
        if width <= 0 or height <= 0:
            return None

        dst_rect: tuple[Point, ...] = ((0, 0), (width, 0), (width, height), (0, height))
        coeffs = perspective_coefficients(points, dst_rect)
        if coeffs is None:
            return None

        cropped = image.transform((width, height), Image.PERSPECTIVE, coeffs, resample=Image.BICUBIC)

        if height / width >= ROTATE_ASPECT_RATIO:
            cropped = cropped.transpose(Image.ROTATE_90)
        return cropped
    except Exception:  # noqa: BLE001 - untrusted geometry over a whole volume
        return None


def _convex_hull(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Andrew's monotone chain, pure Python. Returns hull vertices in CCW order.

    In place of `cv2.minAreaRect`'s internal hull computation. Duplicate
    points are collapsed by the initial sort/dedupe; a hull of fewer than 3
    points means the input was collinear or a single point, which the caller
    treats as degenerate (no enclosing rectangle has a well-defined area).
    """
    pts = sorted(set(points))
    if len(pts) <= 2:
        return pts

    def cross(o: tuple[float, float], a: tuple[float, float], b: tuple[float, float]) -> float:
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower: list[tuple[float, float]] = []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)

    upper: list[tuple[float, float]] = []
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)

    return lower[:-1] + upper[:-1]


def _min_area_rect(hull: list[tuple[float, float]]) -> tuple[tuple[float, float], ...] | None:
    """Rotating calipers: the minimum-area rectangle enclosing a convex hull.

    For each hull edge, project every hull point onto that edge's direction
    and its perpendicular; the extents in that rotated frame give one
    candidate rectangle. The true minimum-area enclosing rectangle always has
    one side flush with a hull edge, so checking every edge (O(n^2) here,
    rather than the classic O(n) two-pointer sweep) is guaranteed to find it
    and is cheap for the small point counts a detected region produces.
    """
    n = len(hull)
    if n < 3:
        return None

    best_area: float | None = None
    best_corners: tuple[tuple[float, float], ...] | None = None
    for i in range(n):
        p1, p2 = hull[i], hull[(i + 1) % n]
        edge_x, edge_y = p2[0] - p1[0], p2[1] - p1[1]
        length = math.hypot(edge_x, edge_y)
        if length < _EPSILON:
            continue
        ux, uy = edge_x / length, edge_y / length
        vx, vy = -uy, ux

        min_u = min_v = math.inf
        max_u = max_v = -math.inf
        for px, py in hull:
            u = px * ux + py * uy
            v = px * vx + py * vy
            min_u, max_u = min(min_u, u), max(max_u, u)
            min_v, max_v = min(min_v, v), max(max_v, v)

        area = (max_u - min_u) * (max_v - min_v)
        if best_area is None or area < best_area:
            best_area = area
            best_corners = (
                (min_u * ux + min_v * vx, min_u * uy + min_v * vy),
                (max_u * ux + min_v * vx, max_u * uy + min_v * vy),
                (max_u * ux + max_v * vx, max_u * uy + max_v * vy),
                (min_u * ux + max_v * vx, min_u * uy + max_v * vy),
            )

    return best_corners


def min_area_quad(points: Sequence[tuple[float, float]]) -> tuple[tuple[int, int], ...] | None:
    """Upstream `get_minarea_rect_crop`'s box logic, minus OpenCV.

    Replaces `cv2.minAreaRect` with a pure-Python convex hull + rotating
    calipers, then reproduces upstream's corner reassignment exactly: sort the
    4 rectangle corners by x only, then use the relative y of the two
    leftmost and two rightmost points to assign `[TL, TR, BR, BL]`.

    Returns `None` for fewer than 3 points, a degenerate (collinear or
    single-point) hull, non-finite coordinates, or a resulting rectangle with
    no measurable area -- never raises.
    """
    try:
        pts = [(float(x), float(y)) for x, y in points]
    except (TypeError, ValueError):
        return None
    if len(pts) < 3 or not _all_finite(pts):
        return None

    hull = _convex_hull(pts)
    if len(hull) < 3:
        return None

    rect = _min_area_rect(hull)
    if rect is None:
        return None

    # Upstream: sort by x only, then use points[1]/points[0] and
    # points[3]/points[2]'s relative y to assign TL/TR/BR/BL.
    ordered = sorted(rect, key=lambda p: p[0])
    index_a, index_d = (0, 1) if ordered[1][1] > ordered[0][1] else (1, 0)
    index_b, index_c = (2, 3) if ordered[3][1] > ordered[2][1] else (3, 2)
    box = (ordered[index_a], ordered[index_b], ordered[index_c], ordered[index_d])

    quad = tuple((int(round(x)), int(round(y))) for x, y in box)
    if _polygon_area(quad) < _EPSILON:
        # Rounding to the integer `Point` grid collapsed a thin sliver to
        # zero area; there is nothing usable left to crop.
        return None
    return quad


def min_area_rect_crop(image: Image.Image, polygon: Polygon) -> Image.Image | None:
    """Upstream `get_minarea_rect_crop`: minimum-area quad, then rotate-crop it.

    For a polygon that is not already an axis-honest 4-point box (e.g. a rough
    region from a coarser detector), this finds the tightest enclosing
    rotated rectangle first. Returns `None` for the same degenerate cases as
    `min_area_quad` and `rotate_crop`.
    """
    quad = min_area_quad(polygon.points)
    if quad is None:
        return None
    return rotate_crop(image, Polygon(points=quad))


def crop_to_png_bytes(image: Image.Image, polygon: Polygon) -> bytes | None:
    """`rotate_crop`, encoded to PNG bytes -- the bytes-in/bytes-out form a
    backend actually uses. Never writes to disk; returns `None` on any
    failure, including a degenerate polygon or an unencodable image mode.
    """
    try:
        cropped = rotate_crop(image, polygon)
        if cropped is None:
            return None
        if cropped.mode not in {"RGB", "RGBA", "L", "LA"}:
            cropped = cropped.convert("RGB")
        buffer = io.BytesIO()
        cropped.save(buffer, format="PNG")
        return buffer.getvalue()
    except Exception:  # noqa: BLE001 - callers get None, never an exception
        return None
