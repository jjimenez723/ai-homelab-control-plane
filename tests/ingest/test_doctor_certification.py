"""`organize doctor` tells the operator whether the semantic-naming model is a
certified configuration (D-060) -- advisory, read-only, never raising."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tools.ingest.config import IngestConfig, NamingConfig, SemanticNamingConfig
from tools.ingest.status import semantic_certification_note

MODELS = """
models:
  - name: capable-local
    provider: ollama
    runtime: ollama
    model_id: qwen3.5:9b
    tier: capable_local
    capabilities: [text_generation, structured_output]
    context_length: 32768
    quantization: Q4_K_M
    local: true
    enabled: true
"""
CERTS_TEMPLATE = """
version: 1
certifications:
  - model: capable-local
    task_class: naming.semantic
    suite: acb-v1/semantic-naming
    suite_version: "1.1.0"
    runtime: ollama
    quantization: Q4_K_M
    prompt_id: acb-v1-semantic-naming
    prompt_version: "{prompt_version}"
    tool_schema_version: "none"
    threshold: 0.75
    measured_pass_rate: 1.0
    measured_samples: 70
    p95_latency_seconds: 2.36
    peak_rss_bytes: 5498564770
    evaluated_at: 2026-09-18
    expires_at: 2027-03-18
    status: active
"""


def _config(model: str) -> IngestConfig:
    return IngestConfig(naming=NamingConfig(proposer="semantic", semantic=SemanticNamingConfig(model=model)))


class SemanticCertificationNoteTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        (self.root / "config").mkdir()

    def _write(self, models: str | None, certs: str | None) -> None:
        if models is not None:
            (self.root / "config" / "models.yaml").write_text(models)
        if certs is not None:
            (self.root / "config" / "certifications.yaml").write_text(certs)

    def test_no_registry_file_is_reported_not_raised(self):
        self.assertIn("models.yaml is absent", semantic_certification_note(_config("qwen3.5:9b"), repo_root=self.root))

    def test_no_certifications_file_is_not_certified(self):
        self._write(MODELS, None)
        self.assertTrue(semantic_certification_note(_config("qwen3.5:9b"), repo_root=self.root).startswith("not certified"))

    def test_a_certified_tag_at_the_active_prompt_is_reported_as_certified(self):
        from tools.prompts.registry import load_default_registry

        active = load_default_registry().active("acb-v1-semantic-naming").version
        self._write(MODELS, CERTS_TEMPLATE.format(prompt_version=active))
        note = semantic_certification_note(_config("qwen3.5:9b"), repo_root=self.root)
        self.assertTrue(note.startswith("certified as capable-local"), note)
        self.assertIn(active, note)

    def test_a_certification_pinned_to_another_prompt_version_does_not_count(self):
        self._write(MODELS, CERTS_TEMPLATE.format(prompt_version="0.0.1"))
        note = semantic_certification_note(_config("qwen3.5:9b"), repo_root=self.root)
        self.assertTrue(note.startswith("not certified"), note)
        self.assertIn("capable-local", note)

    def test_an_unregistered_tag_is_not_certified(self):
        self._write(MODELS, CERTS_TEMPLATE.format(prompt_version="1.1.0"))
        note = semantic_certification_note(_config("qwen3.5:4b"), repo_root=self.root)
        self.assertIn("no config/models.yaml entry has model_id 'qwen3.5:4b'", note)

    def test_a_corrupt_file_degrades_to_unknown(self):
        self._write("models: [", CERTS_TEMPLATE.format(prompt_version="1.1.0"))
        self.assertTrue(semantic_certification_note(_config("qwen3.5:9b"), repo_root=self.root).startswith("unknown"))


if __name__ == "__main__":
    unittest.main()
