# 25 — Angel volume layout (v1)

This is the authoritative map of the `/Volumes/Angel` volume. It supersedes
the "recommended logical tree" in `docs/02-t7-filesystem.md`, which now
points here; `docs/02` remains authoritative for *why* the volume is shaped
this way (filesystem decision, catalog placement, the derived-view
primitive, the code-execution rule).

Three files carry the same map in three forms, and they are kept in step:

| Form | Where | Audience |
|---|---|---|
| Prose (this file) | Git `docs/25-angel-volume-layout.md` | a person deciding where something goes |
| Executable | Git `scripts/bootstrap-t7.sh` | a fresh volume |
| Machine-readable | `/Volumes/Angel/90-system/angel-manifest.yaml` | tooling, and a future audit |

Plus a `README.md` at the volume root and in each top-level directory, each
answering the same four questions: what belongs here, what never does, who
writes here, and whether it is backed up. `scripts/bootstrap-t7.sh` writes
all of them and is idempotent — a second run changes nothing, and a README a
human has edited is never overwritten (it writes `<file>.new` beside it and
reports the divergence).

## Identity before any write

The volume's identity of record is its **APFS Volume UUID**, held once in the
gitignored `config/ingest.yaml` (`t7.volume_uuid`). Neither the Finder name
`Angel` nor the path `/Volumes/Angel` is ever sufficient (D-019). Every tool
in this repository that writes here resolves the UUID through `diskutil`
first and refuses on a mismatch, an absent volume, a locked volume, or a
read-only mount:

| Tool | Gate |
|---|---|
| `scripts/organize` | `tools/ingest/t7.py::resolve_t7` |
| `scripts/backup-t7` | `tools/backup.py` (same UUID, plus a repository-ID check) |
| model caches | `tools/models/storage.py::resolve_ai_storage` |
| `scripts/refresh-angel-mirrors` | `tools/angel_sync.py` → `resolve_t7` |
| `scripts/export-angel-skills` | `tools/angel_sync.py` → `resolve_t7` |
| `scripts/repos-inventory` | `resolve_t7`, unless `--report-dir` is given |
| `scripts/bootstrap-t7.sh` | `--expect-uuid`, **required** for any path under `/Volumes` |

## The seven categories this layout has to keep apart

The numeric top-level names are unchanged and are not renamed (they make the
order stable across Finder, terminals, Linux mounts, and tooling). What v1
adds is a place for each of the categories that previously had none, and a
rule for telling them apart:

1. **Canonical, human-owned data.** Irreplaceable. `20-clients/`,
   `30-shared-media/`, and `00-inbox/`.
2. **Development repositories.** `10-code/` — mirrors and archives, never a
   working tree.
3. **Generated applications.** `10-code/generated-apps/`.
4. **Durable knowledge.** `50-research/knowledge/` — reference data, never
   instructions.
5. **Skills.** Trusted (Git, exported to `40-ai/skills/trusted/`) versus
   untrusted (`40-ai/skills/candidates/`).
6. **Regenerable AI artifacts.** `40-ai/` — model caches, datasets,
   evaluation, derived evidence.
7. **System and runtime.** `90-system/` — catalog, reports, staging, cache.

## The tree

```text
/Volumes/Angel/
├── README.md
├── 00-inbox/                       arrival zone; nothing here is organized
│   ├── camera-imports/  client-uploads/  downloads/  quarantine/
│   ├── organized/                  verified, catalog-committed assets (D-023)
│   ├── legacy-dump/{macbook,dell-xeon,dell-i7,old-dell,sd-cards,other}/
│   └── restic-smoke-test/          live restore-drill target; do not touch
│
├── 10-code/                        NOT a working filesystem
│   ├── mirrors/<name>.git          bare mirrors of active repositories
│   ├── archives/<name>.git         bare mirrors of retired/duplicate repos
│   ├── generated-apps/<app>/       source of generated applications
│   └── products/ research/ client-systems/ internal-tools/
│
├── 20-clients/<client-slug>/       CANONICAL client work (layout in docs/02)
│
├── 30-shared-media/
│   └── stock-owned/ audio/ fonts-licenses/ reusable-assets/
│
├── 40-ai/
│   ├── model-cache/{ollama,huggingface,mlx,whisper}/   restic-EXCLUDED
│   ├── datasets/ indexes/ manifests/
│   ├── derived/                    perception output: OCR, vision, frames
│   ├── evaluation/
│   │   ├── benchmarks/legacy-20/   raw Legacy-20 canary JSONL
│   │   ├── campaigns/              angel-eval records + regenerated reports
│   │   └── telemetry/              coordinator snapshots (D-050)
│   └── skills/
│       ├── trusted/                read-only export of Git skills/ + MANIFEST
│       └── candidates/             UNTRUSTED; never executed
│
├── 50-research/
│   ├── app-opportunities/ competitor-research/ market-research/
│   ├── social-video-knowledge/
│   └── knowledge/                  durable non-executable reference material
│
├── 60-exports/
│   └── handoff/ temporary/
│
├── 70-archives/                    cold, closed-out material
│
└── 90-system/
    ├── angel-manifest.yaml         machine-readable form of this map
    ├── catalog.sqlite  sidecars/   ingest catalog, single writer (D-009)
    ├── inventories/ checksums/ logs/ migration-reports/
    ├── staging/                    .partial transfers; restic-EXCLUDED
    └── cache/                      runtime scratch; restic-EXCLUDED
```

## `10-code/`: what "development repositories" means here

`docs/02` already settled the rule, and this layout implements it: **active
working trees stay on the Mac's internal disk under `~/Projects/<repo>`.**
Angel is removable USB storage; running an editor, a file watcher, or a
package manager against it risks work every time the cable moves, and that
reason is independent of the filesystem. GitHub stays the source of truth
for committed code.

So `10-code/` holds clean, non-working copies:

- **`mirrors/<name>.git`** — bare `git clone --mirror --no-hardlinks` copies
  of repositories that are actively worked in. One mirror per distinct GitHub
  remote. They are cloned **from the local working copy, not from GitHub**,
  which is the entire point: `docs/09-backup-recovery.md` lists unpushed Git
  work as irreplaceable, and a mirror of GitHub would by definition not hold
  it. A refresh uses `git remote update` **without** `--prune`, so a branch
  deleted upstream stays covered here.
- **`archives/<name>.git`** — bare mirrors of repositories no longer worked
  in: stale duplicate checkouts, finished engagements, superseded
  experiments. Kept as real object databases so history stays queryable.

`<name>` in both is **one path component**, validated by
`tools/angel_sync.py::validate_mirror_name` before it is joined to the
resolved root: letters, digits, dot, dash and underscore, starting with a
letter or digit, and never `.`, `..`, `.git`, or a name ending in a dot. The
allowlist exists because `./scripts/refresh-angel-mirrors` derives the name
from a repository directory but `angel-sync archive --name` takes it from the
command line, and before D-063 a name containing `..` had the clone land
*outside* the UUID-verified volume root entirely. A symlink or a non-git
directory sitting at the mirror path is likewise refused rather than written
through — resolving a volume by UUID and then following a link out of it
would make the resolution decorative.
- **`generated-apps/<app>/`** — see below.
- **`products/`, `research/`, `client-systems/`, `internal-tools/`** — the
  original scaffold's domains, for clean exports and per-domain code
  archives. Retained rather than renamed.

`10-code/` never holds `node_modules/`, a virtualenv, build output, or a
checkout someone is editing.

### Generated applications go to `10-code/generated-apps/`, not `60-exports/`

Both were candidates; this is the reasoning.

A generated application is **source code with a Git destiny**. The software
factory (`docs/06`) produces something that will get a remote, a test suite,
and a history, and until it does it needs to live somewhere it can be read,
diffed, and re-generated. `60-exports/` is for artifacts *leaving* the
system — a rendered video, a PDF, a built bundle handed to a client — and its
contents are explicitly disposable once delivered. Filing generated source
there would put the only copy of pre-Git work in the directory whose README
says "safe to delete after sending".

So: generated app **source** → `10-code/generated-apps/<app>/`. Its built or
rendered **output**, when it is handed to someone → `60-exports/handoff/`.
Once the app has its own GitHub remote, it graduates: working tree to
`~/Projects/<app>`, mirror to `10-code/mirrors/<app>.git`, and the
`generated-apps/` copy becomes an archive or is retired.

## `40-ai/derived/`: one home for derived media and evidence

`30-shared-media/derived/` was considered and deliberately **not** created.

Derived views of client media already have a home: each project's own
`20-proxies/` under `20-clients/<client>/30-media/...`, where a proxy sits
next to the original it derives from. Adding a second, parallel derived tree
under `30-shared-media/` would make "where is the proxy for this clip" have
two answers.

What genuinely had no home is what the *perception layer* produces: OCR
blocks with geometry and confidence, vision descriptions, sampled video
frames (D-055, D-056), transcripts. That is not media — it is evidence about
media, it is written by tooling rather than a person, and its backup class is
"regenerable by re-running perception". It goes to `40-ai/derived/`, beside
the other machine-written artifacts, and `docs/17-file-evidence.md`'s rule
still applies: it has no authority over any decision.

## Trust boundaries

Two directories on this volume hold material that is **never executed and
never read as instructions**:

- `40-ai/skills/candidates/` — candidate skills compiled from video (D-013).
- `50-research/knowledge/` — knowledge notes, digests, transcripts, claims.

The trusted skill tree is Git `skills/`. `40-ai/skills/trusted/` is a
read-only export of it with a `MANIFEST.yaml` recording the source git SHA,
whether that checkout was dirty, and a sha256 per file. The export is a
verifiable record, not a load path; editing it changes nothing.
`docs/26-skills-and-knowledge-trust.md` is the boundary in full.

## Backup policy per area

The restic source is pinned to the volume's UUID (D-018). Exclusions are
narrow and each one is a positive claim that the data is worthless to
restore:

| Path | In backup | Why |
|---|---|---|
| `20-clients/**` | yes | irreplaceable; highest priority |
| `30-shared-media/**`, `50-research/**`, `70-archives/**` | yes | owner-created |
| `10-code/mirrors/**`, `10-code/archives/**` | yes | this is how unpushed history gets off-site |
| `40-ai/{datasets,indexes,manifests,derived,evaluation,skills}/**` | yes | manifests and measurements are cheap and hard to reproduce |
| `40-ai/model-cache/**` | **no** | regenerable from recorded provider/tag/digest (D-037) |
| `90-system/{catalog.sqlite,sidecars,inventories,checksums,logs,migration-reports}` | yes | catalog and provenance |
| `90-system/staging/**` | **no** | an incomplete transfer has nothing worth restoring |
| `90-system/cache/**` | **no** | runtime scratch, regenerable by definition |
| `**/*.partial` | **no** | same reason as staging |
| macOS volume metadata | **no** | `.Spotlight-V100` is unreadable even to the owner and made restic exit 3 |

Backup is never a substitute for Git remotes, and Angel is never the only
copy of anything (`docs/09-backup-recovery.md`).

## Staging and atomicity

Every transfer onto this volume lands as `90-system/staging/<asset_id>.partial`
first, is flushed and closed, is SHA-256-verified at the destination, is
finalized atomically with its catalog commit, and only then is the source
unlinked. A crash, an unplug, or a hash mismatch always leaves the source
intact and the transfer retryable (`docs/13-dump-first-ingestion.md`).

`90-system/cache/` is the sibling concept for volume-local scratch that is
not a transfer: neither is backed up, and neither is ever the only copy.

## Owner decision: where development repositories should live

This section is a **recommendation for the owner**, not something any tool
does. `scripts/repos-inventory` writes the same recommendation per repository
into `90-system/migration-reports/`.

The canonical development home is **`~/Projects/<repo>`**, on the Mac's
internal disk, with Angel holding a bare mirror for backup coverage. Today
most repositories are under `~/Documents/`, and two facts make relocating
them a human decision rather than an automated one:

1. **`~/Documents` is Google-Drive-synced.** Moving a repository out of it is
   a replicated change: the sync client sees a deletion. That is a decision
   with consequences on another service, and no tool in this repository makes
   it.
2. **`kronos-quant-lab` is a separate trust domain (D-011).** It is
   inventoried — knowing where it is matters — but it is never mirrored by
   this repository's tooling and never proposed for relocation. Its future is
   a dedicated OS user and eventually a separate machine.

Accordingly:

- **No owner repository is moved or deleted by this system.** The inventory
  is read-only: no `--apply`, no `git push`, no `git fetch` against a remote,
  hooks disabled on every invocation.
- **Recommended, for the owner to do by hand:** move active repositories to
  `~/Projects/<repo>`, keep GitHub as the source of truth, and let
  `./scripts/refresh-angel-mirrors` maintain the Angel mirrors. Repositories
  with no remote need a remote before anything else; until then the local
  copy is the only copy and no mirror can be named after it.
- **Stale duplicate checkouts** should be archived to
  `10-code/archives/<name>-<date>.git` and left in place afterwards, so that
  the owner — not a script — decides when to remove the original.

### What was done under this heading on 2026-09-17

`~/Documents/ai-homelab-control-plane` is a stale duplicate of
`~/Projects/ai-homelab-control-plane`: clean, nothing unpushed, no stashes,
and its HEAD `8d45635` is an ancestor of `origin/main`, so every commit it
holds is already in the remote. It was archived as
`10-code/archives/ai-homelab-control-plane-documents-2026-09-17.git`
(`git fsck` clean, HEAD verified contained in `origin/main`) and **left
exactly where it was**, with the recommendation to retire it recorded in
`90-system/migration-reports/`.

`~/Documents/ChatGPT/TLC - Service at the Park 2026` holds a `.git` with no
`origin` remote and **no commits at all**, plus a 391 MiB object store. It is
reported and nothing else: there is no history to mirror and no remote to
name a mirror after. What it is and whether it matters is the owner's call.

## Regenerating any of this

```bash
./scripts/bootstrap-t7.sh /Volumes/Angel --expect-uuid <uuid>   # dirs, READMEs, manifest
./scripts/repos-inventory                                       # repo inventory reports
./scripts/refresh-angel-mirrors                                 # create/refresh mirrors
./scripts/refresh-angel-mirrors --dry-run                       # what would change
./scripts/export-angel-skills                                   # refresh the trusted copy
```

Every one of them is additive: none deletes anything on the volume. A stale
file in the trusted export is reported in `MANIFEST.yaml` under
`stale_files`, for a human to reconcile.
