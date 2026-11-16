"""Persistent local segmentation service (Stage 1: interactive single-image segmentation).

The backend (and therefore the model) is created ONCE in the FastAPI lifespan and lives for the
life of the process. Run with exactly ONE uvicorn worker: model + caches are per-process.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import io
import json
import logging
import os
import queue
import shutil
import tempfile
import threading
import time
from contextlib import asynccontextmanager

import numpy as np
from concurrent.futures import ThreadPoolExecutor

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse
from PIL import Image
from pydantic import BaseModel, Field, ValidationError

from sam2common.polygon import mask_to_polygon, polygon_to_mask
from sam2common.rle import cvat_points_to_mask, mask_to_cvat_points
from sam2common.roi import MAX_LINES as MAX_ROI_LINES, Roi, RoiError

from .cvat_frames import CvatClient, CvatError
from .gpu_queue import FifoSlot, QueueFull, Ticket, TicketExpired
from .backends import FakeBackend, Sam2ImageBackend, Sam2VideoBackend, SegmentationBackend

MAX_PROPAGATION_FRAMES = int(os.environ.get("MAX_PROPAGATION_FRAMES", "600"))
CONSUMER_STALL_TIMEOUT_S = float(os.environ.get("CONSUMER_STALL_TIMEOUT_S", "120"))
CVAT_INTERNAL_URL = os.environ.get("CVAT_INTERNAL_URL", "http://cvat_server:8080")
MAX_OBJECTS = int(os.environ.get("MAX_OBJECTS", "8"))  # objects tracked together in one propagate-job call
CVAT_HOST_HEADER = os.environ.get("CVAT_HOST_HEADER", "localhost")  # must be a valid domain name (no underscore)
MAX_QUEUE = int(os.environ.get("MAX_QUEUE", "8"))  # runs allowed to WAIT behind the running one (more -> HTTP 429)
QUEUE_STALE_S = float(os.environ.get("QUEUE_STALE_S", "300"))  # a waiting request nobody polls for this long is dropped
QUEUE_HEARTBEAT_S = float(os.environ.get("QUEUE_HEARTBEAT_S", "3"))  # {"queued": n} is repeated this often while waiting
_limit = os.environ.get("GPU_MEMORY_LIMIT_GB", "").strip()
GPU_MEMORY_LIMIT_GB = float(_limit) if _limit else None  # optional cap on PyTorch's GPU memory (emulate a smaller card)

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
log = logging.getLogger("sam2service")


def build_backend() -> SegmentationBackend:
    kind = os.environ.get("SEG_BACKEND", "sam2")
    cache = int(os.environ.get("EMBEDDING_CACHE_SIZE", "4"))
    if kind == "fake":
        return FakeBackend(cache_size=cache)
    if kind in ("sam2", "sam2-image"):  # "sam2-image" = Stage 1 behaviour (no video model loaded)
        cls = Sam2VideoBackend if kind == "sam2" else Sam2ImageBackend
        return cls(
            config=os.environ.get("SAM2_CONFIG", "configs/sam2.1/sam2.1_hiera_l.yaml"),
            checkpoint=os.environ.get("SAM2_CHECKPOINT", "/checkpoints/sam2.1_hiera_large.pt"),
            device=os.environ.get("DEVICE", "cuda"),
            cache_size=cache,
            memory_limit_gb=GPU_MEMORY_LIMIT_GB,
        )
    raise RuntimeError(f"Unknown SEG_BACKEND={kind!r}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    if getattr(app.state, "backend", None) is None:  # tests may inject one
        app.state.backend = build_backend()
    if getattr(app.state, "cvat", None) is None:
        app.state.cvat = CvatClient(CVAT_INTERNAL_URL, host_header=CVAT_HOST_HEADER)
    if getattr(app.state, "gpu_queue", None) is None:
        # ONE propagation runs at a time; the others wait here in arrival order (see _stream_propagation)
        app.state.gpu_queue = FifoSlot(max_waiting=MAX_QUEUE, stale_s=QUEUE_STALE_S)
    log.info("Backend ready: %s", app.state.backend.name)
    yield


app = FastAPI(title="cvat-sam2 segmentation service", lifespan=lifespan)


class InteractRequest(BaseModel):
    image: str  # base64 (what CVAT sends to Nuclio)
    pos_points: list[list[float]] = []
    neg_points: list[list[float]] = []
    obj_bbox: list[list[float]] | None = None  # flat list of corner pairs, 2 per rectangle


@app.get("/healthz")
def healthz():
    b: SegmentationBackend = app.state.backend
    s = b.stats
    return {
        "status": "ok",
        "backend": b.name,
        "model_loads": s.model_loads,
        "requests_served": s.requests_served,
        "embedding_cache_hits": s.embedding_cache_hits,
        "embedding_cache_misses": s.embedding_cache_misses,
        "propagations_run": s.propagations_run,
        "frames_propagated": s.frames_propagated,
        "propagation_busy": app.state.gpu_queue.busy,
        "queue_waiting": app.state.gpu_queue.waiting,
        "queue_max": MAX_QUEUE,
        "gpu": b.memory_info(),  # {} for backends without a GPU
        "max_propagation_frames": MAX_PROPAGATION_FRAMES,
        "max_objects": MAX_OBJECTS,
        "outputs": ["mask", "polygon"],  # formats /v1/video/propagate can produce (absent in older versions)
        "roi_max_lines": MAX_ROI_LINES,  # region-of-interest support (absent in older versions)
    }


@app.post("/v1/interact")
def interact(req: InteractRequest):
    try:
        raw = base64.b64decode(req.image, validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(400, "`image` is not valid base64")
    key = hashlib.sha256(raw).hexdigest()

    def get_image() -> np.ndarray:
        return np.array(Image.open(io.BytesIO(raw)).convert("RGB"))

    box = None
    if req.obj_bbox:
        if len(req.obj_bbox) < 2:
            raise HTTPException(400, "obj_bbox must contain two corner points per rectangle")
        if len(req.obj_bbox) > 2:
            log.warning("%d rectangles received; only the first is used", len(req.obj_bbox) // 2)
        (x1, y1), (x2, y2) = req.obj_bbox[0], req.obj_bbox[1]
        box = [min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2)]

    try:
        res = app.state.backend.interact(key, get_image, req.pos_points, req.neg_points, box)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:  # noqa: BLE001
        log.exception("Inference failed")
        if app.state.backend.is_out_of_memory(e):
            raise HTTPException(503, "The GPU is out of memory; try again in a moment (a propagation may be running)")
        raise HTTPException(500, "Inference failed; see service logs")

    points = mask_to_cvat_points(res.mask)
    if points is None:
        return {"shapes": [], "score": res.score}
    return {"shapes": [{"type": "mask", "points": points, "attributes": []}], "score": res.score}


class PropagateMeta(BaseModel):
    prompt_index: int  # index (within the uploaded frames) of the frame holding the prompt mask
    height: int
    width: int
    prompt_points: list[int]  # CVAT mask `points`: RLE of the bbox crop + [xtl, ytl, xbr, ybr]
    output: str = Field("mask", pattern="^(mask|polygon)$")  # per-frame result format
    simplify_px: float = Field(1.5, ge=0, le=20)  # polygon output: Douglas-Peucker tolerance in pixels
    roi_lines: list[list[float]] | None = None  # region of interest, see sam2common/roi.py


def _parse_roi(lines: list[list[float]] | None) -> Roi | None:
    """None / [] = no region of interest. Invalid definitions are a 400 with the reason."""
    if not lines:
        return None
    try:
        return Roi(lines)
    except RoiError as e:
        raise HTTPException(400, f"Invalid ROI: {e}")


def _check_prompts_inside(roi: Roi | None, prompts: dict[int, np.ndarray], single: bool) -> None:
    """Refuse (400) when a prompt mask starts outside the region of interest: it would be dropped at once and
    produce an empty track, which is never what the annotator wants. Nothing is changed in the job."""
    if roi is None:
        return
    bad = []
    for obj, mask in prompts.items():
        c = Roi.centroid(mask)
        line = None if c is None else roi.excluding_line(*c)
        if line is not None:
            bad.append((obj, line))
    if not bad:
        return
    if single:
        raise HTTPException(400, f"The prompt starts outside the region of interest (its centroid is on the excluded "
                                 f"side of ROI line {bad[0][1]}); nothing to track")
    detail = ", ".join(f"object {o} (line {ln})" for o, ln in bad)
    raise HTTPException(400, f"{len(bad)} object(s) start outside the region of interest, centroid on the excluded "
                             f"side of the given line: {detail}. Deselect them or fix the ROI; nothing was changed")


def _ndjson(obj: dict) -> bytes:
    return (json.dumps(obj, separators=(",", ":")) + "\n").encode()


@app.post("/v1/video/propagate")
def propagate(meta: str = Form(...), frames: list[UploadFile] = File(...)):
    """Stateless propagation. Multipart body: `meta` (JSON, see PropagateMeta) and `frames`
    (JPEG files, in temporal order; their order defines the frame indices 0..N-1).

    Response: application/x-ndjson, one line per frame as soon as it is computed:
      {"frame": <index>, "points": <CVAT mask points | flat polygon [x1,y1,...]> | null}
      (null = object not found; for output="polygon" also when no usable contour exists)
    terminated by {"done": true, "frames": N} or, on failure, {"error": "..."}.
    Only one propagation runs at a time; the others wait in a FIFO queue (see _stream_propagation).
    """
    backend: SegmentationBackend = app.state.backend
    try:
        m = PropagateMeta.model_validate_json(meta)
    except ValidationError as e:
        raise HTTPException(400, f"Invalid meta: {e.errors()[:3]}")
    n = len(frames)
    if not 1 <= n <= MAX_PROPAGATION_FRAMES:
        raise HTTPException(400, f"Got {n} frames; allowed range is 1..{MAX_PROPAGATION_FRAMES}")
    if not 0 <= m.prompt_index < n:
        raise HTTPException(400, "prompt_index is outside the uploaded frames")
    if len(m.prompt_points) < 5:
        raise HTTPException(400, "prompt_points is not a valid CVAT mask")
    xtl, ytl, xbr, ybr = m.prompt_points[-4:]
    if not (0 <= xtl <= xbr < m.width and 0 <= ytl <= ybr < m.height):
        raise HTTPException(400, "prompt mask bounding box is outside the image")
    try:
        prompt_mask = cvat_points_to_mask(m.prompt_points, m.height, m.width)
    except Exception:  # noqa: BLE001  (inconsistent RLE length etc.)
        raise HTTPException(400, "prompt_points RLE is inconsistent with its bounding box")
    if not prompt_mask.any():
        raise HTTPException(400, "prompt mask is empty")
    roi = _parse_roi(m.roi_lines)
    _check_prompts_inside(roi, {1: prompt_mask}, single=True)

    # Persist the frames. JPEG only (what SAM 2's loader reads); sizes must match the prompt.
    ticket = _join_queue()  # from here on EVERY exit path must give the ticket back
    tmpdir = tempfile.mkdtemp(prefix="sam2frames_")
    try:
        for i, f in enumerate(frames):
            data = f.file.read()
            if data[:2] != b"\xff\xd8":
                raise HTTPException(400, f"Frame {i} is not a JPEG")
            if i == 0 and Image.open(io.BytesIO(data)).size != (m.width, m.height):
                raise HTTPException(400, "Frame size does not match meta width/height")
            with open(os.path.join(tmpdir, f"{i:05d}.jpg"), "wb") as out:
                out.write(data)
    except BaseException:
        shutil.rmtree(tmpdir, ignore_errors=True)
        app.state.gpu_queue.leave(ticket)
        raise

    return _stream_propagation(tmpdir, m.prompt_index, prompt_mask, m.output, m.simplify_px, ticket=ticket, roi=roi)


def _join_queue() -> Ticket:
    """Take a place in the GPU queue, or refuse (HTTP 429) when too many runs already wait."""
    try:
        return app.state.gpu_queue.join()
    except QueueFull as e:
        raise HTTPException(429, f"The SAM 2 queue is full ({e}); try again in a moment")


def _stream_propagation(tmpdir: str, prompt_index: int, prompt_mask: np.ndarray | None, output: str,
                        simplify_px: float, prompts: dict[int, np.ndarray] | None = None, *,
                        ticket: Ticket, roi: Roi | None = None) -> StreamingResponse:
    """Run one propagation over the JPEG frames in `tmpdir` and stream NDJSON results. Takes ownership of
    `tmpdir` and of `ticket` (both are released on every exit path). Only one propagation runs at a time: until
    its turn comes the stream carries {"queued": n} lines (n = runs ahead; repeated as a heartbeat), then the
    normal lines. With `prompts` (obj_id -> mask) several objects are tracked in one pass and every line carries
    "obj"; otherwise the single-object format is used (no "obj"). With `roi`, an object whose mask centroid leaves
    the region of interest is dropped for good: from that frame on its lines carry points=null (the client turns
    them into "outside" markers, overwriting any older result), and the final line reports {"dropped": {obj: frame}}."""
    backend: SegmentationBackend = app.state.backend
    slot: FifoSlot = app.state.gpu_queue
    n_frames = len([f for f in os.listdir(tmpdir) if f.endswith(".jpg")])
    n_objects = len(prompts) if prompts is not None else 1

    q: queue.Queue = queue.Queue(maxsize=64)
    cancel = threading.Event()
    dropped: dict[int, int] = {}  # obj id -> frame index (as in the stream) where it left the region of interest

    def judge(obj: int, idx: int, mask: np.ndarray) -> np.ndarray | None:
        """Region-of-interest rule: the mask, or None once the object has left the region (never comes back)."""
        if roi is None:
            return mask
        if obj in dropped:
            return None
        if roi.mask_is_inside(mask) is False:  # None = empty mask = nothing to judge
            dropped[obj] = idx
            log.info("Object %d left the region of interest at frame %d", obj, idx)
            return None
        return mask

    def put(item) -> bool:
        waited = 0.0
        while not cancel.is_set() and waited < CONSUMER_STALL_TIMEOUT_S:
            try:
                q.put(item, timeout=0.5)
                return True
            except queue.Full:
                waited += 0.5
        cancel.set()  # consumer vanished
        return False

    def worker() -> None:
        # Everything GPU-related happens in THIS thread (torch contexts are thread-local).
        count = 0
        final: dict | None = None

        def encode(mask: np.ndarray):
            return mask_to_cvat_points(mask) if output == "mask" else mask_to_polygon(mask, simplify_px)

        try:
            if prompts is None:
                for idx, mask in backend.propagate(tmpdir, prompt_index, prompt_mask, cancel):
                    if idx == prompt_index:
                        mask = prompt_mask  # on the prompt frame the user's own annotation is authoritative
                    else:
                        mask = judge(1, idx, mask)
                    if not put({"frame": idx, "points": None if mask is None else encode(mask)}):
                        break  # consumer vanished
                    count += 1
                else:
                    if not cancel.is_set():
                        final = {"done": True, "frames": count, "output": output}
                        if roi is not None:
                            final["roi_lines"] = len(roi)  # echo: lets a client detect a service that ignored the ROI
                        if dropped:
                            final["dropped"] = {str(o): f for o, f in dropped.items()}
            else:
                frames_seen: set[int] = set()
                for idx, obj, mask in backend.propagate_multi(tmpdir, prompt_index, prompts, cancel):
                    if idx == prompt_index:
                        mask = prompts[obj]  # the user's own annotation is authoritative on the prompt frame
                    else:
                        mask = judge(obj, idx, mask)
                    if not put({"frame": idx, "obj": obj, "points": None if mask is None else encode(mask)}):
                        break
                    frames_seen.add(idx)
                    count += 1
                else:
                    if not cancel.is_set():
                        final = {"done": True, "frames": len(frames_seen), "objects": len(prompts), "output": output}
                        if roi is not None:
                            final["roi_lines"] = len(roi)  # echo: lets a client detect a service that ignored the ROI
                        if dropped:
                            final["dropped"] = {str(o): f for o, f in dropped.items()}
        except NotImplementedError as e:
            final = {"error": str(e)}
        except Exception as e:  # noqa: BLE001
            if backend.is_out_of_memory(e):
                log.error("GPU out of memory after %d results (%d frames, %d objects)", count, n_frames, n_objects)
                final = {"error": f"The GPU ran out of memory while tracking {n_objects} object(s) over {n_frames} "
                                  "frames. Nothing was changed. Try fewer objects or a nearer end frame."}
            else:
                log.exception("Propagation failed after %d frames", count)
                final = {"error": f"Propagation failed after {count} frames; see service logs"}
        finally:
            # Release everything BEFORE announcing the end of the stream: a client that starts its next
            # propagation the moment it sees `done` / `error` must never be refused or made to wait for nothing.
            shutil.rmtree(tmpdir, ignore_errors=True)
            slot.leave(ticket)
        if final is not None:
            put(final)

    thread = threading.Thread(target=worker, name="propagation", daemon=True)

    def stream():
        started = False
        try:
            # --- waiting for the GPU slot (the first check is instant, so an idle service sends no queue lines)
            last_pos: int | None = None
            last_sent = 0.0
            while True:
                pos = 0
                try:
                    got = slot.wait_turn(ticket, 0.0 if last_pos is None else 1.0)
                    if not got:
                        pos = slot.position(ticket)
                except TicketExpired:
                    yield _ndjson({"error": "Waited too long in the queue; please run again"})
                    return
                if got:
                    break
                now = time.monotonic()
                if pos != last_pos or now - last_sent >= QUEUE_HEARTBEAT_S:
                    last_pos, last_sent = pos, now
                    yield _ndjson({"queued": pos})  # also how a vanished client is noticed while waiting
            # --- we hold the slot: run
            thread.start()
            started = True
            while True:
                try:
                    item = q.get(timeout=5)
                except queue.Empty:
                    if thread.is_alive():
                        continue
                    item = {"error": "Propagation worker ended unexpectedly"}
                yield _ndjson(item)
                if "done" in item or "error" in item:
                    return
        finally:
            cancel.set()  # client went away (or finished): make the worker stop and clean up
            if not started:  # never ran: the worker will not do the cleanup
                shutil.rmtree(tmpdir, ignore_errors=True)
                slot.leave(ticket)

    return StreamingResponse(stream(), media_type="application/x-ndjson")


class PromptObject(BaseModel):
    id: int = Field(ge=0)  # caller-chosen id, echoed back as "obj" on every result line
    prompt_type: str = Field(pattern="^(mask|polygon)$")
    prompt_points: list[float]


class PropagateJobRequest(BaseModel):
    job_id: int = Field(ge=1)
    org: str = Field("", pattern=r"^[A-Za-z0-9_-]{0,64}$")  # CVAT organization slug ("" = personal workspace)
    from_frame: int = Field(ge=0)  # frame holding the prompt (job frame numbers, as in CVAT's UI)
    to_frame: int = Field(ge=0)    # last frame to fill
    # EITHER a single prompt (legacy format: lines without "obj") ...
    prompt_type: str | None = Field(None, pattern="^(mask|polygon)$")
    prompt_points: list[float] | None = None  # CVAT mask points (RLE + bbox) or flat polygon [x1, y1, ...]
    # ... OR several objects tracked together in one pass (every line carries "obj")
    objects: list[PromptObject] | None = None
    output: str = Field("mask", pattern="^(mask|polygon)$")
    simplify_px: float = Field(1.5, ge=0, le=20)
    roi_lines: list[list[float]] | None = None  # region of interest, see sam2common/roi.py


def _to_jpeg(raw: bytes) -> tuple[bytes, tuple[int, int]]:
    img = Image.open(io.BytesIO(raw)).convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=95)
    return buf.getvalue(), img.size


@app.post("/v1/video/propagate-job")
def propagate_job(req: PropagateJobRequest, request: Request):
    """Like /v1/video/propagate, but the service downloads the frames from CVAT itself using the caller's
    forwarded session (Cookie / Authorization). Same NDJSON response. Used by the CVAT UI plugin."""
    cvat: CvatClient = app.state.cvat
    headers = dict(request.headers)
    try:
        cvat.check_auth(headers)  # before any heavy work: unauthenticated callers stop here
        start, stop = cvat.job_range(headers, req.job_id, req.org)
    except CvatError as e:
        log.warning("propagate-job refused for job %s: %s", req.job_id, e)
        raise HTTPException(e.status, str(e))
    if not (start <= req.from_frame < req.to_frame <= stop):
        raise HTTPException(400, f"Need job start <= from_frame < to_frame <= job stop; got "
                                 f"{req.from_frame}..{req.to_frame} in a job covering {start}..{stop}")
    n = req.to_frame - req.from_frame + 1
    if n > MAX_PROPAGATION_FRAMES:
        raise HTTPException(400, f"{n} frames exceed the limit of {MAX_PROPAGATION_FRAMES}; choose a nearer end frame")

    # Request-shape checks first (cheap): nothing is downloaded and no queue place is taken for a bad request.
    single = req.prompt_type is not None or req.prompt_points is not None
    if single == (req.objects is not None):
        raise HTTPException(400, "Give either prompt_type + prompt_points, or objects")
    if single and (req.prompt_type is None or req.prompt_points is None):
        raise HTTPException(400, "prompt_type and prompt_points must be given together")
    if req.objects is not None:
        ids = [o.id for o in req.objects]
        if not 1 <= len(ids) <= MAX_OBJECTS:
            raise HTTPException(400, f"Got {len(ids)} objects; allowed range is 1..{MAX_OBJECTS}")
        if len(set(ids)) != len(ids):
            raise HTTPException(400, "Object ids must be unique")

    roi = _parse_roi(req.roi_lines)

    def fetch(frame: int) -> tuple[bytes, tuple[int, int]]:
        return _to_jpeg(cvat.fetch_frame(headers, req.job_id, frame, req.org))

    # Take a place in the GPU queue BEFORE downloading, so the frames of a waiting run are fetched while the GPU
    # is busy with the run ahead of it. From here on EVERY exit path must give the ticket back.
    ticket = _join_queue()
    tmpdir: str | None = None
    try:
        try:
            with ThreadPoolExecutor(max_workers=4) as pool:
                frames = list(pool.map(fetch, range(req.from_frame, req.to_frame + 1)))
        except CvatError as e:
            raise HTTPException(e.status, str(e))
        except Exception as e:  # noqa: BLE001  (undecodable image etc.)
            raise HTTPException(502, f"Could not read a frame from CVAT: {type(e).__name__}: {e}")
        width, height = frames[0][1]
        if any(sz != (width, height) for _, sz in frames):
            raise HTTPException(400, "Frames in the range have different sizes")

        def to_mask(kind: str, points: list[float], who: str) -> np.ndarray:
            try:
                if kind == "mask":
                    m = cvat_points_to_mask([int(round(v)) for v in points], height, width)
                else:
                    if len(points) < 6 or len(points) % 2:
                        raise ValueError("a polygon needs an even number of coordinates, at least 6")
                    m = polygon_to_mask(points, height, width)
            except Exception as e:  # noqa: BLE001
                raise HTTPException(400, f"Invalid prompt{who} for {width}x{height} frames: {e}")
            if not m.any():
                raise HTTPException(400, f"The prompt{who} is empty at this frame size")
            return m

        prompt_mask = None
        prompts: dict[int, np.ndarray] | None = None
        if req.objects is None:
            prompt_mask = to_mask(req.prompt_type, req.prompt_points, "")
            _check_prompts_inside(roi, {1: prompt_mask}, single=True)
        else:
            prompts = {o.id: to_mask(o.prompt_type, o.prompt_points, f" of object {o.id}") for o in req.objects}
            _check_prompts_inside(roi, prompts, single=False)

        tmpdir = tempfile.mkdtemp(prefix="sam2frames_")
        for i, (data, _) in enumerate(frames):
            with open(os.path.join(tmpdir, f"{i:05d}.jpg"), "wb") as out:
                out.write(data)
        frames = []  # free the encoded copies; they are on disk now
        return _stream_propagation(tmpdir, 0, prompt_mask, req.output, req.simplify_px, prompts, ticket=ticket, roi=roi)
    except BaseException:
        if tmpdir is not None:
            shutil.rmtree(tmpdir, ignore_errors=True)
        app.state.gpu_queue.leave(ticket)
        raise
