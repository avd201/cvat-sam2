"""CVAT mask encoding.

CVAT mask shape `points` = RLE counts of the mask cropped to its tight bounding box
(row-major, FIRST count is always a run of zeros), followed by [xtl, ytl, xbr, ybr]
(inclusive pixel coordinates of that box in the full image).
Reference: cvat-core/src/rle-utils.ts (mask2Rle) and cvat-core/src/lambda-manager.ts.
"""
from __future__ import annotations

import numpy as np


def mask_to_rle(flat: np.ndarray) -> list[int]:
    """Encode a flat binary array; always starts with the length of the initial zero run."""
    flat = np.asarray(flat).astype(bool).ravel()
    if flat.size == 0:
        return [0]
    change = np.flatnonzero(flat[1:] != flat[:-1]) + 1
    bounds = np.concatenate(([0], change, [flat.size]))
    runs = np.diff(bounds).tolist()
    if flat[0]:
        runs.insert(0, 0)
    return runs


def rle_to_mask(rle: list[int], size: int) -> np.ndarray:
    out = np.zeros(size, dtype=bool)
    pos, val = 0, False
    for run in rle:
        out[pos : pos + run] = val
        pos += run
        val = not val
    return out


def mask_to_cvat_points(mask: np.ndarray) -> list[int] | None:
    """Full-image boolean mask -> CVAT mask `points`. Returns None for an empty mask."""
    ys, xs = np.nonzero(mask)
    if ys.size == 0:
        return None
    xtl, xbr = int(xs.min()), int(xs.max())
    ytl, ybr = int(ys.min()), int(ys.max())
    crop = mask[ytl : ybr + 1, xtl : xbr + 1]
    return mask_to_rle(crop.ravel()) + [xtl, ytl, xbr, ybr]


def normalize_points(points) -> list[int]:
    """CVAT's REST API / SDK return mask `points` as floats (e.g. 3.0). Convert to ints, and fail
    loudly if a value is not integral, since RLE counts and bbox corners must be whole pixels."""
    out = []
    for v in points:
        iv = int(round(v))
        if abs(v - iv) > 1e-6:
            raise ValueError(f"mask point {v!r} is not an integer")
        out.append(iv)
    return out


def cvat_points_to_mask(points, height: int, width: int) -> np.ndarray:
    """Inverse of mask_to_cvat_points (used by tests and later by the video stages)."""
    *rle, xtl, ytl, xbr, ybr = normalize_points(points)
    h, w = ybr - ytl + 1, xbr - xtl + 1
    crop = rle_to_mask(rle, h * w).reshape(h, w)
    full = np.zeros((height, width), dtype=bool)
    full[ytl : ybr + 1, xtl : xbr + 1] = crop
    return full


def points_area(points: list[int]) -> int:
    """Number of foreground pixels in CVAT mask `points` (sum of the odd-indexed RLE runs)."""
    return int(sum(normalize_points(points)[:-4][1::2]))
