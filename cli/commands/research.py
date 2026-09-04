"""Inspection commands for structured NAVE research artifacts."""

from __future__ import annotations

import json
from pathlib import Path

import typer

from cli.professional_typer import ProfessionalTyper
from research.core.contracts import ResearchResult
from research.core.store import ResearchStore

research_app = ProfessionalTyper(help="Inspect read-only structured research results.")


@research_app.command("status")
def status(
    workflow: str | None = typer.Option(None, "--workflow", help="Workflow name to inspect."),
    json_out: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Show the latest stored result or the available result index."""
    store = ResearchStore()
    if workflow:
        result = store.load_result(workflow)
        payload = result.to_dict() if result else {
            "workflow": workflow,
            "status": "DATA_UNAVAILABLE",
            "results": [],
        }
    else:
        payload = {"results": store.list_results()}
    if json_out:
        typer.echo(json.dumps(payload, indent=2, default=str))
        return
    if workflow and payload.get("status"):
        typer.echo(f"{payload.get('workflow')}: {payload.get('status')}")
        return
    typer.echo(f"stored research results: {len(payload['results'])}")
    for item in payload["results"]:
        typer.echo(f"- {item.get('workflow', '?')}: {item.get('status', '?')}")


@research_app.command("report")
def report(
    json_file: Path = typer.Option(..., "--json-file", exists=True, readable=True),
    markdown: bool = typer.Option(False, "--markdown", help="Render Markdown instead of JSON."),
) -> None:
    """Validate and render a saved structured result."""
    result = ResearchResult.from_dict(json.loads(json_file.read_text(encoding="utf-8")))
    typer.echo(result.to_markdown() if markdown else result.to_json())
