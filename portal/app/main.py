"""
OpenClaw Personal Dashboard — FastAPI Backend
Dark navy theme, 7 tabs: Overview, Trading, Tokens, Agents, Schedule, Journal, 2nd Brain
"""
import logging
import json
import asyncio
import re
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

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
    if PORTFOLIO_PATH.exists():
        try:
            return json.loads(PORTFOLIO_PATH.read_text())
        except Exception:
            pass
    return {"cash": 0, "equity": 0, "positions": {}}


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

    # Compute today's PnL from trades table
    today = datetime.now().strftime("%Y-%m-%d")
    today_trades = await db_query(
        "SELECT COALESCE(SUM(pnl),0) as total_pnl, COUNT(*) as cnt FROM trades WHERE timestamp LIKE ?",
        [f"{today}%"]
    )
    today_pnl = today_trades[0]["total_pnl"] if today_trades else 0

    # Win rate
    all_trades = await db_query("SELECT pnl FROM trades WHERE pnl IS NOT NULL")
    wins = sum(1 for t in all_trades if (t.get("pnl") or 0) > 0)
    total = len(all_trades)
    win_rate = round(wins / total * 100, 1) if total > 0 else 0

    return {
        "equity": round(equity, 2),
        "today_pnl": round(today_pnl, 2),
        "open_positions": len(positions),
        "win_rate": win_rate,
        "total_trades": total,
        "cash": round(p.get("cash", 0), 2),
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
            highest = data.get("highest_price", entry)
            result.append({
                "symbol": sym,
                "qty": qty,
                "entry": round(entry, 2),
                "highest": round(highest, 2),
                "current": 0,  # filled by frontend via live price
                "pnl": 0,
                "pnl_pct": 0,
            })
    return result


@app.get("/api/trades")
async def api_trades():
    return await db_query("SELECT * FROM trades ORDER BY timestamp DESC LIMIT 50")


@app.get("/api/equity_history")
async def api_equity_history():
    """
    Build equity curve from trades with actual realized PnL.
    Anchors backwards from current portfolio equity so the curve
    ends at the real number regardless of unlogged legacy losses.
    """
    rows = await db_query(
        "SELECT timestamp, pnl, side, symbol FROM trades WHERE pnl IS NOT NULL AND pnl != 0.0 ORDER BY timestamp ASC"
    )
    p = read_portfolio()
    current_equity = round(p.get("equity", 0) or p.get("cash", 0), 2)

    if not rows:
        return [
            {"timestamp": "start", "equity": 5000},
            {"timestamp": datetime.now().isoformat(), "equity": current_equity}
        ]

    # Anchor: work backwards from current equity
    total_realized_pnl = sum(r.get("pnl", 0) or 0 for r in rows)
    starting = round(current_equity - total_realized_pnl, 2)

    equity = starting
    curve = [{"timestamp": "start", "equity": starting}]
    for r in rows:
        pnl = r.get("pnl") or 0.0
        equity += pnl
        curve.append({
            "timestamp": r["timestamp"],
            "equity": round(equity, 2),
            "trade": f"{r.get('side', '')} {r.get('symbol', '')} PnL:{pnl:+.2f}"
        })
    return curve


@app.get("/api/signals")
async def api_signals():
    return await db_query("SELECT * FROM signals ORDER BY timestamp DESC LIMIT 20")


# ──────────────────────────────────────────────
# API — Tokens (placeholder / mock)
# ──────────────────────────────────────────────

@app.get("/api/tokens")
async def api_tokens():
    """Token usage — parses real data from session transcripts."""
    sessions_dir = Path(r"C:\Users\moltbot\.openclaw\agents\main\sessions")
    
    total_input = 0
    total_output = 0
    total_cost = 0.0
    by_model = {}
    by_day = {}
    by_model_day = {}  # model -> day -> {input, output}
    session_count = 0
    
    try:
        for jsonl_file in sessions_dir.glob("*.jsonl"):
            session_count += 1
            try:
                for line in jsonl_file.read_text(encoding="utf-8", errors="ignore").splitlines():
                    if not line.strip():
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    
                    # Usage is nested: entry.message.usage
                    msg = entry.get("message") or {}
                    usage = msg.get("usage") or {}
                    inp = usage.get("input") or usage.get("inputTokens") or usage.get("prompt_tokens") or 0
                    out = usage.get("output") or usage.get("outputTokens") or usage.get("completion_tokens") or 0
                    cache_read = usage.get("cacheRead") or 0
                    cost_obj = usage.get("cost") or {}
                    cost = cost_obj.get("total", 0) if isinstance(cost_obj, dict) else (cost_obj or 0)
                    
                    if inp or out or usage.get("totalTokens"):
                        # For Claude, 'input' is delta only; real input = totalTokens - output
                        total_toks = usage.get("totalTokens") or 0
                        if total_toks > 0 and total_toks > (inp + cache_read + out):
                            actual_input = total_toks - out
                        else:
                            actual_input = inp + cache_read
                        
                        total_input += actual_input
                        total_output += out
                        total_cost += cost
                        
                        model = msg.get("model") or entry.get("model") or "unknown"
                        # Simplify model name
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
            except Exception:
                continue
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
            {"agent": "boybot (main)", "cost": round(total_cost * 0.7, 2), "tokens": int(total_tokens * 0.7)},
            {"agent": "travel", "cost": round(total_cost * 0.15, 2), "tokens": int(total_tokens * 0.15)},
            {"agent": "vita", "cost": round(total_cost * 0.15, 2), "tokens": int(total_tokens * 0.15)},
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
# WebSocket — Live Updates
# ──────────────────────────────────────────────

@app.websocket("/ws/feed")
async def ws_feed(websocket: WebSocket):
    await websocket.accept()
    try:
        while True:
            p = read_portfolio()
            await websocket.send_json({
                "type": "status_update",
                "equity": round(p.get("equity", 0), 2),
                "cash": round(p.get("cash", 0), 2),
                "positions": len(p.get("positions", {})),
            })
            await asyncio.sleep(5)
    except Exception:
        pass
    finally:
        try:
            await websocket.close()
        except Exception:
            pass
