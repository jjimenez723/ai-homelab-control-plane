# 10 — Phase 0 Migration Plan

This plan intentionally avoids destructive operations.

## Phase 0A — inventory

- Record T7 capacity, filesystem, encryption state, free space, health, and speed.
- Record all source locations containing:
  - Git repositories;
  - client media;
  - project assets;
  - research;
  - personal documents that should not be mixed into this system.
- Inventory all Git repositories and their remotes.
- Identify uncommitted/untracked work before copying anything.

## Phase 0B — establish tree

Create only the top-level directories from `docs/02-t7-filesystem.md`.

Do not yet automate renaming.

## Phase 0C — code

For repositories already safely pushed:
- prefer fresh clones onto `T7/10-code/...`;
- verify expected branch/remotes;
- do not copy `node_modules`, virtual environments, build outputs, or disposable caches unless there is a specific reason.

For repositories with uncommitted work:
- commit/push safely where appropriate, or
- copy the complete repository including `.git` and record that it contains uncommitted state.

## Phase 0D — media

- Copy raw media into client/project raw folders.
- Preserve original names.
- Generate checksums/manifests.
- Do not delete original cards/disks yet.
- Do not allow an LLM to rename or delete originals.

## Phase 0E — second copy

Before cleanup of source disks, establish another verified backup for irreplaceable media.

## Phase 0F — AI catalog pilot

Choose one small client shoot.

Run:
1. metadata extraction;
2. thumbnails/keyframes;
3. vision tags;
4. filename proposals;
5. search/indexing.

Measure accuracy manually.

Only after the pilot is reliable should rename/materialization automation expand.

## Phase 0G — worker-node pilot

Convert one Dell, not all at once.

Prove:
- headless boot;
- SSH;
- updates;
- firewall;
- worker registration;
- one sandboxed job;
- logs;
- restart recovery.

Then repeat for the second worker.

## Rollback principle

At every phase, the old workflow remains usable until the new phase is verified.
