# Decisions

Decision log for the AI homelab control plane. One entry per decision; newest at the bottom. A decision is changed by adding a new entry that supersedes it, never by editing history. Status: `proposed` (in an open PR), `accepted` (merged to `main`), `superseded by D-xxx`.

Reasoning lives in `reviews/ASTRA-V0-ARCHITECTURE-REVIEW.md` (cited as "review §n"). Keep entries short: context, decision, consequence, trigger to revisit.

---

## D-001 — Distribute jobs, not model tensors (V0)
- Status: accepted (PRINCIPLES.md §1, 2026-09-09); reaffirmed 2026-09-10 (review §9).
- Decision: no cross-machine tensor/pipeline parallelism over Wi-Fi/1 GbE between an Apple Silicon Mac and Intel laptops. Every model runs whole on one node.
- Revisit trigger: a single model the owner measurably needs that does not fit the largest single node, **and** an NVIDIA node exists with a ≥10 GbE link to a peer.

## D-002 — T7 filesystem: APFS (Encrypted), case-insensitive
- Status: proposed 2026-09-10 (review §10).
- Decision: format the T7 as APFS Encrypted, case-insensitive. Passphrase in the password manager. No exFAT, no case-sensitive APFS, no ext4.
- Consequence: Linux nodes can never mount the T7. All Linux access is via job payloads or read-only SMB from the Mac.
- Revisit trigger: the T7 must physically move between a Mac and a Linux box routinely (then a second, ext4-formatted drive is bought; the T7 is not reformatted).

## D-003 — The T7 is Mac-attached; the Mac is the storage host
- Status: proposed 2026-09-10 (review §10, §13).
- Decision: the T7 attaches to `mac-m4pro` over USB 3.2 Gen 2. Hashing, inventory, cataloging, and clonefile view generation run on the Mac. Linux workers do compute-heavy per-file jobs (transcode, transcription batch) on copies.
- Consequence: the Mac must stay awake/attached during ingest; `caffeinate` and a launchd job handle this. Internal disk must have ≥ 60 GB free (it had 12 GB on 2026-09-10).

## D-004 — Backup chain: T7 → append-only restic on dell-xeon → Backblaze B2
- Status: proposed 2026-09-10 (review §12).
- Decision: restic, rest-server `--append-only` on `dell-xeon`'s internal disk, nightly `restic copy` to B2 with a delete-less application key. Monthly restore drill. Consolidating onto the T7 is not a backup.
- Consequence: no source disk or SD card is wiped until the first successful restic snapshot **and** the B2 copy of that client folder both verify.

## D-005 — Node roster: retire dell-lite; dell-i7 reserved
- Status: proposed 2026-09-10 (review §14).
- Decision: `dell-lite` (8 GB, broken display) is retired. `dell-xeon` is the first and, for 60 days, only Linux worker. `dell-i7` is provisioned after the worker gate passes and is the designated candidate for a physically separate Kronos execution box.

## D-006 — Queue: SQLite behind a coordinator HTTP API; pull-based workers
- Status: proposed 2026-09-10 (review §15).
- Decision: one coordinator process (Python, on the Mac) owns `jobs.sqlite` and exposes claim/heartbeat/complete over HTTP on the Tailscale interface. Workers poll. Leases, attempts, idempotency keys, and JSON results are in the schema from day one. No Redis/NATS/RabbitMQ/Celery/Temporal/Ray/Nomad/Kubernetes.
- Revisit trigger (any one): >3 concurrent workers, >50k jobs/day, a second writer needed, or the coordinator must survive the Mac being closed — then the same API moves onto PostgreSQL on `dell-xeon`.

## D-007 — Network: Tailscale with ACL tags; no port forwarding
- Status: proposed 2026-09-10 (review §13).
- Decision: Tailscale personal plan; tags `tag:coordinator`, `tag:worker`, `tag:quant`, `tag:backup`; Tailscale SSH; ACL denies `tag:worker → tag:quant` and `tag:worker → tag:backup` entirely. Raw WireGuard rejected (key management for no benefit at this scale).

## D-008 — Inference runtime: Ollama on the Mac for V0; MLX/llama.cpp as measured alternatives; vLLM only on a future NVIDIA node
- Status: proposed 2026-09-10 (review §16).
- Decision: Ollama is the V0 model server (OpenAI-compatible API, fixed context per model, stable Metal). MLX is benchmarked side-by-side for the same model and adopted per-model only when it wins on measured tokens/s **and** passes a 24-hour stability run. CPU inference on Dells is limited to embeddings, whisper/parakeet transcription, and ≤4B classifiers; no CPU coding agents.
- Revisit trigger: an NVIDIA node arrives (vLLM there), or MLX server passes the stability run.

## D-009 — Catalog: SQLite on the T7 with sidecars as the durable, human-readable form
- Status: proposed 2026-09-10 (review §11, §19).
- Decision: `90-system/catalog.sqlite` (WAL, single writer = ingest daemon) is the query layer; per-asset JSON sidecars are the durable record and are regenerated from/into the catalog. Embeddings in `sqlite-vec` beside the catalog. Human corrections are a separate table that always wins over model output.
- Revisit trigger: >5 M assets, >3 volumes, or multi-writer need → PostgreSQL + pgvector on `dell-xeon`, same schema.

## D-010 — Coding agents run on Linux workers in disposable worktrees with per-repo, branch-scoped tokens
- Status: proposed 2026-09-10 (review §21, §22).
- Decision: no coding agent runs as the owner's primary macOS user. Agents run in sandboxed `systemd-run` units on `dell-xeon` with a fresh `git worktree`, a fine-grained GitHub token limited to that repository and to non-`main` branches, no access to `~/`, and network limited to GitHub and package registries. Frontier calls go through the router on the coordinator, never from the worker with a raw API key.

## D-011 — Kronos stays a separate trust domain: separate OS user now, separate machine before any live capital
- Status: proposed 2026-09-10 (review §24).
- Decision: Kronos research moves to a dedicated macOS user (`quant`) on the Mac immediately (separate keychain, home, HF cache). Broker/live credentials exist only inside that user's keychain / a separate password-manager vault. The general cluster may run Kronos **pure-function** jobs (public data in, files out) with no credentials. Before any live-capital gate exists, execution moves to a physically separate machine (`dell-i7` rebuilt, or a mini PC) tagged `tag:quant`, unreachable from `tag:worker`.

## D-012 — Metrics: every job writes a structured record to the catalog; no metrics stack in V0
- Status: proposed 2026-09-10 (review §25).
- Decision: `jobs` rows carry model, tokens, wall time, energy estimate, verification result, retries, escalation, and human-correction flags. A `report` CLI prints weekly summaries. Prometheus/Grafana/OTel are deferred until day 60+ and only if the CLI report is insufficient.

## D-013 — Video-derived artifacts are data, never instructions; candidate skills live outside `skills/`
- Status: proposed 2026-09-10 (review §20, §21; implemented in the same PR).
- Decision: `knowledge/` (ignored) holds all video-derived output including `knowledge/skill-candidates/`. Anything consumed by an agent from `knowledge/` is passed as delimited quoted data with an `untrusted` label. Promotion to `skills/` is a human PR.

## D-014 — Git governance: squash-only, auto-delete branches, GitHub Pro for rulesets, gitleaks + Dependabot in CI
- Status: proposed 2026-09-10 (review §32).
- Decision: repository settings become squash-only merges with branch auto-delete; `main` gets a ruleset (requires PR + `Python tests` + linear history, no force push, no deletion) — which requires GitHub Pro for a private repo; until then a local `pre-push` hook is installed. Dependabot for actions and pip; gitleaks in CI (secret scanning for private repos is a paid GitHub feature). CODEOWNERS and issue templates are not added.

## D-015 — Generated app repositories standardize on six files, not nine
- Status: proposed 2026-09-10 (review §22).
- Decision: `README.md`, `AGENTS.md`, `SPEC.md`, `ARCHITECTURE.md` (with an inline decisions section), `SECURITY.md`, `PLAN.md`. `TESTING.md` folds into `AGENTS.md` (commands) and `PROGRESS.md` is replaced by PR history plus `PLAN.md` checkboxes. A per-task **context pack** is generated by a tool, not hand-written.

## D-016 — Day-to-day ingestion is manual-first; AI never owns source deletion
- Status: accepted 2026-09-10 (owner direction).
- Decision: keep the one-time legacy dump non-destructive, but make recurring cleanup a separate transactional ingestion path. Desktop and Documents are manual-trigger only. Downloads is manual during development and may become opt-in automatic only after fault-injection acceptance tests pass and only for settled files. SD/removable media is detected automatically by volume UUID but a registered SD card still requires explicit apply before originals are removed.
- Safety boundary: crossing from Mac/SD to the T7 is implemented as copy-to-staging → close/flush → SHA-256 verify → atomic destination/register commit → source unlink. The LLM/VLM never receives permission to unlink source paths; it operates only after verified ingestion to classify, tag, name, and choose an organized destination. Unknown removable media and protected/excluded paths are never ingested automatically.
- Consequence: the owner gets a one-command / double-click cleanup flow without files disappearing while actively working. A later launchd watcher may automate Downloads, but mounting the T7 alone never empties Desktop or Documents.
- Relationship to D-004: this permits verified file-level moves during normal ingestion. Bulk wiping/reformatting of a source disk or SD card remains subject to the backup/restore policy in D-004.
- Revisit trigger: the manual triggers become meaningfully burdensome and telemetry/fault tests show the transactional path has operated safely for at least 30 days.

## D-017 — Keep the existing T7 as exFAT with Samsung hardware password protection
- Status: accepted 2026-09-11 (owner direction; Samsung Magician password/security enabled).
- Supersedes: D-002 in full and the `clonefile`/APFS-derived-view portion of D-003. D-003's Mac-attached storage-host role otherwise remains.
- Decision: do **not** reformat the populated Samsung T7. Keep its existing exFAT filesystem and use the T7's Samsung Magician password/security layer for at-rest protection. The owner unlocks the device before use; when locked/unmounted, ingestion must treat the T7 as unavailable and modify no source files.
- Consequences: exFAT provides broad portability but not APFS clonefile/snapshots or dependable POSIX ownership, ACL, symlink, hardlink, xattr, or filesystem-immutability semantics. Active Git workspaces remain on internal/native filesystems. The organized view uses catalog-backed paths or verified ordinary copies rather than hardlinks or `cp -c`. Legacy-dump integrity is enforced with hashes/manifests, transaction rules, backups, and application policy rather than `chflags`.
- Catalog consequence: the T7 remains Mac-attached and single-writer for ingestion. SQLite/sidecar state must be checkpointed/closed cleanly before planned disconnects, and the backup chain remains mandatory because exFAT has weaker crash-recovery semantics than APFS.
- Revisit trigger: the owner obtains another storage device or explicitly chooses a downtime/migration window where all T7 data is independently verified elsewhere and the benefits of APFS outweigh the migration cost.

