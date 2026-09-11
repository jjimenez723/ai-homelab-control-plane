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
T7 / `angel` (exFAT + Samsung Magician password, Mac-attached)   primary working copy
   |
   | encrypted, deduplicated restic snapshots
   v
Google Drive: AI-Homelab-Backups/T7-restic                  immediate off-site copy
  (repository objects written through rclone, not Drive sync)
```

Rules:
- The dedicated Google Drive repository is `rclone:gdrive-backup:AI-Homelab-Backups/T7-restic`; it contains only encrypted restic repository data, never a browsable T7 mirror.
- The source is pinned by `t7.volume_uuid`, never `/Volumes/T7` or its current Finder name. If Samsung security is locked, the disk is disconnected, or the UUID differs, backup exits without making an empty snapshot.
- The repository passphrase is held only in macOS Keychain (`ai-homelab-t7-restic`) and read by `RESTIC_PASSWORD_COMMAND`. **Losing it permanently loses access to this backup.** Never place it in Drive, Git, `.env`, config, or logs.
- If rclone configuration encryption is enabled, its separate passphrase belongs in Keychain (`ai-homelab-rclone-config`) and is supplied through `RCLONE_PASSWORD_COMMAND`; manual and scheduled rclone calls stay noninteractive.
- Google Drive is not immutable or append-only. It is useful immediate off-site protection, but does not eliminate the later append-only `dell-xeon` restic repository and independent B2 failure domain from D-004.
- No worker node holds the backup credentials. Workers are never backup sources of record.
- Run `backup-t7-check` (metadata plus a 5% data subset) regularly; run `backup-t7-check --deep` periodically and record a real restore drill before declaring production readiness.
- Time Machine of the Mac internal disk continues independently; it is not the T7 strategy.

## Operations

All commands use `config/ingest.yaml` (ignored by Git) and inherit the source UUID from `t7.volume_uuid`.

```bash
./scripts/backup-t7-init                    # safe: initializes only if repository does not exist
./scripts/backup-t7 --preflight             # read-only source, remote, Keychain, and size report
./scripts/backup-t7                         # normal snapshot: t7, angel, automatic tags
./scripts/backup-t7 --tag pre-organize      # lifecycle snapshot: t7, angel, pre-organize
./scripts/backup-t7 --tag post-organize
./scripts/backup-t7-snapshots               # ID, time, host, paths, tags
./scripts/backup-t7-check                   # metadata + random 5% data reads
./scripts/backup-t7-check --deep            # full data read, schedule periodically
./scripts/backup-t7-retention               # report only; no deletion
./scripts/backup-t7-retention --apply --confirmed-after-restore
./scripts/restore-t7 SNAPSHOT_ID --target ~/T7-Restore
./scripts/restore-t7 SNAPSHOT_ID --target ~/T7-Restore --include /path/in/snapshot
```

Restores require `--target`; `/`, the home directory itself, the live T7, and a path inside the live T7 are refused. For a drill, restore a small non-sensitive source subset into a temporary local directory, hash both copies, then remove only that temporary restored copy after verification. `restic diff SNAPSHOT_A SNAPSHOT_B` is useful for comparing snapshots.

The normal backup writes non-secret JSONL metadata (start/end, source UUID, repository, tags, processed bytes/files, snapshot ID, and outcome) under ignored `.state/backup-logs/`. The excluded paths are intentionally narrow: T7 ingest staging and `*.partial` transfer artifacts. Originals, catalog, SQLite sidecars, manifests, human corrections, and JPEG derivatives are included.

`launchd/com.ai-homelab.t7-backup.plist.template` is a nightly 02:15 template. `./scripts/render-backup-t7-launchd` only renders a private LaunchAgent; it never loads or enables it. Do not load it until manual backup, check, and restore acceptance are recorded. A missing/locked T7 exits cleanly, and launchd does not contain credentials.

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

After the remote and Keychain report are green, run `./scripts/backup-t7-init`, then `./scripts/backup-t7 --preflight`. Review that output and obtain explicit approval before running the first `./scripts/backup-t7` upload.

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
