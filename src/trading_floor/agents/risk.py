class RiskAgent:
    def __init__(self, cfg, tracer):
        self.cfg = cfg
        self.tracer = tracer

    def evaluate(self, context):
        self.tracer.emit_span("risk.evaluate", {"context": context})
        max_positions = self.cfg.get("risk", {}).get("max_positions", 2)
        plans = context.get("plan", {}).get("plans", [])
        ok = len(plans) <= max_positions
        return ok, f"risk ok ({len(plans)} <= {max_positions})"
