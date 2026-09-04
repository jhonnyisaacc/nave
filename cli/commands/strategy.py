"""Generic read-only strategy lifecycle commands."""

from __future__ import annotations

import json
from pathlib import Path

import typer

from cli.commands.options import _emit_research_result, _research_rows
from cli.professional_typer import ProfessionalTyper
from research.options import OptionDomain, OptionResearchWorkflow

strategy_app = ProfessionalTyper(help="Read-only strategy evaluation commands")


@strategy_app.command("evaluate")
def evaluate(
    strategy_name: str = typer.Argument(..., help="Registered strategy name."),
    domain: OptionDomain = typer.Option(OptionDomain.CRYPTO, "--domain"),
    input_file: Path | None = typer.Option(None, "--input-file", help="JSON outcomes."),
    decision_time: str | None = typer.Option(None, "--decision-time", help="Timezone-aware ISO timestamp."),
    json_out: bool = typer.Option(False, "--json", help="Emit structured JSON."),
    output: Path | None = typer.Option(None, "--output", help="Optional report output path."),
) -> None:
    """Evaluate supplied outcomes and classify a strategy conservatively."""
    result = OptionResearchWorkflow().evaluate(
        domain,
        _research_rows(input_file),
        strategy_name=strategy_name,
        decision_time=decision_time,
        persist=False,
    )
    _emit_research_result(result, json_out=json_out, output=output)
