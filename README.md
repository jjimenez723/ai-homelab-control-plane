# AI Homelab Control Plane

Planning repository for a private, scalable AI automation environment built around existing personal computers, a Samsung T7 SSD, local/open-weight models, and selectively used frontier models.

This repository is **documentation and control-plane design only**. It should not contain client media, model weights, production secrets, trading credentials, private datasets, or copies of unrelated source repositories.

## North-star objective

Build a system that can:

1. coordinate multiple heterogeneous computers as independent workers;
2. route tasks to local or frontier models based on capability, cost, latency, privacy, and risk;
3. intelligently ingest, classify, tag, rename, and organize client media;
4. make software repositories highly legible to coding agents;
5. support a repeatable app research → specification → implementation → testing → deployment pipeline;
6. preserve a strict security boundary around high-risk systems such as the Kronos-based trading assistant;
7. scale later by adding GPU nodes without redesigning the whole platform.

## Current hardware

| Node | Approximate hardware | Initial role |
|---|---|---|
| `mac-m4pro` | Apple M4 Pro, 48 GB unified memory | Primary workstation, strongest current local inference node, coordinator |
| `dell-xeon` | Dell Precision 7520-class, Xeon, 32 GB RAM | Headless Linux worker; CPU jobs, builds, indexing, automation |
| `dell-i7` | Dell, Intel i7, 32 GB RAM | Headless Linux worker; builds, tests, queues, scraping/ETL |
| `dell-lite` | Older Dell, 8 GB RAM, broken screen | Lightweight infrastructure/monitoring worker |
| `t7` | Samsung T7 SSD | Primary portable data/project workspace; **not a backup by itself** |

Hardware details are intentionally marked approximate until inventoried from each machine.

## Architecture principle

Do **not** begin by trying to pool RAM and split one large model across the machines.

Version 0 should distribute **jobs**, not model tensors:

```text
                         YOU
                          |
                    Control Plane
                          |
             +------------+------------+
             |            |            |
          Router        Queue       Registry
             |            |            |
        +----+----+-------+-------+----+----+
        |         |               |         |
     mac-m4pro  dell-xeon       dell-i7   dell-lite
        |         |               |         |
     inference   builds        tests/ETL   monitoring
        |
    Frontier APIs (only when policy allows)
```

## Important boundaries

- GitHub remains the source of truth for version-controlled source code.
- The T7 may hold clean clones, workspaces, media, datasets, manifests, and exports.
- Secrets must not live in ordinary project folders or be synchronized casually.
- Trading infrastructure is a separate trust/security domain.
- AI may propose actions; deterministic policy gates decide whether high-risk actions are allowed.
- No system should treat the T7 as the only copy of irreplaceable data.
- Nodes should eventually be replaceable workers, not pets that contain unique state.

## Repository map

```text
.
├── README.md
├── PRINCIPLES.md
├── docs/
│   ├── 01-hardware-inventory.md
│   ├── 02-t7-filesystem.md
│   ├── 03-compute-cluster.md
│   ├── 04-model-routing.md
│   ├── 05-media-pipeline.md
│   ├── 06-software-factory.md
│   ├── 07-trading-security-boundary.md
│   ├── 08-security-threat-model.md
│   ├── 09-backup-recovery.md
│   ├── 10-migration-plan.md
│   ├── 11-open-decisions.md
│   ├── 12-video-learning-pipeline.md
│   └── 13-dump-first-ingestion.md
├── data/
│   └── video-learning-queue.yaml
├── skills/
│   └── social-video-learner/
│       └── SKILL.md
├── reviews/
│   └── GPT6-ASTRA-ARCHITECTURE-REVIEW.md
├── templates/
│   ├── project-manifest.yaml
│   ├── client-manifest.yaml
│   ├── media-sidecar.example.json
│   └── node-manifest.yaml
└── scripts/
    └── bootstrap-t7.sh
```

## First implementation milestone

The first milestone is intentionally boring:

1. inventory every machine;
2. prepare the empty T7;
3. create a protected `00-inbox/legacy-dump/` landing zone;
4. dump existing repos/media/client work with minimal manual sorting;
5. let the ingestion service inventory, hash, classify, tag, and propose organization without deleting originals;
6. generate checksums for irreplaceable media;
7. establish a second backup before any source deletion;
8. only then begin automating ingestion and worker nodes.

See `docs/10-migration-plan.md`.

## Review

Before implementing the cluster, give this repository to a strong planning/reasoning model and use:

`reviews/GPT6-ASTRA-ARCHITECTURE-REVIEW.md`

The model should attack the architecture, not merely rewrite it.

## Social-video learning

The stack now includes a local-first `social-video-learner` skill for YouTube and public Instagram Reels. It transcribes, inspects key frames/on-screen text, produces durable knowledge notes, and routes genuinely procedural tutorials into a reviewable skill-compilation flow. See `docs/12-video-learning-pipeline.md`.
