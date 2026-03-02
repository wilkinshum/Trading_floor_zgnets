"""Trading Floor v4.0 entry point.

Supports CLI modes: --intraday-scan, --swing-scan, --swing-exits,
--intraday-close, --nightly-review, --force-close-all, --full
(default: time-appropriate action).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, date
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

ET = ZoneInfo("America/New_York")


def deep_merge(base: dict, overrides: dict) -> dict:
    """Recursively merge overrides into base dict (mutates base)."""
    for k, v in overrides.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            deep_merge(base[k], v)
        else:
            base[k] = v
    return base


def load_config(path: str) -> dict:
    """Load config with optional overrides.yaml merge."""
    with open(path, "r", encoding="utf-8") as f:
        base = yaml.safe_load(f)
    overrides_path = Path(path).parent / "overrides.yaml"
    if overrides_path.exists():
        with open(overrides_path, "r", encoding="utf-8") as f:
            overrides = yaml.safe_load(f)
        if overrides:
            deep_merge(base, overrides)
    return base


def _resolve_env(cfg: dict) -> dict:
    """Replace ${ENV_VAR} placeholders in string values."""
    alpaca = cfg.get("alpaca", {})
    for k, v in alpaca.items():
        if isinstance(v, str) and v.startswith("${") and v.endswith("}"):
            env_key = v[2:-1]
            alpaca[k] = os.environ.get(env_key, "")
    return cfg


def _init_components(cfg):
    """Initialize broker, execution service, budgeter, db."""
    from trading_floor.db import Database
    from trading_floor.broker import (
        AlpacaBroker, ExecutionService, OrderLedger,
        PortfolioState, StrategyBudgeter,
    )

    db_path = Path(cfg["logging"].get("db_path", "trading.db"))
    db = Database(db_path)

    alpaca_cfg = cfg.get("alpaca", {})
    broker = AlpacaBroker(
        api_key=alpaca_cfg.get("api_key", ""),
        api_secret=alpaca_cfg.get("api_secret", ""),
        paper=cfg.get("broker", {}).get("mode", "paper") == "paper",
    )

    portfolio_state = PortfolioState(broker)
    order_ledger = OrderLedger(db, broker)

    strat_budgets = {}
    for name, sc in cfg.get("strategies", {}).items():
        if sc.get("enabled", True):
            strat_budgets[name] = sc.get("budget", 0)

    budgeter = StrategyBudgeter(db, portfolio_state, strat_budgets)

    exec_svc = ExecutionService(broker, order_ledger, budgeter, portfolio_state)

    return broker, exec_svc, budgeter, portfolio_state, db


def _init_self_learner(cfg, db):
    """Initialize the self-learner (lazy — only when needed)."""
    from trading_floor.review import SelfLearner
    return SelfLearner(cfg, db)


def _load_regime_state() -> dict:
    """Load regime_state.json, return empty dict on failure."""
    regime_path = Path("configs/regime_state.json")
    if regime_path.exists():
        try:
            data = json.loads(regime_path.read_text())
            hmm = data.get("hmm", {})
            return {
                "bull_confidence": hmm.get("bull_prob", 0.5),
                "bear_confidence": hmm.get("bear_prob", 0.3),
            }
        except Exception:
            pass
    return {"bull_confidence": 0.5, "bear_confidence": 0.3}


def _get_position_trade_data(db, pos_id: int, strategy: str, exit_price: float = 0) -> dict:
    """Build trade_data dict for self_learner.process_trade() from position_meta."""
    conn = db._get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT symbol, entry_price, entry_qty, entry_time, exit_time, "
            "signals_json, pnl, pnl_pct FROM position_meta WHERE id=?",
            (pos_id,),
        )
        row = cur.fetchone()
        if not row:
            return {}
        symbol, entry_price, entry_qty, entry_time, exit_time, signals_json, pnl, pnl_pct = row
    finally:
        conn.close()

    signal_scores = {}
    if signals_json:
        try:
            signal_scores = json.loads(signals_json)
        except (json.JSONDecodeError, TypeError):
            signal_scores = {"momentum": 0, "meanrev": 0, "breakout": 0, "news": 0}

    position_value = (entry_price or 0) * (entry_qty or 0)

    # Compute PnL if not already set
    if pnl is None and entry_price and exit_price:
        pnl = (exit_price - entry_price) * (entry_qty or 0)

    # Compute holding days
    holding_days = 1.0
    if entry_time and exit_time:
        try:
            from datetime import timezone
            et = datetime.fromisoformat(entry_time)
            xt = datetime.fromisoformat(exit_time)
            holding_days = max((xt - et).total_seconds() / 86400, 0.01)
        except Exception:
            pass

    return {
        "strategy": strategy,
        "symbol": symbol,
        "signal_scores": signal_scores,
        "pnl": pnl or 0.0,
        "position_value": position_value or 1.0,
        "holding_days": holding_days,
        "position_meta_id": pos_id,
        "entry_price": entry_price or 0,
        "entry_time": entry_time,
        "exit_time": exit_time,
    }


def _process_closed_positions(db, self_learner, strategy: str, closed_pos_ids: list,
                               exit_prices: dict = None):
    """Call self_learner.process_trade() for each closed position."""
    regime_state = _load_regime_state()
    for pos_id in closed_pos_ids:
        ep = (exit_prices or {}).get(pos_id, 0)
        trade_data = _get_position_trade_data(db, pos_id, strategy, ep)
        if trade_data and trade_data.get("signal_scores"):
            try:
                self_learner.process_trade(trade_data, regime_state)
            except Exception as e:
                print(f"[SelfLearner] Error processing trade {pos_id}: {e}")


def main():
    ap = argparse.ArgumentParser(description="Trading Floor v4.0")
    ap.add_argument("--config", default="configs/workflow.yaml")
    ap.add_argument("--intraday-scan", action="store_true", help="Run intraday scan + execute")
    ap.add_argument("--swing-scan", action="store_true", help="Run swing scan + execute")
    ap.add_argument("--swing-exits", action="store_true", help="Run swing exit management")
    ap.add_argument("--intraday-close", action="store_true", help="Force close all intraday positions")
    ap.add_argument("--nightly-review", action="store_true", help="Run nightly self-learning review")
    ap.add_argument("--force-close-all", action="store_true", help="Force close ALL positions")
    ap.add_argument("--full", action="store_true", help="Run time-appropriate actions")
    ap.add_argument("--legacy", action="store_true", help="Run legacy TradingFloor.run()")
    args = ap.parse_args()

    cfg = load_config(args.config)
    cfg = _resolve_env(cfg)

    Path(cfg["logging"]["trades_csv"]).parent.mkdir(parents=True, exist_ok=True)
    Path(cfg["logging"]["events_csv"]).parent.mkdir(parents=True, exist_ok=True)

    # Legacy mode
    if args.legacy:
        from trading_floor.workflow import TradingFloor
        tf = TradingFloor(cfg)
        tf.run()
        return

    # Determine mode — if no flag, default to --full
    explicit = any([args.intraday_scan, args.swing_scan, args.swing_exits,
                    args.intraday_close, args.nightly_review, args.force_close_all])
    if not explicit and not args.full:
        args.full = True

    broker, exec_svc, budgeter, portfolio_state, db = _init_components(cfg)
    summary = []

    # Lazy strategy builders
    def get_intraday():
        from trading_floor.strategies.intraday import IntradayStrategy
        return IntradayStrategy(cfg, broker, exec_svc, budgeter, db)

    def get_swing():
        from trading_floor.strategies.swing import SwingStrategy
        return SwingStrategy(cfg, broker, exec_svc, budgeter, db)

    # Lazy self-learner
    _self_learner = [None]

    def get_self_learner():
        if _self_learner[0] is None:
            _self_learner[0] = _init_self_learner(cfg, db)
        return _self_learner[0]

    now = datetime.now(ET)

    if args.nightly_review:
        sl = get_self_learner()
        report = sl.nightly_review()
        print(report)
        # Check if Friday → weekly_apply
        if now.weekday() == 4:  # Friday
            results = sl.weekly_apply()
            print("\n## Weekly Apply Results")
            for strat, info in results.items():
                print(f"  {strat}: tier={info['confidence_tier']}, "
                      f"trades={info['trade_count']}, applied={info['applied']}")
        summary.append("Nightly review complete")

    if args.force_close_all:
        intra = get_intraday()
        intra_results = intra.force_close()
        sw = get_swing()
        swing_positions = sw._get_open_positions()
        swing_results = []
        closed_swing_ids = []
        for pos in swing_positions:
            sym = pos["symbol"]
            alpaca_pos = next(
                (p for p in exec_svc.portfolio.positions if p["symbol"] == sym), None)
            if alpaca_pos:
                qty = abs(alpaca_pos["qty"])
                result = exec_svc.submit(
                    symbol=sym, qty=qty, side="sell", strategy="swing",
                    order_type="market", position_meta_id=pos["id"])
                if result.get("status") == "submitted":
                    sw._close_position_meta(pos["id"], "manual",
                                             alpaca_pos["current_price"])
                    closed_swing_ids.append(pos["id"])
                swing_results.append(result)
        # Process self-learning for all closed
        sl = get_self_learner()
        intra_closed = [r.get("position_meta_id") for r in intra_results
                        if r.get("status") == "submitted" and r.get("position_meta_id")]
        _process_closed_positions(db, sl, "intraday", intra_closed)
        _process_closed_positions(db, sl, "swing", closed_swing_ids)
        summary.append(f"Force close all: {len(intra_results)} intraday, {len(swing_results)} swing")

    if args.intraday_scan:
        strat = get_intraday()
        signals = strat.scan()
        results = strat.execute(signals) if signals else []
        summary.append(f"Intraday: {len(signals)} signals, {sum(1 for r in results if r.get('status')=='submitted')} orders")

    if args.swing_scan:
        strat = get_swing()
        signals = strat.scan()
        results = strat.execute(signals) if signals else []
        summary.append(f"Swing: {len(signals)} signals, {sum(1 for r in results if r.get('status')=='submitted')} orders")

    if args.swing_exits:
        strat = get_swing()
        actions = strat.manage_exits()
        # Process self-learning for closed swing positions
        if actions:
            sl = get_self_learner()
            closed_ids = []
            exit_prices = {}
            for a in actions:
                if a.get("action", "").startswith("exit_"):
                    # Find the position_meta_id from the DB
                    conn = db._get_conn()
                    try:
                        cur = conn.cursor()
                        cur.execute(
                            "SELECT id, exit_price FROM position_meta "
                            "WHERE symbol=? AND strategy='swing' AND status='closed' "
                            "ORDER BY exit_time DESC LIMIT 1",
                            (a["symbol"],))
                        row = cur.fetchone()
                        if row:
                            closed_ids.append(row[0])
                            exit_prices[row[0]] = row[1] or 0
                    finally:
                        conn.close()
            _process_closed_positions(db, sl, "swing", closed_ids, exit_prices)
        summary.append(f"Swing exits: {len(actions)} actions")

    if args.intraday_close:
        strat = get_intraday()
        results = strat.force_close()
        # Process self-learning for closed intraday positions
        if results:
            sl = get_self_learner()
            closed_ids = []
            for r in results:
                if r.get("status") == "submitted":
                    # Find position_meta_id
                    sym = r.get("symbol", "")
                    if not sym:
                        continue
                    conn = db._get_conn()
                    try:
                        cur = conn.cursor()
                        cur.execute(
                            "SELECT id FROM position_meta "
                            "WHERE symbol=? AND strategy='intraday' AND status='closed' "
                            "ORDER BY exit_time DESC LIMIT 1",
                            (sym,))
                        row = cur.fetchone()
                        if row:
                            closed_ids.append(row[0])
                    finally:
                        conn.close()
            _process_closed_positions(db, sl, "intraday", closed_ids)
        summary.append(f"Intraday close: {len(results)} positions closed")

    if args.full:
        # Time-based routing
        # Intraday scan: 9:30-11:30
        if 9 <= now.hour <= 11:
            intra = get_intraday()
            sigs = intra.scan()
            res = intra.execute(sigs) if sigs else []
            summary.append(f"Intraday: {len(sigs)} signals, {sum(1 for r in res if r.get('status')=='submitted')} orders")

        # Swing AM window: 9:40-10:00
        if now.hour == 9 and 40 <= now.minute <= 59 or now.hour == 10 and now.minute == 0:
            sw = get_swing()
            sigs = sw.scan()
            res = sw.execute(sigs) if sigs else []
            summary.append(f"Swing AM: {len(sigs)} signals")

        # Swing PM window: 15:45-15:55
        if now.hour == 15 and 45 <= now.minute <= 55:
            sw = get_swing()
            sigs = sw.scan()
            res = sw.execute(sigs) if sigs else []
            summary.append(f"Swing PM: {len(sigs)} signals")

        # Swing exits: run daily (any time market is open)
        if 9 <= now.hour <= 15:
            sw = get_swing()
            actions = sw.manage_exits()
            if actions:
                summary.append(f"Swing exits: {len(actions)} actions")

        # Intraday force close at 15:45
        if now.hour == 15 and now.minute >= 45:
            intra = get_intraday()
            results = intra.force_close()
            if results:
                summary.append(f"Intraday close: {len(results)} positions")

    if summary:
        print(f"[TradingFloor v4.0] {' | '.join(summary)}")
    else:
        print("[TradingFloor v4.0] No actions taken (outside market hours or no flags)")


if __name__ == "__main__":
    main()
