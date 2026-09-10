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
