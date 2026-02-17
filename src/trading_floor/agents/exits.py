from __future__ import annotations
import math


class ExitManager:
    def __init__(self, cfg, tracer):
        self.cfg = cfg
        self.tracer = tracer
        # Hard stop fallback (used when ATR unavailable)
        self.hard_stop = cfg.get("risk", {}).get("stop_loss", 0.02)
        # ATR-based stop config
        self.atr_multiplier = cfg.get("risk", {}).get("atr_stop_multiplier", 2.0)
        self.atr_period = cfg.get("risk", {}).get("atr_period", 14)
        # Trailing stop config
        self.breakeven_trigger = cfg.get("risk", {}).get("trailing_breakeven_trigger", 0.015)
        self.trail_trigger = cfg.get("risk", {}).get("trailing_trigger", 0.025)
        self.trail_pct = cfg.get("risk", {}).get("trailing_pct", 0.012)
        # Take profit
        self.take_profit = cfg.get("risk", {}).get("take_profit", 0.05)
        # Portfolio kill switch
        self.portfolio_kill_pct = cfg.get("risk", {}).get("portfolio_kill_pct", 0.05)
        # Max positions
        self.max_positions = cfg.get("risk", {}).get("max_positions", 3)

    def _calc_atr_stop(self, sym: str, price_data: dict, entry_price: float) -> float:
        """Calculate ATR-based stop distance as a percentage.
        Returns the stop percentage (e.g., 0.025 for 2.5%).
        Falls back to hard_stop if ATR can't be calculated.
        """
        if not price_data or sym not in price_data:
            return self.hard_stop

        try:
            import pandas as pd
            df = price_data[sym]
            # df might be just a close Series — need OHLC for true ATR
            # If we only have close, approximate ATR from close-to-close changes
            if isinstance(df, pd.Series):
                # Approximate: use rolling std of returns * sqrt(1) as ATR proxy
                returns = df.pct_change().dropna()
                if len(returns) < self.atr_period:
                    return self.hard_stop
                atr_proxy = returns.rolling(self.atr_period).std().iloc[-1]
                if math.isnan(atr_proxy) or atr_proxy <= 0:
                    return self.hard_stop
                stop_pct = float(atr_proxy * self.atr_multiplier)
            elif isinstance(df, pd.DataFrame):
                # True ATR if we have high/low/close columns
                h = df['high'] if 'high' in df.columns else None
                l = df['low'] if 'low' in df.columns else None
                c = df['close']
                if h is not None and l is not None:
                    tr = pd.concat([
                        h - l,
                        (h - c.shift(1)).abs(),
                        (l - c.shift(1)).abs()
                    ], axis=1).max(axis=1)
                    atr = tr.rolling(self.atr_period).mean().iloc[-1]
                    if math.isnan(atr) or atr <= 0:
                        return self.hard_stop
                    stop_pct = float((atr / entry_price) * self.atr_multiplier)
                else:
                    return self.hard_stop
            else:
                return self.hard_stop

            # Clamp: minimum 0.5%, maximum 5%
            stop_pct = max(0.005, min(0.05, stop_pct))
            return stop_pct

        except Exception:
            return self.hard_stop

    def check_exits(self, context) -> dict:
        """
        Check positions for exits. Returns {symbol: "SELL"|"BUY"}.

        Logic (layered):
          1. Portfolio kill switch: if total unrealized loss > portfolio_kill_pct, exit ALL
          2. ATR-based hard stop: adaptive per-symbol (2x ATR)
          3. Breakeven stop: once up >1.5%, stop moves to entry price
          4. Trailing stop: once up >2.5%, trail 1.2% below high/low watermark
          5. Take profit: exit at +5%
        """
        self.tracer.emit_span("exit_manager.check", {"positions": len(context.get("positions", []))})

        forced_exits = {}
        portfolio = context.get("portfolio_obj")
        if not portfolio:
            return {}

        price_data = context.get("price_data", {})

        # --- Portfolio Kill Switch ---
        total_unrealized = sum(
            pos.unrealized_pnl for pos in portfolio.state.positions.values()
        )
        equity = context.get("portfolio_equity", portfolio.state.equity)
        if equity > 0 and total_unrealized < 0:
            loss_pct = abs(total_unrealized) / equity
            if loss_pct >= self.portfolio_kill_pct:
                print(f"[ExitManager] PORTFOLIO KILL SWITCH: unrealized loss {loss_pct:.1%} >= {self.portfolio_kill_pct:.0%} threshold. Closing ALL positions.")
                for sym, pos in portfolio.state.positions.items():
                    forced_exits[sym] = "SELL" if pos.quantity > 0 else "BUY"
                return forced_exits

        # --- Per-Position Checks ---
        for sym, pos in portfolio.state.positions.items():
            if pos.current_price <= 0 or pos.avg_price <= 0:
                continue

            # Calculate ATR-based stop for this symbol
            atr_stop = self._calc_atr_stop(sym, price_data, pos.avg_price)

            if pos.quantity > 0:  # Long
                entry_pnl_pct = (pos.current_price - pos.avg_price) / pos.avg_price
                hwm = pos.highest_price if pos.highest_price > 0 else pos.avg_price
                drawdown_from_hwm = (pos.current_price - hwm) / hwm

                # 1. Take profit
                if entry_pnl_pct >= self.take_profit:
                    print(f"[ExitManager] {sym} TAKE PROFIT: +{entry_pnl_pct:.1%} >= +{self.take_profit:.0%}")
                    forced_exits[sym] = "SELL"
                    continue

                # 2. ATR-based hard stop
                if entry_pnl_pct <= -atr_stop:
                    print(f"[ExitManager] {sym} ATR STOP: {entry_pnl_pct:.1%} <= -{atr_stop:.1%}")
                    forced_exits[sym] = "SELL"
                    continue

                # 3. Trailing stop
                peak_gain = (hwm - pos.avg_price) / pos.avg_price
                if peak_gain >= self.trail_trigger:
                    if drawdown_from_hwm <= -self.trail_pct:
                        print(f"[ExitManager] {sym} TRAILING STOP: dropped {drawdown_from_hwm:.1%} from HWM")
                        forced_exits[sym] = "SELL"
                        continue

                # 4. Breakeven stop
                elif peak_gain >= self.breakeven_trigger:
                    if entry_pnl_pct <= 0:
                        print(f"[ExitManager] {sym} BREAKEVEN STOP: was up {peak_gain:.1%}, now flat/negative")
                        forced_exits[sym] = "SELL"
                        continue

            elif pos.quantity < 0:  # Short
                entry_pnl_pct = (pos.avg_price - pos.current_price) / pos.avg_price
                lwm = pos.lowest_price if pos.lowest_price > 0 else pos.avg_price
                drawup_from_lwm = (pos.current_price - lwm) / lwm

                # 1. Take profit
                if entry_pnl_pct >= self.take_profit:
                    print(f"[ExitManager] {sym} TAKE PROFIT: +{entry_pnl_pct:.1%} >= +{self.take_profit:.0%}")
                    forced_exits[sym] = "BUY"
                    continue

                # 2. ATR-based hard stop
                if entry_pnl_pct <= -atr_stop:
                    print(f"[ExitManager] {sym} ATR STOP: {entry_pnl_pct:.1%} <= -{atr_stop:.1%}")
                    forced_exits[sym] = "BUY"
                    continue

                # 3. Trailing stop
                peak_gain = (pos.avg_price - lwm) / pos.avg_price
                if peak_gain >= self.trail_trigger:
                    if drawup_from_lwm >= self.trail_pct:
                        print(f"[ExitManager] {sym} TRAILING STOP: rose {drawup_from_lwm:.1%} from LWM")
                        forced_exits[sym] = "BUY"
                        continue

                # 4. Breakeven stop
                elif peak_gain >= self.breakeven_trigger:
                    if entry_pnl_pct <= 0:
                        print(f"[ExitManager] {sym} BREAKEVEN STOP: was up {peak_gain:.1%}, now flat/negative")
                        forced_exits[sym] = "BUY"
                        continue

        return forced_exits

    def check_max_positions(self, portfolio, new_plans: list) -> list:
        """Enforce max positions: reject new entries if at capacity.
        Returns filtered plans list.
        """
        current_count = len(portfolio.state.positions)
        available_slots = max(0, self.max_positions - current_count)

        if available_slots >= len(new_plans):
            return new_plans

        print(f"[ExitManager] Position cap: {current_count}/{self.max_positions} filled. "
              f"Allowing {available_slots} of {len(new_plans)} new trades.")

        # Sort by absolute conviction, take top N
        sorted_plans = sorted(new_plans, key=lambda p: abs(p.get("score", 0)), reverse=True)
        return sorted_plans[:available_slots]
