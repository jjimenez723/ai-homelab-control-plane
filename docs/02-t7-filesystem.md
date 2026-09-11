# 02 — T7 Filesystem and Information Architecture

## Role of the T7

The T7 is the first organized storage layer for:
- clean Git clones/workspaces;
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

## Recommended logical tree

```text
T7/
├── 00-inbox/
│   ├── camera-imports/
│   ├── client-uploads/
│   ├── downloads/
│   └── quarantine/
│
├── 10-code/
│   ├── products/
│   │   ├── mafi/
│   │   ├── squad-quit/
│   │   └── brazil-nurse-scheduler/
│   ├── research/
│   │   └── kronos-quant-lab/
│   ├── client-systems/
│   └── internal-tools/
│
├── 20-clients/
│   └── <client-slug>/
│       ├── manifest.yaml
│       ├── 00-admin/
│       ├── 10-briefs/
│       ├── 20-brand/
│       ├── 30-media/
│       │   └── YYYY/
│       │       └── YYYY-MM-DD_<project-slug>/
│       │           ├── 00-raw/
│       │           ├── 10-selected/
│       │           ├── 20-proxies/
│       │           ├── 30-edits/
│       │           └── 40-exports/
│       ├── 40-web/
│       ├── 50-social/
│       └── 90-archive/
│
├── 30-shared-media/
│   ├── stock-owned/
│   ├── audio/
│   ├── fonts-licenses/
│   └── reusable-assets/
│
├── 40-ai/
│   ├── model-cache/
│   ├── datasets/
│   ├── indexes/
│   ├── manifests/
│   └── evaluation/
│
├── 50-research/
│   ├── app-opportunities/
│   ├── competitor-research/
│   └── market-research/
│
├── 60-exports/
│   ├── temporary/
│   └── handoff/
│
├── 70-archives/
│
└── 90-system/
    ├── inventories/
    ├── checksums/
    ├── logs/
    └── migration-reports/
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

Current decision: `DECISIONS.md` D-017 supersedes D-002 and the APFS-clone portion of D-003. The original Astra review's APFS recommendation is retained as historical reasoning, not the current implementation target.

- Keep the existing Samsung T7 as **exFAT**. **Do not reformat it** as part of this project.
- At-rest access control is provided by the T7's Samsung Magician password/security feature, which the owner has enabled. Store the password in the password manager. The owner unlocks the device before use.
- The T7 remains primarily **Mac-attached** as the storage host. If it is locked, absent, read-only, or has the wrong configured volume UUID, ingestion exits without modifying source files.
- exFAT portability is useful, but its limitations are explicit architecture constraints: no APFS clonefile/snapshots and no reliance on POSIX ownership, ACL, symlink, hardlink, xattr, or filesystem-immutability semantics.
- Active Git development stays on the Mac/internal native filesystem or Linux worker filesystems. Do not run package-manager-heavy working trees directly from the T7.
- The D-018 encrypted restic/Google Drive backup remains required now; the D-004 Dell/B2 chain remains the desired later independent backup. Hardware password protection is access control, not backup.

## Derived-view primitive

The organized view under `20-clients/`, `10-code/`, etc. is derived from the catalog using **catalog-backed references or verified ordinary copies** when a physical view is needed.

Rules:
- never use hardlinks for the organized view;
- do not call `cp -c` or assume clonefile support;
- a copy must be treated as a derivative, never as the sole canonical original;
- dry-run must report the estimated additional disk usage before materializing a large copied view;
- view regeneration remains idempotent and hash-aware.

After hashing, legacy-dump protection is enforced by checksum manifests, catalog state, backup policy, and application-level refusal to mutate legacy originals. Do not rely on `chflags` or POSIX read-only permissions on exFAT.

## Catalog placement

The ingest catalog (`90-system/catalog.sqlite`, single writer) lives **on the T7** so it travels with the data; per-asset sidecars remain the durable human-readable recovery form. Because the volume is exFAT, the writer must close/checkpoint SQLite cleanly before planned ejects, and the catalog/sidecars must be covered by the backup chain. Embedding indexes live beside it. Only the ingest daemon on the Mac writes to it; everything else reads a snapshot/exported copy. See D-009 and D-017.

## Code execution rule

Avoid making every Linux node build directly inside a network-mounted T7 repository.

Active development clones live on the Mac's **internal** disk, not on the T7. The T7 may hold exported archives or bare mirrors where tested, but it is not the working filesystem for package-manager-heavy repositories because exFAT lacks native POSIX/symlink semantics. Running an editor, watcher, or package manager against removable USB storage also risks work when the cable moves.

Preferred pattern:
1. Git remote is source of truth.
2. T7 contains a clean local clone/workspace.
3. Worker receives commit/ref/task.
4. Worker creates or updates its own local clone/worktree on internal storage.
5. Worker builds/tests locally.
6. Results/artifacts are returned to the control plane.

This avoids turning storage/network latency into the bottleneck and reduces cross-OS filesystem problems.
