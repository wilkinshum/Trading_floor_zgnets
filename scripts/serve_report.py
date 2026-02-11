import http.server
import socketserver
import threading
import time
from pathlib import Path

from generate_report import main as generate

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"

PORT = 8000
REFRESH_SECONDS = 30


def refresher():
    while True:
        try:
            generate()
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
