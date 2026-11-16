#!/usr/bin/env python3
"""Convert a CVAT "CVAT for video 1.1" export (annotations.xml, or the zip that contains it) to Ultralytics YOLO labels.

Why this exists: CVAT's own YOLO exports either drop polygons (YOLO 1.1, Ultralytics Detection) or drop the track
identity (Ultralytics Segmentation). The video XML is lossless, so this reads it directly.

Layout written (mirrors CVAT's own "Ultralytics YOLO Segmentation 1.0" export of CVAT v2.78.0):
    <out>/data.yaml                      names: {0: label, ...} (order of <labels> in the XML), path: ., train: train.txt
    <out>/train.txt                      one image path per frame of the job (images are NOT written, they are not in the XML)
    <out>/labels/train/frame_NNNNNN.txt  only for frames that have at least one object (as CVAT does)
Numbers use 6 decimals, as CVAT does. Lines within a file are ordered by track id (CVAT's order is arbitrary).

Formats:  seg  "label x1 y1 x2 y2 ..."   (polygons; rectangles become 4-corner polygons), all normalised to 0..1
          det  "label cx cy w h"         (bounding box of the polygon / the rectangle), normalised to 0..1
With --track-ids the integer CVAT track id is appended to every line (the convention CVAT's own Ultralytics importer
reads). CAUTION: Ultralytics' training loader treats such lines as corrupt ("labels require 5 columns" / odd polygon
length) and ignores the image, so do NOT train on a labels folder written with --track-ids.

Region of interest: polylines labelled `roi_line` (see the README) are NOT objects: they are left out of the class list
(the other labels are numbered consecutively without them) and written to <out>/roi.json, so the evaluation knows which
part of the image the ground truth covers. With --sidecar every label file gets a line-aligned companion
<out>/tracks/train/frame_NNNNNN.txt holding "track_id occluded" per object: identity and occlusion without touching the
training-safe label files.

What is NOT exported (each is counted and reported on stderr, never dropped silently):
  * frames with outside="1" (the object is absent there), and with --skip-occluded also occluded="1" ones;
  * frames BETWEEN two keyframes: the XML stores keyframes only and CVAT interpolates the rest itself. Tracks written by
    the SAM 2 tool have a keyframe on every frame, so nothing is lost for them; hand-edited tracks may have gaps;
  * mask, polyline, points, ellipse, cuboid, skeleton elements and rotated rectangles. Masks in particular carry no stable
    track id in CVAT (each mask shape is exported as its own one-frame track): use polygon output of the tool for tracking.
"""
from __future__ import annotations

import argparse
import io
import json
import sys
import zipfile
from collections import Counter
from pathlib import Path
from xml.etree import ElementTree as ET

SUPPORTED = ("polygon", "box")
ROI_LABEL = "roi_line"


class ConversionError(Exception):
    pass


def load_xml(path: Path) -> ET.Element:
    try:
        if path.suffix.lower() == ".zip":
            with zipfile.ZipFile(path) as z:
                name = next((n for n in z.namelist() if n.endswith("annotations.xml")), None)
                if name is None:
                    raise ConversionError(f"{path}: no annotations.xml inside the zip")
                return ET.parse(io.BytesIO(z.read(name))).getroot()
        return ET.parse(path).getroot()
    except ET.ParseError as e:
        raise ConversionError(f"{path}: not valid XML ({e})") from e
    except zipfile.BadZipFile as e:
        raise ConversionError(f"{path}: not a valid zip file ({e})") from e
    except OSError as e:
        raise ConversionError(f"cannot read {path}: {e.strerror or e}") from e


def parse_points(text: str) -> list[tuple[float, float]]:
    return [(float(a), float(b)) for a, b in (p.split(",") for p in text.split(";"))]


def fmt(v: float) -> str:
    return f"{v:.6f}"


def convert(root: ET.Element, out: Path, fmt_kind: str = "seg", track_ids: bool = False,
            skip_occluded: bool = False, image_ext: str = "png", sidecar: bool = False) -> Counter:
    """Write the dataset to `out`; returns counters describing what was written and what was skipped."""
    stats: Counter = Counter()
    job = root.find("meta/job")
    size = root.find("meta/original_size")
    if job is None or size is None:
        raise ConversionError("meta/job or meta/original_size missing: is this a 'CVAT for video 1.1' export?")
    width, height = float(size.findtext("width")), float(size.findtext("height"))
    start, stop = int(job.findtext("start_frame", "0")), int(job.findtext("stop_frame", "0"))
    names = [n for n in ((l.findtext("name") or "").strip() for l in job.iter("label")) if n != ROI_LABEL]
    if not names:
        raise ConversionError("no <labels> in the XML (other than the region-of-interest label)")
    class_id = {n: i for i, n in enumerate(names)}
    all_tracks = root.findall("track")
    tracks = [t for t in all_tracks if t.get("label") != ROI_LABEL]
    roi_lines = _roi_lines(all_tracks, stats)
    if not tracks:
        raise ConversionError("no <track> elements: this converter reads 'CVAT for video 1.1' exports (tracks)")

    per_frame: dict[int, list[tuple[int, str, int]]] = {}   # frame -> [(track id, line, occluded)]
    for tr in tracks:
        label, tid = tr.get("label"), int(tr.get("id"))
        if label not in class_id:
            raise ConversionError(f"track {tid} has label {label!r} which is not declared in <labels>")
        elements = sorted(tr, key=lambda e: int(e.get("frame", -1)) if e.get("frame") is not None else -1)
        prev = None
        for el in elements:
            if el.get("frame") is None:
                continue
            frame, outside = int(el.get("frame")), el.get("outside") == "1"
            # keyframes missing between the previous visible keyframe and this one
            if prev is not None and not prev[1] and frame - prev[0] > 1:
                stats["gap_frames_not_exported"] += frame - prev[0] - 1
            prev = (frame, outside)
            if outside:
                stats["outside_frames_skipped"] += 1
                continue
            if el.tag not in SUPPORTED:
                stats[f"skipped_{el.tag}"] += 1
                continue
            if skip_occluded and el.get("occluded") == "1":
                stats["occluded_skipped"] += 1
                continue
            if el.tag == "box":
                if float(el.get("rotation", "0") or 0) != 0:
                    stats["skipped_rotated_box"] += 1
                    continue
                x0, y0, x1, y1 = (float(el.get(k)) for k in ("xtl", "ytl", "xbr", "ybr"))
                pts = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
            else:
                pts = parse_points(el.get("points"))
                if len(pts) < 3:
                    stats["skipped_degenerate"] += 1
                    continue
            pts = [(min(max(x, 0.0), width), min(max(y, 0.0), height)) for x, y in pts]
            if fmt_kind == "seg":
                nums = [fmt(v) for x, y in pts for v in (x / width, y / height)]
            else:
                xs, ys = [p[0] for p in pts], [p[1] for p in pts]
                x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
                nums = [fmt((x0 + x1) / 2 / width), fmt((y0 + y1) / 2 / height), fmt((x1 - x0) / width), fmt((y1 - y0) / height)]
            line = " ".join([str(class_id[label]), *nums] + ([str(tid)] if track_ids else []))
            per_frame.setdefault(frame, []).append((tid, line, int(el.get("occluded") == "1")))
            stats["objects_written"] += 1

    labels_dir = out / "labels" / "train"
    labels_dir.mkdir(parents=True, exist_ok=True)
    side_dir = out / "tracks" / "train"
    if sidecar:
        side_dir.mkdir(parents=True, exist_ok=True)
    for frame, items in per_frame.items():
        items.sort(key=lambda it: it[0])
        (labels_dir / f"frame_{frame:06d}.txt").write_text("\n".join(l for _, l, _ in items) + "\n")
        if sidecar:   # same order as the label file: line i of this file describes line i of the label file
            (side_dir / f"frame_{frame:06d}.txt").write_text("".join(f"{t} {o}\n" for t, _, o in items))
    stats["label_files"] = len(per_frame)
    if roi_lines:
        (out / "roi.json").write_text(json.dumps({
            "width": int(width), "height": int(height),
            "rule": "an object belongs to the region while its mask centroid is inside every line's kept side",
            "lines": roi_lines,
            "line_format": "[x1,y1,x2,y2] keeps the on-screen LEFT of 1->2; [x1,y1,x2,y2,x3,y3] keeps the side of point 3 "
                           "(image pixels, y down); lines are infinite; see sam2common/roi.py",
        }, indent=2) + "\n")
    stats["roi_lines"] = len(roi_lines)
    stats["tracks"] = len(tracks)
    (out / "data.yaml").write_text("names:\n" + "".join(f"  {i}: {n}\n" for i, n in enumerate(names))
                                   + "path: .\ntrain: train.txt\n")
    (out / "train.txt").write_text("".join(f"data/images/train/frame_{f:06d}.{image_ext}\n" for f in range(start, stop + 1)))
    stats["frames_in_job"] = stop - start + 1
    return stats


def _roi_lines(tracks: list[ET.Element], stats: Counter) -> list[list[float]]:
    """Region-of-interest lines: polyline tracks labelled `roi_line`, taken from their first visible keyframe.
    The camera is assumed fixed, so a line that moves between keyframes is reported, and its first position is used."""
    lines: list[list[float]] = []
    for tr in tracks:
        if tr.get("label") != ROI_LABEL:
            continue
        visible = [e for e in tr if e.tag == "polyline" and e.get("outside") != "1"]
        if not visible:
            stats["roi_line_tracks_without_a_visible_polyline"] += 1
            continue
        visible.sort(key=lambda e: int(e.get("frame")))
        pts = [v for p in parse_points(visible[0].get("points")) for v in p]
        if len(pts) not in (4, 6):
            raise ConversionError(f"roi_line track {tr.get('id')} has {len(pts) // 2} points; it needs 2 or 3")
        if any(e.get("points") != visible[0].get("points") for e in visible[1:]):
            stats["roi_line_moves_between_keyframes"] += 1
        lines.append(pts)
    return lines


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input", type=Path, help="annotations.xml, or the .zip exported as 'CVAT for video 1.1'")
    ap.add_argument("-o", "--output", type=Path, required=True, help="output directory (created; existing label files are overwritten)")
    ap.add_argument("--format", choices=("seg", "det"), default="seg", dest="kind", help="seg = polygons (default), det = bounding boxes")
    ap.add_argument("--track-ids", action="store_true", help="append the CVAT track id to every line (NOT trainable with Ultralytics, see above)")
    ap.add_argument("--sidecar", action="store_true", help="also write tracks/train/frame_N.txt (\"track_id occluded\" per label line)")
    ap.add_argument("--skip-occluded", action="store_true", help="also skip frames flagged occluded (default: keep them)")
    ap.add_argument("--image-ext", default="png", help="extension used for the image paths in train.txt (default png, as in CVAT's export)")
    a = ap.parse_args(argv)
    try:
        stats = convert(load_xml(a.input), a.output, a.kind, a.track_ids, a.skip_occluded, a.image_ext.lstrip("."), a.sidecar)
    except ConversionError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(f"Wrote {stats['objects_written']} objects from {stats['tracks']} tracks into {stats['label_files']} label files "
          f"({stats['frames_in_job']} frames in the job) in {a.output}"
          + (f"; region of interest: {stats['roi_lines']} line(s) in roi.json" if stats["roi_lines"] else ""))
    for key in sorted(k for k in stats if k not in ("objects_written", "tracks", "label_files", "frames_in_job", "roi_lines")):
        print(f"WARNING/INFO {key}: {stats[key]}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
