"""
ORB Range Marker — 15-minute opening range snapshot (Phase 4)

Runs at 9:45 AM ET. For each scanner candidate, computes the 15-min
opening range (high/low), measured move, and applies post-range filters.
Outputs enriched candidates to web/orb_ranges.json.

Architect notes incorporated:
- Grace window: retry if bar count < 15 (1-2 min lag on IEX)
- Validate bar count before marking range final
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Tuple
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")

try:
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest, StockSnapshotRequest
    from alpaca.data.enums import DataFeed
    from alpaca.data.timeframe import TimeFrame
except ImportError:
    StockHistoricalDataClient = None
    StockBarsRequest = None
    StockSnapshotRequest = None
    DataFeed = None
    TimeFrame = None


class ORBRangeMarker:
    """Computes 15-min opening range for scanner candidates."""

    def __init__(self, config: dict, data_client=None):
        """config = full orb_config['orb'] (needs scanner + entry sections)."""
        self.config = config or {}
        entry = self.config.get("entry", {})
        self.spread_max_pct = float(entry.get("spread_max_pct", 0.003))

        # Post-range filter thresholds
        self.mm_min = 1.50       # measured move > $1.50
        self.mm_max_pct = 0.03   # measured move < 3% of price
        self.range_min_pct = 0.003  # range width > 0.3% of price

        self._client = data_client
        if self._client is None and StockHistoricalDataClient is not None:
            api_key = os.environ.get("ALPACA_API_KEY", "")
            api_secret = os.environ.get("ALPACA_SECRET_KEY") or os.environ.get("ALPACA_API_SECRET", "")
            if api_key and api_secret:
                self._client = StockHistoricalDataClient(api_key=api_key, secret_key=api_secret)

        # Grace window retries for bar lag (architect rec)
        self.max_bar_retries = 3
        self.bar_retry_delay = 20  # seconds

    def mark_ranges(self, candidates: List[Dict]) -> List[Dict]:
        """For each candidate, compute 15-min opening range.
        Returns enriched candidates that pass post-range filters.
        """
        if not candidates:
            return []

        symbols = [c["symbol"] for c in candidates]
        ranges = self._get_15min_ranges(symbols)
        spreads = self._get_current_spreads(symbols)

        results: List[Dict] = []
        for cand in candidates:
            sym = cand["symbol"]
            rng = ranges.get(sym)
            if not rng:
                logger.info("RangeMarker: %s — no 1-min bars, skipped", sym)
                continue

            range_high, range_low, range_vol, bar_count = rng
            if range_high <= range_low:
                logger.info("RangeMarker: %s — invalid range (high <= low)", sym)
                continue

            measured_move = range_high - range_low
            midprice = (range_high + range_low) / 2.0
            mm_pct = measured_move / midprice if midprice > 0 else 0
            spread = spreads.get(sym, 0.0)

            # Post-range filters
            passes, reason = self._apply_post_range_filters(
                measured_move, mm_pct, midprice, spread
            )

            enriched = {
                **cand,
                "range_high": round(range_high, 4),
                "range_low": round(range_low, 4),
                "range_width": round(measured_move, 4),
                "range_vol": int(range_vol),
                "bar_count": bar_count,
                "measured_move": round(measured_move, 4),
                "measured_move_pct": round(mm_pct * 100, 2),
                "spread": round(spread, 4),
                "meets_post_range_filters": passes,
                "filter_reason": reason,
            }

            if passes:
                results.append(enriched)
            else:
                logger.info("RangeMarker: %s filtered — %s", sym, reason)

        self._write_output(results)
        logger.info("RangeMarker: %d of %d candidates passed post-range filters",
                     len(results), len(candidates))
        return results

    # ── Data fetching ────────────────────────────────────────

    def _get_15min_ranges(self, symbols: List[str]) -> Dict[str, Tuple[float, float, float, int]]:
        """Batch fetch 1-min bars for 9:30-9:45 ET. Returns {sym: (high, low, vol, bar_count)}."""
        if not self._client or not StockBarsRequest or not TimeFrame:
            return {}

        today = datetime.now(ET).date()
        start = datetime(today.year, today.month, today.day, 9, 30, tzinfo=ET)
        end = datetime(today.year, today.month, today.day, 9, 45, tzinfo=ET)

        for attempt in range(self.max_bar_retries):
            try:
                req = StockBarsRequest(
                    symbol_or_symbols=symbols,
                    timeframe=TimeFrame.Minute,
                    start=start, end=end,
                    feed=DataFeed.IEX,
                )
                all_bars = self._client.get_stock_bars(req)
            except Exception as exc:
                logger.error("RangeMarker: bars fetch failed (attempt %d): %s", attempt + 1, exc)
                if attempt < self.max_bar_retries - 1:
                    time.sleep(self.bar_retry_delay)
                continue

            # Check if we have enough bars for at least some symbols
            result: Dict[str, Tuple[float, float, float, int]] = {}
            missing = []
            for sym in symbols:
                bars = self._extract_bars(all_bars, sym)
                if not bars:
                    missing.append(sym)
                    continue

                highs = [float(getattr(b, "high", 0) or 0) for b in bars]
                lows = [float(getattr(b, "low", 0) or 0) for b in bars if (getattr(b, "low", 0) or 0) > 0]
                vols = [float(getattr(b, "volume", 0) or 0) for b in bars]

                if not highs or not lows:
                    missing.append(sym)
                    continue

                result[sym] = (max(highs), min(lows), sum(vols), len(bars))

            # If most symbols have data, or we've retried enough, return
            if not missing or attempt == self.max_bar_retries - 1:
                if missing:
                    logger.warning("RangeMarker: no bars for %s after %d attempts", missing, attempt + 1)
                return result

            # Retry if significant symbols missing
            logger.info("RangeMarker: %d symbols missing bars, retrying in %ds...", len(missing), self.bar_retry_delay)
            time.sleep(self.bar_retry_delay)

        return {}

    def _get_current_spreads(self, symbols: List[str]) -> Dict[str, float]:
        """Get current bid-ask spread via snapshot."""
        if not self._client or not StockSnapshotRequest:
            return {}
        try:
            req = StockSnapshotRequest(symbol_or_symbols=symbols, feed=DataFeed.IEX)
            raw = self._client.get_stock_snapshot(req)
        except Exception as exc:
            logger.error("RangeMarker: spread snapshot failed: %s", exc)
            return {}

        result: Dict[str, float] = {}
        for sym in symbols:
            snap = raw.get(sym) if hasattr(raw, "get") else getattr(raw, "data", {}).get(sym)
            if not snap:
                continue
            quote = getattr(snap, "latest_quote", None)
            if quote and getattr(quote, "ask_price", None) and getattr(quote, "bid_price", None):
                result[sym] = float(quote.ask_price) - float(quote.bid_price)
        return result

    # ── Filters ──────────────────────────────────────────────

    def _apply_post_range_filters(self, measured_move: float, mm_pct: float,
                                   midprice: float, spread: float) -> Tuple[bool, str]:
        """Apply post-range filters. Returns (passes, reason)."""
        if measured_move < self.mm_min:
            return False, f"measured_move_too_small ({measured_move:.2f} < {self.mm_min})"

        if mm_pct > self.mm_max_pct:
            return False, f"measured_move_too_large ({mm_pct*100:.1f}% > {self.mm_max_pct*100:.1f}%)"

        range_pct = measured_move / midprice if midprice > 0 else 0
        if range_pct < self.range_min_pct:
            return False, f"range_too_narrow ({range_pct*100:.2f}% < {self.range_min_pct*100:.1f}%)"

        if midprice > 0 and spread > 0:
            spread_pct = spread / midprice
            if spread_pct > self.spread_max_pct:
                return False, f"spread_too_wide ({spread_pct*100:.2f}% > {self.spread_max_pct*100:.1f}%)"

        return True, "passed"

    # ── Helpers ──────────────────────────────────────────────

    @staticmethod
    def _extract_bars(bars_resp, symbol: str) -> list:
        if hasattr(bars_resp, "get"):
            return list(bars_resp.get(symbol, []))
        if hasattr(bars_resp, "data"):
            return list(bars_resp.data.get(symbol, []))
        return []

    def _write_output(self, candidates: List[Dict]) -> None:
        out = Path("web/orb_ranges.json")
        out.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "timestamp": datetime.now(ET).isoformat(),
            "count": len(candidates),
            "candidates": candidates,
        }
        try:
            out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        except Exception as exc:
            logger.error("RangeMarker: failed to write output: %s", exc)
