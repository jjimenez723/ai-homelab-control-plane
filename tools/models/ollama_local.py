"""Tiny loopback-only Ollama HTTP client.

This sits outside the evidence package on purpose: evidence has a strict
no-network-import boundary.  The client only accepts already-bounded bytes
and exposes no filesystem or command execution API.
"""

from __future__ import annotations

import base64
import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


class OllamaLocalClient:
    def __init__(self, endpoint: str):
        self.endpoint = endpoint.rstrip("/")
        parsed = urllib.parse.urlparse(self.endpoint)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("Ollama endpoint must be loopback-only HTTP")

    def available(self) -> bool:
        try:
            with urllib.request.urlopen(self.endpoint + "/api/tags", timeout=1.5) as response:
                return 200 <= response.status < 300
        except (OSError, urllib.error.URLError):
            return False

    def generate_json(self, *, model: str, prompt: str, png_bytes: bytes, timeout_seconds: int) -> dict[str, Any]:
        payload = json.dumps({
            "model": model,
            "prompt": prompt,
            "images": [base64.b64encode(png_bytes).decode("ascii")],
            "stream": False,
            "format": "json",
            "options": {"num_ctx": 4096, "temperature": 0},
        }, separators=(",", ":")).encode("utf-8")
        request = urllib.request.Request(
            self.endpoint + "/api/generate", data=payload,
            headers={"Content-Type": "application/json"}, method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=max(1, timeout_seconds)) as response:
                if not 200 <= response.status < 300:
                    raise ConnectionError(f"Ollama HTTP status {response.status}")
                outer = json.loads(response.read(2_000_000).decode("utf-8"))
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, TimeoutError):
                raise TimeoutError("Ollama request timed out") from exc
            raise ConnectionError(f"Ollama unavailable: {exc.reason}") from exc
        if not isinstance(outer, dict):
            raise ValueError("Ollama response is not an object")
        raw = outer.get("response")
        value = json.loads(raw) if isinstance(raw, str) else raw
        if not isinstance(value, dict):
            raise ValueError("Ollama structured response is not an object")
        value["_runtime_model"] = str(outer.get("model", "unknown"))
        return value


class OllamaTextProvider:
    """Adapter used by the benchmark/router; model IDs still live in registry data."""
    name = "ollama"

    def __init__(self, endpoint: str, model: str, *, capabilities: tuple[str, ...] = ("text_generation", "structured_output")):
        self.client = OllamaLocalClient(endpoint)
        self.model = model
        self.capabilities = capabilities

    def available(self) -> bool:
        return self.client.available()

    def generate(self, prompt: str, *, context_tokens: int, timeout_seconds: int | None = None,
                 think: bool = False, **_unused: Any) -> dict[str, Any]:
        # Tier 1 structured tasks are short, bounded transformations. Turning
        # off optional thinking prevents reasoning tokens from consuming the
        # deliberately small output cap, and avoids reasoning text contaminating
        # an exact verifier. Callers that measure reasoning may opt in.
        payload = json.dumps({"model": self.model, "prompt": prompt, "stream": False, "think": think,
                              "options": {"num_ctx": context_tokens, "temperature": 0, "num_predict": 64}}, separators=(",", ":")).encode("utf-8")
        request = urllib.request.Request(self.client.endpoint + "/api/generate", data=payload,
                                         headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(request, timeout=max(1, timeout_seconds or 120)) as response:
            outer = json.loads(response.read(2_000_000).decode("utf-8"))
        if not isinstance(outer, dict):
            raise ValueError("Ollama response is not an object")
        return {"output": str(outer.get("response", "")).strip(), "input_tokens": outer.get("prompt_eval_count"),
                "output_tokens": outer.get("eval_count"), "prefill_seconds": (outer.get("prompt_eval_duration") or 0) / 1e9,
                "generation_seconds": (outer.get("eval_duration") or 0) / 1e9,
                "runtime_model": outer.get("model")}
