# 19 — Model evaluation: ACB-v1 and the Legacy 20 canary

## Objective

Replace "one 20-task benchmark → one score → one model tier" with a
documented, capability-based evaluation architecture. The question this
answers is:

> Which model/runtime/prompt configuration is qualified for each specific
> workload Angel may route to it?

The evaluated unit is approximately:

```text
model + weights/quantization + runtime + capability role + prompt version
      + tool-schema version + context policy
```

A model is never "good" or "bad" in the abstract. A *configuration* is
certified, or not, for a *role* (§3).

### Angel does not select models on raw standalone intelligence

Angel's production execution unit is not the model alone. It is:

```text
model + task-specific production prompt + bounded spec/context
      + relevant trusted skills/capabilities + approved libraries/reference
        material + tools + deterministic verifier
```

Small/local models are *intentionally* meant to be capability-augmented this
way — a 0.5B–4B model paired with a detailed production task contract, the
right skill guidance, and a deterministic verifier is a different, and
potentially far more capable, configuration than the same model given a bare
instruction. This is why "context policy" in the evaluated unit above is not
a footnote: it expands to the *context pack* layer defined in
`docs/20-prompt-management.md` §2 — bounded spec/context, trusted
skills/capabilities, and approved libraries/reference material all compose
there, selected per task rather than injected wholesale. Evaluation that
ignores this and tests only a bare model with a generic instruction is
measuring a diagnostic baseline, not production capability — see §9.
`docs/04-model-routing.md` describes the tiers a job can be handed to; this
document describes how a candidate earns the right to receive one.

This document does not implement the registry, suites, or Inspect AI
migration it describes. It records the target architecture and the plan to
reach it. Nothing here changes routing authority, model output authority, or
the trust boundary already established in `DECISIONS.md`.

## Why one score was wrong

The original Phase 2 measurement (`docs/18-phase-2-evaluation.md`) ran one
mixed 20-task corpus — 10 classification, 5 filename-proposal, 5 disposable
code-fixture tasks — against every local candidate and produced one pass
count per model (4/20, 4/20, 0/20). That number cannot answer the actual
routing question, for two independent reasons:

1. **The tasks are not one capability.** Classification, filename proposal,
   and bounded code editing are different skills with different failure
   modes and different consumers (router tier-0/1 handoff, the naming
   proposer, a future coding-agent job class). Averaging them into one
   fraction hides which capability actually failed.
2. **A single global score invites two wrong incentives**: penalizing a
   cheap model for failing work it would never be routed, and promoting an
   expensive model to simple work merely because it can also do hard work.

ACB-v1 (§2) replaces the single score with per-capability suites and
per-configuration certification (§3). The Legacy 20 (§1) does not disappear
— it becomes a frozen regression canary with a narrower, honestly stated
purpose.

## 1. Angel Canary v1 — Legacy 20

The existing 20-task corpus (`data/evaluation/phase2_tasks.json`, schema
version 1) is renamed, conceptually, **Angel Canary v1 — Legacy 20**. Its
file, contents, expected answers, and verifiers are unchanged by this
document. Its purposes going forward are:

- historical comparability against the 2026-09-12 measurements;
- regression detection (did something that used to pass stop passing);
- evaluator cross-validation (§6: same task, same model, harness A vs.
  harness B must agree);
- release/gate sanity checking.

It is **not** a complete benchmark of model capability, and it must not
become the dataset used for iterative prompt tuning (§2.5). Any change to its
tasks, expected answers, or verifiers requires a new decision entry, the
same way `DECISIONS.md` D-038 required one for the verifier repair. It moves
from `data/evaluation/phase2_tasks.json` `version: 1` to a new version only
if such a decision is made; no such change is made by this document.

### 1.1 Exact inventory (read from the fixture and harness, not from prior prose)

All 20 tasks share these JSON fields: `id`, `kind`, `prompt`, `expected`,
`intended_tier`, `capabilities`, `risk`, `privacy`, `verifier`, and (code
tasks only) `fixture.tests`. Every task is scored by one function,
`verify()` in `tools/bench/harness.py`, dispatched on `task.verifier`.

**Classification (10 tasks, verifier `exact` → `output == task.expected`):**

| ID | Purpose (prompt, verbatim) | Expected | Multiple valid answers? | Canary-suitable? |
|---|---|---|---|---|
| cls-01 | Classify a PDF invoice from Acme dated 2025-01-01. | `invoice` | Low | Yes |
| cls-02 | Classify a JPEG photograph of a mountain landscape. | `photo` | Low | Yes |
| cls-03 | Classify Python source code defining a sorting function. | `text_generation` | Low, but see note | Yes, with caveat |
| cls-04 | Classify a meeting agenda with attendees and action items. | `meeting` | Medium (`agenda`) | Yes |
| cls-05 | Classify a CSV of monthly revenue and expenses. | `spreadsheet` | Medium (`csv`, `data`) | Yes |
| cls-06 | Classify a README describing how to install a package. | `documentation` | Low | Yes |
| cls-07 | Classify a WAV recording of a spoken interview. | `audio` | Low | Yes |
| cls-08 | Classify a ZIP archive containing a project backup. | `archive` | Medium (`zip`, `backup`) | Yes |
| cls-09 | Classify a signed agreement between two parties. | `contract` | Medium (`agreement`) | Yes |
| cls-10 | Classify a screenshot of a web dashboard. | `screenshot` | Low | Yes |

Note on cls-03: its expected value is the capability name `text_generation`,
not a document-kind noun like the other nine (`invoice`, `photo`, …). This is
a genuine inconsistency in the frozen fixture. It is preserved as-is — the
corpus is not edited to make historical models score better (§11) — but a
future capability-taxonomy revision should not copy this pattern.

**Filename proposal (5 tasks, verifier `filename` → string equality plus a
vestigial path-separator/NUL check that can never fire once equality holds):**

| ID | Purpose | Expected | Multiple valid answers? | Canary-suitable? |
|---|---|---|---|---|
| name-01 | Propose a filename for an Acme invoice dated 2025-01-01. | `2025-01-01_Acme_invoice.pdf` | **High** | Weak — see below |
| name-02 | Propose a filename for a mountain landscape photograph. | `mountain-landscape.jpg` | **High** | Weak |
| name-03 | Propose a filename for a meeting agenda dated 2025-03-04. | `2025-03-04_meeting-agenda.pdf` | **High** | Weak |
| name-04 | Propose a filename for a monthly revenue CSV for 2024. | `2024_monthly-revenue.csv` | **High** | Weak |
| name-05 | Propose a filename for a software installation guide. | `software-installation-guide.md` | **High** | Weak |

Every one of these five accepts exactly one frozen string. A model
proposing `Acme_invoice_2025-01-01.pdf` for name-01 is semantically correct
and would fail. This makes the filename tasks a fine *regression canary for
the harness* (the scoring is deterministic and reproducible) but a poor
*measurement of filename-proposal quality* — that measurement now belongs to
the `semantic-naming` ACB-v1 suite (§2.1), which is designed to tolerate
multiple valid canonical names the way `tools/ingest/naming/quality.py`
already has to.

**Code fixture (5 tasks, verifier `code_fixture`):**

| ID | Purpose | `fixture.tests` | Intended tier | Risk |
|---|---|---|---|---|
| code-01 | Replace TODO with a function that adds two integers. | `test_add` | 0 | medium |
| code-02 | Implement a safe slugify function for ASCII words. | `test_slugify` | 1 | medium |
| code-03 | Fix the off-by-one error in a list chunker. | `test_chunker` | 1 | medium |
| code-04 | Add JSON output for a pure function. | `test_json` | 1 | medium |
| code-05 | Add a timeout-safe retry helper without filesystem access. | `test_retry` | 2 | high |

**Update (D-045, 2026-09-12): `fixture.tests` is no longer a selector name
only.** `tools/fixtures/fixtures/<task-id>/` now holds a real bundle for
each of the five — `fixture.json`, a public `task.md` contract, `starter/`
files the model sees, and `hidden/` verifier-owned `unittest` tests it never
sees. `verify()`'s `code_fixture` branch (`tools/bench/harness.py`) loads
that bundle and runs the submission's code against the hidden tests inside
`tools.fixtures.runner.evaluate_submission`, a bounded macOS Seatbelt
(`sandbox-exec`) sandbox:

```python
if task.verifier == "code_fixture":
    try:
        spec = load_fixture(task.id)
    except (FileNotFoundError, ValueError):
        return False, "code-fixture-unavailable"
    submission = submission_from_model_output(output, spec.editable_files)
    if submission is None:
        return False, "invalid-submission"
    result = evaluate_submission(task.id, submission)
    return result.success, result.status
```

`submission_from_model_output` extracts only code text from the model's
output — a `tests_passed`/`success` claim field is never read, so wrapping a
claim around no code (or around wrong code) still fails. What the sandbox
actually enforces, and what it explicitly does not (read-confidentiality of
the hidden tests against a host-savvy adversary is not one of them, and
that gap is documented rather than hidden), is in `tools/fixtures/sandbox.py`
and D-045. `tests/test_fixture_runner.py` covers both directions (a correct
solution passes, a wrong one fails) plus the adversarial cases D-045 lists;
those tests — and
`tests/test_phase2_bench_router.py::test_code_fixture_runs_real_hidden_tests`
— skip rather than fail on a host without `sandbox-exec` (this repository's
`ubuntu-latest` CI included), while
`test_sandbox_unavailable_fails_closed_without_running_anything` proves the
fail-closed behavior itself unconditionally on any platform. A task with no
fixture bundle still gets the original fail-closed
`code-fixture-unavailable`, exercised by
`tests/test_phase2_bench_router.py::test_code_fixture_unavailable_for_a_task_with_no_bundle`.

### 1.2 What the harness actually does

`tools/bench/corpus.py` loads the fixture and hard-enforces the 10/5/5/20
composition and unique IDs (`load_corpus` raises otherwise) — a structural
guard against silent corpus drift. `tools/bench/harness.py` builds one
production-like prompt per task kind via `benchmark_prompt()` (three literal
templates — classification, filename, code fallback; see
`docs/20-prompt-management.md` for why these are the seed of the prompt
registry problem), left-pads it with inert filler text to hit an exact
`2048`/`8192`/`32768`-token context target, calls
`provider.generate(prompt, context_tokens=..., task=...)`, and scores the
result with `verify()`. `tools/bench/llm_bench.py` is a programmatic entry
point (`main(provider, ...)`), not an argument-parsing CLI; nothing in the
repository currently invokes it outside tests.

### 1.3 Historical results (preserved, not re-measured here)

The full narrative — hardware, storage boundary, the T7/Angel UUID
migration, and every measured run — is `docs/18-phase-2-evaluation.md` and
is unchanged by this document. Summary, for this document's context:

| Candidate | Corpus result | Note |
|---|---:|---|
| Ollama `qwen2.5:0.5b` (fixed-context benchmark only) | n/a | context/latency measured, not corpus-scored |
| Ollama `qwen3.5:4b` (Q4_K_M, digest `2a654d98e6fb`) | 4/20 | all 5 code tasks fail-closed |
| Ollama `qwen3.5:9b` (Q4_K_M, digest `6488c96fa5fa`) | 4/20 | slower, no accuracy gain over 4B |
| Ollama `gpt-oss:20b` | 0/20 | 4/20 requests hit the 120 s adapter timeout while loading |

These results are **historical evidence, not model capability**, for three
compounding reasons: they come from one mixed corpus rather than
role-specific suites; the code-fixture verifier was defective at
measurement time and now fails closed rather than passing anything; and no
evaluator cross-validation (§6) has yet been performed. Do not cite "4/20"
as "this model is 20% capable." Do not delete these rows — they are the
canary's own historical baseline.

## 2. Angel Capability Benchmark v1 (ACB-v1)

ACB-v1 is a set of **role-specific capability suites**, not one global test.
Each suite measures one thing well rather than many things vaguely. A
model/configuration is measured against only the suites relevant to the
roles it might actually be routed (§3).

### 2.1 Capability taxonomy

| Suite | Measures | Primary deterministic oracle |
|---|---|---|
| Routing / classification | task class, risk, privacy, required capabilities, likely worker/model class, escalation need | exact/schema match |
| Tool calling | tool selection, argument construction, abstention, nonexistent-tool avoidance, sequential/parallel/multi-turn calls, failure recovery | schema validation, call-trace replay |
| Structured extraction | bounded facts/entities/fields into a known schema | schema/field match |
| Constrained transformation | bounded, deterministic input→output transforms | exact/schema match |
| Bulk reading / summarization | constrained factual summary of supplied material, no unsupported invention | claim-support check against source, not vibes |
| Semantic naming | canonical-name proposals from evidence, respecting filename/provenance constraints | policy validator (`tools/ingest/naming/policy.py`-style rules), not exact string match |
| Coding — boilerplate/pattern replication | predictable implementation from existing repo patterns | tests/type checks |
| Coding — backend implementation | Python/TS/Node, APIs, SQLite, persistence, concurrency, validation, migrations, transactions, error handling | tests, sandboxed execution |
| Coding — frontend implementation | components, React/UI behavior, state, forms, accessibility, responsive layout, async states, design-system adherence | tests, type checks, accessibility/browser checks |
| Coding — debugging | diagnose and repair from broken code + symptoms/tests/logs | tests (must go from red to green) |
| Coding — review | correctness, regressions, missing tests, security, architecture violations, false-positive avoidance | judged against a known-planted-defect set, deterministically scored |
| Repository understanding | cross-file/module reasoning in a real repository | schema/fact match against ground truth |
| Orchestration / decomposition / delegation | decomposition, worker/tool selection, dependency correctness, parallelization, avoiding unnecessary delegation/duplication, escalation, failure recovery | trace-based scoring (§2.3) |
| Failure recovery / escalation | behavior under verifier rejection, tool failure, missing context, capability boundary, required escalation | trace-based scoring |
| Vision | image/document/UI understanding | schema match against labeled ground truth |
| OCR | text-in-image perception, evaluated as its own specialist capability | text-match/edit-distance against ground truth |
| Embeddings / retrieval | retrieval quality | retrieval metrics (recall@k, MRR), not an LLM leaderboard |
| Speech / ASR | transcription | ASR metrics (WER), not an LLM leaderboard |

Specialist perception/retrieval/ASR systems are evaluated on their own
metrics and are never collapsed into one LLM leaderboard. This taxonomy is
the measurement layer underneath `PRINCIPLES.md` §3 ("capability-aware
routing"); it does not replace that principle, it gives it evidence.

**Coding — frontend implementation** (`ui.implementation`) is evaluated
against the design-system/library contract this repository already
requires — `docs/14-design-system-and-ui-library-policy.md`'s default stack
per platform (Base UI/React Aria + Tailwind + shadcn/ui + Motion for React
web; MUI for Material/data-heavy products; Tamagui/React Native Paper for
Expo; SwiftUI/system controls first on Apple platforms, HIG as the quality
bar, not a component library) — not against free-form invention. The suite
measures whether a model follows the chosen contract, not whether it can
invent an equally plausible one; that policy is not restated here, only
referenced, per this document's own consolidation rule (§11).

**Coding — backend implementation** reserves a future capability family,
`backend.firebase`, for a common/default managed-backend option (Auth,
Firestore schema/data modeling, indexes, Security Rules, Storage, Cloud
Functions, App Check, Emulator Suite, environment separation, deployment,
deterministic/emulator-backed verification). Firebase is a default when a
product spec does not require something else, never a universal
requirement. A dedicated Firebase documentation/capability pass is planned
separately; this document only reserves the taxonomy slot and does not
build a Firebase subsystem.

### 2.2 Coding language/framework metadata

Coding tasks carry metadata for at least: Python, TypeScript, JavaScript,
SQL, Bash, Swift, Go, Rust, Java, HTML/CSS, YAML/JSON/configuration, plus an
optional framework tag. The data is expected to reveal specialization (a
model strong in Python backend work is not assumed strong in Swift or
frontend accessibility) rather than assuming one coding model is universally
best.

### 2.3 Orchestration and tool-use suites, measured as traces

Orchestration and tool-calling suites are not scored by final answer alone.
Each run records a trace: number of model/tool calls, dependency order,
parallelization, duplicated work/context, escalation decisions, retries, and
final verified outcome. A task where the correct behavior is **not to
delegate at all** is a first-class case, scored as a failure if the model
delegates anyway — this guards the same failure mode D-033's telemetry
schema already tracks operationally (`retries`, `escalated`,
`escalation_reason`) but at the evaluation stage, before a configuration is
ever certified for orchestration work.

### 2.4 SLMs are a deliberate research track, not "small = worse"

Small language models are tested primarily for bounded roles: routing,
classification, tool selection, tool-argument generation, structured
extraction, simple transformations, factual bulk reading, relevance
filtering, boilerplate/pattern replication. The research question is:

> What is the smallest configuration that reliably completes each workload?

Enough out-of-role testing is kept to find each SLM's escalation boundary,
but a 1–4B model is not primarily judged on architecture or debugging work
it would never be routed in production. `qwen2.5:0.5b` and the staged
`qwen3.5:4b`/`9b` candidates in the Legacy 20 were, by construction, judged
on all 20 mixed tasks including code — ACB-v1 is what corrects that for
future candidates.

### 2.5 Development / validation / holdout splits

Every capability suite with enough tasks to matter defines three splits:

- **Development** — inspected freely while designing prompts/adapters.
- **Validation** — used to compare candidate configurations.
- **Frozen holdout/canary** — never used for iterative prompt optimization;
  used only for gate/release/regression decisions.

The Legacy 20 (§1) is the frozen holdout/canary for the original mixed
corpus and stays that way unless a demonstrated fixture defect (like D-038)
requires a repair. Repeatedly tuning prompts against a holdout case and
calling the resulting gain "unbiased improvement" is exactly the failure
mode these splits exist to prevent (D-040).

### 2.6 Task metadata schema (target)

Not every field applies to every task:

```text
task_id, benchmark_version, capability, sub_capability, difficulty,
language, framework, modality, context_size, available_tools, risk,
expected_behavior, verifier/oracle, deterministic_or_probabilistic,
split (dev/validation/holdout), provenance, external_benchmark_mapping
```

### 2.7 Run/result metadata schema (target)

The current ledger (`tools/coordinator/ledger.py`) already records a useful
subset of this for production jobs (tier, provider, model, tokens, wall
time, cost/energy estimate, verifier result, retries, escalation). The
evaluation result store should support research, not just print a score, so
it targets the superset:

```text
model, model_version_or_checksum, quantization, runtime_and_version,
prompt_id_and_version, task_or_suite_version, tool_schema_version,
context_metadata, sampling_parameters, tokens_in, tokens_out,
ttft (if available), prefill_tok_s, generation_tok_s, wall_time,
peak_memory, model_disk_footprint, verifier_result, retries, escalation,
failure_category, energy_estimate (labeled as an estimate, per D-035/router
`energy_method`)
```

Every evaluation run should also record: `prompt_id`/version, the
**rendered-prompt SHA-256**, renderer version, tool-schema version, and
context-pack SHA-256 (`docs/20-prompt-management.md` §4) — the same fields
`reviews/ASTRA-V0-ARCHITECTURE-REVIEW.md`'s original `stages` table sketch
already anticipated with `prompt_sha`.

## 3. Certification belongs to a configuration

Production certification is a claim about a specific tuple, not a model
name:

```text
model + quant + runtime + prompt_version + tool_schema_version + capability
```

A certification record holds: suite passed, threshold, measured
latency/resource envelope, known failure modes, escalation policy, and
evaluation date/version. Example profile shape:

```text
qwen-small-X

certified:
  routing.simple
  tool.single
  extraction.structured

not certified:
  code.backend
  debugging
  orchestration
```

A cheap model is not penalized for failing architecture work it is never
routed. A large model is not automatically used for simple work merely
because it can also do hard work. Routing should consult certified
capability data rather than parameter count alone — this is a target for
`tools/router/router.py`, which today selects candidates by
tier/capability/context match only (`ModelRegistry.available(...)` in
`tools/models/registry.py`) and has no certification concept yet. Wiring
certification into `_candidates()` is future implementation work, not part
of this documentation pass.

## 4. Minimum coding-suite verification standard

Prefer deterministic verification — tests, type checks, schema checks,
browser/accessibility checks — over LLM-as-judge. Use LLM-as-judge only
where no meaningful deterministic oracle exists (for example, review-quality
judgment on a case with no single correct diff). This mirrors the project's
existing rule (`docs/04-model-routing.md` "Confidence is not enough") one
layer up, at the benchmark-design stage rather than the production-routing
stage.

## 5. External benchmark strategy

External suites (BFCL-style tool calling, SWE-bench-style coding,
standardized reasoning/language suites, VLM/OCR/retrieval/ASR benchmarks)
are supplementary evidence with recorded provenance and licensing. They do
not replace Angel-specific workload evaluation — Angel's own tool surface,
own repository, and own risk model are not represented in any public suite.

## 6. Evaluation framework direction

**Inspect AI is the preferred candidate evaluation substrate, pending
cross-validation.** It is not adopted yet — nothing in this pass migrates
the harness. Reasons it is the leading candidate: custom tasks, deterministic
and custom scorers, tool use, sandboxed code execution, sample-level
logging, agent-style evaluations, multiple model backends, and
reproducibility — all properties `tools/bench/harness.py` currently
reimplements narrowly for one corpus shape.

Validation path before any adoption decision: run the same model against the
same Legacy 20 task with equivalent prompt semantics through **both** (1)
the existing Angel evaluator (`tools/bench/harness.py`) and (2) an
Inspect-based implementation, and investigate any disagreement before
treating Inspect's result as authoritative. A disagreement is a bug in one
of the two harnesses (or a genuine prompt-semantics gap) until proven
otherwise — never assumed away.

Angel retains ownership of: task definitions, deterministic truth, routing
policy, safety rules, prompt definitions (`docs/20-prompt-management.md`),
storage policy, and telemetry requirements, regardless of which harness runs
the loop. Generic evaluator plumbing (the run loop, model backends, sandbox
execution, sample logging) is the part eventually delegated to mature OSS
infrastructure — the plumbing, not the judgment.

## 7. Failure taxonomy (target)

Every non-success run/result should classify into one of: `verifier_reject`
(deterministic oracle said no), `tool_failure`, `context_missing`,
`capability_boundary` (task is genuinely outside what this configuration was
certified for), `timeout`, `malformed_output`, `truncated_output`,
`escalation_required`.

`truncated_output` was added by D-049 and is not a nicety. Without it, a
generation that stopped because it hit `num_predict` has nowhere to go but
`verifier_reject`, and a harness limit is then read back as "the model was
wrong" — which is exactly what happened to 8 of §10's 50 runs. It is set
whenever generation ends at the ceiling, before any other classification.

This
gives `tools/report` something more useful than a bare pass/fail rate, and
gives orchestration-suite scoring (§2.3) a vocabulary for what "recovery"
means.

## 8. Production task classes

ACB-v1 capability suites (§2.1) are broad; production routing decisions are
made at the granularity of a `task_class`, the same field
`docs/04-model-routing.md`'s example job contract already carries
(`task_class: code.implementation`). Evaluation should eventually be
organized by task class within each suite, because different task classes
in the same broad suite can certify different models:

| `task_class` | Primary ACB-v1 suite |
|---|---|
| `code.implementation` | Coding — backend or frontend implementation |
| `code.refactor` | Coding — boilerplate/pattern replication, or backend/frontend implementation |
| `code.debug` | Coding — debugging |
| `code.testing` | Coding — backend/frontend implementation |
| `code.orchestration` | Orchestration / decomposition / delegation |
| `repo.documentation` | Bulk reading/summarization, repository understanding |
| `ui.implementation` | Coding — frontend implementation (§2.1 note) |
| `backend.firebase` | Coding — backend implementation (reserved family, §2.1 note) |
| classification/filename-style tasks | Routing/classification, semantic naming |

This list is illustrative, not exhaustive, and grows as production task
classes are actually defined in job contracts — it is not invented ahead of
need. The goal is **the smallest/cheapest local model that reliably passes
the real production contract for each task class**, not one model that wins
a generic benchmark. Certification (§3) is already scoped to `capability`;
in practice that capability is expected to be a `task_class` for coding and
orchestration work, not the broader suite name.

## 9. Raw-model baseline vs. production-capability benchmark

Per D-044: two distinct things must never be reported as one number:

- **Raw-model baseline** — a model given a bare/generic instruction with no
  production prompt, spec, skills, libraries, or tools. This is diagnostic
  information only: it characterizes the model itself, in isolation, and is
  useful for comparing base capability across candidates. It is **never**
  routing or certification evidence.
- **Production-capability benchmark** — the full execution unit from the
  Objective section: model + production prompt + bounded spec/context +
  relevant trusted skills/capabilities + approved libraries/reference
  material + tools + a deterministic verifier. This is the only evidence
  that may support a certification record (§3) or a Gate 2 claim (`PLAN.md`
  Phase 2).

The historical Legacy 20 results (§1.3) are raw-model-baseline-shaped: one
generic contract per task kind, applied uniformly, with no task-specific
spec, skills, or reference material attached. They remain valid as
historical/regression evidence under that label (§1) and are not
re-measured or reinterpreted as production-capability results by this
document. Gate 2 requires production-capability benchmark results for its
required roles (`PLAN.md` Phase 2); it does not accept a raw baseline in
their place, however good that baseline looks.

## 10. Augmentation ablation experiment (run 2026-09-13, D-046)

To measure whether Angel's capability augmentation materially improves
small/local model performance — rather than assuming it does — a controlled
ablation was run for a representative subset of tasks, holding the model
and the verifier fixed across five conditions:

| Condition | Configuration |
|---|---|
| A | raw/basic instruction (the raw-model baseline, §9) |
| B | production task prompt |
| C | B + strong SPEC/context |
| D | C + relevant trusted skills |
| E | D + relevant reference material/tools |

This does not become the permanent benchmark for every task — it is a
one-time (or periodically repeated) measurement of augmentation's marginal
value, not a per-task grading rubric. Its prerequisite, the D-038 trusted
fixture runner, was done first (D-045, `tools/fixtures/`), unblocking real
`code_fixture` execution evidence for this ablation.

### 10.1 Runner and reproducibility

`tools/bench/ablation.py` implements the five conditions and reuses
`tools.bench.harness.verify()` and `tools.fixtures.runner` unchanged — no new
verifier or scoring path was introduced. Condition prompts are built by
`build_condition_prompt()`:

- **A** is the frozen `task.prompt` verbatim, no wrapper.
- **B** is `tools.bench.harness.benchmark_prompt()` (classification/filename),
  or the same function's no-fixture fallback text (code — see below).
- **C** adds, for classification, the fixed 10-label taxonomy from §1.1; for
  filename, a condensed version of the real rules in
  `tools/ingest/naming/policy.py` (date-prefix, slug format, extension
  policy); for code, it becomes `benchmark_prompt()`'s fixture branch (the
  task's real `task.md` + starter file — D-045's bundle).
- **D** adds a relevant trusted skill from `skills/`. At the time of this run
  `skills/` held only `social-video-learner` and `ui-design-system-builder`,
  neither applicable to routing/classification, semantic naming, or general
  Python code-fixture work, so **D is identical to C for every task in this
  subset** — recorded explicitly (`skill_applied:
  "none-applicable: ..."`), not silently skipped. This is a gap in the skill
  library, not a null result about whether skills help.
- **E** adds reference material genuinely relevant to the task: three
  few-shot examples for classification/filename drawn from Legacy-20 tasks
  *outside* the tested subset (no answer leakage for the item being scored),
  and one factual Python-stdlib note per code task (e.g. `json.dumps`/
  `json.loads` for code-04) — never the hidden test or a full solution.

Ten tasks were selected as the representative subset: `cls-01`, `cls-04`,
`cls-09` (spanning §1.1's low→medium classification ambiguity), `name-01`,
`name-05`, and all five code fixtures `code-01`..`code-05` (coding is
over-weighted relative to its 5/20 corpus share because D-045 is what made
real execution evidence possible here). `tests/test_ablation.py` covers
condition construction and scoring with a fake provider (no network/model
required) so this stays reproducible in CI; the live run against a real
model requires local Ollama and is not part of the test suite.

Sampling was held fixed across every run: `temperature=0`, `seed=42`,
`num_ctx=4096`, `think=false`, `num_predict=64` (classification/filename) or
`512` (code — `tools/models/ollama_local.py`'s production
`OllamaTextProvider` hard-codes `num_predict=64`, which would truncate a
code answer before it compiles; `tools/bench/ablation.py`'s
`OllamaAblationProvider` is a separate, ablation-scoped client for exactly
this reason and changes nothing under `tools/models/`). Ollama/llama.cpp at
`temperature=0` is *not* a guaranteed bit-exact determinism contract across
runs or hardware; the seed is recorded for repeatability, not claimed as a
proof of it.

**Amendments from the 2026-09-14 review (D-049, D-047).** Three corrections
apply to everything in §10 and are recorded here rather than by editing the
measured numbers, which stand:

1. **Condition E's few-shot examples were six verbatim Legacy-20 items**
   (`cls-02`, `cls-06`, `cls-08`, `name-02`, `name-03`, `name-04`), prompt
   *and* expected answer. The run below evaluated a disjoint subset, so its
   results are uncontaminated — but the runner accepted arbitrary task ids,
   so a later run on any of those six would have scored a model on a prompt
   containing its own answer, and 30% of the frozen holdout had been spent
   as prompt material against §2.5 and `docs/20` §6. The examples are now
   written for the experiment, and `assert_no_canary_contamination` refuses
   at run time rather than relying on the caller being careful.
2. **Condition B is not the production prompt for code tasks.** It uses the
   generic no-fixture contract even where a fixture bundle exists, so that C
   can isolate the fixture spec's marginal value. That was always the
   design; the condition label now says so.
3. **The code fixtures' semantics changed on 2026-09-14** (D-047, the
   verdict-integrity rebuild). `code-01` and `code-03` — the two that passed
   — keep their contracts and gain randomised inputs; `code-04` gains a
   probe for the `summarize` function its `task.md` always told the model to
   preserve, which is precisely the failure §10.4 observed and the fixture
   could not see. **§10.3's code-task numbers are therefore not a comparable
   baseline for future runs.** Classification and filename tasks are
   unaffected.

### 10.2 Models tested

| Model | Result |
|---|---|
| `qwen2.5:0.5b` (Ollama, digest `a8b0c5157701`, Q4_K_M) | Run — see §10.3 |
| `qwen3.5:4b` | **Blocked**: not staged in the local Ollama model store for this pass. Staging it means a multi-GB transfer across the UUID-gated Angel/T7 boundary (D-034/D-037); this evaluation pass does not perform that transfer, to avoid touching gated production storage and open-ended disk/time cost that was not pre-authorized for this run. |
| `qwen3.5:9b` | Blocked, same reason. |
| `gpt-oss:20b` | Blocked, same reason; D-034 additionally flagged disk-floor risk for models at this size. |
| `moondream:latest` | Not applicable — a vision model, out of scope for this text/code ablation. |

No result for any blocked model is fabricated or estimated. Only
`qwen2.5:0.5b` produced real evidence in this pass — **every finding below
is an N=1-model observation** and is reported as such, not generalized to
"small models" broadly.

### 10.3 Results (`qwen2.5:0.5b`, 50 runs = 10 tasks × 5 conditions)

Raw per-run records (prompt, raw model text, parsed output, verifier status,
tokens, timing) are local runtime state at
`.state/evaluation/ablation/20260913T051116Z_qwen2.5-0.5b.jsonl`
(gitignored per D-035, not exported to Angel for this pass). The committed,
non-raw aggregate is
`data/evaluation/ablation_ae_20260913_qwen2.5-0.5b.json`.

**Observed fact — pass rate by condition (10 tasks each):**

| Condition | A | B | C | D | E |
|---|---:|---:|---:|---:|---:|
| Passed | 0/10 | 1/10 | 4/10 | 4/10 | 5/10 |

**Observed fact — per task:**

| Task | A | B | C | D | E |
|---|---|---|---|---|---|
| cls-01 | fail | pass | pass | pass | pass |
| cls-04 | fail | fail | fail | fail | **pass** |
| cls-09 | fail | fail | pass | pass | pass |
| name-01 | fail | fail | fail | fail | fail |
| name-05 | fail | fail | fail | fail | fail |
| code-01 | fail | fail | pass | pass | pass |
| code-02 | fail | fail | fail | fail | fail |
| code-03 | fail | fail | pass | pass | pass |
| code-04 | fail | fail | fail | fail | fail |
| code-05 | fail | fail | fail | fail | fail |

Every failure classified as `failure_category: verifier_reject` (a real,
scored wrong answer) — zero timeouts, zero malformed/unparseable outputs,
zero sandbox/tool failures across all 50 runs. `num_predict=512` was never
exhausted for a code task (largest observed `output_tokens` was 124 of 512).

**Observed fact — qualitative detail:**

- `cls-04` (expected `meeting`): B/C/D all answer the semantically
  defensible `agenda`, which the frozen corpus scores wrong (§1.1 already
  flagged this task's ambiguity as "Medium (`agenda`)"). Only E's fixed
  10-label taxonomy plus few-shot examples pushes the model to the exact
  expected label.
- `cls-09` (expected `contract`): B answers `agreement` (also flagged
  ambiguous in §1.1); C's fixed taxonomy — which does not offer `agreement`
  as an option — is what fixes it, not D or E.
- `name-01`/`name-05`: outputs are semantically reasonable at every
  condition (e.g. `invoice-2025-01-01.pdf` for `name-01`) but never
  reconstruct the one frozen accepted string
  (`2025-01-01_Acme_invoice.pdf`) even with the real naming-policy spec (C)
  and few-shot canonical examples (E). Zero of 25 filename-task runs passed.
- `code-04`: the model's answer at every condition returns only the new
  `summarize_json` function and drops the pre-existing `summarize` function
  and `import json` the task explicitly said to keep ("Return ONLY the full
  new contents of `solution.py`... do not change `summarize` itself") — a
  full-file-replacement instruction-following failure, not a logic error in
  the new function itself, and it is not fixed by any augmentation level.
- `code-02`/`code-05`: genuine logic bugs (missing `import re`; a slugify
  regex that strips the hyphens it just inserted; a retry loop that
  re-raises on the first exception instead of retrying `attempts` times) —
  a real capability boundary for this model at this size, not an
  augmentation or format problem.

### 10.4 Interpretation

*(Interpretation — not observed fact; based on N=1 model, 10 tasks.)*

- Augmentation has clear, positive marginal value for this model on
  classification and on the two code tasks it could ever solve: A→B and
  B→C each move real tasks from fail to pass, and the model never solves a
  classification or the two easiest code tasks without at least
  condition-C augmentation.
- **The largest single jump is B→C** — adding the concrete
  spec/context (fixed label taxonomy; the fixture's real task.md + starter
  file) — not the production-prompt wrapper alone (A→B) and not, in this
  run, trusted skills or reference material (C→D→E), because D was
  identical to C in every case (no applicable skill exists yet) and E only
  changed one outcome (`cls-04`).
- **Diminishing returns begin at D** for this subset, but this is
  confounded, not a finding about skills' value in general: D could not
  differ from C because `skills/` has nothing relevant to these three
  capability classes yet. This ablation cannot say whether trusted skills
  would help — it can only say Angel does not yet have one to test for
  routing/classification, semantic naming, or general Python coding.
- ~~**No augmentation level increased malformed output or distraction** for
  this model — failures were uniformly wrong-but-well-formed answers
  (`verifier_reject`), never truncation, timeout, or unparseable text, even
  at condition E's longest prompts.~~ **Withdrawn 2026-09-14 (D-049).** This
  read a label back as a fact. The runner had no truncation category, so
  every cut-off generation was recorded as `verifier_reject`; the committed
  artifact shows **8 of the 50 runs ended with `output_tokens` exactly at
  the `num_predict` ceiling** — `cls-04/A`, `cls-09/A`, `name-01/A`,
  `name-05/A`, `code-03/A`, `code-05/A`, `code-02/C`, `code-02/D`. Six are
  condition A, whose bare prompt invites prose that the 64-token cap then
  cuts, so **part of the measured A→B gain is output-budget compliance
  rather than capability**. Timeouts and unparseable text were genuinely
  absent; truncation was not.
- ~~**Filename tasks look immune to augmentation** in this run (0/25)...~~
  **Withdrawn 2026-09-14 (D-049).** The 0/25 is real but it is not evidence
  about augmentation, because condition C's injected spec pointed away from
  the graded answer. `_NAMING_SPEC` demanded a "lower-case,
  hyphen-separated" stem; two of the five frozen answers
  (`2025-01-01_Acme_invoice.pdf`, `2024_monthly-revenue.csv`) do not have
  that shape, and `tools/ingest/naming/policy.py` — which the spec claimed
  to restate — produces a third shape again (`sanitize_stem` preserves a
  capitalised word and only collapses separator runs). A model obeying the
  spec exactly could not score. The spec also opened with the literal path
  `tools/ingest/naming/policy.py`, and the model's condition-C answer for
  `name-05` was `ingest-naming-policy.py` — it copied the citation. §1.1's
  "Weak" canary-fitness label for `name-*` stands on its own merits, and
  D-046's decision to reject that verifier as naming-augmentation evidence
  stands; this run simply does not add to it either way.
- **Latency/cost**: all 50 runs completed in 33.7s wall time on CPU
  (Ollama, loopback); no run approached its timeout budget (60s
  classification/filename, 180s code). Cost is $0 — local inference has no
  metered API, so no cost/latency trade-off curve exists for this model;
  cost only becomes a real axis once a metered/frontier candidate is
  evaluated.
- **Consistency across tasks**: within the code class specifically,
  augmentation's effect is bimodal, not smooth — `code-01`/`code-03` go
  from unsolvable to solved at C and stay solved; `code-02`/`code-04`/
  `code-05` stay unsolved at every condition. A 0.5B model's coding ceiling
  in this sample looks closer to "spec unlocks what the model could already
  express correctly" than "richer context teaches it a new skill."

### 10.5 Recommended policy (evidence-backed, not implemented)

- For future SLM benchmarking passes (`PLAN.md` Phase 2's remaining
  bullets), use **at least condition-C-equivalent augmentation**
  (production prompt + task-specific spec/context) as the default, not a
  bare instruction (A).

  **Status downgraded 2026-09-14 (D-049): this is a conservative default,
  not a measured finding.** Exact paired McNemar tests over §10.3's own
  committed per-task results:

  | comparison | tasks gained | tasks lost | exact two-sided p |
  |---|---:|---:|---:|
  | A→B | 1 | 0 | 1.000 |
  | B→C | 3 | 0 | 0.250 |
  | A→C | 4 | 0 | 0.125 |
  | C→D | 0 | 0 | 1.000 |
  | C→E | 1 | 0 | 1.000 |
  | A→E | 5 | 0 | 0.0625 |

  No pairwise comparison reaches conventional significance at N=10 with one
  sample per cell. The direction is consistent and every discordant pair
  favours more augmentation, which is why the default stands; but §10.4's
  claim that the largest jump is **B→C specifically** rests on three
  discordant tasks and cannot be separated from A→B. Condition C is adopted
  because A is clearly inadequate and C is the safe side of an unresolved
  question — not because C was measured to beat B.

  Future suites require **k≥5 samples per item with reported confidence
  intervals**. One sample per cell cannot support a locus claim.
- **Do not yet claim a measured skills/reference-material policy** — no
  applicable skill existed to test, and only one reference-material change
  (few-shot classification examples) altered an outcome. Building the
  first routing/classification and semantic-naming skills (so D can
  actually differ from C) is a prerequisite for a real D/E measurement,
  not something this ablation can substitute for.
- **Reject the Legacy-20 filename verifier as evidence for naming-quality
  augmentation policy.** It is unanimously failed regardless of
  augmentation and, per §1.1, was never meant to measure naming quality —
  that belongs to the future `semantic-naming` ACB-v1 suite with a policy
  validator, not exact-string match.
- `qwen2.5:0.5b` remains uncertified for any production role by this
  ablation alone — it is one diagnostic-adjacent run on a 10-task subset,
  not a production-capability benchmark against a full ACB-v1 suite (§9),
  and does not by itself justify routing changes.
- What remains uncertain: whether `qwen3.5:4b`/`9b` or `gpt-oss:20b` show
  the same B→C-dominant pattern (untested, blocked this pass); whether a
  real trusted skill changes the D/E picture; whether the code-04
  full-file-replacement failure is a prompt-clarity fix (say so more
  emphatically) or a genuine model-size ceiling; and whether classification
  ambiguity (`cls-04`, `cls-09`) is better fixed by a closed label set (C)
  than by more examples (E) in general, or only in this sample.

### 10.6 Next experiment

Per D-046, the next steps are, in order: (1) run this same ablation against
`qwen3.5:4b`/`9b` once/if they are legitimately staged (not as part of an
evaluation pass alone — see the storage boundary note in §10.2); (2) build
the first routing/classification and semantic-naming skill artifacts so
condition D can be measured rather than defaulted to C; (3) proceed to the
broader SLM-by-role ACB-v1 benchmarking pass in `PLAN.md` Phase 2 using
condition-C-equivalent augmentation as the floor per §10.5, once (1) or (2)
are available.

## 11. Scope of this document

This document is architecture and inventory, not implementation, with two
exceptions: §10's augmentation ablation was run on 2026-09-13 (D-046)
because it was explicitly the next scheduled experiment once D-045 unblocked
it, and §12 below records that the two Gate-2 ACB-v1 suites and the
supporting campaign machinery were built on 2026-09-15 (D-052). This
document still does not: download additional models beyond what §10.2
already had locally, run the real campaign against a staged candidate, wire
certification into the router, or build a Firebase capability subsystem. It
does not loosen the Legacy 20's tasks, answers, or verifiers, and neither
§10's nor §12's results change Gate 2's status — see `PLAN.md` Phase 2 for
the current gate wording, which remains **RED**.

## 12. ACB-v1 suites and the campaign runner exist (D-052, 2026-09-15)

The two Gate-2 suites §2.1's table names as the required minimum (D-044) are
implemented, each with `dev`/`validation`/`frozen_holdout` splits and a
deterministic oracle (no LLM-as-judge, per §4):

* `acb-v1/routing-classification` (`tools/bench/acb_v1/routing_
  classification.py`) — 57 tasks across 15 scenarios (straightforward
  routing, semantic paraphrase, ambiguous-but-resolvable, deterministic
  work, model-assisted work, perception/OCR, semantic naming, structured
  extraction, coding, orchestration, general generation, unsupported work,
  escalation-required, adversarial-irrelevant, and prompt-injection-shaped
  content), 19 tasks per split. The closed label set is the production
  task-class taxonomy (`tools/models/task_classes.py`) plus `unsupported`
  and `escalation_required`, so the suite and a future router share one
  vocabulary rather than inventing a second.
* `acb-v1/semantic-naming` (`tools/bench/acb_v1/semantic_naming.py`) — 42
  tasks across 14 categories (invoices, receipts, contracts, screenshots,
  photographs, spreadsheets, client documents, recordings, client media,
  ambiguous assets, contradictory metadata, distracting text, prompt
  injection, source-path leakage), 14 tasks per split. The oracle is
  deterministic property verification layered on
  `tools/ingest/naming/policy.py::validate_proposal` — the same function
  production naming uses, not a second parallel implementation — so
  multiple semantically valid filenames pass and a name is never rejected
  merely for differing from one frozen string, which is exactly the defect
  §1.1 documents in the Legacy-20 filename tasks.

Both suites carry an explicit contamination guard
(`tools/bench/suite.py::assert_no_holdout_leak`) that refuses if any
`frozen_holdout` item's text appears in the prompt material used to build a
suite's prompts, tested in both suites' test files.

The prompt registry D-041 describes is implemented (`tools/prompts/`,
D-052) and is the one path both suites and a future production caller
render through (`tools/prompts/production.py::render_for_task_class`).

The autonomous campaign runner (`tools/campaign/`, `./scripts/angel-eval`)
that executes a suite against a set of candidate configurations,
checkpointing and resuming without an AI agent supervising it, is
documented in `docs/22-evaluation-campaign.md`.

**As of D-052 this certified no model**, and that remained true until
2026-09-18: no suite had been run against a staged candidate.
Section 13 below records what happened when it was. The paragraph that
followed here read, correctly at the time: "Gate 2 remains **RED** ...
clauses 2 (prompts versioned) and 5 (suites exist) move from 'no' to 'yes'
as of D-052; clauses 4 (evaluator cross-validated) and 6 (measured
certifications) do not" -- §6's Inspect AI
compatibility finding and the independent-implementation fallback are
recorded in `tools/bench/cross_validation.py` and `docs/22-evaluation-
campaign.md`, and clause 6 waited on an actual campaign run against staged
candidates. That run has now happened (§13); clause 6's status is a matter
for `PLAN.md`'s own clause table, which the integrator maintains.

## 13. Measured Gate-2 results (D-059, D-060, 2026-09-18)

The suites of §12 have now been run for real, against staged local candidates,
with the harness defects the first run exposed repaired first. Four campaigns,
all on one M4 Pro 48 GB with loopback Ollama 0.33.3, all records append-only in
`.state/campaigns/<campaign_id>/results.jsonl`:

| Campaign | Split | What it was for |
|---|---|---|
| `gate2-validation-001` (2026-09-15, 660 records) | validation | The first real run. Its numbers are preserved verbatim and are the measurement of the 1.0 configuration; three of its four headline figures turned out to be measuring the harness, which is what `docs/25-gate2-naming-audit.md` audits. |
| `gate2-dev-001`, `gate2-dev-naming-001`, `gate2-dev-reasoning-001` (2026-09-18, 1,044 records) | **dev only** | The controlled prompt experiment. Eight prompt arms, one change per arm. |
| `gate2-validation-002` (2026-09-18, 825 records) | validation | The comparison run at suite 1.1.0 / prompt 1.1.0, plus a prompt-1.0.0 control on the same model. |
| `gate2-holdout-001` (2026-09-18, 165 records) | frozen_holdout | Certification, one finalist, serialized (D-053). |

### 13.1 Validation results (suite 1.1.0, 5 samples per item)

`acb-v1/routing-classification`, 19 items × 5 samples = 95 attempts; threshold
0.80 point estimate with a Wilson lower bound ≥ 0.60:

| Candidate | Prompt | Pass rate | 95 % CI | Qualifies |
|---|---|---:|---|---|
| `qwen3.5:9b` | 1.1.0 | **1.000** | [0.961, 1.000] | yes |
| `qwen3.5:4b` | 1.1.0 | 0.947 | [0.883, 0.977] | yes |
| `gpt-oss:20b` | 1.1.0 | 0.842 | [0.756, 0.902] | yes |
| `qwen3.5:9b` | 1.0.0 (control) | 0.632 | [0.531, 0.722] | no |
| `qwen2.5:0.5b` | 1.1.0 | 0.053 | [0.023, 0.117] | no |

`acb-v1/semantic-naming`, 14 items × 5 samples = 70 attempts; threshold 0.75:

| Candidate | Prompt | Pass rate | 95 % CI | Qualifies |
|---|---|---:|---|---|
| `gpt-oss:20b` | 1.1.0 | **1.000** | [0.948, 1.000] | yes |
| `qwen3.5:9b` | 1.1.0 | 0.929 | [0.843, 0.969] | yes |
| `qwen3.5:4b` | 1.1.0 | 0.714 | [0.599, 0.807] | no |
| `qwen3.5:9b` | 1.0.0 (control) | 0.714 | [0.599, 0.807] | no |
| `qwen2.5:0.5b` | 1.1.0 | 0.429 | [0.319, 0.545] | no |

Zero infrastructure failures, zero truncations, and zero of 33 multiply-sampled
items flipped verdict across 5 samples for any candidate — at temperature 0 with
a fixed seed, this runtime is behaving deterministically, which is recorded as
an observation, not claimed as a guarantee (D-049).

### 13.2 What changed the numbers

Three separate causes, measured separately, in decreasing size of effect:

1. **Harness defects (D-059).** The naming oracle rejected evidence-supported
   dates, matched "synonym" sets by exact token equality, and reported every
   empty answer as a wrong extension; `gpt-oss:20b` was run at an output budget
   its reasoning consumed entirely. On the dev split, fixing the suite alone
   moved `qwen3.5:4b` naming from 0.214 (validation, suite 1.0) to 0.714 (dev,
   suite 1.1.0) *with the unchanged 1.0.0 prompt*, and moved `gpt-oss:20b` from
   unmeasurable to 0.857 naming / 0.579 routing with that same prompt.
2. **The prompt (D-060).** On the *same* model, same suite version and same
   split, prompt 1.1.0 versus 1.0.0 is 1.000 versus 0.632 on routing and 0.929
   versus 0.714 on naming. The routing half of that gain is one specific thing:
   1.0.0 published fifteen label names and defined none of them.
3. **The model.** Only after the first two are held fixed does the remaining
   spread mean anything: 0.053 (`qwen2.5:0.5b`) to 1.000 (`qwen3.5:9b`) on
   routing is a real capability difference, and it is the one number of the
   three that says anything about which model to use.

This ordering is the point of §9's raw-baseline-versus-production-capability
distinction, arrived at the hard way: for three of four candidates in
`gate2-validation-001`, the measurement was not yet about the model.

### 13.3 Frozen holdout and certification

One finalist only, per D-040/D-053 — a holdout run is certification, not a
bakeoff. `qwen3.5:9b` + prompt 1.1.0 + suite 1.1.0, serialized:

| Suite | Pass rate | 95 % CI | Threshold | Qualifies |
|---|---:|---|---:|---|
| `acb-v1/routing-classification` | 0.895 | [0.817, 0.942] | 0.80 | yes |
| `acb-v1/semantic-naming` | 1.000 | [0.948, 1.000] | 0.75 | yes |

Measured envelope: p95 latency 1.74 s (routing) and 2.36 s (naming); resident
footprint 5,498,564,770 bytes read from Ollama `/api/ps` under this exact
configuration, not estimated; 0 infrastructure failures; 0 truncations.
Recorded as two certifications (`classification`, `naming.semantic`) in
`config/certifications.yaml` — see D-060 for the full records and for why the
`routing` task class is deliberately *not* certified from this suite, and
`docs/21-autonomous-routing.md` for what a certification does and does not
license.

`gpt-oss:20b` also satisfied both validation thresholds and was deliberately
not taken to the holdout: spending holdout items on a second candidate would
have left nothing clean to certify against. It remains a validation-qualified
candidate at roughly 2.5× the resident footprint.

### 13.4 What is still failing, and where it points

Every remaining failure at the frozen configuration, with the locus the
analysis assigns it (`tools/bench/gate2_failure_analysis.py::attribution_hint`
— a triage hint, not a verdict):

* `qwen3.5:9b` routing, holdout: 2 of 19 items, both `model`. One labels a
  physically impossible request `escalation_required` rather than
  `unsupported` (a possible-but-unauthorised versus impossible distinction);
  one answers with the modality of the attached material instead of the job's
  own task class. Both 5 of 5 samples — consistent, not noise.
* `qwen3.5:9b` naming, validation: 1 of 14 items, `prompt_contract_or_model`.
  On the deliberately unidentifiable asset it returns the original filename
  unchanged, which the production quality gate rejects as not an improvement.
  The evidence does support a name (`blurred outdoor scene`); whether declining
  to rename is the *better* production behaviour is an open question this suite
  currently answers "no" (§13.5).
* `qwen3.5:4b` naming: drops the client name on two items whose evidence names
  it, which is the difference between 0.714 and qualifying.
* `qwen2.5:0.5b`: answered `general` for almost everything on routing (0.053).
  §2.4's SLM research track has its answer for this role: not at this size.

### 13.5 Open questions this run raises

* The `ambiguous_asset` scenario now scores evidence groundedness, and the
  finalist's one remaining naming failure is declining to rename such a file at
  all. Production may well prefer that. If so the *suite* is wrong there, not
  the model — but that is a benchmark-design decision for a later pass, with
  its own version bump, not an adjustment to make a number look better.
* `routing` remains unmeasured (the label is the correct answer for none of the
  57 items) and therefore uncertified.
* Both splits are 19 and 14 items. The Wilson intervals are honest about that:
  the finalist's holdout routing interval is [0.817, 0.942], so "0.895" should
  be read as "somewhere above 0.8", not as a third decimal place.
