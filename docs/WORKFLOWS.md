# CLI and annotation workflows

Complete the [deployment guide](DEPLOYMENT.md) first. Run CLI commands from the repository root with its virtual environment and CVAT credentials configured. For browser operations, use the [plugin guide](../plugin/README.md).

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
  `propagation_busy` is false again. `docker logs sam2-service` shows no second "Loading SAM 2".

## Automated tests

See the [project README](../README.md#tests-and-verification-scope).

## Region of interest (ROI): stop tracking where the footage stops being usable
Mark one or more straight lines; an object is tracked only while it is inside the region they enclose.
- **Rule.** An object is *inside* while the **centroid of its mask** is inside; the first frame where it is outside, the track
  ends for good (that frame and all later ones are written as "outside", overwriting older results; it never comes back, even
  if the mask re-enters). Several lines = the object must be inside **all** of them (a convex region). Lines are infinite: the
  two points only give the direction. A prompt whose centroid is already outside is refused (400) with the object named;
  a prompt that merely straddles a line is fine.
- **Drawing it in CVAT.** Add a label named `roi_line` (type Polyline or Any) to the task. Draw each line as a **polyline
  track** (so it exists on every frame; a plain shape would only exist on its own frame), on the frame where you start the
  propagation or earlier. **3 points (recommended):** the line, then a third point on the side to KEEP. **2 points:** the
  kept side is on your LEFT walking from point 1 to point 2, as seen on screen. Any other number of points is reported, never
  guessed. The propagate dialog says how many ROI lines it found, and the result message says where objects left the region.
- **CLI.** `--roi-line X1,Y1,X2,Y2[,X3,Y3]`, repeatable (image pixels, y down), same meaning as above.
- **Service.** Request field `roi_lines` (list of 4- or 6-number lists, at most `roi_max_lines` = 8, advertised by `/healthz`).
  The final stream line echoes `roi_lines` (how many were applied) and `dropped` ({object: frame}). Plugin and CLI refuse
  a service that did not echo it, so an outdated service can never silently ignore the region.
- **Export.** `cli/cvat_xml_to_yolo.py` keeps `roi_line` out of the classes and writes `roi.json`, so evaluation can ignore
  detections outside the region; `--sidecar` adds line-aligned `tracks/train/frame_N.txt` ("track_id occluded") next to
  training-safe label files.
- **Limits.** Fixed camera only (a line does not move). Convex regions only. The centroid rule drops a straddling object about
  when half of it has crossed. Dropped objects are still tracked by SAM 2 until the end of the run (the results are discarded;
  SAM 2 has `remove_object`, not used yet), so the GPU time is not saved.

## Converting an export to YOLO labels (`cli/cvat_xml_to_yolo.py`)
CVAT's own YOLO exports lose information for this tool's output (YOLO 1.1 / Ultralytics Detection drop polygons; Ultralytics
Segmentation drops the track id). Export the job as **CVAT for video 1.1** (lossless), then:

    python cli/cvat_xml_to_yolo.py job_export.zip -o yolo_seg                 # polygons, Ultralytics Segmentation layout
    python cli/cvat_xml_to_yolo.py job_export.zip -o yolo_det --format det    # bounding boxes of the polygons
    python cli/cvat_xml_to_yolo.py job_export.zip -o yolo_ids --track-ids     # + CVAT track id at the end of each line
    python cli/cvat_xml_to_yolo.py job_export.zip -o yolo_seg --sidecar       # trainable labels + tracks/ (id, occluded) per line

Layout, number format and file naming mirror CVAT's own Ultralytics Segmentation export (checked: the polygon lines are
byte-identical to it). `--track-ids` follows the convention CVAT's importer reads, but **Ultralytics' training loader treats
such lines as corrupt**, so keep that output separate from a folder you train on. Frames between two keyframes, mask shapes
and other shape types are not exported and are reported on stderr (details in the script's `--help`).

## Operations

See [configuration, updates, and troubleshooting](DEPLOYMENT.md).

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
- One propagation at a time; others queue (see Operations). If the GPU hangs mid-run the queue stalls behind it (restart the service). Image clicks stay available while a run is in progress.
- Two copies of the weights in VRAM (image + video predictor), ~1 GB total extra.
- Embedding cache restores private SAM2ImagePredictor attributes (pinned commit; re-check on upgrade).
- Service has no authentication; it listens on 127.0.0.1 (host) and on the `cvat_cvat` Docker network.
- If a client disconnects mid-stream the worker stops at the next frame or after 120 s at the latest.


### Existing masks/polygon tracks → tight boxes

Build `2026-11-12.2-box-replace` provides **Menu → SAM 2: convert to box tracks…**. Select one or more polygon tracks or
mask objects; conversion creates tight axis-aligned rectangle tracks for their full duration in the current job,
keeps originals by default (or removes them when **Replace originals** is checked), preserves outside gaps and
metadata, and allocates new track IDs in both modes. Press Ctrl+S to save.
No margin is added. Only the UI needs rebuilding; the service and Nuclio are unchanged.
See [plugin guide](../plugin/README.md) for geometry conventions, limits, installation and manual checks.
