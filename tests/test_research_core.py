from datetime import UTC, datetime, timedelta

import pytest

from research.core.contracts import (
    EvidenceKind,
    EvidenceReference,
    PointInTime,
    ResearchResult,
    ResearchStatus,
    RunMetadata,
)
from research.core.strategy import UnsupportedPhase, run_phase
from research.core.store import ResearchStore


NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)


def metadata() -> RunMetadata:
    return RunMetadata(
        strategy_name="fixture",
        strategy_version="1.0.0",
        run_id="run-1",
        decision_time=NOW,
        started_at=NOW - timedelta(seconds=1),
        completed_at=NOW,
        input_available_at=NOW - timedelta(minutes=1),
    )


def evidence(*, available_at: datetime | None = NOW - timedelta(minutes=2)) -> EvidenceReference:
    return EvidenceReference(
        reference_id="e-1",
        source="fixture",
        claim="A source-backed claim",
        kind=EvidenceKind.FACT,
        confidence=0.9,
        point_in_time=PointInTime(
            event_time=NOW - timedelta(hours=1),
            available_at=available_at,
            decision_time=NOW,
        ),
        citation="https://example.test/e-1",
    )


def test_status_serialization_and_json_round_trip():
    result = ResearchResult(
        workflow="fixture.scan",
        status=ResearchStatus.NO_SETUP,
        metadata=metadata(),
        payload={"universe_scanned": 10},
        evidence=(evidence(),),
    )

    payload = result.to_dict()
    assert payload["status"] == "NO_SETUP"
    assert payload["safety_boundary"] == "READ_ONLY_RESEARCH_ONLY_HUMAN_GATED"
    assert ResearchResult.from_dict(payload).to_dict() == payload


def test_markdown_report_exposes_status_and_timestamp_semantics():
    result = ResearchResult(
        workflow="fixture.scan",
        status=ResearchStatus.INSUFFICIENT_EVIDENCE,
        metadata=metadata(),
        evidence=(evidence(available_at=None),),
        warnings=("availability could not be established",),
    )

    markdown = result.to_markdown()
    assert "INSUFFICIENT_EVIDENCE" in markdown
    assert "Decision time:" in markdown
    assert "UNKNOWN" in markdown
    assert "availability could not be established" in markdown


def test_point_in_time_does_not_fake_missing_availability():
    point = PointInTime(event_time=NOW - timedelta(days=1), decision_time=NOW)
    assert point.availability == "UNKNOWN"
    late = PointInTime(available_at=NOW + timedelta(seconds=1), decision_time=NOW)
    assert late.availability == "LATE"


def test_incomplete_state_is_rejected():
    with pytest.raises(ValueError, match="requires at least one evidence"):
        ResearchResult(
            workflow="fixture.scan",
            status=ResearchStatus.SETUP_FOUND,
            metadata=metadata(),
        ).to_dict()

    with pytest.raises(ValueError, match="requires a visible warning"):
        ResearchResult(
            workflow="fixture.scan",
            status=ResearchStatus.ERROR,
            metadata=metadata(),
        ).to_dict()


def test_store_persists_results_and_contexts_atomically(tmp_path):
    store = ResearchStore(tmp_path)
    result = ResearchResult(
        workflow="fixture.scan",
        status=ResearchStatus.DATA_UNAVAILABLE,
        metadata=metadata(),
        warnings=("provider unavailable",),
    )
    path = store.save_result(result)
    assert path.exists()
    assert store.load_result("fixture.scan").status is ResearchStatus.DATA_UNAVAILABLE
    store.save_context("macro", {"regime": "unknown"})
    assert store.load_context("macro") == {"regime": "unknown"}


class PartialStrategy:
    name = "partial"
    version = "1"

    def scan(self, value: int) -> int:
        return value + 1


def test_strategy_phases_are_optional():
    strategy = PartialStrategy()
    assert run_phase(strategy, "scan", 2) == 3
    with pytest.raises(UnsupportedPhase):
        run_phase(strategy, "evaluate")
