#!/usr/bin/env python3
"""
End-to-end functional test for V4.0 on Alpaca paper account.
Tests the full trade lifecycle: signal → entry → exit → self-learning.
Uses real Alpaca API (paper trading).

Usage: python scripts/functional_test.py
"""

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Ensure project root on path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
os.chdir(ROOT)

import yaml

STEPS = []


def step(name, fn):
    """Run a test step and record PASS/FAIL."""
    try:
        result = fn()
        STEPS.append(("PASS", name, result or ""))
        print(f"  ✅ {name}")
        return result
    except Exception as e:
        STEPS.append(("FAIL", name, str(e)))
        print(f"  ❌ {name}: {e}")
        return None


def main():
    print("=" * 60)
    print("Trading Floor V4.0 — Functional Test")
    print(f"Time: {datetime.now()}")
    print("=" * 60)

    # ── Step 1: Load config ──
    def load_cfg():
        from trading_floor.run import load_config, _resolve_env
        cfg = load_config("configs/workflow.yaml")
        cfg = _resolve_env(cfg)
        return cfg

    cfg = step("1. Load config", load_cfg)
    if not cfg:
        print("\nFATAL: Cannot load config")
        sys.exit(1)

    # ── Step 2: Init components ──
    components = {}

    def init_all():
        from trading_floor.db import Database
        from trading_floor.broker import (
            AlpacaBroker, ExecutionService, OrderLedger,
            PortfolioState, StrategyBudgeter,
        )
        from trading_floor.review import SelfLearner

        db = Database(Path(cfg["logging"].get("db_path", "trading.db")))

        alpaca_cfg = cfg.get("alpaca", {})
        broker = AlpacaBroker(
            api_key=alpaca_cfg.get("api_key", ""),
            api_secret=alpaca_cfg.get("api_secret", ""),
            paper=True,
        )

        portfolio = PortfolioState(broker)
        ledger = OrderLedger(db, broker)
        budgeter = StrategyBudgeter(db, portfolio,
                                     {"intraday": 2000, "swing": 3000})
        exec_svc = ExecutionService(broker, ledger, budgeter, portfolio)
        sl = SelfLearner(cfg, db)

        components.update({
            "db": db, "broker": broker, "portfolio": portfolio,
            "ledger": ledger, "budgeter": budgeter, "exec_svc": exec_svc,
            "self_learner": sl,
        })
        return f"equity=${portfolio.equity:.2f}, buying_power=${portfolio.buying_power:.2f}"

    step("2. Init broker + components", init_all)
    if not components:
        print("\nFATAL: Cannot init components")
        sys.exit(1)

    exec_svc = components["exec_svc"]
    db = components["db"]
    broker = components["broker"]
    portfolio = components["portfolio"]
    sl = components["self_learner"]

    # ── Step 3: Check account ──
    def check_account():
        acct = broker.get_account()
        bp = float(acct.buying_power)
        assert bp > 500, f"Buying power too low: ${bp}"
        return f"Account {acct.account_number}, BP=${bp:.2f}"

    step("3. Check Alpaca account", check_account)

    # ── Step 4: Buy 1 share of SPY ──
    buy_result = {}

    def buy_spy():
        nonlocal buy_result
        # Create position_meta manually
        conn = db._get_conn()
        try:
            cur = conn.cursor()
            signal_scores = {"momentum": 0.7, "meanrev": -0.1, "breakout": 0.2, "news": 0.3}
            cur.execute(
                """INSERT INTO position_meta
                   (symbol, strategy, side, entry_price, entry_time, entry_qty,
                    signals_json, status)
                   VALUES (?,?,?,?,?,?,?,?)""",
                ("SPY", "intraday", "buy", 0, datetime.now(timezone.utc).isoformat(),
                 1, json.dumps(signal_scores), "open"),
            )
            conn.commit()
            pos_id = cur.lastrowid
        finally:
            conn.close()

        result = exec_svc.submit(
            symbol="SPY", qty=1, side="buy", strategy="intraday",
            order_type="market", estimated_cost=600,
            position_meta_id=pos_id,
        )
        assert result["status"] == "submitted", f"Order rejected: {result.get('reason')}"
        buy_result.update(result)
        buy_result["pos_id"] = pos_id
        return f"order_id={result['order_id']}, alpaca={result['alpaca_order_id']}"

    step("4. Submit market BUY 1 SPY", buy_spy)

    # ── Step 5: Wait for fill ──
    def wait_buy_fill():
        order_id = buy_result.get("order_id")
        if not order_id:
            raise RuntimeError("No order to poll")
        ledger = components["ledger"]
        for _ in range(30):
            info = ledger.sync_order(order_id)
            if info["status"] == "filled":
                return f"Filled (status={info['status']})"
            time.sleep(1)
        # Check final status
        info = ledger.sync_order(order_id)
        if info["status"] == "filled":
            return f"Filled"
        raise RuntimeError(f"Order not filled after 30s (status={info['status']})")

    step("5. Wait for BUY fill", wait_buy_fill)

    # ── Step 6: Verify DB records ──
    def verify_db():
        conn = db._get_conn()
        try:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM orders WHERE symbol='SPY' AND strategy='intraday'")
            order_count = cur.fetchone()[0]
            assert order_count > 0, "No orders in DB"
            cur.execute("SELECT COUNT(*) FROM fills WHERE alpaca_order_id=?",
                        (buy_result.get("alpaca_order_id"),))
            fill_count = cur.fetchone()[0]
        finally:
            conn.close()
        return f"orders={order_count}, fills={fill_count}"

    step("6. Verify orders/fills in DB", verify_db)

    # ── Step 7: Update position_meta with entry price ──
    def update_entry_price():
        conn = db._get_conn()
        try:
            cur = conn.cursor()
            cur.execute("SELECT avg_fill_price FROM orders WHERE id=?",
                        (buy_result.get("order_id"),))
            row = cur.fetchone()
            fill_price = row[0] if row else 0
            cur.execute("UPDATE position_meta SET entry_price=? WHERE id=?",
                        (fill_price, buy_result["pos_id"]))
            conn.commit()
        finally:
            conn.close()
        return f"entry_price={fill_price}"

    step("7. Record entry price in position_meta", update_entry_price)

    # ── Step 8: Close position (sell SPY) ──
    sell_result = {}

    def sell_spy():
        nonlocal sell_result
        # Small sleep to avoid dedup
        time.sleep(2)
        result = exec_svc.submit(
            symbol="SPY", qty=1, side="sell", strategy="intraday",
            order_type="market", position_meta_id=buy_result["pos_id"],
        )
        assert result["status"] == "submitted", f"Sell rejected: {result.get('reason')}"
        sell_result.update(result)
        return f"order_id={result['order_id']}"

    step("8. Submit market SELL 1 SPY", sell_spy)

    # ── Step 9: Wait for sell fill ──
    def wait_sell_fill():
        order_id = sell_result.get("order_id")
        if not order_id:
            raise RuntimeError("No sell order")
        ledger = components["ledger"]
        for _ in range(30):
            info = ledger.sync_order(order_id)
            if info["status"] == "filled":
                return "Filled"
            time.sleep(1)
        info = ledger.sync_order(order_id)
        if info["status"] == "filled":
            return "Filled"
        raise RuntimeError(f"Sell not filled (status={info['status']})")

    step("9. Wait for SELL fill", wait_sell_fill)

    # ── Step 10: Close position_meta + compute PnL ──
    def close_meta():
        conn = db._get_conn()
        try:
            cur = conn.cursor()
            cur.execute("SELECT avg_fill_price FROM orders WHERE id=?",
                        (sell_result.get("order_id"),))
            row = cur.fetchone()
            exit_price = row[0] if row else 0

            cur.execute("SELECT entry_price FROM position_meta WHERE id=?",
                        (buy_result["pos_id"],))
            row = cur.fetchone()
            entry_price = row[0] if row else 0
            pnl = (exit_price - entry_price) * 1

            cur.execute(
                "UPDATE position_meta SET status='closed', exit_price=?, "
                "exit_time=?, exit_reason='manual', pnl=? WHERE id=?",
                (exit_price, datetime.now(timezone.utc).isoformat(), pnl,
                 buy_result["pos_id"]))
            conn.commit()
        finally:
            conn.close()
        return f"exit_price={exit_price}, pnl=${pnl:.4f}"

    step("10. Close position_meta + PnL", close_meta)

    # ── Step 11: Self-learner process_trade ──
    def process_trade():
        from trading_floor.run import _get_position_trade_data, _load_regime_state
        trade_data = _get_position_trade_data(db, buy_result["pos_id"], "intraday")
        regime_state = _load_regime_state()
        sl.process_trade(trade_data, regime_state)
        # Verify signal_accuracy record
        conn = db._get_conn()
        try:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM signal_accuracy WHERE position_meta_id=?",
                        (buy_result["pos_id"],))
            count = cur.fetchone()[0]
        finally:
            conn.close()
        assert count > 0, "No signal_accuracy records created"
        return f"{count} signal_accuracy records"

    step("11. Self-learner process_trade", process_trade)

    # ── Step 12: Check mw_state.json updated ──
    def check_mw():
        mw_path = Path("configs/mw_state.json")
        assert mw_path.exists(), "mw_state.json not found"
        data = json.loads(mw_path.read_text())
        assert "intraday" in data, "No intraday in mw_state"
        return f"Keys: {list(data.keys())}"

    step("12. Verify mw_state.json", check_mw)

    # ── Step 13: Nightly review ──
    def nightly():
        report = sl.nightly_review()
        assert len(report) > 50, "Report too short"
        review_path = Path("memory/reviews") / f"{datetime.now().strftime('%Y-%m-%d')}.md"
        assert review_path.exists(), f"Review file not saved at {review_path}"
        return f"Report length={len(report)} chars"

    step("13. Nightly review", nightly)

    # ── Step 14: Verify flat ──
    def check_flat():
        portfolio.invalidate()
        spy_pos = [p for p in portfolio.positions if p["symbol"] == "SPY"]
        if spy_pos:
            raise RuntimeError(f"Still holding SPY: {spy_pos}")
        return "Portfolio flat (no SPY)"

    step("14. Verify portfolio flat", check_flat)

    # ── Summary ──
    print("\n" + "=" * 60)
    print("FUNCTIONAL TEST SUMMARY")
    print("=" * 60)
    passes = sum(1 for s in STEPS if s[0] == "PASS")
    fails = sum(1 for s in STEPS if s[0] == "FAIL")
    for status, name, detail in STEPS:
        icon = "✅" if status == "PASS" else "❌"
        print(f"  {icon} {name}: {detail}")
    print(f"\n{passes} PASSED, {fails} FAILED out of {len(STEPS)} steps")
    sys.exit(0 if fails == 0 else 1)


if __name__ == "__main__":
    main()
