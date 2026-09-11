# Plan

Execution order for the V0 control plane, written so a future agent (or the owner in six months) can pick up the next unchecked item without re-deriving the architecture. Each phase has an **acceptance gate**; do not start the next phase until the gate is green and recorded here with a date and the PR/tag that proved it.

Source of truth for *why*: `reviews/ASTRA-V0-ARCHITECTURE-REVIEW.md` and `DECISIONS.md`. This file is for *what next*. Progress is tracked by checking boxes in a PR, not in a separate file.

Conventions: `[ ]` not started, `[~]` in progress (link the branch), `[x] YYYY-MM-DD #PR` done.

---

## Phase 0 — Stop the bleeding (days 0–7)

Nothing in this phase needs a Dell, a queue, or a model.

- [ ] Free the Mac's internal disk to ≥ 60 GB (it had 12 GB free on 2026-09-10). APFS at 98% will corrupt snapshots and kill Whisper/LLM runs. Move nothing to the T7 yet; delete caches (`~/Library/Caches`, Xcode DerivedData, `node_modules` graveyards, old simulators).
- [ ] Buy/allocate a second drive or confirm `dell-xeon` has ≥ 2 TB internal free for the restic repo.
- [ ] Create a Backblaze B2 bucket with versioning; create an application key **without** `deleteFiles`.
- [ ] Put the following in the password manager: T7 APFS passphrase (to be created), restic password, B2 key. Print the restic password and store it away from the laptop.
- [ ] Apply repository settings from review §32 (squash-only, auto-delete branches); install `scripts/git-hooks/pre-push`.
- [ ] Decide GitHub Pro (rulesets on a private repo) — yes/no recorded in DECISIONS.md.
- [ ] Create the `quant` macOS user; move Kronos work there (D-011). Verify the primary user can no longer read `~quant`.

**Gate 0:** `df -h /System/Volumes/Data` shows ≥ 60 GB free; B2 bucket + append-only key exist; `git push origin main` from the Mac is refused by the hook; `ls ~quant` from the primary user is denied.

## Phase 1 — T7 format, dump, inventory, hash, lock (days 7–21)

- [ ] Format T7: `diskutil apfs createContainer` → APFS Encrypted, case-insensitive (D-002). Record serial, capacity, and measured throughput (`dd`/`fio`) in `docs/01-hardware-inventory.md`.
- [ ] `scripts/bootstrap-t7.sh /Volumes/T7`.
- [ ] Exclude the T7 from Spotlight (`mdutil -i off`) and from Time Machine.
- [ ] Copy (never move) legacy sources into `00-inbox/legacy-dump/<source>/` with `rsync -aHAX --info=progress2`; keep a per-source `rsync` log in `90-system/migration-reports/`.
- [ ] Build `tools/ingest/inventory.py`: walk, stat, sha256 (one pass, ≥ 700 MB/s measured), MIME sniff, `.git` detection with remotes/dirty/unpushed/stash state, write `90-system/catalog.sqlite` (schema in review §11).
- [ ] `chflags -R uchg` the dump after the inventory verifies file count + total bytes against the rsync log.
- [ ] First restic snapshot of `00-inbox/` to `dell-xeon` (or, if the Dell is not ready, to the second drive) and `restic check`.
- [ ] Produce `90-system/migration-reports/unpushed-repos.md` — the one list the owner must act on manually (unpushed commits cannot be inferred).

**Gate 1:** catalog row count == inventory file count; a random 1% sample re-hashes identical; `restic restore` of one random client folder to `/tmp` matches hashes; source disks are still untouched.

## Phase 1A — Manual-first daily ingestion (build immediately after the catalog primitive)

This is the normal day-to-day cleanup path after the one-time legacy dump. It is intentionally **not** a continuously destructive watcher.

- [ ] Build one ingest transaction primitive used by every source: copy to a T7 `.partial`/staging path → flush/close → SHA-256 destination verification → atomic destination rename/register in SQLite → only then unlink the source. A crash, unplug, hash mismatch, or database failure must leave the source intact and the transaction retryable/idempotent.
- [ ] Build the `organize` CLI with `desktop`, `documents`, `downloads`, `sd`, and `all` targets plus `--dry-run` and `--apply`. Default behavior during development is dry-run/manual invocation; no command may silently become destructive.
- [ ] Add a double-clickable macOS launcher (for example `Organize Files.command`) that invokes the same CLI rather than duplicating logic.
- [ ] Desktop and Documents are **manual-trigger only**. Merely mounting the T7 or logging in must never empty or rearrange either folder.
- [ ] Downloads is manual-trigger during development. After Gate 1A passes, add an **opt-in** launchd/FSEvents mode that considers only settled files (default: unchanged for ≥20 minutes), ignores incomplete browser downloads, and pauses entirely when the T7 is unavailable.
- [ ] Detect SD/removable-media insertion automatically by volume UUID, but only registered source volumes are eligible. Detection may surface/queue work; source files are moved only after an explicit `organize sd --apply` / `organize all --apply`. Never vacuum arbitrary removable disks.
- [ ] Require the expected T7 volume UUID and writable/unlocked state before any apply operation. If absent, exit cleanly without modifying sources.
- [ ] Hard-exclude Git repositories/worktrees and their contents, `.git`, `node_modules`, virtualenvs, app/package bundles, Photos libraries, active/incomplete downloads, symlinks that escape an allowed root, system/hidden metadata, and paths carrying an explicit `keep-local` marker. Add a configurable exclusion file.
- [ ] Extend the catalog/sidecar provenance with original volume UUID, original path/name, source class, detected/transfer/verified/classified timestamps, transfer state, final path/name, classifier/model/confidence, and recovery/error state.
- [ ] Keep transfer authority deterministic. AI receives only already-verified T7 assets and may propose/classify/tag/rename destinations; it never gets direct authority to delete a Mac/SD source path.
- [ ] Classification policy: high-confidence items may be materialized into the organized view automatically; medium/low confidence goes to `00-inbox/needs-review/` with proposed destination/name preserved in metadata.
- [ ] Add integration tests for: T7 missing, unknown SD card, file still changing, interrupted copy, injected hash mismatch, duplicate/retry, database failure after copy, source unlink failure, excluded Git repo, and successful resume after reconnect.

**Gate 1A:** dry-run is zero-write; every destructive path uses the verified transaction primitive; fault-injection tests prove the source survives every failure before commit; an unknown removable disk is ignored; Desktop/Documents never move without explicit invocation; a registered SD import requires explicit apply; a successful test move is byte-identical on the T7 and absent at source only after verification. Only after this gate may the opt-in Downloads watcher be enabled.

## Phase 2 — Local models exist and are measured (days 14–30)

- [ ] Install Ollama; pull the capable tier (Qwen 3.6-35B-A3B class, MXFP8/Q4), the small tier (≤ 4B), a VLM (Qwen3-VL-8B class), embeddings (bge-m3 or nomic-embed), whisper `large-v3-turbo`.
- [ ] `tools/bench/llm_bench.py`: for each model record prefill tok/s at 2k/8k/32k, generation tok/s, peak RSS, wall-clock for a fixed 20-task eval set (10 classification, 5 rename proposals, 5 bounded code edits with tests). Save to `40-ai/evaluation/`.
- [ ] Router v0 (`tools/router/`): deterministic → small → capable → frontier, with the job contract from `docs/04-model-routing.md`; every call writes a `jobs` row (D-012).
- [ ] `tools/report` prints last-7-days: jobs by tier, success rate, escalation rate, tokens, $ estimate, energy estimate.

**Gate 2:** benchmark table committed; router routes the 20-task set with ≥ 90% deterministic-verifiable success on tiers 0–1 and a recorded escalation rate; `report` runs.

## Phase 3 — Catalog pilot on one client (days 21–45)

- [ ] Pick one small client shoot (≤ 500 files). Run metadata → keyframes → VLM tags → canonical-name proposal → embeddings.
- [ ] Human-label 100 assets as the canary set; store in `corrections` table.
- [ ] Materialize the derived view with APFS clones (`cp -c`) into `20-clients/<client>/…`; `--dry-run` first; abort if > 20% of proposals change between runs.
- [ ] Search CLI: `tools/search "vertical countertop install clips"` returns ranked assets from sqlite-vec.

**Gate 3:** precision of client/project inference ≥ 95% and of tags ≥ 80% on the canary set; a deliberate model swap to a worse model is caught by the canary diff; view regeneration is idempotent (second run makes zero changes).

## Phase 4 — First Linux worker (days 30–60)

- [ ] `dell-xeon`: Ubuntu LTS server, full-disk encryption, `ufw` default deny, Tailscale with `tag:worker`, Tailscale SSH, unattended-upgrades, no password auth.
- [ ] `worker` service: polls the coordinator API, runs each job as a sandboxed `systemd-run` unit (docs/03), reports heartbeat/results.
- [ ] Coordinator API on the Mac bound to the Tailscale IP (D-006).
- [ ] First job classes: `media.transcode_proxy`, `media.transcribe` (whisper on CPU, batch), `video.social_learn` (the existing pipeline, moved off the Mac), `embed.text`.
- [ ] restic rest-server `--append-only` on `dell-xeon` becomes backup copy 2; nightly `restic copy` to B2 becomes copy 3.

**Gate 4:** a job submitted from the Mac runs on the Dell inside a unit with no `~/`, no network (for a no-network class), and a memory cap; killing the worker mid-job leaves the job re-claimable after lease expiry; `tailscale ping` from the worker to the `quant` user's box (if any) fails by ACL; a B2 restore drill passes.

## Phase 5 — Coding agents in the factory (days 45–90)

- [ ] Template repo (`app-template`): chosen stack, design tokens, CI, six standard files (D-015), `context-pack` generator.
- [ ] `agent.code_task` job class: fresh worktree on the worker, branch-scoped fine-grained token, bounded task from `PLAN.md`, runs tests, pushes branch, opens PR with the context pack attached.
- [ ] Frontier review step on the coordinator; local tier only receives tasks ≤ 12k context.
- [ ] Run the pipeline end-to-end on one real bounded task in one real product repo.

**Gate 5:** one PR produced entirely by a local model passes CI and human review; the `report` shows measured $ per merged task local vs frontier; no credential other than the branch-scoped token was reachable from the worker (verified by a probe job that tries to read `~/.ssh`, env, keychain and must fail).

## Phase 6 — Decide on hardware (day 90)

- [ ] Compute the six GPU triggers in review §27 from 30 days of `report` data. Buy nothing that is not justified by a red trigger.

---

## Explicitly not in this plan

Kubernetes, Nomad, Ray, Temporal, Redis, NATS, RabbitMQ, distributed inference, multi-node training, a metrics stack, **unbounded AI-directed source deletion**, automatic skill installation, any live-trading path, `dell-lite` provisioning. Deterministic, hash-verified source removal through the approved ingestion transaction is in scope.
