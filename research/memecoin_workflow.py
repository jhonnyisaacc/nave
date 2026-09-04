"""Point-in-time memecoin discovery, evaluation, and missed-move audits."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping

from research.core.contracts import EvidenceKind, EvidenceReference, PointInTime, ResearchResult, ResearchStatus, RunMetadata
from research.core.store import ResearchStore
from research.memecoin.research_primitives import validate_feature_derivability


STRATEGY_NAME = "memecoin-point-in-time-discovery"
STRATEGY_VERSION = "1.0.0"
FEATURES = ("volume_acceleration", "liquidity", "holder_structure", "wallet_activity", "narrative")


def _time(value: Any, default: datetime) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return default
    return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _asset(row: Mapping[str, Any]) -> str:
    return str(row.get("asset") or row.get("symbol") or row.get("mint") or "").strip()


def _eligible(row: Mapping[str, Any], *, min_volume_acceleration: float, min_liquidity_usd: float) -> tuple[bool, list[str]]:
    decision_time = _time(row.get("decision_time"), datetime.now(UTC))
    available_at = row.get("available_at")
    if not available_at:
        return False, ["unknown_feature_availability"]
    available = _time(available_at, decision_time)
    if available > decision_time:
        return False, ["hindsight_feature_not_available_at_decision"]
    blockers: list[str] = []
    features = row.get("features") if isinstance(row.get("features"), Mapping) else row
    try:
        if float(features.get("volume_acceleration")) < min_volume_acceleration:
            blockers.append("volume_acceleration")
    except (TypeError, ValueError):
        blockers.append("volume_acceleration_missing")
    try:
        if float(features.get("liquidity_usd")) < min_liquidity_usd:
            blockers.append("liquidity")
    except (TypeError, ValueError):
        blockers.append("liquidity_missing")
    if str(features.get("risk_status") or "UNKNOWN").upper() != "PASS":
        blockers.append("safety_or_contract_risk")
    if features.get("holder_structure") in (None, "UNKNOWN"):
        blockers.append("holder_structure")
    if features.get("wallet_activity") in (None, "UNKNOWN"):
        blockers.append("wallet_activity")
    return not blockers, blockers


def discover_rows(
    rows: list[Mapping[str, Any]],
    *,
    min_volume_acceleration: float = 2.0,
    min_liquidity_usd: float = 25_000.0,
) -> dict[str, Any]:
    """Apply a small evidence-backed feature set without using future values."""
    selected: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    derivability = validate_feature_derivability(rows)
    for raw in rows:
        row = dict(raw)
        name = _asset(row)
        if not name:
            rejected.append({"asset": None, "rejection_filters": ["missing_asset_identity"]})
            continue
        passed, blockers = _eligible(
            row,
            min_volume_acceleration=min_volume_acceleration,
            min_liquidity_usd=min_liquidity_usd,
        )
        snapshot = {
            "asset": name,
            "mint": row.get("mint"),
            "decision_time": row.get("decision_time"),
            "available_at": row.get("available_at"),
            "features": dict(row.get("features") or {}),
            "feature_set": list(FEATURES),
            "point_in_time": passed,
        }
        if passed:
            selected.append({
                **snapshot,
                "thesis": "volume acceleration + liquidity + holder structure + wallet activity + narrative",
                "research_only": True,
                "confidence": 0.5,
                "major_risks": ["contract risk and manipulation", "liquidity can disappear", "social/narrative signals are noisy"],
            })
        else:
            rejected.append({**snapshot, "rejection_filters": blockers})
    return {
        "universe_count": len(rows),
        "eligible_count": len(selected),
        "selected": selected,
        "rejected": rejected,
        "feature_set": list(FEATURES),
        "derivability": derivability,
        "decision_time_rule": "available_at <= decision_time; missing/late data is not eligible",
    }


def missed_moves(scan_payload: Mapping[str, Any], outcomes: list[Mapping[str, Any]], *, move_threshold: float) -> list[dict[str, Any]]:
    selected = {str(row.get("asset") or "").lower() for row in scan_payload.get("selected") or []}
    rejected = {str(row.get("asset") or "").lower(): row for row in scan_payload.get("rejected") or []}
    output: list[dict[str, Any]] = []
    for outcome in outcomes:
        asset = _asset(outcome)
        try:
            move = float(outcome.get("later_move_pct", outcome.get("forward_return")))
        except (TypeError, ValueError):
            continue
        if not asset or move < move_threshold or asset.lower() in selected:
            continue
        row = rejected.get(asset.lower())
        decision_time = _time((row or outcome).get("decision_time"), datetime.now(UTC))
        info_time_raw = outcome.get("information_available_at")
        info_time = _time(info_time_raw, decision_time) if info_time_raw else None
        information_state = "UNKNOWN" if info_time is None else (
            "BEFORE_MOVE" if info_time <= decision_time else "AFTER_DECISION"
        )
        output.append({
            "asset": asset,
            "decision_time_snapshot": row,
            "later_move_pct": move,
            "available_information": outcome.get("available_information", row.get("features") if row else None),
            "information_existed_before_move": information_state,
            "universe_membership": outcome.get("universe_membership", row is not None),
            "failed_filter": (row or {}).get("rejection_filters") or ["not_in_observed_universe"],
            "possible_missing_feature": outcome.get("possible_missing_feature") or "inspect the recorded rejected snapshot",
        })
    return output


class MemecoinResearchWorkflow:
    def __init__(self, *, store: ResearchStore | None = None):
        self.store = store or ResearchStore()

    def _result(self, workflow: str, status: ResearchStatus, payload: Mapping[str, Any], *, warnings: list[str] | None = None) -> ResearchResult:
        now = datetime.now(UTC)
        return ResearchResult(
            workflow=workflow,
            status=status,
            metadata=RunMetadata(
                strategy_name=STRATEGY_NAME,
                strategy_version=STRATEGY_VERSION,
                run_id=str(uuid.uuid4()),
                decision_time=now,
                started_at=now,
                completed_at=now,
                input_available_at=now,
            ),
            payload=payload,
            evidence=(EvidenceReference(
                reference_id=f"memecoin-{workflow}",
                source="nave.memecoin.research",
                claim="Memecoin research was calculated from decision-time snapshots",
                kind=EvidenceKind.INFERENCE,
                point_in_time=PointInTime(available_at=now, decision_time=now),
            ),),
            warnings=tuple(warnings or []),
        )

    def discover(self, rows: list[Mapping[str, Any]], *, dune_cache: Path | None = None) -> ResearchResult:
        dune_usage = {
            "mode": "cached" if dune_cache else "local_input",
            "query_executed": False,
            "estimated_credits": 0,
            "actual_credits": 0,
            "source": str(dune_cache) if dune_cache else "caller-provided rows",
        }
        payload_rows = rows
        if dune_cache:
            raw = json.loads(dune_cache.read_text(encoding="utf-8"))
            payload_rows = raw.get("rows", raw) if isinstance(raw, Mapping) else raw
        result_payload = discover_rows(payload_rows)
        result_payload["dune_usage"] = dune_usage
        result_payload["case_study"] = {"asset": "$MEME", "used_as": "one cohort case study only", "overfit_guard": "no asset-specific rule added"}
        status = ResearchStatus.SETUP_FOUND if result_payload["selected"] else ResearchStatus.NO_SETUP
        result = self._result(
            "memecoin.discover",
            status,
            result_payload,
            warnings=["discovery is research-only; no automatic portfolio action"] if result_payload["selected"] else [],
        )
        self.store.save_result(result)
        return result

    def evaluate(self, *, scan_result: ResearchResult, outcomes: list[Mapping[str, Any]]) -> ResearchResult:
        selected = {str(row.get("asset") or "").lower() for row in scan_result.payload.get("selected") or []}
        evaluated = []
        for outcome in outcomes:
            if str(_asset(outcome)).lower() not in selected:
                continue
            try:
                value = float(outcome.get("later_move_pct", outcome.get("forward_return")))
            except (TypeError, ValueError):
                continue
            evaluated.append({**dict(outcome), "forward_move": value, "hit": value > 0})
        hits = sum(bool(row["hit"]) for row in evaluated)
        payload = {
            "strategy": STRATEGY_NAME,
            "strategy_version": scan_result.metadata.strategy_version,
            "evaluated": evaluated,
            "metrics": {
                "selected_count": len(selected),
                "evaluated_count": len(evaluated),
                "hit_rate": hits / len(evaluated) if evaluated else None,
                "mean_forward_move": sum(row["forward_move"] for row in evaluated) / len(evaluated) if evaluated else None,
                "cohort_comparison_required": True,
            },
            "source_scan_run_id": scan_result.metadata.run_id,
        }
        result = self._result(
            "memecoin.evaluate",
            ResearchStatus.STRATEGY_NOT_VALIDATED,
            payload,
            warnings=["evaluation is not validation; compare against cohorts and preserve out-of-sample data"],
        )
        self.store.save_result(result)
        return result

    def missed_moves(self, *, scan_result: ResearchResult, outcomes: list[Mapping[str, Any]], move_threshold: float = 0.50) -> ResearchResult:
        rows = missed_moves(scan_result.payload, outcomes, move_threshold=move_threshold)
        result = self._result(
            "memecoin.missed_moves",
            ResearchStatus.ACTION_REQUIRED if rows else ResearchStatus.NO_SETUP,
            {"strategy": STRATEGY_NAME, "strategy_version": scan_result.metadata.strategy_version, "move_threshold": move_threshold, "missed_moves": rows, "source_scan_run_id": scan_result.metadata.run_id},
            warnings=["future outcomes are audit-only and are not used to alter the decision-time snapshot"],
        )
        self.store.save_result(result)
        return result

    def status(self) -> dict[str, Any]:
        output = {}
        for workflow in ("memecoin.discover", "memecoin.evaluate", "memecoin.missed_moves"):
            result = self.store.load_result(workflow)
            if result:
                output[workflow] = {"status": result.status.value, "run_id": result.metadata.run_id}
        return output
