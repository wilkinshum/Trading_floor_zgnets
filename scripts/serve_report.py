import http.server
import os
import socketserver
import subprocess
import threading
import time
from pathlib import Path

from generate_report import main as generate

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"
LOGS_DIR = ROOT / "logs" / "watchdog"

PORT = 8000
REFRESH_SECONDS = 30
LOG_TAIL_LINES = 200


def tail_lines(path: Path, max_lines: int) -> list[str]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", errors="replace") as f:
        lines = f.readlines()
    return lines[-max_lines:]


def read_openclaw_logs(max_lines: int) -> list[str]:
    try:
        proc = subprocess.run(
            ["openclaw", "logs", "--limit", str(max_lines), "--plain"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        output = (proc.stdout or "") + (proc.stderr or "")
        if not output.strip():
            return ["(no openclaw logs)\n"]
        return [line + "\n" for line in output.splitlines()]
    except Exception as e:
        return [f"(openclaw logs unavailable: {e})\n"]


def write_combined_logs():
    sections = [
        ("report", LOGS_DIR / "report.log"),
        ("mission-control", LOGS_DIR / "mission-control.log"),
    ]
    parts = []
    for name, path in sections:
        parts.append(f"===== {name} ({path}) =====\n")
        lines = tail_lines(path, LOG_TAIL_LINES)
        parts.extend(lines if lines else ["(no logs yet)\n"])
        parts.append("\n")

    parts.append("===== openclaw (gateway logs) =====\n")
    parts.extend(read_openclaw_logs(LOG_TAIL_LINES))
    parts.append("\n")

    (WEB / "logs.txt").write_text("".join(parts), encoding="utf-8")


def refresher():
    while True:
        try:
            generate()
            write_combined_logs()
        except Exception as e:
            print(f"[report] generate error: {e}")
        time.sleep(REFRESH_SECONDS)


def run_server():
    handler = http.server.SimpleHTTPRequestHandler
    with socketserver.TCPServer(("", PORT), handler) as httpd:
        print(f"Serving on http://localhost:{PORT}")
        httpd.serve_forever()


def main():
    # ensure we serve from web directory
    os_cwd = Path.cwd()
    try:
        WEB.mkdir(parents=True, exist_ok=True)
        # initial generate
        generate()
        # start refresher thread
        t = threading.Thread(target=refresher, daemon=True)
        t.start()
        # serve from web dir
        import os
        os.chdir(str(WEB))
        run_server()
    finally:
        try:
            os.chdir(str(os_cwd))
        except Exception:
            pass


if __name__ == "__main__":
    main()
