from datetime import UTC, datetime

import pandas as pd

from research.portfolio_providers import PortfolioContextProvider, load_current_ism_inputs
from trading.stocks.ism_scraper import ISMIndustryRanking, ISMReport


NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)


class ReportFetcher:
    def fetch_report(self, *, kind):
        return ISMReport(
            kind=kind,
            report_month="September 2026",
            pmi=51.0,
            expanding=[ISMIndustryRanking("Software", "expanding", 1, "Information Technology")],
            contracting=[],
            source_url=f"https://ism.test/{kind}",
        )


def test_ism_inputs_prefer_openbb_fred_for_headline_and_keep_official_ranking():
    result = load_current_ism_inputs(
        report_fetcher=ReportFetcher(),
        fred_fetcher=lambda series: {"records": [{"date": "2026-09-03", "value": 53.2}], "as_of": "2026-09-04"},
    )
    assert result["manufacturing"]["pmi"] == 53.2
    assert result["manufacturing"]["pmi_source"] == "NAPM via OpenBB/FRED"
    assert result["manufacturing"]["hottest_industries"][0]["industry"] == "Software"


def test_portfolio_context_uses_openbb_history_and_keeps_missing_fundamentals_truthful():
    def history(symbol, _start, _end):
        return {
            "records": [
                {"date": "2026-08-01", "close": 100},
                {"date": "2026-09-04", "close": 110},
            ],
            "as_of": "2026-09-04T12:00:00+00:00",
        }

    class Fundamentals:
        def fundamentals(self, symbol):
            raise RuntimeError("provider offline")

    result = PortfolioContextProvider(history_fetcher=history, fundamentals=Fundamentals()).build_review_context(
        ["AAPL"], now=NOW
    )
    assert result["AAPL"]["market_state"]["current_price"] == 110
    assert result["AAPL"]["technical_condition"] == "healthy"
    assert result["AAPL"]["company_information"]["unavailable_reason"] == "provider offline"
