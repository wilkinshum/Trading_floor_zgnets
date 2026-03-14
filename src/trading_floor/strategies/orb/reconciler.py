"""
ORB Reconciler — Alpaca vs DB position reconciliation
Phase: 8 | ORB Trading Desk

Runs at 11:35 AM ET after ORB session ends.
Compares Alpaca broker positions against local SQLite DB.
Alerts on any mismatch. Cleans up stale state.

Design:
- No background thread — called by orchestrator (Phase 9)
- All broker calls wrapped in try/except
- Writes structured report to web/orb_reconciliation.json
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class MismatchType:
    GHOST = "ghost"          # In Alpaca, not in DB
    PHANTOM = "phantom"      # In DB, not in Alpaca
    QTY = "qty_mismatch"     # Both exist, qty differs
    SIDE = "side_mismatch"   # Both exist, side differs


class ORBReconciler:
    """Compares Alpaca positions to local DB and reports mismatches."""

    def __init__(
        self,
        broker,
        db_path: str,
        floor_manager=None,
        report_dir: str = "web",
    ):
        self.broker = broker
        self.db_path = db_path
        self.floor_manager = floor_manager
        self.report_dir = Path(report_dir)
        self.report_dir.mkdir(parents=True, exist_ok=True)

    # ── Main entry point ─────────────────────────────────────

    def reconcile(self, strategy: str = "orb") -> Dict[str, Any]:
        """Run full reconciliation. Returns report dict."""
        report = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "strategy": strategy,
            "mismatches": [],
            "stale_pendings_cleaned": 0,
            "orphaned_orders": [],
            "stale_budgets_released": 0,
            "status": "ok",
        }

        # 1. Position reconciliation
        try:
            mismatches = self._reconcile_positions(strategy)
            report["mismatches"] = mismatches
            if any(m["severity"] == "critical" for m in mismatches):
                report["status"] = "critical"
            elif mismatches:
                report["status"] = "warning"
        except Exception as e:
            logger.error("Position reconciliation failed: %s", e)
            report["mismatches"] = [{"type": "error", "detail": str(e), "severity": "critical"}]
            report["status"] = "error"

        # 2. Stale pending cleanup
        try:
            report["stale_pendings_cleaned"] = self._cleanup_stale_pendings()
        except Exception as e:
            logger.error("Stale pending cleanup failed: %s", e)

        # 3. Orphaned orders check
        try:
            report["orphaned_orders"] = self._check_orphaned_orders(strategy)
        except Exception as e:
            logger.error("Orphaned order check failed: %s", e)

        # 4. Stale budget reservations
        try:
            report["stale_budgets_released"] = self._release_stale_budgets(strategy)
        except Exception as e:
            logger.error("Stale budget release failed: %s", e)

        # Write report
        self._write_report(report)
        return report

    # ── Position reconciliation ──────────────────────────────

    def _reconcile_positions(self, strategy: str) -> List[Dict[str, Any]]:
        """Compare Alpaca positions vs DB open positions."""
        mismatches = []

        # Get Alpaca positions
        try:
            alpaca_positions = self.broker.get_positions()
        except Exception as e:
            raise RuntimeError(f"Broker get_positions failed: {e}") from e

        # Build lookup: symbol → {qty, side}
        alpaca_map: Dict[str, Dict] = {}
        for pos in alpaca_positions:
            sym = pos.symbol if hasattr(pos, "symbol") else pos.get("symbol", "")
            qty = float(pos.qty if hasattr(pos, "qty") else pos.get("qty", 0))
            side = pos.side if hasattr(pos, "side") else pos.get("side", "long")
            # Alpaca: positive qty = long, negative or side=short = short
            if isinstance(side, str):
                side = side.lower()
            else:
                side = "long" if qty > 0 else "short"
            alpaca_map[sym] = {"qty": abs(qty), "side": side, "matched": False}

        # Get DB positions
        db_positions = self._get_db_open_positions(strategy)
        db_map: Dict[str, Dict] = {}
        for row in db_positions:
            sym = row["symbol"]
            db_map[sym] = {
                "id": row["id"],
                "qty": float(row["entry_qty"]),
                "side": row["side"],
                "entry_price": row["entry_price"],
            }

        # Compare: DB positions vs Alpaca
        for sym, db_pos in db_map.items():
            if sym not in alpaca_map:
                # PHANTOM: DB says open, Alpaca doesn't have it
                mismatches.append({
                    "type": MismatchType.PHANTOM,
                    "symbol": sym,
                    "detail": f"DB position_meta id={db_pos['id']} open, but not in Alpaca",
                    "severity": "critical",
                    "db_qty": db_pos["qty"],
                    "db_side": db_pos["side"],
                    "auto_action": "mark_closed",
                })
                # Auto-fix: mark DB position as closed
                self._mark_db_closed(db_pos["id"], "reconciler_phantom")
            else:
                alp = alpaca_map[sym]
                alp["matched"] = True
                # Check qty
                if abs(alp["qty"] - db_pos["qty"]) > 0.01:
                    mismatches.append({
                        "type": MismatchType.QTY,
                        "symbol": sym,
                        "detail": f"Alpaca qty={alp['qty']}, DB qty={db_pos['qty']}",
                        "severity": "warning",
                        "alpaca_qty": alp["qty"],
                        "db_qty": db_pos["qty"],
                    })
                # Check side
                if alp["side"] != db_pos["side"]:
                    mismatches.append({
                        "type": MismatchType.SIDE,
                        "symbol": sym,
                        "detail": f"Alpaca side={alp['side']}, DB side={db_pos['side']}",
                        "severity": "critical",
                        "alpaca_side": alp["side"],
                        "db_side": db_pos["side"],
                    })

        # Check: Alpaca positions not matched to any DB record (for this strategy)
        # Note: only flag ORB-tagged positions or untagged ones
        for sym, alp in alpaca_map.items():
            if not alp["matched"] and sym not in db_map:
                # Could be a swing position — only flag if we can confirm it's ORB
                # For now, log as informational ghost
                mismatches.append({
                    "type": MismatchType.GHOST,
                    "symbol": sym,
                    "detail": f"In Alpaca (qty={alp['qty']}, side={alp['side']}) but no open DB record for strategy='{strategy}'",
                    "severity": "info",
                    "alpaca_qty": alp["qty"],
                    "alpaca_side": alp["side"],
                })

        return mismatches

    # ── DB helpers ───────────────────────────────────────────

    def _get_db_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.row_factory = sqlite3.Row
        return conn

    def _get_db_open_positions(self, strategy: str) -> List[Dict]:
        conn = self._get_db_connection()
        try:
            rows = conn.execute(
                "SELECT id, symbol, side, entry_qty, entry_price "
                "FROM position_meta WHERE strategy=? AND status='open'",
                (strategy,)
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def _mark_db_closed(self, position_id: int, reason: str):
        """Mark a phantom DB position as closed."""
        conn = self._get_db_connection()
        try:
            conn.execute(
                "UPDATE position_meta SET status='closed', exit_reason=?, "
                "exit_time=? WHERE id=?",
                (reason, datetime.now(timezone.utc).isoformat(), position_id)
            )
            conn.commit()
            logger.info("Marked position_meta id=%d as closed (reason=%s)",
                        position_id, reason)
        finally:
            conn.close()

    # ── Stale pending cleanup ────────────────────────────────

    def _cleanup_stale_pendings(self) -> int:
        """Delegate to floor manager if available."""
        if self.floor_manager is None:
            return 0
        try:
            cleaned = self.floor_manager.cleanup_stale_pendings()
            if cleaned:
                logger.info("Cleaned %d stale pending slots", cleaned)
            return cleaned or 0
        except Exception as e:
            logger.error("cleanup_stale_pendings failed: %s", e)
            return 0

    # ── Orphaned orders ──────────────────────────────────────

    def _check_orphaned_orders(self, strategy: str) -> List[Dict]:
        """Find orders in DB with no matching position_meta."""
        orphans = []
        conn = self._get_db_connection()
        try:
            rows = conn.execute(
                "SELECT o.id, o.alpaca_order_id, o.symbol, o.status, o.position_meta_id "
                "FROM orders o "
                "WHERE o.strategy=? AND o.status NOT IN ('filled', 'cancelled', 'expired') "
                "AND (o.position_meta_id IS NULL OR o.position_meta_id NOT IN "
                "  (SELECT id FROM position_meta WHERE strategy=? AND status='open'))",
                (strategy, strategy)
            ).fetchall()
            for r in rows:
                orphans.append({
                    "order_id": r["id"],
                    "alpaca_order_id": r["alpaca_order_id"],
                    "symbol": r["symbol"],
                    "status": r["status"],
                })
        finally:
            conn.close()
        if orphans:
            logger.warning("Found %d orphaned orders", len(orphans))
        return orphans

    # ── Stale budget reservations ────────────────────────────

    def _release_stale_budgets(self, strategy: str) -> int:
        """Release budget reservations that are still 'reserved' after session end."""
        conn = self._get_db_connection()
        try:
            cursor = conn.execute(
                "UPDATE budget_reservations SET status='released', "
                "released_at=? WHERE strategy=? AND status='reserved'",
                (datetime.now(timezone.utc).isoformat(), strategy)
            )
            count = cursor.rowcount
            conn.commit()
            if count:
                logger.info("Released %d stale budget reservations", count)
            return count
        except Exception:
            # Table may not exist yet
            return 0
        finally:
            conn.close()

    # ── Report ───────────────────────────────────────────────

    def _write_report(self, report: Dict):
        """Atomic write to web/orb_reconciliation.json."""
        out = self.report_dir / "orb_reconciliation.json"
        tmp = out.with_suffix(".json.tmp")
        try:
            tmp.write_text(json.dumps(report, indent=2, default=str))
            os.replace(str(tmp), str(out))
            logger.info("Reconciliation report written to %s", out)
        except Exception as e:
            logger.error("Failed to write reconciliation report: %s", e)

    # ── Summary for alerts ───────────────────────────────────

    def format_alert(self, report: Dict) -> Optional[str]:
        """Format a WhatsApp-friendly alert if there are issues. Returns None if clean."""
        mismatches = report.get("mismatches", [])
        orphans = report.get("orphaned_orders", [])
        stale = report.get("stale_pendings_cleaned", 0)
        budgets = report.get("stale_budgets_released", 0)

        if not mismatches and not orphans and not stale and not budgets:
            return None

        lines = ["⚠️ ORB Reconciliation Alert"]
        for m in mismatches:
            icon = "🔴" if m.get("severity") == "critical" else "🟡"
            lines.append(f"{icon} {m['type'].upper()}: {m.get('symbol', '?')} — {m['detail']}")
        if orphans:
            lines.append(f"📋 {len(orphans)} orphaned order(s)")
        if stale:
            lines.append(f"🧹 Cleaned {stale} stale pending(s)")
        if budgets:
            lines.append(f"💰 Released {budgets} stale budget reservation(s)")
        return "\n".join(lines)
