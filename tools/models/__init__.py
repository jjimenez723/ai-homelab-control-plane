"""Provider/model registry used by local routing and benchmarking."""

from .registry import Capability, ModelRegistry, ModelSpec, load_registry
from .ollama_local import OllamaTextProvider

__all__ = ["Capability", "ModelRegistry", "ModelSpec", "OllamaTextProvider", "load_registry"]
