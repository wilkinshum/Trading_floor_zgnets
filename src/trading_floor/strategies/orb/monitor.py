"""
ORB Monitor — Phase 7
Central state machine managing candidate lifecycles from post-range
through entry, position management, and exit (9:45–11:30 AM ET).
"""

import json
import os
import time
import logging
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, asdict, field
from typing import Optional, Dict, List, Tuple, Any
from enum import Enum

logger = logging.getLogger(__name__)


# ── States ───────────────────────────────────────────────────

class ORBState(Enum):
    WATCHING_FOR_CONSOLIDATION = "WATCHING_FOR_CONSOLIDATION"
    WATCHING_FOR_BREAKOUT = "WATCHING_FOR_BREAKOUT"
    WATCHING_FOR_RETEST = "WATCHING_FOR_RETEST"
    IN_POSITION = "IN_POSITION"
    CLOSED = "CLOSED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"
    DONE = "DONE"


# Terminal states — no more ticking
_TERMINAL = {ORBState.DONE.value, ORBState.SKIPPED.value}

# Poll intervals per state (seconds)
_POLL_INTERVALS = {
    ORBState.WATCHING_FOR_CONSOLIDATION.value: 30,
    ORBState.WATCHING_FOR_BREAKOUT.value: 15,
    ORBState.WATCHING_FOR_RETEST.value: 10,
    ORBState.IN_POSITION.value: 10,
}


# ── Candidate Dataclass ─────────────────────────────────────

@dataclass
class CandidateState:
    symbol: str
    direction: str              # "long" or "short"
    state: str                  # ORBState value
    orb_high: float
    orb_low: float
    measured_move: float
    sector: str
    wave: int = 1               # current wave (max from config)
    retries: int = 0            # retries this wave
    band_high: Optional[float] = None
    band_low: Optional[float] = None
    breakout_level: Optional[float] = None
    inside_band_count: int = 0  # consecutive bars back inside band
    entry_price: Optional[float] = None
    entry_qty: Optional[int] = None
    alpaca_order_id: Optional[str] = None
    pending_id: Optional[int] = None
    position_meta_id: Optional[int] = None
    partial_done: bool = False
    trail_stop: Optional[float] = None
    last_update: Optional[str] = None


# ── Detection Functions (standalone) ─────────────────────────

def is_consolidating(candles: list, orb_hi: float, orb_lo: float,
                     mm: float, direction: str, config: dict
                     ) -> Tuple[bool, Optional[float], Optional[float]]:
    """Detect post-ORB contraction. Returns (ok, band_high, band_low)."""
    cons = config.get("consolidation", {})
    min_candles = cons.get("min_candles", 3)

    if len(candles) < max(min_candles, 10):
        return False, None, None

    last = candles[-min_candles:]
    ranges = [c.high - c.low for c in last]

    # Contraction: at least 2 of (min_candles-1) pairs shrinking
    threshold = cons.get("range_contraction_threshold", 0.9)
    dec = sum(1 for i in range(1, len(ranges))
              if ranges[i] <= ranges[i - 1] * threshold)
    if dec < 2:
        return False, None, None

    # Band
    band_high = max(c.high for c in last)
    band_low = min(c.low for c in last)
    band_width = band_high - band_low
    price = last[-1].close

    max_bw = min(
        cons.get("band_max_pct_of_mm", 0.35) * mm,
        cons.get("band_max_pct_of_price", 0.006) * price,
    )
    if band_width >= max_bw:
        return False, None, None

    # Volume declining
    vols = [c.volume for c in candles]
    sma5 = sum(vols[-5:]) / min(len(vols), 5)
    sma10 = sum(vols[-10:]) / min(len(vols), 10)
    last_vol = vols[-1]
    prev_vol = vols[-2] if len(vols) >= 2 else last_vol

    if not (last_vol < sma5 and (last_vol < prev_vol or last_vol < 0.8 * sma10)):
        return False, None, None

    # Location
    loc_pct = cons.get("location_pct", 0.3)
    if direction == "long":
        if band_high < orb_hi - loc_pct * mm:
            return False, None, None
    else:
        if band_low > orb_lo + loc_pct * mm:
            return False, None, None

    return True, band_high, band_low


def is_breakout(candle, band_hi: float, band_lo: float,
                vol: float, vol_sma10: float,
                direction: str, config: dict) -> bool:
    """Breakout = candle CLOSE beyond consolidation band + volume confirm."""
    bk = config.get("breakout", {})
    ext = bk.get("min_extension_pct", 0.1)
    vol_mult = bk.get("min_vol_multiple", 1.2)
    band_width = band_hi - band_lo

    if direction == "long":
        return (candle.close > band_hi + ext * band_width
                and vol >= vol_mult * vol_sma10)
    else:
        return (candle.close < band_lo - ext * band_width
                and vol >= vol_mult * vol_sma10)


def is_retest(candle, breakout_level: float, mm: float,
              price: float, direction: str, config: dict) -> bool:
    """Retest = pullback to breakout level with strong PA."""
    rt = config.get("retest", {})
    prox = min(
        max(rt.get("proximity_pct_price", 0.005) * price,
            rt.get("proximity_pct_mm_min", 0.20) * mm),
        rt.get("proximity_pct_mm_max", 0.25) * mm,
    )

    body = abs(candle.close - candle.open)
    rng = candle.high - candle.low
    if rng == 0:
        return False
    strong_pa = body / rng >= rt.get("body_ratio_min", 0.60)

    if direction == "long":
        touched = (candle.low <= breakout_level + prox
                   and candle.low >= breakout_level - prox)
        held = candle.close >= breakout_level - prox
    else:
        touched = (candle.high >= breakout_level - prox
                   and candle.high <= breakout_level + prox)
        held = candle.close <= breakout_level + prox

    return touched and held and strong_pa


# ── ORB Monitor ──────────────────────────────────────────────

class ORBMonitor:
    """Central state machine for ORB trade lifecycle.

    Called by the orchestrator (Phase 9). Does NOT run its own thread.
    """

    def __init__(self, broker, executor, exit_manager,
                 portfolio_intel, floor_manager,
                 config: dict, db_path: str):
        """
        Args:
            broker: AlpacaBroker (get_bars, get_latest_quote)
            executor: ORBExecutor (Phase 6)
            exit_manager: ORBExitManager (Phase 5)
            portfolio_intel: PortfolioIntelligence (Phase 3)
            floor_manager: FloorPositionManager (Phase 2)
            config: dict with keys: entry, exit, risk, timing, execution
            db_path: SQLite path
        """
        self.broker = broker
        self.executor = executor
        self.exit_manager = exit_manager
        self.portfolio_intel = portfolio_intel
        self.floor_manager = floor_manager
        self.config = config
        self.db_path = db_path

        self.candidates: Dict[str, CandidateState] = {}
        self.state_file = os.path.join("web", "orb_state.json")
        self.daily_pnl = 0.0  # tracked in-memory

    # ── Candidate Loading ────────────────────────────────────

    def load_candidates(self, candidates_path: str, ranges_path: str) -> None:
        """Load candidates from Scanner + RangeMarker JSON outputs."""
        try:
            with open(candidates_path) as f:
                cands = json.load(f)
            with open(ranges_path) as f:
                ranges = json.load(f)

            for c in cands:
                sym = c["symbol"]
                r = ranges.get(sym, {})
                self.candidates[sym] = CandidateState(
                    symbol=sym,
                    direction=c.get("direction", "long"),
                    state=ORBState.WATCHING_FOR_CONSOLIDATION.value,
                    orb_high=r.get("high", c.get("orb_high", 0.0)),
                    orb_low=r.get("low", c.get("orb_low", 0.0)),
                    measured_move=r.get("measured_move", c.get("measured_move", 0.0)),
                    sector=c.get("sector", "Unknown"),
                    last_update=datetime.now(timezone.utc).isoformat(),
                )

            logger.info("Loaded %d candidates", len(self.candidates))
        except Exception as e:
            logger.error("Failed to load candidates: %s", e)
            raise

    # ── State Persistence ────────────────────────────────────

    def save_state(self) -> None:
        """Atomic write to web/orb_state.json."""
        try:
            data = {
                sym: asdict(cs) for sym, cs in self.candidates.items()
            }
            data["_meta"] = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "daily_pnl": self.daily_pnl,
            }

            tmp = self.state_file + ".tmp"
            os.makedirs(os.path.dirname(tmp) or ".", exist_ok=True)
            with open(tmp, "w") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp, self.state_file)
        except Exception as e:
            logger.error("save_state failed: %s", e)

    def load_state(self) -> bool:
        """Recover from orb_state.json. Returns True if recovered."""
        try:
            # Also check .tmp for crash-during-write recovery
            path = self.state_file
            tmp = path + ".tmp"
            if not os.path.exists(path):
                if os.path.exists(tmp):
                    path = tmp
                else:
                    return False

            with open(path) as f:
                data = json.load(f)

            meta = data.pop("_meta", {})
            self.daily_pnl = meta.get("daily_pnl", 0.0)

            self.candidates = {}
            for sym, d in data.items():
                self.candidates[sym] = CandidateState(**d)

            logger.info("Recovered %d candidates from state", len(self.candidates))
            return True
        except Exception as e:
            logger.error("load_state failed: %s", e)
            return False

    # ── Main Loop ────────────────────────────────────────────

    def run(self, candidates_path: str, ranges_path: str) -> Dict[str, Any]:
        """Main execution loop. Returns summary dict."""
        self.load_candidates(candidates_path, ranges_path)

        # Try crash recovery (overrides freshly loaded candidates)
        if os.path.exists(self.state_file):
            if self.load_state():
                logger.info("Resumed from crash recovery state")

        end_time_str = self.config.get("timing", {}).get("force_close_time", "11:30")
        trades_taken = 0

        while True:
            now = datetime.now(timezone.utc)

            # Check if past end time (caller should set this in ET)
            # For safety, also break if all done
            all_done = all(
                c.state in _TERMINAL for c in self.candidates.values()
            )
            if all_done:
                logger.info("All candidates terminal")
                break

            # Tick each active candidate
            for sym in list(self.candidates.keys()):
                cs = self.candidates[sym]
                if cs.state in _TERMINAL:
                    continue
                try:
                    self._tick(sym)
                except Exception as e:
                    logger.error("_tick(%s) crashed: %s", sym, e)

            self.save_state()

            interval = self._get_poll_interval()
            time.sleep(interval)

        self.save_state()
        summary = {
            "candidates": len(self.candidates),
            "done": sum(1 for c in self.candidates.values()
                        if c.state == ORBState.DONE.value),
            "skipped": sum(1 for c in self.candidates.values()
                           if c.state == ORBState.SKIPPED.value),
            "daily_pnl": self.daily_pnl,
        }
        logger.info("Monitor finished: %s", summary)
        return summary

    # ── Tick Dispatch ────────────────────────────────────────

    def _tick(self, symbol: str) -> None:
        """Dispatch to state-specific handler."""
        cs = self.candidates[symbol]
        state = cs.state

        if state == ORBState.WATCHING_FOR_CONSOLIDATION.value:
            self._tick_consolidation(symbol, cs)
        elif state == ORBState.WATCHING_FOR_BREAKOUT.value:
            self._tick_breakout(symbol, cs)
        elif state == ORBState.WATCHING_FOR_RETEST.value:
            self._tick_retest(symbol, cs)
        elif state == ORBState.IN_POSITION.value:
            self._tick_position(symbol, cs)
        elif state == ORBState.CLOSED.value:
            self._tick_closed(symbol, cs)
        elif state == ORBState.FAILED.value:
            self._tick_failed(symbol, cs)

    def _transition(self, cs: CandidateState, new_state: ORBState) -> None:
        """Log and apply state transition."""
        old = cs.state
        cs.state = new_state.value
        cs.last_update = datetime.now(timezone.utc).isoformat()
        logger.info("%s: %s → %s (wave %d, retry %d)",
                    cs.symbol, old, new_state.value, cs.wave, cs.retries)

    # ── State Handlers ───────────────────────────────────────

    def _tick_consolidation(self, sym: str, cs: CandidateState) -> None:
        bars = self._fetch_bars(sym, limit=20)
        if not bars:
            return

        ok, bh, bl = is_consolidating(
            bars, cs.orb_high, cs.orb_low, cs.measured_move,
            cs.direction, self.config.get("entry", {})
        )
        if ok:
            cs.band_high = bh
            cs.band_low = bl
            cs.inside_band_count = 0
            self._transition(cs, ORBState.WATCHING_FOR_BREAKOUT)

    def _tick_breakout(self, sym: str, cs: CandidateState) -> None:
        bars = self._fetch_bars(sym, limit=10)
        if not bars:
            return

        latest = bars[-1]
        vol_sma = sum(c.volume for c in bars) / len(bars) if bars else 1

        if is_breakout(latest, cs.band_high, cs.band_low,
                       latest.volume, vol_sma, cs.direction,
                       self.config.get("entry", {})):
            # Set breakout level at the band edge
            if cs.direction == "long":
                cs.breakout_level = cs.band_high
            else:
                cs.breakout_level = cs.band_low
            cs.inside_band_count = 0
            self._transition(cs, ORBState.WATCHING_FOR_RETEST)
        else:
            # Check if price returned inside band
            if cs.band_low <= latest.close <= cs.band_high:
                cs.inside_band_count += 1
            else:
                cs.inside_band_count = 0

            if cs.inside_band_count >= 2:
                self._transition(cs, ORBState.FAILED)

    def _tick_retest(self, sym: str, cs: CandidateState) -> None:
        bars = self._fetch_bars(sym, limit=5)
        if not bars:
            return

        latest = bars[-1]
        price = latest.close
        entry_cfg = self.config.get("entry", {})

        if is_retest(latest, cs.breakout_level, cs.measured_move,
                     price, cs.direction, entry_cfg):
            if self._validate_entry_checklist(sym, cs, latest):
                self._attempt_entry(sym, cs, price)
                return

        # Check wave timeout
        max_time = entry_cfg.get("max_wave1_time", "10:30") if cs.wave == 1 \
            else entry_cfg.get("max_wave23_time", "11:00")
        # Simple timeout: if stuck in retest for >5 min, fail
        if cs.last_update:
            try:
                lu = datetime.fromisoformat(cs.last_update)
                if (datetime.now(timezone.utc) - lu).total_seconds() > 300:
                    self._transition(cs, ORBState.FAILED)
            except Exception:
                pass

    def _attempt_entry(self, sym: str, cs: CandidateState, price: float) -> None:
        """Call executor to enter position."""
        try:
            # Calculate stop and target from exit config + measured move
            exit_cfg = self.config.get("exit", {})
            mm = cs.measured_move

            if cs.direction == "long":
                stop_price = round(price - 0.30 * mm, 2)
                tp_price = round(price + exit_cfg.get("partial_target_pct_of_mm", 0.5) * mm, 2)
            else:
                stop_price = round(price + 0.30 * mm, 2)
                tp_price = round(price - exit_cfg.get("partial_target_pct_of_mm", 0.5) * mm, 2)

            # Size: use execution config
            exec_cfg = self.config.get("execution", {})
            risk_per_trade = self.config.get("risk", {}).get("flash_crash_cap", 60)
            stop_dist = abs(price - stop_price)
            qty = max(1, int(risk_per_trade / stop_dist)) if stop_dist > 0 else 1

            result = self.executor.enter_position(
                symbol=sym,
                side="buy" if cs.direction == "long" else "sell",
                qty=qty,
                limit_price=price,
                stop_price=stop_price,
                tp_price=tp_price,
                sector=cs.sector,
            )

            if result.get("status") in ("submitted", "filled"):
                cs.entry_price = price
                cs.entry_qty = qty
                cs.alpaca_order_id = result.get("alpaca_order_id")
                cs.pending_id = result.get("pending_id")
                self._transition(cs, ORBState.IN_POSITION)
            else:
                logger.warning("%s entry rejected: %s", sym, result)
                self._transition(cs, ORBState.FAILED)

        except Exception as e:
            logger.error("%s entry failed: %s", sym, e)
            self._transition(cs, ORBState.FAILED)

    def _tick_position(self, sym: str, cs: CandidateState) -> None:
        """Manage open position — check exits via ExitManager."""
        try:
            bars = self._fetch_bars(sym, limit=20)
            if not bars:
                return

            latest = bars[-1]
            current_price = latest.close

            # Build position dict for ExitManager.check_exit()
            position = {
                "entry_price": cs.entry_price,
                "qty": cs.entry_qty,
                "direction": cs.direction,
                "partial_done": cs.partial_done,
                "trail_stop": cs.trail_stop,
            }

            # Build market dict
            market = {
                "current_price": current_price,
                "atr": self._calc_atr(bars),
                "timestamp": datetime.now(timezone.utc),
            }

            exit_signal = self.exit_manager.check_exit(position, market)

            if exit_signal is None:
                return

            action = exit_signal.get("action")

            if action == "partial_exit" and not cs.partial_done:
                partial_qty = int(cs.entry_qty * 0.5)
                result = self.executor.execute_partial_exit(
                    sym, partial_qty, round(current_price, 2)
                )
                if result.get("status") in ("filled", "market_fallback"):
                    cs.partial_done = True
                    cs.trail_stop = exit_signal.get("trail_stop")
                    logger.info("%s partial exit done: %s", sym, result["status"])

            elif action == "full_exit":
                order_type = exit_signal.get("order_type", "market")
                exit_price = exit_signal.get("price")
                result = self.executor.execute_exit(
                    sym, cs.entry_qty if not cs.partial_done
                    else int(cs.entry_qty * 0.5),
                    order_type=order_type,
                    price=exit_price,
                    position_meta_id=cs.position_meta_id,
                )
                if result.get("status") in ("submitted", "filled"):
                    # Track P&L
                    if cs.entry_price and current_price:
                        pnl = (current_price - cs.entry_price) * cs.entry_qty
                        if cs.direction == "short":
                            pnl = -pnl
                        self.daily_pnl += pnl
                    self._transition(cs, ORBState.CLOSED)

            elif action == "modify_stop" and exit_signal.get("new_stop"):
                new_stop = exit_signal["new_stop"]
                if cs.trail_stop is None or (
                    (cs.direction == "long" and new_stop > cs.trail_stop) or
                    (cs.direction == "short" and new_stop < cs.trail_stop)
                ):
                    cs.trail_stop = new_stop
                    # If we have a live stop order, modify it
                    # (Monitor tracks trail_stop; executor modifies order)

        except Exception as e:
            logger.error("%s position tick error: %s", sym, e)

    def _tick_closed(self, sym: str, cs: CandidateState) -> None:
        """Wave reset or done."""
        max_waves = self.config.get("risk", {}).get("max_waves_per_stock", 3)

        if cs.wave < max_waves:
            cs.wave += 1
            cs.retries = 0
            cs.band_high = None
            cs.band_low = None
            cs.breakout_level = None
            cs.inside_band_count = 0
            cs.entry_price = None
            cs.entry_qty = None
            cs.alpaca_order_id = None
            cs.pending_id = None
            cs.position_meta_id = None
            cs.partial_done = False
            cs.trail_stop = None
            self._transition(cs, ORBState.WATCHING_FOR_CONSOLIDATION)
        else:
            self._transition(cs, ORBState.DONE)

    def _tick_failed(self, sym: str, cs: CandidateState) -> None:
        """Retry or skip."""
        max_retries = self.config.get("entry", {}).get("max_retries", 1)

        if cs.retries < max_retries:
            cs.retries += 1
            cs.band_high = None
            cs.band_low = None
            cs.breakout_level = None
            cs.inside_band_count = 0
            self._transition(cs, ORBState.WATCHING_FOR_CONSOLIDATION)
        else:
            self._transition(cs, ORBState.SKIPPED)

    # ── Entry Checklist ──────────────────────────────────────

    def _validate_entry_checklist(self, sym: str,
                                  cs: CandidateState, candle) -> bool:
        """16-point entry checklist. Returns True if all pass."""
        entry_cfg = self.config.get("entry", {})
        risk_cfg = self.config.get("risk", {})

        try:
            # 1. Spread check
            try:
                quote = self.broker.get_latest_quote(sym)
                if quote and quote.ask_price and quote.bid_price:
                    spread_pct = (quote.ask_price - quote.bid_price) / quote.ask_price
                    if spread_pct > entry_cfg.get("spread_max_pct", 0.003):
                        logger.info("%s checklist FAIL: spread %.4f%%", sym, spread_pct * 100)
                        return False
            except Exception:
                pass  # If quote fails, skip spread check

            # 2. Data freshness
            if hasattr(candle, "timestamp") and candle.timestamp:
                try:
                    ts = candle.timestamp if isinstance(candle.timestamp, datetime) \
                        else datetime.fromisoformat(str(candle.timestamp))
                    age = (datetime.now(timezone.utc) - ts).total_seconds()
                    if age > entry_cfg.get("data_freshness_sec", 15):
                        logger.info("%s checklist FAIL: data age %.0fs", sym, age)
                        return False
                except Exception:
                    pass

            # 3. Floor position check
            try:
                allowed, reason = self.floor_manager.can_open_position(
                    "orb", sym, cs.sector
                )
                if not allowed:
                    # Release the reservation we just made (floor_manager reserves on check)
                    pid = getattr(self.floor_manager, "last_pending_id", None)
                    if pid:
                        self.floor_manager.release_slot(pid)
                    logger.info("%s checklist FAIL: floor %s", sym, reason)
                    return False
                # Release — we'll re-reserve in executor.enter_position
                pid = getattr(self.floor_manager, "last_pending_id", None)
                if pid:
                    self.floor_manager.release_slot(pid)
            except Exception as e:
                logger.warning("%s floor check error: %s", sym, e)

            # 4. Portfolio intelligence
            try:
                result = self.portfolio_intel.pre_entry_check(
                    sym, cs.direction, cs.sector
                )
                if result and result.get("hard_block"):
                    logger.info("%s checklist FAIL: portfolio intel", sym)
                    return False
            except Exception:
                pass

            # 5. Daily loss cap
            daily_cap = risk_cfg.get("flash_crash_cap", 120)
            if abs(self.daily_pnl) >= daily_cap:
                logger.info("%s checklist FAIL: daily loss cap (%.2f)", sym, self.daily_pnl)
                return False

            # All passed
            return True

        except Exception as e:
            logger.error("%s checklist error: %s", sym, e)
            return False

    # ── Helpers ───────────────────────────────────────────────

    def _get_poll_interval(self) -> float:
        """Shortest interval needed for active candidates."""
        intervals = []
        for cs in self.candidates.values():
            if cs.state not in _TERMINAL:
                intervals.append(_POLL_INTERVALS.get(cs.state, 30))

        return min(intervals) if intervals else 30.0

    def _fetch_bars(self, symbol: str, limit: int = 20) -> list:
        """Fetch 1-min bars from broker."""
        try:
            return self.broker.get_bars(symbol, "1Min", limit=limit)
        except Exception as e:
            logger.error("fetch_bars(%s) failed: %s", symbol, e)
            return []

    def _calc_atr(self, bars: list, period: int = 14) -> float:
        """Calculate ATR from bars."""
        if len(bars) < 2:
            return 0.0
        trs = []
        for i in range(1, len(bars)):
            hi = bars[i].high
            lo = bars[i].low
            pc = bars[i - 1].close
            tr = max(hi - lo, abs(hi - pc), abs(lo - pc))
            trs.append(tr)
        n = min(period, len(trs))
        return sum(trs[-n:]) / n if n > 0 else 0.0
