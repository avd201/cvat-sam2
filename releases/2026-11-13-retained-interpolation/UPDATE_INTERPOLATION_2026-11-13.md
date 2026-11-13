# Retained interpolation — 2026-11-13.1-retained-interpolation

Source archive: `cvat-sam2-2026-11-13-retained-interpolation-fixes.zip`.
Base: `2026-11-12.2-box-replace`, CVAT Community v2.78.0.

## Changes

Fixes handoff audit A1 and A2:

- **Delete from here:** removing future polygon keyframes no longer changes the geometry of retained
  earlier frames. Both include-current and later-only modes preserve the retained boundary.
- **Re-propagate:** the original interpolated prompt and all preceding frames are preserved before
  writing K+1 onward. The prompt is never replaced by the service's prompt-frame result.

The plugin snapshots every retained interpolated frame between the previous keyframe and the retained
boundary, before any write. It saves those snapshots as explicit keyframes, then performs the existing
forward update/deletion. Existing boundary keyframes need no extra snapshots. A re-propagation with no
future frames and a later-only deletion on the last frame do not add snapshots.

A single anchor was trialed and failed three changing-vertex-count regressions: CVAT's polygon
interpolation re-matches and resamples vertices when an interval is split. Saving the original per-frame
geometry avoids that change. The affected interval may gain keyframes and additional Undo entries;
there is still no single-action Undo or general transaction rollback.

Track identity, labels/groups, visibility, occlusion, z-order, rotation and immutable/mutable attribute
values are preserved on retained frames. Other objects are not targeted. Source provenance continues
to follow CVAT's ordinary edit behavior (e.g. automatic tracks may become semi-automatic).

This release does not repair annotation changes already saved by an older build.

## Verification

Fresh automated checks, without browser, CVAT server, GPU or deployment:

- Unmodified release plus new real-core regressions: **15 of 48 cases failed**, reproducing A1 and A2.
- Fixed release: **all 48 cases pass**, alongside every existing core-harness scenario. Matrix covers
  translated sparse polygons, unequal vertex counts, outside gaps and dense tracks; both delete modes
  and re-propagation; prompts at frames 1, 5, 10 and the job's last frame (20).
- Every retained frame is compared for geometry and annotation metadata, with track identity checks,
  unrelated-track export comparisons, future visibility/new-result assertions, and export/reimport.
- Plugin unit tests: **44 passed, 0 skipped** (planning, boxes and stream-client tests).
- Plugin entry bundles using esbuild. TypeScript 5.8.3 includes all plugin sources and the harness:
  **no plugin errors**; 12 CVAT baseline errors remain, matching a check excluding this plugin.
- Python tests were not rerun: service, CLI and Python files are unchanged.

Regression cases are permanent in `plugin/sam2-propagate/test/core-harness/harness.ts`.
The existing harness/full runner overwrites the plugin directory in the supplied CVAT checkout;
use an expendable checkout with dependencies installed. That tooling issue (audit C2) is unchanged.

## Install on cedro

This is a UI-only release. Extract the archive under `~/cvat` so it updates `~/cvat/cvat-sam2/`.
Save current browser annotations with Ctrl+S before refreshing/recreating the UI.

```bash
cd ~/cvat/cvat-sam2
cat plugin/VERSION
sudo ./plugin/build_ui.sh ~/cvat/cvat
cd ~/cvat/cvat
sudo env CVAT_HOST=localhost docker compose \
  -f docker-compose.yml \
  -f components/serverless/docker-compose.serverless.yml \
  up -d --no-deps cvat_server cvat_ui
```

Use the recreation command above; the unchanged build script prints an older command. Hard-refresh
with Ctrl+Shift+R. The browser console must show
`[SAM 2 plugin] version 2026-11-13.1-retained-interpolation loaded`.
No SAM service rebuild or Nuclio redeployment is needed for this code change.

The build script still overwrites `cvat/ui:v2.78.0`; retain the previous release for rebuilding/rollback.
It requires the CVAT checkout at the exact v2.78.0 tag.

## Live acceptance still needed

On a disposable job, create a polygon track with different geometry at two separated keyframes.
Record earlier/interpolated frames, then test each operation separately from a fresh copy:

1. Delete from an intermediate frame with include-current enabled; compare every earlier frame.
2. Delete later-only; compare the current frame and every earlier frame.
3. Re-propagate from an interpolated prompt; compare that prompt and every earlier frame.
4. Repeat with a changing polygon vertex count and an outside gap. Keep an unrelated object present
   and verify it stays unchanged. Test both sparse and dense tracks.
5. Ctrl+S, reload and export/reimport; confirm the retained geometry and object identities persist.

The other handoff audit items remain open, including cancellation/overlapping mutations, partial
failure reporting, deleted/sampled-frame policy, locks/reference safeguards and external deployment
boundaries. Box replacement live acceptance also remains outstanding. No smoothing was added.

## Command arguments

`build_ui.sh ~/cvat/cvat` supplies the CVAT checkout; the existing default image tag is `v2.78.0`.
`env CVAT_HOST=localhost` passes the known routing hostname through sudo. Compose `-f` selects the two
configuration files, `-d` runs in the background, and `--no-deps` limits recreation to the named services.
No scripts or CLI options were introduced or changed in this release.
