"""
Lightweight regime monitor — runs every 5 min via cron.

Checks HMM regime state and writes to a shared JSON file that the
trading workflow reads before execution. Minimal tokens: no LLM needed,
pure Python computation.

Output: configs/regime_state.json
"""
from __future__ import annotations

import json
import sys
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

# Add project root to path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import numpy as np
import yaml

from trading_floor.data import YahooDataProvider
from trading_floor.hmm import HMMRegimeDetector
from trading_floor.regime import detect_regime

ET = ZoneInfo("America/New_York")
STATE_FILE = PROJECT_ROOT / "configs" / "regime_state.json"


def load_config():
    cfg_path = PROJECT_ROOT / "configs" / "workflow.yaml"
    with open(cfg_path) as f:
        return yaml.safe_load(f)


def run():
    cfg = load_config()
    now = datetime.now(ET)

    # Fetch SPY + VIX data (lightweight, cached)
    provider = YahooDataProvider(
        interval=cfg.get("data", {}).get("interval", "5m"),
        lookback=cfg.get("data", {}).get("lookback", "5d"),
    )
    data = provider.fetch(["SPY", "^VIX", "BTC-USD"])

    result = {
        "timestamp": now.isoformat(),
        "error": None,
    }

    # --- HMM regime ---
    hmm_cfg = cfg.get("shadow_mode", {}).get("hmm", {})
    hmm = HMMRegimeDetector(
        n_states=hmm_cfg.get("n_states", 3),
        lookback=hmm_cfg.get("lookback", 60),
    )

    spy_md = data.get("SPY")
    if spy_md is not None and not spy_md.df.empty:
        spy_closes = spy_md.df["close"].dropna().values
        obs = hmm._discretize(spy_closes)
        if len(obs) > 10:
            hmm.fit(obs)
        hmm_result = hmm.predict(observations=obs)
        result["hmm"] = {
            "state_label": hmm_result["state_label"],
            "confidence": round(hmm_result["confidence"], 4),
            "probabilities": {
                "bull": round(hmm_result["probabilities"][0], 4),
                "bear": round(hmm_result["probabilities"][1], 4),
                "transition": round(hmm_result["probabilities"][2], 4),
            },
            "transition_risk": round(hmm_result["transition_risk"], 4),
        }
    else:
        result["hmm"] = None
        result["error"] = "No SPY data"

    # --- Simple regime (SMA + VIX) ---
    vix_md = data.get("^VIX")
    if spy_md and vix_md:
        spy_closes_list = spy_md.df["close"].dropna().tolist()
        vix_closes_list = vix_md.df["close"].dropna().tolist()
        simple = detect_regime(spy_closes_list, vix_closes_list)
        result["simple_regime"] = simple
    else:
        result["simple_regime"] = None

    # --- BTC momentum (for crypto correlation) ---
    btc_md = data.get("BTC-USD")
    if btc_md is not None and not btc_md.df.empty:
        btc_closes = btc_md.df["close"].dropna().values
        if len(btc_closes) >= 11:
            momentum = float((btc_closes[-1] - btc_closes[-10]) / btc_closes[-10])
            result["btc"] = {
                "price": round(float(btc_closes[-1]), 2),
                "momentum_10": round(momentum, 6),
                "trending": "up" if momentum > 0.005 else ("down" if momentum < -0.005 else "flat"),
            }
        else:
            result["btc"] = None
    else:
        result["btc"] = None

    # --- History (last 5 readings for trend detection) ---
    prev = _load_previous()
    history = prev.get("history", [])
    # Add current reading to history
    if result["hmm"]:
        history.append({
            "ts": now.isoformat(),
            "label": result["hmm"]["state_label"],
            "confidence": result["hmm"]["confidence"],
            "bear_prob": result["hmm"]["probabilities"]["bear"],
        })
    # Keep last 12 readings (1 hour at 5-min intervals)
    history = history[-12:]
    result["history"] = history

    # --- Regime change detection ---
    if len(history) >= 2:
        prev_label = history[-2]["label"]
        curr_label = history[-1]["label"]
        if prev_label != curr_label:
            result["regime_change"] = {
                "from": prev_label,
                "to": curr_label,
                "at": now.isoformat(),
            }
        else:
            result["regime_change"] = None
    else:
        result["regime_change"] = None

    # Write state file
    STATE_FILE.parent.mkdir(exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(result, f, indent=2)

    # Print summary for cron output
    hmm_info = result.get("hmm")
    if hmm_info:
        label = hmm_info["state_label"].upper()
        conf = hmm_info["confidence"]
        bear_p = hmm_info["probabilities"]["bear"]
        change = result.get("regime_change")
        change_str = f" ⚠️ REGIME CHANGE: {change['from']}→{change['to']}" if change else ""
        btc_str = ""
        if result.get("btc"):
            btc_str = f" | BTC: ${result['btc']['price']:,.0f} ({result['btc']['trending']})"
        print(f"Regime: {label} ({conf:.0%}) | Bear: {bear_p:.0%}{btc_str}{change_str}")
    else:
        print("Regime: NO DATA")


def _load_previous():
    """Load previous state file if it exists."""
    try:
        if STATE_FILE.exists():
            with open(STATE_FILE) as f:
                return json.load(f)
    except (json.JSONDecodeError, OSError):
        pass
    return {}


if __name__ == "__main__":
    run()
