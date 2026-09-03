"""Crypto momentum command group for BTC/ETH derivatives scans."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from cli.professional_typer import ProfessionalTyper
from trading.crypto.momentum import load_momentum_config
from trading.crypto.momentum.formatters import render_universe_momentum_scan
from trading.crypto.momentum.service import MomentumMarketService
from trading.crypto.analysis import CryptoAnalysisService
from trading.crypto.analysis.daily_display import render_daily_entry_check, run_daily_entry_check

crypto_app = ProfessionalTyper(help="Crypto BTC/ETH — use [bold]nave daily[/bold] for entry checks")
DEFAULT_SCORE_THRESHOLD = load_momentum_config().score_tradeable_threshold
DEFAULT_OPERATOR_SCORE_THRESHOLD = 90


def _json_default(value: Any) -> Any:
    """Serialize numpy/pandas scalar values emitted by live market frames."""
    item = getattr(value, "item", None)
    if callable(item):
        try:
            return item()
        except Exception:
            pass
    return str(value)


def _build_scan_payload(
    *,
    symbols: str,
    tf: str,
    account_equity: float,
    risk_pct: float,
    score_threshold: int,
    apply_cadence_policy: bool,
) -> dict:
    service = MomentumMarketService()
    return service.scan_live(
        symbols=service.parse_symbols(symbols),
        timeframes=service.parse_timeframes(tf),
        account_equity=account_equity,
        risk_pct=risk_pct,
        score_threshold=score_threshold,
        apply_cadence_policy=apply_cadence_policy,
    )


def _build_playbook_payload(
    *,
    symbol: str,
    side: str,
    tf: str,
    account_equity: float,
    risk_pct: float,
    score_threshold: int,
) -> dict:
    service = MomentumMarketService()
    return service.playbook_live(
        symbol=service.parse_symbols(symbol)[0],
        side=side.lower(),
        timeframes=service.parse_timeframes(tf),
        account_equity=account_equity,
        risk_pct=risk_pct,
        score_threshold=score_threshold,
    )


def _render_scan(payload: dict) -> None:
    console = Console()
    table = Table(title="Derivatives momentum scan", show_lines=False)
    table.add_column("Symbol")
    table.add_column("Side")
    table.add_column("Status")
    table.add_column("Tradeable")
    table.add_column("Score", justify="right")
    table.add_column("Entry", justify="right")
    table.add_column("Invalidation", justify="right")
    table.add_column("TP2", justify="right")
    table.add_column("RR", justify="right")
    table.add_column("Move %", justify="right")

    for symbol, entry in payload["results"].items():
        for plan in entry["plans"]:
            table.add_row(
                symbol,
                plan["side"],
                plan["setup_status"],
                "yes" if plan["tradeable"] else "no",
                str(plan["confidence_score"]),
                _format_entry_reference(plan),
                f"{plan['invalidation']:.2f}",
                f"{plan['tp2']:.2f}",
                f"{plan['rr_estimated']:.2f}",
                f"{plan['expected_move_pct'] * 100:.1f}",
            )
    console.print(table)


def _render_playbook(payload: dict) -> None:
    plan = payload["plan"]
    console = Console()
    console.print(f"[bold]{payload['symbol']}[/bold] {plan['side']} momentum playbook")
    console.print(f"status: {plan['setup_status']}  tradeable: {plan['tradeable']}  score: {plan['confidence_score']}")
    console.print(f"entry zone: {plan['entry_zone'][0]:.2f} -> {plan['entry_zone'][1]:.2f}")
    console.print(f"invalidation: {plan['invalidation']:.2f}")
    console.print(f"targets: {plan['tp1']:.2f} / {plan['tp2']:.2f} / {plan['tp3']:.2f}")
    console.print(f"expected move: {plan['expected_move_pct'] * 100:.1f}%  RR: {plan['rr_estimated']:.2f}")


def _format_entry_reference(plan: dict[str, Any]) -> str:
    zone = plan.get("entry_zone") or []
    if not zone:
        return "—"
    try:
        if str(plan.get("side") or "").lower() == "short":
            return f"{float(zone[0]):.2f}"
        return f"{float(zone[-1]):.2f}"
    except (TypeError, ValueError):
        return "—"


def _emit_scan_payload(payload: dict, *, json_out: bool, telegram_markdown_v2: bool) -> None:
    if json_out:
        typer.echo(json.dumps(payload, indent=2, default=_json_default))
        return
    if telegram_markdown_v2:
        from trading.crypto.momentum.formatters import render_momentum_scan_markdown_v2

        messages = render_momentum_scan_markdown_v2(payload)
        for idx, message in enumerate(messages, start=1):
            if idx > 1:
                typer.echo("\n---\n")
            typer.echo(message)
        return
    _render_scan(payload)


def _run_scan_command(
    *,
    symbols: str,
    tf: str,
    account_equity: float,
    risk_pct: float,
    score_threshold: int,
    adaptive_threshold: bool,
    telegram_markdown_v2: bool,
    json_out: bool,
) -> None:
    payload = _build_scan_payload(
        symbols=symbols,
        tf=tf,
        account_equity=account_equity,
        risk_pct=risk_pct,
        score_threshold=score_threshold,
        apply_cadence_policy=adaptive_threshold,
    )
    _emit_scan_payload(
        payload,
        json_out=json_out,
        telegram_markdown_v2=telegram_markdown_v2,
    )


def _run_playbook_command(
    *,
    symbol: str,
    side: str,
    tf: str,
    account_equity: float,
    risk_pct: float,
    score_threshold: int,
    json_out: bool,
) -> None:
    payload = _build_playbook_payload(
        symbol=symbol,
        side=side,
        tf=tf,
        account_equity=account_equity,
        risk_pct=risk_pct,
        score_threshold=score_threshold,
    )
    if json_out:
        typer.echo(json.dumps(payload, indent=2, default=_json_default))
        return
    _render_playbook(payload)


@crypto_app.command("momentum-scan")
def momentum_scan(
    symbols: str = typer.Option("BTCUSDT,ETHUSDT", "--symbols", help="Comma-separated perp symbols."),
    tf: str = typer.Option("4h,1h", "--tf", help="Setup and trigger timeframe, e.g. 4h,1h or 4h,15m."),
    account_equity: float = typer.Option(10000.0, "--account-equity", help="Account equity used for sizing context."),
    risk_pct: float = typer.Option(0.005, "--risk-pct", help="Risk per trade as decimal, e.g. 0.005 = 0.5%."),
    score_threshold: int = typer.Option(
        DEFAULT_SCORE_THRESHOLD,
        "--score-threshold",
        help="Minimum score to flag a setup as tradeable.",
    ),
    adaptive_threshold: bool = typer.Option(
        False,
        "--adaptive-threshold/--no-adaptive-threshold",
        help="Apply the cadence-recommended threshold instead of only reporting it.",
    ),
    telegram_markdown_v2: bool = typer.Option(
        False,
        "--telegram-markdown-v2",
        help="Render Telegram-friendly MarkdownV2 digest (chunked).",
    ),
    json_out: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Scan BTC/ETH derivatives for fresh momentum setups."""
    _run_scan_command(
        symbols=symbols,
        tf=tf,
        account_equity=account_equity,
        risk_pct=risk_pct,
        score_threshold=score_threshold,
        adaptive_threshold=adaptive_threshold,
        telegram_markdown_v2=telegram_markdown_v2,
        json_out=json_out,
    )


@crypto_app.command("scan")
def scan(
    symbols: str = typer.Option("BTCUSDT,ETHUSDT", "--symbols", help="Comma-separated perp symbols."),
    tf: str = typer.Option("4h,1h", "--tf", help="Setup and trigger timeframe, e.g. 4h,1h or 4h,15m."),
    account_equity: float = typer.Option(10000.0, "--account-equity", help="Account equity used for sizing context."),
    risk_pct: float = typer.Option(0.005, "--risk-pct", help="Risk per trade as decimal, e.g. 0.005 = 0.5%."),
    score_threshold: int = typer.Option(
        DEFAULT_OPERATOR_SCORE_THRESHOLD,
        "--score-threshold",
        help=(
            "Minimum score to flag a setup as tradeable. "
            "Default market scan uses a stricter operator threshold; "
            "momentum-scan uses the configured engine threshold."
        ),
    ),
    adaptive_threshold: bool = typer.Option(
        False,
        "--adaptive-threshold/--no-adaptive-threshold",
        help="Apply the cadence-recommended threshold instead of only reporting it.",
    ),
    telegram_markdown_v2: bool = typer.Option(
        False,
        "--telegram-markdown-v2",
        help="Render Telegram-friendly MarkdownV2 digest (chunked).",
    ),
    json_out: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Default market scan: routes to the momentum engine."""
    _run_scan_command(
        symbols=symbols,
        tf=tf,
        account_equity=account_equity,
        risk_pct=risk_pct,
        score_threshold=score_threshold,
        adaptive_threshold=adaptive_threshold,
        telegram_markdown_v2=telegram_markdown_v2,
        json_out=json_out,
    )


@crypto_app.command("momentum-playbook")
def momentum_playbook(
    symbol: str = typer.Option(..., "--symbol", help="Perp symbol, e.g. BTCUSDT."),
    side: str = typer.Option(..., "--side", help="Trade direction: long or short."),
    tf: str = typer.Option("4h,1h", "--tf", help="Setup and trigger timeframe, e.g. 4h,1h or 4h,15m."),
    account_equity: float = typer.Option(10000.0, "--account-equity", help="Account equity used for sizing context."),
    risk_pct: float = typer.Option(0.005, "--risk-pct", help="Risk per trade as decimal, e.g. 0.005 = 0.5%."),
    score_threshold: int = typer.Option(
        DEFAULT_SCORE_THRESHOLD,
        "--score-threshold",
        help="Minimum score to flag a setup as tradeable.",
    ),
    json_out: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Build a concrete derivatives momentum playbook for one symbol and side."""
    _run_playbook_command(
        symbol=symbol,
        side=side,
        tf=tf,
        account_equity=account_equity,
        risk_pct=risk_pct,
        score_threshold=score_threshold,
        json_out=json_out,
    )


@crypto_app.command("playbook")
def playbook(
    symbol: str = typer.Option(..., "--symbol", help="Perp symbol, e.g. BTCUSDT."),
    side: str = typer.Option(..., "--side", help="Trade direction: long or short."),
    tf: str = typer.Option("4h,1h", "--tf", help="Setup and trigger timeframe, e.g. 4h,1h or 4h,15m."),
    account_equity: float = typer.Option(10000.0, "--account-equity", help="Account equity used for sizing context."),
    risk_pct: float = typer.Option(0.005, "--risk-pct", help="Risk per trade as decimal, e.g. 0.005 = 0.5%."),
    score_threshold: int = typer.Option(
        DEFAULT_SCORE_THRESHOLD,
        "--score-threshold",
        help="Minimum score to flag a setup as tradeable.",
    ),
    json_out: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Default trade-plan builder: routes to the momentum engine."""
    _run_playbook_command(
        symbol=symbol,
        side=side,
        tf=tf,
        account_equity=account_equity,
        risk_pct=risk_pct,
        score_threshold=score_threshold,
        json_out=json_out,
    )


@crypto_app.command("daily")
def crypto_daily(
    coins: str = typer.Option("BTC,ETH", "--coins", "-c"),
    account_equity: float = typer.Option(10000.0, "--account-equity"),
    risk_pct: float = typer.Option(0.005, "--risk-pct"),
    include_options: bool = typer.Option(True, "--options/--no-options"),
    options_source: str = typer.Option("deribit", "--options-source"),
    adaptive_threshold: bool = typer.Option(
        True,
        "--adaptive-threshold/--no-adaptive-threshold",
        help="Apply the cadence-recommended threshold in the daily review.",
    ),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Same as [bold]nave daily[/bold] — when to enter BTC/ETH today."""
    coin_list = [part.strip().upper() for part in coins.replace(",", " ").split() if part.strip()]
    payload = run_daily_entry_check(
        coin_list,
        account_equity=account_equity,
        risk_pct=risk_pct,
        include_options=include_options,
        options_source=options_source,
        apply_cadence_policy=adaptive_threshold,
    )
    if json_out:
        typer.echo(json.dumps(payload, indent=2, default=_json_default))
        return
    render_daily_entry_check(payload, console=Console())


@crypto_app.command("position-review")
def position_review(
    coins: str = typer.Option("BTC,ETH", "--coins", help="Comma or space separated coins."),
    account_equity: float = typer.Option(10000.0, "--account-equity"),
    risk_pct: float = typer.Option(0.005, "--risk-pct"),
    include_options: bool = typer.Option(True, "--options/--no-options"),
    options_source: str = typer.Option("deribit", "--options-source"),
    adaptive_threshold: bool = typer.Option(
        True,
        "--adaptive-threshold/--no-adaptive-threshold",
        help="Apply the cadence-recommended threshold in the position review.",
    ),
    json_out: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Unified BTC/ETH: COT + momentum + regime + options."""
    coin_list = [part.strip().upper() for part in coins.replace(",", " ").split() if part.strip()]
    payload = CryptoAnalysisService().review(
        coin_list,
        account_equity=account_equity,
        risk_pct=risk_pct,
        include_options=include_options,
        options_source=options_source,
        apply_cadence_policy=adaptive_threshold,
    )
    if json_out:
        typer.echo(json.dumps(payload, indent=2, default=_json_default))
        return
    render_daily_entry_check(payload, console=Console())


@crypto_app.command("momentum-backtest")
def momentum_backtest(
    symbols: str = typer.Option("BTCUSDT,ETHUSDT", "--symbols", help="Comma-separated perp symbols."),
    tf: str = typer.Option("4h,1h", "--tf", help="Setup and trigger timeframe, e.g. 4h,1h or 4h,15m."),
    lookback_days: int = typer.Option(180, "--lookback-days", help="Historical days to evaluate."),
    json_out: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Run the simple historical momentum evaluator versus the baseline breakout."""
    service = MomentumMarketService()
    payload = service.backtest_live(
        symbols=service.parse_symbols(symbols),
        timeframes=service.parse_timeframes(tf),
        lookback_days=lookback_days,
    )
    if json_out:
        typer.echo(json.dumps(payload, indent=2, default=_json_default))
        return

    console = Console()
    table = Table(title="Derivatives momentum backtest", show_lines=False)
    table.add_column("Symbol")
    table.add_column("Trades", justify="right")
    table.add_column("Win %", justify="right")
    table.add_column("Expectancy", justify="right")
    table.add_column("Max DD", justify="right")
    table.add_column(">=8%", justify="right")
    table.add_column("Delta Exp", justify="right")
    for symbol, result in payload["results"].items():
        metrics = result["metrics"]
        delta = result.get("baseline", {}).get("delta", {})
        table.add_row(
            symbol,
            str(result["trade_count"]),
            f"{metrics['win_rate'] * 100:.1f}",
            f"{metrics['expectancy']:.2f}",
            f"{metrics['max_drawdown']:.2f}",
            f"{metrics['pct_reaching_8'] * 100:.1f}",
            f"{delta.get('expectancy', 0.0):+.2f}",
        )
    console.print(table)


@crypto_app.command("universe-momentum-scan")
def universe_momentum_scan(
    fixture: Path = typer.Option(
        ...,
        "--fixture",
        exists=True,
        dir_okay=False,
        readable=True,
        help="Explicit offline point-in-time fixture; no current-universe fallback exists.",
    ),
    start: str = typer.Option(..., "--start", help="UTC observation-window start."),
    end: str = typer.Option(..., "--end", help="UTC observation-window end."),
    symbols: str = typer.Option(
        "ARB,CAKE,CRV,TWT,EDGE,PONS",
        "--symbols",
        help="Target tickers or canonical asset IDs for the audit.",
    ),
    cadence: str = typer.Option("6h", "--cadence", help="Observation cadence, e.g. 3h or 6h."),
    universe_size: int = typer.Option(100, "--universe-size", min=1),
    config: Path | None = typer.Option(None, "--config", exists=True, dir_okay=False),
    validate_setups: bool = typer.Option(True, "--validate-setups/--no-validate-setups"),
    sensitivity: bool = typer.Option(True, "--sensitivity/--no-sensitivity"),
    max_candidates: int = typer.Option(25, "--max-candidates", min=1),
    json_out: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Replay point-in-time top-100 plus liquid-perpetual discovery offline."""
    target_symbols = [part.strip().upper() for part in symbols.replace(" ", ",").split(",") if part.strip()]
    payload = MomentumMarketService().research_universe_momentum_scan(
        fixture_path=str(fixture),
        start=start,
        end=end,
        cadence=cadence,
        universe_size=universe_size,
        symbols=target_symbols or None,
        config_path=str(config) if config is not None else None,
        validate_setups=validate_setups,
        include_sensitivity=sensitivity,
        max_candidates=max_candidates,
    )
    if json_out:
        typer.echo(json.dumps(payload, indent=2, default=_json_default))
        return
    typer.echo(render_universe_momentum_scan(payload, max_rows=max_candidates))
