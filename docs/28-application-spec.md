# 28 — Application Specification v1

## Purpose

`docs/06-software-factory.md` names the pipeline stage between "product
brief" and "bounded coding goals" as the point where an idea becomes
something an implementation agent can act on without re-deriving intent
from prose. Before this document, that handoff had no fixed shape: a
product brief is prose, and prose under-specifies exactly the things an
autonomous or semi-autonomous implementation agent needs to not guess --
which fields are PII, which screen a workflow's steps actually touch,
which test proves which acceptance criterion, what backend profile the
app runs against.

The **application specification** is that fixed shape: one YAML or JSON
document, one schema (`templates/app-spec/schema-v1.json`), validated
structurally and semantically by `tools/appspec`, consumed by
implementation agents through `python -m tools.appspec agent-brief`. It is
"small but complete" by design -- every section in the schema exists
because at least one downstream consumer (an implementation agent, a test
writer, a reviewer, a privacy audit) needs it, and nothing is included
speculatively.

## The pipeline

```text
Idea
  |
Research / product brief          (prose; docs/06 stages 1-4)
  |
Planning model produces a spec    <-- an LLM or a human, from the brief
  |
tools.appspec validate            <-- structural + semantic gate, fails closed
  |
tools.appspec agent-brief         <-- bounded, task-class-scoped extraction
  |
Implementation agents             (code.implementation / ui.implementation /
  |                                 code.testing / repo.documentation)
Tests                             (from the spec's own tests[] plan,
  |                                 each linked to an acceptance criterion)
App
```

The spec is not a replacement for the product brief, `SPEC.md`, or
`ARCHITECTURE.md` (D-015) -- it is the machine-checkable artifact those
documents converge on before implementation starts. A spec that fails
`tools.appspec validate` is not ready for an implementation agent, in the
same sense a job that fails a deterministic verifier is not "done" per
D-047/D-048: an unvalidated spec is not evidence that implementation can
proceed safely, it is silence.

## Schema tour

`templates/app-spec/schema-v1.json` is JSON Schema 2020-12, `$id`
`https://schemas.ai-homelab.internal/app-spec/v1/schema-v1.json`. Every
spec instance declares `spec_version: "1.0"`. The schema is intentionally
closed (`additionalProperties: false` throughout): an implementation agent
should never encounter an undocumented field it has to guess the meaning
of, and a typo'd key fails validation instead of being silently ignored.

Top-level sections, grouped by what they answer:

| Group | Sections | Answers |
|---|---|---|
| Intent | `product_intent`, `target_users` | Why this app, for whom |
| Shape | `platforms`, `workflows`, `entities`, `relationships` | What it does, on what data |
| Access | `permissions`, `authentication`, `authorization` | Who may do what |
| Runtime | `backend_profile`, `persistence`, `realtime`, `offline`, `notifications`, `storage_media` | What it runs on and how it behaves live |
| Data governance | `privacy`, `analytics` | What is sensitive, what is measured |
| Surface | `integrations`, `screens`, `navigation`, `design_system` | External services and UI shape |
| Contract | `apis`, `acceptance_criteria`, `tests` | The implementable, testable contract |
| Operate | `deployment`, `observability`, `migrations` | How it ships and is watched |
| Bounds | `implementation_constraints`, `open_questions` | What is fixed, what is still unknown |

Every addressable element (persona, workflow, entity, screen, acceptance
criterion, test, ...) carries an `id` matching `^[a-z][a-z0-9_-]*$`.
Cross-references are always by id (`workflow.actor` names a persona id,
`screens[].workflow_refs` names workflow ids, `tests[].acceptance_criteria_refs`
names acceptance-criterion ids), never by restating content. This is what
makes semantic validation possible at all: a reference is either a real id
in the document or it is a defect the validator can name precisely.

`backend_profile.id` names one of the profile ids a concurrent workstream
publishes in `templates/backend-profiles/index.yaml`:
`social-behavior-change`, `nursing-shift-productivity`,
`single-player-game`, `multiplayer-game`. `tools/appspec` does not depend
on that file existing -- `tools.appspec.validate.KNOWN_BACKEND_PROFILE_IDS`
is the authoritative fallback list, and the index file, when present, only
*adds* recognized ids on top of it (see "Relationship to concurrent work"
below).

## Structural vs. semantic validation

`tools.appspec.validate.validate_spec(path_or_dict) -> ValidationReport`
runs two passes, and the second only runs if the first is clean:

1. **Structural** -- does the document match the schema's types, required
   properties, enums, array shapes, and patterns. Implemented by
   `tools/appspec/jsonschema_subset.py`, a small (~150 line) validator for
   exactly the JSON Schema keywords the app-spec schema uses (`type`,
   `required`, `enum`, `additionalProperties`, `properties`, `items`,
   `minItems`, `pattern`, and in-document `$ref`). `jsonschema` is not a
   declared dependency anywhere in this repository and CI
   (`.github/workflows/ci.yml`) does not install it, so this repository
   does not gain a new pinned dependency for one schema; see the module's
   own docstring for the full reasoning and its explicit non-goals
   (no `oneOf`/`allOf`, no cross-document `$ref`, no numeric/length bounds).
2. **Semantic** -- cross-reference and coverage checks that a schema alone
   cannot express:
   - every id reference resolves to a real id in the document (no dangling
     `actor`, `screen_ref`, `from_entity`/`to_entity`, `role`, `entity`,
     `workflow_ref`, `acceptance_criteria_refs`, navigation edge endpoint,
     or `entry_screen`);
   - every workflow has at least one acceptance criterion;
   - every acceptance criterion is covered by at least one test;
   - every screen is reachable from `navigation.entry_screen` by following
     `navigation.edges`;
   - every entity field flagged `pii: true` or `phi: true` has a matching
     `privacy.data_classes` entry for that `(entity, field)` pair;
   - `backend_profile.id` is a known profile id.

A `ValidationReport` carries `valid`, `errors` (each is what failed and
where), and `warnings` (non-blocking; today, only "the backend-profile
index file was not found, so only the fallback list was checked").
`report.raise_if_invalid()` raises `SpecInvalid` for callers that want an
exception instead of a boolean.

## How agents consume `agent-brief`

`python -m tools.appspec agent-brief <file>` prints one bounded brief per
Phase 4/5 implementation task class from `tools/models/task_classes.py` --
`code.implementation`, `ui.implementation`, `code.testing`,
`repo.documentation` -- reusing those exact task-class names rather than
inventing a parallel vocabulary. Each brief extracts only the slice of the
spec that task class needs:

- `code.implementation`: backend profile, persistence, entities and their
  fields/types, relationships, APIs, permissions, implementation
  constraints, migrations.
- `ui.implementation`: platforms, design system (stack/library/tokens),
  screens, navigation graph, and workflow goals for UX context.
- `code.testing`: acceptance criteria (Given/When/Then), the test plan,
  and the APIs available to exercise.
- `repo.documentation`: product intent, target users, workflow summaries,
  and open questions.

This is deliberately not the whole spec re-serialized four times. An
implementation agent scoped to `ui.implementation` should not receive API
request/response shapes it will not touch, the same way `docs/06`'s
acceptance-gate discipline ("allowed scope; files/areas that must not
change") bounds a coding task before it starts. `--task-class <name>`
prints a single brief; with no flag, all four print in a fixed order.

`agent-brief` calls `load_spec` directly rather than `validate_spec` --
callers are expected to have run `validate` first (the CLI does not
re-validate on every brief invocation) and should not build implementation
plans from a spec that has not passed the gate.

## Versioning policy

- `spec_version` on every instance is `"1.0"` today. It identifies the
  version of the *document shape* an instance was authored against, not
  the product's own version.
- **Additive-only within a minor line**: adding an optional field to an
  existing object, or adding a new enum value, does not require a
  `spec_version` bump. This mirrors the additive-migration discipline
  `tools/ingest/migrations.py` already applies to the T7 catalog and local
  ledger (D-021's "additive, idempotent" pattern, generalized to a
  document schema instead of a SQLite schema).
- **A `spec_version` bump is required** for: removing or renaming a
  required field, narrowing an enum, changing a cross-reference's target
  section, or changing `$id`. `tools/appspec/schema.py::SCHEMA_VERSION`
  and the schema's own `$id` change together with `spec_version`; the
  validator does not attempt to support multiple schema versions in one
  module -- a breaking change gets its own `schema-v2.json` and its own
  `SCHEMA_VERSION`, the same "existing IDs are not rewritten" discipline
  D-027's catalog migration used for a breaking identity change.
- A spec instance's `spec_version` is checked structurally (the schema
  enums it to `"1.0"` today); `tools.appspec` refuses rather than guesses
  when an instance claims a version this schema does not recognize.

## Deliberately out of scope

- **Not a workflow engine.** The spec describes workflows; it does not
  execute them. Nothing in `tools/appspec` calls an API, renders a screen,
  or runs a migration.
- **Not a UI builder or a codegen tool.** `agent-brief` extracts and
  summarizes; it does not generate SwiftUI/React/Compose source. Actual
  implementation remains an agent task bounded by `docs/06`'s acceptance
  gates.
- **Not a replacement for `docs/14`'s design-system gate.** `design_system`
  records the *choice* (stack, library ref, tokens) a spec author already
  made per `data/ui-library-registry.yaml`; it does not re-implement the
  `ui-design-system-builder` skill's review workflow.
- **No literal secrets.** `deployment.secrets_policy` is prose describing
  where secrets live and how they are provisioned, never a value. This
  mirrors D-014's gitleaks/Dependabot posture and D-030's "extracted text
  never crosses this boundary" discipline, applied to spec authoring.
- **No certification or routing behavior.** A spec names a
  `backend_profile`; it says nothing about which model, local or frontier,
  should implement any part of it. That remains `tools/router/`'s job
  (D-051) and is unaffected by this package.
- **Not itself an ACB-v1 capability suite.** A future `spec-following` or
  `spec-to-implementation` capability suite is plausible (`docs/19`
  §2.1-style), but this decision does not create one.

## Relationship to `templates/project-manifest.yaml` and D-015's six files

`templates/project-manifest.yaml` is per-repository control-plane
metadata: repository location, data classification, agent policy
(`local_models_allowed`, `destructive_actions_require_human`), and
verification commands. It answers "how may an agent act on this
repository at all." The application spec answers a different question --
"what is this application supposed to do, for whom, safely, and how is
that proven" -- and is product-shaped rather than repository-shaped. A
generated app repository is expected to have both: a `project-manifest.yaml`
governing agent conduct, and (typically under `docs/` or the repository
root) an application spec governing what gets built.

D-015 standardizes six durable files per generated app repository
(`README.md`, `AGENTS.md`, `SPEC.md`, `ARCHITECTURE.md`, `SECURITY.md`,
`PLAN.md`). The application spec is not a seventh file competing with
those -- it is structured source material several of them draw from:
`SPEC.md` is expected to be *derived from* (or to directly embed) the
spec's `product_intent`, `workflows`, and `acceptance_criteria`;
`ARCHITECTURE.md` draws from `entities`, `relationships`, `apis`,
`persistence`, and `backend_profile`; `SECURITY.md` draws from
`privacy`, `authentication`, `authorization`, and `permissions`; `PLAN.md`
draws from `tests` and `migrations`. The spec is the machine-checkable
source; the six files remain the human-legible, per-repository documents
D-015 already requires.

## Where the pieces live

- `templates/app-spec/schema-v1.json` -- the schema.
- `templates/app-spec/examples/{squad-quit,nurse-shift,single-player-game,multiplayer-game}.yaml`
  -- one representative, realistic-but-fictional spec per backend-profile
  class (`docs/06`'s example portfolio: a social-behavior-change app, a
  nursing-shift productivity app, and the single-player/multiplayer game
  classes), each validating cleanly including every semantic check.
- `tools/appspec/` -- `schema.py` (loads/caches the schema),
  `jsonschema_subset.py` (the structural validator), `validate.py`
  (`ValidationReport`, semantic checks, `known_backend_profile_ids()`),
  `brief.py` (`generate_brief`/`generate_all_briefs`), `cli.py` and
  `__main__.py` (`python -m tools.appspec ...`).
- `tests/test_appspec.py` -- schema loading, all four examples validating,
  one mutated failure per semantic check, CLI smoke tests, and unit tests
  for the JSON Schema subset validator itself.
