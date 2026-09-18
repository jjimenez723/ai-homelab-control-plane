# 09 — Backup and Recovery

## Core point

Moving everything to the Samsung T7 is **consolidation**, not backup.

If the T7 becomes the only copy, risk has increased.

## Minimum target

Adopt a 3-2-1-inspired approach over time:
- at least 3 copies of irreplaceable data;
- at least 2 storage/media locations or failure domains;
- at least 1 offsite or otherwise isolated copy.

GitHub counts as a remote copy for committed source code, but not for:
- uncommitted work;
- ignored assets;
- client media;
- local datasets;
- secrets;
- model caches.

## Before deleting originals

For every media migration:
1. copy, do not move;
2. compute/record checksums;
3. verify destination count/size;
4. spot-check opening representative files;
5. create second independent backup;
6. only then consider deleting old copies.

## Immediate V0 architecture (D-018)

```text
T7 / `angel` (APFS Encrypted, Mac-attached, pinned by Volume UUID)   primary working copy
   |
   | encrypted, deduplicated restic snapshots
   v
Google Drive: AI-Homelab-Backups/T7-restic                  immediate off-site copy
  (repository objects written through rclone, not Drive sync)
```

Rules:
- The dedicated Google Drive repository is `rclone:gdrive-backup:AI-Homelab-Backups/T7-restic`; it contains only encrypted restic repository data, never a browsable T7 mirror. Every operation verifies its stable Restic repository ID before proceeding, so repointing the mutable rclone alias cannot silently select a different repository.
- The source is pinned by `t7.volume_uuid`, never `/Volumes/Angel` or its current Finder name. If the encrypted volume is locked/unmounted, the disk is disconnected, or the UUID differs, backup exits without making an empty snapshot.
- The repository passphrase is held only in macOS Keychain (`ai-homelab-t7-restic`) and read by `RESTIC_PASSWORD_COMMAND`. **Losing it permanently loses access to this backup.** Never place it in Drive, Git, `.env`, config, or logs.
- If rclone configuration encryption is enabled, its separate passphrase belongs in Keychain (`ai-homelab-rclone-config`) and is supplied through `RCLONE_PASSWORD_COMMAND`; manual and scheduled rclone calls stay noninteractive.
- Google Drive is not immutable or append-only. It is useful immediate off-site protection, but does not eliminate the later append-only `dell-xeon` restic repository and independent B2 failure domain from D-004.
- No worker node holds the backup credentials. Workers are never backup sources of record.
- Run `backup-t7-check` (metadata plus a 5% data subset) regularly; run `backup-t7-check --deep` periodically and record a real restore drill before declaring production readiness.
- Time Machine of the Mac internal disk continues independently; it is not the T7 strategy.

## Operations

All commands use `config/ingest.yaml` (ignored by Git) and inherit the source UUID from `t7.volume_uuid`.

```bash
./scripts/backup-t7-init                    # safe: verifies the pinned repository; never initializes
./scripts/backup-t7 --preflight             # read-only source, remote, Keychain, and size report
./scripts/backup-t7                         # normal snapshot: t7, angel, automatic tags
./scripts/backup-t7 --tag pre-organize      # lifecycle snapshot: t7, angel, pre-organize
./scripts/backup-t7 --tag post-organize
./scripts/backup-t7-snapshots               # ID, time, host, paths, tags
./scripts/backup-t7-check                   # metadata + random 5% data reads
./scripts/backup-t7-check --deep            # full data read, schedule periodically
./scripts/backup-t7-retention               # report only; no deletion
./scripts/restore-t7 SNAPSHOT_ID --target ~/T7-Restore
./scripts/restore-t7 SNAPSHOT_ID --target ~/T7-Restore --include /path/in/snapshot
./scripts/restore-t7-drill                  # monthly configured, isolated hash-checked drill
./scripts/backup-t7-status                  # durable local operational state
```

Restores require `--target`; `/`, the home directory itself, the live T7, and a path inside the live T7 are refused. For a drill, restore a small non-sensitive source subset into a temporary local directory, hash both copies, then remove only that temporary restored copy after verification. `restic diff SNAPSHOT_A SNAPSHOT_B` is useful for comparing snapshots.

The normal backup writes owner-only, non-secret JSONL metadata and atomically updated status under ignored `.state/backup/` on the Mac—not on the backed-up Angel volume. A machine-global lock under `~/Library/Application Support/ai-homelab-control-plane/backup/` serializes old and new checkouts as well as backup/check/restore operations. `status.json` records last attempt, last successful backup/snapshot, Restic result, duration, check status, restore-drill status, and a useful failure message. Exit code **3 is always failure**, never a successful backup. The excluded paths are intentionally narrow: T7 ingest staging, `*.partial` transfer artifacts, and macOS volume metadata (`.Spotlight-V100`, `.fseventsd`, `.TemporaryItems`, `.Trashes`). Originals, catalog, SQLite sidecars, manifests, human corrections, and JPEG derivatives are included.

## Unattended scheduling

`com.ai-homelab.t7-backup` runs daily at 02:15 and `com.ai-homelab.t7-check` runs Sunday at 03:15 (metadata plus a 5% data sample). Both call the same lock-protected runner, explicitly set a minimal system/Homebrew `PATH`, and use Keychain commands rather than launchd credential fields. A missing, locked, or UUID-mismatched Angel exits safely; the scheduler does not mount any disk. Failed scheduled runs display a local macOS notification using `osascript` and retain stdout/stderr under `.state/backup/logs/`.

```bash
./scripts/render-backup-t7-launchd             # render both plists only; safe to inspect
./scripts/install-backup-t7-launchd            # rerender from this checkout, validate, and load both
./scripts/uninstall-backup-t7-launchd          # unload both; leaves plists for inspection
launchctl print "gui/$UID/com.ai-homelab.t7-backup"
launchctl print "gui/$UID/com.ai-homelab.t7-check"
./scripts/backup-t7-status
```

The backup job never runs retention. Retention is separate and is **disabled by default in code** — `backup.retention.enabled` defaults to `false` in `tools/ingest/config.py`, so it is off whether or not a config file mentions it. The live `config/ingest.yaml` on this machine has no `backup.retention` block at all and therefore inherits that default; `config/ingest.example.yaml` shows the block explicitly with `enabled: false` so the knob is discoverable. Its proposed policy is 7 daily / 4 weekly / 6 monthly (no yearly retention). `./scripts/backup-t7-retention` is a repository-pinned `restic forget --dry-run` preview. Even with `--apply --confirmed-after-restore`, destructive `forget --prune` refuses unless `backup.retention.enabled: true` is set deliberately. Do not enable it yet: this protects snapshot `277aff3a` from accidental removal while its questionable historical provenance is preserved.

For a monthly restore drill, configure `backup.restore_drill_relative_path` to a small stable path relative to Angel's UUID-verified root. `restore-t7-drill` restores only that path into a temporary directory below `.state/backup/restore-drills`, compares SHA-256 hashes, and removes that temporary target. It never writes to Angel or a production recovery target.

## Fresh-Mac disaster recovery

1. Install Restic, rclone, and uv (for example, `brew install restic rclone uv`), clone this control-plane repository, then recreate its environment with `uv venv .venv && uv pip install --python .venv/bin/python -r requirements-ingest.txt -r requirements-social-video.txt`. Never copy an old virtualenv between checkouts.
2. Copy `config/ingest.example.yaml` to ignored `config/ingest.yaml`; set the actual Angel APFS UUID. Do not copy a secret config into Git.
3. Run `rclone config` interactively to create `gdrive-backup` and authorize the Google Drive account. If its config is encrypted, store that config password in Keychain under the configured rclone service. Store the Restic repository password in Keychain under the configured Restic service. Credentials, OAuth tokens, and passwords are never committed.
4. Confirm access: `./scripts/backup-t7 --preflight`, then `./scripts/backup-t7-snapshots` to locate a snapshot.
5. Validate before recovery: `./scripts/backup-t7-check` (or `--deep` when time allows).
6. Restore only to an empty, local recovery directory: `./scripts/restore-t7 SNAPSHOT_ID --target ~/T7-Recovery`. Add `--include /path/in/snapshot` for a scoped recovery. Never target Angel, `/`, or the home directory itself.
7. On a replacement Mac, render and install the LaunchAgents from the new checkout with `./scripts/install-backup-t7-launchd`, then inspect both jobs with `launchctl print` before forcing a test run.

### One-time credential setup

Create a Google Cloud project, enable the Google Drive API, configure OAuth consent as required, and create a **Desktop app** OAuth client. Then use `rclone config` interactively: create remote `gdrive-backup`, choose the Google Drive backend, paste that dedicated client ID and secret only into rclone's prompts, select the `drive.file` scope (files created by rclone), decline service-account and advanced settings unless deliberately needed, and complete the browser authorization. Verify without writing data using `rclone lsd gdrive-backup:`. Rclone's config/token remains in its user configuration directory and is never committed.

Create the repository password directly in macOS Keychain—this command ends in `-w` so macOS prompts securely instead of placing the password in shell history or process arguments:

```bash
security add-generic-password -U -a "$USER" -s ai-homelab-t7-restic -w
```

If you enabled rclone configuration encryption, also run this once and enter the **same rclone configuration password** chosen in `rclone config`:

```bash
security add-generic-password -U -a "$USER" -s ai-homelab-rclone-config -w
```

After the remote and Keychain report are green, run `./scripts/backup-t7-init` to verify the existing pinned repository, then `./scripts/backup-t7 --preflight`. This workflow deliberately cannot create or reinitialize a missing repository; changing the repository requires an explicit reviewed code change. Review the preflight output and obtain explicit approval before running the first `./scripts/backup-t7` upload.

## Status (re-verified 2026-09-18 during the V1 completion pass)

| Item | State on 2026-09-18 |
|---|---|
| Source volume | mounted, UUID matches; **2.2 GiB** on the volume after the D-061 layout migration (14 bare repository mirrors, one archive, READMEs, manifest, exported knowledge and skills) — none of which is in any snapshot yet |
| Last successful backup | snapshot `968cce99`, 2026-09-15 02:15 (62.5 KiB, pre-layout) |
| 09-16, 09-17 scheduled runs | exit 1 at the UUID check — Angel was unplugged at 02:15; correct, not a fault |
| 09-18 manual run | **failed before restic ran**: rclone reports `invalid_grant: maybe token expired?` — the Google Drive OAuth refresh token for `gdrive-backup` is expired or revoked. `backup-t7-check` and `restore-t7-drill` fail the same way, so nothing on the volume can be verified or restored until it is repaired |
| Repair | **owner action, interactive:** `rclone config reconnect gdrive-backup:` (browser OAuth), then `./scripts/backup-t7 --preflight`, `./scripts/backup-t7 --tag post-layout-v1`, `./scripts/backup-t7-check`, `./scripts/restore-t7-drill`. The tooling now prints this instruction verbatim instead of the earlier generic "unavailable or unauthorized" |
| Tests | `tests/test_backup.py` (36) and `tests/test_backup_recovery_safety.py` green; a `--preflight` still validates the source, exclusions, and Keychain items |

Until the reconnect happens the D-061 mirrors exist only on the T7 (their commits also exist on GitHub, but local-only branches do not), so the volume is temporarily a single copy for that material. This is the second time the first real test of the off-site chain was a credential problem rather than a data problem — the D-004 independent failure domain remains the fix.

## Status (verified 2026-09-12 on `mac-m4pro`)

| Item | State |
|---|---|
| Source volume | `angel` / `Angel`, APFS (Encrypted), mounted at `/Volumes/Angel`, writable |
| Source identity | pinned by Volume UUID in `config/ingest.yaml` (`t7.volume_uuid`); updated after the 2026-09-11 erase (D-019) |
| Repository | `rclone:gdrive-backup:AI-Homelab-Backups/T7-restic` — **already existed; reused, not re-initialized** (`backup-t7-init` reported "Repository already exists") |
| Keychain | `ai-homelab-t7-restic` and `ai-homelab-rclone-config` both resolve; no secret appears in argv, config, or logs |
| Integrity | `backup-t7-check` (metadata + 5% packfile read) 2026-09-12 — "no errors were found" across all 8 snapshots, 18/18 packs read |
| Restore drill | `restore-t7-drill` 2026-09-12 — `"restore_drill_status": "success"`, exit 0. `backup.restore_drill_relative_path` is configured to a small live marker file under `00-inbox/`; the drill restores only that path into a temp directory below `.state/backup/restore-drills`, compares SHA-256, and removes the temporary target. (Earlier manual drill, 2026-09-11: snapshot `2b5de56e` restored with `--include`; all three test files hash-identical.) |
| Backup run | `c8b90556` (tags `t7`, `angel`, `automatic`) 2026-09-12 — **exit 0 / `"outcome": "success"`**, 5 files / 28,818 bytes processed, 0 bytes added. Angel currently holds only the bootstrap scaffold and the drill marker; no ingested asset has landed on it yet. |
| Stored in Drive | as of 2026-09-11: 378 objects, 5.883 GiB, laid out as a real restic repository (`config`, `data/`, `index/`, `keys/`, `locks/`, `snapshots/`); sampled objects are ciphertext, not browsable files |
| Deep check | `backup-t7-check --deep` not yet run |
| Scheduling | `com.ai-homelab.t7-backup` (nightly 02:15) and `com.ai-homelab.t7-check` (Sunday 03:15) are **rendered, loaded, and bootstrapped** into `gui/$UID`; `com.ai-homelab.file-intelligence` is deliberately **not** loaded (opt-in, D-024) |

Snapshot `277aff3a` (2026-09-11 08:08, 7.906 GiB, 361,572 files) predates the erase and carries the retired volume UUID. **It is a partial snapshot of a failing disk, not a complete copy of the old T7** — see the coverage section below. Do not run `backup-t7-retention --apply`, `restic forget`, or `restic prune` against this repository.

### Resolved: restic exit 3 caused by `.Spotlight-V100`

macOS keeps `/Volumes/Angel/.Spotlight-V100` unreadable even to the owner (`openfile for readdirnames failed: operation not permitted`), so restic reported one unreadable path and exited **3** on every run. The snapshot was always complete, but `tools/backup.py` maps any non-zero exit to `"outcome": "restic_failure"`, which made healthy backups look failed — both 2026-09-11 runs before the fix show this.

Fixed 2026-09-11 by adding macOS volume metadata to `backup.exclude_patterns` (code defaults in `tools/ingest/config.py`, mirrored in `config/ingest.example.yaml`), so restic never descends into those paths. The next run exited 0 with `"outcome": "success"`.

Optional and still worthwhile, per PLAN.md Phase 1: `sudo mdutil -i off /Volumes/Angel` stops Spotlight indexing the T7 altogether, saving write wear and CPU. It needs an interactive sudo password and is not required for backups to succeed.

## Pre-erase salvage coverage (the 2026-09-11 exFAT failure)

The old exFAT `Angel` volume failed before it was erased: `~/Desktop/angel-recovery-logs/rclone-recovery.log` records **31,769 input/output errors** across a 2h37m `rclone copy /Volumes/Angel "gdrive-backup:Angel Backup"` run that ended with 44,802 total errors and only 508 files transferred. The restic run the same morning hit the same wall and exited 3. Neither salvage is complete, and they captured **different** subsets. The volume was then erased and recreated as APFS, so neither can be re-run.

Two independent partial copies exist in Google Drive:

- `AI-Homelab-Backups/T7-restic`, snapshot `277aff3a` — encrypted restic. Holds `App spec` (247,308 entries), `BiteSites` (113,907), `CAAH QA` (290), `CAAH Clips` (20). Everything alphabetically after `CAAH QA` is present only as a zero-content `._name` AppleDouble stub.
- `Angel Backup/` — plain rclone copy, 510 files / 6.034 GiB. Holds `Henry's Animal Hospital Take` (3.4 GiB), `CAAH QA` (1.8 GiB), `CAAH Clips` (882 MiB), `Fine Line Group LLC` (3.4 MiB), and small fragments of `App spec`/`BiteSites`/`Mafi`. Its other top-level folders are empty directory shells.

| Pre-erase folder | restic `277aff3a` | Drive `Angel Backup` | Local Mac / Drive Desktop | Status |
|---|---|---|---|---|
| App spec | full (247k entries) | 72 files | `~/Documents/App spec` (574M) | covered |
| BiteSites | full (114k entries) | 76 files | `~/Documents/BiteSites` (270M) | covered |
| CAAH Clips | 20 entries | 882 MiB / 20 files | — | Drive-only |
| CAAH QA | 290 entries | 1.8 GiB / 103 files | — | Drive-only |
| Fine Line Group LLC | stub only | 3.4 MiB / 23 files | — | Drive-only, likely partial |
| Henry's Animal Hospital Take | stub only | 3.4 GiB / 124 files | `~/Documents` (3.7G) | covered |
| Mafi | stub only | 735 KiB / 69 files | `~/Documents/Mafi` (6.8G) | covered locally |
| NJCCC Photos | absent | empty shell | — | **no copy found** |
| NodeVideo | stub only | — | `~/Documents/NodeVideo` (empty) | unknown; local dir is empty |
| Oasis Pure Water | absent | empty shell | `~/Documents` (726M) | covered locally |
| Oasis Pure Water Video | stub only | empty shell | `~/Desktop` (1.3G) | covered locally |
| Pupusa Loca Menu Images | stub only | empty shell | `~/Documents` (59M) | covered locally |
| SCREENSHOTS | stub only | empty shell | — | **no copy found** |
| Stone Bellisimo | stub only | empty shell | Drive Desktop (237 files) | covered in Drive |
| dashboard-demos | stub only | 2 empty files | `~/Documents` (5.1G) | covered locally |
| fonts | stub only | empty shell | `~/Documents` (444K) | covered locally |
| huggingface | stub only | empty shell | `~/Documents` (24K) | covered locally (re-downloadable) |
| kronos-quant-lab | stub only | 1 empty file | `~/Documents` (3.9G) | covered locally |
| tlc-dashboard | stub only | empty shell | `~/Documents` (1.0G) | covered locally |

Unresolved: **`NJCCC Photos`** and **`SCREENSHOTS`** have no located copy in either salvage or on the Mac. `Fine Line Group LLC` exists only as the 23-file rclone fragment. Confirm from memory whether those folders held anything irreplaceable before treating them as lost.

This is the concrete argument for D-004's independent failure domain: the immediate Drive repository was configured, but the first real test of it was a dying disk, and a backup that had never completed a verified run could not protect what had not yet been captured.

## Recovery objectives

| Data class | RPO | RTO | Recovery path |
|---|---|---|---|
| Client raw media | 24 h | 1 day | restic restore from the dedicated Google Drive repository; later Dell/B2 copies provide independent fallback |
| Catalog + sidecars | 1 h | 1 h | restic from the dedicated Google Drive repository (catalog is one SQLite file) |
| Committed code | 0 | minutes | GitHub |
| Derived view / proxies | n/a | rebuild | regenerate from catalog |
| Model caches | n/a | re-download | pinned checksums |

Irreplaceable: original client footage, human corrections in the catalog, unpushed Git work found during ingestion. Everything else is recreatable.

## Suggested data classes

| Class | Example | Backup priority |
|---|---|---|
| Irreplaceable | original client footage | highest |
| Source-controlled | committed repos | high, but recoverable from Git |
| Generated | proxies/builds | low |
| Cache | model/npm/pip caches | usually none |
| Restricted secrets | broker/API credentials | secure secret backup, not ordinary file backup |

## Coordinator telemetry (D-050)

The Mac-local job ledger at `.state/coordinator/jobs.sqlite` is **not** in
this backup, and that was an accident rather than a decision. D-033 put it
on the Mac deliberately, so routing survives Angel being detached; `.state/`
is gitignored; and the restic source is pinned to the T7 root. The net
effect was that the telemetry model certification (D-042) and the Phase 6
"measure before upgrading hardware" rule both depend on existed in exactly
one copy, on one internal SSD.

The ledger stays where it is and stays authoritative. What changed is that a
normalized, derived snapshot can be written into UUID-verified Angel
`40-ai/evaluation/telemetry/`, which this repository already covers
(`40-ai/model-cache/**` is the only exclusion, D-037):

```bash
python -m tools.report --days 30 --snapshot-dir /Volumes/Angel/40-ai/evaluation/telemetry
```

Properties worth knowing before relying on it:

- **Derived, not state.** Losing every snapshot costs history, never
  correctness. Nothing reads a snapshot back into the ledger, and it is not
  a restore source.
- **Aggregates only.** Job counts, dispositions, tiers, task classes,
  verification coverage, tokens, timing, cost and energy estimates. Never
  prompt or response text, which stays on the Mac under D-030/D-035.
- **UUID-gated.** `tools.coordinator.snapshot.export_to_angel` goes through
  the same `resolve_ai_storage` check as everything else and raises on an
  absent, locked, read-only or uninitialised volume. It does not fall back
  to the internal disk, because a local fallback would recreate the
  single-copy problem it exists to close.
- **Atomic.** Written via `mkstemp` + `os.replace`, so a concurrent backup
  never captures a half-written file.
