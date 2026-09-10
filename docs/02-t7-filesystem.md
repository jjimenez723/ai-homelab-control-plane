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

## Filesystem recommendation to review before formatting

If the T7 primarily stays attached to the Mac and Linux nodes access it over the network, **APFS (Encrypted)** is a strong candidate. The Linux workers then consume files through a network protocol rather than mounting APFS directly.

Do not automatically choose exFAT merely for cross-platform portability if the drive will contain active developer work. exFAT gives up useful filesystem semantics and is a poor foundation for some development workflows.

Do not choose a case-sensitive filesystem until existing repositories are checked for filename-case assumptions.

This is a review item, not an instruction to erase/reformat the drive today.

## Code execution rule

Avoid making every Linux node build directly inside a network-mounted T7 repository.

Preferred pattern:
1. Git remote is source of truth.
2. T7 contains a clean local clone/workspace.
3. Worker receives commit/ref/task.
4. Worker creates or updates its own local clone/worktree on internal storage.
5. Worker builds/tests locally.
6. Results/artifacts are returned to the control plane.

This avoids turning storage/network latency into the bottleneck and reduces cross-OS filesystem problems.
