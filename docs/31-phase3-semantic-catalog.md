# Phase 3 — Semantic catalog pilot

Phase 3 extends Angel's verified asset catalog with semantic proposals,
canonical client/project identities, embeddings, search, and a derived client
view. It does not change the V1 canonical location (`00-inbox/organized/`),
retention policy, naming pipeline, or source-unlink authority.

## Status

The implementation is present on `feat/phase3-semantic-catalog` and is
covered by focused tests. The live Angel volume currently has only 3 files in
the inbox and 1 organized canonical asset, so it cannot supply the required
100-asset canary or a representative ≤500-asset client shoot. The tagged T7
snapshot `709746c261f530d0f3ba91e3331a990aef95b872fd1334400aea52fab238551a`
was created on 2026-09-20 with `phase3-pilot`, and its isolated restore drill
passed SHA-256 verification. Gate 3 remains blocked only on the real pilot
and its measured canary evidence. Phase 4 worker provisioning is intentionally
untouched.

## Identity and semantic schema

Client manifests live at `20-clients/<client-id>/manifest.yaml` and support:

```yaml
client_id: stone-bellisimo
display_name: Stone Bellisimo
aliases: [Stone Bellissimo]
active: true
notes: ""
```

Project manifests may live below `projects/<project-id>/manifest.yaml`, or be
listed in the client manifest. They carry `project_id`, `client_id`,
`display_name`, optional `date`, `aliases`, and `status`. Classifier output
must choose a canonical ID from these registries. Unknown or ambiguous
identities become review-required; arbitrary output never becomes a directory.

Catalog migration 7 adds the Phase 3 tables (migration 6 creates them and
migration 7 adds deterministic media facets):

* `semantic_classifications` — append-only client, project, subject, tags,
  confidences, validator, model/prompt/evidence hashes, raw bounded output,
  and review state;
* `semantic_corrections` — append-only human locks with old value, new value,
  actor, timestamp, and reason;
* `embeddings` — content/evidence-derived input identity, model/version/digest,
  schema version, vector and timestamp;
* `organized_view_runs`, `organized_view_proposals`, and `materializations` —
  dry-run, drift, conflict, and derived-copy audit state.

`Catalog.latest_semantic_classification()` overlays the latest human correction
for each field over every later model run. This is the read authority used by
embeddings, search, and view planning, so model or prompt upgrades cannot
silently undo a human decision.

## Model and evidence boundary

`tools/semantic/contract.py` defines frozen plain-data inputs. A classifier
receives bounded filename, metadata, native text, OCR, transcript, visual
descriptions, and shoot context, plus canonical candidate records. It receives
no path, file handle, database connection, or mutation callback.

The production prompt is `phase3-semantic-classification@1.0.0`. It uses the
existing prompt registry and renderer, and records rendered prompt SHA-256,
model identity/digest, evidence SHA-256, validator version, and bounded raw
output. `tools/bench/acb_v1/semantic_catalog.py` is an additive capability
suite. The task class remains attended until a real Phase 3 certification is
measured; Gate 2 certifications are not assumed to cover it.

Tags are normalized lower-case hyphenated metadata, capped at 20 values. They
are never interpreted as directories. Person identity recognition is out of
scope.

## Embeddings and search

The current offline baseline is `angel-hash-embedding@1.0.0`, a deterministic
content-addressed SQLite vector layer. It avoids an online dependency while
keeping the schema ready for a measured sqlite-vec implementation. Identical
asset/input/model/schema combinations reuse their row; changed semantic text
creates a new input identity. Search joins vectors back to stable asset IDs,
catalog filenames, canonical paths, MIME, client/project IDs, and tags.

```bash
tools/search "vertical countertop install clips" \
  --client stone-bellisimo --tag vertical-video
```

Supported deterministic filters are client, project, date range (when the
project ID carries an ISO date), MIME prefix, orientation metadata, and tag.
The query does not call a model when indexed embeddings are present.

## Derived organized view

`tools/semantic/materializer.py` builds a dry-run `MaterializationPlan` before
any view write. A plan reports considered assets, review items, conflicts,
estimated additional bytes, free-space headroom, proposal drift, and changed
asset IDs. Drift above 20% aborts. Unknown identities, illegal components,
missing manifests, destination collisions, and different existing bytes are
review/refusal states.

The apply path is explicit and uses ordinary verified copies. It does not use
hardlinks, clonefile, path-derived identity, or source moves. Each new
destination is created exclusively, copied, flushed, re-hashed, and only then
counted as materialized. A second identical run reports zero additions,
rewrites, and moves. The canonical copy remains in `00-inbox/organized/`.

`tools/semantic_view.py` defaults to dry run; `--apply` is the explicit apply
path after the normal T7 UUID preflight. It consumes the approved canonical
filename and never creates a path from raw model text.

## Gate 3 and pilot procedure

The frozen canary must contain 100 representative real assets from one shoot
of at most 500 assets, including photos, videos, orientations, ambiguous and
weak evidence, OCR/transcript/no-transcript cases, and similar projects.
`tools/semantic/evaluation.py` reports client/project precision and recall,
tag precision and recall, coverage, unknown/review rate, confusion categories,
per-identity errors, and tag false-positive/false-negative counts. Candidate
comparison reports changed classifications/tags and confidence shifts.

Gate 3 is green only when client/project precision is at least 95%, tag
precision is at least 80%, coverage is reported, drift is at most 20%, and the
view passes the idempotence test. A newer model does not replace a baseline
unless the candidate comparison also passes these thresholds.

The intended read-only pilot sequence is:

```text
existing catalog asset
  -> bounded V1 perception evidence
  -> versioned semantic proposal
  -> deterministic validator
  -> human correction/canary label
  -> cached embedding
  -> placement dry run
  -> semantic search acceptance
```

Retention remains `IGNORE` / `REVIEW` / `INGEST`, and naming remains its own
proposal/audit chain. A client inference is not retention permission.

## Human truth and operator workflow

`docs/32-operator-dashboard-and-human-truth.md` defines the separate local
canary registry, versioned human labels, blind-first locks, deterministic
100-item selection, training export, and heterogeneous general-file corpus.
The existing `tools.semantic.evaluation` remains the Gate 3 metric authority;
the dashboard only adapts locked truth into its `CanaryRecord` inputs.
