"""Production context adapters for the deterministic portfolio workflows.

OpenBB is preferred for macro/index series and equity history. Existing
repo-native providers remain explicit fallbacks: yfinance for market history,
FMP for optional company fundamentals, and the official ISM release for the
industry prose that is not an index series.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from typing import Any

import pandas as pd

from trading.stocks.data_provider import FMPClient
from trading.stocks.ism_scraper import ISMReport, ISMReportFetcher
from trading.stocks.price_provider import YFinancePriceProvider
from trading.stocks.universe import DEFAULT_UNIVERSE


def _openbb_fred(series_id: str) -> Mapping[str, Any]:
    try:
        from app.services.openbb import fetch_fred_series
    except ImportError:
        from backend.app.services.openbb import fetch_fred_series
    return fetch_fred_series(series_id)


def _latest_number(records: Sequence[Mapping[str, Any]]) -> float | None:
    for record in reversed(records):
        for key in ("value", "Value", "close", "Close", "last"):
            value = record.get(key)
            try:
                if value is not None:
                    return float(value)
            except (TypeError, ValueError):
                continue
    return None


def _report_payload(report: ISMReport, *, pmi: float | None, pmi_source: str, pmi_as_of: Any) -> dict[str, Any]:
    return {
        "kind": report.kind,
        "report_month": report.report_month,
        "pmi": pmi,
        "pmi_source": pmi_source,
        "pmi_as_of": str(pmi_as_of) if pmi_as_of else None,
        "source_url": report.source_url,
        "source": "official ISM release for industry rankings",
        "hottest_industries": [asdict(item) for item in report.expanding],
        "worst_industries": [asdict(item) for item in report.contracting],
        "industry_rankings": [asdict(item) for item in (*report.expanding, *report.contracting)],
    }


def load_current_ism_inputs(
    *,
    report_fetcher: ISMReportFetcher | None = None,
    fred_fetcher: Callable[[str], Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Fetch current Manufacturing and Services inputs with provenance."""
    report_fetcher = report_fetcher or ISMReportFetcher()
    fred_fetcher = fred_fetcher or _openbb_fred
    series_ids = {"manufacturing": "NAPM", "services": "NMFBAI"}
    outputs: dict[str, Any] = {"provider": "OpenBB/FRED + official ISM release", "warnings": []}
    for kind, series_id in series_ids.items():
        report = report_fetcher.fetch_report(kind=kind)  # type: ignore[arg-type]
        pmi = report.pmi
        pmi_source = "official ISM release"
        pmi_as_of = report.report_month
        try:
            fred = fred_fetcher(series_id)
            records = fred.get("records") if isinstance(fred, Mapping) else None
            if isinstance(records, list):
                current = _latest_number([item for item in records if isinstance(item, Mapping)])
                if current is not None:
                    pmi = current
                    pmi_source = f"{series_id} via OpenBB/FRED"
                    pmi_as_of = fred.get("as_of")
        except Exception as exc:  # noqa: BLE001
            outputs["warnings"].append(f"{kind} headline PMI OpenBB unavailable: {exc}")
        outputs[kind] = _report_payload(report, pmi=pmi, pmi_source=pmi_source, pmi_as_of=pmi_as_of)
    return outputs


def _records_to_series(records: Sequence[Mapping[str, Any]]) -> pd.Series:
    values: list[tuple[pd.Timestamp, float]] = []
    for record in records:
        date_value = record.get("date") or record.get("Date") or record.get("timestamp")
        value = record.get("close") or record.get("Close") or record.get("value") or record.get("last")
        try:
            if date_value is None or value is None:
                continue
            timestamp = pd.Timestamp(date_value)
            timestamp = timestamp.tz_localize("UTC") if timestamp.tzinfo is None else timestamp.tz_convert("UTC")
            values.append((timestamp, float(value)))
        except (TypeError, ValueError):
            continue
    if not values:
        return pd.Series(dtype=float)
    return pd.Series(dict(values)).sort_index()


def _sector_for(ticker: str) -> str | None:
    for sector, tickers in DEFAULT_UNIVERSE.items():
        if ticker.upper() in {str(item).upper() for item in tickers}:
            return sector
    return None


class PortfolioContextProvider:
    """Acquire current review evidence without embedding it in decision rules."""

    def __init__(
        self,
        *,
        history_fetcher: Callable[[str, str | None, str | None], Mapping[str, Any]] | None = None,
        price_provider: YFinancePriceProvider | None = None,
        fundamentals: FMPClient | None = None,
    ) -> None:
        self.history_fetcher = history_fetcher or self._openbb_history
        self.price_provider = price_provider or YFinancePriceProvider()
        self.fundamentals = fundamentals or FMPClient()

    @staticmethod
    def _openbb_history(symbol: str, start: str | None, end: str | None) -> Mapping[str, Any]:
        try:
            from app.services.openbb import fetch_equity_history
        except ImportError:
            from backend.app.services.openbb import fetch_equity_history
        return fetch_equity_history(symbol, start, end)

    def _history(self, ticker: str, now: datetime) -> tuple[pd.Series, str, str | None]:
        start = (now.date() - timedelta(days=90)).isoformat()
        try:
            payload = self.history_fetcher(ticker, start, now.date().isoformat())
            records = payload.get("records") if isinstance(payload, Mapping) else None
            if isinstance(records, list):
                series = _records_to_series([item for item in records if isinstance(item, Mapping)])
                if not series.empty:
                    return series, "OpenBB equity.price.historical (yfinance)", str(payload.get("as_of") or now.isoformat())
        except Exception:
            pass
        try:
            rows = self.price_provider.fetch_daily_closes(
                [ticker], start=now.date() - timedelta(days=90), end=now.date()
            )
            series = rows.get(ticker.upper(), pd.Series(dtype=float))
            if not series.empty:
                return series.astype(float), "repo YFinancePriceProvider", now.isoformat()
        except Exception:
            pass
        return pd.Series(dtype=float), "unavailable", None

    def build_review_context(
        self,
        tickers: Sequence[str],
        *,
        macro_context: Mapping[str, Any] | None = None,
        now: datetime | None = None,
    ) -> dict[str, dict[str, Any]]:
        observed_at = (now or datetime.now(UTC)).astimezone(UTC)
        output: dict[str, dict[str, Any]] = {}
        macro_validated = bool(macro_context and macro_context.get("validated") is True)
        for raw_ticker in tickers:
            ticker = str(raw_ticker).upper()
            series, price_source, price_as_of = self._history(ticker, observed_at)
            current = float(series.iloc[-1]) if not series.empty else None
            moving_average = float(series.tail(20).mean()) if len(series) >= 2 else None
            return_20d = float(series.iloc[-1] / series.iloc[-min(len(series), 20)] - 1) if len(series) >= 2 else None
            technical = "unknown"
            if current is not None and moving_average is not None:
                technical = "weak" if current < moving_average * 0.95 or (return_20d is not None and return_20d < -0.10) else "healthy"
            try:
                fundamentals = self.fundamentals.fundamentals(ticker)
                fundamentals_payload = asdict(fundamentals)
                fundamental_source = "FMP with repo yfinance enrichment"
            except Exception as exc:  # noqa: BLE001
                fundamentals_payload = {"symbol": ticker, "sector": _sector_for(ticker), "unavailable_reason": str(exc)}
                fundamental_source = "unavailable"
            sector = fundamentals_payload.get("sector") or _sector_for(ticker)
            output[ticker] = {
                "macro_regime": "neutral" if macro_validated else "unknown",
                "macro_context_status": "VALIDATED" if macro_validated else "UNKNOWN",
                "company_information": fundamentals_payload,
                "sector_context": {"sector": sector, "source": fundamental_source},
                "market_state": {"current_price": current, "source": price_source, "as_of": price_as_of},
                "technical_condition": technical,
                "technical": {"moving_average_20": moving_average, "return_20d": return_20d},
                "fundamentals": fundamentals_payload,
                "sources": [source for source in (price_source, fundamental_source) if source != "unavailable"],
                "freshness": {"observed_at": observed_at.isoformat(), "price_as_of": price_as_of},
                "meaningful_new_information": False,
            }
        return output


__all__ = ["PortfolioContextProvider", "load_current_ism_inputs"]
