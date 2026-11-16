"""微博应急直抓：只在智谱返回 1113（余额 / 资源包耗尽）时启用。

替换 provider 的代价太大，这里只做**并列兜底**：调用方发现网页搜索配额失效时，
改走 ``scripts/weibo_search.py`` 直抓 ``m.weibo.cn`` 搜索第一页。

边界（实测结论，不要放宽）：

* 未登录只拿得到第一页；翻页会跳登录墙，所以不翻页、不取评论；
* 结果只当**网友发言线索**（``upstream_ugc`` 等级由调用方决定），不是官方事实；
* 6 小时缓存只为避免同一 query 反复启动浏览器，不进索引；
* 单次查询有总预算，超时按通道 skip 降级，不影响其他通道。
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import logging
import re
import subprocess
import sys
import time
from pathlib import Path

from app.config import APP_ROOT, AppSettings
from app.services import web_search

logger = logging.getLogger(__name__)

CACHE_TTL_SECONDS = 6 * 3600

# v1：直抓通道的第一版结构。以后正文/图片/作者字段变了就升版本，
# 让旧缓存失效重抓——否则界面会连续 6 小时看不到新字段，像功能没生效。
CACHE_VERSION = 1

# 单条证据最多带几张图给 OCR；微博九宫格全带会把下载预算吃光。
MAX_IMAGES = 5


class WeiboDirectUnavailable(RuntimeError):
    """微博直抓不可用（未启用 / 没装 Playwright / 超时 / 登录墙）。"""


def script_path() -> Path:
    return APP_ROOT / "scripts" / "weibo_search.py"


def readiness(app: AppSettings) -> tuple[bool, str]:
    """直抓是否可用；状态接口与召回共用这一份判断。"""
    if not app.weibo_direct_enabled:
        return False, "未启用（WEIBO_DIRECT_ENABLED=false）"
    if not script_path().exists():
        return False, f"找不到直抓脚本：{script_path()}"
    if importlib.util.find_spec("playwright") is None:
        return False, "未安装 playwright（pip install playwright）"
    return True, ""


def _cache_path(app: AppSettings, query: str, limit: int) -> Path:
    """缓存键带来源与条数：换实现或调大条数必须重新抓，不能吃旧结果。"""
    raw = f"weibo_direct|{limit}|{query}"
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]
    return app.weibo_cache_dir / f"{digest}.json"


def _read_cache(path: Path) -> list[dict] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    version = payload.get("v")
    if version != CACHE_VERSION:
        logger.info(
            "微博直抓缓存版本不匹配（v%s != v%s），跳过：%s", version, CACHE_VERSION, path.name
        )
        return None
    if time.time() - float(payload.get("ts") or 0) > CACHE_TTL_SECONDS:
        return None
    posts = payload.get("posts")
    return posts if isinstance(posts, list) else None


def _write_cache(path: Path, posts: list[dict]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {"v": CACHE_VERSION, "ts": time.time(), "posts": posts},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    except OSError as exc:
        logger.info("微博直抓缓存写入失败：%s", exc)


_LATIN_RE = re.compile(r"[a-z0-9]{2,}")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]+")

# 纯中文查询拆词时丢掉的疑问/功能字：留在词里会让「四时花的价格大概多少」
# 变成一个整串，永远命中不了只写「四时花」的帖子。
_QUERY_STOP_CHARS = set(
    "的了是在和与或及吗呢啊吧都有什么怎么如何哪些哪个哪家能否可以买想看看"
    "多少几价格钱元块约大概左右求问一下测评口碑怎么样"
)

# 转发/分享类空壳：正文没有信息，占着证据位只会让 writer 复述一句废话。
_EMPTY_TEXTS = {"转发微博", "轉發微博", "分享图片", "分享圖片", "网页链接"}


def _term_in(term: str, text: str) -> bool:
    """拉丁词按词边界匹配，避免 ``ap`` 命中 Cape / wrap。"""
    if term.isascii():
        return re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", text) is not None
    return term in text


def _query_chunks(query: str) -> list[str]:
    chunks: list[str] = []
    buf: list[str] = []
    for ch in query or "":
        if ch in _QUERY_STOP_CHARS or not _CJK_RE.match(ch):
            if buf:
                chunks.append("".join(buf))
                buf = []
            continue
        buf.append(ch)
    if buf:
        chunks.append("".join(buf))
    return [chunk for chunk in chunks if len(chunk) >= 2]


def _relevant(query: str, text: str) -> bool:
    """帖子正文与查询词要有实质重合。

    规则与搜索通道的 ``_weibo_relevant`` 一致：查询里有拉丁词时必须命中至少
    一个（品牌/款式是身份）；纯中文查询命中**最长片段**（通常是品牌/系列名），
    只共享「秋日私语」这类泛化短语不算相关。
    """
    lowered = (text or "").lower()
    latin = _LATIN_RE.findall((query or "").lower())
    if latin:
        return any(_term_in(token, lowered) for token in latin)
    chunks = _query_chunks(query)
    if chunks:
        return _term_in(max(chunks, key=len), lowered)
    return True


def _is_ad(post: dict) -> bool:
    return bool(post.get("is_ad") or post.get("isAd") or post.get("promotion"))


def _filter_posts(query: str, posts: list[dict], limit: int) -> list[dict]:
    """丢掉广告、转发空壳、重复与无关帖；留多少按实际召回质量定。"""
    out: list[dict] = []
    seen: set[str] = set()
    for post in posts:
        if not isinstance(post, dict):
            continue
        if _is_ad(post):
            continue
        text = str(post.get("text") or "").strip()
        url = str(post.get("url") or "").strip()
        if not text or not url or url in seen:
            continue
        if text in _EMPTY_TEXTS:
            continue
        if not _relevant(query, text):
            continue
        seen.add(url)
        out.append(post)
        if len(out) >= limit:
            break
    return out


def _posts_to_results(posts: list[dict]) -> list[web_search.SearchResult]:
    results: list[web_search.SearchResult] = []
    for post in posts:
        if not isinstance(post, dict):
            continue
        text = str(post.get("text") or "").strip()
        url = str(post.get("url") or "").strip()
        if not text or not url:
            continue
        pics = [str(item).strip() for item in (post.get("pics") or []) if str(item).strip()]
        # 标题只用来让证据列表有可读的一行；正文不截断，价格往往在后半段。
        title = text[:60]
        results.append(
            web_search.SearchResult(
                title=title,
                url=url,
                snippet=text[:300],
                content=text,
                site_name="微博",
                published_at=str(post.get("created_at") or "").strip(),
                provider="weibo_direct",
                image_count=len(pics),
                image_urls=pics[:MAX_IMAGES],
                author_id=str(post.get("author_id") or "").strip(),
                author_name=str(post.get("author_name") or "").strip(),
            )
        )
    return results


def _fetch_posts(query: str, app: AppSettings, *, limit: int, timeout: float) -> list[dict]:
    command = [
        sys.executable,
        str(script_path()),
        "--query",
        query,
        "--limit",
        str(limit),
        "--timeout",
        str(timeout),
    ]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout + 20,
        )
    except subprocess.TimeoutExpired as exc:
        raise WeiboDirectUnavailable(f"微博直抓超时（{timeout:.0f}s）") from exc

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
        raise WeiboDirectUnavailable(f"微博直抓脚本没有返回 JSON：{detail}")

    if not payload.get("ok"):
        error = str(payload.get("error") or "unknown")
        detail = str(payload.get("detail") or "")
        if error in {"no_payload", "login_required"}:
            raise WeiboDirectUnavailable(f"微博直抓被登录墙拦住（{error}）：{detail}")
        raise WeiboDirectUnavailable(f"微博直抓失败（{error}）：{detail}")

    posts = payload.get("posts") or []
    return posts if isinstance(posts, list) else []


def search(
    query: str,
    app: AppSettings,
    *,
    max_results: int = 5,
    timeout: float | None = None,
) -> list[web_search.SearchResult]:
    """按 query 直抓微博第一页；不可用时抛 ``WeiboDirectUnavailable``。"""
    query = (query or "").strip()
    if not query:
        return []
    ready, reason = readiness(app)
    if not ready:
        raise WeiboDirectUnavailable(reason)

    limit = max(1, min(10, max_results))
    timeout_seconds = float(timeout or app.weibo_direct_timeout)
    cache = _cache_path(app, query, limit)
    cached = _read_cache(cache)
    if cached is not None:
        return _posts_to_results(cached)

    posts = _fetch_posts(query, app, limit=limit, timeout=timeout_seconds)
    kept = _filter_posts(query, posts, limit)
    _write_cache(cache, kept)
    logger.info("微博直抓 %s：原始 %s 条，过滤后 %s 条", query, len(posts), len(kept))
    return _posts_to_results(kept)
