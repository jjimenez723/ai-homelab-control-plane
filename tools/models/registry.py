"""Validated, data-driven provider/model registry.

Model names belong in configuration.  Routing code should make decisions from
capabilities and policy, never from product-name conditionals.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import yaml


CAPABILITIES = {"text_generation", "structured_output", "vision", "embeddings", "transcription"}
# Public alias for callers that want to type-check capability names without
# coupling themselves to the registry's internal set.
Capability = str
TIERS = {"deterministic", "small_local", "capable_local", "frontier"}


@dataclass(frozen=True)
class ModelSpec:
    name: str
    provider: str
    runtime: str
    model_id: str
    tier: str
    capabilities: frozenset[str]
    context_length: int | None = None
    quantization: str | None = None
    local: bool = True
    enabled: bool = True
    version: str | None = None
    input_price_per_million: float | None = None
    output_price_per_million: float | None = None

    def supports(self, required: Iterable[str], *, context_tokens: int | None = None) -> bool:
        aliases = {"text": "text_generation", "code": "text_generation"}
        required = {aliases.get(value, value) for value in required}
        return required.issubset(self.capabilities) and (
            context_tokens is None or (self.context_length is not None and self.context_length >= context_tokens)
        )


class ModelRegistry:
    def __init__(self, models: Iterable[ModelSpec]):
        self._models = {model.name: model for model in models}
        if len(self._models) == 0:
            raise ValueError("registry must contain at least one model")

    def get(self, name: str) -> ModelSpec:
        try:
            return self._models[name]
        except KeyError as exc:
            raise KeyError(f"unknown model registry entry: {name}") from exc

    def all(self) -> tuple[ModelSpec, ...]:
        return tuple(self._models.values())

    def available(self, *, tier: str | None = None, required: Iterable[str] = (), context_tokens: int | None = None) -> list[ModelSpec]:
        return [m for m in self._models.values() if m.enabled and (tier is None or m.tier == tier) and m.supports(required, context_tokens=context_tokens)]


def _as_bool(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{field} must be a boolean")
    return value


def load_registry(path: Path) -> ModelRegistry:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    entries = raw.get("models")
    if not isinstance(entries, list):
        raise ValueError("registry requires a models list")
    models: list[ModelSpec] = []
    names: set[str] = set()
    for index, item in enumerate(entries):
        if not isinstance(item, dict):
            raise ValueError(f"models[{index}] must be a mapping")
        required = ("name", "provider", "runtime", "model_id", "tier", "capabilities")
        missing = [key for key in required if key not in item]
        if missing:
            raise ValueError(f"models[{index}] missing {', '.join(missing)}")
        name = item["name"]
        if not isinstance(name, str) or not name or name in names:
            raise ValueError(f"models[{index}].name must be unique and non-empty")
        names.add(name)
        tier = item["tier"]
        if tier not in TIERS:
            raise ValueError(f"models[{index}].tier is invalid: {tier!r}")
        capabilities = item["capabilities"]
        if not isinstance(capabilities, list) or not all(isinstance(c, str) and c in CAPABILITIES for c in capabilities):
            raise ValueError(f"models[{index}].capabilities contains an unknown capability")
        context = item.get("context_length")
        if context is not None and (not isinstance(context, int) or context <= 0):
            raise ValueError(f"models[{index}].context_length must be a positive integer")
        models.append(ModelSpec(
            name=name, provider=str(item["provider"]), runtime=str(item["runtime"]), model_id=str(item["model_id"]),
            tier=tier, capabilities=frozenset(capabilities), context_length=context,
            quantization=item.get("quantization"), local=_as_bool(item.get("local", True), f"models[{index}].local"),
            enabled=_as_bool(item.get("enabled", True), f"models[{index}].enabled"), version=item.get("version"),
            input_price_per_million=item.get("input_price_per_million"), output_price_per_million=item.get("output_price_per_million"),
        ))
    return ModelRegistry(models)
