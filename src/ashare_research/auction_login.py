"""Invitation-only website accounts. Redis is authoritative; no auction writes.

Only salted password hashes and session digests are persisted. New login rotates
the single live session atomically across all serverless instances.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import string
import threading
import time

from .auction_query import AuctionQueryError
from .auction_web_security import Principal

COOKIE = "__Host-auction-session"
TTL = 7 * 86400
PREFIX = "auction:accounts:"
SLOTS = threading.BoundedSemaphore(2)


def account_id(phone):
    if not isinstance(phone, str) or not re.fullmatch(r"1[3-9][0-9]{9}", phone):
        raise AuctionQueryError("PARAMETERS_INVALID: 请输入中国大陆11位手机号")
    return hashlib.sha256(phone.encode()).hexdigest()


def valid_password(password):
    if (not isinstance(password, str) or not re.fullmatch(r"[A-Za-z0-9]{8}", password)
            or not re.search(r"[A-Za-z]", password) or not re.search(r"[0-9]", password)):
        raise AuctionQueryError("PARAMETERS_INVALID: 密码必须为8位英文和数字的组合")
    return password


def generate_password():
    while True:
        value = "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(8))
        if re.search(r"[A-Za-z]", value) and re.search(r"[0-9]", value):
            return value


def password_hash(password, salt=None):
    valid_password(password)
    if not SLOTS.acquire(blocking=False):
        raise AuctionQueryError("QUERY_BUSY: 登录繁忙")
    try:
        salt = salt or secrets.token_hex(16)
        # OWASP's memory/CPU-equivalent scrypt profile: N=2^14, r=8, p=5.
        digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt),
                                n=16384, r=8, p=5, maxmem=32*1024*1024).hex()
        return "scrypt-16384-8-5$" + salt + "$" + digest
    finally:
        SLOTS.release()


LOGIN = """
local raw=redis.call('GET',KEYS[1]); if not raw then return 0 end
local row=cjson.decode(raw)
if not row.active or row.revision~=ARGV[1] then return 0 end
row.session=ARGV[2]; row.expires=tonumber(ARGV[3])
redis.call('SET',KEYS[1],cjson.encode(row)); return 1
"""
CREATE = """
if redis.call('EXISTS',KEYS[1])==1 then return -1 end
if redis.call('SCARD',KEYS[2])>=tonumber(ARGV[3]) then return -2 end
redis.call('SET',KEYS[1],ARGV[1]); redis.call('SADD',KEYS[2],ARGV[2]); return 1
"""
UPDATE = """
local raw=redis.call('GET',KEYS[1]); if not raw then return 0 end
local row=cjson.decode(raw)
if ARGV[1]=='logout' then
 if row.session~=ARGV[2] then return 0 end
else
 row.revision=ARGV[3]
 if ARGV[1]=='disable' then row.active=false end
 if ARGV[1]=='enable' then row.active=true end
 if ARGV[1]=='reset' then row.password=ARGV[2] end
 if ARGV[1]=='promote' then row.admin=true end
end
row.session=''; row.expires=0
redis.call('SET',KEYS[1],cjson.encode(row)); return 1
"""
THROTTLE = """
local a=redis.call('INCR',KEYS[1]); if a==1 then redis.call('EXPIRE',KEYS[1],60) end
local g=redis.call('INCR',KEYS[2]); if g==1 then redis.call('EXPIRE',KEYS[2],60) end
if a>5 or g>30 then return 0 end; return 1
"""


class InvitationSessions:
    def __init__(self, redis, servers, admin_subjects=(), account_limit=100):
        if (redis is None or not servers or not 1 <= account_limit <= 1000
                or not isinstance(admin_subjects, (list, tuple))
                or any(not isinstance(s, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", s)
                       for s in admin_subjects)):
            raise ValueError("LOGIN_CONFIG_INVALID")
        self.redis, self.servers = redis, frozenset(servers)
        self.admin_subjects = frozenset(admin_subjects)
        self.account_limit = account_limit
        # Unknown users still pay the same password verification work.
        self.dummy = password_hash(generate_password())

    def _read(self, aid):
        raw = self.redis.command(["GET", PREFIX + aid])
        try:
            return self._validate_row(json.loads(raw)) if raw else None
        except Exception:
            raise AuctionQueryError("CONTROL_UNAVAILABLE: 账号存储不可用") from None

    @staticmethod
    def _validate_row(row):
        if (not isinstance(row, dict) or not isinstance(row.get("active"), bool)
                or not isinstance(row.get("admin"), bool)
                or not isinstance(row.get("expires"), (int,float)) or isinstance(row["expires"], bool)
                or not re.fullmatch(r"scrypt-16384-8-5\$[0-9a-f]{32}\$[0-9a-f]{128}", row.get("password", ""))
                or not re.fullmatch(r"[0-9a-f]{32}", row.get("revision", ""))
                or not re.fullmatch(r"(?:[0-9a-f]{64})?", row.get("session", ""))
                or not re.fullmatch(r"1[3-9][0-9]\*{4}[0-9]{4}", row.get("label", ""))):
            raise ValueError()
        return row

    def login(self, phone, password):
        # Validate shape without distinguishing unknown/disabled/wrong password.
        try:
            aid = account_id(phone)
            valid_password(password)
        except AuctionQueryError:
            raise AuctionQueryError("UNAUTHORIZED: 账号或密码错误") from None
        if not self.redis.command(["EVAL", THROTTLE, 2, "auction:login:"+aid,
                                   "auction:login:global"]):
            raise AuctionQueryError("RATE_LIMITED: 登录尝试过多")
        row = self._read(aid)
        stored = row["password"] if row else self.dummy
        computed = password_hash(password, stored.split("$")[1])
        if not hmac.compare_digest(computed, stored) or not row or not row["active"]:
            raise AuctionQueryError("UNAUTHORIZED: 账号或密码错误")
        secret = secrets.token_urlsafe(32)
        digest = hashlib.sha256(secret.encode()).hexdigest()
        if not self.redis.command(["EVAL", LOGIN, 1, PREFIX+aid, row["revision"],
                                   digest, int(time.time())+TTL]):
            raise AuctionQueryError("UNAUTHORIZED: 账号状态已变更，请重新登录")
        return aid + "." + secret

    def verify(self, cookie):
        if not isinstance(cookie, str) or not re.fullmatch(r"[0-9a-f]{64}\.[A-Za-z0-9_-]{43}", cookie):
            raise AuctionQueryError("UNAUTHORIZED: 登录已失效")
        aid, secret = cookie.split(".")
        row = self._read(aid)
        if (not row or not row["active"] or row["expires"] <= time.time()
                or not hmac.compare_digest(row["session"], hashlib.sha256(secret.encode()).hexdigest())):
            raise AuctionQueryError("UNAUTHORIZED: 登录已失效")
        return Principal("account_"+aid, self.servers, bool(row["admin"]))

    def is_admin(self, principal):
        return principal.admin or principal.subject in self.admin_subjects

    def create(self, phone, password=None, admin=False):
        aid = account_id(phone)
        if not isinstance(admin, bool):
            raise AuctionQueryError("PARAMETERS_INVALID: 管理员标记必须为布尔值")
        password = generate_password() if password is None else valid_password(password)
        row = {"password":password_hash(password), "active":True, "admin":admin,
               "revision":secrets.token_hex(16), "session":"", "expires":0,
               "label":phone[:3]+"****"+phone[-4:]}
        status = self.redis.command(["EVAL", CREATE, 2, PREFIX+aid, PREFIX+"index",
                                    json.dumps(row), aid, self.account_limit])
        if status != 1:
            raise AuctionQueryError("ACCOUNT_EXISTS: 账号已存在" if status == -1 else
                                    "ACCOUNT_LIMIT: 账号数量达到上限")
        return {"account_id":aid, "label":row["label"], "password":password}

    def manage(self, aid, action, password=None):
        if not isinstance(aid, str) or not re.fullmatch(r"[0-9a-f]{64}", aid):
            raise AuctionQueryError("PARAMETERS_INVALID: 账号编号无效")
        if action not in ("disable", "enable", "reset", "revoke", "promote"):
            raise AuctionQueryError("PARAMETERS_INVALID: 账号操作无效")
        if action != "reset" and password is not None:
            raise AuctionQueryError("PARAMETERS_INVALID: 此操作不接收密码")
        value = ""
        if action == "reset":
            password = generate_password() if password is None else valid_password(password)
            value = password_hash(password)
        if not self.redis.command(["EVAL", UPDATE, 1, PREFIX+aid, action, value, secrets.token_hex(16)]):
            raise AuctionQueryError("ACCOUNT_MISSING: 账号不存在")
        return {"ok":True, **({"password":password} if action == "reset" else {})}

    def logout(self, cookie):
        self.verify(cookie)
        aid, secret = cookie.split(".")
        self.redis.command(["EVAL", UPDATE, 1, PREFIX+aid, "logout",
                            hashlib.sha256(secret.encode()).hexdigest(), ""])

    def list_accounts(self):
        ids = self.redis.command(["SMEMBERS", PREFIX+"index"])
        if len(ids) > self.account_limit:
            raise AuctionQueryError("CONTROL_UNAVAILABLE: 账号索引异常")
        rows = self.redis.command(["MGET", *[PREFIX+aid for aid in sorted(ids)]]) if ids else []
        return [{"account_id":aid, "label":row["label"], "active":row["active"],
                 "admin":row["admin"]} for aid, raw in zip(sorted(ids), rows)
                if raw for row in [self._validate_row(json.loads(raw))]]
