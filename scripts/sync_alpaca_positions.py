"""
sync_alpaca_positions.py
Syncs open Alpaca positions + their entry orders into the local SQLite DB.
Skips positions already tracked in position_meta (by symbol + status='open').
Run periodically or on-demand to backfill manually-placed trades.
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.chdir(os.path.join(os.path.dirname(__file__), ".."))

import sqlite3
from datetime import datetime, timezone
from trading_floor.run import load_config

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import GetOrdersRequest
from alpaca.trading.enums import QueryOrderStatus, OrderSide

def main():
    cfg = load_config("configs/workflow.yaml")
    a = cfg.get("alpaca", {})
    tc = TradingClient(api_key=a["api_key"], secret_key=a["api_secret"], paper=True)

    db_path = cfg.get("logging", {}).get("db_path", "trading.db")
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row
    cur = db.cursor()

    # Get symbols already tracked as open in position_meta
    cur.execute("SELECT symbol FROM position_meta WHERE status='open'")
    tracked = {r["symbol"] for r in cur.fetchall()}

    # Get all open Alpaca positions
    positions = tc.get_all_positions()
    if not positions:
        print("No open Alpaca positions.")
        return

    synced = 0
    skipped = 0

    for pos in positions:
        sym = pos.symbol
        if sym in tracked:
            print(f"  SKIP {sym} — already tracked in DB")
            skipped += 1
            continue

        # Find the original BUY fill for this position from Alpaca order history
        try:
            req = GetOrdersRequest(
                status=QueryOrderStatus.CLOSED,
                symbols=[sym],
                limit=50,
            )
            orders = tc.get_orders(req)
        except Exception as e:
            print(f"  ERROR fetching orders for {sym}: {e}")
            continue

        # Find earliest filled buy order (the entry)
        entry_order = None
        for o in reversed(orders):  # oldest first
            if o.side == OrderSide.BUY and o.filled_at and float(o.filled_qty or 0) > 0:
                entry_order = o
                break

        if not entry_order:
            print(f"  WARN {sym} — no filled BUY order found on Alpaca, using position avg_entry")
            entry_time = None
            entry_order_id = None
        else:
            entry_time = str(entry_order.filled_at)
            entry_order_id = str(entry_order.id)

        side = "buy" if float(pos.qty) > 0 else "sell"
        entry_price = float(pos.avg_entry_price)
        entry_qty = abs(float(pos.qty))

        # Find any open sell orders (TP/SL) for this symbol
        try:
            open_req = GetOrdersRequest(
                status=QueryOrderStatus.OPEN,
                symbols=[sym],
            )
            open_orders = tc.get_orders(open_req)
        except Exception:
            open_orders = []

        stop_price = None
        tp_price = None
        for o in open_orders:
            if o.side == OrderSide.SELL:
                if o.order_type.value == "limit":
                    tp_price = float(o.limit_price) if o.limit_price else None
                elif o.order_type.value == "stop":
                    stop_price = float(o.stop_price) if o.stop_price else None
                elif o.order_type.value == "stop_limit":
                    stop_price = float(o.stop_price) if o.stop_price else None

        # Insert into position_meta
        cur.execute("""
            INSERT INTO position_meta
            (symbol, strategy, side, entry_order_id, entry_price, entry_time,
             entry_qty, stop_price, tp_price, max_hold_days, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open')
        """, (sym, "swing", side, entry_order_id, entry_price, entry_time,
              entry_qty, stop_price, tp_price, 10))

        pm_id = cur.lastrowid

        # Also insert the entry order into orders table
        if entry_order:
            cur.execute("""
                INSERT INTO orders
                (alpaca_order_id, client_order_id, position_meta_id, symbol,
                 strategy, side, order_type, qty, filled_qty, limit_price,
                 stop_price, avg_fill_price, status, submitted_at, filled_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                str(entry_order.id), str(entry_order.client_order_id), pm_id, sym,
                "swing", "buy", entry_order.order_type.value,
                float(entry_order.qty), float(entry_order.filled_qty or 0),
                float(entry_order.limit_price) if entry_order.limit_price else None,
                float(entry_order.stop_price) if entry_order.stop_price else None,
                float(entry_order.filled_avg_price) if entry_order.filled_avg_price else None,
                "filled", str(entry_order.submitted_at), str(entry_order.filled_at),
            ))

        # Insert open sell orders too
        for o in open_orders:
            if o.side == OrderSide.SELL:
                cur.execute("""
                    INSERT INTO orders
                    (alpaca_order_id, client_order_id, position_meta_id, symbol,
                     strategy, side, order_type, qty, filled_qty, limit_price,
                     stop_price, avg_fill_price, status, submitted_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    str(o.id), str(o.client_order_id), pm_id, sym,
                    "swing", "sell", o.order_type.value,
                    float(o.qty), float(o.filled_qty or 0),
                    float(o.limit_price) if o.limit_price else None,
                    float(o.stop_price) if o.stop_price else None,
                    None, str(o.status.value), str(o.submitted_at),
                ))

        print(f"  SYNCED {sym}: {side} {entry_qty} @ ${entry_price:.2f}, "
              f"entry={entry_time}, SL=${stop_price}, TP=${tp_price}, pm_id={pm_id}")
        synced += 1

    db.commit()
    db.close()
    print(f"\nDone: {synced} synced, {skipped} already tracked.")


if __name__ == "__main__":
    main()
