"""Kalman Filter for adaptive price trend estimation."""
from __future__ import annotations

import math
import numpy as np


class KalmanFilter:
    """1D Kalman Filter with 2D state: [price_level, trend/velocity].

    Replaces static moving averages with an adaptive filter that:
    - Estimates true price level and trend (velocity)
    - Adapts to volatility automatically
    - Provides uncertainty bounds (dynamic Bollinger Bands)
    """

    def __init__(self, process_variance: float = 1e-5, measurement_variance: float = 1e-3):
        self.process_variance = process_variance
        self.measurement_variance = measurement_variance
        self._initialized = False
        self.reset()

    def reset(self):
        """Reset filter state."""
        # State vector: [level, trend]
        self.x = np.zeros(2)
        # State covariance
        self.P = np.eye(2)
        # Transition matrix: level_{t} = level_{t-1} + trend_{t-1}, trend stays
        self.F = np.array([[1.0, 1.0],
                           [0.0, 1.0]])
        # Measurement matrix: we observe level only
        self.H = np.array([[1.0, 0.0]])
        # Process noise
        self.Q = np.array([[self.process_variance, 0.0],
                           [0.0, self.process_variance * 0.1]])
        # Measurement noise
        self.R = np.array([[self.measurement_variance]])
        self._initialized = False
        self._n_updates = 0

    def update(self, measurement: float) -> dict:
        """Process new price observation.

        Returns dict with level, trend, upper, lower, uncertainty, signal.
        """
        if measurement is None or (isinstance(measurement, float) and math.isnan(measurement)):
            # Return last state if available
            if self._initialized:
                unc = math.sqrt(max(self.P[0, 0], 1e-12))
                return {
                    "level": float(self.x[0]),
                    "trend": float(self.x[1]),
                    "upper": float(self.x[0] + 2.0 * unc),
                    "lower": float(self.x[0] - 2.0 * unc),
                    "uncertainty": unc,
                    "signal": 0.0,
                }
            return {"level": 0.0, "trend": 0.0, "upper": 0.0, "lower": 0.0,
                    "uncertainty": 0.0, "signal": 0.0}

        z = float(measurement)

        if not self._initialized:
            self.x = np.array([z, 0.0])
            self.P = np.array([[self.measurement_variance, 0.0],
                               [0.0, self.measurement_variance]])
            self._initialized = True
            self._n_updates = 1
            return {
                "level": z,
                "trend": 0.0,
                "upper": z,
                "lower": z,
                "uncertainty": math.sqrt(self.measurement_variance),
                "signal": 0.0,
            }

        # --- Predict ---
        x_pred = self.F @ self.x
        P_pred = self.F @ self.P @ self.F.T + self.Q

        # --- Update ---
        z_vec = np.array([z])
        y = z_vec - self.H @ x_pred                    # innovation
        S = self.H @ P_pred @ self.H.T + self.R        # innovation covariance
        S_inv = 1.0 / S[0, 0]
        K = (P_pred @ self.H.T) * S_inv                # Kalman gain (2x1)

        self.x = x_pred + K.flatten() * y[0]
        self.P = (np.eye(2) - K @ self.H) @ P_pred

        # Adaptive process noise: scale Q by recent innovation magnitude
        innovation_var = y[0] ** 2
        alpha = 0.05  # learning rate for adaptive Q
        adaptive_scale = max(1.0, innovation_var * S_inv)
        self.Q = self.Q * (1 - alpha) + alpha * adaptive_scale * np.array(
            [[self.process_variance, 0.0],
             [0.0, self.process_variance * 0.1]])

        self._n_updates += 1

        unc = math.sqrt(max(self.P[0, 0], 1e-12))
        level = float(self.x[0])
        trend = float(self.x[1])

        signal = (z - level) / unc if unc > 1e-12 else 0.0

        return {
            "level": level,
            "trend": trend,
            "upper": level + 2.0 * unc,
            "lower": level - 2.0 * unc,
            "uncertainty": unc,
            "signal": float(signal),
        }
