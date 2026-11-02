#!/usr/bin/env bash
# Deploys the proxy interactor into CVAT's local Nuclio (same pattern as CVAT's serverless/deploy_cpu.sh).
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
nuctl create project cvat --platform local 2>/dev/null || true
nuctl deploy --project-name cvat \
  --path "$HERE/nuclio/sam2-proxy-interactor" \
  --file "$HERE/nuclio/sam2-proxy-interactor/function.yaml" \
  --platform local \
  --platform-config '{"attributes": {"network": "cvat_cvat"}}'
nuctl get function --platform local
