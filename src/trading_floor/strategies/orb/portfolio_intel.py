"""
Portfolio Intelligence — advisory layer for ORB desk (Phase 3)

Provides:
- Cross-desk awareness (ORB vs Swing)
- Correlation-aware sizing
- Sector exposure limits
- Net exposure tracking/logging

Architect review (B-) recs incorporated:
- Correlation cached with staleness guard (>24h = fallback to conservative)
- Sector exposure uses entry_price*qty (current price TODO for future)
- Hard block only on same-symbol-same-direction; rest is advisory
- Net exposure >90% flagged + logged
"""
from __future__ import annotations

import json
import logging
import math
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Tuple

logger = logging.getLogger(__name__)

try:
    import numpy as np  # type: ignore
except Exception:
    np = None

try:
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    from alpaca.data.enums import DataFeed
except Exception:
    StockHistoricalDataClient = None  # type: ignore
    StockBarsRequest = None
    TimeFrame = None
    DataFeed = None


class PortfolioIntelligence:
    """Advisory layer for pre-entry risk checks across ORB + Swing desks."""

    def __init__(self, db_path: str, orb_capital: float = 3000, swing_capital: float = 2000):
        self.db_path = Path(db_path)
        self.orb_capital = float(orb_capital)
        self.swing_capital = float(swing_capital)
        self.total_capital = self.orb_capital + self.swing_capital

        # Sector exposure limits (from A+ plan)
        self.orb_sector_limit = 0.4 * self.orb_capital      # $1,200
        self.total_sector_limit = 0.6 * self.total_capital   # $3,000

        # Correlation cache: {frozenset(symbols): {"matrix": {...}, "updated": datetime}}
        self._corr_cache: dict = {}
        self._corr_max_age = timedelta(hours=24)

        # Exposure log path
        self.exposure_path = Path("web") / "orb_exposure.json"

        # Alpaca client (for correlation bars)
        self._client = None
        if StockHistoricalDataClient is not None:
            api_key = os.environ.get("ALPACA_API_KEY", "")
            api_secret = os.environ.get("ALPACA_SECRET_KEY") or os.environ.get("ALPACA_API_SECRET", "")
            if api_key and api_secret:
                try:
                    self._client = StockHistoricalDataClient(api_key=api_key, secret_key=api_secret)
                except Exception as exc:
                    logger.warning("Alpaca client init failed: %s", exc)

    # ── Helpers ──────────────────────────────────────────────

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    @staticmethod
    def _normalize_side(side: str) -> str:
        s = (side or "").lower().strip()
        if s in ("buy", "long", "bull"):
            return "buy"
        if s in ("sell", "short", "bear"):
            return "sell"
        return s

    @staticmethod
    def _normalize_strategy(strategy: str) -> str:
        s = (strategy or "").lower().strip()
        if s in ("orb", "intraday"):
            return "intraday"
        return "swing" if s == "swing" else s

    def _get_open_positions(self, strategy: str = None) -> list[tuple]:
        """Returns [(symbol, side, sector, entry_price, entry_qty, strategy), ...]"""
        conn = self._connect()
        try:
            if strategy:
                strat = self._normalize_strategy(strategy)
                rows = conn.execute(
                    """SELECT symbol, side, sector, entry_price, entry_qty, strategy
                       FROM position_meta WHERE status='open' AND strategy=?""",
                    (strat,),
                ).fetchall()
            else:
                rows = conn.execute(
                    """SELECT symbol, side, sector, entry_price, entry_qty, strategy
                       FROM position_meta WHERE status='open'"""
                ).fetchall()
            return rows
        finally:
            conn.close()

    def _get_sector_exposure(self, sector: str, strategy: str = None) -> float:
        """Sum of entry_price * entry_qty for open positions in sector."""
        if not sector:
            return 0.0
        conn = self._connect()
        try:
            if strategy:
                strat = self._normalize_strategy(strategy)
                row = conn.execute(
                    """SELECT COALESCE(SUM(COALESCE(entry_price,0) * COALESCE(entry_qty,0)), 0)
                       FROM position_meta WHERE status='open' AND sector=? AND strategy=?""",
                    (sector, strat),
                ).fetchone()
            else:
                row = conn.execute(
                    """SELECT COALESCE(SUM(COALESCE(entry_price,0) * COALESCE(entry_qty,0)), 0)
                       FROM position_meta WHERE status='open' AND sector=?""",
                    (sector,),
                ).fetchone()
            return float(row[0]) if row else 0.0
        finally:
            conn.close()

    def _fetch_close_prices(self, symbols: list[str]) -> dict[str, list[float]]:
        """Fetch 20-day daily closes from Alpaca IEX."""
        if not self._client or not StockBarsRequest or not TimeFrame:
            logger.warning("Alpaca client unavailable — correlation disabled")
            return {}

        end = datetime.now(timezone.utc)
        start = end - timedelta(days=30)  # ~20 trading days

        try:
            req = StockBarsRequest(
                symbol_or_symbols=symbols,
                timeframe=TimeFrame.Day,
                start=start,
                end=end,
                feed=DataFeed.IEX if DataFeed else None,
            )
            bars = self._client.get_stock_bars(req)
        except Exception as exc:
            logger.warning("Alpaca bars fetch failed: %s", exc)
            return {}

        closes: dict[str, list[float]] = {}
        for sym in symbols:
            sym_bars = bars.get(sym, []) if hasattr(bars, "get") else getattr(bars, "data", {}).get(sym, [])
            if not sym_bars:
                continue
            if hasattr(sym_bars[0], "timestamp"):
                sym_bars = sorted(sym_bars, key=lambda b: b.timestamp)
            series = [float(b.close) for b in sym_bars if getattr(b, "close", None) is not None]
            if len(series) >= 2:
                closes[sym] = series[-20:]
        return closes

    @staticmethod
    def _returns(prices: list[float]) -> list[float]:
        if len(prices) < 2:
            return []
        return [(prices[i] / prices[i - 1]) - 1.0 for i in range(1, len(prices)) if prices[i - 1] != 0]

    def _corr(self, a: list[float], b: list[float]) -> float:
        """Pearson correlation of returns. Uses numpy if available, else manual."""
        ra, rb = self._returns(a), self._returns(b)
        if len(ra) < 2 or len(rb) < 2:
            return 0.0
        n = min(len(ra), len(rb))
        ra, rb = ra[-n:], rb[-n:]

        if np is not None:
            try:
                return float(np.corrcoef(ra, rb)[0, 1])
            except Exception:
                return 0.0

        mean_a = sum(ra) / n
        mean_b = sum(rb) / n
        num = sum((ra[i] - mean_a) * (rb[i] - mean_b) for i in range(n))
        da = sum((ra[i] - mean_a) ** 2 for i in range(n))
        db = sum((rb[i] - mean_b) ** 2 for i in range(n))
        denom = math.sqrt(da * db)
        return num / denom if denom > 0 else 0.0

    # ── Public API ─────────────────────────────────────────────

    def check_cross_desk(self, symbol: str, side: str, sector: str = "") -> tuple[bool, str]:
        """Check ORB entry against swing desk positions.

        Returns (allowed, reason):
        - HARD BLOCK: same symbol, same direction → double concentration
        - ALLOW: same symbol, opposite direction → hedge
        - FLAG (advisory): same sector, both long
        - CLEAR: no conflict
        """
        side_n = self._normalize_side(side)
        swing_positions = self._get_open_positions("swing")

        for sym, s, sec, _, _, _ in swing_positions:
            if sym == symbol:
                if self._normalize_side(s) == side_n:
                    return False, "cross_desk_same_symbol_same_direction"
                return True, "cross_desk_hedge_ok"

        # Soft flag: same sector, both long
        if sector and side_n == "buy":
            for sym, s, sec, _, _, _ in swing_positions:
                if sec == sector and self._normalize_side(s) == "buy":
                    return True, "cross_desk_same_sector_long_flag"

        return True, "clear"

    def get_correlation_adjustment(self, symbol: str, current_positions: list[str]) -> float:
        """Returns sizing multiplier (0.5–1.0) based on correlation with held names.

        Uses cached correlation data with 24h staleness guard (architect rec #2).
        If stale or unavailable, returns 1.0 (conservative: no reduction).
        """
        others = [s for s in current_positions if s and s != symbol]
        if not others:
            return 1.0

        all_syms = sorted(set([symbol] + others))
        cache_key = frozenset(all_syms)

        # Check cache
        cached = self._corr_cache.get(cache_key)
        if cached:
            age = datetime.now(timezone.utc) - cached["updated"]
            if age <= self._corr_max_age:
                for other in others:
                    pair = frozenset([symbol, other])
                    corr = cached["pairs"].get(pair, 0.0)
                    if corr >= 0.7:
                        logger.info("Cached correlation %s vs %s: %.3f → 0.5x", symbol, other, corr)
                        return 0.5
                return 1.0
            else:
                logger.warning("Correlation cache stale (%s old) — refetching", age)

        # Fetch fresh data
        prices = self._fetch_close_prices(all_syms)
        if symbol not in prices:
            return 1.0

        # Compute pairwise correlations and cache
        pairs = {}
        result_mult = 1.0
        for other in others:
            if other not in prices:
                continue
            corr = self._corr(prices[symbol], prices[other])
            pairs[frozenset([symbol, other])] = corr
            logger.info("Correlation %s vs %s: %.3f", symbol, other, corr)
            if corr >= 0.7:
                result_mult = 0.5

        self._corr_cache[cache_key] = {
            "pairs": pairs,
            "updated": datetime.now(timezone.utc),
        }
        return result_mult

    def check_sector_exposure(self, sector: str, proposed_amount: float,
                              strategy: str = "orb") -> tuple[bool, float, str]:
        """Check sector $ exposure limits.

        Returns (allowed, current_pct, reason).
        - ORB desk: max 40% of $3K in one sector
        - Combined: max 60% of $5K in one sector across both desks
        """
        if not sector:
            return True, 0.0, "unknown_sector_skipped"

        orb_exposure = self._get_sector_exposure(sector, "intraday")
        total_exposure = self._get_sector_exposure(sector)

        strat = self._normalize_strategy(strategy)
        if strat == "intraday":
            orb_after = orb_exposure + proposed_amount
            total_after = total_exposure + proposed_amount
        else:
            orb_after = orb_exposure
            total_after = total_exposure + proposed_amount

        orb_pct = orb_after / self.orb_capital if self.orb_capital else 0.0
        total_pct = total_after / self.total_capital if self.total_capital else 0.0

        if strat == "intraday" and orb_after > self.orb_sector_limit:
            return False, orb_pct, "orb_sector_limit_exceeded"
        if total_after > self.total_sector_limit:
            return False, total_pct, "total_sector_limit_exceeded"

        return True, max(orb_pct, total_pct), "ok"

    def get_net_exposure(self) -> dict:
        """Calculate and log net exposure across ORB positions.

        Flags if >90% in one direction (beta, not alpha).
        Writes to web/orb_exposure.json.
        """
        orb_positions = self._get_open_positions("intraday")

        long_amt = 0.0
        short_amt = 0.0
        for sym, side, sec, price, qty, strat in orb_positions:
            amount = (price or 0.0) * (qty or 0.0)
            if self._normalize_side(side) == "buy":
                long_amt += amount
            else:
                short_amt += amount

        net = long_amt - short_amt
        total = long_amt + short_amt
        net_pct = abs(net) / total if total > 0 else 0.0
        bias = "long" if net > 0 else ("short" if net < 0 else "flat")
        flagged = total > 0 and net_pct > 0.9

        if flagged:
            logger.warning("Net exposure >90%%: %.1f%% %s ($%.0f net of $%.0f)", net_pct * 100, bias, net, total)

        result = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "long": round(long_amt, 2),
            "short": round(short_amt, 2),
            "net": round(net, 2),
            "net_pct": round(net_pct, 4),
            "bias": bias,
            "flagged": flagged,
        }

        try:
            self.exposure_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.exposure_path, "w", encoding="utf-8") as f:
                json.dump(result, f, indent=2)
        except Exception as exc:
            logger.warning("Failed to write exposure log: %s", exc)

        return result

    def pre_entry_check(self, symbol: str, side: str, sector: str,
                        proposed_amount: float) -> dict:
        """Combined pre-entry intelligence check.

        Returns:
            {
                "allowed": bool,
                "checks": [{"name": str, "allowed": bool, "reason": str, ...}, ...],
                "sizing_mult": float,
            }
        """
        checks = []
        allowed = True
        sizing_mult = 1.0

        # 1. Cross-desk check (hard block first — architect rec #6)
        cross_ok, cross_reason = self.check_cross_desk(symbol, side, sector)
        checks.append({"name": "cross_desk", "allowed": cross_ok, "reason": cross_reason})
        if not cross_ok:
            allowed = False

        # 2. Sector exposure check
        sect_ok, sect_pct, sect_reason = self.check_sector_exposure(sector, proposed_amount, "orb")
        checks.append({"name": "sector_exposure", "allowed": sect_ok, "pct": round(sect_pct, 4), "reason": sect_reason})
        if not sect_ok:
            allowed = False

        # 3. Correlation sizing (advisory — adjusts size, doesn't block)
        open_syms = [s for s, _, _, _, _, _ in self._get_open_positions("intraday")]
        corr_mult = self.get_correlation_adjustment(symbol, open_syms)
        sizing_mult = min(sizing_mult, corr_mult)
        checks.append({"name": "correlation", "sizing_mult": corr_mult,
                        "reason": "correlated" if corr_mult < 1.0 else "ok"})

        # 4. Net exposure (advisory — log only)
        exposure = self.get_net_exposure()
        checks.append({"name": "net_exposure", "flagged": exposure["flagged"],
                        "bias": exposure["bias"], "net_pct": exposure["net_pct"]})

        return {
            "allowed": allowed,
            "checks": checks,
            "sizing_mult": sizing_mult,
        }
