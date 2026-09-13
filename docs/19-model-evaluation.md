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
certified for), `timeout`, `malformed_output`, `escalation_required`. This
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

## 10. Augmentation ablation experiment (planned, not yet run)

To measure whether Angel's capability augmentation materially improves
small/local model performance — rather than assuming it does — a controlled
ablation is planned for a representative subset of tasks, holding the model
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
value, not a per-task grading rubric. It is **designed here, not run
here**. Its prerequisite, the D-038 trusted fixture runner, is now done
(D-045, `tools/fixtures/`) — this ablation is the next experiment, run
before the broader SLM-by-role benchmarking pass in `PLAN.md` Phase 2, since
its result should inform how much context/skill augmentation those later
runs use.

## 11. Scope of this document

This document is architecture and inventory, not implementation. It does
not: download additional models, run a new bakeoff, implement the prompt
registry (`docs/20-prompt-management.md`), implement Inspect AI, wire
certification into the router, build a Firebase capability subsystem, or
run the ablation experiment in §10. It does not loosen the Legacy 20's
tasks, answers, or verifiers, and it does not change Gate 2's status — see
`PLAN.md` Phase 2 for the current gate wording, which remains **RED**.
