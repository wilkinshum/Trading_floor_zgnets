class ComplianceAgent:
    def __init__(self, cfg, tracer):
        self.cfg = cfg
        self.tracer = tracer

    def review(self, plan):
        self.tracer.emit_span("compliance.review", {"plan": plan})
        allowed = set(self.cfg.get("universe", []))
        plans = plan.get("plans", [])
        for p in plans:
            if p["symbol"] not in allowed:
                return False, f"symbol not allowed: {p['symbol']}"
        return True, "compliance ok"
