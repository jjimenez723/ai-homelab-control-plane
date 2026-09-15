import unittest
from unittest import mock

from tools.bench import ablation
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
    """Duck-types tools.models.ollama_local.OllamaTextProvider without a network call.

    Including `sampling()`, which the real adapter now owns so that a run
    record and the request it describes cannot disagree about temperature,
    seed, or the output ceiling (D-049).
    """

    name = "fake"
    think = False

    def __init__(self, model="fake-v1", seed=42, num_ctx=4096, response="ok"):
        self.model = model
        self.seed = seed
        self.num_ctx = num_ctx
        self.response = response
        self.calls: list[str] = []

    def sampling(self, *, num_predict=None, context_tokens=None):
        return {"num_ctx": self.num_ctx or context_tokens or 4096, "temperature": 0,
                "num_predict": int(num_predict if num_predict is not None else 64),
                "seed": self.seed}

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


class CanaryContaminationTests(unittest.TestCase):
    """The frozen canary is never used to build the prompt that scores it.

    docs/19 section 2.5 and docs/20 section 6 both say the holdout is never
    used for prompt construction. The first version of this runner took all
    six of its few-shot examples verbatim from the Legacy 20 -- prompt and
    expected answer -- and relied on the caller choosing a disjoint subset.
    These tests make the runner enforce it. See D-049.
    """

    def setUp(self):
        self.tasks = {task.id: task for task in load_corpus()}

    def test_the_shipped_examples_are_clean_for_the_whole_corpus(self):
        ablation.assert_no_canary_contamination(tuple(self.tasks), self.tasks)

    def test_the_default_subset_is_clean(self):
        ablation.assert_no_canary_contamination(ablation.DEFAULT_TASK_IDS, self.tasks)

    def test_no_shipped_example_restates_a_corpus_prompt(self):
        corpus_prompts = {ablation._normalize(task.prompt) for task in self.tasks.values()}
        for prompt, _answer in ablation.example_material():
            with self.subTest(example=prompt[:50]):
                self.assertNotIn(ablation._normalize(prompt), corpus_prompts)

    def test_a_worked_example_taken_from_the_corpus_is_refused(self):
        leaked = self.tasks["cls-02"]
        with mock.patch.object(ablation, "_CLASSIFICATION_FEWSHOT",
                               ((leaked.prompt, str(leaked.expected)),)):
            with self.assertRaises(ablation.CanaryContamination):
                ablation.assert_no_canary_contamination(("cls-02",), self.tasks)

    def test_a_leaked_filename_answer_is_refused(self):
        leaked = self.tasks["name-04"]
        with mock.patch.object(ablation, "_FILENAME_FEWSHOT",
                               (("Some unrelated request.", str(leaked.expected)),)):
            with self.assertRaises(ablation.CanaryContamination):
                ablation.assert_no_canary_contamination(("name-04",), self.tasks)

    def test_a_closed_label_set_is_the_output_contract_not_a_leak(self):
        """Every classification answer is in the taxonomy. That is the point.

        Refusing on this would make condition C impossible to express, and
        telling a model which ten labels exist is not telling it which one
        applies.
        """
        contract = "\n".join(ablation.output_contract_material())
        self.assertIn("invoice", contract)
        ablation.assert_no_canary_contamination(("cls-01",), self.tasks)

    def test_run_ablation_refuses_a_contaminated_task_list(self):
        leaked = self.tasks["cls-02"]
        with mock.patch.object(ablation, "_CLASSIFICATION_FEWSHOT",
                               ((leaked.prompt, str(leaked.expected)),)):
            with self.assertRaises(ablation.CanaryContamination):
                ablation.run_ablation(FakeAblationProvider(), ("cls-02",), ("E",))


class InjectedSpecQualityTests(unittest.TestCase):
    """Condition C must not steer a model away from the graded answer."""

    def setUp(self):
        self.tasks = {task.id: task for task in load_corpus()}

    def test_the_naming_spec_cites_no_source_path(self):
        """`qwen2.5:0.5b` answered name-05 with `ingest-naming-policy.py`.

        It was copying the citation the spec opened with. A spec describes
        a contract; it does not hand the model a filename to echo.
        """
        self.assertNotIn("tools/", ablation._NAMING_SPEC)
        self.assertNotIn(".py", ablation._NAMING_SPEC)

    def test_the_naming_spec_does_not_contradict_the_graded_answers(self):
        """The old spec demanded lower-case hyphen-separated stems.

        Two of the five frozen answers (`2025-01-01_Acme_invoice.pdf`,
        `2024_monthly-revenue.csv`) do not have that shape, so a model that
        obeyed the spec exactly could not score. A spec that makes the
        graded answer unreachable measures nothing.
        """
        spec = ablation._NAMING_SPEC.lower()
        self.assertNotIn("lower-case", spec)
        self.assertNotIn("hyphen, no repeated", spec)


class TruncationCategoryTests(unittest.TestCase):
    """A cut-off generation is a harness limit, not a wrong answer."""

    def test_hitting_the_token_ceiling_is_categorised_as_truncation(self):
        self.assertEqual(
            ablation._failure_category(False, "exact", truncated=True), ablation.TRUNCATED_OUTPUT)

    def test_a_scored_wrong_answer_is_still_a_verifier_reject(self):
        self.assertEqual(ablation._failure_category(False, "exact", truncated=False), "verifier_reject")

    def test_success_has_no_failure_category_even_at_the_ceiling(self):
        self.assertIsNone(ablation._failure_category(True, "exact", truncated=True))

    def test_a_run_at_the_ceiling_is_flagged_on_the_record(self):
        class CeilingProvider(FakeAblationProvider):
            def generate(self, prompt, *, num_predict, timeout_seconds):
                value = super().generate(prompt, num_predict=num_predict, timeout_seconds=timeout_seconds)
                value["output_tokens"] = num_predict
                return value

        task = next(task for task in load_corpus() if task.id == "cls-01")
        result = ablation.run_condition(CeilingProvider(), task, "A")
        self.assertTrue(result.truncated)
        self.assertEqual(result.failure_category, ablation.TRUNCATED_OUTPUT)

    def test_a_normal_length_run_is_not_flagged(self):
        task = next(task for task in load_corpus() if task.id == "cls-01")
        result = ablation.run_condition(FakeAblationProvider(), task, "A")
        self.assertFalse(result.truncated)
