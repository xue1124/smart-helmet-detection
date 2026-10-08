# -*- coding: utf-8 -*-
"""Authentication: login, JWT-like token, middleware helpers."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from datetime import datetime
from typing import Any, Dict, Optional

from sqlalchemy.orm import Session

from services.common.datetime_format import format_dt

from services.persistence.models.user import User
from services.auth.passwords import verify_password

TOKEN_COOKIE = "sc_token"
TOKEN_TTL_HOURS = int(os.environ.get("AUTH_TOKEN_HOURS", "168"))


def _auth_secret() -> bytes:
    secret = (os.environ.get("AUTH_SECRET") or "").strip()
    if not secret:
        secret = "smart-construction-dev-secret-change-me"
    return secret.encode("utf-8")


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64url_decode(data: str) -> bytes:
    pad = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + pad)


def create_access_token(user_id: int, username: str, role: str) -> str:
    payload = {
        "uid": user_id,
        "sub": username,
        "role": role,
        "exp": int(time.time()) + TOKEN_TTL_HOURS * 3600,
    }
    body = _b64url_encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    sig = hmac.new(_auth_secret(), body.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{body}.{sig}"


def decode_access_token(token: str) -> Optional[Dict[str, Any]]:
    if not token or "." not in token:
        return None
    body, sig = token.rsplit(".", 1)
    expected = hmac.new(_auth_secret(), body.encode("ascii"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig):
        return None
    try:
        payload = json.loads(_b64url_decode(body))
    except (json.JSONDecodeError, ValueError):
        return None
    exp = int(payload.get("exp") or 0)
    if exp < time.time():
        return None
    return payload


def authenticate_user(db: Session, username: str, password: str) -> Optional[User]:
    name = (username or "").strip()
    if not name or not password:
        return None
    user = db.query(User).filter(User.username == name).first()
    if not user or not user.enabled:
        return None
    if not verify_password(password, user.password_hash):
        return None
    user.last_login_at = datetime.now()
    db.commit()
    db.refresh(user)
    return user


def get_user_by_id(db: Session, user_id: int) -> Optional[User]:
    return db.query(User).filter(User.id == user_id).first()


def user_to_dict(user: User) -> dict:
    return {
        "id": user.id,
        "username": user.username,
        "role": user.role,
        "last_login_at": format_dt(user.last_login_at),
    }


def is_public_api_path(path: str) -> bool:
    if path == "/api/v1/health":
        return True
    if path == "/api/v1/auth/login":
        return True
    return False
