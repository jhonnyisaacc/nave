"""Verify N6 modules import and the isolated path is wired correctly."""


def test_n6_squeeze_daily_imports():
    from trading.crypto.analysis.squeeze_daily import SqueezeDailyState, detect_squeeze_daily
    assert SqueezeDailyState is not None
    assert callable(detect_squeeze_daily)


def test_engine_has_n6_method():
    from trading.crypto.theory_v2 import TheoryV2Engine
    assert hasattr(TheoryV2Engine, "evaluate_squeeze_daily")


def test_squeeze_daily_self_contained():
    """The isolated N6 module must NOT *import* from the N5 weekly detector."""
    import sys
    # The module must not import the N5 weekly detector at runtime.
    import_paths = [m for m in ("squeeze_detector", "trading.crypto.analysis.squeeze_detector")
                    if m in (sys.modules or {})]
    assert not import_paths, f"N6 must not import N5 squeeze_detector: {import_paths}"


def test_validation_artifact_records_input_snapshot():
    from scripts.squeeze_daily_backtest import _input_snapshot

    snapshot = _input_snapshot(["BTC"])
    assert snapshot["algorithm"] == "sha256"
    assert {entry["path"].split("/")[-1] for entry in snapshot["files"]} >= {
        "BTC_1h.parquet",
        "BTC_4h.parquet",
        "BTC_1d.parquet",
        "BTC_1w.parquet",
    }
    assert all("exists" in entry for entry in snapshot["files"])
    assert all((not entry["exists"]) or len(entry["sha256"]) == 64 for entry in snapshot["files"])
