"""Intraday strategy — wraps the existing TradingFloor 13-gate pipeline.

Does NOT rewrite the pipeline; delegates scan logic to workflow.TradingFloor
and routes execution through the Phase 1 ExecutionService + StrategyBudgeter.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from trading_floor.strategies.base import BaseStrategy, Signal, ET

logger = logging.getLogger(__name__)

STRATEGY_NAME = "intraday"


class IntradayStrategy(BaseStrategy):
    """Wraps the legacy TradingFloor pipeline as a strategy engine.

    Args:
        cfg: Full config dict (workflow.yaml merged with overrides).
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

        strat_cfg = cfg.get("strategies", {}).get("intraday", {})
        self.budget = strat_cfg.get("budget", 2000)
        self.max_positions = strat_cfg.get("max_positions", 3)
        self.take_profit_pct = strat_cfg.get("take_profit", 0.025)
        self.stop_loss_atr = strat_cfg.get("stop_loss_atr", 2.0)
        self.close_by = strat_cfg.get("close_by", "15:45")
        self.exclusions = strat_cfg.get("universe_exclude", [])
        self.min_shares = cfg.get("broker", {}).get("min_shares", 10)
        self.entry_start = "09:30"
        self.entry_end = "11:30"

        # Lazy-init the legacy TradingFloor — only created when scan() called
        self._legacy_floor = None

    def _get_legacy_floor(self):
        """Lazy-create the legacy TradingFloor wrapper."""
        if self._legacy_floor is None:
            from trading_floor.workflow import TradingFloor as LegacyFloor
            self._legacy_floor = LegacyFloor(self.cfg)
        return self._legacy_floor

    # ── scan ─────────────────────────────────────────────────

    def scan(self, market_data: Any = None) -> List[Signal]:
        """Run the legacy 13-gate pipeline and capture signals.

        The legacy TradingFloor.run() does everything (scout, scoring,
        challenge, filters, execution via Portfolio). We call its internal
        scoring to extract signals without executing trades in the legacy path.
        """
        now = datetime.now(ET)
        if not self.is_in_time_window(self.entry_start, self.entry_end, now):
            logger.info("IntradayStrategy: outside entry window %s-%s",
                        self.entry_start, self.entry_end)
            return []

        floor = self._get_legacy_floor()

        # Use the legacy scout + signal scoring to generate signals
        # We replicate the data-fetch + scoring portion without executing trades
        from trading_floor.data import YahooDataProvider, filter_trading_window, latest_timestamp

        data_provider = YahooDataProvider(
            interval=self.cfg.get("data", {}).get("interval", "5m"),
            lookback=self.cfg.get("data", {}).get("lookback", "5d"),
        )

        universe = self.filter_universe(self.cfg.get("universe", []), self.exclusions)
        fetch_list = list(set(universe + ["SPY", "^VIX"]))
        md = data_provider.fetch(fetch_list)

        windowed = {}
        current_prices = {}
        for sym, m in md.items():
            if sym not in universe:
                continue
            windowed[sym] = filter_trading_window(
                m.df,
                tz=self.cfg["hours"]["tz"],
                start=self.cfg["hours"]["start"],
                end=self.cfg["hours"]["end"],
            )
            if not m.df.empty:
                current_prices[sym] = m.df["close"].iloc[-1]

        ranked = floor.scout.rank(windowed)
        scout_top_n = self.cfg.get("scout_top_n", 5)
        top_symbols = set(r["symbol"] for r in ranked[:scout_top_n])

        weights = self.cfg.get("signals", {}).get("weights", {})
        threshold = self.cfg.get("signals", {}).get("trade_threshold", 0.25)
        timestamp = latest_timestamp()

        signals: List[Signal] = []
        for sym, df in windowed.items():
            if df.empty or sym not in top_symbols:
                continue

            mom_raw = floor.signal_mom.score(df)
            mean_raw = floor.signal_mean.score(df)
            brk_raw = floor.signal_break.score(df)
            news_raw = floor.signal_news.get_sentiment(sym)

            raw_scores = {
                "momentum": mom_raw,
                "meanrev": mean_raw,
                "breakout": brk_raw,
                "news": news_raw if news_raw else 0.0,
            }

            score = self.score_signals(weights, raw_scores)

            if abs(score) < threshold:
                continue

            side = "buy" if score > 0 else "sell"

            # Compute ATR for stop loss
            atr = self._calc_atr(df)
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
                    "atr": atr,
                },
            ))

        signals.sort(key=lambda s: abs(s.score), reverse=True)
        logger.info("IntradayStrategy scan: %d signals", len(signals))
        return signals

    def _calc_atr(self, df, period: int = 14) -> float:
        """Calculate ATR from a DataFrame with high/low/close columns."""
        try:
            if len(df) < period + 1:
                return 0.0
            import pandas as pd
            h = df["high"] if "high" in df.columns else df["close"]
            l = df["low"] if "low" in df.columns else df["close"]
            c = df["close"]
            tr = pd.concat([
                h - l,
                (h - c.shift(1)).abs(),
                (l - c.shift(1)).abs(),
            ], axis=1).max(axis=1)
            return float(tr.rolling(period).mean().iloc[-1])
        except Exception:
            return 0.0

    # ── execute ──────────────────────────────────────────────

    def execute(self, signals: List[Signal]) -> List[Dict[str, Any]]:
        """Submit orders for approved intraday signals via ExecutionService."""
        results = []

        # Check current intraday position count
        open_count = self._open_position_count()

        for sig in signals:
            if open_count >= self.max_positions:
                logger.info("IntradayStrategy: max positions %d reached, skipping %s",
                            self.max_positions, sig.symbol)
                results.append({"status": "rejected", "reason": "max_positions",
                                "symbol": sig.symbol})
                continue

            price = sig.metadata.get("price", 0.0)
            atr = sig.metadata.get("atr", 0.0)
            if price <= 0:
                results.append({"status": "rejected", "reason": "no_price",
                                "symbol": sig.symbol})
                continue

            # Budget check
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

            # Bracket: TP at +2.5%, SL at entry - 2*ATR
            tp_price = round(price * (1 + self.take_profit_pct), 2)
            sl_price = round(price - self.stop_loss_atr * atr, 2) if atr > 0 else round(price * 0.98, 2)

            # Create position_meta record
            pos_id = self._create_position_meta(sig, price, qty, sl_price, tp_price)

            ts = int(datetime.now(timezone.utc).timestamp() * 1000)
            client_oid = f"intraday_{sig.symbol}_{ts}"

            result = self.exec_svc.submit(
                symbol=sig.symbol,
                qty=qty,
                side=sig.side,
                strategy=STRATEGY_NAME,
                order_type="market",
                estimated_cost=estimated_cost,
                position_meta_id=pos_id,
                take_profit={"limit_price": tp_price},
                stop_loss={"stop_price": sl_price},
            )
            results.append(result)

            if result.get("status") == "submitted":
                open_count += 1

        return results

    # ── manage_exits ─────────────────────────────────────────

    def manage_exits(self) -> List[Dict[str, Any]]:
        """Check open intraday positions for trailing stop / exit conditions."""
        actions = []
        positions = self._get_open_positions()
        for pos in positions:
            sym = pos["symbol"]
            try:
                alpaca_pos = next(
                    (p for p in self.exec_svc.portfolio.positions if p["symbol"] == sym),
                    None,
                )
                if not alpaca_pos:
                    continue

                unrealized_plpc = alpaca_pos.get("unrealized_plpc", 0)
                # Trailing stop: if gain >= 2.5%, tighten stop to breakeven
                if unrealized_plpc >= 0.025:
                    entry_price = alpaca_pos["avg_entry_price"]
                    # Update stop to breakeven in position_meta
                    self._update_stop(pos["id"], entry_price)
                    actions.append({"symbol": sym, "action": "trail_to_breakeven"})
            except Exception as e:
                logger.warning("IntradayStrategy exit check error for %s: %s", sym, e)
        return actions

    # ── force_close ──────────────────────────────────────────

    def force_close(self) -> List[Dict[str, Any]]:
        """Close ALL open intraday positions (called at 3:45 PM)."""
        results = []
        positions = self._get_open_positions()
        for pos in positions:
            sym = pos["symbol"]
            try:
                # Find qty from Alpaca
                alpaca_pos = next(
                    (p for p in self.exec_svc.portfolio.positions if p["symbol"] == sym),
                    None,
                )
                if not alpaca_pos:
                    # Position exists in meta but not in broker — mark closed
                    self._close_position_meta(pos["id"], exit_reason="time")
                    continue

                qty = abs(alpaca_pos["qty"])
                result = self.exec_svc.submit(
                    symbol=sym,
                    qty=qty,
                    side="sell",
                    strategy=STRATEGY_NAME,
                    order_type="market",
                    position_meta_id=pos["id"],
                )
                if result.get("status") == "submitted":
                    self._close_position_meta(pos["id"], exit_reason="time")
                results.append(result)
            except Exception as e:
                logger.error("IntradayStrategy force_close error for %s: %s", sym, e)
                results.append({"status": "error", "symbol": sym, "reason": str(e)})
        return results

    # ── helpers ───────────────────────────────────────────────

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
                "SELECT id, symbol, entry_price, entry_qty, stop_price, tp_price "
                "FROM position_meta WHERE strategy=? AND status='open'",
                (STRATEGY_NAME,),
            )
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]
        finally:
            conn.close()

    def _create_position_meta(self, sig: Signal, price: float, qty: int,
                               sl_price: float, tp_price: float) -> int:
        conn = self.db._get_conn()
        try:
            import json
            cur = conn.cursor()
            cur.execute(
                """INSERT INTO position_meta
                   (symbol, strategy, side, entry_price, entry_time, entry_qty,
                    stop_price, tp_price, signals_json, status)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (sig.symbol, STRATEGY_NAME, sig.side, price,
                 datetime.now(timezone.utc).isoformat(), qty,
                 sl_price, tp_price, json.dumps(sig.scores_breakdown), "open"),
            )
            conn.commit()
            return cur.lastrowid
        finally:
            conn.close()

    def _close_position_meta(self, pos_id: int, exit_reason: str = "manual"):
        conn = self.db._get_conn()
        try:
            conn.execute(
                "UPDATE position_meta SET status='closed', exit_reason=?, exit_time=? WHERE id=?",
                (exit_reason, datetime.now(timezone.utc).isoformat(), pos_id),
            )
            conn.commit()
        finally:
            conn.close()

    def _update_stop(self, pos_id: int, new_stop: float):
        conn = self.db._get_conn()
        try:
            conn.execute(
                "UPDATE position_meta SET stop_price=? WHERE id=?",
                (new_stop, pos_id),
            )
            conn.commit()
        finally:
            conn.close()
