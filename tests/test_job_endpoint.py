"""Tests for /v1/video/propagate-job (service downloads frames from CVAT with the caller's session)."""
import io
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "sam-service"))
sys.path.insert(0, str(ROOT))

from app.backends import FakeBackend  # noqa: E402
from app.cvat_frames import CvatClient, CvatError  # noqa: E402
from app.main import app  # noqa: E402
from sam2common.rle import cvat_points_to_mask, mask_to_cvat_points  # noqa: E402

H, W = 40, 60


def jpeg(seed=0, fmt="JPEG"):
    arr = np.random.default_rng(seed).integers(0, 255, (H, W, 3), dtype=np.uint8)
    buf = io.BytesIO()
    Image.fromarray(arr).save(buf, format=fmt)
    return buf.getvalue()


class FakeCvat:
    """Stands in for CvatClient: records calls, can refuse auth or frames."""

    def __init__(self, start=0, stop=100, auth_ok=True, fail_frame=None, png=False):
        self.start, self.stop, self.auth_ok, self.fail_frame, self.png = start, stop, auth_ok, fail_frame, png
        self.calls = []

    def check_auth(self, headers):
        self.calls.append(("auth", headers.get("cookie")))
        if not self.auth_ok:
            raise CvatError(401, "CVAT refused the request: not logged in or no access to this job")

    def job_range(self, headers, job_id, org):
        self.calls.append(("job", job_id, org))
        return self.start, self.stop

    def fetch_frame(self, headers, job_id, number, org):
        self.calls.append(("frame", number))
        if number == self.fail_frame:
            raise CvatError(404, "CVAT: job or frame not found")
        return jpeg(number, "PNG" if self.png else "JPEG")


@pytest.fixture()
def setup():
    backend, cvat = FakeBackend(), FakeCvat()
    app.state.backend, app.state.cvat, app.state.propagation_lock = backend, cvat, None
    with TestClient(app) as c:
        yield c, backend, cvat
    app.state.backend = app.state.cvat = None


def mask_points():
    m = np.zeros((H, W), bool)
    m[10:20, 0:10] = True
    return mask_to_cvat_points(m)


def body(**kw):
    d = {"job_id": 7, "org": "", "from_frame": 5, "to_frame": 9, "prompt_type": "mask",
         "prompt_points": [float(v) for v in mask_points()], "output": "mask", "simplify_px": 1.5}
    d.update(kw)
    return d


def lines(r):
    return [json.loads(x) for x in r.text.splitlines() if x]


def test_happy_path_mask_prompt_float_points_and_session_forwarded(setup):
    c, backend, cvat = setup
    r = c.post("/v1/video/propagate-job", json=body(), headers={"Cookie": "sessionid=abc"})
    assert r.status_code == 200
    msgs = lines(r)
    assert [m["frame"] for m in msgs[:-1]] == [0, 1, 2, 3, 4] and msgs[-1]["done"]  # relative to from_frame
    assert cvat.calls[0] == ("auth", "sessionid=abc")
    assert sorted(x[1] for x in cvat.calls if x[0] == "frame") == [5, 6, 7, 8, 9]  # exactly the range
    assert cvat_points_to_mask(msgs[2]["points"], H, W)[10:20, 4:14].all()  # moved 2 px/frame


def test_polygon_prompt_and_polygon_output(setup):
    pytest.importorskip("cv2")
    c, *_ = setup
    poly = [0.0, 10.0, 10.0, 10.0, 10.0, 20.0, 0.0, 20.0]
    r = c.post("/v1/video/propagate-job", json=body(prompt_type="polygon", prompt_points=poly, output="polygon"))
    msgs = lines(r)
    assert r.status_code == 200 and len(msgs[0]["points"]) >= 6 and len(msgs[0]["points"]) % 2 == 0
    assert msgs[-1]["output"] == "polygon"


def test_png_frames_from_cvat_are_accepted(setup):
    c, backend, cvat = setup
    cvat.png = True
    assert c.post("/v1/video/propagate-job", json=body()).status_code == 200


def test_unauthenticated_caller_is_rejected_before_any_frame_or_gpu_work(setup):
    c, backend, cvat = setup
    cvat.auth_ok = False
    r = c.post("/v1/video/propagate-job", json=body())
    assert r.status_code == 401 and [x[0] for x in cvat.calls] == ["auth"]
    assert backend.stats.propagations_run == 0


def test_org_slug_is_passed_through(setup):
    c, backend, cvat = setup
    c.post("/v1/video/propagate-job", json=body(org="my-org_1"))
    assert ("job", 7, "my-org_1") in cvat.calls


@pytest.mark.parametrize("kw", [
    {"from_frame": 9, "to_frame": 5}, {"from_frame": 5, "to_frame": 5}, {"to_frame": 101}, {"from_frame": -1},
])
def test_bad_ranges_are_400_and_nothing_is_fetched(setup, kw):
    c, backend, cvat = setup
    assert c.post("/v1/video/propagate-job", json=body(**kw)).status_code in (400, 422)
    assert not [x for x in cvat.calls if x[0] == "frame"]


def test_range_limit(setup, monkeypatch):
    from app import main
    c, backend, cvat = setup
    monkeypatch.setattr(main, "MAX_PROPAGATION_FRAMES", 3)
    assert c.post("/v1/video/propagate-job", json=body()).status_code == 400


@pytest.mark.parametrize("kw", [{"org": "../etc"}, {"org": "a b"}, {"prompt_type": "box"}, {"output": "svg"}])
def test_invalid_fields_are_rejected(setup, kw):
    assert setup[0].post("/v1/video/propagate-job", json=body(**kw)).status_code == 422


def test_frame_failure_is_reported_with_cvat_status(setup):
    c, backend, cvat = setup
    cvat.fail_frame = 7
    r = c.post("/v1/video/propagate-job", json=body())
    assert r.status_code == 404 and backend.stats.propagations_run == 0


@pytest.mark.parametrize("pts", [[1.0, 2.0], [0.0, 0.0, 5.0, 0.0, 5.0], [90.0, 90.0, 99.0, 90.0, 99.0, 99.0]])
def test_bad_polygon_prompts_are_400(setup, pts):
    c, *_ = setup
    assert c.post("/v1/video/propagate-job", json=body(prompt_type="polygon", prompt_points=pts)).status_code == 400


# ---- CvatClient against a real local HTTP server: header whitelist + error mapping ----
import re

_VALID_HOST = re.compile(r"^([a-z0-9.-]+)(:[0-9]+)?$")  # what Django's get_host() accepts (no underscore)


class _Handler(BaseHTTPRequestHandler):
    seen = []

    def do_GET(self):  # noqa: N802
        _Handler.seen.append((self.path, dict(self.headers)))
        if not _VALID_HOST.match((self.headers.get("Host") or "").lower()):  # Django: DisallowedHost -> 400
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"Bad Request (400)")
        elif self.path.startswith("/api/users/self"):
            ok = "sessionid=good" in (self.headers.get("Cookie") or "")
            self.send_response(200 if ok else 401)
            self.end_headers()
            self.wfile.write(b"{}")
        elif self.path.startswith("/api/jobs/7/data"):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"FRAMEBYTES")
        elif self.path.startswith("/api/jobs/7"):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"start_frame": 3, "stop_frame": 50}')
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *a):
        pass


@pytest.fixture()
def cvat_server():
    srv = HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    _Handler.seen.clear()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


def test_cvat_client_forwards_only_cookie_and_authorization(cvat_server):
    cl = CvatClient(cvat_server)
    h = {"Cookie": "sessionid=good", "Authorization": "Token t", "X-Forwarded-For": "1.2.3.4", "Host": "evil"}
    cl.check_auth(h)
    assert cl.job_range(h, 7, "org1") == (3, 50)
    assert cl.fetch_frame(h, 7, 12, "org1") == b"FRAMEBYTES"
    paths = [p for p, _ in _Handler.seen]
    assert "/api/jobs/7/data?type=frame&number=12&quality=original&org=org1" in paths
    for _, hdrs in _Handler.seen:
        low = {k.lower(): v for k, v in hdrs.items()}  # header names are case-insensitive
        assert low.get("cookie") == "sessionid=good" and low.get("authorization") == "Token t"
        assert "x-forwarded-for" not in low


def test_cvat_client_maps_errors(cvat_server):
    cl = CvatClient(cvat_server)
    with pytest.raises(CvatError) as e:
        cl.check_auth({"Cookie": "sessionid=bad"})
    assert e.value.status == 401
    with pytest.raises(CvatError) as e:
        cl.job_range({"Cookie": "sessionid=good"}, 99, "")
    assert e.value.status == 404
    with pytest.raises(CvatError) as e:
        CvatClient("http://127.0.0.1:1", timeout=1).check_auth({})
    assert e.value.status == 502


def test_prompt_frame_result_is_the_users_own_mask_not_the_models(setup):
    """Frame 0 of the stream must equal the prompt exactly (the plugin builds the first keyframe from it)."""
    c, backend, cvat = setup
    orig = backend.propagate

    def noisy(frames_dir, prompt_index, prompt_mask, cancel):
        for idx, m in orig(frames_dir, prompt_index, prompt_mask, cancel):
            yield idx, (np.zeros_like(m) if idx == prompt_index else m)  # model "disagrees" on the prompt frame

    backend.propagate = noisy
    msgs = lines(c.post("/v1/video/propagate-job", json=body()))
    assert np.array_equal(cvat_points_to_mask(msgs[0]["points"], H, W), cvat_points_to_mask(mask_points(), H, W))


def test_unexpected_cvat_status_is_reported_with_status_and_body(cvat_server):
    """A CVAT answer that is not 200/401/403/404 must say WHAT it was, not just 'unreachable'."""
    class H(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"Bad Request (400): disallowed host")

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        with pytest.raises(CvatError) as e:
            CvatClient(f"http://127.0.0.1:{srv.server_port}").check_auth({})
        assert e.value.status == 502
        assert "HTTP 400" in str(e.value) and "/api/users/self" in str(e.value) and "disallowed host" in str(e.value)
    finally:
        srv.shutdown()


def test_cvat_client_sends_a_valid_host_header_so_django_does_not_answer_400(cvat_server):
    """Regression: calling CVAT as `cvat_server:8080` got HTTP 400 (underscore is an invalid Host)."""
    CvatClient(cvat_server).check_auth({"Cookie": "sessionid=good"})  # default host_header="localhost" -> accepted
    low = {k.lower(): v for k, v in _Handler.seen[-1][1].items()}
    assert low["host"] == "localhost"
    with pytest.raises(CvatError) as e:  # the old behaviour, reproduced: Host derived from the container name
        CvatClient(cvat_server, host_header="cvat_server:8080").check_auth({"Cookie": "sessionid=good"})
    assert e.value.status == 502 and "HTTP 400" in str(e.value)


# ---------------------------------------------------------------------------------------------- multi-object
def obj_mask_points(x0, y0=10, size=10):
    m = np.zeros((H, W), bool)
    m[y0 : y0 + size, x0 : x0 + size] = True
    return mask_to_cvat_points(m)


def multi_body(**kw):
    d = {"job_id": 7, "org": "", "from_frame": 5, "to_frame": 9, "output": "mask", "simplify_px": 1.5,
         "objects": [
             {"id": 1, "prompt_type": "mask", "prompt_points": [float(v) for v in obj_mask_points(0, 10)]},
             {"id": 2, "prompt_type": "mask", "prompt_points": [float(v) for v in obj_mask_points(20, 25)]},
         ]}
    d.update(kw)
    return d


def by_obj(msgs):
    out = {}
    for m in msgs:
        if "obj" in m:
            out.setdefault(m["obj"], {})[m["frame"]] = m["points"]
    return out


def test_two_objects_are_tracked_independently_in_one_call(setup):
    c, backend, cvat = setup
    r = c.post("/v1/video/propagate-job", json=multi_body())
    msgs = lines(r)
    assert r.status_code == 200
    assert msgs[-1] == {"done": True, "frames": 5, "objects": 2, "output": "mask"}
    per = by_obj(msgs)
    assert sorted(per) == [1, 2] and all(sorted(v) == [0, 1, 2, 3, 4] for v in per.values())
    m1 = cvat_points_to_mask(per[1][3], H, W)
    m2 = cvat_points_to_mask(per[2][3], H, W)
    assert m1[10:20, 6:16].all() and m1.sum() == 100          # object 1 moved 6 px, nothing of object 2 in it
    assert m2[25:35, 26:36].all() and m2.sum() == 100          # object 2 moved 6 px on its own
    assert not (m1 & m2).any()
    assert sorted(x[1] for x in cvat.calls if x[0] == "frame") == [5, 6, 7, 8, 9]   # frames fetched ONCE for both
    assert backend.stats.propagations_run == 2                 # the generic fallback runs once per object


def test_multi_prompt_frame_is_each_users_own_annotation(setup):
    c, backend, cvat = setup
    orig = backend.propagate

    def noisy(frames_dir, prompt_index, prompt_mask, cancel):
        for idx, m in orig(frames_dir, prompt_index, prompt_mask, cancel):
            yield idx, (np.zeros_like(m) if idx == prompt_index else m)

    backend.propagate = noisy
    per = by_obj(lines(c.post("/v1/video/propagate-job", json=multi_body())))
    assert np.array_equal(cvat_points_to_mask(per[1][0], H, W), cvat_points_to_mask(obj_mask_points(0, 10), H, W))
    assert np.array_equal(cvat_points_to_mask(per[2][0], H, W), cvat_points_to_mask(obj_mask_points(20, 25), H, W))


def test_mixed_prompt_types_and_polygon_output(setup):
    pytest.importorskip("cv2")
    c, *_ = setup
    objs = [{"id": 0, "prompt_type": "mask", "prompt_points": [float(v) for v in obj_mask_points(0, 10)]},
            {"id": 5, "prompt_type": "polygon", "prompt_points": [20.0, 25.0, 30.0, 25.0, 30.0, 35.0, 20.0, 35.0]}]
    msgs = lines(c.post("/v1/video/propagate-job", json=multi_body(objects=objs, output="polygon")))
    per = by_obj(msgs)
    assert sorted(per) == [0, 5] and msgs[-1]["output"] == "polygon" and msgs[-1]["objects"] == 2
    assert all(len(p) >= 6 and len(p) % 2 == 0 for p in per[5].values() if p is not None)


def test_single_object_format_is_unchanged(setup):
    c, *_ = setup
    msgs = lines(c.post("/v1/video/propagate-job", json=body()))
    assert all("obj" not in m for m in msgs) and "objects" not in msgs[-1]


@pytest.mark.parametrize("patch,msg", [
    ({"objects": []}, "Got 0 objects"),
    ({"objects": None}, "either prompt_type"),                                       # neither form
    ({"prompt_type": "mask", "prompt_points": [1.0]}, "either prompt_type"),         # both forms
    ({"objects": [{"id": 1, "prompt_type": "mask", "prompt_points": [0.0]},
                  {"id": 1, "prompt_type": "mask", "prompt_points": [0.0]}]}, "unique"),
    ({"objects": [{"id": 3, "prompt_type": "polygon", "prompt_points": [90.0, 90.0, 99.0, 90.0, 99.0, 99.0]}]},
     "object 3"),                                                                   # the message names the object
])
def test_multi_object_validation(setup, patch, msg):
    c, backend, cvat = setup
    r = c.post("/v1/video/propagate-job", json=multi_body(**patch))
    assert r.status_code == 400 and msg in r.text, r.text
    assert backend.stats.propagations_run == 0


def test_partial_single_prompt_is_rejected(setup):
    c, *_ = setup
    b = body()
    del b["prompt_points"]
    assert c.post("/v1/video/propagate-job", json=b).status_code == 400


def test_too_many_objects(setup, monkeypatch):
    from app import main
    c, *_ = setup
    monkeypatch.setattr(main, "MAX_OBJECTS", 1)
    r = c.post("/v1/video/propagate-job", json=multi_body())
    assert r.status_code == 400 and "allowed range is 1..1" in r.text
    assert c.get("/healthz").json()["max_objects"] == 1


def test_back_to_back_multi_runs_never_hit_429(setup):
    c, *_ = setup
    for _ in range(10):
        r = c.post("/v1/video/propagate-job", json=multi_body(to_frame=7))
        assert r.status_code == 200 and lines(r)[-1]["done"] is True
