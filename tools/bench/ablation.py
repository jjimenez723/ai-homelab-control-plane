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
content for C/E is task-driven rather than injected wholesale (D-044). Where
no relevant trusted skill exists in skills/ for a task's capability class,
condition D is identical to C and that gap is recorded, not papered over.

Scoring reuses tools.bench.harness.verify() and tools.fixtures.runner exactly
as production/canary scoring does. No new verifier is introduced.

Three corrections from the 2026-09-14 review (D-049), each of which had made
a recorded conclusion say more than the data did:

* condition E's few-shot examples were six verbatim Legacy-20 items, prompt
  and answer both. They are now written for this experiment, and
  `assert_no_canary_contamination` refuses at run time rather than trusting
  the caller to pick a disjoint subset;
* `_NAMING_SPEC` claimed to restate tools/ingest/naming/policy.py and did
  not -- it demanded a shape neither the policy nor two of the five frozen
  answers have, and its file-path citation was copied verbatim into a model
  answer. It now describes the contract and cites nothing;
* a generation that stopped at the `num_predict` ceiling had no failure
  category, so 8 of the first run's 50 runs were recorded as wrong answers.
  `truncated_output` is now first-class and set before any other
  classification.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tools.bench.corpus import Task, load_corpus
from tools.bench.harness import benchmark_prompt, verify
from tools.fixtures.runner import evaluate_submission, submission_from_model_output
from tools.fixtures.spec import load_fixture
from tools.models.ollama_local import OllamaTextProvider

CONDITIONS: tuple[str, ...] = ("A", "B", "C", "D", "E")

CONDITION_LABELS: dict[str, str] = {
    "A": "raw/basic instruction (raw-model baseline)",
    # For classification and filename tasks this is literally what production
    # would send. For code tasks it is NOT: `_fallback_code_prompt` uses the
    # generic no-fixture contract even where a fixture bundle exists, so that
    # condition C can isolate the marginal value of the fixture's own spec.
    # The label says so, because "production task prompt" on its own was a
    # claim the code branch did not honour (D-049).
    "B": "production task prompt (code tasks: generic no-fixture contract, not the production one)",
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

# Condition E's few-shot examples. These are written for this experiment and
# are deliberately NOT Legacy-20 items.
#
# The first version of this file took all six verbatim from the corpus --
# `cls-02`, `cls-06`, `cls-08`, `name-02`, `name-03`, `name-04`, prompt and
# expected answer both. The run that was actually recorded (D-046) happened
# to evaluate a disjoint subset, so its numbers stand; but `run_ablation`
# accepts arbitrary task ids, so a later run on any of those six would have
# scored the model on a prompt containing its own answer. It also spent six
# of the twenty frozen canary items as prompt-construction material, which
# is the thing docs/19 section 2.5 and docs/20 section 6 both forbid: the
# holdout is never used to build a prompt. See D-049.
#
# `assert_no_canary_contamination` below turns "we were careful" into "the
# runner refuses".
_CLASSIFICATION_FEWSHOT = (
    ("Classify: a RAW photograph of a bird in flight.", "photo"),
    ("Classify: an API reference page explaining function parameters.", "documentation"),
    ("Classify: a TAR.GZ bundle of last year's project files.", "archive"),
)

_FILENAME_FEWSHOT = (
    ("Propose a safe filename for a bird photograph taken on 2024-07-19.",
     "2024-07-19_bird-photograph.jpg"),
    ("Propose a safe filename for an API reference page.", "api-reference.md"),
    ("Propose a safe filename for a hardware store receipt dated 2023-11-02.",
     "2023-11-02_hardware-store-receipt.pdf"),
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

# Deliberately carries no source-file citation. The earlier version of this
# string opened with "Naming spec (tools/ingest/naming/policy.py):" and
# `qwen2.5:0.5b` answered `name-05` with `ingest-naming-policy.py` -- it
# echoed the citation as the filename. A spec injected as context must
# describe the contract, not point at a path the model can copy.
#
# It also no longer claims to restate `tools/ingest/naming/policy.py`,
# because it did not: the policy's `sanitize_stem` preserves a capitalised
# word and only collapses separator runs, so "lower-case, hyphen-separated"
# was a third contract, agreeing with neither the code nor the Legacy-20
# expected answers (`2025-01-01_Acme_invoice.pdf`, `2024_monthly-revenue.csv`).
# A model that followed the old spec perfectly could not produce the graded
# answer. See D-049.
_NAMING_SPEC = (
    "Filename contract: a filename is an optional leading date, then a short "
    "descriptive stem, then the extension. A date is written ISO-8601 "
    "(YYYY-MM-DD) and separated from the stem by a single underscore. The "
    "stem is ASCII, contains no whitespace and no path separators, and uses "
    "single separator characters rather than runs. The extension comes from "
    "the source document type and is never invented. Avoid control "
    "characters and the reserved device stems con, prn, aux, nul, com1-9, "
    "lpt1-9."
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


class CanaryContamination(ValueError):
    """Prompt material and evaluated items overlap. Refuse rather than score."""


def _normalize(text: str) -> str:
    return " ".join(str(text).split()).strip().lower()


def output_contract_material() -> tuple[str, ...]:
    """Material that states the *shape* of a valid answer.

    A closed label set is the output contract, not a leak: condition C's
    whole point is that the model is told which ten labels exist, and every
    evaluated classification answer is necessarily one of them. Telling a
    model the alphabet is not telling it the word.
    """
    return (_classification_spec(), _NAMING_SPEC)


def example_material() -> tuple[tuple[str, str], ...]:
    """Worked (prompt, answer) pairs -- the material that *can* leak."""
    return _CLASSIFICATION_FEWSHOT + _FILENAME_FEWSHOT


def assert_no_canary_contamination(task_ids: tuple[str, ...], tasks: dict[str, Task]) -> None:
    """Refuse to run if an evaluated item is present in the prompt material.

    Checked at run time rather than trusted at authoring time, because the
    task list is a caller-supplied argument. A measurement whose prompt
    contains its own answer is not a weak measurement; it is not a
    measurement. See D-049.

    Three rules, each drawn from what would actually invalidate a result:

    * an evaluated item's own prompt text must not appear in any injected
      material -- that is a worked example of the item being scored;
    * a worked example must not restate an evaluated item's prompt;
    * for **filename** tasks the expected answer must not appear anywhere
      in the examples, because a filename answer is specific to one item.
      Classification answers are exempt from that last rule precisely
      because they come from the enumerated contract above.
    """
    examples = example_material()
    example_blob = "\n".join(f"{prompt}\n{answer}" for prompt, answer in examples)
    contract_blob = "\n".join(output_contract_material())
    normalized_examples = {_normalize(prompt) for prompt, _ in examples}

    offenders: list[str] = []
    for task_id in task_ids:
        task = tasks.get(task_id)
        if task is None:
            continue
        prompt = _normalize(task.prompt)
        if prompt and (prompt in _normalize(example_blob) or prompt in _normalize(contract_blob)):
            offenders.append(f"{task_id}: its prompt appears in the injected material")
            continue
        if prompt and prompt in normalized_examples:
            offenders.append(f"{task_id}: a worked example restates its prompt")
            continue
        if task.kind == "filename":
            answer = str(task.expected)
            if answer and _normalize(answer) in _normalize(example_blob):
                offenders.append(f"{task_id}: its expected filename {answer!r} appears in a worked example")

    if offenders:
        raise CanaryContamination(
            "refusing to run: evaluated task(s) are present in this runner's own prompt "
            "material, so the result would not be a measurement -- " + "; ".join(offenders)
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
    "no-observation": "verifier_reject", "probe-error": "verifier_reject",
    "invalid-submission": "malformed_output", "code-fixture-unavailable": "capability_boundary",
    "sandbox-unavailable": "tool_failure", "tamper-detected": "tool_failure",
    "timeout": "timeout", "cpu-limit-exceeded": "timeout", "verifier-error": "tool_failure",
    "provider-error": "runtime_unavailable", "unknown-verifier": "tool_failure",
}

# A generation that stopped because it hit `num_predict` is a harness limit,
# not a wrong answer. The first ablation run (D-046) had no such category, so
# all 8 of its 50 runs that ended exactly at the token ceiling -- 6 of them in
# condition A, whose bare prompt invites prose the 64-token cap then cuts --
# were recorded as `verifier_reject` and read back as "the model was wrong".
# See D-049.
TRUNCATED_OUTPUT = "truncated_output"


def _failure_category(success: bool, status: str, *, truncated: bool = False) -> str | None:
    if success:
        return None
    if truncated:
        return TRUNCATED_OUTPUT
    return _FAILURE_CATEGORY.get(status, "malformed_output")


def ollama_ablation_provider(endpoint: str, model: str, *, seed: int = 42, num_ctx: int = 4096) -> OllamaTextProvider:
    """The shared loopback adapter, configured for this experiment.

    This used to be a private near-copy of `OllamaTextProvider` that existed
    solely because that class hard-coded ``num_predict=64``, which truncates
    a code answer before it can compile. Two adapters with different
    sampling meant an eval/production split the project's own prompt
    architecture forbids (docs/20 section 4), so the ceiling became a
    parameter and the copy is gone (D-049). Per-call `num_predict` still
    varies by task kind; see `_num_predict_for`.
    """
    return OllamaTextProvider(endpoint, model, seed=seed, num_ctx=num_ctx, think=False)


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
    # True when generation stopped at the `num_predict` ceiling rather than
    # because the model finished. A truncated answer is a harness limit, not
    # evidence about capability, and must not be read as a wrong answer.
    truncated: bool = False

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _num_predict_for(task: Task) -> int:
    return 512 if task.kind == "code" else 64


def _timeout_for(task: Task) -> int:
    return 180 if task.kind == "code" else 60


def run_condition(
    provider: Any, task: Task, condition: str, *,
    model_digest: str | None = None, quantization: str | None = None,
) -> AblationRunResult:
    cp = build_condition_prompt(task, condition)
    prompt_sha256 = hashlib.sha256(cp.text.encode("utf-8")).hexdigest()
    num_predict = _num_predict_for(task)
    sampling = dict(provider.sampling(num_predict=num_predict))
    sampling["think"] = getattr(provider, "think", False)
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
        response = provider.generate(cp.text, num_predict=num_predict, timeout_seconds=_timeout_for(task))
    except (TimeoutError, ConnectionError, ValueError) as exc:
        wall = time.perf_counter() - started_wall
        status = "timeout" if isinstance(exc, TimeoutError) else "provider-error"
        return AblationRunResult(
            **common, wall_seconds=wall, prefill_seconds=None, generation_seconds=None,
            input_tokens=None, output_tokens=None, raw_response=None, parsed_output=None,
            success=False, verifier_status=status, failure_category=_failure_category(False, status),
            error=f"{type(exc).__name__}: {exc}", truncated=False,
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

    output_tokens = response.get("output_tokens")
    # Ollama reports `eval_count`; equality with the cap is the only signal
    # available that the model was cut off rather than finished.
    truncated = bool(output_tokens is not None and output_tokens >= sampling["num_predict"])

    return AblationRunResult(
        **common, wall_seconds=wall,
        prefill_seconds=response.get("prefill_seconds"), generation_seconds=response.get("generation_seconds"),
        input_tokens=response.get("input_tokens"), output_tokens=output_tokens,
        raw_response=raw_text, parsed_output=parsed, success=success, verifier_status=status,
        failure_category=_failure_category(success, status, truncated=truncated), error=None,
        truncated=truncated,
    )


def run_ablation(
    provider: Any, task_ids: tuple[str, ...] = DEFAULT_TASK_IDS,
    conditions: tuple[str, ...] = CONDITIONS, *,
    model_digest: str | None = None, quantization: str | None = None,
) -> list[AblationRunResult]:
    tasks = {task.id: task for task in load_corpus()}
    missing = [task_id for task_id in task_ids if task_id not in tasks]
    if missing:
        raise ValueError(f"unknown task id(s): {missing}")
    assert_no_canary_contamination(tuple(task_ids), tasks)
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

    provider = ollama_ablation_provider(args.endpoint, args.model)
    if not provider.available():
        print(f"BLOCKED: Ollama not reachable at {args.endpoint}; recording nothing for {args.model}")
        return 1

    results = run_ablation(provider, tuple(args.task_ids), model_digest=args.model_digest,
                            quantization=args.quantization)
    write_jsonl(results, Path(args.out))
    passed = sum(1 for r in results if r.success)
    truncated = sum(1 for r in results if r.truncated)
    print(f"{passed}/{len(results)} runs passed; raw results at {args.out}")
    if truncated:
        print(f"WARNING: {truncated}/{len(results)} runs hit the num_predict ceiling and are "
              f"categorised '{TRUNCATED_OUTPUT}', not as wrong answers.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
