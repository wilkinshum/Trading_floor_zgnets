"""
Pre-execution filters — final gate checks right before trade execution.

These run AFTER signal generation and PM plan creation, but BEFORE portfolio.execute().
Any filter can block a trade.

Filters:
1. Regime re-check (HMM) — block if regime flipped since signal generation
2. Volume confirmation — block if volume below 20-period average
3. Time-of-day filter — require stronger signals in first hour (9:30-10:30 ET)
4. Crypto/sector correlation — check BTC direction for crypto-adjacent stocks
5. Kalman agreement — block if Kalman disagrees with signal direction
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

from trading_floor.hmm import HMMRegimeDetector
from trading_floor.sector_map import get_sector

REGIME_STATE_FILE = Path(__file__).resolve().parent.parent.parent / "configs" / "regime_state.json"

logger = logging.getLogger(__name__)

# Default config values
DEFAULTS = {
    "volume_lookback": 20,
    "volume_min_ratio": 1.0,  # must be >= 1.0x average volume
    "morning_cutoff_hour": 10,
    "morning_cutoff_minute": 30,
    "morning_min_score": 0.6,  # abs(score) must exceed this before 10:30
    "morning_require_kalman": True,
    "crypto_momentum_periods": 10,
    "crypto_symbols": [
        "IREN", "HUT", "MARA", "RIOT", "CORZ", "BITF", "MSTR", "COIN",
    ],
    "crypto_sectors": ["Crypto/AI Infra"],
    "kalman_agreement_required": True,
    "min_price": 5.0,
    "last_entry_minutes": 30,
    "crypto_momentum_threshold": 0.003,
}


def _get_cfg(cfg: dict, key: str):
    return cfg.get("pre_execution", {}).get(key, DEFAULTS[key])


# ---------------------------------------------------------------------------
# 1. Regime re-check
# ---------------------------------------------------------------------------

def _load_regime_state() -> dict | None:
    """Load the latest regime state from the 5-min monitor."""
    try:
        if REGIME_STATE_FILE.exists():
            with open(REGIME_STATE_FILE) as f:
                return json.load(f)
    except (json.JSONDecodeError, OSError):
        pass
    return None


def check_regime_recheck(
    hmm: HMMRegimeDetector,
    spy_data,
    side: str,
    original_regime_label: str | None = None,
) -> tuple[bool, str]:
    """Re-run HMM right before execution. Also consult the 5-min regime monitor."""
    # First check the shared regime state file (more recent, 5-min interval)
    regime_state = _load_regime_state()
    if regime_state:
        rs_hmm = regime_state.get("hmm")
        regime_change = regime_state.get("regime_change")

        # Block if regime just changed (within the last monitor cycle)
        if regime_change:
            return False, (
                f"Regime monitor: regime changed {regime_change['from']}→{regime_change['to']} "
                f"at {regime_change['at']}. Trade blocked for safety."
            )

        # Use monitor's latest reading for direction check
        if rs_hmm:
            mon_label = rs_hmm["state_label"]
            mon_conf = rs_hmm["confidence"]

            if side == "BUY" and mon_label == "bear" and mon_conf > 0.7:
                return False, f"BUY blocked: regime monitor says bear ({mon_conf:.0%})"
            if side == "SELL" and mon_label == "bull" and mon_conf > 0.7:
                return False, f"SELL blocked: regime monitor says bull ({mon_conf:.0%})"

            # Check for rapid bear probability increase in history
            history = regime_state.get("history", [])
            if len(history) >= 3:
                recent_bears = [h["bear_prob"] for h in history[-3:]]
                if recent_bears[-1] - recent_bears[0] > 0.20:
                    return False, (
                        f"Regime monitor: bear probability spiking "
                        f"({recent_bears[0]:.0%}→{recent_bears[-1]:.0%} in last 3 readings). "
                        f"Trade blocked."
                    )

    # Fallback: also do the live HMM check
    if spy_data is None:
        if regime_state and regime_state.get("hmm"):
            return True, f"regime OK (from monitor): {regime_state['hmm']['state_label']}"
        return True, "no SPY data for regime recheck"

    try:
        spy_arr = np.asarray(spy_data, dtype=float)
        spy_arr = spy_arr[~np.isnan(spy_arr)]
        if len(spy_arr) < 5:
            return True, "insufficient SPY data"

        obs = hmm._discretize(spy_arr)
        result = hmm.predict(observations=obs)
        current_label = result["state_label"]

        # If we had an original regime and it changed, block
        if original_regime_label and original_regime_label != current_label:
            return False, (
                f"Regime flipped: was '{original_regime_label}', now '{current_label}' "
                f"(confidence {result['confidence']:.0%}). Trade blocked."
            )

        # Also block buys in bear and shorts in bull with high confidence
        if side == "BUY" and current_label == "bear" and result["confidence"] > 0.7:
            return False, f"BUY blocked: HMM says bear regime ({result['confidence']:.0%})"
        if side == "SELL" and current_label == "bull" and result["confidence"] > 0.7:
            return False, f"SELL blocked: HMM says bull regime ({result['confidence']:.0%})"

        return True, f"regime OK: {current_label} ({result['confidence']:.0%})"
    except Exception as e:
        logger.warning("Regime recheck failed: %s", e)
        return True, f"regime recheck error: {e}"


# ---------------------------------------------------------------------------
# 2. Volume confirmation
# ---------------------------------------------------------------------------

def check_volume(
    df,  # pd.DataFrame with 'volume' column
    cfg: dict,
) -> tuple[bool, str]:
    """Block if current volume is below the N-period average."""
    lookback = _get_cfg(cfg, "volume_lookback")
    min_ratio = _get_cfg(cfg, "volume_min_ratio")

    if df is None or df.empty:
        return True, "no volume data"

    col = None
    for c in df.columns:
        if "volume" in str(c).lower():
            col = c
            break
    if col is None:
        return True, "no volume column"

    vol_series = df[col].dropna()
    if len(vol_series) < lookback + 1:
        return True, f"insufficient volume data ({len(vol_series)} < {lookback + 1})"

    avg_vol = vol_series.iloc[-(lookback + 1):-1].mean()
    current_vol = vol_series.iloc[-1]

    if avg_vol <= 0:
        return True, "zero average volume"

    ratio = current_vol / avg_vol
    if ratio < min_ratio:
        return False, (
            f"Volume too low: {current_vol:,.0f} vs {lookback}-period avg {avg_vol:,.0f} "
            f"(ratio {ratio:.2f} < {min_ratio:.2f})"
        )

    return True, f"volume OK: ratio {ratio:.2f}"


# ---------------------------------------------------------------------------
# 3. Time-of-day filter
# ---------------------------------------------------------------------------

def check_time_of_day(
    score: float,
    cfg: dict,
    kalman_agrees: bool | None = None,
) -> tuple[bool, str]:
    """Require stronger signals during the first trading hour (9:30-10:30 ET)."""
    tz = ZoneInfo("America/New_York")
    now = datetime.now(tz)
    cutoff_h = _get_cfg(cfg, "morning_cutoff_hour")
    cutoff_m = _get_cfg(cfg, "morning_cutoff_minute")
    min_score = _get_cfg(cfg, "morning_min_score")
    require_kalman = _get_cfg(cfg, "morning_require_kalman")

    market_open = now.replace(hour=9, minute=30, second=0, microsecond=0)
    cutoff = now.replace(hour=cutoff_h, minute=cutoff_m, second=0, microsecond=0)

    if not (market_open <= now <= cutoff):
        return True, "outside morning window"

    # During first hour: require stronger signal
    if abs(score) < min_score:
        return False, (
            f"Morning filter: |score|={abs(score):.3f} < {min_score} threshold. "
            f"Stronger signal needed before {cutoff_h}:{cutoff_m:02d} ET."
        )

    # During first hour: require Kalman agreement if configured
    if require_kalman and kalman_agrees is not None and not kalman_agrees:
        return False, (
            f"Morning filter: Kalman disagrees during first hour. "
            f"Kalman agreement mandatory before {cutoff_h}:{cutoff_m:02d} ET."
        )

    return True, "morning filter passed"


# ---------------------------------------------------------------------------
# 4. Crypto/sector correlation filter
# ---------------------------------------------------------------------------

def check_crypto_correlation(
    symbol: str,
    side: str,
    crypto_benchmark_prices,  # BTC-USD price series
    cfg: dict,
) -> tuple[bool, str]:
    """For crypto-adjacent stocks, check BTC momentum direction."""
    crypto_symbols = _get_cfg(cfg, "crypto_symbols")
    crypto_sectors = _get_cfg(cfg, "crypto_sectors")
    momentum_periods = _get_cfg(cfg, "crypto_momentum_periods")

    # Check if symbol is crypto-adjacent
    sector_info = get_sector(symbol)
    is_crypto = (
        symbol in crypto_symbols
        or (sector_info and sector_info.get("sector") in crypto_sectors)
    )
    if not is_crypto:
        return True, "not crypto-adjacent"

    if crypto_benchmark_prices is None:
        return True, "no BTC data for correlation check"

    try:
        prices = np.asarray(crypto_benchmark_prices, dtype=float)
        prices = prices[~np.isnan(prices)]
        if len(prices) < momentum_periods + 1:
            return True, "insufficient BTC data"

        # Simple momentum: compare current price to N periods ago
        btc_momentum = (prices[-1] - prices[-momentum_periods]) / prices[-momentum_periods]
        btc_trending_up = btc_momentum > _get_cfg(cfg, "crypto_momentum_threshold")
        btc_trending_down = btc_momentum < -_get_cfg(cfg, "crypto_momentum_threshold")

        if side == "SELL" and btc_trending_up:
            return False, (
                f"Crypto correlation block: shorting {symbol} while BTC trending up "
                f"(momentum {btc_momentum:+.2%} over {momentum_periods} periods)"
            )
        if side == "BUY" and btc_trending_down:
            return False, (
                f"Crypto correlation block: buying {symbol} while BTC trending down "
                f"(momentum {btc_momentum:+.2%} over {momentum_periods} periods)"
            )

        return True, f"crypto correlation OK (BTC momentum {btc_momentum:+.2%})"
    except Exception as e:
        logger.warning("Crypto correlation check failed: %s", e)
        return True, f"crypto check error: {e}"


# ---------------------------------------------------------------------------
# 5. Kalman agreement check
# ---------------------------------------------------------------------------

def check_kalman_agreement(
    symbol: str,
    side: str,
    kalman_results: dict,  # {symbol: {"signal": float, ...}}
    cfg: dict,
) -> tuple[bool, str, bool]:
    """
    Check if Kalman filter agrees with the signal direction.
    Returns (passed, reason, agrees).
    """
    required = _get_cfg(cfg, "kalman_agreement_required")

    kr = kalman_results.get(symbol)
    if kr is None:
        if required:
            return False, f"Kalman has no data for {symbol} — required but unavailable", False
        return True, "no Kalman data (not required)", False

    kalman_trend = kr.get("trend", 0.0)
    agrees = (
        (side == "BUY" and kalman_trend > 0)
        or (side == "SELL" and kalman_trend < 0)
    )

    if not agrees and required:
        return False, (
            f"Kalman disagrees: trend={kalman_trend:+.6f} vs side={side}. "
            f"Kalman agreement is mandatory."
        ), False

    return True, f"Kalman {'agrees' if agrees else 'disagrees'} (trend={kalman_trend:+.6f})", agrees


# ---------------------------------------------------------------------------
# 6. Minimum price filter
# ---------------------------------------------------------------------------

def check_min_price(
    price: float,
    cfg: dict,
) -> tuple[bool, str]:
    """Block stocks trading below a minimum price threshold."""
    min_price = _get_cfg(cfg, "min_price")
    if price <= 0:
        return True, "no price data"
    if price < min_price:
        return False, f"Price ${price:.2f} below ${min_price:.2f} minimum"
    return True, f"price OK: ${price:.2f}"


# ---------------------------------------------------------------------------
# 7. Last-entry cutoff
# ---------------------------------------------------------------------------

def check_last_entry_cutoff(
    cfg: dict,
) -> tuple[bool, str]:
    """Block new entries in the last N minutes of the trading window."""
    cutoff_minutes = _get_cfg(cfg, "last_entry_minutes")
    tz = ZoneInfo(cfg.get("hours", {}).get("tz", "America/New_York"))
    now = datetime.now(tz)

    end_parts = cfg.get("hours", {}).get("end", "11:30").split(":")
    end_time = now.replace(hour=int(end_parts[0]), minute=int(end_parts[1]), second=0, microsecond=0)

    cutoff_time = end_time - timedelta(minutes=cutoff_minutes)

    if now >= cutoff_time:
        return False, (
            f"Last-entry cutoff: {cutoff_minutes} min before window end "
            f"({cutoff_time.strftime('%H:%M')}–{end_time.strftime('%H:%M')}). No new entries."
        )
    return True, "within entry window"


# ---------------------------------------------------------------------------
# Combined runner
# ---------------------------------------------------------------------------

def run_all_pre_execution_filters(
    symbol: str,
    side: str,
    score: float,
    cfg: dict,
    *,
    hmm: HMMRegimeDetector | None = None,
    spy_data=None,
    original_regime_label: str | None = None,
    volume_df=None,
    crypto_benchmark_prices=None,
    kalman_results: dict | None = None,
    price: float = 0.0,
) -> tuple[bool, list[str]]:
    """
    Run all pre-execution filters. Returns (proceed, list_of_reasons).
    Any failure blocks the trade.
    """
    reasons = []
    blocked = False

    # 1. Regime re-check
    if hmm is not None:
        ok, msg = check_regime_recheck(hmm, spy_data, side, original_regime_label)
        if not ok:
            blocked = True
        reasons.append(f"regime: {msg}")

    # 2. Volume
    ok, msg = check_volume(volume_df, cfg)
    if not ok:
        blocked = True
    reasons.append(f"volume: {msg}")

    # 5/6. Kalman agreement (check first so we can pass to time-of-day)
    kalman_agrees = None
    if kalman_results is not None:
        ok, msg, kalman_agrees = check_kalman_agreement(symbol, side, kalman_results, cfg)
        if not ok:
            blocked = True
        reasons.append(f"kalman: {msg}")

    # 3. Time-of-day
    ok, msg = check_time_of_day(score, cfg, kalman_agrees=kalman_agrees)
    if not ok:
        blocked = True
    reasons.append(f"time: {msg}")

    # 4. Crypto correlation
    ok, msg = check_crypto_correlation(symbol, side, crypto_benchmark_prices, cfg)
    if not ok:
        blocked = True
    reasons.append(f"crypto: {msg}")

    # 6. Minimum price
    if price > 0:
        ok, msg = check_min_price(price, cfg)
        if not ok:
            blocked = True
        reasons.append(f"min_price: {msg}")

    # 7. Last-entry cutoff (new entries only)
    ok, msg = check_last_entry_cutoff(cfg)
    if not ok:
        blocked = True
    reasons.append(f"last_entry: {msg}")

    return (not blocked), reasons
