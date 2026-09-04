"""Normalized public financial-disclosure research source layer."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Iterable, Mapping, Protocol

from research.core.contracts import EvidenceKind, EvidenceReference, PointInTime, ResearchResult, ResearchStatus, RunMetadata
from research.core.store import ResearchStore


class SourceFamily(StrEnum):
    CONGRESS = "congress"
    EXECUTIVE = "executive"


@dataclass(frozen=True)
class NormalizedDisclosure:
    subject_filer: str
    owner: str | None
    asset: str
    transaction_type: str
    transaction_date: str | None
    disclosure_date: str | None
    amount_range: str | None
    source_url_reference: str
    source_family: SourceFamily
    confidence: float | None = None
    timeliness: str = "UNKNOWN"
    filing_lag_days: int | None = None
    unique_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self) | {
            "source_family": self.source_family.value,
        }


class DisclosureProvider(Protocol):
    def fetch(self) -> list[Mapping[str, Any]]: ...


def _clean(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _date(value: Any) -> date | None:
    raw = _clean(value)
    if not raw:
        return None
    try:
        return date.fromisoformat(raw[:10])
    except ValueError:
        return None


def _lag(transaction_date: str | None, disclosure_date: str | None) -> tuple[int | None, str]:
    transaction = _date(transaction_date)
    disclosure = _date(disclosure_date)
    if not transaction or not disclosure:
        return None, "UNKNOWN"
    days = (disclosure - transaction).days
    if days < 0:
        return days, "INVALID_DATE_ORDER"
    if days > 60:
        return days, "STALE"
    if days > 45:
        return days, "DELAYED"
    return days, "TIMELY"


def _stable_id(record: Mapping[str, Any], fields: Mapping[str, Any]) -> str:
    source = _clean(record.get("link") or record.get("source_url") or record.get("source_reference"))
    if source:
        return source
    canonical = "|".join(str(fields.get(key) or "").strip().lower() for key in sorted(fields))
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _normalize(
    record: Mapping[str, Any], *, family: SourceFamily
) -> NormalizedDisclosure | None:
    if family is SourceFamily.CONGRESS:
        first = _clean(record.get("firstName")) or ""
        last = _clean(record.get("lastName")) or ""
        subject = (f"{first} {last}").strip() or _clean(record.get("politician")) or "Unknown"
        asset = _clean(record.get("symbol") or record.get("assetDescription"))
        tx_type = _clean(record.get("type") or record.get("transactionType"))
        owner = _clean(record.get("owner"))
        source = _clean(record.get("link") or record.get("source_url"))
    else:
        subject = _clean(record.get("subject") or record.get("filer") or record.get("name")) or "Unknown"
        asset = _clean(record.get("asset") or record.get("symbol") or record.get("asset_description"))
        tx_type = _clean(record.get("transaction_type") or record.get("type"))
        owner = _clean(record.get("owner"))
        source = _clean(record.get("source_url") or record.get("link") or record.get("source_reference"))
    if not asset or not tx_type:
        return None
    transaction_date = _clean(record.get("transactionDate") or record.get("transaction_date"))
    disclosure_date = _clean(record.get("disclosureDate") or record.get("disclosure_date"))
    fields = {
        "subject": subject,
        "owner": owner,
        "asset": asset,
        "transaction_type": tx_type,
        "transaction_date": transaction_date,
        "disclosure_date": disclosure_date,
        "amount_range": _clean(record.get("amount") or record.get("amount_range")),
        "family": family.value,
    }
    lag, timeliness = _lag(transaction_date, disclosure_date)
    return NormalizedDisclosure(
        subject_filer=subject,
        owner=owner,
        asset=asset.upper(),
        transaction_type=tx_type.upper(),
        transaction_date=transaction_date,
        disclosure_date=disclosure_date,
        amount_range=fields["amount_range"],
        source_url_reference=source or "unlinked-official-record",
        source_family=family,
        confidence=float(record["confidence"]) if isinstance(record.get("confidence"), (int, float)) else None,
        timeliness=timeliness,
        filing_lag_days=lag,
        unique_id=_stable_id(record, fields),
    )


def normalize_congress(record: Mapping[str, Any]) -> NormalizedDisclosure | None:
    return _normalize(record, family=SourceFamily.CONGRESS)


def normalize_executive(record: Mapping[str, Any]) -> NormalizedDisclosure | None:
    return _normalize(record, family=SourceFamily.EXECUTIVE)


def normalize_records(
    records: Iterable[Mapping[str, Any]], *, family: SourceFamily
) -> list[NormalizedDisclosure]:
    output: dict[str, NormalizedDisclosure] = {}
    for record in records:
        normalized = _normalize(record, family=family)
        if normalized:
            output.setdefault(normalized.unique_id, normalized)
    return list(output.values())


class DisclosureWorkflow:
    def __init__(self, *, store: ResearchStore | None = None):
        self.store = store or ResearchStore()

    def sync_payload(
        self,
        *,
        congress_records: Iterable[Mapping[str, Any]] = (),
        executive_records: Iterable[Mapping[str, Any]] = (),
        now: datetime | None = None,
    ) -> ResearchResult:
        decision_time = now or datetime.now(UTC)
        records = normalize_records(congress_records, family=SourceFamily.CONGRESS)
        records.extend(normalize_records(executive_records, family=SourceFamily.EXECUTIVE))
        unique: dict[str, NormalizedDisclosure] = {}
        for record in records:
            unique.setdefault(record.unique_id, record)
        seen_context = self.store.load_context("disclosures_seen") or {}
        seen = {str(item) for item in seen_context.get("unique_ids", [])}
        new_records = [record for record in unique.values() if record.unique_id not in seen]
        next_seen = seen | set(unique)
        self.store.save_context(
            "disclosures_seen",
            {"unique_ids": sorted(next_seen), "updated_at": decision_time.isoformat()},
        )
        evidence = [
            EvidenceReference(
                reference_id=f"disclosure-{record.unique_id}",
                source=record.source_family.value,
                claim=f"Normalized public disclosure for {record.subject_filer} / {record.asset}",
                kind=EvidenceKind.FACT,
                confidence=record.confidence,
                point_in_time=PointInTime(
                    event_time=datetime.combine(_date(record.transaction_date), datetime.min.time(), tzinfo=UTC)
                    if _date(record.transaction_date)
                    else None,
                    available_at=datetime.combine(_date(record.disclosure_date), datetime.min.time(), tzinfo=UTC)
                    if _date(record.disclosure_date)
                    else None,
                    decision_time=decision_time,
                ),
                citation=record.source_url_reference if record.source_url_reference.startswith("http") else None,
                metadata={"source_family": record.source_family.value},
            )
            for record in new_records
        ]
        result = ResearchResult(
            workflow="disclosures.sync",
            status=ResearchStatus.SETUP_FOUND if new_records else ResearchStatus.NO_SETUP,
            metadata=RunMetadata(
                strategy_name="political-disclosures-normalization",
                strategy_version="1.0.0",
                run_id=str(uuid.uuid4()),
                decision_time=decision_time,
                started_at=decision_time,
                completed_at=decision_time,
                input_available_at=decision_time,
            ),
            payload={
                "records": [record.to_dict() for record in new_records],
                "fetched_total": len(unique),
                "new_total": len(new_records),
                "source_families": [family.value for family in SourceFamily],
                "portfolio_candidate_consumed": False,
                "disclosure_is_not_a_buy_signal": True,
            },
            evidence=tuple(evidence),
            warnings=["disclosures are delayed context and require independent portfolio evidence"] if new_records else [],
        )
        self.store.save_result(result)
        return result

    def sync_files(
        self,
        *,
        congress_file: Path | None = None,
        executive_file: Path | None = None,
        now: datetime | None = None,
    ) -> ResearchResult:
        def load(path: Path | None) -> list[Mapping[str, Any]]:
            if path is None:
                return []
            raw = json.loads(path.read_text(encoding="utf-8"))
            rows = raw.get("records", raw) if isinstance(raw, Mapping) else raw
            return [row for row in rows if isinstance(row, Mapping)]

        congress_records = load(congress_file)
        if congress_file is None:
            try:
                from trading.stocks.politicians.provider import FMPPoliticianTradesProvider

                congress_records = [asdict(item) for item in FMPPoliticianTradesProvider().fetch_all()]
            except Exception as exc:  # provider/configuration failures remain explicit state
                result = ResearchResult(
                    workflow="disclosures.sync",
                    status=ResearchStatus.DATA_UNAVAILABLE,
                    metadata=RunMetadata(
                        strategy_name="political-disclosures-normalization",
                        strategy_version="1.0.0",
                        run_id=str(uuid.uuid4()),
                        decision_time=now or datetime.now(UTC),
                        started_at=now or datetime.now(UTC),
                        completed_at=now or datetime.now(UTC),
                        input_available_at=None,
                    ),
                    payload={
                        "records": [],
                        "fetched_total": 0,
                        "new_total": 0,
                        "source_families": [family.value for family in SourceFamily],
                        "portfolio_candidate_consumed": False,
                        "disclosure_is_not_a_buy_signal": True,
                    },
                    warnings=[f"congress provider unavailable: {exc}"],
                )
                self.store.save_result(result)
                return result
        return self.sync_payload(
            congress_records=congress_records,
            executive_records=load(executive_file),
            now=now,
        )
