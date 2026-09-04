"""NAVE-owned human-gated portfolio research commands."""

from __future__ import annotations

import json
from pathlib import Path

import typer

from cli.professional_typer import ProfessionalTyper
from research.core.store import ResearchStore
from research.portfolio_providers import PortfolioContextProvider, load_current_ism_inputs
from research.portfolio import (
    PortfolioState,
    PortfolioWorkflow,
    check_watch,
    ism_rank,
    load_portfolio_state,
    portfolio_candidates,
    review_positions,
)

portfolio_app = ProfessionalTyper(help="Human-gated read-only portfolio research.")
DEFAULT_WATCHLIST_FILE = Path(__file__).resolve().parents[2] / "config" / "portfolio_watchlist.json"


def _load_json(path: Path) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise typer.BadParameter(f"{path} must contain a JSON object")
    return payload


def _load_watches(path: Path | None, state: PortfolioState) -> list[dict]:
    target = path or DEFAULT_WATCHLIST_FILE
    if target.exists():
        raw = json.loads(target.read_text(encoding="utf-8"))
        watches = raw.get("watches", raw.get("stocks", raw)) if isinstance(raw, dict) else raw
        if isinstance(watches, list):
            return [dict(item) for item in watches if isinstance(item, dict)]
    return [dict(item) for item in state.watchlist]


def _emit(result, *, json_out: bool, markdown: bool) -> None:
    if markdown:
        typer.echo(result.to_markdown())
    elif json_out:
        typer.echo(result.to_json())
    else:
        typer.echo(f"{result.workflow}: {result.status.value}")
        if result.warnings:
            typer.echo("Warnings: " + "; ".join(result.warnings))


@portfolio_app.command("review")
def review(
    evidence_file: Path | None = typer.Option(None, "--evidence-file", exists=True, readable=True),
    portfolio_file: Path | None = typer.Option(None, "--portfolio-file", exists=True, readable=True),
    state_dir: Path | None = typer.Option(None, "--state-dir"),
    json_out: bool = typer.Option(False, "--json"),
    markdown: bool = typer.Option(False, "--markdown"),
) -> None:
    """Review current positions from user-local state."""
    state = load_portfolio_state(portfolio_file)
    if evidence_file:
        evidence = _load_json(evidence_file)
    else:
        store = ResearchStore(state_dir)
        tickers = [position.ticker for position in state.positions]
        evidence = PortfolioContextProvider().build_review_context(
            tickers,
            macro_context=store.load_context("cava"),
        )
    result = review_positions(state, evidence, now=None)
    _emit(PortfolioWorkflow(store=ResearchStore(state_dir)).save(result), json_out=json_out, markdown=markdown)


@portfolio_app.command("candidates")
def candidates(
    ism_file: Path | None = typer.Option(None, "--ism-file", exists=True, readable=True),
    portfolio_file: Path | None = typer.Option(None, "--portfolio-file", exists=True, readable=True),
    state_dir: Path | None = typer.Option(None, "--state-dir"),
    json_out: bool = typer.Option(False, "--json"),
    markdown: bool = typer.Option(False, "--markdown"),
) -> None:
    """Build provenance-preserving candidates from both ISM reports."""
    payload = _load_json(ism_file) if ism_file else load_current_ism_inputs()
    state = load_portfolio_state(portfolio_file)
    result = portfolio_candidates(
        payload.get("manufacturing") or {},
        payload.get("services") or {},
        state=state,
    )
    _emit(PortfolioWorkflow(store=ResearchStore(state_dir)).save(result), json_out=json_out, markdown=markdown)


@portfolio_app.command("ism")
def ism(
    ism_file: Path | None = typer.Option(None, "--ism-file", exists=True, readable=True),
    portfolio_file: Path | None = typer.Option(None, "--portfolio-file", exists=True, readable=True),
    state_dir: Path | None = typer.Option(None, "--state-dir"),
    json_out: bool = typer.Option(False, "--json"),
    markdown: bool = typer.Option(False, "--markdown"),
) -> None:
    """Rank the actual Manufacturing and Services ISM industry/company mapping."""
    payload = _load_json(ism_file) if ism_file else load_current_ism_inputs()
    result = ism_rank(
        payload.get("manufacturing") or {},
        payload.get("services") or {},
        state=load_portfolio_state(portfolio_file),
    )
    _emit(PortfolioWorkflow(store=ResearchStore(state_dir)).save(result), json_out=json_out, markdown=markdown)


@portfolio_app.command("watch")
def watch(
    watch_file: Path | None = typer.Option(None, "--watch-file", exists=True, readable=True),
    prices_file: Path | None = typer.Option(None, "--prices-file", exists=True, readable=True),
    state_dir: Path | None = typer.Option(None, "--state-dir"),
    json_out: bool = typer.Option(False, "--json"),
    markdown: bool = typer.Option(False, "--markdown"),
) -> None:
    """Run a cheap deterministic price threshold check."""
    store = ResearchStore(state_dir)
    state = load_portfolio_state()
    watches = _load_watches(watch_file, state)
    if prices_file:
        raw_prices = _load_json(prices_file)
        prices = raw_prices.get("prices", raw_prices)
        previous = {}
    else:
        context = PortfolioContextProvider().build_review_context(
            [str(item.get("ticker") or "") for item in watches],
            macro_context=store.load_context("cava"),
        )
        prices = {
            ticker: value.get("market_state", {}).get("current_price")
            for ticker, value in context.items()
            if value.get("market_state", {}).get("current_price") is not None
        }
        previous = store.load_context("portfolio_watch_prices") or {}
        store.save_context("portfolio_watch_prices", prices)
    result = check_watch(watches, prices, previous_prices=previous)
    result_payload = dict(result.payload)
    result_payload["watchlist_source"] = str(watch_file or DEFAULT_WATCHLIST_FILE)
    result = result.__class__(
        workflow=result.workflow,
        status=result.status,
        metadata=result.metadata,
        payload=result_payload,
        evidence=result.evidence,
        warnings=result.warnings,
        generated_at=result.generated_at,
        safety_boundary=result.safety_boundary,
    )
    _emit(PortfolioWorkflow(store=store).save(result), json_out=json_out, markdown=markdown)
