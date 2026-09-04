"""Normalized political/public financial disclosure commands."""

from __future__ import annotations

from pathlib import Path

import typer

from cli.professional_typer import ProfessionalTyper
from research.core.store import ResearchStore
from research.disclosures import DisclosureWorkflow

disclosures_app = ProfessionalTyper(help="Research-only normalized public disclosures.")


@disclosures_app.command("sync")
def sync(
    congress_file: Path | None = typer.Option(None, "--congress-file", exists=True, readable=True),
    executive_file: Path | None = typer.Option(None, "--executive-file", exists=True, readable=True),
    state_dir: Path | None = typer.Option(None, "--state-dir"),
    json_out: bool = typer.Option(False, "--json"),
    markdown: bool = typer.Option(False, "--markdown"),
) -> None:
    """Normalize congress and executive disclosure feeds with dedupe."""
    result = DisclosureWorkflow(store=ResearchStore(state_dir)).sync_files(
        congress_file=congress_file,
        executive_file=executive_file,
    )
    if markdown:
        typer.echo(result.to_markdown())
    elif json_out:
        typer.echo(result.to_json())
    else:
        typer.echo(f"{result.workflow}: {result.status.value}")
        if result.warnings:
            typer.echo("Warnings: " + "; ".join(result.warnings))
