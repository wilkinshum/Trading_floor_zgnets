"""
ORB Exit Manager — Tiered exit logic (Phase 5)

Pure logic class — no DB, no broker calls. Returns exit action dicts.
Caller (Monitor, Phase 7) handles execution and scoring.

Architect notes (B+):
- Tightest stop wins (never loosen)
- Partial exit: limit with timeout, market fallback (execution layer)
- Simultaneous loss protection → Portfolio Intelligence, not here
- Halt protection → Monitor, not here
- Scoring → caller handles after fill
"""
import logging
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")


class ORBExitManager:
    """Per-position exit logic for ORB desk."""

    def __init__(self, config: dict, broker=None):
        try:
            self.config = config or {}
            self.broker = broker
            exit_cfg = self.config.get("exit") or {}
            self.partial_pct = float(exit_cfg.get("partial_pct", 0.50))
            self.partial_target_pct = float(exit_cfg.get("partial_target_pct", 0.50))
            self.trailing_atr_mult = float(exit_cfg.get("trailing_atr_mult", 0.50))
            self.trailing_min_pct = float(exit_cfg.get("trailing_min_pct", 0.0075))
            self.stop_mm_pct = float(exit_cfg.get("stop_mm_pct", 0.30))
            self.time_breakeven = self._parse_time(exit_cfg.get("time_breakeven", "10:45"))
            self.time_tight = self._parse_time(exit_cfg.get("time_tight", "11:00"))
            self.time_tight_pct = float(exit_cfg.get("time_tight_pct", 0.003))
            self.time_force_close = self._parse_time(exit_cfg.get("time_force_close", "11:30"))
            self.tz = ET
        except Exception as exc:
            logger.exception("ORBExitManager init failed: %s", exc)
            # Set safe defaults so object is usable
            self.partial_pct = 0.50
            self.partial_target_pct = 0.50
            self.trailing_atr_mult = 0.50
            self.trailing_min_pct = 0.0075
            self.stop_mm_pct = 0.30
            self.time_breakeven = dtime(10, 45)
            self.time_tight = dtime(11, 0)
            self.time_tight_pct = 0.003
            self.time_force_close = dtime(11, 30)
            self.tz = ET

    @staticmethod
    def _parse_time(tstr: str) -> dtime:
        try:
            parts = str(tstr).split(":")
            return dtime(int(parts[0]), int(parts[1]))
        except Exception:
            return dtime(0, 0)

    def _to_et_time(self, dt: datetime) -> dtime:
        try:
            if dt.tzinfo is None:
                return dt.time()
            return dt.astimezone(self.tz).time()
        except Exception:
            return dt.time() if isinstance(dt, datetime) else dtime(0, 0)

    # ── Main entry point ─────────────────────────────────────

    def check_exit(self, position: dict) -> dict | None:
        """Called by monitor on each tick. Returns exit action or None.

        position keys: symbol, side, entry_price, qty, remaining_qty,
                       measured_move, retest_low, entry_time, partial_done,
                       current_price, current_time, atr_1min, trailing_stop
        """
        try:
            if not position:
                return None

            side = (position.get("side") or "long").lower()
            entry_price = float(position["entry_price"])
            measured_move = float(position["measured_move"])
            retest_low = position.get("retest_low")
            current_price = float(position["current_price"])
            remaining_qty = int(position.get("remaining_qty") or position.get("qty", 0))
            current_time = position.get("current_time")
            if not current_time:
                return None

            ct = self._to_et_time(current_time)

            # Priority 1: Time-stop (11:30 — no exceptions)
            if ct >= self.time_force_close:
                return {
                    "action": "time_stop",
                    "qty": remaining_qty,
                    "order_type": "market",
                    "price": None,
                    "reason": "force_close_1130",
                }

            # Priority 2: Stop loss check
            stop = self.compute_initial_stop(measured_move, entry_price, retest_low, side)
            if stop is not None:
                hit = (side == "long" and current_price <= stop) or \
                      (side == "short" and current_price >= stop)
                if hit:
                    return {
                        "action": "stop_loss",
                        "qty": remaining_qty,
                        "order_type": "market",
                        "price": None,
                        "reason": "initial_stop_hit",
                    }

            # Priority 3: Trailing stop (after partial done)
            if position.get("partial_done"):
                trail_stop = self.compute_trailing_stop(position)
                if trail_stop is not None:
                    # Architect rec: tightest stop wins
                    # If time decay would be tighter, use that instead
                    effective_stop = trail_stop
                    if ct >= self.time_tight and not self.should_partial_exit(position):
                        if side == "long":
                            tight = current_price * (1 - self.time_tight_pct)
                            effective_stop = max(trail_stop, tight)
                        else:
                            tight = current_price * (1 + self.time_tight_pct)
                            effective_stop = min(trail_stop, tight)
                    elif ct >= self.time_breakeven:
                        if side == "long":
                            effective_stop = max(trail_stop, entry_price)
                        else:
                            effective_stop = min(trail_stop, entry_price)

                    trail_hit = (side == "long" and current_price <= effective_stop) or \
                                (side == "short" and current_price >= effective_stop)
                    if trail_hit:
                        return {
                            "action": "trailing_stop",
                            "qty": remaining_qty,
                            "order_type": "market",
                            "price": None,
                            "reason": "trailing_stop_hit",
                        }

            # Priority 4: Time decay (only if partial NOT done and target NOT hit)
            time_action = self.get_time_decay_action(position)
            if time_action:
                if time_action == "time_decay_be":
                    return {
                        "action": "time_decay_be",
                        "qty": 0,  # 0 = stop adjustment, not a trade
                        "order_type": "stop",
                        "price": entry_price,
                        "reason": "move_stop_to_breakeven_1045",
                    }
                elif time_action == "time_decay_tight":
                    if side == "long":
                        tight_stop = current_price * (1 - self.time_tight_pct)
                    else:
                        tight_stop = current_price * (1 + self.time_tight_pct)
                    return {
                        "action": "time_decay_tight",
                        "qty": 0,
                        "order_type": "stop",
                        "price": round(tight_stop, 2),
                        "reason": "tight_trail_1100",
                    }

            # Priority 5: Partial exit
            if self.should_partial_exit(position):
                partial_qty = max(1, int(round(remaining_qty * self.partial_pct)))
                if side == "long":
                    limit_price = round(current_price - 0.02, 2)
                else:
                    limit_price = round(current_price + 0.02, 2)
                return {
                    "action": "partial",
                    "qty": partial_qty,
                    "order_type": "limit",
                    "price": limit_price,
                    "reason": "partial_50pct_target",
                }

            return None

        except Exception as exc:
            logger.exception("check_exit error: %s", exc)
            return None

    # ── Component methods ────────────────────────────────────

    def compute_initial_stop(self, measured_move: float, entry_price: float,
                              retest_low: float | None, side: str) -> float | None:
        """Initial stop = tighter of 30% MM or retest candle low."""
        try:
            side = (side or "long").lower()
            mm_dist = measured_move * self.stop_mm_pct

            if side == "long":
                mm_stop = entry_price - mm_dist
                if retest_low is not None:
                    return max(mm_stop, float(retest_low))  # tighter = higher for longs
                return mm_stop
            else:
                mm_stop = entry_price + mm_dist
                if retest_low is not None:
                    return min(mm_stop, float(retest_low))  # tighter = lower for shorts
                return mm_stop
        except Exception as exc:
            logger.exception("compute_initial_stop error: %s", exc)
            return None

    def compute_trailing_stop(self, position: dict) -> float | None:
        """ATR-based trailing stop. Only trails in profit direction (never widens)."""
        try:
            side = (position.get("side") or "long").lower()
            entry_price = float(position["entry_price"])
            current_price = float(position["current_price"])
            atr = float(position.get("atr_1min") or 0)
            prev_stop = position.get("trailing_stop")

            atr_dist = atr * self.trailing_atr_mult
            min_floor = entry_price * self.trailing_min_pct
            trail_dist = max(atr_dist, min_floor)

            if side == "long":
                computed = current_price - trail_dist
                if prev_stop is not None:
                    return max(float(prev_stop), computed)  # only trails UP
                return computed
            else:
                computed = current_price + trail_dist
                if prev_stop is not None:
                    return min(float(prev_stop), computed)  # only trails DOWN
                return computed
        except Exception as exc:
            logger.exception("compute_trailing_stop error: %s", exc)
            return None

    def should_partial_exit(self, position: dict) -> bool:
        """Has price reached 50% of measured move from entry?"""
        try:
            if position.get("partial_done"):
                return False
            side = (position.get("side") or "long").lower()
            entry_price = float(position["entry_price"])
            mm = float(position["measured_move"])
            current_price = float(position["current_price"])

            if side == "long":
                target = entry_price + (mm * self.partial_target_pct)
                return current_price >= target
            else:
                target = entry_price - (mm * self.partial_target_pct)
                return current_price <= target
        except Exception as exc:
            logger.exception("should_partial_exit error: %s", exc)
            return False

    def get_time_decay_action(self, position: dict) -> str | None:
        """Check time-based decay. Only fires if partial NOT done and target NOT hit."""
        try:
            if position.get("partial_done"):
                return None
            if self.should_partial_exit(position):
                return None

            current_time = position.get("current_time")
            if not current_time:
                return None
            ct = self._to_et_time(current_time)

            if ct >= self.time_tight:
                return "time_decay_tight"
            if ct >= self.time_breakeven:
                return "time_decay_be"
            return None
        except Exception as exc:
            logger.exception("get_time_decay_action error: %s", exc)
            return None
