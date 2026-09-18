"""D-060: the real certification file, checked where it exists.

`config/certifications.yaml` is machine state (gitignored): a certification
names the exact local model digest and quantization measured on one host. So
these tests assert two different things:

* the committed *example* stays the empty list -- unchanged from D-051, and
  still asserted by `tests/test_autonomous_routing.py`;
* the real file, **if this machine has one**, loads, meets its own thresholds,
  and is pinned to prompt and suite versions that actually exist in the
  registry. A certification pinned to a prompt version nobody can resolve is
  the one failure mode a loader cannot catch on its own.

Skipping when the file is absent is deliberate: it makes the test honest on a
machine that has certified nothing, rather than either failing there or
quietly asserting nothing everywhere.
"""

from __future__ import annotations

import datetime as dt
import unittest
from pathlib import Path

from tools.bench.acb_v1 import routing_classification as rc
from tools.bench.acb_v1 import semantic_naming as sn
from tools.models.certification import load_certifications
from tools.models.task_classes import suite_for
from tools.prompts.registry import load_default_registry

_CONFIG = Path(__file__).resolve().parents[1] / "config"
_REAL = _CONFIG / "certifications.yaml"
_EXAMPLE = _CONFIG / "certifications.example.yaml"

_SUITE_VERSIONS = {
    rc.SUITE_ID: rc.SUITE_VERSION,
    sn.SUITE_ID: sn.SUITE_VERSION,
}


class ExampleStaysEmptyTests(unittest.TestCase):
    def test_the_committed_example_certifies_nothing(self) -> None:
        self.assertEqual(load_certifications(_EXAMPLE).all(), ())


@unittest.skipUnless(_REAL.exists(), "this machine has no config/certifications.yaml")
class RealCertificationFileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.records = load_certifications(_REAL).all()

    def test_it_is_not_empty_if_it_exists(self) -> None:
        self.assertTrue(self.records, "an existing certification file should hold a record")

    def test_every_active_record_meets_its_own_threshold(self) -> None:
        for record in self.records:
            if record.status == "active":
                self.assertTrue(record.meets_threshold(),
                                f"{record.model}/{record.task_class} is active below threshold")

    def test_every_record_pins_a_prompt_version_that_exists(self) -> None:
        registry = load_default_registry()
        for record in self.records:
            # Raises UnknownPrompt if the pinned version is not in the library.
            prompt = registry.get(record.prompt_id, record.prompt_version)
            self.assertEqual(prompt.prompt_id, record.prompt_id)

    def test_every_record_pins_the_current_suite_version(self) -> None:
        # A certification measured against an older suite version is not wrong,
        # but it is not evidence about today's suite either -- so it must be
        # noticed rather than silently trusted.
        for record in self.records:
            expected = _SUITE_VERSIONS.get(record.suite)
            self.assertIsNotNone(expected, f"unknown suite {record.suite!r}")
            self.assertEqual(record.suite_version, expected,
                             f"{record.model}/{record.task_class} was measured against "
                             f"suite {record.suite} {record.suite_version}, but the suite is "
                             f"now {expected}")

    def test_every_record_names_the_suite_that_gates_its_task_class(self) -> None:
        for record in self.records:
            self.assertEqual(record.suite, suite_for(record.task_class))

    def test_no_record_is_already_expired(self) -> None:
        today = dt.date.today()
        for record in self.records:
            self.assertFalse(record.is_expired(today),
                             f"{record.model}/{record.task_class} expired {record.expires_at}")

    def test_every_record_carries_a_measured_resource_envelope(self) -> None:
        # D-042/PLAN Gate-2 clause 6: "measured reliability/resource envelopes".
        # A record without them is a pass rate wearing a certification's clothes.
        for record in self.records:
            self.assertIsNotNone(record.p95_latency_seconds, record.task_class)
            self.assertIsNotNone(record.peak_rss_bytes, record.task_class)
            self.assertGreater(record.measured_samples, 0)

    def test_known_failure_modes_are_recorded_not_omitted(self) -> None:
        for record in self.records:
            self.assertTrue(record.known_failure_modes,
                            f"{record.task_class}: a certification with no recorded failure "
                            "modes is either untrue or unexamined")


if __name__ == "__main__":
    unittest.main()
