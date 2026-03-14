"""
ORB Executor — Phase 6
Handles order execution for the ORB Trading Desk.
Bridges ExitManager decisions → AlpacaBroker via ExecutionService.
"""

import time
import logging
import threading
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)


class ORBExecutor:
    """Order execution layer for ORB trades.

    Handles: bracket entry, fill confirmation, partial exits,
    full exits, and stop modification. Thread-safe via per-symbol locks.

    Does NOT decide when/what to trade — Monitor (Phase 7) calls this.
    Does NOT compute exit levels — ExitManager provides signals.
    """

    def __init__(self, broker, exec_service, floor_manager,
                 config: Dict[str, Any], db_path: str):
        self.broker = broker
        self.exec_service = exec_service
        self.floor_manager = floor_manager
        self.config = config
        self.db_path = db_path
        self._symbol_locks: Dict[str, threading.Lock] = {}
        self._locks_lock = threading.Lock()

    def _get_symbol_lock(self, symbol: str) -> threading.Lock:
        """Get or create a per-symbol lock."""
        with self._locks_lock:
            if symbol not in self._symbol_locks:
                self._symbol_locks[symbol] = threading.Lock()
            return self._symbol_locks[symbol]

    # ── Entry ────────────────────────────────────────────────

    def enter_position(self, symbol: str, side: str, qty: int,
                       limit_price: float, stop_price: float,
                       tp_price: float, sector: str = "Unknown") -> Dict[str, Any]:
        """Submit bracket order for new ORB position.

        Flow: reserve slot → submit bracket → confirm slot (or release on failure).
        """
        pending_id = None
        with self._get_symbol_lock(symbol):
            try:
                allowed, reason = self.floor_manager.can_open_position(
                    "orb", symbol, sector
                )
                if not allowed:
                    logger.warning("Floor rejected %s: %s", symbol, reason)
                    return {"status": "rejected", "reason": "floor_limit",
                            "detail": reason}

                # Extract pending_id from floor_manager
                pending_id = getattr(self.floor_manager, "last_pending_id", None)
                if pending_id is None:
                    logger.error("No pending_id after reservation for %s", symbol)
                    return {"status": "error", "reason": "no_pending_id"}

                result = self.exec_service.submit(
                    symbol=symbol,
                    qty=qty,
                    side=side,
                    strategy="orb",
                    order_type="limit",
                    limit_price=limit_price,
                    estimated_cost=qty * limit_price,
                    take_profit={"limit_price": round(tp_price, 2)},
                    stop_loss={"stop_price": round(stop_price, 2)},
                )

                if result.get("status") == "rejected":
                    self.floor_manager.release_slot(pending_id)
                    pending_id = None  # Mark as released
                    return result

                # Success — confirm the slot with full details
                self.floor_manager.confirm_position(
                    pending_id, symbol, limit_price, qty
                )
                result["pending_id"] = pending_id
                logger.info("ORB entry submitted: %s %s %s qty=%d @ %.2f",
                            side, symbol, sector, qty, limit_price)
                return result

            except Exception as e:
                if pending_id is not None:
                    try:
                        self.floor_manager.release_slot(pending_id)
                    except Exception:
                        pass
                logger.error("enter_position error for %s: %s", symbol, e)
                return {"status": "error", "reason": str(e)}

    # ── Fill Confirmation ────────────────────────────────────

    def confirm_fill(self, alpaca_order_id: str,
                     timeout: Optional[float] = None) -> Dict[str, Any]:
        """Poll Alpaca for order fill. Cancel on timeout."""
        try:
            timeout_sec = timeout if timeout is not None else self.config.get("confirm_timeout_sec", 30)
            poll_sec = self.config.get("confirm_poll_sec", 5)
            deadline = time.time() + timeout_sec

            while time.time() < deadline:
                order = self.broker.get_order(alpaca_order_id)

                if order.status == "filled":
                    return {
                        "status": "filled",
                        "fill_price": float(order.filled_avg_price),
                        "filled_qty": float(order.filled_qty),
                    }
                if order.status in ("cancelled", "expired", "rejected"):
                    return {"status": "failed", "reason": order.status}

                time.sleep(poll_sec)

            # Timeout — cancel
            try:
                self.broker.cancel_order(alpaca_order_id)
            except Exception as ce:
                logger.error("Failed to cancel on timeout: %s", ce)

            return {"status": "timeout"}

        except Exception as e:
            logger.error("confirm_fill error: %s", e)
            return {"status": "error", "reason": str(e)}

    # ── Partial Exit ─────────────────────────────────────────

    def execute_partial_exit(self, symbol: str, qty: int,
                             limit_price: float,
                             fallback_timeout: float = 10) -> Dict[str, Any]:
        """Limit sell for partial exit; falls back to market if not filled."""
        with self._get_symbol_lock(symbol):
            try:
                result = self.exec_service.submit(
                    symbol=symbol, qty=qty, side="sell",
                    strategy="orb", order_type="limit",
                    limit_price=limit_price,
                )

                if result.get("status") == "rejected":
                    return result

                alpaca_id = result.get("alpaca_order_id")
                if not alpaca_id:
                    return {"status": "error", "reason": "no_alpaca_order_id"}

                # Poll for fill (release lock during sleep — confirm_fill is read-only)
                fill = self.confirm_fill(alpaca_id, timeout=fallback_timeout)

                if fill["status"] == "filled":
                    return {
                        "status": "filled",
                        "fill_price": fill["fill_price"],
                        "qty_sold": qty,
                    }

                # Fallback to market
                logger.info("Partial limit didn't fill for %s, market fallback", symbol)
                try:
                    self.broker.cancel_order(alpaca_id)
                except Exception:
                    pass

                market = self.exec_service.submit(
                    symbol=symbol, qty=qty, side="sell",
                    strategy="orb", order_type="market",
                )

                if market.get("status") == "submitted":
                    return {"status": "market_fallback", "fill_price": None, "qty_sold": qty}
                return market

            except Exception as e:
                logger.error("execute_partial_exit error for %s: %s", symbol, e)
                return {"status": "error", "reason": str(e)}

    # ── Full Exit ────────────────────────────────────────────

    def execute_exit(self, symbol: str, qty: int,
                     order_type: str = "market",
                     price: Optional[float] = None,
                     position_meta_id: Optional[int] = None) -> Dict[str, Any]:
        """Full position exit (trailing stop hit, time stop, force close).

        Calls floor_manager.close_position if available, and attempts
        self-learner scoring (never blocks on scoring failure).
        """
        with self._get_symbol_lock(symbol):
            try:
                params: Dict[str, Any] = {
                    "symbol": symbol,
                    "qty": qty,
                    "side": "sell",
                    "strategy": "orb",
                    "order_type": order_type,
                }
                if order_type == "stop" and price is not None:
                    params["stop_price"] = price
                elif order_type == "limit" and price is not None:
                    params["limit_price"] = price

                result = self.exec_service.submit(**params)

                if result.get("status") == "submitted":
                    # Close in floor manager
                    try:
                        if hasattr(self.floor_manager, "close_position"):
                            self.floor_manager.close_position(symbol, "orb")
                    except Exception as e:
                        logger.warning("floor close_position failed: %s", e)

                    # Self-learner scoring (never block exit)
                    if position_meta_id is not None:
                        self._score_trade(position_meta_id)

                return result

            except Exception as e:
                logger.error("execute_exit error for %s: %s", symbol, e)
                return {"status": "error", "reason": str(e)}

    def _score_trade(self, position_meta_id: int) -> None:
        """Attempt self-learner scoring. Never raises."""
        try:
            from trading_floor.review.self_learner import SelfLearner
            learner = SelfLearner(self.db_path)
            learner._score_closed_trade(position_meta_id)
            logger.info("Trade scored: pm_id=%d", position_meta_id)
        except ImportError:
            logger.debug("Self-learner module not available")
        except Exception as e:
            logger.warning("Scoring failed (non-blocking) pm_id=%d: %s",
                           position_meta_id, e)

    # ── Stop Modification ────────────────────────────────────

    def modify_stop(self, current_order_id: str,
                    new_stop_price: float) -> Dict[str, Any]:
        """Cancel existing stop and submit new one at updated price.

        Prepares replacement params BEFORE cancel to minimize unprotected gap.
        """
        try:
            # Get order details BEFORE canceling (prep new order params first)
            try:
                current_order = self.broker.get_order(current_order_id)
                symbol = current_order.symbol
                qty = int(float(current_order.qty))
            except Exception as e:
                return {"status": "error", "reason": f"get_order_failed: {e}"}

            # Pre-build replacement params
            new_order_params = {
                "symbol": symbol,
                "qty": qty,
                "side": "sell",
                "order_type": "stop",
                "time_in_force": self.config.get("time_in_force", "day"),
                "stop_price": new_stop_price,
            }

            with self._get_symbol_lock(symbol):
                # Cancel + immediately resubmit (minimize gap)
                try:
                    self.broker.cancel_order(current_order_id)
                except Exception:
                    return {"status": "failed", "reason": "cancel_failed"}

                new_order = self.broker.submit_order(**new_order_params)

                return {
                    "status": "replaced",
                    "new_order_id": str(new_order.id),
                    "new_stop_price": new_stop_price,
                }

        except Exception as e:
            logger.error("modify_stop error: %s", e)
            return {"status": "error", "reason": str(e)}
