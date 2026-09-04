"""Small atomic JSON store for research results and shared contexts."""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from research.core.contracts import ResearchResult


def default_state_root() -> Path:
    configured = os.getenv("NAVE_RESEARCH_STATE_DIR")
    return Path(configured).expanduser() if configured else Path.home() / ".nave" / "research"


def _safe_name(value: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_.-]+", "_", value.strip())
    if not cleaned:
        raise ValueError("state name must contain at least one safe character")
    return cleaned


class ResearchStore:
    """Persist only explicit research artifacts; never performs execution."""

    def __init__(self, root: Path | None = None):
        self.root = (root or default_state_root()).expanduser()
        self.results_root = self.root / "results"
        self.context_root = self.root / "contexts"

    @staticmethod
    def _atomic_write(path: Path, payload: Mapping[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, ensure_ascii=False, default=str)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)

    def save_result(self, result: ResearchResult) -> Path:
        payload = result.to_dict()
        name = _safe_name(result.workflow)
        path = self.results_root / f"{name}.json"
        self._atomic_write(path, payload)
        return path

    def load_result(self, workflow: str) -> ResearchResult | None:
        path = self.results_root / f"{_safe_name(workflow)}.json"
        if not path.exists():
            return None
        return ResearchResult.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def list_results(self, *, workflow: str | None = None) -> list[Mapping[str, Any]]:
        if workflow:
            result = self.load_result(workflow)
            return [result.to_dict()] if result else []
        if not self.results_root.exists():
            return []
        return [
            json.loads(path.read_text(encoding="utf-8"))
            for path in sorted(self.results_root.glob("*.json"))
        ]

    def save_context(self, name: str, payload: Mapping[str, Any]) -> Path:
        path = self.context_root / f"{_safe_name(name)}.json"
        self._atomic_write(path, payload)
        return path

    def load_context(self, name: str) -> Mapping[str, Any] | None:
        path = self.context_root / f"{_safe_name(name)}.json"
        if not path.exists():
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, Mapping):
            raise ValueError(f"context {name!r} must contain a JSON object")
        return value
