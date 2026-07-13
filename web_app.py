import json
import os
import threading
import time
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from api import handle_api
from core import init_db


HOST = os.environ.get("APP_HOST", "127.0.0.1")
START_PORT = int(os.environ.get("APP_PORT", "8765"))
OPEN_BROWSER = os.environ.get("OPEN_BROWSER", "1") == "1"
ALLOW_SHUTDOWN = os.environ.get("ALLOW_SHUTDOWN", "1") == "1"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
STATIC_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
}


class LocalWebHandler(BaseHTTPRequestHandler):
    server_version = "WeightedSelectionLocal/1.0"

    def log_message(self, fmt, *args):
        return

    def send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path in ("/", "/index.html"):
            path = os.path.join(STATIC_DIR, "index.html")
        elif parsed.path.startswith("/static/"):
            name = parsed.path.removeprefix("/static/")
            if "/" in name or "\\" in name or not name:
                self.send_error(404)
                return
            path = os.path.join(STATIC_DIR, name)
        else:
            self.send_error(404)
            return

        if not os.path.isfile(path):
            self.send_error(404)
            return

        with open(path, "rb") as f:
            body = f.read()
        content_type = STATIC_TYPES.get(os.path.splitext(path)[1], "application/octet-stream")
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/shutdown":
            if not ALLOW_SHUTDOWN:
                self.send_json({"ok": False, "error": "サーバー上では終了操作を利用できません。"}, status=403)
                return
            self.send_json({"ok": True})
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return

        try:
            result = handle_api(parsed.path, self.headers, self.rfile)
            self.send_json(result)
        except KeyError:
            self.send_error(404)
        except Exception as exc:
            traceback.print_exc()
            self.send_json({"ok": False, "error": str(exc)}, status=400)


def make_server():
    for port in range(START_PORT, START_PORT + 20):
        try:
            return ThreadingHTTPServer((HOST, port), LocalWebHandler)
        except OSError:
            continue
    raise RuntimeError("利用可能なローカルポートが見つかりません。")


def main():
    init_db()
    server = make_server()
    url = f"http://{HOST}:{server.server_port}/"
    print(f"Local web app: {url}", flush=True)
    if OPEN_BROWSER:
        threading.Thread(target=lambda: (time.sleep(0.4), webbrowser.open(url)), daemon=True).start()
    try:
        server.serve_forever()
    finally:
        server.server_close()
        print("Local web app stopped.", flush=True)


if __name__ == "__main__":
    main()
