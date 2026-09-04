"""Memecoin CLI command group — Solana scanner + safety filter.

Subcommands:
    nave memecoin scan         Pull recent Pump.fun launches, gate, score, rank.
    nave memecoin check        Run the full safety + score pipeline on one mint.
"""

from __future__ import annotations

import json as _json
import logging
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from cli.professional_typer import ProfessionalTyper
from core.logger import configure_logger
from trading.memecoin import MemecoinScanner
from trading.memecoin.scanner import SORT_ACTIVE, SORT_FRESH, SORT_TOP_MCAP
from research.core.contracts import ResearchResult
from research.core.store import ResearchStore
from research.memecoin_workflow import MemecoinResearchWorkflow
from research.dune.materializer import DuneMaterializer

_SORT_CHOICES = {
    "active": SORT_ACTIVE,
    "fresh": SORT_FRESH,
    "top": SORT_TOP_MCAP,
}

logger = configure_logger(__name__, level=logging.INFO)

memecoin_app = ProfessionalTyper(
    help="Solana memecoin scanner (Pump.fun + Helius + DexScreener + Jupiter)."
)
dune_app = ProfessionalTyper(help="Bounded Dune materialization for memecoin research.")
memecoin_app.add_typer(dune_app, name="dune")


def _emit_research(result: ResearchResult, *, json_out: bool, markdown: bool) -> None:
    if markdown:
        typer.echo(result.to_markdown())
    elif json_out:
        typer.echo(result.to_json())
    else:
        typer.echo(f"{result.workflow}: {result.status.value}")
        if result.warnings:
            typer.echo("Warnings: " + "; ".join(result.warnings))


@memecoin_app.command("discover")
def discover(
    input_file: Path | None = typer.Option(None, "--input-file", exists=True, readable=True),
    dune_cache: Path | None = typer.Option(None, "--dune-cache", exists=True, readable=True),
    state_dir: Path | None = typer.Option(None, "--state-dir"),
    json_out: bool = typer.Option(False, "--json"),
    markdown: bool = typer.Option(False, "--markdown"),
) -> None:
    """Discover point-in-time candidates from an explicit local snapshot."""
    if input_file:
        raw = _json.loads(input_file.read_text(encoding="utf-8"))
        rows = raw.get("rows", raw) if isinstance(raw, dict) else raw
    elif dune_cache:
        rows = []
    else:
        raise typer.BadParameter("pass --input-file for replay or --dune-cache for a materialized Dune result")
    result = MemecoinResearchWorkflow(store=ResearchStore(state_dir)).discover(rows, dune_cache=dune_cache)
    _emit_research(result, json_out=json_out, markdown=markdown)


@dune_app.command("materialize")
def materialize_dune(
    query_id: str = typer.Option(..., "--query-id", help="Saved Dune query ID."),
    output: Path = typer.Option(..., "--output", help="Local JSON materialization path."),
    limit: int = typer.Option(10_000, "--limit", min=1, max=100_000),
    force: bool = typer.Option(False, "--force/--no-force", help="Re-run even when the matching cache exists."),
) -> None:
    """Run one bounded Dune query and cache it for later local discovery."""
    payload = DuneMaterializer().materialize(
        query_id=query_id,
        output=output,
        limit=limit,
        force=force,
    )
    typer.echo(_json.dumps({key: value for key, value in payload.items() if key != "rows"}, indent=2, default=str))


def _load_result(store: ResearchStore, path: Path | None) -> ResearchResult:
    if path:
        return ResearchResult.from_dict(_json.loads(path.read_text(encoding="utf-8")))
    result = store.load_result("memecoin.discover")
    if result is None:
        raise typer.BadParameter("no discovery result found; pass --discover-file or run memecoin discover")
    return result


@memecoin_app.command("evaluate")
def evaluate(
    outcomes_file: Path = typer.Option(..., "--outcomes-file", exists=True, readable=True),
    discover_file: Path | None = typer.Option(None, "--discover-file", exists=True, readable=True),
    state_dir: Path | None = typer.Option(None, "--state-dir"),
    json_out: bool = typer.Option(False, "--json"),
    markdown: bool = typer.Option(False, "--markdown"),
) -> None:
    """Evaluate selected research rows against later outcomes."""
    store = ResearchStore(state_dir)
    raw = _json.loads(outcomes_file.read_text(encoding="utf-8"))
    outcomes = raw.get("outcomes", raw) if isinstance(raw, dict) else raw
    result = MemecoinResearchWorkflow(store=store).evaluate(scan_result=_load_result(store, discover_file), outcomes=outcomes)
    _emit_research(result, json_out=json_out, markdown=markdown)


@memecoin_app.command("missed-moves")
def missed_moves_command(
    outcomes_file: Path = typer.Option(..., "--outcomes-file", exists=True, readable=True),
    discover_file: Path | None = typer.Option(None, "--discover-file", exists=True, readable=True),
    move_threshold: float = typer.Option(0.50, "--move-threshold"),
    state_dir: Path | None = typer.Option(None, "--state-dir"),
    json_out: bool = typer.Option(False, "--json"),
    markdown: bool = typer.Option(False, "--markdown"),
) -> None:
    """Audit large movers missed by the point-in-time discovery filters."""
    store = ResearchStore(state_dir)
    raw = _json.loads(outcomes_file.read_text(encoding="utf-8"))
    outcomes = raw.get("outcomes", raw) if isinstance(raw, dict) else raw
    result = MemecoinResearchWorkflow(store=store).missed_moves(
        scan_result=_load_result(store, discover_file), outcomes=outcomes, move_threshold=move_threshold
    )
    _emit_research(result, json_out=json_out, markdown=markdown)


@memecoin_app.command("backtest")
def backtest(
    discover_file: Path = typer.Option(..., "--discover-file", exists=True, readable=True),
    outcomes_file: Path = typer.Option(..., "--outcomes-file", exists=True, readable=True),
    state_dir: Path | None = typer.Option(None, "--state-dir"),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Run the bounded structured evaluation as a backtest report."""
    store = ResearchStore(state_dir)
    scan = ResearchResult.from_dict(_json.loads(discover_file.read_text(encoding="utf-8")))
    raw = _json.loads(outcomes_file.read_text(encoding="utf-8"))
    outcomes = raw.get("outcomes", raw) if isinstance(raw, dict) else raw
    result = MemecoinResearchWorkflow(store=store).evaluate(scan_result=scan, outcomes=outcomes)
    _emit_research(result, json_out=json_out, markdown=False)


@memecoin_app.command("status")
def research_status(
    state_dir: Path | None = typer.Option(None, "--state-dir"),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Show discovery/evaluation/missed-move result states."""
    payload = MemecoinResearchWorkflow(store=ResearchStore(state_dir)).status()
    typer.echo(_json.dumps(payload, indent=2))


def _verdict_color(verdict: str) -> str:
    return {
        "PASS": "green",
        "WATCH": "yellow",
        "FAIL": "red",
    }.get(verdict, "white")


def _label_color(label: str) -> str:
    return {
        "GOOD": "green",
        "WATCH": "yellow",
        "SHILL": "red",
    }.get(label, "white")


@memecoin_app.command("scan")
def scan(
    limit: int = typer.Option(
        50, "--limit", help="How many recent Pump.fun rows to pull."
    ),
    top_n: int = typer.Option(
        10, "--top-n", help="Keep the top-N passing candidates by score."
    ),
    sort: str = typer.Option(
        "active",
        "--sort",
        help=(
            "Pump.fun discovery sort. 'active' (default) surfaces both fresh "
            "and graduated tokens that are currently moving. 'fresh' returns "
            "just-minted tokens (most fail the liquidity gate). 'top' rotates "
            "the top-mcap rows on Pump.fun's active list."
        ),
    ),
    keep_skipped: bool = typer.Option(
        False,
        "--keep-skipped",
        help="Include liquidity-rejected tokens (observability).",
    ),
    json_out: bool = typer.Option(
        False, "--json", help="Emit raw JSON instead of the table."
    ),
) -> None:
    """Run the discover → gate → safety → score pipeline."""
    sort_value = _SORT_CHOICES.get(sort.lower())
    if sort_value is None:
        raise typer.BadParameter(
            f"--sort must be one of {sorted(_SORT_CHOICES)}, got {sort!r}"
        )

    scanner = MemecoinScanner()
    candidates = scanner.scan(
        limit=limit, top_n=top_n, sort=sort_value, keep_skipped=keep_skipped
    )

    payload = {
        "tool": "memecoin_scan",
        "params": {
            "limit": limit,
            "top_n": top_n,
            "sort": sort,
            "keep_skipped": keep_skipped,
        },
        "count": len(candidates),
        "candidates": [c.to_dict() for c in candidates],
    }

    if json_out:
        typer.echo(_json.dumps(payload, default=str, indent=2))
        return

    console = Console()
    if not candidates:
        console.print(
            "[yellow]No candidates returned.[/yellow] Either the Pump.fun feed "
            "is empty right now or every recent launch failed the $25k "
            "liquidity gate or a safety check."
        )
        return

    table = Table(
        title=f"Memecoin scan — {len(candidates)} candidates "
        f"(limit={limit}, top_n={top_n})",
        show_lines=False,
    )
    table.add_column("Score", justify="right")
    table.add_column("Label")
    table.add_column("Verdict")
    table.add_column("Symbol")
    table.add_column("Mint", overflow="ellipsis")
    table.add_column("Liq $", justify="right")
    table.add_column("FDV $", justify="right")
    table.add_column("Vol24h $", justify="right")
    table.add_column("5m %", justify="right")
    table.add_column("1h %", justify="right")
    table.add_column("Top-10 %", justify="right")
    table.add_column("Rug")

    for c in candidates:
        market = c.market
        liq = (market.liquidity_usd if market else None) or 0.0
        fdv = (market.fdv_usd if market else None) or (
            market.market_cap_usd if market else None
        )
        vol = (market.volume_24h_usd if market else None) or 0.0
        m5 = market.price_change_5m_pct if market else None
        h1 = market.price_change_1h_pct if market else None
        conc = c.safety.checks.get("holder_concentration", {}) if c.safety.checks else {}
        top10 = conc.get("top_10_pct") if isinstance(conc, dict) else None
        table.add_row(
            f"{c.score.total}",
            f"[{_label_color(c.score.label.value)}]{c.score.label.value}[/]",
            f"[{_verdict_color(c.safety.verdict.value)}]{c.safety.verdict.value}[/]",
            c.symbol or "?",
            c.mint,
            f"{liq:,.0f}",
            f"{fdv:,.0f}" if fdv else "-",
            f"{vol:,.0f}",
            f"{m5:+.1f}" if m5 is not None else "-",
            f"{h1:+.1f}" if h1 is not None else "-",
            f"{top10:.1f}" if isinstance(top10, (int, float)) else "-",
            f"{c.safety.rug_score}",
        )
    console.print(table)


@memecoin_app.command("check")
def check(
    mint: str = typer.Argument(..., help="Solana mint address (base58)."),
    json_out: bool = typer.Option(
        False, "--json", help="Emit raw JSON instead of the formatted block."
    ),
) -> None:
    """Run the full safety + score pipeline on a single mint."""
    scanner = MemecoinScanner()
    candidate = scanner.check(mint)
    payload = candidate.to_dict()

    if json_out:
        typer.echo(_json.dumps(payload, default=str, indent=2))
        return

    console = Console()
    safety = candidate.safety
    score = candidate.score
    market = candidate.market

    console.print()
    console.print(
        f"[bold]{candidate.symbol or '?'}[/bold]  "
        f"({candidate.name or 'unknown name'})  "
        f"[dim]{candidate.mint}[/dim]"
    )
    console.print(
        f"verdict: [{_verdict_color(safety.verdict.value)}]{safety.verdict.value}[/]   "
        f"rug_score: {safety.rug_score}   "
        f"label: [{_label_color(score.label.value)}]{score.label.value}[/] "
        f"({score.total}/100)"
    )
    if candidate.skipped_reason:
        console.print(f"[red]skipped:[/] {candidate.skipped_reason}")

    if market:
        console.print(
            f"market: liq=${(market.liquidity_usd or 0):,.0f}  "
            f"fdv=${(market.fdv_usd or market.market_cap_usd or 0):,.0f}  "
            f"vol24h=${(market.volume_24h_usd or 0):,.0f}  "
            f"dex={market.dex or '-'}"
        )

    console.print()
    console.print("[bold]Safety checks[/bold]")
    for name, value in (safety.checks or {}).items():
        console.print(f"  - {name}: {value}")

    console.print()
    console.print("[bold]Score breakdown[/bold]")
    for band_name, band in (score.bands or {}).items():
        console.print(f"  - {band_name}: {band}")
    if score.rationale:
        console.print()
        console.print("[bold]Rationale[/bold]")
        for line in score.rationale:
            console.print(f"  • {line}")
    console.print()
