"""UUID-gated locations for large, regenerable local-AI artifacts.

A Finder label is not an identity: no caller gets an Ollama/Hugging Face/
Whisper cache path until the mounted volume has matched the configured APFS
UUID.  This deliberately reuses the ingest volume resolver and never treats
an existing ``/Volumes/Angel`` directory as permission to write.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from tools.ingest.config import IngestConfig
from tools.ingest.t7 import resolve_t7
from tools.ingest.volumes import VolumeError, VolumeProber


AI_ROOT = Path("40-ai")
CACHE_ROOT = AI_ROOT / "model-cache"


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
        "WHISPER_CACHE_DIR": str(storage.whisper),
    }
