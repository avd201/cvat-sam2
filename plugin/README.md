# SAM 2 propagation plugin for CVAT Community (v2.78.0)

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

## Verification done without a live CVAT
- Type-checked against the real v2.78.0 `cvat-ui` / `cvat-core` sources (no new errors vs. the clean baseline).
- `test/plan.test.mjs`: pure logic (`node --experimental-strip-types --test test/plan.test.mjs`).
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
