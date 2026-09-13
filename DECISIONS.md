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
- Status: superseded by D-019 (2026-09-11; the owner exercised this entry's own revisit trigger and reformatted the T7 as APFS).
- Supersedes: D-002 in full and the `clonefile`/APFS-derived-view portion of D-003. D-003's Mac-attached storage-host role otherwise remains.
- Decision: do **not** reformat the populated Samsung T7. Keep its existing exFAT filesystem and use the T7's Samsung Magician password/security layer for at-rest protection. The owner unlocks the device before use; when locked/unmounted, ingestion must treat the T7 as unavailable and modify no source files.
- Consequences: exFAT provides broad portability but not APFS clonefile/snapshots or dependable POSIX ownership, ACL, symlink, hardlink, xattr, or filesystem-immutability semantics. Active Git workspaces remain on internal/native filesystems. The organized view uses catalog-backed paths or verified ordinary copies rather than hardlinks or `cp -c`. Legacy-dump integrity is enforced with hashes/manifests, transaction rules, backups, and application policy rather than `chflags`.
- Catalog consequence: the T7 remains Mac-attached and single-writer for ingestion. SQLite/sidecar state must be checkpointed/closed cleanly before planned disconnects, and the backup chain remains mandatory because exFAT has weaker crash-recovery semantics than APFS.
- Revisit trigger: the owner obtains another storage device or explicitly chooses a downtime/migration window where all T7 data is independently verified elsewhere and the benefits of APFS outweigh the migration cost.

## D-018 — Immediate V0 off-site backup: encrypted restic via rclone to Google Drive
- Status: accepted; operational since 2026-09-12. OAuth is configured, a snapshot, `backup-t7-check`, and a real `restore-t7-drill` all completed successfully on 2026-09-12, and both the nightly `com.ai-homelab.t7-backup` and weekly `com.ai-homelab.t7-check` LaunchAgents are loaded.
- Supersedes: the **immediate-target** portion of D-004 only. D-004's later independent `dell-xeon` append-only repository and Backblaze B2 copy remain desirable separate failure domains.
- Decision: while the Dell/B2 path is not yet provisioned, `angel` (the Samsung T7) is backed up to the dedicated repository `rclone:gdrive-backup:AI-Homelab-Backups/T7-restic`. Restic encrypts, snapshots, and deduplicates data before rclone stores repository objects in Drive. Google Drive Desktop mirroring/sync is not a backup mechanism.
- Consequence: Drive is off-site but is neither immutable nor append-only. The Mac's dedicated OAuth scope and Keychain-held restic password are still sensitive; a compromised Mac/Drive account could delete repository objects. This does not replace the future append-only Dell copy.
- Revisit trigger: the Dell append-only repository is operational and has passed a restore drill, or Drive retention/availability/cost no longer meets the requirement.

## D-019 — T7 `angel` reformatted as APFS (Encrypted); volume UUID is the identity of record
- Status: accepted 2026-09-12. Records a change the owner had already made physically: the volume was erased, recreated, and verified on `mac-m4pro`.
- Supersedes: D-017 in full. Reinstates the substance of D-002 (APFS Encrypted, case-insensitive) and restores the APFS-dependent portions of D-003.
- Trigger: D-017's revisit trigger contemplated a deliberate migration window with T7 data independently verified elsewhere. **That is not what happened.** The exFAT volume began returning I/O errors (31,769 read failures recorded in `~/Desktop/angel-recovery-logs/rclone-recovery.log` on 2026-09-11); two salvage attempts ran against a failing filesystem, each capturing a different partial subset, and the volume was then erased and recreated as APFS. The reformat was a response to failure, not a planned migration. See `docs/09-backup-recovery.md` for exactly what was and was not salvaged.
- Decision: `angel` is an APFS (Encrypted, FileVault-backed) volume mounted at `/Volumes/Angel`. At-rest protection is APFS encryption rather than the Samsung Magician password layer; the passphrase belongs in the password manager. Its macOS **Volume UUID is the authoritative identity** and is recorded once, in the gitignored `config/ingest.yaml` under `t7.volume_uuid`. No tracked file, doc, or script may hard-code it, and no code path may fall back to matching on the volume name `Angel` or the path `/Volumes/Angel`.
- Consequences: APFS restores native POSIX ownership/ACL/symlink/hardlink/xattr semantics, snapshots, and `clonefile`. The prohibitions D-017 placed on the derived view (no hardlinks, no `cp -c`) are **not** automatically lifted — the no-hardlink rule in `docs/02` stands on its own correctness grounds, and adopting clonefile for the organized view requires its own decision. Linux nodes cannot mount encrypted APFS at all (review §10), so all worker access stays job-payload or read-only SMB from the Mac.
- Consequence: the reformat issued a new Volume UUID. Every snapshot taken before 2026-09-11 is tagged with the retired UUID `3EE676ED-…` in `.state/backup-logs/`; that record is the provenance link back to the pre-erase data and must not be edited away.
- Open risk: a reformat does not repair failing hardware. This drive produced tens of thousands of read errors days ago. Do not treat it as trustworthy primary storage until it passes a full-capacity write-then-verify pass, and do not let it become the only copy of anything.
- Revisit trigger: the T7 must routinely mount on a Linux box (buy a second ext4 drive instead; do not reformat this one), or the owner replaces the device.

## D-020 — Naming is universal where useful; Angel retention is selective
- Status: accepted 2026-09-12 (implemented in `tools/ingest/naming/` and `tools/ingest/triage/`).
- Decision: naming and retention are separate decisions reached by separate code from separate evidence. Every eligible settled file is asked what it should be called; only some files are then asked whether they belong in the durable Angel library. A screenshot may be renamed and never archived.
- Decision: `IGNORE` means "not in the durable library". It never means "delete from the Mac", and no code in the naming or triage layer can delete anything. The only file removal in this system remains the source unlink inside `transaction.transfer_file`, after the bytes are verified on Angel.
- Decision: age in Downloads is a settle signal, not a value signal. A file being old enough to be safe to touch says nothing about whether it is worth keeping; the two are never conflated.
- Consequence: triage is biased toward `REVIEW`. A false `REVIEW` costs the owner a glance at a report; a false `INGEST` puts junk in a permanent library that restic then backs up forever.
- Consequence: the two answers are stored in different tables (`naming_history` and `classification_runs`), so a later change to one cannot silently rewrite the other.
- Revisit trigger: the review queue grows faster than the owner clears it for 30 consecutive days, which would mean the REVIEW bias has stopped being conservatism and started being an unread inbox.

## D-021 — The naming ledger lives on the Mac, not on Angel
- Status: accepted 2026-09-12 (implemented as `LocalLedger` in `tools/ingest/ledger.py`).
- Decision: a second SQLite database at `.state/file-intelligence/local-ledger.sqlite` holds naming and triage bookkeeping for files that have **not** been ingested: `local_files`, `naming_history`, `classification_runs`, `media_metadata`, `duplicate_observations`, and `runs`.
- Rationale: naming must keep working with Angel unplugged. Putting the naming ledger on the T7 would have quietly made every naming decision depend on the drive being connected, which contradicts D-020.
- Decision: durable truth about ingested assets stays in the T7 catalog (`90-system/catalog.sqlite`), which is what restic backs up. The two databases share table shapes via `schema_sql.py` so a file's record has the same form before and after ingestion.
- Consequence: the local ledger is rebuildable state, not truth. Losing it costs bookkeeping and at worst one redundant re-evaluation of a file's name; it never costs a file. It is therefore deliberately not backed up.
- Consequence: local file identity is `(device, inode)`, not path — a path-derived identity would make every rename look like a new file and lose both the audit lineage and the human-correction lock.
- Revisit trigger: a second machine needs to participate in naming, at which point one Mac-local ledger stops being sufficient and the identity scheme needs a host component.

## D-022 — A model proposes; trusted code disposes
- Status: accepted 2026-09-12 (enforced structurally by `tools/ingest/naming/`).
- Decision: the proposer boundary is enforced by the shape of the data, not by convention. A `NameProposer` receives `NamingEvidence`, which contains no `Path`, no file handle, and no callable — so it has nothing to mutate the filesystem with. It returns a string and a confidence.
- Decision: `naming.policy.safe_rename` is the only rename primitive. It cannot overwrite: it uses `os.link` + `unlink` (atomic, fails if the target exists) with an `O_CREAT|O_EXCL` reservation fallback where hardlinks are unavailable. `os.rename` is not used anywhere, because it silently clobbers on POSIX. It takes a *name*, not a path, so it cannot relocate a file either.
- Decision: the extension comes from the file, never from the proposal. `transaction.transfer_file` remains the only code in the system that unlinks a source.
- Decision: human corrections outrank all later model output, permanently. `organize correct-name` records an owner-authored name; from then on the automatic path refuses to reconsider that file regardless of what any future proposer suggests.
- Consequence: swapping the deterministic proposer for a model-backed one changes nothing downstream. The seam is a protocol; no safety property depends on which implementation is behind it.
- Revisit trigger: a proposer needs input the evidence record cannot carry as plain data — if that ever means handing a proposer a live handle, this decision must be reopened rather than quietly widened.

## D-023 — `00-inbox/organized` keeps its name; its semantics are documented instead
- Status: accepted 2026-09-12.
- Context: the directory name overstates what it holds, and a rename to `00-inbox/ingested/` was evaluated.
- Decision: keep the name for this phase. The path is referenced by `t7.inbox_relative_path` in the live `config/ingest.yaml`, by the code default in `tools/ingest/config.py`, by `config/ingest.example.yaml`, and by `tests/ingest/helpers.py`; the volume's live `00-inbox/` tree already carries the configured `restore_drill_relative_path`, which points at a real file that the restore drill verifies. Renaming would be silent path churn across config, code, tests, and an operational drill for an aesthetic gain.
- Decision: document the semantics instead. What the directory actually holds is **verified, catalog-committed canonical assets awaiting or carrying enrichment** — not fully organized output. The organized view itself is Phase 3 scope and does not exist.
- Consequence: nothing may infer from the name that a file inside it has been classified, placed, or reviewed.
- Revisit trigger: an explicit, tested migration command exists that moves the directory and updates config, code, and tests together, **and** the owner asks for it.

## D-024 — Production automation is opt-in, hourly, and one-shot
- Status: accepted 2026-09-12 (`launchd/com.ai-homelab.file-intelligence.plist.template`, `scripts/file-intelligence-scheduled`).
- Decision: scheduling is launchd `StartCalendarInterval` at minute :20, not a resident FSEvents daemon. A one-shot invocation is inspectable, cannot leak memory or file descriptors, and a missed firing is recovered by the next one rather than needing supervision.
- Decision: disabled by default. `scripts/install-file-intelligence-launchd` parses `config/ingest.yaml` as YAML with the repository's own interpreter and refuses to install unless `automation.enabled` is literally `true` — a commented-out or unrelated nested `enabled: true` is not consent to schedule a process that reads and moves the owner's files.
- Decision: runs take a non-blocking `flock` in the state directory. A second run skips and exits cleanly rather than queuing; the next hourly firing picks the work up, and a backlog of blocked scans is worse than a missed one. The kernel releases the lock if the process dies, so a crash cannot wedge the pipeline.
- Decision: automated runs use `max(settle_seconds, downloads_watcher_min_settle_seconds)`. Taking the maximum rather than switching means lowering one value in config can never quietly shorten the window an unattended job waits.
- Consequence: enabling the agent does not enable transfers. `triage.auto_ingest` is a separate, independently defaulted-off gate, so the first thing the agent does once scheduled is name and report, not move.
- Consequence: the agent's only sources are Downloads and Screenshots. Desktop and Documents remain manual-trigger only (D-016), and scheduling this agent does not change that.
- Revisit trigger: the hourly cadence demonstrably misses files that matter (a same-hour download acted on before the next firing), or the run regularly exceeds an hour and the skip path becomes the normal path rather than the exception.

## D-025 — Screenshots are a configured source with no inferred default path
- Status: accepted 2026-09-12 (`sources.screenshots` in `config/ingest.example.yaml`; reported by `organize doctor`).
- Context: screenshots are the strongest case for automatic naming — macOS generates them with a timestamp name and nobody ever renames them by hand. macOS also writes them to `~/Desktop` by default, and `com.apple.screencapture location` can point at any directory.
- Decision: `sources.screenshots.path` has no default. It ships as `null` with `enabled: false`, and screenshots are a naming source only when the owner sets that path deliberately. The macOS setting is never read and adopted as the path.
- Rationale: inferring the path would, on a default Mac, silently enrol the entire Desktop in an hourly renaming pipeline. A convenience that widens the blast radius to a directory the owner never nominated is not a convenience. Screenshots are a *naming* source specifically — this decision says nothing about whether any screenshot is kept (D-020).
- Decision: `organize doctor` reports what `com.apple.screencapture location` currently says so the owner can copy it into config; it reports, it does not adopt. Where a configured screenshots path overlaps Desktop, Documents, or Downloads, only files whose names identify them as screen captures are evaluated.
- Consequence: on a machine that never configures it, screenshot naming simply does not happen. That is the intended failure mode, not a gap.
- Revisit trigger: macOS gains a capture location that cannot collide with a document folder, or the owner wants a dedicated screenshots folder picked up without configuration.

## D-026 — `t7.storage_root_override` requires an explicit CLI flag
- Status: accepted 2026-09-12 (`--allow-dev-storage-root`, enforced in `tools/ingest/t7.py`).
- Context: `t7.storage_root_override` exists so tests and local dry runs can point the pipeline at a scratch directory. As originally written it was honoured from config alone, and honouring it skipped the volume-UUID check for the **entire** destructive path — including the source unlink. A stale override left in `config/ingest.yaml` would have let `--apply` verify a copy into an ordinary folder and then delete the owner's original.
- Decision: the override is honoured only when `--allow-dev-storage-root` is passed on the command line for that invocation. A config file alone can never disable the UUID boundary. The scheduled LaunchAgent never passes the flag.
- Relationship to D-019: the D-019 boundary is unchanged and absolute. The T7 is identified by its Volume UUID, recorded once in the gitignored `config/ingest.yaml`, and no code path may fall back to the volume name `Angel` or the path `/Volumes/Angel`. This entry carves no exception into that rule; it closes a path that had quietly bypassed it.
- Consequence: `organize doctor` reports a configured override as a finding and states that it is honoured only with the flag, so a development setting cannot sit unnoticed on a production machine.
- Revisit trigger: the test suite builds its configuration in-process and no longer needs the override at all, at which point it should be deleted rather than gated.


## D-027 — Content identifies bytes; asset rows identify source occurrences
- Status: accepted 2026-09-12.
- Context: Phase 1A derived `asset_id` from `(source_volume_id, original_path)` and enforced `UNIQUE(source_volume_id, original_path)`. That made retries simple, but a normal event — deleting `Downloads/invoice.pdf` and later downloading a different `invoice.pdf` — forced two unrelated files into one identity. The safety patch in PR #9 correctly refused the collision, but could only park the second file in `needs_review`.
- Decision: a pathname is provenance, not identity. `blobs.sha256` is the content identity. Each `assets` row is one source occurrence and new IDs are derived from `(source volume, original path, verified SHA-256)`. The catalog may therefore contain multiple rows for the same source path when different bytes occupied it over time.
- Decision: byte-identical files at different paths remain distinct asset/provenance rows that reference the same blob. Recognizing equality never authorizes duplicate deletion or garbage collection.
- Decision: the transfer hashes and re-stats the source before selecting an asset row. Resume lookup by path is permitted only when the source is absent and there are no bytes available to hash; that path may finish bookkeeping but can never authorize deletion of a newly present file.
- Decision: immediately before the only source unlink, the pipeline re-verifies both sides: the destination still hashes to the recorded content and the source still has the same device/inode/size/mtime fingerprint and SHA-256 that were copied.
- Migration: catalog migration 4 atomically rebuilds `assets` to remove the legacy `UNIQUE(source_volume_id, original_path)` constraint while preserving all existing asset IDs and rows. Existing IDs are not rewritten; new IDs use the content-aware scheme.
- Consequence: a path can be reused safely, exact duplicates remain auditable as separate occurrences, and an in-place rewrite or pathname swap during ingestion cannot inherit the prior file's deletion authority.
- Revisit trigger: the catalog later needs a first-class source-occurrence table separate from `assets` for multi-location lifecycle tracking; if so, migrate provenance there without changing `blobs.sha256` as content identity.

## D-028 — Different bytes never inherit content-specific authority
- Status: accepted 2026-09-12.
- Context: the Mac-side local identity is based on `(volume, device, inode)` so it survives a rename. An inode can also be reused, and applications can rewrite an existing inode in place. Those two cases cannot be distinguished reliably after the bytes have changed.
- Decision: once a previously observed local identity has a different SHA-256, it starts a new ledger generation even when the inode and pathname are unchanged. Earlier naming/classification rows remain as audit history but are out of scope for the new bytes.
- Decision: retention, classification, model naming judgments, and ingestion linkage are content-specific and never cross a generation boundary automatically.
- Decision: explicit human naming authority is different. When changed bytes remain at the same path on the same local identity, the owner's filename lock is re-asserted into the new generation because invariant 12 says a person's name outranks later automation. If the inode is reused at a different path, no naming authority carries forward.
- Decision: a generation reset has no filesystem authority. It never renames or deletes the path by itself.
- Decision: the hourly hash cache requires unchanged path, size, mtime, and ctime. Angel refreshes the observation timestamp after its own rename so rename-generated ctime does not force a redundant re-hash; a later content rewrite still advances ctime and invalidates the cache even if mtime is restored.
- Consequence: rewritten bytes cannot inherit stale retention/deletion authority, inode reuse cannot inherit a stranger's lineage, and ordinary edits do not revoke an owner-authored filename.
- Revisit trigger: a stronger platform file-generation identifier becomes available and is proven stable across renames but changes on replacement, allowing edits and replacements to be distinguished without trusting path or timestamps.

## D-029 — Perception is built before it is trusted; evidence has no authority in this phase
- Status: accepted 2026-09-12.
- Context: naming a screenshot meaningfully requires reading what it shows, which needs OCR and image understanding. The obvious move is to build perception and immediately feed it to the namer. The obvious move is wrong: a perception layer's failure modes are only discoverable by running it over real files, and wiring it into decisions on day one means discovering them by way of misnamed or misfiled documents.
- Decision: `tools/ingest/evidence/` implements FACTS only. It collects native text, OCR blocks with geometry and confidence, a vision description, and layout structure, caches them, and exposes them for inspection. It changes no naming, retention, or ingestion decision.
- Decision: the separation is structural, not a convention. No module under `tools/ingest/naming/` or `tools/ingest/triage/` may import anything from `tools/ingest/evidence/`, and a test walks their ASTs and fails the build if one does. The perception call in `pipeline.py` sits deliberately *below* both decisions, which are already final by the time it runs.
- Decision: OCR and vision are separate seams (`OCRBackend`, `VisionBackend`), never one "understand this file" interface. "What text is visibly present" and "what am I looking at" are different questions with different engines and different failure modes.
- Decision: model-facing backends receive `RasterImage` PNG bytes, never a `Path`. A backend that may one day be a model is not trusted and then constrained; it is not given the means. Trusted in-repo `TextExtractor` implementations do receive a path, because they must stream large files, and because they are deterministic repo code rather than inference.
- Consequence: the evidence corpus accumulates from the hourly run, can be inspected with `organize evidence`, and can be characterized before anything is allowed to act on it. The interpretation layer (evidence to likely client/project/category) is a separate future stage, not a fold into the namer.
- Revisit trigger: a real OCR and vision backend have run over the owner's actual Downloads and screenshots for long enough to characterize their failure modes, and a separate, testable interpretation stage exists with its own confidence. Then, and only then, evidence becomes an input to a proposal — still a proposal, still validated, still outranked by a human's name.

## D-030 — Extracted document text stays on the Mac and out of normal output
- Status: accepted 2026-09-12.
- Context: `media/probe.py` deliberately records only `text_available: bool` for PDFs and drops the extracted text, because the T7 catalog is restic-backed to Google Drive and would otherwise become a searchable, off-site copy of every contract, medical letter and tax return on the disk. The evidence layer has to store text to be useful at all, which reopens exactly that question.
- Decision: evidence is stored only in the Mac-side local ledger (mode 0600, not backed up by restic, rebuildable). The `file_evidence` table is deliberately absent from the T7 catalog's migration chain entirely — not merely unwritten there, so there is no table for a future accident to write into.
- Decision: stored text is bounded (`evidence.max_text_chars`, default 20 000, with a `truncated` warning). It is evidence about a document, not a mirror of one.
- Decision: `media_metadata.facts` is unchanged and gains no text. Measured facts and perceived content stay in separate tables.
- Decision: `organize evidence` prints no extracted text by default — route, producers, versions, confidences, counts and warnings only. `--show-text` is an explicit per-invocation act. Nothing writes extracted text to a log file, because an `organize` run piped to a log would otherwise persist it.
- Consequence: the corpus is useful locally and is not an off-site disclosure. Losing the local ledger costs re-derivable measurements, never a file.
- Revisit trigger: evidence needs to survive on Angel for assets that have been ingested. If so, decide separately and explicitly what may cross to a restic-backed volume — plausibly structure, confidences and image kinds, but not verbatim document text.

## D-031 — Perceived evidence is cached by content, never by pathname
- Status: accepted 2026-09-12.
- Context: OCR and vision are expensive, so results must be cached; and D-027/D-028 already established that a pathname is an observation rather than an identity. A perception cache keyed by path would reintroduce exactly the bug those decisions removed, one layer out, with the failure mode "this scanned contract inherited the OCR text of the different file that used to have this name".
- Decision: the cache key is `uuid5` over the content SHA-256, the evidence kind, the producer and its version, the model and its version, and the evidence pipeline version. A pathname appears nowhere in it. A rename with identical bytes therefore reuses the work; different bytes at the same path cannot find a row to inherit.
- Decision: a backend or model version bump invalidates only the evidence kind it can have changed. Upgrading the OCR engine misses the OCR rows and leaves native-text and vision rows valid.
- Decision: the pipeline version folds in a digest of the configuration that changes *what a producer sees* (raster DPI, page cap, pixel budget, text cap, block cap), so lowering the raster DPI invalidates the OCR taken at the old one automatically. Timeouts and `enabled` flags are excluded: they decide whether a stage runs, not what it would perceive.
- Decision: a backend reporting itself unavailable is recorded as a warning but is **not** cached. Availability is a property of the machine, not of the bytes; caching absence would mean installing an OCR engine never re-asks about anything already scanned.
- Decision: `file_evidence` upserts rather than appending. That is not a violation of the append-only rule governing `naming_history` and `classification_runs`: the key already encodes every input that could make the answer different, so a second write under the same id is a re-derivation of the same question, not a competing judgment.
- Consequence: the hourly run is cheap on a folder that has not changed, a rename costs nothing, and stale perception cannot be attributed to bytes it never described.
- Revisit trigger: evidence becomes an input to a decision (see D-029), at which point a cache hit is load-bearing for that decision and the key may need to include the interpretation layer's version as well.

## D-032 — Tesseract is a validated machine-local OCR option, not a global default
- Status: accepted 2026-09-12 (real Downloads and screenshots validation).
- Context: `NullOCRBackend` is the repository-safe default because an OCR engine is machine-local optional capability, not a repository prerequisite. The owner's Mac validated Tesseract 5.5.3 against the initial real corpus: seven `tesseract-ocr` evidence records were produced, including useful visible text from an encrypted PDF whose embedded-text extractor correctly declined decryption.
- Decision: retain `NullOCRBackend` as the default. The owner's gitignored machine-local configuration may set `evidence.ocr.backend: tesseract`; Tesseract is not a mandatory global default.
- Consequence: OCR remains non-authoritative FACTS. It does not feed naming, triage, ingestion, moves, or deletion; the validated result records perception capability, not document understanding or decision authority.
- Revisit trigger: a repository-wide OCR default is proposed, at which point its installation, portability, corpus coverage, privacy, and failure modes require an explicit decision.

## D-033 — Phase 2 job telemetry is Mac-local coordinator state
- Status: accepted 2026-09-12 (Phase 2 implementation).
- Clarifies D-006 and supersedes the location implication in D-012: `.state/coordinator/jobs.sqlite` is the authoritative V0 operational job/attempt/event ledger. It is Mac-local, owner-only, additive-migrated state and does not require Angel to be attached. The T7 catalog remains asset/storage truth. Its historical `jobs` schema is preserved for compatibility and is not dropped or rewritten.
- Decision: a user-level job has append-only attempts and events; a verifier failure/retry/escalation is retained rather than overwritten. This is the schema intended to become the Phase 4 coordinator database behind D-006's API, not an early network service.

## D-034 — Phase 2 model results are constrained by the internal SSD floor
- Status: measured 2026-09-12.
- Context: the M4 Pro has 48 GB unified memory and only 62,922,680 KiB free after installing Ollama 0.33.3 and the measured small/VLM set. D-003's 60 GB free floor leaves no responsible capacity for a 35B-class capable model, embeddings, or Whisper large-v3-turbo.
- Decision: keep Ollama's default internal cache; never place it on Angel. The measured temporary local set is `qwen2.5:0.5b` (Q4_K_M, 397,821,319 bytes) and `moondream:latest` (Q4_0, 1,738,451,197 bytes). Neither is promoted as a capable-local choice. Capable-local, embeddings, and transcription remain unavailable pending storage headroom and measurement.

## D-035 — Benchmark artifacts are split by size and review value
- Status: accepted 2026-09-12.
- Decision: the versioned corpus, normalized evaluation table, registry and methodology are committed. Raw JSONL, generated transcripts, caches, and any large exports remain runtime state; an optional export to Angel's `40-ai/evaluation/` must use the existing UUID resolver and is never a Git path. Angel absence must not block benchmarking.

## D-036 — Interpretation is read-only and non-authoritative
- Status: accepted 2026-09-12.
- Decision: `tools/ingest/interpretation/` consumes bounded plain evidence values and emits inspectable hypotheses only. It does not import naming, triage, transaction, pipeline, or filesystem mutation APIs. OCR/native/visual instruction text remains data. No hypothesis may become a filename, retention decision, ingestion, move, or deletion input in Phase 2.

## D-037 — Angel is the UUID-gated bulk tier for regenerable AI artifacts
- Status: accepted 2026-09-12; supersedes D-034's "never place it on Angel" storage conclusion.
- Context: the verified, encrypted Angel T7 has ample capacity while model weights and runtime download caches are regenerable. Keeping them on the internal SSD needlessly constrained capable-local evaluation.
- Decision: large regenerable AI artifacts live only below UUID-verified `40-ai/`: `model-cache/{ollama,huggingface,mlx,whisper}`, plus `datasets`, `evaluation`, `indexes`, and `manifests`. The resolver reuses the existing T7 APFS UUID check and fails closed for missing, locked, read-only, wrong, or merely name-matched volumes. It never creates or uses a fallback under `/Volumes/Angel` or the internal SSD.
- Decision: Ollama is started with its supported `OLLAMA_MODELS` environment variable only after this preflight. The Mac-side coordinator ledger, source repository, local configuration, credentials, and essential runtime state remain internal.
- Decision: restic excludes `40-ai/model-cache/**` precisely. It does not exclude all `40-ai`; manifests, evaluation results, deliberate datasets, and expensive non-regenerable indexes remain eligible for backup.
- Consequence: with Angel absent, local model backends are unavailable and Tier 0 continues; no model download may silently consume internal storage. This availability trade-off is intentional.

## D-038 — The frozen code-fixture verifier fails closed pending trusted execution
- Status: accepted 2026-09-12 after bounded independent review.
- Context: the frozen corpus retained only fixture selector names and no executable fixtures/tests. Its old harness accepted a model-produced boolean/`tests_passed` claim, contrary to the project's deterministic-verification rule.
- Decision: the corpus, expected values, tier labels, and historical 4/20 RED record remain untouched. Until a verifier-owned confined runner and fixture bundle exist, every `code_fixture` result is `code-fixture-unavailable` and therefore fails. A model claim is never test evidence.
- Consequence: Gate 2 cannot turn green from unverified code claims. A future repair must add public task contracts plus hidden verifier-owned tests, bounded execution with no filesystem/network/secret authority, and regression proof; it must then rerun the unchanged corpus.

## D-039 — Model capability is evaluated per role; there is no universal model score
- Status: accepted 2026-09-12 (documentation/architecture pass; see `docs/19-model-evaluation.md`).
- Context: the Phase 2 measurement in `docs/18-phase-2-evaluation.md` ran one mixed 20-task corpus (classification + filename proposal + code fixtures) and produced one pass fraction per model. That fraction cannot say which specific capability failed, and it structurally penalizes a cheap model for failing work it would never be routed while never penalizing a capable model for being needlessly expensive on simple work.
- Decision: Angel Capability Benchmark v1 (ACB-v1) replaces the single score with role-specific capability suites (routing/classification, tool calling, structured extraction, constrained transformation, bulk reading, semantic naming, five coding sub-suites, repository understanding, orchestration, failure recovery/escalation, vision, OCR, embeddings/retrieval, ASR). A model is measured only against the suites relevant to roles it might actually be routed.
- Decision: small language models are a deliberate research track for bounded roles (routing, classification, tool selection, extraction, simple transforms, boilerplate), not inferior general models. Enough out-of-role testing is kept to find each SLM's escalation boundary.
- Consequence: candidate reports become capability profiles ("certified: routing.simple, tool.single" / "not certified: code.backend, orchestration"), not one leaderboard percentage. This does not itself change `tools/router/router.py`, which still selects by tier/capability/context match; see D-042.
- Revisit trigger: none required to adopt this framing further — it is the default going forward. A future decision would only be needed to change the taxonomy itself.

## D-040 — Angel Canary v1 (the Legacy 20) is a frozen historical regression canary, not a capability benchmark
- Status: accepted 2026-09-12; clarifies the conceptual role of the corpus governed by D-035 and D-038, changes no code, task, answer, or verifier.
- Context: the existing `data/evaluation/phase2_tasks.json` corpus was the only Phase 2 benchmark and was being read as a capability measurement. Under D-039 that framing is wrong, but the corpus and its historical 4/20, 4/20, 0/20 results remain real evidence and real regression-detection value.
- Decision: the corpus is renamed, conceptually, Angel Canary v1 — Legacy 20. Its purposes are historical comparability, regression detection, evaluator cross-validation (D-043), and release/gate sanity checking. It is not a complete benchmark of model capability and must not become the dataset used for iterative prompt tuning.
- Decision: every meaningful ACB-v1 capability suite defines development/validation/frozen-holdout splits (`docs/19-model-evaluation.md` §2.5). The Legacy 20 plays the frozen-holdout role for the original corpus. Prompt or task development may use development/validation cases freely; a frozen holdout is never tuned against, and a score improved by repeatedly trying variants against holdout cases is not reported as unbiased.
- Consequence: the historical 4/20, 4/20, and 0/20 results, and the D-038 verifier-defect record, remain exactly as documented in `docs/18-phase-2-evaluation.md`. Changing the Legacy 20's tasks, answers, or verifiers requires its own decision, the same standard D-038 already met.
- Revisit trigger: a demonstrated fixture defect (the D-038 pattern) — not a desire for a better score.

## D-041 — Prompts are versioned first-class configuration, rendered identically for evaluation and production
- Status: accepted 2026-09-12 (architecture direction; registry not yet implemented — see `docs/20-prompt-management.md`).
- Context: today's only model-facing prompts in the evaluation path are three unversioned literal templates in `tools/bench/harness.py:benchmark_prompt()`. There is no prompt identifier, no schema, and no recorded link between an evaluated prompt and whatever a production caller would eventually send.
- Decision: a prompt is addressed by `prompt_id` + `version`, never by an unversioned string. Composition is layered and reasoned about separately: policy (trusted), role, task contract, context pack (untrusted data — D-022/D-028/D-029/D-036 apply unchanged), tool contract, output contract, few-shot examples, and a runtime adapter that converts to a specific provider's wire format. Semantic prompt design is never conflated with provider-specific chat formatting.
- Decision: a model-role configuration is deployed using the same versioned prompt definition and rendering path that was evaluated. Production inputs differ from evaluation inputs; the prompt recipe does not. Rendered production prompts containing private owner/client content are never committed, matching the D-030 boundary already drawn around extracted document text.
- Decision: two evaluation tracks are kept distinct and never merged into one number — a canonical track (one simple contract across compatible models, for apples-to-apples comparison) and a production-optimized track (the best measured prompt per model/role). Production performance belongs to `model × runtime × prompt × role`, not the model alone.
- Consequence: every evaluation run should eventually record prompt ID/version, rendered-prompt SHA-256, renderer version, tool-schema version, and context-pack SHA-256, alongside the model/run metadata `docs/19-model-evaluation.md` §2.7 already targets.
- Revisit trigger: the registry, renderer, and canonical/production split are implemented — at which point this decision's status moves from architecture direction to implemented behavior.

## D-042 — Production model-role eligibility belongs to a certified configuration, not a model name
- Status: accepted 2026-09-12 (architecture direction; certification records not yet implemented).
- Context: `tools/models/registry.py` already separates model identity from role via `tier`/`capabilities`/`context_length`, and `tools/router/router.py` already selects candidates by that match. Neither currently records that a specific configuration was *measured and passed* a role-specific suite; a large model is not automatically preferred and a small model is not automatically excluded from a role on parameter count alone.
- Decision: production certification is a claim about `model + quant + runtime + prompt_version + tool_schema_version + capability`, recording suite passed, threshold, measured latency/resource envelope, known failure modes, escalation policy, and evaluation date/version. Routing should consult certified capability data rather than parameter count alone once such records exist.
- Consequence: this decision does not change `tools/router/router.py` today. Wiring certification into `_candidates()` is future implementation work tracked in `PLAN.md` Phase 2, not asserted as done here.
- Revisit trigger: a certification record schema and at least one populated record exist, at which point router selection logic is revisited to consult them.

## D-043 — Inspect AI is the preferred evaluation-substrate direction, pending cross-validation
- Status: accepted 2026-09-12 as a direction; not adopted, not migrated.
- Context: `tools/bench/harness.py` reimplements, narrowly, what a mature evaluation framework already provides generally: custom tasks, deterministic/custom scorers, tool use, sandboxed code execution, sample-level logging, agent-style evaluations, multiple model backends, and reproducibility.
- Decision: Inspect AI is the preferred candidate evaluation substrate for these concerns, pending cross-validation. No migration has happened. Before any adoption decision, the same model against the same Legacy 20 task with equivalent prompt semantics must be run through both the existing Angel evaluator and an Inspect-based implementation, and any disagreement investigated before Inspect's result is treated as authoritative.
- Decision: regardless of which harness runs the loop, Angel retains task definitions, deterministic truth, routing policy, safety rules, prompt definitions, storage policy, and telemetry requirements. Only generic evaluator plumbing (run loop, model backends, sandboxing, sample logging) is a candidate for delegation to OSS infrastructure.
- Consequence: no dependency on Inspect AI is added to the runtime path by this decision. A future decision is required to actually adopt it after cross-validation.
- Revisit trigger: the cross-validation probe above is run and its disagreements (if any) are resolved.

## D-044 — Angel does not certify models on raw standalone intelligence; certification requires a production-capability benchmark
- Status: accepted 2026-09-12 (architecture direction; the distinction below applies to future evaluation, not a re-scoring of past results).
- Context: a bare model given a generic instruction and a model given a full production task contract are different, measurably different-performing configurations. Treating the first as evidence for the second overstates what a cheap/small model can do in production and understates what it can do when properly augmented.
- Decision: Angel's production execution unit is `model + task-specific production prompt + bounded spec/context + relevant trusted skills/capabilities + approved libraries/reference material + tools + a deterministic verifier` — not the model alone. Small/local models are intentionally meant to be capability-augmented this way; the research question for SLMs (D-039) is what is the smallest model that reliably passes the real production contract for a given task class, with this full augmentation present.
- Decision: a **raw-model baseline** (bare instruction, no production prompt/spec/skills/tools) is diagnostic information only and is never routing or certification evidence. A **production-capability benchmark** (the full execution unit above) is the only evidence that may support a certification record (D-042) or a Gate 2 claim (`PLAN.md` Phase 2). The two are never reported as one number.
- Decision: the historical Legacy 20 results (D-040) are raw-model-baseline-shaped — one generic contract per task kind, uniformly applied, with no task-specific spec or skill augmentation. They remain valid historical/regression evidence under that label and are not reinterpreted as production-capability results by this decision.
- Consequence: production task classes are evaluated with the same skills/spec/reference material a production job of that class would actually receive (task-driven selection, never the entire skill library injected by default), per `docs/19-model-evaluation.md` §8–§10 and `docs/20-prompt-management.md` §2. An augmentation ablation experiment (raw instruction → production prompt → +spec → +skills → +skills+reference/tools) is designed in `docs/19-model-evaluation.md` §10 to measure augmentation's marginal value, scheduled to run after the D-038 trusted fixture runner exists, not before.
- Revisit trigger: the ablation experiment runs and its results either confirm or revise how much augmentation later SLM-by-role benchmarking (`PLAN.md` Phase 2) uses by default.
