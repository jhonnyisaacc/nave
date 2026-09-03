from trading.crypto.momentum.backtest import MomentumBacktester
from trading.crypto.momentum.config import MomentumConfig, load_momentum_config
from trading.crypto.momentum.engine import MomentumSetupEngine
from trading.crypto.momentum.execution_plan import TradePlan, recommend_position_sizing
from trading.crypto.momentum.formatters import (
    render_entry_zone_alert_markdown_v2,
    render_momentum_scan_markdown_v2,
)
from trading.crypto.momentum.review import build_review_summary
from trading.crypto.momentum.discovery import (
    DISCOVERY_HYPOTHESIS,
    AssetMarketData,
    DiscoveryCandidate,
    DiscoveryConfig,
    LiquidityAssessment,
    load_discovery_config,
    rank_universe,
)
from trading.crypto.momentum.replay import (
    ExistingMomentumSetupValidator,
    FixtureMarketDataProvider,
    PaperSetup,
    UniverseMomentumReplay,
    load_replay_fixture,
    no_chase_allowed,
    parse_cadence,
    simulate_paper_setup,
)
from trading.crypto.momentum.universe import (
    CurrentUniverseProvider,
    FixtureUniverseProvider,
    UniverseMember,
    UniverseSnapshot,
    UniverseProviderUnavailable,
    deduplicate_members,
    identity_key_for,
)


def run_period_backtest(*args, **kwargs):
    """Load the legacy file-backed workflow only when it is explicitly used."""
    from trading.crypto.momentum.workflow import run_period_backtest as _run_period_backtest

    return _run_period_backtest(*args, **kwargs)

__all__ = [
    "MomentumBacktester",
    "MomentumConfig",
    "MomentumSetupEngine",
    "TradePlan",
    "build_review_summary",
    "load_momentum_config",
    "render_entry_zone_alert_markdown_v2",
    "render_momentum_scan_markdown_v2",
    "recommend_position_sizing",
    "run_period_backtest",
    "AssetMarketData",
    "DISCOVERY_HYPOTHESIS",
    "DiscoveryCandidate",
    "DiscoveryConfig",
    "CurrentUniverseProvider",
    "ExistingMomentumSetupValidator",
    "FixtureMarketDataProvider",
    "FixtureUniverseProvider",
    "LiquidityAssessment",
    "PaperSetup",
    "UniverseMember",
    "UniverseMomentumReplay",
    "UniverseProviderUnavailable",
    "UniverseSnapshot",
    "deduplicate_members",
    "identity_key_for",
    "load_discovery_config",
    "load_replay_fixture",
    "no_chase_allowed",
    "parse_cadence",
    "rank_universe",
    "simulate_paper_setup",
]
