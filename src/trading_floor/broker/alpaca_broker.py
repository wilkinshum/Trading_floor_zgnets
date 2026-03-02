"""Alpaca API wrapper for Trading Floor v4.0.

Wraps alpaca-py TradingClient and StockHistoricalDataClient with
rate-limit handling and exponential backoff retries.
"""

import time
import logging
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import (
    MarketOrderRequest,
    LimitOrderRequest,
    StopLimitOrderRequest,
    StopOrderRequest,
    GetOrdersRequest,
)
from alpaca.trading.enums import OrderSide, TimeInForce, OrderType, QueryOrderStatus
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, StockLatestQuoteRequest
from alpaca.data.timeframe import TimeFrame

logger = logging.getLogger(__name__)

# Alpaca rate limit: 200 req/min
_RATE_LIMIT = 200
_RATE_WINDOW = 60  # seconds


class RateLimiter:
    """Simple sliding-window rate limiter."""

    def __init__(self, max_calls: int = _RATE_LIMIT, window: float = _RATE_WINDOW):
        self.max_calls = max_calls
        self.window = window
        self._calls: List[float] = []

    def wait_if_needed(self):
        """Block until a request slot is available."""
        now = time.monotonic()
        self._calls = [t for t in self._calls if now - t < self.window]
        if len(self._calls) >= self.max_calls:
            sleep_time = self._calls[0] + self.window - now + 0.1
            logger.warning("Rate limit approached, sleeping %.1fs", sleep_time)
            time.sleep(sleep_time)
        self._calls.append(time.monotonic())


def _retry_with_backoff(func, max_retries: int = 3, base_delay: float = 1.0):
    """Execute func with exponential backoff on failure."""
    for attempt in range(max_retries + 1):
        try:
            return func()
        except Exception as e:
            if attempt == max_retries:
                raise
            delay = base_delay * (2 ** attempt)
            logger.warning("Retry %d/%d after error: %s (sleeping %.1fs)",
                           attempt + 1, max_retries, e, delay)
            time.sleep(delay)


class AlpacaBroker:
    """Alpaca API wrapper with rate limiting and retries.

    Args:
        api_key: Alpaca API key.
        api_secret: Alpaca API secret.
        paper: If True, use paper trading endpoint.
    """

    def __init__(self, api_key: str, api_secret: str, paper: bool = True):
        self.trading_client = TradingClient(
            api_key=api_key,
            secret_key=api_secret,
            paper=paper,
        )
        self.data_client = StockHistoricalDataClient(
            api_key=api_key,
            secret_key=api_secret,
        )
        self._rate_limiter = RateLimiter()

    def _call(self, func):
        """Rate-limited + retried API call."""
        self._rate_limiter.wait_if_needed()
        return _retry_with_backoff(func)

    # ── Account ──────────────────────────────────────────────

    def get_account(self):
        """Get current account info from Alpaca."""
        return self._call(lambda: self.trading_client.get_account())

    # ── Positions ────────────────────────────────────────────

    def get_positions(self):
        """Get all open positions."""
        return self._call(lambda: self.trading_client.get_all_positions())

    def get_position(self, symbol: str):
        """Get position for a specific symbol."""
        return self._call(lambda: self.trading_client.get_open_position(symbol))

    def close_position(self, symbol: str, qty: Optional[float] = None):
        """Close a position (fully or partially)."""
        kwargs = {}
        if qty is not None:
            kwargs["qty"] = str(qty)
        return self._call(
            lambda: self.trading_client.close_position(symbol, **kwargs)
        )

    # ── Orders ───────────────────────────────────────────────

    @staticmethod
    def make_client_order_id(strategy: str, symbol: str) -> str:
        """Generate a unique client_order_id: {strategy}_{symbol}_{timestamp}."""
        ts = int(datetime.now(timezone.utc).timestamp() * 1000)
        return f"{strategy}_{symbol}_{ts}"

    def submit_order(
        self,
        symbol: str,
        qty: float,
        side: str,
        order_type: str = "market",
        time_in_force: str = "day",
        limit_price: Optional[float] = None,
        stop_price: Optional[float] = None,
        client_order_id: Optional[str] = None,
        take_profit: Optional[Dict[str, float]] = None,
        stop_loss: Optional[Dict[str, float]] = None,
    ):
        """Submit an order to Alpaca.

        Args:
            symbol: Ticker symbol.
            qty: Number of shares.
            side: 'buy' or 'sell'.
            order_type: 'market', 'limit', 'stop', 'stop_limit'.
            time_in_force: 'day', 'gtc', 'ioc', etc.
            limit_price: Required for limit/stop_limit orders.
            stop_price: Required for stop/stop_limit orders.
            client_order_id: Optional custom order ID.
            take_profit: Optional dict with 'limit_price' for bracket.
            stop_loss: Optional dict with 'stop_price' (and opt 'limit_price') for bracket.

        Returns:
            Alpaca Order object.
        """
        _side = OrderSide.BUY if side.lower() == "buy" else OrderSide.SELL
        _tif = getattr(TimeInForce, time_in_force.upper(), TimeInForce.DAY)

        common = dict(
            symbol=symbol,
            qty=qty,
            side=_side,
            time_in_force=_tif,
        )
        if client_order_id:
            common["client_order_id"] = client_order_id
        if take_profit:
            common["take_profit"] = take_profit
        if stop_loss:
            common["stop_loss"] = stop_loss

        if order_type == "market":
            req = MarketOrderRequest(**common)
        elif order_type == "limit":
            req = LimitOrderRequest(limit_price=limit_price, **common)
        elif order_type == "stop":
            req = StopOrderRequest(stop_price=stop_price, **common)
        elif order_type == "stop_limit":
            req = StopLimitOrderRequest(
                limit_price=limit_price, stop_price=stop_price, **common
            )
        else:
            raise ValueError(f"Unknown order_type: {order_type}")

        return self._call(lambda: self.trading_client.submit_order(req))

    def cancel_order(self, order_id: str):
        """Cancel an order by Alpaca order ID."""
        return self._call(lambda: self.trading_client.cancel_order_by_id(order_id))

    def get_order(self, order_id: str):
        """Get order details by Alpaca order ID."""
        return self._call(lambda: self.trading_client.get_order_by_id(order_id))

    def get_orders(self, status: str = "open", limit: int = 100):
        """List orders by status."""
        _status = getattr(QueryOrderStatus, status.upper(), QueryOrderStatus.OPEN)
        req = GetOrdersRequest(status=_status, limit=limit)
        return self._call(lambda: self.trading_client.get_orders(req))

    # ── Market Data ──────────────────────────────────────────

    def get_bars(
        self,
        symbol: str,
        timeframe: str = "1Day",
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
        limit: Optional[int] = None,
    ):
        """Get historical bars for a symbol.

        Args:
            symbol: Ticker.
            timeframe: '1Min', '5Min', '1Hour', '1Day', etc.
            start: Start datetime.
            end: End datetime.
            limit: Max bars to return.
        """
        tf_map = {
            "1Min": TimeFrame.Minute,
            "5Min": TimeFrame(5, "Min"),
            "15Min": TimeFrame(15, "Min"),
            "1Hour": TimeFrame.Hour,
            "1Day": TimeFrame.Day,
        }
        tf = tf_map.get(timeframe, TimeFrame.Day)

        params = dict(symbol_or_symbols=symbol, timeframe=tf)
        if start:
            params["start"] = start
        if end:
            params["end"] = end
        if limit:
            params["limit"] = limit

        req = StockBarsRequest(**params)
        return self._call(lambda: self.data_client.get_stock_bars(req))

    def get_latest_quotes(self, symbols: List[str]):
        """Get latest quotes for a list of symbols."""
        req = StockLatestQuoteRequest(symbol_or_symbols=symbols)
        return self._call(lambda: self.data_client.get_stock_latest_quote(req))
