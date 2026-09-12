# 16 — File Intelligence: naming, retention triage, and automation

This is the operational half of `tools/ingest/`. `docs/13-dump-first-ingestion.md` describes *moving* files onto Angel; this document describes deciding **what a file is called** and **whether it belongs on Angel at all** — two questions that the code answers separately and on purpose.

Read this before changing anything under `tools/ingest/naming/`, `tools/ingest/triage/`, or `tools/ingest/pipeline.py`. Several things that look like incidental structure are load-bearing safety properties, and the module split is the enforcement mechanism rather than tidiness.

Decisions of record: D-016 (manual-first ingestion), D-019 (the T7 volume-UUID boundary), D-020 (naming vs retention), D-021 (the local ledger), D-022 (a model proposes, trusted code disposes), D-023 (`00-inbox/organized` keeps its name), D-024 (opt-in automation), D-025 (screenshots are configured, never inferred), D-026 (`storage_root_override` needs an explicit flag).

`docs/15-file-intelligence-acceptance-drill.md` is the companion: a ten-minute hand-run drill against a scratch directory and a scratch T7 root that demonstrates the guarantees described here. Run it once before trusting the pipeline with anything that matters.

## The four stages, and why they are separate

```text
discovery  ──▶  naming  ──▶  retention  ──▶  ingestion
   │              │             │                │
 what is       what should   does this      move it, verify it,
 there?        it be         belong in      and only then unlink
               called?       the library?   the source
```

Every stage reads the output of the one before it. None of them can skip ahead, and — this is the part that matters — a decision at one stage never implies a decision at the next.

**Discovery** (`pipeline.py`, `scanner.py`, `exclusions.py`) finds settled, eligible files. A file still being written is skipped entirely: hashing it would record bytes that are about to change, and renaming it could break whatever is writing it.

**Naming** (`naming/`) asks what the file should be called. It runs over every eligible file, including files that will never be archived. This is the whole point of D-020: a screenshot deserves a useful filename whether or not it deserves permanent storage, and making the name depend on the retention answer would mean the Mac's own Downloads folder stays illegible forever.

**Retention** (`triage/policy.py`) asks whether the file belongs in the durable Angel library. It answers `IGNORE`, `REVIEW`, or `INGEST` — and `IGNORE` means *not in the library*, never *delete from the Mac*. Nothing in this stage can delete anything.

**Ingestion** (`transaction.py`, unchanged from Phase 1A) is the only stage that can remove a file from the Mac, and only as the final step of a verified transfer: copy to a `.partial` staging path → flush/close → SHA-256 verification at the destination → atomic finalize and catalog commit → *then* unlink the source.

Collapsing any two of these is the specific mistake this design exists to prevent. If naming implied retention, every renamed screenshot would end up on Angel. If retention implied deletion, a wrong guess would cost a file. If ingestion were reachable without verification, a crash mid-copy would cost a file. They are separate because the failure modes are different sizes.

### Angel being absent only disables the last stage

Discovery, naming, and triage all run with the T7 unplugged. Ingestion does not, and reports that it did not. This is not graceful degradation bolted on afterwards — it is why the naming ledger lives on the Mac (D-021). A design where naming needed the drive connected would have made the most frequent operation depend on the least reliable precondition.

## The AI boundary, and how it is enforced structurally

The system is built so a model can be plugged into exactly one place. That place is `naming/proposer.py`:

```python
class NameProposer(Protocol):
    model: str
    model_version: str
    def propose(self, evidence: NamingEvidence) -> NameProposal | None: ...
```

A proposer receives `NamingEvidence` and returns a string plus a confidence. The boundary is enforced by **the shape of the data, not by a rule someone has to remember**:

- `NamingEvidence` (`naming/evidence.py`) is a frozen dataclass containing strings, numbers, a dict of measured facts, and one optional PNG byte-string. It carries **no `Path`, no file object, and no callable.** A proposer — including a future model-backed one — therefore has nothing to mutate the filesystem *with*. It is not trusted and then constrained; it is simply not given the means.
- The thumbnail is `bytes`, not a path (`media/probe.py::thumbnail_png` writes nothing to disk). A vision model gets the pixels it is meant to look at with no handle to the user's original and no temp file to leak.
- Evidence is gathered by trusted code *before* any proposer is called, deterministically and read-only.
- What comes back is validated before it can become a filename (`naming/policy.py::validate_proposal`), and the rename itself is performed by `naming/policy.py::safe_rename`, which takes a **name, not a path**, so a caller cannot use it to relocate a file into another directory. Renaming and moving stay separate powers.
- `transaction.py::transfer_file` remains the only code in the system that unlinks a source file.

The proposer shipped today is `DeterministicProposer`. It names from facts the file states about itself — a PDF's own title, an image's EXIF description, a zip's contents, a capture date, pixel dimensions — and where it cannot establish a subject it says something factual and generic rather than inventing one. `2026-09-12_web-browser-product-page.png` is deliberately preferred over a confidently wrong client name.

Swapping in a model-backed proposer changes nothing downstream. No safety property in this document depends on which implementation sits behind the protocol.

> The source docstrings cite "invariant 3", "invariant 7", "invariant 12" and so on. Those numbers refer to the owner's file-intelligence specification, which is not tracked in this repository. The invariants themselves are restated in prose here and in `README.md` so this document stands alone.

## Two databases: the local ledger and the T7 catalog

| | T7 catalog | Local ledger |
|---|---|---|
| Path | `90-system/catalog.sqlite` on Angel | `.state/file-intelligence/local-ledger.sqlite` in the checkout |
| Scope | assets that **have** been ingested | files on the Mac that have **not** been ingested |
| Status | durable truth | rebuildable bookkeeping |
| Available when Angel is unplugged | no | yes |
| Backed up by restic | yes | no, deliberately |
| Tables | `assets`, `repos`, … plus the shared four | `local_files`, `runs`, plus the shared four |

The four shared tables — `naming_history`, `classification_runs`, `media_metadata`, `duplicate_observations` — are defined once in `schema_sql.py` and created in both databases, so a file that starts life in Downloads and is later ingested keeps one continuous, comparable record on both sides. `LedgerTables` in `ledger.py` is mixed into both connection classes for the same reason.

Three separations inside those tables are structural and must not be collapsed:

1. **Measured facts vs. proposals.** `media_metadata` holds what a probe measured. `naming_history` and `classification_runs` hold what a proposer suggested. A proposal never overwrites a fact.
2. **History vs. current state.** Both proposal tables are append-only. A reclassification appends a new row; the newest wins for display, and the earlier recommendation stays auditable after a model upgrade.
3. **Machine vs. human.** `approved_by` and `human_corrected` mark rows a person authored. They are rows, not a mutable flag, precisely so they cannot be overwritten by a later automatic run.

### Identity

Ingested assets keep the Phase 1A identity: `asset_id = uuid5(namespace, "<volume_uuid>:<original_path>")`.

Files on the Mac use `local_file_id = uuid5(namespace, "local:<volume>:<device>:<inode>")` — **not** path-derived. Naming renames files, and a path-derived identity would make every rename look like a brand-new file, losing both the audit lineage and the human-correction lock. Inode numbers can be reused after a delete, so `LocalLedger.observe_file` additionally compares the recorded content hash before inheriting a lineage.

### Migrations

`migrations.py` is shared by both databases. Rules, all enforced by convention and review rather than by a framework:

- never drop or rewrite a column, only add;
- `CREATE TABLE IF NOT EXISTS` and guarded `ALTER TABLE ... ADD COLUMN` (`add_column_if_missing`), so running an old database forward twice is a no-op;
- an existing row is never modified by a migration.

`user_version` is the applied-version marker and is only advanced after a step returns, so a crash mid-migration leaves the version unchanged and the next run re-applies the whole step over the parts that already exist. Because every step is additive and idempotent, re-application cannot lose data. No step is wrapped in a hand-rolled transaction: SQLite commits implicitly around `executescript`, so a `BEGIN`/`COMMIT` here would be a lie about atomicity rather than a guarantee of it.

A database reporting a version **newer** than the code supports is refused outright — upgrade the checkout rather than downgrading the database. The T7 catalog is currently `SCHEMA_VERSION = 2`; migration 2 adds the four shared tables plus `first_seen_filename`, `local_file_id`, `retention_recommendation`, and `retention_run_id` on `assets`.

## Naming policy

`naming/rename.py` is the gate. The order of its checks is the order of the guarantees, and it matters:

1. **A human's name wins, permanently.** If the ledger holds a human-authored name for this file, nothing automatic touches it again — ever, regardless of what a later and better proposer would suggest.
2. **A settled name is not reconsidered.** If the current filename is one this system already chose or already chose to keep, return without writing a row. Re-recording an unchanged decision on every scan would grow the ledger without adding information.
3. **Protected extensions are never renamed.** `.app`, `.dmg`, `.pkg`, `.mpkg`, `.framework`, `.kext` — something else on the system finds these by name.
4. **A useful name is left alone.** `naming/quality.py` answers GOOD or POOR deterministically, from the name alone, before any proposer is consulted. `Fine-Line-Group-2026-SOW.pdf` is a better filename than anything a model will invent for it. POOR means *eligible for a proposal*, not *will be renamed*.
5. **A question already answered by this proposer version is not re-asked.** A proposer *upgrade* deliberately reopens it.
6. **Then, and only then, ask for a proposal**, and require it to clear `naming.min_confidence` (default 0.55).
7. **Validate.** `validate_proposal` rejects anything path-shaped, strips whatever extension the proposal carried and re-attaches the real one from the file on disk, sanitizes the stem, and then **re-runs the quality check on its own output**: a proposal that would still score POOR is refused, because applying it would make the file eligible again next run — a rename loop.
8. **Dry run stops here.** Otherwise perform the single mutation.

Every branch, including every decision *not* to rename, is appended to `naming_history`. That is what makes repeat runs cheap and lets a person see why a file is called what it is called.

### Filename legality

`sanitize_stem` removes control characters and zero-width/bidi-override characters (invisible in a filename, and a classic way to make one name look like another), replaces the union of characters forbidden on APFS/HFS+/exFAT/POSIX, collapses separator runs, and strips leading dots and trailing dots and spaces — a name the filesystem would silently alter is a name the ledger would be lying about. A single underscore is preserved after a leading ISO date (`2026-09-12_kitchen-layout`) and nowhere else. The configured `max_filename_length` (default 120) is a readability cap; `MAX_COMPONENT_BYTES = 255` is the hard APFS ceiling underneath it.

### Collision handling

`collision_free_name` derives its first fallback from content (`-<sha8>`) rather than a counter, so re-running over the same file proposes the same name instead of climbing `-2`, `-3`, `-4`. The numeric tail after that exists only for two genuinely different files sharing both a hash prefix and a stem.

## Screenshot handling

Screenshots are a first-class source for **naming** and are explicitly not a vacuum. `sources.screenshots` has:

- **no default path, and none is ever inferred.** macOS keeps the real location in `com.apple.screencapture location`, which on a default install is `~/Desktop`. A silent fallback would put the entire Desktop under automatic evaluation, which D-016 forbids. An enabled screenshots source with no path is a configuration error that fails loudly at load time.
- `organize doctor` **reports** what macOS currently says without adopting it. A source path the tool inferred is a source path the tool should not be walking; the owner pastes it into the config deliberately or not at all.
- When the configured folder *is* also the Desktop, Documents, or Downloads (`screenshots_root_is_shared`), eligibility additionally requires the filename to identify the file as a screen capture. Pointing this at the macOS default therefore cannot turn into "evaluate everything on the Desktop".
- The scan is flat, never recursive, and restricted to image types. A folder someone put inside their screenshots folder is theirs.

Detection itself is by filename prefix (`screenshot`, `screen shot`, `cleanshot`, and the common localized spellings) — far more reliable than inferring from pixels. The probe's `is_probably_screenshot` (a PNG with no camera EXIF) is explicitly a heuristic and is treated as a secondary cue only: enough to route a file to review, never enough to justify a retention decision on its own.

Retention for screenshots is deliberately unresolved. Telling a disposable UI state from durable design reference requires reading what is *in* the image, which needs OCR and vision — the worker layer, which does not exist. So screenshots triage to `REVIEW` at confidence 0.5 and are reported rather than guessed at.

## Downloads triage states

Downloads is scanned at the top level only. Directories are left alone entirely: Downloads is full of unpacked app folders and project trees, and walking into them would mean renaming files that something else owns by name. A directory is a unit the owner made.

| Outcome | Meaning | What happens to the file |
|---|---|---|
| `IGNORE` | Not for the durable library | **Stays exactly where it is on the Mac.** Nothing is deleted. |
| `REVIEW` | We cannot tell | Stays where it is; appears in `organize review` |
| `INGEST` | A candidate for Angel | Still only transferred if `triage.auto_ingest` is on **and** confidence ≥ `min_ingest_confidence`; otherwise reported and left alone |

The bias is toward `REVIEW` and it is deliberate: a false `REVIEW` costs the owner a glance at a report, a false `INGEST` puts junk in a permanent library that restic then backs up forever, and a false `IGNORE` costs nothing at all because nothing is deleted either way.

Representative rules, all deterministic and reading only measured facts:

- transient extensions (`.torrent`, `.crdownload`, `.part`, `.download`, `.tmp`) → `IGNORE`;
- installers and disk images → `IGNORE`: re-downloadable, and not a record of anything;
- byte-identical content already in the Angel catalog → `IGNORE`, with the existing `asset_id` named in the reason;
- zero-byte, or under `min_interesting_bytes` (4096) for an image or video → `IGNORE` as a fragment, icon, or spacer;
- captures from a registered removable source (`sd`) → `INGEST` at 0.9: a camera card exists to be emptied into a library. Downloads and screenshots are not that;
- a document with an owner-recognisable filename → `INGEST` at 0.78; a document without one → `REVIEW`;
- archives → `REVIEW`: the contents decide, and they are not unpacked here;
- anything unrecognised → `REVIEW`.

**Age is a settle signal, not a value signal.** A file being old enough in Downloads to be safe to touch says nothing whatsoever about whether it is worth keeping. The two are never conflated.

Duplicates are *observed*, never acted on. `duplicate_observations` distinguishes byte-identical content from a same-name-different-bytes clash from a derived relationship, because those call for different human decisions. `organize duplicates` reports them. There is no destructive duplicate cleanup and none is planned in this phase.

## The automation model

```text
launchd StartCalendarInterval :20
        │
        ▼
scripts/file-intelligence-scheduled          one-shot bash wrapper, explicit PATH
        │
        ▼
organize triage all --apply --automated      runs once, exits. Not a daemon.
```

- **Hourly one-shot, not a resident watcher.** A one-shot invocation is inspectable, cannot leak memory or descriptors, and a missed firing is recovered by the next one rather than needing supervision. There is no FSEvents watcher anywhere in this codebase.
- **Disabled by default.** `scripts/install-file-intelligence-launchd` refuses to render or bootstrap anything unless `automation.enabled` is literally `true`. It checks this by parsing `config/ingest.yaml` as YAML with the repository's own interpreter rather than grepping — a commented-out or unrelated nested `enabled: true` elsewhere in the file is not consent to schedule a process that reads and moves the owner's files. An unparseable config is likewise not consent.
- **The installer always re-renders from the current checkout**, so installing after moving the checkout cannot silently reload a plist pointing at the old location.
- **Runs are serialized by a non-blocking `flock`** in the state directory. A second run — an hourly firing overlapping an owner running `organize` by hand — exits cleanly rather than queuing. Skipping is correct: the next firing picks the work up, and a backlog of blocked scans is worse than a missed one. The kernel releases the lock if the process dies, so a crash cannot wedge the pipeline permanently.
- **Automated runs use `max(settle_seconds, downloads_watcher_min_settle_seconds)`** (`IngestConfig.effective_settle_seconds`). Taking the maximum rather than switching means lowering one value in config can never quietly shorten the window an unattended job waits. Default: 1200 s.
- **Enabling the agent does not enable transfers.** `triage.auto_ingest` is an independent gate, also off by default. Once scheduled, the first thing the agent does is name and report.
- **Desktop and Documents are not sources for it.** `TRIAGE_SOURCE_CLASSES` is `(downloads, screenshots)`. Scheduling this agent does not change D-016.

`organize status` reads the atomically published `status.json` and the ledger's `runs` table; `organize doctor` answers the different question of whether this machine is configured to run safely at all. Both are strictly read-only.

## Every filesystem-mutating code path in the new code

This is the complete list. If you add to it, add to this table.

| Path | What it does | Why it is safe |
|---|---|---|
| `naming/policy.py::safe_rename` | The only rename in the naming pipeline | Takes a *name*, not a path, so it cannot relocate a file. Refuses separators and `.`/`..`. Resolves collisions before acting. |
| `naming/policy.py::_link_rename` | `os.link` + `os.unlink` | Atomic and fails if the target exists. `os.rename` is never used anywhere, because it silently clobbers on POSIX. Worst case of a crash between the two calls is a file reachable under two names — never a file that is gone. |
| `naming/policy.py::_reserve_rename` | Fallback where hardlinks are unavailable (exFAT) | `O_CREAT\|O_EXCL` claims the name atomically first, so the `os.replace` that follows can only overwrite the empty placeholder this call just created. A failed replace removes the zero-byte reservation rather than leaving a stray file. |
| `transaction.py::transfer_file` | The only code that unlinks a source | Unchanged Phase 1A primitive: staging copy → verify SHA-256 at the destination → atomic finalize + catalog commit → unlink. Regression-tested in `tests/ingest/test_mutation_safety.py`. |
| `ledger.py::LocalLedger.__init__` | Creates the state directory and the ledger database | Mode `0700` directory, `0600` database, inside the checkout (`automation.state_relative_path` is validated to be relative and free of `..`). Losing this file costs bookkeeping, never a file. |
| `migrations.py::apply_migrations` | DDL on both databases | Additive and idempotent only; see the migrations section. Never drops, rewrites, or modifies a row. |
| `automation.py::RunPaths.ensure` | Creates the state and log directories | Mode `0700`, `exist_ok=True`, inside the checkout. |
| `automation.py::run_lock` | Writes the pid and timestamp into the lock file | The file's contents are advisory only; the lock is the `flock`. Released by the kernel on death. |
| `automation.py::write_status` | Publishes the last-run summary | `mkstemp` in the same directory + `os.replace`, so a reader can never see a half-written record even if the run is killed mid-write; the temp file is removed on any failure. |
| Catalog writes via `pipeline.py::_attach_provenance` | Records provenance for an asset that was just successfully transferred | Runs only after `transfer_file` reports success with a final path. |
| launchd `StandardOutPath` / `StandardErrorPath` | Appends to `.state/file-intelligence/logs/` | Inside the checkout, under the `0700` state directory; `.state/` is gitignored. |

Everything else in the new code is read-only by construction. In particular `media/probe.py` opens files and never writes to them, never writes beside them, and never asks a library to expand anything onto disk — ingestion runs against a user's only copy of a file, so a metadata read must not be able to damage it. It also never raises: it is called in a loop over a whole volume of unknown, possibly truncated, possibly hostile files, so one bad file degrades to an `errors` entry rather than ending the run. Optional tooling (`ffprobe`, `pypdf`) is detected rather than required, and a decompression-bomb guard refuses to decode an image declaring an implausible canvas.

`organize correct-name PATH` writes a ledger row and nothing else. It does not rename anything; it records a name that already exists on disk, after which the automatic path leaves that file alone permanently.

## What is deliberately NOT built

Do not claim, document, or build on any of the following. They do not exist:

- **Semantic or vision-based naming.** No OCR, no image understanding, no content summarization. The proposer is deterministic and names from facts the file states about itself. Recognising what a screenshot actually shows is the worker layer.
- **Any LLM or model router.** `NameProposer` is a protocol with one deterministic implementation. Nothing calls a model anywhere in `tools/ingest/`.
- **The worker layer.** No jobs, no queue, no coordinator.
- **Organized-view materialization.** `00-inbox/organized/` is a flat `<source_class>/` staging tree holding verified, catalog-committed canonical assets — **not** organized output. The name is retained for the reasons in D-023; the semantics are documented rather than implied. Client/project placement is Phase 3.
- **Destructive duplicate cleanup.** Duplicates are observed and reported. Nothing acts on them.
- **`00-inbox/needs-review/` materialization.** A `REVIEW` file stays exactly where it is on the Mac and appears in a report. It is not moved anywhere.
- **Desktop and Documents automation of any kind.** Manual trigger only, per D-016, and the scheduled agent does not take them as sources.
- **Legacy dump and migration.** That is Phase 1 debt and a separate track; it does not block this one.

## Configuration reference

All defaults live in `tools/ingest/config.py` so the tool works with no config file at all. `config/ingest.yaml` (gitignored) only overrides what differs on this machine; `config/ingest.example.yaml` is the committed template and carries the `REPLACE-WITH-T7-VOLUME-UUID` placeholder. Per D-019, the real volume UUID must never appear in a tracked file.

| Key | Default | Note |
|---|---|---|
| `sources.screenshots.path` | `null` | Never inferred. See the screenshots section. |
| `sources.screenshots.enabled` | `false` | Enabled without a path is a load-time error, not a cue to search. |
| `naming.enabled` | `true` | |
| `naming.source_classes` | `screenshots, downloads, sd` | Desktop and Documents absent by design. |
| `naming.min_confidence` | `0.55` | Below this a proposal is recorded and ignored. |
| `naming.max_filename_length` | `120` | Readability cap; validated to 8–200. |
| `naming.protected_extensions` | `.app .dmg .pkg .mpkg .framework .kext` | Never renamed. |
| `naming.vocabulary` | `[]` | Owner's client/project words; presence makes a name count as meaningful. |
| `triage.enabled` | `true` | |
| `triage.auto_ingest` | **`false`** | Nothing is ever transferred automatically while this is off. |
| `triage.min_ingest_confidence` | `0.85` | Second, independent gate. |
| `triage.min_interesting_bytes` | `4096` | |
| `automation.enabled` | **`false`** | The installer refuses without this. |
| `automation.state_relative_path` | `.state/file-intelligence` | Validated relative and `..`-free. |
| `settle_seconds` | `30` | Manual runs. |
| `downloads_watcher_min_settle_seconds` | `1200` | Automated runs use the **larger** of the two. |

## Commands

```bash
./scripts/organize triage downloads --dry-run   # propose names, classify retention, write nothing
./scripts/organize triage screenshots --apply   # apply renames; moves nothing unless auto_ingest is on
./scripts/organize triage all --apply --automated   # what the LaunchAgent runs
./scripts/organize status                       # what the pipeline knows and is waiting on
./scripts/organize doctor                       # is this machine configured to run safely
./scripts/organize review --limit 40            # files waiting on a human decision
./scripts/organize duplicates                   # observed relationships, never acted on
./scripts/organize correct-name ~/Downloads/whatever.pdf

./scripts/render-file-intelligence-launchd ~/Library/LaunchAgents   # render only; safe to inspect
./scripts/install-file-intelligence-launchd                        # refuses unless automation.enabled
./scripts/uninstall-file-intelligence-launchd
```

The Phase 1A ingestion spellings are unchanged: `organize desktop|documents|downloads|sd|all`, with `--dry-run` (the default) and `--apply`.

## If you are picking this up cold

1. Run `./scripts/organize doctor` first. It tells you what is configured, what is missing, and where macOS is currently putting screenshots.
2. Run `./scripts/organize triage downloads --dry-run` and read the proposals before applying anything. Dry run is the default and writes nothing.
3. Read `naming/__init__.py`, `triage/__init__.py`, `ledger.py`, and `migrations.py` — their module docstrings carry the reasoning, and they are kept current deliberately.
4. `tests/ingest/test_mutation_safety.py` is the file to run and extend before touching anything that writes. `tests/ingest/test_naming.py` covers the policy rules.
5. If you are about to make a proposer more capable, you almost certainly do not need to change anything outside `proposer.py`. If you find yourself wanting to hand it a `Path`, stop: that is D-022, and it needs a new decision entry rather than a widened signature.
