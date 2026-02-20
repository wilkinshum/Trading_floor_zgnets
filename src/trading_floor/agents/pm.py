from __future__ import annotations

import logging
import math
from typing import Dict, List

import numpy as np

from trading_floor.agent_memory import AgentMemory
from trading_floor.regime import detect_regime

logger = logging.getLogger(__name__)


class PMAgent:
    def __init__(self, cfg, tracer):
        self.cfg = cfg
        self.tracer = tracer
        # Initialize agent memory if enabled
        mem_cfg = cfg.get("agent_memory", {})
        self.memory_enabled = mem_cfg.get("enabled", False)
        self.memory = None
        if self.memory_enabled:
            db_path = cfg.get("logging", {}).get("db_path", "trading.db")
            self.memory = AgentMemory("pm", db_path, mem_cfg)

    def create_plan(self, context):
        self.tracer.emit_span("pm.create_plan", {"context": context.get("timestamp", "")})
        ranked = context.get("ranked", [])
        signals = context.get("signals", {})
        market_regime = context.get("market_regime", {"is_downtrend": False, "is_fear": False})

        max_positions = self.cfg.get("risk", {}).get("max_positions", 2)
        max_trades = self.cfg.get("signals", {}).get("max_trades_per_cycle", max_positions)
        threshold = self.cfg.get("signals", {}).get("trade_threshold", 0.001)
        sizing_method = self.cfg.get("signals", {}).get("sizing_method", "volatility")  # kelly | fixed_fractional | volatility
        corr_threshold = self.cfg.get("signals", {}).get("correlation_threshold", 0.7)

        # --- Build candidate list ---
        candidates = []
        held_symbols = set(context.get("positions", []))
        for item in ranked:
            sym = item["symbol"]
            score = signals.get(sym, 0.0)

            # Market regime filter
            if market_regime["is_downtrend"] and score > 0:
                continue

            if score >= threshold:
                if sym in held_symbols:
                    continue
                candidates.append({"symbol": sym, "side": "BUY", "score": score, "vol": item.get("vol", 0.20)})
            elif score <= -threshold:
                candidates.append({"symbol": sym, "side": "SELL", "score": score, "vol": item.get("vol", 0.20)})

        # --- Rank by conviction (absolute score) ---
        candidates.sort(key=lambda x: abs(x["score"]), reverse=True)

        # --- Correlation filter ---
        price_data = context.get("price_data", {})  # {sym: pd.Series of closes}
        if price_data and len(candidates) > 1:
            candidates = self._filter_correlated(candidates, price_data, corr_threshold, max_trades)
        else:
            candidates = candidates[:max_trades]

        # --- Position sizing ---
        portfolio_equity = context.get("portfolio_equity", 5000.0)
        if not portfolio_equity or math.isnan(portfolio_equity) or portfolio_equity <= 0:
            portfolio_equity = self.cfg.get("risk", {}).get("equity", 5000.0)
        portfolio_cash = context.get("portfolio_cash", portfolio_equity)
        if (not portfolio_cash or math.isnan(portfolio_cash)
                or portfolio_cash <= 0):
            portfolio_cash = portfolio_equity
        sizing_capital = min(portfolio_cash, portfolio_equity / max(1, max_positions))
        vol_map = {item["symbol"]: item.get("vol", 0.20) for item in ranked}

        for plan in candidates:
            sym = plan["symbol"]
            annual_vol = vol_map.get(sym, plan.get("vol", 0.20))
            if annual_vol <= 0:
                annual_vol = 0.20

            if sizing_method == "kelly":
                dollar_size = self._kelly_size(plan["score"], annual_vol, sizing_capital, max_trades)
            elif sizing_method == "fixed_fractional":
                frac = self.cfg.get("signals", {}).get("fixed_fraction", 0.02)  # risk 2% of equity
                stop_loss = self.cfg.get("risk", {}).get("stop_loss", 0.02)
                dollar_size = (sizing_capital * frac) / max(stop_loss, 0.01)
            else:
                # Default: volatility-adjusted
                base_alloc = sizing_capital / max(1, max_trades)
                target_vol = 0.20
                size_factor = target_vol / annual_vol
                size_factor = max(0.5, min(1.5, size_factor))
                dollar_size = base_alloc * size_factor

            # Fear regime: cut size
            if market_regime.get("is_fear"):
                dollar_size *= 0.5

            plan["target_value"] = dollar_size

        plans = [{"symbol": c["symbol"], "side": c["side"], "score": c["score"],
                  "target_value": c.get("target_value", 0)} for c in candidates]

        # --- Memory integration ---
        current_regime = context.get("regime")  # caller should provide regime dict
        if self.memory and self.memory_enabled and not self.memory._disabled:
            weights = self.cfg.get("signals", {}).get("weights", {})
            for plan in plans:
                sym = plan["symbol"]
                memory_influenced = False

                # Check memory for weight adjustments
                suggestion = self.memory.suggest_weight_adjustment(
                    weights.get("momentum", 0.25)
                )
                if suggestion and suggestion.get("action") == "disable":
                    logger.warning("PM memory auto-disabled due to underperformance")
                    self.memory_enabled = False
                    break
                elif suggestion and "adjustment" in suggestion:
                    memory_influenced = True
                    adj = suggestion["adjustment"]
                    # Apply bounded adjustment to the plan score
                    plan["score"] = plan["score"] * (1 + adj)
                    plan["memory_audit"] = {
                        "adjustment": adj,
                        "win_rate": suggestion.get("win_rate"),
                        "sample_size": suggestion.get("sample_size"),
                    }

                # Record observation
                if current_regime:
                    self.memory.record(
                        {
                            "symbol": sym,
                            "signal": plan["side"],
                            "signal_value": plan["score"],
                            "confidence": abs(plan["score"]),
                            "outcome": "pending",
                            "memory_influenced": memory_influenced,
                        },
                        current_regime,
                    )

                plan["memory_influenced"] = memory_influenced

        return {"plans": plans}, f"pm generated {len(plans)} plans (top-{max_trades} conviction, corr-filtered)"

    def _filter_correlated(self, candidates: list, price_data: dict,
                           threshold: float, max_n: int) -> list:
        """Drop highly correlated candidates, keeping the higher-conviction one."""
        selected = []
        for cand in candidates:
            if len(selected) >= max_n:
                break
            sym = cand["symbol"]
            if sym not in price_data:
                selected.append(cand)
                continue

            too_correlated = False
            for existing in selected:
                esym = existing["symbol"]
                if esym not in price_data:
                    continue
                corr = self._calc_correlation(price_data[sym], price_data[esym])
                if abs(corr) > threshold:
                    too_correlated = True
                    break

            if not too_correlated:
                selected.append(cand)

        return selected

    @staticmethod
    def _calc_correlation(series_a, series_b) -> float:
        """Calculate Pearson correlation between two price series."""
        try:
            # Align lengths
            min_len = min(len(series_a), len(series_b))
            if min_len < 5:
                return 0.0
            a = series_a.iloc[-min_len:].pct_change().dropna().values
            b = series_b.iloc[-min_len:].pct_change().dropna().values
            min_len = min(len(a), len(b))
            if min_len < 5:
                return 0.0
            a, b = a[:min_len], b[:min_len]
            corr = float(np.corrcoef(a, b)[0, 1])
            return corr if not math.isnan(corr) else 0.0
        except Exception:
            return 0.0

    @staticmethod
    def _kelly_size(score: float, vol: float, equity: float, max_positions: int) -> float:
        """
        Simplified Kelly Criterion sizing.
        Uses signal score as edge estimate, vol as odds proxy.
        Applies half-Kelly for safety.
        """
        # Interpret |score| as win probability estimate (clamped)
        # This is a rough heuristic — real Kelly needs actual win rate
        edge = min(abs(score), 0.5)  # cap at 50% edge
        if vol <= 0:
            vol = 0.20
        odds = 1.0 / vol  # higher vol = worse odds

        # Kelly fraction = edge / (1/odds) = edge * odds... simplified:
        # f* = (p * b - q) / b where p=win_prob, q=1-p, b=payoff ratio
        # Approximate: f* = edge - (1 - edge) / odds
        p = 0.5 + edge  # win probability
        q = 1 - p
        b = odds
        kelly_f = (p * b - q) / max(b, 0.01)
        kelly_f = max(0.0, min(kelly_f, 0.25))  # cap at 25%

        # Half-Kelly for safety
        half_kelly = kelly_f * 0.5

        # Don't exceed equal allocation
        max_alloc = equity / max(1, max_positions)
        return min(equity * half_kelly, max_alloc)
