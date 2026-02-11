from __future__ import annotations

class ExitManager:
    def __init__(self, cfg, tracer):
        self.cfg = cfg
        self.tracer = tracer
        self.stop_loss = cfg.get("risk", {}).get("stop_loss", 0.02)
        self.take_profit = cfg.get("risk", {}).get("take_profit", 0.05)

    def check_exits(self, context) -> dict:
        """
        Check existing positions for Stop Loss or Trailing Stop.
        Returns a dictionary of forced exits: {symbol: "SELL"|"BUY"}
        """
        self.tracer.emit_span("exit_manager.check", {"positions": len(context.get("positions", []))})
        
        forced_exits = {}
        portfolio = context.get("portfolio_obj")
        if not portfolio:
            return {}
            
        # Hard Stop Loss (from entry)
        hard_stop = self.stop_loss
        # Trailing Stop: If price falls X% from highest high
        trailing_stop_pct = self.take_profit # Reusing TP config as Trailing delta for now, or 0.03
        
        for sym, pos in portfolio.state.positions.items():
            if pos.current_price <= 0: continue
            
            if pos.quantity > 0: # Long
                # 1. Hard Stop
                entry_pnl = (pos.current_price - pos.avg_price) / pos.avg_price
                if entry_pnl <= -hard_stop:
                    forced_exits[sym] = "SELL" # Stop Loss
                    continue
                
                # 2. Trailing Stop
                # Drop from Highest Price
                drawdown = (pos.current_price - pos.highest_price) / pos.highest_price
                if drawdown <= -trailing_stop_pct:
                    forced_exits[sym] = "SELL" # Trailing Stop Hit

            elif pos.quantity < 0: # Short
                # 1. Hard Stop
                entry_pnl = (pos.avg_price - pos.current_price) / pos.avg_price
                if entry_pnl <= -hard_stop:
                    forced_exits[sym] = "BUY" # Stop Loss
                    continue

                # 2. Trailing Stop
                # Rise from Lowest Price
                drawup = (pos.current_price - pos.lowest_price) / pos.lowest_price
                # Since short, drawup > 0 is bad for profits (price rising).
                # But wait, trailing stop for short means:
                # We were in profit (price dropped to Lowest), now it bounced back up X%.
                # So if (Current - Lowest) / Lowest >= TrailingPct -> Exit
                if drawup >= trailing_stop_pct:
                    forced_exits[sym] = "BUY" # Trailing Stop Hit

        return forced_exits
