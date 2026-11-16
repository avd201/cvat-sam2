"""Region of interest over HTTP: objects are dropped when their centroid leaves, for good; bad ROIs / prompts are refused."""
import io
import json
import sys
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "sam-service"))
sys.path.insert(0, str(ROOT))

from app.backends import FakeBackend  # noqa: E402
from app.main import app  # noqa: E402
from sam2common.rle import mask_to_cvat_points  # noqa: E402
from test_job_endpoint import FakeCvat, H, W  # noqa: E402  (60x40 frames, fake CVAT)


def rect(x0, x1, y0=10, y1=19):
    return [x0, y0, x1, y0, x1, y1, x0, y1]


def lines(resp):
    return [json.loads(x) for x in resp.text.splitlines() if x]


def job(**kw):
    d = {"job_id": 7, "org": "", "from_frame": 0, "to_frame": 14, "output": "polygon", "simplify_px": 0}
    d.update(kw)
    return d


# keep everything LEFT of x=20 (third point at x=0 says which side is kept)
KEEP_LEFT_OF_20 = [20, 0, 20, 40, 0, 20]


@pytest.fixture()
def setup():
    backend, cvat = FakeBackend(), FakeCvat()
    app.state.backend, app.state.cvat, app.state.gpu_queue = backend, cvat, None
    with TestClient(app) as c:
        yield c, backend, cvat
    app.state.backend = app.state.cvat = None


def post(c, **kw):
    return c.post("/v1/video/propagate-job", json=job(**kw))


def test_objects_leave_at_the_frame_their_centroid_crosses_and_stay_dropped(setup):
    c, _, _ = setup
    # A: centroid x=5, moves +2/frame -> 21 > 20 at frame 8.   B: centroid x=15 -> 21 at frame 3.
    objs = [{"id": 1, "prompt_type": "polygon", "prompt_points": rect(0, 9)},
            {"id": 2, "prompt_type": "polygon", "prompt_points": rect(10, 19)}]
    out = lines(post(c, objects=objs, roi_lines=[KEEP_LEFT_OF_20]))
    done = out[-1]
    assert done["done"] is True and done["dropped"] == {"1": 8, "2": 3}
    assert done["roi_lines"] == 1                                                     # echo: lets clients detect a service that ignored the ROI
    by_obj = {1: {}, 2: {}}
    for m in out[:-1]:
        by_obj[m["obj"]][m["frame"]] = m["points"]
    for obj, first_out in ((1, 8), (2, 3)):
        assert all(by_obj[obj][f] is not None for f in range(first_out))              # kept while inside (incl. prompt frame)
        assert all(by_obj[obj][f] is None for f in range(first_out, 15))              # explicit "outside" from then on
    assert sorted(by_obj[1]) == list(range(15))                                        # every frame is reported, so older results get overwritten


def test_without_a_roi_nothing_changes(setup):
    c, _, _ = setup
    out = lines(post(c, objects=[{"id": 1, "prompt_type": "polygon", "prompt_points": rect(0, 9)}]))
    assert "dropped" not in out[-1] and out[-1]["done"] is True and "roi_lines" not in out[-1]
    assert all(m["points"] is not None for m in out[:-1] if m["frame"] < 28)            # the fake only empties past the image edge
    out = lines(post(c, objects=[{"id": 1, "prompt_type": "polygon", "prompt_points": rect(0, 9)}], roi_lines=[]))
    assert "dropped" not in out[-1]                                                     # an empty list means "no ROI"


def test_single_object_request_is_supported_and_mask_output_gets_nulls_too(setup):
    c, _, _ = setup
    out = lines(post(c, prompt_type="polygon", prompt_points=rect(10, 19), output="mask", roi_lines=[KEEP_LEFT_OF_20]))
    assert out[-1]["dropped"] == {"1": 3}
    assert [m["points"] is None for m in out[:-1]] == [False] * 3 + [True] * 12
    assert "obj" not in out[0]                                                          # single-object stream format unchanged


def test_two_lines_keep_a_band(setup):
    c, _, _ = setup
    band = [KEEP_LEFT_OF_20, [2, 0, 2, 40, 10, 20]]                                     # x <= 20 and x >= 2
    out = lines(post(c, prompt_type="polygon", prompt_points=rect(0, 9), roi_lines=band))   # centroid 5: inside both
    assert out[-1]["dropped"] == {"1": 8}
    out = lines(post(c, prompt_type="polygon", prompt_points=rect(0, 3), roi_lines=band))   # centroid 2.0 -> inside (x>=2), then 4,6...
    assert out[-1]["done"] is True


def test_prompt_outside_the_roi_is_refused_naming_the_objects_and_nothing_is_left_behind(setup):
    c, _, _ = setup
    objs = [{"id": 1, "prompt_type": "polygon", "prompt_points": rect(0, 9)},
            {"id": 4, "prompt_type": "polygon", "prompt_points": rect(30, 39)},
            {"id": 6, "prompt_type": "polygon", "prompt_points": rect(40, 49)}]
    r = post(c, objects=objs, roi_lines=[KEEP_LEFT_OF_20])
    assert r.status_code == 400
    assert "2 object(s) start outside the region of interest" in r.text and "object 4 (line 1)" in r.text and "object 6 (line 1)" in r.text
    assert "object 1" not in r.text
    h = c.get("/healthz").json()
    assert h["queue_waiting"] == 0 and h["propagation_busy"] is False
    single = post(c, prompt_type="polygon", prompt_points=rect(30, 39), roi_lines=[KEEP_LEFT_OF_20])
    assert single.status_code == 400 and "The prompt starts outside the region of interest" in single.text


def test_straddling_prompt_is_accepted_when_its_centroid_is_inside(setup):
    c, _, _ = setup
    r = lines(post(c, prompt_type="polygon", prompt_points=rect(12, 25), roi_lines=[KEEP_LEFT_OF_20]))   # centroid 19 -> inside
    assert r[-1]["done"] is True and r[-1]["dropped"] == {"1": 1}


@pytest.mark.parametrize("roi, msg", [([[1, 2, 3]], "needs 4 numbers"), ([[5, 5, 5, 5]], "coincide"),
                                      ([[0, 0, 10, 10, 5, 5]], "lies on the line"), ([[0, 0, 1, 1]] * 9, "allowed range")])
def test_invalid_roi_is_a_400_before_anything_is_downloaded(setup, roi, msg):
    c, _, cvat = setup
    r = post(c, prompt_type="polygon", prompt_points=rect(0, 9), roi_lines=roi)
    assert r.status_code == 400 and "Invalid ROI" in r.text and msg in r.text
    assert not [x for x in cvat.calls if x[0] == "frame"]
    assert c.get("/healthz").json()["queue_waiting"] == 0


class ScriptedBackend(FakeBackend):
    """Masks: inside, inside, OUTSIDE, then back INSIDE again."""

    def propagate(self, frames_dir, prompt_index, prompt_mask, cancel):
        import os
        n = len([f for f in os.listdir(frames_dir) if f.endswith(".jpg")])
        for idx in range(prompt_index, n):
            x0 = {0: 0, 1: 2, 2: 30, 3: 2}.get(idx, 2)
            m = np.zeros_like(prompt_mask)
            m[10:20, x0:x0 + 10] = True
            yield idx, m


def test_an_object_that_came_back_stays_dropped(setup):
    c, _, _ = setup
    app.state.backend = ScriptedBackend()
    out = lines(post(c, to_frame=5, prompt_type="polygon", prompt_points=rect(0, 9), roi_lines=[KEEP_LEFT_OF_20]))
    assert [m["points"] is None for m in out[:-1]] == [False, False, True, True, True, True]
    assert out[-1]["dropped"] == {"1": 2}


def test_multipart_endpoint_supports_the_roi_too(setup):
    c, _, _ = setup
    mk = np.zeros((H, W), bool)
    mk[10:20, 10:20] = True
    meta = {"prompt_index": 0, "height": H, "width": W, "prompt_points": mask_to_cvat_points(mk), "output": "polygon",
            "simplify_px": 0, "roi_lines": [KEEP_LEFT_OF_20]}

    def jpg(i):
        buf = io.BytesIO()
        Image.fromarray(np.random.default_rng(i).integers(0, 255, (H, W, 3), dtype=np.uint8)).save(buf, format="JPEG")
        return ("frames", (f"{i:05d}.jpg", buf.getvalue(), "image/jpeg"))

    r = c.post("/v1/video/propagate", data={"meta": json.dumps(meta)}, files=[jpg(i) for i in range(8)])
    out = lines(r)
    assert out[-1]["dropped"] == {"1": 3}
    bad = dict(meta, roi_lines=[[0, 0, 0, 0]])
    assert c.post("/v1/video/propagate", data={"meta": json.dumps(bad)}, files=[jpg(0)]).status_code == 400


def test_two_lines_are_echoed_as_two_and_healthz_advertises_support(setup):
    c, _, _ = setup
    out = lines(post(c, prompt_type="polygon", prompt_points=rect(0, 9), roi_lines=[KEEP_LEFT_OF_20, [2, 0, 2, 40, 10, 20]]))
    assert out[-1]["roi_lines"] == 2
    assert c.get("/healthz").json()["roi_max_lines"] == 8
