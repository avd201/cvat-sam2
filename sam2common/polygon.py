"""Mask -> polygon conversion (shared by the service and the CLI).

CVAT polygons are a single closed ring without holes, so only the LARGEST outer contour of the mask is
kept: holes and small disconnected parts are dropped (documented limitation of polygon output)."""
from __future__ import annotations

import numpy as np


def mask_to_polygon(mask: np.ndarray, epsilon: float = 1.5, min_area: float = 16.0) -> list[float] | None:
    """Boolean HxW mask -> flat [x1, y1, x2, y2, ...] floats, or None if no usable contour.
    `epsilon` is the Douglas-Peucker tolerance in pixels (0 = keep every contour point)."""
    import cv2  # imported lazily: only needed when polygon output is requested

    m = np.ascontiguousarray(np.asarray(mask).astype(np.uint8))
    contours, _ = cv2.findContours(m, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    if not contours:
        return None
    best = max(contours, key=cv2.contourArea)
    if cv2.contourArea(best) < min_area:
        return None
    ring = cv2.approxPolyDP(best, epsilon, True) if epsilon > 0 else best
    if len(ring) < 3:
        return None
    return [float(v) for v in ring.reshape(-1).tolist()]


def polygon_area(points: list[float]) -> float:
    """Shoelace area of a flat [x1, y1, ...] polygon."""
    p = np.asarray(points, dtype=float).reshape(-1, 2)
    x, y = p[:, 0], p[:, 1]
    return float(abs(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1))) / 2.0)


def polygon_to_mask(points: list[float], height: int, width: int) -> np.ndarray:
    """Rasterise a flat polygon to a boolean HxW mask."""
    from PIL import Image, ImageDraw

    pairs = [(float(points[i]), float(points[i + 1])) for i in range(0, len(points) - 1, 2)]
    img = Image.new("L", (width, height), 0)
    ImageDraw.Draw(img).polygon(pairs, fill=1, outline=1)
    return np.array(img, dtype=bool)
