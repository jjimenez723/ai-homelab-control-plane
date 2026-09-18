"""D-059: reasoning models are measured honestly, not accidentally truncated.

`gate2-validation-001` ran `gpt-oss:20b` at `num_predict: 64` and recorded
95-100% truncation on both Gate-2 suites: the model's reasoning, which Ollama
returns on a separate ``thinking`` channel but bills against the same output
budget, consumed the whole budget before the answer began. These tests pin the
three parts of the fix -- the provider separating the two channels, the manifest
refusing a reasoning candidate at a non-reasoning budget, and the execution
path recording the reasoning fact instead of reading an empty answer as a wrong
one -- against a fake provider, with no model or network involved.
"""

from __future__ import annotations

import unittest

from tools.campaign.execution import build_provider, enumerate_work_units, execute_work_unit
from tools.campaign.fingerprint import Candidate, PromptRef, configuration_fingerprint
from tools.campaign.manifest import CampaignManifest, ManifestError, SuiteSpec, SuiteThreshold, _load_candidate
from tools.models.ollama_local import OllamaTextProvider


def _candidate(**overrides) -> Candidate:
    base = dict(
        name="cand", model="fake-reasoner", runtime="ollama",
        endpoint="http://127.0.0.1:11434", tool_schema_version="none",
        prompts={
            "acb-v1/routing-classification": PromptRef("acb-v1-routing-classification", "1.0.0"),
            "acb-v1/semantic-naming": PromptRef("acb-v1-semantic-naming", "1.0.0"),
        },
        sampling={"temperature": 0, "seed": 42, "num_ctx": 4096, "num_predict": 1024, "think": "low"},
        reasoning=True,
    )
    base.update(overrides)
    return Candidate(**base)


def _unit(candidate: Candidate):
    manifest = CampaignManifest(
        campaign_id="test-reasoning", description="d",
        suites=(SuiteSpec("acb-v1/routing-classification", "dev", SuiteThreshold()),),
        candidates=(candidate,), samples_per_item=1, timeout_seconds=30, concurrency=1,
    )
    return enumerate_work_units(manifest)[0]


class ReasoningFakeProvider:
    """A provider that thinks on one channel and answers on another."""

    def __init__(self, *, answer: str, thinking: str = "", finish_reason: str | None = None,
                 output_tokens: int = 40):
        self.answer = answer
        self.thinking = thinking
        self.finish_reason = finish_reason
        self.output_tokens = output_tokens
        self.calls: list[dict] = []

    def generate(self, prompt, **kwargs):
        self.calls.append(kwargs)
        return {
            "output": self.answer,
            "thinking": self.thinking,
            "thinking_chars": len(self.thinking),
            "think_requested": "low",
            "reasoning_declared": True,
            "input_tokens": 120,
            "output_tokens": self.output_tokens,
            "finish_reason": self.finish_reason,
        }


class ProviderChannelTests(unittest.TestCase):
    def test_think_value_is_passed_through_not_coerced_to_bool(self) -> None:
        provider = build_provider(_candidate())
        self.assertEqual(provider.think, "low")
        self.assertTrue(provider.reasoning)

    def test_num_predict_is_per_candidate(self) -> None:
        self.assertEqual(build_provider(_candidate()).num_predict, 1024)
        small = _candidate(reasoning=False, sampling={"num_predict": 128})
        self.assertEqual(build_provider(small).num_predict, 128)

    def test_an_invalid_think_value_is_refused_by_the_provider(self) -> None:
        with self.assertRaises(ValueError):
            OllamaTextProvider("http://127.0.0.1:11434", "m", think="maximum")

    def test_reasoning_is_part_of_the_configuration_fingerprint(self) -> None:
        reasoning = configuration_fingerprint(
            _candidate(), suite_id="acb-v1/routing-classification", suite_version="1.1.0")
        plain = configuration_fingerprint(
            _candidate(reasoning=False), suite_id="acb-v1/routing-classification",
            suite_version="1.1.0")
        self.assertNotEqual(reasoning, plain)


class ManifestGuardTests(unittest.TestCase):
    def _raw(self, **overrides):
        raw = {
            "name": "c", "model": "gpt-oss:20b", "runtime": "ollama",
            "reasoning": True,
            "sampling": {"num_predict": 1024, "think": "low"},
            "prompts": {"acb-v1/semantic-naming": {
                "prompt_id": "acb-v1-semantic-naming", "prompt_version": "1.0.0"}},
        }
        raw.update(overrides)
        return raw

    def test_a_reasoning_candidate_at_the_old_budget_is_refused(self) -> None:
        with self.assertRaises(ManifestError) as caught:
            _load_candidate(self._raw(sampling={"num_predict": 64}), index=0)
        self.assertIn("reasoning", str(caught.exception))
        self.assertIn("64", str(caught.exception))

    def test_a_reasoning_candidate_at_an_adequate_budget_loads(self) -> None:
        candidate = _load_candidate(self._raw(), index=0)
        self.assertTrue(candidate.reasoning)
        self.assertEqual(candidate.sampling["num_predict"], 1024)

    def test_a_non_reasoning_candidate_may_use_a_small_budget(self) -> None:
        candidate = _load_candidate(self._raw(reasoning=False, sampling={"num_predict": 64}), index=0)
        self.assertFalse(candidate.reasoning)

    def test_an_unknown_think_value_is_refused(self) -> None:
        with self.assertRaises(ManifestError):
            _load_candidate(self._raw(sampling={"num_predict": 1024, "think": "yes"}), index=0)


class ExecutionRecordingTests(unittest.TestCase):
    def test_only_the_answer_channel_is_scored(self) -> None:
        unit = _unit(_candidate())
        label = unit.item.expected["label"]
        # The reasoning text contains a *different*, wrong label. If reasoning
        # were scored (or concatenated), this would not be a pass.
        provider = ReasoningFakeProvider(
            answer=label, thinking=f"Maybe it is unsupported? No. The answer is {label}.")
        record = execute_work_unit(unit, provider)
        self.assertEqual(record.status, "verified_pass")
        self.assertEqual(record.raw_output, label)
        self.assertGreater(record.thinking_chars or 0, 0)

    def test_reasoning_that_eats_the_budget_is_truncated_not_a_wrong_answer(self) -> None:
        unit = _unit(_candidate())
        provider = ReasoningFakeProvider(
            answer="", thinking="We need to output a label. " * 20,
            finish_reason="length", output_tokens=1024)
        record = execute_work_unit(unit, provider)
        self.assertEqual(record.status, "truncated")
        self.assertFalse(record.passed)
        self.assertTrue(record.detail["reasoning"]["answer_empty_after_reasoning"])
        self.assertEqual(record.num_predict, 1024)

    def test_an_empty_answer_with_no_stop_reason_is_still_truncation(self) -> None:
        unit = _unit(_candidate())
        provider = ReasoningFakeProvider(answer="", thinking="thinking hard", finish_reason=None)
        record = execute_work_unit(unit, provider)
        self.assertEqual(record.status, "truncated")

    def test_a_deliberate_empty_answer_with_a_stop_reason_is_invalid_not_truncated(self) -> None:
        unit = _unit(_candidate())
        provider = ReasoningFakeProvider(answer="", thinking="done thinking", finish_reason="stop")
        record = execute_work_unit(unit, provider)
        # An empty answer the runtime says it finished writing is not a
        # truncation; it is a model that produced nothing usable.
        self.assertEqual(record.status, "verified_fail")
        self.assertEqual(record.detail.get("failure"), "label_not_in_closed_set")

    def test_the_requested_think_setting_is_recorded(self) -> None:
        unit = _unit(_candidate())
        provider = ReasoningFakeProvider(answer=unit.item.expected["label"], thinking="brief")
        record = execute_work_unit(unit, provider)
        self.assertEqual(record.detail["reasoning"]["think_requested"], "low")
        self.assertTrue(record.detail["reasoning"]["reasoning_declared"])

    def test_a_non_reasoning_provider_records_no_reasoning_block(self) -> None:
        unit = _unit(_candidate(reasoning=False))

        class Plain:
            def generate(self, prompt, **kwargs):
                return {"output": unit.item.expected["label"], "input_tokens": 5,
                        "output_tokens": 3, "finish_reason": "stop"}

        record = execute_work_unit(unit, Plain())
        self.assertEqual(record.status, "verified_pass")
        self.assertNotIn("reasoning", record.detail)
        self.assertIsNone(record.thinking_chars)


class TruncationPrecedenceTests(unittest.TestCase):
    def test_truncated_outranks_the_verifier_reason_for_a_half_written_answer(self) -> None:
        manifest = CampaignManifest(
            campaign_id="test-reasoning", description="d",
            suites=(SuiteSpec("acb-v1/semantic-naming", "dev", SuiteThreshold()),),
            candidates=(_candidate(),), samples_per_item=1, timeout_seconds=30, concurrency=1,
        )
        unit = enumerate_work_units(manifest)[0]
        # A naming answer cut off mid-stem: the verifier's own complaint would
        # be "wrong extension", which says nothing true about the model.
        provider = ReasoningFakeProvider(answer="2025-01-01_acme-inv",
                                         thinking="", finish_reason="length")
        record = execute_work_unit(unit, provider)
        self.assertEqual(record.status, "truncated")


if __name__ == "__main__":
    unittest.main()
