# Architect Review — ARCHITECTURE_V4 (2026-03-02)

Below is a critical review of the Alpaca migration + dual-strategy plan. I’m focusing on boundaries, data flow, safety, edge cases, schema, order management, and budget split on a single paper account.

---

## 1) Module boundaries & coupling
**What’s good**
- Clear separation between broker, position manager, strategies, and self-learner.
- Broker as single interface to Alpaca is the right choke-point.

**Risks / Tight coupling**
- **PositionManager directly depends on DB + broker + strategy logic.** It is doing tagging + budgeting + reconciliation + fill logic. This is a lot of responsibility and risks coupling to strategy-specific metadata (signals_json). 
  - **Change**: Split into (a) `PortfolioState` (read-only view of Alpaca + cached DB), (b) `OrderLedger` (fills + partial fills + reconciliation), and (c) `StrategyBudgeter` (per-strategy budget/limits). PositionManager can orchestrate these but should not own them all.
- **SelfLearner writing to workflow.yaml** is a config-layer side effect. That’s a coupling from review to runtime config file.
  - **Change**: Write adjustments to a separate `overrides.yaml` (or a DB table) and merge at runtime. Keep the base config immutable; rollbacks become trivial.
- **Strategies are likely to call broker & position manager directly.** This can be fine, but it means both strategies must adhere to exact broker semantics.
  - **Change**: Introduce a thin `ExecutionService` interface that normalizes strategy requests and handles idempotency / duplicates. It can live in `workflow.py` or `execution.py`.

**Outcome**: Boundaries are *mostly* clean, but PositionManager is overloaded and SelfLearner directly editing core config is risky. Recommend splitting responsibility and adding a runtime override layer.

---

## 2) Data flow correctness & race conditions (intraday + swing on one Alpaca account)
**Risks**
- **Simultaneous order submission:** Intraday scan (every 15m) and Swing scan (9:35) can fire at overlapping times and both allocate against the same cash. If both read account state before orders fill, you can overshoot cash or position limits.
- **Order fill timing:** Alpaca fills asynchronously. If swing submits at 9:35 and intraday submits at 9:45, both may assume full budget available.
- **Strategy tag mismatch:** Alpaca positions aren’t strategy-aware. If a swing trade is closed by intraday logic (or vice versa), tagging misalignment can occur.

**Changes**
1. **Atomic budget reservation**: Implement a **local reservation ledger** before order submission. When a strategy submits an order, reserve estimated cost; release on fill/cancel. This prevents double-spend. 
2. **Global lock / queue**: At minimum, serialize order submissions via a single `ExecutionService` queue (in process or DB). If the system can run in multiple cron processes, use a DB lock or file lock.
3. **Position ownership enforcement**: Enforce that intraday exit logic only closes positions tagged intraday, and swing exits only close swing. Use `client_order_id` or `order_tag` metadata in Alpaca to encode strategy and use it for reconciliation.
4. **Timing overlap**: Move swing scan to **after** intraday first scan or schedule with non-overlap (e.g., 9:40 vs 9:35 if intraday 9:30). But the real fix is reservation + lock.

---

## 3) Self-learning safety & bounds
**Current bounds are good, but insufficiently guarded.**

**Risks**
- **Sample bias**: 14-day windows can be too short; 20 trades threshold may be too low for meaningful significance.
- **Multiple adjustments at once**: Even with “max 2 params”, a 5% shift across several signals each week can drift quickly.
- **Automated allocation shift**: Allocation rebalancing could silently starve one strategy, amplifying short-term noise.
- **Auto-apply false**: Good. But if it turns on later, safeguards must be better.

**Changes**
- Increase **min_trades_to_adjust** to **30–50**, and require **min_winrate delta** (e.g., >5pp improvement) before adjustments.
- Add **stability gates**: only adjust if performance consistency across 2+ review windows (e.g., two consecutive 14-day windows).
- Add **max absolute weight** guard per signal (e.g., momentum never below 0.30 for intraday, etc.).
- Disallow allocation changes unless **n>=60 trades** per strategy + **Sharpe diff > 0.5** or **pnl diff > X%** over 60 days.
- Require **human approval** (manual flag) before turning `auto_apply` on. Keep a CLI or dashboard workflow.

---

## 4) Missing edge cases
Plan mentions some but misses explicit handling logic.

**Must handle:**
1. **Partial fills** — need order fill accumulation with correct average price; exits should use filled qty only.
2. **API rate limits** — Alpaca will throttle; use backoff, caching, and batch endpoints (e.g., get_bars for multiple symbols). Limit `get_latest_quotes` frequency.
3. **Market halts / LULD** — positions can halt intraday; stop orders won’t execute. Need a periodic “halt detection” and fail-safe exit if trading resumes.
4. **Short sales** — are they allowed? If not, ensure strategies never generate sell-short orders.
5. **Overnight gaps for swing** — stop loss orders should be placed as **stop-market** or **stop-limit**; gap risk must be documented. Need daily check if a gap skip blew past SL.
6. **Day-end close errors** — 3:45 close_all may partially fail. Must retry and report residual positions.
7. **Time zone / DST** — cron schedules should be in ET with DST safe handling.
8. **Alpaca maintenance windows** — orders may be rejected during maintenance; must retry or skip.

---

## 5) DB schema sufficiency & normalization
**Schema is okay but missing key normalization and index coverage.**

**Issues**
- `position_meta` mixes trade lifecycle + strategy metadata. Good for now, but it lacks **order-level tracking** (fills, partials, cancels). 
- `signal_accuracy` references `trade_id` but trade_id is a position id; if multiple entries/exits on same symbol in a day, you need a **trade_id** per entry event.
- No **orders table** to map `alpaca_order_id` -> fills -> status. Alpaca can split fills.
- JSON fields (signals_json, adjustments_json, accuracy_json) are fine but should be paired with summary columns for query efficiency.

**Changes**
- Add `orders` table:
  ```sql
  CREATE TABLE orders (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      alpaca_order_id TEXT UNIQUE,
      client_order_id TEXT,
      symbol TEXT,
      strategy TEXT,
      side TEXT,
      qty REAL,
      filled_qty REAL,
      avg_fill_price REAL,
      status TEXT,
      submitted_at TIMESTAMP,
      updated_at TIMESTAMP
  );
  ```
- Add `fills` table:
  ```sql
  CREATE TABLE fills (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      alpaca_order_id TEXT,
      fill_price REAL,
      fill_qty REAL,
      fill_time TIMESTAMP
  );
  ```
- Add indexes: `position_meta(strategy, status)`, `orders(symbol, status)`, `signal_accuracy(signal_type, timestamp)`.
- Consider normalizing signal scores into a `signal_snapshot` table if you expect to query them frequently (optional).

---

## 6) Bracket orders vs managed stops
**Recommendation: Use Alpaca bracket orders for intraday; use managed stops for swing.**

**Why**
- Intraday benefits from broker-side protection (fast, no bot downtime) and clean OCO behavior.
- Swing exits often use dynamic trailing or daily checks, which are better managed by your system (unless Alpaca supports trailing stops exactly as you need).

**Caveats**
- If you use bracket orders, ensure you **store the leg order ids** (take-profit & stop) in DB for reconciliation.
- If managing stops yourself, you must monitor order placement reliability and handle gaps manually.

**Change**
- Intraday: Use bracket OCO for TP+SL.
- Swing: Place initial stop (stop-market) optionally, but allow system to update/trail daily. Never rely solely on local stop logic without any broker stop on multi-day positions.

---

## 7) Budget split on a single paper account
**Concerns**
- Budget split is conceptual only unless enforced via reservation ledger. Alpaca account is one pool.
- If swing positions consume margin, intraday may be starved, and vice versa.
- Closing intraday at 15:45 can free cash that swing expects to keep stable (potential allocation drift).

**Changes**
- Enforce **per-strategy cash reservation** (as noted above). Deduct reserved cash from available balance when sizing orders.
- Add **intraday-only buying power** vs **swing-only** in the PositionManager ledger to prevent cross-bleed.
- Add **reconciliation rule**: If total equity < sum(reserved), scale down or block new orders until positions close.
- Consider **separate Alpaca paper accounts** if possible (cleanest separation, no cross-strategy interference). If not, reservation ledger + strict order gating is mandatory.

---

## Summary of Required Changes (High Priority)
1. **Add reservation ledger + execution queue** to prevent cash double-spend across strategies.
2. **Split PositionManager responsibilities** or at least isolate budgeting vs reconciliation vs tagging.
3. **Add orders + fills tables** and handle partial fills properly.
4. **Use bracket orders for intraday; managed stops for swing** with broker-side stop protection.
5. **Harden self-learner**: higher trade thresholds, stability gates, manual approval for auto-apply.
6. **Add explicit handling for rate limits, market halts, gaps, and end-of-day failure to close.**

If these changes are adopted, the design becomes production-worthy for paper trading and a safer foundation for eventual live trading.
