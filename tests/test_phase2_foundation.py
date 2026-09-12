from pathlib import Path

from tools.coordinator.ledger import JobLedger
from tools.models.registry import load_registry


def test_registry_is_data_driven_and_validates_capabilities(tmp_path: Path):
    registry = load_registry(Path("config/models.example.yaml"))
    assert registry.get("small-local").tier == "small_local"
    assert registry.available(tier="frontier") == []  # disabled in example config


def test_job_ledger_keeps_multiple_attempts_and_is_idempotent(tmp_path: Path):
    with JobLedger(tmp_path / "jobs.sqlite") as ledger:
        job_id = ledger.create_job("classification", {"value": "x"}, idempotency_key="fixture-1")
        assert ledger.create_job("classification", {}, idempotency_key="fixture-1") == job_id
        first = ledger.start_attempt(job_id, tier="small_local", provider="fake", model="fake-1")
        ledger.finish_attempt(first, status="failed", error_reason="verifier")
        second = ledger.start_attempt(job_id, tier="capable_local", provider="fake", model="fake-2")
        ledger.finish_attempt(second, status="succeeded", input_tokens=2, output_tokens=4)
        ledger.finish_job(job_id, disposition="success", escalated=True, escalation_reason="verifier")
        assert len(ledger.attempts(job_id)) == 2
        assert ledger.get_job(job_id)["retry_count"] == 1
