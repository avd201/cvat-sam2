"""Stage 1 tests (CPU, FakeBackend). Run from repo root:  pytest -q tests"""
import base64
import io
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
from sam2common.rle import cvat_points_to_mask, mask_to_cvat_points, mask_to_rle  # noqa: E402


def png_b64(w=120, h=80, seed=0) -> str:
    arr = np.random.default_rng(seed).integers(0, 255, (h, w, 3), dtype=np.uint8)
    buf = io.BytesIO()
    Image.fromarray(arr).save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


@pytest.fixture()
def client():
    app.state.backend = FakeBackend(cache_size=2)
    app.state.propagation_lock = None  # recreated by the lifespan
    with TestClient(app) as c:
        yield c
    app.state.backend = None


def test_rle_matches_cvat_examples():
    # Examples from cvat-core/src/rle-utils.ts docstring
    assert mask_to_rle(np.array([0, 0, 0, 1])) == [3, 1]
    assert mask_to_rle(np.array([1, 1, 0, 0])) == [0, 2, 2]


def test_rle_roundtrip_random():
    rng = np.random.default_rng(1)
    for _ in range(20):
        m = rng.random((37, 53)) > 0.7
        if not m.any():
            continue
        assert np.array_equal(cvat_points_to_mask(mask_to_cvat_points(m), 37, 53), m)


def test_positive_and_negative_points(client):
    img = png_b64()
    r = client.post("/v1/interact", json={"image": img, "pos_points": [[60, 40]]})
    assert r.status_code == 200
    m1 = cvat_points_to_mask(r.json()["shapes"][0]["points"], 80, 120)
    assert m1[40, 60] and m1.sum() > 1000
    r = client.post("/v1/interact", json={"image": img, "pos_points": [[60, 40]], "neg_points": [[60, 40]]})
    assert r.json()["shapes"] == []  # fully removed -> empty result, not an error


def test_box_prompt(client):
    r = client.post("/v1/interact", json={"image": png_b64(), "obj_bbox": [[10, 10], [30, 20]]})
    m = cvat_points_to_mask(r.json()["shapes"][0]["points"], 80, 120)
    assert m.sum() == 21 * 11


def test_model_loaded_once_and_embedding_reused(client):
    a, b, c = png_b64(seed=1), png_b64(seed=2), png_b64(seed=3)
    for _ in range(5):  # five clicks on the same frame
        assert client.post("/v1/interact", json={"image": a, "pos_points": [[5, 5]]}).status_code == 200
    h = client.get("/healthz").json()
    assert h["model_loads"] == 1
    assert h["embedding_cache_misses"] == 1 and h["embedding_cache_hits"] == 4
    client.post("/v1/interact", json={"image": b, "pos_points": [[5, 5]]})
    client.post("/v1/interact", json={"image": c, "pos_points": [[5, 5]]})  # evicts `a` (cache_size=2)
    client.post("/v1/interact", json={"image": a, "pos_points": [[5, 5]]})
    h = client.get("/healthz").json()
    assert h["model_loads"] == 1 and h["embedding_cache_misses"] == 4


@pytest.mark.parametrize(
    "body",
    [
        {"image": "!!notbase64!!", "pos_points": [[1, 1]]},
        {"image": "AAAA"},  # no prompts (valid base64, never decoded as image)
        {"image": "AAAA", "neg_points": [[1, 1]]},  # negatives alone
        {"image": "AAAA", "obj_bbox": [[1, 1]]},  # incomplete box
    ],
)
def test_bad_requests_are_400(client, body):
    assert client.post("/v1/interact", json=body).status_code == 400
