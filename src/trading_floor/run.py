from pathlib import Path
import yaml
from trading_floor.workflow import TradingFloor


def load_config(path: str):
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="configs/workflow.yaml")
    args = ap.parse_args()

    cfg = load_config(args.config)
    Path(cfg["logging"]["trades_csv"]).parent.mkdir(parents=True, exist_ok=True)
    Path(cfg["logging"]["events_csv"]).parent.mkdir(parents=True, exist_ok=True)

    tf = TradingFloor(cfg)
    tf.run()


if __name__ == "__main__":
    main()
