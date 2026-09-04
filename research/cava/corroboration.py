"""Bounded, source-backed corroboration for Cava transcript claims.

OpenBB is the first transport for public macro series.  The direct FRED
endpoint is only a narrowly scoped fallback for environments where an OpenBB
extension is not installed or temporarily fails.  No social feed is required
to complete the workflow.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any

import httpx

from research.core.contracts import EvidenceKind, EvidenceReference, PointInTime


FRED_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"

_TOPICS: dict[str, tuple[str, ...]] = {
    "inflation": ("inflación", "inflacion", "cpi", "precios", "inflation"),
    "rates": ("tipo", "tasas", "fed", "bono", "interés", "interes", "rates", "yield"),
    "dollar": ("dólar", "dolar", "dxy", "dollar"),
    "copper": ("cobre", "copper"),
    "gold": ("oro", "gold"),
    "liquidity": ("liquidez", "liquidity", "tga", "rrp"),
}

_SERIES: dict[str, str] = {
    "inflation": "CPIAUCSL",
    "rates": "DFF",
    "dollar": "DTWEXBGS",
    "copper": "PCOPPUSDM",
    "gold": "GOLDAMGBD228NLBM",
    "liquidity": "WALCL",
}

_UP = re.compile(
    r"\b(alta|alto|sube|subió|subio|aumenta|aumentó|aumento|crece|creció|repunte|rise|rises|rising|increase|increased|high)\b",
    re.IGNORECASE,
)
_DOWN = re.compile(
    r"\b(baja|bajo|bajó|bajo|disminuye|disminuyó|cae|cayó|desciende|reduce|fall|falls|falling|decrease|decreased|low)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class CavaCorroboration:
    """The bounded output of one corroboration pass."""

    evidence: tuple[EvidenceReference, ...] = ()
    indicators: tuple[Mapping[str, Any], ...] = ()
    contradictions: tuple[Mapping[str, Any], ...] = ()
    warnings: tuple[str, ...] = ()
    sources: tuple[str, ...] = ()
    legacy_callback: bool = False


def _topic_for(claim: str) -> str | None:
    lowered = claim.lower()
    for topic, terms in _TOPICS.items():
        if any(term in lowered for term in terms):
            return topic
    return None


def _topics_for(claim: str) -> list[str]:
    lowered = claim.lower()
    return [topic for topic, terms in _TOPICS.items() if any(term in lowered for term in terms)]


def _direction(claim: str) -> str | None:
    up = bool(_UP.search(claim))
    down = bool(_DOWN.search(claim))
    if up == down:
        return None
    return "up" if up else "down"


def _number(value: Any) -> float | None:
    try:
        if value is None or isinstance(value, bool):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _record_value(record: Mapping[str, Any]) -> float | None:
    for key in ("value", "Value", "close", "Close", "last", "observation_value"):
        if key in record:
            parsed = _number(record[key])
            if parsed is not None:
                return parsed
    for value in reversed(list(record.values())):
        parsed = _number(value)
        if parsed is not None:
            return parsed
    return None


def _record_date(record: Mapping[str, Any]) -> str | None:
    for key in ("date", "Date", "period", "observation_date", "timestamp"):
        value = record.get(key)
        if value:
            return str(value)
    return None


def _openbb_fred(series_id: str) -> Mapping[str, Any]:
    """Use the repository's OpenBB adapter without making it import-time hard."""
    try:
        from app.services.openbb import fetch_fred_series
    except ImportError:
        from backend.app.services.openbb import fetch_fred_series
    return fetch_fred_series(series_id)


def _direct_fred(series_id: str, http: httpx.Client) -> Mapping[str, Any]:
    response = http.get(FRED_URL, params={"id": series_id}, timeout=15.0)
    response.raise_for_status()
    lines = response.text.splitlines()
    if not lines:
        raise RuntimeError("FRED returned an empty response")
    headers = [item.strip() for item in lines[0].split(",")]
    records: list[dict[str, Any]] = []
    for line in lines[1:]:
        values = [item.strip() for item in line.split(",")]
        if len(values) != len(headers):
            continue
        record = dict(zip(headers, values))
        if _record_value(record) is not None:
            records.append(record)
    return {"series_id": series_id, "records": records, "as_of": datetime.now(UTC).isoformat()}


@dataclass
class CavaCorroborator:
    """Corroborate recognized macro topics with at most one call per topic."""

    series_fetcher: Callable[[str], Mapping[str, Any]] | None = None
    http: httpx.Client | None = None
    max_topics: int = 6
    _warnings: list[str] = field(default_factory=list, init=False, repr=False)

    def _fetch(self, series_id: str) -> tuple[Mapping[str, Any], str]:
        if self.series_fetcher is not None:
            return self.series_fetcher(series_id), "injected"
        try:
            return _openbb_fred(series_id), "openbb"
        except Exception as openbb_error:  # noqa: BLE001
            try:
                client = self.http or httpx.Client(timeout=15.0)
                return _direct_fred(series_id, client), "fred_direct"
            except Exception as direct_error:  # noqa: BLE001
                raise RuntimeError(
                    f"{series_id}: OpenBB unavailable ({openbb_error}); FRED fallback failed ({direct_error})"
                ) from direct_error

    def __call__(
        self,
        _video: Any,
        claims: list[EvidenceReference],
        decision_time: datetime,
    ) -> CavaCorroboration:
        self._warnings = []
        evidence: list[EvidenceReference] = []
        indicators: list[Mapping[str, Any]] = []
        contradictions: list[Mapping[str, Any]] = []
        sources: list[str] = []
        topics: dict[str, EvidenceReference] = {}
        for claim in claims:
            for topic in _topics_for(claim.claim):
                if topic not in topics and len(topics) < self.max_topics:
                    topics[topic] = claim

        for topic, transcript_claim in topics.items():
            series_id = _SERIES[topic]
            try:
                raw, provider_path = self._fetch(series_id)
                records = raw.get("records") if isinstance(raw, Mapping) else None
                records = records if isinstance(records, list) else []
                observations = [
                    (_record_date(item), _record_value(item))
                    for item in records
                    if isinstance(item, Mapping) and _record_value(item) is not None
                ]
                if not observations:
                    raise RuntimeError("series returned no numeric observations")
                latest_date, latest = observations[-1]
                prior = observations[-2][1] if len(observations) > 1 else None
                source_name = "FRED via OpenBB" if provider_path == "openbb" else "FRED"
                citation = f"https://fred.stlouisfed.org/series/{series_id}"
                source_label = f"{source_name}:{series_id}"
                sources.append(source_label)
                evidence.append(
                    EvidenceReference(
                        reference_id=f"cava-corroboration-{topic}",
                        source=source_label,
                        claim=(
                            f"{series_id} latest observed value is {latest}"
                            + (f" (prior {prior})" if prior is not None else "")
                        ),
                        kind=EvidenceKind.FACT,
                        confidence=0.86 if provider_path == "openbb" else 0.82,
                        point_in_time=PointInTime(
                            event_time=None,
                            available_at=decision_time,
                            decision_time=decision_time,
                        ),
                        citation=citation,
                        metadata={
                            "topic": topic,
                            "series_id": series_id,
                            "observation_date": latest_date,
                            "provider_path": provider_path,
                            "transcript_claim_id": transcript_claim.reference_id,
                            "relationship": "context",
                        },
                    )
                )
                indicator: dict[str, Any] = {
                    "topic": topic,
                    "series_id": series_id,
                    "latest": latest,
                    "prior": prior,
                    "observation_date": latest_date,
                    "source": source_label,
                    "classification": "FACT",
                }
                claim_direction = _direction(transcript_claim.claim)
                if claim_direction and prior is not None and latest != prior:
                    observed_direction = "up" if latest > prior else "down"
                    relationship = "supports" if observed_direction == claim_direction else "contradicts"
                    indicator["relationship"] = relationship
                    if relationship == "contradicts":
                        contradiction = {
                            "topic": topic,
                            "claim_id": transcript_claim.reference_id,
                            "claim": transcript_claim.claim,
                            "source": source_label,
                            "observed_direction": observed_direction,
                            "claimed_direction": claim_direction,
                            "classification": "FACT",
                        }
                        contradictions.append(contradiction)
                        evidence[-1] = replace(
                            evidence[-1],
                            metadata={**evidence[-1].metadata, "relationship": "contradicts"},
                        )
                indicators.append(indicator)
            except Exception as exc:  # noqa: BLE001
                self._warnings.append(f"corroboration unavailable for {topic}: {exc}")

        if not topics:
            self._warnings.append("no recognized macro indicator was present in the transcript")
        return CavaCorroboration(
            evidence=tuple(evidence),
            indicators=tuple(indicators),
            contradictions=tuple(contradictions),
            warnings=tuple(self._warnings),
            sources=tuple(dict.fromkeys(sources)),
        )


__all__ = ["CavaCorroboration", "CavaCorroborator"]
