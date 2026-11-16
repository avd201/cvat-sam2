"""The propagation queue as seen over HTTP (FakeBackend; `backend.hold` keeps a run on the GPU slot on demand)."""
import io
import json
import socket
import sys
import threading
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
from app.gpu_queue import FifoSlot  # noqa: E402
from app.main import app  # noqa: E402
from sam2common.rle import mask_to_cvat_points  # noqa: E402

H, W = 40, 60


def jpeg(seed=0) -> bytes:
    arr = np.random.default_rng(seed).integers(0, 255, (H, W, 3), dtype=np.uint8)
    buf = io.BytesIO()
    Image.fromarray(arr).save(buf, format="JPEG")
    return buf.getvalue()


def meta() -> str:
    m = np.zeros((H, W), bool)
    m[10:20, 0:10] = True
    return json.dumps({"prompt_index": 0, "height": H, "width": W, "prompt_points": mask_to_cvat_points(m)})


def files(n=3):
    return [("frames", (f"{i:05d}.jpg", jpeg(i), "image/jpeg")) for i in range(n)]


def lines(resp):
    return [json.loads(x) for x in resp.text.splitlines() if x]


def until(cond, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


@pytest.fixture()
def env():
    backend = FakeBackend()
    app.state.backend, app.state.gpu_queue = backend, None
    with TestClient(app) as c:
        yield c, backend
    app.state.backend = None


def post_in_thread(c, out, key):
    def run():
        r = c.post("/v1/video/propagate", data={"meta": meta()}, files=files())
        out[key] = (r.status_code, lines(r) if r.status_code == 200 else r.text)

    th = threading.Thread(target=run)
    th.start()
    return th


def test_waiting_runs_are_queued_in_order_and_told_their_position(env):
    c, backend = env
    backend.hold = threading.Event()
    out: dict = {}
    a = post_in_thread(c, out, "a")
    assert until(lambda: backend.runs_started == 1)                     # a holds the GPU slot
    b = post_in_thread(c, out, "b")
    assert until(lambda: c.get("/healthz").json()["queue_waiting"] == 1)
    cc = post_in_thread(c, out, "c")
    assert until(lambda: c.get("/healthz").json()["queue_waiting"] == 2)
    h = c.get("/healthz").json()
    assert h["propagation_busy"] is True and h["queue_max"] == service_main.MAX_QUEUE
    backend.hold.set()
    for th in (a, b, cc):
        th.join(15)
    assert all(out[k][0] == 200 for k in "abc")
    for k in "abc":
        assert out[k][1][-1]["done"] is True                             # everybody finishes normally
    assert not any("queued" in m for m in out["a"][1])                   # an idle service sends no queue lines
    assert out["b"][1][0] == {"queued": 1}                               # b: one run (a) ahead
    assert out["c"][1][0] == {"queued": 2}                               # c: a and b ahead
    # (later position changes are only reported on the next poll, at most 1 s apart; with instant fake runs
    #  c's turn simply comes first, so no further queue line is asserted here)
    assert backend.runs_started == 3
    h = c.get("/healthz").json()
    assert h["propagation_busy"] is False and h["queue_waiting"] == 0


def test_queue_full_is_429_and_a_place_frees_up_afterwards(env):
    c, backend = env
    app.state.gpu_queue = FifoSlot(max_waiting=1)
    backend.hold = threading.Event()
    out: dict = {}
    a = post_in_thread(c, out, "a")
    assert until(lambda: backend.runs_started == 1)
    b = post_in_thread(c, out, "b")
    assert until(lambda: c.get("/healthz").json()["queue_waiting"] == 1)
    r = c.post("/v1/video/propagate", data={"meta": meta()}, files=files())
    assert r.status_code == 429 and "queue is full" in r.text
    backend.hold.set()
    a.join(15)
    b.join(15)
    assert out["a"][0] == out["b"][0] == 200
    assert c.post("/v1/video/propagate", data={"meta": meta()}, files=files()).status_code == 200


def test_bad_request_never_takes_or_keeps_a_queue_place(env):
    c, _ = env
    bad = c.post("/v1/video/propagate", data={"meta": meta()}, files=[("frames", ("0.jpg", b"not a jpeg", "image/jpeg"))])
    assert bad.status_code == 400
    h = c.get("/healthz").json()
    assert h["queue_waiting"] == 0 and h["propagation_busy"] is False


def test_out_of_memory_midway_gives_a_clear_message_and_the_service_recovers(env):
    c, backend = env
    backend.oom_at_frame = 2
    msgs = lines(c.post("/v1/video/propagate", data={"meta": meta()}, files=files(5)))
    err = msgs[-1]["error"]
    assert "ran out of memory" in err and "5 frames" in err and "1 object" in err and "Nothing was changed" in err
    assert [m["frame"] for m in msgs if "frame" in m] == [0, 1]
    h = c.get("/healthz").json()
    assert h["propagation_busy"] is False and h["queue_waiting"] == 0
    backend.oom_at_frame = None
    assert lines(c.post("/v1/video/propagate", data={"meta": meta()}, files=files(3)))[-1]["done"] is True


def test_a_failure_that_is_not_oom_keeps_the_generic_message(env):
    c, backend = env
    backend.fail_at_frame = 1
    err = lines(c.post("/v1/video/propagate", data={"meta": meta()}, files=files(4)))[-1]["error"]
    assert "ran out of memory" not in err and "see service logs" in err


def test_healthz_has_queue_and_gpu_fields(env):
    c, _ = env
    h = c.get("/healthz").json()
    assert {"propagation_busy", "queue_waiting", "queue_max", "gpu"} <= set(h) and h["gpu"] == {}  # fake: no GPU


# ---------------------------------------------------------------- real server: a client that disconnects while waiting
def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_client_that_disconnects_while_waiting_gives_its_place_back(monkeypatch):
    uvicorn = pytest.importorskip("uvicorn")
    httpx = pytest.importorskip("httpx")
    monkeypatch.setattr(service_main, "QUEUE_HEARTBEAT_S", 0.2)
    backend = FakeBackend()
    backend.hold = threading.Event()
    app.state.backend, app.state.gpu_queue = backend, None
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    base = f"http://127.0.0.1:{port}"
    try:
        assert until(lambda: server.started)
        out: dict = {}

        def first():  # holds the slot until `hold` is set
            r = httpx.post(base + "/v1/video/propagate", data={"meta": meta()}, files=files(), timeout=30)
            out["a"] = r.status_code

        ta = threading.Thread(target=first)
        ta.start()
        assert until(lambda: backend.runs_started == 1)

        with httpx.stream("POST", base + "/v1/video/propagate", data={"meta": meta()}, files=files(), timeout=30) as r:
            first_line = next(r.iter_lines())
            assert json.loads(first_line) == {"queued": 1}
            assert httpx.get(base + "/healthz").json()["queue_waiting"] == 1
        # leaving the `with` closed the connection: the queue place must come back without any help
        assert until(lambda: httpx.get(base + "/healthz").json()["queue_waiting"] == 0, timeout=15)

        backend.hold.set()
        ta.join(15)
        assert out["a"] == 200
        assert backend.runs_started == 1                                  # the abandoned request never ran
        h = httpx.get(base + "/healthz").json()
        assert h["propagation_busy"] is False
    finally:
        backend.hold.set()
        server.should_exit = True
        th.join(10)
        app.state.backend = None
