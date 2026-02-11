import pandas as pd
import yaml
from pathlib import Path
import numpy as np

# Config
REPO_ROOT = Path(__file__).resolve().parents[1]
TRADES_CSV = REPO_ROOT / "trading_logs" / "trades.csv"
SIGNALS_CSV = REPO_ROOT / "trading_logs" / "signals.csv"
CONFIG_YAML = REPO_ROOT / "configs" / "workflow.yaml"

def load_data():
    if not TRADES_CSV.exists() or not SIGNALS_CSV.exists():
        print("[Optimizer] Missing logs. Need trades.csv and signals.csv.")
        return None, None
    
    trades = pd.read_csv(TRADES_CSV)
    signals = pd.read_csv(SIGNALS_CSV)
    
    # Simple join on symbol + timestamp (approximate matching might be needed in real high-freq, 
    # but here timestamps should align as they come from the same run loop).
    merged = pd.merge(trades, signals, on=["timestamp", "symbol", "side"], how="inner")
    return merged

def optimize_weights(df):
    """
    Regime-Based Optimization.
    1. Identify Market Regime (Trend vs Chop).
    2. Adjust weights: 
       - Strong Trend -> Bias Momentum/Breakout.
       - Chop/Range -> Bias MeanReversion.
    """
    if df.empty:
        return None

    # heuristic: Look at the profitability of "Trend" signals (Mom+Break) vs "MeanRev".
    # df has columns: score_mom, score_mean, score_break ... and pnl.
    # Note: pnl is only non-zero on closing trades.
    
    closing_trades = df[df["pnl"] != 0].copy()
    if closing_trades.empty:
        print("[Optimizer] No closed trades with PnL to analyze yet.")
        return None

    print(f"[Optimizer] Analyzing {len(closing_trades)} closed trades...")

    # Correlation Analysis
    # Which score component correlates best with PnL?
    # We multiply score * pnl. 
    # If Score > 0 (Buy) and PnL > 0 (Profit) -> Positive correlation.
    # If Score < 0 (Sell) and PnL > 0 (Profit) -> Wait, Short PnL is positive if price drops.
    # The 'score' for Short is negative.
    # So Score * PnL isn't quite right directly if PnL is absolute dollars.
    # But generally: Did the signal direction match the profitable move?
    
    # Let's assess contribution.
    # If (Signal > 0 and PnL > 0) OR (Signal < 0 and PnL > 0) -> Good Signal.
    # If (Signal > 0 and PnL < 0) -> Bad Signal.
    
    # We want to reward the component that had the 'strongest' signal in the 'correct' direction.
    
    scores = {"momentum": 0.0, "meanrev": 0.0, "breakout": 0.0, "news": 0.0}
    
    for _, row in closing_trades.iterrows():
        pnl = row["pnl"]
        # Analyze which component pushed for this trade?
        # This is fuzzy because we are looking at the CLOSE signal, not the OPEN signal.
        # Ideally we'd look up the OPEN signal. 
        # For this MVP, we assume the signal type persists (Trend strategies stay trend).
        
        # Simple Regime Check:
        # If Momentum Score was high magnitude matching the trade side, and trade won -> Boost Mom.
        
        # Let's simplify: Just adjust based on aggregate performance of the *strategy types*.
        # We'll rely on the signal logged at the *Closing* event as a proxy for the regime.
        # (If Momentum is still screaming BUY when we sold for profit, maybe Momentum was right).
        
        # Let's accumulate 'votes' for weights.
        
        # Impact = Magnitude of Score * Sign of PnL
        # If PnL > 0, we reinforce the strong signals.
        # If PnL < 0, we penalize the strong signals.
        
        direction = 1.0 if pnl > 0 else -1.0
        
        scores["momentum"] += abs(row["score_mom"]) * direction
        scores["meanrev"] += abs(row["score_mean"]) * direction
        scores["breakout"] += abs(row["score_break"]) * direction
        scores["news"] += abs(row["score_news"]) * direction # News is 0-1 scaled usually

    # Normalize scores to 0-1 range sum
    # Softmax or simple ratio?
    # Let's use simple ratio of positive scores. 
    # If a score is negative (net loser), floor it at a small epsilon.
    
    print(f"[Optimizer] Raw Performance Scores: {scores}")
    
    min_weight = 0.10
    total_score = sum(max(s, 0) for s in scores.values())
    
    if total_score == 0:
        print("[Optimizer] All strategies negative. Resetting to equal weights.")
        return {k: 0.25 for k in scores}
        
    new_weights = {}
    for k, v in scores.items():
        # Floor performance at 0 for weight calc
        perf = max(v, 0)
        # Allocate proportional weight
        w = perf / total_score
        # Blend with existing/neutral to avoid wild swings (Learning Rate)
        # LR = 0.1 (Move 10% towards new ideal)
        w = (w * 0.2) + (0.25 * 0.8) 
        new_weights[k] = round(w, 2)
        
    # Re-normalize to sum to 1.0
    w_sum = sum(new_weights.values())
    for k in new_weights:
        new_weights[k] = round(new_weights[k] / w_sum, 2)
        
    return new_weights

def main():
    print("--- Running Optimizer ---")
    merged = load_data()
    if merged is None: return

    new_weights = optimize_weights(merged)
    
    if new_weights:
        print(f"Proposed Weights: {new_weights}")
        
        # Load YAML to preserve comments/structure? PyYAML might clobber.
        # We'll just update the dict and dump.
        with open(CONFIG_YAML, "r") as f:
            cfg = yaml.safe_load(f)
        
        current_weights = cfg.get("signals", {}).get("weights", {})
        if current_weights == new_weights:
            print("Weights unchanged.")
        else:
            cfg.setdefault("signals", {})["weights"] = new_weights
            with open(CONFIG_YAML, "w") as f:
                yaml.dump(cfg, f, sort_keys=False)
            print("Updated workflow.yaml")

if __name__ == "__main__":
    main()
