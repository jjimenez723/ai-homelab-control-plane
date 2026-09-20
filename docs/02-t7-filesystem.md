# 02 — T7 Filesystem and Information Architecture

## Role of the T7

The T7 is the first organized storage layer for:
- clean Git mirrors and archives (never working trees -- see the code-execution rule);
- client source media;
- app/project assets;
- research datasets;
- model artifacts that are practical to keep locally;
- structured manifests and indexes;
- exports and deliverables.

It is **not**:
- the sole backup;
- a secret store;
- a place to dump every cache;
- the database of record for production services;
- a substitute for Git remotes.

## The authoritative layout is docs/25

`docs/25-angel-volume-layout.md` is the authoritative map of the volume
(D-061). It exists in three synchronised forms -- that prose, the executable
`scripts/bootstrap-t7.sh`, and the machine-readable
`/Volumes/Angel/90-system/angel-manifest.yaml` -- plus a `README.md` at the
volume root and in each top-level directory.

This document keeps what `docs/25` does not repeat: why the volume is shaped
this way. The filesystem decision, the catalog placement, the derived-view
primitive, and the code-execution rule below are all still authoritative
here.

The top level, unchanged since the bootstrap scaffold and deliberately not
renamed (the names are referenced by config, code, tests, and a live restore
drill):

```text
00-inbox/  10-code/  20-clients/  30-shared-media/  40-ai/
50-research/  60-exports/  70-archives/  90-system/
```

What v1 of the layout added inside them -- bare repository mirrors and
archives under `10-code/`, generated application source, derived perception
evidence, benchmark/campaign/telemetry splits under `40-ai/evaluation/`, the
trusted and untrusted skill trees, durable knowledge, and volume-local
runtime cache -- is specified in `docs/25`, not here.

`20-clients/<client-slug>/` retains the per-client shape this document
introduced, and it is the canonical, irreplaceable data on the volume:

```text
20-clients/<client-slug>/
├── manifest.yaml
├── 00-admin/  10-briefs/  20-brand/
├── 30-media/YYYY/YYYY-MM-DD_<project-slug>/
│   └── 00-raw/  10-selected/  20-proxies/  30-edits/  40-exports/
├── 40-web/  50-social/
└── 90-archive/
```

## Why numeric prefixes?

They make the top-level order stable across Finder, terminals, Linux mounts, scripts, and agent tooling.

## Naming convention

Prefer:
```text
YYYY-MM-DD_client_project_subject_variant.ext
```

Example:
```text
2026-09-09_stone-bellisimo_kitchen-island_quartz-wide_001.jpg
```

Do not force descriptive renaming directly onto camera originals until the ingestion system is proven. Initially preserve raw filenames in `00-raw/` and write proposed names/tags into sidecar metadata.

## Filesystem decision

Current decision: `DECISIONS.md` D-019 supersedes D-017 and restores the substance of D-002. The T7 was erased and recreated on 2026-09-11.

- The Samsung T7 (`angel`) is **APFS (Encrypted)**, case-insensitive, mounted at `/Volumes/Angel`. It is not reformatted again as part of this project.
- At-rest protection is APFS/FileVault encryption. Store the passphrase in the password manager. The volume is unlocked before use; a locked volume is simply not mounted and ingestion treats it as unavailable.
- The T7 remains primarily **Mac-attached** as the storage host. If it is locked, absent, read-only, or has the wrong configured volume UUID, ingestion exits without modifying source files.
- **Identity of record is the macOS Volume UUID**, held once in the gitignored `config/ingest.yaml` (`t7.volume_uuid`). Neither the Finder name `Angel` nor the path `/Volumes/Angel` is ever sufficient on its own: `tools/ingest/t7.py` and `tools/backup.py` resolve the volume by UUID through `diskutil` and refuse to proceed on a mismatch. A reformat issues a new UUID, so re-erasing the drive requires updating that one value.
- Encrypted APFS cannot be mounted by the Linux workers at all. Worker access stays job-payload or read-only SMB from the Mac.
- Active Git development stays on the Mac/internal native filesystem or Linux worker filesystems. Do not run package-manager-heavy working trees directly from the T7.
- The D-018 encrypted restic/Google Drive backup remains required now; the D-004 Dell/B2 chain remains the desired later independent backup. Volume encryption is access control, not backup.

## Derived-view primitive

The organized view under `20-clients/`, `10-code/`, etc. is derived from the catalog using **catalog-backed references or verified ordinary copies** when a physical view is needed.

Rules:
- never use hardlinks for the organized view (a shared inode means editing the view edits the original — this rule is independent of the filesystem);
- `clonefile`/`cp -c` is technically available again under D-019, but the derived view does not use it until a decision explicitly adopts it;
- a copy must be treated as a derivative, never as the sole canonical original;
- dry-run must report the estimated additional disk usage before materializing a large copied view;
- Phase 3 records client/project/tag proposals and materializations in the catalog,
  aborts when proposal drift exceeds 20%, and requires an explicit apply path;
- see `docs/31-phase3-semantic-catalog.md` for the semantic schema and pilot gate.
- view regeneration remains idempotent and hash-aware.

After hashing, legacy-dump protection is enforced by checksum manifests, catalog state, backup policy, and application-level refusal to mutate legacy originals. APFS makes `chflags`/POSIX read-only bits meaningful again, but they remain a defence in depth rather than the mechanism of record.

## Catalog placement

The ingest catalog (`90-system/catalog.sqlite`, single writer) lives **on the T7** so it travels with the data; per-asset sidecars remain the durable human-readable recovery form. The writer must still close/checkpoint SQLite cleanly before planned ejects, and the catalog/sidecars must be covered by the backup chain. Embedding indexes live beside it. Only the ingest daemon on the Mac writes to it; everything else reads a snapshot/exported copy. See D-009 and D-019.

## Code execution rule

Avoid making every Linux node build directly inside a network-mounted T7 repository.

Active development clones live on the Mac's **internal** disk under `~/Projects/<repo>`, not on the T7. The T7 holds bare mirrors (`10-code/mirrors/<name>.git`) and archives (`10-code/archives/`) -- this is now implemented, not hypothetical (D-061, `./scripts/refresh-angel-mirrors`) -- but it is not the working filesystem for package-manager-heavy repositories. APFS removes the old exFAT symlink/permission objection; the remaining reason stands on its own — running an editor, watcher, or package manager against removable USB storage risks work when the cable moves.

Preferred pattern:
1. Git remote is source of truth.
2. T7 contains a clean bare mirror, refreshed from the local working copy so
   that local-only branches the remote has never seen are covered too.
3. Worker receives commit/ref/task.
4. Worker creates or updates its own local clone/worktree on internal storage.
5. Worker builds/tests locally.
6. Results/artifacts are returned to the control plane.

This avoids turning storage/network latency into the bottleneck and reduces cross-OS filesystem problems.
