#!/usr/bin/env python3
"""ORB Orchestrator — single long-running script for the Opening Range Breakout strategy.

Runs 9:00–11:35 AM ET on market days. Internally times each phase:
  9:00  Health check
  9:25  Pre-market scan
  9:30  Dead zone (wait)
  9:45  Range snapshot
  9:45–11:30  Monitor loop
  11:30 Force close (monitor handles)
  11:35 Reconciliation
"""

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import yaml

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'src'))

from trading_floor.strategies.orb.scanner import ORBScanner
from trading_floor.strategies.orb.range_marker import ORBRangeMarker
from trading_floor.strategies.orb.monitor import ORBMonitor
from trading_floor.strategies.orb.reconciler import ORBReconciler
from trading_floor.strategies.orb.executor import ORBExecutor
from trading_floor.strategies.orb.exit_manager import ORBExitManager
from trading_floor.strategies.orb.portfolio_intel import PortfolioIntelligence
from trading_floor.strategies.orb.floor_manager import FloorPositionManager
from trading_floor.broker.alpaca_broker import AlpacaBroker

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
ET = timezone(timedelta(hours=-4))  # EDT


def _et_now() -> datetime:
    return datetime.now(timezone.utc).astimezone(ET)


def _et_today_at(hour: int, minute: int = 0) -> datetime:
    return _et_now().replace(hour=hour, minute=minute, second=0, microsecond=0)


class ORBOrchestrator:
    def __init__(self, config_path: str = "configs/orb_config.yaml",
                 db_path: str = "data/trading.db", dry_run: bool = True):
        # Load config
        with open(config_path, "r") as f:
            self.config = yaml.safe_load(f)
        self.db_path = db_path
        self.dry_run = dry_run
        self.config["dry_run"] = dry_run

        # Logging
        today_str = _et_now().strftime("%Y-%m-%d")
        log_dir = Path("logs")
        log_dir.mkdir(exist_ok=True)
        log_file = log_dir / f"orb_orchestrator_{today_str}.log"

        self.logger = logging.getLogger("orb_orchestrator")
        self.logger.setLevel(logging.DEBUG)
        fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
        fh = logging.FileHandler(log_file)
        fh.setFormatter(fmt)
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        self.logger.addHandler(fh)
        self.logger.addHandler(sh)

        # Broker
        api_key = self.config.get("alpaca_api_key", os.environ.get("ALPACA_API_KEY", ""))
        api_secret = self.config.get("alpaca_api_secret", os.environ.get("ALPACA_API_SECRET", ""))
        paper = self.config.get("paper", True)
        self.broker = AlpacaBroker(api_key, api_secret, paper=paper)

        # Components — using actual constructor signatures
        self.floor_manager = FloorPositionManager(db_path)
        self.portfolio_intel = PortfolioIntelligence(
            db_path,
            orb_capital=self.config.get("orb_capital", 3000),
            swing_capital=self.config.get("swing_capital", 2000),
        )
        self.exit_manager = ORBExitManager(self.config, broker=self.broker)
        self.executor = ORBExecutor(
            self.broker, self.broker, self.floor_manager,
            self.config, db_path
        )
        self.scanner = ORBScanner(self.config, data_client=self.broker)
        self.range_marker = ORBRangeMarker(self.config, data_client=self.broker)
        self.monitor = ORBMonitor(
            self.broker, self.executor, self.exit_manager,
            self.portfolio_intel, self.floor_manager,
            self.config, db_path
        )
        self.reconciler = ORBReconciler(
            self.broker, db_path, floor_manager=self.floor_manager, report_dir="web"
        )

        self.phase = "init"
        Path("web").mkdir(exist_ok=True)

        self.logger.info("ORBOrchestrator initialized (dry_run=%s)", dry_run)

    # ------------------------------------------------------------------
    # Heartbeat
    # ------------------------------------------------------------------
    def _write_heartbeat(self, status: str = "ok", extra: dict | None = None):
        hb = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "phase": self.phase,
            "status": status,
            "dry_run": self.dry_run,
        }
        if extra:
            hb.update(extra)
        self._atomic_json("web/orb_heartbeat.json", hb)

    def _atomic_json(self, path: str, data):
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2, default=str)
        os.replace(tmp, path)

    # ------------------------------------------------------------------
    # Wait helper
    # ------------------------------------------------------------------
    def _wait_until(self, target: datetime):
        """Sleep until target time, writing heartbeat every 30s."""
        while _et_now() < target:
            remaining = (target - _et_now()).total_seconds()
            self._write_heartbeat("waiting", {"wait_until": target.strftime("%H:%M"), "remaining_s": int(remaining)})
            time.sleep(min(30, max(1, remaining)))

    # ------------------------------------------------------------------
    # Phases
    # ------------------------------------------------------------------
    def health_check(self) -> bool:
        self.phase = "health_check"
        self._write_heartbeat()
        self.logger.info("=== HEALTH CHECK ===")

        # Broker connectivity
        try:
            account = self.broker.get_account()
            self.logger.info("Broker connected — account %s", account.get("id", "?") if isinstance(account, dict) else getattr(account, "id", "?"))
        except Exception as e:
            self.logger.error("Broker connectivity FAILED: %s", e)
            self._write_heartbeat("error", {"reason": str(e)})
            return False

        # Buying power
        min_bp = self.config.get("min_buying_power", 1000)
        bp_raw = account.get("buying_power", 0) if isinstance(account, dict) else getattr(account, "buying_power", 0)
        bp = float(bp_raw)
        if bp < min_bp:
            self.logger.error("Buying power $%.2f < minimum $%.2f", bp, min_bp)
            self._write_heartbeat("error", {"reason": "insufficient_buying_power", "buying_power": bp})
            return False
        self.logger.info("Buying power: $%.2f", bp)

        # Regime
        regime_path = self.config.get("regime_path", "configs/regime_state.json")
        try:
            with open(regime_path) as f:
                regime = json.load(f)
            regime_label = regime.get("regime", "UNKNOWN")
            self.logger.info("Regime: %s", regime_label)
            if regime_label == "CRASH":
                self.logger.warning("CRASH regime — aborting")
                self._write_heartbeat("abort", {"reason": "crash_regime"})
                return False
        except FileNotFoundError:
            self.logger.warning("No regime file at %s — proceeding with caution", regime_path)
        except Exception as e:
            self.logger.warning("Error reading regime: %s — proceeding", e)

        self._write_heartbeat("ok")
        return True

    def scan(self) -> list:
        self.phase = "scan"
        self._write_heartbeat()
        self.logger.info("=== PRE-MARKET SCAN ===")

        try:
            candidates = self.scanner.scan()
        except Exception as e:
            self.logger.error("Scanner failed: %s", e)
            self._write_heartbeat("error", {"reason": str(e)})
            raise

        # Cap at top 8
        candidates = candidates[:8]
        self.logger.info("Scan returned %d candidates: %s",
                         len(candidates), [c.get("symbol", c.get("sym", "?")) for c in candidates])

        self._atomic_json("web/orb_candidates.json", {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "candidates": candidates
        })
        self._write_heartbeat("ok", {"candidate_count": len(candidates)})
        return candidates

    def mark_ranges(self, candidates: list) -> list:
        self.phase = "mark_ranges"
        self._write_heartbeat()
        self.logger.info("=== RANGE SNAPSHOT ===")

        try:
            enriched = self.range_marker.mark_ranges(candidates)
        except Exception as e:
            self.logger.error("RangeMarker failed: %s", e)
            self._write_heartbeat("error", {"reason": str(e)})
            raise

        self.logger.info("Post-range candidates: %d", len(enriched))
        self._atomic_json("web/orb_ranges.json", {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "candidates": enriched
        })
        self._write_heartbeat("ok", {"range_count": len(enriched)})
        return enriched

    def run_monitor(self, candidates: list):
        self.phase = "monitor"
        self._write_heartbeat()
        self.logger.info("=== MONITOR PHASE (%d candidates) ===", len(candidates))

        # Write candidates and ranges to temp JSON for monitor.run()
        candidates_path = "web/orb_candidates.json"
        ranges_path = "web/orb_ranges.json"

        try:
            self.monitor.run(candidates_path, ranges_path)
        except Exception as e:
            self.logger.error("Monitor crashed: %s", e, exc_info=True)
            self._write_heartbeat("error", {"reason": str(e)})
        finally:
            try:
                self.monitor.save_state()
            except Exception:
                self.logger.warning("Failed to save monitor state", exc_info=True)

        self.logger.info("Monitor phase complete")
        self._write_heartbeat("ok")

    def reconcile(self):
        self.phase = "reconcile"
        self._write_heartbeat()
        self.logger.info("=== RECONCILIATION ===")

        try:
            report = self.reconciler.reconcile(strategy="orb")
            alert = self.reconciler.format_alert(report)
            if alert:
                self.logger.warning("RECONCILIATION ALERT:\n%s", alert)
            else:
                self.logger.info("Reconciliation clean — no mismatches")

            self._atomic_json("web/orb_daily_summary.json", {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "report": report,
                "alert": alert,
            })
        except Exception as e:
            self.logger.error("Reconciliation failed: %s", e, exc_info=True)
            self._write_heartbeat("error", {"reason": str(e)})
            return

        self._write_heartbeat("ok")

    # ------------------------------------------------------------------
    # Main run
    # ------------------------------------------------------------------
    def run(self, skip_health: bool = False, scan_only: bool = False):
        self.logger.info("========================================")
        self.logger.info("ORB Orchestrator starting (dry_run=%s)", self.dry_run)
        self.logger.info("========================================")

        # Weekend check
        now = _et_now()
        if now.weekday() >= 5:
            self.logger.info("Weekend (day=%d) — nothing to do", now.weekday())
            self._write_heartbeat("skip", {"reason": "weekend"})
            return 0

        # Health check
        if not skip_health:
            self._wait_until(_et_today_at(9, 0))
            if not self.health_check():
                self.logger.error("Health check failed — exiting")
                return 1
        else:
            self.logger.info("Skipping health check (--skip-health)")

        # Scan at 9:25
        self._wait_until(_et_today_at(9, 25))
        try:
            candidates = self.scan()
        except Exception:
            return 1
        if not candidates:
            self.logger.info("No candidates — clean exit")
            self._write_heartbeat("done", {"reason": "no_candidates"})
            return 0

        if scan_only:
            self.logger.info("--scan-only: exiting after scan")
            self._write_heartbeat("done", {"reason": "scan_only"})
            return 0

        # Dead zone 9:30–9:45
        self.phase = "dead_zone"
        self.logger.info("=== DEAD ZONE (9:30-9:45) ===")
        self._wait_until(_et_today_at(9, 45))

        # Range snapshot at 9:45
        try:
            candidates = self.mark_ranges(candidates)
        except Exception:
            return 1
        if not candidates:
            self.logger.info("No candidates after range marking — clean exit")
            self._write_heartbeat("done", {"reason": "no_ranges"})
            return 0

        # Monitor 9:45–11:30
        self.run_monitor(candidates)

        # Reconciliation at ~11:35
        self._wait_until(_et_today_at(11, 35))
        self.reconcile()

        self.phase = "done"
        self._write_heartbeat("done")
        self.logger.info("ORB Orchestrator complete")
        return 0


def main():
    parser = argparse.ArgumentParser(description="ORB Opening Range Breakout Orchestrator")
    parser.add_argument("--dry-run", action="store_true", default=True, help="Log only, no real orders (default)")
    parser.add_argument("--live", dest="dry_run", action="store_false", help="Submit real orders (paper account)")
    parser.add_argument("--scan-only", action="store_true", help="Run scan then exit")
    parser.add_argument("--skip-health", action="store_true", help="Skip health check")
    parser.add_argument("--config", default="configs/orb_config.yaml", help="Config path")
    parser.add_argument("--db", default="data/trading.db", help="Database path")
    args = parser.parse_args()

    orch = ORBOrchestrator(config_path=args.config, db_path=args.db, dry_run=args.dry_run)
    code = orch.run(skip_health=args.skip_health, scan_only=args.scan_only)
    sys.exit(code)


if __name__ == "__main__":
    main()
