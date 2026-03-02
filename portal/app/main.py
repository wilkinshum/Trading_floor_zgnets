"""
OpenClaw Personal Dashboard — FastAPI Backend
Dark navy theme, 7 tabs: Overview, Trading, Tokens, Agents, Schedule, Journal, 2nd Brain
"""
import logging
import json
import asyncio
import re
import subprocess
import os
from datetime import datetime, timedelta
from pathlib import Path

import requests as http_requests
import yaml as _yaml

import aiosqlite
from fastapi import FastAPI, WebSocket, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

# --- Config ---
BASE_DIR = Path(__file__).resolve().parent.parent
PROJECT_ROOT = BASE_DIR.parent
DB_PATH = PROJECT_ROOT / "trading.db"
RECEIPTS_CSV = Path(r"C:\Users\moltbot\OneDrive\Desktop\receipts_snake.csv")
PORTFOLIO_PATH = PROJECT_ROOT / "portfolio.json"
WORKFLOW_YAML = PROJECT_ROOT / "configs" / "workflow.yaml"
MEMORY_DIR = Path(r"C:\Users\moltbot\.openclaw\workspace\memory")
WORKSPACE_DIR = Path(r"C:\Users\moltbot\.openclaw\workspace")
REVIEWS_DIR = PROJECT_ROOT / "trading_logs" / "daily_reviews"

app = FastAPI(title="OpenClaw Dashboard")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────

def read_portfolio():
    """Read live portfolio from Alpaca paper account, fallback to portfolio.json."""
    try:
        cfg = _get_alpaca_cfg()
        if cfg:
            headers = {"APCA-API-KEY-ID": cfg["key"], "APCA-API-SECRET-KEY": cfg["secret"]}
            base = cfg["base"]
            acct = http_requests.get(f"{base}/account", headers=headers, timeout=5).json()
            positions = http_requests.get(f"{base}/positions", headers=headers, timeout=5).json()
            pos_dict = {}
            for p in (positions if isinstance(positions, list) else []):
                pos_dict[p["symbol"]] = {
                    "quantity": float(p.get("qty", 0)),
                    "avg_price": float(p.get("avg_entry_price", 0)),
                    "current_price": float(p.get("current_price", 0)),
                    "unrealized_pl": float(p.get("unrealized_pl", 0)),
                    "unrealized_plpc": float(p.get("unrealized_plpc", 0)),
                    "market_value": float(p.get("market_value", 0)),
                    "side": p.get("side", "long"),
                }
            return {
                "equity": float(acct.get("equity", 0)),
                "cash": float(acct.get("cash", 0)),
                "buying_power": float(acct.get("buying_power", 0)),
                "positions": pos_dict,
                "source": "alpaca",
                "account_number": acct.get("account_number", ""),
                "status": acct.get("status", ""),
            }
    except Exception as e:
        logging.warning(f"Alpaca fetch failed, falling back to portfolio.json: {e}")
    if PORTFOLIO_PATH.exists():
        try:
            return json.loads(PORTFOLIO_PATH.read_text())
        except Exception:
            pass
    return {"cash": 0, "equity": 0, "positions": {}}


def _get_alpaca_cfg():
    """Load Alpaca API config from workflow.yaml or env vars."""
    key = os.environ.get("ALPACA_API_KEY", "")
    secret = os.environ.get("ALPACA_API_SECRET", "")
    base = "https://paper-api.alpaca.markets/v2"
    if key and secret:
        return {"key": key, "secret": secret, "base": base}
    if WORKFLOW_YAML.exists():
        try:
            cfg = _yaml.safe_load(WORKFLOW_YAML.read_text())
            alpaca = cfg.get("alpaca", {})
            key_val = alpaca.get("api_key", "")
            sec_val = alpaca.get("api_secret", "")
            base = alpaca.get("base_url", base)

            def _get_user_env(name: str) -> str:
                """Read user env var from registry (works even if process started before var was set)."""
                try:
                    import winreg
                    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Environment") as k:
                        val, _ = winreg.QueryValueEx(k, name)
                        return str(val)
                except Exception:
                    return ""

            def _resolve(v: str) -> str:
                v = (v or "").strip()
                if v.startswith("${") and v.endswith("}"):
                    env_name = v[2:-1]
                    return os.environ.get(env_name, "") or _get_user_env(env_name)
                return v

            key = _resolve(key_val)
            secret = _resolve(sec_val)
            if key and secret:
                return {"key": key, "secret": secret, "base": base}
        except Exception:
            pass
    return None


async def db_query(sql, params=None):
    """Run a read query against trading.db, return list of dicts."""
    if not DB_PATH.exists():
        return []
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(sql, params or []) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]


def list_memory_dates():
    """Return sorted list of YYYY-MM-DD date strings from memory dir."""
    dates = []
    if MEMORY_DIR.exists():
        for f in MEMORY_DIR.glob("????-??-??.md"):
            dates.append(f.stem)
    dates.sort(reverse=True)
    return dates


def read_file_safe(path: Path) -> str:
    if path.exists():
        return path.read_text(encoding="utf-8", errors="replace")
    return ""


def get_cron_jobs():
    """Try to read OpenClaw cron jobs."""
    try:
        result = subprocess.run(
            ["openclaw", "cron", "list", "--json"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            return json.loads(result.stdout)
    except Exception:
        pass
    # Fallback: try reading cron config files
    cron_dir = WORKSPACE_DIR / ".openclaw" / "crons"
    if not cron_dir.exists():
        cron_dir = Path(r"C:\Users\moltbot\.openclaw\crons")
    jobs = []
    if cron_dir.exists():
        for f in cron_dir.glob("*.json"):
            try:
                jobs.append(json.loads(f.read_text()))
            except Exception:
                pass
    return jobs


# ──────────────────────────────────────────────
# Routes — Pages
# ──────────────────────────────────────────────

@app.get("/")
async def index(request: Request):
    return templates.TemplateResponse("dashboard.html", {"request": request})


# ──────────────────────────────────────────────
# API — Overview
# ──────────────────────────────────────────────

@app.get("/api/overview")
async def api_overview():
    p = read_portfolio()
    equity = p.get("equity", 0)
    positions = p.get("positions", {})
    starting_equity = 5000.0

    # Compute today's PnL from trades table
    today = datetime.now().strftime("%Y-%m-%d")
    today_trades = await db_query(
        "SELECT COALESCE(SUM(pnl),0) as total_pnl, COUNT(*) as cnt FROM trades WHERE timestamp LIKE ?",
        [f"{today}%"]
    )
    today_pnl = today_trades[0]["total_pnl"] if today_trades else 0

    # Also check position_meta for V4 trades
    today_v4 = await db_query(
        "SELECT COALESCE(SUM(pnl),0) as total_pnl, COUNT(*) as cnt FROM position_meta WHERE exit_time LIKE ? AND pnl IS NOT NULL",
        [f"{today}%"]
    )
    today_pnl_v4 = today_v4[0]["total_pnl"] if today_v4 else 0

    # Win rate from both tables
    all_trades = await db_query("SELECT pnl FROM trades WHERE pnl IS NOT NULL")
    all_v4 = await db_query("SELECT pnl FROM position_meta WHERE pnl IS NOT NULL")
    all_pnls = [t.get("pnl", 0) or 0 for t in all_trades] + [t.get("pnl", 0) or 0 for t in all_v4]
    wins = sum(1 for p in all_pnls if p > 0)
    total = len(all_pnls)
    win_rate = round(wins / total * 100, 1) if total > 0 else 0

    # Unrealized PnL from open positions
    unrealized = sum(pos.get("unrealized_pl", 0) for pos in positions.values()) if isinstance(positions, dict) else 0

    # Alpaca order history
    recent_orders = []
    try:
        cfg = _get_alpaca_cfg()
        if cfg:
            headers = {"APCA-API-KEY-ID": cfg["key"], "APCA-API-SECRET-KEY": cfg["secret"]}
            resp = http_requests.get(f"{cfg['base']}/orders?status=all&limit=20&direction=desc", headers=headers, timeout=5)
            if resp.status_code == 200:
                for o in resp.json():
                    recent_orders.append({
                        "symbol": o.get("symbol"),
                        "side": o.get("side"),
                        "qty": o.get("qty"),
                        "status": o.get("status"),
                        "filled_at": o.get("filled_at"),
                        "filled_avg_price": o.get("filled_avg_price"),
                        "created_at": o.get("created_at"),
                        "type": o.get("type"),
                    })
    except Exception:
        pass

    return {
        "equity": round(equity, 2),
        "starting_equity": starting_equity,
        "total_return": round(equity - starting_equity, 2),
        "total_return_pct": round((equity - starting_equity) / starting_equity * 100, 2) if starting_equity else 0,
        "today_pnl": round(today_pnl + today_pnl_v4, 2),
        "unrealized_pnl": round(unrealized, 2),
        "open_positions": len(positions),
        "win_rate": win_rate,
        "total_trades": total,
        "cash": round(p.get("cash", 0), 2),
        "buying_power": round(p.get("buying_power", 0), 2),
        "source": p.get("source", "portfolio.json"),
        "account_number": p.get("account_number", ""),
        "account_status": p.get("status", ""),
        "recent_orders": recent_orders,
    }


# ──────────────────────────────────────────────
# API — Trading
# ──────────────────────────────────────────────

@app.get("/api/positions")
async def api_positions():
    p = read_portfolio()
    positions = p.get("positions", {})
    result = []
    for sym, data in positions.items():
        if isinstance(data, dict):
            qty = data.get("quantity", data.get("qty", 0))
            entry = data.get("avg_price", data.get("entry_price", 0))
            current = data.get("current_price", 0)
            unrealized = data.get("unrealized_pl", 0)
            unrealized_pct = data.get("unrealized_plpc", 0)
            market_val = data.get("market_value", 0)
            result.append({
                "symbol": sym,
                "qty": qty,
                "entry": round(float(entry), 2),
                "current": round(float(current), 2),
                "market_value": round(float(market_val), 2),
                "pnl": round(float(unrealized), 2),
                "pnl_pct": round(float(unrealized_pct) * 100, 2),
                "side": data.get("side", "long"),
            })
    return result


@app.get("/api/trades")
async def api_trades():
    return await db_query("SELECT * FROM trades ORDER BY timestamp DESC LIMIT 50")


@app.get("/api/equity_history")
async def api_equity_history():
    """
    Build equity curve from both legacy trades and V4 position_meta.
    Anchors from $5,000 starting equity.
    """
    # Legacy trades
    legacy = await db_query(
        "SELECT timestamp, pnl, side, symbol FROM trades WHERE pnl IS NOT NULL AND pnl != 0.0 ORDER BY timestamp ASC"
    )
    # V4 trades from position_meta
    v4 = await db_query(
        "SELECT exit_time as timestamp, pnl, strategy, symbol FROM position_meta WHERE pnl IS NOT NULL AND exit_time IS NOT NULL ORDER BY exit_time ASC"
    )

    p = read_portfolio()
    current_equity = round(p.get("equity", 0) or p.get("cash", 0), 2)
    starting = 5000.0

    # Merge and sort all trades by timestamp
    all_trades = []
    for r in legacy:
        all_trades.append({"timestamp": r["timestamp"], "pnl": r.get("pnl", 0), "label": f"{r.get('side','')} {r.get('symbol','')}", "source": "legacy"})
    for r in v4:
        all_trades.append({"timestamp": r["timestamp"], "pnl": r.get("pnl", 0), "label": f"{r.get('strategy','')} {r.get('symbol','')}", "source": "v4"})
    all_trades.sort(key=lambda x: x["timestamp"] or "")

    if not all_trades:
        return [
            {"timestamp": "start", "equity": starting},
            {"timestamp": datetime.now().isoformat(), "equity": current_equity}
        ]

    equity = starting
    curve = [{"timestamp": "start", "equity": starting}]
    for t in all_trades:
        pnl = t.get("pnl") or 0.0
        equity += pnl
        curve.append({
            "timestamp": t["timestamp"],
            "equity": round(equity, 2),
            "trade": f"{t['label']} PnL:{pnl:+.2f}",
            "source": t["source"],
        })
    # Add current live equity as final point
    curve.append({"timestamp": datetime.now().isoformat(), "equity": current_equity, "trade": "current"})
    return curve


@app.get("/api/signals")
async def api_signals():
    return await db_query("SELECT * FROM signals ORDER BY timestamp DESC LIMIT 20")


# ──────────────────────────────────────────────
# API — Tokens (placeholder / mock)
# ──────────────────────────────────────────────

@app.get("/api/tokens")
async def api_tokens():
    """Token usage — parses real data from ALL agent session transcripts dynamically."""
    agents_root = Path(r"C:\Users\moltbot\.openclaw\agents")
    
    total_input = 0
    total_output = 0
    total_cost = 0.0
    by_model = {}
    by_day = {}
    by_model_day = {}  # model -> day -> {input, output}
    by_agent = {}      # agent_id -> {input, output, cost, calls, sessions}
    session_count = 0
    
    def parse_usage_entry(entry, agent_id):
        """Parse a single JSONL entry and accumulate stats."""
        nonlocal total_input, total_output, total_cost, session_count
        
        msg = entry.get("message") or {}
        usage = msg.get("usage") or {}
        inp = usage.get("input") or usage.get("inputTokens") or usage.get("prompt_tokens") or 0
        out = usage.get("output") or usage.get("outputTokens") or usage.get("completion_tokens") or 0
        cache_read = usage.get("cacheRead") or 0
        cost_obj = usage.get("cost") or {}
        cost = cost_obj.get("total", 0) if isinstance(cost_obj, dict) else (cost_obj or 0)
        
        if not (inp or out or usage.get("totalTokens")):
            return
        
        total_toks = usage.get("totalTokens") or 0
        if total_toks > 0 and total_toks > (inp + cache_read + out):
            actual_input = total_toks - out
        else:
            actual_input = inp + cache_read
        
        total_input += actual_input
        total_output += out
        total_cost += cost
        
        # Per-agent
        if agent_id not in by_agent:
            by_agent[agent_id] = {"input": 0, "output": 0, "cost": 0.0, "calls": 0, "sessions": 0}
        by_agent[agent_id]["input"] += actual_input
        by_agent[agent_id]["output"] += out
        by_agent[agent_id]["cost"] += cost
        by_agent[agent_id]["calls"] += 1
        
        # Per-model
        model = msg.get("model") or entry.get("model") or "unknown"
        model_short = model.split("/")[-1] if "/" in model else model
        if model_short not in by_model:
            by_model[model_short] = {"input": 0, "output": 0, "cost": 0.0, "calls": 0}
        by_model[model_short]["input"] += actual_input
        by_model[model_short]["output"] += out
        by_model[model_short]["cost"] += cost
        by_model[model_short]["calls"] += 1
        
        # Daily breakdown
        ts = entry.get("timestamp") or msg.get("timestamp") or ""
        if ts:
            day = str(ts)[:10]
            if len(day) == 10 and day[4] == "-":
                if day not in by_day:
                    by_day[day] = {"input": 0, "output": 0, "cost": 0.0}
                by_day[day]["input"] += actual_input
                by_day[day]["output"] += out
                by_day[day]["cost"] += cost
                # Per-model-day
                if model_short not in by_model_day:
                    by_model_day[model_short] = {}
                if day not in by_model_day[model_short]:
                    by_model_day[model_short][day] = {"input": 0, "output": 0}
                by_model_day[model_short][day]["input"] += actual_input
                by_model_day[model_short][day]["output"] += out
    
    try:
        # Dynamically discover all agent directories
        if agents_root.exists():
            for agent_dir in sorted(agents_root.iterdir()):
                if not agent_dir.is_dir():
                    continue
                agent_id = agent_dir.name
                sessions_dir = agent_dir / "sessions"
                if not sessions_dir.exists():
                    continue
                
                agent_sessions = 0
                for jsonl_file in sessions_dir.glob("*.jsonl"):
                    session_count += 1
                    agent_sessions += 1
                    try:
                        for line in jsonl_file.read_text(encoding="utf-8", errors="ignore").splitlines():
                            if not line.strip():
                                continue
                            try:
                                entry = json.loads(line)
                            except json.JSONDecodeError:
                                continue
                            parse_usage_entry(entry, agent_id)
                    except Exception:
                        continue
                
                # Track session count per agent
                if agent_id in by_agent:
                    by_agent[agent_id]["sessions"] = agent_sessions
                elif agent_sessions > 0:
                    by_agent[agent_id] = {"input": 0, "output": 0, "cost": 0.0, "calls": 0, "sessions": agent_sessions}
    except Exception as e:
        logging.error(f"Token parsing error: {e}")
    
    # Aggregate by month
    by_month_agg = {}
    for d, v in by_day.items():
        m = d[:7]
        if m not in by_month_agg:
            by_month_agg[m] = {"input": 0, "output": 0, "cost": 0.0}
        by_month_agg[m]["input"] += v["input"]
        by_month_agg[m]["output"] += v["output"]
        by_month_agg[m]["cost"] += v["cost"]

    total_tokens = total_input + total_output
    days_active = max(len(by_day), 1)
    daily_avg_cost = total_cost / days_active if total_cost > 0 else 0
    
    return {
        "budget": 50.00,
        "budget_used": round(total_cost, 2),
        "total_spent": round(total_cost, 2),
        "daily_avg": round(daily_avg_cost, 2),
        "projected_monthly": round(daily_avg_cost * 30, 2),
        "total_tokens": total_tokens,
        "input_tokens": total_input,
        "output_tokens": total_output,
        "sessions_parsed": session_count,
        "days_active": days_active,
        "by_model": [
            {"model": k, "input": v["input"], "output": v["output"], "cost": round(v["cost"], 4), "calls": v["calls"]}
            for k, v in sorted(by_model.items(), key=lambda x: x[1]["input"] + x[1]["output"], reverse=True)
        ],
        "by_day": [
            {"date": k, "input": v["input"], "output": v["output"], "cost": round(v["cost"], 4)}
            for k, v in sorted(by_day.items())
        ],
        "by_provider": [
            {"provider": k, "cost": round(v["cost"], 2), "tokens": v["input"] + v["output"]}
            for k, v in sorted(by_model.items(), key=lambda x: x[1]["input"] + x[1]["output"], reverse=True)
        ],
        "by_agent": [
            {
                "agent": agent_id,
                "cost": round(data["cost"], 2),
                "tokens": data["input"] + data["output"],
                "input": data["input"],
                "output": data["output"],
                "calls": data["calls"],
                "sessions": data["sessions"],
            }
            for agent_id, data in sorted(by_agent.items(), key=lambda x: x[1]["input"] + x[1]["output"], reverse=True)
        ],
        "by_model_day": {
            model: [{"date": d, "tokens": v["input"] + v["output"]} for d, v in sorted(days.items())]
            for model, days in by_model_day.items()
        },
        "by_month": [
            {"month": k, "input": v["input"], "output": v["output"], "cost": round(v["cost"], 4)}
            for k, v in sorted(by_month_agg.items())
        ],
    }


# ──────────────────────────────────────────────
# API — Agents
# ──────────────────────────────────────────────

@app.get("/api/agents")
async def api_agents():
    docs = []
    for name in ["MEMORY.md", "SOUL.md", "USER.md", "AGENTS.md", "TOOLS.md"]:
        p = WORKSPACE_DIR / name
        if p.exists():
            docs.append({"name": name, "size": p.stat().st_size, "path": str(p)})

    # Try reading openclaw.json for real agent config
    agents_list = []
    role_map = {
        "main": ("main (boybot)", "Manager · Orchestration"),
        "trading-ops": ("trading-ops", "Execution & Monitoring"),
        "architect": ("architect", "Code Review Gate"),
        "qa": ("qa", "Dev & Testing"),
        "strategy": ("strategy", "Trading Logic & Signals"),
        "travel": ("travel", "Travel Research"),
        "vita": ("vita", "Vita's Agent"),
        "image": ("image", "Image Processing"),
        "accountant": ("accountant", "Receipt Filing"),
        "finance": ("finance", "Financial Analyst"),
    }
    openclaw_cfg = Path(r"C:\Users\moltbot\.openclaw\openclaw.json")
    if openclaw_cfg.exists():
        try:
            cfg = json.loads(openclaw_cfg.read_text())
            agents_data = cfg.get("agents", {})
            agent_list_raw = agents_data.get("list", []) if isinstance(agents_data, dict) else agents_data
            for agent in agent_list_raw:
                aid = agent.get("id", "unknown")
                display_name, role = role_map.get(aid, (aid, aid))
                model_cfg = agent.get("model", {})
                if isinstance(model_cfg, dict):
                    model = model_cfg.get("primary", "default")
                else:
                    model = str(model_cfg) or "default"
                # Strip provider prefix for display
                model = model.replace("github-copilot/", "")
                if model == "default":
                    # Inherit from defaults
                    defaults_model = agents_data.get("defaults", {}).get("model", {}).get("primary", "claude-opus-4.6")
                    model = defaults_model.replace("github-copilot/", "")
                agents_list.append({
                    "name": display_name,
                    "role": role,
                    "model": model,
                    "status": "active",
                })
        except Exception:
            pass

    if not agents_list:
        agents_list = [
            {"name": "main (boybot)", "role": "Manager", "model": "claude-opus-4.6", "status": "active"},
            {"name": "trading-ops", "role": "Trading Operations", "model": "gpt-5.2-codex", "status": "active"},
            {"name": "architect", "role": "Code Review & Architecture", "model": "gpt-5.2-codex", "status": "active"},
            {"name": "qa", "role": "Quality Assurance", "model": "gpt-5.2-codex", "status": "active"},
            {"name": "strategy", "role": "Trading Strategy", "model": "claude-opus-4.6", "status": "active"},
            {"name": "travel", "role": "Travel Agent", "model": "claude-haiku-4.5", "status": "active"},
            {"name": "vita", "role": "Health Agent", "model": "claude-opus-4.6", "status": "active"},
            {"name": "image", "role": "Image Processing", "model": "gpt-5-mini", "status": "dormant"},
            {"name": "accountant", "role": "Receipt Filing", "model": "gpt-4o-mini", "status": "active"},
        ]

    return {
        "org": {
            "owner": {"name": "Snake", "role": "Owner"},
            "manager": {"name": "boybot", "role": "Manager", "model": "claude-opus-4.6"},
            "agents": agents_list,
        },
        "documents": docs,
    }


# ──────────────────────────────────────────────
# API — Schedule (Cron)
# ──────────────────────────────────────────────

@app.get("/api/schedule")
async def api_schedule():
    jobs = get_cron_jobs()
    # Normalize format from openclaw cron list --json
    result = []
    if isinstance(jobs, list):
        for j in jobs:
            sched = j.get("schedule", {})
            expr = sched.get("expr", j.get("schedule", ""))
            if isinstance(expr, dict):
                expr = expr.get("expr", "")
            tz = sched.get("tz", "") if isinstance(sched, dict) else ""
            result.append({
                "id": j.get("id", ""),
                "name": j.get("name", "Unknown"),
                "schedule": f"{expr} ({tz})" if tz else str(expr),
                "enabled": j.get("enabled", True),
                "status": "Active" if j.get("enabled", True) else "Disabled",
                "agent": j.get("agentId", "main"),
                "lastStatus": j.get("state", {}).get("lastStatus", ""),
                "lastDuration": j.get("state", {}).get("lastDurationMs", 0),
                "nextRun": j.get("state", {}).get("nextRunAtMs", 0),
            })
    if not result:
        result = [
            {"name": "Trading Workflow", "schedule": "*/30 9-11 * * 1-5 (ET)", "status": "Active", "agent": "main", "id": "e8c37a2a"},
            {"name": "Exit Monitor", "schedule": "*/5 9-15 * * 1-5 (ET)", "status": "Active", "agent": "main", "id": "34aec0ca"},
            {"name": "Healthcheck Day", "schedule": "*/15 8-22 * * * (ET)", "status": "Active", "agent": "main", "id": "94021f90"},
            {"name": "Healthcheck Night", "schedule": "0 23,0-7 * * * (ET)", "status": "Active", "agent": "main", "id": "ed4eecfd"},
            {"name": "Backup", "schedule": "0 */4 * * * (ET)", "status": "Active", "agent": "main", "id": "c5ea3f7b"},
        ]
    return result


# ──────────────────────────────────────────────
# API — Journal (Trading Journal with reasoning)
# ──────────────────────────────────────────────

JOURNAL_DIR = PROJECT_ROOT / "trading_logs" / "journals"

@app.get("/api/journal")
async def api_journal():
    """List all journal entries (trading + memory)."""
    entries = []

    # Trading journal entries (generated)
    if JOURNAL_DIR.exists():
        for jp in sorted(JOURNAL_DIR.glob("*.json"), reverse=True):
            try:
                data = json.loads(jp.read_text())
                regime = data.get("market_regime", "Unknown")
                trades_count = len(data.get("trades_executed", []))
                blocked = data.get("trades_blocked", 0)
                portfolio = data.get("portfolio_snapshot", {})
                equity = portfolio.get("equity", 0) if portfolio else 0
                entries.append({
                    "date": data["date"],
                    "type": "trading",
                    "preview": f"Regime: {regime} | Trades: {trades_count} | Blocked: {blocked} | Equity: ${equity:,.0f}",
                    "tags": [regime, f"{trades_count} trades", f"{blocked} blocked"],
                    "has_narrative": bool(data.get("narrative")),
                })
            except Exception:
                pass

    # Memory entries (daily logs)
    dates = list_memory_dates()
    trading_dates = {e["date"] for e in entries}
    for d in dates:
        path = MEMORY_DIR / f"{d}.md"
        content = read_file_safe(path)
        lines = content.strip().split("\n")
        preview = lines[0][:120] if lines else ""
        tags = [l[3:].strip() for l in lines if l.startswith("## ")]
        entries.append({
            "date": d,
            "type": "memory",
            "preview": preview,
            "tags": tags[:5],
            "size": len(content),
            "has_trading": d in trading_dates,
        })

    # Sort by date descending
    entries.sort(key=lambda x: x["date"], reverse=True)
    return entries


@app.get("/api/journal/{date}")
async def api_journal_entry(date: str):
    """Get journal entry — trading journal + memory notes."""
    result = {"date": date}

    # Trading journal
    json_path = JOURNAL_DIR / f"{date}.json"
    if json_path.exists():
        try:
            result["trading"] = json.loads(json_path.read_text())
        except Exception:
            pass

    md_path = JOURNAL_DIR / f"{date}.md"
    if md_path.exists():
        result["trading_narrative"] = read_file_safe(md_path)

    # Memory notes
    mem_path = MEMORY_DIR / f"{date}.md"
    if mem_path.exists():
        result["memory"] = read_file_safe(mem_path)

    if len(result) == 1:
        return JSONResponse({"error": "Not found"}, 404)
    return result


@app.post("/api/journal/generate/{date}")
async def api_journal_generate(date: str):
    """Generate/regenerate a trading journal entry for a given date."""
    try:
        result = subprocess.run(
            [str(PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"),
             str(PROJECT_ROOT / "scripts" / "generate_journal.py"), date],
            capture_output=True, text=True, timeout=30,
            cwd=str(PROJECT_ROOT),
        )
        if result.returncode != 0:
            return JSONResponse({"error": result.stderr}, 500)
        # Return the generated entry
        json_path = JOURNAL_DIR / f"{date}.json"
        if json_path.exists():
            return json.loads(json_path.read_text())
        return {"status": "generated", "output": result.stdout}
    except Exception as e:
        return JSONResponse({"error": str(e)}, 500)


# ──────────────────────────────────────────────
# API — 2nd Brain (Knowledge)
# ──────────────────────────────────────────────

@app.get("/api/brain")
async def api_brain(q: str = ""):
    entries = []

    # Parse knowledge.md
    km = MEMORY_DIR / "knowledge.md"
    if km.exists():
        content = read_file_safe(km)
        # Split by ## headings
        sections = re.split(r'\n## ', content)
        for sec in sections[1:]:  # skip header
            lines = sec.strip().split("\n")
            title = lines[0].strip()
            body = "\n".join(lines[1:]).strip()
            tags = ["Knowledge"]
            if "trading" in title.lower() or "trading" in body.lower():
                tags.append("Trading")
            entries.append({
                "title": title,
                "body": body[:300],
                "tags": tags,
                "source": "knowledge.md"
            })

    # Parse places.json
    pj = MEMORY_DIR / "places.json"
    if pj.exists():
        try:
            places = json.loads(read_file_safe(pj))
            if isinstance(places, list):
                for p in places:
                    name = p.get("name", "Unknown")
                    entries.append({
                        "title": name,
                        "body": json.dumps(p, indent=2)[:300],
                        "tags": ["Places", "Notes"],
                        "source": "places.json"
                    })
            elif isinstance(places, dict):
                for k, v in places.items():
                    entries.append({
                        "title": k,
                        "body": json.dumps(v, indent=2)[:300] if isinstance(v, (dict, list)) else str(v)[:300],
                        "tags": ["Places", "Notes"],
                        "source": "places.json"
                    })
        except Exception:
            pass

    # Filter by query
    if q:
        ql = q.lower()
        entries = [e for e in entries if ql in e["title"].lower() or ql in e["body"].lower()]

    return entries


# ──────────────────────────────────────────────
# API — Receipts / Finance
# ──────────────────────────────────────────────

@app.get("/api/receipts")
async def api_receipts(start: str = "", end: str = "", category: str = ""):
    """Return receipts from CSV with optional date/category filters."""
    import csv as _csv
    if not RECEIPTS_CSV.exists():
        return {"receipts": [], "summary": {}}
    rows = []
    with open(RECEIPTS_CSV, encoding="utf-8") as f:
        for i, r in enumerate(_csv.DictReader(f)):
            r["_idx"] = i
            rows.append(r)
    # filters
    if start:
        rows = [r for r in rows if r.get("date", "") >= start]
    if end:
        rows = [r for r in rows if r.get("date", "") <= end]
    if category:
        rows = [r for r in rows if r.get("category", "").lower() == category.lower()]
    # build summary
    total_spent = 0
    total_hst = 0
    total_gst = 0
    total_tax = 0
    total_tip = 0
    by_category = {}
    by_month = {}
    by_store = {}
    for r in rows:
        amt = 0
        try:
            amt = float(r.get("total", 0))
        except (ValueError, TypeError):
            pass
        total_spent += amt
        # Accumulate tax columns
        for field, accum_name in [("tax", "total_tax"), ("hst", "total_hst"), ("gst", "total_gst"), ("tip", "total_tip")]:
            try:
                val = float(r.get(field, 0) or 0)
            except (ValueError, TypeError):
                val = 0
            if field == "tax":
                total_tax += val
            elif field == "hst":
                total_hst += val
            elif field == "gst":
                total_gst += val
            elif field == "tip":
                total_tip += val
        cat = r.get("category", "Uncategorized") or "Uncategorized"
        by_category[cat] = round(by_category.get(cat, 0) + amt, 2)
        month = r.get("date", "")[:7]
        if month:
            by_month[month] = round(by_month.get(month, 0) + amt, 2)
        store = r.get("store", "Unknown") or "Unknown"
        by_store[store] = round(by_store.get(store, 0) + amt, 2)
    # top stores
    top_stores = sorted(by_store.items(), key=lambda x: -x[1])[:15]
    # sort months
    sorted_months = sorted(by_month.items())
    categories = sorted(by_category.keys())
    return {
        "receipts": rows,
        "summary": {
            "total_spent": round(total_spent, 2),
            "total_tax": round(total_tax, 2),
            "total_hst": round(total_hst, 2),
            "total_gst": round(total_gst, 2),
            "total_tip": round(total_tip, 2),
            "receipt_count": len(rows),
            "by_category": dict(sorted(by_category.items(), key=lambda x: -x[1])),
            "by_month": dict(sorted_months),
            "top_stores": [{"store": s, "total": t} for s, t in top_stores],
            "categories": categories,
        }
    }


@app.put("/api/receipts/{row_index}/category")
async def update_receipt_category(row_index: int, body: dict):
    """Update a receipt's category in the CSV by row index."""
    import csv as _csv
    new_category = body.get("category", "").strip()
    if not new_category:
        return JSONResponse({"error": "Category required"}, 400)
    if not RECEIPTS_CSV.exists():
        return JSONResponse({"error": "CSV not found"}, 404)

    # Read all rows
    with open(RECEIPTS_CSV, encoding="utf-8") as f:
        reader = _csv.DictReader(f)
        fieldnames = reader.fieldnames
        rows = list(reader)

    if row_index < 0 or row_index >= len(rows):
        return JSONResponse({"error": f"Invalid row index {row_index}"}, 400)

    old_category = rows[row_index].get("category", "")
    rows[row_index]["category"] = new_category

    # Write back
    with open(RECEIPTS_CSV, "w", encoding="utf-8", newline="") as f:
        writer = _csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    return {"ok": True, "row": row_index, "old": old_category, "new": new_category}


# ──────────────────────────────────────────────
# API — Reports (kept for backwards compat)
# ──────────────────────────────────────────────

@app.get("/api/reports")
async def api_reports():
    if not REVIEWS_DIR.exists():
        return []
    return [{"date": f.stem, "filename": f.name, "size": f.stat().st_size}
            for f in sorted(REVIEWS_DIR.glob("*.md"), reverse=True)]


@app.get("/api/reports/{date}")
async def api_report_detail(date: str):
    path = REVIEWS_DIR / f"{date}.md"
    if not path.exists():
        return JSONResponse({"error": "Not found"}, 404)
    return {"date": date, "raw": read_file_safe(path)}


# ──────────────────────────────────────────────
# Backtest Results API
# ──────────────────────────────────────────────

BACKTEST_RESULTS = PROJECT_ROOT / "backtest_results.json"
SWING_BACKTEST_RESULTS = PROJECT_ROOT / "swing_backtest_results.json"

@app.get("/api/backtest")
async def api_backtest():
    if not BACKTEST_RESULTS.exists():
        return {"status": "no_results", "message": "No backtest results yet. Run the backtester first."}
    try:
        data = json.loads(BACKTEST_RESULTS.read_text(encoding="utf-8"))
        return {"status": "ok", **data}
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.get("/api/swing_backtest")
async def api_swing_backtest():
    if not SWING_BACKTEST_RESULTS.exists():
        return {"status": "no_results", "message": "No swing backtest results yet. Run scripts/backtest_swing.py first."}
    try:
        data = json.loads(SWING_BACKTEST_RESULTS.read_text(encoding="utf-8"))
        return {"status": "ok", **data}
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.post("/api/backtest/run")
async def api_backtest_run(request: Request):
    """Trigger a backtest run (async)."""
    body = await request.json() if request.headers.get("content-type") == "application/json" else {}
    days = body.get("days", 30)
    step = body.get("step", 0.05)
    script = PROJECT_ROOT / "scripts" / "backtest_weights.py"
    if not script.exists():
        return JSONResponse({"error": "Backtest script not found"}, 404)
    # Run in background
    proc = await asyncio.create_subprocess_exec(
        "python", str(script), "--days", str(days), "--step", str(step),
        cwd=str(PROJECT_ROOT),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    return {"status": "started", "pid": proc.pid, "days": days, "step": step}


# ──────────────────────────────────────────────
# WebSocket — Live Updates
# ──────────────────────────────────────────────

@app.websocket("/ws/feed")
async def ws_feed(websocket: WebSocket):
    await websocket.accept()
    try:
        while True:
            p = read_portfolio()
            positions = p.get("positions", {})
            pos_list = []
            for sym, data in positions.items():
                if isinstance(data, dict):
                    pos_list.append({
                        "symbol": sym,
                        "qty": data.get("quantity", 0),
                        "pnl": round(float(data.get("unrealized_pl", 0)), 2),
                        "current": round(float(data.get("current_price", 0)), 2),
                    })
            await websocket.send_json({
                "type": "status_update",
                "equity": round(p.get("equity", 0), 2),
                "cash": round(p.get("cash", 0), 2),
                "buying_power": round(p.get("buying_power", 0), 2),
                "positions": len(positions),
                "positions_detail": pos_list,
                "source": p.get("source", "unknown"),
            })
            await asyncio.sleep(10)
    except Exception:
        pass
    finally:
        try:
            await websocket.close()
        except Exception:
            pass
