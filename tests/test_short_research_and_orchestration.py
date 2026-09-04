import json
from datetime import UTC, datetime

from research.core.contracts import ResearchResult, ResearchStatus
from research.orchestration import (
    duplicate_job_keys,
    load_job_declarations,
    present_result,
    validate_job_declarations,
)
from research.shorts import StockShortResearchWorkflow


DECISION = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)


def _row(**overrides):
    row = {
        "ticker": "ACME",
        "available_at": "2026-09-04T11:00:00+00:00",
        "event_time": "2026-09-04T10:55:00+00:00",
        "macro_regime": "bearish",
        "sector_weakness": True,
        "earnings_revision_deterioration": True,
        "valuation_support": False,
        "technical_breakdown": True,
        "catalyst": "guidance risk",
        "positioning_crowding": False,
        "source": "fixture",
    }
    row.update(overrides)
    return row


def test_short_scan_requires_multiple_non_macro_factors_and_keeps_rejections():
    result = StockShortResearchWorkflow().scan(
        [_row(), _row(ticker="MACRO", sector_weakness=False, earnings_revision_deterioration=False, technical_breakdown=False, catalyst=None)],
        decision_time=DECISION,
    )

    assert result.status is ResearchStatus.SETUP_FOUND
    assert result.payload["universe_scanned"] == 2
    assert result.payload["final_candidates"][0]["asset"] == "ACME"
    assert result.payload["rejected_candidates"][0]["reason"] == "macro_only"


def test_short_scan_rejects_unknown_availability_and_is_read_only():
    result = StockShortResearchWorkflow().scan([_row(available_at=None)], decision_time=DECISION)

    assert result.status is ResearchStatus.INSUFFICIENT_EVIDENCE
    assert result.payload["rejected_candidates"][0]["reason"] == "availability_unknown"
    assert result.payload["execution_enabled"] is False


def test_short_evaluation_and_missed_moves_are_explicit():
    workflow = StockShortResearchWorkflow()
    evaluation = workflow.evaluate(
        [{"ticker": "ACME", "selected": True, "short_return_pct": 12.0}],
        decision_time=DECISION,
    )
    missed = workflow.missed_moves(
        [{"ticker": "MISSED", "available_at": "2026-09-04T10:00:00+00:00", "later_move_pct": 35, "failed_filter": "sector_weakness"}],
        decision_time=DECISION,
    )

    assert evaluation.status is ResearchStatus.STRATEGY_NOT_VALIDATED
    assert evaluation.payload["metrics"]["hit_rate"] == 1.0
    assert missed.status is ResearchStatus.ACTION_REQUIRED
    assert missed.payload["hindsight_data_used_as_signal"] is False


def test_quant_presentation_preserves_no_setup_and_action_required():
    workflow = StockShortResearchWorkflow()
    no_setup = workflow.scan([], decision_time=DECISION)
    action = workflow.missed_moves(
        [{"ticker": "MISSED", "available_at": "2026-09-04T10:00:00+00:00", "later_move_pct": 35}],
        decision_time=DECISION,
    )

    no_setup_view = present_result(no_setup)
    action_view = present_result(action.to_dict())
    assert no_setup_view["status"] == "INSUFFICIENT_EVIDENCE"
    assert action_view["status"] == "ACTION_REQUIRED"
    assert action_view["human_decision_required"] is True
    assert isinstance(action, ResearchResult)


def test_job_manifest_has_no_enabled_unready_jobs_and_detects_duplicates():
    jobs = load_job_declarations(__import__("pathlib").Path("ops/quant_nave_jobs.json"))
    assert not validate_job_declarations(jobs)
    assert duplicate_job_keys(jobs) == []
    duplicate = [jobs[0], jobs[0]]
    assert duplicate_job_keys(duplicate) == [jobs[0].key]
    assert validate_job_declarations(duplicate)[0].startswith("duplicate key:")

