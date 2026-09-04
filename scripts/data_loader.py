"""
data_loader — crypto OHLCV loading for the theory_refinement workflow.

Public API:
    load(coin, timeframe, start, end) -> pandas.DataFrame

On first import, scans the project-level ``data/`` directory recursively and
prints an inventory of the files it finds. Later calls to ``load`` consult
this inventory to decide whether to read a local file, gap-fill from OpenBB,
or fetch the whole range from OpenBB.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

import pandas as pd


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #


class DataNotFoundError(RuntimeError):
    """Raised when neither local files nor OpenBB can satisfy a load request."""


# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"


def _diagnostic(*args: Any, **kwargs: Any) -> None:
    """Keep loader diagnostics off machine-readable command stdout."""
    kwargs.setdefault("file", sys.stderr)
    print(*args, **kwargs)

# Ticker aliases — all stored lowercase for case-insensitive matching.
COIN_ALIASES: dict[str, list[str]] = {
    "BTC": ["btc", "btcusdt", "btcusd", "btc-usd", "bitcoin", "xbt"],
    "ETH": ["eth", "ethusdt", "ethusd", "eth-usd", "ethereum"],
    "LINK": ["link", "linkusdt", "linkusd", "link-usd", "chainlink"],
    "SOL": ["sol", "solusdt", "solusd", "sol-usd", "solana"],
}

# Timeframe canonical names mapped to pandas offset aliases and minute counts.
TIMEFRAME_MINUTES: dict[str, int] = {
    "1H": 60,
    "4H": 240,
    "1D": 60 * 24,
    "1W": 60 * 24 * 7,
}

TIMEFRAME_PANDAS_RULE: dict[str, str] = {
    "1H": "1h",
    "4H": "4h",
    "1D": "1D",
    "1W": "1W",
}

# Filename tokens → canonical timeframe.
TIMEFRAME_FILENAME_TOKENS: dict[str, str] = {
    "1h": "1H",
    "60m": "1H",
    "hourly": "1H",
    "4h": "4H",
    "240m": "4H",
    "1d": "1D",
    "daily": "1D",
    "day": "1D",
    "1w": "1W",
    "weekly": "1W",
    "week": "1W",
}

COLUMN_ALIASES: dict[str, str] = {
    "date": "timestamp",
    "time": "timestamp",
    "datetime": "timestamp",
    "timestamp": "timestamp",
    "open": "open",
    "o": "open",
    "high": "high",
    "h": "high",
    "low": "low",
    "l": "low",
    "close": "close",
    "c": "close",
    "adj close": "close",
    "volume": "volume",
    "vol": "volume",
    "v": "volume",
}

REQUIRED_COLUMNS = ["timestamp", "open", "high", "low", "close", "volume"]


# --------------------------------------------------------------------------- #
# Inventory
# --------------------------------------------------------------------------- #


@dataclass
class LocalFile:
    path: Path
    coin: str
    timeframe: str
    start: pd.Timestamp
    end: pd.Timestamp
    rows: int


@dataclass
class Inventory:
    # { coin: { timeframe: [LocalFile, ...] } }
    files: dict[str, dict[str, list[LocalFile]]] = field(default_factory=dict)

    def add(self, entry: LocalFile) -> None:
        self.files.setdefault(entry.coin, {})
        tf_entries = self.files[entry.coin].setdefault(entry.timeframe, [])
        tf_entries.append(entry)
        tf_entries.sort(key=lambda e: (e.start, e.end, e.rows))

    def candidates(self, coin: str, timeframe: str) -> list[LocalFile]:
        return list(self.files.get(coin, {}).get(timeframe, []))

    def best_covering(
        self,
        coin: str,
        timeframe: str,
        start: pd.Timestamp,
        end: pd.Timestamp,
    ) -> LocalFile | None:
        candidates = self.candidates(coin, timeframe)
        if not candidates:
            return None

        def overlap(entry: LocalFile) -> pd.Timedelta:
            overlap_start = max(entry.start, start)
            overlap_end = min(entry.end, end)
            if overlap_end < overlap_start:
                return pd.Timedelta(0)
            return overlap_end - overlap_start

        full = [
            entry
            for entry in candidates
            if entry.start <= start and entry.end >= end
        ]
        if full:
            return min(full, key=lambda entry: (entry.end - entry.start, -entry.rows))

        overlapped = [entry for entry in candidates if overlap(entry) > pd.Timedelta(0)]
        if overlapped:
            return max(
                overlapped,
                key=lambda entry: (
                    overlap(entry),
                    entry.end,
                    -TIMEFRAME_MINUTES[entry.timeframe],
                    entry.rows,
                ),
            )

        return max(candidates, key=lambda entry: (entry.end, entry.rows))

    def lowest_available(
        self,
        coin: str,
        target_tf: str,
        start: pd.Timestamp,
        end: pd.Timestamp,
    ) -> LocalFile | None:
        """Return the highest-resolution local file that can resample to target_tf."""
        target_minutes = TIMEFRAME_MINUTES[target_tf]
        candidates: list[LocalFile] = []
        for tf, entries in self.files.get(coin, {}).items():
            if TIMEFRAME_MINUTES[tf] < target_minutes:
                candidates.extend(entries)
        if not candidates:
            return None
        exact_cover = [
            entry
            for entry in candidates
            if entry.start <= start and entry.end >= end
        ]
        if exact_cover:
            exact_cover.sort(
                key=lambda entry: (
                    TIMEFRAME_MINUTES[entry.timeframe],
                    entry.end - entry.start,
                    -entry.rows,
                )
            )
            return exact_cover[0]

        overlapping = [
            entry
            for entry in candidates
            if min(entry.end, end) >= max(entry.start, start)
        ]
        if overlapping:
            overlapping.sort(
                key=lambda entry: (
                    -TIMEFRAME_MINUTES[entry.timeframe],
                    -(min(entry.end, end) - max(entry.start, start)).total_seconds(),
                    -entry.end.timestamp(),
                    -entry.rows,
                )
            )
            return overlapping[0]

        candidates.sort(
            key=lambda entry: (
                -TIMEFRAME_MINUTES[entry.timeframe],
                -entry.end.timestamp(),
                -entry.rows,
            )
        )
        return candidates[0]


_INVENTORY: Inventory | None = None


# --------------------------------------------------------------------------- #
# File scanning
# --------------------------------------------------------------------------- #


def _detect_coin(filename: str) -> str | None:
    name = filename.lower()
    # Sort aliases by length, longest first, so "btcusdt" beats "btc".
    for canonical, aliases in COIN_ALIASES.items():
        for alias in sorted(aliases, key=len, reverse=True):
            # Match as a contiguous token (allow separators around it).
            if re.search(rf"(?:^|[^a-z0-9]){re.escape(alias)}(?:[^a-z0-9]|$)", name):
                return canonical
    return None


def _detect_timeframe_from_filename(filename: str) -> str | None:
    name = filename.lower()
    # Sort tokens longest-first to avoid "1w" matching inside "1week".
    for token in sorted(TIMEFRAME_FILENAME_TOKENS, key=len, reverse=True):
        if re.search(rf"(?:^|[^a-z0-9]){re.escape(token)}(?:[^a-z0-9]|$)", name):
            return TIMEFRAME_FILENAME_TOKENS[token]
    return None


def _infer_timeframe_from_index(ts: pd.Series) -> str | None:
    if len(ts) < 3:
        return None
    diffs = ts.sort_values().diff().dropna()
    if diffs.empty:
        return None
    median_diff = pd.Timedelta(diffs.median())
    median_minutes = median_diff.total_seconds() / 60
    # Snap to the closest canonical timeframe by log-distance.
    best, best_ratio = None, None
    for tf, minutes in TIMEFRAME_MINUTES.items():
        ratio = max(minutes, median_minutes) / min(minutes, median_minutes)
        if best_ratio is None or ratio < best_ratio:
            best, best_ratio = tf, ratio
    # Only accept if within 15% of a canonical timeframe.
    if best_ratio is not None and best_ratio <= 1.15:
        return best
    return None


def _read_raw(path: Path) -> pd.DataFrame:
    if path.suffix.lower() == ".parquet":
        return pd.read_parquet(path)
    return pd.read_csv(path)


def _normalize(df: pd.DataFrame) -> pd.DataFrame:
    renamed = {}
    for col in df.columns:
        key = col.strip().lower()
        if key in COLUMN_ALIASES:
            renamed[col] = COLUMN_ALIASES[key]
    df = df.rename(columns=renamed)
    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"missing required columns: {missing}")
    df = df[REQUIRED_COLUMNS].copy()
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True, errors="coerce")
    df = df.dropna(subset=["timestamp"])
    for col in ("open", "high", "low", "close", "volume"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["open", "high", "low", "close"])
    df = df.sort_values("timestamp").drop_duplicates("timestamp").reset_index(drop=True)
    return df


def _scan(data_dir: Path) -> Inventory:
    inventory = Inventory()
    if not data_dir.exists():
        _diagnostic(f"[data_loader] data directory not found: {data_dir}")
        return inventory

    files = sorted(
        [p for p in data_dir.rglob("*") if p.suffix.lower() in (".csv", ".parquet")]
    )
    for path in files:
        rel = path.relative_to(PROJECT_ROOT).as_posix()
        if "options_cache" in rel:
            continue
        coin = _detect_coin(path.name)
        if coin is None:
            continue
        try:
            raw = _read_raw(path)
            df = _normalize(raw)
        except Exception as exc:  # noqa: BLE001
            _diagnostic(f"[data_loader] skip {path.relative_to(PROJECT_ROOT)}: {exc}")
            continue
        if df.empty:
            continue
        tf = _detect_timeframe_from_filename(path.name) or _infer_timeframe_from_index(
            df["timestamp"]
        )
        if tf is None:
            _diagnostic(
                f"[data_loader] skip {path.relative_to(PROJECT_ROOT)}: "
                "cannot determine timeframe"
            )
            continue
        entry = LocalFile(
            path=path,
            coin=coin,
            timeframe=tf,
            start=df["timestamp"].iloc[0],
            end=df["timestamp"].iloc[-1],
            rows=len(df),
        )
        inventory.add(entry)

    _print_inventory(inventory)
    return inventory


def _print_inventory(inventory: Inventory) -> None:
    if not inventory.files:
        _diagnostic("[data_loader] no local OHLCV files discovered under data/")
        return

    for coin in sorted(inventory.files):
        tf_map = inventory.files[coin]
        for tf in sorted(tf_map, key=lambda t: TIMEFRAME_MINUTES[t]):
            for entry in tf_map[tf]:
                rel = entry.path.relative_to(PROJECT_ROOT)
                start_s = entry.start.strftime("%Y-%m-%d")
                end_s = entry.end.strftime("%Y-%m-%d")
                _diagnostic(
                    f"[data_loader] found: {coin} {tf} — {rel} "
                    f"({start_s} → {end_s}, {entry.rows} rows)"
                )
        # Note which higher timeframes will be resampled.
        available_tfs = set(tf_map.keys())
        for tf in TIMEFRAME_MINUTES:
            if tf in available_tfs:
                continue
            source = inventory.lowest_available(
                coin,
                tf,
                pd.Timestamp.min.tz_localize("UTC"),
                pd.Timestamp.max.tz_localize("UTC"),
            )
            if source is not None:
                assert source is not None
                _diagnostic(
                    f"[data_loader] note: {coin} {tf} will be resampled "
                    f"from {coin} {source.timeframe}"
                )


def _get_inventory() -> Inventory:
    global _INVENTORY
    if _INVENTORY is None:
        _INVENTORY = _scan(DATA_DIR)
    return _INVENTORY


# --------------------------------------------------------------------------- #
# Resampling
# --------------------------------------------------------------------------- #


def _resample(df: pd.DataFrame, target_tf: str) -> pd.DataFrame:
    rule = TIMEFRAME_PANDAS_RULE[target_tf]
    indexed = df.set_index("timestamp")
    agg = indexed.resample(rule, label="left", closed="left").agg(
        {
            "open": "first",
            "high": "max",
            "low": "min",
            "close": "last",
            "volume": "sum",
        }
    )
    agg = agg.dropna(subset=["open", "high", "low", "close"])
    return agg.reset_index()


# --------------------------------------------------------------------------- #
# OpenBB fetcher
# --------------------------------------------------------------------------- #


def _openbb_symbol(coin: str) -> str:
    return {"BTC": "BTC-USD", "ETH": "ETH-USD"}.get(coin, f"{coin}-USD")


def _openbb_interval(timeframe: str) -> str:
    # OpenBB does not expose a native 4H interval on this backend. Fetch 1H and
    # let the existing resample path promote it to 4H.
    return {"1H": "1h", "4H": "1h", "1D": "1d", "1W": "1W"}[timeframe]


# --------------------------------------------------------------------------- #
# Binance REST fetcher (native 4H and 1H support, history from ~2017-08)
# --------------------------------------------------------------------------- #


BINANCE_URL = "https://api.binance.com/api/v3/klines"
BINANCE_INTERVAL: dict[str, str] = {"1H": "1h", "4H": "4h", "1D": "1d", "1W": "1w"}


def _binance_symbol(coin: str) -> str:
    return {"BTC": "BTCUSDT", "ETH": "ETHUSDT"}.get(coin, f"{coin}USDT")


def _cache_dir() -> Path:
    return DATA_DIR / "binance_cache"


def _cache_path(coin: str, timeframe: str) -> Path:
    return _cache_dir() / f"{coin}_{timeframe.lower()}.parquet"


def _write_binance_cache(coin: str, timeframe: str, df: pd.DataFrame) -> None:
    """Append fetched klines into a persistent parquet cache."""
    if df.empty:
        return
    cache_dir = _cache_dir()
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
    except Exception as exc:  # noqa: BLE001
        _diagnostic(f"[data_loader] cache dir create failed: {exc}")
        return
    path = _cache_path(coin, timeframe)
    merged = df
    if path.exists():
        try:
            existing = pd.read_parquet(path)
            merged = (
                pd.concat([existing, df], ignore_index=True)
                .drop_duplicates("timestamp")
                .sort_values("timestamp")
                .reset_index(drop=True)
            )
        except Exception as exc:  # noqa: BLE001
            _diagnostic(f"[data_loader] cache merge failed, overwriting: {exc}")
    try:
        merged.to_parquet(path, index=False)
        rel = path.relative_to(PROJECT_ROOT)
        _diagnostic(
            f"[data_loader] cached {len(merged)} rows for {coin} {timeframe} → {rel}"
        )
    except Exception as exc:  # noqa: BLE001
        _diagnostic(f"[data_loader] cache write failed: {exc}")


def _fetch_binance(
    coin: str, timeframe: str, start: pd.Timestamp, end: pd.Timestamp
) -> pd.DataFrame | None:
    """Fetch klines from Binance REST with pagination. Returns normalized OHLCV."""
    if timeframe not in BINANCE_INTERVAL:
        return None
    try:
        import requests  # type: ignore
    except Exception as exc:  # noqa: BLE001
        _diagnostic(f"[data_loader] 'requests' unavailable: {exc}")
        return None

    symbol = _binance_symbol(coin)
    interval = BINANCE_INTERVAL[timeframe]
    cursor_ms = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)

    all_rows: list[list[Any]] = []
    max_requests = 500  # safety limit — 500 * 1000 klines is plenty for any period
    requests_made = 0

    session = requests.Session()
    while requests_made < max_requests and cursor_ms <= end_ms:
        params = {
            "symbol": symbol,
            "interval": interval,
            "startTime": cursor_ms,
            "endTime": end_ms,
            "limit": 1000,
        }
        try:
            resp = session.get(BINANCE_URL, params=params, timeout=30)
        except Exception as exc:  # noqa: BLE001
            _diagnostic(f"[data_loader] Binance fetch failed for {coin} {timeframe}: {exc}")
            return None
        requests_made += 1

        if resp.status_code != 200:
            snippet = resp.text[:200].replace("\n", " ")
            _diagnostic(
                f"[data_loader] Binance {coin} {timeframe} HTTP {resp.status_code}: "
                f"{snippet}"
            )
            return None

        try:
            batch = resp.json()
        except Exception as exc:  # noqa: BLE001
            _diagnostic(f"[data_loader] Binance JSON decode failed: {exc}")
            return None

        if not isinstance(batch, list) or not batch:
            break

        all_rows.extend(batch)
        last_open_ms = int(batch[-1][0])
        if len(batch) < 1000:
            break
        cursor_ms = last_open_ms + 1

    if not all_rows:
        return None

    df = pd.DataFrame(
        all_rows,
        columns=[
            "open_time",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "close_time",
            "quote_volume",
            "trades",
            "taker_buy_base",
            "taker_buy_quote",
            "ignore",
        ],
    )
    df["timestamp"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    df = df[["timestamp", "open", "high", "low", "close", "volume"]].copy()
    for col in ("open", "high", "low", "close", "volume"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = (
        df.dropna(subset=["open", "high", "low", "close"])
        .sort_values("timestamp")
        .drop_duplicates("timestamp")
        .reset_index(drop=True)
    )
    if df.empty:
        return None

    _diagnostic(
        f"[data_loader] Binance returned {len(df)} rows for {coin} {timeframe} "
        f"({df['timestamp'].iloc[0].date()} → {df['timestamp'].iloc[-1].date()}) "
        f"after {requests_made} request(s)"
    )
    _write_binance_cache(coin, timeframe, df)
    return df


def _fetch_remote(
    coin: str, timeframe: str, start: pd.Timestamp, end: pd.Timestamp
) -> pd.DataFrame | None:
    """Try Binance first (native 4H, long history), then OpenBB."""
    df = _fetch_binance(coin, timeframe, start, end)
    if df is not None and not df.empty:
        return df
    return _fetch_openbb(coin, timeframe, start, end)


def _fetch_openbb(
    coin: str, timeframe: str, start: pd.Timestamp, end: pd.Timestamp
) -> pd.DataFrame | None:
    try:
        from openbb import obb  # type: ignore
    except Exception as exc:  # noqa: BLE001
        _diagnostic(f"[data_loader] OpenBB unavailable: {exc}")
        return None

    symbol = _openbb_symbol(coin)
    interval = _openbb_interval(timeframe)
    try:
        result: Any = obb.crypto.price.historical(  # type: ignore[attr-defined]
            symbol=symbol,
            start_date=start.strftime("%Y-%m-%d"),
            end_date=end.strftime("%Y-%m-%d"),
            interval=interval,
        )
        df = result.to_df().reset_index()
    except Exception as exc:  # noqa: BLE001
        _diagnostic(f"[data_loader] OpenBB fetch failed for {coin} {timeframe}: {exc}")
        return None

    try:
        df = _normalize(df)
    except Exception as exc:  # noqa: BLE001
        _diagnostic(f"[data_loader] OpenBB response normalization failed: {exc}")
        return None

    if df.empty:
        return None
    # If the caller asked for a non-native interval (e.g. 4H but OpenBB only
    # returned 1H), resample so the contract holds.
    inferred = _infer_timeframe_from_index(df["timestamp"])
    if inferred is not None and TIMEFRAME_MINUTES[inferred] < TIMEFRAME_MINUTES[timeframe]:
        df = _resample(df, timeframe)
    return df


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #


def _to_ts(value: Any) -> pd.Timestamp:
    ts = pd.Timestamp(value)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    else:
        ts = ts.tz_convert("UTC")
    return ts


def _slice(df: pd.DataFrame, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    mask = (df["timestamp"] >= start) & (df["timestamp"] <= end)
    return df.loc[mask].reset_index(drop=True)


def _coverage_score(
    df: pd.DataFrame,
    start: pd.Timestamp,
    end: pd.Timestamp,
) -> tuple[int, float, float]:
    if df.empty:
        return (0, 0.0, float("-inf"))
    local_start = df["timestamp"].iloc[0]
    local_end = df["timestamp"].iloc[-1]
    overlap_start = max(local_start, start)
    overlap_end = min(local_end, end)
    overlap_seconds = 0.0
    if overlap_end >= overlap_start:
        overlap_seconds = (overlap_end - overlap_start).total_seconds()
    full_cover = int(local_start <= start and local_end >= end)
    return (full_cover, overlap_seconds, local_end.timestamp())


def _available_files_summary() -> str:
    inv = _get_inventory()
    if not inv.files:
        return "  (none)"
    lines = []
    for coin, tf_map in inv.files.items():
        for tf, entries in tf_map.items():
            for entry in entries:
                rel = entry.path.relative_to(PROJECT_ROOT)
                lines.append(f"  - {coin} {tf}: {rel}")
    return "\n".join(lines)


def _load_local(entry: LocalFile) -> pd.DataFrame:
    return _normalize(_read_raw(entry.path))


def load(
    coin: str,
    timeframe: str,
    start: Any,
    end: Any,
) -> pd.DataFrame:
    """Load OHLCV for ``coin`` at ``timeframe`` between ``start`` and ``end``.

    Resolution order:
        1. Local file covering the full range.
        2. Local file + OpenBB gap-fill.
        3. OpenBB only.

    Raises :class:`DataNotFoundError` if none of those succeed.
    """
    coin = coin.upper()
    timeframe = timeframe.upper().replace("H", "H").replace("D", "D").replace("W", "W")
    if timeframe not in TIMEFRAME_MINUTES:
        # Accept a few common spellings.
        aliased = {
            "HOURLY": "1H",
            "DAILY": "1D",
            "WEEKLY": "1W",
            "H1": "1H",
            "H4": "4H",
            "D1": "1D",
            "W1": "1W",
        }.get(timeframe)
        if aliased is None:
            raise ValueError(f"unsupported timeframe: {timeframe}")
        timeframe = aliased

    start_ts = _to_ts(start)
    end_ts = _to_ts(end)
    if end_ts < start_ts:
        raise ValueError(f"end ({end_ts}) is before start ({start_ts})")

    inventory = _get_inventory()

    # ------------------------------------------------------------------ #
    # Find a local source for the requested timeframe (native or resampled)
    # ------------------------------------------------------------------ #
    native = inventory.best_covering(coin, timeframe, start_ts, end_ts)
    source = inventory.lowest_available(coin, timeframe, start_ts, end_ts)

    local_options: list[tuple[pd.DataFrame, str]] = []
    if native is not None:
        local_options.append(
            (
                _load_local(native),
                str(native.path.relative_to(PROJECT_ROOT)),
            )
        )
    if source is not None:
        local_options.append(
            (
                _resample(_load_local(source), timeframe),
                f"{source.path.relative_to(PROJECT_ROOT)} "
                f"(resampled {source.timeframe}→{timeframe})",
            )
        )

    local_df: pd.DataFrame | None = None
    local_label: str | None = None
    if local_options:
        local_df, local_label = max(
            local_options,
            key=lambda item: _coverage_score(item[0], start_ts, end_ts),
        )

    # ------------------------------------------------------------------ #
    # Case 1 / 2: local file exists
    # ------------------------------------------------------------------ #
    if local_df is not None and not local_df.empty:
        local_start = local_df["timestamp"].iloc[0]
        local_end = local_df["timestamp"].iloc[-1]

        covers_start = local_start <= start_ts
        covers_end = local_end >= end_ts

        if covers_start and covers_end:
            _diagnostic(f"[{coin} {timeframe}] source: local — {local_label}")
            return _slice(local_df, start_ts, end_ts)

        # Gap fill with OpenBB for anything missing at either end.
        gap_frames: list[pd.DataFrame] = []
        gap_notes: list[str] = []

        if not covers_start:
            gap_start = start_ts
            gap_end = min(end_ts, local_start - pd.Timedelta(minutes=1))
            if gap_end >= gap_start:
                fetched = _fetch_remote(coin, timeframe, gap_start, gap_end)
                if fetched is not None and not fetched.empty:
                    gap_frames.append(fetched)
                    gap_notes.append(
                        f"{gap_start.strftime('%Y-%m-%d')} → "
                        f"{gap_end.strftime('%Y-%m-%d')}"
                    )

        gap_frames.append(_slice(local_df, start_ts, end_ts))

        if not covers_end:
            gap_start = max(start_ts, local_end + pd.Timedelta(minutes=1))
            gap_end = end_ts
            if gap_end >= gap_start:
                fetched = _fetch_remote(coin, timeframe, gap_start, gap_end)
                if fetched is not None and not fetched.empty:
                    gap_frames.append(fetched)
                    gap_notes.append(
                        f"{gap_start.strftime('%Y-%m-%d')} → "
                        f"{gap_end.strftime('%Y-%m-%d')}"
                    )

        merged = (
            pd.concat(gap_frames, ignore_index=True)
            .drop_duplicates("timestamp")
            .sort_values("timestamp")
            .reset_index(drop=True)
        )
        if merged.empty:
            raise DataNotFoundError(
                f"local file {local_label} did not cover {start_ts.date()} → "
                f"{end_ts.date()} and remote gap-fill returned nothing"
            )

        if gap_notes:
            _diagnostic(
                f"[{coin} {timeframe}] source: local ({local_label}) + "
                f"remote gap-fill ({'; '.join(gap_notes)})"
            )
        else:
            _diagnostic(
                f"[{coin} {timeframe}] source: local — {local_label} "
                f"(partial; remote gap-fill unavailable)"
            )
        return _slice(merged, start_ts, end_ts)

    # ------------------------------------------------------------------ #
    # Case 3: no local file — remote only (Binance → OpenBB)
    # ------------------------------------------------------------------ #
    fetched = _fetch_remote(coin, timeframe, start_ts, end_ts)
    if fetched is not None and not fetched.empty:
        _diagnostic(f"[{coin} {timeframe}] source: remote (no local file found)")
        return _slice(fetched, start_ts, end_ts)

    raise DataNotFoundError(
        f"no data available for {coin} {timeframe} between "
        f"{start_ts.date()} and {end_ts.date()}.\n"
        f"Available local files:\n{_available_files_summary()}"
    )


# --------------------------------------------------------------------------- #
# Run the inventory scan at import time so the first `load()` call is ready.
# --------------------------------------------------------------------------- #

_get_inventory()
