"""Stocks CLI command group — ISM-driven equity workflow.

Subcommands:
    nave stocks ism-scan            Fetch and render the latest ISM report.
    nave stocks screen              Run the PE-vs-sector + EPS-growth screener.
    nave stocks journal-stats       Print stock-only journal stats.
    nave stocks politicians-scan    Same as nave congress (Congressional trades).
"""

from __future__ import annotations

import json as _json
import logging
from pathlib import Path
from typing import Optional, cast

import typer
from rich.console import Console
from rich.table import Table

from cli.professional_typer import ProfessionalTyper
from core.logger import configure_logger
from trading.brokers import AlpacaBroker, OndoBroker
from trading.stocks import (
    DEFAULT_UNIVERSE,
    ISMReportFetcher,
    ISMSectorStrategy,
    ISMShortPerpStrategy,
    MassiveClient,
    StockJournal,
    build_ism_industry_report,
    build_ism_equity_pipeline,
    render_ism_report_markdown_v2,
    render_x_summary_markdown_v2,
)
from trading.stocks.ism_calendar import (
    CalendarKind,
    ISMCalendarError,
    fetch_ism_calendar,
    load_calendar,
    next_release,
)
from trading.stocks.portfolio_manager import (
    Candidate,
    Evidence,
    PortfolioPolicy,
    allocate_monthly_budget,
    rank_candidates,
)
from trading.stocks.short_backtest import ISMShortBacktester
from trading.stocks.social_analyzer import (
    analyze_tickers,
    render_sheet as render_x_sheet,
)
from trading.stocks.x_client import (
    DEFAULT_LIMIT_PER_TICKER,
    DEFAULT_LOOKBACK_DAYS,
)
from research.shorts import StockShortResearchWorkflow

logger = configure_logger(__name__, level=logging.INFO)

stocks_app = ProfessionalTyper(
    help="ISM-driven stock trading workflow (Alpaca + Ondo stubs).")

ism_calendar_app = ProfessionalTyper(
    help="Internal ISM release calendar (sourced from FMP)."
)
stocks_app.add_typer(ism_calendar_app, name="ism-calendar")
short_research_app = ProfessionalTyper(help="Read-only multi-factor stock-short research")
stocks_app.add_typer(short_research_app, name="short")


def _load_short_research_rows(input_file: Optional[Path]) -> list[dict]:
    if input_file is None:
        return []
    if not input_file.exists() or not input_file.is_file():
        raise typer.BadParameter(f"input file does not exist: {input_file}", param_hint="--input-file")
    payload = _json.loads(input_file.read_text(encoding="utf-8"))
    if isinstance(payload, dict):
        payload = payload.get("snapshots", payload.get("outcomes", []))
    if not isinstance(payload, list) or not all(isinstance(item, dict) for item in payload):
        raise typer.BadParameter("input file must contain a JSON list of objects", param_hint="--input-file")
    return payload


def _emit_short_research_result(result, *, json_out: bool, output: Optional[Path]) -> None:
    rendered = result.to_json() if json_out else result.to_markdown()
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered, encoding="utf-8")
        typer.echo(str(output))
    else:
        typer.echo(rendered, nl=False)


@short_research_app.command("scan")
def short_research_scan(
    input_file: Optional[Path] = typer.Option(None, "--input-file", help="JSON point-in-time stock snapshots."),
    decision_time: Optional[str] = typer.Option(None, "--decision-time", help="Timezone-aware ISO timestamp."),
    json_out: bool = typer.Option(False, "--json", help="Emit structured JSON."),
    output: Optional[Path] = typer.Option(None, "--output", help="Optional report output path."),
    persist: bool = typer.Option(True, "--persist/--no-persist", help="Persist the research result under NAVE state."),
) -> None:
    """Scan stock-short factors; this command never places or sizes a trade."""
    result = StockShortResearchWorkflow().scan(
        _load_short_research_rows(input_file),
        decision_time=decision_time,
        persist=persist,
    )
    _emit_short_research_result(result, json_out=json_out, output=output)


@stocks_app.command("portfolio-review")
def portfolio_review(
    candidates_json: str = typer.Option(
        ...,
        "--candidates-json",
        help="JSON array of normalised candidates and evidence from upstream adapters.",
    ),
    monthly_budget: float = typer.Option(300.0, "--monthly-budget", min=0.0),
    open_tickers: Optional[str] = typer.Option(
        None,
        "--open-tickers",
        help="JSON array of tickers already held; used to enforce max_positions.",
    ),
    json_out: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """Build a human-gated monthly portfolio review; never places orders."""
    try:
        raw_candidates = _json.loads(candidates_json)
        if not isinstance(raw_candidates, list):
            raise TypeError("candidates JSON must be an array")
        candidates = [
            Candidate(
                ticker=str(item["ticker"]),
                evidence=Evidence(**dict(item.get("evidence", {}))),
                price=item.get("price"),
                entry_zone=tuple(item["entry_zone"]) if item.get("entry_zone") else None,
                invalidation=item.get("invalidation"),
                direct_defense=bool(item.get("direct_defense", False)),
            )
            for item in raw_candidates
        ]
        held: list[str] = []
        if open_tickers:
            parsed_open = _json.loads(open_tickers)
            if not isinstance(parsed_open, list):
                raise TypeError("open tickers JSON must be an array")
            held = [str(ticker) for ticker in parsed_open]
    except (KeyError, TypeError, ValueError, _json.JSONDecodeError) as exc:
        raise typer.BadParameter(f"invalid portfolio-review input: {exc}") from exc

    policy = PortfolioPolicy(monthly_budget=monthly_budget)
    ranked = rank_candidates(candidates, policy=policy)
    allocations = allocate_monthly_budget(ranked, policy=policy, open_tickers=held)
    payload = {
        "mode": "human_gated_dry_run",
        "policy": {"monthly_budget": policy.monthly_budget,
                   "reserve_cash_weight": policy.reserve_cash_weight},
        "ranked": [decision.as_dict() for decision in ranked],
        "allocations": [decision.as_dict() for decision in allocations],
    }
    if json_out:
        typer.echo(_json.dumps(payload, indent=2))
        return
    typer.echo("NAVE PORTFOLIO REVIEW — human-gated dry-run")
    allocated = {decision.ticker: decision.allocation_usd for decision in allocations}
    for decision in ranked:
        amount = allocated.get(decision.ticker)
        allocation = f" → ${amount:.2f}" if amount else ""
        typer.echo(f"{decision.action.value.upper():7} {decision.ticker:6} "
                   f"score={decision.score:.2f}{allocation} "
                   f"[{', '.join(decision.reason_codes) or 'no flags'}]")
    typer.echo("No orders were placed.")


def _resolve_universe(universe_json: Optional[str]) -> dict[str, list[str]]:
    if not universe_json:
        return DEFAULT_UNIVERSE
    try:
        parsed = _json.loads(universe_json)
    except _json.JSONDecodeError as exc:
        raise typer.BadParameter(
            f"--universe-json is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise typer.BadParameter(
            "--universe-json must be an object mapping sector → [tickers]")
    return {str(k): [str(t) for t in v] for k, v in parsed.items()}


def _format_plan_leg(value: object) -> str:
    if isinstance(value, dict):
        price = value.get("price")
        rule = value.get("rule")
        if price is None:
            return str(rule or "?")
        return f"{price} ({rule or '?'})"
    return str(value or "?")


@stocks_app.command("ism-scan")
def ism_scan(
    kind: str = typer.Option(
        "manufacturing",
        "--kind",
        help="Report flavour: manufacturing or services",
    ),
    url: Optional[str] = typer.Option(
        None,
        "--url",
        help="Override source URL (useful for fixtures / alternative mirrors).",
    ),
    use_playwright: bool = typer.Option(
        False,
        "--playwright/--no-playwright",
        help="Use the Playwright fallback instead of httpx+BS4.",
    ),
    json_out: bool = typer.Option(
        False, "--json", help="Emit JSON instead of a table."),
) -> None:
    """Fetch the latest ISM Report On Business® and print the ranking."""
    if kind not in {"manufacturing", "services"}:
        raise typer.BadParameter("--kind must be manufacturing or services")

    fetcher = ISMReportFetcher(use_playwright=use_playwright)
    report = fetcher.fetch_report(kind, url=url)  # type: ignore[arg-type]

    if json_out:
        payload = {
            "kind": report.kind,
            "report_month": report.report_month,
            "pmi": report.pmi,
            "source_url": report.source_url,
            "expanding": [
                {"rank": r.rank, "industry": r.industry,
                    "gics_sector": r.gics_sector}
                for r in report.expanding
            ],
            "contracting": [
                {"rank": r.rank, "industry": r.industry,
                    "gics_sector": r.gics_sector}
                for r in report.contracting
            ],
        }
        typer.echo(_json.dumps(payload, indent=2))
        return

    typer.echo(f"ISM {report.kind.capitalize()} — {report.report_month}")
    if report.pmi is not None:
        typer.echo(f"Headline PMI: {report.pmi}")
    typer.echo(f"Source: {report.source_url}")
    typer.echo()
    typer.echo(f"Expanding ({len(report.expanding)}):")
    for r in report.expanding:
        sector = r.gics_sector or "?"
        typer.echo(f"  {r.rank:>2}. {r.industry}  →  {sector}")
    typer.echo()
    typer.echo(f"Contracting ({len(report.contracting)}):")
    for r in report.contracting:
        sector = r.gics_sector or "?"
        typer.echo(f"  {r.rank:>2}. {r.industry}  →  {sector}")


@stocks_app.command("screen")
def screen(
    kind: str = typer.Option("manufacturing", "--kind",
                             help="ISM report flavour"),
    mode: Optional[str] = typer.Option(
        None,
        "--mode",
        help=(
            "Screening strategy: manufacturing (EPS-growth ranking) or "
            "services (long-term revenue growth + PE-relative filter). "
            "Defaults to --kind."
        ),
    ),
    top_n: int = typer.Option(
        5, "--top-n", help="Return the top N candidates"),
    capital: float = typer.Option(
        10000.0, "--capital", help="Total USD to equal-weight across picks"),
    min_eps_growth: Optional[float] = typer.Option(
        None,
        "--min-eps-growth",
        help="Optional EPS-growth filter in percent (next-year estimate).",
    ),
    min_confidence: float = typer.Option(
        0.3,
        "--min-confidence",
        help="Minimum final confidence score (0-1) for screen candidates.",
    ),
    universe_json: Optional[str] = typer.Option(
        None,
        "--universe-json",
        help="Override sector → tickers mapping as a JSON string.",
    ),
    json_out: bool = typer.Option(
        False, "--json", help="Emit JSON plan instead of table."),
    dry_run: bool = typer.Option(
        True, "--dry-run/--live", help="Default dry-run. Broker is stubbed."),
) -> None:
    """Run the full ISM → fundamentals screener and show the proposed plan."""
    if kind not in {"manufacturing", "services"}:
        raise typer.BadParameter("--kind must be manufacturing or services")
    effective_mode = mode or kind
    if effective_mode not in {"manufacturing", "services"}:
        raise typer.BadParameter("--mode must be manufacturing or services")

    universe = _resolve_universe(universe_json)
    massive = MassiveClient()
    broker = AlpacaBroker()
    strategy = ISMSectorStrategy(
        broker=broker,
        massive=massive,
        universe=universe,
        report_kind=kind,  # type: ignore[arg-type]
        mode=effective_mode,  # type: ignore[arg-type]
        capital_usd=capital,
        max_positions=top_n,
        min_eps_growth_next_year=min_eps_growth,
        min_confidence=min_confidence,
        dry_run=dry_run,
    )
    summary = strategy.run_once()

    if json_out:
        payload = {
            "strategy": summary["strategy"],
            "broker": summary["broker"],
            "dry_run": summary["dry_run"],
            "plan": [p.as_dict() for p in summary["plan"]],
            "result": summary["result"],
        }
        typer.echo(_json.dumps(payload, indent=2, default=str))
        return

    typer.echo(
        f"{summary['strategy']} via {summary['broker']}  "
        f"(dry_run={summary['dry_run']})"
    )
    if not summary["plan"]:
        typer.echo("No candidates — check --universe-json or widen the screener.")
        return
    for item in summary["plan"]:
        typer.echo(
            f"  long {item.symbol:<6}  ~${item.size_usd:>9,.2f}  "
            f"[{item.sector}] score={item.score:+.3f}  {item.reason}"
        )


@stocks_app.command("screen-shorts")
def screen_shorts(
    kind: str = typer.Option("manufacturing", "--kind", help="ISM report flavour"),
    mode: Optional[str] = typer.Option(
        None,
        "--mode",
        help="Screening strategy; defaults to --kind.",
    ),
    top_n: int = typer.Option(5, "--top-n", help="Return the top N Ondo short candidates"),
    capital: float = typer.Option(10000.0, "--capital", help="USD to equal-weight across shorts"),
    min_eps_growth: Optional[float] = typer.Option(None, "--min-eps-growth"),
    min_confidence: float = typer.Option(0.3, "--min-confidence"),
    min_short_score: Optional[float] = typer.Option(
        None,
        "--min-short-score",
        help="Require short score above this floor; defaults to 0.05 in normal mode.",
    ),
    research_mode: bool = typer.Option(
        False,
        "--research-mode",
        help="Allow explicit relaxed short-score research thresholds.",
    ),
    universe_json: Optional[str] = typer.Option(None, "--universe-json"),
    json_out: bool = typer.Option(False, "--json"),
    dry_run: bool = typer.Option(True, "--dry-run/--live"),
) -> None:
    """Screen ISM contracting sectors for Ondo stock-perp short candidates."""
    if kind not in {"manufacturing", "services"}:
        raise typer.BadParameter("--kind must be manufacturing or services")
    effective_mode = mode or kind
    if effective_mode not in {"manufacturing", "services"}:
        raise typer.BadParameter("--mode must be manufacturing or services")
    universe = _resolve_universe(universe_json)
    strategy = ISMShortPerpStrategy(
        OndoBroker(),
        massive=MassiveClient(),
        universe=universe,
        report_kind=kind,  # type: ignore[arg-type]
        mode=effective_mode,  # type: ignore[arg-type]
        capital_usd=capital,
        max_positions=top_n,
        min_eps_growth_next_year=min_eps_growth,
        min_confidence=min_confidence,
        min_short_score=min_short_score,
        research_mode=research_mode,
        dry_run=dry_run,
    )
    summary = strategy.run_once()
    if json_out:
        payload = {
            "strategy": summary["strategy"],
            "broker": summary["broker"],
            "venue": "ondo_stock_perp",
            "dry_run": summary["dry_run"],
            "plan": [p.as_dict() for p in summary["plan"]],
            "result": summary["result"],
        }
        typer.echo(_json.dumps(payload, indent=2, default=str))
        return

    typer.echo(
        f"{summary['strategy']} via {summary['broker']} / ondo_stock_perp "
        f"(dry_run={summary['dry_run']})"
    )
    if not summary["plan"]:
        typer.echo("No Ondo-eligible short candidates.")
        return
    for item in summary["plan"]:
        typer.echo(
            f"  short {item.symbol:<6}  ~${item.size_usd:>9,.2f}  "
            f"[{item.sector}] score={item.score:+.3f}  {item.reason}"
        )
        plan = item.as_dict().get("trade_plan")
        if isinstance(plan, dict):
            typer.echo(
                "      plan: "
                f"entry={plan.get('entry_rule')} | "
                f"entry_px={plan.get('entry_price')} | "
                f"target={_format_plan_leg(plan.get('target'))} | "
                f"stop={_format_plan_leg(plan.get('stop'))} | "
                f"hold={plan.get('holding_window_days')}d | "
                f"risk={plan.get('risk_pct')} | "
                f"max_lev={plan.get('max_leverage')}"
            )


@stocks_app.command("ism-short-backtest")
def ism_short_backtest(
    snapshot_dir: str = typer.Option("stocks_history", "--snapshot-dir"),
    kinds: str = typer.Option("manufacturing,services", "--kinds"),
    from_month: Optional[str] = typer.Option(None, "--from"),
    to_month: Optional[str] = typer.Option(None, "--to"),
    min_confidence: float = typer.Option(0.3, "--min-confidence"),
    min_short_score: Optional[float] = typer.Option(
        None,
        "--min-short-score",
        help="Require short score above this floor; defaults to 0.05 in normal mode.",
    ),
    research_mode: bool = typer.Option(
        False,
        "--research-mode",
        help="Allow explicit relaxed short-score research thresholds.",
    ),
    latest_months: int = typer.Option(
        6,
        "--latest-months",
        help="Use the latest N snapshot months when --from is omitted.",
    ),
    all_months: bool = typer.Option(
        False,
        "--all",
        help="Disable the latest-months window and use all matching snapshots.",
    ),
    include_non_ondo: bool = typer.Option(False, "--include-non-ondo"),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Backtest ISM short candidates over stored monthly snapshots."""
    selected_kinds = [part.strip() for part in kinds.split(",") if part.strip()]
    backtester = ISMShortBacktester()
    payload = backtester.evaluate(
        snapshot_dir=snapshot_dir,
        kinds=selected_kinds,
        from_month=from_month,
        to_month=to_month,
        min_confidence=min_confidence,
        min_short_score=min_short_score,
        research_mode=research_mode,
        latest_months=None if all_months else latest_months,
        ondo_only=not include_non_ondo,
    )
    if json_out:
        typer.echo(_json.dumps(payload, indent=2))
        return

    summary = payload["summary"]
    typer.echo("ISM Ondo short backtest")
    lookback = payload.get("lookback") or {}
    typer.echo(
        "Window: "
        f"{lookback.get('from_month')} -> {lookback.get('to_month')} "
        f"(latest_months={lookback.get('latest_months')})"
    )
    typer.echo(f"Snapshots used: {len(payload['snapshots_used'])}")
    typer.echo(
        f"Trades: {summary['trade_count']} | Win rate: {summary['win_rate']:.1%} | "
        f"Avg return: {summary['avg_return_pct']:.2f}%"
    )
    for kind_name, stats in payload.get("by_kind", {}).items():
        typer.echo(
            f"  {kind_name}: {stats['trade_count']} trades, "
            f"avg {stats['avg_return_pct']:.2f}%"
        )


@stocks_app.command("ism-report")
def ism_report(
    kind: str = typer.Option("manufacturing", "--kind",
                             help="ISM report flavour"),
    mode: Optional[str] = typer.Option(
        None,
        "--mode",
        help=(
            "Screening strategy: manufacturing (EPS-growth) or services "
            "(long-term revenue growth + PE-relative). Defaults to --kind."
        ),
    ),
    top_n: int = typer.Option(
        10, "--top-n", help="Top N stocks per ISM side bucket (long/short)"),
    min_eps_growth: Optional[float] = typer.Option(
        None,
        "--min-eps-growth",
        help="Optional EPS-growth filter in percent (next-year estimate).",
    ),
    min_confidence: float = typer.Option(
        0.3,
        "--min-confidence",
        help="Minimum final confidence score (0-1) for report candidates.",
    ),
    min_short_score: Optional[float] = typer.Option(
        None,
        "--min-short-score",
        help="Require short score above this floor; defaults to 0.05 in normal mode.",
    ),
    research_mode: bool = typer.Option(
        False,
        "--research-mode",
        help="Allow explicit relaxed short-score research thresholds.",
    ),
    universe_json: Optional[str] = typer.Option(
        None,
        "--universe-json",
        help="Override sector → tickers mapping as a JSON string.",
    ),
    json_out: bool = typer.Option(False, "--json", help="Emit JSON report."),
    strict_current_month: bool = typer.Option(
        False,
        "--strict-current-month",
        help=(
            "Fail with exit code 2 when report_month does not match the latest "
            "expected covers_month from stored ISM calendar."
        ),
    ),
    sheet: bool = typer.Option(
        False,
        "--sheet",
        help="Render report as Rich terminal tables (human-readable).",
    ),
    telegram_markdown_v2: bool = typer.Option(
        False,
        "--telegram-markdown-v2",
        help="Render Telegram-friendly MarkdownV2 digest (chunked).",
    ),
    save_snapshot: bool = typer.Option(
        True,
        "--save-snapshot/--no-save-snapshot",
        help="Save monthly ISM rankings + screened companies to stocks_history/ (repo-committed) as JSON.",
    ),
    snapshot_dir: Optional[str] = typer.Option(
        None,
        "--snapshot-dir",
        help="Optional output directory for monthly ISM snapshot JSON files.",
    ),
) -> None:
    """Build ISM hottest/worst industry report and filtered stock candidates."""
    if kind not in {"manufacturing", "services"}:
        raise typer.BadParameter("--kind must be manufacturing or services")
    effective_mode = mode or kind
    if effective_mode not in {"manufacturing", "services"}:
        raise typer.BadParameter("--mode must be manufacturing or services")

    payload = build_ism_industry_report(
        kind=kind,
        mode=effective_mode,
        top_n=top_n,
        min_eps_growth_next_year=min_eps_growth,
        min_confidence=min_confidence,
        min_short_score=min_short_score,
        research_mode=research_mode,
        universe=_resolve_universe(universe_json),
        persist_snapshot=save_snapshot,
        snapshot_dir=snapshot_dir,
    )

    freshness_status = str(payload.get("freshness_status") or "unknown")
    if strict_current_month and freshness_status == "stale":
        if json_out and not sheet:
            typer.echo(_json.dumps(payload, indent=2, default=str))
        typer.echo(
            "Report month is stale against stored ISM calendar. "
            f"report_month_key={payload.get('report_month_key')} "
            f"expected={payload.get('expected_covers_month')}",
            err=True,
        )
        raise typer.Exit(
            code=2,
        )

    if json_out and not sheet:
        typer.echo(_json.dumps(payload, indent=2, default=str))
        return

    if telegram_markdown_v2:
        messages = render_ism_report_markdown_v2(payload)
        for idx, message in enumerate(messages, start=1):
            if idx > 1:
                typer.echo("\n---\n")
            typer.echo(message)
        return

    if sheet:
        _render_ism_report_sheet(payload)
        return

    typer.echo(
        f"ISM {payload['kind'].capitalize()} — {payload['report_month']}")
    if payload.get("pmi") is not None:
        typer.echo(f"Headline PMI: {payload['pmi']}")
    typer.echo(
        "Criteria: "
        f"top_n={payload['criteria']['top_n']}, "
        f"min_eps_growth={payload['criteria']['min_eps_growth_next_year']}, "
        f"min_conf={payload['criteria']['min_confidence']}"
    )
    if payload.get("expected_covers_month"):
        typer.echo(
            "Freshness: "
            f"status={payload.get('freshness_status')} "
            f"report_month_key={payload.get('report_month_key')} "
            f"expected={payload.get('expected_covers_month')}"
        )
    if payload.get("saved_to"):
        typer.echo(f"Snapshot saved: {payload['saved_to']}")
    typer.echo()

    hottest = payload["hottest_industries"][:5]
    worst = payload["worst_industries"][:5]
    typer.echo("Hottest industries (ISM expanding):")
    for item in hottest:
        typer.echo(
            f"  {item['rank']:>2}. {item['industry']}  ->  {item['gics_sector'] or '?'}")
    typer.echo()
    typer.echo("Worst industries (ISM contracting):")
    for item in worst:
        typer.echo(
            f"  {item['rank']:>2}. {item['industry']}  ->  {item['gics_sector'] or '?'}")
    typer.echo()

    short_thesis = payload.get("short_thesis")
    if isinstance(short_thesis, dict):
        typer.echo("Short thesis (Ondo stock perps):")
        typer.echo(f"  Venue: {short_thesis.get('venue')}")
        typer.echo(
            f"  Ondo tradeable: {short_thesis.get('ondo_tradeable_count')} / "
            f"{short_thesis.get('short_candidates', payload['summary'].get('short_candidates'))}"
        )
        for sector in short_thesis.get("top_bad_sectors") or []:
            industries = ", ".join(sector.get("driver_industries") or [])
            typer.echo(
                f"  - {sector.get('sector')}: {industries or 'n/a'}"
            )
        typer.echo()

    for label, key in (
        ("Top longs (hottest sectors)", "longs"),
        ("Top shorts (worst sectors)", "shorts"),
        ("Ondo-shortable (worst sectors)", "ondo_shorts"),
    ):
        typer.echo(f"{label}:")
        rows = payload["candidates"].get(key) or payload["candidates"][
            "expanding" if key == "longs" else "contracting"
        ]
        if not rows:
            typer.echo("  (none)")
            continue
        for row in rows:
            industry = row.get("industry") or "?"
            driver_industry = row.get("driver_industry") or "?"
            momentum = row.get("industry_momentum") or "?"
            side = row.get("side") or ("long" if key == "longs" else "short")
            venue = row.get("venue") or "—"
            typer.echo(
                f"  {row['symbol']:<6} [{row['sector']}] company={industry}  driver={driver_industry}  "
                f"{side.upper()} momentum={momentum}  venue={venue}  "
                f"conf={row.get('confidence', '?')}  score={row['score']:+.3f}  "
                f"EPS(next)={row['eps_growth_next_year']}% "
                f"src={row.get('eps_growth_source') or '?'}"
            )
            plan = row.get("trade_plan")
            if isinstance(plan, dict) and side == "short":
                typer.echo(
                    "      plan: "
                    f"entry={plan.get('entry_rule')} | "
                    f"target={_format_plan_leg(plan.get('target'))} | "
                    f"stop={_format_plan_leg(plan.get('stop'))} | "
                    f"hold={plan.get('holding_window_days')}d | "
                    f"risk={plan.get('risk_pct')} | "
                    f"max_lev={plan.get('max_leverage')}"
                )


def _render_ism_report_sheet(payload: dict[str, object]) -> None:
    console = Console()
    criteria = payload.get("criteria") if isinstance(payload, dict) else {}
    if not isinstance(criteria, dict):
        criteria = {}

    console.print(
        f"ISM {(payload.get('kind') or '').__str__().capitalize()} — {payload.get('report_month') or '?'}"
    )
    if payload.get("mode"):
        console.print(f"Mode: {payload.get('mode')}")
    if payload.get("pmi") is not None:
        console.print(f"Headline PMI: {payload.get('pmi')}")
    console.print(
        "Criteria: "
        f"mode={criteria.get('mode')}, "
        f"top_n={criteria.get('top_n')}, "
        f"min_eps_growth={criteria.get('min_eps_growth_next_year')}, "
        f"min_conf={criteria.get('min_confidence')}"
    )
    saved_to = payload.get("saved_to")
    if saved_to:
        console.print(f"Snapshot saved: {saved_to}")
    console.print("")

    hottest = payload.get("hottest_industries")
    if isinstance(hottest, list):
        table = Table(title="Hottest industries (ISM expanding)")
        table.add_column("Rank", justify="right")
        table.add_column("Industry")
        table.add_column("GICS sector")
        for item in hottest[:10]:
            if not isinstance(item, dict):
                continue
            table.add_row(
                str(item.get("rank", "?")),
                str(item.get("industry", "?")),
                str(item.get("gics_sector") or "?"),
            )
        console.print(table)

    worst = payload.get("worst_industries")
    if isinstance(worst, list):
        table = Table(title="Worst industries (ISM contracting)")
        table.add_column("Rank", justify="right")
        table.add_column("Industry")
        table.add_column("GICS sector")
        for item in worst[:10]:
            if not isinstance(item, dict):
                continue
            table.add_row(
                str(item.get("rank", "?")),
                str(item.get("industry", "?")),
                str(item.get("gics_sector") or "?"),
            )
        console.print(table)

    candidates = payload.get("candidates")
    if not isinstance(candidates, dict):
        return

    for key, title in (
        ("longs", "Top longs (hottest sectors)"),
        ("shorts", "Top shorts (worst sectors)"),
    ):
        rows = candidates.get(key)
        if rows is None:
            rows = candidates.get("expanding" if key ==
                                  "longs" else "contracting")
        table = Table(title=title)
        table.add_column("Symbol")
        table.add_column("Name")
        table.add_column("Side")
        table.add_column("Sector")
        table.add_column("Industry")
        table.add_column("Driver")
        table.add_column("Momentum")
        table.add_column("Source")
        table.add_column("Confidence", justify="right")
        table.add_column("Score", justify="right")
        table.add_column("EPS next %", justify="right")
        table.add_column("EPS src")
        # Services-mode extras — filled with "?" when data is absent.
        table.add_column("Rev LT %", justify="right")
        table.add_column("Rev src")
        if isinstance(rows, list) and rows:
            for row in rows:
                if not isinstance(row, dict):
                    continue
                table.add_row(
                    str(row.get("symbol") or "?"),
                    str(row.get("company_name") or ""),
                    str(row.get("side") or ("long" if key == "longs" else "short")),
                    str(row.get("sector") or "?"),
                    str(row.get("industry") or "?"),
                    str(row.get("driver_industry") or "?"),
                    str(row.get("industry_momentum") or "?"),
                    str(row.get("industry_source") or "?"),
                    str(row.get("confidence") if row.get(
                        "confidence") is not None else "?"),
                    str(row.get("score") if row.get(
                        "score") is not None else "?"),
                    str(
                        row.get("eps_growth_next_year")
                        if row.get("eps_growth_next_year") is not None
                        else "?"
                    ),
                    str(row.get("eps_growth_source") or "?"),
                    str(
                        row.get("revenue_growth_long_term")
                        if row.get("revenue_growth_long_term") is not None
                        else "?"
                    ),
                    str(row.get("revenue_growth_source") or "?"),
                )
        else:
            table.add_row(
                "(none)", "", "", "", "", "", "", "", "", "", "", "", "", "",
            )
        console.print(table)


@stocks_app.command("ism-equity-pipeline")
def ism_equity_pipeline(
    manufacturing_json: str = typer.Option(
        "/home/david/quant-portfolio-manager/ism_manufacturing.json",
        "--manufacturing-json",
        help="Stored Manufacturing ISM report JSON.",
    ),
    services_json: str = typer.Option(
        "/home/david/quant-portfolio-manager/ism_services.json",
        "--services-json",
        help="Stored Services ISM report JSON.",
    ),
    research_json: Optional[str] = typer.Option(
        None,
        "--research-json",
        help="Completed per-symbol company research JSON; omit to emit RESEARCHING.",
    ),
    additional_json: Optional[str] = typer.Option(
        None,
        "--additional-json",
        help="JSON list of existing holdings/watches with explicit ISM evidence.",
    ),
    portfolio_symbols: str = typer.Option("", "--portfolio-symbols"),
    watch_symbols: str = typer.Option("", "--watch-symbols"),
    limit: int = typer.Option(6, "--limit", min=1, max=20),
    output: Optional[str] = typer.Option(
        "/home/david/quant-portfolio-manager/ism_equity_pipeline.json",
        "--output",
        help="Durable pipeline artifact path; use --output='' to disable.",
    ),
    json_out: bool = typer.Option(True, "--json/--no-json"),
) -> None:
    """Run both ISM reports through the bounded equity research funnel."""

    def load_payload(path: str) -> dict[str, object]:
        raw = Path(path).read_text(encoding="utf-8")
        # Some repo-local data loaders write a diagnostic line before JSON.
        start = raw.find("{")
        if start < 0:
            raise typer.BadParameter(f"{path} does not contain a JSON object")
        value = _json.loads(raw[start:])
        if not isinstance(value, dict):
            raise typer.BadParameter(f"{path} must contain a JSON object")
        return value

    research: dict[str, dict[str, object]] = {}
    if research_json:
        loaded = load_payload(research_json)
        research = {
            str(symbol).upper(): value
            for symbol, value in loaded.items()
            if isinstance(value, dict)
        }
    additional: list[dict[str, object]] = []
    if additional_json:
        loaded_additional = load_payload(additional_json)
        raw_items = loaded_additional.get("candidates", loaded_additional)
        if isinstance(raw_items, list):
            additional = [item for item in raw_items if isinstance(item, dict)]
    result = build_ism_equity_pipeline(
        load_payload(manufacturing_json),
        load_payload(services_json),
        research_by_symbol=research,
        portfolio_symbols=[item for item in portfolio_symbols.split(",") if item.strip()],
        watch_symbols=[item for item in watch_symbols.split(",") if item.strip()],
        additional_candidates=additional,
        limit=limit,
    )
    if output:
        output_path = Path(output).expanduser()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(_json.dumps(result, indent=2) + "\n", encoding="utf-8")
        result["saved_to"] = str(output_path)
    if json_out:
        typer.echo(_json.dumps(result, indent=2, default=str))
    else:
        typer.echo(
            f"ISM equity pipeline: {len(result['candidate_pool'])} candidates; "
            f"{len(result['human_review'])} human-review decisions"
        )


@stocks_app.command("x-analyze")
def x_analyze(
    tickers: Optional[str] = typer.Option(
        None,
        "--tickers",
        help="Comma-separated tickers to analyze, e.g. NVDA,AAPL,GE.",
    ),
    from_snapshot: Optional[str] = typer.Option(
        None,
        "--from-snapshot",
        help="Path to an ISM snapshot JSON; pulls top picks from candidates.longs.",
    ),
    top: int = typer.Option(
        5,
        "--top",
        help="With --from-snapshot, take the top N long candidates.",
    ),
    days: int = typer.Option(
        DEFAULT_LOOKBACK_DAYS,
        "--days",
        help="Lookback window in days for X search.",
    ),
    limit_per_ticker: int = typer.Option(
        DEFAULT_LIMIT_PER_TICKER,
        "--limit-per-ticker",
        help="Max posts per ticker.",
    ),
    json_out: bool = typer.Option(
        False, "--json", help="Emit the full JSON payload (incl. analysis prompt)."
    ),
    sheet: bool = typer.Option(
        False, "--sheet", help="Render Rich tables (default for terminal)."
    ),
    telegram_markdown_v2: bool = typer.Option(
        False,
        "--telegram-markdown-v2",
        help="Render Telegram-friendly MarkdownV2 summary digest (chunked).",
    ),
    save_snapshot: bool = typer.Option(
        True,
        "--save-snapshot/--no-save-snapshot",
        help="Save the analysis payload to stocks_history/ as JSON.",
    ),
    snapshot_dir: Optional[str] = typer.Option(
        None,
        "--snapshot-dir",
        help="Override snapshot output directory.",
    ),
) -> None:
    """Fetch X posts about tickers and package them with the LLM analysis prompt.

    Output is a JSON payload (machine-readable, persisted) plus an optional
    Rich-table sheet for the terminal. The analysis prompt is baked into the
    payload — pipe the JSON into your LLM, paste it into Claude/ChatGPT, or
    let the Hermes Telegram agent run it.
    """
    resolved = _resolve_tickers(tickers, from_snapshot=from_snapshot, top=top)
    if not resolved:
        raise typer.BadParameter(
            "No tickers resolved. Pass --tickers or --from-snapshot."
        )

    payload = analyze_tickers(
        resolved,
        days=days,
        limit_per_ticker=limit_per_ticker,
        persist=save_snapshot,
        snapshot_dir=snapshot_dir,
    )

    if json_out and not sheet:
        typer.echo(_json.dumps(payload, indent=2, default=str))
        return

    if telegram_markdown_v2:
        messages = render_x_summary_markdown_v2(payload)
        for idx, message in enumerate(messages, start=1):
            if idx > 1:
                typer.echo("\n---\n")
            typer.echo(message)
        return

    # Default to sheet for human consumption; JSON path printed for follow-up.
    render_x_sheet(payload)
    if not sheet and payload.get("saved_to"):
        typer.echo(f"\nJSON payload (with LLM prompt): {payload['saved_to']}")


def _resolve_tickers(
    tickers: Optional[str],
    *,
    from_snapshot: Optional[str],
    top: int,
) -> list[str]:
    if tickers:
        return [t.strip().upper() for t in tickers.split(",") if t.strip()]
    if from_snapshot:
        path = _path_for(from_snapshot)
        if not path.exists():
            raise typer.BadParameter(f"snapshot not found: {path}")
        snapshot = _json.loads(path.read_text())
        longs = (snapshot.get("candidates") or {}).get("longs") or []
        return [str(row.get("symbol", "")).upper() for row in longs[:top] if row.get("symbol")]
    return []


def _path_for(maybe_path: str):
    from pathlib import Path
    return Path(maybe_path).expanduser().resolve()


@ism_calendar_app.command("refresh")
def ism_calendar_refresh(
    year: list[int] = typer.Option(
        None,
        "--year",
        help="Year(s) to refresh. Defaults to the current year. Repeat for many.",
    ),
    snapshot_dir: Optional[str] = typer.Option(
        None,
        "--snapshot-dir",
        help="Override calendar output directory.",
    ),
    json_out: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """Fetch the ISM release calendar from FMP and save it to the repo."""
    from datetime import date

    years = year or [date.today().year]
    written: list[dict[str, object]] = []
    for y in years:
        try:
            calendar = fetch_ism_calendar(y, snapshot_dir=snapshot_dir)
        except ISMCalendarError as exc:
            raise typer.BadParameter(str(exc)) from exc
        written.append(
            {
                "year": calendar.year,
                "releases": len(calendar.releases),
                "manufacturing": len(calendar.by_kind("manufacturing")),
                "services": len(calendar.by_kind("services")),
            }
        )

    if json_out:
        typer.echo(_json.dumps(written, indent=2, default=str))
        return
    for row in written:
        typer.echo(
            f"  ISM {row['year']}: {row['releases']} releases "
            f"(mfg={row['manufacturing']}, svc={row['services']})"
        )


@ism_calendar_app.command("show")
def ism_calendar_show(
    year: int = typer.Option(
        None, "--year", help="Year to show. Defaults to the current year."
    ),
    kind: Optional[str] = typer.Option(
        None,
        "--kind",
        help="Filter to manufacturing or services. Defaults to both.",
    ),
    snapshot_dir: Optional[str] = typer.Option(
        None, "--snapshot-dir", help="Override calendar input directory."
    ),
    json_out: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """Print the stored ISM release calendar as a Rich table or JSON."""
    from datetime import date

    target_year = year or date.today().year
    if kind is not None and kind not in {"manufacturing", "services"}:
        raise typer.BadParameter("--kind must be manufacturing or services")

    calendar = load_calendar(target_year, snapshot_dir=snapshot_dir)
    if calendar is None:
        raise typer.BadParameter(
            f"No stored calendar for {target_year}. Run "
            f"`nave stocks ism-calendar refresh --year {target_year}` first."
        )

    releases = calendar.releases
    if kind is not None:
        releases = [r for r in releases if r.kind == kind]

    if json_out:
        typer.echo(
            _json.dumps(
                {
                    "year": calendar.year,
                    "generated_at": calendar.generated_at,
                    "source": calendar.source,
                    "releases": [r.__dict__ for r in releases],
                },
                indent=2,
                default=str,
            )
        )
        return

    console = Console()
    console.print(
        f"[bold]ISM release calendar — {calendar.year}[/bold]  "
        f"(source: {calendar.source}, generated_at: {calendar.generated_at})"
    )
    table = Table()
    table.add_column("Release date (UTC)")
    table.add_column("Kind")
    table.add_column("Covers")
    table.add_column("Event")
    table.add_column("Impact")
    for r in releases:
        table.add_row(
            r.release_at_utc,
            r.kind,
            r.covers_month or "?",
            r.event,
            r.impact or "?",
        )
    console.print(table)


@ism_calendar_app.command("next")
def ism_calendar_next(
    kind: Optional[str] = typer.Option(
        None, "--kind", help="Filter to manufacturing or services."
    ),
    snapshot_dir: Optional[str] = typer.Option(
        None, "--snapshot-dir", help="Override calendar input directory."
    ),
    json_out: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """Print the next upcoming ISM release from the stored calendar."""
    if kind is not None and kind not in {"manufacturing", "services"}:
        raise typer.BadParameter("--kind must be manufacturing or services")

    kind_filter = cast("CalendarKind | None", kind)
    release = next_release(kind=kind_filter, snapshot_dir=snapshot_dir)
    if release is None:
        raise typer.BadParameter(
            "No upcoming release found. Refresh the calendar first."
        )
    if json_out:
        typer.echo(_json.dumps(release.__dict__, indent=2, default=str))
        return
    typer.echo(
        f"Next ISM {release.kind} release: {release.release_at_utc} "
        f"(covers {release.covers_month or '?'}) — {release.event}"
    )


@stocks_app.command("events-list")
def events_list(
    status: str | None = typer.Option(None, "--status"),
    ticker: str | None = typer.Option(None, "--ticker"),
    due_only: bool = typer.Option(False, "--due-only"),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """List material events that still need review."""
    from trading.stocks.event_journal import list_events

    events = list_events(status=status, ticker=ticker, due_only=due_only)
    if json_out:
        typer.echo(_json.dumps(events, indent=2, default=str))
        return
    if not events:
        typer.echo("No portfolio events require review.")
        return
    for event in events:
        typer.echo(
            f"{event.get('event_id')} | {event.get('status')} | "
            f"{event.get('importance')} | {event.get('ticker')} | "
            f"{event.get('event_type')} | next={event.get('next_review_date') or '?'}"
        )


@stocks_app.command("events-mark")
def events_mark(
    event_id: str = typer.Argument(...),
    status: str = typer.Option(..., "--status", help="new|watching|reviewed|closed"),
    note: str | None = typer.Option(None, "--note"),
    next_review_date: str | None = typer.Option(None, "--next-review-date"),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Mark a material event after human review; never executes an order."""
    from trading.stocks.event_journal import mark_event

    try:
        event = mark_event(
            event_id,
            status=status,
            note=note,
            next_review_date=next_review_date,
        )
    except (KeyError, ValueError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    if json_out:
        typer.echo(_json.dumps(event, indent=2, default=str))
    else:
        typer.echo(f"Marked {event_id}: {event['status']}")


@stocks_app.command("journal-stats")
def journal_stats(json_out: bool = typer.Option(False, "--json")) -> None:
    """Print stock-only journal stats (filters by asset_class=stock)."""
    journal = StockJournal()
    stats = journal.stats()
    if json_out:
        typer.echo(_json.dumps(stats, indent=2, default=str))
        return
    typer.echo("Stock journal stats:")
    for k, v in stats.items():
        typer.echo(f"  {k}: {v}")


@stocks_app.command("politicians-scan")
def politicians_scan(
    json_out: bool = typer.Option(
        False, "--json", help="Emit the full JSON payload."
    ),
    telegram_markdown_v2: bool = typer.Option(
        False,
        "--telegram-markdown-v2",
        help="Render Telegram-friendly MarkdownV2 digest (chunked).",
    ),
    no_persist: bool = typer.Option(
        False,
        "--no-persist",
        help="Run a dry scan without updating the seen-disclosures cache.",
    ),
    save_report: bool = typer.Option(
        True,
        "--save-report/--no-save-report",
        help="Persist a JSON snapshot under var/reports/politicians/.",
    ),
) -> None:
    """Same as [bold]nave congress[/bold] — new disclosures since last run."""
    from cli.commands.congress import _run_congress_scan
    from trading.stocks.politicians.display import render_congress_scan
    from trading.stocks.politicians.formatters import render_politicians_scan_markdown_v2

    payload = _run_congress_scan(persist=not no_persist, save_report=save_report)

    if json_out:
        typer.echo(_json.dumps(payload, indent=2, default=str))
        return

    if telegram_markdown_v2:
        messages = render_politicians_scan_markdown_v2(payload, include_empty=True)
        if not messages:
            typer.echo("No Telegram digest generated.")
            return
        for idx, message in enumerate(messages, start=1):
            if idx > 1:
                typer.echo("\n---\n")
            typer.echo(message)
        return

    render_congress_scan(payload, console=Console())
