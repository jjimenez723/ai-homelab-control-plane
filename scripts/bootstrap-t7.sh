#!/usr/bin/env bash
set -euo pipefail

# SAFE BY DEFAULT:
# Creates directories only. It does not format disks, move files, delete files,
# clone repositories, change permissions, or overwrite existing data.

ROOT="${1:-}"

if [[ -z "$ROOT" ]]; then
  echo "Usage: $0 /path/to/T7-root"
  exit 1
fi

mkdir -p \
  "$ROOT/00-inbox/camera-imports" \
  "$ROOT/00-inbox/client-uploads" \
  "$ROOT/00-inbox/downloads" \
  "$ROOT/00-inbox/quarantine" \
  "$ROOT/00-inbox/legacy-dump/macbook" \
  "$ROOT/00-inbox/legacy-dump/dell-xeon" \
  "$ROOT/00-inbox/legacy-dump/dell-i7" \
  "$ROOT/00-inbox/legacy-dump/old-dell" \
  "$ROOT/00-inbox/legacy-dump/sd-cards" \
  "$ROOT/00-inbox/legacy-dump/other" \
  "$ROOT/10-code/products" \
  "$ROOT/10-code/research" \
  "$ROOT/10-code/client-systems" \
  "$ROOT/10-code/internal-tools" \
  "$ROOT/20-clients" \
  "$ROOT/30-shared-media/stock-owned" \
  "$ROOT/30-shared-media/audio" \
  "$ROOT/30-shared-media/fonts-licenses" \
  "$ROOT/30-shared-media/reusable-assets" \
  "$ROOT/40-ai/model-cache" \
  "$ROOT/40-ai/datasets" \
  "$ROOT/40-ai/indexes" \
  "$ROOT/40-ai/manifests" \
  "$ROOT/40-ai/evaluation" \
  "$ROOT/50-research/app-opportunities" \
  "$ROOT/50-research/competitor-research" \
  "$ROOT/50-research/market-research" \
  "$ROOT/50-research/social-video-knowledge" \
  "$ROOT/60-exports/temporary" \
  "$ROOT/60-exports/handoff" \
  "$ROOT/70-archives" \
  "$ROOT/90-system/inventories" \
  "$ROOT/90-system/checksums" \
  "$ROOT/90-system/logs" \
  "$ROOT/90-system/migration-reports"

echo "Created T7 directory scaffold under: $ROOT"
