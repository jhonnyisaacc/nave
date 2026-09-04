"""NAVE-owned human-gated portfolio research commands."""

from __future__ import annotations

import json
from pathlib import Path

import typer

from cli.professional_typer import ProfessionalTyper
from research.core.store import ResearchStore
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


def _load_json(path: Path) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise typer.BadParameter(f"{path} must contain a JSON object")
    return payload


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
    evidence = _load_json(evidence_file) if evidence_file else {}
    result = review_positions(state, evidence, now=None)
    _emit(PortfolioWorkflow(store=ResearchStore(state_dir)).save(result), json_out=json_out, markdown=markdown)


@portfolio_app.command("candidates")
def candidates(
    ism_file: Path = typer.Option(..., "--ism-file", exists=True, readable=True),
    portfolio_file: Path | None = typer.Option(None, "--portfolio-file", exists=True, readable=True),
    state_dir: Path | None = typer.Option(None, "--state-dir"),
    json_out: bool = typer.Option(False, "--json"),
    markdown: bool = typer.Option(False, "--markdown"),
) -> None:
    """Build provenance-preserving candidates from both ISM reports."""
    payload = _load_json(ism_file)
    state = load_portfolio_state(portfolio_file)
    result = portfolio_candidates(
        payload.get("manufacturing") or {},
        payload.get("services") or {},
        state=state,
    )
    _emit(PortfolioWorkflow(store=ResearchStore(state_dir)).save(result), json_out=json_out, markdown=markdown)


@portfolio_app.command("ism")
def ism(
    ism_file: Path = typer.Option(..., "--ism-file", exists=True, readable=True),
    portfolio_file: Path | None = typer.Option(None, "--portfolio-file", exists=True, readable=True),
    state_dir: Path | None = typer.Option(None, "--state-dir"),
    json_out: bool = typer.Option(False, "--json"),
    markdown: bool = typer.Option(False, "--markdown"),
) -> None:
    """Rank the actual Manufacturing and Services ISM industry/company mapping."""
    payload = _load_json(ism_file)
    result = ism_rank(
        payload.get("manufacturing") or {},
        payload.get("services") or {},
        state=load_portfolio_state(portfolio_file),
    )
    _emit(PortfolioWorkflow(store=ResearchStore(state_dir)).save(result), json_out=json_out, markdown=markdown)


@portfolio_app.command("watch")
def watch(
    watch_file: Path = typer.Option(..., "--watch-file", exists=True, readable=True),
    prices_file: Path = typer.Option(..., "--prices-file", exists=True, readable=True),
    state_dir: Path | None = typer.Option(None, "--state-dir"),
    json_out: bool = typer.Option(False, "--json"),
    markdown: bool = typer.Option(False, "--markdown"),
) -> None:
    """Run a cheap deterministic price threshold check."""
    raw_watches = json.loads(watch_file.read_text(encoding="utf-8"))
    raw_prices = _load_json(prices_file)
    watches = raw_watches.get("watches", raw_watches) if isinstance(raw_watches, dict) else raw_watches
    prices = raw_prices.get("prices", raw_prices)
    result = check_watch(watches, prices)
    _emit(PortfolioWorkflow(store=ResearchStore(state_dir)).save(result), json_out=json_out, markdown=markdown)
