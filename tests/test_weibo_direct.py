"""微博应急直抓：过滤、缓存、超时与配额降级。

直抓只在搜索配额 1113 时启用，所以这里不联网：子进程、搜索接口全部替掉，
只钉死「过滤对不对、缓存命中是否还起浏览器、失败是否按 skip 上报」。
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from app.agent import channels
from app.agent.channels import ChannelUnavailable, RecallContext
from app.config import AppSettings
from app.services import web_search, weibo_direct


def _post(url: str, text: str, **extra) -> dict:
    post = {
        "url": url,
        "text": text,
        "created_at": "2026-11-15",
        "author_id": "1",
        "author_name": "网友",
        "pics": [],
    }
    post.update(extra)
    return post


# --------------------------------------------------------------------- 过滤


def test_filter_drops_ads_retweets_and_irrelevant_posts():
    posts = [
        _post("https://m.weibo.cn/detail/1", "四时花长款预约价 578，现货 598"),
        _post("https://m.weibo.cn/detail/2", "四时花 代购 广告", is_ad=True),
        _post("https://m.weibo.cn/detail/3", "秋天到了，落叶很美"),
        _post("https://m.weibo.cn/detail/4", "转发微博"),
        _post("https://m.weibo.cn/detail/5", "四时花 尾款 388"),
        _post("https://m.weibo.cn/detail/1", "重复的同一帖"),
    ]

    kept = weibo_direct._filter_posts("四时花 价格", posts, 5)

    assert [post["url"] for post in kept] == [
        "https://m.weibo.cn/detail/1",
        "https://m.weibo.cn/detail/5",
    ]


def test_filter_requires_latin_brand_when_query_has_latin():
    posts = [
        _post("https://m.weibo.cn/detail/1", "秋日私语：在落叶中寻找诗意"),
        _post("https://m.weibo.cn/detail/2", "Baby 秋日私语 JSK 返图"),
    ]

    kept = weibo_direct._filter_posts("Baby 秋日私语 JSK", posts, 5)

    assert [post["url"] for post in kept] == ["https://m.weibo.cn/detail/2"]


def test_posts_to_results_keep_images_and_time_for_ocr():
    results = weibo_direct._posts_to_results(
        [
            {
                **_post("https://m.weibo.cn/detail/1", "四时花预约价见图"),
                "created_at": "11-15 12:34",
                "pics": ["https://wx1.sinaimg.cn/a.jpg"],
            }
        ]
    )

    assert len(results) == 1
    item = results[0]
    assert item.published_at == "11-15 12:34"
    assert item.image_count == 1
    assert item.image_urls == ["https://wx1.sinaimg.cn/a.jpg"]
    assert item.provider == "weibo_direct"


def test_weibo_image_host_needs_referer_for_ocr_download():
    """新浪图床防盗链：不带 Referer 下载 403，图片就进不了 OCR。"""
    from app.services import imagefetch

    assert imagefetch.referer_for("https://wx1.sinaimg.cn/a.jpg") == "https://m.weibo.cn/"
    assert imagefetch.referer_for("https://example.com/a.jpg") == ""


# --------------------------------------------------------------------- 缓存


def test_cache_hit_does_not_start_browser(monkeypatch, workdir):
    app = AppSettings(data_dir=workdir, weibo_direct_enabled=True)
    calls: list[int] = []

    def fake_fetch(*_args, **_kwargs):
        calls.append(1)
        return [_post("https://m.weibo.cn/detail/1", "四时花预约价 578，现货 598")]

    monkeypatch.setattr(weibo_direct, "_fetch_posts", fake_fetch)
    # 这个用例只验缓存是否复用：可用性检查是另一个维度的环境依赖（托管 CI 不装
    # Playwright），显式打桩，避免测试结论随 runner 装没装浏览器而变。
    monkeypatch.setattr(weibo_direct, "readiness", lambda _app: (True, ""))

    first = weibo_direct.search("四时花 价格", app, max_results=5)
    second = weibo_direct.search("四时花 价格", app, max_results=5)

    assert len(calls) == 1
    assert [item.url for item in second] == [item.url for item in first]


# --------------------------------------------------------------------- 超时与降级


def test_subprocess_timeout_is_reported_as_unavailable(monkeypatch):
    app = AppSettings()

    def fake_run(*_args, **_kwargs):
        raise subprocess.TimeoutExpired("weibo", 1)

    monkeypatch.setattr(weibo_direct.subprocess, "run", fake_run)

    with pytest.raises(weibo_direct.WeiboDirectUnavailable, match="超时"):
        weibo_direct._fetch_posts("四时花 价格", app, limit=5, timeout=1)


def test_timeout_degrades_to_channel_skip(monkeypatch):
    """超时不能抛到上层：通道层要把它变成 ChannelUnavailable（graph 记 skip）。"""
    app = AppSettings(search_enabled=True, search_api_key="k", search_provider="zhipu")
    monkeypatch.setattr(web_search, "quota_blocked", lambda *_a, **_k: True)
    monkeypatch.setattr(
        weibo_direct,
        "search",
        lambda *_a, **_k: (_ for _ in ()).throw(
            weibo_direct.WeiboDirectUnavailable("微博直抓超时（25s）")
        ),
    )

    with pytest.raises(ChannelUnavailable, match="微博直抓不可用"):
        channels.recall("weibo", "四时花 价格", RecallContext(settings=app))


# --------------------------------------------------------------------- 1113 与状态


def test_quota_1113_switches_weibo_to_direct(monkeypatch):
    app = AppSettings(search_enabled=True, search_api_key="k", search_provider="zhipu")
    monkeypatch.setattr(web_search, "quota_blocked", lambda *_a, **_k: False)

    def fake_search(*_args, **_kwargs):
        raise web_search.SearchQuotaExhausted("搜索配额耗尽（code=1113）")

    calls: list[int] = []

    def fake_direct(*_args, **_kwargs):
        calls.append(1)
        return [
            web_search.SearchResult(
                title="四时花预约价",
                url="https://m.weibo.cn/detail/1",
                content="四时花长款预约价 578，现货 598",
                image_count=1,
                image_urls=["https://wx1.sinaimg.cn/a.jpg"],
            )
        ]

    monkeypatch.setattr(web_search, "search", fake_search)
    monkeypatch.setattr(weibo_direct, "search", fake_direct)

    result = channels.recall("weibo", "四时花 价格", RecallContext(settings=app))

    assert len(calls) == 1
    assert result[0]["channel"] == "weibo"
    assert result[0]["source_kind"] == "realtime"
    assert result[0]["image_urls"] == ["https://wx1.sinaimg.cn/a.jpg"]


def test_search_status_degrades_while_quota_blocked(monkeypatch):
    app = AppSettings(search_enabled=True, search_api_key="k", search_provider="zhipu")
    monkeypatch.setitem(web_search._QUOTA_BLOCKED_UNTIL, "zhipu", time.time() + 900)
    # 本机可能恰好有浏览器；关注建议的断言要固定环境，不能随 runner 漂移。
    monkeypatch.setattr(web_search, "no_key_readiness", lambda _app: (False, "测试环境"))

    status = channels._search_status(app)

    assert status["ready"] is False
    assert "1113" in status["reason"]
    # 网页搜索不可用时，关注建议不能继续指向 web。
    assert channels.follow_source(app) == ""


def test_weibo_direct_is_independent_source_without_search_key(monkeypatch):
    """没有付费 key 时直抓不是兜底，而是主路；且不得调用付费搜索。"""
    app = AppSettings(search_enabled=False, search_api_key="", weibo_direct_enabled=True)
    paid_calls: list[int] = []

    def fake_paid(*_args, **_kwargs):
        paid_calls.append(1)
        return []

    monkeypatch.setattr(web_search, "search", fake_paid)
    monkeypatch.setattr(
        weibo_direct,
        "search",
        lambda *_a, **_k: [
            web_search.SearchResult(
                title="四时花预约价",
                url="https://m.weibo.cn/detail/1",
                content="四时花长款预约价 578，现货 598",
            )
        ],
    )

    items = channels.recall("weibo", "四时花 价格", RecallContext(settings=app))

    assert paid_calls == []
    assert items[0]["channel"] == "weibo"
    assert items[0]["url"] == "https://m.weibo.cn/detail/1"


def test_weibo_status_without_key_reports_direct(monkeypatch):
    app = AppSettings(search_enabled=False, search_api_key="")
    monkeypatch.setattr(weibo_direct, "readiness", lambda _app: (True, ""))

    status = channels._weibo_status(app)

    assert status["enabled"] is True
    assert status["ready"] is True
    assert status["mode"] == "direct"
    assert "直抓" in status["reason"]

    monkeypatch.setattr(weibo_direct, "readiness", lambda _app: (False, "未安装 playwright"))
    blocked = channels._weibo_status(app)
    assert blocked["ready"] is False
    assert blocked["mode"] == "direct"
    assert "未安装 playwright" in blocked["reason"]


def test_weibo_status_uses_direct_when_quota_blocked(monkeypatch):
    app = AppSettings(search_enabled=True, search_api_key="k", search_provider="zhipu")
    monkeypatch.setattr(web_search, "quota_blocked", lambda *_a, **_k: True)

    monkeypatch.setattr(weibo_direct, "readiness", lambda _app: (True, ""))
    ready = channels._weibo_status(app)
    assert ready["ready"] is True
    assert ready["mode"] == "direct"
    assert "配额失效" in ready["reason"]

    monkeypatch.setattr(weibo_direct, "readiness", lambda _app: (False, "未安装 playwright"))
    blocked = channels._weibo_status(app)
    assert blocked["ready"] is False
    assert blocked["mode"] == "direct"
    assert "未安装 playwright" in blocked["reason"]


def test_web_channel_uses_no_key_search_without_paid_key(monkeypatch):
    app = AppSettings(search_enabled=False, search_api_key="", web_browser_enabled=True)
    calls: list[str] = []

    def fake_no_key(*_args, **_kwargs):
        calls.append("no_key")
        return [
            web_search.SearchResult(
                title="四时花预约价",
                url="https://example.com/1",
                content="长款 578 元",
                provider="bing_browser",
            )
        ]

    monkeypatch.setattr(web_search, "search_no_key", fake_no_key)
    monkeypatch.setattr(
        web_search,
        "search",
        lambda *_a, **_k: pytest.fail("没有 key 时不应调用付费搜索"),
    )

    items = channels.recall("web", "四时花 价格", RecallContext(settings=app))

    assert calls == ["no_key"]
    assert items[0]["url"] == "https://example.com/1"


def test_web_status_uses_browser_without_key(monkeypatch):
    app = AppSettings(search_enabled=False, search_api_key="", web_browser_enabled=True)
    monkeypatch.setattr(web_search, "no_key_readiness", lambda _app: (True, ""))

    status = channels._web_status(app)

    assert status["enabled"] is True
    assert status["ready"] is True
    assert status["mode"] == "browser"
    assert "Bing" in status["reason"]

    monkeypatch.setattr(web_search, "no_key_readiness", lambda _app: (False, "找不到可用浏览器"))
    blocked = channels._web_status(app)
    assert blocked["ready"] is False
    assert blocked["mode"] == "browser"
    assert "找不到可用浏览器" in blocked["reason"]


def test_web_status_respects_switch_when_no_paid_key():
    app = AppSettings(search_enabled=False, search_api_key="", web_browser_enabled=False)

    status = channels._web_status(app)

    assert status["ready"] is False
    assert status["mode"] == "unavailable"
    assert "WEB_BROWSER_ENABLED=false" in status["reason"]


# --------------------------------------------------------------------- 浏览器自动选择

ROOT = Path(__file__).resolve().parents[1]


def _load_script(name: str):
    scripts_dir = str(ROOT / "scripts")
    added = scripts_dir not in sys.path
    if added:
        sys.path.insert(0, scripts_dir)
    try:
        spec = importlib.util.spec_from_file_location(f"_test_{name}", ROOT / "scripts" / f"{name}.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
    finally:
        if added:
            sys.path.remove(scripts_dir)
    return module


class _FakeChromium:
    def __init__(self, fail_channels: set[str]) -> None:
        self.fail_channels = fail_channels
        self.calls: list[dict] = []

    def launch(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs.get("channel") in self.fail_channels:
            raise RuntimeError(f"{kwargs['channel']} 不存在")
        return object()


class _FakePlaywright:
    def __init__(self, fail_channels: set[str]) -> None:
        self.chromium = _FakeChromium(fail_channels)


def test_weibo_launch_prefers_edge_then_falls_back_to_chromium():
    browser = _load_script("_browser")

    pw = _FakePlaywright(fail_channels={"msedge"})
    _, channel = browser.launch_browser(pw)

    assert channel == "chromium"
    assert [call.get("channel") for call in pw.chromium.calls] == ["msedge", None]


def test_weibo_launch_reports_clear_error_when_no_browser():
    browser = _load_script("_browser")
    pw = _FakePlaywright(fail_channels={"msedge", None})

    with pytest.raises(browser.BrowserMissing, match="找不到可用浏览器"):
        browser.launch_browser(pw)


def test_weibo_script_emits_browser_missing_json(monkeypatch, capsys):
    script = _load_script("weibo_search")
    monkeypatch.setattr(sys, "argv", ["weibo_search.py", "--query", "四时花"])

    def fake_search(*_args, **_kwargs):
        raise script.BrowserMissing("找不到可用浏览器：Edge 与 Chromium 都不可用")

    monkeypatch.setattr(script, "_search", fake_search)

    assert script.main() == 1
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["ok"] is False
    assert payload["error"] == "browser_missing"
    assert "找不到可用浏览器" in payload["detail"]
