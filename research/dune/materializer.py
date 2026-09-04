"""Bounded Dune query materialization with reusable local cache."""

from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping


def _find_number(value: Any, names: tuple[str, ...]) -> float | None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if str(key).lower() in names:
                try:
                    return float(item)
                except (TypeError, ValueError):
                    pass
            found = _find_number(item, names)
            if found is not None:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _find_number(item, names)
            if found is not None:
                return found
    return None


def _rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, Mapping):
        for key in ("rows", "data", "result"):
            if key in payload:
                return _rows(payload[key])
        return [dict(payload)]
    if isinstance(payload, list):
        return [dict(row) for row in payload if isinstance(row, Mapping)]
    return []


class DuneMaterializer:
    """Run at most one bounded Dune CLI query and persist its envelope."""

    def __init__(self, *, executable: str = "dune", timeout_seconds: int = 120) -> None:
        self.executable = executable
        self.timeout_seconds = timeout_seconds

    @staticmethod
    def query_identity(query_id: str, query_text: str | None = None) -> str:
        digest = hashlib.sha256((query_text or "").encode("utf-8")).hexdigest()[:16] if query_text else ""
        return f"{query_id}:{digest}" if digest else query_id

    def materialize(
        self,
        *,
        query_id: str,
        output: Path,
        limit: int = 10_000,
        force: bool = False,
        query_text: str | None = None,
    ) -> dict[str, Any]:
        if not query_id.strip():
            raise ValueError("query_id is required")
        if limit < 1 or limit > 100_000:
            raise ValueError("limit must be between 1 and 100000")
        identity = self.query_identity(query_id, query_text)
        if output.exists() and not force:
            cached = json.loads(output.read_text(encoding="utf-8"))
            if isinstance(cached, Mapping) and cached.get("query_identity") == identity:
                return {**dict(cached), "cache_hit": True, "query_executed": False}

        command = [self.executable, "query", "run", query_id, "--limit", str(limit), "-o", "json"]
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
            timeout=self.timeout_seconds,
        )
        if completed.returncode != 0:
            raise RuntimeError(completed.stderr.strip() or f"Dune query exited {completed.returncode}")
        try:
            raw = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError("Dune CLI returned non-JSON output") from exc
        envelope = {
            "schema_version": 1,
            "provider": "dune",
            "mode": "remote_materialized",
            "query_id": query_id,
            "query_identity": identity,
            "execution_id": raw.get("execution_id") if isinstance(raw, Mapping) else None,
            "fetched_at": datetime.now(UTC).isoformat(),
            "rows": _rows(raw),
            "row_count": len(_rows(raw)),
            "query_executed": True,
            "cache_hit": False,
            "credit_usage": {
                "actual": _find_number(raw, ("credits", "credits_used", "compute_credits", "credit_usage")),
                "estimated": _find_number(raw, ("estimated_credits", "estimated_credit_usage")),
                "source": "Dune CLI response; null means the response did not report usage",
            },
        }
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(envelope, indent=2, default=str) + "\n", encoding="utf-8")
        return envelope


__all__ = ["DuneMaterializer"]
