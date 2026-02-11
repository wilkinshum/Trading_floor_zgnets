import csv
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EVENTS = ROOT / "trading_logs" / "events.csv"
TRADES = ROOT / "trading_logs" / "trades.csv"
OUT_JSON = ROOT / "web" / "report.json"


def read_last_row(path: Path):
    if not path.exists():
        return None
    with path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.reader(f)
        header = next(reader, None)
        if not header:
            return None
        last = None
        for row in reader:
            if not row:
                continue
            if len(row) > len(header):
                if "plan_notes" not in header and len(row) == len(header) + 1:
                    header = header + ["plan_notes"]
                else:
                    header = header + [f"extra_{i}" for i in range(1, len(row) - len(header) + 1)]
            last = row
        if last is None:
            return None
        last = last + [""] * max(0, len(header) - len(last))
        return dict(zip(header, last))


def read_trades(path: Path):
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        return [row for row in reader if row]


def main():
    payload = {
        "timestamp": "—",
        "status": "No runs yet",
        "plans": [],
        "notes": [],
    }

    last = read_last_row(EVENTS)
    if last:
        payload["timestamp"] = str(last.get("timestamp", "—"))
        payload["status"] = (
            f"Risk: {last.get('risk_ok', False)} | "
            f"Compliance: {last.get('compliance_ok', False)} | "
            f"Approval: {last.get('approval_granted', False)}"
        )
        notes = []
        for k in ["risk_notes", "compliance_notes", "plan_notes"]:
            val = last.get(k)
            if val:
                notes.append(str(val))
        payload["notes"] = notes

    trades = read_trades(TRADES)
    if trades:
        last_ts = payload["timestamp"]
        recent = [t for t in trades if t.get("timestamp") == last_ts] if last_ts != "—" else trades[-5:]
        payload["plans"] = [
            {
                "symbol": r.get("symbol"),
                "side": r.get("side"),
                "score": float(r.get("score")) if r.get("score") else 0.0,
            }
            for r in recent
        ]

    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(payload), encoding="utf-8")
    print(f"Wrote {OUT_JSON}")


if __name__ == "__main__":
    main()
