"""Read-only portfolio state with short-lived caching.

Syncs with Alpaca on each call but caches results for 5 seconds
to avoid unnecessary API rate-limit consumption.
"""

import time
import logging
from typing import Dict, List, Optional, Any

logger = logging.getLogger(__name__)

_CACHE_TTL = 5.0  # seconds


class _CachedValue:
    """Simple TTL cache for a single value."""

    def __init__(self, ttl: float = _CACHE_TTL):
        self.ttl = ttl
        self._value = None
        self._fetched_at: float = 0.0

    def get(self, fetcher):
        now = time.monotonic()
        if self._value is None or (now - self._fetched_at) >= self.ttl:
            self._value = fetcher()
            self._fetched_at = now
        return self._value

    def invalidate(self):
        self._value = None
        self._fetched_at = 0.0


class PortfolioState:
    """Read-only view of the Alpaca account and positions.

    All properties sync with Alpaca on first access and cache for 5 seconds.

    Args:
        broker: An AlpacaBroker instance.
    """

    def __init__(self, broker):
        self._broker = broker
        self._account_cache = _CachedValue()
        self._positions_cache = _CachedValue()

    def invalidate(self):
        """Force refresh on next property access."""
        self._account_cache.invalidate()
        self._positions_cache.invalidate()

    # ── Account properties ───────────────────────────────────

    def _get_account(self):
        return self._account_cache.get(self._broker.get_account)

    @property
    def cash(self) -> float:
        """Available cash."""
        return float(self._get_account().cash)

    @property
    def equity(self) -> float:
        """Total account equity."""
        return float(self._get_account().equity)

    @property
    def buying_power(self) -> float:
        """Current buying power."""
        return float(self._get_account().buying_power)

    @property
    def daily_pnl(self) -> float:
        """Unrealized P&L for the day (equity - last_equity)."""
        acct = self._get_account()
        return float(acct.equity) - float(acct.last_equity)

    # ── Position properties ──────────────────────────────────

    @property
    def positions(self) -> List[Dict[str, Any]]:
        """All open positions as list of dicts."""
        raw = self._positions_cache.get(self._broker.get_positions)
        result = []
        for p in raw:
            result.append({
                "symbol": p.symbol,
                "qty": float(p.qty),
                "side": str(p.side),
                "market_value": float(p.market_value),
                "avg_entry_price": float(p.avg_entry_price),
                "unrealized_pl": float(p.unrealized_pl),
                "unrealized_plpc": float(p.unrealized_plpc),
                "current_price": float(p.current_price),
            })
        return result

    def get_position_value(self, symbol: str) -> float:
        """Get market value of a specific position, 0 if not held."""
        for p in self.positions:
            if p["symbol"] == symbol:
                return abs(p["market_value"])
        return 0.0

    def get_positions_by_strategy(self, strategy: str, db) -> List[Dict[str, Any]]:
        """Get positions belonging to a strategy (via position_meta DB table).

        Args:
            strategy: 'intraday' or 'swing'.
            db: Database instance with position_meta table.
        """
        conn = db._get_conn()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT symbol FROM position_meta WHERE strategy=? AND status='open'",
            (strategy,),
        )
        strategy_symbols = {row[0] for row in cursor.fetchall()}
        conn.close()
        return [p for p in self.positions if p["symbol"] in strategy_symbols]
