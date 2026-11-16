"""微博搜索（浏览器自动化，应急直抓）。

只用于搜索引擎配额耗尽时的兜底：未登录能拿到的只有 ``m.weibo.cn`` 搜索
**第一页**；翻页会跳登录，所以这里不翻页、不取评论。脚本做三件事：

1. 用 Playwright + 本机 Edge 打开移动端搜索页；
2. 抓首屏 ``api/container/getIndex`` 返回的 JSON，解析 mblog（正文 / 时间 /
   作者 / 图片 / 互动数），丢掉广告与推荐卡片；
3. 长文用 ``statuses/extend`` 补全（这个接口不需要登录）。

输出**只有 JSON**（诊断信息走 stderr），失败也输出 JSON：
    {"ok": true, "query": "...", "posts": [{...}]}
    {"ok": false, "error": "no_payload", "detail": "..."}
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
import time
from urllib.parse import quote

BASE = "https://m.weibo.cn"
SEARCH_PATH = "/search?containerid=100103type%3D1%26q%3D"

# 移动端 UA：桌面 UA 会被访客系统拦成 432 / 登录页。
IPHONE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)
REQUEST_HEADERS = {
    "MWeibo-Pwa": "1",
    "X-Requested-With": "XMLHttpRequest",
    "Referer": "https://m.weibo.cn/",
}

_TAG_RE = re.compile(r"<[^>]+>")
_SEARCH_API = "/api/container/getIndex"

# 广告 / 推广字段：mblog 里出现任意一个就不是普通发言。
_AD_KEYS = ("isAd", "is_ad", "ad_state", "promotion")


def _plain(value: str) -> str:
    """微博正文是 HTML 片段：去标签、反转义、压空白。"""
    text = _TAG_RE.sub(" ", value or "")
    text = html.unescape(text).replace("\u200b", "").replace("\u200c", "")
    return " ".join(text.split())


def _launch():
    from playwright.sync_api import sync_playwright

    playwright = sync_playwright().start()
    browser = playwright.chromium.launch(
        channel="msedge",
        headless=True,
        args=["--disable-blink-features=AutomationControlled"],
    )
    context = browser.new_context(
        user_agent=IPHONE_UA,
        viewport={"width": 414, "height": 896},
        is_mobile=True,
        locale="zh-CN",
    )
    page = context.new_page()
    page.set_default_timeout(15000)
    return playwright, browser, context, page


def _wait_first_page(page, timeout: float) -> dict | None:
    """等首屏 getIndex 返回，取第一份 ``ok == 1`` 的 JSON。"""
    responses = []

    def on_response(response):
        if _SEARCH_API in response.url and "searchall" in response.url:
            responses.append(response)

    page.on("response", on_response)
    deadline = time.time() + timeout
    while time.time() < deadline:
        if responses:
            break
        try:
            page.wait_for_timeout(250)
        except Exception:  # noqa: BLE001 - 页面被关掉/跳转
            break

    for response in responses:
        try:
            payload = response.json()
        except Exception:  # noqa: BLE001 - 非 JSON 的同类请求
            continue
        if isinstance(payload, dict) and payload.get("ok") == 1:
            return payload
    return None


def _extend_text(context, post_id: str, timeout_ms: int) -> str:
    """长文补全：statuses/extend 不需要登录。失败就保留摘要正文。"""
    url = f"{BASE}/statuses/extend?id={quote(str(post_id))}"
    try:
        response = context.request.get(url, headers=REQUEST_HEADERS, timeout=timeout_ms)
        payload = response.json()
    except Exception:  # noqa: BLE001 - 单条补全失败不影响其他帖子
        return ""
    if not isinstance(payload, dict) or payload.get("ok") != 1:
        return ""
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    return _plain(str(data.get("longTextContent") or ""))


def _iter_cards(cards: list) -> list[dict]:
    """搜索卡片可能一层包一层；展平后再按 card_type 筛。"""
    flat: list[dict] = []
    for card in cards:
        if not isinstance(card, dict):
            continue
        flat.append(card)
        for sub in card.get("card_group") or []:
            if isinstance(sub, dict):
                flat.append(sub)
    return flat


def _is_ad(card: dict, mblog: dict) -> bool:
    if any(mblog.get(key) for key in _AD_KEYS):
        return True
    return bool(card.get("promotion"))


def _post_from_mblog(mblog: dict) -> dict | None:
    post_id = str(mblog.get("id") or mblog.get("mid") or "").strip()
    if not post_id:
        return None
    user = mblog.get("user") if isinstance(mblog.get("user"), dict) else {}
    pics: list[str] = []
    for pic in mblog.get("pics") or []:
        if not isinstance(pic, dict):
            continue
        large = pic.get("large") if isinstance(pic.get("large"), dict) else {}
        url = str(large.get("url") or pic.get("url") or "").strip()
        if url:
            pics.append(url)
    return {
        "id": post_id,
        "url": f"{BASE}/detail/{post_id}",
        "text": _plain(str(mblog.get("longText") or mblog.get("text") or "")),
        "created_at": str(mblog.get("created_at") or "").strip(),
        "author_id": str(user.get("id") or "").strip(),
        "author_name": str(user.get("screen_name") or "").strip(),
        "pics": pics,
        "reposts": int(mblog.get("reposts_count") or 0),
        "comments": int(mblog.get("comments_count") or 0),
        "likes": int(mblog.get("attitudes_count") or 0),
        "is_long_text": bool(mblog.get("isLongText")),
    }


def _posts_from_payload(payload: dict) -> list[dict]:
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    cards = data.get("cards") if isinstance(data.get("cards"), list) else []
    posts: list[dict] = []
    seen: set[str] = set()
    for card in _iter_cards(cards):
        # 9 才是微博正文卡；推荐位、话题位、广告位都在别的类型里。
        try:
            card_type = int(card.get("card_type") or 0)
        except (TypeError, ValueError):
            card_type = 0
        if card_type != 9:
            continue
        mblog = card.get("mblog")
        if not isinstance(mblog, dict) or _is_ad(card, mblog):
            continue
        post = _post_from_mblog(mblog)
        if post is None or post["id"] in seen or not post["text"]:
            continue
        seen.add(post["id"])
        posts.append(post)
    return posts


def _search(query: str, *, limit: int, timeout: float) -> dict:
    playwright, browser, context, page = _launch()
    try:
        try:
            page.goto(
                BASE + SEARCH_PATH + quote(query),
                wait_until="domcontentloaded",
                timeout=int(min(timeout, 20) * 1000),
            )
        except Exception as exc:  # noqa: BLE001 - 超时也可能已经发出首屏请求
            print(f"search page load: {type(exc).__name__}", file=sys.stderr)

        payload = _wait_first_page(page, timeout)
        if payload is None:
            return {
                "ok": False,
                "error": "no_payload",
                "detail": f"首屏接口没有返回（{page.url}）",
            }

        posts = _posts_from_payload(payload)[: max(1, limit)]
        deadline = time.time() + timeout
        for post in posts:
            if not post["is_long_text"]:
                continue
            remaining = deadline - time.time()
            if remaining <= 1:
                break
            full = _extend_text(context, post["id"], int(min(remaining, 8) * 1000))
            if full and len(full) > len(post["text"]):
                post["text"] = full
        return {"ok": True, "query": query, "posts": posts}
    finally:
        context.close()
        browser.close()
        playwright.stop()


def main() -> int:
    parser = argparse.ArgumentParser(description="微博搜索（应急直抓，只读第一页）")
    parser.add_argument("--query", default="", help="搜索词")
    parser.add_argument("--limit", type=int, default=20, help="最多返回几条帖子")
    parser.add_argument("--timeout", type=float, default=25.0, help="浏览器启动 + 首屏的总预算（秒）")
    args = parser.parse_args()

    try:
        if not args.query.strip():
            result = {"ok": False, "error": "empty_query"}
        else:
            result = _search(
                args.query.strip(),
                limit=max(1, args.limit),
                timeout=max(5.0, args.timeout),
            )
    except ImportError:
        result = {
            "ok": False,
            "error": "playwright_missing",
            "detail": "未安装 playwright：python -m pip install playwright",
        }
    except Exception as exc:  # noqa: BLE001 - 统一输出 JSON，不把 traceback 混进 stdout
        result = {"ok": False, "error": type(exc).__name__, "detail": str(exc)[:300]}

    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
