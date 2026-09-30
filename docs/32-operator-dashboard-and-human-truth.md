# Operator dashboard and trustworthy human truth

## Status and scope

This is the local-first vertical slice for preparing Gate 3; it does **not** certify Gate 3 or touch real Angel assets. The implementation is under `tools/operator_dashboard/`, with the existing Naming Lab retained as the development-only naming workspace.

State is intentionally local and gitignored:

```text
.state/operator-dashboard/
  corpora/<corpus-id>/corpus.json, assets.jsonl, local-path-index.json
  canaries/<canary-id>/manifest.json
  truth/<corpus-id>/truth-events.jsonl
  truth/<corpus-id>/truth-corrections.jsonl
```

Portable records use `sha256:<content-sha256>` identity, never absolute source paths. The local path index is a machine-local convenience mapping. A supplied `local_path` is re-hashed before it is accepted, so a stale or wrong path cannot silently display different bytes under an existing content identity.

## Corpus, freeze, and holdout integrity

`coherent_client` is one real shoot of at most 500 assets and is the only corpus class eligible to freeze a Gate 3 canary. `general_file_intelligence` is a separate heterogeneous benchmark (PDFs, invoices, screenshots, text, office/spreadsheet files, media, archives and code/config); it does not inflate the client/project metric.

A Gate 3 freeze selects exactly 100 unique eligible hashes using a stored seed and round-robin strata over media type, orientation, evidence strength, and evidence flags. The manifest records both its own digest and a digest/count of the **entire candidate pool**. Freezing then seals descriptor membership for that corpus. Paths may be refreshed after a move only if the bytes re-hash to the same identity.

Frozen membership is authoritative from `FrozenCanaryStore`; it is never inferred only from a copied `split` field. Naming Lab seed creation checks this registry. Training export also requires the registry and refuses any identity found in a frozen manifest even if a stale human label says `development`. Corrupt canary manifests fail closed because membership checks verify the manifest digest.

Frozen and general-holdout model comparisons stay hidden in the normal dashboard even after human truth is locked. Seeing holdout errors during prompt/model development would turn the holdout into validation data.

## Human truth

`angel-human-label/v1` is model-neutral: naming action, optional preferred and multiple accepted filenames, required/forbidden semantics, semantic date plus provenance, canonical client/project IDs or explicit unknown, subject, normalized tags, media type, ambiguity/context flags, confidence, reason codes, annotator and time. `angel-taxonomy/v1` versions the vocabulary.

The dashboard loads the existing `ManifestRegistry` and presents canonical client/project choices instead of accepting arbitrary free-text IDs. `UNKNOWN / not established` maps to `None`. A project is rejected unless its canonical client is also selected.

The first truth event for an identity is immutable. Corrections are appended separately with actor, reason, timestamp, prior-truth digest, and replacement truth. The correction chain is verified when current truth is read; an overwrite is never used.

Before the first lock, `blind_review_payload` exposes preview/evidence and safe metadata only. For ordinary development/training items, a model comparison may be shown after lock. For frozen/general holdout items it remains withheld.

## Manifest-bound Gate 3 evaluation

`canary_records_from_truth` is only a low-level adapter into the existing `tools.semantic.evaluation` metric authority. A Gate 3 claim must use `frozen_canary_report` / `evaluate-gate3`, which:

- loads and digest-verifies the named frozen manifest;
- evaluates exactly its 100 identities;
- requires all 100 human truths to be present;
- refuses prediction identities outside the frozen set;
- records the canary manifest digest, candidate-pool digest/count, run identity, timestamp, and the existing Gate 3 metrics.

Missing predictions are retained as unknown/review rather than silently dropped.

## Local workflow

These commands create metadata only; no source bytes are copied or uploaded:

```bash
.venv/bin/python -m tools.operator_dashboard.manage --state .state/operator-dashboard init-corpus \
  --corpus-id 2026-client-shoot --kind coherent_client --purpose "Gate 3 shoot"

.venv/bin/python -m tools.operator_dashboard.manage --state .state/operator-dashboard import-descriptors \
  --corpus-id 2026-client-shoot --json reviewed-descriptors.json

.venv/bin/python -m tools.operator_dashboard.manage --state .state/operator-dashboard freeze-gate3 \
  --corpus-id 2026-client-shoot --canary-id gate3-2026-client-shoot --seed operator-recorded-seed

.venv/bin/python -m tools.operator_dashboard.dashboard_cli \
  --state .state/operator-dashboard --corpus-id 2026-client-shoot
```

Descriptor JSON may include a machine-local `local_path` field; it is removed from the portable descriptor and stored only after its file SHA-256 matches `content_sha256`.

Training export is explicitly registry-bound:

```bash
.venv/bin/python -m tools.operator_dashboard.manage --state .state/operator-dashboard export-training \
  --corpus-id 2026-client-shoot --out .state/operator-dashboard/training.jsonl
```

Final Gate 3 evaluation is explicit rather than a dashboard side effect:

```bash
.venv/bin/python -m tools.operator_dashboard.manage --state .state/operator-dashboard evaluate-gate3 \
  --corpus-id 2026-client-shoot \
  --canary-id gate3-2026-client-shoot \
  --predictions frozen-candidate-results.json \
  --run-identity "model=<digest>;prompt=<version>;validator=<version>" \
  --out .state/operator-dashboard/gate3-report.json
```

The Streamlit dashboard binds only `127.0.0.1` and reuses Naming Lab image, PDF-first-page, sampled-video-frame, and bounded-text previews. Documents/macros/scripts are never executed; archives are inventory-only.

## Hosted identity and local boundary

`/`, `/privacy`, and `/terms` remain public Firebase Hosting pages. `/app` is an unindexed operator identity entry that supports Firebase Google sign-in only after deployment-specific public Firebase configuration and an explicit UID allowlist are supplied. Authentication is not authorization: the loopback companion also needs independently verified bearer-token subject plus a separate local operator allowlist.

`CompanionService` binds only `127.0.0.1`, permits exact origins (never `*`), has no filesystem/path endpoint, and currently exposes only an authenticated bounded overview. The hosted page deliberately makes no localhost request: browser Private Network Access/CORS behavior has not been demonstrated reliably enough for private corpus traffic. No source byte, SQLite catalog, or human-label state goes to Firebase, so `/app` is **not deployed** in this change.

Before enabling a bridge, verify Firebase ID-token validation and UID configuration on the actual machine, then test Chrome/Safari Private Network Access preflights from `https://angel-508305.web.app` to the loopback service. If either fails, retain the local dashboard rather than relaxing the boundary.
