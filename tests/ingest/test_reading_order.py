"""Reading order must be deterministic, or a caption reads as a bag of words.

`reading_order.py` is a line-for-line port of PaddleOCR's `sorted_boxes()`
(see the attribution header at the top of that file), reimplemented over
Angel's own `Polygon`/`OCRBlock` with no NumPy. This suite pins its geometric
contract -- top-to-bottom first, left-to-right within a same-line tolerance --
and, deliberately, the one inherited quirk the module's docstring warns about:
a single backward-propagating bubble pass can leave an earlier box only
*approximately* ordered when an intervening box breaks the chain. That is
tested and asserted explicitly here, not "fixed" on the way past -- see the
module docstring on why an "obviously correct" replacement would itself be an
unvalidated behaviour change.
"""

from __future__ import annotations

import random
import unittest

from tools.ingest.evidence.reading_order import (
    SAME_LINE_TOLERANCE_PX,
    assign_reading_order,
    sorted_polygons,
)
from tools.ingest.evidence.types import OCRBlock, Polygon


def _box(x: int, y: int, *, w: int = 10, h: int = 10) -> Polygon:
    return Polygon.from_bbox(x, y, x + w, y + h)


def _block(text: str, x: int, y: int, *, page: int = 1) -> OCRBlock:
    return OCRBlock(text=text, polygon=_box(x, y), page=page, confidence=0.9, reading_order=0, engine="e", engine_version="1")


class EmptyAndSingletonInputTests(unittest.TestCase):
    def test_sorted_polygons_on_empty_input_is_a_no_op(self):
        self.assertEqual(sorted_polygons([]), [])

    def test_sorted_polygons_on_a_single_polygon_is_unchanged(self):
        polygon = _box(5, 5)
        self.assertEqual(sorted_polygons([polygon]), [polygon])

    def test_assign_reading_order_on_empty_input(self):
        self.assertEqual(assign_reading_order([]), ())

    def test_assign_reading_order_on_a_single_block_is_stamped_zero(self):
        block = _block("only", 0, 0)
        result = assign_reading_order([block])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].reading_order, 0)
        self.assertEqual(result[0].text, "only")


class TwoBoxOrderingTests(unittest.TestCase):
    def test_two_boxes_on_clearly_separate_lines_sort_top_to_bottom(self):
        top = _box(100, 0)  # x is large, y is small: would sort left-first if this were x-major
        bottom = _box(0, 500)
        result = sorted_polygons([bottom, top])
        self.assertEqual(result, [top, bottom])

    def test_two_boxes_on_the_same_line_sort_left_to_right(self):
        left = _box(0, 100)
        right = _box(200, 103)  # within tolerance of `left`'s y
        result = sorted_polygons([right, left])
        self.assertEqual(result, [left, right])

    def test_vertical_difference_within_tolerance_is_treated_as_one_line(self):
        a = _box(50, 100)
        b = _box(0, 100 + SAME_LINE_TOLERANCE_PX - 1)
        result = sorted_polygons([a, b])
        self.assertEqual(result, [b, a], "same line: ordered by x, not by input order")

    def test_vertical_difference_at_or_past_tolerance_is_not_the_same_line(self):
        """The comparison is a strict `<`, matching upstream exactly: a gap of
        precisely `SAME_LINE_TOLERANCE_PX` is NOT the same line."""
        a = _box(50, 0)
        b = _box(0, SAME_LINE_TOLERANCE_PX)
        result = sorted_polygons([a, b])
        self.assertEqual(result, [a, b], "different lines: ordered by y, x is irrelevant")


class IdenticalBoxesTests(unittest.TestCase):
    def test_identical_polygons_keep_their_original_relative_order(self):
        """Two blocks can legitimately share a polygon; the underlying sort is
        stable, and this is what makes that deterministic rather than an
        implementation accident."""
        first = _block("first", 10, 10)
        second = _block("second", 10, 10)
        result = assign_reading_order([first, second])
        self.assertEqual([block.text for block in result], ["first", "second"])


class DeterminismTests(unittest.TestCase):
    """Shuffling the input must not change the output -- true whenever every
    box has a distinct (y, x) key, because the presorting step alone then
    fully determines the result regardless of input order. (Ties are only
    guaranteed *stable*, i.e. relative to their own input order -- see
    `IdenticalBoxesTests` -- not invariant across different shufflings of the
    same ties, which is a different and weaker guarantee.)
    """

    def test_shuffling_distinctly_keyed_boxes_never_changes_the_result(self):
        blocks = [_block(f"b{i}", x=(i * 37) % 400, y=(i * 53) % 500) for i in range(12)]
        baseline = [block.text for block in assign_reading_order(blocks)]
        rng = random.Random(1234)
        for _ in range(20):
            shuffled = blocks[:]
            rng.shuffle(shuffled)
            result = [block.text for block in assign_reading_order(shuffled)]
            self.assertEqual(result, baseline)


class RealisticPageLayoutTests(unittest.TestCase):
    def test_a_two_column_ish_page_produces_a_stable_documented_order(self):
        """Two rows, two columns. Left column above right on each row, and
        both rows of the left column come before either row of the right
        column only if the row gap exceeds the same-line tolerance -- which
        it does here (100px vs. a 10px tolerance)."""
        row1_left = _block("row1-left", 0, 0)
        row1_right = _block("row1-right", 300, 2)
        row2_left = _block("row2-left", 0, 100)
        row2_right = _block("row2-right", 300, 102)
        result = assign_reading_order([row2_right, row1_left, row2_left, row1_right])
        self.assertEqual(
            [block.text for block in result],
            ["row1-left", "row1-right", "row2-left", "row2-right"],
        )
        self.assertEqual([block.reading_order for block in result], [0, 1, 2, 3])


class MultiPageTests(unittest.TestCase):
    def test_pages_are_visited_in_ascending_order_and_reset_the_index(self):
        page2 = _block("p2", 0, 0, page=2)
        page1_b = _block("p1-second", 50, 0, page=1)
        page1_a = _block("p1-first", 0, 0, page=1)
        result = assign_reading_order([page2, page1_b, page1_a])
        self.assertEqual([(b.page, b.reading_order, b.text) for b in result], [
            (1, 0, "p1-first"), (1, 1, "p1-second"), (2, 0, "p2"),
        ])

    def test_every_other_field_survives_untouched_except_reading_order(self):
        original = OCRBlock(text="x", polygon=_box(0, 0), page=1, confidence=0.42, reading_order=99, engine="tesseract-ocr", engine_version="1+5.5.3")
        (stamped,) = assign_reading_order([original])
        self.assertEqual(stamped.reading_order, 0)
        for field in ("text", "polygon", "page", "confidence", "engine", "engine_version"):
            self.assertEqual(getattr(stamped, field), getattr(original, field))


class UpstreamQuirkTests(unittest.TestCase):
    """The documented, deliberately-inherited approximate ordering.

    Three boxes, each within `SAME_LINE_TOLERANCE_PX` of its immediate
    neighbour in y (0 -> 8 -> 16), so a human would read them as one
    continuous line -- but the first-to-last gap (16) exceeds the tolerance.
    The single backward bubble pass compares `A` against `C` directly at the
    point where it would otherwise let `C` continue bubbling past `A`, finds
    them "not the same line" by that direct comparison, and stops -- leaving
    `C` (x=0, the true leftmost box on the line) stranded last instead of
    first. This is exactly the failure mode the module docstring names: "an
    intervening box can leave an earlier box only approximately ordered."
    Verified empirically against this exact input before being encoded here.
    """

    def test_a_three_box_chain_is_left_only_approximately_ordered(self):
        a = _box(100, 0)
        b = _box(50, 8)
        c = _box(0, 16)
        result = sorted_polygons([a, b, c], tolerance=SAME_LINE_TOLERANCE_PX)
        self.assertEqual(
            [p.points[0] for p in result], [(50, 8), (100, 0), (0, 16)],
            "this is the inherited quirk, not the 'obviously correct' [C, B, A] "
            "a naive one-line reading would expect -- see the class docstring",
        )


if __name__ == "__main__":
    unittest.main()
