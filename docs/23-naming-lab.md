# 23 — Angel Naming Lab

## Objective

`docs/19-model-evaluation.md` and `docs/20-prompt-management.md` describe
capability-based evaluation and versioned prompts; `docs/22-evaluation-
campaign.md` describes the unattended campaign runner that measures a
*fixed* prompt against a *fixed* suite. None of that machinery is meant for
iterating on a prompt with real documents in front of a human — that is
what the Naming Lab is for.

The Lab is a **local, interactive, development-time tool**. It lets an
operator point Angel at a folder of real sample files, watch what evidence
gets extracted, propose and compare filenames from local models, accept or
reject the results, and save an edited prompt as a new draft version to try
again — all without ever touching the clean Gate-2 validation suite or the
frozen holdout, and without renaming a single real file.

This document does not certify a model, does not change what Gate 2
measures, and does not turn Gate 2 green. `D-054` in `DECISIONS.md` records
why the Lab is architecturally allowed to combine two things production
keeps strictly apart (OCR/vision evidence and naming), and why that is safe.

## Why a *development seed*, not a benchmark item (spec section 13)

Every file reviewed in the Lab becomes a **`development_seed`**
(`tools/naming_lab/seeds.py::SeedRecord`). `SeedRecord.split` is hard-pinned
to that one literal value — it is not possible to construct a `SeedRecord`
with `split="validation"` or `split="frozen_holdout"`; the dataclass raises.

A development seed may be used to improve prompt wording, deterministic
extraction, synonym semantics, filename grammar, model configuration, or
few-shot examples. **It must never be presented as untouched validation
evidence.** The existing frozen holdout — interleaved inline in
`tools/bench/acb_v1/semantic_naming.py` and `routing_classification.py`,
distinguished only by `SuiteTask.split == "frozen_holdout"` — is never
imported into the Lab; `tools/naming_lab/holdout_guard.py` is a structural,
tested guard against that happening by accident (`tests/
test_naming_lab_holdout_guard.py` proves it against the real suites, not
just a fixture). After prompt/contract development stabilizes here, a
**separate, fresh validation corpus** is required before any claim about
production readiness — this Lab's own results are never that corpus.

## Architecture

`tools/naming_lab/` is a new top-level package, deliberately **outside**
`tools/ingest/naming/` and `tools/ingest/triage/`. See `D-054` for why: an
AST guard (`tests/ingest/test_evidence_security.py`) forbids those two
packages from importing `tools.ingest.evidence`, because that combination is
exactly how a model's perception would end up deciding a rename instead of
merely being recorded. The Lab is neither package, never renames a file, and
every result it produces is human-reviewed evaluation data.

Everything reuses production code rather than re-implementing it:

| Concern | Reused from | Lab module |
|---|---|---|
| Filesystem-safety / extension authority / quality gate | `tools.ingest.naming.policy` (`validate_proposal`, `sanitize_stem`, `normalized_extension`) | `grammar.py` |
| OCR / native-text / vision perception | `tools.ingest.evidence.extract.collect_evidence` + its backends | `evidence_bridge.py` |
| EXIF / AV capture metadata, naming quality | `tools.ingest.naming.evidence.gather_evidence` | `evidence_bridge.py` |
| Prompt identity/versioning/rendering | `tools.prompts.registry`, `tools.prompts.renderer` | `prompts_lab.py` |
| Local model calls | `tools.models.ollama_local.OllamaTextProvider` (the one Ollama client in the repo) | `runner.py` |
| Gate-2 candidate list | `tools.campaign.manifest` / `config/campaigns/gate2.yaml` | `app.py` |
| Frozen-holdout contamination guard pattern | `tools.bench.suite.assert_no_holdout_leak` | `holdout_guard.py` |

New modules and what each owns:

* **`dates.py`** — `format_date_prefix`: the one deterministic
  `2025-06-15 -> "25_0615"` helper (spec section 1). `SemanticDateCandidate`
  (value/kind/source/confidence) and `derive_semantic_date`: the precedence
  in spec section 2 (explicit document date > capture metadata > machine-
  generated filename date > nothing). Filesystem `modified_date` is
  structurally not an input to `derive_semantic_date` — there is no
  parameter for it.
* **`document_dates.py`** — regex-based, trusted-code extraction of *which*
  date in a document's text is its semantic date and why (invoice date beats
  due date; a bare year/quarter/month never becomes a fabricated full date).
  This is also what fixes the Gate-2 "supported dates rejected" defect
  (spec section 14) for the Lab's own scoring: a date is checked against
  *this* extraction over real evidence text, never a hand-duplicated
  allowlist.
* **`grammar.py`** — `compose_filename` builds `[YY_MMDD_]<stem><ext>`,
  trusted code always supplies the date prefix (never parsed out of a
  model's text). `check_raw_compliance` (did the model behave: one line, no
  prose, right extension, right grammar shape, no path/prompt leakage) and
  `validate_end_to_end` (after trusted normalization, does
  `policy.validate_proposal` accept it) are two **independent** results —
  see "Raw compliance vs. end-to-end" below.
* **`evidence_bridge.py`** — the one place that calls both
  `tools.ingest.naming.evidence` and `tools.ingest.evidence`, producing
  `LabEvidence` (production `NamingEvidence` + optional `FileEvidence` +
  derived `SemanticDateCandidate`). Backends are injectable (mirroring
  `collect_evidence` itself) and, undriven, come only from
  `tools.ingest.evidence.backends.build_ocr_backend`/`build_vision_backend`
  — `null`, `tesseract` (offline CLI), `heuristic` (no model), or `ollama`
  (loopback-only). No cloud/frontier model is ever reachable from the Lab.
* **`seeds.py`** — the `SeedRecord` schema and its on-disk `SeedStore`,
  keyed by SHA-256 content hash, never by path. A separate, explicitly
  machine-local `path_index.json` remembers the last-seen path per hash so
  the UI can re-open a moved file; it is never treated as part of the
  portable seed record.
* **`holdout_guard.py`** — `forbid_frozen_holdout` / `usable_tasks`: the
  structural guard described above.
* **`prompts_lab.py`** — draft `PromptVersion` storage. A saved draft is
  always `status="draft"`, its `prompt_id` is namespaced
  `naming-lab-draft-<slug>` (never collides with a production id), and
  saving always allocates a new `vnext-<letter>` version — nothing here can
  overwrite an existing version or edit `tools/prompts/library.py`.
  `starter_vnext_draft` seeds a new draft from a production prompt with the
  section-14 "date prompt mismatch" fixed: one clear rule (the model never
  emits a date; trusted code always adds it).
* **`runner.py`** — `run_candidate` renders a prompt against `LabEvidence`,
  calls one local model, and scores the result into `raw_compliance`,
  `deterministic_filename`, `end_to_end`, `semantic`, and
  `hallucination` (heuristic, feeds human review, never a gate) —
  independently, per spec section 4/9.
* **`store.py`** — append-only JSONL results (mirrors the spirit of
  `tools.campaign.store.ResultRecord`'s schema-v2 fields: raw output and
  finish reason both kept) and a full human-decision audit log.
* **`matrix.py`** — per-(file, prompt, model) cell plus corpus-level
  aggregate rates, every one with an explicit numerator/denominator.
* **`preview.py`** — best-effort previews (image thumbnail, PDF first page)
  reusing the evidence package's own rendering; everything else falls back
  to metadata plus a "reveal in Finder" action.
* **`cli.py`** / **`app.py`** — the launcher and the Streamlit UI.

## `YY_MMDD` rendering, and what stays internal (spec section 1)

The canonical vNext filename grammar is:

```
[YY_MMDD_]<descriptive-stem><actual-extension>
```

```
25_0615_northwind-invoice.pdf
25_0408_cobalt-sow.docx
mountain-sunset.jpg          # no date prefix: no trustworthy semantic date
```

The date prefix is present only when a `SemanticDateCandidate` exists — a
partial date (`"2024"`, `"Q1 2025"`, `"March 2025"`) never becomes a
fabricated full date, and an ambiguous document (two equally-plausible
labeled dates the extractor cannot rank) simply omits the prefix rather than
guessing.

**The full ISO date is always what's stored and reasoned about.**
`SeedRecord.semantic_date.value` and `LabEvidence.semantic_date.value` are
both full `YYYY-MM-DD` strings; `YY_MMDD` is purely the filename-rendering
shorthand, computed on demand by `dates.format_date_prefix`.

One non-obvious wrinkle, worth knowing before touching `grammar.py`:
`policy.py`'s `sanitize_stem` only special-cases a leading `YYYY-MM-DD_`
prefix (the shape production's own proposer has always emitted) to survive
separator normalization with its underscore intact. Handed a `YY_MMDD_`
prefix instead, it would fold that prefix's underscore into a hyphen like
any other separator run. Rather than edit `policy.py` (out of scope — see
"Scope boundary" below) or re-implement its safety/quality checks,
`grammar.validate_end_to_end` bridges: it temporarily swaps the vNext prefix
for the exact full-ISO shape `sanitize_stem` already protects, calls
`policy.validate_proposal` unmodified, and swaps back. The swap is exactly
reversible because the `SemanticDateCandidate` passed in is the one true
source the vNext prefix was computed from.

## Raw compliance vs. end-to-end (spec section 4)

These are two different questions, and the Lab keeps them as two different
fields on every `NamingLabResult`, never one collapsed score:

* **`raw_compliance`** — did the *model* behave: single line, no prose, the
  expected extension, the vNext grammar shape, no path/prompt leakage.
* **`end_to_end`** — after `compose_filename` (trusted date prefix, trusted
  extension) and `policy.validate_proposal` (the exact production gate), is
  the *result* filesystem-safe and an improvement.

A model that guesses the wrong extension always fails `raw_compliance`
(visible, never hidden) — but `compose_filename` always uses the real source
extension, so that alone does not fail `end_to_end`. `tests/
test_naming_lab_grammar.py::ExtensionAuthoritySeparationTests` and `tests/
test_naming_lab_runner.py` both prove this directly.

## Scope boundary: what is Lab-only, on purpose

Sections 1–4 and 14 of the originating task describe `YY_MMDD` rendering,
semantic-date provenance, and fixing known Gate-2 evaluator defects. All of
that is implemented **inside `tools/naming_lab/` only**:

* `tools/ingest/naming/policy.py`'s production `YYYY-MM-DD_` grammar is
  **not changed**. It is guarded by ~770 pre-existing tests and is what
  every real rename in this repo goes through today.
* `tools/bench/acb_v1/semantic_naming.py` (and `routing_classification.py`)
  are **not changed**. They interleave frozen-holdout tasks inline with
  dev/validation tasks; editing them safely is a separate, dedicated,
  carefully-reviewed piece of work, not a side effect of building a dev tool.

The known Gate-2 semantic-naming defects (date-prompt mismatch, supported
dates absent from `allowed_dates`, filename-regex-only date detection,
rigid lexical concept sets, extension mismatch conflated with end-to-end
failure) are recorded here as **known and deliberately deferred**, not
silently preserved. The Lab's own `document_dates.py`/`grammar.py` fix the
same class of problem for its own scoring, and are the reference design a
future dedicated pass should graduate into `tools/bench/acb_v1/` and
`tools/ingest/naming/policy.py` once validated.

## Launching the Lab

```
./scripts/angel-eval naming-lab --corpus /path/to/my/naming-seeds
```

`scripts/angel-eval` dispatches `naming-lab` to `python -m
tools.naming_lab.cli` (everything else still goes to
`tools.campaign.cli`, which stays scoped to unattended campaigns).
`tools/naming_lab/cli.py::build_streamlit_command` hard-codes
`--server.address 127.0.0.1` — there is no flag, environment variable, or
code path that binds the Lab anywhere but loopback
(`tests/test_naming_lab_cli.py`). Install its one new dependency first:

```
pip install -r requirements-naming-lab.txt
```

## Where local state lives, and privacy

Everything the Lab writes lives under `.state/naming-lab/<corpus-id>/`,
already covered by the repository's blanket `.state/` `.gitignore` rule —
no separate ignore entry was needed. `<corpus-id>` is derived from the
corpus directory's resolved path (`seeds.corpus_id_for`), never from
content, so the same folder always maps to the same local store.

Per corpus: `seeds/<sha256>.json` (one small JSON record per file, keyed by
content hash — moving or renaming the source file never loses its
annotations), `path_index.json` (machine-local `sha256 -> last-seen path`,
never treated as portable truth), `results.jsonl` (append-only run
results), `decisions.jsonl` (the full human-review audit trail), and
`prompts/<prompt_id>/<version>.json` (draft prompts).

**The Lab never copies a source file's bytes anywhere.** Only its SHA-256,
a bounded extracted-text excerpt (already capped and sanitized by
`tools.ingest.evidence.types.sanitize_extracted_text`), and small
derived JSON records are written, all under the git-ignored `.state/`
tree. No sample document is ever committed, uploaded, or sent to a
cloud/frontier model — the only network-capable backend
(`OllamaVisionBackend`) is hard-rejected by `OllamaLocalClient` unless its
endpoint is loopback.

## Seed schema (summary)

See `tools/naming_lab/seeds.py::SeedRecord` for the authoritative
dataclass. Summary: `schema_version`, `seed_id`, `sha256`,
`original_filename`, `media_kind`, `extension`, `semantic_date` (nullable;
carries `value`/`kind`/`source`/`confidence`), `accepted_filenames` (zero or
more — a seed can have several personally-acceptable answers, unlike the old
exact-one-string benchmark defect this deliberately avoids repeating),
`required_semantics` / `forbidden_semantics` (free-form concept sets a human
can fill in), `human_notes`, `split` (always `"development_seed"`),
`created_at`/`updated_at`. No absolute machine path is ever part of the
record.

## Prompt graduation: draft -> production

A Lab draft (`naming-lab-draft-*`, `status="draft"`) becoming a production
prompt is **always a separate, reviewed code change** to
`tools/prompts/library.py` — nothing in the Lab can write there. Graduating
a draft means: a human reads its exact `role_template`/`task_template`/
`output_schema`, adds a new `PromptVersion` to `library.py` under
`acb-v1-semantic-naming`'s `prompt_id` (or a new one) with `status="active"`,
records its `content_fingerprint` in `LOCKED_FINGERPRINTS`, and only then
does a real evaluation run (a fresh validation corpus, not this Lab's
development seeds) certify it.

## Limitations, stated explicitly

* No speech-to-text or audio/video content-date extraction beyond whatever
  `ffprobe` container metadata (`media/probe.py`'s `capture_timestamp`)
  already exposes.
* `hallucination` flags in `runner.py` are heuristic and best-effort —
  useful signal for a human reviewer, never a pass/fail gate.
* Previews are intentionally modest: images and PDF first pages render;
  everything else shows metadata/evidence plus a "reveal in Finder" action.
* This Lab does not run, and must not be used to justify running,
  `gate2-validation-001` or any claim that Gate 2 is green.
