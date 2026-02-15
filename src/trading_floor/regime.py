"""Market regime detection module."""
from __future__ import annotations


def detect_regime(spy_data, vix_data) -> dict:
    """
    Determine current market regime from SPY and VIX data.

    Parameters
    ----------
    spy_data : pd.Series or list
        Recent SPY closing prices (at least 20 data points).
    vix_data : pd.Series, list, or float
        VIX values or a single current VIX reading.

    Returns
    -------
    dict with keys:
        spy_trend  : 'bull' | 'bear' | 'sideways'
        vix_level  : 'low' | 'high'
        label      : e.g. 'bull_low_vol', 'bear_high_vol'
    """
    # --- SPY trend via 20-day simple moving average ---
    try:
        closes = list(spy_data)
    except TypeError:
        closes = [float(spy_data)]

    if len(closes) >= 20:
        ma20 = sum(closes[-20:]) / 20.0
    else:
        ma20 = sum(closes) / len(closes) if closes else 0.0

    current_price = closes[-1] if closes else 0.0
    pct_from_ma = (current_price - ma20) / ma20 if ma20 else 0.0

    if pct_from_ma > 0.01:
        spy_trend = "bull"
    elif pct_from_ma < -0.01:
        spy_trend = "bear"
    else:
        spy_trend = "sideways"

    # --- VIX level ---
    try:
        vix_val = float(list(vix_data)[-1])
    except (TypeError, IndexError):
        try:
            vix_val = float(vix_data)
        except (TypeError, ValueError):
            vix_val = 20.0

    vix_level = "high" if vix_val > 25 else "low"

    label = f"{spy_trend}_{vix_level}_vol"

    return {
        "spy_trend": spy_trend,
        "vix_level": vix_level,
        "label": label,
    }
