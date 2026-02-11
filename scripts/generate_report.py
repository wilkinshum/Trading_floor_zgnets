import json
from pathlib import Path
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
EVENTS = ROOT / "trading_logs" / "events.csv"
TRADES = ROOT / "trading_logs" / "trades.csv"
OUT_JSON = ROOT / "web" / "report.json"


def main():
    payload = {
        "timestamp": "—",
        "status": "No runs yet",
        "plans": [],
        "notes": [],
    }

    if EVENTS.exists():
        df = pd.read_csv(EVENTS, on_bad_lines="skip")
        if not df.empty:
            last = df.iloc[-1]
            payload["timestamp"] = str(last.get("timestamp", "—"))
            payload["status"] = f"Risk: {last.get('risk_ok', False)} | Compliance: {last.get('compliance_ok', False)} | Approval: {last.get('approval_granted', False)}"
            notes = []
            for k in ["risk_notes", "compliance_notes", "plan_notes"]:
                if k in df.columns and pd.notna(last.get(k)):
                    notes.append(str(last.get(k)))
            payload["notes"] = notes

    if TRADES.exists():
        df = pd.read_csv(TRADES, on_bad_lines="skip")
        if not df.empty:
            last_ts = payload["timestamp"]
            recent = df[df["timestamp"] == last_ts] if last_ts != "—" else df.tail(5)
            payload["plans"] = [
                {
                    "symbol": r["symbol"],
                    "side": r["side"],
                    "score": float(r["score"]),
                }
                for _, r in recent.iterrows()
            ]

    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(payload), encoding="utf-8")
    print(f"Wrote {OUT_JSON}")


if __name__ == "__main__":
    main()
