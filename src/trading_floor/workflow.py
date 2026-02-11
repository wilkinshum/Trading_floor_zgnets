from datetime import datetime
import json
from pathlib import Path
from trading_floor.data import YahooDataProvider, filter_trading_window, latest_timestamp
from trading_floor.portfolio import Portfolio
from trading_floor.agents.scout import ScoutAgent
from trading_floor.agents.signal_momentum import MomentumSignalAgent
from trading_floor.agents.signal_meanreversion import MeanReversionSignalAgent
from trading_floor.agents.signal_breakout import BreakoutSignalAgent
from trading_floor.agents.news import NewsSentimentAgent
from trading_floor.agents.exits import ExitManager
from trading_floor.agents.risk import RiskAgent
from trading_floor.agents.pm import PMAgent
from trading_floor.agents.compliance import ComplianceAgent
from trading_floor.agents.reviewer import NextDayReviewer
from trading_floor.logging import TradeLogger
from trading_floor.signal_log import SignalLogger
from trading_floor.lightning import LightningTracer
from trading_floor.db import Database
from pathlib import Path


class TradingFloor:
    def __init__(self, cfg):
        self.cfg = cfg
        self.logger = TradeLogger(cfg)
        self.signal_logger = SignalLogger(cfg)
        self.tracer = LightningTracer(cfg)
        
        # Init DB
        db_path = Path(cfg["logging"].get("db_path", "trading.db"))
        self.db = Database(db_path)

        self.data = YahooDataProvider(
            interval=cfg.get("data", {}).get("interval", "5m"),
            lookback=cfg.get("data", {}).get("lookback", "5d"),
        )
        self.portfolio = Portfolio(cfg)
        
        self.scout = ScoutAgent(cfg, self.tracer)
        self.signal_mom = MomentumSignalAgent(cfg, self.tracer)
        self.signal_mean = MeanReversionSignalAgent(cfg, self.tracer)
        self.signal_break = BreakoutSignalAgent(cfg, self.tracer)
        self.signal_news = NewsSentimentAgent(cfg, self.tracer)
        self.exit_manager = ExitManager(cfg, self.tracer)
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
            # Fetch Universe + Market Indicators
            fetch_list = list(set(self.cfg["universe"] + ["SPY", "^VIX"]))
            md = self.data.fetch(fetch_list)
            
            # Market Regime Calculation
            market_regime = {"is_downtrend": False, "is_fear": False}
            
            # SPY Trend Check (MA20)
            if "SPY" in md and not md["SPY"].df.empty:
                spy_series = md["SPY"].df["close"]
                if len(spy_series) >= 20:
                    ma20 = spy_series.rolling(20).mean().iloc[-1]
                    market_regime["is_downtrend"] = spy_series.iloc[-1] < ma20
            
            # VIX Fear Check
            if "^VIX" in md and not md["^VIX"].df.empty:
                vix_val = md["^VIX"].df["close"].iloc[-1]
                market_regime["is_fear"] = vix_val > 25.0
            
            context["market_regime"] = market_regime

            windowed = {}
            current_prices = {}
            
            for sym, m in md.items():
                if sym not in self.cfg["universe"]: continue
                windowed[sym] = filter_trading_window(
                    m.df,
                    tz=self.cfg["hours"]["tz"],
                    start=self.cfg["hours"]["start"],
                    end=self.cfg["hours"]["end"],
                )
                if not m.df.empty:
                    current_prices[sym] = m.df["close"].iloc[-1]

            # Mark portfolio to market
            self.portfolio.mark_to_market(current_prices)
            context["portfolio_equity"] = self.portfolio.state.equity
            context["portfolio_cash"] = self.portfolio.state.cash
            context["positions"] = list(self.portfolio.state.positions.keys())
            context["portfolio_obj"] = self.portfolio

            # 1. Check Exits (Stop Loss / Take Profit)
            forced_exits = self.exit_manager.check_exits(context)
            
            ranked = self.scout.rank(windowed)
            signals = {}
            signal_details = {} # Store components for logging
            
            # Load Weights
            weights = self.cfg.get("signals", {}).get("weights", {
                "momentum": 0.25, "meanrev": 0.25, "breakout": 0.25, "news": 0.25
            })
            
            for sym, df in windowed.items():
                if df.empty: continue
                # Could parallelize this loop if heavy
                mom = self.signal_mom.score(df)
                mean = self.signal_mean.score(df)
                brk = self.signal_break.score(df)
                news = self.signal_news.get_sentiment(sym)
                news_scaled = news * 0.01 
                
                # Weighted Sum
                score = (
                    (mom * weights.get("momentum", 0.25)) +
                    (mean * weights.get("meanrev", 0.25)) +
                    (brk * weights.get("breakout", 0.25)) +
                    (news_scaled * weights.get("news", 0.25))
                )
                
                signals[sym] = score
                signal_details[sym] = {
                    "components": {"momentum": mom, "meanrev": mean, "breakout": brk, "news": news},
                    "weights": weights,
                    "final_score": score
                }

            context.update({"ranked": ranked, "signals": signals})
            plan, plan_notes = self.pm.create_plan(context)
            
            # Merge forced exits into plan
            if forced_exits:
                # If exit manager says close, we override PM entry signals for that symbol
                final_plans = []
                # Add forced exits first
                for sym, side in forced_exits.items():
                    final_plans.append({"symbol": sym, "side": side, "score": 999.9}) # High score for priority
                
                # Add PM plans if not conflicting
                for p in plan.get("plans", []):
                    if p["symbol"] not in forced_exits:
                        final_plans.append(p)
                
                plan["plans"] = final_plans
                plan_notes = f"{plan_notes} + {len(forced_exits)} forced exits"

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
                    sym = p["symbol"]
                    side = p["side"]
                    score = p["score"]
                    target_val = p.get("target_value", 0.0)
                    price = current_prices.get(sym, 0.0)
                    
                    # Execute in portfolio (updates cash/positions)
                    pnl = 0.0
                    if price > 0:
                        pnl = self.portfolio.execute(sym, side, price, target_value=target_val)
                    
                    trade_record = {
                        "timestamp": context["timestamp"],
                        "symbol": sym,
                        "side": side,
                        "quantity": p.get("target_value", 0), # Simplified for now
                        "price": price,
                        "score": score,
                        "pnl": pnl,
                    }
                    
                    self.logger.log_trade(trade_record)
                    self.db.log_trade(trade_record)
                    
                    # Log Signal Components for Optimizer
                    if sym in signal_details:
                        details = signal_details[sym]
                        details["timestamp"] = context["timestamp"]
                        details["symbol"] = sym
                        details["side"] = side
                        self.signal_logger.log_signal(details)
                        self.db.log_signal(details)
                
                # Save portfolio state
                self.portfolio.save()

            # reward signals for Agent Lightning
            self.tracer.emit_reward({
                "risk_ok": int(risk_ok),
                "compliance_ok": int(compliance_ok),
                "approval_granted": int(approval_granted),
                "equity_change": self.portfolio.state.equity - context.get("portfolio_equity", 0) # naive daily change
            })

            _ = self.reviewer.summarize()
