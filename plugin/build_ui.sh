#!/usr/bin/env bash
# Builds a dedicated CVAT UI image with the SAM 2 plugin compiled in.
# Usage: ./plugin/build_ui.sh <path-to-CVAT-checkout> [cvat-version] (default: v2.78.0)
set -euo pipefail
CVAT="${1:?usage: build_ui.sh <path-to-CVAT-checkout> [cvat-version]}"
TAG="${2:-v2.78.0}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# The UI must be built from the same version as the backend images.
VERSION="$(git -C "$CVAT" describe --tags --exact-match 2>/dev/null || true)"
if [ "$VERSION" != "$TAG" ]; then
  echo "error: $CVAT is at '${VERSION:-no tag}', expected tag $TAG (git checkout $TAG)" >&2
  exit 1
fi

DEST="$CVAT/cvat-ui/plugins/sam2-propagate"
rm -rf "$DEST" && mkdir -p "$DEST"
cp -r "$HERE/sam2-propagate/src" "$DEST/src"      # sources only; tests are not part of the image

# Stamp the build so it can be recognised in the browser console ("[SAM 2 plugin] version ... loaded").
PLUGIN_VERSION="$(cat "$HERE/VERSION")"
echo "export const PLUGIN_VERSION = '$PLUGIN_VERSION';" > "$DEST/src/ts/version.ts"
echo "Building SAM 2 plugin version: $PLUGIN_VERSION"

IMAGE="cvat/ui:sam2-${TAG}-${PLUGIN_VERSION}"
docker build -f "$CVAT/Dockerfile.ui" --build-arg CLIENT_PLUGINS=plugins/sam2-propagate -t "$IMAGE" "$CVAT"
echo
echo "Built $IMAGE with plugin version $PLUGIN_VERSION."
echo "From the cvat-sam2 root, recreate the UI container:"
echo "  ./scripts/compose.sh cvat up -d --no-deps --force-recreate cvat_ui"
echo "For HTTPS add --https before cvat. Hard-refresh and check the browser console version."
