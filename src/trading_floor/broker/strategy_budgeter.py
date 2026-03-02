"""Per-strategy budget reservation and enforcement.

Prevents double-spend across intraday/swing by reserving dollar amounts
before order submission and releasing on fill or cancellation.
"""

import logging
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)


class StrategyBudgeter:
    """Manages per-strategy budget reservations.

    Args:
        db: Database instance.
        portfolio_state: PortfolioState instance for live position values.
        strategy_budgets: Dict mapping strategy name to max budget, e.g.
            {'intraday': 2000, 'swing': 3000}.
    """

    def __init__(self, db, portfolio_state, strategy_budgets: dict):
        self.db = db
        self.portfolio_state = portfolio_state
        self.strategy_budgets = strategy_budgets

    def reserve(self, strategy: str, symbol: str, amount: float) -> int:
        """Reserve budget for a pending order.

        Args:
            strategy: Strategy name.
            symbol: Ticker symbol.
            amount: Dollar amount to reserve.

        Returns:
            reservation_id (int).

        Raises:
            ValueError: If insufficient budget available.
        """
        available = self.get_available(strategy)
        if amount > available + 0.01:  # small float tolerance
            raise ValueError(
                f"Insufficient budget for {strategy}: "
                f"need ${amount:.2f}, available ${available:.2f}"
            )

        conn = self.db._get_conn()
        try:
            cursor = conn.cursor()
            cursor.execute(
                """INSERT INTO budget_reservations
                   (strategy, symbol, reserved_amount, status, created_at)
                   VALUES (?,?,?,?,?)""",
                (
                    strategy,
                    symbol,
                    amount,
                    "reserved",
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            conn.commit()
            rid = cursor.lastrowid
            logger.info(
                "Budget reserved: %s %s $%.2f (id=%d, avail=$%.2f)",
                strategy, symbol, amount, rid, available - amount,
            )
            return rid
        finally:
            conn.close()

    def release(self, reservation_id: int):
        """Release a budget reservation (on fill, cancel, or rejection)."""
        conn = self.db._get_conn()
        try:
            cursor = conn.cursor()
            cursor.execute(
                """UPDATE budget_reservations
                   SET status='released', released_at=?
                   WHERE id=? AND status='reserved'""",
                (datetime.now(timezone.utc).isoformat(), reservation_id),
            )
            conn.commit()
            if cursor.rowcount:
                logger.info("Budget reservation %d released", reservation_id)
        finally:
            conn.close()

    def mark_filled(self, reservation_id: int, order_id: int):
        """Mark reservation as filled and link to order."""
        conn = self.db._get_conn()
        try:
            cursor = conn.cursor()
            cursor.execute(
                """UPDATE budget_reservations
                   SET status='filled', order_id=?
                   WHERE id=? AND status='reserved'""",
                (order_id, reservation_id),
            )
            conn.commit()
        finally:
            conn.close()

    def get_reserved(self, strategy: str) -> float:
        """Get total reserved (pending) amount for a strategy."""
        conn = self.db._get_conn()
        try:
            cursor = conn.cursor()
            cursor.execute(
                """SELECT COALESCE(SUM(reserved_amount), 0)
                   FROM budget_reservations
                   WHERE strategy=? AND status='reserved'""",
                (strategy,),
            )
            return cursor.fetchone()[0]
        finally:
            conn.close()

    def get_open_position_value(self, strategy: str) -> float:
        """Get total market value of open positions for a strategy."""
        conn = self.db._get_conn()
        try:
            cursor = conn.cursor()
            cursor.execute(
                """SELECT symbol FROM position_meta
                   WHERE strategy=? AND status='open'""",
                (strategy,),
            )
            symbols = [row[0] for row in cursor.fetchall()]
        finally:
            conn.close()

        total = 0.0
        for sym in symbols:
            total += self.portfolio_state.get_position_value(sym)
        return total

    def get_available(self, strategy: str) -> float:
        """Get available budget = max budget - reserved - open positions value."""
        max_budget = self.strategy_budgets.get(strategy, 0)
        reserved = self.get_reserved(strategy)
        positions_value = self.get_open_position_value(strategy)
        available = max_budget - reserved - positions_value
        return max(0.0, available)
