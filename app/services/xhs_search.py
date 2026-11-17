"""小红书搜索服务：按需调用 ``scripts/xhs_search.py``。

为什么要绕一层脚本：Playwright 的同步 API 不能跑在 FastAPI 的事件循环里，
放进子进程既隔离了浏览器生命周期，也能用 subprocess 超时兜底。

边界：

* 小红书网页版必须登录；没登录、没装 Playwright、没有 profile 时返回
  ``XhsUnavailable``，调用方继续走别的源，不猜数据。
* 结果只当**用户笔记线索**，不是官方事实；实时通道结果只作本次回答的临时证据，
  不落盘（6 小时缓存只是避免同一 query 反复启动浏览器，不进索引）。
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import logging
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from app.config import APP_ROOT, AppSettings
from app.services import web_search

logger = logging.getLogger(__name__)

CACHE_TTL_SECONDS = 6 * 3600

# 缓存结构版本。v1 的 notes 里没有配图字段，读进来会让「图里可能有价格表」
# 的提示整整 6 小时不出现，看起来像功能没生效。版本不匹配就跳过并重抓。
# v2 已含 image_count / images；之后只把保留的图片 URL 上限从 2 提到 3，
# 结构没变，旧 v2 缓存继续可用（只是少一张图），所以不升版本。
# v3 起 notes 带 author：旧缓存读进来会让「是不是官方账号发的」一直判不出来，
# 而这类判断错了会直接影响价格该信谁，所以版本不匹配就重抓。
CACHE_VERSION = 3


class XhsUnavailable(RuntimeError):
    """小红书搜索不可用（未登录 / 没装 Playwright / 超时）。"""


def script_path() -> Path:
    return APP_ROOT / "scripts" / "xhs_search.py"


_PROVIDERS = ("playwright", "spider_xhs")


def _provider(app: AppSettings) -> str:
    value = (app.xhs_provider or "playwright").strip().lower()
    if value not in _PROVIDERS:
        raise XhsUnavailable(
            f"未知的 XHS_PROVIDER：{value}（只支持 {' / '.join(_PROVIDERS)}）"
        )
    return value


def _playwright_readiness(app: AppSettings) -> tuple[bool, str]:
    """Playwright 方案的就绪原因：脚本 / 依赖 / 登录态三者缺一不可。"""
    if not script_path().exists():
        return False, f"找不到搜索脚本：{script_path()}"
    if importlib.util.find_spec("playwright") is None:
        return False, "未安装 playwright（pip install playwright 后再装浏览器）"
    if not (app.xhs_profile_dir / ".logged_in").exists():
        return False, "未登录（先运行 python scripts/xhs_search.py --login 扫码）"
    return True, ""


def readiness(app: AppSettings) -> tuple[bool, str]:
    """是否可搜，并给出具体原因。

    状态接口与 ``available()`` 共用这一份判断，避免「界面说可用、召回时说
    未登录」这种两边各写一遍导致的漂移。
    """
    if not app.xhs_enabled:
        return False, "未启用（XHS_ENABLED=false）"
    try:
        provider = _provider(app)
    except XhsUnavailable as exc:
        return False, str(exc)
    if provider == "spider_xhs":
        from app.services import xhs_spider

        return xhs_spider.readiness(app)
    return _playwright_readiness(app)


def available(app: AppSettings) -> bool:
    """只有“启用 + 对应 provider 就绪”才认为能搜。"""
    return readiness(app)[0]


# 状态展示用的运行健康度。``readiness()`` 仍只判结构（配置 / 依赖 / 登录文件），
# 召回路径完全不受这里影响；只有 ``status_readiness()`` 会把「最近真实调用失败」
# 或「连续空结果」折算成 not ready。
#
# 为什么不做主动探测：小红书没有免签名的轻量自检接口，主动探测要么再走一次
# Playwright（几秒 + 可能触发风控），要么消耗一次搜索配额；而状态接口会被
# 反复轮询。复用真实调用的结果既零额外请求，也不会因为一次网络抖动把召回拦掉。
HEALTH_TTL_SECONDS = 15 * 60
EMPTY_STREAK_DEGRADE = 3
_health: dict[str, dict[str, Any]] = {}


def _record_health(provider: str, *, ok: bool, reason: str = "", empty: bool = False) -> None:
    entry = _health.setdefault(
        provider, {"ts": 0.0, "ok": True, "reason": "", "empty_streak": 0}
    )
    entry["ts"] = time.time()
    entry["ok"] = ok
    if ok:
        entry["reason"] = ""
        entry["empty_streak"] = int(entry.get("empty_streak") or 0) + 1 if empty else 0
    else:
        entry["reason"] = reason


def status_readiness(app: AppSettings) -> tuple[bool, str]:
    """给状态接口用：结构就绪 + 最近调用是否真的成功。

    失败只影响「显示为 not ready」，不影响 ``available()`` / ``search()``：
    登录态失效后第一次真实调用报错时把原因记下来，下一次查询状态就能看到，
    TTL 到期后重新按结构判断，不会永久卡红。
    """
    ready, reason = readiness(app)
    if not ready:
        return ready, reason
    entry = _health.get(_provider(app))
    if not entry or time.time() - float(entry.get("ts") or 0) > HEALTH_TTL_SECONDS:
        return True, ""
    if not entry.get("ok"):
        return False, f"最近一次调用失败：{entry.get('reason') or '未知原因'}（可能是 XHS_COOKIE 失效）"
    if int(entry.get("empty_streak") or 0) >= EMPTY_STREAK_DEGRADE:
        return False, f"最近连续 {EMPTY_STREAK_DEGRADE} 次召回为空，可能是登录态失效或被风控"
    return True, ""


def _cache_path(app: AppSettings, query: str, *, provider: str, limit: int) -> Path:
    """缓存键要带 provider 与 limit。

    只按 query 哈希时，换 provider 会继续返回旧 provider 抓的结果，调大条数
    也永远只见旧的那几条——看起来像「抓不到新笔记」，其实是缓存命中。
    """
    raw = f"{provider}|{limit}|{query}"
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]
    return app.xhs_cache_dir / f"{digest}.json"


def _read_cache(path: Path) -> list[dict] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    version = payload.get("v")
    if version != CACHE_VERSION:
        logger.info(
            "小红书缓存版本不匹配（v%s != v%s），跳过：%s", version, CACHE_VERSION, path.name
        )
        return None
    if time.time() - float(payload.get("ts") or 0) > CACHE_TTL_SECONDS:
        return None
    notes = payload.get("notes")
    return notes if isinstance(notes, list) else None


def _write_cache(path: Path, notes: list[dict]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {"v": CACHE_VERSION, "ts": time.time(), "notes": notes},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    except OSError as exc:
        logger.info("小红书缓存写入失败：%s", exc)


def _notes_to_results(notes: list[dict]) -> list[web_search.SearchResult]:
    results: list[web_search.SearchResult] = []
    for note in notes:
        if not isinstance(note, dict):
            continue
        title = str(note.get("title") or "").strip()
        url = str(note.get("url") or "").strip()
        content = str(note.get("content") or "").strip()
        comments = note.get("comments") or []
        # 正文可以退化成标题，但标题和链接不能缺：没有链接就不是可引用的线索。
        # 搜索卡片常常只有标题，若这里强求正文，整路结果会被清空。
        content = content or title
        if not (title and url and content):
            continue
        joined = "\n".join(
            [content, *(str(item).strip() for item in comments if str(item).strip())]
        )
        image_urls = [str(item) for item in (note.get("images") or []) if str(item).strip()]
        author = note.get("author") if isinstance(note.get("author"), dict) else {}
        results.append(
            web_search.SearchResult(
                title=title,
                url=url,
                snippet=joined[:300],
                content=joined,
                site_name="小红书",
                provider="xiaohongshu",
                image_count=int(note.get("image_count") or len(image_urls)),
                image_urls=image_urls,
                author_id=str(author.get("user_id") or "").strip(),
                author_name=str(author.get("nickname") or "").strip(),
            )
        )
    return results


def _playwright_fetch_notes(
    query: str, app: AppSettings, limit: int, timeout_seconds: float
) -> list[dict]:
    """Playwright 方案：起子进程跑 scripts/xhs_search.py，返回 notes 结构。"""
    command = [
        sys.executable,
        str(script_path()),
        "--query",
        query,
        "--limit",
        str(limit),
        "--open",
        str(min(3, limit)),
        "--profile",
        str(app.xhs_profile_dir),
        "--timeout",
        str(timeout_seconds),
    ]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout_seconds + 20,
        )
    except subprocess.TimeoutExpired as exc:
        raise XhsUnavailable(f"小红书搜索超时（{timeout_seconds:.0f}s）") from exc

    payload: dict = {}
    for line in (completed.stdout or "").strip().splitlines():
        try:
            candidate = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict):
            payload = candidate
    if not payload:
        detail = (completed.stderr or "").strip()[-200:]
        raise XhsUnavailable(f"小红书脚本没有返回 JSON：{detail}")

    if not payload.get("ok"):
        error = str(payload.get("error") or "unknown")
        detail = str(payload.get("detail") or "")
        if error == "not_logged_in":
            (app.xhs_profile_dir / ".logged_in").unlink(missing_ok=True)
        raise XhsUnavailable(f"小红书搜索失败（{error}）：{detail}")

    notes = payload.get("notes") or []
    return notes if isinstance(notes, list) else []


def search(
    query: str,
    app: AppSettings,
    *,
    max_results: int = 3,
    timeout: float | None = None,
) -> list[web_search.SearchResult]:
    """按 query 搜小红书笔记；不可用时抛 ``XhsUnavailable``。"""
    query = (query or "").strip()
    if not query:
        return []
    # 先解析 provider：配置写错时要报「未知 XHS_PROVIDER」，而不是含糊的「未启用」。
    provider = _provider(app)
    if not available(app):
        raise XhsUnavailable("小红书未启用或未登录")

    limit = max(1, min(10, max_results))
    timeout_seconds = float(timeout or app.xhs_timeout)
    cache = _cache_path(app, query, provider=provider, limit=limit)
    cached = _read_cache(cache)
    if cached is not None:
        return _notes_to_results(cached)

    if provider == "spider_xhs":
        from app.services import xhs_spider

        try:
            notes = xhs_spider.fetch_notes(query, app, limit, timeout=timeout_seconds)
        except XhsUnavailable as exc:
            _record_health(provider, ok=False, reason=str(exc))
            raise
    else:
        try:
            notes = _playwright_fetch_notes(query, app, limit, timeout_seconds)
        except XhsUnavailable as exc:
            _record_health(provider, ok=False, reason=str(exc))
            raise

    _record_health(provider, ok=True, empty=not notes)
    _write_cache(cache, notes)
    return _notes_to_results(notes)
