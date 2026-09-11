#!/usr/bin/env bash
# Double-clickable launcher. Calls scripts/organize -- the same CLI used from
# a terminal -- so there is exactly one implementation of the ingestion logic.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

echo "== Organize Files: dry run =="
"$ROOT_DIR/scripts/organize" all --dry-run
echo

read -r -p "Apply the plan above? Originals are removed only after a verified T7 copy. [y/N] " reply
if [[ "$reply" =~ ^[Yy]$ ]]; then
  "$ROOT_DIR/scripts/organize" all --apply
else
  echo "Not applied."
fi

echo
read -n 1 -s -r -p "Press any key to close this window..."
echo
