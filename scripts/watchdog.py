import os
import sys
import time
import json
import signal
import subprocess
from pathlib import Path
from urllib.request import urlopen
from urllib.error import URLError

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = REPO_ROOT.parent
MC_ROOT = WORKSPACE / "mission-control"

LOG_DIR = REPO_ROOT / "logs" / "watchdog"
LOG_DIR.mkdir(parents=True, exist_ok=True)

STOP_FILE = REPO_ROOT / "watchdog.stop"
STATE_FILE = LOG_DIR / "watchdog_state.json"

SERVICES = {
    "report": {
        "name": "report",
        "cmd": [sys.executable, str(REPO_ROOT / "scripts" / "serve_report.py")],
        "cwd": str(REPO_ROOT),
        "url": "http://127.0.0.1:8000/report.json",
        "log": LOG_DIR / "report.log",
    },
    "mission_control": {
        "name": "mission_control",
        "cmd": ["npm", "run", "dev"],
        "cwd": str(MC_ROOT),
        "url": "http://127.0.0.1:3000",
        "log": LOG_DIR / "mission-control.log",
    },
}

CHECK_INTERVAL = 10
HEALTH_TIMEOUT = 3
RESTART_GRACE = 2


class Managed:
    def __init__(self, spec):
        self.spec = spec
        self.proc = None
        self.last_start = None

    def start(self):
        logf = open(self.spec["log"], "a", encoding="utf-8")
        self.proc = subprocess.Popen(
            self.spec["cmd"],
            cwd=self.spec["cwd"],
            stdout=logf,
            stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
        )
        self.last_start = time.time()

    def stop(self):
        if not self.proc or self.proc.poll() is not None:
            return
        try:
            if os.name == "nt":
                self.proc.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                self.proc.terminate()
        except Exception:
            pass

    def running(self):
        return self.proc is not None and self.proc.poll() is None


def healthy(url: str) -> bool:
    try:
        with urlopen(url, timeout=HEALTH_TIMEOUT) as resp:
            return 200 <= resp.status < 500
    except URLError:
        return False


def write_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def main():
    managed = {key: Managed(spec) for key, spec in SERVICES.items()}

    for svc in managed.values():
        svc.start()
        time.sleep(1)

    while True:
        if STOP_FILE.exists():
            for svc in managed.values():
                svc.stop()
            break

        state = {"services": {}, "timestamp": time.time()}
        for key, svc in managed.items():
            spec = svc.spec
            ok = svc.running() and healthy(spec["url"])
            if not ok:
                svc.stop()
                time.sleep(RESTART_GRACE)
                svc.start()
            state["services"][key] = {
                "running": svc.running(),
                "url": spec["url"],
                "last_start": svc.last_start,
            }

        write_state(state)
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
