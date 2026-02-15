from __future__ import annotations


class ExitManager:
    def __init__(self, cfg, tracer):
        self.cfg = cfg
        self.tracer = tracer
        self.hard_stop = cfg.get("risk", {}).get("stop_loss", 0.02)
        # Trailing stop config
        self.breakeven_trigger = cfg.get("risk", {}).get("trailing_breakeven_trigger", 0.02)  # move SL to breakeven at +2%
        self.trail_trigger = cfg.get("risk", {}).get("trailing_trigger", 0.03)  # start trailing at +3%
        self.trail_pct = cfg.get("risk", {}).get("trailing_pct", 0.015)  # trail 1.5% below HWM

    def check_exits(self, context) -> dict:
        """
        Check positions for exits. Returns {symbol: "SELL"|"BUY"}.
        Logic:
          1. Hard stop: -2% from entry → exit
          2. Breakeven stop: once up >2%, stop moves to entry price
          3. Trailing stop: once up >3%, trail 1.5% below high watermark
        """
        self.tracer.emit_span("exit_manager.check", {"positions": len(context.get("positions", []))})

        forced_exits = {}
        portfolio = context.get("portfolio_obj")
        if not portfolio:
            return {}

        for sym, pos in portfolio.state.positions.items():
            if pos.current_price <= 0 or pos.avg_price <= 0:
                continue

            if pos.quantity > 0:  # Long
                entry_pnl_pct = (pos.current_price - pos.avg_price) / pos.avg_price
                hwm = pos.highest_price if pos.highest_price > 0 else pos.avg_price
                drawdown_from_hwm = (pos.current_price - hwm) / hwm  # negative when dropping

                # 1. Hard stop: always active
                if entry_pnl_pct <= -self.hard_stop:
                    forced_exits[sym] = "SELL"
                    continue

                # 2. Trailing stop: if we've been up >trail_trigger
                peak_gain = (hwm - pos.avg_price) / pos.avg_price
                if peak_gain >= self.trail_trigger:
                    # Trail: exit if price drops trail_pct below HWM
                    if drawdown_from_hwm <= -self.trail_pct:
                        forced_exits[sym] = "SELL"
                        continue

                # 3. Breakeven stop: if we've been up >breakeven_trigger but now back to entry
                elif peak_gain >= self.breakeven_trigger:
                    if entry_pnl_pct <= 0:
                        forced_exits[sym] = "SELL"
                        continue

            elif pos.quantity < 0:  # Short
                entry_pnl_pct = (pos.avg_price - pos.current_price) / pos.avg_price
                lwm = pos.lowest_price if pos.lowest_price > 0 else pos.avg_price
                drawup_from_lwm = (pos.current_price - lwm) / lwm  # positive when rising (bad for short)

                # 1. Hard stop
                if entry_pnl_pct <= -self.hard_stop:
                    forced_exits[sym] = "BUY"
                    continue

                # 2. Trailing stop
                peak_gain = (pos.avg_price - lwm) / pos.avg_price
                if peak_gain >= self.trail_trigger:
                    if drawup_from_lwm >= self.trail_pct:
                        forced_exits[sym] = "BUY"
                        continue

                # 3. Breakeven stop
                elif peak_gain >= self.breakeven_trigger:
                    if entry_pnl_pct <= 0:
                        forced_exits[sym] = "BUY"
                        continue

        return forced_exits
