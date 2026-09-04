"""Options analysis command group for Nave CLI."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import typer
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from rich.table import Table
from rich.text import Text

from cli.professional_typer import ProfessionalTyper
from options.analyzer import OptionsAnalyzer
from options.eth_weekly import EthWeeklyOptionsProfile, build_eth_weekly_decision
from options.exceptions import OptionsError
from options.prompt_builder import build_llm_paths, build_llm_prompt
from options.gems_pipeline import format_gem_digest, run_hidden_gems_scan
from options.ticker_registry import (
    DEFAULT_REGISTRY_DIR,
    RegistryPaths,
    build_registry,
    load_registry,
    load_ticker_profile,
)
from options.universe_scan import scan_equity_options_universe as _scan_equity_options_universe
from options.universe import (
    SP500_TOP_100_TICKERS,
    get_sp500_tickers,
    get_sp500_top40,
)
from options.visualization import TerminalChartDependencyError, render_terminal_charts
from research.options import OptionDomain, OptionResearchWorkflow

options_app = ProfessionalTyper(help="Options analytics commands")
registry_app = ProfessionalTyper(help="Per-ticker playbook registry (S&P top 40)")
options_research_app = ProfessionalTyper(help="Read-only options research workflows")
crypto_research_app = ProfessionalTyper(help="Crypto options research (BTC and ETH)")
stocks_research_app = ProfessionalTyper(help="Equity options research")
options_app.add_typer(registry_app, name="registry")
options_app.add_typer(crypto_research_app, name="crypto")
options_app.add_typer(stocks_research_app, name="stocks")


def _research_rows(input_file: Path | None) -> list[dict]:
    if input_file is None:
        return []
    if not input_file.exists() or not input_file.is_file():
        raise typer.BadParameter(f"input file does not exist: {input_file}", param_hint="--input-file")
    payload = json.loads(input_file.read_text(encoding="utf-8"))
    if isinstance(payload, dict):
        payload = payload.get("snapshots", payload.get("outcomes", []))
    if not isinstance(payload, list) or not all(isinstance(item, dict) for item in payload):
        raise typer.BadParameter("input file must contain a JSON list of objects", param_hint="--input-file")
    return payload


def _emit_research_result(result, *, json_out: bool, output: Path | None) -> None:
    rendered = result.to_json() if json_out else result.to_markdown()
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered, encoding="utf-8")
        typer.echo(str(output))
    else:
        typer.echo(rendered, nl=False)


def _run_options_scan(
    domain: OptionDomain,
    input_file: Path | None,
    decision_time: str | None,
    json_out: bool,
    output: Path | None,
) -> None:
    result = OptionResearchWorkflow().scan(
        domain, _research_rows(input_file), decision_time=decision_time, persist=False
    )
    _emit_research_result(result, json_out=json_out, output=output)


@crypto_research_app.command("scan")
def crypto_research_scan(
    input_file: Path | None = typer.Option(None, "--input-file", help="JSON snapshots; no file means no live data is fetched."),
    decision_time: str | None = typer.Option(None, "--decision-time", help="Timezone-aware ISO timestamp."),
    json_out: bool = typer.Option(False, "--json", help="Emit structured JSON."),
    output: Path | None = typer.Option(None, "--output", help="Optional report output path."),
) -> None:
    """Scan BTC/ETH options inputs without producing an execution instruction."""
    _run_options_scan(OptionDomain.CRYPTO, input_file, decision_time, json_out, output)


@stocks_research_app.command("scan")
def stocks_research_scan(
    input_file: Path | None = typer.Option(None, "--input-file", help="JSON snapshots; no file means no live data is fetched."),
    decision_time: str | None = typer.Option(None, "--decision-time", help="Timezone-aware ISO timestamp."),
    json_out: bool = typer.Option(False, "--json", help="Emit structured JSON."),
    output: Path | None = typer.Option(None, "--output", help="Optional report output path."),
) -> None:
    """Scan equity options inputs without producing an execution instruction."""
    _run_options_scan(OptionDomain.STOCKS, input_file, decision_time, json_out, output)


def _build_options_analyzer(*, source: str) -> OptionsAnalyzer:
    try:
        return OptionsAnalyzer(fetcher_source=source)
    except TypeError:
        # Test doubles in unit tests may still expose the older constructor.
        return OptionsAnalyzer()


def _slug(value: str) -> str:
    keep = [ch if ch.isalnum() or ch in {
        "_", "-"} else "_" for ch in value.strip()]
    normalized = "".join(keep).strip("_")
    return normalized or "ticker"


def _default_reports_dir(analyzer: OptionsAnalyzer) -> Path:
    cfg = getattr(analyzer, "config", None)
    reports_dir = getattr(cfg, "reports_dir", None)
    if reports_dir is not None:
        return Path(reports_dir)
    return Path("data") / "options_cache" / "reports"


def _resolve_json_report_path(
    *,
    analyzer: OptionsAnalyzer,
    ticker: str,
    json_path: str | None,
) -> Path:
    if json_path:
        return Path(json_path).expanduser()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    reports_dir = _default_reports_dir(analyzer)
    return reports_dir / f"{_slug(ticker)}_options_report_{stamp}.json"


def _write_json_report(*, payload: dict, out_path: Path) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2,
                        default=str), encoding="utf-8")
    return out_path


def _render_eth_weekly_decision(payload: dict) -> None:
    decision = payload.get("decision", "UNKNOWN")
    reason = payload.get("reason", "")
    momentum = payload.get("momentum") or {}
    option = payload.get("option") or {}
    profile = payload.get("profile") or {}

    typer.echo(f"ETH weekly options: {decision}")
    typer.echo(f"- reason: {reason}")
    typer.echo(
        "- momentum: "
        f"side={momentum.get('side')} "
        f"tradeable={momentum.get('tradeable')} "
        f"confidence={momentum.get('confidence_score')} "
        f"rr={momentum.get('rr_estimated')}"
    )
    typer.echo(
        "- risk: "
        f"account=${profile.get('account_equity')} "
        f"max_loss=${profile.get('max_loss_usd')} "
        f"a_plus_max=${profile.get('max_a_plus_loss_usd')}"
    )
    if option:
        metrics = option.get("metrics") or {}
        typer.echo(
            "- structure: "
            f"{option.get('strategy_name')} "
            f"source={option.get('source')} "
            f"max_loss={metrics.get('max_loss')} "
            f"pop={metrics.get('pop')} "
            f"touch={metrics.get('probability_of_touch')} "
            f"ev={metrics.get('expected_value')}"
        )
    for item in (payload.get("watch") or [])[:3]:
        metrics = item.get("metrics") or {}
        blockers_for_item = item.get("blockers") or []
        typer.echo(
            "- watch: "
            f"{item.get('strategy_name')} "
            f"source={item.get('source')} "
            f"max_loss={metrics.get('max_loss')} "
            f"blockers={','.join(str(blocker) for blocker in blockers_for_item) or 'none'}"
        )
    blockers = payload.get("blockers") or []
    if blockers:
        typer.echo(f"- blockers: {', '.join(str(item) for item in blockers)}")


def _as_float(value: object) -> float | None:
    try:
        if isinstance(value, bool):
            return float(int(value))
        if isinstance(value, (int, float, str)):
            return float(value)
    except (TypeError, ValueError):
        return None
    return None


def _expected_value_cell(value: object) -> Text:
    expected_value = _as_float(value)
    if expected_value is None:
        return Text(str(value))
    if expected_value < 0:
        return Text(f"NEG EV {expected_value:.2f}", style="red bold")
    return Text(f"{expected_value:.2f}", style="green")


def _negative_ev_warning(recommendations: list[dict]) -> str | None:
    if not recommendations:
        return None
    top_metrics = recommendations[0].get("metrics") or {}
    top_strategy = (recommendations[0].get(
        "strategy") or {}).get("name", "top strategy")
    top_ev = _as_float(top_metrics.get("expected_value"))
    if top_ev is None or top_ev >= 0:
        return None
    return (
        f"Top-ranked strategy {top_strategy} has negative modeled expected value ({top_ev:.2f}). "
        "Treat the setup as a pass-or-recheck candidate before sizing risk."
    )


def _collect_risk_warnings(payload: dict, recommendations: list[dict]) -> list[str]:
    warnings: list[str] = []
    negative_warning = _negative_ev_warning(recommendations)
    if negative_warning:
        warnings.append(negative_warning)

    overlay = payload.get("analysis_overlay") or {}
    overlay_warnings = overlay.get("warnings") or []
    for warning in overlay_warnings:
        text = str(warning).strip()
        if text and text not in warnings:
            warnings.append(text)
    return warnings


def _trade_decision(payload: dict) -> dict:
    overlay = payload.get("analysis_overlay") or {}
    return dict(overlay.get("trade_decision") or {})


def _strategy_bias_label(strategy_name: str) -> str:
    if strategy_name in {
        "bull_put_credit_spread",
        "bull_call_debit_spread",
        "cash_secured_put",
        "covered_call",
    }:
        return "Bullish"
    if strategy_name in {"bear_call_credit_spread", "bear_put_debit_spread"}:
        return "Bearish"
    if strategy_name in {"iron_condor", "call_butterfly"}:
        return "Neutral"
    if strategy_name in {"long_strangle", "long_straddle"}:
        return "Long Volatility"
    return "Other"


def _group_recommendations_by_bias(recommendations: list[dict]) -> list[tuple[str, list[dict]]]:
    grouped: dict[str, list[dict]] = {}
    for rec in recommendations:
        strategy_name = str(
            (rec.get("strategy") or {}).get("name") or "unknown")
        label = _strategy_bias_label(strategy_name)
        grouped.setdefault(label, []).append(rec)
    order = ["Bullish", "Bearish", "Neutral", "Long Volatility", "Other"]
    return [(label, grouped[label]) for label in order if grouped.get(label)]


def _render_bias_tables(console: Console, recommendations: list[dict]) -> None:
    rank = 1
    for label, recs in _group_recommendations_by_bias(recommendations):
        rec_table = Table(
            title=f"{label} Strategy Ranking", box=box.SIMPLE_HEAVY)
        rec_table.add_column("Rank", justify="right")
        rec_table.add_column("Strategy")
        rec_table.add_column("Score", justify="right")
        rec_table.add_column("PoP %", justify="right")
        rec_table.add_column("EV", justify="right")
        rec_table.add_column("Touch %", justify="right")
        rec_table.add_column("Tradeoff")
        for rec in recs:
            strategy = (rec.get("strategy", {}) or {}).get("name", "unknown")
            metrics = rec.get("metrics", {}) or {}
            rec_table.add_row(
                str(rank),
                str(strategy),
                str(metrics.get("composite_score")),
                str(metrics.get("pop")),
                _expected_value_cell(metrics.get("expected_value")),
                str(metrics.get("probability_of_touch")),
                str(rec.get("tradeoff_comment") or ""),
            )
            rank += 1
        console.print(rec_table)


def _render_strategy_comparison_table(console: Console, payload: dict) -> None:
    overlay = payload.get("analysis_overlay") or {}
    rows = list(overlay.get("strategy_comparison_table") or [])
    if not rows:
        return

    table = Table(title="Strategy Comparison", box=box.SIMPLE_HEAVY)
    table.add_column("Strategy")
    table.add_column("PoP", justify="right")
    table.add_column("EV", justify="right")
    table.add_column("Max Loss", justify="right")
    table.add_column("Prob. Touch", justify="right")
    table.add_column("Forgivingness", justify="right")
    table.add_column("Theta/Day", justify="right")
    table.add_column("Key Commentary")

    for row in rows:
        strategy = str(row.get("strategy") or "unknown")
        table.add_row(
            strategy,
            str(row.get("pop")),
            str(row.get("expected_value")),
            str(row.get("max_loss")),
            str(row.get("probability_of_touch")),
            str(row.get("forgivingness_score")),
            str(row.get("theta_per_day")),
            str(row.get("key_commentary") or ""),
        )
    console.print(table)


def _render_prompt_data_block(
    console: Console,
    payload: dict,
    *,
    report_path: Path | None,
    llm_prompt_enabled: bool,
) -> None:
    """Render prompt/data block used by terminal chart mode."""
    console.print("\n[bold cyan]=== Prompt and Data ===[/bold cyan]")

    underlying = payload.get("underlying_analysis", {}) or {}
    implied = underlying.get("implied_volatility", {}) or {}
    expected_move = underlying.get("expected_move", {}) or {}
    recommendations = list(payload.get("recommendations") or [])

    data_table = Table(box=box.SIMPLE, show_header=True)
    data_table.add_column("Field")
    data_table.add_column("Value")
    data_table.add_row("Ticker", str(payload.get("ticker") or "N/A"))
    data_table.add_row("Generated At", str(
        payload.get("generated_at") or "N/A"))
    data_table.add_row("Underlying Price", str(underlying.get("price")))
    data_table.add_row("IV Rank", str(implied.get("iv_rank")))
    data_table.add_row("Expected Move (1sd)", str(
        expected_move.get("one_std_move")))
    data_table.add_row("Top Recommendations", str(len(recommendations[:3])))
    if report_path is not None:
        data_table.add_row("JSON Report", str(report_path))
    console.print(data_table)

    if llm_prompt_enabled:
        prompt = str(payload.get("llm_prompt") or "")
        llm_paths = payload.get("llm_paths") or {}
        if prompt:
            console.print(
                Panel(prompt, title="LLM Prompt (Copy/Paste)",
                      border_style="green")
            )
        console.print(
            Panel(
                json.dumps(llm_paths, indent=2, default=str),
                title="LLM Paths (Separate Block)",
                border_style="yellow",
            )
        )
        return

    console.print(
        Panel(
            "LLM prompt block is disabled. Re-run with --llm-prompt to include a copy-ready prompt and llm_paths payload.",
            title="Prompt Hint",
            border_style="cyan",
        )
    )


def _render_sheet_output(
    console: Console,
    payload_out: dict,
    *,
    recommendations: list[dict],
    risk_warnings: list[str],
    report_path: Path | None,
    include_llm_prompt_panels: bool,
) -> None:
    """Render Rich sheet output for options analysis."""
    underlying = payload_out.get("underlying_analysis", {})
    implied = underlying.get("implied_volatility", {}) or {}
    expected_move = underlying.get("expected_move", {}) or {}
    snapshot = underlying.get("options_market_snapshot", {}) or {}

    summary = Table(
        title=f"Options Summary - {payload_out.get('ticker')}", box=box.SIMPLE_HEAVY)
    summary.add_column("Metric")
    summary.add_column("Value")
    summary.add_row("Price", str(underlying.get("price")))
    summary.add_row("IV Mean", str(implied.get("iv_mean")))
    summary.add_row("IV Rank", str(implied.get("iv_rank")))
    summary.add_row("Expected Move (1sd)", str(
        expected_move.get("one_std_move")))
    summary.add_row("Contracts", str(snapshot.get("contracts")))
    summary.add_row("Put/Call OI Ratio",
                    str(snapshot.get("put_call_oi_ratio")))
    console.print(summary)

    decision = _trade_decision(payload_out)
    if decision:
        status = str(decision.get("status") or "unknown").replace("_", " ").upper()
        style = "green" if decision.get("status") == "trade_candidate" else "red"
        console.print(
            Panel(
                str(decision.get("reason") or ""),
                title=f"Trade Decision: {status}",
                border_style=style,
            )
        )

    _render_bias_tables(console, recommendations)
    _render_strategy_comparison_table(console, payload_out)

    for warning in risk_warnings:
        console.print(
            Panel(
                warning,
                title="Risk Warning",
                border_style="red",
            )
        )

    charts = payload_out.get("charts", {}) or {}
    chart_table = Table(title="Chart Artifacts", box=box.SIMPLE)
    chart_table.add_column("Chart")
    chart_table.add_column("Path")
    for key in ["strategy_ranking", "payoff", "greeks", "monte_carlo"]:
        chart_table.add_row(key, str(charts.get(key)))
    console.print(chart_table)

    if report_path is not None:
        copy_help = Text(
            f"JSON report: {report_path}\n"
            f"View: cat {report_path}\n"
            f"Copy (macOS): pbcopy < {report_path}",
        )
        console.print(
            Panel(copy_help, title="Copyable JSON", border_style="cyan"))

    if include_llm_prompt_panels:
        prompt = str(payload_out.get("llm_prompt") or "")
        console.print(
            Panel(prompt, title="LLM Prompt (Copy/Paste)", border_style="green"))
        llm_paths = payload_out.get("llm_paths") or {}
        console.print(
            Panel(
                json.dumps(llm_paths, indent=2, default=str),
                title="LLM Paths (Separate Block)",
                border_style="yellow",
            )
        )


def _render_plain_output(
    payload_out: dict,
    *,
    recommendations: list[dict],
    report_path: Path | None,
    risk_warnings: list[str],
) -> None:
    """Render minimal plain-text output."""
    underlying = payload_out.get("underlying_analysis", {})
    typer.echo(f"Ticker: {payload_out.get('ticker')}")
    typer.echo(f"Price: {underlying.get('price')}")
    decision = _trade_decision(payload_out)
    if decision:
        typer.echo(
            f"Decision: {decision.get('status')} - {decision.get('reason')}"
        )
    if report_path is not None:
        typer.echo(f"JSON report: {report_path}")
    for warning in risk_warnings:
        typer.echo(f"WARNING: {warning}")
    for label, recs in _group_recommendations_by_bias(recommendations):
        typer.echo(f"{label} strategies:")
        for rec in recs:
            strategy = (rec.get("strategy", {}) or {}).get("name", "unknown")
            metrics = rec.get("metrics", {}) or {}
            ev_value = _as_float(metrics.get("expected_value"))
            ev_display = f"{ev_value:.2f}" if ev_value is not None else str(
                metrics.get("expected_value"))
            if ev_value is not None and ev_value < 0:
                ev_display = f"NEG_EV:{ev_display}"
            typer.echo(
                f"- {strategy}: score={metrics.get('composite_score')} pop={metrics.get('pop')} ev={ev_display}"
            )


def _parse_coin_list(value: str) -> list[str]:
    raw = value.replace(" ", ",").split(",")
    return [item.strip().upper() for item in raw if item.strip()]


def _render_opportunities_sheet(console: Console, payload: dict) -> None:
    summary = payload.get("summary") or {}
    momentum = payload.get("momentum") or {}
    tf = momentum.get("timeframes") or {}

    header = Table(title="Options Opportunities Summary", box=box.SIMPLE_HEAVY)
    header.add_column("Metric")
    header.add_column("Value")
    header.add_row("Coins Requested", str(summary.get("coins_requested")))
    header.add_row("Coins Supported", str(summary.get("coins_supported")))
    header.add_row("Momentum Allowed", str(summary.get("momentum_allowed")))
    header.add_row("Options Ready", str(summary.get("options_ready")))
    if tf:
        header.add_row(
            "Timeframes",
            f"bias={tf.get('bias')} setup={tf.get('setup')} trigger={tf.get('trigger')}",
        )
    console.print(header)

    table = Table(title="BTC/ETH Opportunity Details", box=box.SIMPLE_HEAVY)
    table.add_column("Coin")
    table.add_column("Status")
    table.add_column("Momentum")
    table.add_column("Top Strategy")
    table.add_column("EV", justify="right")
    table.add_column("Notes")

    opportunities = payload.get("opportunities") or {}
    for coin in sorted(opportunities.keys()):
        entry = opportunities.get(coin) or {}
        status = str(entry.get("status") or "unknown")
        momentum_ctx = entry.get("momentum") or {}
        momentum_value = momentum_ctx.get("confidence_score")
        momentum_display = str(
            momentum_value) if momentum_value is not None else "n/a"
        decision = entry.get("trade_decision") or {}
        top_strategy = str(
            entry.get("executable_strategy") or entry.get("top_strategy") or "-"
        ).replace("_", " ")
        ev = (entry.get("top_metrics") or {}).get("expected_value")
        if entry.get("executable_strategy"):
            ev = (entry.get("executable_metrics") or {}).get("expected_value")
        notes = (
            decision.get("status")
            or entry.get("reason")
            or entry.get("error")
            or ""
        )
        table.add_row(
            coin,
            status,
            momentum_display,
            top_strategy,
            "-" if ev is None else str(ev),
            str(notes),
        )
    console.print(table)


def _render_equity_scan_sheet(console: Console, payload: dict) -> None:
    summary = payload.get("summary") or {}
    header = Table(title="Options Equity Universe Scan", box=box.SIMPLE_HEAVY)
    header.add_column("Metric")
    header.add_column("Value")
    header.add_row("Universe", str(payload.get("universe")))
    header.add_row("Days To Exp", str(payload.get("days_to_exp")))
    header.add_row("Tickers Requested", str(summary.get("tickers_requested")))
    header.add_row("Tickers Scanned", str(summary.get("tickers_scanned")))
    header.add_row("Trade Candidates", str(summary.get("trade_candidates")))
    header.add_row("Errors", str(summary.get("errors")))
    header.add_row("Scan Status", str(summary.get("scan_status") or "unknown"))
    if summary.get("coverage_ratio") is not None:
        header.add_row("Coverage", f"{float(summary.get('coverage_ratio') or 0.0):.1%}")
    console.print(header)

    warnings = list(payload.get("warnings") or [])
    if warnings:
        console.print(
            Panel(
                "\n".join(str(warning) for warning in warnings),
                title="Scan Quality Warning",
                border_style="yellow",
            )
        )

    table = Table(title="Top Executable Trades", box=box.SIMPLE_HEAVY)
    table.add_column("Rank", justify="right")
    table.add_column("Ticker")
    table.add_column("Strategy")
    table.add_column("Score", justify="right")
    table.add_column("EV", justify="right")
    table.add_column("PoP", justify="right")
    table.add_column("Touch", justify="right")
    table.add_column("Max Loss", justify="right")

    ranked = list(payload.get("ranked") or [])
    if not ranked:
        table.add_row(
            "-",
            "-",
            "No trade candidates passed the quality gate",
            "-",
            "-",
            "-",
            "-",
            "-",
        )
    for idx, item in enumerate(ranked, start=1):
        table.add_row(
            str(idx),
            str(item.get("ticker")),
            str(item.get("strategy_name") or "").replace("_", " "),
            str(item.get("composite_score")),
            str(item.get("expected_value")),
            str(item.get("pop")),
            str(item.get("probability_of_touch")),
            str(item.get("max_loss")),
        )
    console.print(table)

    results = payload.get("results") or {}
    for idx, item in enumerate(ranked, start=1):
        ticker = str(item.get("ticker") or "")
        detail = results.get(ticker) or {}
        setup = detail.get("executable_setup") or {}
        warnings = list(detail.get("warnings") or [])
        lines = [
            f"Strategy: {str(item.get('strategy_name') or '').replace('_', ' ')}",
            f"Setup: {setup.get('setup_summary') or item.get('setup_summary') or 'n/a'}",
            f"Rationale: {setup.get('rationale') or item.get('rationale') or 'n/a'}",
            (
                "Metrics: "
                f"score={item.get('composite_score')} "
                f"EV={item.get('expected_value')} "
                f"PoP={item.get('pop')} "
                f"touch={item.get('probability_of_touch')} "
                f"max_loss={item.get('max_loss')}"
            ),
            (
                f"Deep dive: nave options analyze --ticker {ticker} "
                f"--days-to-exp {payload.get('days_to_exp')}"
            ),
        ]
        if warnings:
            lines.append("Warnings: " + " | ".join(str(warning) for warning in warnings[:2]))
        console.print(
            Panel(
                "\n".join(lines),
                title=f"#{idx} {ticker} Trade Detail",
                border_style="green",
            )
        )

    error_rows = [
        row for row in results.values()
        if isinstance(row, dict) and row.get("status") == "error"
    ]
    if error_rows:
        error_table = Table(title="Scan Errors (first 10)", box=box.SIMPLE)
        error_table.add_column("Ticker")
        error_table.add_column("Error")
        for row in error_rows[:10]:
            error_table.add_row(str(row.get("ticker")), str(row.get("error")))
        console.print(error_table)


@options_app.command("analyze")
def analyze(
    symbol: str | None = typer.Argument(
        None,
        metavar="TICKER",
        help="Optional ticker symbol positional argument (e.g. MSFT or BTC)",
    ),
    ticker: str | None = typer.Option(
        None, "--ticker", help="Underlying ticker symbol"),
    source: str = typer.Option(
        "yfinance",
        "--source",
        help="Data source for chain fetch (yfinance|deribit)",
    ),
    days_to_exp: int = typer.Option(
        30, "--days-to-exp", min=1, max=365, help="Target days to expiration"),
    json_out: bool = typer.Option(
        False, "--json", help="Emit machine-stable JSON output"),
    sheet: bool = typer.Option(
        True, "--sheet/--no-sheet", help="Render human output as a Rich table"),
    save_json: bool = typer.Option(
        True,
        "--save-json/--no-save-json",
        help="Persist the analysis payload to a .json report file for copy/share",
    ),
    json_path: str | None = typer.Option(
        None,
        "--json-path",
        help="Optional output path for the saved .json report",
    ),
    terminal_mode: bool = typer.Option(
        False,
        "--terminal",
        "--ascii",
        help="Render terminal-native charts (plotext) in additive mode",
    ),
    llm_prompt: bool = typer.Option(
        False,
        "--llm-prompt",
        help="Print a copy-ready prompt for another LLM using the saved JSON report",
    ),
    sp500_scan: bool = typer.Option(
        False,
        "--sp500-scan",
        help="Scan a liquid S&P 500 top-100 ticker universe and return executable trades",
    ),
    sp500_limit: int = typer.Option(
        100,
        "--sp500-limit",
        min=1,
        max=200,
        help="Number of default S&P 500 universe tickers to scan (up to 200)",
    ),
    directional_override: bool = typer.Option(
        True,
        "--directional-override/--no-directional-override",
        help="Allow bias-aligned income setups flagged as directional override when strict gate blocks",
    ),
    allow_mega_cap_income: bool = typer.Option(
        True,
        "--allow-mega-cap-income/--no-allow-mega-cap-income",
        help="Relax income gate for liquid mega-caps with high PoP and low touch when bias aligns",
    ),
    top_trades: int = typer.Option(
        3,
        "--top-trades",
        min=1,
        max=20,
        help="Number of executable trade candidates to return in universe scan mode",
    ),
    scan_workers: int = typer.Option(
        6,
        "--scan-workers",
        min=1,
        max=16,
        help="Concurrent workers for S&P 500 scan mode; use 1 for sequential debugging",
    ),
    strategy: str | None = typer.Option(
        None,
        "--strategy",
        help="Evaluate a manual strategy instead of auto-generated candidates; currently bull-put",
    ),
    expiration: str | None = typer.Option(
        None,
        "--expiration",
        help="Manual strategy expiration in YYYY-MM-DD format",
    ),
    short_put: float | None = typer.Option(
        None,
        "--short-put",
        help="Manual bull-put short put strike",
    ),
    long_put: float | None = typer.Option(
        None,
        "--long-put",
        help="Manual bull-put long put strike",
    ),
    short_premium: float | None = typer.Option(
        None,
        "--short-premium",
        help="Manual short-leg premium; uses chain mid price if omitted",
    ),
    long_premium: float | None = typer.Option(
        None,
        "--long-premium",
        help="Manual long-leg premium; uses chain mid price if omitted",
    ),
) -> None:
    """Run options analysis and print recommendations."""
    resolved_ticker = (symbol or ticker or "MSFT").strip().upper()
    analyzer = _build_options_analyzer(source=source)
    console = Console()

    if sp500_scan:
        scan_tickers = (
            list(get_sp500_tickers(sp500_limit))
            if sp500_limit > len(SP500_TOP_100_TICKERS)
            else list(SP500_TOP_100_TICKERS[:sp500_limit])
        )
        show_progress = not json_out

        if show_progress:
            progress = Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                BarColumn(),
                TextColumn("{task.completed}/{task.total}"),
                TimeElapsedColumn(),
                console=console,
            )
            with progress:
                task_id = progress.add_task(
                    f"Scanning S&P 500 options universe ({scan_workers} workers)",
                    total=len(scan_tickers),
                )

                def _on_progress(row: dict) -> None:
                    status = str(row.get("status") or "unknown")
                    ticker_name = str(row.get("ticker") or "")
                    progress.update(
                        task_id,
                        advance=1,
                        description=f"{ticker_name}: {status}",
                    )

                payload = _scan_equity_options_universe(
                    analyzer=analyzer,
                    analyzer_factory=lambda: _build_options_analyzer(source=source),
                    tickers=scan_tickers,
                    days_to_exp=days_to_exp,
                    top_trades=top_trades,
                    workers=scan_workers,
                    progress_callback=_on_progress,
                )
        else:
            payload = _scan_equity_options_universe(
                analyzer=analyzer,
                analyzer_factory=lambda: _build_options_analyzer(source=source),
                tickers=scan_tickers,
                days_to_exp=days_to_exp,
                top_trades=top_trades,
                workers=scan_workers,
            )

        report_path: Path | None = None
        if save_json:
            report_path = _resolve_json_report_path(
                analyzer=analyzer,
                ticker=f"sp500_top_{sp500_limit}_options_scan",
                json_path=json_path,
            )
            payload = dict(payload)
            artifacts = dict(payload.get("artifacts") or {})
            artifacts["json_report_path"] = str(report_path)
            payload["artifacts"] = artifacts
            report_path = _write_json_report(payload=payload, out_path=report_path)

        if json_out:
            typer.echo(json.dumps(payload, indent=2, default=str))
            return

        if sheet:
            _render_equity_scan_sheet(console, payload)
            if report_path is not None:
                console.print(
                    Panel(
                        f"JSON report: {report_path}",
                        title="Scan Report",
                        border_style="cyan",
                    )
                )
            return

        typer.echo("Options equity universe scan")
        summary = payload.get("summary") or {}
        typer.echo(
            f"- scanned={summary.get('tickers_scanned')} "
            f"trade_candidates={summary.get('trade_candidates')} "
            f"errors={summary.get('errors')} "
            f"scan_status={summary.get('scan_status')}"
        )
        for warning in payload.get("warnings") or []:
            typer.echo(f"- warning={warning}")
        for item in payload.get("ranked") or []:
            typer.echo(
                f"- {item.get('ticker')}: {item.get('strategy_name')} "
                f"score={item.get('composite_score')} ev={item.get('expected_value')}"
            )
        if report_path is not None:
            typer.echo(f"JSON report: {report_path}")
        return

    try:
        if strategy:
            payload = analyzer.run(
                ticker=resolved_ticker,
                days_to_exp=days_to_exp,
                strategy=strategy,
                expiration=expiration,
                short_put=short_put,
                long_put=long_put,
                short_premium=short_premium,
                long_premium=long_premium,
                prefer_directional_override=directional_override,
                allow_mega_cap_income_pass=allow_mega_cap_income,
            )
        else:
            payload = analyzer.run(
                ticker=resolved_ticker,
                days_to_exp=days_to_exp,
                prefer_directional_override=directional_override,
                allow_mega_cap_income_pass=allow_mega_cap_income,
            )
    except OptionsError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    report_path: Path | None = None
    if save_json:
        report_path = _resolve_json_report_path(
            analyzer=analyzer,
            ticker=str(payload.get("ticker") or resolved_ticker),
            json_path=json_path,
        )

    payload_out = dict(payload)
    artifacts = dict(payload_out.get("artifacts") or {})
    artifacts["json_report_path"] = str(
        report_path) if report_path is not None else None
    payload_out["artifacts"] = artifacts

    if llm_prompt:
        prompt_source = dict(payload_out)
        payload_out["llm_prompt"] = build_llm_prompt(prompt_source)
        payload_out["llm_paths"] = build_llm_paths(payload_out, report_path)

    if save_json:
        report_path = _write_json_report(
            payload=payload_out,
            out_path=report_path if report_path is not None else _resolve_json_report_path(
                analyzer=analyzer,
                ticker=str(payload.get("ticker") or resolved_ticker),
                json_path=json_path,
            ),
        )

    if json_out:
        typer.echo(json.dumps(payload_out, indent=2, default=str))
        return

    recommendations = payload_out.get("recommendations", [])[:3]
    risk_warnings = _collect_risk_warnings(payload_out, recommendations)

    if terminal_mode:
        _render_prompt_data_block(
            console,
            payload_out,
            report_path=report_path,
            llm_prompt_enabled=llm_prompt,
        )

        console.print("\n[bold magenta]=== Graphs ===[/bold magenta]")
        try:
            render_terminal_charts(payload_out, console=console)
        except TerminalChartDependencyError as exc:
            typer.echo(f"Error: {exc}", err=True)
            raise typer.Exit(code=1) from exc

        console.print("\n[bold green]=== Summary ===[/bold green]")
        if sheet:
            _render_sheet_output(
                console,
                payload_out,
                recommendations=recommendations,
                risk_warnings=risk_warnings,
                report_path=report_path,
                include_llm_prompt_panels=False,
            )
        else:
            _render_plain_output(
                payload_out,
                recommendations=recommendations,
                report_path=report_path,
                risk_warnings=risk_warnings,
            )
        return

    if sheet:
        _render_sheet_output(
            console,
            payload_out,
            recommendations=recommendations,
            risk_warnings=risk_warnings,
            report_path=report_path,
            include_llm_prompt_panels=llm_prompt,
        )
        return

    _render_plain_output(
        payload_out,
        recommendations=recommendations,
        report_path=report_path,
        risk_warnings=risk_warnings,
    )


def _render_registry_table(console: Console, index: dict) -> None:
    table = Table(title="S&P Top-40 — Per-Ticker Learned Setups", box=box.SIMPLE_HEAVY)
    table.add_column("Ticker")
    table.add_column("Bias 20d")
    table.add_column("Merge")
    table.add_column("Learned setup")
    table.add_column("Edge", justify="right")
    table.add_column("Conf.")
    table.add_column("Replay WR", justify="right")
    table.add_column("OOS WR", justify="right")
    table.add_column("Congress", justify="right")
    for sym, meta in sorted((index.get("profiles") or {}).items()):
        wr = meta.get("best_win_rate")
        oos = meta.get("oos_win_rate")
        edge = meta.get("learned_edge_score")
        table.add_row(
            sym,
            str(meta.get("bias_20d") or "-"),
            str(meta.get("merge_status") or "-"),
            str(meta.get("preferred_setup") or "-").replace("_", " "),
            f"{edge:.0f}" if isinstance(edge, (int, float)) else "-",
            str(meta.get("learned_confidence") or "-"),
            f"{wr:.0%}" if isinstance(wr, (int, float)) else "-",
            f"{oos:.0%}" if isinstance(oos, (int, float)) else "-",
            str(meta.get("congress_mentions") or 0),
        )
    console.print(table)


def _render_learned_strategy_matrix(console: Console, profiles: dict[str, dict]) -> None:
    """Full per-ticker setup ranking for learning (not one global template)."""
    table = Table(title="Per-ticker setup learning matrix", box=box.SIMPLE_HEAVY)
    table.add_column("Ticker")
    table.add_column("Primary (20d)")
    table.add_column("Bull")
    table.add_column("Bear")
    table.add_column("Neutral")
    table.add_column("Avoid")
    for sym in sorted(profiles.keys()):
        learned = profiles[sym].get("learned_strategy") or {}
        primary = (learned.get("primary") or {}).get("strategy") or "-"
        by_bias = learned.get("by_bias") or {}
        bull = (by_bias.get("bullish") or {}).get("strategy", "-")
        bear = (by_bias.get("bearish") or {}).get("strategy", "-")
        neut = (by_bias.get("neutral") or {}).get("strategy", "-")
        avoid = ", ".join(
            (a.get("strategy") or "")[:12] for a in (learned.get("avoid") or [])[:2]
        ) or "-"
        table.add_row(
            sym,
            str(primary).replace("_", " "),
            str(bull).replace("_", " "),
            str(bear).replace("_", " "),
            str(neut).replace("_", " "),
            avoid.replace("_", " "),
        )
    console.print(table)


@registry_app.command("build")
def registry_build(
    tickers: str = typer.Option(
        "",
        "--tickers",
        help="Comma-separated tickers (default: S&P top 40)",
    ),
    replay_json: str | None = typer.Option(
        None,
        "--replay-json",
        help="Path to options_yearly_*.json for setup stats",
    ),
    live: bool = typer.Option(
        False,
        "--live",
        help="Include live options snapshot per ticker (slow)",
    ),
    out_dir: str = typer.Option(
        str(DEFAULT_REGISTRY_DIR),
        "--out",
        help="Registry directory",
    ),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Build playbook cards: price behavior, setup stats, X, Congress."""
    symbols = (
        [t.strip().upper() for t in tickers.split(",") if t.strip()]
        if tickers.strip()
        else list(get_sp500_top40())
    )
    replay_path = Path(replay_json).expanduser() if replay_json else None
    result = build_registry(
        symbols,
        paths=RegistryPaths(Path(out_dir)),
        replay_json=replay_path,
        include_live_options=live,
    )
    if json_out:
        typer.echo(json.dumps(result["index"], indent=2, default=str))
        return
    console = Console()
    typer.echo(f"Registry built → {out_dir}")
    _render_registry_table(console, result["index"])


@registry_app.command("iterate")
def registry_iterate(
    backtest: bool = typer.Option(
        False,
        "--backtest",
        help="Refresh yearly replay before learning (slow)",
    ),
    months: int = typer.Option(12, "--months"),
    no_gems: bool = typer.Option(False, "--no-gems"),
    folds: int = typer.Option(4, "--folds"),
    replay_json: str | None = typer.Option(None, "--replay-json"),
    out_dir: str = typer.Option(str(DEFAULT_REGISTRY_DIR), "--out"),
) -> None:
    """Full loop: walk-forward validate → merge journal → rebuild registry → gems."""
    from options.strategy_loop import run_strategy_iteration

    scan_fn = None
    if not no_gems:
        def scan_fn(
            *,
            tickers: list[str],
            days_to_exp: int = 30,
            top_trades: int = 10,
            workers: int = 2,
        ) -> dict:
            analyzer = _build_options_analyzer(source="yfinance")
            return _scan_equity_options_universe(
                analyzer=analyzer,
                analyzer_factory=lambda: _build_options_analyzer(source="yfinance"),
                tickers=tickers,
                days_to_exp=days_to_exp,
                top_trades=top_trades,
                workers=workers,
            )

    replay_path = Path(replay_json).expanduser() if replay_json else None
    result = run_strategy_iteration(
        replay_json=replay_path,
        run_backtest=backtest,
        backtest_months=months,
        registry_dir=Path(out_dir),
        run_gems=not no_gems,
        n_folds=folds,
        scan_fn=scan_fn,
    )
    merge = result.get("merge_readiness") or {}
    typer.echo(f"Iteration report: {result['report_md']}")
    typer.echo(f"Registry: {result['registry']['root']}")
    typer.echo(
        f"Merge ready: {merge.get('ready_to_merge')} "
        f"(approved={merge.get('counts', {}).get('approved')}, "
        f"watch={merge.get('counts', {}).get('watch')})"
    )
    if not merge.get("ready_to_merge"):
        raise typer.Exit(code=1)


@registry_app.command("learn")
def registry_learn(
    ticker: str = typer.Option(
        "",
        "--ticker",
        help="Single ticker deep-dive; default shows full matrix",
    ),
    out_dir: str = typer.Option(str(DEFAULT_REGISTRY_DIR), "--out"),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Show per-ticker learned setups (from replay) — each name has its own strategy."""
    paths = RegistryPaths(Path(out_dir))
    loaded = load_registry(paths)
    if loaded.get("status") == "missing":
        typer.echo(loaded.get("hint", "Registry missing"))
        raise typer.Exit(code=1)

    if ticker.strip():
        profile = load_ticker_profile(ticker, paths=paths)
        if profile is None:
            typer.echo(f"No profile for {ticker.upper()}")
            raise typer.Exit(code=1)
        learned = profile.get("learned_strategy") or {}
        if json_out:
            typer.echo(json.dumps(learned, indent=2, default=str))
            return
        console = Console()
        console.print(Panel(f"[bold]{profile['ticker']}[/bold] learned strategy", border_style="green"))
        console.print(learned.get("narrative") or "")
        console.print("\n[bold]Ranked setups (replay)[/bold]")
        for row in learned.get("ranked") or []:
            console.print(
                f"  {row['strategy']}: edge {row['edge_score']:.0f}, "
                f"{row['win_rate']:.0%} win, n={row['trades']}, ${row['avg_pnl_dollars']:.0f} avg"
            )
        console.print("\n[bold]By bias[/bold]")
        for b, pick in (learned.get("by_bias") or {}).items():
            console.print(
                f"  {b}: {pick.get('strategy')} (edge {pick.get('edge_score')}, "
                f"{pick.get('win_rate', 0):.0%} win)"
            )
        if learned.get("avoid"):
            console.print("\n[bold]Avoid[/bold]")
            for a in learned["avoid"]:
                console.print(f"  {a.get('strategy')}: {a.get('reason')}")
        wf = learned.get("walkforward") or {}
        if wf:
            console.print("\n[bold]Walk-forward (out-of-sample)[/bold]")
            console.print(f"  {wf.get('narrative') or wf.get('status')}")
            for fold in wf.get("folds") or []:
                wr = fold.get("win_rate")
                wr_s = f"{wr:.0%}" if wr is not None else "—"
                console.print(
                    f"  fold {fold.get('fold')}: primary={fold.get('primary')} "
                    f"test_n={fold.get('test_trades')} win={wr_s}"
                )
        return

    profiles: dict[str, dict] = {}
    for sym in loaded["index"].get("tickers") or []:
        p = load_ticker_profile(sym, paths=paths)
        if p:
            profiles[sym] = p
    if json_out:
        payload = {s: p.get("learned_strategy") for s, p in profiles.items()}
        typer.echo(json.dumps(payload, indent=2, default=str))
        return
    _render_learned_strategy_matrix(Console(), profiles)


@registry_app.command("list")
def registry_list(
    out_dir: str = typer.Option(str(DEFAULT_REGISTRY_DIR), "--out"),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """List indexed playbook summaries."""
    loaded = load_registry(RegistryPaths(Path(out_dir)))
    if loaded.get("status") == "missing":
        typer.echo(loaded.get("hint", "Registry missing"))
        raise typer.Exit(code=1)
    index = loaded["index"]
    if json_out:
        typer.echo(json.dumps(index, indent=2))
        return
    _render_registry_table(Console(), index)


@registry_app.command("show")
def registry_show(
    ticker: str = typer.Argument(..., help="Ticker symbol"),
    out_dir: str = typer.Option(str(DEFAULT_REGISTRY_DIR), "--out"),
    json_out: bool = typer.Option(False, "--json"),
    sheet: bool = typer.Option(True, "--sheet/--no-sheet"),
) -> None:
    """Show full playbook card for one ticker."""
    profile = load_ticker_profile(ticker, RegistryPaths(Path(out_dir)))
    if profile is None:
        typer.echo(f"No profile for {ticker.upper()}. Run: nave options registry build")
        raise typer.Exit(code=1)
    if json_out:
        typer.echo(json.dumps(profile, indent=2, default=str))
        return
    if not sheet:
        typer.echo(json.dumps(profile, indent=2, default=str))
        return
    console = Console()
    pb = profile.get("playbook") or {}
    console.print(Panel(f"[bold]{profile['ticker']}[/bold] playbook", border_style="cyan"))
    console.print(f"[dim]Updated[/dim] {profile.get('updated_at')}")
    console.print("\n[bold]Price behavior[/bold]")
    for k, v in (profile.get("price_behavior") or {}).items():
        console.print(f"  {k}: {v}")
    learned = profile.get("learned_strategy") or {}
    console.print("\n[bold]Learned strategy (per ticker)[/bold]")
    console.print(f"  {learned.get('narrative') or '—'}")
    console.print(
        f"  confidence={learned.get('confidence')} "
        f"size={(learned.get('execution') or {}).get('size')}"
    )
    console.print("\n[bold]All setups ranked (replay)[/bold]")
    for strat in learned.get("ranked") or (profile.get("setup_performance") or {}).get(
        "strategies"
    ) or []:
        edge = strat.get("edge_score", "-")
        console.print(
            f"  - {strat['strategy']}: edge {edge}, {strat['trades']} trades, "
            f"win {strat['win_rate']:.0%}, avg ${strat['avg_pnl_dollars']:.0f}"
        )
    console.print("\n[bold]Best setup if bias shifts[/bold]")
    for b, pick in (learned.get("by_bias") or {}).items():
        console.print(
            f"  {b}: {pick.get('strategy')} ({pick.get('win_rate', 0):.0%} win, n={pick.get('trades')})"
        )
    console.print("\n[bold]X — entry / targets / opinion[/bold]")
    xo = profile.get("x_opinion") or {}
    console.print(f"  {xo.get('summary') or xo.get('hint') or xo.get('status')}")
    if xo.get("entry_prices"):
        console.print(f"  entry prices mentioned: {xo.get('entry_prices')}")
    if xo.get("target_prices"):
        console.print(f"  targets mentioned: {xo.get('target_prices')}")
    for quote in (xo.get("sample_quotes") or [])[:2]:
        console.print(f"  quote: {quote[:100]}...")
    console.print("\n[bold]Congress holdings proxy[/bold]")
    cg = profile.get("congress_holdings") or profile.get("congress") or {}
    console.print(
        f"  mentions={cg.get('mentions')} flow={cg.get('flow_lean')} "
        f"(P {cg.get('purchase_count')} / S {cg.get('sale_count')})"
    )
    console.print("\n[bold]Rules[/bold]")
    for rule in pb.get("rules") or []:
        console.print(f"  • {rule}")
    if profile.get("live_options"):
        console.print("\n[bold]Live options[/bold]")
        lo = profile["live_options"]
        console.print(
            f"  {lo.get('trade_decision')} | {lo.get('strategy')} | "
            f"PoP {lo.get('pop')} touch {lo.get('touch')}"
        )


def _load_congress_tickers() -> frozenset[str]:
    """Tickers from the latest saved politicians scan report, if any."""
    root = Path(__file__).resolve().parents[2] / "var" / "reports" / "politicians"
    if not root.is_dir():
        return frozenset()
    reports = sorted(root.glob("*.json"), reverse=True)
    if not reports:
        return frozenset()
    try:
        payload = json.loads(reports[0].read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return frozenset()
    out: set[str] = set()
    for trade in payload.get("new_trades") or payload.get("trades") or []:
        sym = str(trade.get("symbol") or trade.get("ticker") or "").strip().upper()
        if sym:
            out.add(sym)
    return frozenset(out)


def _render_position_detail_panels(
    console: Console,
    items: list[dict],
    *,
    title_prefix: str,
    border_style: str = "green",
    max_panels: int = 10,
) -> None:
    from options.position_context import format_position_panel_lines

    for idx, item in enumerate(items[:max_panels], start=1):
        ctx = item.get("position") or item
        ticker = str(ctx.get("ticker") or item.get("ticker") or "?")
        tier = item.get("tier")
        gem_score = item.get("gem_score")
        title = f"{title_prefix} #{idx} {ticker}"
        if gem_score is not None:
            title += f" (gem {gem_score})"
        if tier:
            title += f" [{tier}]"
        console.print(
            Panel(
                "\n".join(format_position_panel_lines(ctx)),
                title=title,
                border_style=border_style,
            )
        )


def _render_gems_sheet(console: Console, gem_payload: dict) -> None:
    gems = gem_payload.get("gems") or []
    watch = gem_payload.get("watchlist") or []
    scan_picks = gem_payload.get("scan_picks") or []

    summary = Table(title="Options daily scan summary", box=box.SIMPLE_HEAVY)
    summary.add_column("Metric")
    summary.add_column("Value")
    summary.add_row("Filter profile", str(gem_payload.get("filter_profile") or "daily"))
    summary.add_row("Gems", str(len(gems)))
    summary.add_row("Watchlist", str(len(watch)))
    summary.add_row("Scan picks", str(len(scan_picks)))
    summary.add_row("Actionable before filter", str(gem_payload.get("actionable_before_filter") or 0))
    console.print(summary)

    if scan_picks:
        pick_table = Table(
            title="Scan picks (executable trades ranked by analyzer)",
            box=box.SIMPLE,
        )
        pick_table.add_column("Ticker")
        pick_table.add_column("Strategy")
        pick_table.add_column("Position")
        pick_table.add_column("Score", justify="right")
        pick_table.add_column("PoP", justify="right")
        pick_table.add_column("EV", justify="right")
        pick_table.add_column("Touch", justify="right")
        for item in scan_picks[:10]:
            ctx = item.get("position") or item
            metrics = ctx.get("metrics") or item
            pick_table.add_row(
                str(ctx.get("ticker") or item.get("ticker")),
                str(ctx.get("strategy") or item.get("strategy") or "-").replace("_", " "),
                str(ctx.get("setup_summary") or item.get("setup_summary") or "-")[:36],
                str(metrics.get("composite_score") or item.get("composite_score") or "-"),
                str(metrics.get("pop") or item.get("pop") or "-"),
                str(metrics.get("expected_value") or item.get("expected_value") or "-"),
                str(metrics.get("probability_of_touch") or item.get("probability_of_touch") or "-"),
            )
        console.print(pick_table)
        _render_position_detail_panels(
            console,
            scan_picks,
            title_prefix="Scan pick",
            border_style="cyan",
        )

    if watch:
        watch_table = Table(title="Watchlist (relaxed gates)", box=box.SIMPLE)
        watch_table.add_column("Ticker")
        watch_table.add_column("Gem", justify="right")
        watch_table.add_column("Position")
        watch_table.add_column("PoP", justify="right")
        for item in watch[:8]:
            ctx = item.get("position") or item
            metrics = ctx.get("metrics") or item.get("metrics") or {}
            watch_table.add_row(
                str(ctx.get("ticker") or item.get("ticker")),
                str(item.get("gem_score")),
                str(ctx.get("setup_summary") or "-")[:36],
                str(metrics.get("pop", "-")),
            )
        console.print(watch_table)
        _render_position_detail_panels(
            console,
            watch,
            title_prefix="Watch",
            border_style="yellow",
            max_panels=5,
        )

    if gems:
        table = Table(title="Hidden gem prospects (structure + X crowd)", box=box.SIMPLE_HEAVY)
        table.add_column("Ticker")
        table.add_column("Gem", justify="right")
        table.add_column("Tier")
        table.add_column("Strategy")
        table.add_column("Position")
        table.add_column("PoP", justify="right")
        table.add_column("Touch", justify="right")
        table.add_column("X", justify="right")
        table.add_column("Why")
        for item in gems:
            ctx = item.get("position") or item
            metrics = ctx.get("metrics") or item.get("metrics") or {}
            why = "; ".join(item.get("reasons") or [])[:60]
            table.add_row(
                str(ctx.get("ticker") or item.get("ticker")),
                str(item.get("gem_score")),
                str(item.get("tier")),
                str(ctx.get("strategy") or item.get("strategy") or "-").replace("_", " "),
                str(ctx.get("setup_summary") or "-")[:32],
                f"{metrics.get('pop', '-')}",
                f"{metrics.get('probability_of_touch', '-')}",
                str(item.get("x_interest_score") or 0),
                why,
            )
        console.print(table)
        _render_position_detail_panels(console, gems, title_prefix="Gem", border_style="green")

    if not gems and not watch and not scan_picks:
        console.print(
            Panel(
                "No setups surfaced. Try a larger universe or:\n"
                "  nave options analyze --sp500-scan --sp500-limit 100 --days-to-exp 30",
                title="No results",
                border_style="yellow",
            )
        )
    x_loaded = gem_payload.get("x_snapshots_loaded", 0)
    if x_loaded == 0:
        console.print(
            "[yellow]No X snapshots in stocks_history/ — run "
            "`nave stocks x-analyze --tickers TICK1,TICK2` on your shortlist.[/yellow]"
        )


def _run_gems_scan(
    *,
    limit: int,
    days_to_exp: int,
    top_gems: int,
    scan_workers: int,
    source: str,
    json_out: bool,
    sheet: bool,
    save_json: bool,
    json_path: str | None,
    with_congress: bool,
    fetch_x: int,
    strict_filters: bool = False,
) -> None:
    """Scan S&P names for executable income setups; rank hidden gems + congress boost."""
    analyzer = _build_options_analyzer(source=source)
    console = Console()
    tickers = (
        list(get_sp500_tickers(limit))
        if limit > len(SP500_TOP_100_TICKERS)
        else list(SP500_TOP_100_TICKERS[:limit])
    )

    scan_payload = _scan_equity_options_universe(
        analyzer=analyzer,
        analyzer_factory=lambda: _build_options_analyzer(source=source),
        tickers=tickers,
        days_to_exp=days_to_exp,
        top_trades=top_gems,
        workers=scan_workers,
    )

    congress = _load_congress_tickers() if with_congress else frozenset()
    filter_profile = "strict" if strict_filters else "daily"
    payload = run_hidden_gems_scan(
        scan_payload,
        congress_tickers=congress,
        top=top_gems,
        fetch_x_for_top=fetch_x,
        filter_profile=filter_profile,
    )
    payload["scan"] = scan_payload
    gem_payload = payload["hidden_gems"]

    report_path: Path | None = None
    if save_json:
        report_path = _resolve_json_report_path(
            analyzer=analyzer,
            ticker=f"hidden_gems_{limit}",
            json_path=json_path,
        )
        payload = dict(payload)
        payload["artifacts"] = {"json_report_path": str(report_path)}
        report_path = _write_json_report(payload=payload, out_path=report_path)

    if json_out:
        typer.echo(json.dumps(payload, indent=2, default=str))
        return

    typer.echo(format_gem_digest(gem_payload))
    typer.echo("")
    if sheet:
        _render_gems_sheet(console, gem_payload)
    else:
        for item in gem_payload.get("gems") or []:
            typer.echo(
                f"  {item['ticker']} score={item['gem_score']} "
                f"{item.get('strategy')} — {', '.join(item.get('reasons') or [])}"
            )
    if report_path is not None:
        typer.echo(f"JSON report: {report_path}")

    try:
        from options.forward_tracker import record_daily_recommendations

        track_result = record_daily_recommendations(payload)
        if not json_out:
            typer.echo(
                f"\n[dim]Forward tracker: {track_result['recommendation_count']} rows → "
                f"{track_result['path']}[/dim]"
            )
    except Exception as exc:
        if not json_out:
            typer.echo(f"[yellow]Forward tracker skipped: {exc}[/yellow]")


@options_app.command("track")
def options_track(
    report: str | None = typer.Option(
        None,
        "--report",
        help="Path to a saved options daily JSON report (record mode)",
    ),
    mark: bool = typer.Option(
        False,
        "--mark",
        help="Mark open recommendations (default: record from --report or latest)",
    ),
    offsets: str = typer.Option("1,3,5,7", "--offsets", help="Days since entry for mark mode"),
    as_of: str | None = typer.Option(None, "--as-of", help="Mark exit date YYYY-MM-DD"),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Record daily scan picks or mark forward PnL vs replay model."""
    from datetime import date, datetime, timezone

    from options.forward_tracker import (
        mark_open_recommendations,
        record_daily_recommendations,
        render_tracker_report,
    )

    if mark:
        offset_list = [int(part.strip()) for part in offsets.split(",") if part.strip()]
        exit_day = (
            date.fromisoformat(as_of)
            if as_of
            else datetime.now(timezone.utc).date()
        )
        summary = mark_open_recommendations(as_of=exit_day, offsets_days=offset_list)
        if json_out:
            typer.echo(json.dumps(summary, indent=2, default=str))
        else:
            typer.echo(render_tracker_report(summary))
        return

    path = Path(report) if report else None
    if path is None:
        reports = sorted(
            _default_reports_dir().glob("hidden_gems_*_options_report_*.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if not reports:
            raise typer.BadParameter("No report found; pass --report or run nave options daily first")
        path = reports[0]
    payload = json.loads(path.read_text(encoding="utf-8"))
    result = record_daily_recommendations(payload)
    if json_out:
        typer.echo(json.dumps(result, indent=2, default=str))
    else:
        typer.echo(f"Recorded {result['recommendation_count']} recommendations → {result['path']}")


@options_app.command("daily")
def options_daily(
    limit: int = typer.Option(
        40,
        "--limit",
        "--sp500-limit",
        min=10,
        max=200,
        help="S&P universe size (default top 40 liquid names)",
    ),
    days_to_exp: int = typer.Option(
        30,
        "--days-to-exp",
        min=1,
        max=365,
        help="Target days to expiration (~30d income setups)",
    ),
    top: int = typer.Option(10, "--top", min=1, max=50, help="Setups to surface"),
    scan_workers: int = typer.Option(4, "--scan-workers", min=1, max=12),
    source: str = typer.Option("yfinance", "--source"),
    json_out: bool = typer.Option(False, "--json"),
    sheet: bool = typer.Option(True, "--sheet/--no-sheet"),
    save_json: bool = typer.Option(True, "--save-json/--no-save-json"),
    json_path: str | None = typer.Option(None, "--json-path"),
    with_congress: bool = typer.Option(
        True,
        "--with-congress/--no-congress",
        help="Boost tickers from latest congressional disclosure report",
    ),
    refresh_congress: bool = typer.Option(
        True,
        "--refresh-congress/--no-refresh-congress",
        help="Run nave congress first (needs FMP_API_KEY) to refresh politician filings",
    ),
    fetch_x: int = typer.Option(
        0,
        "--fetch-x",
        min=0,
        max=12,
        help="Fetch fresh X posts for top N gems (requires twscrape; 0=cache only)",
    ),
    strict_filters: bool = typer.Option(
        False,
        "--strict-filters",
        help="Use replay-tuned strict gem gates instead of daily operator defaults",
    ),
) -> None:
    """Daily equity income scan: congress refresh + ~30d ranked setups for this week."""
    if refresh_congress and with_congress:
        from cli.commands.congress import _run_congress_scan

        status_to_stderr = json_out
        typer.echo("Refreshing congressional disclosures (FMP)...", err=status_to_stderr)
        try:
            report = _run_congress_scan(persist=True, save_report=True)
            new_count = len(report.get("new_trades") or [])
            typer.echo(
                f"Congress scan: {new_count} new filing(s) since last run.",
                err=status_to_stderr,
            )
        except Exception as exc:
            typer.echo(
                f"[yellow]Congress refresh skipped ({exc}). "
                "Set FMP_API_KEY or use --no-refresh-congress.[/yellow]",
                err=status_to_stderr,
            )
        typer.echo("", err=status_to_stderr)

    _run_gems_scan(
        limit=limit,
        days_to_exp=days_to_exp,
        top_gems=top,
        scan_workers=scan_workers,
        source=source,
        json_out=json_out,
        sheet=sheet,
        save_json=save_json,
        json_path=json_path,
        with_congress=with_congress,
        fetch_x=fetch_x,
        strict_filters=strict_filters,
    )


@options_app.command("gems")
def gems(
    limit: int = typer.Option(
        100,
        "--limit",
        "--sp500-limit",
        min=10,
        max=200,
        help="S&P 500 universe size to scan",
    ),
    days_to_exp: int = typer.Option(30, "--days-to-exp", min=1, max=365),
    top_gems: int = typer.Option(15, "--top", min=1, max=50, help="Hidden gems to show"),
    scan_workers: int = typer.Option(4, "--scan-workers", min=1, max=12),
    source: str = typer.Option("yfinance", "--source"),
    json_out: bool = typer.Option(False, "--json"),
    sheet: bool = typer.Option(True, "--sheet/--no-sheet"),
    save_json: bool = typer.Option(True, "--save-json/--no-save-json"),
    json_path: str | None = typer.Option(None, "--json-path"),
    with_congress: bool = typer.Option(
        True,
        "--with-congress/--no-congress",
        help="Boost tickers in latest congressional disclosure report",
    ),
    fetch_x: int = typer.Option(
        0,
        "--fetch-x",
        min=0,
        max=12,
        help="Fetch fresh X posts for top N gems (requires twscrape; 0=cache only)",
    ),
    strict_filters: bool = typer.Option(
        False,
        "--strict-filters",
        help="Use replay-tuned strict gem gates instead of daily operator defaults",
    ),
) -> None:
    """Scan for under-the-radar income setups with strong odds + X crowd interest."""
    _run_gems_scan(
        limit=limit,
        days_to_exp=days_to_exp,
        top_gems=top_gems,
        scan_workers=scan_workers,
        source=source,
        json_out=json_out,
        sheet=sheet,
        save_json=save_json,
        json_path=json_path,
        with_congress=with_congress,
        fetch_x=fetch_x,
        strict_filters=strict_filters,
    )


@options_app.command("eth-weekly")
def eth_weekly(
    days_to_exp: int = typer.Option(
        10,
        "--days-to-exp",
        min=5,
        max=21,
        help="Target DTE for the ETH weekly options expression.",
    ),
    tf: str = typer.Option(
        "4h,1h",
        "--tf",
        help="Momentum setup/trigger timeframe pair.",
    ),
    score_threshold: int = typer.Option(
        90,
        "--score-threshold",
        min=1,
        max=100,
        help="Minimum momentum score threshold for weekly options.",
    ),
    account_equity: float = typer.Option(
        1000.0,
        "--account-equity",
        min=1.0,
        help="Account equity used for the small-account risk guard.",
    ),
    risk_pct: float = typer.Option(
        0.01,
        "--risk-pct",
        min=0.001,
        max=0.02,
        help="Risk percentage passed to momentum filtering.",
    ),
    max_loss: float = typer.Option(
        20.0,
        "--max-loss",
        min=1.0,
        help="Maximum allowed option max loss in USD.",
    ),
    max_a_plus_loss: float = typer.Option(
        30.0,
        "--max-a-plus-loss",
        min=1.0,
        help="Manual review ceiling for exceptional setups.",
    ),
    min_confidence: int = typer.Option(
        90,
        "--min-confidence",
        min=1,
        max=100,
        help="Minimum ETH momentum confidence required for ENTER.",
    ),
    source: str = typer.Option(
        "deribit",
        "--source",
        help="Option chain source. Use deribit for ETH execution checks.",
    ),
    json_out: bool = typer.Option(
        False,
        "--json",
        help="Emit machine-stable JSON output.",
    ),
    save_json: bool = typer.Option(
        True,
        "--save-json/--no-save-json",
        help="Persist the ETH weekly decision payload to a JSON report.",
    ),
    json_path: str | None = typer.Option(
        None,
        "--json-path",
        help="Optional output path for the saved JSON report.",
    ),
) -> None:
    """ETH weekly options decision: COT + momentum + Deribit options + risk guard."""
    analyzer = _build_options_analyzer(source=source)
    try:
        scan_payload = analyzer.scan_crypto_opportunities(
            coins=["ETH"],
            days_to_exp=days_to_exp,
            tf=tf,
            account_equity=account_equity,
            risk_pct=risk_pct,
            score_threshold=score_threshold,
            require_tradeable=True,
        )
    except OptionsError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    profile = EthWeeklyOptionsProfile(
        account_equity=account_equity,
        max_loss_usd=max_loss,
        max_a_plus_loss_usd=max_a_plus_loss,
        min_confidence=min_confidence,
    )
    decision = build_eth_weekly_decision(scan_payload, profile=profile)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "command": "nave options eth-weekly",
        "scan": scan_payload,
        "decision": decision,
    }

    report_path: Path | None = None
    if save_json:
        report_path = _resolve_json_report_path(
            analyzer=analyzer,
            ticker="ETH_weekly_options",
            json_path=json_path,
        )
        payload["artifacts"] = {"json_report_path": str(report_path)}
        _write_json_report(payload=payload, out_path=report_path)

    if json_out:
        typer.echo(json.dumps(payload, indent=2, default=str))
        return

    _render_eth_weekly_decision(decision)
    if report_path is not None:
        typer.echo(f"JSON report: {report_path}")


@options_app.command("opportunities")
def opportunities(
    coins: str = typer.Option(
        "BTC,ETH",
        "--coins",
        help="Comma-separated coin list (currently supports BTC,ETH)",
    ),
    days_to_exp: int = typer.Option(
        30,
        "--days-to-exp",
        min=1,
        max=365,
        help="Target days to expiration",
    ),
    tf: str = typer.Option(
        "4h,1h",
        "--tf",
        help="Momentum setup/trigger timeframe pair (e.g. 4h,1h)",
    ),
    score_threshold: int = typer.Option(
        75,
        "--score-threshold",
        min=1,
        max=100,
        help="Minimum momentum score threshold",
    ),
    account_equity: float = typer.Option(
        10000.0,
        "--account-equity",
        min=1.0,
        help="Account equity context used by momentum sizing",
    ),
    risk_pct: float = typer.Option(
        0.005,
        "--risk-pct",
        min=0.001,
        max=0.02,
        help="Risk percentage passed to momentum filtering",
    ),
    require_tradeable: bool = typer.Option(
        True,
        "--require-tradeable/--allow-watchlist",
        help="Only run options analysis for momentum-tradeable setups",
    ),
    json_out: bool = typer.Option(
        False,
        "--json",
        help="Emit machine-stable JSON output",
    ),
    sheet: bool = typer.Option(
        False,
        "--sheet",
        help="Render report as Rich terminal tables (human-readable).",
    ),
    source: str = typer.Option(
        "yfinance",
        "--source",
        help="Data source for option chains (yfinance|deribit)",
    ),
) -> None:
    """Scan BTC/ETH options opportunities using momentum as an upstream filter."""
    analyzer = _build_options_analyzer(source=source)
    console = Console()

    try:
        payload = analyzer.scan_crypto_opportunities(
            coins=_parse_coin_list(coins),
            days_to_exp=days_to_exp,
            tf=tf,
            account_equity=account_equity,
            risk_pct=risk_pct,
            score_threshold=score_threshold,
            require_tradeable=require_tradeable,
        )
    except OptionsError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    if json_out and not sheet:
        typer.echo(json.dumps(payload, indent=2, default=str))
        return

    if sheet:
        _render_opportunities_sheet(console, payload)
        return

    summary = payload.get("summary") or {}
    typer.echo("Options opportunities")
    typer.echo(f"- coins_requested={summary.get('coins_requested')}")
    typer.echo(f"- momentum_allowed={summary.get('momentum_allowed')}")
    typer.echo(f"- options_ready={summary.get('options_ready')}")
    for coin, entry in sorted((payload.get("opportunities") or {}).items()):
        status = entry.get("status")
        strategy = entry.get("top_strategy")
        typer.echo(f"- {coin}: status={status} top_strategy={strategy}")
