# Portions of this file are adapted from PaddleOCR, which is licensed under the
# Apache License, Version 2.0.
#
#   Copyright (c) 2020 PaddlePaddle Authors. All Rights Reserved.
#   Upstream: https://github.com/PaddlePaddle/PaddleOCR
#   Upstream file: tools/infer/predict_system.py
#   Upstream function(s): sorted_boxes()
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
# Modified by the Angel control plane: rewritten in pure Python over Angel
# `Polygon` values with no NumPy, with explicit handling for empty and
# single-element input, and the tolerance exposed as a named constant.

"""Deterministic top-to-bottom, left-to-right ordering of detected text regions.

A vision detector returns text regions in whatever order it found them, which
is usually neither reading order nor stable across runs. This module answers
"in what order would a person read these regions" using only geometry -- no
language model, no layout classifier -- so that a caption assembled from OCR
blocks reads as a sentence rather than a bag of fragments.

The algorithm below is PaddleOCR's `sorted_boxes()`, ported line for line. It
is *not* a general-purpose reading-order solver and it is not meant to be one:
it is a single backward-propagating insertion pass over boxes pre-sorted by
their top-left corner, and it has a known quirk -- a box that interrupts a
run of same-line boxes can leave an earlier box only approximately ordered,
because the inner loop stops at the first pair that is not obviously
out of order rather than re-checking the whole line. That quirk is inherited
deliberately, not fixed, for two reasons: it is what has been validated against
real scanned pages upstream, and "obviously correct" replacement sorts (e.g. a
plain sort on the box's vertical band) are an *unvalidated change of behaviour*
dressed up as a cleanup. If a real regression is ever traced to this quirk, fix
it with evidence, not by "improving" the algorithm on the way past.

Pure module: no filesystem access, no subprocess, no numpy. Safe to run over
untrusted geometry from an untrusted detector.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Sequence
from typing import TypeVar

from tools.ingest.evidence.types import OCRBlock, Point, Polygon

# Upstream's hardcoded `10`. A strict `<` comparison, matching upstream: two
# tops exactly 10px apart are *not* treated as the same line.
SAME_LINE_TOLERANCE_PX = 10

_T = TypeVar("_T")


def _order_by_first_point(
    items: list[_T], *, key: Callable[[_T], Point], tolerance: int
) -> list[_T]:
    """The shared core of upstream `sorted_boxes()`, keyed on an arbitrary point.

    `sorted_polygons` and `assign_reading_order` both need this exact ordering
    behaviour, but `assign_reading_order` must carry an `OCRBlock` -- not a bare
    `Polygon` -- through the sort so it can recover which block ended up where.
    Two blocks can legitimately share an identical polygon (rare, but the type
    does not forbid it), so recovering block identity by looking up a sorted
    `Polygon` value afterwards would be ambiguous. Factoring the algorithm out
    and applying it directly to whatever carries the point keeps both callers
    provably identical without that ambiguity.
    """
    # Python's `sorted` is stable, so items with an identical key keep their
    # relative input order here -- this is what makes the whole function
    # deterministic for duplicate polygons.
    ordered = sorted(items, key=lambda item: (key(item)[1], key(item)[0]))
    count = len(ordered)
    for i in range(count - 1):
        # Upstream walks backward from the newly placed element and stops at
        # the first pair that is either far apart vertically or already in x
        # order -- a single left-propagating insertion pass, not a sort to
        # convergence. An intervening box can therefore break the chain and
        # leave an earlier box only approximately ordered; see the module
        # docstring for why that is kept rather than "fixed".
        for j in range(i, -1, -1):
            a, b = key(ordered[j]), key(ordered[j + 1])
            if abs(b[1] - a[1]) < tolerance and b[0] < a[0]:
                ordered[j], ordered[j + 1] = ordered[j + 1], ordered[j]
            else:
                break
    return ordered


def sorted_polygons(
    polygons: Sequence[Polygon], *, tolerance: int = SAME_LINE_TOLERANCE_PX
) -> list[Polygon]:
    """Order polygons the way PaddleOCR's `sorted_boxes()` orders detected boxes.

    Primary key is the first point of each polygon, `(y, x)` -- i.e. top then
    left. Ties within `tolerance` vertical pixels are then resolved by a single
    backward bubble pass on x, exactly as upstream does it (see module
    docstring for the quirk this inherits). Empty input is a no-op; a single
    polygon is returned unchanged.
    """
    if len(polygons) <= 1:
        return list(polygons)
    return _order_by_first_point(list(polygons), key=lambda polygon: polygon.points[0], tolerance=tolerance)


def assign_reading_order(
    blocks: Sequence[OCRBlock], *, tolerance: int = SAME_LINE_TOLERANCE_PX
) -> tuple[OCRBlock, ...]:
    """Group `blocks` by page and stamp each with its 0-based reading order.

    Ordering within a page is exactly `sorted_polygons` applied to
    `block.polygon` (see `_order_by_first_point`, shared by both functions).
    Pages are visited in ascending order so the result is deterministic
    regardless of the input's page interleaving. Equal keys never reorder
    unpredictably: the underlying sort is stable, so blocks whose polygons are
    identical keep their original relative order.
    """
    if not blocks:
        return ()

    by_page: dict[int, list[OCRBlock]] = {}
    for block in blocks:
        by_page.setdefault(block.page, []).append(block)

    result: list[OCRBlock] = []
    for page in sorted(by_page):
        page_blocks = by_page[page]
        ordered = (
            page_blocks
            if len(page_blocks) <= 1
            else _order_by_first_point(
                page_blocks, key=lambda block: block.polygon.points[0], tolerance=tolerance
            )
        )
        for index, block in enumerate(ordered):
            result.append(dataclasses.replace(block, reading_order=index))
    return tuple(result)
