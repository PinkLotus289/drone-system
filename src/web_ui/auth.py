"""Аутентификация по общему код-паролю (одна система — много операторов).

Без внешних зависимостей: подписанный HMAC-токен в HttpOnly-cookie.
Токен несёт {op: позывной, sid: id сессии, exp: срок}. Проверяется на каждом
запросе middleware'ом (см. main.py). HTTPS обязателен в проде (COOKIE_SECURE).
"""
from __future__ import annotations
import base64
import hashlib
import hmac
import json
import secrets
import time
from typing import Optional

COOKIE_NAME = "skybite_session"


def _b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def new_sid() -> str:
    """Уникальный id сессии операторского клиента (для арбитража управления)."""
    return secrets.token_hex(8)


def sign_token(payload: dict, secret: str) -> str:
    body = _b64e(json.dumps(payload, separators=(",", ":")).encode())
    sig = _b64e(hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest())
    return f"{body}.{sig}"


def verify_token(token: str, secret: str) -> Optional[dict]:
    """Возвращает payload, если подпись валидна и срок не истёк, иначе None."""
    try:
        body, sig = token.split(".", 1)
        exp_sig = _b64e(hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(sig, exp_sig):
            return None
        payload = json.loads(_b64d(body))
        if float(payload.get("exp", 0)) < time.time():
            return None
        return payload
    except Exception:
        return None


def make_session_token(operator: str, secret: str, ttl_hours: int) -> tuple[str, str]:
    """Создаёт токен сессии. Возвращает (token, sid)."""
    sid = new_sid()
    payload = {
        "op": (operator or "operator").strip()[:40] or "operator",
        "sid": sid,
        "iat": int(time.time()),
        "exp": int(time.time()) + ttl_hours * 3600,
    }
    return sign_token(payload, secret), sid


def operator_from_cookies(cookies: dict, secret: str) -> Optional[dict]:
    """Достаёт и проверяет сессию из cookies (dict name->value)."""
    tok = cookies.get(COOKIE_NAME)
    if not tok:
        return None
    return verify_token(tok, secret)
