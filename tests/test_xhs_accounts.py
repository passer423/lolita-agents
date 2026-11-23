"""品牌 → 小红书官方账号：白名单、只信认证、缓存、查不到回退。

这一层的风险不在「能不能搜到」，而在「搜错了会不会把网友价当官方价」。所以
每个用例都盯一件事：只有 ``red_official_verified`` 且名字对得上品牌才采信；
认证过就缓存住不再请求；超时/失败不写缓存；找不到官方号时召回一条都不许变。
"""

from __future__ import annotations

import time

import pytest

from app.agent import channels, dress, writer
from app.agent.channels import RecallContext
from app.config import AppSettings
from app.services import web_search, xhs_accounts, xhs_search, xhs_spider


def _brand(**kwargs) -> dress.Brand:
    base = {"id": "classical-puppets", "name": "古典玩偶", "region": "CN", "aliases": ("古典玩偶",)}
    base.update(kwargs)
    return dress.Brand(**base)


def _spider_app(tmp_path) -> AppSettings:
    return AppSettings(
        data_dir=tmp_path,
        xhs_enabled=True,
        xhs_provider="spider_xhs",
        xhs_cookie="cookie",
    )


def _offline(monkeypatch, tmp_path):
    """搜素通道假装可用，但不碰网络：只有显式打桩的账号搜索会跑。"""
    monkeypatch.setattr(xhs_search, "available", lambda _app: True)
    monkeypatch.setattr(xhs_search, "_provider", lambda _app: "spider_xhs")


# ------------------------------------------------------------------ 字段透传


def test_note_to_dict_keeps_author():
    note = {
        "id": "n1",
        "xsec_token": "t",
        "note_card": {"display_title": "标题", "user": {"user_id": "u1", "nickname": "某人"}},
    }

    item = xhs_spider._note_to_dict(note)

    assert item["author"] == {"user_id": "u1", "nickname": "某人"}


def test_note_without_author_keeps_empty_author():
    """约 10% 的卡片没有 user：留空即可，不能为它加请求也不能崩。"""
    item = xhs_spider._note_to_dict({"id": "n1", "note_card": {"display_title": "标题"}})

    assert item["author"] == {"user_id": "", "nickname": ""}


def test_notes_to_results_carries_author():
    results = xhs_search._notes_to_results(
        [
            {
                "title": "四时花 返图",
                "url": "https://www.xiaohongshu.com/explore/1",
                "content": "正文",
                "author": {"user_id": "u1", "nickname": "白聆"},
            }
        ]
    )

    assert results[0].author_id == "u1"
    assert results[0].author_name == "白聆"
    assert results[0].to_dict()["author_id"] == "u1"


def test_search_evidence_carries_author_but_not_into_text():
    items = channels._search_evidence(
        "xhs",
        xhs_search._notes_to_results(
            [{"title": "t", "url": "https://x/1", "content": "c", "author": {"user_id": "u1"}}]
        ),
    )

    assert items[0]["author_id"] == "u1"
    assert "u1" not in items[0]["text"]


# ------------------------------------------------------------------ 账号解析


def test_whitelist_account_needs_no_request(tmp_path, monkeypatch):
    monkeypatch.setattr(
        xhs_accounts,
        "_search_verified",
        lambda *_args, **_kwargs: pytest.fail("白名单命中时不该再发用户搜索"),
    )

    account = xhs_accounts.resolve(
        _brand(xhs_account_id="u9", xhs_account_name="古典玩偶ClassicalPuppets"),
        _spider_app(tmp_path),
    )

    assert account == {
        "user_id": "u9",
        "nickname": "古典玩偶ClassicalPuppets",
        "source": "brands",
    }


def test_only_verified_and_name_matching_account_is_accepted(monkeypatch):
    class FakeApi:
        def __init__(self, _auth):
            pass

        def search_some_user(self, _query, _limit):
            return True, "", [
                {"name": "古典玩偶种草小天使", "id": "fan", "red_official_verified": False},
                {"name": "某个认证的美妆号", "id": "bad", "red_official_verified": True},
                {"name": "古典玩偶ClassicalPuppets", "id": "official", "red_official_verified": True},
            ]

    class FakeAuth:
        @classmethod
        def from_cookie(cls, _cookie):
            return cls()

    monkeypatch.setattr(xhs_spider, "_load_api", lambda _app: (FakeApi, FakeAuth))

    found = xhs_accounts._search_verified(_brand(), AppSettings(xhs_cookie="c"))

    # 未认证的「种草」号和认证了但跟品牌无关的号都不能采信
    assert found == {"user_id": "official", "nickname": "古典玩偶ClassicalPuppets"}


def test_resolve_searches_once_then_hits_cache(tmp_path, monkeypatch):
    _offline(monkeypatch, tmp_path)
    calls: list[str] = []

    def fake_search(brand, _app):
        calls.append(brand.id)
        return {"user_id": "u1", "nickname": "古典玩偶ClassicalPuppets"}

    monkeypatch.setattr(xhs_accounts, "_search_verified", fake_search)

    first = xhs_accounts.resolve(_brand(), _spider_app(tmp_path))
    second = xhs_accounts.resolve(_brand(), _spider_app(tmp_path))

    assert first["user_id"] == "u1" and first["source"] == "search"
    assert second["user_id"] == "u1" and second["source"] == "cache"
    assert calls == ["classical-puppets"]


def test_no_verified_account_is_negative_cached(tmp_path, monkeypatch):
    """搜到了但没有认证号：记空结果，7 天内不再打请求。"""
    _offline(monkeypatch, tmp_path)
    calls: list[str] = []
    monkeypatch.setattr(
        xhs_accounts, "_search_verified", lambda brand, _app: calls.append(brand.id) or None
    )

    app = _spider_app(tmp_path)

    assert xhs_accounts.resolve(_brand(), app) is None
    assert xhs_accounts.resolve(_brand(), app) is None
    assert calls == ["classical-puppets"]


def test_failure_is_not_cached(tmp_path, monkeypatch):
    """超时/风控失败不能记成「这个品牌没有官方号」，下次还要再试。"""
    _offline(monkeypatch, tmp_path)
    calls: list[str] = []

    def boom(brand, _app):
        calls.append(brand.id)
        raise RuntimeError("461")

    monkeypatch.setattr(xhs_accounts, "_search_verified", boom)

    app = _spider_app(tmp_path)

    assert xhs_accounts.resolve(_brand(), app) is None
    assert xhs_accounts.resolve(_brand(), app) is None
    assert calls == ["classical-puppets", "classical-puppets"]


def test_resolve_gives_up_within_budget(tmp_path, monkeypatch):
    _offline(monkeypatch, tmp_path)
    monkeypatch.setattr(xhs_accounts, "SEARCH_TIMEOUT", 0.05)

    def slow(_brand, _app):
        time.sleep(0.5)
        return {"user_id": "late"}

    monkeypatch.setattr(xhs_accounts, "_search_verified", slow)

    started = time.monotonic()
    assert xhs_accounts.resolve(_brand(), _spider_app(tmp_path)) is None
    assert time.monotonic() - started < 0.4


def test_playwright_provider_cannot_search_accounts(tmp_path, monkeypatch):
    monkeypatch.setattr(xhs_search, "available", lambda _app: True)
    monkeypatch.setattr(xhs_search, "_provider", lambda _app: "playwright")

    app = AppSettings(data_dir=tmp_path, xhs_enabled=True, xhs_provider="playwright")

    assert xhs_accounts.resolve(_brand(), app) is None


# ------------------------------------------------------------------ 官方帖挑选


def test_fetch_account_notes_filters_by_series_in_body(monkeypatch):
    """官方帖标题多是「月色倾洒…」，系列名只在正文：要按正文过滤，且只取 details 预算内的。"""
    cards = [
        {"note_id": "n1", "display_title": "月色倾洒，染一池莲华梦"},
        {"note_id": "n2", "display_title": "双11现货活动来啦"},
        {"note_id": "n3", "display_title": "12月上新",
         "xsec_token": "t3"},
    ]
    bodies = {
        "n1": "四时花 再贩 预约价 578 元",
        "n2": "现货活动 全场九折",
        "n3": "四时花 上新 定金 188 元",
    }
    detail_calls: list[str] = []

    class FakeApi:
        def __init__(self, _auth):
            pass

        def get_user_note_info(self, _uid, _cursor, _token, _source):
            return True, "", {"data": {"notes": cards}}

        def get_note_info(self, url):
            note_id = url.split("/explore/")[1].split("?")[0]
            detail_calls.append(note_id)
            return True, "", {
                "data": {"items": [{"note_card": {"title": f"t{note_id}", "desc": bodies[note_id]}}]}
            }

    class FakeAuth:
        @classmethod
        def from_cookie(cls, _cookie):
            return cls()

    monkeypatch.setattr(xhs_spider, "_load_api", lambda _app: (FakeApi, FakeAuth))

    notes = xhs_spider.fetch_account_notes(
        "u1", AppSettings(xhs_cookie="c"), series=("四时花",), limit=2, probe=3
    )

    titles = [note["title"] for note in notes]
    # 「上新/现货」这类情报帖排在前面，正文里没有系列名的会被过滤掉
    assert titles == ["tn3", "tn1"]
    assert all("四时花" in note["content"] for note in notes)
    # 没命中系列的帖子也要取详情才知道，所以会多取一条；但总数不许超过 probe
    assert len(detail_calls) == 3


# ---------------------------------------------------------------------------
# 登录态失效：状态接口要反映「最近真实调用失败」，但不能反过来拦召回。


@pytest.fixture
def _clean_health():
    xhs_search._health.clear()
    yield
    xhs_search._health.clear()


def test_status_shows_recent_auth_failure_without_blocking_recall(
    monkeypatch, tmp_path, _clean_health
):
    app = _spider_app(tmp_path)
    app.ensure_dirs()
    monkeypatch.setattr(xhs_spider, "readiness", lambda _app: (True, ""))
    monkeypatch.setattr(
        xhs_spider,
        "fetch_notes",
        lambda *_a, **_k: (_ for _ in ()).throw(xhs_search.XhsUnavailable("登录已过期")),
    )

    with pytest.raises(xhs_search.XhsUnavailable):
        xhs_search.search("痛包", app)

    # 召回侧仍只看结构，不会被一次失败拦住
    assert xhs_search.available(app) is True
    ready, reason = xhs_search.status_readiness(app)
    assert ready is False
    assert "登录已过期" in reason


def test_status_degrades_after_three_empty_results(monkeypatch, tmp_path, _clean_health):
    app = _spider_app(tmp_path)
    app.ensure_dirs()
    monkeypatch.setattr(xhs_spider, "readiness", lambda _app: (True, ""))
    monkeypatch.setattr(xhs_spider, "fetch_notes", lambda *_a, **_k: [])

    for query in ("痛包", "狗短", "fullset"):
        assert xhs_search.search(query, app) == []

    ready, reason = xhs_search.status_readiness(app)
    assert ready is False
    assert "空" in reason
    # 空结果同样不拦召回
    assert xhs_search.available(app) is True


# ------------------------------------------------------------------ 召回接线


def test_existing_official_note_is_marked_without_account_requests(tmp_path, monkeypatch):
    monkeypatch.setattr(xhs_accounts, "brand_for_question", lambda _q, _app: (_brand(), ()))
    monkeypatch.setattr(xhs_accounts, "resolve", lambda *_a, **_k: {"user_id": "u1"})
    monkeypatch.setattr(
        xhs_accounts,
        "official_notes",
        lambda *_a, **_k: pytest.fail("召回里已有官方帖时不该再读账号主页"),
    )
    items = [{"author_id": "u1", "title": "上新"}, {"author_id": "u2", "title": "返图"}]

    extra = channels._official_evidence("古典玩偶 上新", RecallContext(settings=AppSettings()), items, None)

    assert extra == []
    assert items[0]["official"] is True
    assert "official" not in items[1]


def test_account_notes_are_appended_and_flagged(tmp_path, monkeypatch):
    monkeypatch.setattr(xhs_accounts, "brand_for_question", lambda _q, _app: (_brand(), ("安东尼蛋糕",)))
    monkeypatch.setattr(xhs_accounts, "resolve", lambda *_a, **_k: {"user_id": "u1", "nickname": "古典玩偶"})
    monkeypatch.setattr(
        xhs_accounts,
        "official_notes",
        lambda *_a, **_k: [
            {
                "title": "12月上新",
                "url": "https://www.xiaohongshu.com/explore/9",
                "content": "安东尼蛋糕 OP 844 元",
                "author": {"user_id": "u1", "nickname": "古典玩偶"},
            }
        ],
    )
    ctx = RecallContext(settings=AppSettings())

    extra = channels._official_evidence("安东尼蛋糕价格", ctx, [{"author_id": "u2"}], None)

    assert len(extra) == 1
    assert extra[0]["official"] is True
    assert extra[0]["official_account"] == "古典玩偶"
    assert extra[0]["text"] == "安东尼蛋糕 OP 844 元"


def test_care_question_does_not_read_account_page(tmp_path, monkeypatch):
    """洗护/尺码类问题不该为官方上新帖多花请求、多等一两秒。"""
    monkeypatch.setattr(xhs_accounts, "brand_for_question", lambda _q, _app: (_brand(), ()))
    monkeypatch.setattr(xhs_accounts, "resolve", lambda *_a, **_k: {"user_id": "u1"})
    monkeypatch.setattr(
        xhs_accounts,
        "official_notes",
        lambda *_a, **_k: pytest.fail("洗护问题不该读官方账号主页"),
    )

    extra = channels._official_evidence(
        "古典玩偶的裙子怎么洗", RecallContext(settings=AppSettings()), [{"author_id": "u2"}], None
    )

    assert extra == []


def test_unknown_brand_or_no_account_leaves_results_untouched(tmp_path, monkeypatch):
    """四时花：品牌没有登记官方号、搜索也没有认证号 —— 召回必须原样返回。"""
    _offline(monkeypatch, tmp_path)
    monkeypatch.setattr(xhs_accounts, "_search_verified", lambda *_a, **_k: None)
    results = [
        web_search.SearchResult(title="四时花 返图", url="https://x/1", content="c", author_id="u2"),
        web_search.SearchResult(title="四时花 价格", url="https://x/2", content="c", author_id=""),
    ]
    monkeypatch.setattr(xhs_search, "search", lambda *_a, **_k: results)
    app = _spider_app(tmp_path)

    items = channels._recall_xhs("四时花价格", RecallContext(settings=app))

    assert [item["title"] for item in items] == ["四时花 返图", "四时花 价格"]
    assert all("official" not in item for item in items)


def test_brand_for_question_maps_dress_to_brand():
    brand, series = xhs_accounts.brand_for_question("四时花价格", AppSettings())

    assert brand is not None and brand.id == "sheep-orchid"
    assert series == ("四时花", "Four Seasons Flower")


# ------------------------------------------------------------------ 写作称呼


def test_official_flag_beats_shop_keywords():
    item = {"source_kind": "realtime", "official": True, "title": "淘宝店铺上新", "text": "店铺公告"}

    assert writer.source_appellation(item) == writer.APPELLATION_OFFICIAL


def test_ugc_stays_ugc():
    item = {"source_kind": "realtime", "official": False, "title": "四时花 返图", "text": "好美"}

    assert writer.source_appellation(item) == writer.APPELLATION_WEB


def test_official_evidence_line_is_labelled():
    """写作提示词里那条证据的标签必须是「官方」，模型才会照口径引用。"""
    item = {
        "n": 1,
        "source_kind": "realtime",
        "official": True,
        "title": "12月上新",
        "text": "OP 844 元",
        "url": "https://www.xiaohongshu.com/explore/9",
    }

    line = writer._evidence_line(item)

    assert line.startswith("[1]（官方）")
    assert "淘宝" not in line


# ------------------------------------------------------------------ 总开关


def test_disabled_xhs_sends_no_request_and_reports_disabled(monkeypatch):
    """XHS_ENABLED=false：不发任何请求，状态如实显示 disabled + 原因。"""
    app = AppSettings(xhs_enabled=False)
    calls: list[str] = []

    monkeypatch.setattr(
        xhs_search, "search", lambda *_a, **_k: calls.append("search") or []
    )

    with pytest.raises(channels.ChannelUnavailable, match="XHS_ENABLED=false"):
        channels.recall("xhs", "四时花 价格", RecallContext(settings=app))

    assert calls == []
    status = channels._xhs_status(app)
    assert status == {
        "enabled": False,
        "ready": False,
        "reason": "未启用（XHS_ENABLED=false）",
    }
