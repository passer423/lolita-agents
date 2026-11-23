"""网页搜索 provider 的解析与请求边界。

两个 provider 的响应结构不同（博查两层 webPages、智谱 search_result），
且都允许缺字段。解析层的错误不会报错，只会让「召回变少」——所以这里逐项钉死。
"""

from __future__ import annotations

import email.message
import io
from urllib.error import HTTPError, URLError

import pytest

from app.config import AppSettings
from app.services import web_search

# --------------------------------------------------------------------- 博查


def test_bocha_parses_nested_data_wrapper():
    data = {
        "data": {
            "webPages": {
                "value": [
                    {
                        "name": "Baby 秋日私语",
                        "url": "https://example.com/a",
                        "snippet": "摘要",
                        "summary": "更长的正文",
                        "siteName": "官网",
                        "datePublished": "2026-09-01",
                    },
                    {"title": "缺链接", "summary": "应被丢掉"},
                    "不是对象",
                ]
            }
        }
    }

    results = web_search._parse_bocha(data)

    assert len(results) == 1
    item = results[0]
    assert item.title == "Baby 秋日私语"
    assert item.url == "https://example.com/a"
    assert item.snippet == "摘要"
    assert item.content == "更长的正文"
    assert item.site_name == "官网"
    assert item.published_at == "2026-09-01"
    assert item.provider == "bocha"


def test_bocha_accepts_flat_webpages_and_values_alias():
    """有的响应不包 data，且列表键可能叫 values；两种都要兼容。"""
    data = {
        "webPages": {
            "values": [
                {
                    "name": "AP 新款",
                    "link": "https://example.com/b",
                    "snippet": "只有摘要",
                }
            ]
        }
    }

    results = web_search._parse_bocha(data)

    assert len(results) == 1
    assert results[0].url == "https://example.com/b"
    # 没有 summary/content 时正文退回摘要，不能留空
    assert results[0].content == "只有摘要"


def test_bocha_missing_payload_returns_empty():
    assert web_search._parse_bocha({}) == []
    assert web_search._parse_bocha({"data": "坏结构"}) == []


# --------------------------------------------------------------------- 智谱


def test_zhipu_parses_results_and_truncates_snippet():
    data = {
        "search_result": [
            {
                "title": "蕾丝怎么洗",
                "link": "https://example.com/c",
                "content": "正文" * 300,
                "media": "知乎",
                "publish_date": "2026-10-01",
            },
            {"title": "缺链接", "content": "应被丢掉"},
            None,
        ]
    }

    results = web_search._parse_zhipu(data)

    assert len(results) == 1
    item = results[0]
    assert item.title == "蕾丝怎么洗"
    assert item.url == "https://example.com/c"
    assert item.site_name == "知乎"
    assert item.published_at == "2026-10-01"
    assert item.provider == "zhipu"
    # 摘要截 300 字，正文保留完整内容
    assert len(item.snippet) == 300
    assert len(item.content) == 600


def test_zhipu_accepts_url_alias_and_missing_payload():
    assert web_search._parse_zhipu(
        {"search_result": [{"title": "t", "url": "https://example.com/d"}]}
    )[0].url == "https://example.com/d"
    assert web_search._parse_zhipu({}) == []


# --------------------------------------------------------------------- search 边界


def test_search_requires_key_and_known_provider():
    disabled = AppSettings(search_enabled=False, search_api_key="k")
    with pytest.raises(web_search.SearchUnavailable, match="SEARCH_API_KEY"):
        web_search.search("Baby", disabled)

    enabled = AppSettings(search_enabled=True, search_api_key="k", search_provider="nope")
    with pytest.raises(web_search.SearchUnavailable, match="不支持的 SEARCH_PROVIDER"):
        web_search.search("Baby", enabled)


def test_search_parses_and_dedupes_by_url(monkeypatch):
    app = AppSettings(
        search_enabled=True,
        search_api_key="k",
        search_provider="zhipu",
        search_max_results=5,
    )
    monkeypatch.setattr(
        web_search,
        "_post_json",
        lambda *_a, **_k: {
            "search_result": [
                {"title": "A", "link": "https://example.com/1", "content": "a"},
                {"title": "A 重复", "link": "https://example.com/1", "content": "a"},
                {"title": "B", "link": "https://example.com/2", "content": "b"},
            ]
        },
    )

    results = web_search.search("Baby", app)

    assert [item.url for item in results] == [
        "https://example.com/1",
        "https://example.com/2",
    ]
    assert results[0].provider == "zhipu"


# --------------------------------------------------------------------- 重试与配额


class _FakeResponse:
    headers = email.message.Message()

    def __init__(self, body: bytes):
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


def _http_error(url: str, code: int, body: str) -> HTTPError:
    return HTTPError(url, code, "error", None, io.BytesIO(body.encode("utf-8")))


def test_quota_1113_is_not_retried_and_blocks_next_call(monkeypatch, workdir):
    """1113 是账号级资源包耗尽：不重试，并在 TTL 内直接降级。"""
    web_search._QUOTA_BLOCKED_UNTIL.clear()
    calls: list[int] = []

    def fake_urlopen(*_args, **_kwargs):
        calls.append(1)
        raise _http_error(
            web_search.ZHIPU_URL,
            429,
            '{"error":{"code":"1113","message":"余额不足或无可用资源包,请充值"}}',
        )

    monkeypatch.setattr(web_search, "urlopen", fake_urlopen)
    app = AppSettings(
        data_dir=workdir,
        search_enabled=True,
        search_api_key="k",
        search_provider="zhipu",
        search_quota_ttl=900,
    )

    with pytest.raises(web_search.SearchQuotaExhausted, match="1113"):
        web_search.search("Baby", app)

    assert len(calls) == 1
    assert web_search.quota_blocked(app)

    # 进程重启会丢内存标记，但磁盘上的 TTL 还在：状态接口不能重新报就绪。
    web_search._QUOTA_BLOCKED_UNTIL.clear()
    assert web_search.quota_blocked(app)

    # TTL 内不再打接口：下一次召回直接按失效处理。
    with pytest.raises(web_search.SearchQuotaExhausted):
        web_search.search("Baby", app)
    assert len(calls) == 1

    web_search._QUOTA_BLOCKED_UNTIL.clear()


def test_server_error_retries_once(monkeypatch):
    web_search._QUOTA_BLOCKED_UNTIL.clear()
    monkeypatch.setattr(web_search.time, "sleep", lambda _seconds: None)
    calls: list[int] = []

    def fake_urlopen(*_args, **_kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise _http_error(web_search.ZHIPU_URL, 503, "upstream")
        return _FakeResponse(
            b'{"search_result":[{"title":"A","link":"https://example.com/1","content":"a"}]}'
        )

    monkeypatch.setattr(web_search, "urlopen", fake_urlopen)
    app = AppSettings(search_enabled=True, search_api_key="k", search_provider="zhipu")

    results = web_search.search("Baby", app)

    assert len(calls) == 2
    assert [item.url for item in results] == ["https://example.com/1"]


def test_network_error_retries_once_then_reports(monkeypatch):
    web_search._QUOTA_BLOCKED_UNTIL.clear()
    monkeypatch.setattr(web_search.time, "sleep", lambda _seconds: None)
    calls: list[int] = []

    def fake_urlopen(*_args, **_kwargs):
        calls.append(1)
        raise URLError("timed out")

    monkeypatch.setattr(web_search, "urlopen", fake_urlopen)
    app = AppSettings(search_enabled=True, search_api_key="k", search_provider="zhipu")

    with pytest.raises(web_search.SearchUnavailable, match="连不上"):
        web_search.search("Baby", app)

    assert len(calls) == 2


# --------------------------------------------------------------------- 无 key 来源


def test_bing_payload_keeps_only_clickable_results():
    payload = {
        "ok": True,
        "results": [
            {"title": "四时花预约", "url": "https://example.com/1", "snippet": "578 元", "site": "weibo.com"},
            {"title": "缺链接", "url": "", "snippet": "丢掉"},
            "不是对象",
        ],
    }

    results = web_search._results_from_bing(payload)

    assert len(results) == 1
    assert results[0].url == "https://example.com/1"
    assert results[0].provider == "bing_browser"
    assert results[0].text_only is False


SO360_HTML = """
<ul>
  <li class="res-list">
    <h3><a href="https://www.so.com/link?url=bad">四时花预约价</a></h3>
    <p class="res-desc">长款 578 元，现货 598 元</p>
  </li>
  <li class="res-list">
    <h3>Baby 秋日私语返图</h3>
    <p>实拍与尺码讨论</p>
  </li>
</ul>
"""


def test_360_results_are_text_only_without_url():
    results = web_search._results_from_360(SO360_HTML, limit=5)

    assert len(results) == 2
    first = results[0]
    # 360 的跳转链接不可点：连 href 都不读，url 必须为空。
    assert first.url == ""
    assert first.text_only is True
    assert first.provider == "360_text"
    assert "文本兜底，无原链接" in first.title
    assert "578" in first.content


MOBILE_360_HTML = """
<div class="g-card">
  <div class="g-f-title"><a href="https://m.so.com/link?bad">四时花 预约价</a></div>
  <p class="g-f-info-sub">长款 578 元，现货 598 元</p>
</div>
<div class="g-card">
  <div class="g-f-title"><a href="https://m.so.com/link?bad2">Baby 返图</a></div>
  <p>实拍与尺码讨论</p>
</div>
"""


def test_360_mobile_markup_is_supported():
    """桌面/移动入口结构不同；换入口不能整路为空，也不能带出链接。"""
    results = web_search._results_from_360(MOBILE_360_HTML, limit=5)

    assert [item.title for item in results] == [
        "四时花 预约价（文本兜底，无原链接）",
        "Baby 返图（文本兜底，无原链接）",
    ]
    assert all(item.url == "" and item.text_only for item in results)
    assert "578" in results[0].content


def test_search_no_key_prefers_bing_and_does_not_call_360(monkeypatch):
    app = AppSettings(web_browser_enabled=True)
    bing = [
        web_search.SearchResult(
            title="四时花",
            url="https://example.com/1",
            snippet="578 元",
            provider="bing_browser",
        )
    ]
    monkeypatch.setattr(web_search, "_bing_browser_search", lambda *_a, **_k: bing)

    def fail_360(*_args, **_kwargs):
        raise AssertionError("Bing 有结果时不应调用 360")

    monkeypatch.setattr(web_search, "_search_360", fail_360)

    results = web_search.search_no_key("四时花", app)

    assert [item.url for item in results] == ["https://example.com/1"]
    assert results[0].provider == "bing_browser"


def test_search_no_key_falls_back_to_360_when_bing_empty(monkeypatch):
    app = AppSettings(web_browser_enabled=True)
    monkeypatch.setattr(web_search, "_bing_browser_search", lambda *_a, **_k: [])
    monkeypatch.setattr(
        web_search,
        "_search_360",
        lambda *_a, **_k: web_search._results_from_360(SO360_HTML, limit=5),
    )

    results = web_search.search_no_key("四时花", app)

    assert len(results) == 2
    assert all(item.text_only and item.url == "" for item in results)
    assert results[0].provider == "360_text"


def test_search_no_key_reports_both_failures(monkeypatch):
    app = AppSettings(web_browser_enabled=True)

    def broken(*_args, **_kwargs):
        raise web_search.SearchUnavailable("boom")

    monkeypatch.setattr(web_search, "_bing_browser_search", broken)
    monkeypatch.setattr(web_search, "_search_360", broken)

    with pytest.raises(web_search.SearchUnavailable, match="无 key 网页来源不可用"):
        web_search.search_no_key("四时花", app)


def test_search_no_key_respects_switch(monkeypatch):
    app = AppSettings(web_browser_enabled=False)

    with pytest.raises(web_search.SearchUnavailable, match="WEB_BROWSER_ENABLED=false"):
        web_search.search_no_key("四时花", app)


def test_text_only_evidence_gets_distinct_refs():
    """文本兜底没有 URL，去重键不能都落空，否则多条结果会被合并成一条。"""
    from app.agent import channels

    results = web_search._results_from_360(SO360_HTML, limit=5)
    items = channels._search_evidence("web", results)

    assert [item["url"] for item in items] == ["", ""]
    assert all(item["text_only"] is True for item in items)
    refs = [item["ref"] for item in items]
    assert len(set(refs)) == 2
    assert all(refs)
