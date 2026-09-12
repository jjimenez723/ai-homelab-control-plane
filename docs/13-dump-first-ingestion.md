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

Mode A is unchanged by the Phase 1B naming and triage work described under Mode B. The legacy dump stays copy-only and non-destructive, nothing in it is renamed, and no retention decision is made about it. The bulk inventory pass over a legacy dump is still to be built.

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

During development, all sources are manual-trigger. Downloads and Screenshots may additionally be enabled as an opt-in, hourly launchd **one-shot** — not a resident FSEvents watcher (D-024). It is disabled by default, and enabling it enables naming and reporting, not transfers.

### Source policy

- **Desktop:** manual only.
- **Documents:** manual only.
- **Downloads:** manual during development; later opt-in automation for settled files only (default unchanged for at least 20 minutes).
- **Screenshots:** a naming source only when `sources.screenshots.path` is set; there is no default and the path is never inferred (D-025).
- **SD cards/removable media:** detect mount automatically by volume UUID, but only registered source volumes are eligible and originals require explicit `--apply`.
- **T7 absent/locked/read-only:** no apply operation runs.

Mounting the T7 by itself never empties Desktop or Documents.

### Naming and retention triage (Phase 1B)

Before anything is transferred, Mode B asks two separate questions about each settled file. The stage runs entirely on the Mac and moves nothing.

1. **Discovery** — the source walk and the exclusion set below produce a candidate.
2. **Deterministic metadata** — size, mtime, MIME, and container-level media facts (`tools/ingest/media/probe.py`) are measured from the file, never guessed from its name.
3. **Name quality** — `tools/ingest/naming/quality.py` decides whether the existing filename already carries information. A name the owner clearly chose is left alone; `IMG_4821.HEIC`, `Screenshot 2026-09-12 at 11.03.47.png`, and `download (3).pdf` are not.
4. **Name proposal** — only for a file that failed the quality check. The proposer receives `NamingEvidence`: plain data with no `Path`, no file handle, and no callable, so it has nothing to mutate the filesystem with. The V0 proposer is deterministic and names from facts the file states about itself; a model can be substituted behind the same protocol without changing anything below it (D-022). There is no OCR, no image understanding, and no client/project inference.
5. **Rename** — `naming/policy.py` holds the only rename primitive. It takes a *name*, not a path, so it cannot relocate a file, and it cannot overwrite an existing one. The extension comes from the file, never from the proposal. A name recorded with `organize correct-name` outranks every later proposal, permanently.
6. **Retention triage** — `tools/ingest/triage/policy.py` classifies the file `ignore`, `review`, or `ingest`, biased toward `review`, reading only measured facts.
7. **Verified transfer** — only an `ingest` file reaches the transactional primitive below, and only when the owner applies it or `triage.auto_ingest` is deliberately turned on (it is `false` by default).

Steps 1-6 read and write only the Mac-side ledger at `.state/file-intelligence/local-ledger.sqlite` (D-021), so naming keeps working with the T7 unplugged. Step 7 is the only step that needs Angel, and it is unchanged from Phase 1A.

Two boundaries hold regardless of configuration:

- **Naming does not imply retention.** A file can be given a useful name and never be archived. The two answers come from different code reading different evidence and are stored in different tables (D-020).
- **`ignore` is not `delete`.** `ignore` means "not in the durable library on Angel". Nothing in the naming or triage layer can remove a file; the only file removal in this system remains the source unlink in step 7 of the transactional primitive, after the bytes are verified on the T7.

The commands:

```text
organize triage downloads
organize triage screenshots
organize triage all --dry-run
organize triage all --apply

organize status          # what the pipeline knows and is waiting on
organize doctor          # whether this machine is configured to run safely
organize review          # files waiting on a human decision
organize duplicates      # observed duplicate relationships, never acted on
organize correct-name PATH
```

Screenshots are a first-class naming source with **no inferred default path** (D-025). macOS writes screen captures to `~/Desktop` by default, so guessing would put the whole Desktop under evaluation; `sources.screenshots.path` must be set deliberately, and `organize doctor` reports the macOS setting without adopting it.

`docs/15-file-intelligence-acceptance-drill.md` is the hand-run drill that demonstrates each of these properties on throwaway files before the pipeline is pointed at anything real.

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

## Implementation (Phase 1A)

Mode B is implemented in `tools/ingest/` and exposed as the `organize` CLI (`scripts/organize`, `Organize Files.command`). `tools/ingest/transaction.py` is the one transactional move primitive described above; every source class in `tools/ingest/sources.py` routes through it. `tools/ingest/catalog.py` holds the SQLite schema and sidecar writer; `tools/ingest/media/heic.py` is the HEIC->JPEG derivative. `config/ingest.example.yaml` documents the human-editable configuration. `tests/ingest/` is the fault-injection suite covering the failure modes listed above.
