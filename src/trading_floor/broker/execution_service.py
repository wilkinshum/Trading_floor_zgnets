"""Serialized execution service for order submission.

Single entry point for all orders. Provides deduplication (no duplicate
orders for same symbol+strategy within 60s), budget validation, and
serialized execution to prevent race conditions.
"""

import time
import threading
import logging
from datetime import datetime, timezone
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)

_DEDUP_WINDOW = 60  # seconds


class ExecutionService:
    """Serialized order queue with deduplication and budget validation.

    Args:
        broker: AlpacaBroker instance.
        order_ledger: OrderLedger instance.
        strategy_budgeter: StrategyBudgeter instance.
        portfolio_state: PortfolioState instance.
    """

    def __init__(self, broker, order_ledger, strategy_budgeter, portfolio_state):
        self.broker = broker
        self.ledger = order_ledger
        self.budgeter = strategy_budgeter
        self.portfolio = portfolio_state
        self._lock = threading.Lock()
        self._recent_orders: Dict[str, float] = {}  # key → timestamp

    def _dedup_key(self, symbol: str, strategy: str, side: str) -> str:
        return f"{strategy}:{symbol}:{side}"

    def _is_duplicate(self, symbol: str, strategy: str, side: str) -> bool:
        """Check if a similar order was submitted within the dedup window."""
        key = self._dedup_key(symbol, strategy, side)
        now = time.monotonic()
        # Clean old entries
        self._recent_orders = {
            k: v for k, v in self._recent_orders.items()
            if now - v < _DEDUP_WINDOW
        }
        return key in self._recent_orders

    def _record_submission(self, symbol: str, strategy: str, side: str):
        key = self._dedup_key(symbol, strategy, side)
        self._recent_orders[key] = time.monotonic()

    def submit(
        self,
        symbol: str,
        qty: float,
        side: str,
        strategy: str,
        order_type: str = "market",
        limit_price: Optional[float] = None,
        stop_price: Optional[float] = None,
        estimated_cost: Optional[float] = None,
        position_meta_id: Optional[int] = None,
        take_profit: Optional[Dict[str, float]] = None,
        stop_loss: Optional[Dict[str, float]] = None,
    ) -> Dict[str, Any]:
        """Submit an order through the execution pipeline.

        Steps:
            1. Deduplication check
            2. Budget reservation (for buys)
            3. Order submission to Alpaca
            4. Record in local ledger
            5. Return result

        Returns:
            Dict with 'status' ('submitted'/'rejected'), 'order_id', 'reason', etc.
        """
        with self._lock:
            return self._submit_locked(
                symbol, qty, side, strategy, order_type,
                limit_price, stop_price, estimated_cost,
                position_meta_id, take_profit, stop_loss,
            )

    def _submit_locked(
        self, symbol, qty, side, strategy, order_type,
        limit_price, stop_price, estimated_cost,
        position_meta_id, take_profit, stop_loss,
    ) -> Dict[str, Any]:
        # 1. Deduplication
        if self._is_duplicate(symbol, strategy, side):
            reason = f"Duplicate order: {strategy} {side} {symbol} within {_DEDUP_WINDOW}s"
            logger.warning(reason)
            return {"status": "rejected", "reason": reason}

        # 2. Budget reservation (buys only)
        reservation_id = None
        if side.lower() == "buy" and estimated_cost:
            try:
                reservation_id = self.budgeter.reserve(
                    strategy, symbol, estimated_cost
                )
            except ValueError as e:
                return {"status": "rejected", "reason": str(e)}

        # 3. Submit to Alpaca
        client_order_id = self.broker.make_client_order_id(strategy, symbol)
        try:
            alpaca_order = self.broker.submit_order(
                symbol=symbol,
                qty=qty,
                side=side,
                order_type=order_type,
                limit_price=limit_price,
                stop_price=stop_price,
                client_order_id=client_order_id,
                take_profit=take_profit,
                stop_loss=stop_loss,
            )
        except Exception as e:
            # Release reservation on failure
            if reservation_id:
                self.budgeter.release(reservation_id)
            logger.error("Order submission failed: %s", e)
            return {"status": "rejected", "reason": str(e)}

        # 4. Record in ledger
        alpaca_id = str(alpaca_order.id)
        local_order_id = self.ledger.record_order(
            alpaca_order_id=alpaca_id,
            client_order_id=client_order_id,
            symbol=symbol,
            strategy=strategy,
            side=side,
            order_type=order_type,
            qty=qty,
            limit_price=limit_price,
            stop_price=stop_price,
            position_meta_id=position_meta_id,
        )

        # Link reservation to order
        if reservation_id:
            self.budgeter.mark_filled(reservation_id, local_order_id)

        # 5. Record for dedup
        self._record_submission(symbol, strategy, side)

        # Invalidate portfolio cache
        self.portfolio.invalidate()

        logger.info(
            "Order submitted: %s %s %s qty=%.1f (alpaca=%s, local=%d)",
            strategy, side, symbol, qty, alpaca_id, local_order_id,
        )

        return {
            "status": "submitted",
            "order_id": local_order_id,
            "alpaca_order_id": alpaca_id,
            "client_order_id": client_order_id,
            "reservation_id": reservation_id,
        }
