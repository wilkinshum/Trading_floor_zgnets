import math

from trading_floor.sector_filter import check_sector_filter


class RiskAgent:
    def __init__(self, cfg, tracer):
        self.cfg = cfg
        self.tracer = tracer
        self.max_positions = cfg.get("risk", {}).get("max_positions", 3)
        self.max_atr_pct = cfg.get("risk", {}).get("max_atr_pct", 0.10)
        self.min_atr_pct = cfg.get("risk", {}).get("min_atr_pct", 0.005)
        self.atr_period = cfg.get("risk", {}).get("atr_period", 14)
        self.sector_filter_threshold = cfg.get("risk", {}).get("sector_filter_threshold", -0.15)

    def _calc_atr_pct(self, sym: str, price_data: dict):
        if not price_data or sym not in price_data:
            return None

        try:
            import pandas as pd
            df = price_data[sym]
            if isinstance(df, pd.Series):
                returns = df.pct_change().dropna()
                if len(returns) < self.atr_period:
                    return None
                atr_pct = returns.rolling(self.atr_period).std().iloc[-1]
                if math.isnan(atr_pct):
                    return None
                if atr_pct <= 0:
                    return 0.0
                return float(atr_pct)
            if isinstance(df, pd.DataFrame):
                if "close" not in df.columns:
                    return None
                c = df["close"]
                price = c.iloc[-1]
                if price <= 0:
                    return None
                h = df["high"] if "high" in df.columns else None
                l = df["low"] if "low" in df.columns else None
                if h is None or l is None:
                    return None
                tr = pd.concat([
                    h - l,
                    (h - c.shift(1)).abs(),
                    (l - c.shift(1)).abs()
                ], axis=1).max(axis=1)
                atr = tr.rolling(self.atr_period).mean().iloc[-1]
                if math.isnan(atr):
                    return None
                if atr <= 0:
                    return 0.0
                return float(atr / price)
        except Exception:
            return None

        return None

    def evaluate(self, context):
        self.tracer.emit_span("risk.evaluate", {"context": context})
        plans = context.get("plan", {}).get("plans", [])
        price_data = context.get("price_data", {})

        filtered_plans = []
        rejected = []

        for plan in plans:
            if plan.get("score", 0) == 999.9 or plan.get("side") == "SELL":
                filtered_plans.append(plan)
                continue

            sym = plan.get("symbol")
            atr_pct = self._calc_atr_pct(sym, price_data)
            if atr_pct is None:
                filtered_plans.append(plan)
                continue

            if atr_pct > self.max_atr_pct:
                print(
                    f"[RiskAgent] VOLATILITY FILTER: {sym} too volatile "
                    f"(ATR {atr_pct:.2%} > max {self.max_atr_pct:.2%}) - REJECTED"
                )
                rejected.append(sym)
                continue
            if atr_pct < self.min_atr_pct:
                print(
                    f"[RiskAgent] VOLATILITY FILTER: {sym} too flat "
                    f"(ATR {atr_pct:.2%} < min {self.min_atr_pct:.2%}) - REJECTED"
                )
                rejected.append(sym)
                continue

            # Sector news filter
            passed, reason, sector_score = check_sector_filter(
                sym, threshold=self.sector_filter_threshold
            )
            if not passed:
                print(f"[RiskAgent] SECTOR FILTER: {sym} - {reason} - REJECTED")
                rejected.append(sym)
                continue

            filtered_plans.append(plan)

        if len(filtered_plans) != len(plans):
            context["plan"]["plans"] = filtered_plans

        # Count existing positions + new planned entries
        existing = len(context.get("positions", []))
        exits = sum(1 for p in filtered_plans if p.get("score", 0) == 999.9)
        new_entries = len(filtered_plans) - exits
        net_positions = existing - exits + new_entries

        ok = net_positions <= self.max_positions
        notes = (
            f"risk: {existing} existing - {exits} exits + "
            f"{new_entries} new = {net_positions} (max {self.max_positions})"
        )

        if rejected:
            notes += f" | volatility_filter rejected {len(rejected)}: {', '.join(rejected)}"

        if not ok:
            notes += " EXCEEDED"

        return ok, notes
