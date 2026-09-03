from __future__ import annotations

import json
from dataclasses import replace

import pandas as pd
import pytest
from typer.testing import CliRunner

from trading.crypto.momentum.discovery import (
    AssetMarketData,
    DiscoveryConfig,
    assess_liquidity,
    price_return,
    rank_universe,
)
from trading.crypto.momentum.formatters import render_universe_momentum_scan
from trading.crypto.momentum.replay import (
    FixtureMarketDataProvider,
    PaperSetup,
    UniverseMomentumReplay,
    load_replay_fixture,
    no_chase_allowed,
    simulate_paper_setup,
)
from trading.crypto.momentum.universe import (
    FixtureUniverseProvider,
    UniverseMember,
    UniverseProviderUnavailable,
    deduplicate_members,
    members_for_target,
)


FIXTURE = "tests/fixtures/crypto_momentum_replay.json"


def _member(
    symbol: str = "TEST",
    *,
    canonical_asset_id: str | None = "test-asset",
    source: str = "historical_top_market_cap",
    rank: int | None = 10,
    venue: str = "fixture",
) -> UniverseMember:
    return UniverseMember(
        symbol=symbol,
        canonical_asset_id=canonical_asset_id,
        contract_address=None,
        venue=venue,
        contract_symbol=symbol,
        quote_currency="USDT",
        observation_timestamp=pd.Timestamp("2026-08-25T00:00:00Z").to_pydatetime(),
        source_timestamp=pd.Timestamp("2026-08-25T00:00:00Z").to_pydatetime(),
        available_at=pd.Timestamp("2026-08-25T00:00:00Z").to_pydatetime(),
        universe_source=source,
        data_completeness="complete",
        rank=rank,
    )


def _ohlcv(values: list[float], *, start: str = "2026-08-20T00:00:00Z", freq: str = "1h") -> pd.DataFrame:
    index = pd.date_range(start, periods=len(values), freq=freq, tz="UTC")
    return pd.DataFrame(
        {
            "timestamp": index,
            "open": values,
            "high": [value * 1.01 for value in values],
            "low": [value * 0.99 for value in values],
            "close": values,
            "volume": [1_000_000.0] * len(values),
        }
    )


def test_point_in_time_provider_rejects_unavailable_and_excludes_future_snapshot() -> None:
    provider = FixtureUniverseProvider.from_path(FIXTURE)

    with __import__("pytest").raises(UniverseProviderUnavailable):
        provider.snapshot_at("2026-08-24T00:04:00Z", universe_size=100)

    historical = provider.snapshot_at("2026-09-01T00:00:00Z", universe_size=100)
    symbols = {member.symbol for member in historical.members}
    assert "FUTURE" not in symbols
    assert historical.observation_timestamp.isoformat() == "2026-08-29T00:00:00+00:00"

    current = provider.snapshot_at("2026-09-02T00:05:00Z", universe_size=100)
    assert {member.symbol for member in current.members} == {"FUTURE"}


def test_historical_universe_is_not_current_universe() -> None:
    provider = FixtureUniverseProvider.from_path(FIXTURE)

    historical = provider.snapshot_at("2026-08-28T23:59:00Z", universe_size=100)
    after_publication = provider.snapshot_at("2026-08-29T00:05:00Z", universe_size=100)

    historical_ranks = {
        member.symbol: member.rank
        for member in deduplicate_members(historical.members)
        if member.rank is not None
    }
    after_ranks = {
        member.symbol: member.rank
        for member in deduplicate_members(after_publication.members)
        if member.rank is not None
    }
    assert historical_ranks["ARB"] == 40
    assert after_ranks["ARB"] == 38


def test_canonical_identity_deduplicates_venues_but_keeps_ambiguous_tickers() -> None:
    provider = FixtureUniverseProvider.from_path(FIXTURE)
    snapshot = provider.snapshot_at("2026-08-25T00:00:00Z", universe_size=100)
    members = deduplicate_members(snapshot.members)

    assert sum(member.canonical_asset_id == "arbitrum" for member in members) == 1
    assert len(members_for_target(members, "EDGE")) == 2
    assert len(members_for_target(members, "arbitrum")) == 1


def test_missing_market_data_is_unknown_not_a_negative_filter() -> None:
    member = _member()
    candidate = rank_universe(
        (member,),
        {member.identity_key: AssetMarketData(frames={})},
        "2026-08-25T00:00:00Z",
    )[0]

    assert candidate.ranking_state == "UNKNOWN"
    assert candidate.eligible is False
    assert "return_7d" in candidate.missingness
    assert candidate.liquidity.state == "UNKNOWN"


def test_liquidity_filter_pass_rejects_and_unknown_states() -> None:
    fixture = load_replay_fixture(FIXTURE)
    member = fixture.universe_provider.snapshot_at("2026-08-25T00:00:00Z", universe_size=100).members[2]
    passing = assess_liquidity(member, fixture.market_data[member.identity_key], "2026-08-25T00:00:00Z", DiscoveryConfig())
    rejecting_member = replace(member, canonical_asset_id="edge-network")
    rejected = assess_liquidity(
        rejecting_member,
        fixture.market_data["asset:edge-network"],
        "2026-08-25T00:00:00Z",
        DiscoveryConfig(),
    )
    unknown = assess_liquidity(member, AssetMarketData(frames={}), "2026-08-25T00:00:00Z", DiscoveryConfig())

    assert passing.state == "PASS"
    assert rejected.state == "REJECT"
    assert "quote_volume_below_minimum" in rejected.reasons
    assert unknown.state == "UNKNOWN"
    assert "derivatives_data_unavailable_or_stale" in unknown.reasons


def test_returns_relative_strength_and_features_use_only_data_at_observation() -> None:
    frame = _ohlcv([100.0, 110.0, 1_000.0], start="2026-08-25T00:00:00Z")
    assert price_return(frame, "2026-08-25T01:00:00Z", 1) == pytest.approx(0.1)

    fixture = load_replay_fixture(FIXTURE)
    snapshot = fixture.universe_provider.snapshot_at("2026-08-25T00:00:00Z", universe_size=100)
    candidates = rank_universe(
        snapshot.members,
        fixture.market_data,
        "2026-08-25T00:00:00Z",
    )
    arb = next(candidate for candidate in candidates if candidate.member.symbol == "ARB")

    assert arb.features["return_7d"] is not None
    assert arb.features["relative_strength"]["BTC"] is not None
    assert arb.features["data_timestamp"] <= "2026-08-25T00:00:00+00:00"


class _FakeSetupValidator:
    def __init__(self) -> None:
        self.timeframes: list[set[str]] = []

    def validate(self, candidate, market, observation_timestamp, config):
        self.timeframes.append(set(market.frames))
        return PaperSetup(
            classification="PROMISING EXPLORATORY SIGNAL",
            valid=True,
            direction="long",
            entry_zone=[100.0, 101.0],
            invalidation=95.0,
            targets=[103.0, 106.0, 109.0],
            expected_move_pct=0.08,
            net_rr=1.8,
            estimated_cost_pct=0.003,
            no_chase=True,
        )


def test_setup_validation_is_separate_and_receives_1h_4h_frames() -> None:
    fixture = load_replay_fixture(FIXTURE)
    validator = _FakeSetupValidator()
    replay = UniverseMomentumReplay(
        fixture.universe_provider,
        FixtureMarketDataProvider(fixture.market_data),
        setup_validator=validator,
    )
    payload = replay.run(
        start="2026-08-25T00:00:00Z",
        end="2026-08-25T00:00:00Z",
        cadence="6h",
        symbols=["ARB"],
        validate_setups=True,
        include_sensitivity=False,
    )

    setup = payload["first_eligible"][0]["setup_validation"]
    assert {"1h", "4h", "1d"}.issubset(validator.timeframes[0])
    assert setup["classification"] == "PROMISING EXPLORATORY SIGNAL"
    assert setup["valid"] is True
    assert all(key not in setup for key in ("action", "signal", "order"))


def test_fees_funding_and_slippage_reduce_paper_result() -> None:
    setup = PaperSetup(
        "PROMISING EXPLORATORY SIGNAL",
        True,
        "long",
        [100.0, 100.0],
        95.0,
        [105.0, 110.0, 115.0],
        0.10,
        2.0,
        0.0,
        True,
    )
    future = _ohlcv([100.0, 103.0, 111.0], start="2026-08-25T01:00:00Z")
    result = simulate_paper_setup(
        setup,
        future,
        config=DiscoveryConfig(fee_bps_per_side=5.0, default_slippage_bps=10.0),
        funding_rate=0.0001,
    )

    assert result["status"] == "COMPLETED"
    assert result["exit_reason"] == "tp2"
    assert result["fee_impact"] > 0
    assert result["slippage_impact"] > 0
    assert result["funding_impact"] > 0
    assert result["net_return"] < result["gross_return"]


def test_no_chase_rejects_price_beyond_valid_entry_zone() -> None:
    assert no_chase_allowed(101.0, 100.0, "long", 0.02) is True
    assert no_chase_allowed(103.0, 100.0, "long", 0.02) is False
    assert no_chase_allowed(98.0, 100.0, "short", 0.02) is True
    assert no_chase_allowed(96.0, 100.0, "short", 0.02) is False


def test_replay_is_deterministic_and_reports_target_states() -> None:
    fixture = load_replay_fixture(FIXTURE)
    replay = UniverseMomentumReplay(
        fixture.universe_provider,
        FixtureMarketDataProvider(fixture.market_data),
    )
    kwargs = {
        "start": "2026-08-25T00:00:00Z",
        "end": "2026-08-25T00:00:00Z",
        "cadence": "6h",
        "symbols": ["ARB", "CAKE", "CRV", "TWT", "EDGE", "PONS"],
        "validate_setups": False,
        "include_sensitivity": False,
    }
    first = replay.run(**kwargs)
    second = replay.run(**kwargs)
    first.pop("generated_at")
    second.pop("generated_at")

    assert first == second
    assert {item["target"]: item["status"] for item in first["target_report"]} == {
        "ARB": "DETECTED_FIRST_ELIGIBLE",
        "CAKE": "DETECTED_FIRST_ELIGIBLE",
        "CRV": "DETECTED_FIRST_ELIGIBLE",
        "TWT": "DETECTED_FIRST_ELIGIBLE",
        "EDGE": "UNKNOWN_ASSET_IDENTITY",
        "PONS": "OUTSIDE_HISTORICAL_TOP_100",
    }


def test_json_schema_and_human_formatter_are_research_only() -> None:
    fixture = load_replay_fixture(FIXTURE)
    payload = UniverseMomentumReplay(
        fixture.universe_provider,
        FixtureMarketDataProvider(fixture.market_data),
    ).run(
        start="2026-08-25T00:00:00Z",
        end="2026-08-25T00:00:00Z",
        symbols=["ARB"],
        validate_setups=False,
        include_sensitivity=False,
    )
    encoded = json.dumps(payload, sort_keys=True)
    rendered = render_universe_momentum_scan(payload)

    assert '"schema_version": "crypto-momentum-discovery-replay.v1"' in encoded
    assert "research only" in rendered
    assert "ARB" in rendered
    assert all(term not in encoded.upper() for term in ("BUY", "SELL", "ENTER"))


def test_fixture_research_path_does_not_touch_market_client() -> None:
    class _GuardClient:
        def __getattr__(self, name):
            raise AssertionError(f"live client touched: {name}")

    from trading.crypto.momentum.service import MomentumMarketService

    service = MomentumMarketService(market_client=_GuardClient())
    payload = service.research_universe_momentum_scan(
        fixture_path=FIXTURE,
        start="2026-08-25T00:00:00Z",
        end="2026-08-25T00:00:00Z",
        symbols=["ARB"],
        validate_setups=False,
        include_sensitivity=False,
    )

    assert payload["mode"] == "historical_research_only"
    assert payload["provider"]["kind"] == "offline_fixture"


def test_cli_research_scan_json_smoke() -> None:
    from cli.main import app

    result = CliRunner().invoke(
        app,
        [
            "crypto",
            "universe-momentum-scan",
            "--fixture",
            FIXTURE,
            "--symbols",
            "ARB",
            "--start",
            "2026-08-25T00:00:00Z",
            "--end",
            "2026-08-25T00:00:00Z",
            "--no-sensitivity",
            "--no-validate-setups",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.stdout
    decoded = json.loads(result.stdout)
    assert decoded["schema_version"] == "crypto-momentum-discovery-replay.v1"
    assert decoded["provider"]["kind"] == "offline_fixture"
