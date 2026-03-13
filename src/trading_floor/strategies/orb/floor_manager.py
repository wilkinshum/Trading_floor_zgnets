"""
Floor Position Manager — Cross-desk position limit enforcer
Phase: 2 | ORB Trading Desk

Enforces: max 2 ORB + max 3 Swing = 5 total, max 1 per GICS sector per desk.
Uses file-based mutex (msvcrt) + SQLite WAL for process-safe slot reservation.

Architect review notes (incorporated):
- Lock scope kept minimal (DB ops only)
- BEGIN IMMEDIATE for transaction serialization
- Stale pending cleanup on every reserve attempt
- Unknown sector policy: ALLOW with warning (not block)
"""
from __future__ import annotations

import logging
import os
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Tuple

import msvcrt
import yaml

logger = logging.getLogger(__name__)


class FloorPositionManager:
    """Cross-desk position limit enforcer with mutex."""

    def __init__(self, db_path: str, lock_file: str = "data/.floor_lock"):
        self.db_path = Path(db_path)
        self.lock_file = Path(lock_file)
        self.lock_file.parent.mkdir(parents=True, exist_ok=True)

        # Defaults
        self.max_orb_positions = 2
        self.max_swing_positions = 3
        self.max_total_positions = 5
        self.max_per_sector = 1

        # Try loading from config
        cfg_path = Path("configs/orb_config.yaml")
        try:
            if cfg_path.exists():
                with open(cfg_path, "r", encoding="utf-8") as f:
                    cfg = yaml.safe_load(f) or {}
                risk = cfg.get("orb", {}).get("risk", {})
                self.max_orb_positions = int(risk.get("max_positions", 2))
                self.max_total_positions = int(risk.get("max_total_positions", 5))
                self.max_per_sector = int(risk.get("max_per_sector", 1))
                self.max_swing_positions = self.max_total_positions - self.max_orb_positions
        except Exception as exc:
            logger.warning("Failed to load ORB config, using defaults: %s", exc)

        self.last_pending_id: int | None = None

    def can_open_position(self, strategy: str, symbol: str, sector: str = "") -> tuple[bool, str]:
        """Check and reserve a position slot. Returns (allowed, reason).

        Atomically: cleanup stale → check limits → reserve pending row.
        """
        if not symbol:
            return False, "invalid_symbol"

        strategy_db, desk = self._normalize_strategy(strategy)

        if not sector:
            logger.warning("No sector for %s — sector limit not enforced", symbol)

        with self._file_lock():
            conn = self._connect()
            try:
                # BEGIN IMMEDIATE for write serialization (architect rec #1)
                conn.execute("BEGIN IMMEDIATE")

                # Always cleanup stale pendings first (architect rec #3)
                self._cleanup_stale_pendings(conn, max_age_minutes=5)

                counts = self._get_counts(conn)
                orb_count = counts.get("intraday", 0)
                swing_count = counts.get("swing", 0)
                total_count = orb_count + swing_count

                # Check desk cap
                if desk == "orb" and orb_count >= self.max_orb_positions:
                    conn.rollback()
                    return False, "orb_limit_reached"
                if desk == "swing" and swing_count >= self.max_swing_positions:
                    conn.rollback()
                    return False, "swing_limit_reached"

                # Check total cap
                if total_count >= self.max_total_positions:
                    conn.rollback()
                    return False, "total_limit_reached"

                # Check sector cap (skip if no sector — architect rec #5: allow with warning)
                if sector:
                    sector_count = self._get_sector_count(conn, strategy_db, sector)
                    if sector_count >= self.max_per_sector:
                        conn.rollback()
                        return False, "sector_limit_reached"

                # Reserve slot
                now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
                cur = conn.execute(
                    """INSERT INTO position_meta
                       (symbol, strategy, side, status, sector, created_at)
                       VALUES (?, ?, 'buy', 'pending', ?, ?)""",
                    (symbol, strategy_db, sector or None, now),
                )
                pending_id = cur.lastrowid
                conn.commit()

                self.last_pending_id = pending_id
                logger.info("Reserved slot: %s %s %s pending_id=%d", desk, symbol, sector, pending_id)
                return True, f"reserved pending_id={pending_id}"
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    def confirm_position(self, pending_id: int, symbol: str,
                         entry_price: float, qty: int) -> bool:
        """Convert pending -> open after fill confirmation."""
        if pending_id is None:
            return False
        with self._file_lock():
            conn = self._connect()
            try:
                now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
                cur = conn.execute(
                    """UPDATE position_meta
                       SET status='open', symbol=?, entry_price=?, entry_qty=?, entry_time=?
                       WHERE id=? AND status='pending'""",
                    (symbol, entry_price, qty, now, pending_id),
                )
                conn.commit()
                if cur.rowcount == 1:
                    logger.info("Confirmed position pending_id=%d -> open", pending_id)
                    return True
                logger.warning("confirm_position: pending_id=%d not found or not pending", pending_id)
                return False
            finally:
                conn.close()

    def release_slot(self, pending_id: int) -> None:
        """Cancel a pending reservation (order rejected/timeout)."""
        if pending_id is None:
            return
        with self._file_lock():
            conn = self._connect()
            try:
                conn.execute(
                    "DELETE FROM position_meta WHERE id=? AND status='pending'",
                    (pending_id,),
                )
                conn.commit()
                logger.info("Released pending slot id=%d", pending_id)
            finally:
                conn.close()

    def get_floor_status(self) -> dict:
        """Return current position counts by strategy and sector."""
        conn = self._connect()
        try:
            rows = conn.execute(
                """SELECT strategy, status, COUNT(*) FROM position_meta
                   WHERE status IN ('open','pending') GROUP BY strategy, status"""
            ).fetchall()
            status_counts = {}
            for strat, status, cnt in rows:
                status_counts[(strat, status)] = cnt

            orb_sectors = self._get_sector_counts(conn, "intraday")
            swing_sectors = self._get_sector_counts(conn, "swing")

            return {
                "limits": {
                    "orb": self.max_orb_positions,
                    "swing": self.max_swing_positions,
                    "total": self.max_total_positions,
                    "per_sector": self.max_per_sector,
                },
                "orb": {
                    "open": status_counts.get(("intraday", "open"), 0),
                    "pending": status_counts.get(("intraday", "pending"), 0),
                    "by_sector": orb_sectors,
                },
                "swing": {
                    "open": status_counts.get(("swing", "open"), 0),
                    "pending": status_counts.get(("swing", "pending"), 0),
                    "by_sector": swing_sectors,
                },
                "total": {
                    "open": status_counts.get(("intraday", "open"), 0) + status_counts.get(("swing", "open"), 0),
                    "pending": status_counts.get(("intraday", "pending"), 0) + status_counts.get(("swing", "pending"), 0),
                },
            }
        finally:
            conn.close()

    def cleanup_stale_pendings(self, max_age_minutes: int = 5) -> int:
        """Public cleanup. Returns count removed."""
        with self._file_lock():
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                count = self._cleanup_stale_pendings(conn, max_age_minutes)
                conn.commit()
                return count
            finally:
                conn.close()

    # ── Internal ─────────────────────────────────────────────────

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _normalize_strategy(self, strategy: str) -> tuple[str, str]:
        """Returns (db_value, desk_name)."""
        s = (strategy or "").lower().strip()
        if s in ("orb", "intraday"):
            return "intraday", "orb"
        if s == "swing":
            return "swing", "swing"
        raise ValueError(f"Unsupported strategy: {strategy!r}")

    def _get_counts(self, conn: sqlite3.Connection) -> dict[str, int]:
        rows = conn.execute(
            """SELECT strategy, COUNT(*) FROM position_meta
               WHERE status IN ('open','pending') GROUP BY strategy"""
        ).fetchall()
        return {r[0]: r[1] for r in rows}

    def _get_sector_count(self, conn: sqlite3.Connection, strategy: str, sector: str) -> int:
        row = conn.execute(
            """SELECT COUNT(*) FROM position_meta
               WHERE strategy=? AND sector=? AND status IN ('open','pending')""",
            (strategy, sector),
        ).fetchone()
        return row[0] if row else 0

    def _get_sector_counts(self, conn: sqlite3.Connection, strategy: str) -> dict[str, int]:
        rows = conn.execute(
            """SELECT sector, COUNT(*) FROM position_meta
               WHERE strategy=? AND status IN ('open','pending') GROUP BY sector""",
            (strategy,),
        ).fetchall()
        return {r[0]: r[1] for r in rows if r[0]}

    def _cleanup_stale_pendings(self, conn: sqlite3.Connection, max_age_minutes: int) -> int:
        cutoff = (datetime.utcnow() - timedelta(minutes=max_age_minutes)).strftime("%Y-%m-%d %H:%M:%S")
        cur = conn.execute(
            "DELETE FROM position_meta WHERE status='pending' AND created_at < ?",
            (cutoff,),
        )
        if cur.rowcount:
            logger.info("Cleaned %d stale pending slots", cur.rowcount)
        return cur.rowcount

    @contextmanager
    def _file_lock(self):
        """Non-blocking file lock with retry. Scope: minimal (DB ops only)."""
        self.lock_file.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.lock_file, "a+b")
        try:
            # Ensure file has content to lock
            fh.seek(0, 2)
            if fh.tell() == 0:
                fh.write(b"0")
                fh.flush()
            fh.seek(0)

            for attempt in range(5):
                try:
                    msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if attempt == 4:
                        raise TimeoutError("Floor lock timeout after 5 attempts")
                    time.sleep(0.5)
            yield
        finally:
            try:
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            except Exception:
                pass
            fh.close()
