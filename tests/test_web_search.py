"""网页搜索 provider 的解析与请求边界。

两个 provider 的响应结构不同（博查两层 webPages、智谱 search_result），
且都允许缺字段。解析层的错误不会报错，只会让「召回变少」——所以这里逐项钉死。
"""

from __future__ import annotations

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
