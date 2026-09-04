"""Live CFTC/OpenBB market-regime context for crypto research."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime
from typing import Any

from trading.crypto.cot.cot_analyzer import COTAnalyzer
from trading.crypto.cot.cot_fetcher import fetch_latest_cot


def _date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
        except ValueError:
            try:
                return date.fromisoformat(value[:10])
            except ValueError:
                return None
    return None


class COTContextProvider:
    """Fetch BTC/ETH COT once and expose it as market context, not alt signals."""

    def __init__(
        self,
        *,
        fetcher: Callable[[], Mapping[str, Any]] | None = None,
        analyzer: COTAnalyzer | None = None,
        stale_after_days: int = 14,
    ) -> None:
        self.fetcher = fetcher or fetch_latest_cot
        self.analyzer = analyzer or COTAnalyzer()
        self.stale_after_days = stale_after_days

    def fetch(self, *, now: datetime | None = None) -> dict[str, Any]:
        observed_at = (now or datetime.now(UTC)).astimezone(UTC)
        try:
            raw = self.fetcher()
            if not isinstance(raw, Mapping) or not raw:
                raise RuntimeError("CFTC/OpenBB returned no COT markets")
            biases = self.analyzer.analyze(dict(raw))
        except Exception as exc:  # noqa: BLE001
            return {
                "status": "UNAVAILABLE",
                "regime": "unknown",
                "scope": "market/regime context; no per-altcoin COT signal",
                "source": "CFTC via OpenBB with official CFTC fallback",
                "as_of_date": None,
                "release_date": None,
                "freshness_days": None,
                "warnings": [f"COT unavailable: {exc}"],
                "markets": {},
            }

        markets: dict[str, Any] = {}
        as_of_dates: list[date] = []
        release_dates: list[date] = []
        for asset, bias in biases.items():
            payload = raw.get(asset) if isinstance(raw.get(asset), Mapping) else {}
            as_of = _date(payload.get("as_of_date") or payload.get("latest_date"))
            release = _date(payload.get("release_date"))
            if as_of:
                as_of_dates.append(as_of)
            if release:
                release_dates.append(release)
            markets[str(asset).upper()] = {
                "bias": bias.bias,
                "confidence": bias.confidence,
                "historical_percentile": bias.historical_percentile,
                "as_of_date": as_of.isoformat() if as_of else None,
                "release_date": release.isoformat() if release else None,
                "source": str(payload.get("source") or bias.metadata.get("source") or "CFTC"),
            }

        freshest = min(as_of_dates) if as_of_dates else None
        freshness_days = (observed_at.date() - freshest).days if freshest else None
        stale = freshness_days is None or freshness_days > self.stale_after_days
        directions = {item["bias"] for item in markets.values()}
        if directions == {"bullish"} and len(markets) >= 2:
            regime = "bullish"
        elif directions == {"bearish"} and len(markets) >= 2:
            regime = "bearish"
        elif markets:
            regime = "neutral"
        else:
            regime = "unknown"
        return {
            "status": "STALE" if stale else "OK",
            "regime": regime if not stale else "unknown",
            "scope": "market/regime context; no per-altcoin COT signal",
            "source": "CFTC via OpenBB with official CFTC fallback",
            "as_of_date": freshest.isoformat() if freshest else None,
            "release_date": max(release_dates).isoformat() if release_dates else None,
            "freshness_days": freshness_days,
            "markets": markets,
            "warnings": (["COT report is stale; no directional COT gate was applied"] if stale else []),
        }


__all__ = ["COTContextProvider"]
