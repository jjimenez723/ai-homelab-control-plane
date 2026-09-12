"""Perspective cropping never mutates the source, and degrades on bad geometry.

`perspective.py` is PaddleOCR's `get_rotate_crop_image()`/`get_minarea_rect_crop()`
ported off OpenCV onto a hand-solved projective transform and Pillow (see the
attribution header at the top of that file). This suite exercises the two
safety properties the binding spec singles out for it: DEGENERATE GEOMETRY
NEVER RAISES (a deliberate divergence from upstream, which asserts and then
crashes), and the source image's bytes on disk are never touched -- every
function here is image/bytes in, image/bytes out, with nothing that could
open the source path for writing.
"""

from __future__ import annotations

import hashlib
import io
import math
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from tools.ingest.evidence.perspective import (
    ROTATE_ASPECT_RATIO,
    crop_to_png_bytes,
    crop_width_height,
    min_area_quad,
    min_area_rect_crop,
    perspective_coefficients,
    rotate_crop,
)
from tools.ingest.evidence.types import Polygon

from tests.ingest.evidence_fakes import make_tilted_rect_image


def _dominant_color(image: Image.Image) -> tuple[int, int, int]:
    from collections import Counter

    rgb = image.convert("RGB")
    counts = Counter(rgb.getdata())
    return counts.most_common(1)[0][0]


def _close(a: tuple[int, int, int], b: tuple[int, int, int], *, tolerance: int = 20) -> bool:
    return all(abs(x - y) <= tolerance for x, y in zip(a, b))


class RotateCropRecoversTheDrawnRectangleTests(unittest.TestCase):
    def test_a_slightly_tilted_rectangle_crops_back_to_its_own_colour(self):
        color = (200, 30, 30)
        with tempfile.TemporaryDirectory() as tmp:
            path, corners = make_tilted_rect_image(
                Path(tmp) / "tilted.png", angle=12, rect_size=(160, 60), color=color,
            )
            with Image.open(path) as source:
                source.load()
                cropped = rotate_crop(source, Polygon(points=corners))

        self.assertIsNotNone(cropped)
        self.assertTrue(_close(_dominant_color(cropped), color), f"{_dominant_color(cropped)} not close to {color}")

    def test_crop_to_png_bytes_returns_decodable_bytes_not_a_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, corners = make_tilted_rect_image(Path(tmp) / "tilted.png", angle=8, rect_size=(160, 60))
            with Image.open(path) as source:
                source.load()
                png_bytes = crop_to_png_bytes(source, Polygon(points=corners))

        self.assertIsInstance(png_bytes, bytes)
        with Image.open(io.BytesIO(png_bytes)) as decoded:
            decoded.load()
            self.assertGreater(decoded.width, 0)
            self.assertGreater(decoded.height, 0)

    def test_a_tall_quad_comes_back_rotated(self):
        """`rect_size` with height/width >= `ROTATE_ASPECT_RATIO` must trigger
        the 90-degree counter-clockwise rotation upstream also performs."""
        rect_size = (60, 160)  # h/w ~= 2.67 >= 1.5
        with tempfile.TemporaryDirectory() as tmp:
            path, corners = make_tilted_rect_image(Path(tmp) / "tall.png", angle=5, rect_size=rect_size, color=(10, 120, 10))
            with Image.open(path) as source:
                source.load()
                crop_w, crop_h = crop_width_height(corners)
                self.assertGreaterEqual(crop_h / crop_w, ROTATE_ASPECT_RATIO)
                cropped = rotate_crop(source, Polygon(points=corners))

        self.assertIsNotNone(cropped)
        # After the 90-degree rotation, the *displayed* width/height swap
        # relative to the unrotated crop's own width/height.
        self.assertEqual(cropped.width, crop_h)
        self.assertEqual(cropped.height, crop_w)
        self.assertTrue(_close(_dominant_color(cropped), (10, 120, 10)))


class DegenerateGeometryNeverRaisesTests(unittest.TestCase):
    """Upstream `assert`s and then divides by zero; this package must not."""

    def setUp(self):
        self.image = Image.new("RGB", (200, 200), (255, 255, 255))

    def test_fewer_than_four_points(self):
        self.assertIsNone(rotate_crop(self.image, Polygon(points=((0, 0), (10, 0), (10, 10)))))

    def test_negative_coordinates(self):
        self.assertIsNone(rotate_crop(self.image, Polygon(points=((-5, -5), (10, -5), (10, 10), (-5, 10)))))

    def test_zero_area_quad(self):
        self.assertIsNone(rotate_crop(self.image, Polygon(points=((5, 5), (5, 5), (5, 5), (5, 5)))))

    def test_collinear_zero_area_quad(self):
        self.assertIsNone(rotate_crop(self.image, Polygon(points=((0, 0), (5, 0), (10, 0), (15, 0)))))

    def test_a_quad_wholly_outside_the_image(self):
        self.assertIsNone(rotate_crop(self.image, Polygon(points=((1000, 1000), (1010, 1000), (1010, 1010), (1000, 1010)))))

    def test_non_finite_values(self):
        self.assertIsNone(rotate_crop(self.image, Polygon(points=((0, 0), (float("inf"), 0), (10, 10), (0, 10)))))
        self.assertIsNone(rotate_crop(self.image, Polygon(points=((0, 0), (float("nan"), 0), (10, 10), (0, 10)))))

    def test_crop_to_png_bytes_returns_none_rather_than_raising(self):
        self.assertIsNone(crop_to_png_bytes(self.image, Polygon(points=((0, 0), (1, 1)))))

    def test_perspective_coefficients_on_a_singular_system(self):
        identical = ((10, 10), (10, 10), (10, 10), (10, 10))
        dst = ((0, 0), (5, 0), (5, 5), (0, 5))
        self.assertIsNone(perspective_coefficients(identical, dst))

    def test_min_area_quad_degenerates_cleanly(self):
        self.assertIsNone(min_area_quad([(0, 0), (1, 1)]))  # < 3 points
        self.assertIsNone(min_area_quad([(0, 0), (5, 0), (10, 0)]))  # collinear
        self.assertIsNone(min_area_quad([(0, 0), (float("nan"), 1), (2, 2)]))

    def test_min_area_rect_crop_on_degenerate_input_returns_none(self):
        self.assertIsNone(min_area_rect_crop(self.image, Polygon(points=((0, 0), (5, 0), (10, 0)))))

    def test_min_area_rect_crop_recovers_a_noisy_quad(self):
        """A roughly-axis-aligned but slightly noisy region still produces a
        valid crop rather than None."""
        noisy = Polygon(points=((10, 9), (110, 11), (109, 61), (9, 59)))
        cropped = min_area_rect_crop(self.image, noisy)
        self.assertIsNotNone(cropped)
        self.assertGreater(cropped.width, 0)
        self.assertGreater(cropped.height, 0)


class SourceImageIsNeverMutatedTests(unittest.TestCase):
    def test_source_bytes_on_disk_are_unchanged_after_cropping(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, corners = make_tilted_rect_image(Path(tmp) / "tilted.png", angle=20)
            before = hashlib.sha256(path.read_bytes()).hexdigest()

            with Image.open(path) as source:
                source.load()
                rotate_crop(source, Polygon(points=corners))
                crop_to_png_bytes(source, Polygon(points=corners))
                min_area_rect_crop(source, Polygon(points=corners))
                # And several degenerate calls too, for good measure.
                rotate_crop(source, Polygon(points=((0, 0), (1, 1))))
                rotate_crop(source, Polygon(points=((-1, -1), (5, -1), (5, 5), (-1, 5))))

            after = hashlib.sha256(path.read_bytes()).hexdigest()

        self.assertEqual(before, after)

    def test_no_new_files_appear_beside_the_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path, corners = make_tilted_rect_image(root / "tilted.png", angle=15)
            before = {p.name for p in root.iterdir()}

            with Image.open(path) as source:
                source.load()
                crop_to_png_bytes(source, Polygon(points=corners))

            after = {p.name for p in root.iterdir()}

        self.assertEqual(before, after)


class CropWidthHeightTests(unittest.TestCase):
    def test_matches_the_larger_of_each_pair_of_edges_truncated_not_rounded(self):
        points = ((0, 0), (10, 0), (10, 4), (0, 4))
        self.assertEqual(crop_width_height(points), (10, 4))

    def test_fewer_than_four_points_is_zero_zero(self):
        self.assertEqual(crop_width_height([(0, 0), (1, 1)]), (0, 0))


if __name__ == "__main__":
    unittest.main()
