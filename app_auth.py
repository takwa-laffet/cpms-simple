"""Authentication utilities for CPMS backend."""
import hashlib
import hmac
import logging
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel

from app_config import (
    AUTH_COOKIE_NAME,
    AUTH_EMAIL,
    AUTH_PASSWORD,
    AUTH_SECRET_KEY,
    AUTH_SESSION_TTL_SECONDS,
    USERS_FILE,
)

LOGGER = logging.getLogger("cpms")


class LoginPayload(BaseModel):
    email: str
    password: str


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def current_unix_timestamp() -> int:
    return int(datetime.now(timezone.utc).timestamp())


def hash_password(password: str) -> str:
    return hashlib.sha256(f"{AUTH_SECRET_KEY}:{password}".encode("utf-8")).hexdigest()


def _auth_signature(payload: str) -> str:
    digest = hmac.new(AUTH_SECRET_KEY.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).digest()
    return __import__("base64").urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def build_auth_cookie_value(email: str) -> str:
    issued_at = str(current_unix_timestamp())
    payload = f"{email}:{issued_at}"
    return f"{payload}:{_auth_signature(payload)}"


def verify_auth_cookie_value(cookie_value: str | None) -> str | None:
    if not cookie_value:
        return None

    try:
        email, issued_at_text, signature = cookie_value.rsplit(":", 2)
    except ValueError:
        return None

    payload = f"{email}:{issued_at_text}"
    expected_signature = _auth_signature(payload)
    if not hmac.compare_digest(signature, expected_signature):
        return None

    try:
        issued_at = int(issued_at_text)
    except ValueError:
        return None

    if current_unix_timestamp() - issued_at > AUTH_SESSION_TTL_SECONDS:
        return None

    user = get_user_by_email(email)
    if user is None or not user.get("active", True):
        return None

    return user.get("email")


def read_json_file(file_path: Path, default):
    if not file_path.exists():
        return default

    try:
        import json
        data = json.loads(file_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return default

    return data if data is not None else default


def read_users() -> list[dict]:
    import json
    users = read_json_file(USERS_FILE, [])
    if not isinstance(users, list):
        users = []

    # Ensure seed admin user exists
    if not any(isinstance(user, dict) and user.get("email") == AUTH_EMAIL for user in users):
        users.append({
            "id": "admin-1",
            "email": AUTH_EMAIL,
            "password_hash": hash_password(AUTH_PASSWORD),
            "role": "admin",
            "name": "Rback Admin",
            "active": True,
            "created_at": utc_now_iso(),
        })
        USERS_FILE.write_text(json.dumps(users, indent=2, ensure_ascii=False), encoding="utf-8")

    return users


def write_users(users: list[dict]) -> None:
    import json
    USERS_FILE.write_text(json.dumps(users, indent=2, ensure_ascii=False), encoding="utf-8")


def public_user(user: dict) -> dict:
    return {
        "id": user.get("id"),
        "email": user.get("email"),
        "role": user.get("role", "operator"),
        "name": user.get("name") or user.get("email"),
        "active": bool(user.get("active", True)),
        "created_at": user.get("created_at"),
    }


def get_user_by_email(email: str) -> dict | None:
    normalized_email = email.strip().lower()
    for user in read_users():
        if str(user.get("email", "")).strip().lower() == normalized_email:
            return user
    return None


def authenticate_user(email: str, password: str) -> dict | None:
    user = get_user_by_email(email)
    if user is None or not user.get("active", True):
        return None

    if not hmac.compare_digest(str(user.get("password_hash", "")), hash_password(password)):
        return None

    return user


from fastapi import HTTPException, Request, status


def get_authenticated_email(request: Request) -> str | None:
    return verify_auth_cookie_value(request.cookies.get(AUTH_COOKIE_NAME))


def require_authenticated_email(request: Request) -> str:
    email = get_authenticated_email(request)
    if email is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    return email


from fastapi.responses import JSONResponse, RedirectResponse


def build_auth_response(email: str, wants_json: bool, redirect_url: str = "/cityos"):
    if wants_json:
        response = JSONResponse({"status": "ok", "email": email})
    else:
        response = RedirectResponse(url=redirect_url, status_code=status.HTTP_303_SEE_OTHER)

    response.set_cookie(
        AUTH_COOKIE_NAME,
        build_auth_cookie_value(email),
        httponly=True,
        samesite="lax",
        secure=False,
        max_age=AUTH_SESSION_TTL_SECONDS,
        path="/",
    )
    return response


def get_authenticated_user(request: Request) -> dict | None:
    email = get_authenticated_email(request)
    if email is None:
        return None
    user = get_user_by_email(email)
    if user is None or not user.get("active", True):
        return None
    return user


def require_authenticated_user(request: Request) -> dict:
    user = get_authenticated_user(request)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    return user


def require_role(request: Request, allowed_roles: set[str]) -> dict:
    user = require_authenticated_user(request)
    if user.get("role") not in allowed_roles:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient permissions")
    return user