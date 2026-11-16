# SAM 2 propagation plugin for CVAT Community (v2.78.0)

## Retained-frame integrity fix (2026-11-13)

Build `2026-11-13.1-retained-interpolation` preserves earlier geometry when deleting a polygon track's
future or re-propagating from an interpolated prompt. The retained part of the affected interpolation
interval becomes explicit per-frame keyframes; earlier intervals and existing keyframes are unchanged.
This is necessary because CVAT re-matches polygon vertices when an interval is split: one boundary
anchor alone does not preserve all shapes. Track IDs, visibility and per-frame metadata are retained.
This can add keyframes and Undo entries. Save/reload/export has automated real-core round-trip coverage;
live browser/server acceptance is still required. See [release notes](../UPDATE_INTERPOLATION_2026-11-13.md).

Adds two entries, **"SAM 2: propagate object…"** (Alt+Shift+P) and **"SAM 2: delete object from here…"** (Alt+Shift+D), to the annotation page's top-left **Menu** dropdown (between "Upload annotations" and "Export job dataset") (and the shortcut
**Alt+Shift+P**). Select one object, go to the frame where it is correct, run it: SAM 2 tracks it forward and the
result appears in the job immediately (no reload). Press **Ctrl+S** to save.

| Selected object | Output choices | What happens |
|---|---|---|
| mask shape | mask shapes (grouped) or polygon track | masks: one shape per frame in the object's group; polygon: a new track replaces the shape |
| polygon shape | polygon track or mask shapes | same as above |
| polygon track | polygon track | its keyframes are edited **in place** (object id preserved) |

**Correct and re-propagate:** fix the object on frame K (edit it, or redraw it), select it, run again from K.
Frames before K are untouched; K+1..end-frame are replaced. Several objects are independent: each run only touches the
object you selected.

The selected object is the single *selected* object, or else the one under the mouse (hover + Alt+Shift+P).

## How it works
Browser --`/sam2/v1/video/propagate-job`--> CVAT's Traefik --> sam2-service. The browser sends only job id, frame
range and prompt; the service downloads the frames itself from CVAT **using the caller's forwarded session cookie**, so
CVAT's own permissions apply and an unauthenticated caller never reaches the GPU. Only that endpoint is routed
through Traefik (see `docker-compose.sam2.yml`).

## Build / install (once per CVAT version)
Prerequisites: CVAT checkout **at tag v2.78.0**, `sam2-service` rebuilt from this repo and running.
```
sudo ./plugin/build_ui.sh ~/cvat/cvat            # ~10+ min, several GB RAM; builds cvat/ui:v2.78.0 with the plugin
cd ~/cvat/cvat && sudo docker compose -f docker-compose.yml -f components/serverless/docker-compose.serverless.yml up -d cvat_ui
cd ~/cvat/cvat-sam2 && sudo docker compose -f docker-compose.sam2.yml up -d --build --force-recreate   # Traefik labels
```
Hard-refresh the browser (Ctrl+Shift+R). A CVAT upgrade requires a rebuild against the new tag, and a re-check that the
APIs below still exist.

## Waiting for the GPU
If another run is in progress the dialog says how many runs are ahead ("Waiting for the GPU: n propagation(s) ahead"); it then continues by itself. Cancel works while waiting. A full queue (HTTP 429) is reported as such.

## Region of interest
Polylines labelled `roi_line` on the current frame (2 or 3 points, see the main README) are sent with the run. The dialog shows how many were found; an object whose mask centroid leaves the region is dropped for good and the result message names the frame. A service that does not apply the region is refused.

## Verification done without a live CVAT
- Type-checked against the real v2.78.0 `cvat-ui` / `cvat-core` sources (no new errors vs. the clean baseline).
- `test/plan.test.mjs`: pure logic; `test/service.test.mjs`: the real stream client against a mocked `fetch` (queue lines, errors). Run both: `node --experimental-strip-types --test test/*.test.mjs`.
- `test/core-harness/`: the plugin's real `apply.ts` run against the real `cvat-core` `Collection` class
  (`./test/core-harness/run.sh <cvat-checkout>`): mask re-propagation, polygon-track creation, in-place re-propagation
  with a lost object, closing marker, end of job. This caught a real bug (mixed-frame `removeBatch`).
- NOT verified: the browser UI itself (menu item, dialog, selection handling, canvas refresh) -- that needs a live run.

## Known limitations
- Undo does not revert a whole propagation: masks/tracks created via `annotations.import` have no history entries,
  and in-place track edits are one undo step per frame. Save before a big run; reload the job to discard.
- Polygon output keeps only the largest outer contour per frame (no holes / detached parts).
- Mask tracks do not exist in the Community UI, hence the grouped-shapes representation for masks.
- Frames in one run: up to `MAX_PROPAGATION_FRAMES` (600); the end-frame is chosen in the dialog.
- The dialog is rendered outside CVAT's React tree, so it ignores CVAT's dark theme.
- Touches `ObjectState`, `job.annotations.{get,import,removeBatch,group}` and the `annotationPage.menuActions.items`
  slot: all internal-ish APIs that can change between CVAT versions.

## Objects: polygon tracks vs masks
- A **polygon track** is one CVAT object: one id, edited in place. CVAT's own Delete removes the *whole* track.
  **"Delete object from here"** removes the track from the current frame (or the next one) onward and leaves earlier
  frames untouched. (CVAT's native "Switch outside property", key `o`, only hides one frame of a propagated track,
  because propagation writes a keyframe on every frame.)
- **Masks** cannot be tracks in the Community UI. Each frame's mask is its own shape with its own id; the **group**
  number is the object identity ("Color by: Group" shows it). Deleting one mask therefore affects only that frame.
  "Delete object from here" removes the masks of the selected mask's group from the cut frame on.
- If you **redraw** a mask (e.g. with the SAM tool) instead of editing it, it starts as a new object. In the propagate
  dialog, "Result belongs to" lets it **replace** an existing object: that object's masks on the prompt frame and the
  following frames are replaced and your mask joins it, so old and new are not left side by side. A neighbouring
  object with the same label is pre-selected when there is exactly one.
- Attribute values of the selected object are carried over (immutable ones on the track, mutable ones per keyframe).

## Which build is running? (version stamp)
`plugin/VERSION` is baked into the UI image by `build_ui.sh`. After a hard refresh, the browser console (F12) prints
`[SAM 2 plugin] version <VERSION> loaded` when the plugin starts. If a build finishes in seconds with every Docker step
`CACHED`, nothing new was compiled in: you most likely extracted an older zip (browsers rename repeated downloads to
`name(2).zip`, `name(3).zip`, ...). Check with `cat plugin/VERSION` and compare with the zip you intended to install.

## Several objects at once
Select **several objects on the same frame** and run **SAM 2: propagate object(s)…** once. In CVAT v2.78.0, hold the
"add/remove selection" key (**Ctrl**, or **Cmd** on macOS) and click objects on the canvas or in the Objects sidebar;
Shift+click in the sidebar selects a range. The same applies to **delete object(s) from here**.
- One request tracks all selected objects in a single pass: the frames are downloaded and loaded once, and each
  object keeps its own memory, so they do not influence each other. Up to `MAX_OBJECTS` (default 8) per run.
- One output format applies to the whole selection. A selection that contains a polygon track is polygon-only (a track
  cannot become masks); if the selected objects allow no common format, the plugin says so and does nothing.
- Every object is checked **before** anything runs, and every object's result is validated **before** any is written.
  If CVAT fails part-way while writing, the message lists exactly which objects were applied; nothing is rolled back.
- "Result belongs to" (replace an existing object) is offered only when a single mask is selected.
- A single object behaves exactly as before (and the service keeps its original single-object response format).


## Convert existing objects to tight box tracks (2026-11-12)

1. Select a polygon track, or a mask belonging to a propagated mask group. Ctrl/Cmd+click selects several objects.
2. Open **Menu → SAM 2: convert to box tracks…**. Optionally check **Replace originals**, then click **Create box tracks**.
3. Inspect the boxes, then press **Ctrl+S**. Conversion changes the browser copy until you save.

This works across each selected object's full duration **within the current job**, including frames before the
current frame. A polygon is evaluated through CVAT's own interpolation on every frame, then its coordinate
minima/maxima become a rectangle keyframe. Boxes have zero rotation and **no added margin**.
Mask groups become one rectangle track per group; missing frames get outside markers, so they are not
interpolated across. Selecting several masks of the same group converts the group once. An ungrouped mask
becomes a one-frame box track. Standalone polygon shapes and other shape types are not accepted by this action.

Masks are scanned in RLE form for the true foreground bounds, including disconnected components; no largest-component
filter is applied. The mask crop's bottom-right coordinate is inclusive. Boxes use pixel **edges**: a 10×10 mask
at pixels x=10..19, y=20..29 becomes `[10, 20, 20, 30]`, with width and height 10. This is the tight boundary of
the occupied pixels, not a padding option. Fractional polygon coordinates are retained without rounding.

Original polygons/masks are **kept by default**. Check **Replace originals** to remove the converted polygon tracks
and all masks of their selected groups within this job, after box creation succeeds. Box tracks receive new CVAT IDs
in both modes; replacement does not retain the polygon track ID. The source label, group, source field,
z-order, immutable attributes, per-frame mutable attributes and occlusion flags are carried over. New keyframes
are written for every visible frame, including polygon frames that CVAT interpolated. Outside gaps and the end
of an object are preserved. Tracks visible on the job's final frame remain visible there.

Groups must identify one mask object: multiple masks on a frame, mixed labels, or changing immutable attribute
values are refused before importing any boxes. All selected objects are planned and validated before the import.
Cancel during preparation leaves annotations unchanged. Once boxes have been imported, replacement completes
without further cancellation. A source-removal failure keeps the boxes and reports how many sources were removed;
inspect the result before saving. Reference ground-truth annotations cannot be replaced. Repeating conversion creates another set of boxes;
this initial version does not update earlier conversions. Re-propagating a source later also requires rebuilding
its boxes manually. As with propagation, this import does not create a single Undo action.

This is a UI-only change: rebuild the UI with `sudo ./plugin/build_ui.sh ~/cvat/cvat`, recreate `cvat_ui` using
the existing deployment command, and hard-refresh the browser. No SAM service or Nuclio rebuild is needed.
The build script's required positional argument is the CVAT checkout; its optional second argument is the image
version (default `v2.78.0`, which must match the checkout tag). No new CLI options were added.

Manual acceptance check (not yet run against a live server):

- Convert a polygon track with two spaced keyframes; inspect boxes on intermediate frames and on both sides of an outside gap.
- Convert a mask group with a missing frame and later reappearance; check one box track, a gap, and a resumed box.
- With the checkbox unchecked, verify originals remain. With it checked, verify converted source tracks/groups are removed
  while other objects remain. Box labels/groups/attributes/occluded flags must match; an ROI-closed source stays closed.
- Save, reload the job, and export a box-based format to verify the boxes persist.
- Try multiple selected objects, cancellation, and two masks sharing one group on one frame (must refuse the ambiguous group).
