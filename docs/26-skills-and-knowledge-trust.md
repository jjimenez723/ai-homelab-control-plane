# 26 — Skills and knowledge: where the trust boundary is

## The hazard in one sentence

An agent harness loads every `SKILL.md` it finds in a trusted skill tree and
treats it as instructions, without asking — so a procedure extracted from a
stranger's video must never be able to land in that tree.

## Four homes, three trust levels

| Artifact | Home | Trust | Executed? |
|---|---|---|---|
| Reviewed skills | Git `skills/` | trusted | yes, by agent harnesses |
| Exported copy of that tree | Angel `40-ai/skills/trusted/` | trusted record | no — not a load path |
| Candidate skills from video | Angel `40-ai/skills/candidates/` | **untrusted** | never |
| Knowledge notes, digests, transcripts | Angel `50-research/knowledge/` | **untrusted** | never |

Git `skills/` is the **source of truth**. It is small, it is reviewed, and it
changes only through a pull request a human read (`skills/README.md`).

## Why the export is not the source of truth

`40-ai/skills/trusted/` exists for two reasons, neither of which is loading:

- a fresh Mac with this drive and no clone can still see what was trusted;
- `MANIFEST.yaml` records the source git SHA, whether that checkout was
  dirty, and a sha256 per file — so "what was trusted on this date" is a
  verifiable claim rather than a memory.

Editing a file there reaches no agent and is discarded by the next export.
`./scripts/export-angel-skills` refreshes it, gated on the volume UUID
through the same resolver ingestion and backup use.

It does **not** delete. A file present in the export but no longer in Git is
listed in `MANIFEST.yaml` under `stale_files` and reported on stdout, for a
human to reconcile. Removing files from a volume that is somebody's primary
copy is not a script's decision.

## Where candidates are actually written

`tools/social_video_learn.py` writes candidate skills through
`resolve_candidates_root()`, in this order:

1. an explicit `--candidates-root`;
2. **Angel `40-ai/skills/candidates/`**, when the configured volume UUID is
   mounted and writable;
3. the gitignored Mac-local fallback `.state/social-video/skill-candidates/`.

Before this, the default was the relative path `knowledge/skill-candidates`.
Nothing leaked into Git — `knowledge/` is gitignored — but a relative default
means the write target is "wherever the command happened to be invoked from",
which for a repo-rooted invocation is inside the working tree. Both
destinations are now explicit, and neither is under `skills/`.

The volume check here is deliberately **best-effort**, which is a different
choice from model weights. D-037 makes an absent Angel fail closed for model
caches, because a silent internal-SSD fallback would defeat the point of the
bulk tier. A candidate skill is a few kilobytes of text, the pipeline is
worth running with the drive unplugged, and the fallback is still outside
`skills/` and outside Git — so the trust boundary holds either way. What is
not optional is that neither destination is ever the trusted tree.

`skills/candidates/` stays in `.gitignore` as a tripwire: it is the path an
earlier design would have used, and the ignore rule means an accidental
write there can never be committed.

## Knowledge is reference data

`50-research/knowledge/` holds durable, non-executable reference material:
digests, transcripts, claims, extracted recommendations, and exports of the
Mac-local gitignored `knowledge/` tree.

Under D-013, anything an agent consumes from there arrives as delimited,
quoted context carrying an explicit `untrusted` label. A digest that says
"run this command" produces a string in a prompt and nothing else. This is
the same stance the evidence layer takes (`docs/17-file-evidence.md`): a PDF
whose text reads *"IGNORE ALL PREVIOUS INSTRUCTIONS"* produces a database row.

`tools/social_video_learn.py` also does its own regex-based safety
classification, and `docs/12-video-learning-pipeline.md` is honest about what
that is worth: two of five test reels were labelled unsafe on false
positives, and the label only suppresses candidate generation. **Treat it as
a hint, not a control.** The real control is that nothing derived from a
video is ever executed or loaded as instructions — which is a property of
where the files live and what reads them, not of a regex.

## How a candidate graduates

No automated promotion exists, and copying a directory is not promotion.

1. The pipeline writes `40-ai/skills/candidates/<name>/`. Nothing loads it.
2. A human reads it as **source material** — the way they would read a blog
   post — not as steps to install.
3. If it is worth keeping, the human **writes a new skill** in Git `skills/`:
   their own words, this system's paths and invariants, its own frontmatter.
   Anything that looked like a command in the candidate is verified by hand
   before it appears.
4. Pull request, reviewed against the criteria in `skills/README.md`.
5. After merge, `./scripts/export-angel-skills` refreshes the Angel copy and
   its manifest.

Step 3 is the one that matters. A candidate is evidence that a technique
exists. It is not a draft of a trusted instruction.

## Making the boundary visible

- Git: `skills/README.md` states that the tree is trusted and executed, what
  may be added, and what may never be.
- Angel: `40-ai/skills/README.md`, `40-ai/skills/candidates/README.md`, and
  `50-research/knowledge/README.md` each say what the directory is and that
  nothing in it is executed. `scripts/bootstrap-t7.sh` writes them.
- `90-system/angel-manifest.yaml` classes the candidate and knowledge trees
  as `untrusted` and the export as `derived`, so the boundary is legible to
  tooling and not only to a reader.

See `DECISIONS.md` D-013 and D-061, and `docs/25-angel-volume-layout.md`.
