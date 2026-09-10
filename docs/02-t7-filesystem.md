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

Decided in `DECISIONS.md` (D-002, D-003); rationale in `reviews/ASTRA-V0-ARCHITECTURE-REVIEW.md` §10.

- Format the T7 as **APFS (Encrypted), case-insensitive** (`diskutil apfs` default). Store the recovery key in the password manager, not on any node.
- The T7 is **Mac-attached only**. Linux cannot mount encrypted APFS; the Linux drivers for unencrypted APFS are read-only/experimental and must not be used for client data. Linux workers never see the T7 as a block device or a mounted volume. They receive job payloads (files copied by the coordinator) or, for bulk reads, a read-only SMB export from the Mac.
- Case-insensitive matches the Mac's internal volume, so every Git repository behaves identically on both. A case-sensitive volume breaks Xcode, Adobe, Unity, Steam, and several package managers. If an old Linux-born repository has two files differing only by case, the ingest inventory flags it and it is fixed in Git, not by choosing a filesystem.
- exFAT is rejected: no permissions, no symlinks/hardlinks (breaks `npm`/`pnpm`/`uv`), no clonefile, no snapshots, no encryption.
- Never bit-lock the T7 to one machine's Secure Enclave; the APFS passphrase must be a real passphrase so the drive can be unlocked on another Mac after a loss.

## Derived-view primitive

The organized view under `20-clients/`, `10-code/`, etc. is materialized from the catalog using **APFS clones** (`cp -c`, `clonefile(2)`): zero extra space, a separate inode, so editing a file in the view never mutates the original in `00-inbox/legacy-dump/`. Hardlinks are rejected for this reason. Symlinks are acceptable for read-only views but break in Finder-centric workflows.

After hashing, the legacy dump is locked with `chflags -R uchg` and owner read-only permissions. Nothing in this repository unlocks it; unlocking is a manual step recorded in `90-system/migration-reports/`.

## Catalog placement

The ingest catalog (`90-system/catalog.sqlite`, WAL mode, single writer) lives **on the T7** so it travels with the data and a restore is a file copy. Embedding indexes live beside it. Only the ingest daemon on the Mac writes to it; everything else reads a snapshot copy. See review §10 and §11.

## Code execution rule

Avoid making every Linux node build directly inside a network-mounted T7 repository.

Active development clones live on the Mac's **internal** disk, not on the T7. The T7 holds mirrors (`git clone --mirror`) and workspaces that are not open in an editor. Running an editor, watcher, or package manager against a USB volume is slower and loses work when the cable moves. (This requires freeing the internal disk first; it was measured at 98% full on 2026-09-10.)

Preferred pattern:
1. Git remote is source of truth.
2. T7 contains a clean local clone/workspace.
3. Worker receives commit/ref/task.
4. Worker creates or updates its own local clone/worktree on internal storage.
5. Worker builds/tests locally.
6. Results/artifacts are returned to the control plane.

This avoids turning storage/network latency into the bottleneck and reduces cross-OS filesystem problems.
