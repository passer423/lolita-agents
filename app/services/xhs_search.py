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

from app.config import APP_ROOT, AppSettings
from app.services import web_search

logger = logging.getLogger(__name__)

CACHE_TTL_SECONDS = 6 * 3600


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
    if time.time() - float(payload.get("ts") or 0) > CACHE_TTL_SECONDS:
        return None
    notes = payload.get("notes")
    return notes if isinstance(notes, list) else None


def _write_cache(path: Path, notes: list[dict]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"ts": time.time(), "notes": notes}, ensure_ascii=False),
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
        results.append(
            web_search.SearchResult(
                title=title,
                url=url,
                snippet=joined[:300],
                content=joined,
                site_name="小红书",
                provider="xiaohongshu",
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

        notes = xhs_spider.fetch_notes(query, app, limit, timeout=timeout_seconds)
    else:
        notes = _playwright_fetch_notes(query, app, limit, timeout_seconds)

    _write_cache(cache, notes)
    return _notes_to_results(notes)
