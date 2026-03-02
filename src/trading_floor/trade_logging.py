import csv
from pathlib import Path


class TradeLogger:
    def __init__(self, cfg):
        self.trades_csv = Path(cfg["logging"]["trades_csv"])
        self.events_csv = Path(cfg["logging"]["events_csv"])

    def log_event(self, row: dict):
        self._append(self.events_csv, row)

    def log_trade(self, row: dict):
        self._append(self.trades_csv, row)

    def _append(self, path: Path, row: dict):
        path.parent.mkdir(parents=True, exist_ok=True)
        exists = path.exists()
        with open(path, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=list(row.keys()))
            if not exists:
                writer.writeheader()
            writer.writerow(row)
