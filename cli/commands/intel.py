"""Intelligence workflows owned by NAVE."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import typer

from cli.professional_typer import ProfessionalTyper
from research.cava.pipeline import CAVA_RSS_URL, CavaWorkflow
from research.cava.transcript import SupadataTranscriptProvider
from research.core.store import ResearchStore

intel_app = ProfessionalTyper(help="NAVE intelligence workflows.")
cava_app = ProfessionalTyper(help="José Luis Cava video and macro intelligence.")
context_app = ProfessionalTyper(help="Shared validated context used by research workflows.")
intel_app.add_typer(cava_app, name="cava")
intel_app.add_typer(context_app, name="context")


def _fetch_rss() -> str:
    try:
        response = httpx.get(CAVA_RSS_URL, timeout=20.0)
        response.raise_for_status()
        return response.text
    except httpx.HTTPError as exc:
        raise typer.ClickException(f"Cava RSS unavailable: {exc}") from exc


@cava_app.command("daily")
def cava_daily(
    rss_file: Path | None = typer.Option(None, "--rss-file", exists=True, readable=True),
    state_dir: Path | None = typer.Option(None, "--state-dir", help="Runtime state directory."),
    json_out: bool = typer.Option(False, "--json", help="Emit JSON only."),
    markdown: bool = typer.Option(False, "--markdown", help="Render the structured result as Markdown."),
) -> None:
    """Process the newest unprocessed Cava video through Supadata."""
    store = ResearchStore(state_dir)
    workflow = CavaWorkflow(store=store)
    try:
        rss_xml = rss_file.read_text(encoding="utf-8") if rss_file else _fetch_rss()
        result = workflow.run(
            rss_xml=rss_xml,
            transcript_provider=SupadataTranscriptProvider(),
            now=datetime.now(UTC),
        )
    except (OSError, typer.ClickException) as exc:
        result = workflow.unavailable(str(exc), now=datetime.now(UTC))
    if markdown:
        typer.echo(result.to_markdown())
    elif json_out:
        typer.echo(result.to_json())
    else:
        typer.echo(f"{result.workflow}: {result.status.value}")
        if result.warnings:
            typer.echo("Warnings: " + "; ".join(result.warnings))


@context_app.command("latest")
def context_latest(
    state_dir: Path | None = typer.Option(None, "--state-dir", help="Runtime state directory."),
    json_out: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Show the latest validated Cava context without re-scraping."""
    context = ResearchStore(state_dir).load_context("cava")
    payload = context or {"status": "DATA_UNAVAILABLE", "context": None}
    if json_out:
        typer.echo(json.dumps(payload, indent=2, default=str))
    else:
        typer.echo(json.dumps(payload, indent=2, default=str))
