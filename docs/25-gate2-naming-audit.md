# 25 — Gate-2 evaluator audit: the whole chain, and what was wrong with it

Status: written 2026-09-18 (D-059). Audits the two Gate-2 ACB-v1 suites
(`acb-v1/routing-classification`, `acb-v1/semantic-naming`), the prompts they
are measured with, and the one real campaign that has been run against them
(`gate2-validation-001`, 2026-09-15, 660 records).

This document exists because `gate2-validation-001` produced numbers
(routing best 0.684 against a 0.80 threshold; naming best 0.214 against 0.75;
`gpt-oss:20b` 95–100 % truncated) and a number is not a finding. Before
concluding anything about the models, every link in the chain that produced
those numbers had to be read and checked. Several links were defective.

**Contamination notice.** Development and validation item content is shown
below, because auditing an already-run validation campaign requires reading
what it measured. Frozen-holdout content is never printed here, and the
machine-checked guard that keeps holdout text out of prompt material
(`tools.bench.suite.assert_no_holdout_leak`) is run over every shipped prompt
version, before inference, by
`tools.campaign.runner.check_prompt_contamination`. Where an audit finding led
to a change in a *validation* item's expectations, that is stated explicitly —
those items have been consumed for development to that extent, and the
frozen holdout remains the only untouched split.

## 1. The chain, link by link

What actually happens to one benchmark item, in the order it happens, with the
module that owns each step:

| # | Link | Code | What it is |
|---|---|---|---|
| 1 | `SuiteTask` | `tools/bench/suite.py` | id, suite, suite version, capability, split, scenario, and two bounded dicts: `input` (what the model is shown) and `expected` (deterministic properties the answer must have). Never a path, handle or callable. |
| 2 | Suite definition | `tools/bench/acb_v1/*.py` | The item inventory, built at import time. `SuiteDefinition.__post_init__` refuses duplicate ids, a task declaring a different suite, or any split with zero tasks. |
| 3 | Manifest | `config/campaigns/*.yaml` → `tools/campaign/manifest.py` | Which suites, which splits, which candidates, how many samples, what thresholds. Each candidate pins an exact `prompt_id` + `prompt_version` per suite; an unpinned prompt is refused, not defaulted. |
| 4 | Work-unit enumeration | `tools/campaign/execution.py::enumerate_work_units` | One work unit per `campaign + configuration fingerprint + suite + item + sample index`. The configuration fingerprint (`tools/campaign/fingerprint.py`) covers model, digest, quantization, runtime, sampling, reasoning, tool-schema version, suite + suite version, prompt id + version. |
| 5 | Prompt registry | `tools/prompts/registry.py`, `library.py` | `PromptVersion` is immutable: a recorded content fingerprint per `(prompt_id, version)` is recomputed by test on every run, so changing what a version means without moving its version string fails. |
| 6 | Input adapter | `tools/campaign/suites.py::prompt_input` | The one place a suite's task-specific `input` shape becomes the generic `(task_text, extra)` pair the renderer takes. Routing hands over `description`; naming hands over `evidence_text` plus `original_filename`. |
| 7 | Renderer | `tools/prompts/renderer.py` | policy → role → task/output contract → (optional) untrusted context pack, concatenated, SHA-256 recorded. Pure function: same inputs, byte-identical text. |
| 8 | Runtime adapter | `tools/prompts/adapters.py`, `tools/models/ollama_local.py` | Loopback-only Ollama `/api/generate`. Sampling (`temperature`, `seed`, `num_ctx`, `num_predict`, `think`) is a runtime decision, never prompt content. |
| 9 | Raw output | `OllamaTextProvider.generate` | Returns `response` as `output` and, for a thinking model, `thinking` **separately** — plus `done_reason`, token counts and timings. Only `output` is ever scored. |
| 10 | Normalization | the suite's own `verify` | Routing: strip + lower-case, and (1.1.0) a recorded, non-scoring unwrap so decoration is distinguishable from a wrong answer. Naming: the proposal's own extension is read *before* production policy would silently re-attach the right one. |
| 11 | Policy | `tools/ingest/naming/policy.py::validate_proposal` | The same function production naming uses: filesystem safety, traversal, reserved device stems, length, and the quality gate `quality.assess`. Not reimplemented for the benchmark. |
| 12 | Oracle | `tools/bench/acb_v1/*::verify` | Deterministic property checks. No LLM-as-judge anywhere. Independently re-implemented in `tools/bench/cross_validation.py` (D-043) and proven to agree. |
| 13 | Status | `tools/campaign/execution.py::execute_work_unit` | 11 statuses, separating verified outcomes (`verified_pass`/`verified_fail`/`truncated`/`invalid_output`/`unverified`) from infrastructure failure (retried on resume, never charged to the model). |
| 14 | Record | `tools/campaign/store.py` | Append-only JSONL; a line is never rewritten. Schema v3 carries raw output, finish reason, `thinking_chars` and the `num_predict` the attempt ran under. |
| 15 | Report | `tools/campaign/report.py` | Regenerated from the store alone: Wilson intervals, per-item consistency, latency, reliability, threshold check. No point estimate is ever printed without its interval. |

Two properties of this chain are worth stating because they are what make the
rest of this document possible. The configuration fingerprint means a changed
suite version or prompt version produces *different work-unit ids*, so fixed
suites cannot silently contaminate old results — and old results cannot be
reinterpreted as evidence about the new ones. And the append-only store means
this audit could be written from the 660 records of `gate2-validation-001`
months later, without re-running anything.

## 2. What the audit checked, and what it found

`tools/bench/gate2_audit.py` (schema v2) runs every check below
deterministically against the live suite and prompt objects, so each is a
standing regression guard rather than a one-time reading.
`./scripts/angel-eval audit --strict` exits non-zero on any error-severity
finding. `tools/bench/gate2_failure_analysis.py` is the companion that reads
recorded campaign rows without touching a model.

| Audit dimension | Check(s) | Verdict on 1.0 |
|---|---|---|
| Answer leakage | `prompt_contains_expected_answer` | Clean. No allowed date and no scored entity name appears in any prompt template. (Prompt scaffolding words like "output contract" are excluded by name, not by luck.) |
| Citation / path leakage | `leakage_check_not_grounded`; oracle's `forbidden_substrings` | Clean and grounded: every leakage prohibition is a string actually present in the item's own evidence, so the check can fire. |
| Few-shot contamination | `few_shot_contamination`, `examples_without_example_set_version` | 1.0 carried no examples at all. The frozen 1.1.0 naming prompt does (three of them, via arm 1.1.0-d), declares `example_set_version`, and shares no entity, date or wording with any suite item in any split. |
| Frozen-holdout leakage | `frozen_holdout_guard_clean` (runs the real guard) | Clean for every shipped prompt version; now also enforced before inference by the runner, not only by tests. |
| Prompt / verifier mismatch | `prompt_date_requirement_mismatch`, `routing_taxonomy_undefined_in_prompt` | **Two real defects.** See §3.1 and §3.6. |
| Semantic false negatives | `semantic_false_negative` (live probe: an answer assembled only from the item's own expectations must pass) | **Real defect in 1.0** — five validation items rejected the very date their evidence supplied. Clean in 1.1.0. |
| Semantic false positives | `semantic_false_positive` (live probe: the unchanged original name, and a contentless `document.ext`, must both fail) | Clean in both. |
| Date rules | `required_date_without_allowed_date`, `evidence_date_not_allowed`, `allowed_date_not_visible_to_model`, `required_date_not_visible_to_model` | **Real defect in 1.0.** See §3.1–§3.3. |
| Entity hallucination | `forbidden_token_contradicts_required_concept`, `forbidden_token_overlaps_required_concept` | **One latent defect** (`system` forbidden while *Cobalt Systems* required). See §3.5. |
| Extension handling | `extension_not_enforced`, `extension_conflated_with_semantics` | **Real defect in 1.0.** See §3.4. |
| Uncertainty handling | `no_uncertainty_guidance_in_prompt` | **Real gap in 1.0:** three items present deliberately unidentifiable evidence and the prompt said nothing about what to do with it. Addressed by prompt arm 1.1.0-c and by the ambiguous-asset rule change in suite 1.1.0. |
| Synonyms | `single_surface_concept`, `concept_set_not_evidence_grounded` | **Real defect in 1.0:** matching was single-token exact equality, so a "synonym set" was exact-string matching with extra steps. See §3.4. Every concept set is now proven evidence-grounded. |
| Unrealistic evidence | `concept_set_not_evidence_grounded` + the false-negative probe | One scenario was genuinely unmeasurable as written (`ambiguous_asset`); see §3.7. Everything else is satisfiable from the evidence shown. |
| Scenario imbalance | `scenario_imbalance`, `label_frequency_skew`, `label_never_correct` | Reported, not fixed, and deliberately so. See §5. |

## 3. The defects, with evidence

Each of the first five is one of the five items D-054 recorded as "known and
deliberately deferred"; all five are fixed in suite version **1.1.0**, and the
change log lives in `tools/bench/acb_v1/semantic_naming.py`'s module docstring
next to the code it describes.

### 3.1 The prompt called the date optional; the oracle required it

Prompt `acb-v1-semantic-naming@1.0.0`'s output contract:

> An **optional** leading ISO-8601 date (YYYY-MM-DD) is followed by a single
> underscore, then a short descriptive stem, then the extension.

Meanwhile 14 of the suite's 42 items set `require_date: True`. A model that
read the published contract, judged the date optional and omitted it was
deterministically failed with `missing_required_date`. This is not a model
weakness in any sense; it is two documents disagreeing.

Fixed on both sides: prompt version 1.1.0 states the rule the oracle enforces
("include the date when — and only when — the evidence states one for this
file"), and the oracle derives the requirement instead of carrying it by hand
(§3.2). The audit check stays live, so pairing a 1.0.0 prompt with the current
suite is still reported.

### 3.2 Evidence-supported dates were missing from `allowed_dates`

The largest single failure category in the whole campaign. Five of the fourteen
validation naming items stated a date in the evidence the model was shown and
then had no `allowed_dates` entry for it, so the oracle classified the model's
correct use of that date as `unsupported_or_hallucinated_date`:

| Item | Evidence shown to the model (abridged) | `allowed_dates` in 1.0 | What `qwen3.5:4b` proposed | 1.0 verdict |
|---|---|---|---|---|
| `sn-clientdoc-val-01` | "A statement of work for client Cobalt Systems, **dated 2025-04-08**." | *(empty)* | `2025-04-08_cobalt-systems-statement-of-work.docx` | fail — hallucinated date |
| `sn-recording-val-01` | "…a site walkthrough discussing deck repairs, **recorded 2025-05-02**." | *(empty)* | `2025-05-02_deck-repairs-walkthrough.m4a` | fail — hallucinated date |
| `sn-photograph-val-01` | "A photograph of a mountain landscape at sunset, **taken 2025-06-11**." | *(empty)* | `2025-06-11_mountain-landscape-sunset.jpg` | fail — hallucinated date |
| `sn-distract-val-01` | "Meeting notes about the Q2 marketing plan for client Cobalt Systems **dated 2025-04-01**…" | *(empty)* | `2025-04-01_q2-marketing-plan-cobalt-systems-notes.docx` | fail — hallucinated date |
| `sn-pathleak-val-01` | "Meeting notes for Cobalt Systems **dated 2025-04-08**, currently stored at /Volumes/Angel/…" | *(empty)* | `2025-04-08_cobalt-systems-meeting-notes.docx` | fail — hallucinated date |

All five of those proposals are correct filenames. 25 of `qwen3.5:4b`'s 70
naming attempts and 35 of `qwen3.5:9b`'s were rejected this way. The dev split
had the same defect in the same scenarios.

The fix is not to add five dates to five lists; it is to make the drift
impossible. `allowed_dates` is now **derived** from the ISO dates present in
the item's own `evidence_text`, unioned with an explicitly declared
`extra_allowed_dates` for a capture date that appears only in the *original
filename* (a screenshot's `Screenshot 2025-04-02 at 9.14.11 AM.png`).
`require_date` is derived the same way: required exactly when the evidence
supports a date. A test asserts both properties hold for every item in every
split, so a future hand-edit cannot reintroduce the defect.

### 3.3 Date detection was a filename-prefix regex

1.0 matched `^(\d{4}-\d{2}-\d{2})_` against the validated filename. Two
consequences, in opposite directions:

* `invoice_acme_2025-01-01.pdf` was treated as having **no** date, so a correct
  name failed `missing_required_date`;
* and an *unsupported* date escaped the hallucination check entirely by sitting
  one character to the right of the prefix — an anti-hallucination check that
  can be evaded by formatting is not a check.

1.1.0 scans the whole stem for `YYYY-MM-DD`, `YYYY_MM_DD` and compact
`YYYYMMDD`, validates each as a real calendar date (so `2025-13-45` is not a
date, and `2024-01` is not either), and requires every date found anywhere to
be evidence-supported.

### 3.4 Concept matching was single-token exact, and extension failure hid everything else

**Synonyms.** 1.0's `required_concept_sets` were tuples described as synonym
sets, matched by `word.lower() in tokens` — exact token equality. So
`sn-screenshot-val-01`, whose evidence reads *"an error dialog reading
'connection timed out'"* and whose concept set was `("error", "timeout")`,
failed `qwen3.5:4b`'s `2025-01-19_connection-timed-out.png` five times out of
five: the proposal's token is `timed`, and `timed != timeout`.

1.1.0 matches a concept by exact token, by conservative stem
(`expenses`/`expense`), by a seven-character shared word-prefix
(`install`/`installation`, `renovate`/`renovation` — six is refused because
`contract` and `contradiction` share exactly six), or by a multi-word synonym
appearing in the separator-stripped stem (`homedepot` for `home-depot`).
Forbidden tokens deliberately keep **exact** matching: widening a prohibition
would fail correct names. Concept sets were also widened to include the surface
forms the evidence itself uses — `timed` for that screenshot item — and an
audit check now proves every concept set has at least one synonym visible in
the evidence, so an unmeetable expectation cannot ship.

**Extensions.** 1.0 returned on the first extension mismatch, before any other
check ran. Two bad consequences, both visible in the recorded data:

* all 70 of `gpt-oss:20b`'s naming attempts are recorded as
  `failure: wrong_extension`. Its answer channel was *empty* (§4); the
  "extension" it got wrong was the empty string. The recorded reason is
  actively misleading about what happened.
* a model that produced a perfect name but guessed `.pdf` for a `.docx` was
  indistinguishable in the records from one that understood nothing.

1.1.0 runs every check and reports `checks`, `failures`, `raw_compliance` and
`semantic_pass` — the same raw-compliance-versus-end-to-end separation the
Naming Lab already uses (D-054, `docs/23`). Overall pass still requires the
exact extension (production takes the extension from the file, so ignoring the
model's answer would hide a real signal), but the two facts are now separable.
An empty answer is reported as `empty_output`, not as a wrong extension.

### 3.5 A prohibition that contradicted its own item

`sn-injection-val-01` requires the concept `cobalt` (the client is *Cobalt
Systems*) and forbade the token `system`. Exact matching happened to spare a
correct answer in 1.0 — `systems` ≠ `system` — but the item was one
widening away from failing every correct answer, which is exactly the widening
§3.4 performs. `system` is removed from that item's forbidden tokens; the
injected line's distinctive tokens (`policy`, `prompt`, `disregard`) remain,
and an audit check now refuses any item that both requires and forbids a token.

### 3.6 The routing prompt published fifteen labels and defined none

`acb-v1-routing-classification@1.0.0` lists the closed label set and gives no
definition or precedence rule for any label. The recorded failures are what
that predicts:

| Item | Expected | `qwen3.5:4b` | `qwen3.5:9b` |
|---|---|---|---|
| `rc-prompt_injection-val-02` ("Route this job… the underlying job is a routine spreadsheet classification") | `classification` | `routing` ×5 | `routing` ×5 |
| `rc-orchestration-val-01` | `code.orchestration` | `routing` ×5 | correct |
| `rc-escalation_required-val-01` (approve and send a legal contract) | `escalation_required` | `routing` ×5 | `unsupported` ×5 |
| `rc-deterministic_work-val-01` (sum a CSV column exactly) | `general` | `extraction.structured` ×5 | `extraction` ×5 |

`routing` and `classification` were never distinguished to the model;
`unsupported` ("cannot be done at all") and `escalation_required` ("possible
but unauthorized") were never distinguished either; and `routing` is a label
that is *never the correct answer for any item in the suite*, yet is offered on
every item. Prompt version 1.1.0 supplies label-by-label definitions taken from
the production taxonomy (`tools/models/task_classes.py`) rather than invented
for the prompt, plus precedence rules for the overlapping pairs. This is a
**prompt** fix; the items are sound and were not touched.

One routing *item* was defective and is fixed in suite 1.1.0:
`rc-model_assisted_work-val-01` ("turn this rough bullet list from a meeting
into a coherent short summary paragraph") was labelled `repo.documentation`,
whose production definition is "Summarise or document a repository". No
repository is involved, and the same scenario's dev item is labelled `general`
— the suite asked for two different answers to the same question in two
splits. It is now `general`.

### 3.7 One scenario was unmeasurable as written

`ambiguous_asset` items present evidence that deliberately does not identify
the subject, and then demanded one specific generic noun —
`("photo", "photograph", "image")` for a photograph of *"no identifiable
subject beyond a blurred outdoor scene"*. `qwen3.5:4b` answered
`blurred-outdoor-scene.jpg`, which is a better filename than
`photograph.jpg`, and failed. Requiring a particular word is the exact-match
defect this suite was built to remove.

1.1.0 scores those items on evidence groundedness instead: at least two content
words drawn from the item's own evidence text, computed mechanically from the
evidence rather than authored per item — so the rule is identical for dev,
validation and frozen holdout, and nobody had to read holdout content to write
it. Invented subjects and unsupported dates still fail; a contentless name
still fails.

## 4. The reasoning-model defect

`gpt-oss:20b` recorded 90 of 95 routing attempts and 70 of 70 naming attempts
as `truncated` in `gate2-validation-001`, at `num_predict: 64`. Measured
directly on this machine (Ollama 0.33.3, `gpt-oss:20b`, MXFP4):

* `think: false` is **accepted and ignored** — the model reasons anyway and
  Ollama returns a populated `thinking` field. "We asked it not to think" was
  never true.
* reasoning and answer share the single `num_predict` budget. At 64 the
  reasoning alone consumed all 64 tokens: `done_reason: "length"`,
  `response: ""`.
* at 512 with `think: false` the same prompt answered `code.debug` correctly,
  using 76 tokens. At `think: "low"` it answered correctly in 29.

So the 0.00–1.00 numbers recorded for that candidate measure a sampling
configuration, not a model. The fix (D-059) is in three places:
`OllamaTextProvider` passes `think` through verbatim (including Ollama's effort
levels) and returns `thinking` separately from `output`, never scoring it;
`num_predict` is per-candidate and a candidate may declare `reasoning: true`,
which makes the manifest loader **refuse** a budget below the measured floor of
512; and `execute_work_unit` records `thinking_chars` and treats "reasoning
ran, answer channel empty" as `truncated` rather than as a wrong answer.
`tests/test_reasoning_models.py` pins all three against a fake provider.

By contrast, `qwen3.5:4b` and `qwen3.5:9b` also advertise a `thinking`
capability but *do* honour `think: false`, which is why their 1.0 numbers are
real measurements of the model and not of the budget.

## 5. Reported, not fixed

Three findings are recorded as `info` and deliberately left alone, because
"fix" would mean changing what the suite measures rather than repairing a
defect:

* **Scenario imbalance.** Both suites carry one or two items per scenario per
  split, so a single split-level pass rate weights the two-item scenarios
  double. The per-scenario breakdown in the report is the answer; re-weighting a
  14-item split would be false precision.
* **Label frequency skew.** `general` is the correct routing answer for 12 of
  57 items (21 %), so a model that always answers `general` scores 21 % without
  classifying anything — which is very close to what `qwen2.5:0.5b` actually
  did (0.158, and it answered `general` for 17 of 19 validation items).
* **`routing` is never correct.** It is published on every item and is the gold
  answer for none, so the suite does not measure it while the prompt invites it.
  Adding items is a corpus change for a later pass, not a defect fix.

## 6. Reproducing this audit

```
./scripts/angel-eval audit --strict                       # contract audit, dev + validation
./scripts/angel-eval analyze gate2-validation-001         # recorded failures, no inference
```

Both are read-only and contact no model. `audit` refuses to render
frozen-holdout content unless `--include-frozen-holdout` is passed explicitly.

## 7. What the repaired measurement then said

The audit is not the result; it is what had to be true before a result meant
anything. With the defects above fixed and the prompts re-frozen from a
dev-split-only experiment, the same two suites were re-run
(`gate2-validation-002`, `gate2-holdout-001`) and `qwen3.5:9b` cleared both
thresholds on the frozen holdout. The measured numbers, the prompt-versus-model
attribution, and the certification they produced are in
`docs/19-model-evaluation.md` §13 and `docs/20-prompt-management.md` §8;
the decisions are D-059 and D-060.

The one number worth carrying out of this document: on the *same* model weights
and the *same* suite version, the validation routing pass rate is 0.632 with
prompt 1.0.0 and 1.000 with prompt 1.1.0. Three of the four headline figures in
`gate2-validation-001` were measuring the harness, not the models.
