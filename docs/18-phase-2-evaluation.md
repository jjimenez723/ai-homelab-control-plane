# Phase 2 evaluation — 2026-09-12

## Machine and storage boundary

- MacBookPro Mac16,7; Apple M4 Pro; 48 GB unified memory; macOS 26.5.2 (25F84).
- Internal Data volume had 64,612,816 KiB free before model pulls and
  62,948,708 KiB afterward (about 60.0 GiB). Angel was mounted but was not
  used for runtime/cache/export; no owner file or Angel data was changed.
- Ollama 0.33.3 was installed with Homebrew, started manually on loopback
  `127.0.0.1:11435` only (no LaunchAgent/service), then measured.

The D-003 60 GB internal-free floor prevents responsibly adding a 35B-class
capable model, embeddings, or Whisper large-v3-turbo. This is a measured
constraint, not a claim that those tiers passed.

## Selected local runtime/model results

| Class | Exact runtime/model | Size / quantization | Result |
|---|---|---:|---|
| Small text | Ollama 0.33.3 `qwen2.5:0.5b` | 397,821,319 B, Q4_K_M | Ran; 32k context loaded |
| Local VLM | Ollama 0.33.3 `moondream:latest` | 1,738,451,197 B, Q4_0 | Ran through the bounded loopback `VisionBackend`; its first structured response used an invalid image-kind and was correctly rejected as malformed |
| Capable local | not installed | — | unavailable: free-space floor |
| Embeddings/transcription | not installed | — | unavailable: free-space floor |
| Frontier | unconfigured | — | explicit unavailable, never used for confidential/local-only input |

The VLM is deliberately opt-in (`evidence.vision.backend: ollama`); the
repository default remains heuristic. It receives only raster PNG bytes,
bounded prompt/image data, and a timeout. Its output is strict-validated and
failure is an evidence warning. No real corpus was submitted because the
model did not meet the structured-output acceptance check on the bounded
smoke raster.

## Fixed-context benchmark

One public corpus task (`cls-01`) was padded with inert public tokens to each
target context; generation was capped at 64 tokens. RSS is the benchmark
process high-water mark, not a GPU/unified-memory sensor. Ollama did not
return prefill/generation counters for the loaded 32k run, so those values are
honestly unknown rather than inferred.

| Context | Prompt tokens | Prefill s / tok/s | Generation tokens | Generation s / tok/s | Wall s | Peak RSS |
|---:|---:|---:|---:|---:|---:|---:|
| 2,048 | 2,040 | 0.033 / 62,692 | 64 | 0.548 / 117 | 0.615 | 27,426,816 B |
| 8,192 | 8,184 | 1.284 / 6,375 | 35 | 0.198 / 177 | 1.583 | 27,901,952 B |
| 32,768 | 32,760 | unknown | 32 | unknown | 9.355 | 29,048,832 B |

## 20-task deterministic evaluation

The committed `data/evaluation/phase2_tasks.json` contains 10 classification,
5 filename, and 5 disposable-fixture code tasks. No owner data, live web
dependency, secret, or model output is tracked. Router v0 used deterministic
handlers for `cls-03`, `cls-06`, `cls-08`, and `code-01`; the other sixteen
were sent to the small local model with deterministic exact/filename/fixture
verifiers. The capable and frontier tiers were unavailable.

| Route result | Tasks | Verified |
|---|---:|---:|
| Tier 0 deterministic | 4 | 4/4 |
| Tier 1 `qwen2.5:0.5b` | 16 | 0/16 |
| Tier 2/3 | 0 executions | unavailable |
| Total Tier 0–1 deterministic-verifiable | 20 | **4/20 (20%)** |

Escalation was 16/20 (80%). The normalised local run recorded 695 input and
973 output tokens, 8.509 s wall time, $0 local API cost, and 0.043 Wh using a
configured 0.005 Wh/s estimate. That energy figure is **not** a direct power
measurement.

`python -m tools.report /tmp/phase2-jobs.sqlite --days 7` completed and
reported these totals. Its production default is the ignored local coordinator
path; the `/tmp` database was a disposable evaluation fixture.

## Gate 2: RED

The router, telemetry, report, corpus and local VLM seam exist and are tested,
but the required Tier 0–1 success threshold is 90% and measured 20%. The
small model also did not satisfy the strict task verifiers, while the required
capable tier cannot fit without violating the storage floor. Nothing has been
tuned by weakening a verifier or changing benchmark answers. Interpretation
remains inspectable/non-authoritative; it is not wired to naming, triage,
ingestion, movement, retention, or deletion.
