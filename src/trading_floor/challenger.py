"""
Trade Challenger — agents can raise challenges on trade plans that don't seem logical.
Each challenge has a severity (warn, block) and a reason.
Multiple challenges stack; any 'block' prevents the trade.

Checks:
1. Signal disagreement (breakout says BUY but mean rev screams SELL)
2. Re-entry guard (just got stopped out of this stock recently)
3. Regime mismatch (buying in a strong bear regime)
4. News absence (entering with zero news data)
5. Consecutive losses (stock has lost money every time we traded it)
6. Sector overload (too many stocks from correlated sectors)
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta
from typing import Optional

logger = logging.getLogger(__name__)


class Challenge:
    def __init__(self, agent: str, severity: str, reason: str, details: dict = None):
        self.agent = agent          # which agent raised it
        self.severity = severity    # 'warn' or 'block'
        self.reason = reason
        self.details = details or {}

    def __repr__(self):
        return f"[{self.severity.upper()}] {self.agent}: {self.reason}"


class TradeChallengeSystem:
    def __init__(self, cfg: dict, db_path: str = "trading.db"):
        self.cfg = cfg
        self.db_path = db_path
        # Thresholds
        self.disagreement_threshold = cfg.get("challenges", {}).get("disagreement_threshold", 1.5)
        self.reentry_minutes = cfg.get("challenges", {}).get("reentry_cooldown_minutes", 60)
        self.min_news_for_entry = cfg.get("challenges", {}).get("min_news_score", 0.0)
        self.max_consecutive_losses = cfg.get("challenges", {}).get("max_consecutive_losses", 3)

    def challenge_plan(self, plan: dict, context: dict) -> list[Challenge]:
        """
        Run all challenge checks on a trade plan.
        Returns list of Challenge objects.
        """
        challenges = []
        sym = plan.get("symbol", "")
        side = plan.get("side", "")
        score = plan.get("score", 0)

        # Skip forced exits
        if score == 999.9:
            return challenges

        signals = context.get("signal_details", {}).get(sym, {})
        regime = context.get("market_regime", {})

        # 1. Signal Disagreement
        c = self._check_signal_disagreement(sym, side, signals)
        if c:
            challenges.append(c)

        # 2. Re-entry Guard (warns, stacks with #2b for effective block)
        c = self._check_reentry(sym, side)
        if c:
            challenges.append(c)

        # 2b. Re-entry Signal Quality (only fires on re-entries — needs unanimous signals)
        c = self._check_reentry_signal_quality(sym, side, signals)
        if c:
            challenges.append(c)

        # 3. Regime Mismatch
        c = self._check_regime_mismatch(sym, side, regime)
        if c:
            challenges.append(c)

        # 4. News Absence
        c = self._check_news_absence(sym, side, signals)
        if c:
            challenges.append(c)

        # 5. Consecutive Losses
        c = self._check_consecutive_losses(sym)
        if c:
            challenges.append(c)

        # 6. Mean Reversion Opposition
        c = self._check_meanrev_opposition(sym, side, signals)
        if c:
            challenges.append(c)

        if challenges:
            for ch in challenges:
                logger.warning("Challenge on %s %s: %s", side, sym, ch)

        return challenges

    def _check_signal_disagreement(self, sym: str, side: str, signals: dict) -> Optional[Challenge]:
        """
        If signals violently disagree, that's a red flag.
        E.g., breakout=+1.0 but momentum=-1.0 → spread of 2.0
        Only considers signals with weight > 0 (zero-weight signals are disabled).
        """
        weights = self.cfg.get("signals", {}).get("weights", {})
        scores = []
        active_keys = []
        for key in ['momentum', 'meanrev', 'breakout', 'news']:
            if weights.get(key, 0) <= 0:
                continue  # skip zero-weight signals
            val = signals.get(key)
            if val is not None:
                scores.append(val)
                active_keys.append(key)

        if len(scores) < 2:
            return None

        spread = max(scores) - min(scores)
        if spread >= self.disagreement_threshold:
            # Check which active signals disagree
            bull_signals = [k for k in active_keys if signals.get(k, 0) > 0.3]
            bear_signals = [k for k in active_keys if signals.get(k, 0) < -0.3]

            return Challenge(
                agent="risk",
                severity="block" if spread >= 1.5 else "warn",
                reason=(
                    f"Signal disagreement: spread={spread:.2f}. "
                    f"Bull signals: {bull_signals}, Bear signals: {bear_signals}. "
                    f"Conflicting signals suggest uncertain direction."
                ),
                details={"spread": spread, "signals": signals}
            )
        return None

    def _check_reentry(self, sym: str, side: str) -> Optional[Challenge]:
        """
        GROUND RULE: Re-entry to a stock exited today requires overwhelming evidence.
        Instead of blocking, raise warnings that stack — all signals must agree,
        news must be present, and regime must be favorable. The should_proceed()
        logic will block if too many warnings accumulate (2+ = block).
        """
        try:
            db = sqlite3.connect(self.db_path)
            today = datetime.utcnow().strftime("%Y-%m-%d")
            rows = db.execute('''
                SELECT side, pnl, timestamp FROM trades
                WHERE symbol = ? AND date(timestamp) = ? AND pnl != 0
                ORDER BY timestamp DESC LIMIT 1
            ''', (sym, today)).fetchall()
            db.close()

            if rows:
                last_side, last_pnl, last_ts = rows[0]
                outcome = f"profit ${last_pnl:+.2f}" if last_pnl > 0 else f"loss ${last_pnl:+.2f}"
                return Challenge(
                    agent="compliance",
                    severity="warn",
                    reason=(
                        f"Re-entry caution: {sym} already exited today at {last_ts[11:16]} "
                        f"({outcome}). All signals must agree for re-entry."
                    ),
                    details={"last_pnl": last_pnl, "last_ts": last_ts, "is_reentry": True}
                )
        except Exception as e:
            logger.debug("Re-entry check failed: %s", e)
        return None

    def _check_reentry_signal_quality(self, sym: str, side: str, signals: dict) -> Optional[Challenge]:
        """
        For re-entries: require ALL signal components to agree on direction.
        If any signal disagrees or news is absent, warn (stacks with re-entry warning → block).
        """
        if not signals:
            return None

        components = signals.get(sym, {})
        if not components:
            return None

        # Check if this is actually a re-entry (called after _check_reentry sets the flag)
        try:
            db = sqlite3.connect(self.db_path)
            today = datetime.utcnow().strftime("%Y-%m-%d")
            rows = db.execute(
                'SELECT 1 FROM trades WHERE symbol = ? AND date(timestamp) = ? AND pnl != 0 LIMIT 1',
                (sym, today)
            ).fetchall()
            db.close()
            if not rows:
                return None  # Not a re-entry, skip this check
        except Exception:
            return None

        # For re-entries, every signal must point the same direction
        buy_signals = [k for k, v in components.items() if v > 0.1]
        sell_signals = [k for k, v in components.items() if v < -0.1]
        neutral = [k for k, v in components.items() if -0.1 <= v <= 0.1]
        news_val = components.get("news", 0)

        problems = []
        if side == "BUY" and sell_signals:
            problems.append(f"{', '.join(sell_signals)} bearish")
        elif side == "SELL" and buy_signals:
            problems.append(f"{', '.join(buy_signals)} bullish")
        if neutral:
            problems.append(f"{', '.join(neutral)} neutral/weak")
        if abs(news_val) < 0.05:
            problems.append("no news confirmation")

        if problems:
            return Challenge(
                agent="strategy",
                severity="warn",
                reason=(
                    f"Re-entry needs unanimous signals for {sym}, but: {'; '.join(problems)}. "
                    f"Components: {', '.join(f'{k}={v:+.2f}' for k,v in components.items())}"
                ),
                details={"components": components, "problems": problems}
            )
        return None

    def _check_regime_mismatch(self, sym: str, side: str, regime: dict) -> Optional[Challenge]:
        """
        Challenge buying in a strong bear regime or shorting in a strong bull.
        """
        hmm_bear = regime.get("hmm_bear_prob", 0)
        hmm_bull = regime.get("hmm_bull_prob", 0)

        if side == "BUY" and hmm_bear > 0.75:
            return Challenge(
                agent="strategy",
                severity="warn",
                reason=f"Buying in strong bear regime (bear prob={hmm_bear:.0%}). Consider waiting for confirmation.",
                details={"hmm_bear_prob": hmm_bear}
            )
        elif side == "SELL" and hmm_bull > 0.75:
            return Challenge(
                agent="strategy",
                severity="warn",
                reason=f"Shorting in strong bull regime (bull prob={hmm_bull:.0%}). Counter-trend risk.",
                details={"hmm_bull_prob": hmm_bull}
            )
        return None

    def _check_news_absence(self, sym: str, side: str, signals: dict) -> Optional[Challenge]:
        """
        Challenge entering when there's zero news signal — we're flying blind.
        """
        news_score = signals.get("news", None)
        if news_score is not None and news_score == 0.0:
            return Challenge(
                agent="risk",
                severity="warn",
                reason=f"Zero news signal for {sym} — no news data available. Entering blind.",
                details={"news_score": news_score}
            )
        return None

    def _check_consecutive_losses(self, sym: str) -> Optional[Challenge]:
        """
        Challenge entering a stock that's lost money every time we traded it.
        """
        try:
            db = sqlite3.connect(self.db_path)
            rows = db.execute('''
                SELECT pnl FROM trades
                WHERE symbol = ? AND pnl != 0
                ORDER BY timestamp DESC
                LIMIT ?
            ''', (sym, self.max_consecutive_losses)).fetchall()
            db.close()

            if len(rows) >= self.max_consecutive_losses:
                all_losses = all(r[0] < 0 for r in rows)
                if all_losses:
                    total_lost = sum(r[0] for r in rows)
                    return Challenge(
                        agent="strategy",
                        severity="block",
                        reason=(
                            f"{sym} has {len(rows)} consecutive losses "
                            f"(total ${total_lost:+.2f}). Stop trading this name."
                        ),
                        details={"consecutive_losses": len(rows), "total_lost": total_lost}
                    )
        except Exception as e:
            logger.debug("Consecutive loss check failed: %s", e)
        return None

    def _check_meanrev_opposition(self, sym: str, side: str, signals: dict) -> Optional[Challenge]:
        """
        If mean reversion strongly opposes trade direction, warn — but only for BUYs.
        
        For SELL signals: meanrev opposition (positive/oversold) actually CONFIRMS
        a momentum breakdown. A stock that's "oversold" and still being sold is showing
        strong bearish momentum — don't penalize this.
        
        For BUY signals: meanrev opposition (negative/overbought) is a legitimate
        concern — buying an overbought stock is risky.
        
        meanrev has weight=0 in composite but its value is still computed.
        """
        mr = signals.get("meanrev", 0)
        if mr is None:
            return None
        if side == "BUY" and mr < -0.5:
            return Challenge(
                agent="strategy",
                severity="warn",
                reason=f"Mean reversion strongly bearish ({mr:+.2f}) — opposes BUY on {sym}",
                details={"meanrev": mr}
            )
        # For SELL: meanrev > 0.5 means "oversold, should bounce" but this actually
        # confirms momentum breakdown — do NOT challenge sells on meanrev opposition
        return None

    def should_proceed(self, challenges: list[Challenge]) -> tuple[bool, str]:
        """
        Determine if trade should proceed given challenges.
        Returns (proceed, summary).

        Possible outcomes:
        - True, "No challenges raised"
        - False, "BLOCKED ..."
        - "caution", "CAUTION ..." — needs finance agent review
        """
        if not challenges:
            return True, "No challenges raised"

        blocks = [c for c in challenges if c.severity == "block"]
        warns = [c for c in challenges if c.severity == "warn"]

        if blocks:
            reasons = "; ".join(c.reason for c in blocks)
            return False, f"BLOCKED ({len(blocks)} blocks): {reasons}"

        if len(warns) >= 2:
            # 2+ warnings = effective block
            reasons = "; ".join(c.reason for c in warns)
            return False, f"BLOCKED (multiple warnings): {reasons}"

        # Single warning — route to finance agent for review (was auto-pass)
        if len(warns) == 1:
            return "caution", f"CAUTION (needs finance agent review): {warns[0].reason}"

        return True, "No actionable challenges"
