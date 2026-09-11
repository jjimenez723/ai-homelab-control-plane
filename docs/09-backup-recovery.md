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

## Concrete V0 architecture (decided — D-004)

```text
T7 (exFAT + Samsung Magician password, Mac-attached)   primary working copy
   |
   | restic backup (hourly when attached, scheduled by launchd)
   v
dell-xeon internal disk, restic rest-server --append-only   copy 2, same building
   |
   | restic copy / second repo, nightly
   v
Backblaze B2 bucket (object lock / versioning on)           copy 3, offsite
```

Rules:
- The backup identity is a dedicated credential that can **append but not delete** (rest-server `--append-only`; B2 application key without `deleteFiles`). Ransomware or a destructive agent on the Mac can therefore add junk but cannot erase history.
- No worker node holds the backup credentials. Workers are never backup sources of record.
- `restic check --read-data-subset=5%` weekly; a scripted **restore drill** of one random client folder monthly, recorded in `90-system/migration-reports/`.
- Time Machine of the Mac internal disk continues independently; it is not the T7 strategy.
- Secrets (Samsung T7 security password, restic password, B2 key) live in the password manager with printed recovery in a physically separate place. Losing the restic password loses every backup. The T7 password protects local access but does not replace independent backups.

Budget: B2 ≈ $6/TB/month; the second copy is free (existing Xeon disk); restic is free.

## Recovery objectives

| Data class | RPO | RTO | Recovery path |
|---|---|---|---|
| Client raw media | 24 h | 1 day | restic restore from copy 2, else B2 |
| Catalog + sidecars | 1 h | 1 h | restic (catalog is one SQLite file) |
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
