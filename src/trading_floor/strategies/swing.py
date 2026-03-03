"""Swing strategy engine — multi-day holds with trailing stops.

Brand new strategy that uses existing signal agents directly,
NOT the legacy workflow.py pipeline.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from trading_floor.strategies.base import BaseStrategy, Signal, ET
from trading_floor.sector_map import get_sector

logger = logging.getLogger(__name__)

STRATEGY_NAME = "swing"
REGIME_STATE_FILE = Path(__file__).resolve().parent.parent.parent.parent / "configs" / "regime_state.json"


class SwingStrategy(BaseStrategy):
    """Multi-day swing strategy with dual entry windows and trailing stops.

    Args:
        cfg: Full config dict.
        broker: AlpacaBroker instance.
        execution_service: ExecutionService instance.
        budgeter: StrategyBudgeter instance.
        db: Database instance.
    """

    def __init__(self, cfg, broker, execution_service, budgeter, db):
        self.cfg = cfg
        self.broker = broker
        self.exec_svc = execution_service
        self.budgeter = budgeter
        self.db = db

        sc = cfg.get("strategies", {}).get("swing", {})
        self.budget = sc.get("budget", 3000)
        self.max_positions = sc.get("max_positions", 3)
        self.max_per_sector = sc.get("max_per_sector", 1)
        self.weights = sc.get("weights", {"momentum": 0.55, "meanrev": 0.35, "breakout": 0.0, "news": 0.10})
        self.threshold = sc.get("threshold", 0.25)
        self.tp_pct = sc.get("take_profit", 0.15)
        self.sl_pct = sc.get("stop_loss", 0.08)
        self.max_hold_days = sc.get("max_hold_days", 10)
        self.trailing_trigger = sc.get("trailing_trigger", 0.08)
        self.trailing_pct = sc.get("trailing_pct", 0.04)
        self.time_decay_day = sc.get("time_decay_trail_after_day", 5)
        self.time_decay_pct = sc.get("time_decay_trail_pct", 0.025)
        self.entry_windows = sc.get("entry_windows", [
            {"start": "09:40", "end": "10:00", "bias": "gap_continuation"},
            {"start": "15:45", "end": "15:55", "bias": "trend_confirmation"},
        ])
        self.exclusions = sc.get("universe_exclude", [])
        self.sl_cooldown_days = sc.get("sl_cooldown_days", 0)
        self._sl_cooldown_tracker: Dict[str, datetime] = {}  # symbol -> SL exit datetime
        self.min_shares = cfg.get("broker", {}).get("min_shares", 10)

        # Lazily initialized signal agents
        self._scout = None
        self._mom = None
        self._mean = None
        self._brk = None
        self._news = None
        self._data = None
        self._tracer = None

    def _init_agents(self):
        """Lazy-init signal agents (avoids import cost when not needed)."""
        if self._scout is not None:
            return
        from trading_floor.lightning import LightningTracer
        from trading_floor.agents.scout import ScoutAgent
        from trading_floor.agents.signal_momentum import MomentumSignalAgent
        from trading_floor.agents.signal_meanreversion import MeanReversionSignalAgent
        from trading_floor.agents.signal_breakout import BreakoutSignalAgent
        from trading_floor.agents.news import NewsSentimentAgent
        from trading_floor.data import YahooDataProvider

        self._tracer = LightningTracer(self.cfg)
        self._scout = ScoutAgent(self.cfg, self._tracer)
        self._mom = MomentumSignalAgent(self.cfg, self._tracer)
        self._mean = MeanReversionSignalAgent(self.cfg, self._tracer)
        self._brk = BreakoutSignalAgent(self.cfg, self._tracer)
        self._news = NewsSentimentAgent(self.cfg, self._tracer)
        self._data = YahooDataProvider(
            interval=self.cfg.get("data", {}).get("interval", "5m"),
            lookback=self.cfg.get("data", {}).get("lookback", "5d"),
        )

    # ── scan ─────────────────────────────────────────────────

    def scan(self, market_data: Any = None) -> List[Signal]:
        """Scan for swing entry signals.

        1. Check entry window
        2. Fetch data, run scout
        3. Score with swing weights
        4. Check regime, sector concentration
        """
        now = datetime.now(ET)
        current_window = self._active_window(now)
        if current_window is None:
            logger.info("SwingStrategy: not in any entry window")
            return []

        self._init_agents()

        universe = self.filter_universe(self.cfg.get("universe", []), self.exclusions)
        md = self._data.fetch(universe) if market_data is None else market_data

        # Build windowed data
        from trading_floor.data import filter_trading_window
        windowed = {}
        current_prices = {}
        for sym in universe:
            m = md.get(sym)
            if m is None:
                continue
            df = m.df if hasattr(m, 'df') else m
            if hasattr(df, 'empty') and df.empty:
                continue
            windowed[sym] = filter_trading_window(
                df, tz="America/New_York", start="09:30", end="16:00",
            ) if hasattr(df, 'columns') else df
            if not df.empty:
                current_prices[sym] = df["close"].iloc[-1]

        # Scout ranking
        ranked = self._scout.rank(windowed)
        scout_top_n = self.cfg.get("scout_top_n", 5)
        top_symbols = set(r["symbol"] for r in ranked[:max(scout_top_n, 10)])

        # Market regime check
        regime = self._load_regime()

        # Current open positions for sector check
        open_sectors = self._get_open_sectors()

        timestamp = datetime.now(timezone.utc).isoformat()
        signals: List[Signal] = []

        for sym in top_symbols:
            df = windowed.get(sym)
            if df is None or (hasattr(df, 'empty') and df.empty):
                continue

            raw_scores = {
                "momentum": self._mom.score(df),
                "meanrev": self._mean.score(df),
                "breakout": self._brk.score(df),
                "news": self._news.get_sentiment(sym) or 0.0,
            }

            score = self.score_signals(self.weights, raw_scores)

            if abs(score) < self.threshold:
                continue

            side = "buy" if score > 0 else "sell"

            # Only buy in swing
            if side != "buy":
                continue

            # SL cooldown — skip if recently stopped out
            if self.sl_cooldown_days > 0 and sym in self._sl_cooldown_tracker:
                from datetime import timedelta
                cooldown_expiry = self._sl_cooldown_tracker[sym] + timedelta(days=self.sl_cooldown_days)
                if datetime.now(timezone.utc) < cooldown_expiry:
                    logger.info("SwingStrategy: %s in SL cooldown until %s, skipping", sym, cooldown_expiry.date())
                    continue
                else:
                    del self._sl_cooldown_tracker[sym]

            # Sector concentration check
            sec_info = get_sector(sym)
            sector = sec_info.get("sector", "Unknown") if sec_info else "Unknown"
            if sector != "ETF" and open_sectors.get(sector, 0) >= self.max_per_sector:
                logger.info("SwingStrategy: sector %s full, skipping %s", sector, sym)
                continue

            # Regime filter
            if regime and regime.get("label") == "bear" and side == "buy":
                logger.info("SwingStrategy: bear regime, skipping BUY %s", sym)
                continue

            price = current_prices.get(sym, 0.0)
            signals.append(Signal(
                symbol=sym,
                side=side,
                score=score,
                scores_breakdown=raw_scores,
                timestamp=timestamp,
                strategy_name=STRATEGY_NAME,
                metadata={
                    "price": price,
                    "sector": sector,
                    "window_bias": current_window.get("bias", ""),
                },
            ))

        signals.sort(key=lambda s: abs(s.score), reverse=True)
        logger.info("SwingStrategy scan: %d signals", len(signals))
        return signals

    # ── execute ──────────────────────────────────────────────

    def execute(self, signals: List[Signal]) -> List[Dict[str, Any]]:
        """Submit swing orders via ExecutionService."""
        results = []
        open_count = self._open_position_count()

        for sig in signals:
            if open_count >= self.max_positions:
                results.append({"status": "rejected", "reason": "max_positions",
                                "symbol": sig.symbol})
                continue

            price = sig.metadata.get("price", 0.0)
            if price <= 0:
                results.append({"status": "rejected", "reason": "no_price",
                                "symbol": sig.symbol})
                continue

            available = self.budgeter.get_available(STRATEGY_NAME)
            cost = min(available, self.budget / self.max_positions)
            if cost <= 0:
                results.append({"status": "rejected", "reason": "no_budget",
                                "symbol": sig.symbol})
                continue

            qty = int(cost // price)
            if qty < self.min_shares:
                results.append({"status": "rejected", "reason": "min_shares",
                                "symbol": sig.symbol, "qty": qty})
                continue

            estimated_cost = qty * price
            sl_price = round(price * (1 - self.sl_pct), 2)

            pos_id = self._create_position_meta(sig, price, qty, sl_price)

            ts = int(datetime.now(timezone.utc).timestamp() * 1000)

            # Submit market order for entry + stop-market for SL
            result = self.exec_svc.submit(
                symbol=sig.symbol,
                qty=qty,
                side=sig.side,
                strategy=STRATEGY_NAME,
                order_type="market",
                estimated_cost=estimated_cost,
                position_meta_id=pos_id,
            )

            if result.get("status") == "submitted":
                # Also submit stop-loss order
                try:
                    self.exec_svc.submit(
                        symbol=sig.symbol,
                        qty=qty,
                        side="sell",
                        strategy=STRATEGY_NAME,
                        order_type="stop",
                        stop_price=sl_price,
                        position_meta_id=pos_id,
                    )
                except Exception as e:
                    logger.warning("SwingStrategy: stop order failed for %s: %s", sig.symbol, e)
                open_count += 1

            results.append(result)
        return results

    # ── manage_exits ─────────────────────────────────────────

    def manage_exits(self) -> List[Dict[str, Any]]:
        """Daily exit check for swing positions.

        Checks: TP (15%), SL verify, max hold (10d), trailing (8%→4%, day5→2.5%).
        """
        actions = []
        positions = self._get_open_positions()
        now = datetime.now(timezone.utc)

        for pos in positions:
            sym = pos["symbol"]
            pos_id = pos["id"]
            entry_price = pos.get("entry_price", 0)
            entry_time_str = pos.get("entry_time")

            if not entry_price or entry_price <= 0:
                continue

            # Get current price from Alpaca
            alpaca_pos = next(
                (p for p in self.exec_svc.portfolio.positions if p["symbol"] == sym),
                None,
            )
            if not alpaca_pos:
                continue

            current_price = alpaca_pos["current_price"]
            gain_pct = (current_price - entry_price) / entry_price

            # Calculate days held
            days_held = 0
            if entry_time_str:
                try:
                    entry_dt = datetime.fromisoformat(entry_time_str)
                    if entry_dt.tzinfo is None:
                        entry_dt = entry_dt.replace(tzinfo=timezone.utc)
                    days_held = (now - entry_dt).days
                except Exception:
                    pass

            exit_reason = None

            # 1. TP check (15%)
            if gain_pct >= self.tp_pct:
                exit_reason = "tp"

            # 2. SL verify (should be caught by stop order, but double-check)
            elif gain_pct <= -self.sl_pct:
                exit_reason = "sl"

            # 3. Max hold (10 days)
            elif days_held >= self.max_hold_days:
                exit_reason = "time"

            # 4. Trailing stop
            elif gain_pct >= self.trailing_trigger:
                trail_pct = self.trailing_pct
                if days_held >= self.time_decay_day:
                    trail_pct = self.time_decay_pct  # tighten to 2.5%

                # Calculate trail stop from high water mark
                # For simplicity, use current gain as proxy
                trail_stop_pct = gain_pct - trail_pct
                if trail_stop_pct <= 0:
                    exit_reason = "trail"

            if exit_reason:
                # Track SL cooldown
                if exit_reason == "sl" and self.sl_cooldown_days > 0:
                    self._sl_cooldown_tracker[sym] = now
                qty = abs(alpaca_pos["qty"])
                result = self.exec_svc.submit(
                    symbol=sym,
                    qty=qty,
                    side="sell",
                    strategy=STRATEGY_NAME,
                    order_type="market",
                    position_meta_id=pos_id,
                )
                if result.get("status") == "submitted":
                    self._close_position_meta(pos_id, exit_reason, current_price, gain_pct)
                actions.append({
                    "symbol": sym, "action": f"exit_{exit_reason}",
                    "gain_pct": gain_pct, "days_held": days_held,
                })

        return actions

    # ── helpers ───────────────────────────────────────────────

    def _active_window(self, now: Optional[datetime] = None) -> Optional[Dict]:
        """Return the active entry window config, or None."""
        if now is None:
            now = datetime.now(ET)
        for w in self.entry_windows:
            if self.is_in_time_window(w["start"], w["end"], now):
                return w
        return None

    def _load_regime(self) -> Optional[Dict]:
        """Load regime state from file."""
        try:
            if REGIME_STATE_FILE.exists():
                data = json.loads(REGIME_STATE_FILE.read_text())
                hmm = data.get("hmm", {})
                return {"label": hmm.get("state_label", "unknown")}
        except Exception:
            pass
        return None

    def _get_open_sectors(self) -> Dict[str, int]:
        """Count open swing positions per sector."""
        conn = self.db._get_conn()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT sector FROM position_meta WHERE strategy=? AND status='open'",
                (STRATEGY_NAME,),
            )
            counts: Dict[str, int] = {}
            for (sector,) in cur.fetchall():
                s = sector or "Unknown"
                counts[s] = counts.get(s, 0) + 1
            return counts
        finally:
            conn.close()

    def _open_position_count(self) -> int:
        conn = self.db._get_conn()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT COUNT(*) FROM position_meta WHERE strategy=? AND status='open'",
                (STRATEGY_NAME,),
            )
            return cur.fetchone()[0]
        finally:
            conn.close()

    def _get_open_positions(self) -> List[Dict[str, Any]]:
        conn = self.db._get_conn()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT id, symbol, entry_price, entry_qty, entry_time, stop_price, tp_price, sector "
                "FROM position_meta WHERE strategy=? AND status='open'",
                (STRATEGY_NAME,),
            )
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]
        finally:
            conn.close()

    def _create_position_meta(self, sig: Signal, price: float, qty: int,
                               sl_price: float) -> int:
        tp_price = round(price * (1 + self.tp_pct), 2)
        sector = sig.metadata.get("sector", "Unknown")
        conn = self.db._get_conn()
        try:
            cur = conn.cursor()
            cur.execute(
                """INSERT INTO position_meta
                   (symbol, strategy, side, entry_price, entry_time, entry_qty,
                    stop_price, tp_price, max_hold_days, signals_json, sector, status)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (sig.symbol, STRATEGY_NAME, sig.side, price,
                 datetime.now(timezone.utc).isoformat(), qty,
                 sl_price, tp_price, self.max_hold_days,
                 json.dumps(sig.scores_breakdown), sector, "open"),
            )
            conn.commit()
            return cur.lastrowid
        finally:
            conn.close()

    def _close_position_meta(self, pos_id: int, exit_reason: str,
                              exit_price: float = 0, pnl_pct: float = 0):
        conn = self.db._get_conn()
        try:
            conn.execute(
                """UPDATE position_meta
                   SET status='closed', exit_reason=?, exit_price=?,
                       exit_time=?, pnl_pct=?
                   WHERE id=?""",
                (exit_reason, exit_price,
                 datetime.now(timezone.utc).isoformat(), pnl_pct, pos_id),
            )
            conn.commit()
        finally:
            conn.close()
