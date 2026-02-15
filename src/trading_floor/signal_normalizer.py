from __future__ import annotations

from collections import deque
import math


class SignalNormalizer:
    """
    Normalizes raw signal scores to [-1, +1] using a rolling z-score.
    Maintains a history buffer per signal component.
    """

    def __init__(self, lookback: int = 100):
        self.lookback = lookback
        self._history: dict[str, deque] = {}  # key = "symbol:component"

    def normalize(self, symbol: str, component: str, raw_score: float) -> float:
        """
        Add raw_score to history and return z-score clamped to [-1, +1].
        Falls back to tanh scaling if insufficient history.
        """
        key = f"{component}"  # normalize across all symbols per component
        if key not in self._history:
            self._history[key] = deque(maxlen=self.lookback)

        self._history[key].append(raw_score)
        buf = self._history[key]

        if len(buf) < 10:
            # Not enough history — use tanh scaling (maps any range to -1..+1)
            # Scale factor: multiply raw by 100 so typical 0.005 becomes 0.5 → tanh ≈ 0.46
            return math.tanh(raw_score * 100)

        mean = sum(buf) / len(buf)
        std = (sum((x - mean) ** 2 for x in buf) / len(buf)) ** 0.5

        if std < 1e-10:
            return math.tanh(raw_score * 100)

        z = (raw_score - mean) / std
        # Clamp to [-1, +1]
        return max(-1.0, min(1.0, z / 3.0))  # divide by 3 so ±3σ maps to ±1
