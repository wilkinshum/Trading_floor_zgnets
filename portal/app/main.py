import logging
from fastapi import FastAPI, WebSocket, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
import aiosqlite
import json
import asyncio
from pathlib import Path

# --- Config ---
BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR.parent / "trading.db"
# Ensure absolute path resolution for DB
if not DB_PATH.exists():
    # Fallback try relative to CWD if running from root
    POSSIBLE_DB = Path("Trading_floor_zgnets/trading.db").resolve()
    if POSSIBLE_DB.exists():
        DB_PATH = POSSIBLE_DB

PORTFOLIO_PATH = BASE_DIR.parent / "portfolio.json"

app = FastAPI(title="ZG Nets Trading Portal")

# --- Middleware ---
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Static & Templates ---
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

# --- Database Helper ---
async def get_db_trades():
    if not DB_PATH.exists():
        return []
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM trades ORDER BY timestamp DESC LIMIT 50") as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

async def get_db_signals():
    if not DB_PATH.exists():
        return []
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM signals ORDER BY timestamp DESC LIMIT 50") as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

# --- Routes ---

@app.get("/")
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

@app.get("/api/status")
async def api_status():
    portfolio = {}
    if PORTFOLIO_PATH.exists():
        try:
            portfolio = json.loads(PORTFOLIO_PATH.read_text())
        except:
            pass
    return {
        "status": "online",
        "portfolio": portfolio,
        "db_connected": DB_PATH.exists()
    }

@app.get("/api/trades")
async def api_trades():
    return await get_db_trades()

@app.get("/api/signals")
async def api_signals():
    return await get_db_signals()

@app.get("/api/equity_history")
async def api_equity_history():
    """Compute running equity from trade PnL history."""
    if not DB_PATH.exists():
        return []
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT timestamp, pnl FROM trades ORDER BY timestamp ASC") as cursor:
            rows = await cursor.fetchall()
    
    # Build equity curve from cumulative PnL
    starting_equity = 5000.0
    equity = starting_equity
    curve = [{"timestamp": "start", "equity": starting_equity}]
    for row in rows:
        r = dict(row)
        pnl = r.get("pnl") or 0.0
        equity += pnl
        curve.append({"timestamp": r["timestamp"], "equity": round(equity, 2)})
    return curve

@app.get("/report.json")
async def report_json():
    """Dummy endpoint to stop 404 spam from old cached pages."""
    return {}

# --- Daily Reports ---
REVIEWS_DIR = BASE_DIR.parent / "trading_logs" / "daily_reviews"

@app.get("/reports")
async def reports_page(request: Request):
    return templates.TemplateResponse("reports.html", {"request": request})

@app.get("/api/reports")
async def api_reports():
    """List all daily review reports (newest first)."""
    if not REVIEWS_DIR.exists():
        return []
    reports = []
    for f in sorted(REVIEWS_DIR.glob("*.md"), reverse=True):
        reports.append({
            "date": f.stem,
            "filename": f.name,
            "size": f.stat().st_size,
        })
    return reports

@app.get("/api/reports/{date}")
async def api_report_detail(date: str):
    """Get a single daily report parsed into sections."""
    path = REVIEWS_DIR / f"{date}.md"
    if not path.exists():
        return {"error": "Not found"}
    raw = path.read_text(encoding="utf-8")
    
    # Parse markdown into sections
    sections = []
    current_title = ""
    current_lines = []
    for line in raw.split("\n"):
        if line.startswith("## "):
            if current_title or current_lines:
                sections.append({"title": current_title, "content": "\n".join(current_lines).strip()})
            current_title = line[3:].strip()
            current_lines = []
        elif line.startswith("# ") and not current_title:
            current_title = ""
            current_lines = []
        else:
            current_lines.append(line)
    if current_title or current_lines:
        sections.append({"title": current_title, "content": "\n".join(current_lines).strip()})
    
    return {"date": date, "raw": raw, "sections": sections}

# --- WebSocket Feed ---
@app.websocket("/ws/feed")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    logger = logging.getLogger("portal")
    try:
        while True:
            portfolio = {}
            if PORTFOLIO_PATH.exists():
                try:
                    portfolio = json.loads(PORTFOLIO_PATH.read_text())
                except Exception:
                    pass
            
            try:
                await websocket.send_json({
                    "type": "status_update",
                    "portfolio": portfolio
                })
            except Exception:
                break  # Client disconnected, exit loop cleanly
            await asyncio.sleep(5)
    except Exception as e:
        logger.debug(f"WebSocket closed: {e}")
    finally:
        try:
            await websocket.close()
        except Exception:
            pass
