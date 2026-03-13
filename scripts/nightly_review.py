#!/usr/bin/env python3
"""Nightly Review Script — bridges the self-learning system with cron/reporting.

Called by the finance agent cron at 5:00 PM ET weekdays.
Runs three layers:
  1. Data layer:  Backfill signal_accuracy for any unprocessed closed trades
  2. ML layer:    Run SelfLearner.nightly_review() for MW updates + revert checks
  3. Judge layer: LLM-style scoring of each closed trade today (heuristic v1, upgradeable)
  4. Report:      Write review markdown + print WhatsApp-ready scorecard to stdout

Usage:
    python scripts/nightly_review.py                  # today's review
    python scripts/nightly_review.py --date 2026-03-11  # specific date
    python scripts/nightly_review.py --backfill        # process ALL unscored closed trades
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

# ── Path setup ───────────────────────────────────────────────
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
os.chdir(ROOT)

import yaml
from trading_floor.db import Database
from trading_floor.review import SelfLearner

# ── Config ───────────────────────────────────────────────────

def load_config() -> dict:
    cfg_path = ROOT / "configs" / "workflow.yaml"
    with open(cfg_path) as f:
        return yaml.safe_load(f)


# ── 1. Backfill signal_accuracy for unprocessed closed trades ─

def get_unscored_closed_trades(db: Database, target_date: date | None = None) -> list[dict]:
    """Find closed positions in position_meta that have no signal_accuracy rows."""
    conn = db._get_conn()
    conn.row_factory = sqlite3.Row
    try:
        if target_date:
            rows = conn.execute("""
                SELECT pm.* FROM position_meta pm
                WHERE pm.status = 'closed'
                  AND DATE(pm.exit_time) = ?
                  AND pm.id NOT IN (SELECT DISTINCT position_meta_id FROM signal_accuracy WHERE position_meta_id IS NOT NULL)
            """, (target_date.isoformat(),)).fetchall()
        else:
            rows = conn.execute("""
                SELECT pm.* FROM position_meta pm
                WHERE pm.status = 'closed'
                  AND pm.id NOT IN (SELECT DISTINCT position_meta_id FROM signal_accuracy WHERE position_meta_id IS NOT NULL)
            """).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_signals_for_trade(db: Database, symbol: str, entry_time: str) -> dict:
    """Get the signal scores closest to the trade entry time."""
    conn = db._get_conn()
    conn.row_factory = sqlite3.Row
    try:
        # Look for signals within ±2 hours of entry
        row = conn.execute("""
            SELECT score_mom, score_mean, score_break, score_news, final_score
            FROM signals
            WHERE symbol = ?
              AND ABS(julianday(timestamp) - julianday(?)) < 0.084  -- ~2 hours
            ORDER BY ABS(julianday(timestamp) - julianday(?))
            LIMIT 1
        """, (symbol, entry_time, entry_time,)).fetchone()
        if row:
            return {
                "momentum": row["score_mom"] or 0.0,
                "meanrev": row["score_mean"] or 0.0,
                "breakout": row["score_break"] or 0.0,
                "news": row["score_news"] or 0.0,
            }
        # Fallback: try signals_json from position_meta
        return {"momentum": 0.0, "meanrev": 0.0, "breakout": 0.0, "news": 0.0}
    finally:
        conn.close()


def get_regime_at_time(db: Database, timestamp: str) -> dict:
    """Get HMM regime state closest to a timestamp from shadow_predictions."""
    conn = db._get_conn()
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("""
            SELECT hmm_state, hmm_bull_prob, hmm_bear_prob
            FROM shadow_predictions
            WHERE ABS(julianday(timestamp) - julianday(?)) < 0.5
            ORDER BY ABS(julianday(timestamp) - julianday(?))
            LIMIT 1
        """, (timestamp, timestamp)).fetchone()
        if row:
            return {
                "hmm_state": row["hmm_state"],
                "bull_confidence": row["hmm_bull_prob"] or 0.5,
                "bear_confidence": row["hmm_bear_prob"] or 0.3,
            }
        return {"hmm_state": None, "bull_confidence": 0.5, "bear_confidence": 0.3}
    finally:
        conn.close()


def backfill_signal_accuracy(cfg: dict, db: Database, learner: SelfLearner,
                              target_date: date | None = None) -> int:
    """Process unscored closed trades through the self-learner."""
    trades = get_unscored_closed_trades(db, target_date)
    if not trades:
        return 0

    processed = 0
    for t in trades:
        symbol = t["symbol"]
        entry_time = t.get("entry_time", "")
        exit_time = t.get("exit_time", "")
        pnl = t.get("pnl") or 0.0
        entry_price = t.get("entry_price") or 0.0
        entry_qty = t.get("entry_qty") or 0

        # Get signal scores
        signals = get_signals_for_trade(db, symbol, entry_time)

        # Also try signals_json from position_meta
        if all(v == 0.0 for v in signals.values()) and t.get("signals_json"):
            try:
                sj = json.loads(t["signals_json"])
                for key in ("momentum", "meanrev", "breakout", "news"):
                    if key in sj:
                        signals[key] = sj[key]
            except (json.JSONDecodeError, TypeError):
                pass

        position_value = entry_price * entry_qty if entry_price and entry_qty else 1.0

        # Calculate holding days
        holding_days = 1.0
        if entry_time and exit_time:
            try:
                et = datetime.fromisoformat(entry_time.replace("Z", "+00:00"))
                xt = datetime.fromisoformat(exit_time.replace("Z", "+00:00"))
                holding_days = max((xt - et).total_seconds() / 86400, 0.1)
            except (ValueError, TypeError):
                pass

        regime = get_regime_at_time(db, entry_time)

        trade_data = {
            "strategy": t.get("strategy", "swing"),
            "symbol": symbol,
            "signal_scores": signals,
            "pnl": pnl,
            "position_value": position_value,
            "holding_days": holding_days,
            "entry_price": entry_price,
            "entry_time": entry_time,
            "exit_time": exit_time,
            "position_meta_id": t["id"],
        }

        try:
            learner.process_trade(trade_data, regime)
            processed += 1
            print(f"  ✓ Scored pm_id={t['id']} {symbol}: PnL ${pnl:.2f}, "
                  f"signals={{mom:{signals['momentum']:.2f}, mr:{signals['meanrev']:.2f}, "
                  f"brk:{signals['breakout']:.2f}, nws:{signals['news']:.2f}}}")
        except Exception as e:
            print(f"  ✗ Error scoring pm_id={t['id']} {symbol}: {e}")

    return processed


# ── 2. Trade Judge (heuristic v1) ────────────────────────────

def judge_trade(trade: dict, signals: dict, regime: dict) -> dict:
    """Score a closed trade on multiple dimensions (1-10 scale).

    Heuristic v1 — no LLM call, pure rule-based. Upgradeable to LLM later.
    """
    pnl = trade.get("pnl") or 0.0
    entry_price = trade.get("entry_price") or 1.0
    entry_qty = trade.get("entry_qty") or 1
    tp_price = trade.get("tp_price")
    stop_price = trade.get("stop_price")
    exit_reason = trade.get("exit_reason", "")
    position_value = entry_price * entry_qty

    pnl_pct = (pnl / position_value * 100) if position_value else 0.0

    # Signal quality (1-10): how much did signals agree?
    sig_values = [v for v in signals.values() if v != 0.0]
    if sig_values:
        # All same sign = good agreement
        signs = [1 if v > 0 else -1 for v in sig_values]
        agreement = abs(sum(signs)) / len(signs)  # 1.0 = perfect agreement
        avg_strength = sum(abs(v) for v in sig_values) / len(sig_values)
        signal_quality = min(10, max(1, int(agreement * 5 + avg_strength * 5)))
    else:
        signal_quality = 1

    # Risk management (1-10): did we have SL/TP?
    risk_score = 5  # baseline
    if tp_price and tp_price > 0:
        risk_score += 2
    if stop_price and stop_price > 0:
        risk_score += 2
    if not stop_price:
        risk_score -= 2  # no stop loss = bad
    risk_score = min(10, max(1, risk_score))

    # Exit quality (1-10)
    exit_score = 5
    if exit_reason == "take_profit":
        exit_score = 9  # planned exit
    elif exit_reason == "stop_loss":
        exit_score = 6  # discipline
    elif exit_reason == "max_hold":
        exit_score = 4  # timed out
    elif exit_reason == "trailing_stop":
        exit_score = 8  # let winner run
    elif exit_reason == "manual":
        exit_score = 5

    # Outcome (1-10): based on PnL %
    if pnl_pct > 5:
        outcome = 10
    elif pnl_pct > 2:
        outcome = 8
    elif pnl_pct > 0:
        outcome = 6
    elif pnl_pct > -2:
        outcome = 4
    elif pnl_pct > -5:
        outcome = 2
    else:
        outcome = 1

    # Regime alignment: did we trade with or against the trend?
    regime_score = 5
    bull_conf = regime.get("bull_confidence", 0.5)
    bear_conf = regime.get("bear_confidence", 0.3)
    side = trade.get("side", "long")
    if side == "long" and bull_conf > 0.6:
        regime_score = 8  # long in bull = aligned
    elif side == "long" and bear_conf > 0.6:
        regime_score = 3  # long in bear = misaligned
    elif side == "short" and bear_conf > 0.6:
        regime_score = 8
    elif side == "short" and bull_conf > 0.6:
        regime_score = 3

    composite = round((signal_quality * 0.25 + risk_score * 0.20 +
                       exit_score * 0.20 + outcome * 0.25 + regime_score * 0.10), 1)

    return {
        "signal_quality": signal_quality,
        "risk_management": risk_score,
        "exit_quality": exit_score,
        "outcome": outcome,
        "regime_alignment": regime_score,
        "composite": composite,
        "pnl_pct": round(pnl_pct, 2),
    }


# ── 3. Quiet day analysis ────────────────────────────────────

def analyze_quiet_day(db: Database, cfg: dict, target_date: date) -> str:
    """When no trades happened, analyze why. Were signals weak? Was it good discipline?"""
    conn = db._get_conn()
    conn.row_factory = sqlite3.Row
    try:
        # Get today's signals
        signals = conn.execute("""
            SELECT symbol, final_score, score_mom, score_mean, score_break, score_news
            FROM signals WHERE DATE(timestamp) = ?
            ORDER BY ABS(final_score) DESC
        """, (target_date.isoformat(),)).fetchall()

        # Get open positions
        open_pos = conn.execute("""
            SELECT symbol, entry_price, entry_qty, strategy
            FROM position_meta WHERE status = 'open'
        """).fetchall()
    finally:
        conn.close()

    threshold = cfg.get("strategies", {}).get("swing", {}).get("threshold", 0.25)

    if not signals:
        return ("📭 **Quiet day — no signals generated.**\n"
                "No stocks from the universe were scanned today. "
                "Check if the workflow cron ran and if AlpacaDataProvider returned data.")

    above_threshold = [s for s in signals if abs(s["final_score"] or 0) >= threshold]
    best = signals[0] if signals else None
    best_score = abs(best["final_score"] or 0) if best else 0

    lines = []
    lines.append(f"📭 **Quiet day — {len(signals)} signals, {len(above_threshold)} above threshold ({threshold})**")

    if above_threshold:
        lines.append(f"\n🎯 **Above threshold (but no trade):**")
        for s in above_threshold[:5]:
            lines.append(f"  • {s['symbol']}: score={s['final_score']:.3f} "
                         f"(mom={s['score_mom'] or 0:.2f}, mr={s['score_mean'] or 0:.2f}, "
                         f"brk={s['score_break'] or 0:.2f}, nws={s['score_news'] or 0:.2f})")
        lines.append("\n⚠️ Signals passed threshold but no trade triggered. Possible reasons:")
        lines.append("  • Max positions reached")
        lines.append("  • Sector concentration limit")
        lines.append("  • Risk check blocked (ATR too high/low)")
        lines.append("  • SL cooldown active")
    else:
        lines.append(f"\n✅ **Good discipline** — strongest signal was {best['symbol']} "
                     f"at {best_score:.3f} (below {threshold} threshold)")
        lines.append("No conviction = no trade. System working as designed.")

    if best and best_score > 0.15:
        lines.append(f"\n📊 **Top 5 signals today:**")
        for s in signals[:5]:
            lines.append(f"  • {s['symbol']}: {s['final_score']:.3f}")

    if open_pos:
        lines.append(f"\n📍 **{len(open_pos)} open positions being managed:**")
        for p in open_pos:
            lines.append(f"  • {p['symbol']} ({p['strategy']}): {p['entry_qty']} shares @ ${p['entry_price']:.2f}")

    return "\n".join(lines)


# ── 4. Build WhatsApp scorecard ──────────────────────────────

def build_scorecard(target_date: date, db: Database, cfg: dict,
                    learner: SelfLearner, scored_count: int) -> str:
    """Build the WhatsApp-friendly summary + full report."""

    conn = db._get_conn()
    conn.row_factory = sqlite3.Row
    try:
        # Today's closed trades
        closed_today = conn.execute("""
            SELECT * FROM position_meta
            WHERE status = 'closed' AND DATE(exit_time) = ?
        """, (target_date.isoformat(),)).fetchall()

        # Open positions
        open_positions = conn.execute("""
            SELECT * FROM position_meta WHERE status = 'open'
        """).fetchall()

        # Rolling stats (30d)
        cutoff_30d = (target_date - timedelta(days=30)).isoformat()
        stats_30d = conn.execute("""
            SELECT COUNT(*) as trades,
                   SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins,
                   COALESCE(SUM(pnl), 0) as total_pnl
            FROM position_meta
            WHERE status = 'closed' AND exit_time >= ?
        """, (cutoff_30d,)).fetchone()

        # Signal accuracy (30d)
        accuracy_30d = conn.execute("""
            SELECT signal_type,
                   SUM(CASE WHEN was_correct THEN 1 ELSE 0 END) as correct,
                   COUNT(*) as total
            FROM signal_accuracy
            WHERE created_at >= ?
            GROUP BY signal_type
        """, (cutoff_30d,)).fetchall()
    finally:
        conn.close()

    closed_today = [dict(r) for r in closed_today]
    open_positions = [dict(r) for r in open_positions]

    # ── Judge each closed trade ──
    judgments = []
    for t in closed_today:
        signals = get_signals_for_trade(db, t["symbol"], t.get("entry_time", ""))
        if all(v == 0.0 for v in signals.values()) and t.get("signals_json"):
            try:
                sj = json.loads(t["signals_json"])
                for k in ("momentum", "meanrev", "breakout", "news"):
                    if k in sj:
                        signals[k] = sj[k]
            except (json.JSONDecodeError, TypeError):
                pass
        regime = get_regime_at_time(db, t.get("entry_time", ""))
        judgment = judge_trade(t, signals, regime)
        judgment["symbol"] = t["symbol"]
        judgment["pnl"] = t.get("pnl") or 0.0
        judgments.append(judgment)

    # ── Run self-learner nightly review ──
    review_report = learner.nightly_review()

    # ── Save full report ──
    report_dir = ROOT / "web"
    report_dir.mkdir(exist_ok=True)
    report_path = report_dir / f"nightly_review_{target_date}.md"

    full_report_lines = [
        f"# Nightly Review — {target_date}",
        f"Generated: {datetime.now().isoformat()}",
        "",
    ]

    if closed_today:
        day_pnl = sum(t.get("pnl") or 0 for t in closed_today)
        wins = sum(1 for t in closed_today if (t.get("pnl") or 0) > 0)
        full_report_lines.append(f"## Today's Trades: {len(closed_today)} closed")
        full_report_lines.append(f"- Day PnL: ${day_pnl:.2f}")
        full_report_lines.append(f"- Win rate: {wins}/{len(closed_today)}")
        full_report_lines.append("")

        for j in judgments:
            full_report_lines.append(f"### {j['symbol']} — PnL ${j['pnl']:.2f} ({j['pnl_pct']:+.1f}%)")
            full_report_lines.append(f"  - Signal Quality: {j['signal_quality']}/10")
            full_report_lines.append(f"  - Risk Management: {j['risk_management']}/10")
            full_report_lines.append(f"  - Exit Quality: {j['exit_quality']}/10")
            full_report_lines.append(f"  - Outcome: {j['outcome']}/10")
            full_report_lines.append(f"  - Regime Alignment: {j['regime_alignment']}/10")
            full_report_lines.append(f"  - **Composite: {j['composite']}/10**")
            full_report_lines.append("")
    else:
        quiet_analysis = analyze_quiet_day(db, cfg, target_date)
        full_report_lines.append("## No Trades Today")
        full_report_lines.append(quiet_analysis)
        full_report_lines.append("")

    if open_positions:
        full_report_lines.append(f"## Open Positions: {len(open_positions)}")
        for p in open_positions:
            full_report_lines.append(f"- {p['symbol']}: {p['entry_qty']} shares @ ${p['entry_price']:.2f} "
                                     f"(TP: ${p.get('tp_price') or 0:.2f}, SL: ${p.get('stop_price') or 'NONE'})")
        full_report_lines.append("")

    # Signal accuracy section
    if accuracy_30d:
        full_report_lines.append("## Signal Accuracy (30d)")
        for row in accuracy_30d:
            row = dict(row) if not isinstance(row, dict) else row
            total = row["total"]
            correct = row["correct"]
            pct = (correct / total * 100) if total > 0 else 0
            full_report_lines.append(f"- {row['signal_type']}: {pct:.0f}% ({correct}/{total})")
        full_report_lines.append("")

    # Append self-learner report
    full_report_lines.append("## Self-Learning System Report")
    full_report_lines.append(review_report)

    full_report = "\n".join(full_report_lines)
    report_path.write_text(full_report, encoding="utf-8")
    print(f"📄 Full report saved: {report_path}")

    # ── Build WhatsApp scorecard (stdout) ──
    wa_lines = [f"📊 **Nightly Review — {target_date.strftime('%b %d')}**", ""]

    if closed_today:
        day_pnl = sum(t.get("pnl") or 0 for t in closed_today)
        wins = sum(1 for t in closed_today if (t.get("pnl") or 0) > 0)
        wa_lines.append(f"💰 Day PnL: ${day_pnl:+.2f} | Trades: {len(closed_today)} ({wins}W {len(closed_today)-wins}L)")
        for j in judgments:
            emoji = "🟢" if j["pnl"] > 0 else "🔴"
            wa_lines.append(f"{emoji} {j['symbol']}: ${j['pnl']:+.2f} ({j['pnl_pct']:+.1f}%) — Score: {j['composite']}/10")
    else:
        wa_lines.append("📭 No trades closed today")

    wa_lines.append("")

    if open_positions:
        wa_lines.append(f"📍 Open: {', '.join(p['symbol'] for p in open_positions)}")

    # 30d rolling
    if stats_30d:
        trades_30 = stats_30d["trades"] or 0
        wins_30 = stats_30d["wins"] or 0
        pnl_30 = stats_30d["total_pnl"] or 0
        wr_30 = (wins_30 / trades_30 * 100) if trades_30 > 0 else 0
        wa_lines.append(f"📈 30d: {trades_30} trades, {wr_30:.0f}% WR, ${pnl_30:+.2f}")

    # Signal accuracy summary
    if accuracy_30d:
        acc_parts = []
        for row in accuracy_30d:
            row = dict(row) if not isinstance(row, dict) else row
            total = row["total"]
            correct = row["correct"]
            pct = (correct / total * 100) if total > 0 else 0
            sig = row["signal_type"][:3]
            acc_parts.append(f"{sig} {pct:.0f}%")
        wa_lines.append(f"🎯 Accuracy: {' | '.join(acc_parts)}")

    wa_lines.append(f"\n📝 Full: web/nightly_review_{target_date}.md")

    # Print signal_accuracy backfill count
    if scored_count > 0:
        wa_lines.append(f"🔬 Scored {scored_count} new trade(s) into signal_accuracy")

    return "\n".join(wa_lines)


# ── Main ─────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Nightly trading review")
    parser.add_argument("--date", type=str, help="Date to review (YYYY-MM-DD), default=today")
    parser.add_argument("--backfill", action="store_true", help="Score ALL unprocessed closed trades")
    args = parser.parse_args()

    if args.date:
        target = date.fromisoformat(args.date)
    else:
        target = date.today()

    print(f"🌙 Nightly Review — {target}")
    print("=" * 50)

    cfg = load_config()
    db = Database(str(ROOT / "trading.db"))
    learner = SelfLearner(cfg, db)

    # Step 1: Backfill signal_accuracy
    print("\n📊 Step 1: Scoring closed trades...")
    if args.backfill:
        scored = backfill_signal_accuracy(cfg, db, learner, target_date=None)
    else:
        scored = backfill_signal_accuracy(cfg, db, learner, target_date=target)

    if scored == 0:
        # Also try to score any historically unscored trades
        all_scored = backfill_signal_accuracy(cfg, db, learner, target_date=None)
        scored += all_scored

    print(f"  → {scored} trades scored")

    # Step 2: Build scorecard
    print("\n📋 Step 2: Building review...")
    scorecard = build_scorecard(target, db, cfg, learner, scored)

    print("\n" + "=" * 50)
    print("📱 WhatsApp Scorecard:")
    print("=" * 50)
    print(scorecard)

    return scorecard


if __name__ == "__main__":
    main()
