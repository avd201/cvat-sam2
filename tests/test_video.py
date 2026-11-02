"""Stage 2 service tests (CPU, FakeBackend tracker: mask moves +2 px/frame, then leaves the image)."""
import io
import json
import os
import sys
import time
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "sam-service"))
sys.path.insert(0, str(ROOT))

from app import main as service_main  # noqa: E402
from app.backends import FakeBackend  # noqa: E402
from app.main import app  # noqa: E402
from sam2common.rle import cvat_points_to_mask, mask_to_cvat_points  # noqa: E402

H, W = 40, 60


def jpeg(seed=0, w=W, h=H) -> bytes:
    arr = np.random.default_rng(seed).integers(0, 255, (h, w, 3), dtype=np.uint8)
    buf = io.BytesIO()
    Image.fromarray(arr).save(buf, format="JPEG")
    return buf.getvalue()


def prompt_points() -> list[int]:
    m = np.zeros((H, W), bool)
    m[10:20, 0:10] = True
    return mask_to_cvat_points(m)


def meta(**kw) -> str:
    d = {"prompt_index": 0, "height": H, "width": W, "prompt_points": prompt_points()}
    d.update(kw)
    return json.dumps(d)


def post(client, n=5, meta_str=None, frames=None):
    frames = frames if frames is not None else [jpeg(i) for i in range(n)]
    files = [("frames", (f"{i:05d}.jpg", b, "image/jpeg")) for i, b in enumerate(frames)]
    return client.post("/v1/video/propagate", data={"meta": meta_str or meta()}, files=files)


def lines(resp):
    return [json.loads(x) for x in resp.text.splitlines() if x]


@pytest.fixture()
def backend():
    return FakeBackend()


@pytest.fixture()
def client(backend):
    app.state.backend = backend
    app.state.propagation_lock = None
    with TestClient(app) as c:
        yield c
    app.state.backend = None


def test_propagation_streams_every_frame_in_order(client):
    r = post(client, n=5)
    assert r.status_code == 200
    msgs = lines(r)
    assert [m["frame"] for m in msgs[:-1]] == [0, 1, 2, 3, 4]
    assert msgs[-1] == {"done": True, "frames": 5, "output": "mask"}
    # frame k holds the prompt shifted right by 2k px
    for m in msgs[:-1]:
        mask = cvat_points_to_mask(m["points"], H, W)
        assert mask[10:20, 2 * m["frame"] : 2 * m["frame"] + 10].all()
        assert mask.sum() == 100


def test_empty_mask_is_null_points(client):
    n = 40  # shift reaches 60 px = image width at frame 30
    msgs = lines(post(client, n=n))
    by_frame = {m["frame"]: m["points"] for m in msgs if "frame" in m}
    assert by_frame[29] is not None and by_frame[30] is None and by_frame[39] is None


def test_prompt_on_later_frame_only_returns_following_frames(client):
    msgs = lines(post(client, n=6, meta_str=meta(prompt_index=3)))
    assert [m["frame"] for m in msgs if "frame" in m] == [3, 4, 5]


def test_frames_dir_cleaned_up_and_lock_released(client, backend):
    post(client, n=3)
    deadline = time.time() + 5
    while os.path.exists(backend.last_frames_dir) and time.time() < deadline:
        time.sleep(0.05)
    assert not os.path.exists(backend.last_frames_dir)
    assert client.get("/healthz").json()["propagation_busy"] is False
    # the model was not "reloaded": same backend instance, load counter untouched
    assert backend.stats.model_loads == 1 and backend.stats.propagations_run == 1


def test_error_midway_is_reported_and_service_recovers(client, backend):
    backend.fail_at_frame = 2
    msgs = lines(post(client, n=5))
    assert [m["frame"] for m in msgs if "frame" in m] == [0, 1]
    assert "error" in msgs[-1] and "after 2 frames" in msgs[-1]["error"]
    backend.fail_at_frame = None
    assert lines(post(client, n=3))[-1]["done"] is True  # lock was released, state clean


def test_concurrent_propagation_gets_429(client):
    assert service_main.app.state.propagation_lock.acquire(blocking=False)
    try:
        assert post(client, n=2).status_code == 429
    finally:
        service_main.app.state.propagation_lock.release()
    assert post(client, n=2).status_code == 200


@pytest.mark.parametrize(
    "kw,frames",
    [
        ({"meta_str": "not json"}, None),
        ({"meta_str": meta(prompt_index=9)}, None),
        ({"meta_str": meta(width=999)}, None),  # bbox/size mismatch
        ({"meta_str": meta(prompt_points=[5, 0, 0, 0, 0])}, None),  # bbox is empty-ish/inconsistent RLE
        ({}, [b"not a jpeg"]),
        ({}, [jpeg(w=30, h=30)]),  # size differs from meta
    ],
)
def test_bad_requests_are_400_and_leave_no_state(client, kw, frames):
    r = post(client, n=3, frames=frames, **kw)
    assert r.status_code == 400, r.text
    assert client.get("/healthz").json()["propagation_busy"] is False


def test_too_many_frames_rejected(client, monkeypatch):
    monkeypatch.setattr(service_main, "MAX_PROPAGATION_FRAMES", 3)
    assert post(client, n=4).status_code == 400


def test_polygon_output_streams_flat_polygons_and_null_when_gone(client):
    pytest.importorskip("cv2")
    msgs = lines(post(client, n=40, meta_str=meta(output="polygon", simplify_px=1.0)))
    by = {m["frame"]: m["points"] for m in msgs if "frame" in m}
    p0 = by[0]
    assert len(p0) % 2 == 0 and len(p0) >= 6 and min(p0[0::2]) == 0 and max(p0[0::2]) == 9
    assert min(by[5][0::2]) == 10  # moved right by 2 px/frame
    assert by[39] is None          # left the image -> no contour
    assert msgs[-1]["done"] is True


@pytest.mark.parametrize("bad", [{"output": "svg"}, {"simplify_px": -1}, {"simplify_px": 99}])
def test_invalid_output_options_are_400(client, bad):
    assert post(client, n=3, meta_str=meta(**bad)).status_code == 400


def test_done_line_and_healthz_advertise_the_output_format(client):
    assert lines(post(client, n=2, meta_str=meta(output="polygon")))[-1]["output"] == "polygon"
    assert client.get("/healthz").json()["outputs"] == ["mask", "polygon"]


def test_next_propagation_may_start_the_moment_done_is_seen(client):
    """Regression: the lock used to be released after the final line was sent -> sporadic 429."""
    for _ in range(25):
        r = post(client, n=2)
        assert r.status_code == 200 and lines(r)[-1]["done"] is True


def test_next_propagation_may_start_the_moment_an_error_is_seen(client, backend):
    backend.fail_at_frame = 1
    for _ in range(25):
        assert "error" in lines(post(client, n=3))[-1]
