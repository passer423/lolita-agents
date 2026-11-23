"""Bing 浏览器检索（无 key 网页来源的主路）。

公网演示没有付费搜索 key。实测国内网络下 Bing 的浏览器页面可用、链接可点，
而 DDG / Google / Brave / Startpage 连不上，百度弹滑块——所以只做 Bing。
Playwright 同步 API 不能跑在 FastAPI 事件循环里，脚本化后由服务层用子进程调用。

输出**只有 JSON**（诊断走 stderr），失败也输出 JSON：
    {"ok": true, "query": "...", "results": [{"title","url","snippet","site"}], "browser": "msedge"}
    {"ok": false, "error": "browser_missing", "detail": "..."}
"""

from __future__ import annotations

import argparse
import json
import time
from urllib.parse import quote, urlsplit

from _browser import BrowserMissing, launch_browser

BASE = "https://cn.bing.com/search"
DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# 只读自然结果卡；广告卡（b_ad）与侧栏推荐不取。
_EXTRACT_JS = """
() => Array.from(document.querySelectorAll('li.b_algo')).map((li) => {
  if ((li.className || '').split(/\\s+/).includes('b_ad')) return null;
  const link = li.querySelector('h2 a');
  if (!link) return null;
  const caption = li.querySelector('.b_caption p') || li.querySelector('.b_caption');
  return {
    title: (link.innerText || link.textContent || '').trim(),
    url: link.href || '',
    snippet: (caption ? (caption.innerText || caption.textContent || '') : '').trim(),
    site: (() => {
      try { return new URL(link.href).hostname; } catch (err) { return ''; }
    })(),
  };
}).filter(Boolean)
"""


def _clean(items: list, limit: int) -> list[dict]:
    results: list[dict] = []
    seen: set[str] = set()
    for item in items or []:
        if not isinstance(item, dict):
            continue
        title = " ".join(str(item.get("title") or "").split())
        url = str(item.get("url") or "").strip()
        if not title or not url or not url.startswith(("http://", "https://")):
            continue
        host = str(item.get("site") or "").strip() or (urlsplit(url).hostname or "")
        if url in seen:
            continue
        seen.add(url)
        results.append(
            {
                "title": title,
                "url": url,
                "snippet": " ".join(str(item.get("snippet") or "").split()),
                "site": host,
            }
        )
        if len(results) >= max(1, limit):
            break
    return results


def _search(query: str, *, limit: int, timeout: float) -> dict:
    from playwright.sync_api import sync_playwright

    playwright = sync_playwright().start()
    browser = None
    context = None
    try:
        browser, channel = launch_browser(playwright)
        context = browser.new_context(
            user_agent=DESKTOP_UA,
            viewport={"width": 1280, "height": 900},
            locale="zh-CN",
        )
        page = context.new_page()
        page.set_default_timeout(15000)
        deadline = time.time() + timeout
        page.goto(
            f"{BASE}?q={quote(query)}&setlang=zh-Hans",
            wait_until="domcontentloaded",
            timeout=int(min(timeout, 20) * 1000),
        )
        remaining_ms = max(1000, int((deadline - time.time()) * 1000))
        try:
            page.wait_for_selector("li.b_algo", timeout=min(remaining_ms, 15000))
        except Exception:  # noqa: BLE001 - 没有结果就是空，交给 360 兜底
            return {"ok": True, "query": query, "results": [], "browser": channel}
        results = _clean(page.evaluate(_EXTRACT_JS), limit)
        return {"ok": True, "query": query, "results": results, "browser": channel}
    finally:
        if context is not None:
            context.close()
        if browser is not None:
            browser.close()
        playwright.stop()


def main() -> int:
    parser = argparse.ArgumentParser(description="Bing 浏览器检索（无 key 网页来源）")
    parser.add_argument("--query", default="", help="搜索词")
    parser.add_argument("--limit", type=int, default=10, help="最多返回几条结果")
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
    except BrowserMissing as exc:
        result = {"ok": False, "error": "browser_missing", "detail": str(exc)}
    except Exception as exc:  # noqa: BLE001 - 统一输出 JSON，不把 traceback 混进 stdout
        result = {"ok": False, "error": type(exc).__name__, "detail": str(exc)[:300]}

    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
