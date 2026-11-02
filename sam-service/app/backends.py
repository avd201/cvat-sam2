"""Model backends. The HTTP layer only talks to `SegmentationBackend`, so SAM 2 can later be
swapped for another model by adding a class here (Stage 1 only needs single-image interaction;
video methods are added in Stage 2)."""
from __future__ import annotations

import abc
import logging
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Callable, Iterator, Sequence

import numpy as np

log = logging.getLogger("sam2service.backend")

Point = Sequence[float]


@dataclass
class InteractResult:
    mask: np.ndarray  # bool, HxW, full image resolution
    score: float


@dataclass
class BackendStats:
    model_loads: int = 0
    requests_served: int = 0
    embedding_cache_hits: int = 0
    embedding_cache_misses: int = 0
    propagations_run: int = 0
    frames_propagated: int = 0
    extra: dict = field(default_factory=dict)


class SegmentationBackend(abc.ABC):
    name = "abstract"

    def __init__(self) -> None:
        self.stats = BackendStats()

    @abc.abstractmethod
    def interact(
        self,
        image_key: str,
        get_image: Callable[[], np.ndarray],
        pos_points: list[Point],
        neg_points: list[Point],
        box: list[float] | None,
    ) -> InteractResult:
        """`image_key` identifies the image content; `get_image` decodes it (RGB uint8 HxWx3)
        and is only called on an embedding-cache miss."""

    def propagate(
        self,
        frames_dir: str,
        prompt_index: int,
        prompt_mask: np.ndarray,
        cancel: threading.Event,
    ) -> Iterator[tuple[int, np.ndarray]]:
        """Track the object given by `prompt_mask` (bool HxW) on frame `prompt_index` of the JPEG
        sequence in `frames_dir` (files `00000.jpg`, `00001.jpg`, ...) through all following frames.
        Yields (frame_index, bool mask) for every frame from `prompt_index` on, in order.
        MUST be consumed entirely from ONE thread (torch contexts are thread-local).
        Stops early when `cancel` is set."""
        raise NotImplementedError(f"Backend {self.name!r} does not support video propagation")

    def propagate_multi(
        self,
        frames_dir: str,
        prompt_index: int,
        prompts: dict[int, np.ndarray],
        cancel: threading.Event,
    ) -> Iterator[tuple[int, int, np.ndarray]]:
        """Track several objects (obj_id -> bool prompt mask on frame `prompt_index`) through the same frames.
        Yields (frame_index, obj_id, bool mask). The default runs `propagate` once per object, so ANY backend
        supports it (correct, but frames are processed once per object); backends that can track objects together
        override it. Same threading rules as `propagate`."""
        for obj_id, mask in prompts.items():
            for idx, out in self.propagate(frames_dir, prompt_index, mask, cancel):
                yield idx, obj_id, out


def _validate(pos_points, neg_points, box) -> None:
    if not pos_points and not neg_points and box is None:
        raise ValueError("At least one prompt (point or box) is required")
    if not pos_points and box is None:
        raise ValueError("Negative points alone are not a valid prompt: add a positive point or a box")


class FakeBackend(SegmentationBackend):
    """Deterministic CPU backend for tests / plumbing checks (no model, no GPU).
    Mask = box if a box is given, plus a disc of radius 20 around each positive point,
    minus a disc around each negative point."""

    name = "fake"

    def __init__(self, cache_size: int = 4, fail_at_frame: int | None = None) -> None:
        super().__init__()
        self.stats.model_loads = 1  # "loading" happens exactly once, at construction
        self.fail_at_frame = fail_at_frame  # tests: raise when this frame index is reached
        self.last_frames_dir: str | None = None
        self._cache: OrderedDict[str, tuple[int, int]] = OrderedDict()
        self._cache_size = cache_size
        self._lock = threading.Lock()

    def interact(self, image_key, get_image, pos_points, neg_points, box):
        _validate(pos_points, neg_points, box)
        with self._lock:
            self.stats.requests_served += 1
            if image_key in self._cache:
                self._cache.move_to_end(image_key)
                self.stats.embedding_cache_hits += 1
            else:
                self.stats.embedding_cache_misses += 1
                h, w = get_image().shape[:2]
                self._cache[image_key] = (h, w)
                while len(self._cache) > self._cache_size:
                    self._cache.popitem(last=False)
            h, w = self._cache[image_key]
            yy, xx = np.mgrid[0:h, 0:w]
            mask = np.zeros((h, w), dtype=bool)
            if box is not None:
                x1, y1, x2, y2 = (int(round(v)) for v in box)
                mask[y1 : y2 + 1, x1 : x2 + 1] = True
            for x, y in pos_points:
                mask |= (xx - x) ** 2 + (yy - y) ** 2 <= 20**2
            for x, y in neg_points:
                mask &= ~((xx - x) ** 2 + (yy - y) ** 2 <= 20**2)
            return InteractResult(mask=mask, score=1.0)

    def propagate(self, frames_dir, prompt_index, prompt_mask, cancel):
        """Fake tracker: the prompt mask moves right by 2 px per frame; once it has left the image
        the mask is empty (exercises the 'object disappeared' path)."""
        import os

        self.last_frames_dir = frames_dir
        n = len([f for f in os.listdir(frames_dir) if f.endswith(".jpg")])
        h, w = prompt_mask.shape
        self.stats.propagations_run += 1
        for idx in range(prompt_index, n):
            if cancel.is_set():
                return
            if self.fail_at_frame is not None and idx == self.fail_at_frame:
                raise RuntimeError("simulated inference failure")
            shift = (idx - prompt_index) * 2
            out = np.zeros_like(prompt_mask)
            if shift < w:
                out[:, shift:] = prompt_mask[:, : w - shift]
            self.stats.frames_propagated += 1
            yield idx, out


class Sam2ImageBackend(SegmentationBackend):
    """SAM 2 / 2.1 single-image interactive segmentation (SAM2ImagePredictor).

    The model is built once in __init__. The image embedding is cached (LRU, keyed by image
    content hash) so repeated clicks on the same frame skip the expensive image encoder.
    NOTE: the cache restores the predictor's private attributes (_features, _orig_hw,
    _is_image_set) because SAM2ImagePredictor has no public API for exporting/importing an
    embedding. This is technical debt: re-check on any sam2 upgrade (pin the commit).
    """

    name = "sam2-image"

    def __init__(self, config: str, checkpoint: str, device: str = "cuda", cache_size: int = 4) -> None:
        super().__init__()
        import torch
        from sam2.build_sam import build_sam2
        from sam2.sam2_image_predictor import SAM2ImagePredictor

        self._torch = torch
        self._device = device
        log.info("Loading SAM 2: config=%s checkpoint=%s device=%s", config, checkpoint, device)
        model = build_sam2(config, checkpoint, device=device)
        self._predictor = SAM2ImagePredictor(model)
        self.stats.model_loads += 1
        self._cache: OrderedDict[str, dict] = OrderedDict()
        self._cache_size = cache_size
        self._lock = threading.Lock()  # one GPU job at a time

    def _autocast(self):
        if self._device.startswith("cuda"):
            return self._torch.autocast("cuda", dtype=self._torch.bfloat16)
        return self._torch.autocast("cpu", enabled=False)

    def interact(self, image_key, get_image, pos_points, neg_points, box):
        _validate(pos_points, neg_points, box)
        torch, p = self._torch, self._predictor
        with self._lock, torch.inference_mode(), self._autocast():
            self.stats.requests_served += 1
            cached = self._cache.get(image_key)
            if cached is not None:
                self._cache.move_to_end(image_key)
                self.stats.embedding_cache_hits += 1
                p._features, p._orig_hw, p._is_image_set = cached["features"], cached["orig_hw"], True
                p._is_batch = False
            else:
                self.stats.embedding_cache_misses += 1
                p.set_image(get_image())
                self._cache[image_key] = {"features": p._features, "orig_hw": p._orig_hw}
                while len(self._cache) > self._cache_size:
                    self._cache.popitem(last=False)

            has_pts = bool(pos_points or neg_points)
            coords = np.array([*pos_points, *neg_points], dtype=np.float32) if has_pts else None
            labels = (
                np.array([1] * len(pos_points) + [0] * len(neg_points), dtype=np.int32) if has_pts else None
            )
            # Multimask output is only useful for a single ambiguous click.
            multimask = has_pts and len(coords) == 1 and box is None
            masks, scores, _ = p.predict(
                point_coords=coords,
                point_labels=labels,
                box=np.asarray(box, dtype=np.float32) if box is not None else None,
                multimask_output=multimask,
            )
            best = int(np.argmax(scores))
            return InteractResult(mask=masks[best] > 0.5, score=float(scores[best]))


class Sam2VideoBackend(Sam2ImageBackend):
    """Image interaction (inherited, unchanged from Stage 1) + SAM 2 video propagation.

    Design: propagation is STATELESS per call. CVAT is the source of truth; each call receives the
    frame range plus one mask prompt, builds a SAM 2 inference state, tracks forward, streams the
    masks out and frees the state. Re-propagating from frame N after a correction is therefore just
    a new call starting at N: earlier frames are neither recomputed nor needed.

    The video model is a second SAM2VideoPredictor instance with its own copy of the weights
    (~0.9 GB for Hiera-Large). Sharing one model between the image and video paths is possible but
    was not done, to avoid any risk to the Stage 1 path (technical debt).
    """

    name = "sam2"

    def __init__(self, config: str, checkpoint: str, device: str = "cuda", cache_size: int = 4) -> None:
        super().__init__(config, checkpoint, device, cache_size)
        from sam2.build_sam import build_sam2_video_predictor

        torch = self._torch
        if device.startswith("cuda") and torch.cuda.get_device_properties(0).major >= 8:
            # As in the official SAM 2 notebooks, for Ampere or newer GPUs.
            torch.backends.cuda.matmul.allow_tf32 = True
            torch.backends.cudnn.allow_tf32 = True
        log.info("Loading SAM 2 video predictor")
        self._video_predictor = build_sam2_video_predictor(config, checkpoint, device=device)
        self.stats.model_loads += 1
        self._video_lock = threading.Lock()

    def propagate(self, frames_dir, prompt_index, prompt_mask, cancel):
        torch, vp = self._torch, self._video_predictor
        with self._video_lock, torch.inference_mode(), self._autocast():
            self.stats.propagations_run += 1
            state = vp.init_state(
                video_path=frames_dir,
                offload_video_to_cpu=True,  # frames stay in CPU RAM; GPU holds only memory features
                offload_state_to_cpu=False,
                async_loading_frames=False,
            )
            try:
                vp.add_new_mask(state, frame_idx=prompt_index, obj_id=1, mask=torch.from_numpy(prompt_mask))
                for idx, _obj_ids, logits in vp.propagate_in_video(state, start_frame_idx=prompt_index):
                    if cancel.is_set():
                        return
                    self.stats.frames_propagated += 1
                    yield idx, (logits[0, 0] > 0.0).cpu().numpy()
            finally:
                vp.reset_state(state)
                del state
                if self._device.startswith("cuda"):
                    torch.cuda.empty_cache()

    def propagate_multi(self, frames_dir, prompt_index, prompts, cancel):
        """All objects share ONE inference state: the frames are loaded once and every object is tracked in the
        same pass, each with its own memory (so they do not influence each other). SAM 2 requires every object to
        be added before tracking starts, which is what is done here."""
        torch, vp = self._torch, self._video_predictor
        with self._video_lock, torch.inference_mode(), self._autocast():
            self.stats.propagations_run += 1
            state = vp.init_state(
                video_path=frames_dir,
                offload_video_to_cpu=True,
                offload_state_to_cpu=False,
                async_loading_frames=False,
            )
            try:
                for obj_id, mask in prompts.items():
                    vp.add_new_mask(state, frame_idx=prompt_index, obj_id=obj_id, mask=torch.from_numpy(mask))
                for idx, obj_ids, logits in vp.propagate_in_video(state, start_frame_idx=prompt_index):
                    if cancel.is_set():
                        return
                    self.stats.frames_propagated += 1
                    for k, obj_id in enumerate(obj_ids):  # logits: [len(obj_ids), 1, H, W], in obj_ids order
                        yield idx, obj_id, (logits[k, 0] > 0.0).cpu().numpy()
            finally:
                vp.reset_state(state)
                del state
                if self._device.startswith("cuda"):
                    torch.cuda.empty_cache()
