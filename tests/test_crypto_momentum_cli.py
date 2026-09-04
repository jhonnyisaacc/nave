from __future__ import annotations

import json

from typer.testing import CliRunner

from cli.commands.crypto import _format_entry_reference
from cli.main import app

runner = CliRunner()


def test_cli_registers_crypto_group() -> None:
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "crypto" in result.stdout


def test_crypto_momentum_scan_json(monkeypatch) -> None:
    payload = {
        "strategy": "derivatives_momentum_v1",
        "symbols": ["BTCUSDT", "ETHUSDT"],
        "summary": {
            "tradeable_count": 1,
            "cadence_state": "normal",
            "recommended_score_threshold": 90,
            "effective_score_threshold": 90,
            "cadence_policy_applied": False,
        },
        "cadence": {"state": "normal", "recommended_threshold": 90, "effective_threshold": 90, "applied": False},
        "results": {
            "BTCUSDT": {"plans": [{"side": "long", "tradeable": True, "confidence_score": 88, "setup_status": "confirmed", "entry_zone": [100.0, 101.0], "invalidation": 98.0, "tp2": 109.0, "rr_estimated": 2.2, "expected_move_pct": 0.08}], "tradeable": []},
            "ETHUSDT": {"plans": [], "tradeable": []},
        },
    }

    from trading.crypto.momentum.service import MomentumMarketService

    monkeypatch.setattr(MomentumMarketService, "scan_live", lambda self, **kwargs: payload)
    result = runner.invoke(app, ["crypto", "momentum-scan", "--symbols", "BTCUSDT,ETHUSDT", "--json"])

    assert result.exit_code == 0
    decoded = json.loads(result.stdout)
    assert decoded["strategy"] == "derivatives_momentum_v1"
    assert decoded["summary"]["tradeable_count"] == 1
    assert decoded["summary"]["recommended_score_threshold"] == 90
    assert decoded["summary"]["effective_score_threshold"] == 90
    assert decoded["cadence"]["state"] == "normal"


def test_crypto_momentum_scan_can_append_universe_discovery(monkeypatch) -> None:
    captured: dict[str, object] = {}
    payload = {
        "strategy": "derivatives_momentum_v1",
        "symbols": ["BTCUSDT", "ETHUSDT"],
        "summary": {"tradeable_count": 0},
        "cadence": {},
        "results": {},
        "universe_discovery": {"mode": "current_research_only", "status": "OK"},
    }

    from trading.crypto.momentum.service import MomentumMarketService

    def fake_scan_live(self, **kwargs):
        captured.update(kwargs)
        return payload

    monkeypatch.setattr(MomentumMarketService, "scan_live", fake_scan_live)
    result = runner.invoke(
        app,
        [
            "crypto",
            "momentum-scan",
            "--include-universe-discovery",
            "--universe-size",
            "50",
            "--json",
        ],
    )

    assert result.exit_code == 0
    assert captured["include_universe_discovery"] is True
    assert captured["universe_size"] == 50


def test_crypto_momentum_playbook_json(monkeypatch) -> None:
    payload = {
        "strategy": "derivatives_momentum_v1",
        "symbol": "BTCUSDT",
        "plan": {
            "side": "short",
            "setup_status": "confirmed",
            "tradeable": True,
            "confidence_score": 90,
            "entry_zone": [100.0, 101.0],
            "invalidation": 102.0,
            "tp1": 96.0,
            "tp2": 92.0,
            "tp3": 88.0,
            "rr_estimated": 2.0,
            "expected_move_pct": 0.09,
        },
    }

    from trading.crypto.momentum.service import MomentumMarketService

    monkeypatch.setattr(MomentumMarketService, "playbook_live", lambda self, **kwargs: payload)
    result = runner.invoke(app, ["crypto", "momentum-playbook", "--symbol", "BTCUSDT", "--side", "short", "--json"])

    assert result.exit_code == 0
    decoded = json.loads(result.stdout)
    assert decoded["plan"]["side"] == "short"
    assert decoded["plan"]["tradeable"] is True


def test_crypto_scan_entry_reference_is_side_aware() -> None:
    assert _format_entry_reference({"side": "long", "entry_zone": [100.0, 101.0]}) == "101.00"
    assert _format_entry_reference({"side": "short", "entry_zone": [100.0, 101.0]}) == "100.00"


def test_crypto_scan_alias_defaults_to_momentum(monkeypatch) -> None:
    captured: dict[str, int] = {}

    payload = {
        "strategy": "derivatives_momentum_v1",
        "symbols": ["BTCUSDT"],
        "summary": {
            "tradeable_count": 1,
            "cadence_state": "normal",
            "recommended_score_threshold": 90,
            "effective_score_threshold": 90,
            "cadence_policy_applied": False,
        },
        "cadence": {"state": "normal", "recommended_threshold": 90, "effective_threshold": 90, "applied": False},
        "results": {"BTCUSDT": {"plans": [], "tradeable": []}},
    }

    from trading.crypto.momentum.service import MomentumMarketService

    def fake_scan_live(self, **kwargs):
        captured["score_threshold"] = kwargs["score_threshold"]
        return payload

    monkeypatch.setattr(MomentumMarketService, "scan_live", fake_scan_live)
    result = runner.invoke(app, ["crypto", "scan", "--symbols", "BTCUSDT", "--json"])

    assert result.exit_code == 0
    decoded = json.loads(result.stdout)
    assert decoded["strategy"] == "derivatives_momentum_v1"
    assert captured["score_threshold"] == 90


def test_crypto_scan_can_apply_adaptive_threshold(monkeypatch) -> None:
    captured: dict[str, bool] = {}
    payload = {
        "strategy": "derivatives_momentum_v1",
        "symbols": ["BTCUSDT"],
        "summary": {
            "tradeable_count": 2,
            "cadence_state": "expansion",
            "recommended_score_threshold": 88,
            "effective_score_threshold": 88,
            "cadence_policy_applied": True,
        },
        "cadence": {"state": "expansion", "recommended_threshold": 88, "effective_threshold": 88, "applied": True},
        "results": {"BTCUSDT": {"plans": [], "tradeable": []}},
    }

    from trading.crypto.momentum.service import MomentumMarketService

    def fake_scan_live(self, **kwargs):
        captured["apply_cadence_policy"] = kwargs["apply_cadence_policy"]
        return payload

    monkeypatch.setattr(MomentumMarketService, "scan_live", fake_scan_live)
    result = runner.invoke(app, ["crypto", "scan", "--symbols", "BTCUSDT", "--adaptive-threshold", "--json"])

    assert result.exit_code == 0
    decoded = json.loads(result.stdout)
    assert captured["apply_cadence_policy"] is True
    assert decoded["summary"]["cadence_policy_applied"] is True


def test_crypto_position_review_can_disable_adaptive_threshold(monkeypatch) -> None:
    captured: dict[str, bool] = {}

    from trading.crypto.analysis.service import CryptoAnalysisService

    def fake_review(self, coins, **kwargs):
        captured["apply_cadence_policy"] = kwargs["apply_cadence_policy"]
        return {
            "generated_at": "2026-06-16T00:00:00+00:00",
            "summary": {},
            "recommendations": [],
        }

    monkeypatch.setattr(CryptoAnalysisService, "review", fake_review)
    result = runner.invoke(
        app,
        ["crypto", "position-review", "--no-adaptive-threshold", "--json"],
    )

    assert result.exit_code == 0
    assert captured["apply_cadence_policy"] is False


def test_crypto_daily_can_disable_adaptive_threshold(monkeypatch) -> None:
    captured: dict[str, bool] = {}

    import cli.commands.crypto as crypto_commands

    def fake_daily(coins, **kwargs):
        captured["apply_cadence_policy"] = kwargs["apply_cadence_policy"]
        return {
            "generated_at": "2026-06-16T00:00:00+00:00",
            "summary": {},
            "recommendations": [],
        }

    monkeypatch.setattr(crypto_commands, "run_daily_entry_check", fake_daily)
    result = runner.invoke(
        app,
        ["crypto", "daily", "--no-adaptive-threshold", "--json"],
    )

    assert result.exit_code == 0
    assert captured["apply_cadence_policy"] is False


def test_crypto_momentum_scan_telegram_preview(monkeypatch) -> None:
    payload = {
        "strategy": "derivatives_momentum_v1",
        "symbols": ["BTCUSDT"],
        "summary": {
            "tradeable_count": 0,
            "cadence_state": "quiet",
            "recommended_score_threshold": 90,
            "effective_score_threshold": 90,
            "cadence_policy_applied": False,
        },
        "cadence": {
            "state": "quiet",
            "recommended_threshold": 90,
            "effective_threshold": 90,
            "applied": False,
        },
        "results": {
            "BTCUSDT": {
                "plans": [{
                    "side": "long",
                    "tradeable": False,
                    "confidence_score": 84,
                    "setup_status": "pending",
                    "entry_zone": [100.0, 101.0],
                    "invalidation": 98.0,
                    "tp1": 105.0,
                    "tp2": 109.0,
                    "tp3": 111.0,
                    "rr_estimated": 2.2,
                    "expected_move_pct": 0.08,
                    "reasoning": {"blockers": ["falta trigger"]},
                }],
                "tradeable": [],
            }
        },
    }

    from trading.crypto.momentum.service import MomentumMarketService

    monkeypatch.setattr(MomentumMarketService, "scan_live", lambda self, **kwargs: payload)
    result = runner.invoke(app, ["crypto", "momentum-scan", "--symbols", "BTCUSDT", "--telegram-markdown-v2"])

    assert result.exit_code == 0
    assert "NAVE Crypto" in result.stdout
    assert "BTCUSDT" in result.stdout


def test_crypto_playbook_alias_defaults_to_momentum(monkeypatch) -> None:
    payload = {
        "strategy": "derivatives_momentum_v1",
        "symbol": "ETHUSDT",
        "plan": {
            "side": "long",
            "setup_status": "pending",
            "tradeable": False,
            "confidence_score": 76,
            "entry_zone": [10.0, 11.0],
            "invalidation": 9.5,
            "tp1": 12.0,
            "tp2": 13.0,
            "tp3": 14.0,
            "rr_estimated": 1.9,
            "expected_move_pct": 0.08,
        },
    }

    from trading.crypto.momentum.service import MomentumMarketService

    monkeypatch.setattr(MomentumMarketService, "playbook_live", lambda self, **kwargs: payload)
    result = runner.invoke(app, ["crypto", "playbook", "--symbol", "ETHUSDT", "--side", "long", "--json"])

    assert result.exit_code == 0
    decoded = json.loads(result.stdout)
    assert decoded["strategy"] == "derivatives_momentum_v1"
    assert decoded["plan"]["side"] == "long"
