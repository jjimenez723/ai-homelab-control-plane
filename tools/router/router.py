"""Router v1: deterministic work first, then the least capable *certified* backend.

Three gates stand between a request and a model, in this order, and each
one fails closed:

1. **Authorization.** Work above the configured risk ceiling does not run
   without an explicit human authorization token. `PRINCIPLES.md` §4 says
   an LLM response is never itself authorization; this is where that stops
   being a sentence and starts being a branch.
2. **Privacy.** Confidential or local-only work never reaches a frontier
   provider, whatever else is enabled.
3. **Certification.** For a task class an ACB-v1 suite gates, only a
   configuration with a live, matching, measured certification is a
   candidate (D-042, D-044). Declared capability is a claim about shape;
   certification is a claim about behaviour. Routing needs both.

What "fails closed" means concretely: every one of those gates produces an
explicit disposition and a ledger event naming the reason. None of them
falls through to "try it anyway", and none of them is skipped because the
alternative is having no candidate -- having no candidate *is* the
answer, and `escalated`/`unavailable` is how it is reported.

Verification honesty is the fourth property, and it is not a gate but a
vocabulary: `verified` means a deterministic verifier ran and passed.
"Nobody checked" is `completed_unverified`, a distinct attempt status and a
distinct job disposition, recorded as `verifier_result = NULL`. Collapsing
the two -- which this router did before D-048 -- makes unverified model
output indistinguishable from verified correctness in the one telemetry
store that Phase 6 hardware decisions and model certification both read.
"""
from __future__ import annotations

import datetime as dt
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

from tools.models.certification import CertificationRegistry, Ineligible
from tools.models.registry import ModelRegistry, ModelSpec
from tools.models.task_classes import UnknownTaskClass, capabilities_for, requires_certification

TIER_NAMES = {0: "deterministic", 1: "small_local", 2: "capable_local", 3: "frontier"}
PRIVATE = frozenset({"confidential", "local-only", "local_only", "private"})

# Ordered so a ceiling comparison is a comparison, not a lookup table of
# special cases. An unknown risk label sorts above everything known, so a
# typo escalates to "needs a human" rather than silently to "low".
RISK_ORDER = {"low": 0, "medium": 1, "high": 2, "critical": 3}
UNKNOWN_RISK = max(RISK_ORDER.values()) + 1


def risk_rank(risk: str) -> int:
    return RISK_ORDER.get(str(risk).strip().lower(), UNKNOWN_RISK)


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
    # Pinned configuration identity. A certification is a claim about a
    # specific prompt and tool schema (D-042); leaving these unset means the
    # router cannot prove the configuration it is about to run is the one
    # that was measured, and certification-gated work will escalate. That
    # coupling is deliberate: it is what makes "prompts are versioned" a
    # prerequisite for autonomous routing rather than a nice-to-have.
    prompt_version: str | None = None
    tool_schema_version: str | None = None
    # Deterministic authorization for work above the router's risk ceiling.
    # An opaque token supplied by a human-driven caller and recorded in the
    # ledger; the router checks presence, never content.
    human_authorization: str | None = None


@dataclass
class Attempt:
    tier: int; provider: str; model: str; status: str; wall_seconds: float
    verified: bool = False; error: str | None = None; retry: int = 0
    input_tokens: int | None = None; output_tokens: int | None = None
    estimated_cost_usd: float | None = None; estimated_energy_wh: float | None = None
    energy_method: str | None = None
    # Whether a deterministic verifier ran at all. `verified` without this
    # is meaningless, and the two were conflated before D-048.
    verifier_ran: bool = False
    certification_suite: str | None = None


@dataclass
class RouteResult:
    job_id: str; selected_tier: int | None; selection_reason: str; disposition: str
    attempts: list[Attempt] = field(default_factory=list); output: Any = None
    escalation_path: list[int] = field(default_factory=list)
    # Every configuration considered and declined, with the reason. An
    # autonomous router that cannot say why it declined a backend cannot be
    # audited after the fact.
    ineligible: list[Ineligible] = field(default_factory=list)

    @property
    def verified(self) -> bool:
        return any(attempt.verified for attempt in self.attempts)


class Router:
    """Policy coordinator with injectable providers; CI never needs a runtime."""

    def __init__(self, providers: dict[Any, Any] | None = None, *, registry: ModelRegistry | None = None,
                 ledger: Any = None, frontier_enabled: bool = False, local_energy_wh_per_second: float = 0.005,
                 certifications: CertificationRegistry | None = None,
                 autonomous_risk_ceiling: str = "medium", today: dt.date | None = None):
        self.providers, self.registry, self.ledger = providers or {}, registry, ledger
        self.frontier_enabled, self.local_energy_wh_per_second = frontier_enabled, local_energy_wh_per_second
        self.certifications = certifications
        self.autonomous_risk_ceiling = autonomous_risk_ceiling
        self._today = today

    # ---- candidate selection -------------------------------------------------

    def _provider_for(self, spec: ModelSpec | None, tier: int) -> Any | None:
        return (self.providers.get(spec.name) or self.providers.get(spec.model_id) or self.providers.get(tier)) if spec else self.providers.get(tier)

    def _required_capabilities(self, request: RouteRequest) -> tuple[str, ...]:
        """Declared requirements plus whatever the task class implies.

        The union, not a replacement: a caller may still ask for more than
        the task class requires, and a task class may require something the
        caller forgot (the `code_generation` case that the registry used to
        alias away into `text_generation`).
        """
        return tuple(sorted(set(request.capabilities) | set(capabilities_for(request.task_class))))

    def _certification_gate(self, request: RouteRequest, spec: ModelSpec | None,
                            result_sink: list[Ineligible]) -> tuple[bool, str | None]:
        """Whether this spec may serve this task class, and under which suite."""
        if not requires_certification(request.task_class):
            return True, None
        if spec is None:
            result_sink.append(Ineligible("<unregistered>", request.task_class,
                                          "certification-gated task class needs a registry entry to certify"))
            return False, None
        if self.certifications is None:
            result_sink.append(Ineligible(spec.name, request.task_class,
                                          "no certification registry is configured"))
            return False, None
        record, reason = self.certifications.check(
            model=spec.name, task_class_name=request.task_class, runtime=spec.runtime,
            quantization=spec.quantization, prompt_version=request.prompt_version,
            tool_schema_version=request.tool_schema_version, today=self._today,
        )
        if record is None:
            result_sink.append(reason or Ineligible(spec.name, request.task_class, "not certified"))
            return False, None
        return True, record.suite

    def _candidates(self, request: RouteRequest, ineligible: list[Ineligible]) -> list[tuple[int, ModelSpec | None, Any, str | None]]:
        required = self._required_capabilities(request)
        if self.registry is None:
            choices = []
            for tier in (1, 2, 3):
                provider = self._provider_for(None, tier)
                if provider is None:
                    continue
                permitted, suite = self._certification_gate(request, None, ineligible)
                if permitted:
                    choices.append((tier, None, provider, suite))
            return choices

        choices = []
        for tier, name in list(TIER_NAMES.items())[1:]:
            for spec in self.registry.available(tier=name, required=required, context_tokens=request.context_tokens):
                provider = self._provider_for(spec, tier)
                if provider is None:
                    continue
                permitted, suite = self._certification_gate(request, spec, ineligible)
                if permitted:
                    choices.append((tier, spec, provider, suite))
        return choices

    @staticmethod
    def _available(provider: Any) -> bool:
        try: return bool(provider.available()) if hasattr(provider, "available") else True
        except Exception: return False

    # ---- ledger --------------------------------------------------------------

    def _create_job(self, request: RouteRequest, tier: int | None, reason: str) -> str:
        job_id = str(uuid.uuid4())
        if self.ledger is None: return job_id
        return self.ledger.create_job(
            request.task_class,
            {"task": request.task, "capabilities": list(request.capabilities),
             "context_tokens": request.context_tokens,
             "prompt_version": request.prompt_version,
             "tool_schema_version": request.tool_schema_version,
             "human_authorization_present": request.human_authorization is not None},
            privacy=request.privacy, risk=request.risk, preferred_execution=request.preferred_execution,
            selected_tier=TIER_NAMES.get(tier), selection_reason=reason,
            max_attempts=max(1, request.max_retries + 1), job_id=job_id)

    def _record(self, job_id: str, attempt: Attempt, spec: ModelSpec | None) -> None:
        if self.ledger is None: return
        attempt_id = self.ledger.start_attempt(job_id, tier=TIER_NAMES[attempt.tier], provider=attempt.provider,
                                               model=attempt.model, model_version=spec.version if spec else None)
        # NULL, not "passed", when no verifier ran. The report reads this
        # column to separate measured quality from unmeasured output.
        verifier_result = None
        if attempt.verifier_ran:
            verifier_result = "passed" if attempt.verified else "failed"
        self.ledger.finish_attempt(
            attempt_id, status=attempt.status, verifier_result=verifier_result,
            error_reason=attempt.error, input_tokens=attempt.input_tokens, output_tokens=attempt.output_tokens,
            wall_ms=attempt.wall_seconds * 1000, estimated_cost_usd=attempt.estimated_cost_usd,
            estimated_energy_wh=attempt.estimated_energy_wh, energy_method=attempt.energy_method)
        self.ledger.add_event(job_id, "attempt_finished", asdict(attempt), attempt_id=attempt_id)

    def _event(self, job_id: str, kind: str, payload: dict[str, Any]) -> None:
        if self.ledger is not None:
            self.ledger.add_event(job_id, kind, payload)

    def _finish(self, result: RouteResult, *, disposition: str, status: str | None = None) -> RouteResult:
        result.disposition = disposition
        if self.ledger is not None:
            kwargs = {"disposition": disposition, "escalated": bool(result.escalation_path),
                      "escalation_reason": ";".join(map(str, result.escalation_path)) or None}
            if status is not None:
                kwargs["status"] = status
            self.ledger.finish_job(result.job_id, **kwargs)
        return result

    def _cost(self, spec: ModelSpec | None, inputs: int | None, outputs: int | None) -> float | None:
        if spec is None or spec.local: return 0.0
        if spec.input_price_per_million is None or spec.output_price_per_million is None: return None
        return ((inputs or 0) * spec.input_price_per_million + (outputs or 0) * spec.output_price_per_million) / 1_000_000

    # ---- routing -------------------------------------------------------------

    def route(self, request: RouteRequest) -> RouteResult:
        # An unrecognised task class is refused before anything runs. The
        # alternative -- defaulting to "general" -- lets a typo silently
        # drop a certification requirement.
        try:
            capabilities_for(request.task_class)
        except UnknownTaskClass as exc:
            job_id = self._create_job(request, None, "unknown task class")
            result = RouteResult(job_id, None, str(exc), "refused")
            self._event(job_id, "task_class_unknown", {"task_class": request.task_class})
            return self._finish(result, disposition="refused", status="refused")

        # Gate 1: authorization. Checked before candidate selection so that
        # high-risk work never reaches a provider, even a configured one.
        if risk_rank(request.risk) > risk_rank(self.autonomous_risk_ceiling) and not request.human_authorization:
            reason = (f"risk {request.risk!r} exceeds the autonomous ceiling "
                      f"{self.autonomous_risk_ceiling!r} and no human authorization was supplied")
            job_id = self._create_job(request, None, reason)
            result = RouteResult(job_id, None, reason, "authorization_required")
            self._event(job_id, "authorization_required",
                        {"risk": request.risk, "ceiling": self.autonomous_risk_ceiling})
            return self._finish(result, disposition="authorization_required", status="blocked")

        ineligible: list[Ineligible] = []
        candidates = self._candidates(request, ineligible)
        frontier_was_configured = any(item[0] == 3 for item in candidates)

        # Gate 2: privacy and execution preference. Applied as an explicit
        # loop rather than a chain of comprehensions so that every dropped
        # candidate carries the reason it was dropped -- an operator has to
        # be able to tell "no frontier is configured" from "a frontier is
        # configured and policy refused it".
        permitted: list[tuple[int, ModelSpec | None, Any, str | None]] = []
        frontier_dropped = False
        for entry in candidates:
            tier_number, spec, _provider, _suite = entry
            model_label = spec.name if spec else TIER_NAMES.get(tier_number, str(tier_number))
            reason = None
            if tier_number == 3:
                if request.privacy.lower() in PRIVATE:
                    reason = f"privacy {request.privacy!r} forbids a frontier provider"
                elif request.preferred_execution == "local":
                    reason = "preferred_execution is 'local'"
                elif not self.frontier_enabled and request.preferred_execution != "frontier":
                    reason = "frontier tier is not enabled on this coordinator"
            if reason is None:
                permitted.append(entry)
                continue
            frontier_dropped = frontier_dropped or tier_number == 3
            ineligible.append(Ineligible(model_label, request.task_class, reason))
        candidates = permitted

        tier = 0 if request.deterministic else (candidates[0][0] if candidates else None)
        reason = ("deterministic handler supplied" if tier == 0 else
                  "first enabled backend satisfying capability/context/privacy/certification policy" if tier else
                  "no permitted certified model configured")
        result = RouteResult(self._create_job(request, tier, reason), tier, reason, "unavailable",
                             ineligible=list(ineligible))
        for entry in ineligible:
            self._event(result.job_id, "candidate_ineligible", asdict(entry))

        if request.deterministic:
            started = time.perf_counter()
            try:
                output = request.deterministic()
                verifier_ran = request.verifier is not None
                verified = bool(request.verifier(output)) if verifier_ran else False
                status = "succeeded" if verified else ("completed_unverified" if not verifier_ran else "verification_failed")
                attempt = Attempt(0, "deterministic", "builtin", status, time.perf_counter() - started,
                                  verified, input_tokens=0, output_tokens=0, estimated_cost_usd=0.0,
                                  energy_method="not measured", verifier_ran=verifier_ran)
            except Exception as exc:
                attempt = Attempt(0, "deterministic", "builtin", "failed", time.perf_counter() - started,
                                  error=f"{type(exc).__name__}: {exc}", input_tokens=0, output_tokens=0,
                                  estimated_cost_usd=0.0)
                output = None
            result.attempts.append(attempt); self._record(result.job_id, attempt, None)
            if attempt.verified or attempt.status == "completed_unverified":
                result.output = output
                return self._finish(result, disposition="success" if attempt.verified else "unverified")
            result.escalation_path.append(0)

        for tier, spec, provider, suite in candidates:
            if tier == 3 and not self.frontier_enabled:
                result.escalation_path.append(tier); continue
            if not self._available(provider):
                result.escalation_path.append(tier)
                self._event(result.job_id, "backend_unavailable",
                            {"tier": tier, "model": spec.name if spec else getattr(provider, "model", "unknown")})
                continue
            for retry in range(max(0, request.max_retries) + 1):
                started = time.perf_counter()
                provider_name = spec.provider if spec else getattr(provider, "name", "provider")
                model_name = spec.model_id if spec else getattr(provider, "model", "unknown")
                local = spec is None or spec.local
                try:
                    raw = provider.generate(request.task, context_tokens=request.context_tokens,
                                            timeout_seconds=request.timeout_seconds)
                    output = raw.get("output", raw.get("response", raw)) if isinstance(raw, dict) else raw
                    verifier_ran = request.verifier is not None
                    verified = bool(request.verifier(output)) if verifier_ran else False
                    inputs = raw.get("input_tokens", raw.get("prompt_eval_count")) if isinstance(raw, dict) else None
                    outputs = raw.get("output_tokens", raw.get("eval_count")) if isinstance(raw, dict) else None
                    # Provider counters are preferred; the bounded fallback is
                    # explicitly only an estimate for simple fake/legacy APIs.
                    if inputs is None: inputs = len(request.task.split())
                    if outputs is None: outputs = len(str(output).split())
                    wall = time.perf_counter() - started
                    status = ("succeeded" if verified else
                              "completed_unverified" if not verifier_ran else "verification_failed")
                    attempt = Attempt(
                        tier, provider_name, model_name, status, wall, verified=verified, retry=retry,
                        input_tokens=inputs, output_tokens=outputs,
                        estimated_cost_usd=self._cost(spec, inputs, outputs),
                        estimated_energy_wh=wall * self.local_energy_wh_per_second if local else None,
                        energy_method="configured estimate; not direct sensor measurement" if local else None,
                        verifier_ran=verifier_ran, certification_suite=suite)
                except Exception as exc:
                    wall = time.perf_counter() - started
                    attempt = Attempt(
                        tier, provider_name, model_name, "failed", wall,
                        error=f"{type(exc).__name__}: {exc}", retry=retry,
                        estimated_cost_usd=self._cost(spec, None, None),
                        estimated_energy_wh=wall * self.local_energy_wh_per_second if local else None,
                        energy_method="configured estimate; not direct sensor measurement" if local else None,
                        certification_suite=suite)
                    output = None
                result.attempts.append(attempt); self._record(result.job_id, attempt, spec)
                if attempt.verified or attempt.status == "completed_unverified":
                    result.output = output
                    return self._finish(result, disposition="success" if attempt.verified else "unverified")
                if retry < request.max_retries: continue
            result.escalation_path.append(tier)

        if frontier_dropped and frontier_was_configured and 3 not in result.escalation_path:
            # An explicit policy outcome, not a silent fallback: the frontier
            # tier existed, was considered, and was refused. The matching
            # `candidate_ineligible` event above says which rule refused it.
            result.escalation_path.append(3)
        disposition = "escalated" if result.attempts else "unavailable"
        return self._finish(result, disposition=disposition,
                            status="unavailable" if not result.attempts else "completed")
