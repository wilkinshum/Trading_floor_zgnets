class PMAgent:
    def __init__(self, cfg, tracer):
        self.cfg = cfg
        self.tracer = tracer

    def create_plan(self, context):
        self.tracer.emit_span("pm.create_plan", {"context": context})
        ranked = context.get("ranked", [])
        signals = context.get("signals", {})
        market_regime = context.get("market_regime", {"is_downtrend": False, "is_fear": False})
        
        max_positions = self.cfg.get("risk", {}).get("max_positions", 2)
        threshold = self.cfg.get("signals", {}).get("trade_threshold", 0.001)

        plans = []
        for item in ranked:
            sym = item["symbol"]
            score = signals.get(sym, 0.0)
            
            # --- MARKET REGIME FILTERS ---
            if market_regime["is_downtrend"] and score > 0:
                # Block Longs in Downtrend (unless huge score? no, strict for now)
                continue
                
            if score >= threshold:
                plans.append({"symbol": sym, "side": "BUY", "score": score})
            elif score <= -threshold:
                plans.append({"symbol": sym, "side": "SELL", "score": score})

        plans.sort(key=lambda x: abs(x["score"]), reverse=True)
        plans = plans[:max_positions]
        
        # --- RISK SIZING (VOLATILITY ADJUSTMENT) ---
        portfolio_equity = context.get("portfolio_equity", 5000.0)
        vol_map = {item["symbol"]: item["vol"] for item in ranked}
        
        for plan in plans:
            sym = plan["symbol"]
            annual_vol = vol_map.get(sym, 0.20)
            if annual_vol <= 0: annual_vol = 0.20
            
            # Base Alloc
            base_alloc = portfolio_equity / max_positions
            
            # Volatility Adjustment
            target_vol = 0.20
            size_factor = target_vol / annual_vol
            size_factor = max(0.5, min(1.5, size_factor))
            
            # --- FEAR REGIME SIZING ---
            if market_regime["is_fear"]:
                size_factor *= 0.5 # Cut size in half if VIX > 25
            
            dollar_size = base_alloc * size_factor
            plan["target_value"] = dollar_size
            
        return {"plans": plans}, f"pm generated {len(plans)} plans (vol-sized, regime-aware)"
