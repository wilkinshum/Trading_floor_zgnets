"""Standalone script to run the Daily Review Agent."""
import sys
from pathlib import Path
import yaml

# Add project root to path
project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root / "src"))

from trading_floor.agents.daily_review import DailyReviewAgent


def main():
    config_path = project_root / "configs" / "workflow.yaml"
    with open(config_path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    db_path = cfg.get("logging", {}).get("db_path", "trading.db")
    agent = DailyReviewAgent(cfg, db_path=db_path)

    # Allow passing a date argument: python run_daily_review.py 2026-02-13
    date_str = sys.argv[1] if len(sys.argv) > 1 else None
    result = agent.run(date_str)

    print(f"Daily Review complete for {result['date']}")
    print(f"  Trades today: {result['today_trades']}")
    print(f"  Trades (30d): {result['recent_trades']}")
    print(f"  30d metrics: {result['metrics_30d']}")
    print(f"  Config updated: {result['config_updated']}")
    print(f"  Report: {result['report_path']}")


if __name__ == "__main__":
    main()
