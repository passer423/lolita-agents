"""国内搜索 provider：博查 / 智谱。

只做“按 query 取候选结果”，不做抓取、不做抽取、不判断真假。调用方（召回通道）负责：
* 把结果只当线索；
* 搜索摘要只当本次回答的临时证据，不落盘、不入索引。

没有 `SEARCH_API_KEY` 时明确抛 `SearchUnavailable`，不偷偷降级成猜。
"""

from __future__ import annotations

import contextlib
import json
import logging
import subprocess
import sys
import time
from dataclasses import dataclass, field
from html.parser import HTMLParser
from http.cookiejar import CookieJar
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import (
    HTTPCookieProcessor,
    HTTPSHandler,
    Request,
    build_opener,
    urlopen,
)

from app.config import APP_ROOT, AppSettings
from app.services import browser, imagefetch

logger = logging.getLogger(__name__)

BOCHA_URL = "https://api.bochaai.com/v1/web-search"
ZHIPU_URL = "https://open.bigmodel.cn/api/paas/v4/web_search"

SEO360_URL = "https://www.so.com/s"
SEO360_MOBILE_URL = "https://m.so.com/s"
SEO360_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
SEO360_MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)


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

# 无 key 网页检索的**实际调用**健康度。结构就绪（装了浏览器）不等于搜得动：
# 实测 Bing 浏览器 0 条 + 360 风控时，一次要等约 19 秒。这里记录最近一次
# 真实调用的结果，让状态如实降级，并在短窗口内直接跳过，避免每次都干等。
_NO_KEY_HEALTH: dict[str, dict] = {}
_NO_KEY_HEALTH_TTL = 10 * 60
_NO_KEY_DEGRADE_TTL = 10 * 60

# 和配额状态一样落一份小文件：进程重启 / --reload 后内存清零，只靠内存会让
# 状态接口在 Bing 已被风控时又报 ready，直到下一次真实调用才再次变红。
_NO_KEY_STATE_FILE = "web_browser_health.json"


def _no_key_health_key(app: AppSettings) -> str:
    return str(app.data_dir)


def _no_key_state_path(app: AppSettings) -> Path:
    return app.data_dir / "cache" / _NO_KEY_STATE_FILE


def _load_no_key_state(app: AppSettings) -> dict | None:
    try:
        raw = json.loads(_no_key_state_path(app).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return raw if isinstance(raw, dict) else None


def _save_no_key_state(app: AppSettings, entry: dict) -> None:
    path = _no_key_state_path(app)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(entry, ensure_ascii=False), encoding="utf-8")
    except OSError as exc:
        logger.info("无 key 网页健康状态写入失败：%s", exc)


def _no_key_entry(app: AppSettings) -> dict | None:
    key = _no_key_health_key(app)
    entry = _NO_KEY_HEALTH.get(key)
    if entry is None:
        entry = _load_no_key_state(app)
        if entry:
            _NO_KEY_HEALTH[key] = entry
    return entry


def record_no_key_result(app: AppSettings, ok: bool, reason: str = "") -> None:
    entry = {
        "at": time.time(),
        "ok": bool(ok),
        "reason": (reason or "").strip()[:200],
    }
    _NO_KEY_HEALTH[_no_key_health_key(app)] = entry
    _save_no_key_state(app, entry)


def reset_no_key_health() -> None:
    """测试与手动恢复用：清掉最近失败记录（磁盘记录由 TTL 自然过期）。"""
    _NO_KEY_HEALTH.clear()


def no_key_health(app: AppSettings) -> tuple[bool, str]:
    """结构就绪 + 最近真实调用结果；供状态接口如实展示。"""
    ready, reason = no_key_readiness(app)
    if not ready:
        return ready, reason
    entry = _no_key_entry(app)
    if entry and not entry.get("ok"):
        age = time.time() - float(entry.get("at") or 0)
        if age <= _NO_KEY_HEALTH_TTL:
            detail = entry.get("reason") or "最近一次检索失败"
            return False, (
                f"最近一次实际检索失败（{detail}）；"
                f"{int(_NO_KEY_HEALTH_TTL // 60)} 分钟内按不可用降级，之后自动重试"
            )
    return True, ""


def no_key_verified(app: AppSettings) -> tuple[bool, str]:
    """最近一次无 key 调用是否**成功**过（用于「降级路径未验证」的如实展示）。"""
    ready, reason = no_key_readiness(app)
    if not ready:
        return False, reason
    entry = _no_key_entry(app)
    if entry and entry.get("ok"):
        age = time.time() - float(entry.get("at") or 0)
        if age <= _NO_KEY_HEALTH_TTL:
            return True, ""
    return False, f"最近 {int(_NO_KEY_HEALTH_TTL // 60)} 分钟没有成功的无 key 调用记录"


def no_key_blocked(app: AppSettings) -> tuple[bool, str]:
    """最近一次真实失败是否还在「快速跳过」窗口内。

    只影响无 key 路径；付费搜索可用时不会走到这里。
    """
    entry = _no_key_entry(app)
    if not entry or entry.get("ok"):
        return False, ""
    age = time.time() - float(entry.get("at") or 0)
    if age <= _NO_KEY_DEGRADE_TTL:
        return True, entry.get("reason") or "最近一次检索失败"
    return False, ""


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
    # 无 key 的 360 兜底拿不到可点击原链接：``url`` 留空，并在标题里标明
    # 「文本兜底，无原链接」。下游（证据行 / 引用）据此不得把它当可引用链接。
    text_only: bool = False

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
            "text_only": self.text_only,
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


# --------------------------------------------------------------------- 无 key 来源
#
# 公网演示不配付费 key。实测可用性（2026-11）：
# * Bing 浏览器检索：约 2.4s，10 条，链接可点，稳定；
# * 360：约 0.38s，能拿到标题/摘要，但跳转链接返回 400 —— 只能当**文本兜底**；
# * DDG / Google / Brave / Startpage 网络不可达，百度弹滑块，Sogou 不稳定。
# 所以顺序固定为 Bing 浏览器 → 360 文本，不引入其它来源，也不部署 SearXNG。


def paid_config_status(app: AppSettings) -> tuple[bool, str]:
    """付费搜索是否配置完整；不完整时返回可读原因（不发起请求）。"""
    if not app.search_enabled:
        return False, "未启用（SEARCH_ENABLED=false）"
    if not app.search_api_key.strip():
        return False, "缺少 SEARCH_API_KEY"
    provider = (app.search_provider or "bocha").strip().lower() or "bocha"
    if provider not in {"bocha", "zhipu"}:
        return False, f"不支持的 SEARCH_PROVIDER：{provider}（只支持 bocha / zhipu）"
    return True, ""


def no_key_readiness(app: AppSettings) -> tuple[bool, str]:
    """无 key 网页来源是否可用（Bing 浏览器 + 360 文本兜底）。"""
    if not app.web_browser_enabled:
        return False, "未启用（WEB_BROWSER_ENABLED=false）"
    return browser.readiness()


def _results_from_bing(payload: dict) -> list[SearchResult]:
    results: list[SearchResult] = []
    for item in payload.get("results") or []:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        url = str(item.get("url") or "").strip()
        if not title or not url:
            continue
        snippet = str(item.get("snippet") or "").strip()
        results.append(
            SearchResult(
                title=title,
                url=url,
                snippet=snippet,
                content=snippet,
                site_name=str(item.get("site") or "").strip(),
                provider="bing_browser",
            )
        )
    return results


def _bing_browser_search(query: str, *, limit: int, timeout: float) -> list[SearchResult]:
    """调用浏览器脚本检索 Bing；脚本失败或为空时抛异常，交给 360 兜底。"""
    script = APP_ROOT / "scripts" / "bing_search.py"
    if not script.exists():
        raise SearchUnavailable(f"找不到 Bing 检索脚本：{script}")
    command = [
        sys.executable,
        str(script),
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
        raise SearchUnavailable(f"Bing 浏览器检索超时（{timeout:.0f}s）") from exc

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
        raise SearchUnavailable(f"Bing 检索脚本没有返回 JSON：{detail}")
    if not payload.get("ok"):
        error = str(payload.get("error") or "unknown")
        detail = str(payload.get("detail") or "")
        raise SearchUnavailable(f"Bing 浏览器检索失败（{error}）：{detail}")
    return _results_from_bing(payload)


_VOID_TAGS = {
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "param",
    "source",
    "track",
    "wbr",
}


class _So360Parser(HTMLParser):
    """只取 360 结果的标题与摘要，**刻意不取链接**。

    360 的跳转链接会 400，取出来只会让下游误以为可点开。这里连 href 都不读，
    从源头上避免它变成可引用链接。同时兼容桌面（``li.res-list``）与移动页
    常见的结果容器，避免换一个入口就整路为空。
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.items: list[tuple[str, str]] = []
        self._depth = 0
        self._block: dict | None = None
        self._roles: list[str] = []
        self._in_title = 0
        self._in_snippet = 0
        self._snippet_started = False
        self._title: list[str] = []
        self._snippet: list[str] = []

    @staticmethod
    def _classes(attrs: list[tuple[str, str | None]]) -> set[str]:
        return (dict(attrs).get("class") or "").split()

    @classmethod
    def _is_result(cls, tag: str, attrs: list[tuple[str, str | None]]) -> bool:
        if tag not in {"li", "div", "article", "section"}:
            return False
        tokens = cls._classes(attrs)
        return any(
            token == "res-list"
            or token == "g-card"
            or token.startswith("res-")
            or token.startswith("result")
            for token in tokens
        )

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _VOID_TAGS:
            return
        self._depth += 1
        if self._block is None and self._is_result(tag, attrs):
            self._block = {"tag": tag, "depth": self._depth}
            self._title = []
            self._snippet = []
            self._snippet_started = False

        role = ""
        if self._block is not None:
            classes = self._classes(attrs)
            if tag in {"h2", "h3"} or any("title" in token for token in classes):
                role = "title"
                self._in_title += 1
            elif (tag == "p" or any("desc" in token for token in classes)) and not (
                self._snippet_started
            ):
                role = "snippet"
                self._snippet_started = True
                self._in_snippet += 1
        self._roles.append(role)

    def handle_endtag(self, tag: str) -> None:
        if tag in _VOID_TAGS:
            return
        if self._roles:
            role = self._roles.pop()
            if role == "title" and self._in_title:
                self._in_title -= 1
            elif role == "snippet" and self._in_snippet:
                self._in_snippet -= 1
        if self._block is not None and self._depth == self._block["depth"] and tag == self._block["tag"]:
            title = " ".join("".join(self._title).split())
            snippet = " ".join("".join(self._snippet).split())
            if title:
                self.items.append((title, snippet))
            self._block = None
            self._in_title = 0
            self._in_snippet = 0
        self._depth = max(0, self._depth - 1)

    def handle_data(self, data: str) -> None:
        if self._block is None:
            return
        if self._in_title:
            self._title.append(data)
        elif self._in_snippet:
            self._snippet.append(data)


def _results_from_360(html_text: str, *, limit: int) -> list[SearchResult]:
    parser = _So360Parser()
    try:
        parser.feed(html_text or "")
    except Exception:  # noqa: BLE001 - 兜底页面结构变了就当没结果
        return []
    results: list[SearchResult] = []
    for title, snippet in parser.items[: max(1, limit)]:
        results.append(
            SearchResult(
                title=f"{title}（文本兜底，无原链接）",
                url="",
                snippet=snippet,
                content=snippet,
                site_name="360 搜索",
                provider="360_text",
                text_only=True,
            )
        )
    return results


def _fetch_360(url: str, *, ua: str, referer: str, timeout: float) -> str:
    """带 cookie jar 取 360 页面。

    360 对裸请求很敏感：先访问首页拿 cookie，再带 Referer 检索，成功率高很多。
    首页失败不阻断检索（有的节点只拦首页），所以 preflight 单独吞掉异常。
    """
    opener = build_opener(
        HTTPCookieProcessor(CookieJar()),
        HTTPSHandler(context=imagefetch.ssl_context()),
    )
    with contextlib.suppress(HTTPError, URLError, TimeoutError, OSError):
        opener.open(
            Request(referer, headers={"User-Agent": ua}),
            timeout=timeout,
        ).read()
    request = Request(
        url,
        headers={
            "User-Agent": ua,
            "Referer": referer,
            "Accept-Language": "zh-CN,zh;q=0.9",
        },
    )
    with opener.open(request, timeout=timeout) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        return response.read().decode(charset, errors="replace")


def _search_360(query: str, *, limit: int, timeout: float) -> list[SearchResult]:
    """360 文本兜底：桌面页优先，失败再试移动页；连 href 都不读。"""
    per_call = max(1.0, timeout / 3)
    errors: list[str] = []
    attempts = (
        (f"{SEO360_URL}?q={quote(query)}", SEO360_UA, "https://www.so.com/"),
        (f"{SEO360_MOBILE_URL}?q={quote(query)}", SEO360_MOBILE_UA, "https://m.so.com/"),
    )
    for url, ua, referer in attempts:
        try:
            raw = _fetch_360(url, ua=ua, referer=referer, timeout=per_call)
        except HTTPError as exc:
            errors.append(f"HTTP {exc.code}")
            continue
        except (URLError, TimeoutError, OSError) as exc:
            errors.append(f"连不上：{getattr(exc, 'reason', exc)}")
            continue
        results = _results_from_360(raw, limit=limit)
        if results:
            return results
        if "验证码" in raw or "访问异常" in raw:
            errors.append("被风控（验证码）")
        else:
            errors.append("0 条")
    raise SearchUnavailable("360 文本兜底不可用：" + "；".join(errors))


def search_no_key(
    query: str,
    app: AppSettings,
    *,
    max_results: int | None = None,
    timeout: float | None = None,
) -> list[SearchResult]:
    """无 key 网页来源：Bing 浏览器主路，失败/为空才用 360 文本兜底。

    抛 ``SearchUnavailable`` 表示两路都不可用；360 的结果 ``url`` 为空并带
    ``text_only=True``，调用方不得把它当可引用链接。
    """
    if not app.web_browser_enabled:
        raise SearchUnavailable("未启用无 key 网页来源（WEB_BROWSER_ENABLED=false）")

    text = " ".join((query or "").split())[:70]
    if not text:
        raise SearchUnavailable("搜索词为空")

    count = max(1, min(50, max_results or app.web_browser_max_results))
    timeout_seconds = float(timeout or app.web_browser_timeout)
    errors: list[str] = []

    try:
        results = _bing_browser_search(text, limit=count, timeout=timeout_seconds)
        if results:
            logger.info("Bing 浏览器检索 %s：%s 条", text, len(results))
            record_no_key_result(app, True)
            return results
        errors.append("Bing 浏览器检索 0 条")
    except Exception as exc:  # noqa: BLE001 - 主路失败才允许兜底
        errors.append(f"{type(exc).__name__}: {str(exc)[:120]}")

    try:
        results = _search_360(text, limit=count, timeout=min(timeout_seconds, 10.0))
        if results:
            logger.info("Bing 不可用，360 文本兜底 %s：%s 条", text, len(results))
            record_no_key_result(app, True)
            return results
        errors.append("360 文本兜底 0 条")
    except Exception as exc:  # noqa: BLE001 - 兜底失败统一报不可用
        errors.append(f"{type(exc).__name__}: {str(exc)[:120]}")

    detail = "；".join(errors)
    record_no_key_result(app, False, detail)
    raise SearchUnavailable("无 key 网页来源不可用：" + detail)
