"""Abstract base class for trading strategies."""
from __future__ import annotations

import abc
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")


@dataclass
class Signal:
    """A scored trade signal produced by a strategy scan."""
    symbol: str
    side: str  # 'buy' or 'sell'
    score: float
    scores_breakdown: Dict[str, float] = field(default_factory=dict)
    timestamp: str = ""
    strategy_name: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)


class BaseStrategy(abc.ABC):
    """Abstract strategy interface.

    Concrete strategies must implement scan, execute, and manage_exits.
    """

    @abc.abstractmethod
    def scan(self, market_data: Any = None) -> List[Signal]:
        """Scan the market and return scored signals."""

    @abc.abstractmethod
    def execute(self, signals: List[Signal]) -> List[Dict[str, Any]]:
        """Submit orders for approved signals. Returns list of order results."""

    @abc.abstractmethod
    def manage_exits(self) -> List[Dict[str, Any]]:
        """Check open positions and manage exits. Returns list of actions taken."""

    # ── Shared utilities ─────────────────────────────────────

    @staticmethod
    def score_signals(weights: Dict[str, float], raw_scores: Dict[str, float]) -> float:
        """Compute weighted score from raw component scores.

        Zero/None components are excluded and remaining weights renormalized.
        """
        total_weight = 0.0
        weighted_sum = 0.0
        for key, w in weights.items():
            val = raw_scores.get(key)
            if val is None or w <= 0:
                continue
            total_weight += w
            weighted_sum += val * w
        if total_weight <= 0:
            return 0.0
        return weighted_sum / total_weight

    @staticmethod
    def filter_universe(full_universe: List[str], exclusions: List[str]) -> List[str]:
        """Remove excluded symbols from the universe."""
        excl_set = set(exclusions)
        return [s for s in full_universe if s not in excl_set]

    @staticmethod
    def is_in_time_window(start: str, end: str, now: Optional[datetime] = None) -> bool:
        """Check if current ET time is within start-end window (HH:MM strings)."""
        if now is None:
            now = datetime.now(ET)
        elif now.tzinfo is None:
            now = now.replace(tzinfo=ET)
        sh, sm = map(int, start.split(":"))
        eh, em = map(int, end.split(":"))
        start_dt = now.replace(hour=sh, minute=sm, second=0, microsecond=0)
        end_dt = now.replace(hour=eh, minute=em, second=0, microsecond=0)
        return start_dt <= now <= end_dt
