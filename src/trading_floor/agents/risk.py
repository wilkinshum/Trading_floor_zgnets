class RiskAgent:
    def __init__(self, cfg, tracer):
        self.cfg = cfg
        self.tracer = tracer

    def evaluate(self, context):
        self.tracer.emit_span("risk.evaluate", {"context": context})
        max_positions = self.cfg.get("risk", {}).get("max_positions", 3)
        
        # Count existing positions + new planned entries
        existing = len(context.get("positions", []))
        plans = context.get("plan", {}).get("plans", [])
        
        # Forced exits (score=999.9) reduce position count
        exits = sum(1 for p in plans if p.get("score", 0) == 999.9)
        new_entries = len(plans) - exits
        net_positions = existing - exits + new_entries
        
        ok = net_positions <= max_positions
        notes = f"risk: {existing} existing - {exits} exits + {new_entries} new = {net_positions} (max {max_positions})"
        
        if not ok:
            notes += " EXCEEDED"
        
        return ok, notes
