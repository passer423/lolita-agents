"""可选的访问控制：API key / JWT 鉴权、按 key 限流、按天成本配额。

默认 **全关**（``AUTH_MODE=off``、``RATE_LIMIT_REQUESTS=0``、``DAILY_QUOTA=0``）：
这是本地单用户工具，打开就能用是默认体验。只有显式配置后才改变行为。

三条刻意取舍：

* **只保护 ``/api/*``**：首页、``/chat.js``、``/style.css``、``/sw.js`` 等静态
  资源本来就不含数据；把整站挡在登录后面只会让局域网分享变麻烦。API 白名单
  （``/api/health``、``/api/docs``）保持公开，探针和文档不需要凭据。
* **配额不另起计量**：直接读 ``trace.cost_summary(days=1)`` 的 ``totals.cost``，
  那是每次 run 写进 ``chat_runs.jsonl`` 的成本账。价格改了历史账不会变，这里
  也不重新算价。模型没配价格时 ``cost`` 是 ``None``——**不当作 0 来卡人**，
  而是跳过配额并说清楚原因。
* **配置错了要在启动时炸**：开了 ``api_key`` 却没配 key、开了 ``jwt`` 却没配
  密钥，一律在应用构造期抛 ``RuntimeError``。静默变成「谁都能进」或「谁都进
  不来」都比启动失败更难排查。
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
from collections import deque
from datetime import datetime, timedelta
from datetime import time as clock_time

import jwt
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel
from starlette.datastructures import Headers
from starlette.responses import JSONResponse

from app.agent import trace
from app.config import AppSettings
from app.config import settings as default_settings

logger = logging.getLogger(__name__)

AUTH_MODES = ("off", "api_key", "jwt")
JWT_ALGORITHM = "HS256"

#: 不需要凭据的 API 路径（静态资源不走这里，见模块说明）。
PUBLIC_API_PATHS = frozenset(
    {
        "/api/health",
        "/api/auth/token",
        "/api/docs",
        "/api/openapi.json",
        "/api/redoc",
        "/api/docs/oauth2-redirect",
    }
)

#: 只有这两条会真的花 LLM 的钱，配额只拦它们。
COST_PATHS = frozenset({"/api/chat", "/api/chat/stream"})

#: 今日成本账的短缓存：轨迹每分钟才追加几条，没必要每个请求都重扫 20MB。
COST_TTL_SECONDS = 3.0


def parse_api_keys(app: AppSettings | None = None) -> dict[str, str]:
    """``AUTH_API_KEYS="k1:alice, k2"`` → ``{k1: alice, k2: 指纹}``。

    冒号后面是可选的展示名，用于日志和限流分桶；没写名字时用 key 的短指纹，
    **绝不把原始 key 写进日志**。
    """
    app = app or default_settings
    pairs: dict[str, str] = {}
    for raw in str(app.auth_api_keys or "").split(","):
        item = raw.strip()
        if not item:
            continue
        key, _, name = item.partition(":")
        key = key.strip()
        if not key:
            continue
        label = name.strip() or _fingerprint(key)
        pairs[key] = label
    return pairs


def _fingerprint(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]


def mint_token(app: AppSettings | None = None, *, subject: str) -> str:
    app = app or default_settings
    now = datetime.now()
    payload = {
        "sub": subject,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=app.auth_jwt_ttl)).timestamp()),
        "iss": "lolita-agents",
    }
    return jwt.encode(payload, app.auth_jwt_secret, algorithm=JWT_ALGORITHM)


def verify_token(token: str, app: AppSettings | None = None) -> str | None:
    """校验 HS256 签名与过期时间；失败返回 ``None``，不把底层异常抛给调用方。"""
    app = app or default_settings
    try:
        claims = jwt.decode(
            token,
            app.auth_jwt_secret,
            algorithms=[JWT_ALGORITHM],
            options={"require": ["exp", "sub"]},
        )
    except jwt.PyJWTError:
        return None
    subject = claims.get("sub")
    return str(subject) if subject else None


def _seconds_until_midnight() -> int:
    now = datetime.now()
    tomorrow = datetime.combine(now.date() + timedelta(days=1), clock_time.min)
    return max(1, int((tomorrow - now).total_seconds()))


class AccessControl:
    """纯 ASGI 中间件：不缓冲响应，SSE 可以直接流过。

    顺序：先认证（决定 ``identity``），再限流（按 identity 分桶），最后配额
    （只拦花钱的 chat 路径）。
    """

    def __init__(self, app, *, settings: AppSettings | None = None) -> None:
        self.app = app
        self.settings = settings or default_settings
        self.mode = str(self.settings.auth_mode or "off").strip().lower()
        if self.mode not in AUTH_MODES:
            raise RuntimeError(
                f"AUTH_MODE={self.settings.auth_mode!r} 无效，可选：{' / '.join(AUTH_MODES)}"
            )
        self.keys = parse_api_keys(self.settings)
        if self.mode == "api_key" and not self.keys:
            raise RuntimeError(
                "AUTH_MODE=api_key 但没有配置 AUTH_API_KEYS："
                "请填至少一个 key（多个用逗号分隔），或把 AUTH_MODE 改回 off。"
            )
        if self.mode == "jwt":
            if not str(self.settings.auth_jwt_secret or "").strip():
                raise RuntimeError(
                    "AUTH_MODE=jwt 但没有配置 AUTH_JWT_SECRET："
                    "请填一个签名密钥（建议 32 字节以上随机串），或把 AUTH_MODE 改回 off。"
                )
            if len(self.settings.auth_jwt_secret) < 32:
                logger.warning("AUTH_JWT_SECRET 短于 32 字符，建议换成更长的随机串")

        self.rate_requests = max(int(self.settings.rate_limit_requests or 0), 0)
        self.rate_window = max(float(self.settings.rate_limit_window or 0), 0.0)
        self.quota = max(float(self.settings.daily_quota or 0), 0.0)
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()
        self._cost_cache: tuple[tuple, float] | None = None
        self._cost_at = 0.0
        self._unpriced_warned = False

        self._log_startup()

    # ---------------------------------------------------------------- 启动提示
    def _log_startup(self) -> None:
        if self.mode == "off":
            logger.warning(
                "鉴权关闭（AUTH_MODE=off）：任何能访问 %s:%s 的人都能调用全部接口",
                self.settings.bind_host,
                self.settings.bind_port,
            )
            if self.settings.bind_host not in ("127.0.0.1", "localhost", "::1"):
                logger.warning("当前绑定地址不是回环地址，且鉴权关闭，请确认网络环境可信")
        else:
            logger.info("鉴权已启用：mode=%s，API key 数量=%s", self.mode, len(self.keys))
        if self.rate_requests and self.rate_window:
            logger.info("限流：每个 key 每 %.0fs 最多 %s 次", self.rate_window, self.rate_requests)
        if self.quota > 0:
            logger.info(
                "每日成本配额：%.2f %s（数据源 trace.cost_summary）",
                self.quota,
                self.settings.llm_price_currency,
            )

    # ---------------------------------------------------------------- 认证
    def _identity(self, headers: Headers) -> str | None:
        if self.mode == "off":
            return "anonymous"

        if self.mode == "jwt":
            raw = headers.get("authorization", "")
            if not raw.lower().startswith("bearer "):
                return None
            subject = verify_token(raw.split(" ", 1)[1].strip(), self.settings)
            return f"jwt:{subject}" if subject else None

        key = headers.get("x-api-key", "").strip()
        if not key:
            raw = headers.get("authorization", "")
            if raw.lower().startswith("bearer "):
                key = raw.split(" ", 1)[1].strip()
        if not key:
            return None
        for known, label in self.keys.items():
            if _same(key, known):
                return f"key:{label}"
        return None

    def _client_label(self, scope: dict) -> str:
        client = scope.get("client") or ("unknown", 0)
        return f"ip:{client[0]}"

    # ---------------------------------------------------------------- 限流
    def _allow_request(self, identity: str) -> tuple[bool, float]:
        if self.rate_requests <= 0 or self.rate_window <= 0:
            return True, 0.0
        now = time.monotonic()
        with self._lock:
            bucket = self._hits.setdefault(identity, deque())
            while bucket and now - bucket[0] >= self.rate_window:
                bucket.popleft()
            if len(bucket) >= self.rate_requests:
                return False, max(0.0, self.rate_window - (now - bucket[0]))
            bucket.append(now)
        return True, 0.0

    # ---------------------------------------------------------------- 配额
    def _today_cost(self) -> float | None:
        """复用轨迹成本账；未定价模型返回 ``None``（不拿 0 当真实花费）。"""
        stamp = self._trace_stamp()
        now = time.monotonic()
        if (
            self._cost_cache is not None
            and stamp == self._cost_cache[0]
            and now - self._cost_at < COST_TTL_SECONDS
        ):
            return self._cost_cache[1]
        try:
            summary = trace.cost_summary(days=1, app=self.settings)
        except Exception as exc:  # noqa: BLE001 - 账本坏了不能把所有人挡在门外
            logger.warning("读取成本账失败，跳过配额检查：%s", exc)
            return None
        totals = summary.get("totals") or {}
        amount = totals.get("cost")
        if amount is None:
            if not self._unpriced_warned:
                logger.warning(
                    "成本账里有未定价模型（cost=None），今日配额无法执行；"
                    "在 LLM_PRICE_TABLE 里补价格后生效"
                )
                self._unpriced_warned = True
            return None
        value = float(amount)
        self._cost_cache = (stamp, value)
        self._cost_at = now
        return value

    def _trace_stamp(self) -> tuple:
        path = trace.trace_path(self.settings)
        stamps: list[tuple[str, int, int]] = []
        for index in range(int(self.settings.trace_keep_files or 0) + 1):
            candidate = path if index == 0 else path.with_name(f"{path.name}.{index}")
            try:
                stat = candidate.stat()
            except OSError:
                continue
            stamps.append((candidate.name, stat.st_mtime_ns, stat.st_size))
        return tuple(stamps)

    # ---------------------------------------------------------------- ASGI
    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if not path.startswith("/api/") or path in PUBLIC_API_PATHS:
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        identity = self._identity(headers)
        if self.mode != "off" and identity is None:
            await _send_json(
                scope,
                receive,
                send,
                401,
                "缺少或无效的凭据：API key 放 X-API-Key（或 Authorization: Bearer），"
                "JWT 放 Authorization: Bearer",
                headers={"WWW-Authenticate": "Bearer"},
            )
            return

        bucket = identity or self._client_label(scope)
        allowed, retry_after = self._allow_request(bucket)
        if not allowed:
            wait = max(1, int(retry_after + 0.999))
            await _send_json(
                scope,
                receive,
                send,
                429,
                f"请求过于频繁：每 {self.rate_window:.0f} 秒最多 {self.rate_requests} 次",
                headers={"Retry-After": str(wait)},
            )
            return

        if (
            self.quota > 0
            and path in COST_PATHS
            and scope.get("method", "").upper() == "POST"
        ):
            spent = self._today_cost()
            if spent is not None and spent >= self.quota:
                wait = _seconds_until_midnight()
                currency = self.settings.llm_price_currency
                await _send_json(
                    scope,
                    receive,
                    send,
                    429,
                    f"今日成本配额已用完：{spent:.2f}/{self.quota:.2f} {currency}，"
                    "明日本地时间 00:00 重置（DAILY_QUOTA 可调）",
                    headers={"Retry-After": str(wait)},
                )
                return

        await self.app(scope, receive, send)


def _same(left: str, right: str) -> bool:
    return hashlib.sha256(left.encode("utf-8")).digest() == hashlib.sha256(
        right.encode("utf-8")
    ).digest()


async def _send_json(scope, receive, send, status: int, detail: str, *, headers=None) -> None:
    response = JSONResponse(
        {"detail": detail}, status_code=status, headers=headers or {}
    )
    await response(scope, receive, send)


# --------------------------------------------------------------------- 令牌接口

router = APIRouter(prefix="/api/auth", tags=["auth"])


class TokenIn(BaseModel):
    api_key: str = ""


@router.post("/token")
def issue_token(
    payload: TokenIn | None = None,
    x_api_key: str | None = Header(None, alias="X-API-Key"),
):
    """用配置里的 API key 换一个短期 JWT（仅 ``AUTH_MODE=jwt`` 时存在）。

    key 只用于换取令牌，不会进入日志；``sub`` 用配置里的展示名。
    """
    settings = default_settings
    if settings.auth_mode.strip().lower() != "jwt":
        raise HTTPException(status_code=404, detail="当前未启用 JWT 模式")
    key = (payload.api_key if payload else "").strip() or (x_api_key or "").strip()
    keys = parse_api_keys(settings)
    if not keys:
        raise HTTPException(
            status_code=503,
            detail="AUTH_MODE=jwt 但没有配置 AUTH_API_KEYS，无法用 key 换令牌",
        )
    subject = next((label for known, label in keys.items() if _same(key, known)), None)
    if not key or subject is None:
        raise HTTPException(status_code=401, detail="API key 无效")
    return {
        "access_token": mint_token(settings, subject=subject),
        "token_type": "Bearer",
        "expires_in": int(settings.auth_jwt_ttl),
    }
