"""Order and fill tracking ledger.

Maps Alpaca order IDs to local DB rows. Handles partial fills by
accumulating quantity and computing weighted average price.
"""

import logging
from datetime import datetime, timezone
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)


class OrderLedger:
    """Tracks orders and fills in the local database.

    Args:
        db: Database instance (trading_floor.db.Database).
        broker: AlpacaBroker instance for status syncing.
    """

    def __init__(self, db, broker=None):
        self.db = db
        self.broker = broker

    def record_order(
        self,
        alpaca_order_id: str,
        client_order_id: str,
        symbol: str,
        strategy: str,
        side: str,
        order_type: str,
        qty: float,
        limit_price: Optional[float] = None,
        stop_price: Optional[float] = None,
        position_meta_id: Optional[int] = None,
    ) -> int:
        """Insert an order into the local DB. Returns the local order id."""
        conn = self.db._get_conn()
        try:
            cursor = conn.cursor()
            cursor.execute(
                """INSERT INTO orders
                   (alpaca_order_id, client_order_id, position_meta_id,
                    symbol, strategy, side, order_type, qty,
                    limit_price, stop_price, status, submitted_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    alpaca_order_id,
                    client_order_id,
                    position_meta_id,
                    symbol,
                    strategy,
                    side,
                    order_type,
                    qty,
                    limit_price,
                    stop_price,
                    "pending",
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            conn.commit()
            return cursor.lastrowid
        finally:
            conn.close()

    def record_fill(
        self,
        order_id: int,
        alpaca_order_id: str,
        fill_price: float,
        fill_qty: float,
        fill_time: Optional[str] = None,
    ):
        """Record a (partial) fill and update the order's filled_qty / avg price."""
        if fill_time is None:
            fill_time = datetime.now(timezone.utc).isoformat()

        conn = self.db._get_conn()
        try:
            cursor = conn.cursor()
            # Insert fill row
            cursor.execute(
                """INSERT INTO fills (order_id, alpaca_order_id, fill_price, fill_qty, fill_time)
                   VALUES (?,?,?,?,?)""",
                (order_id, alpaca_order_id, fill_price, fill_qty, fill_time),
            )

            # Recompute avg fill price from all fills for this order
            cursor.execute(
                "SELECT fill_price, fill_qty FROM fills WHERE order_id=?",
                (order_id,),
            )
            fills = cursor.fetchall()
            total_qty = sum(f[1] for f in fills)
            avg_price = sum(f[0] * f[1] for f in fills) / total_qty if total_qty else 0

            cursor.execute(
                """UPDATE orders SET filled_qty=?, avg_fill_price=? WHERE id=?""",
                (total_qty, avg_price, order_id),
            )
            conn.commit()
        finally:
            conn.close()

    def update_status(self, order_id: int, status: str):
        """Update an order's status (pending → filled/cancelled/rejected)."""
        conn = self.db._get_conn()
        try:
            cursor = conn.cursor()
            ts_field = None
            if status == "filled":
                ts_field = "filled_at"
            elif status in ("cancelled", "canceled"):
                ts_field = "cancelled_at"
                status = "cancelled"

            now = datetime.now(timezone.utc).isoformat()
            if ts_field:
                cursor.execute(
                    f"UPDATE orders SET status=?, {ts_field}=? WHERE id=?",
                    (status, now, order_id),
                )
            else:
                cursor.execute(
                    "UPDATE orders SET status=? WHERE id=?", (status, order_id)
                )
            conn.commit()
        finally:
            conn.close()

    def sync_order(self, order_id: int) -> Dict[str, Any]:
        """Sync a single order's status from Alpaca.

        Returns dict with updated fields.
        """
        if not self.broker:
            raise RuntimeError("No broker attached for syncing")

        conn = self.db._get_conn()
        try:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT alpaca_order_id, status FROM orders WHERE id=?", (order_id,)
            )
            row = cursor.fetchone()
            if not row:
                raise ValueError(f"Order {order_id} not found")
        finally:
            conn.close()

        alpaca_order_id, local_status = row
        if local_status in ("filled", "cancelled", "rejected"):
            return {"status": local_status, "changed": False}

        alpaca_order = self.broker.get_order(alpaca_order_id)
        new_status = str(alpaca_order.status).lower().replace("orderstatus.", "")

        # Record any new fills
        if alpaca_order.filled_qty and float(alpaca_order.filled_qty) > 0:
            filled_qty = float(alpaca_order.filled_qty)
            avg_price = float(alpaca_order.filled_avg_price) if alpaca_order.filled_avg_price else 0
            # Check current fills
            conn = self.db._get_conn()
            try:
                cursor = conn.cursor()
                cursor.execute("SELECT SUM(fill_qty) FROM fills WHERE order_id=?", (order_id,))
                existing = cursor.fetchone()[0] or 0
            finally:
                conn.close()

            if filled_qty > existing:
                self.record_fill(
                    order_id, alpaca_order_id, avg_price, filled_qty - existing
                )

        if new_status != local_status:
            self.update_status(order_id, new_status)

        return {"status": new_status, "changed": new_status != local_status}

    def get_order_by_alpaca_id(self, alpaca_order_id: str) -> Optional[Dict[str, Any]]:
        """Look up a local order by its Alpaca order ID."""
        conn = self.db._get_conn()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM orders WHERE alpaca_order_id=?", (alpaca_order_id,))
            row = cursor.fetchone()
            if not row:
                return None
            cols = [d[0] for d in cursor.description]
            return dict(zip(cols, row))
        finally:
            conn.close()

    def get_order(self, order_id: int) -> Optional[Dict[str, Any]]:
        """Look up a local order by local ID."""
        conn = self.db._get_conn()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM orders WHERE id=?", (order_id,))
            row = cursor.fetchone()
            if not row:
                return None
            cols = [d[0] for d in cursor.description]
            return dict(zip(cols, row))
        finally:
            conn.close()
