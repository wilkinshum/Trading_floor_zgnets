"""Self-Learning Review System — Phase 3.

Exports the main components for adaptive signal weight tuning.
"""

from .self_learner import SelfLearner
from .adaptive_weights import AdaptiveWeights
from .signal_attribution import SignalAttribution
from .safety import SafetyManager
from .reporter import Reporter

__all__ = [
    "SelfLearner",
    "AdaptiveWeights",
    "SignalAttribution",
    "SafetyManager",
    "Reporter",
]
