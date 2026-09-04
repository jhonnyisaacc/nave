"""Unified Nave CLI entrypoint.

The command implementation lives in modular command groups under cli/commands.
This module only assembles the application surface.
"""

from __future__ import annotations

import typer

from cli.commands.congress import congress_app
from cli.commands.crypto import crypto_app
from cli.commands.daily import daily_app
from cli.commands.core import api_app, data_app, mcp_app, trading_app
from cli.commands.cot import cot_app
from cli.commands.hermes import hermes_app
from cli.commands.memecoin import memecoin_app
from cli.commands.options import options_app
from cli.commands.portfolio import portfolio_app
from cli.commands.research import research_app
from cli.commands.stocks import stocks_app
from cli.commands.wallet import wallet_app
from cli.professional_typer import ProfessionalTyper

app = ProfessionalTyper(
    name="nave",
    help="Nave - Professional macro trading and data platform CLI",
    add_completion=True,
)

app.add_typer(daily_app, name="daily")
app.add_typer(congress_app, name="congress")
app.add_typer(data_app, name="data")
app.add_typer(crypto_app, name="crypto")
app.add_typer(trading_app, name="trading")
app.add_typer(api_app, name="api")
app.add_typer(mcp_app, name="mcp")
app.add_typer(cot_app, name="cot")
app.add_typer(hermes_app, name="hermes")
app.add_typer(stocks_app, name="stocks")
app.add_typer(memecoin_app, name="memecoin")
app.add_typer(options_app, name="options")
app.add_typer(portfolio_app, name="portfolio")
app.add_typer(research_app, name="research")
app.add_typer(wallet_app, name="wallet")


@app.command("version")
def version() -> None:
    """Show Nave version."""
    typer.echo("Nave v0.1.0 (refactored with modular CLI)")


if __name__ == "__main__":
    app()
