"""Sam2VideoBackend.propagate_multi against a stand-in for SAM 2's video predictor.

No torch/GPU here, so the predictor and torch are replaced by minimal fakes that mimic the SAM 2 behaviours the
backend relies on (verified in the sam2 source): objects must all be added before tracking starts, and
propagate_in_video yields (frame_idx, obj_ids, masks) with one mask per object IN obj_ids ORDER."""
import contextlib
import sys
import threading
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "sam-service"))
sys.path.insert(0, str(ROOT))

from app.backends import BackendStats, Sam2VideoBackend  # noqa: E402

H, W = 8, 12


class FakeTensor:
    """Just enough of a torch tensor for `(logits[k, 0] > 0.0).cpu().numpy()`."""

    def __init__(self, a):
        self.a = np.asarray(a)

    def __getitem__(self, i):
        return FakeTensor(self.a[i])

    def __gt__(self, v):
        return FakeTensor(self.a > v)

    def cpu(self):
        return self

    def numpy(self):
        return self.a


class FakeTorch:
    @staticmethod
    def inference_mode():
        return contextlib.nullcontext()

    @staticmethod
    def autocast(*a, **k):
        return contextlib.nullcontext()

    @staticmethod
    def from_numpy(x):
        return x


class FakeVideoPredictor:
    """Object k's mask on frame f is a column at x = f + k, so a swapped obj_ids order is detectable."""

    def __init__(self):
        self.log, self.added, self.started, self.reset_called = [], [], False, False

    def init_state(self, **kw):
        self.log.append(("init", kw["video_path"], kw["offload_video_to_cpu"]))
        return {"n": 4}

    def add_new_mask(self, state, frame_idx, obj_id, mask):
        assert not self.started, "SAM 2 only accepts new objects before tracking starts"
        self.added.append((frame_idx, obj_id, int(mask.sum())))

    def propagate_in_video(self, state, start_frame_idx):
        self.started = True
        obj_ids = [oid for _, oid, _ in self.added]
        for f in range(start_frame_idx, state["n"]):
            logits = np.full((len(obj_ids), 1, H, W), -5.0)
            for k, oid in enumerate(obj_ids):
                logits[k, 0, :, f + oid] = 5.0
            yield f, obj_ids, FakeTensor(logits)

    def reset_state(self, state):
        self.reset_called = True


def make_backend():
    b = object.__new__(Sam2VideoBackend)  # bypass __init__: it would import torch and load weights
    b.stats = BackendStats()
    b._torch, b._device = FakeTorch, "cpu"
    b._video_predictor, b._video_lock = FakeVideoPredictor(), threading.Lock()
    return b


def prompt(n_pixels):
    m = np.zeros((H, W), bool)
    m.flat[:n_pixels] = True
    return m


def test_all_objects_share_one_pass_and_masks_map_to_the_right_object():
    b = make_backend()
    out = list(b.propagate_multi("/frames", 0, {1: prompt(3), 6: prompt(5)}, threading.Event()))
    vp = b._video_predictor
    assert vp.log == [("init", "/frames", True)]                       # frames loaded ONCE for both objects
    assert [(o, n) for _, o, n in vp.added] == [(1, 3), (6, 5)]         # both added before tracking
    assert len(out) == 4 * 2 and b.stats.propagations_run == 1 and b.stats.frames_propagated == 4
    for idx, obj, mask in out:
        cols = np.flatnonzero(mask.any(axis=0)).tolist()
        assert cols == [idx + obj], f"frame {idx} object {obj}: mask belongs to another object"
    assert vp.reset_called


def test_cancel_stops_and_still_resets_the_state():
    b, cancel = make_backend(), threading.Event()
    gen = b.propagate_multi("/frames", 0, {1: prompt(2), 2: prompt(2)}, cancel)
    first = [next(gen), next(gen)]                                        # frame 0 for both objects
    cancel.set()
    assert list(gen) == []                                                # stops at the next frame
    assert [f for f, _, _ in first] == [0, 0] and b._video_predictor.reset_called


def test_state_is_reset_even_if_the_predictor_fails():
    b = make_backend()

    def boom(state, start_frame_idx):
        raise RuntimeError("CUDA out of memory")
        yield  # pragma: no cover

    b._video_predictor.propagate_in_video = boom
    with pytest.raises(RuntimeError, match="out of memory"):
        list(b.propagate_multi("/frames", 0, {1: prompt(2)}, threading.Event()))
    assert b._video_predictor.reset_called
