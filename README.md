# cvat-sam2 — SAM 2 for CVAT Community

Watch demon:
[![Watch the video](https://img.youtube.com/vi/u4cDmiqPOdw/maxresdefault.jpg)](https://youtu.be/u4cDmiqPOdw)

Run local SAM 2 segmentation and video propagation in CVAT Community. Select objects in the annotation UI, propagate them forward, correct them, or convert existing annotations to box tracks.

**Start here: [Deployment guide](docs/DEPLOYMENT.md).** It covers a fresh Ubuntu/NVIDIA machine, CVAT, Nuclio, the SAM service, the UI plugin, HTTPS, verification, updates, and rollback.

The project root now contains **`2026-11-13.1-retained-interpolation`**. Clone this repository and build directly; no release ZIP extraction is needed. [Source provenance](docs/RELEASE_SOURCE.md) records the verified archive used for this promotion.

| Component | Supported version |
|---|---|
| CVAT Community and SDK | `v2.78.0` / `2.78.0` |
| Nuclio dashboard and CLI | `1.16.3` |
| SAM 2 | commit `2b90b9f5ceec907a1c18123530e92e794ad901a4` |
| Model | SAM 2.1 Hiera-Large |
| UI plugin | `2026-11-13.1-retained-interpolation` |

The UI plugin is compiled into a dedicated local CVAT image. `scripts/compose.sh` applies the required overlays and reads one shared `.env` for both Compose projects. Keep CVAT at the supported tag: the plugin uses internal CVAT APIs.

```text
Interactive mask: browser → CVAT → internal Nuclio dashboard → Nuclio proxy → SAM service
Propagation:      browser → CVAT Traefik /sam2 → SAM service → CVAT frames
                  results → browser annotations → Ctrl+S to persist
```

Nuclio functions do not publish host ports. The Compose helper configures internal dashboard invocation and keeps the dashboard private. Existing installations must follow the [private Nuclio migration steps](docs/DEPLOYMENT.md#upgrade-an-existing-installation-to-private-nuclio-invocation), including redeploying the function; pulling the code alone does not close its old port.

- [Deployment and troubleshooting](docs/DEPLOYMENT.md)
- [Plugin usage: selection, correction, deletion, ROI, boxes](plugin/README.md)
- [CLI, output formats, ROI and export workflows](docs/WORKFLOWS.md)
- [Retained-interpolation release notes](UPDATE_INTERPOLATION_2026-11-13.md)

## Tests and verification scope

CPU-only tests do not need weights, a GPU, or a CVAT server:

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r tests/requirements-dev.txt
./scripts/run_all_tests.sh
```

Plugin unit tests need Node.js 22.6+ with TypeScript stripping support (Node 24 recommended). Without Node they are reported as skipped. Stream-client tests also need `npx` to fetch esbuild; check the reported skipped count. Set `CVAT_CHECKOUT=/path/to/cvat` when running the suite to check deployment overlays against the actual CVAT Compose files (requires the Docker Compose CLI, no daemon). The optional `--cvat /path/to/disposable/cvat` layers need a v2.78.0 checkout with Node dependencies; the runner overwrites/removes its plugin directory. See the runner's usage comments before enabling them. A run with skips is not a full pass.

The imported release has automated regression coverage for retained polygon geometry, queueing, ROI, and box conversion. **Live GPU inference, browser behavior, and HTTPS still require the acceptance checks in the deployment guide.** The release does not resolve all prior production-audit findings; see its notes. Back up jobs before upgrading an existing deployment.
