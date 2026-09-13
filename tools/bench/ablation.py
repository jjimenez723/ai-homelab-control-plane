"""A->E augmentation ablation runner (docs/19-model-evaluation.md #10).

Holds the model, task, and verifier fixed and varies only the augmentation
level across five cumulative conditions:

    A  raw/basic instruction              (the raw-model baseline, doc #9)
    B  production task prompt             (tools.bench.harness.benchmark_prompt)
    C  B + strong SPEC/context
    D  C + relevant trusted skills
    E  D + relevant reference material/tools

This measures augmentation's marginal value; it is not a new benchmark and
does not replace the Legacy 20 (docs/19 #1) or ACB-v1 (docs/19 #2). Prompt
content for C/E is drawn from material that genuinely exists in this
repository (tools/ingest/naming/policy.py, the corpus's own label taxonomy,
Python-stdlib facts) rather than invented for the occasion -- per D-044,
augmentation must be task-driven, not injected wholesale. Where no relevant
trusted skill exists in skills/ for a task's capability class, condition D
is identical to C and that gap is recorded, not papered over.

Scoring reuses tools.bench.harness.verify() and tools.fixtures.runner exactly
as production/canary scoring does. No new verifier is introduced.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tools.bench.corpus import Task, load_corpus
from tools.bench.harness import benchmark_prompt, verify
from tools.fixtures.runner import evaluate_submission, submission_from_model_output
from tools.fixtures.spec import load_fixture

CONDITIONS: tuple[str, ...] = ("A", "B", "C", "D", "E")

CONDITION_LABELS: dict[str, str] = {
    "A": "raw/basic instruction (raw-model baseline)",
    "B": "production task prompt",
    "C": "B + strong SPEC/context",
    "D": "C + relevant trusted skills",
    "E": "D + relevant reference material/tools",
}

# Representative subset (docs/19 #10 calls for a subset, not the full 20):
# three classification tasks spanning the low/medium ambiguity range in
# docs/19 #1.1's own table, two filename tasks, and all five code fixtures
# (D-045 is what unblocked this experiment, so coding is deliberately
# over-represented relative to its 5/20 share of the Legacy 20).
DEFAULT_TASK_IDS: tuple[str, ...] = (
    "cls-01", "cls-04", "cls-09",
    "name-01", "name-05",
    "code-01", "code-02", "code-03", "code-04", "code-05",
)

# skills/ contents at the time this experiment was designed (2026-09-13):
# social-video-learner and ui-design-system-builder. Neither is relevant to
# routing/classification, semantic naming, or general Python code-fixture
# work, so condition D adds nothing for any task in this subset. This is a
# genuine finding (Angel's skill library does not yet cover these ACB-v1
# capability classes), not a null augmentation result -- see docs/19-model-
# evaluation.md #10's update for the caveat this implies.
NO_APPLICABLE_SKILL = "none-applicable: no skill in skills/ targets this task's capability class"

_CLASSIFICATION_TAXONOMY = (
    "invoice", "photo", "text_generation", "meeting", "spreadsheet",
    "documentation", "audio", "archive", "contract", "screenshot",
)

_CLASSIFICATION_FEWSHOT = (
    ("Classify: a JPEG photograph of a mountain landscape.", "photo"),
    ("Classify: a README describing how to install a software package.", "documentation"),
    ("Classify: a ZIP archive containing a project backup.", "archive"),
)

_FILENAME_FEWSHOT = (
    ("Propose a safe filename for a mountain landscape photograph.", "mountain-landscape.jpg"),
    ("Propose a safe filename for a meeting agenda dated 2025-03-04.", "2025-03-04_meeting-agenda.pdf"),
    ("Propose a safe filename for monthly revenue CSV for 2024.", "2024_monthly-revenue.csv"),
)

# Factual, publicly-known stdlib/language notes -- not the fixture's hidden
# tests and not a literal solution -- offered as condition E's "approved
# reference material" per the production execution unit in docs/19's
# Objective section.
_CODE_REFERENCE: dict[str, str] = {
    "code-01": "Reference: a Python function is `def name(params): return expression`; "
               "`+` sums two numbers.",
    "code-02": "Reference: `str.lower()` lowercases text; `re.sub(r'[^a-z0-9]+', '-', text)` "
               "collapses runs of non-alphanumeric characters into one separator; "
               "`str.strip('-')` trims leading/trailing separators.",
    "code-03": "Reference: Python slicing `items[i:i+size]` extracts a sub-list; "
               "`range(0, len(items), size)` steps through fixed-size start indices.",
    "code-04": "Reference: `json.dumps(obj)` serializes a Python object to a JSON string; "
               "`json.loads(s)` parses one back into an equal object.",
    "code-05": "Reference: `time.sleep(seconds)` pauses execution; inside `except Exception:` "
               "a bare `raise` re-raises the exception currently being handled.",
}

_NAMING_SPEC = (
    "Naming spec (tools/ingest/naming/policy.py): a filename is "
    "`[YYYY-MM-DD_]slug.ext`. The optional leading date is ISO-8601 plus one "
    "underscore. The slug is lower-case, ASCII, words separated by a single "
    "hyphen, no repeated hyphens, no leading/trailing hyphen. The extension is "
    "taken from the source document type, never invented. No path separators, "
    "control characters, or reserved device stems (con, prn, aux, nul, com1-9, "
    "lpt1-9)."
)


def _classification_spec() -> str:
    labels = ", ".join(_CLASSIFICATION_TAXONOMY)
    return (
        "Category spec: the label must be exactly one of this fixed set "
        f"(lower-case, verbatim): {labels}."
    )


def _fewshot_block(header: str, pairs: tuple[tuple[str, str], ...], left: str, right: str) -> str:
    lines = "\n".join(f"- {left}: {prompt} -> {right}: {answer}" for prompt, answer in pairs)
    return f"{header} (not the item below):\n{lines}"


def _fallback_code_prompt(task: Task) -> str:
    """The generic bounded-code-task contract, verbatim from harness.benchmark_prompt's
    no-fixture branch, used here as condition B even for tasks that do have a
    fixture bundle -- so condition C can isolate the marginal value of adding
    the fixture's task.md/starter spec on top of the same generic framing."""
    return (
        "This is a bounded code task. Return only the proposed code; do not claim that tests passed. "
        "A trusted verifier, not your response, determines success.\n"
        f"Task: {task.prompt}"
    )


@dataclass(frozen=True)
class ConditionPrompt:
    condition: str
    text: str
    skill_applied: str | None


def build_condition_prompt(task: Task, condition: str) -> ConditionPrompt:
    if condition not in CONDITIONS:
        raise ValueError(f"unknown condition: {condition!r}")

    if condition == "A":
        return ConditionPrompt("A", task.prompt, None)

    if task.kind in ("classification", "filename"):
        b_text = benchmark_prompt(task)
        if condition == "B":
            return ConditionPrompt("B", b_text, None)
        spec = _classification_spec() if task.kind == "classification" else _NAMING_SPEC
        c_text = f"{b_text}\n\n{spec}"
        if condition == "C":
            return ConditionPrompt("C", c_text, None)
        if condition == "D":
            return ConditionPrompt("D", c_text, NO_APPLICABLE_SKILL)
        fewshot = (
            _fewshot_block("Reference examples", _CLASSIFICATION_FEWSHOT, "Description", "Label")
            if task.kind == "classification"
            else _fewshot_block("Reference examples", _FILENAME_FEWSHOT, "Request", "Filename")
        )
        return ConditionPrompt("E", f"{c_text}\n\n{fewshot}", NO_APPLICABLE_SKILL)

    # task.kind == "code"
    b_text = _fallback_code_prompt(task)
    if condition == "B":
        return ConditionPrompt("B", b_text, None)
    c_text = benchmark_prompt(task)  # includes task.md + starter file when a fixture bundle exists
    if condition == "C":
        return ConditionPrompt("C", c_text, None)
    if condition == "D":
        return ConditionPrompt("D", c_text, NO_APPLICABLE_SKILL)
    reference = _CODE_REFERENCE.get(task.id, "Reference: none available for this task.")
    return ConditionPrompt("E", f"{c_text}\n\n{reference}", NO_APPLICABLE_SKILL)


def _extract_code(text: str) -> str:
    """Strip one leading/trailing Markdown code fence if present.

    A real caller placing model output into a file would not write the
    fence markers to disk; tools.fixtures.runner.submission_from_model_output
    treats a string as code verbatim and does not do this, so it is done
    once, here, before extraction -- and both the raw and post-strip text are
    recorded so a run where fencing caused the only failure is visible
    rather than silently absorbed.
    """
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        return "\n".join(lines)
    return stripped


_FAILURE_CATEGORY = {
    "exact": "verifier_reject", "filename": "verifier_reject",
    "passed": None, "failed": "verifier_reject",
    "invalid-submission": "malformed_output", "code-fixture-unavailable": "capability_boundary",
    "sandbox-unavailable": "tool_failure", "tamper-detected": "tool_failure",
    "timeout": "timeout", "cpu-limit-exceeded": "timeout", "verifier-error": "tool_failure",
    "provider-error": "runtime_unavailable", "unknown-verifier": "tool_failure",
}


def _failure_category(success: bool, status: str) -> str | None:
    if success:
        return None
    return _FAILURE_CATEGORY.get(status, "malformed_output")


class OllamaAblationProvider:
    """Direct, minimal Ollama /api/generate caller for this experiment only.

    tools.models.ollama_local.OllamaTextProvider hard-codes num_predict=64,
    which truncates a code-fixture answer before it can compile; reusing it
    here would confound "augmentation didn't help" with "the answer was cut
    off". This class changes nothing in tools/models/ -- it is scoped to the
    ablation runner and kept as small as the existing client it mirrors.
    """

    name = "ollama"

    def __init__(self, endpoint: str, model: str, *, seed: int = 42, num_ctx: int = 4096):
        import urllib.parse

        parsed = urllib.parse.urlparse(endpoint)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("Ollama endpoint must be loopback-only HTTP")
        self.endpoint = endpoint.rstrip("/")
        self.model = model
        self.seed = seed
        self.num_ctx = num_ctx

    def available(self) -> bool:
        try:
            with urllib.request.urlopen(self.endpoint + "/api/tags", timeout=1.5) as response:
                return 200 <= response.status < 300
        except (OSError, urllib.error.URLError):
            return False

    def generate(self, prompt: str, *, num_predict: int, timeout_seconds: int) -> dict[str, Any]:
        options = {
            "num_ctx": self.num_ctx, "temperature": 0, "seed": self.seed,
            "num_predict": num_predict,
        }
        payload = json.dumps(
            {"model": self.model, "prompt": prompt, "stream": False, "think": False, "options": options},
            separators=(",", ":"),
        ).encode("utf-8")
        request = urllib.request.Request(
            self.endpoint + "/api/generate", data=payload,
            headers={"Content-Type": "application/json"}, method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                if not 200 <= response.status < 300:
                    raise ConnectionError(f"Ollama HTTP status {response.status}")
                outer = json.loads(response.read(4_000_000).decode("utf-8"))
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, TimeoutError):
                raise TimeoutError("Ollama request timed out") from exc
            raise ConnectionError(f"Ollama unavailable: {exc.reason}") from exc
        if not isinstance(outer, dict):
            raise ValueError("Ollama response is not an object")
        return {
            "output": str(outer.get("response", "")),
            "input_tokens": outer.get("prompt_eval_count"),
            "output_tokens": outer.get("eval_count"),
            "prefill_seconds": (outer.get("prompt_eval_duration") or 0) / 1e9,
            "generation_seconds": (outer.get("eval_duration") or 0) / 1e9,
            "runtime_model": outer.get("model"),
        }


@dataclass
class AblationRunResult:
    task_id: str
    kind: str
    condition: str
    condition_description: str
    skill_applied: str | None
    model: str
    model_digest: str | None
    quantization: str | None
    runtime: str
    sampling: dict[str, Any]
    prompt_sha256: str
    prompt_word_count: int
    started_at: str
    wall_seconds: float
    prefill_seconds: float | None
    generation_seconds: float | None
    input_tokens: int | None
    output_tokens: int | None
    raw_response: str | None
    parsed_output: Any
    success: bool
    verifier_status: str
    failure_category: str | None
    error: str | None
    cost_usd: float | None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _num_predict_for(task: Task) -> int:
    return 512 if task.kind == "code" else 64


def _timeout_for(task: Task) -> int:
    return 180 if task.kind == "code" else 60


def run_condition(
    provider: OllamaAblationProvider, task: Task, condition: str, *,
    model_digest: str | None = None, quantization: str | None = None,
) -> AblationRunResult:
    cp = build_condition_prompt(task, condition)
    prompt_sha256 = hashlib.sha256(cp.text.encode("utf-8")).hexdigest()
    sampling = {"temperature": 0, "seed": provider.seed, "num_ctx": provider.num_ctx,
                "num_predict": _num_predict_for(task), "think": False}
    started_wall = time.perf_counter()
    started_at = datetime.now(timezone.utc).isoformat()

    common = dict(
        task_id=task.id, kind=task.kind, condition=condition,
        condition_description=CONDITION_LABELS[condition], skill_applied=cp.skill_applied,
        model=provider.model, model_digest=model_digest, quantization=quantization,
        runtime="ollama", sampling=sampling, prompt_sha256=prompt_sha256,
        prompt_word_count=len(cp.text.split()), started_at=started_at, cost_usd=0.0,
    )

    try:
        response = provider.generate(cp.text, num_predict=sampling["num_predict"], timeout_seconds=_timeout_for(task))
    except (TimeoutError, ConnectionError, ValueError) as exc:
        wall = time.perf_counter() - started_wall
        status = "timeout" if isinstance(exc, TimeoutError) else "provider-error"
        return AblationRunResult(
            **common, wall_seconds=wall, prefill_seconds=None, generation_seconds=None,
            input_tokens=None, output_tokens=None, raw_response=None, parsed_output=None,
            success=False, verifier_status=status, failure_category=_failure_category(False, status),
            error=f"{type(exc).__name__}: {exc}",
        )

    wall = time.perf_counter() - started_wall
    raw_text = response["output"]

    if task.kind == "code":
        parsed = _extract_code(raw_text)
        try:
            spec = load_fixture(task.id)
            submission = submission_from_model_output(parsed, spec.editable_files)
            if submission is None:
                success, status = False, "invalid-submission"
            else:
                fixture_result = evaluate_submission(task.id, submission)
                success, status = fixture_result.success, fixture_result.status
        except (FileNotFoundError, ValueError):
            success, status = False, "code-fixture-unavailable"
    else:
        parsed = raw_text.strip()
        success, status = verify(task, parsed)

    return AblationRunResult(
        **common, wall_seconds=wall,
        prefill_seconds=response.get("prefill_seconds"), generation_seconds=response.get("generation_seconds"),
        input_tokens=response.get("input_tokens"), output_tokens=response.get("output_tokens"),
        raw_response=raw_text, parsed_output=parsed, success=success, verifier_status=status,
        failure_category=_failure_category(success, status), error=None,
    )


def run_ablation(
    provider: OllamaAblationProvider, task_ids: tuple[str, ...] = DEFAULT_TASK_IDS,
    conditions: tuple[str, ...] = CONDITIONS, *,
    model_digest: str | None = None, quantization: str | None = None,
) -> list[AblationRunResult]:
    tasks = {task.id: task for task in load_corpus()}
    missing = [task_id for task_id in task_ids if task_id not in tasks]
    if missing:
        raise ValueError(f"unknown task id(s): {missing}")
    results = []
    for task_id in task_ids:
        for condition in conditions:
            results.append(run_condition(provider, tasks[task_id], condition,
                                          model_digest=model_digest, quantization=quantization))
    return results


def write_jsonl(results: list[AblationRunResult], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as stream:
        for result in results:
            stream.write(json.dumps(result.as_dict(), sort_keys=True, default=str) + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", default="http://127.0.0.1:11434")
    parser.add_argument("--model", default="qwen2.5:0.5b")
    parser.add_argument("--model-digest", default=None)
    parser.add_argument("--quantization", default=None)
    parser.add_argument("--task-ids", nargs="*", default=list(DEFAULT_TASK_IDS))
    parser.add_argument("--out", required=True, help="output JSONL path (raw, not for git -- see D-035)")
    args = parser.parse_args(argv)

    provider = OllamaAblationProvider(args.endpoint, args.model)
    if not provider.available():
        print(f"BLOCKED: Ollama not reachable at {args.endpoint}; recording nothing for {args.model}")
        return 1

    results = run_ablation(provider, tuple(args.task_ids), model_digest=args.model_digest,
                            quantization=args.quantization)
    write_jsonl(results, Path(args.out))
    passed = sum(1 for r in results if r.success)
    print(f"{passed}/{len(results)} runs passed; raw results at {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
