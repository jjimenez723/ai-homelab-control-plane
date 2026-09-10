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

## Recovery objectives

Document later:
- what can be recreated;
- what is irreplaceable;
- target recovery time;
- target tolerated data loss;
- where encryption recovery keys live.

## Suggested data classes

| Class | Example | Backup priority |
|---|---|---|
| Irreplaceable | original client footage | highest |
| Source-controlled | committed repos | high, but recoverable from Git |
| Generated | proxies/builds | low |
| Cache | model/npm/pip caches | usually none |
| Restricted secrets | broker/API credentials | secure secret backup, not ordinary file backup |
