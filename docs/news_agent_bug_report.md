# News Agent Bug Report
Date: 2026-02-23
Reporter: Main Agent (research)

## Problem
News agent (`src/trading_floor/agents/news.py`) frequently scores 0.0, and when it does score, it's often wrong.

## Root Causes Found

### Bug 1: Keyword lists too narrow
Most financial headlines use words NOT in `_POSITIVE` or `_NEGATIVE` sets.
- "Pressured" → not in negative list → scores 0.0
- "Reiterates" → not captured
- "Hangover" → not captured
- Many common words missing: "outperform", already there, but "underperform", "downside", "overweight", "underweight", "hold", "target", "guidance" etc. are missing

### Bug 2: No negation handling
- "Apple Stock Recovery Is No Recovery At All" → scores +1.0 (matches "recovery")
- "Stock May Fall" → scores -1.0 (correct by luck)
- Any headline with "no", "not", "isn't", "won't" before a positive word gets wrong score

### Bug 3: "Google News" feed title leaks through
- First headline from RSS is always "Google News" (the feed title)
- Code says `titles[1:max_headlines + 1]` to skip it, but it still appears in results
- This dilutes scoring with a 0.0 entry

### Bug 4: Keyword scoring is binary (-1/0/+1 for most headlines)
- Most headlines match only 1 keyword → score is always -1.0, 0.0, or +1.0
- No granularity. A headline matching "surges" scores the same as "slight gain"
- Average of a few ±1.0 scores → wild swings or cancellation to 0.0

## Test Results (10 stocks, live data)
```
SPY   : -0.1250
QQQ   : +0.0000
AAPL  : +0.3750
NVDA  : +0.2857
TSLA  : -0.3750
META  : -0.2857
AMZN  : +0.0000
MSFT  : +0.0000
GOOGL : +0.0000
AMD   : +0.1667
```
4/10 stocks scored exactly 0.0. Not "all zeros" but many are, and the non-zero scores are unreliable.

## Proposed Fix (for QA to implement)

### Fix 1: Expand keyword lists significantly
Add ~30 more words to each list. Include financial-specific terms:
- Positive: "outperform", "overweight", "upbeat", "exceeds", "tops", "raises", "guidance", "momentum", "demand", "rebound", "support", "dividend", "approve", "launch", "milestone"
- Negative: "underperform", "underweight", "downside", "misses", "lowers", "guidance", "pressure", "headwind", "slowdown", "delay", "suspend", "recall", "overvalued", "bubble", "selloff", "correction"

### Fix 2: Add basic negation detection
Before keyword scoring, check for negation words ("no", "not", "n't", "never", "without", "fail") within 3 words before a keyword. Flip the polarity.

### Fix 3: Filter "Google News" from headlines
Add explicit check: `if h.strip().lower() == "google news": continue`

### Fix 4: Weight keywords by strength
Instead of all keywords = 1.0, assign weights:
- Strong words (surge, crash, plunge, soar) = 1.0
- Medium words (gain, drop, rise, fall) = 0.6
- Weak words (positive, negative, concern) = 0.3

## Impact Assessment Needed (for Architect)
- News signal weight = 0.25 (25% of final score)
- Currently producing unreliable scores → either 0.0 (killing 25% of signal) or wrong direction
- Fixing this should: (a) produce more non-zero scores, (b) improve directional accuracy
- Risk: over-expanding keywords could introduce false signals
- Recommendation: expand conservatively, backtest before/after
