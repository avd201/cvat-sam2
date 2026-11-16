"""ROI geometry: orientation convention, 3-point form, several lines, centroid rule, validation."""
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sam2common.roi import MAX_LINES, Roi, RoiError  # noqa: E402


def blob(h, w, x0, x1, y0, y1):
    m = np.zeros((h, w), bool)
    m[y0:y1, x0:x1] = True
    return m


def test_four_numbers_keep_the_on_screen_left_of_the_walking_direction():
    # Walk from (100,0) down to (100,200) (screen: downwards). Facing down, your left hand points to +x (screen right).
    r = Roi([[100, 0, 100, 200]])
    assert r.contains(150, 50) and not r.contains(50, 50)
    # Walk the other way (up): left is now screen-left.
    r = Roi([[100, 200, 100, 0]])
    assert r.contains(50, 50) and not r.contains(150, 50)
    # Walk to the right along y=100: left is UP on screen (smaller y).
    r = Roi([[0, 100, 200, 100]])
    assert r.contains(50, 40) and not r.contains(50, 160)


def test_six_numbers_keep_the_side_of_the_third_point_whatever_the_drawing_direction():
    for first, second in (((100, 0), (100, 200)), ((100, 200), (100, 0))):
        keep_right = Roi([[*first, *second, 300, 50]])
        assert keep_right.contains(300, 50) and keep_right.contains(150, 10) and not keep_right.contains(50, 10)
        keep_left = Roi([[*first, *second, 10, 50]])
        assert keep_left.contains(50, 10) and not keep_left.contains(150, 10)


def test_inclined_line():
    r = Roi([[0, 0, 100, 100, 100, 0]])        # the diagonal; keep the upper-right triangle (point (100,0))
    assert r.contains(80, 10) and not r.contains(10, 80)


def test_a_point_exactly_on_the_line_is_inside():
    assert Roi([[100, 0, 100, 200]]).contains(100, 77)


def test_lines_are_infinite_not_walls():
    r = Roi([[100, 0, 100, 10, 200, 5]])       # a short drawn segment still splits the whole image
    assert r.contains(150, 5000) and not r.contains(50, -5000)


def test_several_lines_intersect_their_inside_half_planes():
    band = Roi([[100, 0, 100, 10, 200, 5], [400, 0, 400, 10, 300, 5]])   # keep 100 <= x <= 400
    assert band.contains(250, 0) and not band.contains(50, 0) and not band.contains(450, 0)
    assert band.excluding_line(50, 0) == 1 and band.excluding_line(450, 0) == 2 and band.excluding_line(250, 0) is None


def test_centroid_uses_pixel_centres():
    assert Roi.centroid(blob(10, 10, 2, 4, 3, 5)) == (3.0, 4.0)              # 2x2 block: centre at (3.0, 4.0)
    assert Roi.centroid(np.zeros((5, 5), bool)) is None
    assert Roi.centroid(blob(10, 10, 0, 1, 0, 1)) == (0.5, 0.5)


def test_centroid_rule_not_any_pixel_rule():
    r = Roi([[100, 0, 100, 10, 0, 5]])                                       # keep x <= 100
    straddling_mostly_inside = blob(50, 200, 70, 110, 10, 20)               # 30 px inside, 10 px outside: centroid x=90
    straddling_mostly_outside = blob(50, 200, 90, 130, 10, 20)              # centroid x=110
    assert r.mask_is_inside(straddling_mostly_inside) is True
    assert r.mask_is_inside(straddling_mostly_outside) is False
    assert r.mask_is_inside(np.zeros((50, 200), bool)) is None


@pytest.mark.parametrize("bad, msg", [
    ([[1, 2, 3]], "needs 4 numbers"),
    ([[1, 2, 3, 4, 5]], "needs 4 numbers"),
    ([[5, 5, 5, 5]], "coincide"),
    ([[0, 0, 10, 10, 5, 5]], "lies on the line"),
    ([[0, 0, float("inf"), 1]], "non-finite"),
    ([[0, 0, float("nan"), 1]], "non-finite"),
    ([], "allowed range"),
    ([[0, 0, 1, 1]] * (MAX_LINES + 1), "allowed range"),
])
def test_invalid_definitions_are_refused_with_a_reason(bad, msg):
    with pytest.raises(RoiError, match=msg):
        Roi(bad)
