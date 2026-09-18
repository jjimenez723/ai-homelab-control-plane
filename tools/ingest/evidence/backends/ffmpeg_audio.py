"""Decoding a file's audio to an in-memory WAV clip, via the `ffmpeg` CLI.

One of the modules in the evidence package permitted to use `subprocess` --
see `EVIDENCE_SUBPROCESS_JUSTIFICATIONS` in
`tests/ingest/test_mutation_authority.py`, which fails the build for any
module not named there. Split out from `ffmpeg_video.py` rather than added
to it because the two answer different questions with different guards (a
pixel budget versus a byte-and-seconds budget) and a reader looking for
"where does audio come from" should not have to read a frame sampler.

Writes no file, ever. ffmpeg reads the source from its real path on disk
(read-only; the one narrow exception this package makes for ffmpeg, already
documented in `ffmpeg_video.py` -- streaming an arbitrary container format
into ffmpeg's stdin is not practical) and the decoded audio comes back on
**stdout** as a RIFF/WAVE stream: `-f wav -ac 1 -ar 16000 pipe:1`, no `-y`,
no output path, nothing landing in a temp directory.

Three bounds, all before anything is handed to an engine:

* `-t <max_seconds>` caps the *decode* at ffmpeg's own layer, so the
  subprocess never produces more than roughly `32 KB/s x max_seconds`.
* `MAX_AUDIO_WAV_BYTES` is the hard ceiling that does not depend on ffmpeg
  honouring `-t`; output past it is refused rather than parsed.
* A silent track is reported as `no_audio` and no clip is produced at all,
  because a speech engine run over silence does not return silence -- it
  returns a confident hallucination, which is worse than no evidence.

Note the WAV header: written to a pipe, ffmpeg cannot seek back to fill in
the RIFF and `data` chunk sizes, so it emits the placeholder `0xffffffff`.
That is a valid streaming WAV and whisper.cpp reads it correctly (verified
on this machine, whisper.cpp 1.9.4). The clip's real duration is therefore
computed from the PCM payload this module locates itself, never from the
header's size fields.
"""

from __future__ import annotations

import shutil
import struct
import subprocess
import sys
from array import array
from pathlib import Path

from tools.ingest.evidence.types import (
    WARN_BACKEND_UNAVAILABLE,
    WARN_MALFORMED,
    WARN_NO_AUDIO,
    WARN_TIMEOUT,
    WARN_TOO_LARGE,
    WARN_TRUNCATED,
    AudioClip,
    EvidenceWarning,
)

_STAGE = "audio"
_VERSION_PROBE_TIMEOUT_SECONDS = 10

# What every whisper-family engine wants, normalized once here so no backend
# has to resample and no two backends can disagree about what they were given.
SAMPLE_RATE = 16_000
CHANNELS = 1
BYTES_PER_SAMPLE = 2

# 16 kHz mono 16-bit PCM is 32 KB per second, so this is a little over an
# hour of audio -- comfortably above the 600-second default cap, and a hard
# stop that does not depend on ffmpeg having honoured `-t`.
MAX_AUDIO_WAV_BYTES = 128_000_000

# Below this peak sample (of 32767, so roughly -54 dBFS) there is no signal
# worth transcribing. A measured threshold, not a judgment about content: a
# genuinely quiet recording still peaks far above it, while a muted track,
# a black-video export, and ffmpeg's `anullsrc` all sit at or near zero.
SILENCE_PEAK_THRESHOLD = 64


def ffmpeg_version() -> str | None:
    """The installed ffmpeg's version string, or None if unusable.

    Delegates to `ffmpeg_video.ffmpeg_version`, which holds the single
    per-process cache -- deliberately not cached again here: two caches over
    one fact is two things to invalidate, and "is ffmpeg installed" is one
    fact about the machine, not one per module. Kept as a named function so
    tests can patch *this* module's `available()` the way they patch the
    video module's.
    """
    from tools.ingest.evidence.backends.ffmpeg_video import ffmpeg_version as detect

    return detect()


def available() -> bool:
    return shutil.which("ffmpeg") is not None and ffmpeg_version() is not None


def extract_audio_clip(
    path: Path,
    *,
    source_sha256: str,
    max_seconds: int,
    timeout_seconds: int,
    source_duration_seconds: float | None = None,
) -> tuple[AudioClip | None, tuple[EvidenceWarning, ...]]:
    """The first `max_seconds` of `path`'s audio, as an in-memory WAV clip.

    Returns `(None, warnings)` for every unhappy case -- no audio stream, a
    silent track, ffmpeg absent, a timeout, a corrupt container -- and never
    raises. A `None` clip with a `no_audio` warning is an *answer* about the
    file; a `None` clip with `backend_unavailable` is an answer about this
    machine, and the two are deliberately different codes.
    """
    if not available():
        return None, (
            EvidenceWarning(
                code=WARN_BACKEND_UNAVAILABLE, stage=_STAGE, detail="ffmpeg binary not found on PATH"
            ),
        )

    seconds = max(1, int(max_seconds))
    command = [
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
        "-i", str(path),
        "-vn",                    # never decode video for this
        "-map", "0:a:0",          # the first audio stream, explicitly
        "-t", f"{seconds}",
        "-ac", str(CHANNELS), "-ar", str(SAMPLE_RATE), "-c:a", "pcm_s16le",
        "-f", "wav", "pipe:1",
    ]
    try:
        completed = subprocess.run(command, capture_output=True, timeout=timeout_seconds, check=False)
    except subprocess.TimeoutExpired:
        return None, (
            EvidenceWarning(
                code=WARN_TIMEOUT, stage=_STAGE, detail=f"ffmpeg timed out after {timeout_seconds}s"
            ),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return None, (
            EvidenceWarning(
                code=WARN_BACKEND_UNAVAILABLE,
                stage=_STAGE,
                detail=f"ffmpeg could not be executed: {type(exc).__name__}: {exc}",
            ),
        )

    if completed.returncode != 0 or not completed.stdout:
        stderr_text = completed.stderr.decode("utf-8", "replace").strip()
        detail = _last_line(completed.stderr) or f"exit code {completed.returncode}"
        # ffmpeg's own words are the most informative thing available, and a
        # file with no audio stream is by far the most common reason to land
        # here -- so it is reported as a fact about the file (`no_audio`)
        # rather than as a malfunction, with ffmpeg's reason attached.
        #
        # The *whole* stderr is classified, not just the line reported: with
        # `-map 0:a:0` against a video-only file, ffmpeg 8 prints "Stream map
        # '' matches no streams" several lines before the generic "Error
        # opening output files" it exits on, and classifying only the last
        # line would call a perfectly ordinary silent screen recording
        # malformed.
        code = WARN_NO_AUDIO if _looks_like_missing_audio(stderr_text) else WARN_MALFORMED
        return None, (
            EvidenceWarning(code=code, stage=_STAGE, detail=f"ffmpeg produced no audio: {detail}"),
        )

    if len(completed.stdout) > MAX_AUDIO_WAV_BYTES:
        return None, (
            EvidenceWarning(
                code=WARN_TOO_LARGE, stage=_STAGE, detail="ffmpeg audio output exceeded the size guard"
            ),
        )

    wav_bytes = completed.stdout
    payload = _pcm_payload(wav_bytes)
    if payload is None:
        return None, (
            EvidenceWarning(
                code=WARN_MALFORMED, stage=_STAGE, detail="ffmpeg audio output was not a readable WAV stream"
            ),
        )

    sample_count = len(payload) // BYTES_PER_SAMPLE
    duration = sample_count / float(SAMPLE_RATE)
    if sample_count == 0:
        return None, (
            EvidenceWarning(code=WARN_NO_AUDIO, stage=_STAGE, detail="the audio stream decoded to no samples"),
        )

    peak = _peak_amplitude(payload)
    if peak < SILENCE_PEAK_THRESHOLD:
        return None, (
            EvidenceWarning(
                code=WARN_NO_AUDIO,
                stage=_STAGE,
                detail=f"the audio track is silent (peak sample {peak} < {SILENCE_PEAK_THRESHOLD}); "
                       "transcription skipped",
            ),
        )

    warnings: list[EvidenceWarning] = []
    truncated = False
    if source_duration_seconds and source_duration_seconds > duration + 1.0:
        truncated = True
        warnings.append(
            EvidenceWarning(
                code=WARN_TRUNCATED,
                stage=_STAGE,
                detail=f"only the first {duration:.1f}s of {source_duration_seconds:.1f}s was decoded "
                       f"(evidence.transcription.max_audio_seconds={seconds})",
            )
        )

    return (
        AudioClip(
            wav_bytes=wav_bytes,
            duration_seconds=duration,
            source_sha256=source_sha256,
            sample_rate=SAMPLE_RATE,
            channels=CHANNELS,
            peak_amplitude=peak,
            truncated=truncated,
        ),
        tuple(warnings),
    )


# -- WAV reading --------------------------------------------------------------
#
# Deliberately hand-rolled rather than `wave`/`audioop`: `wave` rejects the
# placeholder chunk sizes a non-seekable ffmpeg writes (which is exactly the
# shape this module always receives), and `audioop` is removed in Python 3.13.
# Walking the chunk list is a dozen lines and has no such surprises.


def _pcm_payload(wav_bytes: bytes) -> bytes | None:
    """The `data` chunk's bytes, tolerating a streaming (placeholder) size.

    ffmpeg writing to a pipe cannot seek back to fill in the RIFF and `data`
    sizes, so both may be `0xffffffff`. A declared size is honoured when it
    is plausible; otherwise the rest of the buffer *is* the payload.
    """
    if len(wav_bytes) < 12 or wav_bytes[0:4] != b"RIFF" or wav_bytes[8:12] != b"WAVE":
        return None
    offset = 12
    while offset + 8 <= len(wav_bytes):
        chunk_id = wav_bytes[offset : offset + 4]
        (declared,) = struct.unpack("<I", wav_bytes[offset + 4 : offset + 8])
        body = offset + 8
        if chunk_id == b"data":
            remaining = len(wav_bytes) - body
            size = declared if 0 < declared <= remaining else remaining
            return wav_bytes[body : body + size]
        if declared == 0xFFFFFFFF or body + declared > len(wav_bytes):
            # An unsized non-data chunk cannot be stepped over safely.
            return None
        offset = body + declared + (declared % 2)  # chunks are word-aligned
    return None


def _peak_amplitude(pcm: bytes) -> int:
    """The loudest absolute sample in signed 16-bit little-endian PCM."""
    usable = len(pcm) - (len(pcm) % BYTES_PER_SAMPLE)
    if usable <= 0:
        return 0
    samples = array("h")
    samples.frombytes(pcm[:usable])
    if sys_is_big_endian():
        samples.byteswap()
    return max(abs(min(samples)), abs(max(samples)))


def sys_is_big_endian() -> bool:
    """Whether `array('h')` would misread little-endian PCM on this machine.

    `array.frombytes` uses native byte order; the WAV payload is always
    little-endian. Named rather than inlined so the assumption is visible.
    """
    import sys

    return sys.byteorder == "big"


# Phrases ffmpeg uses when the requested audio stream simply is not there.
# Matched against the whole stderr, case-insensitively, and kept as a list
# because the exact wording has moved between ffmpeg majors.
_MISSING_AUDIO_PHRASES = (
    "matches no streams",
    "does not contain any stream",
    "output file does not contain any stream",
    "could not find codec parameters for stream",
)


def _looks_like_missing_audio(detail: str) -> bool:
    lowered = detail.lower()
    return any(phrase in lowered for phrase in _MISSING_AUDIO_PHRASES)


def _last_line(raw: bytes) -> str:
    text = raw.decode("utf-8", "replace").strip()
    return text.splitlines()[-1] if text else ""
