"""Persistent per-agent memory with safety guardrails."""
from __future__ import annotations

import logging
import math
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_DEFAULT_CONFIG = {
    "enabled": True,
    "rolling_window": 50,
    "max_age_days": 90,
    "min_samples": 10,
    "max_adjustment": 0.20,
    "underperform_threshold": 0.10,
    "decay_halflife_days": 14,
    "regime_matching": True,
}


class AgentMemory:
    """Persistent per-agent memory with safety guardrails."""

    def __init__(self, agent_name: str, db_path: str, config: dict | None = None):
        self.agent_name = agent_name
        self.db_path = Path(db_path)
        self.cfg = {**_DEFAULT_CONFIG, **(config or {})}
        self._disabled = False
        self._ensure_table()

    # ------------------------------------------------------------------
    # DB helpers
    # ------------------------------------------------------------------
    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_table(self):
        conn = self._conn()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS agent_memory (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_name TEXT NOT NULL,
                symbol TEXT,
                signal_type TEXT,
                signal_value REAL,
                outcome TEXT,
                pnl REAL DEFAULT 0,
                regime_spy TEXT,
                regime_vix TEXT,
                regime_label TEXT,
                confidence REAL,
                memory_influenced BOOLEAN DEFAULT 0,
                timestamp TEXT NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_agent_memory_agent ON agent_memory(agent_name);
            CREATE INDEX IF NOT EXISTS idx_agent_memory_regime ON agent_memory(regime_label);
            CREATE INDEX IF NOT EXISTS idx_agent_memory_timestamp ON agent_memory(timestamp);
        """)
        conn.close()

    # ------------------------------------------------------------------
    # Core API
    # ------------------------------------------------------------------
    def record(self, observation: dict, regime: dict):
        """Store an observation tagged with current market regime and prune."""
        conn = self._conn()
        conn.execute(
            """INSERT INTO agent_memory
               (agent_name, symbol, signal_type, signal_value, outcome, pnl,
                regime_spy, regime_vix, regime_label, confidence,
                memory_influenced, timestamp)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                self.agent_name,
                observation.get("symbol"),
                observation.get("signal"),
                observation.get("signal_value"),
                observation.get("outcome", "pending"),
                observation.get("pnl", 0.0),
                regime.get("spy_trend"),
                regime.get("vix_level"),
                regime.get("label"),
                observation.get("confidence"),
                1 if observation.get("memory_influenced") else 0,
                observation.get("timestamp", datetime.utcnow().isoformat()),
            ),
        )
        conn.commit()
        conn.close()
        self.prune()

    def recall(
        self,
        symbol: str | None = None,
        regime: dict | None = None,
        limit: int | None = None,
    ) -> list[dict]:
        """Retrieve recent observations with exponential-decay weights."""
        limit = limit or self.cfg["rolling_window"]
        clauses = ["agent_name = ?"]
        params: list[Any] = [self.agent_name]

        if symbol:
            clauses.append("symbol = ?")
            params.append(symbol)
        if regime and self.cfg["regime_matching"]:
            if regime.get("label"):
                clauses.append("regime_label = ?")
                params.append(regime["label"])

        sql = (
            f"SELECT * FROM agent_memory WHERE {' AND '.join(clauses)} "
            f"ORDER BY timestamp DESC LIMIT ?"
        )
        params.append(limit)

        conn = self._conn()
        rows = conn.execute(sql, params).fetchall()
        conn.close()

        now = datetime.utcnow()
        halflife = self.cfg["decay_halflife_days"]
        results = []
        for r in rows:
            d = dict(r)
            try:
                ts = datetime.fromisoformat(d["timestamp"])
                age_days = max((now - ts).total_seconds() / 86400, 0)
            except Exception:
                age_days = 0
            d["decay_weight"] = 2 ** (-age_days / halflife)
            results.append(d)
        return results

    def get_signal_accuracy(
        self, signal_type: str | None = None, regime: dict | None = None
    ) -> dict | None:
        """Win rate and avg PnL. Returns None if sample_size < min_samples."""
        clauses = ["agent_name = ?", "outcome IN ('win','loss')"]
        params: list[Any] = [self.agent_name]

        if signal_type:
            clauses.append("signal_type = ?")
            params.append(signal_type)
        if regime and self.cfg["regime_matching"]:
            if regime.get("label"):
                clauses.append("regime_label = ?")
                params.append(regime["label"])

        conn = self._conn()
        rows = conn.execute(
            f"SELECT * FROM agent_memory WHERE {' AND '.join(clauses)} "
            "ORDER BY timestamp DESC",
            params,
        ).fetchall()
        conn.close()

        if len(rows) < self.cfg["min_samples"]:
            return None

        now = datetime.utcnow()
        halflife = self.cfg["decay_halflife_days"]
        total_w = 0.0
        win_w = 0.0
        pnl_weighted = 0.0

        for r in rows:
            try:
                ts = datetime.fromisoformat(r["timestamp"])
                age = max((now - ts).total_seconds() / 86400, 0)
            except Exception:
                age = 0
            w = 2 ** (-age / halflife)
            total_w += w
            if r["outcome"] == "win":
                win_w += w
            pnl_weighted += (r["pnl"] or 0) * w

        win_rate = win_w / total_w if total_w else 0
        avg_pnl = pnl_weighted / total_w if total_w else 0

        return {
            "win_rate": round(win_rate, 4),
            "avg_pnl": round(avg_pnl, 4),
            "sample_size": len(rows),
        }

    def suggest_weight_adjustment(self, current_weight: float) -> dict | None:
        """
        Suggest weight adjustment with guardrails.

        Returns dict with 'new_weight' and optionally 'action'='disable',
        or None if insufficient data.
        """
        if self._disabled:
            return {"new_weight": current_weight, "action": "disabled"}

        accuracy = self.get_signal_accuracy()
        if accuracy is None:
            return None

        # Compare memory-influenced vs default trades
        conn = self._conn()
        mem_rows = conn.execute(
            "SELECT pnl FROM agent_memory WHERE agent_name=? AND memory_influenced=1 AND outcome IN ('win','loss')",
            (self.agent_name,),
        ).fetchall()
        def_rows = conn.execute(
            "SELECT pnl FROM agent_memory WHERE agent_name=? AND memory_influenced=0 AND outcome IN ('win','loss')",
            (self.agent_name,),
        ).fetchall()
        conn.close()

        # Auto-disable check
        if len(mem_rows) >= self.cfg["min_samples"] and len(def_rows) >= self.cfg["min_samples"]:
            mem_avg = sum(r["pnl"] or 0 for r in mem_rows) / len(mem_rows)
            def_avg = sum(r["pnl"] or 0 for r in def_rows) / len(def_rows)
            if def_avg > 0 and (def_avg - mem_avg) / abs(def_avg) > self.cfg["underperform_threshold"]:
                self._disabled = True
                logger.warning(
                    "AgentMemory auto-disabled for %s: memory avg PnL %.4f vs default %.4f",
                    self.agent_name, mem_avg, def_avg,
                )
                return {"new_weight": current_weight, "action": "disable"}

        # Calculate adjustment based on win rate deviation from 0.5
        win_rate = accuracy["win_rate"]
        adjustment = (win_rate - 0.5) * 2  # maps [0,1] -> [-1,1]
        max_adj = self.cfg["max_adjustment"]
        adjustment = max(-max_adj, min(max_adj, adjustment))

        new_weight = current_weight * (1 + adjustment)
        new_weight = max(0.01, new_weight)  # floor

        return {
            "new_weight": round(new_weight, 4),
            "adjustment": round(adjustment, 4),
            "win_rate": accuracy["win_rate"],
            "sample_size": accuracy["sample_size"],
        }

    def get_stats(self) -> dict:
        """Return memory stats for auditing."""
        conn = self._conn()
        total = conn.execute(
            "SELECT COUNT(*) FROM agent_memory WHERE agent_name=?",
            (self.agent_name,),
        ).fetchone()[0]

        regime_dist = conn.execute(
            "SELECT regime_label, COUNT(*) as cnt FROM agent_memory "
            "WHERE agent_name=? GROUP BY regime_label",
            (self.agent_name,),
        ).fetchall()

        outcomes = conn.execute(
            "SELECT outcome, COUNT(*) as cnt, AVG(pnl) as avg_pnl FROM agent_memory "
            "WHERE agent_name=? GROUP BY outcome",
            (self.agent_name,),
        ).fetchall()

        mem_influenced = conn.execute(
            "SELECT COUNT(*) FROM agent_memory WHERE agent_name=? AND memory_influenced=1",
            (self.agent_name,),
        ).fetchone()[0]

        conn.close()

        return {
            "agent": self.agent_name,
            "total_observations": total,
            "memory_influenced_count": mem_influenced,
            "disabled": self._disabled,
            "regime_distribution": {r["regime_label"]: r["cnt"] for r in regime_dist},
            "outcome_stats": {
                r["outcome"]: {"count": r["cnt"], "avg_pnl": round(r["avg_pnl"] or 0, 4)}
                for r in outcomes
            },
        }

    def prune(self):
        """Remove old observations and enforce rolling window."""
        conn = self._conn()
        # Age-based prune
        cutoff = (datetime.utcnow() - timedelta(days=self.cfg["max_age_days"])).isoformat()
        conn.execute(
            "DELETE FROM agent_memory WHERE agent_name=? AND timestamp < ?",
            (self.agent_name, cutoff),
        )

        # Rolling window prune: keep only the most recent N
        window = self.cfg["rolling_window"]
        conn.execute(
            """DELETE FROM agent_memory WHERE agent_name=? AND id NOT IN (
                SELECT id FROM agent_memory WHERE agent_name=?
                ORDER BY timestamp DESC LIMIT ?
            )""",
            (self.agent_name, self.agent_name, window),
        )
        conn.commit()
        conn.close()
