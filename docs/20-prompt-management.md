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

This document is architecture, not implementation. The registry, renderer,
and canonical/production track split described above are not built by this
pass. Existing evaluation prompts (`tools/bench/harness.py`) and production
prompts (none currently exist outside the naming/evidence deterministic
proposers, which are not model-backed yet) are unchanged.

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
