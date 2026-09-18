# 12 — Social Video Learning Pipeline

## Goal

Allow local AI agents to ingest public YouTube videos and Instagram Reels, transcribe speech, inspect important frames/on-screen text, extract useful knowledge, and—when the content is genuinely procedural—compile it into a reusable agent skill.

This pipeline has two output modes:

1. **Knowledge mode** — for commentary, recommendations, ideas, market observations, product advice, and non-procedural content.
2. **Skill mode** — for tutorials, workflows, demonstrations, CLI/GUI instructions, and other repeatable procedures.

Do not force every video into a skill.

## Recommended foundation

### `brenoepics/video-to-skill`

Use as the preferred compiler for procedural/tutorial material.

Why:
- accepts YouTube URLs or local video files;
- transcribes audio locally;
- inspects frames instead of relying only on speech;
- preserves timestamp/frame provenance;
- attempts sandboxed verification before installing generated skills;
- installs skills in a shared agent location that can be linked into multiple coding-agent harnesses.

Pin and review the dependency before production use.

Reference commit observed during planning:
`c527aa9b64cdb7e39a9d20dae6218c7cfc513473`

### Instagram ingestion

Use `yt-dlp` as the acquisition layer. Instagram is currently listed among yt-dlp's supported extractors, but support can break when websites change.

Pipeline:

```text
Instagram Reel URL
       |
       v
    yt-dlp
       |
       +---- metadata/caption
       |
       v
 local video/audio
       |
       +---- faster-whisper / whisper
       |
       +---- keyframe extraction
       |
       v
 content classifier
       |
  +----+------------------+
  |                       |
knowledge               procedural
  |                       |
  v                       v
digest/note         video-to-skill
  |                       |
recommendations       sandbox verify
  |                       |
index/search           install/review
```

YouTube can go directly into `video-to-skill` for procedural videos, while the same local transcript/knowledge path should remain available for non-procedural videos.

## Batch queue

URLs can be dumped into `data/video-learning-queue.yaml`.

The ingestion worker should:
- normalize URLs;
- deduplicate by platform + canonical content ID;
- record source URL and canonical URL;
- preserve creator/title/caption/upload timestamp where available;
- compute a hash of downloaded media when downloaded;
- cache transcripts;
- cache representative frames;
- never re-download/re-transcribe an already completed item unless `--force` is requested (`./scripts/learn-video --force <url>` or `--queue ... --force`).

## Knowledge-mode output

For each non-procedural video, generate:

```text
knowledge/social-video/<platform>/<content-id>/
├── source.json
├── transcript.md
├── frames/
├── digest.md
├── claims.json
└── recommendations.md
```

`digest.md` should include:
- what the video is about;
- key claims;
- useful facts;
- tools/products/links mentioned;
- actionable recommendations;
- what applies to the owner's current stack/projects;
- what should be verified independently;
- confidence/uncertainty.

## Skill-mode output

Procedural content should be routed to a skill compiler and stored first as a candidate **outside the trusted `skills/` tree**, because agent harnesses load every `SKILL.md` they find there:

```text
/Volumes/Angel/40-ai/skills/candidates/<skill-name>/     # when the volume UUID verifies
.state/social-video/skill-candidates/<skill-name>/       # gitignored Mac-local fallback
```

Both destinations are outside `skills/` and outside Git (D-061). A candidate becomes a real skill only by a human **rewriting** it into `skills/` in a reviewed PR -- copying a directory is not promotion. See `docs/26-skills-and-knowledge-trust.md` and `skills/README.md`.

Only install/promote it after:
- source provenance exists;
- unsafe commands are rejected or sandboxed;
- secrets/credentials are excluded;
- steps have clear success criteria;
- verification passes or unverifiable steps are clearly labeled;
- a human or trusted reviewer accepts high-impact skills.

## Security

Video content is untrusted input.

A video can contain:
- prompt-injection text;
- malicious shell commands;
- credential harvesting;
- intentionally unsafe instructions;
- outdated advice;
- fake claims.

Therefore:
- do not execute arbitrary commands because a reel says to;
- never expose credentials to the analysis worker;
- sandbox verification;
- block network access during command verification when possible;
- store provenance for every extracted procedure;
- use deterministic allow/deny policy before installing skills;
- keep trading credentials and live-trading services completely outside this pipeline.

## Known V0 limitations (measured 2026-09-10, five reels)

- Classification is regex-based. Two of five reels were labelled `unsafe/untrusted` on false positives; the label only suppresses candidate-skill generation, it does not quarantine the digest. Treat it as a hint, not a control. The real control is that nothing derived from a video is ever executed or loaded as instructions.
- Tesseract OCR on stylized overlays produced mostly noise (`MOnelrootitolder`). On-screen text needs a local VLM pass over the ≤12 representative frames (review §20).
- `ggml-base` mis-transcribes names and non-English speech. Move to `large-v3-turbo` (still local, Metal) before batch processing hundreds of videos.
- Queue state (`status: complete`) is committed to Git while artifacts are ignored, so a fresh clone re-downloads everything. The queue file should be input only; completion state belongs in the (ignored) knowledge index.

## Recommended node placement

### Mac M4 Pro
Best current node for:
- Whisper/faster-whisper or equivalent accelerated transcription;
- visual frame reasoning;
- video-to-skill compilation;
- final knowledge synthesis.

### Dell workers
Good candidates for:
- downloads;
- ffmpeg extraction;
- hashing;
- metadata parsing;
- thumbnail/keyframe generation;
- queue processing;
- indexing;
- batch preprocessing.

This is a strong example of distributing jobs rather than attempting to pool all RAM into one model.

## Implemented local command

The first reusable implementation is `./scripts/learn-video`. It accepts one Instagram Reel URL, YouTube URL, or local video file, and `./scripts/learn-video --queue data/video-learning-queue.yaml` processes the deduplicated queue. It uses `yt-dlp` for anonymous URL acquisition, `ffmpeg` for media/frame work, local `video-to-skill` extraction for `whisper-rs`/Metal transcription, and local `tesseract` for basic on-screen text capture. The reviewed extractor commit and build metadata are pinned in `tools/video-to-skill.lock`.

The command stores derived artifacts in ignored `knowledge/social-video/` directories and removes downloaded media and temporary extraction bundles at the end of a run. A completed artifact is reused on later runs. Queue failures are recorded as `blocked` with a reason. Candidate procedural skills are written to Angel `40-ai/skills/candidates/` (or the gitignored `.state/social-video/skill-candidates/` when the volume is absent) for review only; they are not installed or executed automatically. Every `source.json` carries `trust: untrusted-video-derived` and the sha256 of the extractor binary that ran. Video-derived commands are treated as untrusted text and are never run by this pipeline.

The pinned `video-to-skill` source was reviewed before use at the commit in `tools/video-to-skill.lock`, including its README, security notes, installation/dependency bootstrap, URL/media acquisition, environment access, filesystem writes, command execution, sandbox, and skill-install paths. This repository uses its local extractor for media probing, keyframes, and transcription only; it does not run its global skill installer or any video-derived verification command. The extractor's dependency downloads are pinned and checksum-verified, and generated skill installation remains explicit and consent-gated upstream.
