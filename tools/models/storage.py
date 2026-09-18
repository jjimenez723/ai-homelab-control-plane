"""UUID-gated locations for large, regenerable local-AI artifacts.

A Finder label is not an identity: no caller gets an Ollama/Hugging Face/
Whisper cache path until the mounted volume has matched the configured APFS
UUID.  This deliberately reuses the ingest volume resolver and never treats
an existing ``/Volumes/Angel`` directory as permission to write.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from tools.ingest.config import IngestConfig
from tools.ingest.t7 import resolve_t7
from tools.ingest.volumes import VolumeError, VolumeProber


AI_ROOT = Path("40-ai")
CACHE_ROOT = AI_ROOT / "model-cache"

# -- whisper model discovery --------------------------------------------------
#
# Speech transcription (`tools/ingest/evidence/backends/transcription.py`,
# D-058) needs a ggml model file, and the sanctioned home for one is the
# whisper directory this module already defines. The evidence layer must not
# carry a machine path in code, and it must not grow a dependency on the
# volume prober either, so the resolution is expressed here, in two steps a
# caller can use independently:
#
#   * `WHISPER_CACHE_ENV` is the variable `cache_environment()` above already
#     publishes. Reading it is how a process that has been set up for Angel
#     finds the cache without re-probing the volume.
#   * `whisper_model_in(directory)` picks a model out of an already-resolved
#     directory, preferring the smallest sensible English model, and returns
#     None rather than raising when the directory holds none.
#
# Neither function creates, writes, or deletes anything.

WHISPER_CACHE_ENV = "WHISPER_CACHE_DIR"
WHISPER_MODEL_GLOB = "ggml-*.bin"

# Preference order, smallest-and-most-specific first: a base.en model is the
# right default for this corpus (English, CPU/Metal-fast, ~148 MB), and a
# multilingual or larger model is used only when it is what is actually
# present.
WHISPER_MODEL_PREFERENCE = (
    "ggml-base.en.bin",
    "ggml-base.bin",
    "ggml-small.en.bin",
    "ggml-small.bin",
    "ggml-medium.en.bin",
    "ggml-medium.bin",
)


def whisper_model_in(directory: Path) -> Path | None:
    """The preferred ggml whisper model inside `directory`, or None.

    Deterministic: the preference list wins, and anything else present is
    chosen by sorted name so two runs on the same cache never disagree.
    Never raises -- an unreadable or absent directory is simply "no model".
    """
    try:
        if not directory.is_dir():
            return None
        present = {path.name: path for path in directory.glob(WHISPER_MODEL_GLOB) if path.is_file()}
    except OSError:
        return None
    for name in WHISPER_MODEL_PREFERENCE:
        if name in present:
            return present[name]
    for name in sorted(present):
        return present[name]
    return None


def whisper_model_from_environment(env: Mapping[str, str] | None = None) -> Path | None:
    """The preferred whisper model under `WHISPER_CACHE_DIR`, or None.

    The one path-free way for a subprocess-backed backend to find the Angel
    whisper cache: the variable is published by `cache_environment()`, so a
    machine that has been through AI-storage preflight answers and one that
    has not simply does not.
    """
    source = env if env is not None else os.environ
    raw = (source.get(WHISPER_CACHE_ENV) or "").strip()
    if not raw:
        return None
    return whisper_model_in(Path(raw).expanduser())


@dataclass(frozen=True)
class AIStorage:
    """Verified paths for regenerable AI artifacts, all below the T7 root."""

    root: Path
    volume_uuid: str
    ai_root: Path
    cache_root: Path
    ollama: Path
    huggingface: Path
    mlx: Path
    whisper: Path
    datasets: Path
    evaluation: Path
    indexes: Path
    manifests: Path


def resolve_ai_storage(config: IngestConfig, prober: VolumeProber, *, create: bool = False) -> AIStorage:
    """Return paths only when the configured, writable T7 is mounted.

    ``create=False`` is a read-only preflight and requires the complete cache
    tree. ``create=True`` is the sole setup mutation: it creates only the
    documented AI directories below an already UUID-checked mounted root.
    Neither mode has an internal-SSD or name-based fallback.
    """
    resolved = resolve_t7(config, prober, for_apply=False)
    if not resolved.volume_uuid:
        raise VolumeError("AI storage requires a UUID-verified T7 volume")
    ai_root = resolved.root / AI_ROOT
    cache_root = resolved.root / CACHE_ROOT
    paths = {
        "ollama": cache_root / "ollama",
        "huggingface": cache_root / "huggingface",
        "mlx": cache_root / "mlx",
        "whisper": cache_root / "whisper",
        "datasets": ai_root / "datasets",
        "evaluation": ai_root / "evaluation",
        "indexes": ai_root / "indexes",
        "manifests": ai_root / "manifests",
    }
    required = (ai_root, cache_root, *paths.values())
    if create:
        for path in required:
            path.mkdir(parents=True, exist_ok=True)
    else:
        missing = [str(path.relative_to(resolved.root)) for path in required if not path.is_dir()]
        if missing:
            raise VolumeError(
                "verified T7 AI storage is not initialized; refusing runtime cache fallback "
                f"(missing: {', '.join(missing)})"
            )
    return AIStorage(root=resolved.root, volume_uuid=resolved.volume_uuid, ai_root=ai_root, cache_root=cache_root, **paths)


def ollama_environment(storage: AIStorage) -> Mapping[str, str]:
    """The supported Ollama placement setting, after preflight succeeds."""
    return {"OLLAMA_MODELS": str(storage.ollama)}


def cache_environment(storage: AIStorage) -> Mapping[str, str]:
    """Conservative cache roots for runtimes that honor standard variables."""
    return {
        "OLLAMA_MODELS": str(storage.ollama),
        "HF_HOME": str(storage.huggingface),
        "MLX_HOME": str(storage.mlx),
        "XDG_CACHE_HOME": str(storage.mlx),
        WHISPER_CACHE_ENV: str(storage.whisper),
    }
