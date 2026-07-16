import base64
import datetime
import hashlib
import os
import secrets
import sqlite3
from dataclasses import dataclass
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse

from core import db_path
from .request_context import set_request_identity


AUTHORIZE_URL = "https://x.com/i/oauth2/authorize"
TOKEN_URL = "https://api.x.com/2/oauth2/token"
CURRENT_USER_URL = "https://api.x.com/2/users/me"


def _enabled(name, default=False):
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _csv_set(name):
    return {
        value.strip().lower()
        for value in os.environ.get(name, "").split(",")
        if value.strip()
    }


@dataclass(frozen=True)
class AuthSettings:
    required: bool
    client_id: str
    client_secret: str
    redirect_uri: str
    session_secret: str
    cookie_secure: bool
    allowed_user_ids: set
    allowed_usernames: set

    @classmethod
    def from_env(cls):
        client_id = os.environ.get("X_CLIENT_ID", "").strip()
        required = _enabled("AUTH_REQUIRED", bool(client_id))
        redirect_uri = os.environ.get(
            "X_REDIRECT_URI", "http://127.0.0.1:8765/auth/callback"
        ).strip()
        session_secret = os.environ.get("SESSION_SECRET", "").strip()
        if required:
            missing = []
            if not client_id:
                missing.append("X_CLIENT_ID")
            if not redirect_uri:
                missing.append("X_REDIRECT_URI")
            if not session_secret:
                missing.append("SESSION_SECRET")
            if missing:
                raise RuntimeError(
                    "X login is enabled, but these settings are missing: "
                    + ", ".join(missing)
                )
        return cls(
            required=required,
            client_id=client_id,
            client_secret=os.environ.get("X_CLIENT_SECRET", "").strip(),
            redirect_uri=redirect_uri,
            session_secret=session_secret or secrets.token_urlsafe(48),
            cookie_secure=_enabled("COOKIE_SECURE", redirect_uri.startswith("https://")),
            allowed_user_ids=_csv_set("ALLOWED_X_USER_IDS"),
            allowed_usernames=_csv_set("ALLOWED_X_USERNAMES"),
        )


class AccountAuthService:
    def __init__(self, settings):
        self.settings = settings

    @staticmethod
    def _challenge(verifier):
        digest = hashlib.sha256(verifier.encode("ascii")).digest()
        return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")

    @staticmethod
    def _now():
        return datetime.datetime.now().isoformat(timespec="seconds")

    @staticmethod
    def _redirect_login(error="", message=""):
        query = urlencode({key: value for key, value in {
            "error": error, "message": message
        }.items() if value})
        return RedirectResponse(f"/login{f'?{query}' if query else ''}", status_code=303)

    def _finish_login(self, request, user):
        request.session.clear()
        request.session["auth_user"] = user
        return RedirectResponse("/", status_code=303)

    async def require_user(self, request: Request):
        user = request.session.get("auth_user")
        guest_id = request.session.get("guest_id")
        if self.settings.required and not user and not guest_id:
            guest_id = secrets.token_urlsafe(18)
            request.session["guest_id"] = guest_id
        if user:
            account_id = user.get("accountId") or f"x:{user['id']}"
            set_request_identity(account_id, guest=False)
        elif guest_id:
            set_request_identity(f"guest:{guest_id}", guest=True)
        else:
            set_request_identity("local", guest=False)

    async def require_saved_account(self, request: Request):
        await self.require_user(request)
        if request.session.get("guest_id"):
            raise HTTPException(status_code=403, detail="ゲストモードでは保存機能を利用できません。")

    async def require_guest(self, request: Request):
        await self.require_user(request)
        if request.session.get("auth_user"):
            raise HTTPException(status_code=403, detail="テストデータは未ログイン時のみ利用できます。")
        if not request.session.get("guest_id"):
            request.session["guest_id"] = secrets.token_urlsafe(18)
            await self.require_user(request)

    def start_x_login(self, request: Request):
        if not self.settings.required:
            return RedirectResponse("/")
        state = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(64)
        request.session["oauth_state"] = state
        request.session["oauth_verifier"] = verifier
        query = urlencode({
            "response_type": "code",
            "client_id": self.settings.client_id,
            "redirect_uri": self.settings.redirect_uri,
            "scope": "tweet.read users.read",
            "state": state,
            "code_challenge": self._challenge(verifier),
            "code_challenge_method": "S256",
        })
        return RedirectResponse(f"{AUTHORIZE_URL}?{query}")

    async def x_callback(self, request: Request, code=None, state=None, error=None):
        import httpx

        if error:
            raise HTTPException(status_code=400, detail=f"X authorization failed: {error}")
        expected_state = request.session.pop("oauth_state", None)
        verifier = request.session.pop("oauth_verifier", None)
        if not code or not state or not expected_state or not secrets.compare_digest(state, expected_state):
            raise HTTPException(status_code=400, detail="ログイン情報を確認できませんでした。")
        if not verifier:
            raise HTTPException(status_code=400, detail="ログインの有効期限が切れました。")

        token_data = {
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": self.settings.redirect_uri,
            "code_verifier": verifier,
        }
        auth = None
        if self.settings.client_secret:
            auth = httpx.BasicAuth(self.settings.client_id, self.settings.client_secret)
        else:
            token_data["client_id"] = self.settings.client_id

        async with httpx.AsyncClient(timeout=15.0) as client:
            token_response = await client.post(TOKEN_URL, data=token_data, auth=auth)
            if token_response.is_error:
                raise HTTPException(status_code=502, detail="Xのアクセストークンを取得できませんでした。")
            access_token = token_response.json().get("access_token")
            if not access_token:
                raise HTTPException(status_code=502, detail="Xからアクセストークンが返されませんでした。")
            user_response = await client.get(
                CURRENT_USER_URL,
                params={"user.fields": "profile_image_url"},
                headers={"Authorization": f"Bearer {access_token}"},
            )
            if user_response.is_error:
                raise HTTPException(status_code=502, detail="Xのユーザー情報を取得できませんでした。")
            user = user_response.json().get("data") or {}

        self._check_x_allowlist(user)
        return self._finish_login(request, self._save_x_user(user))

    def _check_x_allowlist(self, user):
        if not self.settings.allowed_user_ids and not self.settings.allowed_usernames:
            return
        user_id = str(user.get("id", "")).lower()
        username = str(user.get("username", "")).lower()
        if (
            user_id not in self.settings.allowed_user_ids
            and username not in self.settings.allowed_usernames
        ):
            raise HTTPException(status_code=403, detail="このXアカウントには利用権限がありません。")

    def _save_x_user(self, user):
        user_id = str(user.get("id", "")).strip()
        username = str(user.get("username", "")).strip()
        if not user_id or not username:
            raise HTTPException(status_code=502, detail="Xのユーザー情報が不足しています。")
        now = self._now()
        conn = sqlite3.connect(db_path())
        conn.execute("""
            INSERT INTO auth_users (
                x_user_id, username, display_name, profile_image_url,
                first_login_at, last_login_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(x_user_id) DO UPDATE SET
                username=excluded.username,
                display_name=excluded.display_name,
                profile_image_url=excluded.profile_image_url,
                last_login_at=excluded.last_login_at
        """, (
            user_id,
            username,
            str(user.get("name", "")).strip(),
            str(user.get("profile_image_url", "")).strip(),
            now,
            now,
        ))
        conn.commit()
        conn.close()
        return {
            "id": user_id,
            "accountId": f"x:{user_id}",
            "provider": "x",
            "username": username,
            "name": str(user.get("name", "")).strip(),
            "profileImageUrl": str(user.get("profile_image_url", "")).strip(),
        }

    def logout(self, request: Request):
        request.session.clear()
        return RedirectResponse("/", status_code=303)

    def start_guest(self, request: Request):
        request.session.clear()
        request.session["guest_id"] = secrets.token_urlsafe(18)
        return RedirectResponse("/", status_code=303)

    def me(self, request: Request):
        user = request.session.get("auth_user")
        guest = bool(request.session.get("guest_id"))
        return {
            "ok": True,
            "required": self.settings.required,
            "loginAvailable": self.settings.required,
            "xAvailable": self.settings.required,
            "authenticated": bool(user),
            "guest": guest,
            "user": user,
        }


settings = AuthSettings.from_env()
auth_service = AccountAuthService(settings)
auth_router = APIRouter()


@auth_router.get("/auth/login", include_in_schema=False)
async def login_page_redirect():
    return RedirectResponse("/login", status_code=303)


@auth_router.get("/auth/x/login", include_in_schema=False)
async def x_login(request: Request):
    return auth_service.start_x_login(request)


@auth_router.get("/auth/callback", include_in_schema=False)
async def callback(request: Request, code: str = None, state: str = None, error: str = None):
    return await auth_service.x_callback(request, code, state, error)


@auth_router.get("/auth/logout", include_in_schema=False)
async def logout(request: Request):
    return auth_service.logout(request)


@auth_router.get("/auth/guest", include_in_schema=False)
async def guest(request: Request):
    return auth_service.start_guest(request)


@auth_router.get("/api/auth/me", include_in_schema=False)
async def me(request: Request):
    return auth_service.me(request)
