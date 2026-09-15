"""Model configuration: what exists, what it may be asked to do, what it has been measured doing.

Three separate questions, kept separate on purpose:

* `registry` — which model/runtime configurations exist and what *shape* of
  work they declare they can take. A declared capability is a claim, not
  permission.
* `task_classes` — the join between a production task class, the
  capabilities it requires, and the ACB-v1 suite that gates it. Without
  this, `task_class` is a label nothing reads.
* `certification` — which configurations have been *measured* doing a
  capability well enough to be routed it, pinned to the exact runtime,
  quantization, prompt version and tool-schema version that was measured
  (D-042, D-044, D-051).
"""

from .certification import (
    Certification,
    CertificationRegistry,
    Ineligible,
    empty_registry,
    load_certifications,
)
from .ollama_local import OllamaLocalClient, OllamaTextProvider
from .registry import Capability, ModelRegistry, ModelSpec, load_registry
from .storage import AIStorage, cache_environment, ollama_environment, resolve_ai_storage
from .task_classes import (
    TASK_CLASSES,
    TaskClass,
    UnknownTaskClass,
    capabilities_for,
    certifiable_task_classes,
    requires_certification,
    suite_for,
    task_class,
)

__all__ = [
    "AIStorage",
    "Capability",
    "Certification",
    "CertificationRegistry",
    "Ineligible",
    "ModelRegistry",
    "ModelSpec",
    "OllamaLocalClient",
    "OllamaTextProvider",
    "TASK_CLASSES",
    "TaskClass",
    "UnknownTaskClass",
    "cache_environment",
    "capabilities_for",
    "certifiable_task_classes",
    "empty_registry",
    "load_certifications",
    "load_registry",
    "ollama_environment",
    "requires_certification",
    "resolve_ai_storage",
    "suite_for",
    "task_class",
]
