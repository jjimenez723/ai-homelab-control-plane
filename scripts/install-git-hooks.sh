#!/bin/sh
set -eu
repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
git -C "$repo_root" config core.hooksPath scripts/git-hooks
chmod +x "$repo_root"/scripts/git-hooks/*
echo "hooks installed: core.hooksPath=scripts/git-hooks"
