import os
import socket
import threading
import time
import webbrowser
from contextlib import asynccontextmanager

import uvicorn
from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from core import init_db
from fastapi_routes import router


HOST = os.environ.get("APP_HOST", "127.0.0.1")
START_PORT = int(os.environ.get("APP_PORT", "8765"))
OPEN_BROWSER = os.environ.get("OPEN_BROWSER", "1") == "1"
ALLOW_SHUTDOWN = os.environ.get("ALLOW_SHUTDOWN", "1") == "1"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")


@asynccontextmanager
async def lifespan(_app):
    init_db()
    yield


app = FastAPI(
    title="Google form抽選ツール",
    version="2.0.0",
    lifespan=lifespan,
)
app.include_router(router)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.middleware("http")
async def disable_browser_cache(request: Request, call_next):
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    return response


@app.exception_handler(ValueError)
async def value_error_handler(_request: Request, exc: ValueError):
    return JSONResponse(
        status_code=400,
        content={"ok": False, "error": str(exc)},
    )


@app.get("/", include_in_schema=False)
async def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


@app.get("/index.html", include_in_schema=False)
async def index_html():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


def request_server_stop():
    server = getattr(app.state, "server", None)
    if server is not None:
        server.should_exit = True


@app.post("/api/shutdown", include_in_schema=False)
async def shutdown(background_tasks: BackgroundTasks):
    if not ALLOW_SHUTDOWN:
        return JSONResponse(
            status_code=403,
            content={"ok": False, "error": "サーバー上では終了操作を利用できません。"},
        )
    background_tasks.add_task(request_server_stop)
    return {"ok": True}


def find_available_port():
    for port in range(START_PORT, START_PORT + 20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind((HOST, port))
            except OSError:
                continue
            return port
    raise RuntimeError("利用可能なローカルポートが見つかりません。")


def main():
    port = find_available_port()
    url_host = "127.0.0.1" if HOST == "0.0.0.0" else HOST
    url = f"http://{url_host}:{port}/"
    config = uvicorn.Config(app, host=HOST, port=port, log_level="info")
    server = uvicorn.Server(config)
    app.state.server = server
    print(f"FastAPI app: {url}", flush=True)
    if OPEN_BROWSER:
        threading.Thread(
            target=lambda: (time.sleep(0.5), webbrowser.open(url)),
            daemon=True,
        ).start()
    server.run()


if __name__ == "__main__":
    main()
