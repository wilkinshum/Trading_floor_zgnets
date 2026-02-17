from datetime import datetime
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
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
from trading_floor.signal_normalizer import SignalNormalizer
from trading_floor.lightning import LightningTracer
from trading_floor.db import Database
from trading_floor.shadow import ShadowRunner


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
        self.normalizer = SignalNormalizer(
            lookback=cfg.get("signals", {}).get("norm_lookback", 100)
        )

        # Shadow Runner (Kalman + HMM, shadow mode)
        shadow_cfg = cfg.get("shadow_mode", {})
        if shadow_cfg.get("enabled", False):
            self.shadow = ShadowRunner(
                db_path=str(db_path),
                config=shadow_cfg,
            )
        else:
            self.shadow = None

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

    def _is_within_trading_hours(self):
        """Check if current time is within configured trading hours (weekdays only)."""
        from zoneinfo import ZoneInfo
        tz_str = self.cfg["hours"]["tz"]
        tz = ZoneInfo(tz_str)
        now = datetime.now(tz)
        
        # Skip weekends (Saturday=5, Sunday=6)
        if now.weekday() >= 5:
            print(f"[TradingFloor] Weekend. Market closed. Skipping.")
            return False
        
        # Skip market holidays
        holidays = self.cfg.get("hours", {}).get("holidays", [])
        today_str = now.strftime("%Y-%m-%d")
        if today_str in holidays:
            print(f"[TradingFloor] Market holiday ({today_str}). Skipping.")
            return False
        
        start_parts = self.cfg["hours"]["start"].split(":")
        end_parts = self.cfg["hours"]["end"].split(":")
        start_time = now.replace(hour=int(start_parts[0]), minute=int(start_parts[1]), second=0, microsecond=0)
        end_time = now.replace(hour=int(end_parts[0]), minute=int(end_parts[1]), second=0, microsecond=0)
        return start_time <= now <= end_time

    def run(self):
        if not self._is_within_trading_hours():
            print(f"[TradingFloor] Outside trading hours ({self.cfg['hours']['start']}-{self.cfg['hours']['end']} {self.cfg['hours']['tz']}). Skipping.")
            return

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

            if "SPY" in md and not md["SPY"].df.empty:
                spy_series = md["SPY"].df["close"]
                if len(spy_series) >= 20:
                    ma20 = spy_series.rolling(20).mean().iloc[-1]
                    market_regime["is_downtrend"] = spy_series.iloc[-1] < ma20

            if "^VIX" in md and not md["^VIX"].df.empty:
                vix_val = md["^VIX"].df["close"].iloc[-1]
                market_regime["is_fear"] = vix_val > 25.0

            context["market_regime"] = market_regime

            windowed = {}
            current_prices = {}
            price_series = {}  # For correlation checks in PM

            for sym, m in md.items():
                if sym not in self.cfg["universe"]:
                    continue
                windowed[sym] = filter_trading_window(
                    m.df,
                    tz=self.cfg["hours"]["tz"],
                    start=self.cfg["hours"]["start"],
                    end=self.cfg["hours"]["end"],
                )
                if not m.df.empty:
                    current_prices[sym] = m.df["close"].iloc[-1]
                    price_series[sym] = m.df["close"]

            # Mark portfolio to market
            self.portfolio.mark_to_market(current_prices)
            context["portfolio_equity"] = self.portfolio.state.equity
            context["portfolio_cash"] = self.portfolio.state.cash
            context["positions"] = list(self.portfolio.state.positions.keys())
            context["portfolio_obj"] = self.portfolio

            # 1. Check Exits (ATR-based Stop / Trailing / Breakeven / Kill Switch)
            context["price_data"] = price_series  # needed for ATR calc in exits
            forced_exits = self.exit_manager.check_exits(context)

            ranked = self.scout.rank(windowed)
            signals = {}
            signal_details = {}

            # Scout gate: only process top N symbols
            scout_top_n = self.cfg.get("scout_top_n", 5)
            top_symbols = set(r["symbol"] for r in ranked[:scout_top_n])

            # Load Weights
            weights = self.cfg.get("signals", {}).get("weights", {
                "momentum": 0.25, "meanrev": 0.25, "breakout": 0.25, "news": 0.25
            })

            def _score_symbol(sym, df):
                """Score a single symbol with all 4 signal agents (thread-safe)."""
                mom_raw = self.signal_mom.score(df)
                mean_raw = self.signal_mean.score(df)
                brk_raw = self.signal_break.score(df)
                news_raw = self.signal_news.get_sentiment(sym)

                mom = self.normalizer.normalize(sym, "momentum", mom_raw)
                mean = self.normalizer.normalize(sym, "meanrev", mean_raw)
                brk = self.normalizer.normalize(sym, "breakout", brk_raw)
                news = news_raw

                score = (
                    (mom * weights.get("momentum", 0.25)) +
                    (mean * weights.get("meanrev", 0.25)) +
                    (brk * weights.get("breakout", 0.25)) +
                    (news * weights.get("news", 0.25))
                )

                details = {
                    "components": {
                        "momentum": mom, "meanrev": mean,
                        "breakout": brk, "news": news
                    },
                    "raw": {
                        "momentum": mom_raw, "meanrev": mean_raw,
                        "breakout": brk_raw, "news": news_raw
                    },
                    "weights": weights,
                    "final_score": score
                }
                return sym, score, details

            # Run signal scoring in parallel across top symbols
            with ThreadPoolExecutor(max_workers=min(scout_top_n, 8)) as executor:
                futures = {}
                for sym, df in windowed.items():
                    if df.empty or sym not in top_symbols:
                        continue
                    futures[executor.submit(_score_symbol, sym, df)] = sym

                for future in as_completed(futures):
                    sym, score, details = future.result()
                    signals[sym] = score
                    signal_details[sym] = details

            context.update({
                "ranked": ranked,
                "signals": signals,
                "price_data": price_series,  # For PM correlation check
            })

            # --- Shadow Mode: Kalman + HMM ---
            if self.shadow is not None:
                try:
                    spy_prices = md["SPY"].df["close"] if "SPY" in md and not md["SPY"].df.empty else None
                    vix_prices = md["^VIX"].df["close"] if "^VIX" in md and not md["^VIX"].df.empty else None
                    regime_label = ("bear" if market_regime.get("is_downtrend") else "bull") + \
                                   ("_high_vol" if market_regime.get("is_fear") else "_low_vol")
                    shadow_summary = self.shadow.run(
                        price_data=price_series,
                        spy_data=spy_prices,
                        vix_data=vix_prices,
                        existing_signals=signals,
                        existing_regime={"label": regime_label},
                    )
                    agree = shadow_summary.get("kalman_agree", 0)
                    total = shadow_summary.get("kalman_total_compared", 0)
                    hmm = shadow_summary.get("hmm")
                    hmm_str = ""
                    if hmm:
                        hmm_str = f", HMM sees {hmm['state_label']} regime ({hmm['confidence']:.0%} confidence)"
                    print(f"[Shadow] Kalman agrees with {agree}/{total} signals{hmm_str}")
                except Exception as e:
                    print(f"[Shadow] Error: {e}")

            plan, plan_notes = self.pm.create_plan(context)

            # Merge forced exits into plan (exits always execute first)
            exit_plans = []
            new_entry_plans = []
            if forced_exits:
                for sym, side in forced_exits.items():
                    exit_plans.append({"symbol": sym, "side": side, "score": 999.9})
            for p in plan.get("plans", []):
                if p["symbol"] not in forced_exits:
                    new_entry_plans.append(p)

            # Enforce max positions on NEW entries (exits always allowed)
            new_entry_plans = self.exit_manager.check_max_positions(
                self.portfolio, new_entry_plans
            )

            plan["plans"] = exit_plans + new_entry_plans
            if forced_exits:
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

                    pnl = 0.0
                    if price > 0:
                        pnl = self.portfolio.execute(sym, side, price, target_value=target_val)

                    actual_qty = 0
                    if price > 0 and target_val > 0:
                        actual_qty = int(target_val // price)

                    trade_record = {
                        "timestamp": context["timestamp"],
                        "symbol": sym,
                        "side": side,
                        "quantity": actual_qty,
                        "price": price,
                        "score": score,
                        "pnl": pnl,
                    }

                    self.logger.log_trade(trade_record)
                    self.db.log_trade(trade_record)

                    if sym in signal_details:
                        details = signal_details[sym]
                        details["timestamp"] = context["timestamp"]
                        details["symbol"] = sym
                        details["side"] = side
                        self.signal_logger.log_signal(details)
                        self.db.log_signal(details)

                self.portfolio.save()

            self.tracer.emit_reward({
                "risk_ok": int(risk_ok),
                "compliance_ok": int(compliance_ok),
                "approval_granted": int(approval_granted),
                "equity_change": self.portfolio.state.equity - context.get("portfolio_equity", 0)
            })

            review = self.reviewer.summarize()
            if review.get("insights"):
                print(f"[Reviewer] {review['insights'][:200]}")
