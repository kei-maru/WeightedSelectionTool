import os
import secrets
import socket
import threading
import time
import webbrowser
from contextlib import asynccontextmanager

import uvicorn
from fastapi import BackgroundTasks, Depends, FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from core import init_db
from fastapi_routes import router
from services.auth_service import auth_router, auth_service, settings as auth_settings


HOST = os.environ.get("APP_HOST", "127.0.0.1")
START_PORT = int(os.environ.get("APP_PORT", "8765"))
OPEN_BROWSER = os.environ.get("OPEN_BROWSER", "1") == "1"
ALLOW_SHUTDOWN = os.environ.get("ALLOW_SHUTDOWN", "1") == "1"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
TEMPLATE_DIR = os.path.join(BASE_DIR, "templates")


@asynccontextmanager
async def lifespan(_app):
    init_db()
    yield


app = FastAPI(
    title="Google form抽選ツール",
    version="2.0.0",
    lifespan=lifespan,
    docs_url=None if auth_settings.required else "/docs",
    redoc_url=None,
    openapi_url=None if auth_settings.required else "/openapi.json",
)
app.add_middleware(
    SessionMiddleware,
    secret_key=auth_settings.session_secret,
    same_site="lax",
    https_only=auth_settings.cookie_secure,
    max_age=60 * 60 * 12,
)
app.include_router(auth_router)
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


@app.exception_handler(PermissionError)
async def permission_error_handler(_request: Request, exc: PermissionError):
    return JSONResponse(status_code=403, content={"ok": False, "error": str(exc)})


@app.get("/", include_in_schema=False)
async def index(request: Request):
    if auth_settings.required and not (
        request.session.get("auth_user") or request.session.get("guest_id")
    ):
        request.session["guest_id"] = secrets.token_urlsafe(18)
    return FileResponse(os.path.join(TEMPLATE_DIR, "index.html"))


@app.get("/index.html", include_in_schema=False)
async def index_html(request: Request):
    return await index(request)


@app.get("/login", include_in_schema=False)
async def login_page(request: Request):
    if not auth_settings.required:
        return RedirectResponse("/")
    if request.session.get("auth_user") or request.session.get("guest_id"):
        return RedirectResponse("/")
    return FileResponse(os.path.join(TEMPLATE_DIR, "login.html"))


def request_server_stop():
    server = getattr(app.state, "server", None)
    if server is not None:
        server.should_exit = True


@app.post(
    "/api/shutdown",
    include_in_schema=False,
    dependencies=[Depends(auth_service.require_user)],
)
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
