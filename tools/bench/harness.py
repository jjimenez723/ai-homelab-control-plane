from __future__ import annotations

import json, os, platform, resource, time
from dataclasses import asdict, dataclass
from typing import Any, Callable

from .corpus import Task, load_corpus

@dataclass
class BenchmarkResult:
    task_id: str; provider: str; model: str; runtime_version: str | None
    context_tokens: int; prompt_tokens: int; prompt_seconds: float
    generated_tokens: int; generation_seconds: float; wall_seconds: float
    peak_rss_bytes: int | None; success: bool; verifier: str
    error: str | None = None
    status: str = "completed"

    def as_dict(self): return asdict(self)

def _tokens(value: Any) -> int:
    if isinstance(value, dict):
        for key in ("token_count", "tokens", "output_tokens", "completion_tokens"):
            if isinstance(value.get(key), int): return value[key]
        text = value.get("text", value.get("output", ""))
    else: text = value
    return len(str(text).split())

def verify(task: Task, output: Any) -> tuple[bool, str]:
    if task.verifier == "exact": return output == task.expected, "exact"
    if task.verifier == "filename":
        value = output.get("filename") if isinstance(output, dict) else output
        ok = isinstance(value, str) and value == task.expected and "/" not in value and "\\" not in value and "\x00" not in value
        return ok, "filename"
    if task.verifier == "code_fixture":
        # Providers may return a fixture result, while real code verification is injected by callers.
        return (output.get("tests_passed") is True if isinstance(output, dict) else output is True), "code_fixture"
    return False, "unknown-verifier"

def run(provider: Any, tasks: list[Task] | None = None, contexts=(2048, 8192, 32768), *, model="unknown", runtime_version=None) -> list[BenchmarkResult]:
    """Run a fixed corpus against a provider. Provider only needs ``generate(prompt, **kw)``.

    This intentionally accepts fake providers and never requires a network/runtime. Contexts a
    provider cannot handle should be returned as unsupported by that provider, not fabricated here.
    """
    results = []
    for task in tasks or load_corpus():
        for context in contexts:
            # Fixed context targets must be materially exercised, not merely
            # reported.  Padding is inert public text and leaves the task at
            # the end, where an instruction-following model can still answer.
            padding = " benchmark" * max(0, context - len(task.prompt.split()) - 8)
            prompt = f"{padding}\n\n{task.prompt}"
            started = time.perf_counter(); prompt_start = time.perf_counter()
            try:
                response = provider.generate(prompt, context_tokens=context, task=task)
                prompt_seconds = float(response.get("prefill_seconds", time.perf_counter() - prompt_start)) if isinstance(response, dict) else time.perf_counter() - prompt_start
                output = response.get("output", response) if isinstance(response, dict) else response
                generated = _tokens(response)
                generation_seconds = float(response.get("generation_seconds", 0.0)) if isinstance(response, dict) else 0.0
                ok, verifier = verify(task, output)
                error = None; status = "completed"
            except (TimeoutError, ValueError, RuntimeError) as exc:
                prompt_seconds = time.perf_counter() - prompt_start; generated = 0; generation_seconds = 0.0
                ok = False; verifier = "provider-error"; error = f"{type(exc).__name__}: {exc}"
                status = "unsupported" if isinstance(exc, ValueError) and "context" in str(exc).lower() else "failed"
            rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            # macOS reports bytes; Linux reports KiB. Keep a defensible local measurement.
            rss_bytes = rss if platform.system() == "Darwin" else rss * 1024
            results.append(BenchmarkResult(task.id, getattr(provider, "name", provider.__class__.__name__), model,
                runtime_version, context, len(prompt.split()), prompt_seconds, generated,
                generation_seconds, time.perf_counter() - started, rss_bytes, ok, verifier, error, status))
    return results

def write_jsonl(results: list[BenchmarkResult], path: str) -> None:
    with open(path, "w", encoding="utf-8") as stream:
        for result in results: stream.write(json.dumps(result.as_dict(), sort_keys=True) + "\n")
