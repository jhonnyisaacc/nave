"""Read-only shared context interfaces for downstream NAVE workflows."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol

from research.core.store import ResearchStore


class ResearchContext(Protocol):
    """Context surface that keeps downstream workflows provider-agnostic."""

    def latest_macro_context(self) -> Mapping[str, Any] | None: ...

    def latest_cava_context(self) -> Mapping[str, Any] | None: ...

    def portfolio_state(self) -> Mapping[str, Any] | None: ...

    def strategy_results(self, workflow: str | None = None) -> list[Mapping[str, Any]]: ...


class FileResearchContext:
    """Read-only context backed by a :class:`ResearchStore`."""

    def __init__(self, root: Path | None = None):
        self.store = ResearchStore(root)

    def latest_macro_context(self) -> Mapping[str, Any] | None:
        return self.store.load_context("macro")

    def latest_cava_context(self) -> Mapping[str, Any] | None:
        return self.store.load_context("cava")

    def portfolio_state(self) -> Mapping[str, Any] | None:
        return self.store.load_context("portfolio")

    def strategy_results(self, workflow: str | None = None) -> list[Mapping[str, Any]]:
        return self.store.list_results(workflow=workflow)
