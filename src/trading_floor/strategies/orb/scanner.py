"""
ORB Scanner — pre-market gap candidate selection (Phase 4)

Runs at 9:25 AM ET. Scans universe for stocks gapping 2-5% with volume.
Outputs ranked candidates to web/orb_candidates.json.

Architect notes incorporated:
- Stale report.json fallback to neutral alignment (1.0)
- IEX pre-market data may be spotty — scan is best-effort
- Batch requests to minimize rate limit impact
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Tuple
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")

try:
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockSnapshotRequest, StockBarsRequest
    from alpaca.data.enums import DataFeed
    from alpaca.data.timeframe import TimeFrame
except ImportError:
    StockHistoricalDataClient = None
    StockSnapshotRequest = None
    StockBarsRequest = None
    DataFeed = None
    TimeFrame = None

from trading_floor.sector_map import SECTOR_MAP, get_sector


class ORBScanner:
    """Pre-market gap scanner for ORB desk."""

    def __init__(self, config: dict, data_client=None):
        """config = orb_config['orb']['scanner'] section."""
        self.config = config or {}
        self.gap_min_pct = float(self.config.get("gap_min_pct", 2.0))
        self.gap_max_pct = float(self.config.get("gap_max_pct", 5.0))
        self.premarket_vol_min = int(self.config.get("premarket_vol_min", 300_000))
        self.atr_min = float(self.config.get("atr_min", 1.50))
        self.price_min = float(self.config.get("price_min", 10))
        self.price_max = float(self.config.get("price_max", 500))
        self.avg_daily_vol_min = int(self.config.get("avg_daily_vol_min", 1_000_000))
        self.max_candidates = int(self.config.get("max_candidates", 8))

        self._client = data_client
        if self._client is None and StockHistoricalDataClient is not None:
            api_key = os.environ.get("ALPACA_API_KEY", "")
            api_secret = os.environ.get("ALPACA_SECRET_KEY") or os.environ.get("ALPACA_API_SECRET", "")
            if api_key and api_secret:
                self._client = StockHistoricalDataClient(api_key=api_key, secret_key=api_secret)

        self._avg_vol_cache: Dict[str, float] = {}
        self._sector_scores: Dict[str, float] | None = None

    def scan(self) -> List[Dict]:
        """Run pre-market scan. Returns ranked candidates."""
        try:
            symbols = self._get_universe()
            if not symbols:
                logger.warning("Scanner: empty universe")
                return []

            snapshots = self._get_snapshots(symbols)
            if not snapshots:
                logger.warning("Scanner: no snapshots returned")
                return []

            # Batch fetch daily bars for prev_close + avg volume + ATR
            daily_data = self._get_daily_data(symbols)

            candidates: List[Dict] = []
            for sym in symbols:
                snap = snapshots.get(sym)
                if not snap:
                    continue

                price = snap.get("price")
                premarket_vol = snap.get("volume", 0)
                prev_close = snap.get("prev_close") or daily_data.get(sym, {}).get("prev_close")

                if not price or not prev_close or prev_close <= 0:
                    continue

                # 1. Price filter
                if price < self.price_min or price > self.price_max:
                    continue

                # 2. Gap filter
                gap_pct = ((price - prev_close) / prev_close) * 100.0
                gap_abs = abs(gap_pct)
                if gap_abs < self.gap_min_pct or gap_abs > self.gap_max_pct:
                    continue
                gap_dir = "up" if gap_pct > 0 else "down"

                # 3. Pre-market volume
                if premarket_vol < self.premarket_vol_min:
                    continue

                # 4. ATR(14)
                atr14 = daily_data.get(sym, {}).get("atr14", 0.0)
                if atr14 < self.atr_min:
                    continue

                # 5. Average daily volume
                avg_vol = daily_data.get(sym, {}).get("avg_vol", 0)
                if avg_vol < self.avg_daily_vol_min:
                    continue

                # 6. Earnings filter — TODO: add earnings calendar check

                # 7. Sector alignment (boost score, don't filter out)
                aligned, sector_mult = self._check_sector_alignment(sym, gap_dir)
                sector = get_sector(sym).get("sector", "")

                score = gap_abs * sector_mult * atr14

                candidates.append({
                    "symbol": sym,
                    "gap_pct": round(gap_pct, 2),
                    "gap_dir": gap_dir,
                    "premarket_vol": int(premarket_vol),
                    "atr14": round(atr14, 2),
                    "prev_close": round(prev_close, 2),
                    "sector": sector,
                    "sector_alignment": round(sector_mult, 2),
                    "score": round(score, 2),
                    "reason": "passed_filters",
                })

            ranked = sorted(candidates, key=lambda x: x["score"], reverse=True)[:self.max_candidates]
            self._write_output(ranked)
            logger.info("Scanner: %d candidates from %d symbols", len(ranked), len(symbols))
            return ranked

        except Exception as exc:
            logger.error("ORBScanner.scan failed: %s", exc)
            return []

    # ── Data fetching ────────────────────────────────────────

    def _get_universe(self) -> List[str]:
        """Symbols from sector_map (ETFs excluded)."""
        return sorted(s for s, info in SECTOR_MAP.items() if info.get("sector") != "ETF")

    def _get_snapshots(self, symbols: List[str]) -> Dict[str, Dict]:
        """Batch snapshot for price + volume + prev_close."""
        if not self._client or not StockSnapshotRequest:
            return {}
        try:
            req = StockSnapshotRequest(symbol_or_symbols=symbols, feed=DataFeed.IEX)
            raw = self._client.get_stock_snapshot(req)
        except Exception as exc:
            logger.error("Scanner snapshot failed: %s", exc)
            return {}

        result: Dict[str, Dict] = {}
        for sym in symbols:
            snap = raw.get(sym) if hasattr(raw, "get") else getattr(raw, "data", {}).get(sym)
            if not snap:
                continue

            price = None
            if getattr(snap, "latest_trade", None) and getattr(snap.latest_trade, "price", None) is not None:
                price = float(snap.latest_trade.price)
            elif getattr(snap, "minute_bar", None) and getattr(snap.minute_bar, "close", None) is not None:
                price = float(snap.minute_bar.close)
            if price is None:
                continue

            volume = 0
            if getattr(snap, "daily_bar", None) and getattr(snap.daily_bar, "volume", None) is not None:
                volume = int(snap.daily_bar.volume)

            prev_close = None
            if getattr(snap, "previous_daily_bar", None) and getattr(snap.previous_daily_bar, "close", None) is not None:
                prev_close = float(snap.previous_daily_bar.close)

            result[sym] = {"price": price, "volume": volume, "prev_close": prev_close}
        return result

    def _get_daily_data(self, symbols: List[str]) -> Dict[str, Dict]:
        """Batch fetch daily bars for prev_close, avg volume, ATR(14)."""
        if not self._client or not StockBarsRequest or not TimeFrame:
            return {}

        end = datetime.now(ET)
        start = end - timedelta(days=45)  # ~20 trading days + buffer
        try:
            req = StockBarsRequest(
                symbol_or_symbols=symbols,
                timeframe=TimeFrame.Day,
                start=start, end=end,
                feed=DataFeed.IEX,
            )
            all_bars = self._client.get_stock_bars(req)
        except Exception as exc:
            logger.error("Scanner daily bars failed: %s", exc)
            return {}

        result: Dict[str, Dict] = {}
        for sym in symbols:
            bars = self._extract_bars(all_bars, sym)
            if len(bars) < 2:
                continue

            # Sort by timestamp
            if hasattr(bars[0], "timestamp"):
                bars = sorted(bars, key=lambda b: b.timestamp)

            # Prev close
            prev_close = float(getattr(bars[-1], "close", 0) or 0)

            # Avg volume (last 20 bars)
            vols = [float(getattr(b, "volume", 0) or 0) for b in bars[-20:]]
            avg_vol = sum(vols) / len(vols) if vols else 0

            # ATR(14)
            atr14 = 0.0
            if len(bars) >= 15:
                trs = []
                for i in range(1, len(bars)):
                    h = float(getattr(bars[i], "high", 0) or 0)
                    l = float(getattr(bars[i], "low", 0) or 0)
                    pc = float(getattr(bars[i-1], "close", 0) or 0)
                    tr = max(h - l, abs(h - pc), abs(l - pc))
                    trs.append(tr)
                if len(trs) >= 14:
                    atr14 = sum(trs[-14:]) / 14.0

            result[sym] = {"prev_close": prev_close, "avg_vol": avg_vol, "atr14": atr14}
        return result

    def _check_sector_alignment(self, symbol: str, gap_dir: str) -> Tuple[bool, float]:
        """Check gap direction vs morning_strategy sector bias from report.json.
        Returns (aligned, multiplier). Stale/missing = neutral 1.0 (architect rec).
        """
        sector = get_sector(symbol).get("sector", "")
        if not sector:
            return False, 1.0

        if self._sector_scores is None:
            self._sector_scores = {}
            try:
                rpt = Path("web/report.json")
                if rpt.exists():
                    data = json.loads(rpt.read_text(encoding="utf-8"))
                    # Check freshness — same trading day
                    ts = data.get("timestamp", "")
                    today = datetime.now(ET).strftime("%Y-%m-%d")
                    if ts and ts[:10] == today:
                        self._sector_scores = data.get("sector_scores", {})
                    else:
                        logger.warning("Scanner: report.json stale (%s vs %s), using neutral", ts[:10] if ts else "?", today)
            except Exception:
                pass

        score = float(self._sector_scores.get(sector, 0.0))
        if score == 0:
            return False, 1.0

        aligned = (score > 0 and gap_dir == "up") or (score < 0 and gap_dir == "down")
        mag = min(abs(score), 1.0)
        return (True, 1.0 + mag) if aligned else (False, max(0.5, 1.0 - mag))

    # ── Helpers ──────────────────────────────────────────────

    @staticmethod
    def _extract_bars(bars_resp, symbol: str) -> list:
        if hasattr(bars_resp, "get"):
            return list(bars_resp.get(symbol, []))
        if hasattr(bars_resp, "data"):
            return list(bars_resp.data.get(symbol, []))
        return []

    def _write_output(self, candidates: List[Dict]) -> None:
        out = Path("web/orb_candidates.json")
        out.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "timestamp": datetime.now(ET).isoformat(),
            "count": len(candidates),
            "candidates": candidates,
        }
        try:
            out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        except Exception as exc:
            logger.error("Scanner: failed to write output: %s", exc)
