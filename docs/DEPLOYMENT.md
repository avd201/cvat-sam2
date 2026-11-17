# Deploy CVAT with local SAM 2

This runbook targets **Ubuntu 24.04, Linux x86-64, an NVIDIA GPU, and CVAT/SAM on the same host**. Run commands as your normal user unless they explicitly use `sudo`. Start with HTTP over an SSH tunnel; enable HTTPS before public access.

Supported versions: CVAT **v2.78.0**, Nuclio **1.16.3**, plugin **2026-11-13.1-retained-interpolation**. The root source is the promoted release; **no ZIP download or extraction is required**.

Planning recommendation: 8 CPU cores, 32 GB RAM, 24 GB GPU VRAM, and 80 GB free SSD plus video storage. These are not measured minimums. Actual capacity depends on frames and objects. Begin acceptance with one object and 20–30 frames.

## 1. Prepare the host

```bash
sudo apt-get update
sudo apt-get install -y ca-certificates curl git unzip gnupg python3 python3-venv gh ubuntu-drivers-common
nvidia-smi
```

If `nvidia-smi` works, keep the existing compatible driver. Otherwise use your cloud provider's GPU driver procedure, or on a standard Ubuntu host:

```bash
sudo ubuntu-drivers autoinstall
sudo reboot
```

Reconnect and rerun `nvidia-smi`. The service uses CUDA 12.1; a compatible NVIDIA host driver is required. A separate host CUDA toolkit is unnecessary.

Install Docker from its Ubuntu repository on a fresh machine (for an existing installation, verify it rather than reinstalling):

```bash
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
. /etc/os-release
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu ${UBUNTU_CODENAME:-$VERSION_CODENAME} stable" \
  | sudo tee /etc/apt/sources.list.d/docker.list >/dev/null
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo systemctl enable --now docker
sudo usermod -aG docker "$USER"
```

**Log out and reconnect** to activate Docker group membership. This group grants administrative access through Docker. Then:

```bash
docker run --rm hello-world
docker compose version
```

Install NVIDIA Container Toolkit:

```bash
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
  | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
  | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
  | sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list
sudo apt-get update
sudo apt-get install -y nvidia-container-toolkit
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker
docker run --rm --gpus all --entrypoint nvidia-smi \
  pytorch/pytorch:2.5.1-cuda12.1-cudnn9-runtime
```

Continue when the container lists the GPU successfully. Restarting Docker on an existing machine can interrupt its other workloads.

## 2. Clone the source

Authenticate on the deployment machine if the repository is private. A ChatGPT GitHub connection does not authenticate this machine:

```bash
gh auth login
gh auth setup-git
mkdir -p ~/cvat
cd ~/cvat
git clone --branch v2.78.0 --depth 1 https://github.com/cvat-ai/cvat.git cvat
git clone https://github.com/avd201/cvat-sam2.git cvat-sam2
cd ~/cvat/cvat-sam2
cat plugin/VERSION
git -C ../cvat describe --tags --exact-match
```

Expected: `2026-11-13.1-retained-interpolation` and `v2.78.0`. Record `git rev-parse HEAD` with deployment records. To deploy a reviewed commit later, check out that commit explicitly before building.

The directory layout matters: the helper defaults to the sibling `../cvat` checkout. For another location, export `CVAT_DIR=/absolute/path/to/cvat`; supply the same path to `plugin/build_ui.sh`.

## 3. Set one shared configuration

From `~/cvat/cvat-sam2`:

```bash
cp .env.example .env
```

Edit `.env`. For local access or an SSH tunnel, keep:

```dotenv
CVAT_HOST=localhost
```

For direct remote access, use the exact DNS name or IP used in the browser, **without scheme, port, or path**. For example `CVAT_HOST=cvat.example.org`.

`scripts/compose.sh` passes this file to both projects and consistently includes the serverless and custom UI overlays. You do not need a second `.env` in the CVAT checkout. Exported shell variables take precedence over `.env` in Compose; unset an old exported `CVAT_HOST` before changing the file. The helper pins backend/worker images to v2.78.0.

Check rendered SAM configuration without starting anything:

```bash
./scripts/compose.sh sam2 config --quiet
```

For a remote host with `localhost`, run this **on your laptop** and keep it open:

```bash
ssh -N -L 8080:127.0.0.1:8080 user@server
```

Browse to `http://localhost:8080`. Limit server port 8080 to trusted access; the default CVAT Compose file publishes it on all host interfaces. Keep SAM port 8000 loopback-only. Public deployments should use the HTTPS steps below.

## 4. Build the custom UI, then start CVAT

The UI build needs the CVAT checkout, but does not need a running server:

```bash
cd ~/cvat/cvat-sam2
./plugin/build_ui.sh ~/cvat/cvat
./scripts/compose.sh cvat config --quiet
./scripts/compose.sh cvat up -d
./scripts/compose.sh cvat ps
docker logs --tail 100 cvat_server
```

The initial UI build can take 10+ minutes and several GB RAM. It compiles the plugin into:

```text
cvat/ui:sam2-v2.78.0-2026-11-13.1-retained-interpolation
```

The custom UI overlay has `pull_policy: never`; missing-image errors mean the local build has not completed. Stock `cvat/ui:v2.78.0` is not used by the helper. Use the helper for subsequent CVAT operations so the custom image and serverless settings stay applied.

Allow CVAT to finish database initialization. Inspect the logs if startup fails. Then create an administrator:

```bash
docker exec -it cvat_server bash -ic 'python3 ~/manage.py createsuperuser'
docker network inspect cvat_cvat
```

Open `http://localhost:8080` (or `http://YOUR_HOST:8080`) and log in. The `cvat_cvat` network must exist before starting SAM. Do not change the CVAT Compose project name; the proxy and service depend on that network name.

## 5. Install Nuclio CLI and start SAM

Match the CLI to CVAT's Nuclio dashboard version:

```bash
mkdir -p ~/cvat/downloads
curl -fL https://github.com/nuclio/nuclio/releases/download/1.16.3/nuctl-1.16.3-linux-amd64 \
  -o ~/cvat/downloads/nuctl
sudo install -m 0755 ~/cvat/downloads/nuctl /usr/local/bin/nuctl
nuctl version

cd ~/cvat/cvat-sam2
./sam-service/scripts/download_checkpoints.sh ./checkpoints
ls -lh checkpoints/sam2.1_hiera_large.pt
./scripts/compose.sh sam2 up -d --build
./scripts/compose.sh sam2 ps
docker logs --tail 150 sam2-service
```

Wait for model loading, then check:

```bash
curl -fsS http://127.0.0.1:8000/healthz | python3 -m json.tool
```

Expect `status: ok`, `backend: sam2`, `model_loads: 2`, `outputs: ["mask", "polygon"]`, `roi_max_lines: 8`, and queue/GPU statistics. At idle, `propagation_busy` is false and `queue_waiting` is zero. Keep exactly one Uvicorn worker; each extra worker loads another model pair and creates a separate queue.

Deploy the lightweight interactive proxy:

```bash
./deploy_nuclio.sh
nuctl get function --platform local
```

Expect `sam2-service-interactor` to become `ready`. The proxy reaches `http://sam2-service:8000` on `cvat_cvat`; it does not load another GPU model. Its HTTP trigger has `disablePortPublishing: true`: there must be no published host port for this function. CVAT's server and annotation worker invoke it through the internal Nuclio dashboard using `CVAT_NUCLIO_INVOKE_METHOD=dashboard`, set by `docker-compose.cvat-serverless.yml` through the helper. Keep this overlay applied to both HTTP and HTTPS deployments.

The Nuclio dashboard **must remain private**: it can invoke functions and has Docker-socket access. The overlay clears its published ports and disables its Traefik routing. Keep SAM's raw port loopback-only and allow only required ingress at the cloud firewall. Raw inference endpoints still trust the host/Docker network; do not attach untrusted workloads to it. Do not add a function trigger `port`: Nuclio 1.16.3 gives an explicit port precedence over `disablePortPublishing`.

### Upgrade an existing installation to private Nuclio invocation

Updating files alone does not remove the old function's published port. During a maintenance window, update CVAT's invocation method and redeploy the function:

```bash
./scripts/compose.sh cvat up -d --no-deps --force-recreate cvat_server cvat_worker_annotation nuclio
./deploy_nuclio.sh
```

For HTTPS deployments, use `./scripts/compose.sh --https cvat ...` for the first command. No SAM model or UI rebuild is needed for this security change. Restrict ingress before migrating so the old function port is not exposed during the change.

Inspect `docker ps --format 'table {{.Names}}\t{{.Ports}}'` and identify the `sam2-service-interactor` function container. For that actual container name, run `docker port CONTAINER_NAME`: it must print nothing. `docker port nuclio` must also print nothing. An internal `8080/tcp` in `docker ps` is not a published port; a host mapping such as `0.0.0.0:32768->8080/tcp` is. Stop rollout if either container still has a host mapping.

Verify AI Tools interactive segmentation from an authenticated CVAT job, then propagation. From another machine, verify that the previous function port and dashboard port cannot be reached. Check this again after redeployment. CVAT login protects the public invocation path; internal networking provides the boundary around the unauthenticated proxy and raw SAM endpoints.

## 6. Verify the plugin and routing

Hard-refresh the browser with Ctrl+Shift+R. Open developer tools and find:

```text
[SAM 2 plugin] version 2026-11-13.1-retained-interpolation loaded
```

In an annotation job, the top-left Menu should offer SAM 2 propagation, deletion from here, and box conversion. The interactor should appear as **AI Tools → Interactors → Segment Anything 2 (local service)**.

The plugin calls `/sam2/v1/video/propagate-job` on the CVAT origin. Traefik routes only this endpoint to SAM, and SAM checks the caller's session and job access against CVAT before inference. No plugin API key, browser service URL, or CORS configuration is required.

As a routing-only check, open `http://YOUR_HOST:8080/sam2/v1/video/propagate-job` in a browser. A GET should receive **405 Method Not Allowed**, since the endpoint accepts POST. HTML from CVAT or a 404 usually means a hostname/overlay mismatch. This does not prove successful authenticated propagation.

## 7. Run live acceptance on a disposable job

These checks are required on the target machine; automated CPU tests do not establish GPU/browser readiness.

1. Upload a short video and add an object label.
2. Create a mask with the SAM 2 interactor, accept it, and save with Ctrl+S.
3. Select the object and run **Menu → SAM 2: propagate object…** (Alt+Shift+P). Choose 20–30 frames and one object initially.
4. Try grouped-mask output, then polygon-track output on a fresh object. Results should appear without reloading. Save, reload, and confirm persistence.
5. Correct an object on a later frame and re-propagate. Earlier geometry should stay unchanged. With a redrawn mask, use **Result belongs to** to replace the intended existing object.
6. Test multi-selection and **convert to box tracks**. Confirm originals are kept by default, then separately test replacement on a fresh copy.
7. For the retained-interpolation fix, create sparse polygon keyframes with different shapes/vertex counts. From an intermediate interpolated frame, independently test re-propagation and both deletion modes. Compare every retained frame, metadata, and track ID. Include an outside gap and an unrelated object. Save, reload, and export/reimport to check persistence.
8. Check `/healthz` again: propagation counters grow, `model_loads` stays 2, and the queue becomes idle.

Grouped masks are separate shapes sharing a group ID; polygon output is one track but loses holes and detached components. The interpolation fix can add explicit keyframes to preserve geometry. Whole-operation Undo and general transaction rollback are not available. Save/export a baseline before large changes. The release notes list remaining audit findings; this runbook does not claim they are resolved.

## HTTPS with a public domain

The supplied HTTPS overlays have configuration checks but still require live validation. Use a public DNS name pointing to this host, allow inbound TCP 80/443 in the relevant network firewall, and add to the same `.env`:

```dotenv
CVAT_HOST=cvat.example.org
ACME_EMAIL=you@example.org
```

Save browser annotations before changing endpoints. From the project root:

```bash
./scripts/compose.sh --https cvat config --quiet
./scripts/compose.sh --https sam2 config --quiet
./scripts/compose.sh --https cvat up -d
./scripts/compose.sh --https sam2 up -d --force-recreate
```

Visit `https://cvat.example.org`, log in again, and repeat interactive segmentation and propagation acceptance. CVAT's overlay enables the `lets-encrypt` certificate resolver; SAM's overlay moves its route to `websecure`. **Use `--https` for every subsequent helper command on both projects** to keep both overlays applied. Restrict the old 8080/8090 ports at the network boundary if still published. If another reverse proxy terminates TLS, preserve the `/sam2` path, cookies and streaming responses; that topology is outside this runbook's verified configuration.

## Limits, queue and reproducibility

Edit `.env`, then recreate SAM. The HTTP command is:

```bash
./scripts/compose.sh sam2 up -d --force-recreate
```

For HTTPS use `./scripts/compose.sh --https sam2 ...` instead.

| Variable | Default | Meaning |
|---|---:|---|
| `MAX_PROPAGATION_FRAMES` | 600 | Total frames including prompt; end frame is inclusive |
| `MAX_OBJECTS` | 8 | Objects tracked in one run |
| `MAX_QUEUE` | 8 | Waiting runs behind one active propagation |
| `EMBEDDING_CACHE_SIZE` | 4 | Cached image embeddings |
| `GPU_MEMORY_LIMIT_GB` | unset | Optional PyTorch allocator cap in GiB |

GPU memory grows with objects and frames. 600 model input frames use roughly 7 GB CPU RAM before other overhead; queued runs also retain JPEGs on container disk. Split long ranges into segments starting at the previous segment's final frame. Temporal memory is lost at segment boundaries. Reduce ranges/objects on OOM. The optional memory cap excludes the CUDA context and is intended for rehearsing a smaller card, not guaranteeing capacity.

The SAM commit, model, and major image tags are pinned; the archive does not contain a complete service dependency lock or image digests. After a tested deployment:

```bash
./scripts/freeze_service_deps.sh
```

Preserve `sam-service/constraints.txt` with deployment records or commit it for subsequent builds. Keep tested image backups; a future build without constraints may resolve different transitive packages. Do not add weights, `.env`, or credentials to Git.

## Existing installations, updates and rollback

For a machine deployed from the old ZIP/top-level source, first save annotations and back up jobs, the CVAT database, and uploaded data. Stop using the old checkout's Compose commands once switching. Keep the same container/project/network names and paths to checkpoints. Place this checkout at `~/cvat/cvat-sam2` beside the existing CVAT checkout (preserve the old folder as a rollback copy). Copy the old `.env` and `checkpoints/` into it. Review the new `.env.example`; keep only one authoritative configuration file.

The old UI script overwrote `cvat/ui:v2.78.0`. Preserve it before building:

```bash
docker image tag cvat/ui:v2.78.0 cvat/ui:sam2-before-source-promotion
```

Record the current service image ID and source commit as well. Confirm `../cvat` is at v2.78.0, then run the build/start commands in steps 4–5. If starting CVAT through the helper first, it may briefly run the new UI against the old service; keep annotators out until SAM has been rebuilt and acceptance passes. Promotion from the old root requires **both UI and SAM rebuilds**; the proxy only needs redeployment if missing or changed.

For later updates, use a clean checkout and review the new commit before applying it:

```bash
git status --short
git pull --ff-only
./plugin/build_ui.sh ../cvat
./scripts/compose.sh sam2 up -d --build --force-recreate
./scripts/compose.sh cvat up -d --no-deps --force-recreate cvat_ui
```

Insert `--https` in both helper commands for HTTPS installations. Redeploy `./deploy_nuclio.sh` if the proxy changed. For a UI-only change, omit the service rebuild. Hard-refresh, verify the console version, and rerun acceptance. A future plugin version also needs a corresponding custom image tag in `docker-compose.cvat-ui.yml`.

Rollback uses a known-good source checkout and its recorded configuration/images: rebuild the UI from that checkout and recreate the affected containers with matching overlays. To restore the tagged legacy UI without rebuilding, use a final temporary CVAT Compose override setting `cvat_ui.image` to `cvat/ui:sam2-before-source-promotion` and `pull_policy: never`; keep serverless/HTTPS overlays applied. UI rollback does not undo annotations already saved; restore job/data backups for those. Do not use `down -v` for routine maintenance: it deletes persistent volumes.

## Optional CLI

The UI plugin does not need a host Python environment. For CLI scripting:

```bash
cd ~/cvat/cvat-sam2
python3 -m venv .venv
. .venv/bin/activate
pip install -r cli/requirements.txt
export CVAT_HOST_URL=http://localhost:8080
export CVAT_USERNAME=your_username
read -rsp 'CVAT password: ' CVAT_PASSWORD
export CVAT_PASSWORD
printf '\n'
python cli/cvat_sam2.py list --job 123
```

Use the actual CVAT URL (including HTTPS) and real job ID. Run the CLI on the deployment host so it can reach `127.0.0.1:8000`. See [CLI workflows](WORKFLOWS.md). Save browser changes before CLI writes and reload afterward to avoid overwriting them with stale browser state.

## Troubleshooting

| Symptom | Action |
|---|---|
| Docker cannot access GPU | Fix the driver/toolkit until the container `nvidia-smi` test works. |
| `cvat_cvat` not found | Start CVAT through the helper first; keep project name `cvat`. |
| Custom UI image missing | Complete `plugin/build_ui.sh`; the UI is intentionally never pulled. |
| Build says wrong/no CVAT tag | Check v2.78.0 and run as the checkout owner; avoid `sudo` Git ownership issues. |
| SAM restarts/unhealthy | Inspect logs; check weights, GPU access, VRAM and free RAM/disk. |
| Interactor absent | Check Nuclio function state and logs; redeploy proxy after SAM is healthy. |
| Wrong UI version | Check `plugin/VERSION`, rebuild, recreate UI through the helper, hard-refresh. |
| `/sam2` returns HTML/404 | Check shared hostname and HTTPS overlays; use the exact configured browser host. |
| Frame fetch HTTP 400 | `CVAT_HOST_HEADER` must be a valid hostname, not underscore-containing `cvat_server`. |
| 401/403 | Log in again and confirm permission to read the job. |
| 429 | Queue is full; wait and retry. |
| OOM | Reduce frames/objects, inspect `/healthz` GPU stats; a GPU allocator cap can also cause OOM. |
| Queue never advances | Inspect service logs/GPU; restart SAM only after accounting for interrupted runs. |
| Changes disappear on reload | Save plugin output with Ctrl+S. |

```bash
docker logs --tail 150 sam2-service
docker logs --tail 100 cvat_server
docker logs --tail 100 nuclio
nuctl get function --platform local
curl -fsS http://127.0.0.1:8000/healthz | python3 -m json.tool
```

For useful bug reports include the source commit, plugin console version, image IDs, health response, and relevant error logs. Redact passwords, cookies, access tokens, and private annotation data.
