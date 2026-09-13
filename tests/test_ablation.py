import unittest

from tools.bench.ablation import (
    CONDITIONS,
    DEFAULT_TASK_IDS,
    NO_APPLICABLE_SKILL,
    build_condition_prompt,
    run_ablation,
    run_condition,
)
from tools.bench.corpus import load_corpus
from tools.fixtures.sandbox import sandbox_available

_TASKS = {task.id: task for task in load_corpus()}


class FakeAblationProvider:
    """Duck-types tools.bench.ablation.OllamaAblationProvider without a network call."""

    name = "fake"

    def __init__(self, model="fake-v1", seed=42, num_ctx=4096, response="ok"):
        self.model = model
        self.seed = seed
        self.num_ctx = num_ctx
        self.response = response
        self.calls: list[str] = []

    def generate(self, prompt, *, num_predict, timeout_seconds):
        self.calls.append(prompt)
        return {"output": self.response, "input_tokens": 10, "output_tokens": 5,
                "prefill_seconds": 0.01, "generation_seconds": 0.02}


class ConditionPromptTests(unittest.TestCase):
    def test_condition_a_is_the_bare_frozen_prompt(self):
        for task_id in DEFAULT_TASK_IDS:
            cp = build_condition_prompt(_TASKS[task_id], "A")
            self.assertEqual(cp.text, _TASKS[task_id].prompt)
            self.assertIsNone(cp.skill_applied)

    def test_conditions_are_strictly_cumulative_in_length(self):
        # B <= C <= D <= E in prompt length for every task in the subset --
        # each condition only adds augmentation, never removes it.
        for task_id in DEFAULT_TASK_IDS:
            lengths = [len(build_condition_prompt(_TASKS[task_id], c).text) for c in ("B", "C", "D", "E")]
            self.assertEqual(lengths, sorted(lengths), f"{task_id} augmentation is not monotonic: {lengths}")

    def test_d_has_no_applicable_skill_for_this_subset(self):
        # Documents the current gap in skills/: neither shipped skill targets
        # routing/classification, semantic naming, or general Python coding.
        for task_id in DEFAULT_TASK_IDS:
            cp = build_condition_prompt(_TASKS[task_id], "D")
            self.assertEqual(cp.skill_applied, NO_APPLICABLE_SKILL)
            c_text = build_condition_prompt(_TASKS[task_id], "C").text
            self.assertEqual(cp.text, c_text)

    def test_e_adds_reference_material_beyond_d(self):
        for task_id in DEFAULT_TASK_IDS:
            d_text = build_condition_prompt(_TASKS[task_id], "D").text
            e_text = build_condition_prompt(_TASKS[task_id], "E").text
            self.assertGreater(len(e_text), len(d_text))

    def test_code_condition_c_includes_fixture_spec_and_hidden_tests_are_never_shown(self):
        cp = build_condition_prompt(_TASKS["code-01"], "C")
        self.assertIn("solution.py", cp.text)
        self.assertNotIn("test_add", cp.text)  # hidden test filename must never leak into the prompt

    def test_unknown_condition_rejected(self):
        with self.assertRaises(ValueError):
            build_condition_prompt(_TASKS["cls-01"], "Z")


class RunConditionTests(unittest.TestCase):
    def test_fake_provider_classification_condition_a_records_full_metadata(self):
        provider = FakeAblationProvider(response="invoice")
        result = run_condition(provider, _TASKS["cls-01"], "A")
        self.assertEqual(result.condition, "A")
        self.assertEqual(result.kind, "classification")
        self.assertTrue(result.success)
        self.assertEqual(result.verifier_status, "exact")
        self.assertIsNone(result.failure_category)
        self.assertEqual(result.cost_usd, 0.0)
        self.assertEqual(len(result.prompt_sha256), 64)

    def test_fake_provider_classification_wrong_label_fails_closed(self):
        provider = FakeAblationProvider(response="not-a-label")
        result = run_condition(provider, _TASKS["cls-01"], "B")
        self.assertFalse(result.success)
        self.assertEqual(result.failure_category, "verifier_reject")

    def test_fake_provider_code_condition_strips_markdown_fence_before_scoring(self):
        provider = FakeAblationProvider(response="```python\ndef add(a, b):\n    return a + b\n```")
        result = run_condition(provider, _TASKS["code-01"], "C")
        self.assertNotIn("```", result.parsed_output)
        self.assertIn("def add", result.parsed_output)
        if sandbox_available():
            self.assertTrue(result.success)
        else:
            self.assertEqual(result.verifier_status, "sandbox-unavailable")

    def test_fake_provider_code_claim_without_code_is_malformed_not_a_pass(self):
        provider = FakeAblationProvider(response="tests_passed: true")
        result = run_condition(provider, _TASKS["code-05"], "A")
        self.assertFalse(result.success)

    def test_provider_timeout_is_recorded_not_silently_retried(self):
        class TimeoutProvider(FakeAblationProvider):
            def generate(self, prompt, *, num_predict, timeout_seconds):
                raise TimeoutError("simulated")

        result = run_condition(TimeoutProvider(), _TASKS["cls-01"], "A")
        self.assertFalse(result.success)
        self.assertEqual(result.verifier_status, "timeout")
        self.assertEqual(result.failure_category, "timeout")
        self.assertIsNotNone(result.error)


class RunAblationTests(unittest.TestCase):
    def test_run_ablation_covers_every_task_and_condition_exactly_once(self):
        provider = FakeAblationProvider(response="invoice")
        results = run_ablation(provider, task_ids=("cls-01",), conditions=CONDITIONS)
        self.assertEqual(len(results), len(CONDITIONS))
        self.assertEqual({r.condition for r in results}, set(CONDITIONS))
        self.assertEqual(len(provider.calls), len(CONDITIONS))

    def test_unknown_task_id_rejected(self):
        provider = FakeAblationProvider()
        with self.assertRaises(ValueError):
            run_ablation(provider, task_ids=("cls-99",))


if __name__ == "__main__":
    unittest.main()
