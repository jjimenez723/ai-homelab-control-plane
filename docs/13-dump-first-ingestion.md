# 13 — Dump-First and Ongoing T7 Ingestion

The storage system has two distinct ingestion modes. They share catalog, hashing, provenance, and classification code, but **not** the same deletion policy.

## Mode A — one-time legacy dump

For the initial migration, preserve the original sources.

**Dump first, classify second, reorganize non-destructively.**

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

Copy messy source folders into the matching source-device folder with minimal manual effort. Do not require manual naming first. Inventory, hash, and verify the dump before any source disk is wiped or reformatted.

## Mode B — day-to-day cleanup

Normal Desktop/Documents/Downloads/SD cleanup is an explicit, transactional **move** workflow. It is not a background LLM with filesystem delete permission.

### User interface

The canonical interface is one CLI:

```text
organize desktop
organize documents
organize downloads
organize sd
organize all

organize all --dry-run
organize all --apply
```

A double-clickable macOS `Organize Files.command` launcher calls the same CLI.

During development, all sources are manual-trigger. After the acceptance gate is green, Downloads alone may be enabled as an opt-in launchd/FSEvents watcher.

### Source policy

- **Desktop:** manual only.
- **Documents:** manual only.
- **Downloads:** manual during development; later opt-in automation for settled files only (default unchanged for at least 20 minutes).
- **SD cards/removable media:** detect mount automatically by volume UUID, but only registered source volumes are eligible and originals require explicit `--apply`.
- **T7 absent/locked/read-only:** no apply operation runs.

Mounting the T7 by itself never empties Desktop or Documents.

### Transactional move primitive

A cross-volume "move" is implemented safely:

1. validate source is eligible and settled;
2. capture source metadata/provenance;
3. copy into a unique T7 staging/`.partial` path;
4. close/flush the destination;
5. SHA-256 the destination and compare with the source hash;
6. atomically rename/register the verified T7 asset and commit catalog state;
7. only then unlink the source;
8. record completion.

If power is lost, the T7 is unplugged, a hash differs, SQLite fails, or the process is killed before step 7, the source remains. Retries are idempotent and reconcile partial destinations instead of duplicating them.

The deterministic ingestion code owns source deletion. **AI agents never do.**

### Hard exclusions

Default-deny or exclude:

- any Git repository/worktree and `.git`;
- `node_modules`;
- virtual environments;
- application/package bundles;
- Photos libraries;
- browser partials such as `.crdownload`, `.download`, `.part`;
- files still changing;
- system/hidden metadata;
- symlinks escaping an allowed source root;
- configured `keep-local` paths/markers.

The exclusion set is configurable and test-covered.

## What AI does after verified ingestion

For each verified T7 asset:

1. use the deterministic metadata first (original path/name, MIME, timestamps, EXIF, Git/document/media metadata);
2. assign or reuse a stable asset ID;
3. detect exact duplicates;
4. extract richer metadata/keyframes/transcript where appropriate;
5. classify likely type/domain/client/project;
6. generate tags and embeddings;
7. propose a confidence-scored canonical filename and destination;
8. materialize the organized view only under the confidence policy.

High-confidence items may be organized automatically. Medium/low confidence items remain in `00-inbox/needs-review/` with the proposed name/destination stored in metadata.

## Catalog provenance

At minimum preserve:

- asset ID and SHA-256;
- original volume UUID;
- original path and filename;
- source class (desktop/documents/downloads/sd/legacy);
- detected, transferred, verified, classified timestamps;
- MIME/type/size/capture time;
- transfer state and recovery/error state;
- final path and filename;
- classifier/model/version/confidence;
- human corrections.

Sidecars remain the durable human-readable record; SQLite is the query/transaction layer.

## Code repositories

Detected Git repositories are inventoried, not moved by recurring cleanup. Record remote URLs, branch, dirty/untracked state, last commit, likely project/framework/language, and duplicate clones. Never let a model merge/delete duplicate repositories automatically.

## Media

Images/video preserve technical and capture metadata. Video classification may use keyframes + transcript + VLM. SD-card filesystem/camera metadata structures are not blindly destroyed; bulk card wiping/reformatting is a separate user action after backup requirements are satisfied.

### HEIC/HEIF compatibility derivatives

HEIC/HEIF photos are never destructively converted. The ingested HEIC/HEIF remains the authoritative archival original and receives the normal source hash/provenance record.

After verified ingestion, the media pipeline may generate a JPEG compatibility derivative with this default configuration:

```yaml
media:
  heic_to_jpeg:
    enabled: true
    preserve_original: true
    jpeg_quality: 100
    preserve_metadata: true
    preserve_icc_profile: true
    normalize_orientation: true
```

Requirements:

- JPEG quality is fixed at **100** unless the owner explicitly changes the configuration later.
- Quality 100 means the encoder's maximum JPEG quality setting; it is still a lossy JPEG encoding, so the original HEIC/HEIF must be retained.
- Preserve transferable EXIF, capture timestamp, orientation, color/ICC profile, and other safe image metadata where the selected converter supports it.
- The derivative must share the same logical asset ID as the original while having its own file ID/hash/path.
- Record `derived_from`, converter/tool name, converter version, output format, quality setting, conversion timestamp, and conversion status in the catalog/sidecar.
- Use deterministic collision-safe naming. Re-running conversion must not create `photo-2.jpg`, `photo-3.jpg`, or otherwise duplicate an existing valid derivative.
- A corrupted input or converter failure records an error and leaves the verified HEIC/HEIF untouched.
- AI classification treats the HEIC original and JPEG derivative as one logical photo, not two independent assets.
- The organized/consumer-facing view may prefer the JPEG for compatibility while the archival/source view retains the HEIC/HEIF original.

## Organized view

The organized tree is derived from catalog state. Model confidence controls organization, not whether a verified transfer is considered successful. A worse future model must be able to regenerate the view without losing original provenance.

## Garbage collection

Nothing is deleted merely because it appears duplicated. Duplicate garbage collection requires a hash-confirmed canonical copy, backup/retention policy satisfaction, and explicit approval or a separately accepted deletion policy.

This separation lets file movement be reliable and boring while AI classification improves independently.
