"""Update floor_board.md for QA.

This repo/worktree doesn't ship with an updater, so we implement a simple one:
- Alpaca PAPER is the source of truth for positions + open orders.
- Writes a small "AUTO (Alpaca)" section into floor_board.md between markers.

Safe behavior:
- If markers don't exist, they are appended.
- Does NOT attempt to change any "plan" text outside the auto section.

Env vars:
- ALPACA_API_KEY / ALPACA_API_SECRET (preferred)
- APCA_API_KEY_ID / APCA_API_SECRET_KEY (fallback)
- ALPACA_BASE_URL (default paper)

Usage:
  .venv\\Scripts\\python.exe scripts\\update_floor_board.py
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
BOARD = ROOT / "floor_board.md"

START = "<!-- AUTO:ALPACA_START -->"
END = "<!-- AUTO:ALPACA_END -->"


def alpaca_get(base: str, key: str, secret: str, path: str, params: dict | None = None):
    h = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}
    r = requests.get(f"{base}{path}", headers=h, params=params, timeout=20)
    r.raise_for_status()
    return r.json()


def render_auto_section(base: str, key: str, secret: str) -> str:
    acct = alpaca_get(base, key, secret, "/v2/account")
    positions = alpaca_get(base, key, secret, "/v2/positions")
    orders = alpaca_get(base, key, secret, "/v2/orders", params={"status": "open", "limit": 200})

    ts = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")

    lines: list[str] = []
    lines.append("## AUTO (Alpaca PAPER — source of truth)")
    lines.append(f"Last refresh: **{ts}**")
    lines.append("")
    lines.append(
        f"Account: equity={acct.get('equity')} cash={acct.get('cash')} buying_power={acct.get('buying_power')} status={acct.get('status')}"
    )
    lines.append("")

    lines.append("### Live Positions")
    if not positions:
        lines.append("- None")
    else:
        lines.append("| Symbol | Side | Qty | Avg Entry | Market | Unreal PnL | Unreal % |")
        lines.append("|---|---:|---:|---:|---:|---:|---:|")
        for p in positions:
            qty = float(p.get("qty", 0) or 0)
            side = "LONG" if qty >= 0 else "SHORT"
            sym = p.get("symbol", "")
            avg = p.get("avg_entry_price")
            mkt = p.get("current_price")
            upl = p.get("unrealized_pl")
            uplpc = p.get("unrealized_plpc")
            try:
                uplpc_s = f"{float(uplpc) * 100:+.2f}%"
            except Exception:
                uplpc_s = str(uplpc)
            lines.append(f"| {sym} | {side} | {qty:.0f} | {avg} | {mkt} | {upl} | {uplpc_s} |")

    lines.append("")
    lines.append("### Open Orders")
    if not orders:
        lines.append("- None")
    else:
        lines.append("| Symbol | Type | Side | Qty | Status | Limit | Stop |")
        lines.append("|---|---|---|---:|---|---:|---:|")
        for o in orders:
            lines.append(
                "| {sym} | {typ} | {side} | {qty} | {status} | {limit} | {stop} |".format(
                    sym=o.get("symbol"),
                    typ=o.get("type"),
                    side=o.get("side"),
                    qty=o.get("qty"),
                    status=o.get("status"),
                    limit=o.get("limit_price"),
                    stop=o.get("stop_price"),
                )
            )

    return "\n".join(lines).rstrip() + "\n"


def upsert_section(text: str, section: str) -> str:
    if START in text and END in text:
        pre = text.split(START)[0]
        post = text.split(END)[1]
        return pre.rstrip() + "\n\n" + START + "\n" + section + END + "\n" + post.lstrip()
    # append
    return text.rstrip() + "\n\n" + START + "\n" + section + END + "\n"


def main() -> None:
    base = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
    key = os.getenv("ALPACA_API_KEY") or os.getenv("APCA_API_KEY_ID")
    secret = os.getenv("ALPACA_API_SECRET") or os.getenv("APCA_API_SECRET_KEY")
    if not key or not secret:
        raise SystemExit("Missing Alpaca creds for board update")

    if not BOARD.exists():
        BOARD.write_text("# floor_board\n\n", encoding="utf-8")

    current = BOARD.read_text(encoding="utf-8")
    section = render_auto_section(base, key, secret)
    updated = upsert_section(current, section)
    BOARD.write_text(updated, encoding="utf-8")
    print(f"Updated {BOARD}")


if __name__ == "__main__":
    main()
