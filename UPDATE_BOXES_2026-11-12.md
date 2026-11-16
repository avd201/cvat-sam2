> Historical release notes imported from the verified archive. For current installation commands and image names, use [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md). Do not extract the old ZIP over this checkout.

# Tight AABB track conversion — 2026-11-12.2-box-replace

This archive is the complete project, including the existing ROI implementation.
The user confirmed the ROI line workflow works live on 2026-11-12.

## New behavior

**Menu → SAM 2: convert to box tracks…** converts selected polygon tracks and propagated mask groups into
new rectangle tracks. It operates on the full source duration within the current job, not just from the current
frame. Select multiple objects with Ctrl/Cmd+click. The original annotations are kept by default. The new **Replace originals** checkbox removes the converted
polygon tracks and all masks of the selected groups within this job after creating the boxes. Both modes
receive new CVAT track IDs; replacement does not retain the source polygon track ID. Labels/groups/attributes/occluded flags are retained. Every visible frame has a tight
box keyframe, with no extra margin. Outside gaps remain gaps, including ROI-closed track ends.

CVAT has no mask tracks: selecting one grouped mask converts its entire mask group. An ungrouped mask is a
single-frame object. Multiple masks sharing a group on one frame are ambiguous and refused. No new SAM call
or service endpoint is involved. Conversion is a snapshot: later source corrections do not automatically update
boxes. Running conversion again adds another set. The plugin README explains the exact bounds and limitations.

## Install on the existing server

Extract this archive under `~/cvat`, overwriting the existing `cvat-sam2` project as in prior updates. Then:

```bash
cd ~/cvat/cvat-sam2
cat plugin/VERSION
sudo ./plugin/build_ui.sh ~/cvat/cvat
cd ~/cvat/cvat
sudo env CVAT_HOST=localhost docker compose -f docker-compose.yml -f components/serverless/docker-compose.serverless.yml up -d --no-deps cvat_server cvat_ui
```

Hard-refresh the browser (Ctrl+Shift+R). The console should report plugin version `2026-11-12.2-box-replace`.
Select source objects, use the menu, inspect boxes, then Ctrl+S. Service and Nuclio rebuilds are unnecessary.

`build_ui.sh` takes the CVAT checkout path, plus an optional image tag (default `v2.78.0`, matching the checkout).
`env CVAT_HOST=localhost` explicitly retains the localhost routing required by the existing SSH tunnel.
`docker compose -f` selects the configuration files; `-d` runs in the background, and `--no-deps` limits recreation
to the two named services. Set `CVAT_HOST=localhost` in the CVAT checkout’s `.env` for future updates as well.
This corrects the observed Host(cedro)/localhost mismatch without rebuilding the backend image. No CLI options have been introduced or changed in this release.

## Verification

- All 44 plugin unit tests pass (32 original + 12 conversion tests), including default keep behavior, replacement
  order and scope, cancellation, import failure, partial removal failure and reference-annotation refusal. Geometry includes an exhaustive cross-check of all 511
  nonempty 3×3 masks against brute-force foreground bounds, single pixels, disconnected pieces and fractional polygons.
- All real CVAT v2.78.0 core harness scenarios pass, including the new conversion scenario: interpolation sampled
  through real core, masks grouped across frames, outside gaps, attributes/occlusion, source preservation, source replacement without deleting unrelated objects, and export/reimport.
- TypeScript 5.8.3 includes the new plugin files and reports no plugin errors. The CVAT checkout has 12 unrelated
  baseline errors in this environment; this is not a clean whole-CVAT typecheck.
- The plugin entry point bundles successfully with external application dependencies.
- Python/service files were not changed; their test suite was not rerun for this UI-only release.
- The user confirmed the initial box conversion looks good live. The new replacement checkbox has not yet been
  tested in a running browser/server. No GPU or real SAM output was tested here. Live acceptance checks remain
  in `plugin/README.md`, including Save/reload and a box-based export.

Changed files: plugin `index.tsx`, `ui.tsx`, new `boxes.ts`, version files, new box unit tests, the core harness,
and documentation. Existing ROI, service, CLI and deployment code are unchanged.


## Replacement behavior

The checkbox defaults to unchecked each time the dialog opens. Conversion validates all selected objects before
importing boxes. When checked, deletion uses the source client IDs captured during planning (one polygon object,
or each per-frame mask in a selected group), never the newly imported boxes or other groups. Deletions are batched
per frame for CVAT history compatibility. If import fails, no source deletion is attempted. If deletion fails after
some sources were removed, the boxes stay in place, the canvas refreshes, and the dialog reports the partial result.
As with the initial conversion, this operation is not a single Undo action. Press Ctrl+S to persist the result.
