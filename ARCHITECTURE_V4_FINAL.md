# ARCHITECTURE V4 — FINAL (Post-Review Synthesis)
## 2026-03-02 | All Agent Reviews Incorporated

---

## Agent Consensus Matrix

| Topic | Architect | Strategy | Finance | Resolution |
|-------|-----------|----------|---------|------------|
| Budget split | Reservation ledger needed | — | Flip to $2K/$3K | ✅ $2K intraday / $3K swing + reservation ledger |
| Position Manager | Split into 3 components | — | — | ✅ PortfolioState + OrderLedger + StrategyBudgeter |
| Race conditions | Execution queue + locks | — | — | ✅ Serialized ExecutionService |
| Self-learner safety | Higher thresholds, stability gates | Separate per strategy, regime-conditional | Tighten drift to 10% | ✅ All three incorporated |
| Signal accuracy | — | Sector-adjusted + regime-conditional | — | ✅ Enhanced attribution |
| Intraday TP | — | — | 5% → 2.5% (0/131 hit 5%) | ✅ Lower to 2.5% |
| Swing threshold | — | 0.20 → 0.25 | — | ✅ Start at 0.25 |
| Swing entry timing | — | Add 3:50 PM window | — | ✅ Dual entry windows |
| Exclusions | — | AVAV/MP/POWL both; IONQ/RGTI swing-only | — | ✅ Adopted |
| Bracket orders | Intraday=bracket, Swing=managed | — | — | ✅ Adopted |
| Stops/overrides | Use overrides.yaml not edit workflow.yaml | — | — | ✅ Runtime override layer |
| Kill switches | — | Per-strategy kills | 5% daily + 8% weekly + 12% monthly | ✅ Layered |
| Compounding | — | — | Defer until $7,500 equity | ✅ Fixed sizing initially |
| Min shares | — | — | 10-share minimum | ✅ Added |
| Evidence thresholds | — | 30 for weights, 20 for filters, separate swing window 60-90d | — | ✅ Adopted |
| Max hold | — | — | 10 → 7 days or time-decay trail | ✅ 10 days but tighten trail after day 5 |
| Sector limits | — | — | Max 1 swing position per sector | ✅ Added |

---

## Final Module Structure

```
src/trading_floor/
├── broker/
│   ├── __init__.py
│   ├── alpaca_broker.py      # Alpaca API wrapper (orders, account, data)
│   ├── execution_service.py  # Serialized order queue, deduplication, idempotency
│   ├── portfolio_state.py    # Read-only account state (syncs with Alpaca)
│   ├── order_ledger.py       # Order/fill tracking, partial fill handling
│   └── strategy_budgeter.py  # Per-strategy budget reservation & enforcement
├── strategies/
│   ├── __init__.py
│   ├── base.py               # Abstract strategy interface
│   ├── intraday.py           # Refactored from workflow.py (13-gate pipeline)
│   └── swing.py              # New multi-day strategy
├── review/
│   ├── __init__.py
│   └── self_learner.py       # Nightly review + safe auto-adjustments
├── agents/                   # (existing — scout, pm, risk, etc.)
├── run.py                    # Updated entry point: runs both strategies
├── workflow.py               # Legacy — kept as fallback, imports from strategies/
├── db.py                     # Updated with new schema
└── __main__.py               # Entry point
```

---

## Final Configuration (workflow.yaml additions)

```yaml
broker:
  provider: alpaca
  mode: paper
  starting_equity: 5000
  min_shares: 10              # skip trades where qty < 10 shares

strategies:
  intraday:
    enabled: true
    budget: 2000
    max_positions: 3
    weights: {momentum: 0.50, meanrev: 0.00, breakout: 0.15, news: 0.25}
    threshold: 0.25
    take_profit: 0.025        # lowered from 5% — 0/131 trades hit 5%
    stop_loss_atr: 2.0
    close_by: "15:45"
    universe_exclude: [RKLB, ONDS, HUT, AVAV, MP, POWL]

  swing:
    enabled: true
    budget: 3000
    max_positions: 3
    max_per_sector: 1         # sector concentration limit
    weights: {momentum: 0.55, meanrev: 0.35, breakout: 0.00, news: 0.10}
    threshold: 0.25           # raised from 0.20 per strategy agent
    take_profit: 0.15
    stop_loss: 0.08
    max_hold_days: 10
    trailing_trigger: 0.08
    trailing_pct: 0.04
    time_decay_trail_after_day: 5   # tighten trail after day 5
    time_decay_trail_pct: 0.025     # tighter 2.5% trail in final days
    entry_windows:
      - {start: "09:35", end: "10:00", bias: "gap_continuation"}
      - {start: "15:45", end: "15:55", bias: "trend_confirmation"}
    universe_exclude: [RKLB, ONDS, HUT, IONQ, RGTI, AVAV, MP, POWL]

self_learning:
  enabled: true
  auto_apply: false           # manual approval required initially
  intraday:
    review_window_days: 14
    min_trades_to_loosen_filter: 20
    min_trades_to_change_weights: 30
    min_trades_to_add_exclusion: 10
    max_weight_change_per_review: 0.05
    max_total_drift: 0.10
  swing:
    review_window_days: 90    # swing generates fewer trades
    min_trades_to_loosen_filter: 20
    min_trades_to_change_weights: 30
    min_trades_to_add_exclusion: 10
    max_weight_change_per_review: 0.05
    max_total_drift: 0.10
  accuracy_method: regime_conditional  # not naive direction match
  revert_trigger: 3           # auto-revert after 3 consecutive losing days

kill_switches:
  daily_max_loss_pct: 0.05    # $250 on $5K
  weekly_max_loss_pct: 0.08   # $400 on $5K
  monthly_max_loss_pct: 0.12  # $600 on $5K
  per_strategy:
    intraday_daily: 0.03      # $60 on $2K budget
    swing_weekly: 0.04        # $120 on $3K budget

compounding:
  enabled: false              # defer until $7,500 equity
  activation_equity: 7500
```

---

## Final Database Schema

```sql
-- Existing tables preserved (signals, trades, events, agent_memory, shadow_predictions)

-- NEW: Position lifecycle tracking
CREATE TABLE position_meta (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    strategy TEXT NOT NULL CHECK(strategy IN ('intraday', 'swing')),
    side TEXT NOT NULL CHECK(side IN ('buy', 'sell')),
    entry_order_id TEXT,
    entry_price REAL,
    entry_time TIMESTAMP,
    entry_qty REAL,
    exit_order_id TEXT,
    exit_price REAL,
    exit_time TIMESTAMP,
    stop_price REAL,
    tp_price REAL,
    max_hold_days INTEGER,
    signals_json TEXT,
    market_regime TEXT,
    sector TEXT,
    exit_reason TEXT CHECK(exit_reason IN ('tp', 'sl', 'trail', 'time', 'kill_switch', 'manual', NULL)),
    pnl REAL,
    pnl_pct REAL,
    status TEXT DEFAULT 'open' CHECK(status IN ('open', 'pending', 'closed', 'cancelled')),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- NEW: Order tracking (maps to Alpaca orders)
CREATE TABLE orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    alpaca_order_id TEXT UNIQUE,
    client_order_id TEXT UNIQUE,
    position_meta_id INTEGER REFERENCES position_meta(id),
    symbol TEXT NOT NULL,
    strategy TEXT NOT NULL,
    side TEXT NOT NULL,
    order_type TEXT NOT NULL,
    qty REAL NOT NULL,
    filled_qty REAL DEFAULT 0,
    limit_price REAL,
    stop_price REAL,
    avg_fill_price REAL,
    status TEXT DEFAULT 'pending',
    submitted_at TIMESTAMP,
    filled_at TIMESTAMP,
    cancelled_at TIMESTAMP,
    error_message TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- NEW: Fill tracking (partial fills)
CREATE TABLE fills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER REFERENCES orders(id),
    alpaca_order_id TEXT,
    fill_price REAL NOT NULL,
    fill_qty REAL NOT NULL,
    fill_time TIMESTAMP NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- NEW: Budget reservations (prevents double-spend)
CREATE TABLE budget_reservations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy TEXT NOT NULL,
    symbol TEXT NOT NULL,
    reserved_amount REAL NOT NULL,
    order_id INTEGER REFERENCES orders(id),
    status TEXT DEFAULT 'reserved' CHECK(status IN ('reserved', 'filled', 'released')),
    created_at TIMESTAMP,
    released_at TIMESTAMP
);

-- NEW: Signal accuracy tracking (regime-conditional)
CREATE TABLE signal_accuracy (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    position_meta_id INTEGER REFERENCES position_meta(id),
    strategy TEXT NOT NULL,
    signal_type TEXT NOT NULL,
    signal_score REAL,
    price_direction REAL,       -- actual price change %
    sector_return REAL,         -- sector ETF return over same period
    market_regime TEXT,         -- trending/ranging/volatile
    was_correct BOOLEAN,        -- raw direction match
    adjusted_correct BOOLEAN,   -- vs sector-adjusted return
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- NEW: Review history
CREATE TABLE reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    review_date DATE NOT NULL,
    strategy TEXT,              -- NULL = portfolio-level
    trades_analyzed INTEGER,
    pnl REAL,
    win_rate REAL,
    signal_accuracy_json TEXT,
    recommendations_json TEXT,
    adjustments_applied_json TEXT,
    report_path TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- NEW: Config change audit trail
CREATE TABLE config_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    changed_by TEXT NOT NULL,   -- 'self_learner', 'manual', 'finance_agent'
    strategy TEXT,
    field_path TEXT NOT NULL,
    old_value TEXT,
    new_value TEXT,
    reason TEXT,
    reverted BOOLEAN DEFAULT FALSE,
    reverted_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Indexes
CREATE INDEX idx_position_meta_strategy_status ON position_meta(strategy, status);
CREATE INDEX idx_position_meta_symbol ON position_meta(symbol);
CREATE INDEX idx_orders_status ON orders(status);
CREATE INDEX idx_orders_symbol ON orders(symbol, strategy);
CREATE INDEX idx_signal_accuracy_type ON signal_accuracy(signal_type, strategy);
CREATE INDEX idx_budget_reservations_strategy ON budget_reservations(strategy, status);
CREATE INDEX idx_config_history_field ON config_history(field_path, created_at);
```

---

## Runtime Override System (Architect Recommendation)

Instead of self-learner editing workflow.yaml directly:
- Base config: `configs/workflow.yaml` (immutable by automation)
- Overrides: `configs/overrides.yaml` (written by self-learner)
- Merged at runtime: base + overrides, overrides win
- Rollback = delete overrides.yaml

```python
# In run.py
def load_config():
    base = yaml.safe_load(open("configs/workflow.yaml"))
    overrides_path = Path("configs/overrides.yaml")
    if overrides_path.exists():
        overrides = yaml.safe_load(open(overrides_path))
        deep_merge(base, overrides)
    return base
```

---

## Cron Schedule (Final)

```
KEEP:
  */15 9-11 * * 1-5  — Intraday scan (update to use Alpaca broker)
  */5 9-15 * * 1-5   — Intraday exit monitor
  */5 9-16 * * 1-5   — Regime monitor
  30 7 * * 1-5       — Preflight check
  30 8 * * 1-5       — Morning strategy (finance agent)

NEW:
  40 9 * * 1-5       — Swing AM scan (9:40, after first intraday scan)
  50 15 * * 1-5      — Swing PM scan (3:50 PM, EOD entry window)
  0 10 * * 1-5       — Swing daily exit check (10 AM)
  45 15 * * 1-5      — Intraday force-close check (3:45 PM)
  0 20 * * 1-5       — Nightly self-learning review (8 PM)
```

---

## Build Phases

### Phase 1: Core Broker + DB (build first, test first)
Files: broker/alpaca_broker.py, broker/portfolio_state.py, broker/order_ledger.py, 
       broker/strategy_budgeter.py, broker/execution_service.py, db.py (schema migration)
Tests: test_broker.py, test_portfolio_state.py, test_order_ledger.py, test_budgeter.py

### Phase 2: Strategy Engines
Files: strategies/base.py, strategies/intraday.py, strategies/swing.py, run.py (updated)
Tests: test_intraday.py, test_swing.py, test_integration.py

### Phase 3: Self-Learning Review
Files: review/self_learner.py, configs/overrides.yaml support in run.py
Tests: test_self_learner.py

### Phase 4: Integration + Crons + Functional Test
Files: Updated crons, updated preflight, end-to-end test
Tests: test_e2e.py (full cycle: scan → order → fill → exit → review)

---

## Implementation Notes for Developer
- Alpaca SDK: `alpaca-py==0.43.2` already installed in .venv
- Use `alpaca.trading.client.TradingClient` for orders
- Use `alpaca.data.historical.StockHistoricalDataClient` for bars
- Paper base URL: https://paper-api.alpaca.markets
- API keys in workflow.yaml (resolve ${env} vars at runtime)
- All times in ET (America/New_York)
- Python 3.x, existing venv at .venv/
- Existing code uses: yaml, sqlite3, pandas, numpy, requests
- DO NOT break existing workflow.py — strategies/intraday.py wraps it
- client_order_id format: `{strategy}_{symbol}_{timestamp}` for reconciliation
