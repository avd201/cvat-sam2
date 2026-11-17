#!/usr/bin/env bash
# One configuration file and consistent overlays for both Compose projects.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
usage() {
  echo "Usage: $0 [--https] <cvat|sam2> <docker compose arguments...>"
  echo "Reads $ROOT/.env; CVAT_DIR defaults to the sibling ../cvat checkout."
}
HTTPS=false
if [[ "${1:-}" == --https ]]; then HTTPS=true; shift; fi
if [[ "${1:-}" == --help || "${1:-}" == -h ]]; then usage; exit 0; fi
if [[ $# -lt 2 ]]; then usage >&2; exit 2; fi
PROJECT="$1"; shift
if [[ "$PROJECT" != cvat && "$PROJECT" != sam2 ]]; then usage >&2; exit 2; fi
if [[ ! -f "$ROOT/.env" ]]; then
  echo "Missing $ROOT/.env. Copy .env.example to .env and set CVAT_HOST first." >&2
  exit 1
fi
command -v docker >/dev/null || { echo "Install Docker Engine and the Compose plugin first." >&2; exit 1; }
# Pin every CVAT backend/worker even if an old shell or .env selects another version.
export CVAT_VERSION=v2.78.0
FILES=()
if [[ "$PROJECT" == cvat ]]; then
  CVAT_DIR="${CVAT_DIR:-$ROOT/../cvat}"
  if [[ ! -f "$CVAT_DIR/docker-compose.yml" ]]; then
    echo "CVAT checkout not found at $CVAT_DIR. See docs/DEPLOYMENT.md or set CVAT_DIR." >&2
    exit 1
  fi
  CVAT_DIR="$(cd "$CVAT_DIR" && pwd)"
  FILES=(-f "$CVAT_DIR/docker-compose.yml"
         -f "$CVAT_DIR/components/serverless/docker-compose.serverless.yml"
         -f "$ROOT/docker-compose.cvat-serverless.yml"
         -f "$ROOT/docker-compose.cvat-ui.yml")
  if $HTTPS; then FILES+=(-f "$CVAT_DIR/docker-compose.https.yml"); fi
  exec docker compose --project-name cvat --project-directory "$CVAT_DIR" \
    --env-file "$ROOT/.env" "${FILES[@]}" "$@"
else
  FILES=(-f "$ROOT/docker-compose.sam2.yml")
  if $HTTPS; then FILES+=(-f "$ROOT/docker-compose.sam2.https.yml"); fi
  exec docker compose --project-name cvat-sam2 --project-directory "$ROOT" \
    --env-file "$ROOT/.env" "${FILES[@]}" "$@"
fi
