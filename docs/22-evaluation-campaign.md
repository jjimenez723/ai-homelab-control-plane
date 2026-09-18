# 22 — The evaluation-campaign runner (`angel-eval`)

## Objective

`docs/19-model-evaluation.md` and `docs/20-prompt-management.md` describe
the target architecture for capability-based evaluation and versioned
prompts. This document describes the machinery that actually *runs* a
campaign against that architecture, unattended, for hours, and answers the
same question `docs/21-autonomous-routing.md` asks of production routing but
at the evaluation stage: **an AI coding agent's role ends after this
machinery is built and validated.** Running the campaign itself is ordinary,
deterministic software.

This document does not certify a model, does not run the full Gate-2
candidate bakeoff, and does not turn Gate 2 green. It records the CLI, the
manifest schema, the fingerprinting/resumability/status design, and the
report format so the campaign described here can be started later without
this conversation, this session, or any AI agent present.

## What exists as of this pass

* `tools/prompts/` — the D-041 versioned prompt registry (`registry.py`,
  `renderer.py`, `adapters.py`, `library.py`), shared by evaluation and by
  `tools/prompts/production.py`'s task-class lookup (there is no production
  caller yet -- D-051 -- but there is exactly one prompt-construction path
  for when one exists).
* `tools/bench/acb_v1/` — the two Gate-2 suites (D-044): `routing_
  classification.py` and `semantic_naming.py`, each with `dev`/`validation`/
  `frozen_holdout` splits and a deterministic oracle (`tools/bench/suite.py`
  is the shared scaffolding; `tools/bench/cross_validation.py` is the D-043
  independent second implementation, see below).
* `tools/campaign/` — the runner: `manifest.py`, `fingerprint.py`,
  `execution.py`, `store.py`, `runner.py`, `report.py`, `statistics.py`,
  `cli.py`.
* `config/campaigns/gate2.yaml` — the one shipped profile.
* `scripts/angel-eval` — the thin wrapper (`python -m tools.campaign.cli`).

**Superseded as of 2026-09-18:** the real Gate-2 campaigns have now been run
(see "Campaigns actually run" below). What follows described the state before
that. The committed validation profile names the four local candidates already
established by Phase 2 history: `qwen2.5:0.5b`, `qwen3.5:4b`, `qwen3.5:9b`, and
`gpt-oss:20b`. On a real run, a missing manifest-approved Ollama tag is
pulled automatically through the loopback Ollama API, then its actual local
digest and quantization are resolved and pinned into the work-unit
configuration fingerprint. Dry-run remains zero-provider and zero-download.

## Command reference

```bash
# Enumerate suites, splits, candidates, and expected work -- no inference,
# no provider contacted, safe to run at any time.
./scripts/angel-eval campaign --profile gate2 --dry-run

# Run one item per (candidate, suite) pair. Missing manifest-approved
# Ollama candidates are auto-pulled first unless --no-auto-pull is used.
# Smoke output is never certification evidence.
./scripts/angel-eval campaign --profile gate2 --smoke

# Run the full campaign. Checkpoints continuously (one durable JSONL line
# per finished attempt); safe to Ctrl-C at any point.
./scripts/angel-eval campaign --profile gate2

# Re-running the identical command resumes: completed work is skipped,
# incomplete/infrastructure-failed work is retried. `--resume` is accepted
# for clarity but is always the default; use `--no-resume` to force a
# from-scratch recompute under the same campaign_id.
./scripts/angel-eval campaign --profile gate2

# Fail rather than skip an unavailable candidate model, and exit non-zero
# if any infrastructure failure occurred -- useful for a supervised/CI run.
./scripts/angel-eval campaign --profile gate2 --strict

# Regenerate JSON + Markdown reports from the raw append-only record store.
# Reads only what is on disk; needs no model access and no memory of the
# run that produced the records.
./scripts/angel-eval report gate2-validation-001
```

Exit codes: `0` success (or a completed run with no `--strict` violation),
`2` a manifest/profile could not be resolved, `3` the manifest changed under
the same `campaign_id` (`ManifestDrift`), `4` the result store is corrupted,
`5` a candidate is unavailable under `--strict`. No command in this list
prompts interactively; a scheduled/unattended invocation never blocks on
stdin.

## The manifest (`config/campaigns/gate2.yaml`)

The smallest schema that captures the provenance this task requires:
`campaign_id`, `suites` (each an id, a split, and a threshold), `candidates`
(each a model/runtime/quantization/sampling/tool-schema-version plus a
pinned `prompt_id`/`prompt_version` **per suite** -- mirroring
`Certification.configuration_matches`'s own pinning rule), and
`samples_per_item`. `tools/campaign/manifest.py` validates every reference
(a suite id must exist in `tools/campaign/suites.py`; a prompt id/version
must resolve in the live prompt registry) at load time, not at run time.

## Configuration fingerprint

`tools/campaign/fingerprint.py::configuration_fingerprint` hashes exactly
the fields this task's brief names as relevant to measured behavior: model,
model digest, quantization, runtime, prompt id/version, suite/version,
tool-schema version, sampling configuration, and an augmentation/skill
version (recorded as `"none"` today -- itself a fact, per
`docs/19-model-evaluation.md` section 10's finding that no applicable skill
exists yet for these capability classes). Changing any of these changes the
fingerprint, which changes every work-unit id derived from it -- so a
materially different configuration is mechanically a different set of work
units, never a silent overwrite of a prior measurement.

A **work-unit id** is `campaign_id : configuration_fingerprint : suite_id :
item_id : sample<N>` -- exactly the tuple this task's brief specifies
(`campaign + candidate fingerprint + suite + item + sample index`).

A **manifest fingerprint** (`CampaignManifest.manifest_fingerprint()`) covers
the whole resolved manifest. `tools/campaign/runner.py::check_manifest_drift`
records it the first time a `campaign_id` runs and refuses a later run under
a changed manifest and the same id (`ManifestDrift`, exit code 3) unless
`--allow-manifest-change` is passed having understood the consequence: prior
rows under the old manifest remain in the same append-only file, now
describing a different configuration than new rows will.

## Status taxonomy

`tools/campaign/store.py::RESULT_STATUSES`:

| Status | Meaning | Terminal (resume skips it)? |
|---|---|---|
| `verified_pass` | deterministic oracle ran and passed | yes |
| `verified_fail` | deterministic oracle ran and rejected | yes |
| `truncated` | generation stopped at `num_predict`, and did not pass | yes |
| `invalid_output` | oracle rejected the output's basic shape (e.g. non-string) | yes |
| `unverified` | reserved for a future no-verifier path; not produced by these two suites | yes |
| `timeout` | provider call exceeded its timeout | no -- retried |
| `provider_unavailable` | provider unreachable | no -- retried |
| `provider_error` | provider returned a malformed/erroring response | no -- retried |
| `sandbox_error` | reserved for a future code-execution suite | no -- retried |
| `verifier_error` | the oracle itself raised | no -- retried |
| `cancelled` | reserved for a future explicit-interrupt marker | no -- retried |

The invariant this taxonomy exists to enforce: **infrastructure failure is
not model-quality failure.** A `timeout` never counts against a candidate's
measured pass rate, and it is retried on the next resume rather than
recorded once and left ambiguous.

## Repeated sampling

`samples_per_item >= 1` (the shipped profile uses 5, per
`docs/19-model-evaluation.md` section 10.5's k>=5 requirement). Every sample
gets its own append-only record; none are ever averaged away before being
stored, so `tools/campaign/report.py`'s per-item consistency section can
show a flip-flopping item explicitly rather than only its mean. Where a
runtime supports seeding, the seed used is recorded on every record --
`tools/campaign/execution.py` records it, never claims it as a proof of
bit-exact repeatability (the same caution `docs/19-model-evaluation.md`
D-049 already states for Ollama/llama.cpp at `temperature=0`).

## Statistics

`tools/campaign/statistics.py`:

* **Wilson score intervals**, not a bare point estimate, for every reported
  pass rate. A 10/10 sample does not report "100%" without also reporting
  how little that proves at n=10.
* **An exact two-sided paired sign test** for comparing two candidates over
  matched items -- deliberately not McNemar's chi-square approximation,
  which is unreliable at the small per-cell counts this kind of comparison
  usually has (this mirrors `docs/19-model-evaluation.md` section 10.5's own
  choice of the exact form for exactly this reason).

Nothing in this module reports a p-value for a comparison the sample design
does not support, and nothing calls a result "statistically significant"
without the interval that backs the claim.

## Qualification thresholds

Each suite entry in the manifest carries a `threshold`
(`min_pass_rate`, `min_confidence`, `max_timeout_rate`,
`max_provider_error_rate`, optionally `max_p95_latency_seconds`).
`tools/campaign/report.py::_threshold_check` answers, per suite/candidate:
does the measured Wilson lower bound clear `min_confidence`, does the point
estimate clear `min_pass_rate`, and do the infrastructure/timeout rates stay
under their ceilings? This is the schema a future certification decision
reads from -- **this pass records no certification and populates no
`config/certifications.yaml`.**

## Cross-validation (D-043)

`inspect_ai` is not importable in this repository's `.venv`, and the venv's
Python has no `pip` module to install anything with -- there is no working
package installer in this environment, only a plain interpreter. That
finding is recorded in `tools/bench/cross_validation.py` rather than
silently worked around. Per D-043's own fallback ("if Inspect AI is no
longer the best fit, document the evidence and use a better independent
mechanism"), `tools/bench/cross_validation.py` is a from-scratch,
independently-written second implementation of each Gate-2 suite's
deterministic oracle -- sharing no code with `tools/bench/acb_v1/*` beyond
the immutable task data, and not even importing
`tools.ingest.naming.policy` for the naming suite.
`tests/test_cross_validation.py` runs a representative set of real and
adversarial candidate outputs through both implementations and asserts the
verdicts agree; a disagreement there would be a bug in one of the two,
investigated rather than assumed away, exactly as D-043 requires. This does
not change which implementation is authoritative for certification --
`tools/bench/acb_v1/*` remains the suite oracle a campaign result is
measured against. If `inspect_ai` becomes installable in a future
environment, it is a candidate to *replace* this module's cross-checking
role, not the suite oracles themselves (D-043: "Angel retains ownership of
task definitions, deterministic truth, ... regardless of which harness runs
the loop").

## Model staging boundary

A real campaign stages only models explicitly named in its manifest. For
Ollama candidates the runner first checks the exact local tag with
`GET /api/tags`. If it is missing, the default behavior is to pull that
approved tag through loopback Ollama (`POST /api/pull`); `--no-auto-pull`
restores the old fail/skip behavior. The runner never searches for,
recommends, or downloads an undeclared model.

After staging, the runner resolves the actual local digest, quantization, and
model size. Digest/quantization are injected into the per-suite configuration
fingerprint before work-unit ids are built. The first successful resolution
is also checkpointed under
`.state/campaigns/<campaign_id>/resolved_candidates.json`. A later resume
that sees a different digest or quantization under the same candidate name
fails closed with manifest-drift semantics instead of mixing two weight
identities in one campaign.

Pulling still writes wherever the already-running Ollama service is
configured to store model blobs. Angel does not bypass the existing
Ollama/T7 storage configuration or write model files directly.

## Resource-aware candidate concurrency

`manifest.concurrency` is now the **maximum number of simultaneously active
candidate batches**, not a request to run that many samples from one model
at once. The shipped Gate-2 validation profile allows up to four candidates,
but the runner admits them only while a conservative model-memory estimate
fits inside 60% of physical RAM after subtracting unrelated models already
resident in Ollama. The estimate is based on Ollama's staged model size plus
headroom for runtime/context overhead. If the estimate is unavailable, the
runner fails safe to serial candidate execution.

Each admitted candidate runs its own items sequentially. When one finishes,
it is explicitly unloaded and Ollama's running-model endpoint is polled
until the unload is confirmed; only then is its memory budget released and
another waiting candidate may be admitted. Manifest-approved candidate
models that were already resident are cleared before scheduling to establish
a known baseline; unrelated resident Ollama models are never unloaded and
their memory is deducted from the campaign budget.

Validation may therefore run several small models concurrently when they fit
comfortably, while a large model naturally receives a slot by itself. A
campaign containing any `frozen_holdout` suite is forced to one candidate
at a time regardless of the manifest concurrency so final certification
latency/reliability measurements are not contaminated by deliberate
cross-model contention.

## Report contents

`./scripts/angel-eval report <campaign-id>` writes `report.json` and
`report.md` under `<state-root>/<campaign-id>/report/`:

* **Reproducibility** — git SHA and dirty/clean state, generation timestamp,
  host, OS, Python version, and the manifest fingerprint.
* **Quality**, per suite/split/candidate — verified pass rate with its
  Wilson 95% CI, measured-sample count, invalid/infrastructure counts kept
  separate from verified counts, completion rate, timeout rate, truncation
  rate.
* **Per-item consistency** — how many multiply-sampled items produced
  inconsistent pass/fail outcomes across samples, and which ones.
* **Performance** — median/p95 latency, mean input/output tokens.
* **Cost and resources** — API cost (genuinely $0 for local inference,
  labeled as a cost basis rather than "free"), and an explicit note that
  local compute time is a real, unmeasured-here resource cost.
* **Reliability** — provider failure rate, verifier failure rate, overall
  infrastructure failure rate.
* **Qualification** — whether each suite/candidate cell's measured Wilson
  lower bound and infrastructure rates clear the manifest's threshold.

No single "AI score" is ever produced. Every number above is scoped to one
suite and one candidate.

## Resumability, concretely

If a campaign is interrupted after, say, 1,200 of 2,000 planned
evaluations, running the identical `angel-eval campaign` command:

1. loads the manifest and recomputes its fingerprint;
2. confirms it matches the fingerprint recorded for this `campaign_id`
   (`ManifestDrift` otherwise);
3. re-enumerates every work unit the manifest implies;
4. reads the append-only result store and computes which work-unit ids
   already have a terminal record;
5. runs only the remaining ~800, appending one record per finished attempt
   as it goes.

No separate "resume mode" exists to get wrong -- every run already does
exactly this.

## What this document does not claim

This document describes a runner, not a verdict. It does not say whether any
model is good enough to route: that is `docs/19-model-evaluation.md` §13 (the
measured results), `docs/25-gate2-naming-audit.md` (whether the measurement
means anything), and `PLAN.md`'s clause table (the gate itself, maintained by
the integrator).

Two things this runner still does not do. It never downloads a model that is
not an explicitly manifest-approved tag, and it never turns a smoke run into
evidence. And `config/certifications.example.yaml` remains the empty list
`tests/test_autonomous_routing.py` asserts -- a real certification lives in
the gitignored, machine-specific `config/certifications.yaml` (D-060), because
a certification names the exact local model digest measured on one host.

The commands that ran the campaigns below, in order:

```bash
./scripts/angel-eval campaign --profile gate2-dev              # dev split only
./scripts/angel-eval campaign --profile gate2-dev-naming       # dev split only
./scripts/angel-eval campaign --profile gate2-dev-reasoning    # dev split only
# ... freeze the winning prompt versions in tools/prompts/library.py ...
./scripts/angel-eval campaign --profile gate2-validation-002   # comparison
./scripts/angel-eval campaign --profile gate2-holdout --strict # certification
```

## Reasoning models (D-059)

A thinking model's reasoning arrives on a separate channel and is billed
against the same output budget as its answer. Getting that wrong is not a
subtle inaccuracy: `gate2-validation-001` ran `gpt-oss:20b` at
`num_predict: 64` and recorded 95-100 % `truncated` on both suites, measuring
the budget rather than the model.

Measured on this machine (Ollama 0.33.3, M4 Pro):

| Model | `think: false` honoured? | At `num_predict: 64` | At 512 |
|---|---|---|---|
| `gpt-oss:20b` | **no** -- accepted and ignored, reasons anyway | `response: ""`, `done_reason: "length"`, all 64 tokens spent thinking | answers correctly in 76 tokens |
| `qwen3.5:4b`, `qwen3.5:9b` | yes -- empty `thinking`, 3-token answers | answers correctly | answers correctly |

So a candidate declares what it is, and the runner refuses configurations that
cannot measure it:

```yaml
  - name: gpt-oss-20b-p110
    model: gpt-oss:20b
    reasoning: true          # runtime emits a reasoning channel sharing the budget
    sampling:
      num_predict: 1024      # >= 512 is enforced when reasoning: true
      think: low             # passed through verbatim; Ollama's own effort level
```

* `reasoning` is part of the candidate identity and therefore of the
  configuration fingerprint: a reasoning candidate measured at a
  non-reasoning budget is a different (and, as above, meaningless)
  measurement, so it gets different work-unit ids.
* `sampling.think` accepts exactly Ollama's own values (`false`, `true`,
  `"low"`, `"medium"`, `"high"`) and is passed through, never coerced to a
  bool. "We asked it not to think" is never assumed to have worked.
* `sampling.num_predict` is per candidate. With `reasoning: true`,
  `tools/campaign/manifest.py` **refuses** any value below
  `OllamaTextProvider.REASONING_MIN_NUM_PREDICT` (512, the measured floor), so
  the original defect cannot be re-run by accident.
* Only the answer channel is ever scored. `thinking` is recorded as
  `thinking_chars` (result-record schema v3) alongside the attempt's own
  `num_predict`, so "spent the whole budget reasoning" is a distinguishable
  recorded fact rather than an unexplained empty answer.
* `truncated` stays a first-class terminal status and now outranks whatever
  the verifier would otherwise have complained about: a model that never
  finished writing was not measured on the capability, so its half-written
  answer is not reported as a wrong extension or an unknown label.

Re-measured on the dev split with nothing else changed, `gpt-oss:20b` went from
unmeasurable to 0.579 routing / 0.857 naming at the *same* prompt 1.0.0, and to
0.842 / 1.000 at prompt 1.1.0.

## Two guards that run before any inference

Both were previously either test-only or left to whoever wrote the manifest:

* **Contamination.** `check_prompt_contamination` runs
  `tools.bench.suite.assert_no_holdout_leak` over the exact prompt versions a
  campaign will send, on `--dry-run` and on a real run, before a model is
  staged or loaded. D-052 wrote that guard as a run-time check but it only ever
  ran in tests, against whichever version was `active`; a campaign may pin any
  version, including a draft carrying few-shot examples.
* **Shared model tags.** Two candidates that share one Ollama model tag are
  never resident at once. A prompt-variant experiment has several candidates
  over one model, and each candidate batch unloads that tag when it finishes —
  which would have evicted a concurrent sibling's weights mid-batch. Enforced
  in the scheduler, not in the manifest.

## Campaigns actually run

| Campaign | Split | Records | Profile |
|---|---|---:|---|
| `gate2-validation-001` (2026-09-15) | validation | 660 | `gate2` |
| `gate2-dev-001` (2026-09-18) | dev | 594 | `gate2-dev` |
| `gate2-dev-naming-001` | dev | 252 | `gate2-dev-naming` |
| `gate2-dev-reasoning-001` | dev | 198 | `gate2-dev-reasoning` |
| `gate2-validation-002` | validation | 825 | `gate2-validation-002` |
| `gate2-holdout-001` | frozen_holdout | 165 | `gate2-holdout` |

All six live in `.state/campaigns/<campaign_id>/` (gitignored, D-035), each
with `results.jsonl`, `manifest_fingerprint.txt`, `resolved_candidates.json`,
a regenerable `report/`, and a `failure-analysis/` where one was produced.
`gate2-validation-001`'s records are never rewritten: suite 1.1.0 and prompt
1.1.0 produce different configuration fingerprints and therefore different
work-unit ids, so the 1.0 measurement stands as what it was. One consequence
of adding `reasoning` to the candidate identity is that `gate2.yaml`'s manifest
fingerprint moved, so `gate2-validation-001` can no longer be *resumed* under
its own id -- correct, since the configuration it measured no longer exists.

Results, thresholds and the certification they produced are in
`docs/19-model-evaluation.md` §13; the audit that had to happen first is
`docs/25-gate2-naming-audit.md`.
