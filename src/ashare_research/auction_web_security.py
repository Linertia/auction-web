"""Access controls shared by REST and Streamable HTTP; no strategy logic."""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import threading
import time
from collections import OrderedDict
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx

from .auction_query import AuctionQueryError


@dataclass(frozen=True)
class Principal:
    subject: str
    servers: frozenset[str]


principal_context: ContextVar[Principal | None] = ContextVar("auction_principal", default=None)


class BearerAuth:
    def __init__(self, users: dict):
        if not users:
            raise ValueError("AUTH_CONFIG_INVALID: 必须配置身份验证")
        for name, user in users.items():
            if (not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name)
                or not re.fullmatch(r"[0-9a-f]{64}", user.get("sha256", ""))
                or not isinstance(user.get("servers"), list) or not user["servers"]
                or any(not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", key) for key in user["servers"])):
                raise ValueError("AUTH_CONFIG_INVALID: 身份白名单无效")
        self.users = users

    def verify(self, token: str) -> Principal:
        digest = hashlib.sha256(token.encode()).hexdigest()
        for name, user in self.users.items():
            if hmac.compare_digest(digest, user["sha256"]):
                if user.get("expires_at", float("inf")) <= time.time():
                    break
                return Principal(name, frozenset(user["servers"]))
        raise AuctionQueryError("UNAUTHORIZED: 身份验证失败")


class JWTAuth:
    """Resource server only. Authorization/login/PKCE is delegated to external IdP."""
    def __init__(self, issuer: str, audience: str, jwks_url: str, grants: dict):
        from jwt import PyJWKClient
        if not issuer.startswith("https://") or not jwks_url.startswith("https://") or not audience or not grants:
            raise ValueError("AUTH_CONFIG_INVALID: OAuth资源服务器配置不完整")
        self.issuer, self.audience, self.grants = issuer, audience, grants
        self.keys = PyJWKClient(jwks_url, cache_keys=True, timeout=3)

    def verify(self, token: str) -> Principal:
        import jwt
        try:
            key = self.keys.get_signing_key_from_jwt(token).key
            claims = jwt.decode(token, key, algorithms=["RS256"], issuer=self.issuer,
                audience=self.audience, options={"require": ["exp", "iat", "sub", "iss", "aud"]})
            subject = claims["sub"]
            if "auction:read" not in claims.get("scope", "").split() or subject not in self.grants:
                raise ValueError()
            return Principal(subject, frozenset(self.grants[subject]))
        except Exception:
            raise AuctionQueryError("UNAUTHORIZED: 身份验证失败") from None


class MemoryRateLimit:
    """Local testing only; distributed deployment must use RedisRateLimit."""
    def __init__(self, maximum: int = 60):
        self.maximum = maximum
        self.rows = OrderedDict()
        self.lock = threading.Lock()

    def check(self, subject: str):
        key = (subject, int(time.time()) // 60)
        with self.lock:
            self.rows[key] = self.rows.get(key, 0) + 1
            self.rows.move_to_end(key)
            while len(self.rows) > 4096:
                self.rows.popitem(last=False)
            if self.rows[key] > self.maximum:
                raise AuctionQueryError("RATE_LIMITED: 每用户每分钟请求超过限制")


class RedisControls:
    """Fixed Redis REST endpoint from server environment, not exposed as a tool."""
    def __init__(self, url: str, token: str, maximum: int = 60):
        from urllib.parse import urlsplit
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.username or parsed.password or not parsed.hostname:
            raise ValueError("REDIS_CONFIG_INVALID")
        self.url, self.token, self.maximum = url.rstrip("/"), token, maximum

    def command(self, args: list):
        try:
            response = httpx.post(self.url, json=args, headers={"Authorization":f"Bearer {self.token}"},
                timeout=3, follow_redirects=False)
            response.raise_for_status()
            payload = response.json()
            if "error" in payload:
                raise ValueError()
            return payload["result"]
        except Exception:
            raise AuctionQueryError("CONTROL_UNAVAILABLE: 分布式访问控制不可用") from None

    def check(self, subject: str):
        key = "auction:rate:" + hashlib.sha256(subject.encode()).hexdigest() + ":" + str(int(time.time()) // 60)
        count = self.command(["EVAL", "local n=redis.call('INCR',KEYS[1]); if n==1 then redis.call('EXPIRE',KEYS[1],120) end; return n", 1, key])
        if int(count) > self.maximum:
            raise AuctionQueryError("RATE_LIMITED: 每用户每分钟请求超过限制")

    def emit(self, event: dict):
        self.command(["XADD", "auction:audit", "MAXLEN", "~", 100000, "*", "event",
                      json.dumps(event, ensure_ascii=True)])


class AuditLog:
    def __init__(self, redis: RedisControls | None = None):
        self.redis = redis
        self.lock = threading.Lock()

    def emit(self, event: dict):
        with self.lock:
            if self.redis:
                try:
                    self.redis.emit(event)
                except AuctionQueryError:
                    raise AuctionQueryError("AUDIT_UNAVAILABLE: 审计不可用，拒绝返回数据") from None
            else:
                # Structured server logs. No body, arguments, token, raw subject or URL.
                try:
                    print(json.dumps(event, ensure_ascii=True), flush=True)
                except OSError:
                    raise AuctionQueryError("AUDIT_UNAVAILABLE: 审计不可用，拒绝返回数据") from None


def audit_event(request_id: str, status: str, subject: str = "", **fields):
    return {"timestamp":datetime.now(timezone.utc).isoformat(), "request_id":request_id,
            "status":status, "subject_sha256":hashlib.sha256(subject.encode()).hexdigest(), **fields}
