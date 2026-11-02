#!/usr/bin/env python3
"""cvat_sam2 - companion CLI for the local SAM 2 service (Stage 2: video propagation).

Workflow: create a mask on frame N in CVAT (SAM 2 interactor), Save, then run `propagate`.
CVAT stays the source of truth: the CLI reads the mask from CVAT, asks the service to track it
forward through frames N+1..M, and writes one MASK SHAPE per frame, all sharing one `group` id.

Why shapes + group instead of a track: the CVAT Community frontend cannot load mask *tracks*
("An unexpected type of track mask"; cvat-core has no MaskTrack), but it does handle mask shapes.
The group id is the object identity ("Color by: Group" in CVAT's Appearance panel shows it).

`--output polygon-track` writes a real CVAT polygon TRACK instead (one keyframe per frame, `outside`
where the object is lost), which the Community UI supports natively. Polygons are approximate and
hole-free: only the largest outer contour of each mask is kept.

Authentication is taken from the environment only (never from command-line arguments):
  CVAT_ACCESS_TOKEN                      a personal access token, or
  CVAT_USERNAME and CVAT_PASSWORD

Exit codes: 0 ok, 1 usage / CVAT-side error, 2 SAM 2 service error.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # makes `sam2common` importable

from sam2common.polygon import mask_to_polygon, polygon_area, polygon_to_mask  # noqa: E402
from sam2common.rle import cvat_points_to_mask, mask_to_cvat_points, normalize_points, points_area  # noqa: E402

DEFAULT_HOST = os.environ.get("CVAT_HOST_URL", "http://localhost:8080")
DEFAULT_SERVICE = os.environ.get("SAM2_SERVICE_URL", "http://127.0.0.1:8000")
SHAPE_KEYS = ("type", "occluded", "outside", "z_order", "rotation", "points", "frame", "attributes")


class CliError(Exception):
    """Problem on the CVAT / user side (exit code 1)."""


class ServiceError(Exception):
    """Problem reported by, or while talking to, the SAM 2 service (exit code 2)."""


# --------------------------------------------------------------------------- pure logic (unit-tested)
def shape_area(shape: dict) -> float:
    return float(points_area(shape["points"])) if shape["type"] == "mask" else polygon_area(shape["points"])


def list_objects(data: dict) -> list[dict]:
    """Mask/polygon shapes, polygon tracks, and mask tracks (flagged: the Community UI cannot open them)."""
    rows = [{"kind": "shape", "type": x["type"], "id": x["id"], "label_id": x["label_id"], "frame": x["frame"],
             "group": x.get("group", 0)} for x in data.get("shapes") or [] if x["type"] in ("mask", "polygon")]
    for t in data.get("tracks") or []:
        types = {x["type"] for x in t["shapes"]}
        if types and types <= {"mask", "polygon"}:
            rows.append({"kind": "track", "type": next(iter(types)) if len(types) == 1 else "mixed", "id": t["id"],
                         "label_id": t["label_id"], "frame": min(x["frame"] for x in t["shapes"]),
                         "group": t.get("group", 0)})
    return sorted(rows, key=lambda r: (r["kind"] != "shape", r["group"], r["frame"], r["id"]))


def next_group(data: dict) -> int:
    groups = [x.get("group", 0) for x in (data.get("shapes") or []) + (data.get("tracks") or [])]
    return max(groups + [0]) + 1


def find_source(data: dict, *, shape_id: int | None, group: int | None, track_id: int | None = None,
                from_frame: int | None) -> dict:
    """Locate the user's prompt. Returns {"shape", "from_frame", "group", "track"} (track only for
    --track-id; group may be 0). Sources: a mask or polygon shape (--shape-id), a mask of a group
    (--group + --from-frame), or a polygon track keyframe (--track-id + --from-frame)."""
    if sum(x is not None for x in (shape_id, group, track_id)) != 1:
        raise CliError("Give exactly one of --shape-id / --group / --track-id")
    track = None
    if shape_id is not None:
        shape = next((x for x in data.get("shapes") or [] if x["id"] == shape_id), None)
        if shape is None:
            raise CliError(f"No shape with id {shape_id} in this job (run `list`; did you Save in CVAT?)")
        if shape["type"] not in ("mask", "polygon"):
            raise CliError(f"Shape {shape_id} is a '{shape['type']}'; need a mask or polygon")
        if from_frame is not None and from_frame != shape["frame"]:
            raise CliError(f"Shape {shape_id} is on frame {shape['frame']}, not {from_frame}")
    elif track_id is not None:
        track = next((t for t in data.get("tracks") or [] if t["id"] == track_id), None)
        if track is None:
            raise CliError(f"No track with id {track_id} in this job (run `list`; did you Save in CVAT?)")
        if from_frame is None:
            raise CliError("--from-frame is required with --track-id")
        shape = next((x for x in track["shapes"] if x["frame"] == from_frame), None)
        if shape is None:
            have = sorted(x["frame"] for x in track["shapes"])
            raise CliError(f"Track {track_id} has no shape on frame {from_frame}; it has shapes on "
                           f"{have[:10]}{'...' if len(have) > 10 else ''}")
        if shape["type"] != "polygon":
            raise CliError(f"Track {track_id} holds '{shape['type']}' shapes; only polygon tracks can be "
                           f"re-propagated (the Community UI cannot open mask tracks)")
        if shape.get("outside"):
            raise CliError(f"Track {track_id} is marked 'outside' on frame {from_frame}; pick a visible keyframe")
    else:
        if group == 0:
            raise CliError("Group 0 means 'no group'; use --shape-id for an ungrouped mask")
        if from_frame is None:
            raise CliError("--from-frame is required with --group")
        cands = [x for x in data.get("shapes") or [] if x["type"] == "mask"
                 and x.get("group", 0) == group and x["frame"] == from_frame]
        if len(cands) != 1:
            have = sorted(x["frame"] for x in data.get("shapes") or []
                          if x["type"] == "mask" and x.get("group", 0) == group)
            raise CliError(f"Expected exactly one mask of group {group} on frame {from_frame}, found "
                           f"{len(cands)}; group {group} has masks on frames "
                           f"{have[:10]}{'...' if len(have) > 10 else ''}")
        shape = cands[0]
    return {"shape": shape, "from_frame": shape["frame"], "track": track,
            "group": track.get("group", 0) if track else shape.get("group", 0)}


def prompt_mask_points(shape: dict, height: int, width: int) -> list[int]:
    """The prompt as CVAT mask points, whether the user's source is a mask or a polygon."""
    if shape["type"] == "mask":
        return normalize_points(shape["points"])
    pts = mask_to_cvat_points(polygon_to_mask(shape["points"], height, width))
    if pts is None:
        raise CliError("The source polygon is empty at this frame size")
    return pts


def _new_shape(template: dict, frame: int, points: list[int], group: int) -> dict:
    return {"type": "mask", "frame": frame, "label_id": template["label_id"], "group": group,
            "source": template.get("source", "manual"), "occluded": False, "outside": False,
            "z_order": template.get("z_order", 0), "rotation": 0.0, "points": points,
            "attributes": [dict(a) for a in template.get("attributes") or []]}


def plan_shapes(data: dict, src: dict, results: dict[int, list[int] | None], to_frame: int,
                group: int) -> tuple[list[dict], list[dict]]:
    """(to_create, to_delete). New mask shapes for every frame in (from_frame, to_frame] where the
    object was found; old mask shapes of the same group in that range are to be replaced.
    Frames where the object was not found simply get no shape. Frames <= from_frame and > to_frame
    are never touched."""
    from_frame = src["from_frame"]
    create = []
    for f in range(from_frame + 1, to_frame + 1):
        if f not in results:
            raise CliError(f"Service returned no result for frame {f}")
        if results[f] is not None:
            create.append(_new_shape(src["shape"], f, results[f], group))
    delete = [x for x in data.get("shapes") or []
              if group and x["type"] == "mask" and x.get("group", 0) == group
              and from_frame < x["frame"] <= to_frame]
    return create, delete


def track_to_shapes(track: dict, *, all_frames: bool, group: int) -> list[dict]:
    """Convert a mask track (unloadable in the Community UI) to mask shapes. Shapes marked
    `outside` carry no mask and are dropped. Without all_frames only the first visible shape is kept."""
    vis = sorted((x for x in track["shapes"] if not x["outside"] and x["type"] == "mask"),
                 key=lambda x: x["frame"])
    if not vis:
        raise CliError(f"Track {track['id']} has no visible mask shape")
    keep = vis if all_frames else vis[:1]
    tmpl = {"label_id": track["label_id"], "source": track.get("source", "manual")}
    return [_new_shape({**tmpl, "z_order": x.get("z_order", 0), "attributes": x.get("attributes")},
                       x["frame"], x["points"], group) for x in keep]


def _tracked_polygon(template: dict, frame: int, points: list[float], outside: bool) -> dict:
    return {"type": "polygon", "occluded": False, "outside": outside, "z_order": template.get("z_order", 0),
            "rotation": 0.0, "points": [float(v) for v in points], "frame": frame,
            "attributes": [dict(a) for a in template.get("attributes") or []]}


def plan_polygon_track(src: dict, first_points: list[float], results: dict[int, list[float] | None],
                       to_frame: int, stop_frame: int) -> tuple[list[dict], int]:
    """Full shape list for the polygon track + number of existing shapes replaced.
    Frames <= from_frame and > to_frame are kept; frames in (from_frame, to_frame] come from `results`
    (None = object lost -> an `outside` keyframe repeating the last known polygon, as CVAT needs points).

    A CVAT track keeps showing its last keyframe until the end of the job unless an `outside` keyframe
    ends it, so the propagated range is closed with an `outside` keyframe on to_frame+1 -- unless that
    frame is beyond the job (`stop_frame`), already holds a kept keyframe, or the range already ends
    with an `outside` keyframe."""
    from_frame = src["from_frame"]
    if src["track"] is not None:
        existing = [{k: x[k] for k in SHAPE_KEYS if k in x} for x in src["track"]["shapes"]]
    else:
        existing = [_tracked_polygon(src["shape"], from_frame, first_points, False)]
    before = [x for x in existing if x["frame"] <= from_frame]
    after = [x for x in existing if x["frame"] > to_frame]
    replaced = sum(1 for x in existing if from_frame < x["frame"] <= to_frame)
    new, last = [], first_points
    for f in range(from_frame + 1, to_frame + 1):
        if f not in results:
            raise CliError(f"Service returned no result for frame {f}")
        if results[f] is None:
            new.append(_tracked_polygon(src["shape"], f, last, True))
        else:
            last = results[f]
            new.append(_tracked_polygon(src["shape"], f, last, False))
    range_shapes = before + new
    ends_outside = bool(range_shapes) and range_shapes[-1]["outside"]
    if to_frame + 1 <= stop_frame and not ends_outside and not any(x["frame"] == to_frame + 1 for x in after):
        new.append(_tracked_polygon(src["shape"], to_frame + 1, last, True))
    return sorted(before + new + after, key=lambda x: x["frame"]), replaced


def suspect_frames(shapes: list[dict], top: int = 8) -> list[tuple[int, float, int, int]]:
    """Frames where the mask area changes most vs the previous visible frame (candidates to
    review). Returns (frame, relative_change, area_before, area_now), largest change first."""
    vis = [(s["frame"], shape_area(s)) for s in sorted(shapes, key=lambda s: s["frame"])
           if s["type"] in ("mask", "polygon") and not s.get("outside")]
    out = []
    for (_, a0), (f1, a1) in zip(vis, vis[1:]):
        out.append((f1, abs(a1 - a0) / max(a0, 1), int(a0), int(a1)))
    return sorted(out, key=lambda r: -r[1])[:top]


# --------------------------------------------------------------------------- CVAT / service plumbing
def connect(host: str):
    try:
        from cvat_sdk import make_client
    except ImportError:
        raise CliError("cvat-sdk is not installed; run: pip install -r cli/requirements.txt")

    token = os.environ.get("CVAT_ACCESS_TOKEN")
    user, pw = os.environ.get("CVAT_USERNAME"), os.environ.get("CVAT_PASSWORD")
    try:
        if token:
            return make_client(host, access_token=token)
        if user and pw:
            return make_client(host, credentials=(user, pw))
    except Exception as e:  # noqa: BLE001
        raise CliError(f"Could not log in to CVAT at {host}: {e}")
    raise CliError("Set CVAT_ACCESS_TOKEN, or CVAT_USERNAME and CVAT_PASSWORD, in the environment")


def to_jpeg(raw: bytes) -> tuple[bytes, tuple[int, int]]:
    from PIL import Image

    img = Image.open(io.BytesIO(raw)).convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=95)
    return buf.getvalue(), img.size  # (width, height)


def service_health(url: str) -> dict:
    import requests

    try:
        r = requests.get(url.rstrip("/") + "/healthz", timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as e:  # noqa: BLE001
        raise ServiceError(f"SAM 2 service not reachable at {url}: {e}")


OUTDATED_HINT = ("The running SAM 2 service is older than this CLI (it ignores the requested output format). "
                 "Rebuild it: cd <repo> && sudo docker compose -f docker-compose.sam2.yml up -d --build --force-recreate")


def check_output_echo(done_msg: dict, requested: str) -> None:
    """The service echoes the format it really produced; a service without polygon support ignores the
    request silently and would return mask data (which must never be written as polygons)."""
    got = done_msg.get("output", "mask")
    if got != requested:
        raise ServiceError(f"Asked for '{requested}' output but the service produced '{got}'. {OUTDATED_HINT}")


def validate_for_cvat(shapes: list[dict]) -> None:
    """Mirror of CVAT's server-side rules (cvat/apps/engine/serializers.py, ShapeSerializer.validate) so that bad
    data is refused here, with a clear message, before anything is written."""
    bad = []
    for x in shapes:
        n = len(x["points"])
        ok = {"polygon": n >= 6 and n % 2 == 0, "mask": n >= 5}.get(x["type"], True)
        if not ok:
            bad.append((x["frame"], n))
    if bad:
        raise CliError(f"Refusing to write {len(bad)} {shapes[0]['type']} shapes with an invalid number of points "
                       f"(frame, points): {bad[:5]}")


def run_propagation(url: str, jpegs: list[bytes], meta: dict, read_timeout: float) -> dict[int, list[int] | None]:
    """POST the frames, consume the NDJSON stream. Returns {relative_index: points|None}.
    Raises ServiceError (and returns nothing) if the stream does not end with {"done": true}."""
    import requests

    files = [("frames", (f"{i:05d}.jpg", b, "image/jpeg")) for i, b in enumerate(jpegs)]
    try:
        r = requests.post(url.rstrip("/") + "/v1/video/propagate", data={"meta": json.dumps(meta)},
                          files=files, stream=True, timeout=(10, read_timeout))
    except Exception as e:  # noqa: BLE001
        raise ServiceError(f"Request to the SAM 2 service failed: {e}")
    if r.status_code != 200:
        try:
            detail = r.json().get("detail", r.text)
        except Exception:  # noqa: BLE001
            detail = r.text[:300]
        raise ServiceError(f"Service answered HTTP {r.status_code}: {detail}")
    results: dict[int, list[int] | None] = {}
    done_msg = None
    try:
        for line in r.iter_lines():
            if not line:
                continue
            msg = json.loads(line)
            if "error" in msg:
                raise ServiceError(f"Service error after {len(results)} frames: {msg['error']}")
            if "frame" in msg:
                results[msg["frame"]] = msg["points"]
                print(f"\r  propagated {len(results)}/{len(jpegs)} frames", end="", file=sys.stderr, flush=True)
            if msg.get("done"):
                done_msg = msg
    except ServiceError:
        raise
    except Exception as e:  # noqa: BLE001  (connection dropped, timeout, bad JSON)
        raise ServiceError(f"Stream interrupted after {len(results)} frames: {e}")
    finally:
        print(file=sys.stderr)
    if done_msg is None:
        raise ServiceError(f"Stream ended without completion after {len(results)} frames")
    check_output_echo(done_msg, meta.get("output", "mask"))
    return results


# --------------------------------------------------------------------------- commands
def _job(client, job_id: int):
    try:
        return client.jobs.retrieve(job_id)
    except Exception as e:  # noqa: BLE001
        raise CliError(f"Cannot open job {job_id}: {e}")


def _create_shapes(job, shapes: list[dict]) -> None:
    from cvat_sdk import models
    from cvat_sdk.core.proxies.annotations import AnnotationUpdateAction

    req = models.PatchedLabeledDataRequest(shapes=[models.LabeledShapeRequest(**x) for x in shapes])
    job.update_annotations(req, action=AnnotationUpdateAction.CREATE)


def _delete_shapes(job, shapes: list[dict]) -> None:
    """Delete exactly these shapes. (Not `job.remove_annotations(ids=...)`: it applies one id list to
    tags, tracks AND shapes, and ids of different kinds can coincide.)"""
    from cvat_sdk import models
    from cvat_sdk.core.proxies.annotations import AnnotationUpdateAction

    req = models.PatchedLabeledDataRequest(shapes=[models.LabeledShapeRequest(**x) for x in shapes])
    job.update_annotations(req, action=AnnotationUpdateAction.DELETE)


def _delete_track(job, track: dict) -> None:
    from cvat_sdk import models
    from cvat_sdk.core.proxies.annotations import AnnotationUpdateAction

    req = models.PatchedLabeledDataRequest(tracks=[models.LabeledTrackRequest(**track)])
    job.update_annotations(req, action=AnnotationUpdateAction.DELETE)


def _set_group(job, shape: dict, group: int) -> None:
    from cvat_sdk import models
    from cvat_sdk.core.proxies.annotations import AnnotationUpdateAction

    req = models.PatchedLabeledDataRequest(shapes=[models.LabeledShapeRequest(**{**shape, "group": group})])
    job.update_annotations(req, action=AnnotationUpdateAction.UPDATE)  # update keeps the shape id


def cmd_list(args) -> int:
    with connect(args.host) as client:
        job = _job(client, args.job)
        names = {lb.id: lb.name for lb in job.get_labels()}
        rows = list_objects(job.get_annotations().to_dict())
        print(f"job {job.id}: frames {job.start_frame}..{job.stop_frame}")
        if not rows:
            print("no mask shapes found (did you Save in CVAT?)")
        for r in rows:
            label = names.get(r["label_id"], r["label_id"])
            note = ("   <-- mask TRACK: the Community UI cannot open it; run track-to-shapes"
                    if r["kind"] == "track" and r["type"] != "polygon" else "")
            print(f"  {r['kind']:5} {r['type']:8} id={r['id']:<6} label={label!s:<14} "
                  f"frame={r['frame']:<6} group={r['group']}{note}")
    return 0


def cmd_show(args) -> int:
    if (args.group is None) == (args.track_id is None):
        raise CliError("Give exactly one of --group / --track-id")
    with connect(args.host) as client:
        job = _job(client, args.job)
        data = job.get_annotations().to_dict()
    if args.track_id is not None:
        track = next((t for t in data.get("tracks") or [] if t["id"] == args.track_id), None)
        if track is None:
            raise CliError(f"No track with id {args.track_id} in job {args.job}")
        shapes, title = sorted(track["shapes"], key=lambda x: x["frame"]), f"track {args.track_id}"
    else:
        shapes = sorted((x for x in data.get("shapes") or [] if x["type"] == "mask" and x.get("group", 0) == args.group),
                        key=lambda x: x["frame"])
        title = f"group {args.group}"
    if not shapes:
        raise CliError(f"No shapes found for {title} in job {args.job}")
    vis = [x for x in shapes if not x.get("outside")]
    areas = [shape_area(x) for x in vis]
    print(f"{title}: {len(shapes)} shapes ({len(shapes) - len(vis)} marked outside), "
          f"frames {shapes[0]['frame']}..{shapes[-1]['frame']}")
    if areas:
        print(f"area px: min={int(min(areas))} median={int(statistics.median(areas))} max={int(max(areas))}")
    gaps = [(a["frame"], b["frame"]) for a, b in zip(shapes, shapes[1:]) if b["frame"] - a["frame"] > 1]
    print(f"frames missing between consecutive shapes: {gaps[:8] if gaps else 'none'}")
    print("largest frame-to-frame area changes (review candidates):")
    for f, ch, a0, a1 in suspect_frames(shapes):
        print(f"  frame {f:<6} {a0:>8} -> {a1:<8} ({ch:.0%} of previous)")
    if args.all:
        for x in shapes:
            print(f"  frame {x['frame']:<6} outside={bool(x.get('outside'))!s:<5} area={int(shape_area(x))}")
    return 0


def cmd_propagate(args) -> int:
    t0 = time.time()
    health = service_health(args.service_url)
    with connect(args.host) as client:
        job = _job(client, args.job)
        data = job.get_annotations().to_dict()
        src = find_source(data, shape_id=args.shape_id, group=args.group, track_id=args.track_id,
                          from_frame=args.from_frame)
        poly = args.output == "polygon-track"
        if poly and "polygon" not in health.get("outputs", []):
            raise ServiceError(f"The SAM 2 service does not support polygon output. {OUTDATED_HINT}")
        if args.group is not None and poly:
            raise CliError("--group re-propagates grouped MASK shapes; for --output polygon-track use --track-id")
        if args.track_id is not None and not poly:
            raise CliError("--track-id needs --output polygon-track (the Community UI cannot open mask tracks)")
        from_frame = src["from_frame"]
        to_frame = args.to_frame if args.to_frame is not None else job.stop_frame
        if not (job.start_frame <= from_frame < to_frame <= job.stop_frame):
            raise CliError(f"Need job.start <= from-frame < to-frame <= job.stop; got {from_frame}..{to_frame} "
                           f"in a job covering {job.start_frame}..{job.stop_frame}")
        n = to_frame - from_frame + 1
        if n > health["max_propagation_frames"]:
            raise CliError(f"{n} frames exceed the service limit of {health['max_propagation_frames']}; "
                           f"use a smaller --to-frame and run again from the last frame")
        new_group = src["group"] == 0 and not poly
        group = next_group(data) if new_group else src["group"]

        print(f"downloading frames {from_frame}..{to_frame} ({n} frames)", file=sys.stderr)
        jpegs, size = [], None
        for i, f in enumerate(range(from_frame, to_frame + 1)):
            try:
                raw = job.get_frame(f, quality="original").read()
                jpg, sz = to_jpeg(raw)
            except Exception as e:  # noqa: BLE001
                raise CliError(f"Could not fetch frame {f}: {e}")
            if size is None:
                size = sz
            elif sz != size:
                raise CliError(f"Frame {f} has size {sz}, expected {size}; frames must share one size")
            jpegs.append(jpg)
            if (i + 1) % 25 == 0 or i + 1 == n:
                print(f"\r  downloaded {i + 1}/{n}", end="", file=sys.stderr, flush=True)
        print(file=sys.stderr)

        width, height = size
        try:
            src_points = prompt_mask_points(src["shape"], height, width)  # API points are floats; polygon -> mask
            cvat_points_to_mask(src_points, height, width)
        except CliError:
            raise
        except Exception as e:  # noqa: BLE001
            raise CliError(f"The source cannot be used with {width}x{height} frames "
                           f"(points tail {src['shape']['points'][-4:]}): {type(e).__name__}: {e}")
        meta = {"prompt_index": 0, "height": height, "width": width, "prompt_points": src_points,
                "output": "polygon" if poly else "mask", "simplify_px": args.simplify_px}
        rel = run_propagation(args.service_url, jpegs, meta, args.service_timeout)
        results = {from_frame + i: pts for i, pts in rel.items() if i > 0}  # index 0 = the user's own prompt
        missing = sorted(f for f, p in results.items() if p is None)
        if poly:
            return _finish_polygon_track(args, job, src, results, missing, to_frame, src_points, height, width, t0)
        create, delete = plan_shapes(data, src, results, to_frame, group)
        validate_for_cvat(create)
        print(f"service done: object found on {len(create)} of {len(results)} frames"
              + (f"; not found on {missing[:6]}{'...' if len(missing) > 6 else ''}" if missing else ""))
        print(f"plan: group {group}{' (new)' if new_group else ''}: create {len(create)} mask shapes, "
              f"replace {len(delete)} existing ones on frames {from_frame + 1}..{to_frame}")
        if args.dry_run:
            print("--dry-run: nothing written to CVAT")
            return 0
        if delete and not args.yes:
            if not sys.stdin.isatty():
                raise CliError(f"{len(delete)} existing shapes would be replaced; pass --yes to confirm")
            if input("Replace them? [y/N] ").strip().lower() != "y":
                print("aborted, nothing changed")
                return 1

        # Order matters for safety: create the new shapes first (nothing is lost if this fails), then
        # delete the replaced ones, then give the source mask its group.
        _create_shapes(job, create)
        if delete:
            _delete_shapes(job, delete)
        if new_group:
            _set_group(job, src["shape"], group)
    print(f"finished in {time.time() - t0:.0f}s: group {group} now has masks on frame {from_frame} (yours) "
          f"and {len(create)} propagated frames.\nReload the job page in CVAT (F5); "
          f"Appearance -> Color by Group shows the object. Later runs: --group {group} --from-frame <K>")
    return 0


def _finish_polygon_track(args, job, src, results, missing, to_frame, src_points, height, width, t0) -> int:
    if src["shape"]["type"] == "polygon":
        first = [float(v) for v in src["shape"]["points"]]
    else:  # mask prompt: its own keyframe needs a polygon too
        first = mask_to_polygon(cvat_points_to_mask(src_points, height, width), args.simplify_px)
        if first is None:
            raise CliError("The source mask has no usable contour to build a polygon from")
    shapes, replaced = plan_polygon_track(src, first, results, to_frame, job.stop_frame)
    validate_for_cvat(shapes)
    lost = len(missing)
    print(f"service done: object found on {len(results) - lost} of {len(results)} frames"
          + (f"; lost on {missing[:6]}{'...' if lost > 6 else ''} (written as 'outside' keyframes)" if lost else ""))
    print(f"plan: polygon track with {len(shapes)} keyframes (ends with an 'outside' keyframe after frame {to_frame}); "
          f"replaces {replaced} existing keyframes on frames "
          f"{src['from_frame'] + 1}..{to_frame}" + ("" if src["track"] else "; the source shape becomes the first keyframe"))
    if args.dry_run:
        print("--dry-run: nothing written to CVAT")
        return 0
    if replaced and not args.yes:
        if not sys.stdin.isatty():
            raise CliError(f"{replaced} existing keyframes would be replaced; pass --yes to confirm")
        if input("Replace them? [y/N] ").strip().lower() != "y":
            print("aborted, nothing changed")
            return 1
    from cvat_sdk import models
    from cvat_sdk.core.proxies.annotations import AnnotationUpdateAction

    tracked = [models.TrackedShapeRequest(**x) for x in shapes]
    if src["track"] is not None:
        t = src["track"]
        req = models.LabeledTrackRequest(id=t["id"], frame=shapes[0]["frame"], label_id=t["label_id"],
                                         group=t.get("group", 0), source=t.get("source", "manual"),
                                         shapes=tracked, attributes=t.get("attributes") or [])
        # PATCH action=update = delete the track by id, then re-create it with the same id.
        job.update_annotations(models.PatchedLabeledDataRequest(tracks=[req]), action=AnnotationUpdateAction.UPDATE)
        print(f"updated polygon track {t['id']}")
    else:
        sh = src["shape"]
        before = {t["id"] for t in job.get_annotations().to_dict().get("tracks") or []}
        req = models.LabeledTrackRequest(frame=shapes[0]["frame"], label_id=sh["label_id"], group=sh.get("group", 0),
                                         source=sh.get("source", "manual"), shapes=tracked, attributes=[])
        # Create the track first, delete the source shape afterwards: a failure in between leaves a
        # duplicate (harmless) rather than losing the user's annotation.
        job.update_annotations(models.PatchedLabeledDataRequest(tracks=[req]), action=AnnotationUpdateAction.CREATE)
        _delete_shapes(job, [sh])
        new = [t["id"] for t in job.get_annotations().to_dict().get("tracks") or [] if t["id"] not in before]
        print(f"created polygon track {new[0] if new else '?'}; later runs: --output polygon-track "
              f"--track-id {new[0] if new else '<id>'} --from-frame <K>")
    print(f"finished in {time.time() - t0:.0f}s. Reload the job page in CVAT (F5).")
    return 0


def cmd_track_to_shapes(args) -> int:
    """Rescue / conversion for mask tracks written by the earlier CLI version."""
    with connect(args.host) as client:
        job = _job(client, args.job)
        data = job.get_annotations().to_dict()
        track = next((t for t in data.get("tracks") or [] if t["id"] == args.track_id), None)
        if track is None:
            raise CliError(f"No track with id {args.track_id} in job {args.job}")
        group = next_group(data) if args.all_frames else track.get("group", 0)
        shapes = track_to_shapes(track, all_frames=args.all_frames, group=group)
        print(f"track {track['id']} -> {len(shapes)} mask shape(s), group {group}, "
              f"frames {shapes[0]['frame']}..{shapes[-1]['frame']}")
        _create_shapes(job, shapes)  # create first, delete after
        _delete_track(job, track)
        print(f"deleted track {track['id']}. Reload the job page in CVAT (F5).")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="cvat_sam2", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp):
        sp.add_argument("--host", default=DEFAULT_HOST, help=f"CVAT URL (default {DEFAULT_HOST}; env CVAT_HOST_URL)")
        sp.add_argument("--job", type=int, required=True, help="CVAT job id")

    sp = sub.add_parser("list", help="list mask shapes (and any mask tracks) in a job")
    common(sp)
    sp.set_defaults(fn=cmd_list)

    sp = sub.add_parser("show", help="summarise one object (group) and flag frames whose mask size jumps")
    common(sp)
    sp.add_argument("--group", type=int, help="summarise the mask shapes of this group")
    sp.add_argument("--track-id", type=int, help="or summarise this (polygon) track")
    sp.add_argument("--all", action="store_true", help="also print every shape")
    sp.set_defaults(fn=cmd_show)

    sp = sub.add_parser("propagate", help="track a mask forward through the video")
    common(sp)
    sp.add_argument("--shape-id", type=int, help="the prompt: a mask or polygon shape (first run)")
    sp.add_argument("--group", type=int, help="mask output: object to re-propagate; its mask on --from-frame is the prompt")
    sp.add_argument("--track-id", type=int, help="polygon-track output: track to re-propagate; its keyframe on --from-frame is the prompt")
    sp.add_argument("--from-frame", type=int, help="frame of the prompt (required with --group / --track-id)")
    sp.add_argument("--output", choices=["mask", "polygon-track"], default="mask",
                    help="mask: one mask shape per frame in a group (default); polygon-track: a native CVAT polygon track")
    sp.add_argument("--simplify-px", type=float, default=1.5,
                    help="polygon output: simplification tolerance in pixels, 0 = none (default 1.5)")
    sp.add_argument("--to-frame", type=int, help="last frame to fill (default: last frame of the job)")
    sp.add_argument("--service-url", default=DEFAULT_SERVICE, help=f"SAM 2 service (default {DEFAULT_SERVICE})")
    sp.add_argument("--service-timeout", type=float, default=600.0,
                    help="max seconds to wait for the next result from the service (default 600)")
    sp.add_argument("--dry-run", action="store_true", help="run the model but do not write to CVAT")
    sp.add_argument("--yes", action="store_true", help="do not ask before replacing existing shapes")
    sp.set_defaults(fn=cmd_propagate)

    sp = sub.add_parser("track-to-shapes", help="convert a mask track (unloadable in the UI) into mask shapes")
    common(sp)
    sp.add_argument("--track-id", type=int, required=True)
    sp.add_argument("--all-frames", action="store_true",
                    help="keep every visible frame as a shape in one new group (default: only the first frame)")
    sp.set_defaults(fn=cmd_track_to_shapes)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.fn(args)
    except CliError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except ServiceError as e:
        print(f"service error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
