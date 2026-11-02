"""Pure-logic tests for the CLI (no CVAT server, no service)."""
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "cli"))

import cvat_sam2 as cli  # noqa: E402
from sam2common.rle import mask_to_cvat_points, normalize_points, points_area, cvat_points_to_mask  # noqa: E402


def pts(x0, w=10):
    m = np.zeros((30, 50), bool)
    m[5:15, x0 : x0 + w] = True
    return mask_to_cvat_points(m)


def shp(sid, frame, group=0, typ="mask", x0=0, w=10, floats=True):
    p = pts(x0, w)
    return {"id": sid, "type": typ, "frame": frame, "label_id": 2, "group": group, "source": "semi-auto",
            "occluded": False, "outside": False, "z_order": 3, "rotation": 0.0,
            "points": [float(v) for v in p] if floats else p, "attributes": [{"spec_id": 1, "value": "a"}]}


def poly(x0, y0=5, w=10, h=10):
    return [float(v) for v in (x0, y0, x0 + w, y0, x0 + w, y0 + h, x0, y0 + h)]


def pshape(frame, x0=0, outside=False, sid=None):
    d = {"type": "polygon", "frame": frame, "occluded": False, "outside": outside, "z_order": 0, "rotation": 0.0,
         "points": poly(x0), "attributes": []}
    if sid:
        d["id"] = sid
    return d


DATA = {
    "shapes": [shp(10, 0), shp(11, 5, group=3), shp(12, 6, group=3), shp(13, 7, group=3),
               shp(14, 20, group=3), {**shp(15, 6, group=3, typ="polygon"), "points": poly(2)},
               shp(16, 9, group=4), {**shp(17, 3), "type": "rectangle", "points": [1.0, 1.0, 5.0, 5.0]}],
    "tracks": [{"id": 10, "frame": 0, "label_id": 2, "group": 0, "source": "manual",
                "shapes": [{**shp(1, 0), "outside": False}, {**shp(2, 1), "outside": True},
                           {**shp(3, 2, x0=4)}]},
               {"id": 20, "frame": 10, "label_id": 2, "group": 6, "source": "manual", "attributes": [],
                "shapes": [pshape(10, sid=1), pshape(11, x0=1, sid=2), pshape(12, x0=2, sid=3), pshape(40, x0=9, sid=4)]}],
}


def test_float_points_as_returned_by_cvat_api():
    """Regression: CVAT's API returns mask points as floats (3.0), which broke decoding."""
    ints = pts(3)
    floats = [float(v) for v in ints]
    assert normalize_points(floats) == ints
    assert np.array_equal(cvat_points_to_mask(floats, 30, 50), cvat_points_to_mask(ints, 30, 50))
    assert points_area(floats) == points_area(ints) == 100
    with pytest.raises(ValueError, match="not an integer"):
        normalize_points([1.5, 2.0])


def test_list_objects_masks_polygons_and_tracks():
    rows = cli.list_objects(DATA)
    assert [(r["kind"], r["type"], r["id"]) for r in rows if r["kind"] == "shape"] == [
        ("shape", "mask", 10), ("shape", "mask", 11), ("shape", "mask", 12), ("shape", "polygon", 15),
        ("shape", "mask", 13), ("shape", "mask", 14), ("shape", "mask", 16)]  # rectangle 17 not listed
    assert sorted((r["id"], r["type"]) for r in rows if r["kind"] == "track") == [(10, "mask"), (20, "polygon")]


def test_next_group_considers_shapes_and_tracks():
    assert cli.next_group(DATA) == 7  # polygon track 20 has group 6
    assert cli.next_group({}) == 1


def test_find_source_by_shape_and_by_group():
    s = cli.find_source(DATA, shape_id=10, group=None, from_frame=None)
    assert (s["from_frame"], s["group"]) == (0, 0)
    s = cli.find_source(DATA, shape_id=None, group=3, from_frame=6)
    assert s["shape"]["id"] == 12 and s["group"] == 3  # polygon 15 on the same frame is ignored


@pytest.mark.parametrize("kw,msg", [
    (dict(shape_id=None, group=None, from_frame=0), "exactly one"),
    (dict(shape_id=10, group=3, from_frame=0), "exactly one"),
    (dict(shape_id=999, group=None, from_frame=None), "No shape with id 999"),
    (dict(shape_id=17, group=None, from_frame=None), "need a mask or polygon"),
    (dict(shape_id=10, group=None, from_frame=4), "is on frame 0"),
    (dict(shape_id=None, group=3, from_frame=None), "--from-frame is required"),
    (dict(shape_id=None, group=3, from_frame=8), "found 0"),
    (dict(shape_id=None, group=0, from_frame=0), "Group 0"),
])
def test_find_source_errors(kw, msg):
    with pytest.raises(cli.CliError, match=msg):
        cli.find_source(DATA, **kw)


def test_find_source_accepts_polygon_shape_and_polygon_track():
    s = cli.find_source(DATA, shape_id=15, group=None, from_frame=None)
    assert s["shape"]["type"] == "polygon" and s["track"] is None
    s = cli.find_source(DATA, shape_id=None, group=None, track_id=20, from_frame=11)
    assert s["track"]["id"] == 20 and s["from_frame"] == 11 and s["group"] == 6


@pytest.mark.parametrize("kw,msg", [
    (dict(track_id=20, from_frame=13), "has no shape on frame 13"),
    (dict(track_id=20, from_frame=None), "--from-frame is required"),
    (dict(track_id=10, from_frame=0), "only polygon tracks"),
    (dict(track_id=999, from_frame=0), "No track with id 999"),
])
def test_find_source_track_errors(kw, msg):
    with pytest.raises(cli.CliError, match=msg):
        cli.find_source(DATA, shape_id=None, group=None, **kw)


def test_plan_first_run_assigns_new_group_and_skips_missing_frames():
    src = cli.find_source(DATA, shape_id=10, group=None, from_frame=None)
    results = {1: pts(2), 2: None, 3: pts(4)}
    create, delete = cli.plan_shapes(DATA, src, results, 3, group=5)
    assert [(c["frame"], c["group"]) for c in create] == [(1, 5), (3, 5)]  # frame 2: object not found
    assert delete == []
    assert create[0]["z_order"] == 3 and create[0]["attributes"] == [{"spec_id": 1, "value": "a"}]
    assert all("id" not in c and c["type"] == "mask" for c in create)


def test_repropagate_replaces_only_same_group_masks_after_from_frame_within_range():
    src = cli.find_source(DATA, shape_id=None, group=3, from_frame=6)
    results = {f: pts(1) for f in range(7, 11)}
    create, delete = cli.plan_shapes(DATA, src, results, 10, group=3)
    assert sorted(d["id"] for d in delete) == [13]   # frame 7 only: not 11/12 (<=6), not 14 (frame 20 > 10),
    #                                                 not polygon 15, not group 4 shape 16
    assert [c["frame"] for c in create] == [7, 8, 9, 10]


def test_plan_requires_every_frame():
    src = cli.find_source(DATA, shape_id=10, group=None, from_frame=None)
    with pytest.raises(cli.CliError, match="no result for frame 2"):
        cli.plan_shapes(DATA, src, {1: pts(1)}, 2, group=5)


def test_track_to_shapes_drops_outside_and_respects_all_frames():
    tr = DATA["tracks"][0]
    only_first = cli.track_to_shapes(tr, all_frames=False, group=0)
    assert [s["frame"] for s in only_first] == [0]
    everything = cli.track_to_shapes(tr, all_frames=True, group=7)
    assert [(s["frame"], s["group"]) for s in everything] == [(0, 7), (2, 7)]  # outside frame 1 dropped
    with pytest.raises(cli.CliError, match="no visible mask"):
        cli.track_to_shapes({"id": 1, "label_id": 2, "shapes": [{**shp(1, 0), "outside": True}]},
                            all_frames=True, group=1)


def test_suspect_frames_ranks_biggest_jump_first():
    shapes = [shp(1, 0, w=10), shp(2, 1, w=10), shp(3, 2, w=30), shp(4, 3, w=30)]
    top = cli.suspect_frames(shapes)
    assert top[0][0] == 2 and top[0][2] == 100 and top[0][3] == 300


# ---- request objects against the REAL cvat-sdk (skipped if it is not installed) ----
class FakeJob:
    def __init__(self):
        self.calls = []

    def update_annotations(self, req, action):
        self.calls.append((action.value, req.to_dict()))


def test_requests_touch_only_the_intended_kind_of_object():
    pytest.importorskip("cvat_sdk")
    job = FakeJob()
    cli._delete_shapes(job, [DATA["shapes"][1]])
    cli._delete_track(job, DATA["tracks"][0])
    cli._create_shapes(job, cli.track_to_shapes(DATA["tracks"][0], all_frames=True, group=7))
    cli._set_group(job, DATA["shapes"][0], 5)
    (a1, d1), (a2, d2), (a3, d3), (a4, d4) = job.calls
    assert a1 == "delete" and [s["id"] for s in d1["shapes"]] == [11] and not d1.get("tracks")
    assert a2 == "delete" and [t["id"] for t in d2["tracks"]] == [10] and not d2.get("shapes")
    assert a3 == "create" and len(d3["shapes"]) == 2 and not d3.get("tracks")
    assert a4 == "update" and d4["shapes"][0]["id"] == 10 and d4["shapes"][0]["group"] == 5


def test_prompt_mask_points_from_polygon_and_mask():
    p = cli.prompt_mask_points(cli.find_source(DATA, shape_id=15, group=None, from_frame=None)["shape"], 30, 50)
    assert 100 <= points_area(p) <= 150  # ~11x11 incl. outline
    m = cli.prompt_mask_points(DATA["shapes"][0], 30, 50)
    assert all(isinstance(v, int) for v in m) and points_area(m) == 100


def test_shape_area_mask_and_polygon():
    assert cli.shape_area(DATA["shapes"][0]) == 100.0
    assert cli.shape_area(pshape(0)) == 100.0


def test_plan_polygon_track_from_track_replaces_range_and_marks_outside():
    src = cli.find_source(DATA, shape_id=None, group=None, track_id=20, from_frame=10)
    results = {11: poly(3), 12: None, 13: None, 14: poly(5)}
    shapes, replaced = cli.plan_polygon_track(src, poly(0), results, 14, 178)
    assert replaced == 2 and [s["frame"] for s in shapes] == [10, 11, 12, 13, 14, 15, 40]  # 15 = closing outside marker
    by = {s["frame"]: s for s in shapes}
    assert by[10]["points"] == poly(0) and by[40]["points"] == poly(9)  # before / after the range untouched
    assert [by[f]["outside"] for f in (11, 12, 13, 14, 15)] == [False, True, True, False, True]
    assert by[15]["points"] == poly(5)  # the closing marker repeats the last polygon
    assert by[12]["points"] == poly(3) == by[13]["points"]
    assert all("id" not in s and s["type"] == "polygon" for s in shapes)


def test_plan_polygon_track_from_mask_shape_creates_first_keyframe():
    src = cli.find_source(DATA, shape_id=10, group=None, from_frame=None)
    shapes, replaced = cli.plan_polygon_track(src, poly(0), {1: poly(1), 2: poly(2)}, 2, 178)
    assert replaced == 0 and [s["frame"] for s in shapes] == [0, 1, 2, 3] and shapes[0]["points"] == poly(0)
    assert shapes[-1]["outside"]
    assert shapes[0]["z_order"] == 3  # taken from the source shape


def test_polygon_track_request_objects_build_with_real_sdk():
    pytest.importorskip("cvat_sdk")
    from cvat_sdk import models

    src = cli.find_source(DATA, shape_id=None, group=None, track_id=20, from_frame=10)
    shapes, _ = cli.plan_polygon_track(src, poly(0), {11: poly(3), 12: None}, 12, 178)
    req = models.LabeledTrackRequest(id=20, frame=10, label_id=2, group=6, source="manual",
                                     shapes=[models.TrackedShapeRequest(**s) for s in shapes], attributes=[])
    assert [(x.frame, x.outside) for x in req.shapes][:3] == [(10, False), (11, False), (12, True)]


# ---- regression: an outdated service silently returned MASK data when asked for polygons ----
def _blob_mask_points(k):
    yy, xx = np.mgrid[0:200, 0:300]
    m = ((xx - (100 + 3 * k)) / (28 + k % 3)) ** 2 + ((yy - (100 + k % 5)) / 45) ** 2 < 1
    return mask_to_cvat_points(m)


def test_mask_data_masquerading_as_polygons_is_refused_before_writing():
    src = cli.find_source(DATA, shape_id=10, group=None, from_frame=None)
    wrong = {f: _blob_mask_points(f) for f in range(1, 31)}  # what the old service returned
    shapes, _ = cli.plan_polygon_track(src, poly(0), wrong, 30, 178)
    with pytest.raises(cli.CliError, match="invalid number of points"):
        cli.validate_for_cvat(shapes)


def test_validate_for_cvat_accepts_good_data():
    cli.validate_for_cvat([pshape(1), pshape(2)])
    cli.validate_for_cvat([cli._new_shape(DATA["shapes"][0], 3, pts(1), 1)])
    cli.validate_for_cvat([])


def test_validate_for_cvat_server_rules():
    bad_odd = {**pshape(1), "points": [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0]}
    bad_short = {**pshape(2), "points": [1.0, 2.0, 3.0, 4.0]}
    with pytest.raises(cli.CliError, match=r"\(1, 7\), \(2, 4\)"):
        cli.validate_for_cvat([bad_odd, bad_short])


def test_output_echo_detects_service_that_ignored_the_format():
    cli.check_output_echo({"done": True, "output": "polygon"}, "polygon")
    cli.check_output_echo({"done": True}, "mask")  # old services are fine for mask output
    with pytest.raises(cli.ServiceError, match="Rebuild"):
        cli.check_output_echo({"done": True}, "polygon")
    with pytest.raises(cli.ServiceError, match="produced 'mask'"):
        cli.check_output_echo({"done": True, "output": "mask"}, "polygon")


# ---- regression: a track must not keep showing its last keyframe after the propagated range ----
def _track_src():
    return cli.find_source(DATA, shape_id=10, group=None, from_frame=None)


def test_propagated_range_is_closed_with_an_outside_keyframe():
    shapes, _ = cli.plan_polygon_track(_track_src(), poly(0), {f: poly(f) for f in range(1, 11)}, 10, 178)
    assert [(s["frame"], s["outside"]) for s in shapes][-2:] == [(10, False), (11, True)]


def test_no_closing_marker_at_end_of_job():
    shapes, _ = cli.plan_polygon_track(_track_src(), poly(0), {f: poly(f) for f in range(1, 11)}, 10, 10)
    assert shapes[-1]["frame"] == 10 and not shapes[-1]["outside"]


def test_no_extra_marker_when_range_already_ends_outside():
    results = {f: poly(f) for f in range(1, 10)} | {10: None}
    shapes, _ = cli.plan_polygon_track(_track_src(), poly(0), results, 10, 178)
    assert shapes[-1]["frame"] == 10 and shapes[-1]["outside"]


def test_no_marker_when_next_keyframe_follows_immediately():
    src = cli.find_source(DATA, shape_id=None, group=None, track_id=20, from_frame=10)  # keyframes 10,11,12,40
    shapes, _ = cli.plan_polygon_track(src, poly(0), {11: poly(1), 12: poly(2), 13: poly(3)}, 13, 178)
    assert [s["frame"] for s in shapes] == [10, 11, 12, 13, 14, 40]  # 14 = marker; 40 kept
    src2 = cli.find_source({**DATA, "tracks": [{**DATA["tracks"][1], "shapes": DATA["tracks"][1]["shapes"] + [pshape(14, outside=True)]}]},
                           shape_id=None, group=None, track_id=20, from_frame=10)
    shapes2, _ = cli.plan_polygon_track(src2, poly(0), {11: poly(1), 12: poly(2), 13: poly(3)}, 13, 178)
    assert [s["frame"] for s in shapes2].count(14) == 1  # the kept keyframe on 14 is not duplicated


def test_extending_a_track_replaces_its_old_closing_marker():
    base = cli.plan_polygon_track(_track_src(), poly(0), {f: poly(f) for f in range(1, 11)}, 10, 178)[0]
    track = {"id": 5, "label_id": 2, "group": 0, "source": "manual", "shapes": base}
    src = cli.find_source({"tracks": [track], "shapes": []}, shape_id=None, group=None, track_id=5, from_frame=10)
    shapes, replaced = cli.plan_polygon_track(src, poly(10), {f: poly(f) for f in range(11, 21)}, 20, 178)
    assert replaced == 1  # the old marker on frame 11
    assert [(s["frame"], s["outside"]) for s in shapes][-3:] == [(19, False), (20, False), (21, True)]
    assert sum(s["outside"] for s in shapes) == 1
