"""品牌官网源（AP / BABY）解析、缓存与降级测试。

全部离线：解析用固定 HTML 片段，抓取用注入的假 ``_fetch``。
"""

from __future__ import annotations

import time

import pytest

from app.agent import channels
from app.agent.channels import ChannelUnavailable, RecallContext
from app.config import AppSettings
from app.services import brand_site, web_search

AP_HTML = """
<ul>
  <li class="archive__col">
    <a href="/Page/Feature/202610.aspx"><img src="cover.jpg" alt=""></a>
    <h3 class="archive__ttl">Melty Berry Princessシリーズ入荷のご案内</h3>
    <a href="/Page/Feature/202610.aspx">READ MORE</a>
  </li>
  <li class="archive__col">
    <a href="/Page/Feature/utsunomiya.aspx">闭店</a>
    <h3 class="archive__ttl">宇都宮店閉店のお知らせ</h3>
  </li>
</ul>
"""

BABY_HTML = """
<article class="loop-archive post type-post">
  <a href="https://www.babyssb.co.jp/official_news/20270207_brand-new-collection-in-tokyo/">
    <figure><img src="cover.jpg" alt=""></figure>
  </a>
  <div>
    <time datetime="2026-11-20T00:00:00+09:00">2026.11.20</time>
    <h2>BABY,THE STARS SHINE BRIGHT ALICE and the PIRATES BRAND-NEW COLLECTION in TOKYO</h2>
  </div>
</article>
<article class="loop-archive post type-post">
  <a href="https://www.babyssb.co.jp/official_news/gallery/20260201_report/">
    <figure><img src="report.jpg" alt=""></figure>
  </a>
  <div>
    <time datetime="2026-09-30">2026.09.30</time>
    <h2>BRAND-NEW COLLECTION in TOKYO Report</h2>
  </div>
</article>
"""


def _app(tmp_path) -> AppSettings:
    return AppSettings(data_dir=tmp_path / "data", content_dir=tmp_path / "content")


def test_ap_list_parses_title_date_and_link():
    site = next(item for item in brand_site.SITES if item.id == "angelic_pretty")
    items = brand_site.parse_news_page(AP_HTML, site.pages[0], site)
    assert items[0] == {
        "title": "Melty Berry Princessシリーズ入荷のご案内",
        "date": "2026-10",
        "url": "https://angelicpretty.com/Page/Feature/202610.aspx",
    }
    # 闭店通知也能解析出来，但排序时按「新作/入荷」关键词滤掉。
    ranked = brand_site._rank(items)
    assert [item["title"] for item in ranked] == ["Melty Berry Princessシリーズ入荷のご案内"]


def test_baby_list_prefers_time_and_keeps_absolute_link():
    site = next(item for item in brand_site.SITES if item.id == "baby")
    items = brand_site.parse_news_page(BABY_HTML, site.pages[0], site)
    assert len(items) == 2
    assert items[0]["date"] == "2026-11-20"
    assert items[0]["url"].endswith("/official_news/20270207_brand-new-collection-in-tokyo/")
    # gallery 是活动回顾，排序时会被过滤掉。
    ranked = brand_site._rank(items)
    assert len(ranked) == 1
    assert "BRAND-NEW COLLECTION" in ranked[0]["title"]


def test_class_rename_falls_back_to_link_and_heading_pairs():
    site = next(item for item in brand_site.SITES if item.id == "angelic_pretty")
    changed = """
    <div class="list">
      <div><a href="/Page/Feature/202612.aspx">more</a>
        <h2>Christmas Collection 発売のお知らせ</h2></div>
    </div>
    """
    items = brand_site.parse_news_page(changed, site.pages[0], site)
    assert items and items[0]["date"] == "2026-12"
    assert items[0]["title"].startswith("Christmas Collection")


def test_garbage_page_returns_empty_not_fabricated():
    site = brand_site.SITES[0]
    assert brand_site.parse_news_page("<html><body>维护中</body></html>", site.pages[0], site) == []


def test_cache_avoids_refetch(tmp_path, monkeypatch):
    app = _app(tmp_path)
    calls: list[str] = []

    def fake_fetch(url, timeout=brand_site.REQUEST_TIMEOUT):
        calls.append(url)
        return AP_HTML

    monkeypatch.setattr(brand_site, "_fetch", fake_fetch)
    first = brand_site.lookup("AP 最近上新", app)
    second = brand_site.lookup("AP 最近上新", app)

    assert first == second
    assert len(calls) == len(brand_site.SITES[0].pages) == 2
    assert (app.data_dir / "cache" / "brand_site" / "angelic_pretty.json").exists()


def test_fetch_failure_uses_stale_cache_within_window(tmp_path, monkeypatch):
    app = _app(tmp_path)
    site = brand_site.SITES[0]
    path = brand_site._cache_path(app, site.id)
    path.parent.mkdir(parents=True)
    path.write_text(
        '{"fetched_at": %s, "items": [{"title": "Melty Berry Princess入荷", '
        '"date": "2026-10", "url": "https://angelicpretty.com/Page/Feature/202610.aspx"}]}'
        % (time.time() - 60 * 60),
        encoding="utf-8",
    )

    def broken_fetch(*_args, **_kwargs):
        raise OSError("network down")

    monkeypatch.setattr(brand_site, "_fetch", broken_fetch)
    items = brand_site._site_items(site, app)
    assert items and items[0]["stale"] is True
    assert items[0]["title"].startswith("Melty Berry")


def test_fetch_failure_without_cache_raises_unavailable(tmp_path, monkeypatch):
    app = _app(tmp_path)

    def broken_fetch(*_args, **_kwargs):
        raise OSError("network down")

    monkeypatch.setattr(brand_site, "_fetch", broken_fetch)
    with pytest.raises(brand_site.BrandSiteUnavailable):
        brand_site.lookup("BABY 入荷", app)


def test_unknown_brand_raises_before_network(tmp_path, monkeypatch):
    app = _app(tmp_path)

    def should_not_run(*_args, **_kwargs):
        raise AssertionError("没有品牌时不该抓官网")

    monkeypatch.setattr(brand_site, "_fetch", should_not_run)
    with pytest.raises(brand_site.BrandSiteUnavailable):
        brand_site.lookup("最近有什么新款", app)


def test_official_channel_maps_rows_to_official_evidence(tmp_path, monkeypatch):
    app = _app(tmp_path)

    def fake_lookup(question, settings):
        assert question == "AP 最近上新"
        return [
            {
                "brand": "Angelic Pretty",
                "title": "Melty Berry Princessシリーズ入荷のご案内",
                "date": "2026-10",
                "url": "https://angelicpretty.com/Page/Feature/202610.aspx",
            }
        ]

    monkeypatch.setattr(brand_site, "lookup", fake_lookup)
    items = channels._recall_official("AP 最近上新", RecallContext(settings=app))

    assert len(items) == 1
    assert items[0]["channel"] == "official"
    assert items[0]["source_kind"] == "official"
    assert items[0]["published_at"] == "2026-10"
    assert "2026-10" in items[0]["text"]


def test_official_channel_failure_degrades_to_unavailable(tmp_path, monkeypatch):
    app = _app(tmp_path)

    def broken_lookup(*_args, **_kwargs):
        raise brand_site.BrandSiteUnavailable("官网 502")

    monkeypatch.setattr(brand_site, "lookup", broken_lookup)
    with pytest.raises(ChannelUnavailable) as exc:
        channels._recall_official("AP 最近上新", RecallContext(settings=app))
    assert "品牌官网不可用" in str(exc.value)


def test_web_status_degrades_after_real_failure(tmp_path, monkeypatch):
    app = _app(tmp_path)
    monkeypatch.setattr(web_search, "no_key_readiness", lambda _app: (True, ""))
    web_search.reset_no_key_health()
    try:
        assert web_search.no_key_health(app)[0] is True
        web_search.record_no_key_result(app, False, "Bing 浏览器 0 条；360 风控")
        ready, reason = web_search.no_key_health(app)
        assert ready is False
        assert "最近一次实际检索失败" in reason
        blocked, _ = web_search.no_key_blocked(app)
        assert blocked is True
        web_search.record_no_key_result(app, True)
        assert web_search.no_key_health(app)[0] is True
    finally:
        web_search.reset_no_key_health()


def test_web_recall_skips_fast_when_recent_failure(tmp_path, monkeypatch):
    app = AppSettings(
        data_dir=tmp_path / "data",
        content_dir=tmp_path / "content",
        search_enabled=False,
        search_api_key="",
    )
    web_search.reset_no_key_health()
    web_search.record_no_key_result(app, False, "Bing 浏览器 0 条；360 风控")
    try:
        with pytest.raises(ChannelUnavailable) as exc:
            channels._recall_web("AP 最近上新", RecallContext(settings=app))
        assert "最近一次实际失败" in str(exc.value)
    finally:
        web_search.reset_no_key_health()


def test_no_key_health_survives_memory_reset(tmp_path, monkeypatch):
    """进程重启后仍按落盘的最近失败记录报 not ready，不能又回到 ready。"""
    app = AppSettings(data_dir=tmp_path / "data", web_browser_enabled=True)
    monkeypatch.setattr(web_search, "no_key_readiness", lambda _app: (True, ""))
    web_search.record_no_key_result(app, False, "Bing 浏览器 0 条；360 风控")

    web_search.reset_no_key_health()
    ready, reason = web_search.no_key_health(app)
    assert ready is False
    assert "0 条" in reason


def test_web_status_requires_verified_fallback_when_quota_blocked(tmp_path, monkeypatch):
    """付费搜索配额失效时，无 key 兜底没成功过就不能报 ready。"""
    app = AppSettings(
        data_dir=tmp_path / "data",
        search_enabled=True,
        search_api_key="k",
        search_provider="zhipu",
        web_browser_enabled=True,
    )
    monkeypatch.setattr(web_search, "no_key_readiness", lambda _app: (True, ""))
    monkeypatch.setitem(web_search._QUOTA_BLOCKED_UNTIL, "zhipu", time.time() + 900)

    unverified = channels._web_status(app)
    assert unverified["ready"] is False
    assert unverified["mode"] == "browser"
    assert "未验证" in unverified["reason"]

    # 兜底真抓到一次结果后，同一条通道才允许报就绪
    web_search.record_no_key_result(app, True)
    verified = channels._web_status(app)
    assert verified["ready"] is True
    assert verified["mode"] == "browser"
