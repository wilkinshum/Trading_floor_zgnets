"""Exponentiated gradient signal tuning with regime-conditional profiles."""

from __future__ import annotations

import copy
import json
import math
from pathlib import Path
from typing import Any, Dict, Optional

SIGNAL_NAMES = ["momentum", "meanrev", "breakout", "news"]


class AdaptiveWeights:
    """Manages multiplicative weight state per strategy per regime.

    Weights are updated per-trade and persisted to ``configs/mw_state.json``.
    The ``reserve`` component is never updated directly — it absorbs
    normalization slack: ``reserve = 1.0 - sum(signal_weights)``.
    """

    def __init__(self, cfg: dict):
        sl = cfg["self_learning"]
        self.cfg = sl
        self.state_path = Path("configs/mw_state.json")

        # Build baselines from config
        self.baselines: Dict[str, Dict[str, Dict[str, float]]] = {}
        for strat in ("intraday", "swing"):
            self.baselines[strat] = {}
            for regime in ("directional", "non_directional"):
                raw = sl[strat]["baselines"][regime]
                self.baselines[strat][regime] = {s: raw[s] for s in SIGNAL_NAMES}

        # eta per strategy
        self.eta = {
            "intraday": sl["intraday"]["eta"],
            "swing": sl["swing"]["eta"],
        }
        self.max_drift = {
            "intraday": sl["intraday"]["max_drift"],
            "swing": sl["swing"]["max_drift"],
        }
        self.min_floor = {
            "intraday": sl["intraday"]["min_weight_floor"],
            "swing": sl["swing"]["min_weight_floor"],
        }
        self.directional_threshold = sl["regimes"]["directional_threshold"]
        self.vix_override = sl["regimes"]["vix_override"]

        # Current weights state — deep copy of baselines initially
        self.weights: Dict[str, Dict[str, Dict[str, float]]] = {}
        self.load_state()

    # ── Regime classification ────────────────────────────────

    def get_active_regime(self, regime_state: dict, vix: float = None) -> str:
        """Classify regime as directional or non_directional."""
        if vix is not None and vix > self.vix_override:
            return "non_directional"
        bull = regime_state.get("bull_confidence", 0.0)
        bear = regime_state.get("bear_confidence", 0.0)
        if max(bull, bear) > self.directional_threshold:
            return "directional"
        return "non_directional"

    # ── Weight accessors ─────────────────────────────────────

    def get_weights(self, strategy: str, regime: str) -> dict:
        """Return current signal weights + reserve for strategy+regime."""
        w = dict(self.weights[strategy][regime])
        w["reserve"] = round(1.0 - sum(w.values()), 6)
        return w

    def get_baseline(self, strategy: str, regime: str) -> dict:
        """Return baseline weights + reserve."""
        b = dict(self.baselines[strategy][regime])
        b["reserve"] = round(1.0 - sum(b.values()), 6)
        return b

    def get_drift(self, strategy: str, regime: str) -> dict:
        """Return per-signal drift from baseline."""
        cur = self.weights[strategy][regime]
        base = self.baselines[strategy][regime]
        return {s: round(cur[s] - base[s], 6) for s in SIGNAL_NAMES}

    # ── Update rule ──────────────────────────────────────────

    def update(self, strategy: str, regime: str, trade_result: dict):
        """Apply multiplicative weight update from a single trade.

        ``trade_result`` keys: signal_scores, pnl, position_value, holding_days.
        """
        scores = trade_result["signal_scores"]
        pnl = trade_result["pnl"]
        pos_val = trade_result["position_value"]
        holding_days = trade_result.get("holding_days", 1.0)
        eta = self.eta[strategy]
        floor = self.min_floor[strategy]
        drift = self.max_drift[strategy]
        base = self.baselines[strategy][regime]
        w = self.weights[strategy][regime]

        # Compute per-signal utility
        for sig in SIGNAL_NAMES:
            score_i = scores.get(sig, 0.0)
            if base[sig] == 0.0:
                continue  # never update zero-baseline signals

            if strategy == "intraday":
                utility = score_i * math.copysign(1, pnl) * abs(pnl) / pos_val if pos_val else 0.0
            else:
                denom = pos_val * math.sqrt(max(holding_days, 1.0)) if pos_val else 1.0
                utility = score_i * math.copysign(1, pnl) * abs(pnl) / denom

            # Multiplicative update
            if utility > 0:
                w[sig] *= (1 + eta * utility)
            else:
                w[sig] *= (1 - eta * abs(utility))

        # Floor → clip → normalize → re-clip (iterate)
        self._stabilize(strategy, regime)

    def _stabilize(self, strategy: str, regime: str, max_iter: int = 20):
        """Apply floor, drift-clip, normalize, re-clip until stable."""
        w = self.weights[strategy][regime]
        base = self.baselines[strategy][regime]
        floor = self.min_floor[strategy]
        drift = self.max_drift[strategy]

        for _ in range(max_iter):
            old = dict(w)

            # Floor
            for s in SIGNAL_NAMES:
                if base[s] > 0:
                    w[s] = max(w[s], floor)

            # Clip to drift bounds
            for s in SIGNAL_NAMES:
                if base[s] == 0.0:
                    w[s] = 0.0
                else:
                    lo = max(base[s] - drift, floor)
                    hi = base[s] + drift
                    w[s] = max(lo, min(w[s], hi))

            # Normalize signal weights so they sum to 1 - baseline_reserve
            baseline_reserve = round(1.0 - sum(base.values()), 6)
            target_sum = 1.0 - baseline_reserve
            sig_sum = sum(w[s] for s in SIGNAL_NAMES)
            if sig_sum > 0:
                for s in SIGNAL_NAMES:
                    w[s] = w[s] / sig_sum * target_sum

            # Check convergence
            if all(abs(w[s] - old[s]) < 1e-9 for s in SIGNAL_NAMES):
                break

    # ── Revert ───────────────────────────────────────────────

    def revert_to_baseline(self, strategy: str, regime: str = None):
        """Reset weights to baseline."""
        regimes = [regime] if regime else ["directional", "non_directional"]
        for r in regimes:
            self.weights[strategy][r] = dict(self.baselines[strategy][r])

    # ── Persistence ──────────────────────────────────────────

    def save_state(self):
        """Persist current weights to configs/mw_state.json."""
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.state_path, "w") as f:
            json.dump(self.weights, f, indent=2)

    def load_state(self):
        """Load persisted weights or initialize from baselines."""
        if self.state_path.exists():
            try:
                with open(self.state_path) as f:
                    loaded = json.load(f)
                self.weights = {}
                for strat in ("intraday", "swing"):
                    self.weights[strat] = {}
                    for regime in ("directional", "non_directional"):
                        if strat in loaded and regime in loaded[strat]:
                            self.weights[strat][regime] = {
                                s: loaded[strat][regime].get(s, self.baselines[strat][regime][s])
                                for s in SIGNAL_NAMES
                            }
                        else:
                            self.weights[strat][regime] = dict(self.baselines[strat][regime])
                return
            except (json.JSONDecodeError, KeyError):
                pass

        # Initialize from baselines
        self.weights = {}
        for strat in ("intraday", "swing"):
            self.weights[strat] = {}
            for regime in ("directional", "non_directional"):
                self.weights[strat][regime] = dict(self.baselines[strat][regime])
