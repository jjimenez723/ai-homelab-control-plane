"""Tiny loopback-only Ollama HTTP client.

The client accepts only loopback HTTP endpoints and exposes no filesystem or
command-execution API. Campaign staging may pull a model, but only because the
caller supplied the exact manifest-approved tag; Ollama itself remains the
owner of model-cache placement.
"""

from __future__ import annotations

import base64
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class OllamaModelRecord:
    name: str
    digest: str | None
    size_bytes: int
    quantization: str | None


@dataclass(frozen=True)
class OllamaRunningModel:
    name: str
    size_bytes: int
    size_vram_bytes: int


class OllamaLocalClient:
    def __init__(self, endpoint: str):
        self.endpoint = endpoint.rstrip("/")
        parsed = urllib.parse.urlparse(self.endpoint)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("Ollama endpoint must be loopback-only HTTP")

    def _json(self, path: str, *, payload: dict[str, Any] | None = None,
              timeout_seconds: int = 30) -> dict[str, Any]:
        data = None if payload is None else json.dumps(payload, separators=(",", ":")).encode("utf-8")
        request = urllib.request.Request(
            self.endpoint + path, data=data,
            headers={"Content-Type": "application/json"} if data is not None else {},
            method="POST" if data is not None else "GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=max(1, timeout_seconds)) as response:
                if not 200 <= response.status < 300:
                    raise ConnectionError(f"Ollama HTTP status {response.status}")
                value = json.loads(response.read(4_000_000).decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                body = exc.read(16_384).decode("utf-8", errors="replace")
            except Exception:  # noqa: BLE001
                body = ""
            raise ConnectionError(f"Ollama HTTP {exc.code}: {body or exc.reason}") from exc
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, TimeoutError):
                raise TimeoutError("Ollama request timed out") from exc
            raise ConnectionError(f"Ollama unavailable: {exc.reason}") from exc
        if not isinstance(value, dict):
            raise ValueError("Ollama response is not an object")
        return value

    def available(self) -> bool:
        try:
            self._json("/api/tags", timeout_seconds=2)
            return True
        except (ConnectionError, TimeoutError, ValueError):
            return False

    def list_models(self) -> tuple[OllamaModelRecord, ...]:
        outer = self._json("/api/tags", timeout_seconds=5)
        raw_models = outer.get("models")
        if not isinstance(raw_models, list):
            raise ValueError("Ollama /api/tags response has no models list")
        records: list[OllamaModelRecord] = []
        for raw in raw_models:
            if not isinstance(raw, dict):
                continue
            name = raw.get("name") or raw.get("model")
            if not isinstance(name, str) or not name:
                continue
            details = raw.get("details") if isinstance(raw.get("details"), dict) else {}
            records.append(OllamaModelRecord(
                name=name,
                digest=str(raw.get("digest")) if raw.get("digest") else None,
                size_bytes=max(0, int(raw.get("size") or 0)),
                quantization=(str(details.get("quantization_level"))
                              if details.get("quantization_level") else None),
            ))
        return tuple(records)

    def installed_model(self, model: str) -> OllamaModelRecord | None:
        for record in self.list_models():
            if record.name == model:
                return record
        return None

    def pull_model(self, model: str) -> OllamaModelRecord:
        """Pull one explicitly requested tag through Ollama, streaming progress.

        There is intentionally no search/discovery here. The exact tag is a
        caller-owned policy input (the campaign manifest).
        """
        payload = json.dumps({"model": model, "stream": True}, separators=(",", ":")).encode("utf-8")
        request = urllib.request.Request(
            self.endpoint + "/api/pull", data=payload,
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=300) as response:
                if not 200 <= response.status < 300:
                    raise ConnectionError(f"Ollama HTTP status {response.status}")
                for raw_line in response:
                    if not raw_line.strip():
                        continue
                    item = json.loads(raw_line.decode("utf-8"))
                    if not isinstance(item, dict):
                        continue
                    if item.get("error"):
                        raise ConnectionError(f"Ollama pull failed: {item['error']}")
        except urllib.error.HTTPError as exc:
            raise ConnectionError(f"Ollama pull HTTP {exc.code}: {exc.reason}") from exc
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, TimeoutError):
                raise TimeoutError("Ollama pull timed out waiting for progress") from exc
            raise ConnectionError(f"Ollama unavailable during pull: {exc.reason}") from exc

        record = self.installed_model(model)
        if record is None:
            raise RuntimeError(f"Ollama reported pull completion but {model!r} is not installed")
        return record

    def running_models(self) -> tuple[OllamaRunningModel, ...]:
        outer = self._json("/api/ps", timeout_seconds=5)
        raw_models = outer.get("models")
        if not isinstance(raw_models, list):
            raise ValueError("Ollama /api/ps response has no models list")
        records: list[OllamaRunningModel] = []
        for raw in raw_models:
            if not isinstance(raw, dict):
                continue
            name = raw.get("name") or raw.get("model")
            if not isinstance(name, str) or not name:
                continue
            records.append(OllamaRunningModel(
                name=name,
                size_bytes=max(0, int(raw.get("size") or 0)),
                size_vram_bytes=max(0, int(raw.get("size_vram") or 0)),
            ))
        return tuple(records)

    def is_loaded(self, model: str) -> bool:
        return any(record.name == model for record in self.running_models())

    def load_model(self, model: str, *, timeout_seconds: int = 600) -> None:
        self._json("/api/generate", payload={
            "model": model, "prompt": "", "stream": False, "keep_alive": -1,
        }, timeout_seconds=timeout_seconds)
        if not self.is_loaded(model):
            raise RuntimeError(f"Ollama did not report {model!r} resident after preload")

    def unload_model(self, model: str, *, timeout_seconds: int = 60) -> None:
        if not self.is_loaded(model):
            return
        self._json("/api/generate", payload={
            "model": model, "prompt": "", "stream": False, "keep_alive": 0,
        }, timeout_seconds=timeout_seconds)
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if not self.is_loaded(model):
                return
            time.sleep(0.25)
        raise RuntimeError(f"Ollama still reports {model!r} resident after unload timeout")

    def generate_json(self, *, model: str, prompt: str, png_bytes: bytes, timeout_seconds: int) -> dict[str, Any]:
        payload = {
            "model": model,
            "prompt": prompt,
            "images": [base64.b64encode(png_bytes).decode("ascii")],
            "stream": False,
            "format": "json",
            "options": {"num_ctx": 4096, "temperature": 0},
        }
        outer = self._json("/api/generate", payload=payload, timeout_seconds=timeout_seconds)
        raw = outer.get("response")
        value = json.loads(raw) if isinstance(raw, str) else raw
        if not isinstance(value, dict):
            raise ValueError("Ollama structured response is not an object")
        value["_runtime_model"] = str(outer.get("model", "unknown"))
        return value


THINK_VALUES: tuple[object, ...] = (False, True, "low", "medium", "high")


class OllamaTextProvider:
    """The single loopback Ollama adapter used by evaluation and routing.

    Reasoning models (D-059): Ollama returns a model's reasoning in a separate
    ``thinking`` field and its answer in ``response``, and the two share one
    ``num_predict`` budget. Two consequences this adapter makes explicit rather
    than leaving a caller to discover:

    * ``think`` is passed through verbatim, including Ollama's effort levels
      (``"low"``/``"medium"``/``"high"``), not coerced to a bool. Some models
      cannot have reasoning disabled at all -- measured on this machine,
      Ollama 0.33.3 accepts ``think: false`` for ``gpt-oss:20b`` and reasons
      anyway -- so "we asked it not to think" is never assumed to have worked.
    * ``generate`` returns ``thinking`` and ``thinking_chars`` alongside
      ``output``. Only ``output`` is ever scored; the reasoning channel is
      recorded so a truncation can be attributed to reasoning that consumed
      the whole budget instead of being read as a wrong answer.
    """

    name = "ollama"
    DEFAULT_NUM_PREDICT = 64
    # The smallest output budget a reasoning candidate may be measured at.
    # gate2-validation-001 ran `gpt-oss:20b` at 64 and recorded 95-100%
    # truncation: its reasoning alone spent the entire budget before the
    # answer began. Measured floor, not a guess -- see docs/22.
    REASONING_MIN_NUM_PREDICT = 512

    def __init__(self, endpoint: str, model: str, *,
                 capabilities: tuple[str, ...] = ("text_generation", "structured_output"),
                 num_predict: int = DEFAULT_NUM_PREDICT, num_ctx: int | None = None,
                 seed: int | None = None, think: bool | str | None = False,
                 temperature: float = 0.0, reasoning: bool = False):
        self.client = OllamaLocalClient(endpoint)
        self.model = model
        self.capabilities = capabilities
        self.num_predict = int(num_predict)
        self.num_ctx = num_ctx
        self.seed = seed
        if think not in THINK_VALUES:
            raise ValueError(
                f"think must be one of {THINK_VALUES!r} (Ollama's own values), got {think!r}")
        self.think = think
        self.temperature = temperature
        self.reasoning = bool(reasoning)
        self._managed_keep_alive: int | None = None

    @property
    def endpoint(self) -> str:
        return self.client.endpoint

    def available(self) -> bool:
        """True only when the exact configured model tag is installed locally."""
        try:
            return self.client.installed_model(self.model) is not None
        except (ConnectionError, TimeoutError, ValueError):
            return False

    def ensure_model(self, *, auto_pull: bool) -> tuple[OllamaModelRecord | None, bool]:
        record = self.client.installed_model(self.model)
        if record is not None:
            return record, False
        if not auto_pull:
            return None, False
        return self.client.pull_model(self.model), True

    def running_models(self) -> tuple[OllamaRunningModel, ...]:
        return self.client.running_models()

    def is_loaded(self) -> bool:
        return self.client.is_loaded(self.model)

    def preload(self) -> None:
        self.client.load_model(self.model)
        self._managed_keep_alive = -1

    def unload(self) -> None:
        try:
            self.client.unload_model(self.model)
        finally:
            self._managed_keep_alive = None

    def sampling(self, *, num_predict: int | None = None, context_tokens: int | None = None) -> dict[str, Any]:
        options: dict[str, Any] = {
            "num_ctx": self.num_ctx or context_tokens or 4096,
            "temperature": self.temperature,
            "num_predict": int(num_predict if num_predict is not None else self.num_predict),
        }
        if self.seed is not None:
            options["seed"] = self.seed
        return options

    def generate(self, prompt: str, *, context_tokens: int | None = None, timeout_seconds: int | None = None,
                 num_predict: int | None = None, think: bool | str | None = None,
                 **_unused: Any) -> dict[str, Any]:
        options = self.sampling(num_predict=num_predict, context_tokens=context_tokens)
        body: dict[str, Any] = {
            "model": self.model, "prompt": prompt, "stream": False,
            "think": self.think if think is None else think, "options": options,
        }
        if self._managed_keep_alive is not None:
            body["keep_alive"] = self._managed_keep_alive
        outer = self.client._json(
            "/api/generate", payload=body, timeout_seconds=max(1, timeout_seconds or 120))
        thinking = outer.get("thinking")
        thinking_text = str(thinking) if thinking else ""
        return {
            # Only the answer channel is ever scored. Reasoning is recorded
            # separately so a model that thought its whole budget away is not
            # confused with one that answered wrongly (D-059).
            "output": str(outer.get("response", "")).strip(),
            "thinking": thinking_text,
            "thinking_chars": len(thinking_text),
            "think_requested": body["think"],
            "reasoning_declared": self.reasoning,
            "finish_reason": str(outer.get("done_reason")) if outer.get("done_reason") is not None else None,
            "input_tokens": outer.get("prompt_eval_count"),
            "output_tokens": outer.get("eval_count"),
            "num_predict": options["num_predict"],
            "prefill_seconds": (outer.get("prompt_eval_duration") or 0) / 1e9,
            "generation_seconds": (outer.get("eval_duration") or 0) / 1e9,
            "runtime_model": outer.get("model"),
        }
