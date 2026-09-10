# 13 — Dump-First T7 Ingestion

The Samsung T7 is brand new and empty. This changes the migration strategy.

The owner explicitly does **not** want to manually sort legacy projects, client work, photos, and videos before the AI system exists.

That is a reasonable constraint.

## V0 rule

**Dump first, classify second, reorganize non-destructively.**

Create:

```text
T7/
└── 00-inbox/
    └── legacy-dump/
        ├── macbook/
        ├── dell-xeon/
        ├── dell-i7/
        ├── old-dell/
        ├── sd-cards/
        └── other/
```

The user may copy messy source folders into the matching source-device folder with minimal manual effort.

Do not require them to rename files first.

## What the AI ingestion service does

For every file:

1. assign a stable asset/file ID;
2. capture original path + filename;
3. hash the file;
4. detect exact duplicates;
5. extract filesystem/media/document metadata;
6. determine likely type/domain/client/project;
7. generate tags;
8. generate a confidence-scored canonical name;
9. create embeddings/index entries where appropriate;
10. propose an organized destination;
11. create an organized **derived view** without destroying the original dump.

## Code repositories

For detected Git repositories:
- identify `.git`;
- record remote URLs;
- branch;
- dirty/untracked state;
- last commit;
- likely project name;
- framework/language;
- duplicate clones.

Do not let a model merge/delete duplicate repositories automatically.

Prefer fresh canonical clones later once dirty/unpushed work has been identified.

## Media

For images/video:
- preserve originals;
- hash;
- EXIF/technical metadata;
- keyframes/transcript for video;
- vision tags;
- likely client/project;
- canonical-name proposal;
- semantic index.

## Organized view

The long-term organized tree can be generated from the catalog rather than requiring the original physical tree to be perfect.

Possible implementation strategies:
- safe copy;
- hardlink where filesystem semantics permit and policy allows;
- symlink/reference view;
- database-backed virtual catalog;
- later physical migration after confidence is high.

## Garbage collection

Nothing gets deleted merely because it appears duplicated.

Deletion should require:
- hash-confirmed duplicate;
- canonical copy verified;
- backup policy satisfied;
- retention policy satisfied;
- explicit approval or a separately designed deletion policy.

This lets the owner get the benefit of AI organization immediately without betting irreplaceable files on first-pass model accuracy.
