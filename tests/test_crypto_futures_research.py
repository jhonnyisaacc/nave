from datetime import UTC, datetime

from research.core.contracts import ResearchStatus
from research.core.store import ResearchStore
from research.crypto_futures import CryptoFuturesWorkflow, analyze_missed_moves, build_funnel, cot_regime_passes


NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)


def candidate(*, symbol="ALT", rank=90, liquidity="PASS", setup_valid=True, direction="long"):
    return {
        "symbol": symbol,
        "canonical_asset_id": symbol.lower(),
        "ranking_state": "ELIGIBLE" if rank >= 80 else "BELOW_RANK_THRESHOLD",
        "rank_score": rank,
        "features": {"data_timestamp": NOW.isoformat(), "data_source": "fixture"},
        "liquidity": {"state": liquidity},
        "setup_validation": {
            "valid": setup_valid,
            "direction": direction,
            "entry_zone": [100.0, 101.0],
            "invalidation": 95.0,
        },
        "evidence": {"facts": ["fixture"], "unknowns": []},
        "observation_timestamp": NOW.isoformat(),
    }


def replay(*candidates):
    return {
        "observations": [{
            "observation_timestamp": NOW.isoformat(),
            "source": "fixture",
            "source_timestamp": NOW.isoformat(),
            "universe_members_deduplicated": [{"symbol": item["symbol"]} for item in candidates],
            "candidates": list(candidates),
        }],
        "window": {"start": NOW.isoformat(), "end": NOW.isoformat()},
        "outcomes": [],
        "metrics": {},
    }


def test_cot_is_market_regime_context_not_an_altcoin_signal():
    assert cot_regime_passes("bullish", "long") is True
    assert cot_regime_passes("bearish", "long") is False
    assert cot_regime_passes("neutral", "short") is True
    assert cot_regime_passes("bullish", None) is False


def test_funnel_exposes_all_stages_and_suppresses_without_validated_context():
    funnel, final, _ = build_funnel(replay(candidate()), cot_regime="neutral")
    assert funnel["universe"] == 1
    assert funnel["eligible"] == 1
    assert funnel["liquid"] == 1
    assert funnel["momentum_pass"] == 1
    assert funnel["derivatives_pass"] == 1
    assert funnel["macro_pass"] == 0
    assert funnel["final_candidates"] == 0
    assert final == []


def test_validated_macro_and_cot_context_produce_research_candidate(tmp_path):
    workflow = CryptoFuturesWorkflow(store=ResearchStore(tmp_path))
    result = workflow.scan_payload(
        replay(candidate()),
        macro_context={"validated": True, "confidence": 0.8},
        cot_regime="neutral",
        now=NOW,
    )
    assert result.status is ResearchStatus.SETUP_FOUND
    assert result.payload["funnel"]["final_candidates"] == 1
    assert result.payload["final_candidates"][0]["asset"] == "ALT"
    assert result.payload["final_candidates"][0]["filters_passed"]["cot_regime_pass"] is True
    assert workflow.status()["crypto.futures.scan"]["status"] == "SETUP_FOUND"


def test_no_setup_still_persists_scanned_evidence(tmp_path):
    result = CryptoFuturesWorkflow(store=ResearchStore(tmp_path)).scan_payload(
        replay(candidate(rank=50, liquidity="REJECT", setup_valid=False)),
        cot_regime="unknown",
        now=NOW,
    )
    assert result.status is ResearchStatus.NO_SETUP
    assert result.payload["funnel"]["universe"] == 1
    assert result.payload["funnel"]["final_candidates"] == 0
    assert result.evidence


def test_evaluation_and_missed_moves_are_separate_audits(tmp_path):
    workflow = CryptoFuturesWorkflow(store=ResearchStore(tmp_path))
    scan = workflow.scan_payload(
        replay(candidate(symbol="SELECTED"), candidate(symbol="MISSED", rank=50, liquidity="REJECT", setup_valid=False)),
        macro_context={"validated": True},
        cot_regime="neutral",
        now=NOW,
    )
    evaluation = workflow.evaluate(
        scan_result=scan,
        outcomes=[{"asset": "SELECTED", "forward_return": 0.12, "regime": "neutral"}],
    )
    assert evaluation.status is ResearchStatus.STRATEGY_NOT_VALIDATED
    assert evaluation.payload["metrics"]["hit_rate"] == 1.0
    missed = workflow.missed_moves(
        scan_result=scan,
        outcomes=[
            {
                "asset": "MISSED",
                "forward_return": 0.40,
                "universe_membership": True,
                "information_available_at": "2026-09-04T12:01:00+00:00",
                "possible_missing_feature": "volume_acceleration",
            }
        ],
    )
    assert missed.status is ResearchStatus.ACTION_REQUIRED
    row = missed.payload["missed_moves"][0]
    assert row["rejection_filters"] == ["momentum_or_rank", "derivatives_liquidity", "market_structure_or_setup"]
    assert row["information_existed_before_move"] == "AFTER_DECISION"
    assert row["possible_systematic_blind_spot"] == "volume_acceleration"


def test_missed_move_analysis_does_not_treat_unavailable_information_as_known():
    scan = {"final_candidates": [], "observations": []}
    rows = analyze_missed_moves(
        scan,
        [{"asset": "ALT", "forward_return": 0.3, "information_available_at": None}],
    )
    assert rows[0]["information_existed_before_move"] == "UNKNOWN"
