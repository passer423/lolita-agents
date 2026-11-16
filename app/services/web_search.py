"""国内搜索 provider：博查 / 智谱。

只做“按 query 取候选结果”，不做抓取、不做抽取、不判断真假。调用方（召回通道）负责：
* 把结果只当线索；
* 搜索摘要只当本次回答的临时证据，不落盘、不入索引。

没有 `SEARCH_API_KEY` 时明确抛 `SearchUnavailable`，不偷偷降级成猜。
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from app.config import AppSettings
from app.services import imagefetch

logger = logging.getLogger(__name__)

BOCHA_URL = "https://api.bochaai.com/v1/web-search"
ZHIPU_URL = "https://open.bigmodel.cn/api/paas/v4/web_search"


class SearchUnavailable(RuntimeError):
    """没有搜索 key / provider 不支持 / 网络失败。带可读原因给通道层。"""


class SearchQuotaExhausted(SearchUnavailable):
    """provider 配额耗尽（智谱 code=1113）：不重试，按 TTL 降级。"""


# 超时 / 5xx / 网络错误允许一次短重试；配额耗尽（1113）不在此列。
_RETRY_ATTEMPTS = 2
_RETRY_DELAY = 0.6

# 配额失效窗口：按 provider 记一个到期时间。账号资源包耗尽不会秒级恢复，
# 每次都真打接口只会让每个通道都白等一轮 0.17s + 失败。
_QUOTA_BLOCKED_UNTIL: dict[str, float] = {}

# 内存标记活不过进程重启；落一份小文件，重启后状态接口也不会继续报 ready。
_QUOTA_STATE_FILE = "search_quota.json"


def _quota_provider(app: AppSettings, provider: str = "") -> str:
    return (provider or app.search_provider or "bocha").strip().lower() or "bocha"


def _load_quota_state(app: AppSettings) -> dict[str, float]:
    try:
        raw = json.loads((app.data_dir / _QUOTA_STATE_FILE).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    state: dict[str, float] = {}
    for name, value in raw.items():
        try:
            state[str(name)] = float(value)
        except (TypeError, ValueError):
            continue
    return state


def _save_quota_state(app: AppSettings, state: dict[str, float]) -> None:
    path = app.data_dir / _QUOTA_STATE_FILE
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    except OSError as exc:
        logger.info("搜索配额状态写入失败：%s", exc)


def quota_blocked(app: AppSettings, provider: str = "") -> bool:
    """配额是否仍在失效窗口内（窗口长度取 ``SEARCH_QUOTA_TTL``）。"""
    name = _quota_provider(app, provider)
    now = time.time()
    until = _QUOTA_BLOCKED_UNTIL.get(name, 0.0)
    if until <= now:
        until = _load_quota_state(app).get(name, 0.0)
        if until > now:
            _QUOTA_BLOCKED_UNTIL[name] = until
    return until > now


def mark_quota_exhausted(app: AppSettings, provider: str = "") -> None:
    ttl = float(app.search_quota_ttl or 0)
    if ttl <= 0:
        return
    name = _quota_provider(app, provider)
    until = time.time() + ttl
    _QUOTA_BLOCKED_UNTIL[name] = until
    state = _load_quota_state(app)
    state[name] = until
    _save_quota_state(app, state)


@dataclass(slots=True)
class SearchResult:
    title: str
    url: str
    snippet: str = ""
    content: str = ""
    site_name: str = ""
    published_at: str = ""
    provider: str = ""
    # 小红书笔记的配图：只带数量与前几张 URL，给「图里可能有价格表」的提示用。
    # 微博 / 网页通道没有这个信息，默认值让它们零改动。
    image_count: int = 0
    image_urls: list[str] = field(default_factory=list)
    # 小红书笔记作者。用来判断这条是不是品牌官方账号发的：官方帖的定价/开团/再贩
    # 时间可以直接采信。约 10% 的搜索卡片没有作者字段，取不到就留空，不做额外请求。
    author_id: str = ""
    author_name: str = ""

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "url": self.url,
            "snippet": self.snippet,
            "content": self.content,
            "site_name": self.site_name,
            "published_at": self.published_at,
            "provider": self.provider,
            "image_count": self.image_count,
            "image_urls": list(self.image_urls),
            "author_id": self.author_id,
            "author_name": self.author_name,
        }


def _quota_exhausted(detail: str) -> bool:
    """智谱资源包耗尽固定返回 ``code=1113``（HTTP 429）。"""
    return "1113" in (detail or "")


def _post_json(url: str, payload: dict, *, key: str, timeout: float) -> dict:
    request = Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json; charset=utf-8",
        },
        method="POST",
    )
    raw = ""
    for attempt in range(_RETRY_ATTEMPTS):
        retryable = attempt + 1 < _RETRY_ATTEMPTS
        try:
            with urlopen(request, timeout=timeout, context=imagefetch.ssl_context()) as response:  # noqa: S310
                charset = response.headers.get_content_charset() or "utf-8"
                raw = response.read().decode(charset, errors="replace")
            break
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:200]
            if _quota_exhausted(detail):
                # 余额/资源包耗尽：重试只会再白等一次，直接交给上层降级。
                raise SearchQuotaExhausted(
                    f"搜索配额耗尽（code=1113）：{detail or '余额不足或无可用资源包'}"
                ) from exc
            if exc.code >= 500 and retryable:
                time.sleep(_RETRY_DELAY)
                continue
            raise SearchUnavailable(f"搜索 HTTP {exc.code}：{detail}") from exc
        except (URLError, TimeoutError) as exc:
            if retryable:
                time.sleep(_RETRY_DELAY)
                continue
            reason = getattr(exc, "reason", exc)
            raise SearchUnavailable(f"搜索连不上：{reason}") from exc

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SearchUnavailable(f"搜索返回不是合法 JSON：{raw[:120]}") from exc
    if not isinstance(data, dict):
        raise SearchUnavailable("搜索返回不是 JSON 对象")
    return data


def _parse_bocha(data: dict) -> list[SearchResult]:
    # 博查有的响应直接给 webPages，有的包一层 data；两种都兼容。
    root = data.get("data") if isinstance(data.get("data"), dict) else data
    web = root.get("webPages") if isinstance(root.get("webPages"), dict) else {}
    values = web.get("value") or web.get("values") or []
    results: list[SearchResult] = []
    for item in values:
        if not isinstance(item, dict):
            continue
        title = str(item.get("name") or item.get("title") or "").strip()
        url = str(item.get("url") or item.get("link") or "").strip()
        if not url:
            continue
        snippet = str(item.get("snippet") or "").strip()
        content = str(item.get("summary") or item.get("content") or snippet).strip()
        results.append(
            SearchResult(
                title=title,
                url=url,
                snippet=snippet,
                content=content,
                site_name=str(item.get("siteName") or item.get("site_name") or "").strip(),
                published_at=str(item.get("datePublished") or item.get("date_published") or "").strip(),
                provider="bocha",
            )
        )
    return results


def _parse_zhipu(data: dict) -> list[SearchResult]:
    values = data.get("search_result") or []
    results: list[SearchResult] = []
    for item in values:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        url = str(item.get("link") or item.get("url") or "").strip()
        if not url:
            continue
        content = str(item.get("content") or "").strip()
        snippet = content[:300]
        results.append(
            SearchResult(
                title=title,
                url=url,
                snippet=snippet,
                content=content,
                site_name=str(item.get("media") or "").strip(),
                published_at=str(item.get("publish_date") or "").strip(),
                provider="zhipu",
            )
        )
    return results


def _dedupe(results: list[SearchResult]) -> list[SearchResult]:
    seen: set[str] = set()
    out: list[SearchResult] = []
    for result in results:
        if result.url in seen:
            continue
        seen.add(result.url)
        out.append(result)
    return out


def search(
    query: str,
    app: AppSettings,
    *,
    max_results: int | None = None,
    provider: str = "",
) -> list[SearchResult]:
    """按 query 取候选结果。没有 key 时抛 SearchUnavailable。

    ``provider`` 可显式指定 ``bocha`` / ``zhipu``，不依赖全局 ``SEARCH_PROVIDER``。
    """
    if not app.search_enabled or not app.search_api_key.strip():
        raise SearchUnavailable(
            "没有配置搜索 API key：在 .env 里填 SEARCH_API_KEY（博查或智谱）"
        )

    text = " ".join((query or "").split())[:70]
    if not text:
        raise SearchUnavailable("搜索词为空")

    provider = (provider or app.search_provider).strip().lower() or "bocha"
    count = max(1, min(50, max_results or app.search_max_results))

    if quota_blocked(app, provider):
        raise SearchQuotaExhausted(
            "搜索配额失效中（上次返回 code=1113），等 TTL 到期再试"
        )

    try:
        if provider == "bocha":
            url = app.search_base_url.strip() or BOCHA_URL
            data = _post_json(
                url,
                {"query": text, "freshness": "noLimit", "summary": True, "count": count},
                key=app.search_api_key.strip(),
                timeout=app.search_timeout,
            )
            results = _parse_bocha(data)
        elif provider == "zhipu":
            url = app.search_base_url.strip() or ZHIPU_URL
            data = _post_json(
                url,
                {
                    "search_query": text,
                    "search_engine": app.search_engine.strip() or "search_std",
                    "search_intent": False,
                    "count": count,
                    "search_recency_filter": "noLimit",
                    "content_size": "high",
                },
                key=app.search_api_key.strip(),
                timeout=app.search_timeout,
            )
            results = _parse_zhipu(data)
        else:
            raise SearchUnavailable(f"不支持的 SEARCH_PROVIDER：{provider}（只支持 bocha / zhipu）")
    except SearchQuotaExhausted:
        mark_quota_exhausted(app, provider)
        raise

    results = _dedupe(results)
    logger.info("搜索 %s：%s 条（%s）", text, len(results), provider)
    return results
