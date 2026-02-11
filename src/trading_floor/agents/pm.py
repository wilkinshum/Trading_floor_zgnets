class PMAgent:
    def __init__(self, cfg, tracer):
        self.cfg = cfg
        self.tracer = tracer

    def create_plan(self, context):
        self.tracer.emit_span("pm.create_plan", {"context": context})
        ranked = context.get("ranked", [])
        signals = context.get("signals", {})
        max_positions = self.cfg.get("risk", {}).get("max_positions", 2)
        threshold = self.cfg.get("signals", {}).get("trade_threshold", 0.001)

        plans = []
        for item in ranked:
            sym = item["symbol"]
            score = signals.get(sym, 0.0)
            if score >= threshold:
                plans.append({"symbol": sym, "side": "BUY", "score": score})
            elif score <= -threshold:
                plans.append({"symbol": sym, "side": "SELL", "score": score})

        plans.sort(key=lambda x: abs(x["score"]), reverse=True)
        plans = plans[:max_positions]
        
        # --- RISK SIZING (VOLATILITY ADJUSTMENT) ---
        # 1. Base Sizing: Equity / Max Positions (Equal Weight)
        # 2. Vol Adjustment: Target Vol / Asset Vol
        #    If asset is 2x volatile, size is 0.5x.
        
        portfolio_equity = context.get("portfolio_equity", 5000.0)
        target_risk_pct = 0.01  # Target 1% risk per trade (or could be config)
        
        # We need volatility (std dev) for sizing. 
        # "ranked" list from Scout has 'vol' (annualized).
        # Convert to daily vol: annual / sqrt(252)
        # Position Size = (Equity * Risk%) / (Daily Vol * Price)? 
        # Or simpler: Volatility Target Sizing.
        
        # Let's map symbols to their vol from ranked list
        vol_map = {item["symbol"]: item["vol"] for item in ranked}
        
        for plan in plans:
            sym = plan["symbol"]
            annual_vol = vol_map.get(sym, 0.20) # Default 20% if missing
            if annual_vol <= 0: annual_vol = 0.20
            
            # Inverse Volatility Sizing
            # Base allocation (Equal weight)
            base_alloc = portfolio_equity / max_positions
            
            # Adjustment factor: (Target Vol / Asset Vol)
            # Let's say Target Annual Vol = 20% (Market average)
            target_vol = 0.20
            
            # Cap the multiplier to avoid massive positions in low-vol assets (e.g. 3x leverage)
            # Limit to 0.5x to 1.5x range around base alloc? 
            # Or just raw inverse vol.
            
            size_factor = target_vol / annual_vol
            
            # Conservative clamp: Don't exceed 1.5x base allocation, don't go below 0.5x
            size_factor = max(0.5, min(1.5, size_factor))
            
            dollar_size = base_alloc * size_factor
            
            # Pass this desired dollar size to the portfolio execution (as 'quantity' hint or new field)
            # Currently portfolio.execute takes quantity (shares).
            # We don't have price here easily? context has current_prices?
            # Yes, workflow context doesn't pass prices to PM explicitly but we can inject it or just 
            # let portfolio handle it if we pass "dollar_amount".
            # For now, let's calculate shares here if possible, or pass "target_value".
            
            # Workflow context in run() has no prices map passed to PM.
            # Let's modify PM to output "target_value" in the plan, and Portfolio uses that.
            plan["target_value"] = dollar_size
            
        return {"plans": plans}, f"pm generated {len(plans)} plans (vol-sized)"
