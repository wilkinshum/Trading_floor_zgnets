from datetime import datetime
import json
from pathlib import Path
from trading_floor.data import YahooDataProvider, filter_trading_window, latest_timestamp
from trading_floor.agents.scout import ScoutAgent
from trading_floor.agents.signal_momentum import MomentumSignalAgent
from trading_floor.agents.signal_meanreversion import MeanReversionSignalAgent
from trading_floor.agents.signal_breakout import BreakoutSignalAgent
from trading_floor.agents.risk import RiskAgent
from trading_floor.agents.pm import PMAgent
from trading_floor.agents.compliance import ComplianceAgent
from trading_floor.agents.reviewer import NextDayReviewer
from trading_floor.logging import TradeLogger
from trading_floor.lightning import LightningTracer


class TradingFloor:
    def __init__(self, cfg):
        self.cfg = cfg
        self.logger = TradeLogger(cfg)
        self.tracer = LightningTracer(cfg)

        self.data = YahooDataProvider(
            interval=cfg.get("data", {}).get("interval", "5m"),
            lookback=cfg.get("data", {}).get("lookback", "5d"),
        )
        self.scout = ScoutAgent(cfg, self.tracer)
        self.signal_mom = MomentumSignalAgent(cfg, self.tracer)
        self.signal_mean = MeanReversionSignalAgent(cfg, self.tracer)
        self.signal_break = BreakoutSignalAgent(cfg, self.tracer)
        self.risk = RiskAgent(cfg, self.tracer)
        self.pm = PMAgent(cfg, self.tracer)
        self.compliance = ComplianceAgent(cfg, self.tracer)
        self.reviewer = NextDayReviewer(cfg, self.tracer)

    def _approval_check(self):
        approval_cfg = self.cfg.get("approval", {})
        if not approval_cfg.get("required", False):
            return True, "approval not required"

        approval_file = approval_cfg.get("file", "approval.json")
        path = Path(approval_file)
        if not path.is_absolute():
            path = Path.cwd() / path

        if not path.exists():
            return False, f"approval file missing: {path}"

        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return False, "approval file unreadable"

        if data.get("date") and data.get("date") != datetime.now().date().isoformat():
            return False, "approval expired"

        approved = bool(data.get("approved"))
        note = data.get("notes") or data.get("note") or ""
        return approved, note

    def run(self):
        context = {
            "timestamp": latest_timestamp(),
            "universe": self.cfg["universe"],
        }

        with self.tracer.run_context("trading_floor.run", input_payload=context):
            md = self.data.fetch(self.cfg["universe"])
            windowed = {}
            for sym, m in md.items():
                windowed[sym] = filter_trading_window(
                    m.df,
                    tz=self.cfg["hours"]["tz"],
                    start=self.cfg["hours"]["start"],
                    end=self.cfg["hours"]["end"],
                )

            ranked = self.scout.rank(windowed)
            signals = {}
            for sym, df in windowed.items():
                score = (
                    self.signal_mom.score(df)
                    + self.signal_mean.score(df)
                    + self.signal_break.score(df)
                ) / 3.0
                signals[sym] = score

            context.update({"ranked": ranked, "signals": signals})
            plan, plan_notes = self.pm.create_plan(context)
            context["plan"] = plan

            risk_ok, risk_notes = self.risk.evaluate(context)
            compliance_ok, compliance_notes = self.compliance.review(plan)

            approval_ok, approval_note = self._approval_check()
            approval_granted = bool(risk_ok and compliance_ok and approval_ok)

            if not approval_granted:
                plan = {"plans": []}
                plan_notes = "approval pending; plan not logged"
                if approval_note:
                    plan_notes = f"{plan_notes} ({approval_note})"

            self.logger.log_event({
                "timestamp": context["timestamp"],
                "risk_ok": risk_ok,
                "compliance_ok": compliance_ok,
                "approval_granted": approval_granted,
                "risk_notes": risk_notes,
                "compliance_notes": compliance_notes,
                "plan_notes": plan_notes,
            })

            if approval_granted:
                for p in plan.get("plans", []):
                    self.logger.log_trade({
                        "timestamp": context["timestamp"],
                        "symbol": p["symbol"],
                        "side": p["side"],
                        "score": p["score"],
                        "pnl": 0.0,
                    })

            # reward signals for Agent Lightning
            self.tracer.emit_reward({
                "risk_ok": int(risk_ok),
                "compliance_ok": int(compliance_ok),
                "approval_granted": int(approval_granted),
            })

            _ = self.reviewer.summarize()
