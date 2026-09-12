# Plan

Execution order for the V0 control plane, written so a future agent (or the owner in six months) can pick up the next unchecked item without re-deriving the architecture. Each phase has an **acceptance gate**; do not start the next phase until the gate is green and recorded here with a date and the PR/tag that proved it.

Source of truth for *why*: `reviews/ASTRA-V0-ARCHITECTURE-REVIEW.md` and `DECISIONS.md`. This file is for *what next*. Progress is tracked by checking boxes in a PR, not in a separate file.

Conventions: `[ ]` not started, `[~]` in progress (link the branch), `[x] YYYY-MM-DD #PR` done.

---

## Phase 0 — Stop the bleeding (days 0–7)

Nothing in this phase needs a Dell, a queue, or a model.

- [ ] Free the Mac's internal disk to ≥ 60 GB (it had 12 GB free on 2026-09-10). APFS at 98% will corrupt snapshots and kill Whisper/LLM runs. Move nothing to the T7 yet; delete caches (`~/Library/Caches`, Xcode DerivedData, `node_modules` graveyards, old simulators).
- [ ] Buy/allocate a second drive or confirm `dell-xeon` has ≥ 2 TB internal free for the restic repo.
- [x] 2026-09-12 `e35eec6` Configure the immediate D-018 Google Drive/restic backup, pass a real restore drill, and retain the recovery material for the restic password. (No PR number: the backup work landed as direct commits `c0ba6ff` → `5bf6aff` → `e35eec6`. Snapshot, `backup-t7-check`, and `restore-t7-drill` were all green on 2026-09-12; both LaunchAgents are loaded. See `docs/09-backup-recovery.md`.)
- [ ] Later, create the D-004 Backblaze B2 bucket with versioning and an application key **without** `deleteFiles`, after the Dell append-only repository is operating.
- [ ] Apply repository settings from review §32 (squash-only, auto-delete branches); install `scripts/git-hooks/pre-push`.
- [ ] Decide GitHub Pro (rulesets on a private repo) — yes/no recorded in DECISIONS.md.
- [ ] Create the `quant` macOS user; move Kronos work there (D-011). Verify the primary user can no longer read `~quant`.

**Gate 0:** `df -h /System/Volumes/Data` shows ≥ 60 GB free; B2 bucket + append-only key exist; `git push origin main` from the Mac is refused by the hook; `ls ~quant` from the primary user is denied.

## Phase 1 — T7 confirm, bootstrap, dump, inventory, hash (days 7–21)

- [x] **The T7 was erased and recreated as APFS (Encrypted) on 2026-09-11 (D-019); do not reformat it again.** Volume `Angel` at `/Volumes/Angel` is confirmed APFS, unlocked, and writable, and its Volume UUID is recorded in the gitignored `config/ingest.yaml` (`t7.volume_uuid`) — deliberately not in tracked docs. Still to record in `docs/01-hardware-inventory.md`: serial, capacity, and measured throughput (`dd`/`fio`).
- [x] `scripts/bootstrap-t7.sh /Volumes/Angel` (run 2026-09-11 on the recreated APFS volume; creates directories only).
- [ ] Exclude the T7 from Spotlight (`mdutil -i off`) and from Time Machine.
- [ ] Copy (never move) legacy sources into `00-inbox/legacy-dump/<source>/` using a copy path that preserves file bytes, names, and timestamps, and a per-source transfer log in `90-system/migration-reports/`. The destination is APFS again (D-019), so POSIX ownership, ACLs, symlinks, and xattrs now survive the copy — but the restored pre-erase snapshot is the authority on what those metadata were.
- [ ] Point the existing inventory primitives at the legacy dump and measure them. Walk/stat/sha256/MIME sniffing already exist in `tools/ingest/scanner.py` and `hashing.py`, `.git` remote/dirty/unpushed/stash detection in `git_repos.py`, and `90-system/catalog.sqlite` (including the `repos` table) in `catalog.py` — none of that needs rebuilding. What is missing is a bulk-dump entry point that runs them over `00-inbox/legacy-dump/` in one pass, plus the ≥ 700 MB/s throughput measurement the gate asks for. Do **not** write a second `inventory.py` that reimplements any of it.
- [ ] After inventory verifies file count + total bytes, write an immutable checksum manifest and enforce legacy-dump protection in the ingest/application layer. APFS makes `chflags`, POSIX permissions, and snapshots available again (D-019), but they remain defence in depth: the application layer stays the mechanism of record.
- [ ] First restic snapshot of `00-inbox/` to the D-018 dedicated Google Drive repository and `restic check`; add the D-004 Dell append-only copy when that host is ready.
- [ ] Export `90-system/migration-reports/unpushed-repos.md` from the `repos` table — the one list the owner must act on manually, because no tool can decide for them whether an unpushed commit matters. Detection is already done: `tools/ingest/git_repos.py` records remotes, dirty state, unpushed commits, and stashes for every repository it finds, and `catalog.py` stores them. This item is the human-readable report over that data, not the detection.

**Gate 1:** catalog row count == inventory file count; a random 1% sample re-hashes identical; `restic restore` of one random client folder to `/tmp` matches hashes; source disks are still untouched.

## Phase 1A — Manual-first daily ingestion (build immediately after the catalog primitive)

This is the normal day-to-day cleanup path after the one-time legacy dump. It is intentionally **not** a continuously destructive watcher.

- [x] 2026-09-11 #6 Build one ingest transaction primitive used by every source: copy to a T7 `.partial`/staging path → flush/close → SHA-256 destination verification → atomic destination rename/register in SQLite → only then unlink the source. A crash, unplug, hash mismatch, or database failure must leave the source intact and the transaction retryable/idempotent. (`tools/ingest/transaction.py`)
- [x] 2026-09-11 #6 Build the `organize` CLI with `desktop`, `documents`, `downloads`, `sd`, and `all` targets plus `--dry-run` and `--apply`. Default behavior during development is dry-run/manual invocation; no command may silently become destructive. (`tools/ingest/cli.py`)
- [x] 2026-09-11 #6 Add a double-clickable macOS launcher (for example `Organize Files.command`) that invokes the same CLI rather than duplicating logic.
- [x] 2026-09-11 #6 Desktop and Documents are **manual-trigger only**. Merely mounting the T7 or logging in must never empty or rearrange either folder. (Still true after Phase 1B: the scheduled agent added there runs `triage`, whose only source classes are Downloads and Screenshots.)
- [~] Downloads is manual-trigger during development. After Gate 1A passes, add an **opt-in** launchd/FSEvents mode that considers only settled files (default: unchanged for ≥20 minutes), ignores incomplete browser downloads, and pauses entirely when the T7 is unavailable. **Partly done (Phase 1B, 2026-09-12).** What exists: an opt-in hourly launchd agent (`com.ai-homelab.file-intelligence`, `StartCalendarInterval` at :20), **disabled by default** and refused by the installer unless `automation.enabled: true`; an automated run is forced through `max(settle_seconds, downloads_watcher_min_settle_seconds)` so the 1200 s window cannot be shortened by lowering the manual one; incomplete downloads are excluded; and an absent Angel disables ingestion only, never naming. What does **not** exist: any FSEvents watcher or resident daemon — the agent is a one-shot invocation that exits. Nothing auto-ingests: `triage.auto_ingest` is off by default, so an automated run names and reports and moves nothing.
- [x] 2026-09-11 #6 Detect SD/removable-media insertion automatically by volume UUID, but only registered source volumes are eligible. Detection may surface/queue work; source files are moved only after an explicit `organize sd --apply` / `organize all --apply`. Never vacuum arbitrary removable disks. (`tools/ingest/volumes.py`, `sources.py`)
- [x] 2026-09-11 #6 Require the expected T7 volume UUID and writable/unlocked state before any apply operation. If absent, exit cleanly without modifying sources. (`tools/ingest/t7.py`. Hardened on this branch: `t7.storage_root_override` now also requires an explicit `--allow-dev-storage-root` flag, so a config edit alone can no longer bypass UUID verification.)
- [x] 2026-09-11 #6 Hard-exclude Git repositories/worktrees and their contents, `.git`, `node_modules`, virtualenvs, app/package bundles, Photos libraries, active/incomplete downloads, symlinks that escape an allowed root, system/hidden metadata, and paths carrying an explicit `keep-local` marker. Add a configurable exclusion file. (`tools/ingest/exclusions.py`, `scanner.py`, `config/ingest.example.yaml`)
- [x] 2026-09-11 #6 Extend the catalog/sidecar provenance with original volume UUID, original path/name, source class, detected/transfer/verified/classified timestamps, transfer state, final path/name, classifier/model/confidence, and recovery/error state. (`tools/ingest/catalog.py`)
- [x] 2026-09-11 #6 Keep transfer authority deterministic. AI receives only already-verified T7 assets and may propose/classify/tag/rename destinations; it never gets direct authority to delete a Mac/SD source path. **Caveat, honestly stated:** the enforced half of this is that `tools/ingest/transaction.py` is the only code that unlinks a source, and that is now covered by regression tests in `tests/ingest/test_mutation_safety.py`. The other half was previously vacuous — there was no proposal of any kind to constrain. Phase 1B is the first code where a proposal exists at all, and it is data-only: a proposer receives `NamingEvidence`, which contains no `Path`, no file handle, and no callable, so it has nothing to mutate the filesystem with (D-022). A model still has no destructive authority anywhere in this system.
- [~] Classification policy: high-confidence items may be materialized into the organized view automatically; medium/low confidence goes to `00-inbox/needs-review/` with proposed destination/name preserved in metadata. **Partly done (Phase 1B, 2026-09-12).** What exists: deterministic retention triage (`tools/ingest/triage/policy.py`) that answers IGNORE / REVIEW / INGEST with a confidence and a reason, appended to `classification_runs`; a proposed canonical filename stored alongside it; and a two-gate auto-ingest rule (`triage.auto_ingest`, off by default, **and** `min_ingest_confidence`) where an INGEST that fails either gate is reported and left alone. `organize review` lists what is waiting on a human. What does **not** exist: any semantic classifier (no OCR, no image understanding, no client/project inference), and no `00-inbox/needs-review/` materialization — a REVIEW file stays exactly where it is on the Mac and is surfaced in a report rather than moved to the T7. Ingestion still lands every verified asset in a flat `00-inbox/organized/<source_class>/` staging tree; the organized view itself is Phase 3 scope.
- [x] 2026-09-11 #6 Add deterministic HEIC/HEIF derivative generation: preserve the original `.heic`/`.heif` as the archival source, generate a linked `.jpg` compatibility derivative at encoder JPEG quality **100**, preserve transferable EXIF/ICC/orientation/capture metadata, and record the derivative relationship plus converter/version/settings in the catalog/sidecar. Conversion failure must never block or delete the verified original. Do not treat JPEG quality 100 as lossless; the original HEIC remains authoritative. (`tools/ingest/media/heic.py`, `derivatives.py`)
- [x] 2026-09-11 #6 Add HEIC conversion tests for metadata/orientation preservation, filename collision handling, idempotent re-runs, corrupted HEIC input, conversion failure, and confirmation that the original HEIC remains intact. (`tests/ingest/test_heic_derivative.py`)
- [x] 2026-09-11 #6 Add integration tests for: T7 missing, unknown SD card, file still changing, interrupted copy, injected hash mismatch, duplicate/retry, database failure after copy, source unlink failure, excluded Git repo, and successful resume after reconnect. (`tests/ingest/`)

**Gate 1A:** dry-run is zero-write; every destructive path uses the verified transaction primitive; fault-injection tests prove the source survives every failure before commit; an unknown removable disk is ignored; Desktop/Documents never move without explicit invocation; a registered SD import requires explicit apply; a successful test move is byte-identical on the T7 and absent at source only after verification. Only after this gate may the opt-in Downloads watcher be enabled.

**Gate 1A status: green.** PR #6 merged 2026-09-11 (`6674460`). The suite on this branch is **316/316 passing** (`.venv/bin/python -m unittest discover -s tests`, verified 2026-09-12), including fault-injection coverage of every listed criterion plus the `tests/ingest/test_mutation_safety.py` regressions added on this branch. (It was 78/78 when PR #6 merged; the Phase 1B work below accounts for the rest.)

One thing this gate does **not** assert, and must not be read as asserting: **no `--apply` run has yet landed a single asset on the real `/Volumes/Angel`.** The volume currently holds only the bootstrap directory scaffold and the restore-drill marker; `00-inbox/organized/` does not exist there yet and `90-system/catalog.sqlite` has never been created on it. Every passing test moves files into a temporary fixture root, not onto Angel. The first real `--apply` is still an unrehearsed operation and should be done deliberately, on a small set, with `--dry-run` read first.

## Phase 1B — File intelligence: naming, triage, and opt-in automation (this branch, 2026-09-12)

The operational track. **Scope, stated honestly:** this phase makes files on the Mac *legible* — it decides what a file should be called, whether it belongs in the durable Angel library, and when to ask those questions unattended. It does not make the system smarter about *content*: every decision here is deterministic, and the model-shaped hole is a protocol with a deterministic implementation behind it.

Phase 1's legacy-migration debt (the one-time dump, bulk inventory, and `unpushed-repos.md`) does **not** block this track and is not a prerequisite for it: the legacy dump is a one-time historical import into `00-inbox/legacy-dump/`, whereas this is the recurring path for files arriving today, and the two share only the catalog primitives.

- [x] 2026-09-12 Additive, idempotent SQLite migrations shared by both databases (`tools/ingest/migrations.py`, `schema_sql.py`). Every migration is `CREATE TABLE IF NOT EXISTS` / guarded `ADD COLUMN`; nothing is dropped or rewritten, and `user_version` only advances after a step returns, so a crash mid-migration re-applies harmlessly. The T7 catalog is now `SCHEMA_VERSION = 2`; a version newer than the code supports is refused rather than downgraded.
- [x] 2026-09-12 A second, Mac-side database: `LocalLedger` at `.state/file-intelligence/local-ledger.sqlite` (`tools/ingest/ledger.py`), holding `local_files`, `naming_history`, `classification_runs`, `media_metadata`, `duplicate_observations`, and `runs`. It exists because naming must keep working with Angel unplugged (D-021). It is rebuildable state, not truth; durable truth about ingested assets stays in the T7 catalog, which restic backs up.
- [x] 2026-09-12 The naming package (`tools/ingest/naming/`), split so the safety boundary is structural rather than conventional: `quality.py` (deterministic "is this name already useful?"), `evidence.py` (plain-data-only evidence — no `Path` reaches a proposer), `proposer.py` (the `NameProposer` protocol plus `DeterministicProposer`; this is the seam a model plugs into), `policy.py` (filename legality, and `safe_rename`, the only rename primitive, which cannot overwrite), and `rename.py` (the gate that orders the checks).
- [x] 2026-09-12 Retention triage (`tools/ingest/triage/policy.py`): IGNORE / REVIEW / INGEST, biased toward REVIEW, reading only measured facts. `IGNORE` means "not in the durable library" and never "delete from the Mac" (D-020).
- [x] 2026-09-12 Deterministic media probing (`tools/ingest/media/probe.py`) for image/video/PDF/zip, with optional `ffprobe` and `pypdf` detected rather than required, plus `thumbnail_png` for a future vision proposer. Read-only and non-raising by construction: a probe runs against the owner's only copy of a file, and one bad file degrades to an `errors` entry instead of ending the run.
- [x] 2026-09-12 Pipeline and orchestration (`tools/ingest/pipeline.py`), run serialization and status publication (`automation.py` — non-blocking `flock`, atomically written `status.json`), and the read-only reporting layer (`status.py`).
- [x] 2026-09-12 CLI extended to subcommands while keeping the Phase 1A spellings **unchanged** (`organize desktop|documents|downloads|sd|all`): adds `organize triage downloads|screenshots|all`, `organize status`, `organize doctor`, `organize review`, `organize duplicates`, and `organize correct-name PATH`.
- [x] 2026-09-12 New configuration blocks with defaults in code: `sources.screenshots.{path,enabled}` (no default path — the macOS default is the Desktop, and guessing would put the whole Desktop under automatic evaluation), `naming.*`, `triage.*`, `automation.*`.
- [x] 2026-09-12 Opt-in scheduling: `launchd/com.ai-homelab.file-intelligence.plist.template` plus `scripts/{render,install,uninstall}-file-intelligence-launchd` and `scripts/file-intelligence-scheduled`. Hourly at :20, one-shot, **disabled by default**; the installer parses `config/ingest.yaml` as YAML and refuses unless `automation.enabled: true` (D-024).
- [ ] Run the first real `organize triage downloads --dry-run` against the owner's actual Downloads folder and read every proposal before anything is applied.
- [ ] Decide and record the screenshots folder (`organize doctor` reports where macOS is currently saving them) rather than leaving `sources.screenshots` unconfigured.

**Gate 1B:** a dry-run over the real Downloads folder produces proposals the owner agrees with; a rename applied by hand via `organize correct-name` is never overridden afterwards; `organize status` and `organize doctor` are truthful with Angel both connected and disconnected; two concurrent runs result in one clean skip rather than two writers; and the scheduled agent, once enabled, completes an hour's worth of firings without moving a single file while `triage.auto_ingest` is off.

**Explicitly not built in Phase 1B, and not to be claimed:** semantic or vision-based naming (OCR, image understanding), any LLM/model router, the worker layer, organized-view materialization, destructive duplicate cleanup, Desktop/Documents automation of any kind, and the legacy dump/migration.

## Phase 1C — The evidence layer: what is actually in this file (2026-09-12)

The perception track. **Scope, stated honestly:** this phase teaches Angel to answer *"what is in this file?"* and nothing else. It answers neither *"what should this be called?"* nor *"does this belong in the library?"* — those already have deterministic answers from Phase 1B, and this phase does not change a single one of them. The evidence is collected, cached, and made inspectable so that its failure modes can be characterized against real files before anything is allowed to act on it (D-029).

- [x] 2026-09-12 The evidence package (`tools/ingest/evidence/`), split so the safety boundary is structural: `types.py` (frozen plain-data value types plus the text sanitizer), `router.py` (a pure function deciding which perception stages are worth attempting, every rule named and testable), `text.py` (native text from plain text/Markdown/code, PDFs via pypdf, and DOCX via `zipfile` + `ElementTree` with zip-bomb and billion-laughs guards, never executing a macro), `raster.py` (in-memory PDF/image rasterization with a resolution cap), `preprocess.py` (EXIF orientation, alpha flattening, Otsu binarization, decompression-bomb guard), `cache.py` (content-keyed evidence cache), `backends/` (three separate `TextExtractor`/`OCRBackend`/`VisionBackend` seams), and `extract.py` (the orchestrator).
- [x] 2026-09-12 OCR and vision as **separate** seams, never one god-model interface. Model-facing backends receive `RasterImage` PNG bytes and no `Path`, so a backend that may one day be a model has nothing to mutate the filesystem with — the same structural argument that makes `NamingEvidence.thumbnail_png` bytes. Shipped: `NullOCRBackend` (default, records its own absence), optional offline `TesseractOCRBackend`, `NullVisionBackend`, and `HeuristicVisionBackend` (deterministic, offline, honestly low-confidence, and refusing to claim `rendering`/`diagram`/`chart`/`meme` because it cannot tell).
- [x] 2026-09-12 Content-aware caching (`file_evidence`, local ledger only): keyed by content SHA-256 plus producer/model/pipeline version, never by pathname, so a rename reuses work and different bytes at the same path cannot inherit a stale answer (D-031). A backend/model version bump invalidates only that evidence kind. An unavailable backend is never cached, because availability is a property of the machine rather than of the bytes.
- [x] 2026-09-12 `organize evidence [PATH] [--show-text]`, read-only, printing no extracted text by default. Extracted text is stored only on the Mac, bounded, and deliberately absent from the T7 catalog's migration chain (D-030).
- [x] 2026-09-12 Reading order and perspective-crop normalization adapted from PaddleOCR (Apache-2.0, attribution in `THIRD_PARTY_NOTICES.md`), reimplemented in pure Python and Pillow with no NumPy, no OpenCV, and no Paddle dependency of any kind.
- [ ] Run the perception layer over the owner's real Downloads and screenshots for long enough to characterize what the backends actually get wrong, using `organize evidence` to read the corpus.
- [ ] Decide whether to enable the optional tesseract backend on this machine, and record the decision.

**Gate 1C:** an embedded-text PDF is never sent to OCR and a scanned one always is; a rename reuses cached evidence while different bytes at the same path never do; a backend that is missing, crashes, or returns nonsense costs one kind of evidence for one file and nothing else; a PDF or image whose text is an instruction block produces a database row and no action whatsoever; and `triage.auto_ingest`, `automation.enabled`, the human naming lock, and every transfer/data-loss guarantee are bit-for-bit unchanged.

**Explicitly not built in Phase 1C, and not to be claimed:** intelligent naming from evidence, client/project/category inference, organized-view materialization, any interpretation layer at all, large-image tiling, FSEvents, and any new destructive authority whatsoever. Evidence reaches no decision in this phase, and a test enforces that `naming/` and `triage/` import nothing from `evidence/`.

## Phase 2 — Local models exist and are measured (days 14–30)

- [ ] Install Ollama; pull the capable tier (Qwen 3.6-35B-A3B class, MXFP8/Q4), the small tier (≤ 4B), a VLM (Qwen3-VL-8B class), embeddings (bge-m3 or nomic-embed), whisper `large-v3-turbo`.
- [ ] `tools/bench/llm_bench.py`: for each model record prefill tok/s at 2k/8k/32k, generation tok/s, peak RSS, wall-clock for a fixed 20-task eval set (10 classification, 5 rename proposals, 5 bounded code edits with tests). Save to `40-ai/evaluation/`.
- [ ] Router v0 (`tools/router/`): deterministic → small → capable → frontier, with the job contract from `docs/04-model-routing.md`; every call writes a `jobs` row (D-012).
- [ ] `tools/report` prints last-7-days: jobs by tier, success rate, escalation rate, tokens, $ estimate, energy estimate.

**Gate 2:** benchmark table committed; router routes the 20-task set with ≥ 90% deterministic-verifiable success on tiers 0–1 and a recorded escalation rate; `report` runs.

## Phase 3 — Catalog pilot on one client (days 21–45)

- [ ] Pick one small client shoot (≤ 500 files). Run metadata → keyframes → VLM tags → canonical-name proposal → embeddings.
- [ ] Human-label 100 assets as the canary set; store in `corrections` table.
- [ ] Materialize the derived view only when useful using verified ordinary copies or catalog-backed references; never use hardlinks. `clonefile`/`cp -c` works again under D-019 but stays unused until a decision adopts it. `--dry-run` first; report additional disk usage; abort if > 20% of proposals change between runs.
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
