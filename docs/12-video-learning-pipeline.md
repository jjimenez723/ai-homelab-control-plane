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
- never re-download/re-transcribe an already completed item unless `--force` is requested.

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

Procedural content should be routed to a skill compiler and stored first as a candidate:

```text
skills/candidates/<skill-name>/
```

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
