"""REST and stateless Streamable HTTP over the existing read-only core."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import threading
import uuid
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from contextvars import ContextVar
from pathlib import Path

from fastapi import FastAPI, Request
from starlette.responses import HTMLResponse, JSONResponse
from mcp.server.transport_security import TransportSecuritySettings

from .auction_mcp import create_server
from .auction_query import AuctionRankingReader, AuctionQueryError
from .auction_snapshot_store import S3SnapshotStore, SupabaseSnapshotStore
from .auction_web_security import (
    AuditLog, BearerAuth, JWTAuth, MemoryRateLimit, RedisControls, Principal,
    principal_context, audit_event,
)
from .contracts.paths import get_paths

request_context = ContextVar("auction_request_id", default="")
PARAMETERS = {"trade_date", "server_id", "stage", "limit", "offset", "codes", "qualified_only"}
STATUS = {"UNAUTHORIZED":401, "FORBIDDEN":403, "RATE_LIMITED":429, "QUERY_TIMEOUT":504,
          "QUERY_BUSY":503, "DATA_MISSING":404, "CODE_NOT_FOUND":404, "SNAPSHOT_BUSY":409,
          "AUDIT_UNAVAILABLE":503, "CONTROL_UNAVAILABLE":503, "SOURCE_UNAVAILABLE":503}
STATUS.update({"ACCOUNT_EXISTS":409, "ACCOUNT_LIMIT":409, "ACCOUNT_MISSING":404})


def error_response(code: str, request_id: str, status: int | None = None):
    headers = {"Cache-Control":"no-store", "X-Request-ID":request_id}
    if code == "RATE_LIMITED":
        headers["Retry-After"] = "60"
    return JSONResponse({"error":{"code":code, "request_id":request_id}},
                        status_code=status or STATUS.get(code, 400), headers=headers)


class WebQueryService:
    def __init__(self, reader, audit, timeout=8.0):
        self.reader, self.audit, self.timeout = reader, audit, timeout
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="auction-query")
        self.slots = threading.BoundedSemaphore(2)

    async def execute(self, trade_date: str, server_id: str = "local", stage="final",
                      limit: int = 30, offset: int = 0, codes: list[str] | None = None,
                      qualified_only: bool = False, *, display: bool = False) -> dict:
        principal = principal_context.get()
        rid = request_context.get() or uuid.uuid4().hex
        args = [trade_date, server_id, stage, limit, offset, codes, qualified_only]
        digest = hashlib.sha256(json.dumps(args, sort_keys=True).encode()).hexdigest()
        result, error = None, None
        try:
            if principal is None:
                raise AuctionQueryError("UNAUTHORIZED: 身份验证失败")
            if server_id not in principal.servers:
                raise AuctionQueryError("FORBIDDEN: 无权访问指定服务器")
            if not self.slots.acquire(blocking=False):
                raise AuctionQueryError("QUERY_BUSY: 查询繁忙，请稍后重试")
            try:
                if display:
                    from .auction_display import read_display
                    future = self.pool.submit(read_display, self.reader, *args)
                else:
                    future = self.pool.submit(self.reader.rank_auction_candidates, *args)
            except Exception:
                self.slots.release()
                raise
            future.add_done_callback(lambda _: self.slots.release())
            # Timed-out read can finish, but outstanding work stays bounded at two.
            result = await asyncio.wait_for(asyncio.shield(asyncio.wrap_future(future)), self.timeout)
        except asyncio.TimeoutError:
            error = AuctionQueryError("QUERY_TIMEOUT: 查询超过超时限制")
        except AuctionQueryError as exc:
            error = exc
        except Exception:
            error = AuctionQueryError("QUERY_FAILED: 查询失败")
        status = str(error).split(":",1)[0] if error else "ok"
        await asyncio.to_thread(self.audit.emit, audit_event(rid, status,
            principal.subject if principal else "", tool="rank_auction_candidates",
            arguments_sha256=digest, source_sha256=result["source_sha256"] if result else None,
            returned_rows=len(result["rows"]) if result else 0))
        if error:
            raise error
        return {**result, "request_id":rid}


class AccessBoundary:
    def __init__(self, app, auth, limiter, audit, origins, public_url="", sessions=None):
        self.app, self.auth, self.limiter, self.audit = app, auth, limiter, audit
        self.origins, self.public_url = frozenset(origins), public_url.rstrip("/")
        self.sessions = sessions

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = scope["path"]
        if path == "/" or path.startswith("/.well-known/oauth-protected-resource"):
            return await self.app(scope, receive, send)
        rid = uuid.uuid4().hex
        login_paths = ("/api/auth/login", "/api/auth/logout", "/api/auth/me", "/api/auth/accounts")
        if path not in ("/api/auction/rank", "/api/auction/view", "/mcp", "/mcp/") and not (
                self.sessions and path in login_paths):
            return await error_response("NOT_FOUND",rid,404)(scope,receive,send)
        headers = dict(scope.get("headers",[]))
        principal = None
        session_cookie = ""
        try:
            origin = headers.get(b"origin",b"").decode()
            if origin and origin not in self.origins:
                raise AuctionQueryError("FORBIDDEN: Origin未授权")
            auth = headers.get(b"authorization",b"").decode()
            if path == "/api/auth/login":
                if scope["method"] != "POST" or origin not in self.origins:
                    raise AuctionQueryError("FORBIDDEN: 登录请求必须来自本站")
                principal = Principal("anonymous-login", frozenset())
            elif auth.startswith("Bearer ") and len(auth) <= 4096:
                principal = await asyncio.to_thread(self.auth.verify, auth[7:])
            elif self.sessions and path not in ("/mcp", "/mcp/") and not auth:
                from http.cookies import SimpleCookie
                from .auction_login import COOKIE
                cookies = SimpleCookie()
                raw_cookie = headers.get(b"cookie", b"").decode()
                if len(raw_cookie) > 4096:
                    raise AuctionQueryError("UNAUTHORIZED: Cookie无效")
                cookies.load(raw_cookie)
                session_cookie = cookies[COOKIE].value if COOKIE in cookies else ""
                principal = await asyncio.to_thread(self.sessions.verify, session_cookie)
                if scope["method"] not in ("GET", "HEAD") and origin not in self.origins:
                    raise AuctionQueryError("FORBIDDEN: 会话请求必须来自本站")
            else:
                raise AuctionQueryError("UNAUTHORIZED: 需要身份验证")
            if path != "/api/auth/login":
                await asyncio.to_thread(self.limiter.check, principal.subject)
            if path == "/api/auth/accounts" and not self.sessions.is_admin(principal):
                raise AuctionQueryError("FORBIDDEN: 仅管理员可管理账号")
            # Bound actual body size, including chunked requests.
            body = bytearray()
            deadline = time.monotonic() + 5
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AuctionQueryError("REQUEST_TIMEOUT: 请求体接收超时")
                message = await asyncio.wait_for(receive(), remaining)
                if message["type"] == "http.disconnect":
                    return
                body.extend(message.get("body",b""))
                if len(body) > 32768:
                    raise AuctionQueryError("REQUEST_TOO_LARGE: 请求超过32KiB")
                if not message.get("more_body",False):
                    break
            if path.rstrip("/") == "/mcp" and scope["method"] == "POST":
                payload = json.loads(body)
                if isinstance(payload,dict) and payload.get("method") == "tools/call":
                    args = payload.get("params",{}).get("arguments",{})
                    if not isinstance(args,dict) or set(args) - PARAMETERS:
                        raise AuctionQueryError("PARAMETERS_INVALID: 工具参数无效")
        except Exception as exc:
            code = str(exc).split(":",1)[0] if isinstance(exc,AuctionQueryError) else "REQUEST_INVALID"
            try:
                await asyncio.to_thread(self.audit.emit,audit_event(rid,code,
                    principal.subject if principal else "", transport="http"))
            except AuctionQueryError:
                code="AUDIT_UNAVAILABLE"
            response = error_response(code,rid,413 if code=="REQUEST_TOO_LARGE" else None)
            if code == "UNAUTHORIZED":
                response.headers["WWW-Authenticate"] = ('Bearer resource_metadata="' + self.public_url +
                    '/.well-known/oauth-protected-resource/mcp"') if self.public_url else "Bearer"
            return await response(scope,receive,send)
        ptoken = principal_context.set(principal)
        rtoken = request_context.set(rid)
        delivered = False
        async def replay():
            nonlocal delivered
            if not delivered:
                delivered=True
                return {"type":"http.request","body":bytes(body),"more_body":False}
            return await receive()
        audit_failed = False
        async def secure_send(message):
            nonlocal audit_failed
            if audit_failed:
                return
            if message["type"] == "http.response.start":
                message["headers"] = list(message.get("headers",[])) + [
                    (b"cache-control",b"no-store"), (b"x-request-id",rid.encode())]
                try:
                    # Also reject an in-flight query if another login revoked it.
                    if session_cookie and path in ("/api/auction/rank", "/api/auction/view", "/api/auth/me"):
                        await asyncio.to_thread(self.sessions.verify, session_cookie)
                    await asyncio.to_thread(self.audit.emit,audit_event(rid,"http_response",
                        principal.subject, http_status=message["status"]))
                except AuctionQueryError as exc:
                    audit_failed = True
                    await error_response(str(exc).split(":",1)[0],rid)(scope,replay,send)
                    return
            await send(message)
        try:
            await self.app(scope,replay,secure_send)
        finally:
            principal_context.reset(ptoken)
            request_context.reset(rtoken)


def create_app(reader, auth, *, limiter=None, audit=None, timeout=8.0,
               origins=("http://127.0.0.1:8790", "http://localhost:8790"), public_url="", issuer="", sessions=None):
    limiter, audit = limiter or MemoryRateLimit(), audit or AuditLog()
    service = WebQueryService(reader,audit,timeout)
    hosts = ["127.0.0.1:*", "localhost:*"]
    if public_url:
        from urllib.parse import urlsplit
        hosts.append(urlsplit(public_url).netloc)
    mcp = create_server(reader,None,executor=service.execute,stateless_http=True,json_response=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=True,
            allowed_hosts=hosts,allowed_origins=list(origins)))
    mcp_app = mcp.streamable_http_app()
    @asynccontextmanager
    async def lifespan(app):
        async with mcp.session_manager.run():
            yield
        service.pool.shutdown(wait=False,cancel_futures=True)
    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.mcp, app.state.query_service = mcp, service
    app.add_middleware(AccessBoundary,auth=auth,limiter=limiter,audit=audit,
                       origins=origins,public_url=public_url,sessions=sessions)

    if sessions:
        from .auction_login import COOKIE, TTL

        async def auth_args(request, allowed, required):
            try:
                args = await request.json()
                if not isinstance(args, dict) or set(args)-allowed or not required.issubset(args):
                    raise ValueError()
                return args
            except (ValueError, TypeError):
                raise AuctionQueryError("PARAMETERS_INVALID: 登录参数无效") from None

        async def record(action):
            principal = principal_context.get()
            await asyncio.to_thread(audit.emit, audit_event(request_context.get(), action, principal.subject))

        @app.post("/api/auth/login")
        async def login(request: Request):
            args = await auth_args(request, {"phone", "password"}, {"phone", "password"})
            await record("login_attempt")
            cookie = await asyncio.to_thread(sessions.login, args["phone"], args["password"])
            response = JSONResponse({"ok":True, "expires_in":TTL})
            response.set_cookie(COOKIE, cookie, max_age=TTL, secure=True, httponly=True, samesite="strict", path="/")
            return response

        @app.get("/api/auth/me")
        async def me():
            principal = principal_context.get()
            return {"authenticated":True, "admin":sessions.is_admin(principal)}

        @app.post("/api/auth/logout")
        async def logout(request: Request):
            await record("logout")
            await asyncio.to_thread(sessions.logout, request.cookies.get(COOKIE, ""))
            response = JSONResponse({"ok":True})
            response.delete_cookie(COOKIE, secure=True, httponly=True, samesite="strict", path="/")
            return response

        @app.get("/api/auth/accounts")
        async def accounts():
            return {"accounts":await asyncio.to_thread(sessions.list_accounts)}

        @app.post("/api/auth/accounts")
        async def manage_accounts(request: Request):
            args = await auth_args(request, {"action", "phone", "password", "admin", "account_id"}, {"action"})
            if args["action"] == "create":
                if "phone" not in args or "account_id" in args:
                    raise AuctionQueryError("PARAMETERS_INVALID: 创建参数无效")
                await record("account_create")
                return await asyncio.to_thread(sessions.create, args["phone"], args.get("password"), args.get("admin",False))
            if "account_id" not in args or "phone" in args or "admin" in args:
                raise AuctionQueryError("PARAMETERS_INVALID: 管理参数无效")
            if args["action"] == "promote" and principal_context.get().subject not in sessions.admin_subjects:
                raise AuctionQueryError("FORBIDDEN: 只有预设所有者可恢复管理员权限")
            await record("account_manage")
            return await asyncio.to_thread(sessions.manage, args["account_id"], args["action"], args.get("password"))

    @app.exception_handler(AuctionQueryError)
    async def query_error(request, exc):
        return error_response(str(exc).split(":",1)[0],request_context.get())

    @app.post("/api/auction/rank")
    async def rank(request: Request):
        try:
            args = await request.json()
            if not isinstance(args,dict) or set(args) - PARAMETERS or "trade_date" not in args:
                raise ValueError()
        except (ValueError, TypeError):
            raise AuctionQueryError("PARAMETERS_INVALID: 参数或JSON无效") from None
        return await service.execute(**args)

    @app.get("/")
    async def home():
        return HTMLResponse(Path(__file__).with_name("auction_web.html").read_text(encoding="utf-8"),
            headers={"Content-Security-Policy":"default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'",
                     "Cache-Control":"no-store"})

    @app.post("/api/auction/view")
    async def view(request: Request):
        try:
            args = await request.json()
            if not isinstance(args,dict) or set(args) - PARAMETERS or "trade_date" not in args:
                raise ValueError()
        except (ValueError, TypeError):
            raise AuctionQueryError("PARAMETERS_INVALID: 参数或JSON无效") from None
        return await service.execute(**args,display=True)

    @app.get("/.well-known/oauth-protected-resource")
    @app.get("/.well-known/oauth-protected-resource/mcp")
    async def resource_metadata():
        if not issuer or not public_url:
            return error_response("OAUTH_NOT_CONFIGURED",uuid.uuid4().hex,404)
        return {"resource":public_url.rstrip("/") + "/mcp", "authorization_servers":[issuer],
                "scopes_supported":["auction:read"],"bearer_methods_supported":["header"]}

    app.mount("/",mcp_app)
    return app


def app_from_env():
    cloud = os.environ.get("VERCEL") == "1"
    backend = os.environ.get("AUCTION_SNAPSHOT_BACKEND","s3" if cloud else "local")
    if backend == "s3":
        ids = json.loads(os.environ["AUCTION_SERVER_IDS"])
        reader = AuctionRankingReader({key:Path(".") for key in ids}, store=S3SnapshotStore(
            os.environ["AUCTION_S3_BUCKET"],os.environ.get("AUCTION_S3_PREFIX","auction")))
    elif backend == "supabase":
        ids = json.loads(os.environ["AUCTION_SERVER_IDS"])
        reader = AuctionRankingReader({key:Path(".") for key in ids}, store=SupabaseSnapshotStore())
    elif backend == "local" and not cloud:
        root = get_paths().root
        config=json.loads((root / "configs/auction-mcp.json").read_text(encoding="utf-8"))
        reader=AuctionRankingReader({key:root / value for key,value in config["servers"].items()})
    else:
        raise ValueError("CONFIG_INVALID: Vercel必须使用云端只读存储")
    public_url=os.environ.get("AUCTION_PUBLIC_URL","").rstrip("/")
    issuer=os.environ.get("AUCTION_OAUTH_ISSUER","")
    if cloud and not public_url.startswith("https://"):
        raise ValueError("CONFIG_INVALID: 需要HTTPS公开地址")
    if issuer:
        auth=JWTAuth(issuer,public_url + "/mcp",os.environ["AUCTION_OAUTH_JWKS_URL"],
                     json.loads(os.environ["AUCTION_OAUTH_GRANTS_JSON"]))
    else:
        auth=BearerAuth(json.loads(os.environ["AUCTION_AUTH_USERS_JSON"]))
    redis=None
    # Vercel Marketplace uses KV_* names; keep each credential pair together.
    if os.environ.get("UPSTASH_REDIS_REST_URL"):
        redis=RedisControls(os.environ["UPSTASH_REDIS_REST_URL"],os.environ["UPSTASH_REDIS_REST_TOKEN"])
    elif os.environ.get("KV_REST_API_URL"):
        redis=RedisControls(os.environ["KV_REST_API_URL"],os.environ["KV_REST_API_TOKEN"])
    if cloud and redis is None:
        raise ValueError("CONFIG_INVALID: Vercel需要分布式限流与审计")
    origins=(public_url,) if public_url else ("http://127.0.0.1:8790","http://localhost:8790")
    sessions = None
    if os.environ.get("AUCTION_LOGIN_ENABLED") == "1":
        from .auction_login import InvitationSessions
        sessions = InvitationSessions(redis, ids if backend != "local" else config["servers"],
            json.loads(os.environ.get("AUCTION_ADMIN_SUBJECTS", "[]")),
            int(os.environ.get("AUCTION_ACCOUNT_LIMIT", "100")))
    return create_app(reader,auth,limiter=redis or MemoryRateLimit(),audit=AuditLog(redis),
                      origins=origins,public_url=public_url,issuer=issuer,sessions=sessions)


def main():
    import uvicorn
    # Never log headers, body, token or sensitive startup environment.
    try:
        app=app_from_env()
    except Exception:
        raise SystemExit("CONFIG_INVALID: HTTP服务缺少有效的存储/身份/控制配置") from None
    uvicorn.run(app,host="127.0.0.1",port=8790,access_log=False)


if __name__ == "__main__":
    main()
