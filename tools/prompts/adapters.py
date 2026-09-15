"""Runtime adapters: provider-specific wire format, never semantic content.

docs/20-prompt-management.md section 2 draws this line explicitly: the
runtime adapter converts a rendered, versioned prompt into whatever a given
backend needs, and sampling parameters (`temperature`, `think`, `num_predict`,
`seed`) are a runtime/sampling decision, never prompt content. Today's only
runtime is loopback Ollama (`tools.models.ollama_local.OllamaTextProvider`),
which already accepts a plain string -- so the adapter for it is the identity
function on `RenderedPrompt.text`, kept as a named seam rather than inlined so
a second runtime (a frontier chat-completion call, a future llama.cpp server)
has an obvious place to add a real conversion without touching the renderer.
"""

from __future__ import annotations

from typing import Any

from tools.prompts.renderer import RenderedPrompt


def for_ollama(rendered: RenderedPrompt) -> str:
    """Ollama's `/api/generate` takes one prompt string; nothing to convert."""
    return rendered.text


def call_ollama(provider: Any, rendered: RenderedPrompt, **kwargs: Any) -> dict[str, Any]:
    """Send a rendered prompt through an `OllamaTextProvider`-shaped provider.

    A thin, explicit seam: callers that want to record *which* adapter sent a
    request call this rather than reaching into `rendered.text` themselves,
    so a second adapter can be added without every call site changing.
    """
    return provider.generate(for_ollama(rendered), **kwargs)
