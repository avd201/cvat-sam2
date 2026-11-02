#!/usr/bin/env bash
# Downloads the SAM 2.1 Hiera-Large checkpoint (URL as in sam2/checkpoints/download_ckpts.sh).
set -euo pipefail
DEST="${1:-./checkpoints}"
mkdir -p "$DEST"
curl -L --fail -o "$DEST/sam2.1_hiera_large.pt" \
  "https://dl.fbaipublicfiles.com/segment_anything_2/092824/sam2.1_hiera_large.pt"
