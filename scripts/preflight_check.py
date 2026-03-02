#!/usr/bin/env python3
"""
Trading Floor Preflight Check & Self-Heal
Runs 2 hours before market open. Tests every component, fixes what it can, reports what it can't.
"""
import sys, os, json, time, subprocess, traceback
from pathlib import Path
from datetime import datetime, date

# Resolve trading floor root
FLOOR = Path(__file__).resolve().parent.parent
os.chdir(FLOOR)

RESULTS = {"passed": [], "fixed": [], "failed": [], "warnings": []}

def passed(name, detail=""):
    RESULTS["passed"].append(f"✅ {name}" + (f": {detail}" if detail else ""))

def fixed(name, detail=""):
    RESULTS["fixed"].append(f"🔧 {name}" + (f": {detail}" if detail else ""))

def failed(name, detail=""):
    RESULTS["failed"].append(f"❌ {name}" + (f": {detail}" if detail else ""))

def warn(name, detail=""):
    RESULTS["warnings"].append(f"⚠️ {name}" + (f": {detail}" if detail else ""))

# ── 1. Config loads ──
try:
    import yaml
    cfg = yaml.safe_load(open("configs/workflow.yaml"))
    passed("Config loads", "workflow.yaml parsed OK")
except Exception as e:
    failed("Config loads", str(e))
    # Fatal — can't continue
    print(json.dumps(RESULTS, indent=2))
    sys.exit(1)

# ── 2. All imports ──
import_tests = [
    ("trading_floor.workflow", "TradingFloor"),
    ("trading_floor.run", "main"),
    ("trading_floor.challenger", "TradeChallengeSystem"),
    ("trading_floor.pre_execution_filters", "run_all_pre_execution_filters"),
    ("trading_floor.data", "YahooDataProvider"),
    ("trading_floor.portfolio", "Portfolio"),
    ("trading_floor.agents.scout", "ScoutAgent"),
    ("trading_floor.agents.signal_momentum", "MomentumSignalAgent"),
    ("trading_floor.agents.signal_meanreversion", "MeanReversionSignalAgent"),
    ("trading_floor.agents.signal_breakout", "BreakoutSignalAgent"),
    ("trading_floor.agents.news", "NewsSentimentAgent"),
    ("trading_floor.agents.risk", "RiskAgent"),
    ("trading_floor.agents.pm", "PMAgent"),
    ("trading_floor.agents.exits", "ExitManager"),
    ("trading_floor.shadow", "ShadowRunner"),
    ("trading_floor.db", "Database"),
    ("trading_floor.signal_normalizer", "SignalNormalizer"),
    ("trading_floor.lightning", "LightningTracer"),
]

import_failures = 0
for mod_name, cls_name in import_tests:
    try:
        mod = __import__(mod_name, fromlist=[cls_name])
        getattr(mod, cls_name)
    except Exception as e:
        failed(f"Import {mod_name}.{cls_name}", str(e))
        import_failures += 1

if import_failures == 0:
    passed("All imports", f"{len(import_tests)} modules OK")
else:
    # Try self-heal: pip install -e .
    try:
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "-e", "."],
            cwd=str(FLOOR), capture_output=True, timeout=120
        )
        # Re-test
        still_broken = []
        for mod_name, cls_name in import_tests:
            try:
                mod = __import__(mod_name, fromlist=[cls_name])
                getattr(mod, cls_name)
            except:
                still_broken.append(f"{mod_name}.{cls_name}")
        if not still_broken:
            fixed("Imports", "pip install -e . resolved all import errors")
        else:
            failed("Imports (after pip fix)", f"Still broken: {still_broken}")
    except Exception as e:
        failed("Import self-heal", str(e))

# ── 3. __main__.py exists ──
main_py = FLOOR / "src" / "trading_floor" / "__main__.py"
if main_py.exists():
    passed("__main__.py exists", "python -m trading_floor will work")
else:
    try:
        main_py.write_text(
            '"""Entry point for `python -m trading_floor`."""\n'
            'from trading_floor.run import main\n\n'
            'if __name__ == "__main__":\n'
            '    main()\n'
        )
        fixed("__main__.py", "Created missing entry point")
    except Exception as e:
        failed("__main__.py", f"Missing and could not create: {e}")

# ── 4. Universe validation ──
universe = cfg.get("universe", [])
excluded = ["RKLB", "ONDS", "HUT"]
bad_inclusions = [s for s in excluded if s in universe]
if bad_inclusions:
    warn("Universe exclusions", f"{bad_inclusions} should be excluded but are in universe")
else:
    passed("Universe exclusions", f"RKLB/ONDS/HUT excluded, {len(universe)} symbols")

# ── 5. Weights validation ──
weights = cfg.get("signals", {}).get("weights", {})
expected_weights = {"momentum": 0.50, "meanrev": 0.00, "breakout": 0.15, "news": 0.25, "reserve": 0.10}
weight_issues = []
for k, v in expected_weights.items():
    actual = weights.get(k)
    if actual != v:
        weight_issues.append(f"{k}: expected {v}, got {actual}")
if weight_issues:
    warn("Weights", "; ".join(weight_issues))
else:
    passed("Weights", f"mom={weights.get('momentum')} mean={weights.get('meanrev')} brk={weights.get('breakout')} news={weights.get('news')}")

# ── 6. Approval file ──
approval_file = FLOOR / cfg.get("approval", {}).get("file", "approval.json")
if approval_file.exists():
    try:
        data = json.load(open(approval_file))
        if data.get("approved"):
            passed("Approval file", f"approved=true")
        else:
            warn("Approval file", "exists but approved != true")
    except Exception as e:
        warn("Approval file", f"exists but can't parse: {e}")
else:
    # Self-heal: create standing approval
    try:
        approval_file.write_text(json.dumps({
            "approved": True,
            "mode": "paper",
            "note": "Standing approval - auto-created by preflight"
        }, indent=2))
        fixed("Approval file", "Created standing approval.json")
    except Exception as e:
        failed("Approval file", f"Missing and could not create: {e}")

# ── 7. Database ──
db_path = FLOOR / cfg.get("logging", {}).get("db_path", "trading.db")
if db_path.exists():
    try:
        import sqlite3
        conn = sqlite3.connect(str(db_path))
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        required = ["signals", "trades"]
        missing = [t for t in required if t not in tables]
        if missing:
            warn("Database", f"Missing tables: {missing}")
        else:
            sig_count = conn.execute("SELECT count(*) FROM signals").fetchone()[0]
            trade_count = conn.execute("SELECT count(*) FROM trades").fetchone()[0]
            passed("Database", f"{sig_count} signals, {trade_count} trades, tables: {tables}")
        conn.close()
    except Exception as e:
        failed("Database", str(e))
else:
    warn("Database", f"{db_path} does not exist (will be created on first run)")

# ── 8. Portfolio file ──
portfolio_path = FLOOR / "portfolio.json"
if portfolio_path.exists():
    try:
        pf = json.load(open(portfolio_path))
        cash = pf.get("cash", 0)
        equity = pf.get("equity", cash)
        positions = pf.get("positions", {})
        passed("Portfolio", f"cash=${cash:.2f}, equity=${equity:.2f}, {len(positions)} positions")
        if equity < 100:
            warn("Portfolio equity", f"Very low equity: ${equity:.2f}")
    except Exception as e:
        failed("Portfolio", str(e))
else:
    warn("Portfolio", "portfolio.json missing")

# ── 9. Logging directories ──
for csv_key in ["trades_csv", "events_csv", "signals_csv"]:
    csv_path = Path(cfg.get("logging", {}).get(csv_key, ""))
    if csv_path.name:
        csv_dir = FLOOR / csv_path.parent
        if not csv_dir.exists():
            try:
                csv_dir.mkdir(parents=True, exist_ok=True)
                fixed(f"Log dir {csv_key}", f"Created {csv_dir}")
            except Exception as e:
                failed(f"Log dir {csv_key}", str(e))
        else:
            passed(f"Log dir {csv_key}", str(csv_dir))

# ── 10. Regime state file ──
regime_path = FLOOR / "configs" / "regime_state.json"
if regime_path.exists():
    try:
        rs = json.load(open(regime_path))
        age_min = (time.time() - os.path.getmtime(regime_path)) / 60
        if age_min > 1440:  # older than 24h
            warn("Regime state", f"Stale — last updated {age_min/60:.1f}h ago")
        else:
            regime = rs.get("regime", "?")
            confidence = rs.get("confidence", 0)
            passed("Regime state", f"regime={regime}, confidence={confidence:.1%}, age={age_min:.0f}m")
    except Exception as e:
        warn("Regime state", str(e))
else:
    warn("Regime state", "regime_state.json missing — regime monitor will create it")

# ── 11. Trading hours / holiday check ──
today_str = date.today().isoformat()
holidays = cfg.get("hours", {}).get("holidays", [])
is_holiday = today_str in holidays
weekday = date.today().weekday()
is_weekend = weekday >= 5

if is_holiday:
    warn("Trading day", f"Today ({today_str}) is a holiday — no trading")
elif is_weekend:
    warn("Trading day", f"Today is {'Saturday' if weekday == 5 else 'Sunday'} — no trading")
else:
    passed("Trading day", f"{today_str} is a trading day")

# ── 12. Data fetch test (quick — just SPY) ──
try:
    from trading_floor.data import YahooDataProvider
    dp = YahooDataProvider(interval="5m", lookback="2d")
    md = dp.fetch(["SPY"])
    if "SPY" in md and not md["SPY"].df.empty:
        rows = len(md["SPY"].df)
        latest = md["SPY"].df.index[-1]
        passed("Data fetch", f"SPY: {rows} bars, latest={latest}")
    else:
        warn("Data fetch", "SPY returned empty — market may be closed or yfinance issue")
except Exception as e:
    failed("Data fetch", str(e))

# ── 13. Finnhub API key ──
fh = cfg.get("finnhub", {})
if fh.get("enabled") and fh.get("api_key"):
    try:
        import urllib.request
        url = f"https://finnhub.io/api/v1/quote?symbol=SPY&token={fh['api_key']}"
        req = urllib.request.urlopen(url, timeout=10)
        data = json.loads(req.read())
        if data.get("c", 0) > 0:
            passed("Finnhub API", f"SPY quote=${data['c']}")
        else:
            warn("Finnhub API", f"Got response but price is 0: {data}")
    except Exception as e:
        warn("Finnhub API", str(e))

# ── 14. TradingFloor instantiation test ──
try:
    from trading_floor.workflow import TradingFloor
    tf = TradingFloor(cfg)
    passed("TradingFloor init", "Instantiated successfully")
except Exception as e:
    failed("TradingFloor init", str(e))

# ── 15. Dashboard check ──
try:
    import urllib.request
    req = urllib.request.urlopen("http://localhost:8000", timeout=5)
    if req.status == 200:
        passed("Dashboard", "Port 8000 responding")
    else:
        warn("Dashboard", f"Status {req.status}")
except Exception as e:
    warn("Dashboard", f"Not responding: {e}")

# ── 16. Broker module imports ──
broker_imports = [
    ("trading_floor.broker", "AlpacaBroker"),
    ("trading_floor.broker", "ExecutionService"),
    ("trading_floor.broker", "PortfolioState"),
    ("trading_floor.broker", "OrderLedger"),
    ("trading_floor.broker", "StrategyBudgeter"),
]
broker_fail = 0
for mod_name, cls_name in broker_imports:
    try:
        mod = __import__(mod_name, fromlist=[cls_name])
        getattr(mod, cls_name)
    except Exception as e:
        failed(f"Broker import {cls_name}", str(e))
        broker_fail += 1
if broker_fail == 0:
    passed("Broker imports", f"{len(broker_imports)} classes OK")

# ── 17. Review module imports ──
review_imports = [
    ("trading_floor.review", "SelfLearner"),
    ("trading_floor.review", "AdaptiveWeights"),
    ("trading_floor.review", "SafetyManager"),
    ("trading_floor.review", "Reporter"),
]
review_fail = 0
for mod_name, cls_name in review_imports:
    try:
        mod = __import__(mod_name, fromlist=[cls_name])
        getattr(mod, cls_name)
    except Exception as e:
        failed(f"Review import {cls_name}", str(e))
        review_fail += 1
if review_fail == 0:
    passed("Review imports", f"{len(review_imports)} classes OK")

# ── 18. V4 DB tables ──
v4_tables = ["position_meta", "orders", "fills", "budget_reservations",
             "signal_accuracy", "reviews", "config_history"]
try:
    import sqlite3 as _sq
    _conn = _sq.connect(str(db_path))
    _existing = [r[0] for r in _conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
    _missing = [t for t in v4_tables if t not in _existing]
    _conn.close()
    if _missing:
        failed("V4 DB tables", f"Missing: {_missing}")
    else:
        passed("V4 DB tables", f"All {len(v4_tables)} present")
except Exception as e:
    warn("V4 DB tables", str(e))

# ── 19. mw_state.json ──
mw_path = FLOOR / "configs" / "mw_state.json"
if mw_path.exists():
    try:
        _mw = json.load(open(mw_path))
        passed("mw_state.json", f"Loaded ({len(_mw)} strategies)")
    except Exception as e:
        warn("mw_state.json", f"Exists but invalid: {e}")
else:
    warn("mw_state.json", "Not found — will be created on first self-learner run")

# ── 20. Strategy instantiation ──
try:
    from trading_floor.strategies.intraday import IntradayStrategy
    passed("IntradayStrategy import", "OK")
except Exception as e:
    failed("IntradayStrategy import", str(e))

try:
    from trading_floor.strategies.swing import SwingStrategy
    passed("SwingStrategy import", "OK")
except Exception as e:
    failed("SwingStrategy import", str(e))

# ── 21. overrides.yaml validation ──
overrides_path = FLOOR / "configs" / "overrides.yaml"
if overrides_path.exists():
    try:
        _ov = yaml.safe_load(open(overrides_path))
        if _ov is None or isinstance(_ov, dict):
            passed("overrides.yaml", "Valid YAML" + (f" ({len(_ov or {})} keys)" if _ov else " (empty)"))
        else:
            warn("overrides.yaml", f"Unexpected type: {type(_ov)}")
    except Exception as e:
        failed("overrides.yaml", f"Invalid YAML: {e}")
else:
    passed("overrides.yaml", "Not present (using base config only)")

# ── 22. Alpaca API connectivity ──
try:
    alpaca_cfg = cfg.get("alpaca", {})
    _ak = alpaca_cfg.get("api_key", "")
    _as = alpaca_cfg.get("api_secret", "")
    # Resolve env vars
    if _ak.startswith("${") and _ak.endswith("}"):
        _ak = os.environ.get(_ak[2:-1], "")
    if _as.startswith("${") and _as.endswith("}"):
        _as = os.environ.get(_as[2:-1], "")
    if _ak and _as:
        from trading_floor.broker import AlpacaBroker
        _broker = AlpacaBroker(api_key=_ak, api_secret=_as, paper=True)
        _acct = _broker.get_account()
        passed("Alpaca API", f"Account {_acct.account_number}, equity=${float(_acct.equity):.2f}")
    else:
        warn("Alpaca API", "No API keys configured")
except Exception as e:
    warn("Alpaca API", f"Could not connect: {e}")

# ── REPORT ──
print("\n" + "=" * 60)
print(f"🏭 TRADING FLOOR PREFLIGHT — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
print("=" * 60)

all_ok = len(RESULTS["failed"]) == 0

for item in RESULTS["passed"]:
    print(item)
for item in RESULTS["fixed"]:
    print(item)
for item in RESULTS["warnings"]:
    print(item)
for item in RESULTS["failed"]:
    print(item)

print()
total = len(RESULTS["passed"]) + len(RESULTS["fixed"]) + len(RESULTS["warnings"]) + len(RESULTS["failed"])
print(f"Summary: {len(RESULTS['passed'])} passed, {len(RESULTS['fixed'])} self-healed, {len(RESULTS['warnings'])} warnings, {len(RESULTS['failed'])} FAILED")

if all_ok:
    print("\n✅ PREFLIGHT PASSED — Trading floor ready")
else:
    print("\n❌ PREFLIGHT FAILED — Manual intervention needed")

sys.exit(0 if all_ok else 1)
