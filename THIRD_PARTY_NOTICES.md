# Third-Party Notices

This repository contains a small amount of code adapted from other open-source
projects. Everything listed here is isolated in a named module, carries the
upstream licence header in the file itself, and is recorded below so the
provenance survives a future refactor that moves the code.

Nothing in this repository vendors an upstream project wholesale. Where an
upstream idea was simple enough to reimplement cleanly, it was reimplemented
rather than copied; those cases are listed separately at the end so a reader
can see the line that was drawn and why.

## PaddleOCR

* Project: PaddleOCR
* Upstream: https://github.com/PaddlePaddle/PaddleOCR
* Licence: Apache License, Version 2.0
* Upstream notice: `Copyright (c) 2020 PaddlePaddle Authors. All Rights Reserved.`

A copy of the Apache License, Version 2.0 is available at
http://www.apache.org/licenses/LICENSE-2.0.

Two modules in this repository contain code adapted from PaddleOCR. Both carry
the full Apache-2.0 header and a line stating what was changed. No other file in
this repository is derived from PaddleOCR, and nothing here imports `paddle`,
`paddleocr`, `cv2` or `numpy`.

### `tools/ingest/evidence/reading_order.py`

* Adapted from: `tools/infer/predict_system.py`
* Upstream function: `sorted_boxes()`
* What it does: orders detected text regions top-to-bottom and then
  left-to-right, treating regions whose tops differ by fewer than ten pixels as
  belonging to the same line.
* Modifications: rewritten in pure Python over Angel's own `Polygon` value type,
  with no NumPy; empty and single-element input handled explicitly; the
  same-line tolerance exposed as a named constant. The ordering behaviour,
  including the upstream pass's approximate handling of interrupted lines, is
  reproduced deliberately rather than "improved", because it is the behaviour
  that has been validated against real pages.

### `tools/ingest/evidence/perspective.py`

* Adapted from: `tools/infer/utility.py`
* Upstream functions: `get_rotate_crop_image()`, `get_minarea_rect_crop()`
* What it does: turns a detected quadrilateral into an upright rectangular crop
  suitable for text recognition, including the rule that a crop at least 1.5
  times taller than it is wide is rotated ninety degrees.
* Modifications: reimplemented with Pillow and a pure-Python homography solve in
  place of OpenCV's `getPerspectiveTransform` / `warpPerspective`;
  `cv2.minAreaRect` replaced by a pure-Python convex hull and rotating-calipers
  minimum-area rectangle. Degenerate geometry returns `None` instead of raising
  or dividing by zero, because this code runs over a whole volume of unknown
  files and must degrade rather than abort a scan.

## PaddleOCR ideas studied and deliberately reimplemented instead

These upstream functions were read and understood, but the code here is Angel's
own. They are recorded for honesty about where the ideas came from; they are not
derivative works and carry no Apache header.

| Upstream | Angel module | Why it was reimplemented |
|---|---|---|
| `ppocr/utils/utility.py::check_and_read()` | `tools/ingest/evidence/raster.py` | The useful behaviour is "rasterize PDF pages at a bounded resolution". The upstream implementation is entangled with PyMuPDF (AGPL), sniffs file types from the last three characters of a filename, reads only the first frame of a GIF, and returns a two-tuple on one failure path where every other path returns a three-tuple. Angel rasterizes with pypdfium2 and caps by total pixels rather than by a hardcoded 2000-pixel edge. |
| `ppocr/utils/utility.py::binarize_img()` | `tools/ingest/evidence/preprocess.py` | Six lines wrapping OpenCV's Otsu threshold. Angel computes Otsu from Pillow's 256-bin histogram in pure Python. Too small to be worth the coupling or the attribution. |
| `ppocr/utils/utility.py::alpha_to_color()` | `tools/ingest/evidence/preprocess.py` | Standard alpha compositing against a solid background. Pillow does it directly. |
| `tools/infer/utility.py::slice_generator()`, `merge_fragmented()` | not implemented | Large-image tiling with detection results mapped back to global coordinates. Genuinely valuable for architectural plans and very large scans, but no Angel fixture demonstrates the need yet, and adding it before the normalized evidence architecture exists would be optimizing a stage that has not been measured. Recorded as deferred work in `docs/17-file-evidence.md`. |

## Well-known algorithms implemented from their published description

Not adapted from any project's source, and listed only so a reader who
recognizes the technique knows where it came from and that nothing was copied.

| Technique | Angel module | Note |
|---|---|---|
| Average hash (aHash) perceptual image hash | `tools/ingest/evidence/backends/ffmpeg_video.py::average_hash` | The classical published construction -- greyscale, resize to 8x8, one bit per pixel against the mean of those 64 -- written directly in Pillow in about a dozen lines. No code from `imagehash`, `pHash`, or any other implementation was consulted or copied, and neither is a dependency. Used only to decide whether two sampled video frames are the same picture; the exact grid size and Hamming threshold Angel uses are documented in `docs/17-file-evidence.md`. |
