"""Region of interest (ROI) for the tracker, defined by straight lines.

A line splits the image in two; one side is "in" (kept), the other "out" (excluded). Several lines can be given and an
object has to be inside ALL of them, i.e. the ROI is the intersection of the "in" half-planes (a convex region).
Lines are infinite: the two points only fix the direction, they are not the ends of a wall.

Two ways to say which side is "in" (coordinates are image pixels, x to the right, y DOWN, as in CVAT):
  [x1, y1, x2, y2]          "in" is on your LEFT when you walk from point 1 to point 2, as seen on screen.
  [x1, y1, x2, y2, x3, y3]  "in" is the side of the third point (drawn as a 3-point polyline: the line, then a tick
                            on the side to keep). Nothing to remember about handedness.

An object is "inside" while the CENTROID of its mask is inside (pixel centres at +0.5; a centroid exactly on a line
counts as inside). What happens to objects that leave is decided by the caller (the service drops them for good).
"""
from __future__ import annotations

import math

import numpy as np

MAX_LINES = 8
_COLLINEAR_EPS = 1e-9


class RoiError(ValueError):
    """The ROI definition is invalid (wrong number of values, zero-length line, third point on the line...)."""


class Roi:
    def __init__(self, lines: list[list[float]]) -> None:
        if not 1 <= len(lines) <= MAX_LINES:
            raise RoiError(f"Got {len(lines)} ROI lines; allowed range is 1..{MAX_LINES}")
        self._planes = [self._normalise(i, ln) for i, ln in enumerate(lines, 1)]

    def __len__(self) -> int:
        return len(self._planes)

    @staticmethod
    def _normalise(i: int, ln: list[float]) -> tuple[float, float, float, float]:
        """-> (ax, ay, dx, dy) oriented so that 'inside' <=> side(P) <= 0 (see `_side`)."""
        if len(ln) not in (4, 6):
            raise RoiError(f"ROI line {i} needs 4 numbers (x1,y1,x2,y2) or 6 (with a third point on the kept side), got {len(ln)}")
        if not all(math.isfinite(v) for v in ln):
            raise RoiError(f"ROI line {i} contains a non-finite number")
        ax, ay, bx, by = ln[:4]
        dx, dy = bx - ax, by - ay
        if math.hypot(dx, dy) < 1e-6:
            raise RoiError(f"ROI line {i}: the two points coincide, so it defines no direction")
        if len(ln) == 6:
            s = dx * (ln[5] - ay) - dy * (ln[4] - ax)
            if abs(s) <= _COLLINEAR_EPS * math.hypot(dx, dy):
                raise RoiError(f"ROI line {i}: the third point lies on the line, so it does not say which side to keep")
            if s > 0:  # third point is on the 'out' side of A->B: walk the line the other way round
                ax, ay, dx, dy = bx, by, -dx, -dy
        return ax, ay, dx, dy

    @staticmethod
    def _side(plane: tuple[float, float, float, float], x: float, y: float) -> float:
        ax, ay, dx, dy = plane
        # y points down, so a NEGATIVE value is the on-screen LEFT of the direction A->B
        return dx * (y - ay) - dy * (x - ax)

    def contains(self, x: float, y: float) -> bool:
        return all(self._side(p, x, y) <= 0 for p in self._planes)

    def excluding_line(self, x: float, y: float) -> int | None:
        """1-based number of the first line that puts (x, y) on its 'out' side, or None if the point is inside."""
        for i, p in enumerate(self._planes, 1):
            if self._side(p, x, y) > 0:
                return i
        return None

    @staticmethod
    def centroid(mask: np.ndarray) -> tuple[float, float] | None:
        """Centroid (x, y) of a boolean HxW mask, pixel centres at +0.5; None for an empty mask."""
        m = np.asarray(mask, dtype=bool)
        cols = m.sum(axis=0, dtype=np.int64)
        n = int(cols.sum())
        if n == 0:
            return None
        rows = m.sum(axis=1, dtype=np.int64)
        cx = float((cols * (np.arange(m.shape[1]) + 0.5)).sum() / n)
        cy = float((rows * (np.arange(m.shape[0]) + 0.5)).sum() / n)
        return cx, cy

    def mask_is_inside(self, mask: np.ndarray) -> bool | None:
        """True / False by the centroid rule; None for an empty mask (nothing to judge)."""
        c = self.centroid(mask)
        return None if c is None else self.contains(*c)
