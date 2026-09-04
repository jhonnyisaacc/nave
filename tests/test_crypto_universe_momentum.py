from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace

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
    _candidate_matches,
    _first_eligible_records,
    load_replay_fixture,
    no_chase_allowed,
    simulate_paper_setup,
)
from trading.crypto.momentum.universe import (
    CurrentUniverseProvider,
    FixtureUniverseProvider,
    UniverseMember,
    UniverseSnapshot,
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


def test_contract_address_is_a_target_identity_and_outcome_key() -> None:
    member = replace(_member(symbol="X", canonical_asset_id=None), contract_address="0xABC")
    candidate = SimpleNamespace(member=member)
    assert _candidate_matches(candidate, {"0XABC"}) is True

    replay = UniverseMomentumReplay(
        FixtureUniverseProvider([UniverseSnapshot(
            observation_timestamp=member.observation_timestamp,
            source_timestamp=member.source_timestamp,
            available_at=member.available_at,
            source="test",
            members=(member,),
        )]),
        FixtureMarketDataProvider({}),
    )
    serialized = {
        **member.to_dict(),
        "universe_rank": member.rank,
        "rank_score": 75.0,
        "ranking_state": "ELIGIBLE",
        "features": {"return_7d": 0.2},
        "liquidity": {"state": "PASS"},
        "missingness": [],
        "first_eligible_at": member.observation_timestamp.isoformat(),
    }
    report = replay._target_report(
        ["0xABC"],
        [{"universe_members": [member.to_dict()], "candidates": [serialized]}],
        {"contract:0xabc": serialized},
        [{"canonical_asset_id": None, "contract_address": "0xABC", "status": "COMPLETED"}],
    )
    assert report[0]["status"] == "DETECTED_FIRST_ELIGIBLE"
    assert report[0]["outcome"]["status"] == "COMPLETED"


def test_current_provider_keeps_current_top_market_cap_separate_from_history() -> None:
    provider = CurrentUniverseProvider.from_market_cap_rows(
        [{"id": "arbitrum", "symbol": "arb", "market_cap_rank": 1}],
        [{"name": "ARB"}, {"name": "OUTSIDE", "canonical_asset_id": "outside"}],
        observation_timestamp="2026-09-03T00:00:00Z",
    )
    snapshot = provider.snapshot_at("2026-09-03T00:00:00Z", universe_size=100)
    assert snapshot.members[0].universe_source == "current_top_market_cap"
    assert snapshot.members[0].contract_symbol == "ARB"
    assert any(member.symbol == "OUTSIDE" and not member.is_top_ranked for member in snapshot.members)


def test_current_provider_does_not_join_an_ambiguous_ticker_by_symbol() -> None:
    provider = CurrentUniverseProvider.from_market_cap_rows(
        [
            {"id": "edge-protocol", "symbol": "edge", "market_cap_rank": 1},
            {"id": "edge-network", "symbol": "edge", "market_cap_rank": 2},
        ],
        [{"name": "EDGE"}],
        observation_timestamp="2026-09-03T00:00:00Z",
    )

    top_members = tuple(
        member
        for member in provider.snapshot_at("2026-09-03T00:00:00Z", universe_size=100).members
        if member.is_top_ranked
    )
    assert [member.contract_symbol for member in top_members] == [None, None]
    assert all(member.venue is None for member in top_members)


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


def test_paper_simulation_does_not_fill_when_price_gaps_past_entry_zone() -> None:
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
    future = _ohlcv([200.0, 204.0], start="2026-08-25T01:00:00Z")
    result = simulate_paper_setup(
        setup,
        future,
        config=DiscoveryConfig(),
        funding_rate=0.0,
    )

    assert result == {
        "status": "NO_FILL",
        "r_multiple": None,
        "reason": "entry_zone_not_reached",
    }


def test_paper_simulation_uses_observed_liquidity_costs() -> None:
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
        slippage_bps=40.0,
        spread_bps=20.0,
    )
    future = _ohlcv([100.0, 111.0], start="2026-08-25T01:00:00Z")
    result = simulate_paper_setup(
        setup,
        future,
        config=DiscoveryConfig(),
        funding_rate=0.0,
    )

    assert result["status"] == "COMPLETED"
    assert result["slippage_bps"] == 40.0
    assert result["spread_bps"] == 20.0
    assert result["spread_impact"] == pytest.approx(0.002)
    assert result["slippage_impact"] == pytest.approx(0.008)


def test_meaningful_move_requires_the_detected_direction() -> None:
    values = [100.0] * 168 + [80.0]
    market = AssetMarketData(frames={"1h": _ohlcv(values, start="2026-01-01T00:00:00Z")})
    replay = UniverseMomentumReplay(
        FixtureUniverseProvider([]),
        FixtureMarketDataProvider({"asset:test-asset": market}),
    )
    outcome = replay._resolve_outcome(
        {
            "symbol": "TEST",
            "canonical_asset_id": "test-asset",
            "contract_address": None,
            "first_eligible_at": "2026-01-01T00:00:00Z",
            "features": {"price": 100.0},
        },
        validate_setups=False,
    )

    assert outcome["status"] == "COMPLETED"
    assert outcome["forward_return"] < 0
    assert outcome["meaningful_move"] is False


def test_no_chase_rejects_price_beyond_valid_entry_zone() -> None:
    assert no_chase_allowed(101.0, 100.0, "long", 0.02) is True
    assert no_chase_allowed(103.0, 100.0, "long", 0.02) is False
    assert no_chase_allowed(98.0, 100.0, "short", 0.02) is True
    assert no_chase_allowed(96.0, 100.0, "short", 0.02) is False


def test_threshold_sensitivity_uses_the_first_detection_at_that_threshold() -> None:
    def candidate(score: float, timestamp: str) -> dict:
        return {
            "canonical_asset_id": "asset-a",
            "contract_address": None,
            "rank_score": score,
            "features": {"return_7d": 0.2},
            "liquidity": {"state": "PASS"},
            "missingness": [],
            "ranking_state": "ELIGIBLE",
            "observation_timestamp": timestamp,
            "first_eligible_at": timestamp,
        }

    observations = [
        {"observation_timestamp": "2026-08-25T00:00:00+00:00", "candidates": [candidate(55, "2026-08-25T00:00:00+00:00")]},
        {"observation_timestamp": "2026-08-25T06:00:00+00:00", "candidates": [candidate(75, "2026-08-25T06:00:00+00:00")]},
    ]

    first = _first_eligible_records(observations, min_rank_score=70)
    assert first["asset:asset-a"]["first_eligible_at"] == "2026-08-25T06:00:00+00:00"


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
    arb_report = next(item for item in first["target_report"] if item["target"] == "ARB")
    assert arb_report["liquid_perpetual_observed"] is True
    assert set(arb_report["venues"]) == {"binance", "hyperliquid"}


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


def test_existing_scan_can_append_current_universe_research_without_execution(tmp_path, monkeypatch) -> None:
    from trading.crypto.momentum.service import MomentumMarketService, MomentumTimeframes
    from trading.crypto.momentum.thesis import MomentumThesisStore

    service = MomentumMarketService(
        market_client=SimpleNamespace(),
        thesis_store=MomentumThesisStore(path=tmp_path / "theses.json"),
    )
    frame = pd.DataFrame(
        {
            "timestamp": ["2026-08-25T00:00:00Z"],
            "close": [100.0],
        }
    )
    monkeypatch.setattr(
        service,
        "load_live_frames",
        lambda symbol, timeframes: {"daily": frame, "setup": frame, "trigger": frame},
    )
    monkeypatch.setattr(service.engine, "evaluate_symbol", lambda **kwargs: [])
    monkeypatch.setattr(
        service,
        "scan_current_universe_discovery",
        lambda **kwargs: {"mode": "current_research_only", "status": "OK"},
    )
    payload = service.scan_live(
        symbols=["BTCUSDT", "ETHUSDT"],
        timeframes=MomentumTimeframes(bias="1d", setup="4h", trigger="1h"),
        include_universe_discovery=True,
    )

    assert payload["universe_discovery"]["mode"] == "current_research_only"


def test_current_universe_discovery_uses_read_only_providers(tmp_path, monkeypatch) -> None:
    from trading.crypto.momentum.service import MomentumMarketService
    from trading.crypto.momentum.thesis import MomentumThesisStore

    fixture = load_replay_fixture(FIXTURE)
    market = fixture.market_data["asset:arbitrum"]

    class _ReadOnlyClient:
        def get_meta(self):
            return {"universe": [{"name": "ARB"}]}

        def get_l2_book(self, coin):
            return {
                "levels": [
                    [{"px": "99.99", "sz": "1000"}],
                    [{"px": "100.01", "sz": "1000"}],
                ]
            }

    service = MomentumMarketService(
        market_client=_ReadOnlyClient(),
        thesis_store=MomentumThesisStore(path=tmp_path / "theses.json"),
    )
    monkeypatch.setattr(
        service,
        "fetch_current_market_cap_rows",
        lambda universe_size: [{"id": "arbitrum", "symbol": "arb", "market_cap_rank": 1}],
    )
    monkeypatch.setattr(
        service,
        "load_historical_frames",
        lambda symbol, timeframes, lookback_days: {
            "daily": market.frames["1d"],
            "setup": market.frames["4h"],
            "trigger": market.frames["1h"],
            "open_interest": market.derivatives,
            "funding_rate": 0.00001,
        },
    )

    payload = service.scan_current_universe_discovery(universe_size=100, max_candidates=5)

    assert payload["status"] == "OK"
    assert payload["universe"]["source"] == "current_market_cap_and_exchange_metadata"
    assert payload["universe"]["members"][0]["universe_source"] == "current_top_market_cap"


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
