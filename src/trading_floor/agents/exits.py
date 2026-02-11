from __future__ import annotations

class ExitManager:
    def __init__(self, cfg, tracer):
        self.cfg = cfg
        self.tracer = tracer
        self.stop_loss = cfg.get("risk", {}).get("stop_loss", 0.02)
        self.take_profit = cfg.get("risk", {}).get("take_profit", 0.05)

    def check_exits(self, context) -> dict:
        """
        Check existing positions for Stop Loss or Take Profit triggers.
        Returns a dictionary of forced exits: {symbol: "SELL"|"BUY"}
        """
        self.tracer.emit_span("exit_manager.check", {"positions": len(context.get("positions", []))})
        
        forced_exits = {}
        portfolio = context.get("portfolio_obj")
        if not portfolio:
            return {}

        for sym, pos in portfolio.state.positions.items():
            if pos.current_price <= 0: continue
            
            # PnL percentage
            # Long: (Curr - Avg) / Avg
            # Short: (Avg - Curr) / Avg
            
            if pos.quantity > 0:
                pnl_pct = (pos.current_price - pos.avg_price) / pos.avg_price
                if pnl_pct <= -self.stop_loss:
                    forced_exits[sym] = "SELL" # Close Long
                elif pnl_pct >= self.take_profit:
                    forced_exits[sym] = "SELL" # Close Long
                    
            elif pos.quantity < 0:
                pnl_pct = (pos.avg_price - pos.current_price) / pos.avg_price
                if pnl_pct <= -self.stop_loss:
                    forced_exits[sym] = "BUY" # Close Short (Cover)
                elif pnl_pct >= self.take_profit:
                    forced_exits[sym] = "BUY" # Close Short (Cover)

        return forced_exits
