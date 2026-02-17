"""Shadow Runner: runs Kalman + HMM alongside existing signals without affecting trades."""
from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

import numpy as np

from trading_floor.kalman import KalmanFilter
from trading_floor.hmm import HMMRegimeDetector


class ShadowRunner:
    """Runs advanced models in shadow mode alongside existing signals.

    Logs predictions to DB for later comparison but does NOT influence trades.
    """

    def __init__(self, db_path: str, config: dict):
        self.db_path = Path(db_path)
        self.config = config or {}

        kalman_cfg = self.config.get("kalman", {})
        self.kalman_filters: dict[str, KalmanFilter] = {}
        self._kalman_pv = float(kalman_cfg.get("process_variance", 1e-5))
        self._kalman_mv = float(kalman_cfg.get("measurement_variance", 1e-3))

        hmm_cfg = self.config.get("hmm", {})
        self.hmm = HMMRegimeDetector(
            n_states=hmm_cfg.get("n_states", 3),
            lookback=hmm_cfg.get("lookback", 60),
        )
        self._refit_interval = hmm_cfg.get("refit_interval", 5)
        self._run_count = 0

    def _get_kalman(self, symbol: str) -> KalmanFilter:
        if symbol not in self.kalman_filters:
            self.kalman_filters[symbol] = KalmanFilter(
                process_variance=self._kalman_pv,
                measurement_variance=self._kalman_mv,
            )
        return self.kalman_filters[symbol]

    def run(self, price_data: dict, spy_data=None, vix_data=None,
            existing_signals: dict | None = None,
            existing_regime: dict | None = None) -> dict:
        """Run shadow analysis on current market data.

        Parameters
        ----------
        price_data : dict[str, pd.Series or list]
            Symbol -> price series (closing prices).
        spy_data : array-like
            SPY closing prices for regime detection.
        vix_data : array-like
            VIX values.
        existing_signals : dict[str, float]
            Symbol -> existing composite signal score for comparison.
        existing_regime : dict
            Current regime from detect_regime().

        Returns summary dict for logging.
        """
        self._run_count += 1
        existing_signals = existing_signals or {}
        existing_regime = existing_regime or {}

        timestamp = datetime.now().isoformat()
        kalman_results = {}
        records = []

        # --- Kalman filter per symbol ---
        for sym, prices in price_data.items():
            try:
                price_arr = np.asarray(prices, dtype=float)
                price_arr = price_arr[~np.isnan(price_arr)]
            except (TypeError, ValueError):
                continue

            if len(price_arr) == 0:
                continue

            kf = self._get_kalman(sym)
            # Feed all prices (in case filter just initialized)
            result = None
            for p in price_arr:
                result = kf.update(float(p))

            if result is None:
                continue

            kalman_results[sym] = result

            records.append({
                "timestamp": timestamp,
                "symbol": sym,
                "kalman_signal": result["signal"],
                "kalman_level": result["level"],
                "kalman_trend": result["trend"],
                "kalman_uncertainty": result["uncertainty"],
                "existing_signal": existing_signals.get(sym, 0.0),
                "hmm_state": None,
                "hmm_bull_prob": None,
                "hmm_bear_prob": None,
                "hmm_transition_prob": None,
                "hmm_transition_risk": None,
                "existing_regime": existing_regime.get("label", ""),
            })

        # --- HMM regime ---
        hmm_result = None
        if spy_data is not None:
            try:
                spy_arr = np.asarray(spy_data, dtype=float)
                spy_arr = spy_arr[~np.isnan(spy_arr)]
            except (TypeError, ValueError):
                spy_arr = np.array([])

            if len(spy_arr) >= 5:
                obs = self.hmm._discretize(spy_arr, vix_data)

                # Refit periodically
                if self._run_count % self._refit_interval == 0 and len(obs) >= 10:
                    try:
                        self.hmm.fit(obs)
                    except Exception:
                        pass

                hmm_result = self.hmm.predict(observations=obs)

                # Update records with HMM data
                for rec in records:
                    rec["hmm_state"] = hmm_result["state_label"]
                    rec["hmm_bull_prob"] = hmm_result["probabilities"][0]
                    rec["hmm_bear_prob"] = hmm_result["probabilities"][1]
                    rec["hmm_transition_prob"] = hmm_result["probabilities"][2]
                    rec["hmm_transition_risk"] = hmm_result["transition_risk"]

        # --- Log to DB ---
        self._log_records(records)

        # --- Build summary ---
        agree_count = 0
        total = 0
        for sym, kr in kalman_results.items():
            es = existing_signals.get(sym, 0.0)
            if es != 0.0:
                total += 1
                # Agree if same sign
                if (kr["signal"] > 0 and es > 0) or (kr["signal"] < 0 and es < 0):
                    agree_count += 1

        summary = {
            "kalman_symbols": len(kalman_results),
            "kalman_agree": agree_count,
            "kalman_total_compared": total,
            "hmm": hmm_result,
            "existing_regime": existing_regime,
        }

        return summary

    def _log_records(self, records: list[dict]):
        """Insert shadow prediction records into the DB."""
        if not records:
            return
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            for r in records:
                cursor.execute("""
                    INSERT INTO shadow_predictions (
                        timestamp, symbol,
                        kalman_signal, kalman_level, kalman_trend, kalman_uncertainty,
                        existing_signal,
                        hmm_state, hmm_bull_prob, hmm_bear_prob, hmm_transition_prob, hmm_transition_risk,
                        existing_regime
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    r["timestamp"], r["symbol"],
                    r["kalman_signal"], r["kalman_level"], r["kalman_trend"], r["kalman_uncertainty"],
                    r["existing_signal"],
                    r["hmm_state"], r["hmm_bull_prob"], r["hmm_bear_prob"],
                    r["hmm_transition_prob"], r["hmm_transition_risk"],
                    r["existing_regime"],
                ))
            conn.commit()
            conn.close()
        except Exception as e:
            print(f"[ShadowRunner] DB write error: {e}")

    def evaluate(self, date_str: str | None = None) -> dict:
        """Evaluate shadow predictions vs actual outcomes.

        Returns accuracy metrics comparing Kalman vs existing signals.
        """
        if date_str is None:
            date_str = datetime.now().strftime("%Y-%m-%d")

        try:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """SELECT * FROM shadow_predictions
                   WHERE timestamp LIKE ? AND outcome_filled = 1""",
                (f"{date_str}%",)
            ).fetchall()
            conn.close()
        except Exception:
            return {"error": "could not query shadow_predictions", "samples": 0}

        if not rows:
            return {"samples": 0, "message": "No evaluated predictions yet"}

        kalman_correct = 0
        existing_correct = 0
        total_signal = 0

        hmm_bull_correct = 0
        hmm_bear_correct = 0
        regime_total = 0

        for r in rows:
            ret_1d = r["actual_return_1d"]
            if ret_1d is None:
                continue

            # Kalman vs existing signal accuracy
            ks = r["kalman_signal"]
            es = r["existing_signal"]
            if ks is not None and es is not None and ret_1d != 0:
                total_signal += 1
                if (ks > 0 and ret_1d > 0) or (ks < 0 and ret_1d < 0):
                    kalman_correct += 1
                if (es > 0 and ret_1d > 0) or (es < 0 and ret_1d < 0):
                    existing_correct += 1

            # HMM regime accuracy
            hmm_state = r["hmm_state"]
            if hmm_state and ret_1d != 0:
                regime_total += 1
                if hmm_state == "bull" and ret_1d > 0:
                    hmm_bull_correct += 1
                elif hmm_state == "bear" and ret_1d < 0:
                    hmm_bear_correct += 1

        result = {
            "samples": len(rows),
            "signal_comparisons": total_signal,
            "kalman_accuracy": kalman_correct / max(1, total_signal),
            "existing_accuracy": existing_correct / max(1, total_signal),
            "hmm_regime_samples": regime_total,
            "hmm_correct": hmm_bull_correct + hmm_bear_correct,
        }

        # Recommendation
        if total_signal < 20:
            result["recommendation"] = "Need more data"
        elif result["kalman_accuracy"] > result["existing_accuracy"] + 0.05:
            result["recommendation"] = "Ready to switch"
        elif result["existing_accuracy"] > result["kalman_accuracy"] + 0.05:
            result["recommendation"] = "Existing system is better"
        else:
            result["recommendation"] = "Need more data"

        return result
