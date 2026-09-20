# Operator dashboard and trustworthy human truth

## Status and scope

This is the local-first vertical slice for preparing Gate 3; it does **not** certify Gate 3 or touch real Angel assets. The implementation is under `tools/operator_dashboard/`, with the existing Naming Lab retained as the development-only naming workspace.

State is intentionally local and gitignored:

```text
.state/operator-dashboard/
  corpora/<corpus-id>/corpus.json, assets.jsonl, local-path-index.json
  canaries/<canary-id>/manifest.json
  truth/<corpus-id>/truth-events.jsonl
```

Portable records use `sha256:<content-sha256>` identity, never absolute source paths. The local path index is only a convenience mapping, so a move/rename cannot alter membership or a locked label.

## Corpus, selection, and labels

`coherent_client` is one real shoot of at most 500 assets and is the only corpus class eligible to freeze a Gate 3 canary. `general_file_intelligence` is a separate heterogeneous benchmark (PDFs, invoices, screenshots, text, office/spreadsheet files, media, archives and code/config); it does not inflate the client/project metric. Both index descriptors without copying or uploading source bytes.

The allowed splits are `development`, `training_candidate`, `validation`, `frozen_canary`, and `general_holdout`. A freeze selects exactly 100 unique eligible hashes using a stored seed and round-robin strata over media type, orientation, evidence strength, and evidence flags. The immutable manifest records its rules and digest.

Frozen/general-holdout content is refused at Naming Lab seed creation and training export. It may not be a prompt example, prompt-edit input, or fine-tuning candidate. `SeedRecord.split == development_seed` remains unchanged.

`angel-human-label/v1` is model-neutral: naming action, optional preferred and multiple accepted filenames, required/forbidden semantics, date provenance, canonical client/project IDs or explicit unknown, subject, normalized tags, media type, ambiguity/context flags, confidence, reason codes, annotator and time. `angel-taxonomy/v1` versions the initial reason-code, tag-normalization, and media-type vocabulary.

Truth events are append-only locks. A second lock for a content identity is refused, preserving an auditable original truth. Before a lock, `blind_review_payload` exposes preview/evidence and safe metadata only; it cannot serialize a model result or comparison. After lock, comparison can be shown. Canonical IDs are validated through `ManifestRegistry`; explicit unknown remains valid.

`canary_records_from_truth` supplies locks to the existing `tools.semantic.evaluation` implementation. The dashboard never computes Gate 3 metrics itself. Human search acceptance queries should be recorded in the real pilot before considering an embedding change.

## Local workflow

These commands create metadata only; no source bytes are transferred or modified:

```bash
.venv/bin/python -m tools.operator_dashboard.manage --state .state/operator-dashboard init-corpus --corpus-id 2026-client-shoot --kind coherent_client --purpose "Gate 3 shoot"
.venv/bin/python -m tools.operator_dashboard.manage --state .state/operator-dashboard import-descriptors --corpus-id 2026-client-shoot --json reviewed-descriptors.json
.venv/bin/python -m tools.operator_dashboard.manage --state .state/operator-dashboard freeze-gate3 --corpus-id 2026-client-shoot --canary-id gate3-2026-client-shoot --seed operator-recorded-seed
.venv/bin/python -m tools.operator_dashboard.dashboard_cli --state .state/operator-dashboard --corpus-id 2026-client-shoot
```

The Streamlit dashboard binds only `127.0.0.1` and reuses Naming Lab image, PDF-first-page, sampled-video-frame, and bounded-text previews. Documents/macros/scripts are never executed; archives are inventory-only. `export-training` produces JSONL for `development` and `training_candidate` locks only, records schema/taxonomy identity and hashes, and refuses frozen rows.

## Hosted identity and local boundary

`/`, `/privacy`, and `/terms` remain public Firebase Hosting pages. `/app` is an unindexed operator identity entry that supports Firebase Google sign-in only after deployment-specific public Firebase configuration and an explicit UID allowlist are supplied. Authentication is not authorization: the loopback companion also needs independently verified bearer-token subject plus a separate local operator allowlist.

`CompanionService` binds only `127.0.0.1`, permits exact origins (never `*`), has no filesystem/path endpoint, and currently exposes only an authenticated bounded overview. The hosted page deliberately makes no localhost request: browser Private Network Access/CORS behavior has not been demonstrated reliably enough for private corpus traffic. No source byte, SQLite catalog, or human-label state goes to Firebase, so `/app` is **not deployed** in this change.

Before enabling a bridge, verify Firebase ID-token validation and UID configuration on the actual machine, then test Chrome/Safari Private Network Access preflights from `https://angel-508305.web.app` to the loopback service. If either fails, retain the local dashboard rather than relaxing the boundary.
