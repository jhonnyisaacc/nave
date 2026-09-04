from datetime import UTC, datetime, timedelta

from research.core.contracts import ResearchStatus
from research.core.store import ResearchStore
from research.memecoin_workflow import MemecoinResearchWorkflow, discover_rows, missed_moves
from research.dune.materializer import DuneMaterializer


NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)


def row(asset="ALT", *, available_at=NOW, risk="PASS", volume=3.0, liquidity=50_000):
    return {
        "asset": asset,
        "mint": f"mint-{asset}",
        "decision_time": NOW.isoformat(),
        "available_at": available_at.isoformat() if available_at else None,
        "features": {
            "volume_acceleration": volume,
            "liquidity_usd": liquidity,
            "risk_status": risk,
            "holder_structure": "distributed",
            "wallet_activity": "observed",
            "narrative": "fixture",
        },
    }


def test_point_in_time_eligibility_rejects_future_feature():
    result = discover_rows([row(available_at=NOW + timedelta(minutes=1))])
    assert result["eligible_count"] == 0
    assert result["rejected"][0]["rejection_filters"] == ["hindsight_feature_not_available_at_decision"]


def test_discovery_output_and_case_study_do_not_overfit_meme(tmp_path):
    workflow = MemecoinResearchWorkflow(store=ResearchStore(tmp_path))
    result = workflow.discover([row("ALT"), row("MEME")])
    assert result.status is ResearchStatus.SETUP_FOUND
    assert {item["asset"] for item in result.payload["selected"]} == {"ALT", "MEME"}
    assert result.payload["case_study"]["overfit_guard"] == "no asset-specific rule added"


def test_cached_dune_path_does_not_claim_unmeasured_remote_usage(tmp_path):
    cache = tmp_path / "dune.json"
    cache.write_text(__import__("json").dumps({"rows": [row()]}), encoding="utf-8")
    result = MemecoinResearchWorkflow(store=ResearchStore(tmp_path / "state")).discover([], dune_cache=cache)
    assert result.payload["dune_usage"]["mode"] == "cached"
    assert result.payload["dune_usage"]["query_executed"] is False
    assert result.payload["dune_usage"]["actual_credits"] is None


def test_evaluation_and_missed_move_have_no_hindsight_leak(tmp_path):
    workflow = MemecoinResearchWorkflow(store=ResearchStore(tmp_path))
    scan = workflow.discover([row("SELECTED"), row("MISSED", volume=0.5, liquidity=1_000, risk="FAIL")])
    evaluation = workflow.evaluate(scan_result=scan, outcomes=[{"asset": "SELECTED", "later_move_pct": 0.2}])
    assert evaluation.status is ResearchStatus.STRATEGY_NOT_VALIDATED
    missed = workflow.missed_moves(
        scan_result=scan,
        outcomes=[{
            "asset": "MISSED",
            "later_move_pct": 3.0,
            "information_available_at": (NOW + timedelta(hours=1)).isoformat(),
            "possible_missing_feature": "wallet_cluster_velocity",
        }],
    )
    assert missed.status is ResearchStatus.ACTION_REQUIRED
    assert missed.payload["missed_moves"][0]["information_existed_before_move"] == "AFTER_DECISION"
    assert missed.payload["missed_moves"][0]["possible_missing_feature"] == "wallet_cluster_velocity"


def test_malformed_pair_regression_path_remains_local_and_explicit():
    rows = [row("GOOD"), {"asset": "BAD", "decision_time": NOW.isoformat(), "available_at": NOW.isoformat(), "features": {"risk_status": "FAIL"}}]
    result = discover_rows(rows)
    assert result["universe_count"] == 2
    assert any(item["asset"] == "BAD" for item in result["rejected"])


def test_malformed_availability_is_not_coerced_to_decision_time():
    malformed = row()
    malformed["available_at"] = "not-a-timestamp"
    result = discover_rows([malformed])
    assert result["eligible_count"] == 0
    assert result["rejected"][0]["rejection_filters"] == ["invalid_feature_availability"]


def test_case_study_is_only_present_when_meme_is_in_the_snapshot(tmp_path):
    workflow = MemecoinResearchWorkflow(store=ResearchStore(tmp_path))
    result = workflow.discover([row("ALT")])
    assert result.payload["case_study"] is None


def test_dune_materializer_runs_once_then_reuses_matching_cache(tmp_path):
    executable = tmp_path / "dune"
    executable.write_text(
        "#!/bin/sh\nprintf '%s' '{\"rows\":[{\"asset\":\"ALT\"}],\"credits_used\":1.5}'\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    output = tmp_path / "materialized.json"
    materializer = DuneMaterializer(executable=str(executable))
    first = materializer.materialize(query_id="123", output=output)
    second = materializer.materialize(query_id="123", output=output)
    assert first["query_executed"] is True
    assert first["credit_usage"]["actual"] == 1.5
    assert second["cache_hit"] is True
    assert second["query_executed"] is False
