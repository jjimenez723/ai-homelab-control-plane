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

Nothing in this pass has run a real campaign against a staged model. The
committed manifest names `qwen2.5:0.5b` (already exercised in
`docs/19-model-evaluation.md` section 10) as a placeholder candidate;
running it requires that model to be staged locally first (this runner never
downloads one itself -- see "Model installation boundary" below).

## Command reference

```bash
# Enumerate suites, splits, candidates, and expected work -- no inference,
# no provider contacted, safe to run at any time.
./scripts/angel-eval campaign --profile gate2 --dry-run

# Run one item per (candidate, suite) pair against whatever is actually
# reachable, to prove the harness works end to end. Never certification
# evidence.
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
./scripts/angel-eval report gate2-qualification-001
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

## Model installation boundary

`tools/campaign/runner.py::check_candidate_availability` calls each
candidate provider's `available()` -- a loopback reachability/tag check
(`OllamaLocalClient.available`, a `GET /api/tags`), never a pull. An
unavailable candidate is skipped by default (recorded in the run summary)
and the campaign continues with whatever is reachable; `--strict` instead
fails the whole run. **`angel-eval` never downloads a model.** Staging a
candidate is a separate, explicit, human-driven action outside this tool's
scope (the same UUID-gated Angel/T7 storage boundary
`docs/19-model-evaluation.md` section 10.2 already describes for
`qwen3.5:4b`/`9b`/`gpt-oss:20b`).

## Concurrency

`manifest.concurrency` (default `1`) bounds a `ThreadPoolExecutor`. This is
a single-Mac evaluation campaign; there is no distributed scheduler, no
message broker, and no plan to add one. Increasing concurrency risks
resource contention against a local model server and is left to an operator
who understands their own hardware -- the default stays conservative.

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

Gate 2 remains **RED**. This pass built and tested the machinery; it did not
run the campaign against real candidate models, did not populate
`config/certifications.yaml`, and did not touch `config/certifications.
example.yaml`'s empty-list invariant (`tests/test_autonomous_routing.py`
still asserts it). The one command to start the real, unattended Gate-2
qualification campaign once candidates are staged is:

```bash
./scripts/angel-eval campaign --profile gate2
```
