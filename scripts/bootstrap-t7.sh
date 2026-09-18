#!/usr/bin/env bash
set -euo pipefail

# Scaffold the Angel volume layout described in docs/25-angel-volume-layout.md.
#
# SAFE BY DEFAULT:
# Creates directories and writes the layout's README/manifest files. It does
# not format disks, move files, delete files, clone repositories, or change
# permissions. It never overwrites an existing README or manifest whose
# content differs from the one it would write: it writes `<file>.new`
# alongside and reports the divergence at the end, leaving the decision to a
# human. Running it twice in a row makes no change the second time.
#
# Usage:
#   scripts/bootstrap-t7.sh /Volumes/Angel --expect-uuid <APFS-volume-UUID>
#   scripts/bootstrap-t7.sh /tmp/fake-angel            # dev/test root, no UUID
#
# `--expect-uuid` is how a run against the real volume proves it is writing
# to the intended disk: the Finder name `Angel` and the path `/Volumes/Angel`
# are not identity (D-019). Pass the UUID held in the gitignored
# `config/ingest.yaml` (`t7.volume_uuid`). Without the flag, the script
# refuses to write anywhere under `/Volumes`.

ROOT=""
EXPECT_UUID=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --expect-uuid)
      EXPECT_UUID="${2:-}"
      if [[ -z "$EXPECT_UUID" ]]; then
        echo "ERROR: --expect-uuid needs a value" >&2
        exit 2
      fi
      shift 2
      ;;
    --expect-uuid=*)
      EXPECT_UUID="${1#--expect-uuid=}"
      shift
      ;;
    -h|--help)
      echo "Usage: $0 /path/to/Angel-root [--expect-uuid <APFS-volume-UUID>]"
      exit 0
      ;;
    *)
      if [[ -n "$ROOT" ]]; then
        echo "ERROR: unexpected argument: $1" >&2
        exit 2
      fi
      ROOT="$1"
      shift
      ;;
  esac
done

if [[ -z "$ROOT" ]]; then
  echo "Usage: $0 /path/to/Angel-root [--expect-uuid <APFS-volume-UUID>]" >&2
  exit 2
fi

ROOT="${ROOT%/}"

if [[ ! -d "$ROOT" ]]; then
  echo "ERROR: root does not exist: $ROOT" >&2
  exit 2
fi

# The "a mounted volume needs --expect-uuid" check below is a test on this
# string, so the string has to be the real path. `/tmp/angel` symlinked to
# /Volumes/Angel, or `../../Volumes/Angel`, both named the real volume and
# both failed the `/Volumes/*` prefix test -- which skipped UUID verification
# entirely on precisely the disk it exists to protect. `pwd -P` resolves every
# symlink and every `..`, so the check sees what will actually be written to.
ROOT="$(cd "$ROOT" && pwd -P)"
ROOT="${ROOT%/}"

RESOLVED_UUID=""
if [[ -n "$EXPECT_UUID" ]]; then
  if ! command -v diskutil >/dev/null 2>&1; then
    echo "ERROR: --expect-uuid was given but diskutil is unavailable; refusing to write." >&2
    exit 3
  fi
  # Captured first, then parsed: piping `diskutil` straight into an `awk`
  # that exits early makes the pipeline fail under `set -o pipefail` (SIGPIPE),
  # which would turn a plain UUID mismatch into an unexplained exit 1.
  DISKUTIL_INFO="$(diskutil info "$ROOT" 2>/dev/null || true)"
  RESOLVED_UUID="$(printf '%s\n' "$DISKUTIL_INFO" | awk -F': *' '/Volume UUID/ && !seen {print $2; seen=1}' | tr -d '[:space:]')"
  if [[ -z "$RESOLVED_UUID" ]]; then
    echo "ERROR: could not read a Volume UUID for $ROOT; refusing to write." >&2
    exit 3
  fi
  if [[ "$(printf '%s' "$RESOLVED_UUID" | tr '[:lower:]' '[:upper:]')" != "$(printf '%s' "$EXPECT_UUID" | tr '[:lower:]' '[:upper:]')" ]]; then
    echo "ERROR: volume UUID mismatch for $ROOT" >&2
    echo "  expected: $EXPECT_UUID" >&2
    echo "  found:    $RESOLVED_UUID" >&2
    exit 3
  fi
  echo "Volume UUID verified for $ROOT: $RESOLVED_UUID"
elif [[ "$ROOT" == /Volumes || "$ROOT" == /Volumes/* ]]; then
  echo "ERROR: $ROOT is a mounted volume, so --expect-uuid is required (D-019)." >&2
  echo "       A volume name is not an identity. Pass t7.volume_uuid from config/ingest.yaml." >&2
  exit 3
fi

CREATED_DIRS=0
WROTE_FILES=0
DIVERGED=()

make_dir() {
  local path="$1"
  if [[ ! -d "$path" ]]; then
    mkdir -p "$path"
    CREATED_DIRS=$((CREATED_DIRS + 1))
  fi
}

# write_doc <path> < stdin
#
# Writes the document if absent, leaves it alone if byte-identical, and
# writes `<path>.new` if it exists with different content.
write_doc() {
  local path="$1"
  local dir
  dir="$(dirname "$path")"
  mkdir -p "$dir"
  local tmp
  tmp="$(mktemp "$dir/.bootstrap-t7.XXXXXX")"
  cat >"$tmp"
  if [[ ! -e "$path" ]]; then
    mv "$tmp" "$path"
    WROTE_FILES=$((WROTE_FILES + 1))
    return
  fi
  if cmp -s "$tmp" "$path"; then
    rm -f "$tmp"
    return
  fi
  mv "$tmp" "$path.new"
  DIVERGED+=("$path")
}

# ---------------------------------------------------------------------------
# Directories. Order matches docs/25-angel-volume-layout.md.
# ---------------------------------------------------------------------------

for dir in \
  "00-inbox/camera-imports" \
  "00-inbox/client-uploads" \
  "00-inbox/downloads" \
  "00-inbox/quarantine" \
  "00-inbox/organized" \
  "00-inbox/legacy-dump/macbook" \
  "00-inbox/legacy-dump/dell-xeon" \
  "00-inbox/legacy-dump/dell-i7" \
  "00-inbox/legacy-dump/old-dell" \
  "00-inbox/legacy-dump/sd-cards" \
  "00-inbox/legacy-dump/other" \
  "10-code/mirrors" \
  "10-code/archives" \
  "10-code/generated-apps" \
  "10-code/products" \
  "10-code/research" \
  "10-code/client-systems" \
  "10-code/internal-tools" \
  "20-clients" \
  "30-shared-media/stock-owned" \
  "30-shared-media/audio" \
  "30-shared-media/fonts-licenses" \
  "30-shared-media/reusable-assets" \
  "40-ai/model-cache/ollama" \
  "40-ai/model-cache/huggingface" \
  "40-ai/model-cache/mlx" \
  "40-ai/model-cache/whisper" \
  "40-ai/datasets" \
  "40-ai/indexes" \
  "40-ai/manifests" \
  "40-ai/derived" \
  "40-ai/evaluation/benchmarks" \
  "40-ai/evaluation/benchmarks/legacy-20" \
  "40-ai/evaluation/campaigns" \
  "40-ai/evaluation/telemetry" \
  "40-ai/skills/trusted" \
  "40-ai/skills/candidates" \
  "50-research/app-opportunities" \
  "50-research/competitor-research" \
  "50-research/market-research" \
  "50-research/social-video-knowledge" \
  "50-research/knowledge" \
  "60-exports/temporary" \
  "60-exports/handoff" \
  "70-archives" \
  "90-system/inventories" \
  "90-system/checksums" \
  "90-system/logs" \
  "90-system/migration-reports" \
  "90-system/staging" \
  "90-system/cache" \
  ; do
  make_dir "$ROOT/$dir"
done

# ---------------------------------------------------------------------------
# README files. One per top level, plus the volume root. Each answers the same
# four questions: what belongs here, what never does, who writes here, and
# whether it is backed up.
# ---------------------------------------------------------------------------

write_doc "$ROOT/README.md" <<'EOF'
# Angel

The Samsung T7 `Angel`: an APFS (Encrypted) volume, Mac-attached, identified
by its **APFS Volume UUID** and never by this name or by `/Volumes/Angel`
(D-019). Tooling that writes here resolves the UUID through `diskutil` first
and refuses on a mismatch.

The authoritative map of this volume is `docs/25-angel-volume-layout.md` in
the `ai-homelab-control-plane` repository. This file is a copy of its summary
for anyone who plugs the drive in without that repository to hand.

## Angel is consolidation, not backup

If Angel becomes the only copy of something, risk has gone up, not down. An
encrypted restic repository in Google Drive is the current off-site copy
(`docs/09-backup-recovery.md`). Two things on this volume are deliberately
**not** in it: `40-ai/model-cache/**` and `90-system/{staging,cache}/**`.

## Top level

| Directory | Holds | Backed up |
|---|---|---|
| `00-inbox/` | arrival zone; nothing here is organized yet | yes |
| `10-code/` | bare Git mirrors and archives, generated app source. **Not a working tree** | yes |
| `20-clients/` | canonical human-owned client work | yes, highest priority |
| `30-shared-media/` | reusable owned media, audio, licensed fonts | yes |
| `40-ai/` | model caches, datasets, evaluation, derived evidence, skill copies | mostly; `model-cache/` excluded |
| `50-research/` | research notes and durable reference knowledge | yes |
| `60-exports/` | deliverables leaving the system | yes |
| `70-archives/` | cold, closed-out material | yes |
| `90-system/` | catalog, manifests, logs, reports, staging, cache | yes except `staging/` and `cache/` |

## Three rules that are not conventions

1. **Active development does not happen on this volume.** Working trees live
   on the Mac's internal disk under `~/Projects`. `10-code/` holds clean bare
   mirrors and archives (docs/02).
2. **Nothing under `40-ai/skills/candidates/` or `50-research/knowledge/` is
   ever executed or loaded as instructions.** It is video- and web-derived
   data. The trusted skill tree is Git; `40-ai/skills/trusted/` is a
   read-only export of it (`docs/26-skills-and-knowledge-trust.md`).
3. **`00-inbox/restic-smoke-test/` is a live restore drill.** Do not edit,
   rename, or tidy it.

`90-system/angel-manifest.yaml` is the machine-readable form of this map.
EOF

write_doc "$ROOT/00-inbox/README.md" <<'EOF'
# 00-inbox — arrival zone

**What belongs here:** material that has arrived but has not been organized.
Camera imports, client uploads, browser downloads, quarantined items, bulk
legacy dumps from old machines, and `organized/` — which, despite its name,
holds verified, catalog-committed assets *awaiting or carrying* enrichment,
not classified output (D-023). Nothing may infer from that name that a file
inside it has been classified, placed, or reviewed.

**What never belongs here:** anything treated as canonical. A file here has
not been placed. Also never a secret, and never the only copy of client work.

**Who writes here:** the owner (by hand or by copy), and the `organize` CLI,
which writes `organized/` and `legacy-dump/` only on an explicit `--apply`.

**Backup status:** backed up. `*.partial` transfer artifacts are excluded.

`restic-smoke-test/` is the configured monthly restore drill target
(`backup.restore_drill_relative_path`). Leave it exactly as it is.
EOF

write_doc "$ROOT/10-code/README.md" <<'EOF'
# 10-code — code, but not a working filesystem

**What belongs here:**

- `mirrors/<name>.git` — bare `git clone --mirror` copies of active
  repositories, so restic covers committed history (including local-only
  branches) even when GitHub does not. Refresh with
  `./scripts/refresh-angel-mirrors`.
- `archives/<name>.git` — bare mirrors of retired, duplicate, or closed-out
  repositories, kept for provenance.
- `generated-apps/<app>/` — source of applications this system generates,
  before they have their own Git remote. Source, not builds.
- `products/`, `research/`, `client-systems/`, `internal-tools/` — clean
  exports and per-domain code archives.

**What never belongs here:** an active working tree. Do not run an editor,
file watcher, package manager, or build against this volume: it is removable
USB storage and work is lost when the cable moves (docs/02). Active
development lives on the Mac's internal disk under `~/Projects/<repo>`, and
the Git remote stays the source of truth. Also never `node_modules/`, build
output, or a virtualenv.

**Who writes here:** `./scripts/refresh-angel-mirrors` (UUID-gated), and the
owner by hand for archives and generated app source.

**Backup status:** backed up. A mirror is a second copy of history, not a
substitute for GitHub.
EOF

write_doc "$ROOT/20-clients/README.md" <<'EOF'
# 20-clients — canonical, human-owned client work

**What belongs here:** the client work of record, one directory per client
slug, laid out per `docs/02-t7-filesystem.md`: `manifest.yaml`, `00-admin/`,
`10-briefs/`, `20-brand/`, `30-media/YYYY/YYYY-MM-DD_<project-slug>/{00-raw,
10-selected,20-proxies,30-edits,40-exports}/`, `40-web/`, `50-social/`,
`90-archive/`.

`30-media/.../00-raw/` holds original camera files. These are the
irreplaceable class in `docs/09-backup-recovery.md`: no process in this
system may rewrite or delete one.

**What never belongs here:** generated or regenerable material with no client
provenance, model caches, datasets, credentials, or anything a tool would be
happy to recreate. A proxy or a transcode is a derivative and belongs in the
project's own `20-proxies/`, never in place of the original.

**Who writes here:** the owner. The `organize` CLI proposes a destination;
placement remains a human decision (Phase 3 scope).

**Backup status:** backed up, highest priority. Nothing here is regenerable.
EOF

write_doc "$ROOT/30-shared-media/README.md" <<'EOF'
# 30-shared-media — reusable owned assets

**What belongs here:** media that is not client-specific and is reused across
projects: owned stock (`stock-owned/`), music and sound effects (`audio/`),
licensed typefaces together with their licence documents
(`fonts-licenses/`), and reusable graphics (`reusable-assets/`).

**What never belongs here:** client-specific footage — that is canonical work
and belongs under `20-clients/<client>/`. Never an asset whose licence does
not permit the use it is filed under; keep the licence beside the file.
Derived material (proxies, thumbnails, transcodes, sampled frames) belongs in
`40-ai/derived/` or, for a client project, that project's own `20-proxies/`.

**Who writes here:** the owner.

**Backup status:** backed up.
EOF

write_doc "$ROOT/40-ai/README.md" <<'EOF'
# 40-ai — the UUID-gated bulk tier for AI artifacts

Everything here is reached through the volume-UUID resolver
(`tools/models/storage.py`, D-037). There is no internal-SSD fallback: with
Angel absent, local model backends are unavailable and the system continues
at Tier 0 rather than quietly filling the internal disk.

**What belongs here:**

- `model-cache/{ollama,huggingface,mlx,whisper}/` — downloadable weights and
  runtime caches. Regenerable from recorded provider/tag/digest.
- `datasets/` — deliberate, curated datasets.
- `indexes/` — embedding and search indexes, including expensive ones.
- `manifests/` — model and artifact manifests.
- `derived/` — what the perception layer produced: OCR text, vision
  descriptions, sampled video frames, transcripts (docs/17, D-055, D-056).
  Regenerable by re-running perception over the original.
- `evaluation/benchmarks/` — raw benchmark records, e.g. the Legacy-20 canary.
- `evaluation/campaigns/` — `angel-eval` append-only campaign records and the
  reports regenerated from them (docs/22).
- `evaluation/telemetry/` — derived coordinator telemetry snapshots (D-050).
  Losing every snapshot costs history, never correctness.
- `skills/trusted/` — a read-only export of the Git `skills/` tree with a
  `MANIFEST.yaml` of sha256 plus the git SHA it came from.
- `skills/candidates/` — **untrusted** video-derived candidate skills. Never
  executed, never loaded as instructions.

**What never belongs here:** a secret or credential; a canonical original
(client media belongs in `20-clients/`); the Mac-local coordinator ledger,
which stays internal and authoritative (D-033/D-050); anything an agent
harness would auto-load as a trusted skill.

**Who writes here:** `tools/models/storage.py` and the runtimes it configures;
`tools/campaign/`; `tools/coordinator/snapshot.py`;
`./scripts/export-angel-skills`; the social-video pipeline (candidates only).

**Backup status:** backed up **except** `model-cache/**`, which restic
excludes precisely (D-037). Manifests, evaluation records, datasets, indexes,
derived evidence, and both skill trees are backed up.
EOF

write_doc "$ROOT/50-research/README.md" <<'EOF'
# 50-research — research and durable reference knowledge

**What belongs here:** app-opportunity notes, competitor and market research,
knowledge extracted from social video (`social-video-knowledge/`), and
`knowledge/` — durable, non-executable reference material, including exports
of the Mac-local `knowledge/` tree.

**What never belongs here:** anything executable or anything an agent should
treat as an instruction. Material here is **reference data**. When an agent
consumes it, it is passed as delimited, quoted, explicitly untrusted context
(D-013). A procedure extracted from a video is not a skill; candidate skills
live in `40-ai/skills/candidates/` and are not executed either
(`docs/26-skills-and-knowledge-trust.md`).

**Who writes here:** the owner, and the social-video learning pipeline.

**Backup status:** backed up.
EOF

write_doc "$ROOT/60-exports/README.md" <<'EOF'
# 60-exports — things leaving the system

**What belongs here:** finished artifacts on their way out. `handoff/` for
deliverables addressed to a client or a third party; `temporary/` for
short-lived exports that exist to be sent and then removed.

**What never belongs here:** the only copy of anything. An export is a
derivative of canonical work under `20-clients/`, and it is safe to delete
once delivered. Source code does not belong here either: generated
application *source* lives in `10-code/generated-apps/`, and only its built
or rendered output lands here.

**Who writes here:** the owner, and export/render steps.

**Backup status:** backed up, but treated as regenerable. Nothing should be
recovered from here that could not be re-exported.
EOF

write_doc "$ROOT/70-archives/README.md" <<'EOF'
# 70-archives — cold storage

**What belongs here:** closed-out client engagements, superseded project
trees, and bulk material kept for provenance rather than use. Prefer a dated
directory name and a small `README` or manifest saying what a thing was and
why it was kept.

**What never belongs here:** active work, and archived *repositories* — a
retired repository belongs in `10-code/archives/` as a bare mirror, where it
stays a real Git object database rather than a zip nobody can query.

**Who writes here:** the owner.

**Backup status:** backed up. Archived does not mean expendable.
EOF

write_doc "$ROOT/90-system/README.md" <<'EOF'
# 90-system — the volume's own bookkeeping

**What belongs here:**

- `angel-manifest.yaml` — the machine-readable layout of this volume.
- `catalog.sqlite` and `sidecars/` — the ingest catalog (single writer) and
  the per-asset human-readable sidecars that are the durable recovery form
  (D-009, D-019).
- `inventories/`, `checksums/` — inventory and checksum manifests.
- `logs/`, `migration-reports/` — operational logs, and a report for every
  create/move performed against this volume, with before/after hashes.
- `staging/` — `.partial` files mid-transfer. **Not backed up**: an
  incomplete transfer has nothing worth restoring.
- `cache/` — runtime scratch. **Not backed up**, regenerable by definition.

**What never belongs here:** a secret. The restic repository passphrase and
the rclone configuration password live in the macOS Keychain and nowhere
else. Also never Mac-side operational state: the backup status ledger and the
coordinator job ledger stay under the Mac's `.state/` so they survive Angel
being detached.

**Who writes here:** the `organize` CLI (catalog, sidecars, staging),
`scripts/bootstrap-t7.sh` (this file and the manifest),
`scripts/repos-inventory` (migration reports).

**Backup status:** backed up except `staging/**` and `cache/**`.
EOF

write_doc "$ROOT/10-code/mirrors/README.md" <<'EOF'
# 10-code/mirrors — bare mirrors, not working trees

Each `<name>.git` here is a **bare** repository produced by
`git clone --mirror --no-hardlinks` from a local working copy. It has no
checkout: `ls` shows `HEAD`, `objects/`, `refs/`, not source files. That is
correct and not a broken clone.

## Why they exist

GitHub covers committed, pushed code. It does not cover a local-only branch,
and `docs/09-backup-recovery.md` lists "unpushed Git work" as irreplaceable.
These mirrors are cloned from the **local** copy rather than from GitHub, so
they capture branches the remote has never seen, and restic then carries them
off-site.

A mirror is a second copy of history. It is not a backup of uncommitted
changes, and it is not a substitute for pushing.

## How to refresh

```bash
./scripts/refresh-angel-mirrors              # refresh every mirror
./scripts/refresh-angel-mirrors --only mafi  # one mirror
./scripts/refresh-angel-mirrors --dry-run    # show what would be fetched
```

The script verifies this volume's APFS UUID through the same resolver
ingestion and backup use, and refuses to write if the volume is absent,
locked, read-only, or merely name-matched. It fetches from the recorded
source path (`angel.sourcePath` in each mirror's config) and never pushes.

## How to use one

Never work in a mirror. Clone out of it:

```bash
git clone /Volumes/Angel/10-code/mirrors/<name>.git ~/Projects/<name>
```

Then set the GitHub remote (recorded as `angel.sourceRemote` in the mirror's
config) as `origin`.
EOF

write_doc "$ROOT/10-code/archives/README.md" <<'EOF'
# 10-code/archives — retired repositories, kept as Git

A bare mirror here is a repository that is no longer worked in: a stale
duplicate checkout, a finished engagement, or a superseded experiment. It is
kept as a real Git object database rather than a zip, so its history stays
queryable (`git -C <name>.git log`).

Placing a mirror here is a record, not a deletion: the original working copy
stays where the owner left it. `90-system/migration-reports/` says what was
archived, when, from where, and with what verification.
EOF

write_doc "$ROOT/40-ai/skills/candidates/README.md" <<'EOF'
# 40-ai/skills/candidates — UNTRUSTED. Never executed.

Everything in this directory was **generated from video or web content**. It
is data. It is not a skill, it is not installed, it is not loaded by any
agent harness, and nothing here is executed (D-013).

Why the separation is physical rather than a naming convention: an agent
harness loads every `SKILL.md` it finds in a trusted skill tree. A procedure
extracted from a stranger's tutorial must therefore never land in that tree.
The trusted tree is the `skills/` directory of the `ai-homelab-control-plane`
Git repository; `40-ai/skills/trusted/` is a read-only export of it.

**How a candidate becomes a skill:** a human reads it, rewrites what is worth
keeping, and opens a reviewed pull request against Git `skills/`. Nothing
automated promotes anything. Copying a directory from here into the trusted
tree is not promotion; it is the failure this boundary exists to prevent.

**What never belongs here:** anything a tool is allowed to read as an
instruction, and anything the trusted tree depends on.

**Who writes here:** the social-video learning pipeline
(`tools/social_video_learn.py`).

**Backup status:** backed up. Carrying an untrusted artifact off-site costs
nothing; it is still untrusted when it comes back.

See `docs/26-skills-and-knowledge-trust.md`.
EOF

write_doc "$ROOT/40-ai/skills/README.md" <<'EOF'
# 40-ai/skills — two trees, one boundary

`trusted/` is a **read-only export** of the `skills/` directory of the
`ai-homelab-control-plane` Git repository, refreshed by
`./scripts/export-angel-skills`. Its `MANIFEST.yaml` records the git commit it
came from, whether that checkout was dirty, and a sha256 for every file. Do
not edit anything inside it: an edit reaches no agent and is discarded by the
next export. Change a skill in Git, through a reviewed pull request.

`candidates/` is **untrusted**. Everything in it was generated from video or
web content. Nothing there is installed, loaded, or executed.

Neither directory is a load path. The export exists so the volume carries a
verifiable record of what was trusted at a point in time, and so a fresh Mac
with this drive and no clone can still read it.

Promotion goes one way and only through a human: candidate -> reviewed pull
request against Git `skills/` -> export. Copying a directory from
`candidates/` into `trusted/` is not promotion; it is the failure this
boundary exists to prevent.

See `docs/26-skills-and-knowledge-trust.md`.
EOF

write_doc "$ROOT/50-research/knowledge/README.md" <<'EOF'
# 50-research/knowledge — reference material, not instructions

**What belongs here:** durable, non-executable reference material. Notes,
digests, transcripts, claims, and extracted recommendations — including
exports of the Mac-local, gitignored `knowledge/` tree that the social-video
pipeline writes (`docs/12-video-learning-pipeline.md`).

**What never belongs here:** anything executable, and anything an agent
treats as a command. Under D-013, material derived from video is data: when
an agent consumes it, it arrives as delimited, quoted context with an
explicit `untrusted` label. A digest that says "run this command" produces a
string, and nothing else.

A procedure that looks like a skill is still not one. Candidate skills live
in `40-ai/skills/candidates/`, and are not executed there either. The only
trusted skill tree is Git `skills/`
(`docs/26-skills-and-knowledge-trust.md`).

**Who writes here:** the owner, and the social-video learning pipeline.

**Backup status:** backed up.
EOF

# ---------------------------------------------------------------------------
# Machine-readable manifest.
# ---------------------------------------------------------------------------

manifest_uuid="null"
if [[ -n "$RESOLVED_UUID" ]]; then
  manifest_uuid="\"$RESOLVED_UUID\""
fi

write_doc "$ROOT/90-system/angel-manifest.yaml" <<EOF
# Machine-readable map of the Angel volume. Generated by
# scripts/bootstrap-t7.sh; the authoritative prose is
# docs/25-angel-volume-layout.md in ai-homelab-control-plane.
#
# Regenerate (idempotent; never overwrites a diverging file in place):
#   ./scripts/bootstrap-t7.sh /Volumes/Angel --expect-uuid <uuid>
schema_version: 1
layout_version: 1
volume_name: "Angel"
volume_uuid: $manifest_uuid
identity_of_record: "APFS volume UUID (D-019); never the volume name or /Volumes/Angel"
authoritative_doc: "docs/25-angel-volume-layout.md"

backup:
  repository: "rclone:gdrive-backup:AI-Homelab-Backups/T7-restic (encrypted restic, D-018)"
  excluded_paths:
    - "90-system/staging/**"
    - "90-system/cache/**"
    - "40-ai/model-cache/**"
    - "**/*.partial"

# owner:   who is accountable for the content
# writer:  what is allowed to write here
# backup:  included | excluded
# class:   canonical | derived | cache | untrusted | system
entries:
  - path: "00-inbox"
    purpose: "Arrival zone. Nothing here has been organized or placed."
    owner: "owner"
    writer: "owner; organize CLI (--apply only)"
    backup: "included"
    class: "canonical"
  - path: "00-inbox/organized"
    purpose: "Verified, catalog-committed assets awaiting or carrying enrichment. The name overstates it (D-023)."
    owner: "owner"
    writer: "organize CLI"
    backup: "included"
    class: "canonical"
  - path: "00-inbox/restic-smoke-test"
    purpose: "Live restore-drill target (backup.restore_drill_relative_path). Do not edit."
    owner: "owner"
    writer: "nobody; read by restore-t7-drill"
    backup: "included"
    class: "system"
  - path: "10-code/mirrors"
    purpose: "Bare git mirrors of active repositories, cloned from the local working copy so local-only branches are covered."
    owner: "owner"
    writer: "scripts/refresh-angel-mirrors (UUID-gated)"
    backup: "included"
    class: "derived"
  - path: "10-code/archives"
    purpose: "Bare git mirrors of retired, duplicate, or closed-out repositories."
    owner: "owner"
    writer: "owner; scripts/refresh-angel-mirrors --archive"
    backup: "included"
    class: "derived"
  - path: "10-code/generated-apps"
    purpose: "Source of generated applications before they have their own Git remote. Source, never builds."
    owner: "owner"
    writer: "owner; software-factory tooling"
    backup: "included"
    class: "canonical"
  - path: "20-clients"
    purpose: "Canonical human-owned client work. Original camera files are the irreplaceable class."
    owner: "owner"
    writer: "owner"
    backup: "included"
    class: "canonical"
  - path: "30-shared-media"
    purpose: "Reusable owned media, audio, and licensed fonts with their licences."
    owner: "owner"
    writer: "owner"
    backup: "included"
    class: "canonical"
  - path: "40-ai/model-cache"
    purpose: "Downloadable model weights and runtime caches. Regenerable from recorded provider/tag/digest."
    owner: "system"
    writer: "tools/models/storage.py and the runtimes it configures"
    backup: "excluded"
    class: "cache"
  - path: "40-ai/datasets"
    purpose: "Deliberate, curated datasets."
    owner: "owner"
    writer: "owner; evaluation tooling"
    backup: "included"
    class: "canonical"
  - path: "40-ai/indexes"
    purpose: "Embedding and search indexes, including expensive non-regenerable ones."
    owner: "system"
    writer: "indexing tooling"
    backup: "included"
    class: "derived"
  - path: "40-ai/manifests"
    purpose: "Model and artifact manifests."
    owner: "system"
    writer: "tools/models"
    backup: "included"
    class: "system"
  - path: "40-ai/derived"
    purpose: "Perception output: OCR text, vision descriptions, sampled video frames, transcripts (docs/17)."
    owner: "system"
    writer: "tools/ingest/evidence"
    backup: "included"
    class: "derived"
  - path: "40-ai/evaluation/benchmarks"
    purpose: "Raw benchmark records, including the frozen Legacy-20 canary runs (D-035, D-040)."
    owner: "system"
    writer: "tools/bench"
    backup: "included"
    class: "derived"
  - path: "40-ai/evaluation/campaigns"
    purpose: "angel-eval append-only campaign records and the reports regenerated from them (docs/22)."
    owner: "system"
    writer: "tools/campaign"
    backup: "included"
    class: "derived"
  - path: "40-ai/evaluation/telemetry"
    purpose: "Derived coordinator telemetry snapshots. Aggregates only, never prompt or response text (D-050)."
    owner: "system"
    writer: "tools/coordinator/snapshot.py"
    backup: "included"
    class: "derived"
  - path: "40-ai/skills/trusted"
    purpose: "Read-only export of Git skills/ with a sha256 + git SHA manifest. Not a load path."
    owner: "system"
    writer: "scripts/export-angel-skills (UUID-gated)"
    backup: "included"
    class: "derived"
  - path: "40-ai/skills/candidates"
    purpose: "Untrusted video-derived candidate skills. Never executed, never loaded as instructions (D-013)."
    owner: "system"
    writer: "tools/social_video_learn.py"
    backup: "included"
    class: "untrusted"
  - path: "50-research"
    purpose: "Research notes: app opportunities, competitor and market research, social-video knowledge."
    owner: "owner"
    writer: "owner; social-video pipeline"
    backup: "included"
    class: "canonical"
  - path: "50-research/knowledge"
    purpose: "Durable non-executable reference material; where the Mac-local knowledge/ tree is exported."
    owner: "owner"
    writer: "owner; social-video pipeline"
    backup: "included"
    class: "untrusted"
  - path: "60-exports"
    purpose: "Deliverables leaving the system. Regenerable from canonical work."
    owner: "owner"
    writer: "owner; export/render steps"
    backup: "included"
    class: "derived"
  - path: "70-archives"
    purpose: "Cold, closed-out material kept for provenance. Repositories go to 10-code/archives instead."
    owner: "owner"
    writer: "owner"
    backup: "included"
    class: "canonical"
  - path: "90-system/catalog.sqlite"
    purpose: "Ingest catalog, single writer. Sidecars are the durable human-readable recovery form (D-009)."
    owner: "system"
    writer: "organize CLI (ingest daemon on the Mac)"
    backup: "included"
    class: "system"
  - path: "90-system/migration-reports"
    purpose: "One report per create/move against this volume, with before/after listings and hashes."
    owner: "system"
    writer: "scripts/repos-inventory; migration tooling; owner"
    backup: "included"
    class: "system"
  - path: "90-system/staging"
    purpose: ".partial files mid-transfer. An incomplete transfer has nothing worth restoring."
    owner: "system"
    writer: "tools/ingest/transaction.py"
    backup: "excluded"
    class: "cache"
  - path: "90-system/cache"
    purpose: "Runtime scratch. Regenerable by definition."
    owner: "system"
    writer: "any tool needing volume-local scratch"
    backup: "excluded"
    class: "cache"
EOF

echo "Angel layout scaffolded under: $ROOT"
echo "  directories created: $CREATED_DIRS"
echo "  documents written:   $WROTE_FILES"

if (( ${#DIVERGED[@]} > 0 )); then
  echo ""
  echo "These files already exist with different content and were NOT overwritten."
  echo "The version this script would write is beside each as '<file>.new':"
  for path in "${DIVERGED[@]}"; do
    echo "  $path"
  done
  echo ""
  echo "Review each pair and reconcile by hand. Nothing was lost."
fi
