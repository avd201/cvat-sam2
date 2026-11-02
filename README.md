# cvat-sam2 — local SAM 2 for CVAT Community

Pinned: CVAT v2.78.0 (cvat-sdk==2.78.0), sam2 commit 2b90b9f (2024-12-15), SAM 2.1 Hiera-Large.

```
Stage 1  CVAT UI -> Nuclio "sam2-service-interactor" (thin proxy) -> sam2-service  /v1/interact        (click/box -> mask)
Stage 2  CLI cvat_sam2.py --(CVAT API: read mask, write track)--> CVAT
                          --(frames + prompt mask, NDJSON stream back)--> sam2-service  /v1/video/propagate
```
CVAT is the source of truth. The service keeps the models (and a small image-embedding cache) in memory and
holds **no per-video state**: each propagation call carries its frame range and one prompt mask, and everything
temporary is deleted when it ends. A correction on frame N is therefore re-propagated by a new call that starts at N
(frames before N are never touched or recomputed), and restarts / stale sessions cannot corrupt anything.

## CVAT UI plugin (recommended workflow)
Select one **or several** objects, then propagate or delete them from here in one action (details and limits in `plugin/README.md`).

See `plugin/README.md`: a menu item inside CVAT that propagates the selected object and writes the result into the
job without a reload. The CLI below remains available for scripting and for checking results.

## Setup (once)
1. CVAT v2.78.0 + serverless overlay running; `./sam-service/scripts/download_checkpoints.sh ./checkpoints`
2. `sudo docker compose -f docker-compose.sam2.yml up -d --build`   (rebuild after every update of this repo)
3. `sudo ./deploy_nuclio.sh`   (Stage 1 proxy; unchanged in Stage 2)
4. CLI environment (Python >= 3.10):
   `python3 -m venv ~/venvs/cvat-sam2 && . ~/venvs/cvat-sam2/bin/activate && pip install -r cli/requirements.txt`
5. Credentials (environment only):
   `export CVAT_USERNAME=<user>; read -rs CVAT_PASSWORD; export CVAT_PASSWORD`   (or `CVAT_ACCESS_TOKEN`)

Service check (port is published on 127.0.0.1 only): `curl -s localhost:8000/healthz`
Expect `"backend":"sam2"`, `"model_loads":2` (image model + video model, each loaded once; the number must never grow).

## Workflow
Representation: **one mask shape per frame, all sharing a `group` id** (the group id is the object identity).
CVAT Community cannot open mask *tracks* ("An unexpected type of track mask"), so tracks are not used.
In CVAT's Appearance panel, "Color by: Group" shows an object's masks in one colour.
1. CVAT: open the job, AI Tools -> Interactors -> SAM 2: make the mask on frame N, accept, **Save** (Ctrl+S).
2. `python cli/cvat_sam2.py list --job J` -> note the shape id.
3. `python cli/cvat_sam2.py propagate --job J --shape-id S --to-frame M`
   (the source mask gets a new group id; masks for N+1..M are created in that group; frames where SAM 2 does not
   find the object get no shape)
4. CVAT: **reload the job page (F5)**, scrub through the frames.
5. Wrong on frame K? Fix the mask on K in CVAT (edit it, or delete it and draw a new one **and set its group** to G
   in the object's side panel), **Save**, then
   `python cli/cvat_sam2.py propagate --job J --group G --from-frame K --to-frame M`
   (masks of group G on frames <= K are kept, those on K+1..M are replaced; it asks before replacing unless `--yes`).
6. `python cli/cvat_sam2.py show --job J --group G` lists frames where the mask size jumps (review candidates).
Always Save before running the CLI and reload after it; otherwise the browser's copy can overwrite the CLI's result.

Recovering from the earlier (track-writing) CLI version: `python cli/cvat_sam2.py track-to-shapes --job J --track-id T --all-frames`
converts the track into mask shapes of one new group and deletes the track; the job opens again.

## Polygon tracks (alternative output)
`--output polygon-track` writes a native CVAT **polygon track** (one keyframe per frame; `outside` keyframes where
SAM 2 loses the object), which the Community UI opens and handles like any track (interpolation, object id, split/merge).
- First run: `propagate --job J --shape-id S --output polygon-track --to-frame M` (S may be a mask or a polygon shape;
  the shape becomes the first keyframe and the new track id is printed).
- After a correction on frame K: `propagate --job J --output polygon-track --track-id T --from-frame K --to-frame M`.
- The propagated range is closed with an `outside` keyframe on the next frame (CVAT otherwise keeps showing a track's last
  keyframe until the end of the job). Extending a track later replaces that marker.
- `--simplify-px` (default 1.5) sets polygon simplification; 0 keeps every contour point.
- Polygons are single hole-free rings: only the largest outer contour per frame is kept (holes and small detached
  parts are dropped). A mask source is converted to a polygon for its own keyframe.
- `show --track-id T` and `list` understand polygon tracks too.

## Stage 2 manual test
- Dry run first: `propagate ... --to-frame <N+30> --dry-run` prints on how many frames the object was found and the plan
  (how many shapes would be created / replaced); nothing is written.
- Real run, reload: masks on frames N..M, same group, following the object.
- Re-run with `--group G --from-frame K`: masks on frames before K must look identical to before.
- `curl -s localhost:8000/healthz`: `propagations_run` / `frames_propagated` grow, `model_loads` stays 2,
  `propagation_busy` is false again. `sudo docker logs sam2-service` shows no second "Loading SAM 2".

## Automated tests (CPU, no GPU / weights / CVAT): `pip install -r tests/requirements-dev.txt && pytest -q tests`

## Known limitations / technical debt
- Not verified here (no GPU/CVAT in the dev sandbox): the real SAM 2 video path and the CVAT write-back. Verified:
  service logic with a fake tracker over real HTTP, CLI logic, and that cvat-sdk 2.78.0 accepts the request objects.
- Forward propagation only; a prompt at frame N never fills frames before N.
- Polygon output loses holes and detached small parts (largest outer contour only).
- Object identity (mask output) is a group id on independent mask shapes, not a CVAT track: no interpolation, no "outside" marker
  (a frame without a shape = object not found), and moving/deleting one frame never affects the others.
- Whole range is sent in one call, limit `MAX_PROPAGATION_FRAMES` (600). Frames are kept in CPU RAM (~12 MB each
  at SAM 2's 1024x1024 float input: 600 frames ~ 7 GB RAM). Longer videos: run in segments, each starting at the last frame
  of the previous run (long-range memory is lost at the seams).
- Frames are re-encoded as JPEG q95 for the model; frames of a range must share one size.
- Deleted CVAT frames inside the range are processed like any other frame.
- A failed or interrupted model run writes nothing to CVAT. The write itself is create -> delete old -> set group,
  so a failure in the middle leaves duplicates (never lost masks); re-run with --yes to replace them.
- One propagation at a time (a second request gets HTTP 429). Image clicks stay available while it runs.
- Two copies of the weights in VRAM (image + video predictor), ~1 GB total extra.
- Embedding cache restores private SAM2ImagePredictor attributes (pinned commit; re-check on upgrade).
- Service has no authentication; it listens on 127.0.0.1 (host) and on the `cvat_cvat` Docker network.
- If a client disconnects mid-stream the worker stops at the next frame or after 120 s at the latest.
