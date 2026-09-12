"""Router v0: deterministic work first, then the least capable safe backend."""
from __future__ import annotations
import time, uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Callable
from tools.models.registry import ModelRegistry, ModelSpec

TIER_NAMES = {0: "deterministic", 1: "small_local", 2: "capable_local", 3: "frontier"}
PRIVATE = frozenset({"confidential", "local-only", "local_only", "private"})

@dataclass(frozen=True)
class RouteRequest:
    task: str
    task_class: str = "general"
    capabilities: tuple[str, ...] = ("text_generation",)
    privacy: str = "local-only"
    risk: str = "low"
    context_tokens: int = 2048
    preferred_execution: str = "auto"
    verifier: Callable[[Any], bool] | None = None
    deterministic: Callable[[], Any] | None = None
    timeout_seconds: int | None = None
    max_retries: int = 1

@dataclass
class Attempt:
    tier: int; provider: str; model: str; status: str; wall_seconds: float
    verified: bool = False; error: str | None = None; retry: int = 0
    input_tokens: int | None = None; output_tokens: int | None = None
    estimated_cost_usd: float | None = None; estimated_energy_wh: float | None = None
    energy_method: str | None = None

@dataclass
class RouteResult:
    job_id: str; selected_tier: int | None; selection_reason: str; disposition: str
    attempts: list[Attempt] = field(default_factory=list); output: Any = None
    escalation_path: list[int] = field(default_factory=list)

class Router:
    """Policy coordinator with injectable providers; CI never needs a runtime."""
    def __init__(self, providers: dict[Any, Any] | None = None, *, registry: ModelRegistry | None = None,
                 ledger: Any = None, frontier_enabled: bool = False, local_energy_wh_per_second: float = 0.005):
        self.providers, self.registry, self.ledger = providers or {}, registry, ledger
        self.frontier_enabled, self.local_energy_wh_per_second = frontier_enabled, local_energy_wh_per_second

    def _provider_for(self, spec: ModelSpec | None, tier: int) -> Any | None:
        return (self.providers.get(spec.name) or self.providers.get(spec.model_id) or self.providers.get(tier)) if spec else self.providers.get(tier)

    def _candidates(self, request: RouteRequest) -> list[tuple[int, ModelSpec | None, Any]]:
        if self.registry is None:
            return [(tier, None, provider) for tier in (1, 2, 3) if (provider := self._provider_for(None, tier)) is not None]
        choices = []
        for tier, name in list(TIER_NAMES.items())[1:]:
            for spec in self.registry.available(tier=name, required=request.capabilities, context_tokens=request.context_tokens):
                if (provider := self._provider_for(spec, tier)) is not None: choices.append((tier, spec, provider))
        return choices

    @staticmethod
    def _available(provider: Any) -> bool:
        try: return bool(provider.available()) if hasattr(provider, "available") else True
        except Exception: return False

    def _create_job(self, request: RouteRequest, tier: int | None, reason: str) -> str:
        job_id = str(uuid.uuid4())
        if self.ledger is None: return job_id
        return self.ledger.create_job(request.task_class, {"task": request.task, "capabilities": list(request.capabilities), "context_tokens": request.context_tokens}, privacy=request.privacy, risk=request.risk, preferred_execution=request.preferred_execution, selected_tier=TIER_NAMES.get(tier), selection_reason=reason, max_attempts=max(1, request.max_retries + 1), job_id=job_id)

    def _record(self, job_id: str, attempt: Attempt, spec: ModelSpec | None) -> None:
        if self.ledger is None: return
        attempt_id = self.ledger.start_attempt(job_id, tier=TIER_NAMES[attempt.tier], provider=attempt.provider, model=attempt.model, model_version=spec.version if spec else None)
        self.ledger.finish_attempt(attempt_id, status=attempt.status, verifier_result="passed" if attempt.verified else "failed" if attempt.status == "verification_failed" else None, error_reason=attempt.error, input_tokens=attempt.input_tokens, output_tokens=attempt.output_tokens, wall_ms=attempt.wall_seconds * 1000, estimated_cost_usd=attempt.estimated_cost_usd, estimated_energy_wh=attempt.estimated_energy_wh, energy_method=attempt.energy_method)
        self.ledger.add_event(job_id, "attempt_finished", asdict(attempt), attempt_id=attempt_id)

    def _cost(self, spec: ModelSpec | None, inputs: int | None, outputs: int | None) -> float | None:
        if spec is None or spec.local: return 0.0
        if spec.input_price_per_million is None or spec.output_price_per_million is None: return None
        return ((inputs or 0) * spec.input_price_per_million + (outputs or 0) * spec.output_price_per_million) / 1_000_000

    def route(self, request: RouteRequest) -> RouteResult:
        candidates = self._candidates(request)
        frontier_was_configured = any(item[0] == 3 for item in candidates)
        frontier_denied = request.privacy.lower() in PRIVATE or request.preferred_execution == "local"
        if request.preferred_execution == "local": candidates = [x for x in candidates if x[0] < 3]
        if request.preferred_execution != "frontier": candidates = [x for x in candidates if x[0] != 3 or self.frontier_enabled]
        if request.privacy.lower() in PRIVATE: candidates = [x for x in candidates if x[0] != 3]
        tier = 0 if request.deterministic else (candidates[0][0] if candidates else None)
        reason = "deterministic handler supplied" if tier == 0 else "first enabled backend satisfying capability/context/privacy policy" if tier else "no permitted available model configured"
        result = RouteResult(self._create_job(request, tier, reason), tier, reason, "unavailable")
        if request.deterministic:
            started = time.perf_counter()
            try:
                output = request.deterministic(); verified = bool(request.verifier(output)) if request.verifier else True
                attempt = Attempt(0, "deterministic", "builtin", "succeeded" if verified else "verification_failed", time.perf_counter()-started, verified, input_tokens=0, output_tokens=0, estimated_cost_usd=0.0, energy_method="not measured")
            except Exception as exc:
                attempt = Attempt(0, "deterministic", "builtin", "failed", time.perf_counter()-started, error=f"{type(exc).__name__}: {exc}", input_tokens=0, output_tokens=0, estimated_cost_usd=0.0)
            result.attempts.append(attempt); self._record(result.job_id, attempt, None)
            if attempt.verified:
                result.output, result.disposition = output, "success"
                if self.ledger: self.ledger.finish_job(result.job_id, disposition="success")
                return result
            result.escalation_path.append(0)
        for tier, spec, provider in candidates:
            if tier == 3 and not self.frontier_enabled: result.escalation_path.append(tier); continue
            if not self._available(provider):
                result.escalation_path.append(tier)
                if self.ledger: self.ledger.add_event(result.job_id, "backend_unavailable", {"tier": tier, "model": spec.name if spec else getattr(provider, "model", "unknown")})
                continue
            for retry in range(max(0, request.max_retries)+1):
                started = time.perf_counter()
                try:
                    raw = provider.generate(request.task, context_tokens=request.context_tokens, timeout_seconds=request.timeout_seconds)
                    output = raw.get("output", raw.get("response", raw)) if isinstance(raw, dict) else raw
                    verified = bool(request.verifier(output)) if request.verifier else True
                    inputs = raw.get("input_tokens", raw.get("prompt_eval_count")) if isinstance(raw, dict) else None
                    outputs = raw.get("output_tokens", raw.get("eval_count")) if isinstance(raw, dict) else None
                    # Provider counters are preferred; the bounded fallback is
                    # explicitly only an estimate for simple fake/legacy APIs.
                    if inputs is None: inputs = len(request.task.split())
                    if outputs is None: outputs = len(str(output).split())
                    wall = time.perf_counter()-started
                    attempt = Attempt(tier, spec.provider if spec else getattr(provider,"name","provider"), spec.model_id if spec else getattr(provider,"model","unknown"), "succeeded" if verified else "verification_failed", wall, verified=verified, retry=retry, input_tokens=inputs, output_tokens=outputs, estimated_cost_usd=self._cost(spec,inputs,outputs), estimated_energy_wh=wall*self.local_energy_wh_per_second if spec is None or spec.local else None, energy_method="configured estimate; not direct sensor measurement" if spec is None or spec.local else None)
                except Exception as exc:
                    wall = time.perf_counter()-started
                    attempt = Attempt(tier, spec.provider if spec else getattr(provider,"name","provider"), spec.model_id if spec else getattr(provider,"model","unknown"), "failed", wall, error=f"{type(exc).__name__}: {exc}", retry=retry, estimated_cost_usd=self._cost(spec,None,None), estimated_energy_wh=wall*self.local_energy_wh_per_second if spec is None or spec.local else None, energy_method="configured estimate; not direct sensor measurement" if spec is None or spec.local else None)
                result.attempts.append(attempt); self._record(result.job_id, attempt, spec)
                if attempt.verified:
                    result.output, result.disposition = output, "success"
                    if self.ledger: self.ledger.finish_job(result.job_id, disposition="success", escalated=bool(result.escalation_path), escalation_reason=";".join(map(str,result.escalation_path)) or None)
                    return result
                if retry < request.max_retries: continue
            result.escalation_path.append(tier)
        if frontier_denied and frontier_was_configured and 3 not in result.escalation_path:
            # This is an explicit policy outcome, not a silent fallback.
            result.escalation_path.append(3)
        result.disposition = "escalated" if result.attempts else "unavailable"
        if self.ledger: self.ledger.finish_job(result.job_id, disposition=result.disposition, status="unavailable" if not result.attempts else "completed", escalated=bool(result.escalation_path), escalation_reason=";".join(map(str,result.escalation_path)) or None)
        return result
