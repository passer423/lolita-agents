"""用户名账号：注册 / 登录 / 登出 / 登录态门禁。

密码只存哈希。用的是标准库 ``hashlib.scrypt``（每用户随机 salt，N=2^14）：
不引入 bcrypt/argon2 依赖，2C2G 上单次验证约几十毫秒，够用且不会把内存打穿。

登录态是**服务端会话**而不是无状态 JWT：cookie 里放随机 token，库里只存
token 的 SHA-256。登出/过期/被清理都只需删一行，前台立刻失效——JWT 做不到
「立刻吊销」。会话绝对有效期 30 天，刷新页面不重登；过期后前端收到 401 回到
登录页。

门禁是纯 ASGI 中间件，默认**拒绝**：只有 ``PUBLIC_API_PATHS`` 里的路径与
非 ``/api/*`` 资源放行，其余接口必须带有效会话，否则 401。这样新增接口不会
因为忘了加依赖而裸奔。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import logging
import secrets
import threading
import time
from http.cookies import SimpleCookie

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel
from starlette.datastructures import Headers
from starlette.responses import JSONResponse

from app import db
from app.config import AppSettings
from app.config import settings as default_settings

logger = logging.getLogger(__name__)

COOKIE_NAME = "lolita_session"
SESSION_DAYS = 30
TOUCH_SECONDS = 3600.0
PRUNE_SECONDS = 300.0

#: scrypt 参数。N=2^14/r=8/p=1 约 16 MiB、几十毫秒；改参数时旧哈希仍可校验
#: （参数写在哈希串里），不需要重置密码。
SCRYPT_N = 2**14
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_MAXMEM = 64 * 1024 * 1024

#: 用户名规则：3–32 个字符，中文/字母/数字/下划线/连字符/点，至少一个非符号。
USERNAME_MIN = 3
USERNAME_MAX = 32
USERNAME_SYMBOLS = "_-."

#: 不需要登录的 API 路径（登录/注册/探针/文档）。
PUBLIC_API_PATHS = frozenset(
    {
        "/api/health",
        "/api/chat/status",
        "/api/auth/register",
        "/api/auth/login",
        "/api/auth/logout",
        "/api/auth/me",
        "/api/auth/token",
        "/api/docs",
        "/api/openapi.json",
        "/api/redoc",
        "/api/docs/oauth2-redirect",
    }
)

_prune_lock = threading.Lock()
_last_prune = 0.0
_dummy_hash: str | None = None


# --------------------------------------------------------------------- 密码


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        maxmem=SCRYPT_MAXMEM,
        dklen=32,
    )
    return (
        f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}"
        f"${_b64(salt)}${_b64(digest)}"
    )


def verify_password(password: str, stored: str) -> bool:
    """校验 scrypt 哈希。格式不对一律返回 False，不把解析异常抛给调用方。"""
    try:
        algo, n, r, p, salt, digest = str(stored).split("$")
        if algo != "scrypt":
            return False
        n, r, p = int(n), int(r), int(p)
        if n < 2**10 or n > 2**18 or r < 1 or r > 16 or p < 1 or p > 4:
            return False
        expected = _unb64(digest)
        actual = hashlib.scrypt(
            password.encode("utf-8"),
            salt=_unb64(salt),
            n=n,
            r=r,
            p=p,
            maxmem=SCRYPT_MAXMEM,
            dklen=len(expected),
        )
    except (ValueError, TypeError, binascii.Error):
        return False
    return hmac.compare_digest(actual, expected)


# --------------------------------------------------------------------- 规范化


def check_username(username: str) -> str:
    """校验并返回去掉首尾空白后的用户名。

    只查注册；登录不套这套字符限制，历史上用邮箱注册的账号仍可原样登录。
    """
    value = str(username or "").strip()
    if not value:
        raise ValueError("请填写用户名")
    if len(value) < USERNAME_MIN:
        raise ValueError(f"用户名至少 {USERNAME_MIN} 个字符")
    if len(value) > USERNAME_MAX:
        raise ValueError(f"用户名最多 {USERNAME_MAX} 个字符")
    if not all(ch.isalnum() or ch in USERNAME_SYMBOLS for ch in value):
        raise ValueError("用户名只能包含中文、字母、数字、下划线、连字符和点")
    if not any(ch.isalnum() for ch in value):
        raise ValueError("用户名至少要包含一个中文、字母或数字")
    return value


def check_password(password: str) -> str:
    value = str(password or "")
    if len(value) < 8:
        raise ValueError("密码至少 8 位")
    if len(value) > 128:
        raise ValueError("密码最长 128 位")
    return value


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------- 账号动作


def register(db_path, username: str, password: str) -> tuple[dict, str]:
    username = check_username(username)
    password = check_password(password)
    account = db.create_account(db_path, username, hash_password(password))
    return account, _issue_session(db_path, account["id"])


def login(db_path, username: str, password: str) -> tuple[dict, str]:
    # 登录只做 trim，不校验字符集：旧邮箱账号的用户名里带 @，仍要能登录。
    username = str(username or "").strip()
    account = db.get_account_by_username(db_path, username) if username else None
    global _dummy_hash
    if _dummy_hash is None:
        # 账号不存在时也跑一次 scrypt，避免靠响应时间判断用户名是否注册过。
        # 只算一次并缓存，否则每个伪造用户名的登录请求都要多花一次哈希。
        _dummy_hash = hash_password("dummy-password-not-used")
    stored = account["password_hash"] if account else _dummy_hash
    if not verify_password(password, stored) or account is None:
        # 账号不存在与密码错误给同一句话，避免把「这个用户名注册过」告诉攻击者。
        raise PermissionError("用户名或密码不正确")
    db.touch_account(db_path, account["id"])
    return {k: account[k] for k in ("id", "username", "created_at")}, _issue_session(
        db_path, account["id"]
    )


def _issue_session(db_path, account_id: int) -> str:
    token = secrets.token_urlsafe(32)
    db.create_session(db_path, account_id, _token_hash(token), days=SESSION_DAYS)
    return token


def resolve_session(db_path, token: str) -> dict | None:
    """token → 账号。顺带做节流的 last_seen 更新（每会话每小时最多一次）。"""
    if not token:
        return None
    record = db.get_session(db_path, _token_hash(token))
    if record is None:
        return None
    try:
        from datetime import datetime

        seen = datetime.fromisoformat(record["last_seen_at"])
        if (datetime.now() - seen).total_seconds() >= TOUCH_SECONDS:
            db.touch_session(db_path, record["token_hash"])
    except (ValueError, TypeError):
        pass
    return {"id": record["account_id"], "username": record["username"]}


def logout(db_path, token: str) -> None:
    if token:
        db.delete_session(db_path, _token_hash(token))


def maybe_prune(app: AppSettings | None = None) -> None:
    """写入/请求路径上的节流清理：最多每 5 分钟一次，失败只记日志。"""
    global _last_prune
    app = app or default_settings
    now = time.monotonic()
    if now - _last_prune < PRUNE_SECONDS:
        return
    with _prune_lock:
        if now - _last_prune < PRUNE_SECONDS:
            return
        _last_prune = now
        try:
            removed = db.prune_user_data(app.db_path)
            if any(removed.values()):
                logger.info(
                    "清理用户数据：对话 %s 个、会话 %s 个",
                    removed["threads"],
                    removed["sessions"],
                )
        except Exception as exc:  # noqa: BLE001 - 清理失败不能影响请求
            logger.warning("清理用户数据失败：%s", exc)


def _cookie_secure(request: Request) -> bool:
    proto = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip()
    return request.url.scheme == "https" or proto == "https"


def set_session_cookie(response: Response, token: str, request: Request) -> None:
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=SESSION_DAYS * 24 * 3600,
        httponly=True,
        samesite="lax",
        secure=_cookie_secure(request),
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(COOKIE_NAME, path="/")


def cookie_token(headers: Headers) -> str:
    raw = headers.get("cookie") or ""
    if not raw:
        return ""
    jar = SimpleCookie()
    try:
        jar.load(raw)
    except Exception:  # noqa: BLE001 - 畸形 Cookie 当作未登录
        return ""
    morsel = jar.get(COOKIE_NAME)
    return morsel.value if morsel else ""


# --------------------------------------------------------------------- 门禁


def _unauthorized() -> JSONResponse:
    return JSONResponse(
        {"detail": "请先登录：未登录不能访问对话与关注项"},
        status_code=401,
        headers={"WWW-Authenticate": "Cookie"},
    )


class AccountGate:
    """纯 ASGI 中间件：保护除白名单外的全部 ``/api/*``。

    不缓冲响应，SSE 仍按字节流过。解析出的账号写进 ``scope["state"]["account"]``，
    路由用 ``account_of(request)`` 读取。
    """

    def __init__(self, app, *, settings: AppSettings | None = None) -> None:
        self.app = app
        self.settings = settings or default_settings

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if not path.startswith("/api/") or path in PUBLIC_API_PATHS:
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        token = cookie_token(headers)
        account = resolve_session(self.settings.db_path, token)
        if account is None:
            await _unauthorized()(scope, receive, send)
            return

        scope.setdefault("state", {})["account"] = account
        maybe_prune(self.settings)
        await self.app(scope, receive, send)


def account_of(request: Request) -> dict:
    """取门禁写入的账号；没经过门禁（例如直接单测函数）时按未登录处理。"""
    account = getattr(request.state, "account", None)
    if not isinstance(account, dict) or not account.get("id"):
        raise HTTPException(status_code=401, detail="请先登录")
    return account


# --------------------------------------------------------------------- 接口

router = APIRouter(prefix="/api/auth", tags=["accounts"])


class Credentials(BaseModel):
    username: str = ""
    password: str = ""


@router.post("/register", status_code=201)
def register_account(payload: Credentials, request: Request, response: Response) -> dict:
    try:
        account, token = register(default_settings.db_path, payload.username, payload.password)
    except ValueError as exc:
        status = 409 if "注册过" in str(exc) else 422
        raise HTTPException(status_code=status, detail=str(exc)) from exc
    set_session_cookie(response, token, request)
    maybe_prune(default_settings)
    return {"account": {k: account[k] for k in ("id", "username", "created_at")}}


@router.post("/login")
def login_account(payload: Credentials, request: Request, response: Response) -> dict:
    try:
        account, token = login(default_settings.db_path, payload.username, payload.password)
    except PermissionError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    set_session_cookie(response, token, request)
    return {"account": account}


@router.post("/logout")
def logout_account(request: Request, response: Response) -> dict:
    logout(default_settings.db_path, cookie_token(request.headers))
    clear_session_cookie(response)
    return {"ok": True}


@router.get("/me")
def me(request: Request, response: Response) -> dict:
    token = cookie_token(request.headers)
    account = resolve_session(default_settings.db_path, token)
    if account is None:
        clear_session_cookie(response)
        raise HTTPException(status_code=401, detail="未登录")
    return {"account": account}
