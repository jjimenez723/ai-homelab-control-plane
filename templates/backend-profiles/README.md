# Backend profiles

Reusable, machine-readable backend selections for the four application
classes named in the Angel V1 completion pass, workstream F. Read
`docs/27-backend-baselines.md` first — these profiles are opinionated
applications of that evaluation to a specific app shape, not independent
research.

## Files

- `index.yaml` — the stable list of profile ids. The app research → spec →
  implementation pipeline (`docs/06-software-factory.md` north-star item 5)
  validates a generated app's `backend_profile.id` against this file. Ids
  are load-bearing: `social-behavior-change`, `nursing-shift-productivity`,
  `single-player-game`, `multiplayer-game`. Do not rename an id once another
  workstream may depend on it.
- `social-behavior-change.yaml`, `nursing-shift-productivity.yaml`,
  `single-player-game.yaml`, `multiplayer-game.yaml` — one profile per
  application class.
- `reference-boilerplates.yaml` — maintained, appropriately licensed
  starters to *read*, never to vendor into this repository.

## Common schema

Every profile YAML uses the same top-level keys:

```yaml
profile_id: <matches index.yaml id>
version: <semver>
summary: <what this app class is and the headline architectural decision>
fit:
  application_traits: [...]
  scale_assumptions: {...}
services:
  <capability>:
    chosen: <product>
    alternatives: [...]
    rationale: <why>
    cost_driver: <what actually shows up on the bill as usage grows>
    quota_watch_outs: [...]
data_model_guidance: <prose>
security_rules_posture: <prose>
offline: <prose>
realtime: <prose>
notifications: <prose>
storage_media: <prose>
privacy_compliance: <prose>
analytics: <prose>
testing_emulators: <prose>
deployment: <prose>
observability: <prose>
known_pitfalls: [...]
references:
  - url: <source>
    accessed: <YYYY-MM-DD>
```

`tests/test_backend_profiles.py` enforces: every profile file loads as valid
YAML, has every required top-level key, `profile_id` matches its filename
stem and appears in `index.yaml` with a stable id, and every `references`
entry (in profiles and in `reference-boilerplates.yaml`) has both a `url`
and an `accessed`/`last_commit_verified` date.

## Why four different profiles instead of one "Firebase app" template

`docs/27-backend-baselines.md` makes the case service-by-service; the
profiles make it concrete:

- **social-behavior-change**: standard Firebase (Auth + Firestore +
  Functions + FCM), because the data is sensitive-but-not-clinical and
  offline mobile behavior matters more than relational structure.
- **nursing-shift-productivity**: the same Firestore-centric shape, but with
  an explicit, written-down PHI-scoping decision that swaps plain Firebase
  Authentication for Identity Platform and prefers Cloud Run over Cloud
  Functions — because HIPAA BAA coverage, not developer convenience, drives
  the choice (docs/27 §12).
- **single-player-game**: the backend is intentionally minimal and
  additive — local persistence is authoritative, cloud save and
  leaderboards are a thin, server-validated layer on top.
- **multiplayer-game**: the least Firebase-centric profile — Firestore/RTDB
  handle metadata and presence, but authoritative in-match state and
  matchmaking live on Cloud Run/Agones and Memorystore, because tick-rate
  real-time state is a poor and expensive fit for either Firestore or RTDB.

One backend does not fit every app; these four profiles are evidence, not
assertion, of that.

## Exporting to Angel

`scripts/export-angel-knowledge` copies this directory and
`docs/27-backend-baselines.md` to
`/Volumes/Angel/50-research/knowledge/backend-baselines/` after verifying
the T7 volume UUID the same way `tools/backup.py` and `tools/ingest/t7.py`
do. The export is a read-only copy; this tracked directory remains the
source of truth (`knowledge/` is gitignored precisely so it never becomes
one).
