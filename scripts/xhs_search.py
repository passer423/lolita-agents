"""小红书搜索（浏览器自动化）。

小红书网页版必须登录，也没有公开 API。这个脚本用 Playwright + 本机 Edge 的
**持久化 profile**：

    python scripts/xhs_search.py --login   # 第一次：打开浏览器扫码登录
    python scripts/xhs_search.py --query "四时花 预约 价格"

登录会话存在 ``data/xhs/profile``（运行时数据，不进 git）。之后应用里的
``app/services/xhs_search.py`` 会按需调用这个脚本，把结果转成搜索候选。

输出**只有 JSON**（诊断信息走 stderr），失败也输出 JSON，方便上层解析：
    {"ok": true, "query": "...", "notes": [{...}]}
    {"ok": false, "error": "not_logged_in", "detail": "..."}
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from urllib.parse import quote

BASE = "https://www.xiaohongshu.com"
ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROFILE = ROOT / "data" / "xhs" / "profile"

# 搜索页的卡片链接；标题和作者都在卡片里，但直接读链接文本最稳。
CARD_SCRIPT = """() => {
  const seen = new Set();
  const rows = [];
  for (const a of document.querySelectorAll('a[href*="/search_result/"]')) {
    const title = (a.textContent || '').replace(/\\s+/g, ' ').trim();
    const href = a.getAttribute('href') || '';
    if (!title || !href || seen.has(href)) continue;
    seen.add(href);
    rows.push({ title: title.slice(0, 80), href: href.split('&xsec_source')[0] });
    if (rows.length >= 40) break;
  }
  return rows;
}"""

NOTE_SCRIPT = """() => {
  const clean = (value) => (value || '').replace(/\\s+/g, ' ').trim();
  const content = clean(
    (document.querySelector('.note-content, #detail-desc, .note-text') || {}).innerText
  ).slice(0, 1600);
  const comments = [...document.querySelectorAll('.comment-item')]
    .slice(0, 10)
    .map((el) => clean(el.innerText).slice(0, 220))
    .filter(Boolean);
  return { content, comments };
}"""

LOGGED_IN_SCRIPT = """() => {
  const user = window.__INITIAL_STATE__?.user;
  const modal = document.querySelector(
    '.login-container, .login-modal, .sign-in-container, .login-mask'
  );
  // 小红书对游客也会把 loggedIn 置 true，必须同时有 userId 且没有登录弹窗。
  return Boolean(user?.loggedIn && user?.userInfo?.userId && !modal);
}"""


def _launch(profile: Path, *, headless: bool):
    from playwright.sync_api import sync_playwright

    profile.mkdir(parents=True, exist_ok=True)
    playwright = sync_playwright().start()
    context = playwright.chromium.launch_persistent_context(
        user_data_dir=str(profile),
        channel="msedge",
        headless=headless,
        args=["--disable-blink-features=AutomationControlled"],
        viewport={"width": 1280, "height": 900},
    )
    page = context.pages[0] if context.pages else context.new_page()
    page.set_default_timeout(15000)
    return playwright, context, page


def _logged_in(page) -> bool:
    try:
        return bool(page.evaluate(LOGGED_IN_SCRIPT))
    except Exception:  # noqa: BLE001 - 页面还没加载完
        return False


def _login(profile: Path, timeout: float) -> dict:
    playwright, context, page = _launch(profile, headless=False)
    try:
        page.goto(BASE + "/explore", wait_until="domcontentloaded")
        print("请在打开的浏览器里扫码登录小红书…", file=sys.stderr)
        deadline = time.time() + timeout
        while time.time() < deadline:
            if _logged_in(page):
                print("小红书已登录", file=sys.stderr)
                # 给应用一个明确的“已登录”标记，避免每次搜索都先启动浏览器试。
                (profile / ".logged_in").write_text("ok", encoding="utf-8")
                return {"ok": True, "login": True}
            time.sleep(2)
        return {"ok": False, "error": "login_timeout", "detail": "等待登录超时"}
    finally:
        context.close()
        playwright.stop()


def _search(profile: Path, query: str, *, limit: int, open_notes: int, timeout: float) -> dict:
    playwright, context, page = _launch(profile, headless=True)
    try:
        url = BASE + "/search_result?keyword=" + quote(query) + "&type=51"
        page.goto(url, wait_until="domcontentloaded", timeout=int(timeout * 1000))
        try:
            page.wait_for_selector("a[href*='/search_result/']", timeout=15000)
        except Exception:  # noqa: BLE001 - 登录墙或限流
            if not _logged_in(page):
                (profile / ".logged_in").unlink(missing_ok=True)
                return {"ok": False, "error": "not_logged_in", "detail": "小红书未登录"}
            return {"ok": False, "error": "no_results", "detail": "搜索页没有返回笔记"}

        if not _logged_in(page):
            (profile / ".logged_in").unlink(missing_ok=True)
            return {"ok": False, "error": "not_logged_in", "detail": "小红书未登录"}

        rows = page.evaluate(CARD_SCRIPT) or []
        notes = []
        for row in rows[: max(0, open_notes)]:
            note_url = BASE + row["href"]
            try:
                page.goto(note_url, wait_until="domcontentloaded", timeout=int(timeout * 1000))
                page.wait_for_selector(".note-content, #detail-desc, .note-text", timeout=8000)
                detail = page.evaluate(NOTE_SCRIPT) or {}
            except Exception as exc:  # noqa: BLE001 - 单条失败继续
                detail = {"content": "", "comments": [], "error": str(exc)[:120]}
            notes.append(
                {
                    "title": row["title"],
                    "url": note_url,
                    "content": detail.get("content", ""),
                    "comments": detail.get("comments", []),
                    "error": detail.get("error", ""),
                }
            )
        return {"ok": True, "query": query, "notes": notes[:limit]}
    finally:
        context.close()
        playwright.stop()


def main() -> int:
    parser = argparse.ArgumentParser(description="小红书搜索（需先登录）")
    parser.add_argument("--login", action="store_true", help="打开浏览器扫码登录")
    parser.add_argument("--query", default="", help="搜索词")
    parser.add_argument("--limit", type=int, default=5, help="最多返回几条笔记")
    parser.add_argument("--open", type=int, default=3, help="最多打开几篇笔记读正文")
    parser.add_argument("--profile", type=Path, default=DEFAULT_PROFILE, help="登录会话目录")
    parser.add_argument("--timeout", type=float, default=45.0, help="单次页面超时（秒）")
    args = parser.parse_args()

    try:
        if args.login:
            result = _login(args.profile, max(30.0, args.timeout * 6))
        else:
            if not args.query.strip():
                result = {"ok": False, "error": "empty_query"}
            else:
                result = _search(
                    args.profile,
                    args.query.strip(),
                    limit=max(1, args.limit),
                    open_notes=max(0, args.open),
                    timeout=args.timeout,
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
