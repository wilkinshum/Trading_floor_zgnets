"""
Daily Trading Journal Generator
Reads from trading.db, events.csv, trades.csv, and shadow_predictions
to produce a structured daily journal entry with reasoning.
"""
import sys, json, csv, sqlite3
from datetime import datetime, date
from pathlib import Path
from collections import defaultdict

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

PROJECT = Path(__file__).resolve().parent.parent
DB = PROJECT / "trading.db"
TRADES_CSV = PROJECT / "trading_logs" / "trades.csv"
EVENTS_CSV = PROJECT / "trading_logs" / "events.csv"
JOURNAL_DIR = PROJECT / "trading_logs" / "journals"
PORTFOLIO_JSON = PROJECT / "portfolio.json"


def get_shadow_summary(conn, day: str):
    """Summarize shadow predictions for the day."""
    rows = conn.execute(
        "SELECT symbol, kalman_signal, hmm_state, hmm_bull_prob, hmm_bear_prob, timestamp "
        "FROM shadow_predictions WHERE timestamp LIKE ? ORDER BY timestamp",
        (f"{day}%",)
    ).fetchall()
    if not rows:
        return None

    symbols = defaultdict(list)
    for sym, kal, hmm_st, bull_p, bear_p, ts in rows:
        symbols[sym].append({
            "kalman": kal, "hmm_state": hmm_st,
            "bull_prob": bull_p, "bear_prob": bear_p, "time": ts
        })

    # Aggregate
    hmm_states = [r[2] for r in rows]
    bull_probs = [r[3] for r in rows if r[3] is not None]
    bear_probs = [r[4] for r in rows if r[4] is not None]

    bull_count = sum(1 for s in hmm_states if s == "bull")
    bear_count = sum(1 for s in hmm_states if s == "bear")
    neutral_count = len(hmm_states) - bull_count - bear_count

    return {
        "total_predictions": len(rows),
        "unique_symbols": len(symbols),
        "hmm_bull_pct": round(bull_count / len(hmm_states) * 100, 1) if hmm_states else 0,
        "hmm_bear_pct": round(bear_count / len(hmm_states) * 100, 1) if hmm_states else 0,
        "avg_bull_prob": round(sum(bull_probs) / len(bull_probs) * 100, 1) if bull_probs else 0,
        "avg_bear_prob": round(sum(bear_probs) / len(bear_probs) * 100, 1) if bear_probs else 0,
        "symbols": {k: len(v) for k, v in symbols.items()},
    }


def get_agent_memory(conn, day: str):
    """Get agent memory entries for the day."""
    rows = conn.execute(
        "SELECT agent_name, symbol, signal_type, signal_value, outcome, confidence, timestamp "
        "FROM agent_memory WHERE timestamp LIKE ? ORDER BY timestamp",
        (f"{day}%",)
    ).fetchall()
    agents = defaultdict(list)
    for name, sym, sig_type, sig_val, outcome, conf, ts in rows:
        agents[name].append({
            "symbol": sym, "signal_type": sig_type,
            "signal_value": round(sig_val, 4) if sig_val else 0,
            "outcome": outcome, "confidence": round(conf, 4) if conf else 0,
        })
    return dict(agents)


def get_trades(day: str):
    """Get trades from CSV for the day."""
    trades = []
    if TRADES_CSV.exists():
        with open(TRADES_CSV, "r") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if row.get("timestamp", "").startswith(day):
                    trades.append(row)
    return trades


def get_events(day: str):
    """Get events from CSV for the day."""
    events = []
    if EVENTS_CSV.exists():
        with open(EVENTS_CSV, "r") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if row.get("timestamp", "").startswith(day):
                    events.append(row)
    return events


def get_portfolio():
    """Get current portfolio state."""
    if PORTFOLIO_JSON.exists():
        try:
            return json.loads(PORTFOLIO_JSON.read_text())
        except:
            pass
    return None


def generate_journal(day: str = None):
    """Generate a daily journal entry."""
    if day is None:
        day = date.today().isoformat()

    conn = sqlite3.connect(str(DB))

    shadow = get_shadow_summary(conn, day)
    agent_mem = get_agent_memory(conn, day)
    trades = get_trades(day)
    events = get_events(day)
    portfolio = get_portfolio()

    conn.close()

    # Build the journal
    entry = {
        "date": day,
        "generated_at": datetime.now().isoformat(),
        "market_regime": None,
        "shadow_analysis": None,
        "agent_signals": {},
        "trades_executed": [],
        "trades_blocked": 0,
        "events_summary": None,
        "portfolio_snapshot": None,
        "narrative": "",
        "lessons": [],
    }

    # --- Market regime from shadow ---
    if shadow:
        entry["shadow_analysis"] = shadow
        if shadow["hmm_bull_pct"] > 60:
            entry["market_regime"] = "Bullish"
        elif shadow["hmm_bear_pct"] > 60:
            entry["market_regime"] = "Bearish"
        else:
            entry["market_regime"] = "Mixed/Transitional"

    # --- Agent signals ---
    for agent, signals in agent_mem.items():
        top_signals = sorted(signals, key=lambda x: abs(x["signal_value"]), reverse=True)[:5]
        entry["agent_signals"][agent] = {
            "total_signals": len(signals),
            "top_by_strength": top_signals,
            "unique_symbols": len(set(s["symbol"] for s in signals)),
        }

    # --- Trades ---
    for t in trades:
        entry["trades_executed"].append({
            "symbol": t.get("symbol"),
            "side": t.get("side"),
            "quantity": t.get("quantity"),
            "price": t.get("price"),
            "score": t.get("score"),
            "pnl": t.get("pnl"),
        })

    # --- Events ---
    if events:
        approved = sum(1 for e in events if e.get("approval_granted") == "True")
        blocked = sum(1 for e in events if e.get("approval_granted") == "False")
        entry["trades_blocked"] = blocked
        entry["events_summary"] = {
            "total_cycles": len(events),
            "approved": approved,
            "blocked": blocked,
            "block_reasons": list(set(
                e.get("plan_notes", "") for e in events
                if e.get("approval_granted") == "False"
            ))[:3],
        }

    # --- Portfolio ---
    if portfolio:
        state = portfolio.get("state", portfolio)
        entry["portfolio_snapshot"] = {
            "equity": state.get("equity"),
            "cash": state.get("cash"),
            "positions": len(state.get("positions", {})),
        }

    # --- Generate narrative ---
    parts = []
    parts.append(f"# Trading Journal — {day}")
    parts.append("")

    # Market overview
    if entry["market_regime"]:
        parts.append(f"## Market Regime: {entry['market_regime']}")
        if shadow:
            parts.append(f"HMM called bull {shadow['hmm_bull_pct']}% of predictions, "
                        f"bear {shadow['hmm_bear_pct']}%. "
                        f"Average bull probability: {shadow['avg_bull_prob']}%. "
                        f"Analyzed {shadow['unique_symbols']} symbols across "
                        f"{shadow['total_predictions']} predictions.")
        parts.append("")

    # Agent activity
    if agent_mem:
        parts.append("## Agent Activity")
        for agent, info in entry["agent_signals"].items():
            parts.append(f"### {agent.title()} Agent")
            parts.append(f"Generated {info['total_signals']} signals across "
                        f"{info['unique_symbols']} symbols.")
            if info["top_by_strength"]:
                parts.append("**Strongest signals:**")
                for s in info["top_by_strength"][:3]:
                    direction = "LONG" if s["signal_value"] > 0 else "SHORT" if s["signal_value"] < 0 else "NEUTRAL"
                    parts.append(f"- {s['symbol']}: {s['signal_value']:+.4f} ({direction}, "
                               f"confidence: {s['confidence']:.2%})")
            parts.append("")

    # Trades
    parts.append("## Trades")
    if entry["trades_executed"]:
        for t in entry["trades_executed"]:
            pnl_str = f"${float(t['pnl']):+,.2f}" if t.get("pnl") else "pending"
            parts.append(f"- **{t['side']} {t['symbol']}** — qty: {t['quantity']}, "
                        f"price: ${float(t['price']):,.2f}, score: {t['score']}, PnL: {pnl_str}")
    else:
        parts.append("No trades executed today.")
    parts.append("")

    # Blocked trades
    if entry["trades_blocked"] > 0:
        parts.append(f"## Blocked Trades: {entry['trades_blocked']} cycles")
        if entry["events_summary"] and entry["events_summary"]["block_reasons"]:
            for r in entry["events_summary"]["block_reasons"]:
                parts.append(f"- {r}")
        parts.append("")

    # Workflow cycles
    if entry["events_summary"]:
        es = entry["events_summary"]
        parts.append(f"## Workflow: {es['total_cycles']} cycles "
                    f"({es['approved']} approved, {es['blocked']} blocked)")
        parts.append("")

    # Portfolio
    if entry["portfolio_snapshot"]:
        ps = entry["portfolio_snapshot"]
        equity = ps.get("equity", 0)
        parts.append(f"## Portfolio")
        parts.append(f"Equity: ${equity:,.2f} | "
                    f"Cash: ${ps.get('cash', 0):,.2f} | "
                    f"Open positions: {ps.get('positions', 0)}")
        if equity and equity > 0:
            pct = ((equity - 5000) / 5000) * 100
            parts.append(f"Performance since inception: {pct:+.1f}%")
        parts.append("")

    # Lessons
    parts.append("## Lessons & Notes")
    if not entry["trades_executed"] and entry["trades_blocked"] > 0:
        parts.append("- Signals were generated but blocked — check approval status")
    if not entry["trades_executed"] and entry["trades_blocked"] == 0:
        parts.append("- No signals crossed the conviction threshold today")
    if entry["market_regime"] == "Mixed/Transitional":
        parts.append("- Mixed regime — market indecisive, staying cautious is correct")
    if entry["market_regime"] == "Bullish" and not entry["trades_executed"]:
        parts.append("- Bullish regime detected but no entries — signals may be too conservative")
    parts.append("")

    entry["narrative"] = "\n".join(parts)
    return entry


def save_journal(entry):
    """Save journal entry to JSON and markdown."""
    JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
    day = entry["date"]

    # JSON (for API)
    json_path = JOURNAL_DIR / f"{day}.json"
    json_path.write_text(json.dumps(entry, indent=2, default=str))

    # Markdown (human readable)
    md_path = JOURNAL_DIR / f"{day}.md"
    md_path.write_text(entry["narrative"], encoding="utf-8")

    return json_path, md_path


if __name__ == "__main__":
    day = sys.argv[1] if len(sys.argv) > 1 else date.today().isoformat()
    entry = generate_journal(day)
    jp, mp = save_journal(entry)
    print(f"Journal for {day}:")
    print(entry["narrative"])
    print(f"\nSaved: {jp}")
    print(f"Saved: {mp}")
