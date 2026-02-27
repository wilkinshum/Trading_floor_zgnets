from datetime import datetime
import json, sys, io
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# Fix Windows cp1252 encoding for emoji/unicode in print()
if sys.stdout.encoding and sys.stdout.encoding.lower() not in ('utf-8', 'utf8'):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

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
from trading_floor.challenger import TradeChallengeSystem
from trading_floor.pre_execution_filters import run_all_pre_execution_filters


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
        self.challenger = TradeChallengeSystem(cfg, db_path=str(db_path))
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

    def _request_finance_review(self, symbol: str, side: str, score: float, challenge_summary: str) -> bool:
        """
        Route a cautioned trade to the finance agent for review.
        Returns True if approved, False if rejected.

        The finance agent checks:
        - Position sizing relative to portfolio
        - Recent P&L on this symbol
        - Overall portfolio exposure
        - The nature of the caution flag
        """
        try:
            # Check with the reviewer/compliance for a second opinion
            review_context = {
                "symbol": symbol,
                "side": side,
                "score": score,
                "caution_reason": challenge_summary,
                "portfolio_equity": self.portfolio.state.equity,
                "portfolio_cash": self.portfolio.state.cash,
                "existing_positions": list(self.portfolio.state.positions.keys()),
            }

            # Finance agent logic: reject if portfolio is already stressed
            cash_ratio = self.portfolio.state.cash / max(self.portfolio.state.equity, 1)
            if cash_ratio < 0.15:
                print(f"[FinanceAgent] Rejecting {side} {symbol}: cash ratio {cash_ratio:.1%} too low for cautioned trade")
                return False

            # Reject if we already have too many positions
            max_positions = self.cfg.get("risk", {}).get("max_positions", 10)
            if len(self.portfolio.state.positions) >= max_positions and side == "BUY":
                print(f"[FinanceAgent] Rejecting {side} {symbol}: at max positions ({max_positions})")
                return False

            # Reject weak scores on cautioned trades
            caution_min_score = self.cfg.get("pre_execution", {}).get("caution_min_score", 0.5)
            if abs(score) < caution_min_score:
                print(f"[FinanceAgent] Rejecting {side} {symbol}: score {score:.3f} too weak for cautioned trade")
                return False

            # Check recent losses on this symbol
            try:
                import sqlite3
                conn = sqlite3.connect(str(self.db.db_path))
                today = datetime.now().strftime("%Y-%m-%d")
                rows = conn.execute(
                    "SELECT pnl FROM trades WHERE symbol = ? AND date(timestamp) = ?",
                    (symbol, today)
                ).fetchall()
                conn.close()
                today_pnl = sum(r[0] for r in rows if r[0])
                if today_pnl < -50:  # Lost >$50 on this symbol today
                    print(f"[FinanceAgent] Rejecting {side} {symbol}: already lost ${today_pnl:.2f} today")
                    return False
            except Exception:
                pass

            print(f"[FinanceAgent] Approving cautioned trade: {side} {symbol} (score={score:.3f})")
            return True

        except Exception as e:
            print(f"[FinanceAgent] Review error: {e}")
            return False

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
            # Clean up stale approval file
            try:
                path.unlink()
            except OSError:
                pass
            return False, "approval expired (stale file removed)"

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
            fetch_list = list(set(self.cfg["universe"] + ["SPY", "^VIX", "BTC-USD"]))
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

                momentum_w = weights.get("momentum", 0.25)
                meanrev_w = weights.get("meanrev", 0.25)
                breakout_w = weights.get("breakout", 0.25)
                news_w = weights.get("news", 0.25)

                if news is None or news == 0:
                    # No news: use non-news weights, normalize so thresholds are consistent
                    total_non_news = momentum_w + meanrev_w + breakout_w
                    if total_non_news > 0:
                        raw_score = (
                            (mom * momentum_w) +
                            (mean * meanrev_w) +
                            (brk * breakout_w)
                        )
                        score = raw_score / total_non_news  # normalize to [-1, +1]
                        weights_used = {
                            "momentum": momentum_w / total_non_news,
                            "meanrev": meanrev_w / total_non_news,
                            "breakout": breakout_w / total_non_news,
                            "news": 0.0,
                        }
                    else:
                        score = 0.0
                        weights_used = {
                            "momentum": 0.0,
                            "meanrev": 0.0,
                            "breakout": 0.0,
                            "news": 0.0,
                        }
                else:
                    # With news: compute weighted sum and normalize by active weight sum
                    raw_score = (
                        (mom * momentum_w) +
                        (mean * meanrev_w) +
                        (brk * breakout_w) +
                        (news * news_w)
                    )
                    active_weight_sum = momentum_w + meanrev_w + breakout_w + news_w
                    score = raw_score / active_weight_sum if active_weight_sum > 0 else 0.0
                    weights_used = {
                        "momentum": momentum_w,
                        "meanrev": meanrev_w,
                        "breakout": breakout_w,
                        "news": news_w,
                    }

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
                    "weights_used": weights_used,
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

            all_signals = dict(signals)
            all_signal_details = dict(signal_details)

            # Signal persistence filter (require 2 consecutive cycles)
            filtered_signals = {}
            filtered_details = {}
            if signals:
                try:
                    conn = self.db._get_conn()
                    cursor = conn.cursor()
                    today = datetime.utcnow().strftime("%Y-%m-%d")
                    for sym, score in signals.items():
                        cursor.execute(
                            "SELECT final_score FROM signals WHERE symbol = ? AND date(timestamp) = ? ORDER BY id DESC LIMIT 1",
                            (sym, today),
                        )
                        row = cursor.fetchone()
                        prev_score = row[0] if row else None
                        if prev_score is None:
                            filtered_signals[sym] = score
                            filtered_details[sym] = signal_details[sym]
                            continue

                        current_sign = 1 if score > 0 else -1 if score < 0 else 0
                        prev_sign = 1 if prev_score > 0 else -1 if prev_score < 0 else 0

                        if current_sign != 0 and prev_sign != 0 and current_sign != prev_sign:
                            print(f"[TradingFloor] {sym} signal not persistent")
                            continue

                        filtered_signals[sym] = score
                        filtered_details[sym] = signal_details[sym]
                    conn.close()
                except Exception as e:
                    print(f"[TradingFloor] persistence check failed: {e}")
                    filtered_signals = signals
                    filtered_details = signal_details
            else:
                filtered_signals = signals
                filtered_details = signal_details

            signals = filtered_signals
            signal_details = filtered_details

            context.update({
                "ranked": ranked,
                "signals": signals,
                "price_data": price_series,  # For PM correlation check
            })

            # --- Always log signals (even if approval pending) ---
            for sym, details in all_signal_details.items():
                details["timestamp"] = context["timestamp"]
                details["symbol"] = sym
                details["side"] = "BUY" if all_signals.get(sym, 0) > 0 else "SELL"
                try:
                    self.signal_logger.log_signal(details)
                    self.db.log_signal(details)
                except Exception:
                    pass

            # --- Shadow Mode: Kalman + HMM ---
            kalman_results = {}
            hmm_regime_label = None
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
                        hmm_regime_label = hmm["state_label"]
                    print(f"[Shadow] Kalman agrees with {agree}/{total} signals{hmm_str}")

                    # Extract kalman results for pre-execution filters
                    for sym in price_series:
                        kf = self.shadow._get_kalman(sym)
                        if kf._initialized:
                            import math
                            unc = math.sqrt(max(kf.P[0, 0], 1e-12))
                            kalman_results[sym] = {
                                "level": float(kf.x[0]),
                                "trend": float(kf.x[1]),
                                "signal": float(kf.x[1] / unc) if unc > 1e-12 else 0.0,
                                "uncertainty": unc,
                            }
                except Exception as e:
                    print(f"[Shadow] Error: {e}")

            # Pass signal component details to PM for momentum gate & high-bar checks
            context["signal_details"] = {
                sym: d.get("components", {}) for sym, d in signal_details.items()
            }

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
                # Build signal details for challenger
                challenge_context = {
                    "signal_details": {},
                    "market_regime": context.get("market_regime", {}),
                }
                for p in plan.get("plans", []):
                    sym = p["symbol"]
                    if sym in signals:
                        # Reconstruct individual signal components if available
                        challenge_context["signal_details"][sym] = signal_details.get(sym, {}).get("components", {})

                for p in plan.get("plans", []):
                    sym = p["symbol"]
                    side = p["side"]
                    score = p["score"]
                    target_val = p.get("target_value", 0.0)
                    price = current_prices.get(sym, 0.0)

                    # Challenge system — agents question illogical trades
                    if score != 999.9:  # Skip forced exits
                        challenges = self.challenger.challenge_plan(p, challenge_context)
                        proceed, challenge_summary = self.challenger.should_proceed(challenges)
                        if challenges:
                            print(f"[TradingFloor] Challenges for {side} {sym}: {challenge_summary}")
                        if proceed == "caution":
                            # Route to finance agent for review
                            print(f"[TradingFloor] CAUTION FLAG → routing {side} {sym} to finance agent")
                            try:
                                from trading_floor.agents.reviewer import NextDayReviewer
                                finance_ok = self._request_finance_review(sym, side, score, challenge_summary)
                                if not finance_ok:
                                    print(f"[TradingFloor] Finance agent REJECTED: {side} {sym}")
                                    continue
                                print(f"[TradingFloor] Finance agent APPROVED: {side} {sym}")
                            except Exception as e:
                                print(f"[TradingFloor] Finance review failed ({e}), blocking cautioned trade")
                                continue
                        elif not proceed:
                            print(f"[TradingFloor] TRADE BLOCKED by challenge system: {side} {sym}")
                            continue

                        # --- Pre-execution filters (all 6 improvements) ---
                        spy_prices = md["SPY"].df["close"] if "SPY" in md and not md["SPY"].df.empty else None
                        btc_prices = md.get("BTC-USD")
                        btc_series = btc_prices.df["close"] if btc_prices and not btc_prices.df.empty else None

                        # Get volume DataFrame for this symbol
                        vol_df = None
                        if sym in md and not md[sym].df.empty:
                            vol_df = md[sym].df

                        filter_ok, filter_reasons = run_all_pre_execution_filters(
                            symbol=sym,
                            side=side,
                            score=score,
                            cfg=self.cfg,
                            hmm=self.shadow.hmm if self.shadow else None,
                            spy_data=spy_prices,
                            original_regime_label=hmm_regime_label,
                            volume_df=vol_df,
                            crypto_benchmark_prices=btc_series,
                            kalman_results=kalman_results,
                            price=price,
                        )

                        if not filter_ok:
                            block_reasons = [r for r in filter_reasons if not r.split(": ", 1)[-1].startswith(("OK", "outside", "not crypto", "no "))]
                            print(f"[TradingFloor] PRE-EXEC BLOCKED {side} {sym}: {'; '.join(block_reasons)}")
                            continue

                    pnl = 0.0
                    actual_qty = 0
                    if price > 0:
                        if side == "SELL":
                            pos = self.portfolio.state.positions.get(sym)
                            if pos and pos.quantity > 0:
                                # Close existing long position
                                close_qty = abs(pos.quantity)
                                pnl = self.portfolio.execute(sym, side, price, quantity=close_qty)
                                actual_qty = close_qty
                            elif score == 999.9:
                                continue  # Forced exit but no position — skip
                            else:
                                # Open new short position
                                pnl = self.portfolio.execute(sym, side, price, target_value=target_val)
                                if price > 0 and target_val > 0:
                                    actual_qty = int(target_val // price)
                                elif sym in self.portfolio.state.positions:
                                    actual_qty = abs(self.portfolio.state.positions[sym].quantity)
                        else:
                            # BUY — new entry
                            pnl = self.portfolio.execute(sym, side, price, target_value=target_val)
                            if price > 0 and target_val > 0:
                                actual_qty = int(target_val // price)
                            elif sym in self.portfolio.state.positions:
                                actual_qty = abs(self.portfolio.state.positions[sym].quantity)

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
