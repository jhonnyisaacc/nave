"""Point-in-time crypto universe contracts for research replay.

This module deliberately contains no exchange client or execution code.  A
universe snapshot is selected by both observation time and source availability
time so a current ranking cannot silently become a historical ranking.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Protocol


TOP_UNIVERSE_SOURCES = frozenset(
    {
        "historical_top_market_cap",
        "historical_top_100",
        "top_100",
        "current_top_market_cap",
    }
)


class UniverseProviderUnavailable(RuntimeError):
    """Raised when no point-in-time universe snapshot is available."""


def identity_key_for(
    canonical_asset_id: str | None, contract_address: str | None
) -> str | None:
    """Build the stable identity key used across universe and market data rows."""
    if canonical_asset_id:
        return f"asset:{canonical_asset_id.lower()}"
    if contract_address:
        return f"contract:{contract_address.lower()}"
    return None


def as_utc(value: datetime | str) -> datetime:
    """Parse a timestamp and normalize it to timezone-aware UTC."""
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True)
class UniverseMember:
    """One asset/contract observation from a point-in-time universe."""

    symbol: str
    canonical_asset_id: str | None
    contract_address: str | None
    venue: str | None
    contract_symbol: str | None
    quote_currency: str | None
    observation_timestamp: datetime
    source_timestamp: datetime | None
    available_at: datetime | None
    universe_source: str
    data_completeness: str
    missingness_reason: str | None = None
    rank: int | None = None
    exchange_contract_type: str = "perpetual"

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", self.symbol.upper())
        object.__setattr__(self, "observation_timestamp", as_utc(self.observation_timestamp))
        if self.source_timestamp is not None:
            object.__setattr__(self, "source_timestamp", as_utc(self.source_timestamp))
        if self.available_at is not None:
            object.__setattr__(self, "available_at", as_utc(self.available_at))

    @property
    def identity_key(self) -> str | None:
        """Return canonical identity; ticker-only records intentionally return None."""
        return identity_key_for(self.canonical_asset_id, self.contract_address)

    @property
    def is_top_ranked(self) -> bool:
        return self.universe_source in TOP_UNIVERSE_SOURCES and self.rank is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "canonical_asset_id": self.canonical_asset_id,
            "contract_address": self.contract_address,
            "venue": self.venue,
            "contract_symbol": self.contract_symbol,
            "quote_currency": self.quote_currency,
            "observation_timestamp": self.observation_timestamp.isoformat(),
            "source_timestamp": self.source_timestamp.isoformat() if self.source_timestamp else None,
            "available_at": self.available_at.isoformat() if self.available_at else None,
            "universe_source": self.universe_source,
            "data_completeness": self.data_completeness,
            "missingness_reason": self.missingness_reason,
            "rank": self.rank,
            "exchange_contract_type": self.exchange_contract_type,
        }


@dataclass(frozen=True)
class UniverseSnapshot:
    observation_timestamp: datetime
    source_timestamp: datetime | None
    available_at: datetime | None
    source: str
    members: tuple[UniverseMember, ...]
    point_in_time_valid: bool = True
    validity_note: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "observation_timestamp", as_utc(self.observation_timestamp))
        if self.source_timestamp is not None:
            object.__setattr__(self, "source_timestamp", as_utc(self.source_timestamp))
        if self.available_at is not None:
            object.__setattr__(self, "available_at", as_utc(self.available_at))

    def to_dict(self) -> dict[str, Any]:
        return {
            "observation_timestamp": self.observation_timestamp.isoformat(),
            "source_timestamp": self.source_timestamp.isoformat() if self.source_timestamp else None,
            "available_at": self.available_at.isoformat() if self.available_at else None,
            "source": self.source,
            "point_in_time_valid": self.point_in_time_valid,
            "validity_note": self.validity_note,
            "members": [member.to_dict() for member in self.members],
        }


class PointInTimeUniverseProvider(Protocol):
    def snapshot_at(self, observation_timestamp: datetime, *, universe_size: int) -> UniverseSnapshot:
        """Return the latest snapshot available by the observation timestamp."""


class CurrentUniverseProvider:
    """Point-in-time provider for one explicitly observed current snapshot.

    The provider is deliberately constructed from already-observed market-cap
    rows and exchange metadata.  It does not fetch data and cannot be used to
    turn a current snapshot into historical membership.
    """

    def __init__(self, snapshot: UniverseSnapshot) -> None:
        self.snapshot = snapshot

    @classmethod
    def from_market_cap_rows(
        cls,
        rows: list[dict[str, Any]],
        perpetual_contracts: list[dict[str, Any]],
        *,
        observation_timestamp: datetime,
        source: str = "current_market_cap_and_exchange_metadata",
    ) -> "CurrentUniverseProvider":
        observed_at = as_utc(observation_timestamp)
        valid_rows = [row for row in rows if isinstance(row, dict) and row.get("id")]
        valid_rows.sort(
            key=lambda row: (
                int(row["market_cap_rank"])
                if str(row.get("market_cap_rank", "")).isdigit()
                else 10**9,
                str(row.get("id")),
            )
        )
        contracts_by_symbol: dict[str, list[dict[str, Any]]] = {}
        for contract in perpetual_contracts:
            if not isinstance(contract, dict) or not contract.get("name"):
                continue
            contracts_by_symbol.setdefault(str(contract["name"]).upper(), []).append(contract)
        ids_by_symbol: dict[str, list[str]] = {}
        for row in valid_rows:
            ids_by_symbol.setdefault(str(row.get("symbol") or "").upper(), []).append(
                str(row["id"])
            )

        members: list[UniverseMember] = []
        for position, row in enumerate(valid_rows, start=1):
            symbol = str(row.get("symbol") or "").upper()
            contract = _contract_for_row(row, contracts_by_symbol.get(symbol, ()), ids_by_symbol)
            rank = int(row["market_cap_rank"]) if str(row.get("market_cap_rank", "")).isdigit() else position
            members.append(
                UniverseMember(
                    symbol=symbol,
                    canonical_asset_id=str(row["id"]),
                    contract_address=row.get("contract_address")
                    or (contract.get("contract_address") if contract else None),
                    venue="hyperliquid" if contract else None,
                    contract_symbol=str(contract["name"]) if contract else None,
                    quote_currency=str(contract.get("quoteCurrency") or "USDC") if contract else "USD",
                    observation_timestamp=observed_at,
                    source_timestamp=observed_at,
                    available_at=observed_at,
                    universe_source="current_top_market_cap",
                    data_completeness="complete",
                    rank=rank,
                )
            )

        known_top_ids = {member.canonical_asset_id for member in members}
        for symbol, contracts in sorted(contracts_by_symbol.items()):
            for contract in contracts:
                # A contract outside the market-cap response is retained as an
                # unresolved ticker unless the source supplies a canonical ID.
                canonical_id = contract.get("canonical_asset_id")
                if canonical_id is None and len(ids_by_symbol.get(symbol, [])) == 1:
                    canonical_id = ids_by_symbol[symbol][0]
                if canonical_id in known_top_ids:
                    continue
                members.append(
                    UniverseMember(
                        symbol=symbol,
                        canonical_asset_id=str(canonical_id) if canonical_id else None,
                        contract_address=contract.get("contract_address"),
                        venue="hyperliquid",
                        contract_symbol=str(contract["name"]),
                        quote_currency=str(contract.get("quoteCurrency") or "USDC"),
                        observation_timestamp=observed_at,
                        source_timestamp=observed_at,
                        available_at=observed_at,
                        universe_source="liquid_perpetual",
                        data_completeness="complete" if canonical_id else "incomplete",
                        missingness_reason=None if canonical_id else "canonical_asset_id_unresolved",
                    )
                )

        return cls(
            UniverseSnapshot(
                observation_timestamp=observed_at,
                source_timestamp=observed_at,
                available_at=observed_at,
                source=source,
                members=tuple(members),
            )
        )

    def snapshot_at(self, observation_timestamp: datetime, *, universe_size: int) -> UniverseSnapshot:
        if universe_size <= 0:
            raise ValueError("universe_size must be positive")
        as_of = as_utc(observation_timestamp)
        if (
            self.snapshot.observation_timestamp > as_of
            or (self.snapshot.source_timestamp is not None and self.snapshot.source_timestamp > as_of)
            or (self.snapshot.available_at is not None and self.snapshot.available_at > as_of)
        ):
            raise UniverseProviderUnavailable(
                f"current universe snapshot is not available at {as_of.isoformat()}"
            )
        top_ranked = [
            member
            for member in self.snapshot.members
            if member.is_top_ranked and member.rank <= universe_size
        ]
        liquid_perpetuals = [
            member for member in self.snapshot.members if member.universe_source == "liquid_perpetual"
        ]
        return replace(self.snapshot, members=tuple(top_ranked + liquid_perpetuals))


def _contract_for_row(
    row: dict[str, Any],
    contracts: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    ids_by_symbol: dict[str, list[str]],
) -> dict[str, Any] | None:
    """Join a market-cap row to a perp only when the identity is unambiguous."""
    if not contracts:
        return None
    row_id = str(row.get("id") or "").lower()
    row_address = str(row.get("contract_address") or "").lower()
    explicit = [
        contract
        for contract in contracts
        if (
            contract.get("canonical_asset_id")
            and str(contract["canonical_asset_id"]).lower() == row_id
        )
        or (
            contract.get("contract_address")
            and row_address
            and str(contract["contract_address"]).lower() == row_address
        )
    ]
    if len(explicit) == 1:
        return explicit[0]
    symbol = str(row.get("symbol") or "").upper()
    if len(contracts) == 1 and len(ids_by_symbol.get(symbol, [])) == 1:
        return contracts[0]
    return None

def _member_from_payload(payload: dict[str, Any], snapshot: dict[str, Any]) -> UniverseMember:
    observation = payload.get("observation_timestamp", snapshot.get("observation_timestamp"))
    source_timestamp = payload.get("source_timestamp", snapshot.get("source_timestamp"))
    available_at = payload.get("available_at", snapshot.get("available_at"))
    return UniverseMember(
        symbol=str(payload.get("symbol") or ""),
        canonical_asset_id=payload.get("canonical_asset_id"),
        contract_address=payload.get("contract_address"),
        venue=payload.get("venue"),
        contract_symbol=payload.get("contract_symbol"),
        quote_currency=payload.get("quote_currency"),
        observation_timestamp=as_utc(observation),
        source_timestamp=as_utc(source_timestamp) if source_timestamp else None,
        available_at=as_utc(available_at) if available_at else None,
        universe_source=str(payload.get("universe_source") or "historical_top_market_cap"),
        data_completeness=str(payload.get("data_completeness") or "complete"),
        missingness_reason=payload.get("missingness_reason"),
        rank=int(payload["rank"]) if payload.get("rank") is not None else None,
        exchange_contract_type=str(payload.get("exchange_contract_type") or "perpetual"),
    )


class FixtureUniverseProvider:
    """Deterministic local replay provider.

    The fixture may contain current snapshots, but they are only eligible when
    their observation and availability timestamps are both at or before the
    requested observation time.  No fallback to the newest snapshot exists.
    """

    def __init__(self, snapshots: list[UniverseSnapshot], *, fixture_source: str = "offline_fixture") -> None:
        self.snapshots = tuple(sorted(snapshots, key=lambda item: item.observation_timestamp))
        self.fixture_source = fixture_source

    @classmethod
    def from_path(cls, path: str | Path) -> "FixtureUniverseProvider":
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        raw_snapshots = payload.get("universe_snapshots")
        if not isinstance(raw_snapshots, list):
            raise ValueError("fixture must contain a universe_snapshots list")
        snapshots: list[UniverseSnapshot] = []
        for raw in raw_snapshots:
            if not isinstance(raw, dict) or not raw.get("observation_timestamp"):
                raise ValueError("each universe snapshot needs observation_timestamp")
            members = tuple(
                _member_from_payload(member, raw)
                for member in raw.get("members", [])
                if isinstance(member, dict)
            )
            snapshots.append(
                UniverseSnapshot(
                    observation_timestamp=as_utc(raw["observation_timestamp"]),
                    source_timestamp=as_utc(raw["source_timestamp"])
                    if raw.get("source_timestamp")
                    else None,
                    available_at=as_utc(raw["available_at"]) if raw.get("available_at") else None,
                    source=str(raw.get("source") or "offline_fixture"),
                    members=members,
                    point_in_time_valid=bool(raw.get("point_in_time_valid", True)),
                    validity_note=raw.get("validity_note"),
                )
            )
        metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
        return cls(snapshots, fixture_source=str(metadata.get("source") or "offline_fixture"))

    def snapshot_at(self, observation_timestamp: datetime, *, universe_size: int) -> UniverseSnapshot:
        if universe_size <= 0:
            raise ValueError("universe_size must be positive")
        as_of = as_utc(observation_timestamp)
        available = [
            snapshot
            for snapshot in self.snapshots
            if snapshot.observation_timestamp <= as_of
            and (snapshot.source_timestamp is None or snapshot.source_timestamp <= as_of)
            and (snapshot.available_at is None or snapshot.available_at <= as_of)
        ]
        if not available:
            raise UniverseProviderUnavailable(
                f"no point-in-time universe snapshot available at {as_of.isoformat()}"
            )
        snapshot = max(available, key=lambda item: item.observation_timestamp)
        top_ranked = [
            member
            for member in snapshot.members
            if member.universe_source in TOP_UNIVERSE_SOURCES
            and member.rank is not None
            and member.rank <= universe_size
        ]
        liquid_perpetuals = [
            member
            for member in snapshot.members
            if member.universe_source == "liquid_perpetual"
        ]
        members = tuple(top_ranked + liquid_perpetuals)
        valid = snapshot.point_in_time_valid and all(
            member.observation_timestamp <= as_of
            and (member.source_timestamp is None or member.source_timestamp <= as_of)
            and (member.available_at is None or member.available_at <= as_of)
            for member in members
        )
        return replace(
            snapshot,
            members=members,
            point_in_time_valid=valid,
            validity_note=snapshot.validity_note
            or (None if valid else "snapshot or member availability is after observation time"),
        )


def deduplicate_members(members: list[UniverseMember] | tuple[UniverseMember, ...]) -> tuple[UniverseMember, ...]:
    """Deduplicate across venues by canonical ID/address, never by ticker alone."""
    selected: dict[str, UniverseMember] = {}
    unresolved: list[UniverseMember] = []
    for member in members:
        key = member.identity_key
        if key is None:
            unresolved.append(member)
            continue
        current = selected.get(key)
        if current is None or _member_preference(member) < _member_preference(current):
            selected[key] = member
    # Ticker-only observations remain separate so ambiguous symbols are visible.
    output = list(selected.values()) + unresolved
    return tuple(
        sorted(
            output,
            key=lambda item: (
                item.rank is None,
                item.rank if item.rank is not None else 10**9,
                item.symbol,
                item.canonical_asset_id or "",
                item.venue or "",
            ),
        )
    )


def _member_preference(member: UniverseMember) -> tuple[int, str, str]:
    # Prefer top-100 evidence, then deterministic venue ordering.
    source_rank = 0 if member.is_top_ranked else 1
    return source_rank, member.venue or "", member.contract_symbol or ""


def members_for_target(
    members: tuple[UniverseMember, ...], target: str
) -> tuple[UniverseMember, ...]:
    """Resolve a target by canonical ID/address first, then ticker."""
    normalized = target.strip().upper()
    exact_identity = [
        member
        for member in members
        if member.canonical_asset_id and member.canonical_asset_id.upper() == target.strip().upper()
        or member.contract_address and member.contract_address.upper() == target.strip().upper()
    ]
    if exact_identity:
        return tuple(exact_identity)
    return tuple(member for member in members if member.symbol == normalized)
