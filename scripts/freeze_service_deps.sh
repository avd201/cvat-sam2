#!/usr/bin/env bash
# Writes sam-service/constraints.txt: every Python package installed in the RUNNING sam2-service container, pinned
# to its exact version. The next `docker compose build` then reproduces the same dependency set (Dockerfile: pip -c).
# Run it once while the service is in a state you have tested, and commit the result.
#
# Usage: sudo ./scripts/freeze_service_deps.sh [container-name]      (default container: sam2-service)
set -euo pipefail

CONTAINER="${1:-sam2-service}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="$ROOT/sam-service/constraints.txt"

RAW="$(mktemp)"; trap 'rm -f "$RAW"' EXIT
docker exec "$CONTAINER" pip freeze --exclude-editable > "$RAW"

# A constraints file cannot hold direct-URL requirements ("name @ git+https://..." / "@ file:///..."): that is
# sam2 itself (already pinned by commit in the Dockerfile) and conda-built packages of the base image (pinned by the
# image tag). They are skipped and listed below so nothing is dropped silently.
SKIPPED="$(grep -E ' @ |^-e ' "$RAW" || true)"
PINS="$(grep -v -E ' @ |^-e |^#|^$' "$RAW" | sort -f || true)"
COUNT="$(printf '%s\n' "$PINS" | grep -c . || true)"
if [ "$COUNT" -lt 20 ]; then
  echo "error: only $COUNT pins found; is '$CONTAINER' the right container?" >&2
  exit 1
fi

{
  echo "# Generated $(date -u +%FT%TZ) by scripts/freeze_service_deps.sh from container '$CONTAINER'"
  echo "# image: $(docker inspect -f '{{.Image}}' "$CONTAINER")   $(docker exec "$CONTAINER" python --version 2>&1)"
  echo "# Used by sam-service/Dockerfile as: pip install -c constraints.txt ..."
  printf '%s\n' "$PINS"
} > "$OUT"

echo "Wrote $COUNT pins to $OUT"
if [ -n "$SKIPPED" ]; then
  echo "Skipped $(printf '%s\n' "$SKIPPED" | grep -c .) direct-URL entries (not valid in a constraints file):"
  printf '%s\n' "$SKIPPED" | cut -c1-110 | sed 's/^/  /'
fi
echo "Next: sudo docker compose -f docker-compose.sam2.yml up -d --build --force-recreate   (rebuilds with the pins)"
