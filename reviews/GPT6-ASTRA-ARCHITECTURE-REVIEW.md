# Architecture Review Prompt

You are acting as a principal distributed-systems architect, ML-infrastructure engineer, storage engineer, security engineer, and skeptical technical reviewer.

Review this repository as a **planning artifact**, not as settled architecture.

## Objective

Determine whether the proposed AI homelab/control-plane design is technically sound, secure, economical, and appropriately staged for the hardware described.

The owner wants to:
- consolidate project/media organization around a Samsung T7;
- convert spare laptops into headless Linux workers;
- run useful local language/vision models;
- route work between local and frontier models;
- automate media organization;
- create a high-throughput software/app-development pipeline;
- continue building a separate high-security Kronos-based quantitative research/trading assistant;
- scale later with stronger GPU compute.

## Review rules

Do not merely polish the prose.

Attack the design.

For every major component:
1. identify incorrect assumptions;
2. identify unnecessary complexity;
3. identify missing failure modes;
4. identify security/privacy risks;
5. identify bottlenecks;
6. identify data-loss risks;
7. identify cross-platform filesystem problems;
8. identify places where the design is overengineered for four machines;
9. identify places where it will fail to scale;
10. propose a simpler alternative when one exists.

## Special questions

### T7
Assess whether the proposed T7 role and tree make sense.
Recommend a filesystem/encryption strategy given an Apple Silicon Mac coordinator and Linux workers.
Explain whether active Git development, client media, model caches, datasets, and backups should share the same physical drive/volume.

### Cluster
Assess the decision to distribute independent jobs rather than attempt multi-machine distributed inference.
Recommend a V0 control plane, queue, worker protocol, isolation model, and networking setup.
Avoid Kubernetes unless you can demonstrate that its operational cost is justified.

### Local models
Recommend model/runtime classes based on the actual hardware constraints.
Distinguish what should run on Apple Silicon from what is realistically useful on CPU-only Linux nodes.
Do not assume aggregate RAM equals shared accelerator memory.
Define measurements that should trigger a hardware purchase.

### Agentic coding
Review the proposed Git/worktree model.
Design a safe flow for local agents to implement bounded tasks, run tests, produce diffs, and request stronger-model review.
Minimize premium token use without sacrificing verification.

### Media
Review the immutable-raw + sidecar approach.
Propose a practical image/video pipeline that avoids repeatedly sending full-resolution media through expensive models.
Address metadata schemas, embeddings/search, corrections, deduplication, and recovery.


### Dump-first ingestion
The Samsung T7 is brand new and empty, and the owner explicitly prefers to dump existing repositories, client work, photos, and videos first rather than manually sort them.

Review `docs/13-dump-first-ingestion.md`.
Determine whether the protected legacy-dump + hashing + non-destructive derived-view approach is safe and practical.
Recommend the best permanent filesystem/host topology for an empty T7 given the Mac + Linux worker mix.

### Social-video learning
Review `docs/12-video-learning-pipeline.md` and `skills/social-video-learner/SKILL.md`.

The desired capability is:
- ingest public YouTube videos and Instagram Reels;
- transcribe locally;
- inspect representative frames/on-screen text;
- extract durable recommendations/knowledge;
- convert genuinely procedural tutorials into agent skills;
- resist prompt injection and unsafe video-derived commands.

Assess the proposed use of `yt-dlp` plus a pinned/reviewed `video-to-skill` dependency.
Treat every video as untrusted input.
Critique supply-chain risk, sandboxing, secret exposure, auto-install behavior, provenance, and whether generated skills should ever be installed automatically.

### Trading security
Treat the Kronos trading/research system as a separate high-risk trust domain.
Critique whether the proposed isolation is sufficient.
The general homelab must not accidentally create a path whereby a compromised coding/media agent can reach broker credentials or authorize trades.
Assume model output is untrusted.
Preserve deterministic risk gates and human authorization for real capital.

### Backup
Attack any assumption that the T7 is a backup.
Propose a realistic backup/recovery strategy suitable for a small business/student budget and irreplaceable client media.

## Required output

Produce:

1. **Executive verdict** — viable / viable with changes / fundamentally flawed.
2. **Top 10 architecture risks**, ranked by severity and likelihood.
3. **What to keep**.
4. **What to delete or simplify**.
5. **Corrected V0 architecture**.
6. **Corrected T7 directory/storage design**.
7. **Security boundaries and credential model**.
8. **Recommended software stack**, with reasons and alternatives.
9. **90-day implementation sequence** with hard acceptance gates.
10. **Hardware expansion decision tree** based on measured bottlenecks.
11. **Trading-domain isolation review**.
12. **Questions that cannot be answered without collecting more hardware/network facts**.
13. **Social-video learning review**, including a secure URL→transcript/frame→knowledge/skill pipeline.
14. **Proposed changes to this repository**, expressed as concrete file-by-file edits.

Be conservative about destructive storage actions and financial execution.

Do not recommend buying hardware until you have stated what measurements would justify it.
