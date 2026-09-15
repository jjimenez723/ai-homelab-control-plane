# 04 — Model and Task Routing

## Objective

Use the least expensive model/runtime that can reliably complete a task while respecting privacy and risk.

## Routing tiers

### Tier 0 — deterministic code
Use when the task does not need an LLM:
- hashing;
- file moves;
- schema validation;
- metadata parsing;
- exact calculations;
- CI checks;
- access-control decisions.

### Tier 1 — small local model
Examples:
- classification;
- filename proposals;
- log summarization;
- boilerplate;
- extraction;
- simple transformations.

### Tier 2 — capable local model
Examples:
- bounded coding tasks;
- test generation;
- repository summarization;
- straightforward refactors;
- structured vision analysis.

Candidates may include appropriately sized/quantized Qwen, DeepSeek-family, or other open-weight models validated on the actual hardware.

Validation, going forward, means a specific role certification
(`docs/19-model-evaluation.md` §3): `model + quant + runtime + prompt
version + tool-schema version + capability`, not a bare model name. A model
is not assumed capable of a tier merely because it is large, and is not
excluded from a tier merely because it is small — see the SLM research
framing in `docs/19-model-evaluation.md` §2.4.

### Tier 3 — frontier model
Use for:
- architecture;
- high-ambiguity planning;
- difficult debugging;
- security review;
- product strategy;
- cross-repository reasoning;
- final review of consequential changes.

Do not hard-code this document to product names such as future GPT/Claude model versions. Represent providers/models through a registry.

## Example job contract

```yaml
job_id: uuid
task_class: code.implementation
risk: medium
privacy: confidential
inputs:
  repo: products/example
  ref: abc123
requirements:
  capabilities: [code, tools]
  min_context_tokens: 32000
  preferred_execution: local
verification:
  commands:
    - npm test
    - npm run typecheck
escalation:
  on_test_failure: retry_once
  on_low_confidence: frontier_review
```

## Confidence is not enough

A model saying "95% confident" is not verification.

Prefer:
- tests;
- schemas;
- deterministic checks;
- diff inspection;
- sandbox execution;
- independent reviewer model for important work.

## Cost accounting

Record for each job:
- model/provider;
- wall time;
- input/output tokens where applicable;
- estimated dollar cost;
- CPU/GPU time;
- verification result;
- retries;
- final disposition.

This creates real evidence for deciding whether more hardware is worth buying.

## Phase 2 V0 implementation

`tools/models/registry.py` is the data-driven registry: provider, runtime,
configured model identifier, local/frontier status, tier, context limit,
capabilities, version/quantization, and optional frontier token pricing all
live there rather than in router conditionals. `tools/router/` first uses a
deterministic handler, then an enabled small-local backend, capable-local
backend, and finally a frontier backend only when privacy/policy permits it.
A missing backend is an explicit unavailable/escalation outcome; model
confidence never chooses a route.

Operational telemetry is in the Mac-local coordinator ledger at
`.state/coordinator/jobs.sqlite`, not the Angel asset catalog (D-033). Each
job has append-only attempts/events, including verifier result, retries,
escalation path, input/output tokens, timing, price estimate, and a clearly
labeled energy estimate. `python -m tools.report` is read-only and defaults to
seven days. Missing frontier pricing is `unknown`; local API cost can be $0
while energy remains an estimate rather than a sensor reading.

The tracked evaluation corpus and normalized summary are review artifacts.
Raw runs/transcripts are local runtime state or may be deliberately exported
to UUID-verified Angel `40-ai/evaluation/`; they are never committed.

### Routing gates (D-051, 2026-09-14)

`tools/router/router.py` applies three gates in order, each failing closed
and each writing a named ledger event:

1. **Authorization** — work above `autonomous_risk_ceiling` does not run
   without an explicit human authorization token, checked before any
   provider is contacted. An unrecognised risk label ranks above every
   known one, so a typo escalates to a human rather than downgrading to
   `low`. This is `PRINCIPLES.md` §4 as a branch.
2. **Privacy** — confidential/local-only never reaches a frontier provider,
   under any combination of `preferred_execution` and `frontier_enabled`. A
   frontier that was configured and refused is recorded with the rule that
   refused it, not silently dropped.
3. **Certification** — for a task class an ACB-v1 suite gates, only a
   configuration holding a live certification that matches the runtime,
   quantization, prompt version, and tool-schema version about to be used
   is a candidate (`tools/models/certification.py`, D-042).

`tools/models/task_classes.py` is the join these gates need: production task
class → required capabilities → gating suite. An unknown task class is
refused rather than defaulted, because defaulting would let a typo drop a
certification requirement. `code_generation` is a real registry capability;
it used to be aliased to `text_generation`, which meant any text model
satisfied "this job needs code".

One consequence is deliberate: **an unpinned prompt version cannot satisfy
any certification**, so until the `docs/20-prompt-management.md` registry
exists, certification-gated work escalates. That is what makes "a prompt
change is a capability change" enforceable rather than advisory. Today's
evaluation prompts (`tools/bench/harness.py`) remain the un-versioned seed
that architecture is meant to replace.

**Nothing is certified today.** `config/certifications.example.yaml` ships
an empty list and a test keeps it empty until a suite exists, so the
mechanism above currently selects nothing for gated classes and escalates
instead. The decision matrix that proves the gates is
`tests/test_autonomous_routing.py`; the operating modes it does and does not
license are in `docs/21-autonomous-routing.md`.

### Verification is three-state, not two (D-048)

`verified` means a deterministic verifier ran and passed. "Nobody checked"
is `completed_unverified` with `verifier_result = NULL` and disposition
`unverified` — never folded into success. `python -m tools.report` reports
`verified_success_rate` and `completion_rate` separately and gives
per-task-class verification coverage, so an unmeasured task class shows
coverage 0 rather than a perfect score.
