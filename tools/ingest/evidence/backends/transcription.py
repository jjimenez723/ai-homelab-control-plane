"""Speech to text: the third perception seam, and its two implementations.

OCR answers "what text is visibly present?". Vision answers "what am I
looking at?". This answers **"what is being said?"** -- a third, genuinely
different question, given its own `Protocol` for exactly the reason
`backends/__init__.py` gives three seams rather than one god-model
interface: a speech engine's failure modes (hallucinated text over silence,
a wrong language, a clipped tail) look nothing like an OCR engine's, and
folding them together would mean a reader could not tell "this file *says*
invoice" from "this file *shows* the word invoice" (D-058).

What the seam is handed
-----------------------

An `AudioClip`: in-memory 16 kHz mono PCM WAV **bytes**, a duration, and the
SHA-256 of the original file. **No path, no file handle** -- the same
structural argument that makes `RasterImage` bytes rather than a path, and
`NamingEvidence.thumbnail_png` bytes rather than a path. A transcription
backend may one day be a model server or someone else's plugin; it is not
trusted and then constrained, it is simply not given the means.

`NullTranscriptionBackend` is the default and is not a placeholder
-----------------------------------------------------------------

It performs no transcription and *records that it did not*, exactly like
`NullOCRBackend`: a warning, a status, and a stable identity to key a cache
against. "We looked and found nothing" and "we never looked" must not be the
same answer.

`WhisperCppBackend` writes no file, ever
----------------------------------------

`whisper-cli` (Homebrew's `whisper.cpp`) is invoked with a fixed argv, no
shell, and a hard timeout. The WAV is fed on **stdin** (`-f -`) and the
result is read back as JSON on **stdout** (`-oj -of -`); no temp file, no
`--output-txt`, no output directory. That combination was verified on this
machine before this module was written (whisper.cpp 1.9.4: `-f -` reports
`read_audio_data: read N bytes from stdin`, and `-of -` redirects the JSON
writer to stdout) -- **including the streaming WAV header** `ffmpeg -f wav
pipe:1` produces, whose RIFF size field is the placeholder `0xffffffff`.

Note the one non-obvious flag: with `-f -` and no `--output-*`, whisper-cli
prints nothing at all to stdout (it derives `--output-file -` from the input
name and then waits for a format to be asked for). `-oj -of -` is therefore
load-bearing, not decoration.

If a future whisper build stops accepting stdin, this backend must report
itself **unavailable** rather than fall back to a temp file: zero filesystem
writes is an invariant of the whole evidence layer
(`tests/ingest/test_mutation_authority.py::EvidenceLayerNeverWritesTests`),
not a preference, and `tesseract.py` carries the identical instruction.

Version identity, so the cache invalidates itself
-------------------------------------------------

`version` folds in the detected `whisper-cli` version, and the cache key
additionally carries `model`/`model_version` (the model file's name and its
size in bytes -- a stable, cheap identity; re-hashing 148 MB on every file
would cost more than the transcription it is protecting). Upgrading the
binary or swapping the model therefore misses the `transcript` rows and
leaves every other kind of evidence for the same bytes hitting.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Any

from tools.ingest.config import TranscriptionConfig
from tools.ingest.evidence.backends import TranscriptionResult
from tools.ingest.evidence.types import (
    MAX_TRANSCRIPT_SEGMENTS,
    WARN_BACKEND_UNAVAILABLE,
    WARN_MALFORMED,
    WARN_TIMEOUT,
    WARN_TRUNCATED,
    AudioClip,
    EvidenceWarning,
    TranscriptEvidence,
    TranscriptSegment,
    sanitize_extracted_text,
)

_STAGE = "transcript"

# Short: this only ever runs `whisper-cli --version`, which loads no model.
_VERSION_PROBE_TIMEOUT_SECONDS = 15

# whisper-cli's `-ojf` JSON carries one object per decoded token. A ten-minute
# transcript is a few hundred kilobytes of it; anything past this cap is a
# malfunctioning engine rather than speech, and is refused rather than parsed.
MAX_STDOUT_BYTES = 32_000_000

# Per-segment text cap. A "segment" is a phrase; one claiming to be longer
# than this is not a phrase.
_MAX_SEGMENT_CHARS = 4_000

# whisper's special tokens appear in the `-ojf` token stream and carry
# probabilities that say nothing about how well speech was recognized.
_SPECIAL_TOKEN_PREFIX = "[_"

_BINARY = "whisper-cli"


@lru_cache(maxsize=1)
def whisper_cli_version() -> str | None:
    """The installed whisper-cli's version string, or None if unusable.

    Cached for the life of the process, matching
    `tesseract.py::tesseract_version` and `ffmpeg_video.py::ffmpeg_version`:
    a batch run transcribes many files, and "is whisper installed" cannot
    change partway through one run. Tests patch this function rather than the
    environment.
    """
    if shutil.which(_BINARY) is None:
        return None
    try:
        completed = subprocess.run(
            [_BINARY, "--version"],
            capture_output=True,
            timeout=_VERSION_PROBE_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    text = (completed.stdout or completed.stderr).decode("utf-8", "replace")
    # whisper.cpp prints backend-loading chatter around the version line, so
    # the line is looked up rather than assumed to be first.
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.lower().startswith("whisper.cpp version:"):
            candidate = stripped.split(":", 1)[1].strip()
            if candidate:
                return candidate
    stripped_text = text.strip()
    # Fall back to *a* distinct string rather than None, so an unexpected
    # banner still cache-busts instead of masquerading as "absent".
    return stripped_text.splitlines()[0] if stripped_text else None


def resolve_model_path(config: TranscriptionConfig) -> Path | None:
    """The whisper model this configuration means, or None if there is none.

    An explicit `evidence.transcription.model_path` always wins. Otherwise
    the Angel whisper cache is consulted through `tools/models/storage.py`
    (`WHISPER_CACHE_DIR`, the variable its `cache_environment()` publishes),
    which is what keeps a machine-specific path out of this repository: the
    default here is a *policy* ("the sanctioned model cache"), and a machine
    that has not been through AI-storage preflight simply answers None and
    the backend reports itself unavailable.

    Never raises -- including when `tools.models.storage` cannot be imported
    at all, which is the case for any checkout using the evidence layer
    without the model-storage tooling.
    """
    if config.model_path is not None:
        return config.model_path
    try:
        from tools.models.storage import whisper_model_from_environment
    except Exception:  # noqa: BLE001 - optional tooling; absence is an answer
        return None
    try:
        return whisper_model_from_environment()
    except Exception:  # noqa: BLE001 - a resolver must never end a run either
        return None


class NullTranscriptionBackend:
    """The absent speech engine, honestly. See `backends/null.py`.

    The default (`evidence.transcription.backend == "null"`), and the correct
    answer whenever no engine is configured: it performs no transcription and
    records its own absence as a warning plus an `unavailable` status, rather
    than returning nothing and leaving a caller unable to tell "this file has
    no speech in it" apart from "nobody listened".
    """

    name = "null-transcription"
    version = "1"
    model = ""
    model_version = ""

    def available(self) -> bool:
        return False

    def transcribe(self, audio: AudioClip, *, timeout_seconds: int) -> TranscriptionResult:
        return TranscriptionResult(
            transcript=None,
            warnings=(
                EvidenceWarning(
                    code=WARN_BACKEND_UNAVAILABLE,
                    stage=_STAGE,
                    detail="no transcription backend is configured (backend=null)",
                ),
            ),
            status="unavailable",
        )


class WhisperCppBackend:
    """Offline speech-to-text via a locally installed `whisper-cli` binary.

    Needs no Python package, only the system binary and a ggml model file.
    Never raises: a missing binary, a missing model, a timeout, a non-zero
    exit, unparseable JSON, or a hostile payload all become a warning and a
    non-"ok" status.
    """

    name = "whisper-cpp"

    def __init__(self, config: TranscriptionConfig) -> None:
        self._language = (config.language or "en").strip() or "en"
        self._model_path = resolve_model_path(config)

    # -- identity -----------------------------------------------------------

    @property
    def version(self) -> str:
        detected = whisper_cli_version()
        return f"1+{detected}" if detected else "1+absent"

    @property
    def model(self) -> str:
        """The model's file *name*. Deliberately not its path: a cache key is
        published in the ledger and a directory layout is not evidence."""
        return self._model_path.name if self._model_path is not None else ""

    @property
    def model_version(self) -> str:
        """The model file's size in bytes, as its cheap stable identity.

        Re-hashing a 148 MB model for every file would cost more than the
        transcription it protects, while size alone already distinguishes
        every published ggml model from every other one -- and a *changed*
        file of identical size is a corrupted download, which the engine
        itself fails on rather than silently answering differently.
        """
        if self._model_path is None:
            return ""
        try:
            return str(self._model_path.stat().st_size)
        except OSError:
            return ""

    def available(self) -> bool:
        if whisper_cli_version() is None:
            return False
        if self._model_path is None:
            return False
        try:
            return self._model_path.is_file()
        except OSError:
            return False

    # -- the call -----------------------------------------------------------

    def transcribe(self, audio: AudioClip, *, timeout_seconds: int) -> TranscriptionResult:
        if not self.available():
            detail = (
                "whisper-cli binary not found on PATH"
                if whisper_cli_version() is None
                else "no whisper ggml model is available "
                     "(set evidence.transcription.model_path, or populate the Angel whisper cache)"
            )
            return _unavailable(detail)

        # Fixed argv. Nothing here is derived from file content: the model
        # path comes from configuration, the language from configuration, and
        # the audio itself goes on stdin as bytes.
        command = [
            _BINARY,
            "-m", str(self._model_path),
            "-l", self._language,
            "-np",          # no banners on stdout; results only
            "-nt",          # timestamps come from the JSON, not the text
            "-ojf", "-of", "-",  # full JSON, written to stdout rather than a file
            "-f", "-",      # read the WAV from stdin
        ]
        try:
            completed = subprocess.run(
                command,
                input=audio.wav_bytes,
                capture_output=True,
                timeout=timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return _failed(WARN_TIMEOUT, f"whisper-cli timed out after {timeout_seconds}s")
        except (OSError, subprocess.SubprocessError) as exc:
            return _unavailable(f"whisper-cli could not be executed: {type(exc).__name__}: {exc}")

        if completed.returncode != 0:
            return _failed(
                WARN_MALFORMED,
                f"whisper-cli exited {completed.returncode}: {_first_line(completed.stderr)}",
            )
        if len(completed.stdout) > MAX_STDOUT_BYTES:
            return _failed(WARN_MALFORMED, "whisper-cli output exceeded the size guard")
        if not completed.stdout.strip():
            # A clip with nothing recognizable in it. Not a failure: an empty
            # transcript over real audio is a legitimate observation, and is
            # recorded as such rather than as an engine fault.
            return TranscriptionResult(
                transcript=self._empty_transcript(audio),
                warnings=(),
                status="ok",
            )

        try:
            payload = json.loads(completed.stdout.decode("utf-8", "replace"))
        except (json.JSONDecodeError, ValueError) as exc:
            return _failed(WARN_MALFORMED, f"whisper-cli output was not valid json: {exc}")

        try:
            return self._from_payload(payload, audio)
        except Exception as exc:  # noqa: BLE001 - never raise out of a backend
            return _failed(
                WARN_MALFORMED,
                f"whisper-cli json could not be interpreted: {type(exc).__name__}: {exc}",
            )

    # -- parsing ------------------------------------------------------------

    def _empty_transcript(self, audio: AudioClip) -> TranscriptEvidence:
        return TranscriptEvidence(
            text="",
            language=self._language,
            segments=(),
            engine=self.name,
            engine_version=self.version,
            model=self.model,
            duration_covered_seconds=float(audio.duration_seconds),
            truncated=bool(audio.truncated),
            confidence=0.0,
        )

    def _from_payload(self, payload: Any, audio: AudioClip) -> TranscriptionResult:
        """Turn whisper-cli's JSON into bounded, sanitized evidence.

        Everything in the payload is untrusted: it is a serialization of what
        an engine believes it heard in a file the owner did not write. Text is
        sanitized and capped, segment counts are bounded, timestamps are
        coerced to finite non-negative floats, and a payload shape that does
        not match is a `malformed` warning rather than an exception.
        """
        warnings: list[EvidenceWarning] = []
        if not isinstance(payload, dict):
            return _failed(WARN_MALFORMED, "whisper-cli json was not an object")

        language = self._language
        result = payload.get("result")
        if isinstance(result, dict):
            reported = str(result.get("language") or "").strip()
            if reported and len(reported) <= 16:
                language = reported

        rows = payload.get("transcription")
        if not isinstance(rows, list):
            return _failed(WARN_MALFORMED, "whisper-cli json carried no transcription array")

        truncated_segments = False
        if len(rows) > MAX_TRANSCRIPT_SEGMENTS:
            rows = rows[:MAX_TRANSCRIPT_SEGMENTS]
            truncated_segments = True

        segments: list[TranscriptSegment] = []
        probabilities: list[float] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            text, _ = sanitize_extracted_text(str(row.get("text") or ""), max_chars=_MAX_SEGMENT_CHARS)
            text = text.strip()
            offsets = row.get("offsets") if isinstance(row.get("offsets"), dict) else {}
            start = _seconds(offsets.get("from"))
            end = _seconds(offsets.get("to"))
            if end < start:
                end = start
            probabilities.extend(_token_probabilities(row.get("tokens")))
            if not text:
                continue
            segments.append(TranscriptSegment(start_seconds=start, end_seconds=end, text=text))

        if truncated_segments:
            warnings.append(
                EvidenceWarning(
                    code=WARN_TRUNCATED,
                    stage=_STAGE,
                    detail=f"transcript segments capped at {MAX_TRANSCRIPT_SEGMENTS}",
                )
            )

        text = " ".join(segment.text for segment in segments).strip()
        confidence = (sum(probabilities) / len(probabilities)) if probabilities else 0.0
        covered = max((segment.end_seconds for segment in segments), default=0.0)
        return TranscriptionResult(
            transcript=TranscriptEvidence(
                text=text,
                language=language,
                segments=tuple(segments),
                engine=self.name,
                engine_version=self.version,
                model=self.model,
                # The clip's own duration is the honest answer for "how much
                # audio was listened to": the last segment's end time is
                # where speech stopped, not where listening stopped.
                duration_covered_seconds=float(audio.duration_seconds) or covered,
                truncated=bool(audio.truncated) or truncated_segments,
                confidence=max(0.0, min(1.0, confidence)),
            ),
            warnings=tuple(warnings),
            status="partial" if truncated_segments else "ok",
        )


# -- helpers ------------------------------------------------------------------


def _token_probabilities(tokens: Any) -> list[float]:
    """Per-token probabilities from `-ojf`, ignoring whisper's special tokens."""
    if not isinstance(tokens, list):
        return []
    values: list[float] = []
    for token in tokens:
        if not isinstance(token, dict):
            continue
        if str(token.get("text") or "").startswith(_SPECIAL_TOKEN_PREFIX):
            continue
        try:
            probability = float(token.get("p"))
        except (TypeError, ValueError):
            continue
        if probability != probability:  # NaN
            continue
        values.append(max(0.0, min(1.0, probability)))
    return values


def _seconds(value: Any) -> float:
    """A millisecond offset from whisper's JSON, as non-negative finite seconds."""
    try:
        milliseconds = float(value)
    except (TypeError, ValueError):
        return 0.0
    if milliseconds != milliseconds or milliseconds in (float("inf"), float("-inf")):
        return 0.0
    return max(0.0, milliseconds / 1000.0)


def _first_line(raw: bytes) -> str:
    text = raw.decode("utf-8", "replace").strip()
    return text.splitlines()[-1] if text else ""


def _unavailable(detail: str) -> TranscriptionResult:
    return TranscriptionResult(
        transcript=None,
        warnings=(EvidenceWarning(code=WARN_BACKEND_UNAVAILABLE, stage=_STAGE, detail=detail),),
        status="unavailable",
    )


def _failed(code: str, detail: str) -> TranscriptionResult:
    return TranscriptionResult(
        transcript=None,
        warnings=(EvidenceWarning(code=code, stage=_STAGE, detail=detail),),
        status="failed",
    )
