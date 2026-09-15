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
    """The single loopback Ollama adapter used by the benchmark, the ablation, and the router.

    There used to be two. This one hard-coded ``num_predict=64``, which
    silently truncates any answer longer than a label, so the A->E ablation
    grew a private near-copy of this class purely to raise that ceiling
    (D-046). Two adapters with different sampling is exactly the
    eval/production divergence `docs/20-prompt-management.md` section 4
    exists to forbid: a score difference between them is unattributable
    between the model and the harness.

    Sampling is now per-instance configuration with an explicit default, so
    a caller that needs 512 output tokens asks for 512 rather than forking
    the client. ``think=False`` and ``temperature=0`` remain the defaults
    for bounded structured work -- reasoning tokens otherwise consume the
    output budget and contaminate an exact-match verifier -- and both are
    runtime/sampling decisions, not prompt content (D-049).
    """

    name = "ollama"

    # Enough for a label or a filename. Anything that returns code must say
    # so; see `tools/bench/ablation.py`, which asks for 512.
    DEFAULT_NUM_PREDICT = 64

    def __init__(self, endpoint: str, model: str, *,
                 capabilities: tuple[str, ...] = ("text_generation", "structured_output"),
                 num_predict: int = DEFAULT_NUM_PREDICT, num_ctx: int | None = None,
                 seed: int | None = None, think: bool = False, temperature: float = 0.0):
        self.client = OllamaLocalClient(endpoint)
        self.model = model
        self.capabilities = capabilities
        self.num_predict = int(num_predict)
        self.num_ctx = num_ctx
        self.seed = seed
        self.think = think
        self.temperature = temperature

    @property
    def endpoint(self) -> str:
        return self.client.endpoint

    def available(self) -> bool:
        return self.client.available()

    def sampling(self, *, num_predict: int | None = None, context_tokens: int | None = None) -> dict[str, Any]:
        """The exact sampling record for a call, for the run metadata.

        Returned rather than assembled inline so an evaluation record and
        the request it describes cannot drift apart.
        """
        options: dict[str, Any] = {
            "num_ctx": self.num_ctx or context_tokens or 4096,
            "temperature": self.temperature,
            "num_predict": int(num_predict if num_predict is not None else self.num_predict),
        }
        if self.seed is not None:
            options["seed"] = self.seed
        return options

    def generate(self, prompt: str, *, context_tokens: int | None = None, timeout_seconds: int | None = None,
                 num_predict: int | None = None, think: bool | None = None, **_unused: Any) -> dict[str, Any]:
        options = self.sampling(num_predict=num_predict, context_tokens=context_tokens)
        payload = json.dumps(
            {"model": self.model, "prompt": prompt, "stream": False,
             "think": self.think if think is None else think, "options": options},
            separators=(",", ":"),
        ).encode("utf-8")
        request = urllib.request.Request(
            self.client.endpoint + "/api/generate", data=payload,
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=max(1, timeout_seconds or 120)) as response:
                if not 200 <= response.status < 300:
                    raise ConnectionError(f"Ollama HTTP status {response.status}")
                outer = json.loads(response.read(4_000_000).decode("utf-8"))
        except urllib.error.URLError as exc:
            # A transport failure is a runtime-unavailable event, not a
            # capability result. Classifying it here keeps callers from
            # recording "the model was wrong" when the model never ran.
            if isinstance(exc.reason, TimeoutError):
                raise TimeoutError("Ollama request timed out") from exc
            raise ConnectionError(f"Ollama unavailable: {exc.reason}") from exc
        if not isinstance(outer, dict):
            raise ValueError("Ollama response is not an object")
        return {
            "output": str(outer.get("response", "")).strip(),
            "input_tokens": outer.get("prompt_eval_count"),
            "output_tokens": outer.get("eval_count"),
            "prefill_seconds": (outer.get("prompt_eval_duration") or 0) / 1e9,
            "generation_seconds": (outer.get("eval_duration") or 0) / 1e9,
            "runtime_model": outer.get("model"),
        }
