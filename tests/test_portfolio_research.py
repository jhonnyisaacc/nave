from datetime import UTC, datetime

from research.core.contracts import ResearchStatus
from research.portfolio import (
    PortfolioState,
    PositionState,
    check_watch,
    ism_rank,
    portfolio_candidates,
    review_positions,
)


NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)


def ism_payload():
    candidate = {
        "symbol": "ABC",
        "company_name": "Example Co",
        "sector": "Technology",
        "industry": "Software",
        "driver_industry": "Software",
        "confidence": 0.9,
        "match_confidence": 0.9,
    }
    report = {
        "report_month": "2026-08",
        "pmi": 52.0,
        "source_url": "https://example.test/ism",
        "hottest_industries": [{"industry": "Software", "rank": 1}],
        "candidates": {"longs": [candidate]},
    }
    return report, {**report, "pmi": 51.0}


def test_position_review_preserves_human_gated_actions_and_thesis():
    state = PortfolioState(positions=(PositionState("ABC", thesis="original thesis", source_strategy="ism"),))
    result = review_positions(
        state,
        {"ABC": {"macro_regime": "neutral", "technical_condition": "healthy"}},
        now=NOW,
    )
    assert result.status is ResearchStatus.ACTION_REQUIRED
    decision = result.payload["positions"][0]
    assert decision["action"] == "HOLD"
    assert decision["thesis"] == "original thesis"
    assert decision["source_strategy"] == "ism"
    assert decision["human_decision_required"] is True


def test_position_review_emits_review_or_exit_without_execution():
    state = PortfolioState(positions=(PositionState("ABC", candidate_status="broken"),))
    result = review_positions(state, {"ABC": {"invalidation": True}}, now=NOW)
    assert result.payload["positions"][0]["action"] == "EXIT_CANDIDATE"
    assert result.payload["human_decision_required"] is True


def test_ism_ranking_uses_both_reports_and_preserves_provenance():
    manufacturing, services = ism_payload()
    result = ism_rank(manufacturing, services, now=NOW)
    assert result.status is ResearchStatus.SETUP_FOUND
    row = result.payload["candidates"][0]
    assert row["ticker"] == "ABC"
    assert row["why_is_this_here"] == ["ISM manufacturing candidate", "ISM services candidate"]
    assert result.payload["mapping"].startswith("ISM industries")


def test_candidates_do_not_turn_ism_into_buy_signal():
    manufacturing, services = ism_payload()
    result = portfolio_candidates(manufacturing, services, now=NOW)
    assert result.payload["candidates"][0]["status"] == "RESEARCH_REQUIRED"
    assert result.payload["human_decision_required"] is True
    assert "standalone buy" in result.warnings[0]


def test_watch_threshold_is_deterministic_and_does_not_escalate_model():
    result = check_watch(
        [{"ticker": "ABC", "threshold": 100.0, "thesis": "watch"}],
        {"ABC": 101.0},
        now=NOW,
    )
    assert result.status is ResearchStatus.ACTION_REQUIRED
    assert result.payload["events"][0]["event"] == "ZONE_REACHED"
    assert result.payload["model_escalation"] is False


def test_watch_supports_explicit_above_below_and_cross_conditions():
    watches = [
        {"ticker": "A", "condition": "ABOVE", "threshold": 100},
        {"ticker": "B", "condition": "BELOW", "threshold": 100},
        {"ticker": "C", "condition": "CROSS_ABOVE", "threshold": 100},
        {"ticker": "D", "condition": "CROSS_BELOW", "threshold": 100},
    ]
    result = check_watch(
        watches,
        {"A": 101, "B": 99, "C": 101, "D": 99},
        previous_prices={"C": 99, "D": 101},
        now=NOW,
    )
    assert {item["event"] for item in result.payload["events"]} == {
        "ABOVE", "BELOW", "CROSS_ABOVE", "CROSS_BELOW"
    }


def test_watch_zone_requires_explicit_bounds():
    result = check_watch(
        [{"ticker": "ABC", "condition": "ZONE", "zone": [95, 105]}],
        {"ABC": 100},
        now=NOW,
    )
    assert result.payload["events"][0]["zone"] == {"lower": 95.0, "upper": 105.0}
