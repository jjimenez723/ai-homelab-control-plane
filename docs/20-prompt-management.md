# 20 — Prompt management

## Objective

Prompts must not remain anonymous string literals scattered through Python.
Today they are exactly that: `tools/bench/harness.py`'s `benchmark_prompt()`
holds the only three model-facing prompt templates in the evaluation path
(classification, filename, code fallback), unversioned, with no
identifier, no schema, and no recorded relationship to whatever a future
production caller sends. This document defines the target prompt-management
architecture. It does not implement the registry described below; that is
future work (`docs/19-model-evaluation.md` §11).

## 1. The prompt registry (target schema)

A versioned entry, conceptually:

```text
prompt_id, version, status, capability,
applicable_role_model_runtime_constraints,
policy_template, role_template, task_template, output_schema,
tool_schema_version, example_set_version (optional),
token_or_context_budget, renderer_version
```

`status` distinguishes at least `draft`, `active`, `deprecated`. A prompt is
addressed by `prompt_id` + `version`, never by an unversioned name — the
same discipline `tools/models/registry.py` already applies to models
(`ModelSpec.version`) and `tools/coordinator/ledger.py` already applies to
attempts (`model_version`).

## 2. Composition layers

A rendered prompt is the product of layers that are designed, versioned,
and reasoned about separately, then composed by a renderer:

- **Policy** — trusted safety/authority rules. Never derived from task
  input.
- **Role** — router, tool caller, extractor, bulk reader, backend
  implementer, frontend implementer, debugger, reviewer, orchestrator, etc.
  (mirrors the ACB-v1 capability taxonomy, `docs/19` §2.1).
- **Task contract** — the bounded objective and success requirements for
  this specific task.
- **Context pack** — relevant repository files, logs, evidence,
  screenshots, schemas, metadata, documentation, and — task-driven, not
  injected wholesale — the specific trusted skills/capability guidance and
  approved libraries/reference material a production task of this kind
  would actually receive (`docs/19-model-evaluation.md`'s "Angel does not
  select models on raw standalone intelligence"). A React Native + Firebase
  implementation task's context pack looks like the product SPEC plus
  Expo/React Native guidance, Firebase Auth/Firestore/Security-Rules
  guidance, the selected UI/design-system skill
  (`docs/14-design-system-and-ui-library-policy.md`), and testing
  instructions — not the entire skill library. Untrusted by default (§3).
- **Tool contract** — the exact tools and argument schemas available for
  this task, versioned (`tool_schema_version`).
- **Output contract** — the required output representation (a JSON schema,
  a fixed label set, a filename-shape grammar).
- **Few-shot/examples** — versioned, included only where measurement
  justifies the added tokens/bias, never added by default "for safety."
- **Runtime adapter** — the provider/runtime-specific conversion into
  whatever wire format a given backend needs (Ollama's `/api/generate`
  JSON body, an Inspect AI task's message list, a frontier chat-completion
  call, a future llama.cpp server call).

Semantic prompt design (the first seven layers) is never conflated with
provider-specific chat formatting (the runtime adapter). Today's
`OllamaTextProvider.generate()` (`tools/models/ollama_local.py`) already
demonstrates one runtime-adapter-only concern worth keeping separate from
prompt content: it sets `"think": False` and `"temperature": 0` to keep
reasoning tokens from consuming a small output cap and from breaking an
exact-match verifier. That is a runtime/sampling decision, not a policy or
task-contract decision, and the registry should keep it filed that way.

## 3. Untrusted content stays data

This document does not introduce a new trust rule — it restates the one
this repository already enforces structurally (D-022, D-028, D-029, D-036)
and applies it to prompt composition specifically:

Content from repositories, files, PDFs, OCR, screenshots, websites, logs,
email, or any downloaded content is **context/data**, composed only into
the *context pack* layer. It does not become trusted instruction merely
because a model reads it, and it is never interpolated into the *policy*
layer. A model response never directly obtains destructive/mutation
authority regardless of what its context pack contained — the same
guarantee `tools/ingest/naming/proposer.py`'s `NamingEvidence` (no `Path`,
no file handle, no callable — D-022) and the evidence backends'
`RasterImage`-only interface (D-029) already provide structurally for
naming and perception. Prompt composition must not quietly reopen that
boundary by, for example, letting extracted document text (D-030) migrate
from "evidence to inspect" into "instructions the renderer trusts."

## 4. Evaluation and production share a prompt

**A model-role configuration is deployed using the same versioned prompt
definition and rendering path that was evaluated.** Production inputs will
differ from evaluation inputs — that is expected — but the prompt *recipe*
(policy + role + task-contract shape + output contract + tool contract) is
shared, not duplicated into a second, unrelated production string.

Every evaluation run should eventually record: model ID, weights/quant ID,
runtime/version, capability suite/version, task ID, prompt ID/version,
**rendered-prompt SHA-256**, renderer version, tool-schema version,
context-pack SHA-256, sampling parameters, and verifier version
(`docs/19-model-evaluation.md` §2.7). Rendered production prompts
containing private owner/client content are never committed — the same
boundary D-030 already draws around extracted document text, and the same
reason `tools/bench/harness.py`'s benchmark prompts are built from a public,
committed corpus rather than real evidence.

## 5. Two evaluation tracks, never merged into one number

- **Canonical track** — a standardized, intentionally simple prompt
  contract used identically across compatible models. Purpose:
  approximate apples-to-apples comparison, measure capability scaling,
  reduce model-specific prompt engineering as a confounder. This is close
  to what `benchmark_prompt()` already does today — one template per task
  kind, applied to every candidate without per-model tuning — which is the
  right instinct for a canonical track and the wrong instinct if mistaken
  for a production track.
- **Production-optimized track** — the best measured prompt configuration
  for a given model/role, allowed to be model-specific. Purpose: measure
  actual deployable performance, and let specialists/SLMs receive
  instructions tuned to how they actually respond.

For coding and orchestration task classes specifically, the
production-optimized track uses the same task contract, spec/context, and
skill guidance production would actually give the model — not a vague
generic instruction. Benchmarking a coding model on the canonical track's
simple contract measures scaling; benchmarking it on a stripped-down prompt
while production gives it a detailed implementation contract measures
nothing production will ever encounter, and is not reported as a
production-capability result (`docs/19-model-evaluation.md` §9).
`prompt_id`/version is recorded alongside every result specifically so a
later score change can be attributed to a model change or a prompt/context
change, never left ambiguous between the two.

Production performance belongs to `model × runtime × prompt × role`, not
merely the model. A canonical-track score and a production-track score for
the same model are reported side by side, never merged into one unexplained
number.

## 6. Preventing prompt overfitting

Prompt development follows the same dev/validation/holdout discipline as
task design (`docs/19-model-evaluation.md` §2.5): a prompt may be iterated
against development and validation cases freely, but the frozen
holdout/canary (the Legacy 20, for the original corpus) is never used to
tune a prompt. A gain measured by repeatedly trying prompt variants against
holdout cases and keeping the best one is overfitting, not evidence, however
the resulting number is reported.

## 7. Scope of this document

This document was architecture, not implementation, through 2026-09-14.
**As of 2026-09-15 (D-052), the registry and renderer described in sections
1-4 are implemented**: `tools/prompts/registry.py` (`PromptVersion`,
`PromptRegistry`, immutability enforced by a recorded content fingerprint
per version — `tests/test_prompt_registry.py::ImmutabilityTests`),
`tools/prompts/renderer.py` (the policy -> role -> task/output-contract ->
context-pack composition, deterministic: identical inputs render
byte-identical text and SHA-256), `tools/prompts/adapters.py` (the Ollama
runtime adapter, kept as a named seam separate from semantic content), and
`tools/prompts/library.py` (the two Gate-2 prompts, `acb-v1-routing-
classification` and `acb-v1-semantic-naming`, both `version: "1.0.0"` as of
that pass; both now have a measured, frozen `1.1.0` and several `draft`
experiment arms -- section 8).

`tools/prompts/production.py::render_for_task_class` is the one path both
an ACB-v1 suite runner and a production caller render through — proven
identical by `tests/test_prompt_registry.py::SharedPathTests`, not merely
asserted.

**`naming.semantic` now has that production caller**:
`tools/ingest/naming/semantic.py::SemanticProposer`, opt-in via
`naming.proposer: "semantic"` (D-057). That retires D-051's "still never
exercised by a production caller" for this task class and makes the shared
path load-bearing rather than aspirational — editing
`library.py::SEMANTIC_NAMING_V1` now changes both what the Gate-2 suite
measures and what the file-intelligence pipeline sends, which is exactly the
property D-041 exists to guarantee. Bumping that prompt's version therefore
also reopens every naming question the previous version declined, because
`SemanticProposer.model_version` folds in the proposer version that the
ledger's "already answered" check keys on.

`classification` and `routing` still have no production caller — they belong
to the attended-autonomous routing work (D-051), not to file intelligence.

**The canonical/production track split (section 5) is not built.** Only one
track exists today — the canonical one, a simple closed-label/property
contract applied identically to every candidate, with no few-shot and no
per-model tuning. A production-optimized track is future work once real
measured evidence from that production caller justifies one.

Existing evaluation prompts for the Legacy 20
(`tools/bench/harness.py:benchmark_prompt()`) are **unchanged by this
pass** — deliberately: D-040 keeps the Legacy 20 a frozen historical
regression canary, and retrofitting its literal prompt text into the new
registry was judged out of scope and unnecessary risk for this pass rather
than a requirement this document makes. The registry's two prompts serve
only the two new ACB-v1 suites (`docs/19-model-evaluation.md` §12,
`docs/22-evaluation-campaign.md`).

**Two things changed on 2026-09-14 that make the registry a blocker rather
than a plan.**

First, §4's "a prompt change is a capability change" is now mechanically
enforced. A certification pins `prompt_version` and `tool_schema_version`,
and `tools/router/router.py` refuses a candidate whose pinned versions
differ from the ones about to be used — including when they are simply
absent. **An unpinned prompt version cannot satisfy any certification**
(D-051), so certified autonomous routing is impossible until this registry
exists. That is deliberate: it is the shortest honest statement of why
D-041 sits on Gate 2's critical path.

Second, §6's overfitting rule was being broken by the project's own tooling.
The A→E ablation drew all six of condition E's few-shot examples verbatim
from the Legacy 20 — the frozen holdout — prompt and expected answer both.
The recorded run happened to evaluate a disjoint subset, but the holdout had
been spent as prompt-construction material regardless. `tools/bench/
ablation.py` now refuses at run time when evaluated items appear in its own
prompt material (D-049), and it distinguishes a *worked example*, which can
leak, from the *closed label set*, which is the output contract. A future
renderer should carry the same guard: telling a model which labels exist is
not telling it which one applies, but showing it a solved instance of the
item being scored is.

## 8. The 1.1.0 line: a measured prompt experiment (D-059, D-060, 2026-09-18)

Sections 5 and 6 describe the discipline; this section is what happened the
first time it was actually exercised against real models.

### 8.1 Why there was a second version at all

`gate2-validation-001` measured both prompts at `1.0.0` and produced numbers
well under Gate 2's thresholds. Auditing the whole chain first
(`docs/30-gate2-naming-audit.md`) found two defects in the *prompts*,
distinct from anything about the models:

* **The naming output contract contradicted the oracle.** It called the
  leading ISO date "optional" while 14 of 42 items required one. A model could
  obey the published contract and be deterministically failed — §4's "the same
  prompt definition is evaluated and deployed" is worth nothing if the prompt
  and the grader disagree about what a correct answer is.
* **The routing output contract published fifteen label names and defined
  none of them.** `routing` versus `classification`, and `unsupported`
  ("cannot be done at all") versus `escalation_required` ("possible but
  unauthorised"), were left for the model to guess from the label spellings.

Both are prompt bugs, and both are the kind that a single aggregate pass rate
hides completely.

### 8.2 The experiment, on the dev split only

Eight draft versions, each differing from its predecessor in exactly one
thing, all measured on the **dev** split of suite 1.1.0 at 3 samples per item
(`config/campaigns/gate2-dev.yaml`, `gate2-dev-naming.yaml`,
`gate2-dev-reasoning.yaml`). Every arm's prompt id, version, content
fingerprint, generation settings, raw output and verdict is in the append-only
store. Dev pass rates:

| Prompt | One change from previous | `qwen3.5:4b` | `qwen3.5:9b` |
|---|---|---:|---:|
| routing `1.0.0` | (baseline) | 0.474 | 0.526 |
| routing `1.1.0-a` | + label-by-label definitions | 0.684 | 0.895 |
| routing `1.1.0-b` | + precedence rules for the overlapping pairs | 0.684 | **0.947** |
| naming `1.0.0` | (baseline) | 0.714 | 0.643 |
| naming `1.1.0-a` | + the date rule the oracle enforces | 0.643 | 0.429 |
| naming `1.1.0-b` | + keep-the-extension rule | 0.714 | 0.429 |
| naming `1.1.0-c` | + guidance for unidentifiable evidence | 0.643 | 0.643 |
| naming `1.1.0-d` | + three non-suite few-shot examples | **0.786** | **0.786** |
| naming `1.1.0-e` | `1.0.0` with only the word "optional" replaced | 0.643 | 0.571 |

The naming line is the result worth keeping. Stating the date rule *at length*
made `qwen3.5:9b` substantially worse, not better: it began echoing the
original filename back (`scan0007.pdf`, `Book3.xlsx`) and attaching extensions
with an underscore (`..._HEIC`). The minimal restatement (`-e`) did not recover
it either. What recovered and then improved it was **showing** the output shape
rather than describing it. Three sentences of extra instruction cost this model
more than the ambiguity they removed — a measured fact about instruction
density on a 9B model, not a general law, and exactly the kind of thing §5's
"two tracks, never merged into one number" exists to let you see.

### 8.3 Few-shot examples, and the overfitting rule

`1.1.0-d` and the frozen `1.1.0` carry three worked examples and declare
`example_set_version: "naming-nonsuite-2026-09-18"`. They were written from
scratch for the prompt: no entity, vendor, client, date or wording is shared
with any suite item in any split. §6's rule is enforced three ways rather than
asserted once:

* `tools.campaign.runner.check_prompt_contamination` runs the D-052 run-time
  guard (`assert_no_holdout_leak`) over the exact prompt versions a campaign
  will send — on `--dry-run` too, and **before** any model is staged or
  loaded. Previously that guard only ever ran in tests, and only against
  whichever version happened to be `active`; a campaign may pin any version,
  including a draft carrying examples, which is precisely the case that
  needed covering.
* `tools/bench/gate2_audit.py` checks in both directions: no suite item's text
  in the prompt, and no item's expected answer (an allowed date, a scored
  entity name) in the prompt.
* Development used the dev split only. Validation was run once, after the
  freeze, and nothing measured there was used to change a prompt.

### 8.4 The freeze

The winning arms' content is published under new version strings —
`acb-v1-routing-classification@1.1.0` (from `1.1.0-b`) and
`acb-v1-semantic-naming@1.1.0` (from `1.1.0-d`) — each `status: active`, each
with its own `LOCKED_FINGERPRINTS` row. An arm and the frozen version are
separate immutable identities even where their text is identical, so a record
from the experiment can never be mistaken for a record from the frozen
configuration.

`1.0.0` is untouched and stays in the registry as `deprecated`: every record
`gate2-validation-001` produced still resolves to exactly the text that
produced it. `status` is deliberately excluded from the content fingerprint —
promoting or deprecating is a lifecycle change, not a change of meaning.

### 8.5 The prompt effect, measured on validation

On the **validation** split, with the same model, the same suite version and
the same sampling, prompt 1.1.0 versus 1.0.0 on `qwen3.5:9b`:

| Suite | Prompt 1.0.0 | Prompt 1.1.0 |
|---|---:|---:|
| `acb-v1/routing-classification` | 0.632 [0.531, 0.722] | **1.000** [0.961, 1.000] |
| `acb-v1/semantic-naming` | 0.714 [0.599, 0.807] | **0.929** [0.843, 0.969] |

That control arm is in `config/campaigns/gate2-validation-002.yaml` on purpose.
Without it, the prompt's contribution would have had to be inferred from the
dev split, and §4's claim that a prompt version is part of a capability claim
would have rested on an inference rather than a measurement. It is also why a
certification pins `prompt_version` (D-051): on this evidence, the same weights
are a qualifying configuration or a failing one depending on which prompt
version they are asked with.
