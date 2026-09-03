"""
Hyperliquid client — wraps the Hyperliquid REST API and Python SDK.

Design principles:
  - Read-only queries (prices, positions, orders) use direct REST — no auth needed.
  - Write operations (open/close/cancel) load the private key from vault only at
    call time, sign the payload, then discard the key reference.
  - Testnet is the default; pass testnet=False only when ready for live trading.
  - All dollar amounts are USD-denominated floats.

Usage:
    from trading import HyperliquidClient

    client = HyperliquidClient("openfang", testnet=True)
    client.summary()                              # print safe account info
    client.market_open("ETH", "long", 50.0)      # open $50 long ETH

Required packages:  requests, eth-account, hyperliquid-python-sdk
"""

import json
import time
from datetime import datetime, timezone
from typing import Any, Protocol
from typing import Literal

import requests

from trading.crypto.vault import WalletVault


MAINNET_API = "https://api.hyperliquid.xyz"
TESTNET_API = "https://api.hyperliquid-testnet.xyz"
SUPPORTED_CANDLE_INTERVALS = {
    "1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "8h", "12h", "1d", "3d", "1w", "1M",
}


class HyperliquidClientProtocol(Protocol):
    """Strategy-facing client contract for live and mock implementations."""

    def get_open_positions(self) -> list[Any]:
        ...

    def market_open(self, coin: str, side: Literal["long", "short"], size_usd: float) -> dict[str, Any]:
        ...

    def market_close(self, coin: str) -> dict[str, Any]:
        ...


class HyperliquidClient:
    def __init__(self, wallet_name: str | None = "hermes", testnet: bool = True):
        self.testnet = testnet
        self.base_url = TESTNET_API if testnet else MAINNET_API
        self._wallet_name = wallet_name or ""
        self._vault: WalletVault | None = WalletVault() if wallet_name else None
        self._address = ""
        if wallet_name and self._vault is not None:
            try:
                self._address = self._vault.address(wallet_name)
            except Exception:
                # Read-only market endpoints do not require wallet resolution.
                self._address = ""
        self._exchange = None  # lazy-loaded on first write operation

    @property
    def address(self) -> str:
        return self._address

    @property
    def env(self) -> str:
        return "TESTNET" if self.testnet else "MAINNET"

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _require_address(self) -> str:
        if not self._address:
            raise ValueError(
                "No wallet address configured. Initialize HyperliquidClient with a valid wallet_name "
                "for account-specific endpoints."
            )
        return self._address

    def _info(self, payload: dict) -> dict | list:
        resp = requests.post(f"{self.base_url}/info", json=payload, timeout=10)
        resp.raise_for_status()
        return resp.json()

    def _get_exchange(self):
        """Lazy-load the signing exchange client."""
        if self._exchange is not None:
            return self._exchange
        if not self._wallet_name:
            raise ValueError("wallet_name is required for trading operations.")
        try:
            import eth_account
            from hyperliquid.exchange import Exchange
            from hyperliquid.utils import constants
        except ImportError as e:
            raise ImportError(
                "Missing trading deps. Run: pip install eth-account hyperliquid-python-sdk"
            ) from e

        if self._vault is None:
            self._vault = WalletVault()
        private_key = self._vault.private_key(self._wallet_name)
        account = eth_account.Account.from_key(private_key)
        del private_key  # discard from local scope after use
        base_url = constants.TESTNET_API_URL if self.testnet else constants.MAINNET_API_URL
        self._exchange = Exchange(
            account, base_url, account_address=account.address)
        return self._exchange

    # ── Read-only endpoints ───────────────────────────────────────────────────

    def get_account_state(self) -> dict:
        """Full portfolio state: equity, margin, open positions."""
        result = self._info({"type": "clearinghouseState",
                            "user": self._require_address()})
        return result if isinstance(result, dict) else {}

    def get_open_positions(self) -> list[dict]:
        return self.get_account_state().get("assetPositions", [])

    def get_open_orders(self) -> list[dict]:
        result = self._info(
            {"type": "openOrders", "user": self._require_address()})
        return [r for r in result if isinstance(r, dict)] if isinstance(result, list) else []

    def get_fills(self, limit: int = 50) -> list[dict]:
        result = self._info(
            {"type": "userFills", "user": self._require_address()})
        if not isinstance(result, list):
            return []
        return [r for r in result[:limit] if isinstance(r, dict)]

    def get_all_mids(self) -> dict[str, str]:
        """Mid prices for every perp market, e.g. {"BTC": "67500.0", ...}."""
        result = self._info({"type": "allMids"})
        if not isinstance(result, dict):
            return {}
        return {str(k): str(v) for k, v in result.items()}

    def get_l2_book(self, coin: str) -> dict[str, Any]:
        """Return the read-only level-2 order book for a perp market."""
        result = self._info({"type": "l2Book", "coin": coin.upper()})
        return result if isinstance(result, dict) else {}

    def get_mid(self, coin: str) -> float:
        mids = self.get_all_mids()
        if coin not in mids:
            raise ValueError(
                f"Unknown market: {coin}. Check get_all_mids() for valid symbols.")
        return float(mids[coin])

    def get_meta(self) -> dict:
        """Exchange metadata: assets, leverage tiers, tick sizes."""
        result = self._info({"type": "meta"})
        return result if isinstance(result, dict) else {}

    def get_markets(self) -> list[str]:
        """Return sorted list of all tradeable symbols."""
        meta = self.get_meta()
        return sorted(u["name"] for u in meta.get("universe", []))

    def get_asset_meta(self, coin: str) -> dict[str, Any]:
        """Return exchange metadata for a single perp symbol."""
        symbol = coin.upper()
        for asset in self.get_meta().get("universe", []):
            if asset.get("name") == symbol:
                return asset
        raise ValueError(
            f"Unknown market: {symbol}. Check get_markets() for valid symbols."
        )

    def coin_size_from_usd(self, coin: str, size_usd: float) -> float:
        """Convert a USD notional into a valid coin size for Hyperliquid."""
        asset = self.get_asset_meta(coin)
        sz_decimals = int(asset.get("szDecimals", 4))
        price = self.get_mid(coin)
        return round(size_usd / price, sz_decimals)

    def get_candle_snapshot(
        self,
        coin: str,
        interval: str,
        start_time_ms: int,
        end_time_ms: int,
    ) -> list[dict[str, Any]]:
        """Fetch a candle snapshot from Hyperliquid /info.

        Endpoint contract (validated):
          {"type":"candleSnapshot","req":{"coin":"BTC","interval":"1h","startTime":...,"endTime":...}}
        """
        if interval not in SUPPORTED_CANDLE_INTERVALS:
            allowed = ", ".join(sorted(SUPPORTED_CANDLE_INTERVALS))
            raise ValueError(
                f"Unsupported interval '{interval}'. Allowed: {allowed}")
        if start_time_ms >= end_time_ms:
            raise ValueError("start_time_ms must be less than end_time_ms")

        payload = {
            "type": "candleSnapshot",
            "req": {
                "coin": coin.upper(),
                "interval": interval,
                "startTime": int(start_time_ms),
                "endTime": int(end_time_ms),
            },
        }
        result = self._info(payload)
        if not isinstance(result, list):
            return []
        return [row for row in result if isinstance(row, dict)]

    def normalize_candles(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Normalize Hyperliquid candle payload rows to a stable schema."""
        normalized: list[dict[str, Any]] = []
        for row in rows:
            try:
                ts_ms = int(row["t"])
                close_ms = int(row["T"])
                coin = str(row["s"])
                interval = str(row["i"])
                item = {
                    "timestamp_ms": ts_ms,
                    "timestamp": datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc),
                    "close_time_ms": close_ms,
                    "coin": coin,
                    "interval": interval,
                    "open": float(row["o"]),
                    "high": float(row["h"]),
                    "low": float(row["l"]),
                    "close": float(row["c"]),
                    "volume": float(row["v"]),
                    "trade_count": int(row.get("n", 0) or 0),
                    "source": "hyperliquid",
                }
                normalized.append(item)
            except (KeyError, TypeError, ValueError):
                continue

        normalized.sort(key=lambda x: x["timestamp_ms"])
        return normalized

    def get_historical_candles(
        self,
        coin: str,
        interval: str,
        start_time_ms: int = 0,
        end_time_ms: int | None = None,
        max_pages: int = 200,
        throttle_seconds: float = 0.05,
    ) -> list[dict[str, Any]]:
        """Backfill candles by paging backward from end_time_ms.

        Hyperliquid currently caps candleSnapshot responses at roughly 5k rows.
        This method paginates backwards by moving end_time to just before the
        oldest candle in the previous page.
        """
        if end_time_ms is None:
            end_time_ms = int(time.time() * 1000)

        if start_time_ms >= end_time_ms:
            return []

        cursor_end = int(end_time_ms)
        pages = 0
        by_timestamp: dict[int, dict[str, Any]] = {}

        while cursor_end > start_time_ms and pages < max_pages:
            pages += 1
            raw = self.get_candle_snapshot(
                coin=coin,
                interval=interval,
                start_time_ms=start_time_ms,
                end_time_ms=cursor_end,
            )
            normalized = self.normalize_candles(raw)
            if not normalized:
                break

            oldest_ts = normalized[0]["timestamp_ms"]
            newest_ts = normalized[-1]["timestamp_ms"]

            for candle in normalized:
                by_timestamp[candle["timestamp_ms"]] = candle

            # If endpoint returned a single-point window, avoid infinite loop.
            if oldest_ts >= cursor_end or newest_ts >= cursor_end:
                break

            cursor_end = oldest_ts - 1
            if throttle_seconds > 0:
                time.sleep(throttle_seconds)

        candles = list(by_timestamp.values())
        candles.sort(key=lambda x: x["timestamp_ms"])
        return candles

    # ── Trading endpoints ─────────────────────────────────────────────────────

    def market_open(
        self,
        coin: str,
        side: Literal["long", "short"],
        size_usd: float,
        slippage: float = 0.01,
    ) -> dict:
        """
        Open a market position.

        Args:
            coin:      Symbol, e.g. "ETH", "BTC"
            side:      "long" or "short"
            size_usd:  Notional position size in USD
            slippage:  Max acceptable slippage (default 1%)

        Returns:
            Exchange response dict with status and filled details.
        """
        exchange = self._get_exchange()
        is_buy = side == "long"
        size = self.coin_size_from_usd(coin, size_usd)
        if size <= 0:
            raise ValueError(f"Computed non-positive size for {coin} at ${size_usd:.2f}")
        return exchange.market_open(coin, is_buy, size, slippage=slippage)

    def market_close(self, coin: str, slippage: float = 0.01) -> dict:
        """Close the entire open position for a coin at market price."""
        return self._get_exchange().market_close(coin, slippage=slippage)

    def open_position(
        self,
        coin: str,
        direction: Literal["long", "short"],
        size_usd: float,
        leverage: float | None = None,
    ) -> dict:
        """Compatibility wrapper used by strategy/tests."""
        if leverage is not None:
            # Hyperliquid leverage is integer; clamp to at least 1x.
            self.set_leverage(coin, max(1, int(round(leverage))))
        return self.market_open(coin, direction, size_usd)

    def close_position(self, coin: str) -> dict:
        """Compatibility wrapper used by strategy/tests."""
        return self.market_close(coin)

    def limit_order(
        self,
        coin: str,
        side: Literal["buy", "sell"],
        size: float,
        price: float,
        reduce_only: bool = False,
    ) -> dict:
        """Place a GTC limit order. size is in coin units (not USD)."""
        exchange = self._get_exchange()
        return exchange.order(
            coin, side == "buy", size, price,
            {"limit": {"tif": "Gtc"}},
            reduce_only=reduce_only,
        )

    def cancel_order(self, coin: str, order_id: int) -> dict:
        return self._get_exchange().cancel(coin, order_id)

    def set_leverage(self, coin: str, leverage: int, cross_margin: bool = True) -> dict:
        return self._get_exchange().update_leverage(leverage, coin, cross_margin)

    # ── Convenience ───────────────────────────────────────────────────────────

    def summary(self) -> None:
        """Print a safe account summary. No private data is shown."""
        state = self.get_account_state()
        margin = state.get("marginSummary", {})
        positions = self.get_open_positions()
        orders = self.get_open_orders()

        print(f"\n{'─'*50}")
        print(f"  Hyperliquid {self.env}")
        print(f"  Wallet  : {self._wallet_name}  ({self._address})")
        print(f"  Equity  : ${float(margin.get('accountValue', 0)):>12,.2f}")
        print(
            f"  Margin  : ${float(margin.get('totalMarginUsed', 0)):>12,.2f}")
        print(f"  Positions : {len(positions)}  |  Open orders: {len(orders)}")
        if positions:
            print()
            for pos in positions:
                p = pos.get("position", {})
                pnl = float(p.get("unrealizedPnl", 0))
                print(f"    {p.get('coin'):>6}  sz={p.get('szi')}  "
                      f"entry={p.get('entryPx')}  uPnL=${pnl:+.2f}")
        print(f"{'─'*50}\n")


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Hyperliquid CLI")
    parser.add_argument("command", choices=[
                        "summary", "positions", "orders", "mids", "markets"])
    parser.add_argument("--wallet", default="hermes")
    parser.add_argument("--mainnet", action="store_true",
                        help="Use mainnet (default: testnet)")
    args = parser.parse_args()

    client = HyperliquidClient(
        wallet_name=args.wallet, testnet=not args.mainnet)

    if args.command == "summary":
        client.summary()
    elif args.command == "positions":
        print(json.dumps(client.get_open_positions(), indent=2))
    elif args.command == "orders":
        print(json.dumps(client.get_open_orders(), indent=2))
    elif args.command == "mids":
        for coin, mid in sorted(client.get_all_mids().items()):
            print(f"  {coin:<10} {mid}")
    elif args.command == "markets":
        for m in client.get_markets():
            print(f"  {m}")
