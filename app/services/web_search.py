"""国内搜索 provider：博查 / 智谱。

只做“按 query 取候选结果”，不做抓取、不做抽取、不判断真假。调用方（召回通道）负责：
* 把结果只当线索；
* 搜索摘要只当本次回答的临时证据，不落盘、不入索引。

没有 `SEARCH_API_KEY` 时明确抛 `SearchUnavailable`，不偷偷降级成猜。
"""

from __future__ import annotations

import json
import logging
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
        }


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
    try:
        with urlopen(request, timeout=timeout, context=imagefetch.ssl_context()) as response:  # noqa: S310
            charset = response.headers.get_content_charset() or "utf-8"
            raw = response.read().decode(charset, errors="replace")
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:200]
        raise SearchUnavailable(f"搜索 HTTP {exc.code}：{detail}") from exc
    except URLError as exc:
        raise SearchUnavailable(f"搜索连不上：{exc.reason}") from exc

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

    results = _dedupe(results)
    logger.info("搜索 %s：%s 条（%s）", text, len(results), provider)
    return results
