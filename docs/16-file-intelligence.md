# 16 — File Intelligence: naming, retention triage, and automation

This is the operational half of `tools/ingest/`. `docs/13-dump-first-ingestion.md` describes *moving* files onto Angel; this document describes deciding **what a file is called** and **whether it belongs on Angel at all** — two questions that the code answers separately and on purpose.

Read this before changing anything under `tools/ingest/naming/`, `tools/ingest/triage/`, or `tools/ingest/pipeline.py`. Several things that look like incidental structure are load-bearing safety properties, and the module split is the enforcement mechanism rather than tidiness.

Decisions of record: D-016 (manual-first ingestion), D-019 (the T7 volume-UUID boundary), D-020 (naming vs retention), D-021 (the local ledger), D-022 (a model proposes, trusted code disposes), D-023 (`00-inbox/organized` keeps its name), D-024 (opt-in automation), D-025 (screenshots are configured, never inferred), D-026 (`storage_root_override` needs an explicit flag), D-057 (evidence reaches naming through a plain-data bridge; the semantic proposer is opt-in and policy-gated), D-063 (the V1 adversarial review: the filename sanitizer filters by Unicode category, the byte ceiling is trimmed to rather than refused, and filename quality is Unicode-aware).

`docs/17-file-evidence.md` is the layer underneath this one: what a file's bytes actually contain (native text, OCR, a vision description, in time a speech transcript). Since **D-057** exactly one decision reads it — the **filename proposal**, and nothing else. Retention triage and ingestion stay evidence-blind, the AST guard forbidding any module under `naming/` or `triage/` from importing `evidence/` is unchanged and unrelaxed, and the crossing happens in one reviewed module outside both packages (`tools/ingest/perception_bridge.py`).

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

- `NamingEvidence` (`naming/evidence.py`) is a frozen dataclass containing strings, numbers, a dict of measured facts, one optional PNG byte-string, and (since D-057) one optional `PerceivedEvidence` of bounded strings and tuples. It carries **no `Path`, no file object, and no callable.** A proposer — including the model-backed one that now exists — therefore has nothing to mutate the filesystem *with*. It is not trusted and then constrained; it is simply not given the means.
- The thumbnail is `bytes`, not a path (`media/probe.py::thumbnail_png` writes nothing to disk). A vision model gets the pixels it is meant to look at with no handle to the user's original and no temp file to leak.
- Evidence is gathered by trusted code *before* any proposer is called, deterministically and read-only.
- What comes back is validated before it can become a filename (`naming/policy.py::validate_proposal`), and the rename itself is performed by `naming/policy.py::safe_rename`, which takes a **name, not a path**, so a caller cannot use it to relocate a file into another directory. Renaming and moving stay separate powers.
- `transaction.py::transfer_file` remains the only code in the system that unlinks a source file.

### What the proposer is handed, since D-057

`NamingEvidence` gained one field: `perceived: PerceivedEvidence | None`. That is how a document's own text, OCR off a screenshot's pixels, a vision backend's description and (once the transcript stage lands) speech reach a proposer.

`PerceivedEvidence` is the same kind of thing `NamingEvidence` is, and deliberately so:

- **bounded strings and tuples only** — no `Path`, no callable, no nested perception type. The perception layer's own values are far richer (polygons, per-block confidences, provenance, page regions); none of that crosses. Handing a proposer the geometry would hand it a second, unvalidated way to reason about a file, and every extra field is one more thing a future consumer could mistake for authority.
- **every excerpt capped at 1500 characters** and re-sanitized at the boundary (control, C1, bidi-override and zero-width characters stripped), so a value built from a cache row or a fixture is safe by construction rather than by the provenance of its input.
- `sources` says which kinds were **actually present** rather than which stages were attempted, because "OCR ran and found nothing" and "OCR never ran" are different facts and only one means the pixels carry no text.

The conversion lives in **`tools/ingest/perception_bridge.py`**, outside both `naming/` and `triage/`, exactly as `tools/naming_lab/` lives outside them (D-054). That is what keeps the AST guard intact and the crossing reviewable: one module, one import pair, zero filesystem mutation, and a test asserting it is still the only module in `tools/ingest/` importing both sides.

Perception runs **before** naming in `pipeline.py` and is keyed by content hash, so a rename reuses the work on the next run. It cannot make naming fail: an unavailable backend, a disabled evidence layer, or a stage that raises all yield `perceived=None`, and the proposer names from measured facts alone.

### The two proposers

`naming.proposer` chooses. `naming.enabled: false` still wins over both, so turning a model on is two deliberate settings rather than one.

**`DeterministicProposer`** (the default) names from facts the file states about itself — a PDF's own title, an image's EXIF description, a zip's contents, a capture date, pixel dimensions — and now also from evidence it can treat as a *quotation*:

| builder | confidence | evidence |
|---|---|---|
| embedded title metadata | 0.92 | `metadata_title` / `container_title` |
| embedded EXIF/document description | 0.85 | `image_description`, `exif_user_comment` |
| archive contents | 0.78 | the zip's central directory |
| **a document's first heading, or the first meaningful line of its own text** | **0.70** | perceived native text / structure |
| PDF author + page count | 0.72 | metadata |
| **the title-shaped first line of OCR on a known screen capture** | **0.62** | perceived OCR |
| camera make/model | 0.62 | EXIF |
| generic factual fallback | 0.58 | file type and dimensions |

Builder order is "most informative first", which is **not** the same as descending confidence — a document's first heading (0.70) is tried before its author plus page count (0.72), because the heading says what the file is *about* where the author says only who made it. Confidence answers "how sure am I"; order answers "which answer is worth having".

It reads **neither the transcript nor a visual description**. Both are summaries something else wrote *about* the file rather than text the file contains, and `HeuristicVisionBackend` is explicitly a guess from colour distribution and flatness (`docs/17`). Naming a video `hawk-on-a-cedar-fence.mp4` from that would assert certainty the evidence does not carry — the same reasoning that keeps `is_probably_screenshot` out of the generic fallback. OCR is likewise trusted only for a file the *filename or configured folder* already proves is a screen capture, never for a photograph that happens to contain a shop sign.

`2026-09-12_web-browser-product-page.png` is still deliberately preferred over a confidently wrong client name: where perception says nothing usable, the generic factual fallback wins over invention.

**`SemanticProposer`** (`naming/semantic.py`, opt-in) asks a **loopback-only** local model, and may read everything — transcript and visual description included — because everything it produces is a proposal a deterministic policy still has to accept. It is the first production caller of `tools/prompts/production.py::render_for_task_class`, so the prompt it sends is byte-identical to the one the Gate-2 `naming.semantic` suite evaluates (D-041's whole point). The path a raw model string travels:

1. the bounded evidence text is rendered into the active versioned prompt; the only client constructible here is `OllamaTextProvider`, whose `OllamaLocalClient.__init__` refuses any endpoint that is not loopback HTTP — a remote endpoint in config becomes a recorded fallback, not a request, so no document text can leave the machine even if the config says to send it;
2. the answer is **unwrapped** generously (markdown fences, quotes, a `Filename:` label, a trailing explanation, first line only) and then **judged strictly**: anything carrying a path separator or separator-lookalike, `..`, a leading dot, a control/bidi/zero-width character, a changed extension, or a name over the length cap is *refused* rather than repaired. A name that had to be fixed is a name the model did not produce;
3. whatever survives goes through `policy.validate_proposal` in `rename.py` exactly as a deterministic proposal does. Step 2 is defense in depth in front of that, never instead of it;
4. **confidence is derived, never self-reported.** The model outputs only a filename; a score it invented would be worth less than nothing, since a confidently wrong name is the failure mode this system exists to avoid. Base 0.70, minus 0.15 for an all-generic stem, minus 0.10 when no stem word appears anywhere in the evidence, plus 0.10 when two or more do, plus 0.05 for an owner-vocabulary word, clamped to 0.05–0.90. The ceiling sits below `CONFIDENCE_EMBEDDED_TITLE` (0.92) on purpose: a document stating its own title is better evidence than a model's reading of one;
5. **any** failure — connection refused, timeout, `ValueError`, empty output, JSON, prose, a refused string, a derived confidence under `naming.semantic.min_confidence` — falls back to the deterministic proposer (or declines, if `fallback_to_deterministic` is off), with the reason recorded and the raw output kept.

Swapping proposers changes nothing downstream. No safety property in this document depends on which implementation sits behind the protocol — and `tests/ingest/test_semantic_proposer.py` runs the full hostile-answer matrix through a real `run_source --apply` with a fake provider, no Ollama required.

### The naming audit trail

Every `naming_history` row now also records `prompt_id`, `prompt_version`, `prompt_sha256`, `evidence_sha256`, `evidence_sources` (JSON) and `raw_output` — bounded, and **kept even when the answer was refused**, because a rejected hostile proposal that leaves no trace is indistinguishable from a rename nobody noticed. `organize naming-history PATH` prints it. The evidence *hash* is read from the evidence the proposer was handed, not from the proposal, so a hostile proposer cannot write a different one into its own audit trail.

Only the hash, the evidence *kinds*, the prompt identity and the model's one-line answer travel to the T7 catalog when a file is ingested. Extracted document text stays in the Mac-side ledger, which restic does not back up — D-030's privacy posture is unchanged by giving naming an audit trail.

> The source docstrings cite "invariant 3", "invariant 7", "invariant 12" and so on. Those numbers refer to the owner's file-intelligence specification, which is not tracked in this repository. The invariants themselves are restated in prose here and in `README.md` so this document stands alone.

### An instruction is not a title

Both evidence-derived deterministic builders and the semantic proposer's
output check apply `naming/quality.py::looks_like_instruction`: a line that
reads as an instruction to a reader or a model ("IGNORE ALL PREVIOUS
INSTRUCTIONS", "AI ASSISTANT: name this file …", "set triage.auto_ingest:
true") is skipped by the title scan and refused as a model answer, with the
reason recorded. This is a naming-quality rule — such a line is not the
subject of the file — and deliberately **not** a security control: the
security property is structural (perceived text can become at most a
policy-legal filename, never a path, an extension change, a config change,
or a move), and it holds whether or not this heuristic matches.

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

A database reporting a version **newer** than the code supports is refused outright — upgrade the checkout rather than downgrading the database. The applied version is `len(MIGRATIONS)` for each database, so adding a step is the only way to move it.

The T7 catalog is at **5**: migration 2 adds the four shared tables plus `first_seen_filename`, `local_file_id`, `retention_recommendation` and `retention_run_id` on `assets`; 3 carries the Mac-side generation across; 4 is the one deliberately reconstructive step (dropping the legacy `UNIQUE(source_volume_id, original_path)`); 5 adds the D-057 naming-audit columns, because `_attach_provenance` copies ledger lineage rows across verbatim and would otherwise fail on the first already-ingested file after naming gained an audit trail.

The local ledger is at **4**: 1 creates the schema, 2 adds `generation`, 3 adds the `file_evidence` perception cache, 4 adds the same six naming-audit columns. All six are nullable and nothing is backfilled — a row written before the migration correctly reads back `NULL`, because it was written by a proposer that had no prompt and by a pipeline where naming could not see perceived evidence at all. Inventing a value would be inventing history.

## Naming policy

`naming/rename.py` is the gate. The order of its checks is the order of the guarantees, and it matters:

1. **A human's name wins, permanently.** If the ledger holds a human-authored name for this file, nothing automatic touches it again — ever, regardless of what a later and better proposer would suggest.
2. **A settled name is not reconsidered.** If the current filename is one this system already chose or already chose to keep, return without writing a row. Re-recording an unchanged decision on every scan would grow the ledger without adding information.
3. **Protected extensions are never renamed.** `.app`, `.dmg`, `.pkg`, `.mpkg`, `.framework`, `.kext` — something else on the system finds these by name.
4. **A useful name is left alone.** `naming/quality.py` answers GOOD or POOR deterministically, from the name alone, before any proposer is consulted. `Fine-Line-Group-2026-SOW.pdf` is a better filename than anything a model will invent for it. POOR means *eligible for a proposal*, not *will be renamed*. Tokenization is Unicode-aware (`[\W_]+`), so this holds for a name in any script: before D-063 it split on `[^A-Za-z0-9]+`, which treated every non-ASCII letter as a separator — `отчет-о-финансовых-результатах.pdf` yielded zero tokens and therefore scored POOR, making an owner-authored name eligible for renaming, while the quality re-check in step 7 simultaneously refused every replacement in the same script. A token of two characters counts as a word when it is East Asian wide or fullwidth, because 報告 is a word in two characters where `ab` is not.
5. **A question already answered by this proposer version is not re-asked.** A proposer *upgrade* deliberately reopens it.
6. **Then, and only then, ask for a proposal**, and require it to clear `naming.min_confidence` (default 0.55).
7. **Validate.** `validate_proposal` rejects anything path-shaped, strips whatever extension the proposal carried and re-attaches the real one from the file on disk, sanitizes the stem, and then **re-runs the quality check on its own output**: a proposal that would still score POOR is refused, because applying it would make the file eligible again next run — a rename loop.
8. **Dry run stops here.** Otherwise perform the single mutation.

Every branch, including every decision *not* to rename, is appended to `naming_history`. That is what makes repeat runs cheap and lets a person see why a file is called what it is called.

### Filename legality

`sanitize_stem` removes every character in Unicode category `Cc`, `Cf`, `Cs` or `Co` — all C0 *and* C1 control characters, every format character (zero-width, bidi override **and** the bidi marks), every lone surrogate, and every private-use character — replaces the union of characters forbidden on APFS/HFS+/exFAT/POSIX, collapses separator runs, and strips leading dots and trailing dots and spaces. A name the filesystem would silently alter, or that a terminal would interpret, or that renders as whatever the reader's font decides, is a name the ledger would be lying about.

The filter is by **category**, not by a list of characters, and D-063 is why: the list version named nine format characters and missed U+200E, U+200F, U+202C, U+00AD and U+2060-U+2064, missed the whole C1 block (U+009B is the byte that starts an 8-bit ANSI escape sequence, and `organize` prints filenames to a terminal), and missed surrogates — which cannot be UTF-8-encoded at all, so one reaching the length check raised `UnicodeEncodeError` past `rename.py`'s handler and produced a rejection with no `naming_history` row. A hand-enumerated attack class is a list of the members somebody thought of.

A single underscore is preserved after a leading ISO date (`2026-09-12_kitchen-layout`) and nowhere else. The configured `max_filename_length` (default 120) is a readability cap counted in **characters**; `MAX_COMPONENT_BYTES = 255` is the hard APFS ceiling underneath it, counted in **bytes**, and `sanitize_stem` trims to it on a character boundary rather than refusing the name. Those two units diverge 3x for CJK and 4x for emoji, so before D-063 a perfectly ordinary Japanese document title cleared the character cap, blew the byte ceiling, and was refused outright.

### Collision handling

`collision_free_name` derives its first fallback from content (`-<sha8>`) rather than a counter, so re-running over the same file proposes the same name instead of climbing `-2`, `-3`, `-4`. The numeric tail after that exists only for two genuinely different files sharing both a hash prefix and a stem.

## Screenshot handling

Screenshots are a first-class source for **naming** and are explicitly not a vacuum. `sources.screenshots` has:

- **no default path, and none is ever inferred.** macOS keeps the real location in `com.apple.screencapture location`, which on a default install is `~/Desktop`. A silent fallback would put the entire Desktop under automatic evaluation, which D-016 forbids. An enabled screenshots source with no path is a configuration error that fails loudly at load time.
- `organize doctor` **reports** what macOS currently says without adopting it. A source path the tool inferred is a source path the tool should not be walking; the owner pastes it into the config deliberately or not at all.
- When the configured folder *is* also the Desktop, Documents, or Downloads (`screenshots_root_is_shared`), eligibility additionally requires the filename to identify the file as a screen capture. Pointing this at the macOS default therefore cannot turn into "evaluate everything on the Desktop".
- The scan is flat, never recursive, and restricted to image types. A folder someone put inside their screenshots folder is theirs.

Detection itself is by filename prefix (`screenshot`, `screen shot`, `cleanshot`, and the common localized spellings) — far more reliable than inferring from pixels. The probe's `is_probably_screenshot` (a PNG with no camera EXIF) is explicitly a heuristic and is treated as a secondary cue only: enough to route a file to review, never enough to justify a retention decision on its own.

Retention for screenshots is still deliberately unresolved, and D-057 did not change it. Telling a disposable UI state from durable design reference requires reading what is *in* the image — and perception is now allowed to influence the **name** (a screenshot can finally be called `2026-09-12_screen-capture-angel-review-queue.png` instead of `screen-capture-1440x900.png`) while retention still triages to `REVIEW` at confidence 0.5 and is reported rather than guessed at.

That asymmetry is the point rather than an inconsistency. A wrong name costs a confusing filename and is corrected by `organize correct-name`; a wrong retention decision is a step toward a file nobody meant to keep or lose. The two questions have different failure sizes, so they get authority on different schedules.

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

The evidence layer (`tools/ingest/evidence/`, `docs/17-file-evidence.md`) adds **no rows to
this table**: it performs zero filesystem writes of any kind, and an AST guard over the whole
package enforces that with no justification entries permitted. Rasterized pages and crops are
in-memory bytes; the only persistence is ledger rows.

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
| `provenance.py::attach_local_provenance` | Copies a file's pre-ingestion naming lineage onto its Angel asset, then regenerates the sidecar | Catalog and sidecar writes only; touches no user file. Runs only after `transfer_file` reports success. The sidecar rewrite is required because the transfer primitive wrote it before the provenance existed. A failure here is caught and reported, and never undoes a verified transfer. |
| `enrichment.py::enrich_asset` | Records measured media facts for a committed asset, then regenerates the sidecar | Probes the *destination* copy on Angel, not the source — which is already gone. Catalog and sidecar writes only. A probe that raises, a catalog write that fails, and a file type with nothing to say all return `False`; none can damage the transfer. |
| `cli.py::cmd_decide` (`organize keep` / `ignore`) | Appends the owner's own retention decision to the ledger | Writes one `classification_runs` row and updates the local file's current state. Moves nothing, renames nothing, deletes nothing. `keep` only makes the file eligible for the same verified transfer path on a later run. |
| launchd `StandardOutPath` / `StandardErrorPath` | Appends to `.state/file-intelligence/logs/` | Inside the checkout, under the `0700` state directory; `.state/` is gitignored. |

Everything else in the new code is read-only by construction. In particular `media/probe.py` opens files and never writes to them, never writes beside them, and never asks a library to expand anything onto disk — ingestion runs against a user's only copy of a file, so a metadata read must not be able to damage it. It also never raises: it is called in a loop over a whole volume of unknown, possibly truncated, possibly hostile files, so one bad file degrades to an `errors` entry rather than ending the run. Optional tooling (`ffprobe`, `pypdf`) is detected rather than required, and a decompression-bomb guard refuses to decode an image declaring an implausible canvas.

`organize correct-name PATH` writes a ledger row and nothing else. It does not rename anything; it records a name that already exists on disk, after which the automatic path leaves that file alone permanently. `organize naming-history PATH` and `organize evidence PATH` write nothing at all.

## What is deliberately NOT built

Do not claim, document, or build on any of the following. They do not exist:

- **Perceived evidence reaching retention, ingestion, or automation.** D-057 gave it the *filename* and nothing else. `triage/policy.py` reads none of it, may not import the evidence package, and is handed a `NamingEvidence` with `perceived` stripped off — so a future change that wants retention to read OCR text has to widen that call visibly instead of finding the field already there. Two files differing only in what was perceived about them get the identical retention decision, and that is a test.
- **A remote or frontier model in the naming path.** The only client `naming/semantic.py` can construct is the loopback-only Ollama adapter. A non-loopback endpoint in config is refused at construction and recorded as a fallback.
- **Instruction filtering.** Nothing pattern-matches extracted text for hostile phrasing, and no docstring claims otherwise. The defense is structural: a model cannot produce a *path* or a *different file type*, not that it cannot produce a silly name.
- **Model-chosen dates, paths, or destinations.** The extension comes from the file; `safe_rename` takes a name, not a path; client/project placement is Phase 3.
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
| `naming.proposer` | `deterministic` | `deterministic` \| `semantic`. An unknown value is a load-time error, not a silent fallback. `naming.enabled: false` still wins. |
| `naming.semantic.model` | `qwen3.5:4b` | An Ollama tag that must already be installed. Nothing here pulls a model. |
| `naming.semantic.endpoint` | `http://127.0.0.1:11434` | Loopback HTTP only, enforced by `OllamaLocalClient` itself. Anything else becomes a recorded fallback, never a request. |
| `naming.semantic.timeout_seconds` | `60` | Validated 1–600. |
| `naming.semantic.num_predict` | `64` | Validated 1–2048. A filename is short. |
| `naming.semantic.min_confidence` | `0.6` | Against the *derived* confidence; the model never scores itself. |
| `naming.semantic.fallback_to_deterministic` | `true` | `false` means "a model names it or nothing does". |
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
./scripts/organize naming-history ~/Downloads/whatever.pdf   # why is it called that? read-only
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
5. If you are about to make a proposer more capable, you almost certainly do not need to change anything outside `proposer.py` or `semantic.py`. If you find yourself wanting to hand it a `Path`, stop: that is D-022, and it needs a new decision entry rather than a widened signature. If you find yourself wanting to `import tools.ingest.evidence` from inside `naming/`, stop too: add to `PerceivedEvidence` and `perception_bridge.py` instead — the AST guard in `tests/ingest/test_evidence_security.py` exists to make that crossing deliberate, and it has no exception list.
6. `./scripts/organize naming-history PATH` is the first thing to run when a filename looks wrong. It shows what the proposer saw, what it was asked, what it answered, and what policy did with the answer.
