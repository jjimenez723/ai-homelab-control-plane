# skills/ — the trusted tree

Everything in this directory is **trusted and reviewed**. An agent harness
loads every `SKILL.md` it finds here, without asking, as instructions. That
is the whole reason this directory is small, is in Git, and changes only
through a pull request a human read.

Three skills live here today: `social-video-learner/`,
`ui-design-system-builder/`, and `bitesites-site-layer/`. The BiteSites skill
carries the reusable agency footer attribution and neutral branded-map pattern;
Oasis-specific values are kept only in its optional reference material.

## What may be added

A skill belongs here when all of the following hold:

- a human wrote or rewrote it, in their own words, for this system;
- it was reviewed in a pull request, with the diff read rather than skimmed;
- it names no secret, no credential, and no absolute path from one machine;
- it does not instruct an agent to disable a safety gate, bypass a dry run,
  or write outside the paths `docs/25-angel-volume-layout.md` assigns.

## What may never be added

Generated content. Specifically, anything produced by
`tools/social_video_learn.py` from a video or a web page. That material is
data with an `untrusted` label (D-013), and it has its own homes:

| Artifact | Home | Trust |
|---|---|---|
| Reviewed skills | Git `skills/` (here) | trusted; executed |
| Exported copy of this tree | Angel `40-ai/skills/trusted/` | trusted record; not a load path |
| Candidate skills from video | Angel `40-ai/skills/candidates/` | **untrusted**; never executed |
| Knowledge notes, digests, transcripts | Angel `50-research/knowledge/` | **untrusted** reference data |

`skills/candidates/` is in `.gitignore` as a tripwire: it is the path an
earlier version of the pipeline wrote to, and the ignore rule means an
accidental write there can never be committed. It is not a supported
location, and nothing should create it.

## How a candidate graduates

There is no automated promotion, and copying a directory is not promotion.

1. A candidate appears in Angel `40-ai/skills/candidates/<name>/`, written by
   the video pipeline. It is not loaded by anything.
2. A human reads it as *source material*, the way they would read a blog
   post — never as a set of steps to install.
3. If it is worth keeping, the human **writes a new skill** here: their own
   words, this system's paths and invariants, its own frontmatter. Anything
   that looked like a command in the candidate is verified by hand before it
   appears in a skill.
4. That goes up as a pull request. The review asks the four "what may be
   added" questions above.
5. After merge, `./scripts/export-angel-skills` refreshes the Angel copy and
   its sha256 + git-SHA manifest.

Step 3 is the one that matters. A candidate is evidence that a technique
exists; it is not a draft of a trusted instruction.

See `docs/26-skills-and-knowledge-trust.md` for the boundary in full, and
`docs/12-video-learning-pipeline.md` for what the pipeline actually produces.
