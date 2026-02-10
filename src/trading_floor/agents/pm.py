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
        return {"plans": plans}, f"pm generated {len(plans)} plans"
