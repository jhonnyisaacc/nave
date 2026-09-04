"""Optional lifecycle dispatch for research strategies."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class StrategyIdentity(Protocol):
    name: str
    version: str


class UnsupportedPhase(LookupError):
    """Raised only when a caller requests a lifecycle method a strategy lacks."""


def run_phase(strategy: StrategyIdentity, phase: str, *args: Any, **kwargs: Any) -> Any:
    """Call ``scan``, ``evaluate``, ``missed_moves``, or ``status`` when present.

    Strategies need not implement every phase.  The caller decides which phase
    is meaningful for its workflow and gets a precise error for unsupported
    requests instead of an abstract method that would be meaningless.
    """

    if phase not in {"scan", "evaluate", "missed_moves", "status"}:
        raise ValueError(f"unknown strategy phase: {phase!r}")
    method = getattr(strategy, phase, None)
    if not callable(method):
        raise UnsupportedPhase(f"{type(strategy).__name__} does not implement {phase}()")
    return method(*args, **kwargs)
