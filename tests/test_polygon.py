import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
pytest.importorskip("cv2")

from sam2common.polygon import mask_to_polygon, polygon_area, polygon_to_mask  # noqa: E402


def rect(h=60, w=80, y0=10, y1=40, x0=20, x1=50):
    m = np.zeros((h, w), bool)
    m[y0:y1, x0:x1] = True
    return m


def iou(a, b):
    return (a & b).sum() / (a | b).sum()


def test_rectangle_becomes_four_corners():
    poly = mask_to_polygon(rect())
    assert len(poly) == 8
    xs, ys = poly[0::2], poly[1::2]
    assert (min(xs), max(xs), min(ys), max(ys)) == (20, 49, 10, 39)


def test_roundtrip_overlap_is_high_for_a_blob():
    yy, xx = np.mgrid[0:100, 0:100]
    m = (xx - 50) ** 2 + (yy - 45) ** 2 < 30**2
    assert iou(polygon_to_mask(mask_to_polygon(m, epsilon=1.0), 100, 100), m) > 0.95


def test_largest_component_wins_and_holes_are_ignored():
    m = rect()
    m[20:25, 30:35] = False          # a hole
    m[50:53, 2:5] = True             # a small separate part
    poly = mask_to_polygon(m)
    assert polygon_area(poly) > 800 and max(poly[1::2]) < 45  # outer ring only (~29x29), no speck


def test_empty_and_tiny_masks_give_none():
    assert mask_to_polygon(np.zeros((20, 20), bool)) is None
    m = np.zeros((20, 20), bool)
    m[5:7, 5:7] = True
    assert mask_to_polygon(m) is None


def test_epsilon_zero_keeps_more_points():
    yy, xx = np.mgrid[0:100, 0:100]
    m = (xx - 50) ** 2 + (yy - 50) ** 2 < 30**2
    assert len(mask_to_polygon(m, epsilon=0)) > len(mask_to_polygon(m, epsilon=3.0))


def test_polygon_area_shoelace():
    assert polygon_area([0, 0, 10, 0, 10, 5, 0, 5]) == 50.0
