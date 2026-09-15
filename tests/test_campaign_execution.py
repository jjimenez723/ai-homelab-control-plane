"""Work-unit enumeration and execution: status classification, never raises."""

from __future__ import annotations

import unittest

from tools.campaign.execution import enumerate_work_units, execute_work_unit
from tools.campaign.fingerprint import Candidate, PromptRef
from tools.campaign.manifest import CampaignManifest, SuiteSpec, SuiteThreshold


def _candidate(**overrides) -> Candidate:
    base = dict(
        name="cand", model="fake-model", runtime="ollama", endpoint="http://127.0.0.1:11434",
        tool_schema_version="none",
        prompts={
            "acb-v1/routing-classification": PromptRef("acb-v1-routing-classification", "1.0.0"),
            "acb-v1/semantic-naming": PromptRef("acb-v1-semantic-naming", "1.0.0"),
        },
        sampling={"temperature": 0, "seed": 42, "num_ctx": 4096, "num_predict": 64},
    )
    base.update(overrides)
    return Candidate(**base)


def _manifest(*, suite_id="acb-v1/routing-classification", split="dev", samples=1) -> CampaignManifest:
    return CampaignManifest(
        campaign_id="test", description="d",
        suites=(SuiteSpec(suite_id, split, SuiteThreshold()),),
        candidates=(_candidate(),), samples_per_item=samples, timeout_seconds=30, concurrency=1,
    )


class FakeProvider:
    def __init__(self, *, output=None, output_tokens=3, exc=None):
        self.output = output
        self.output_tokens = output_tokens
        self.exc = exc
        self.calls = []

    def generate(self, prompt, **kwargs):
        self.calls.append((prompt, kwargs))
        if self.exc is not None:
            raise self.exc
        return {"output": self.output, "input_tokens": 5, "output_tokens": self.output_tokens}


class EnumerateWorkUnitsTests(unittest.TestCase):
    def test_count_equals_items_times_samples(self) -> None:
        manifest = _manifest(samples=3)
        units = enumerate_work_units(manifest)
        from tools.campaign.suites import suite as lookup_suite
        n_items = len(lookup_suite("acb-v1/routing-classification").by_split("dev"))
        self.assertEqual(len(units), n_items * 3)

    def test_every_sample_index_gets_a_distinct_work_unit_id(self) -> None:
        manifest = _manifest(samples=5)
        units = [u for u in enumerate_work_units(manifest) if u.item.id == enumerate_work_units(manifest)[0].item.id]
        ids = {u.work_unit_id for u in units}
        self.assertEqual(len(ids), len(units))

    def test_two_identical_manifests_produce_identical_work_unit_ids(self) -> None:
        a = [u.work_unit_id for u in enumerate_work_units(_manifest())]
        b = [u.work_unit_id for u in enumerate_work_units(_manifest())]
        self.assertEqual(a, b)


class ExecuteWorkUnitTests(unittest.TestCase):
    def _one_unit(self, **manifest_kwargs):
        manifest = _manifest(**manifest_kwargs)
        return enumerate_work_units(manifest)[0]

    def test_correct_answer_is_verified_pass(self) -> None:
        unit = self._one_unit()
        provider = FakeProvider(output=unit.item.expected["label"])
        record = execute_work_unit(unit, provider)
        self.assertEqual(record.status, "verified_pass")
        self.assertTrue(record.passed)

    def test_wrong_answer_is_verified_fail(self) -> None:
        unit = self._one_unit()
        provider = FakeProvider(output="general" if unit.item.expected["label"] != "general" else "unsupported")
        record = execute_work_unit(unit, provider)
        self.assertEqual(record.status, "verified_fail")
        self.assertFalse(record.passed)

    def test_timeout_is_recorded_as_infrastructure_not_model_failure(self) -> None:
        unit = self._one_unit()
        provider = FakeProvider(exc=TimeoutError("slow"))
        record = execute_work_unit(unit, provider)
        self.assertEqual(record.status, "timeout")
        self.assertIsNone(record.passed)

    def test_connection_error_is_provider_unavailable(self) -> None:
        unit = self._one_unit()
        provider = FakeProvider(exc=ConnectionError("down"))
        record = execute_work_unit(unit, provider)
        self.assertEqual(record.status, "provider_unavailable")

    def test_value_error_is_provider_error(self) -> None:
        unit = self._one_unit()
        provider = FakeProvider(exc=ValueError("bad response"))
        record = execute_work_unit(unit, provider)
        self.assertEqual(record.status, "provider_error")

    def test_output_at_the_num_predict_ceiling_and_wrong_is_truncated_not_verifier_reject(self) -> None:
        unit = self._one_unit()
        provider = FakeProvider(output="not-a-real-label", output_tokens=64)  # == num_predict
        record = execute_work_unit(unit, provider)
        self.assertEqual(record.status, "truncated")

    def test_non_string_output_is_invalid_output(self) -> None:
        unit = self._one_unit()
        provider = FakeProvider(output=None, output_tokens=1)
        record = execute_work_unit(unit, provider)
        self.assertEqual(record.status, "invalid_output")

    def test_verifier_crash_is_recorded_as_verifier_error(self) -> None:
        unit = self._one_unit(suite_id="acb-v1/semantic-naming")
        provider = FakeProvider(output=object())  # something naming's verify() cannot handle cleanly
        # naming.verify already handles non-str gracefully (invalid_output), so force a real crash:
        import tools.campaign.execution as execmod
        original = execmod.lookup_verifier

        def _boom(_suite_id):
            def _raise(*_a, **_kw):
                raise RuntimeError("oracle exploded")
            return _raise

        execmod.lookup_verifier = _boom
        try:
            record = execute_work_unit(unit, provider)
        finally:
            execmod.lookup_verifier = original
        self.assertEqual(record.status, "verifier_error")
        self.assertIsNone(record.passed)

    def test_result_record_carries_prompt_identity(self) -> None:
        unit = self._one_unit()
        provider = FakeProvider(output=unit.item.expected["label"])
        record = execute_work_unit(unit, provider)
        self.assertEqual(record.prompt_id, "acb-v1-routing-classification")
        self.assertEqual(record.prompt_version, "1.0.0")
        self.assertEqual(len(record.prompt_sha256), 64)
        self.assertEqual(record.work_unit_id, unit.work_unit_id)
        self.assertEqual(record.configuration_fingerprint, unit.configuration_fingerprint)


if __name__ == "__main__":
    unittest.main()
