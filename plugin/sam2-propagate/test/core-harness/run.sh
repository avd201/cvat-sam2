#!/usr/bin/env bash
# Usage: run.sh <path-to-cvat-checkout>   (needs `yarn install` done there, and esbuild available via npx)
set -euo pipefail
CVAT="${1:?path to a CVAT v2.78.0 checkout with node_modules installed}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN="$CVAT/cvat-ui/plugins/sam2-propagate"
rm -rf "$PLUGIN" && mkdir -p "$(dirname "$PLUGIN")" && cp -r "$HERE/../.." "$PLUGIN"
npx --yes esbuild "$PLUGIN/test/core-harness/harness.ts" --bundle --platform=node --format=cjs \
    --outfile=/tmp/sam2-core-harness.cjs --log-level=warning
node /tmp/sam2-core-harness.cjs
