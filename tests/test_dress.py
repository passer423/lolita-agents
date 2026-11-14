"""裙子档案工具的纯单测：识别、路由、档案字段整合、社媒抽取、在线解析。

**不联网**：在线查询用固定 HTML 夹具 + 注入 fetcher；模型兜底只测协议。
"""

from __future__ import annotations

import json

import pytest

from app.agent import dress
from app.agent.channels import RecallContext
from app.agent.planner import PlannerOutputError, parse_dress_reply
from app.config import AppSettings
from app.services import lolibrary_online, web_search

BRANDS = [
    {"id": "baby", "name": "Baby, the Stars Shine Bright", "region": "JP",
     "aliases": ["Baby", "BTSSB"]},
    {"id": "ap", "name": "Angelic Pretty", "region": "JP", "aliases": ["AP"]},
    {"id": "sheep-orchid", "name": "Sheep Orchid", "region": "CN",
     "aliases": ["Sheep Orchid"]},
    {"id": "mystery", "name": "Mystery Brand", "region": "UNKNOWN",
     "aliases": ["Mystery"]},
]

ALIASES = [
    {"cn": "皇家千层公主", "en": "Mille-feuille Royal pour la Princesse", "brand": "baby"},
    {"cn": "黄家千层公主", "en": "Mille-feuille Royal pour la Princesse", "brand": "baby"},
    {"cn": "甜心梦幻拿铁", "en": "Sweetheart Dreamy Latte", "brand": "baby"},
    {"cn": "梦幻甜心拿铁", "en": "Sweetheart Dreamy Latte", "brand": "baby"},
    {"cn": "甜心拿铁", "en": "Sweetheart Dreamy Latte", "brand": "baby"},
    {"cn": "四时花", "en": "Four Seasons Flower", "brand": "sheep-orchid"},
]

GLOSSARY = [
    {"cn": "甜心", "en": ["sweetheart"]},
    {"cn": "梦幻", "en": ["dreamy"]},
    {"cn": "拿铁", "en": ["latte"]},
    {"cn": "草莓", "en": ["strawberry"]},
    {"cn": "奶油", "en": ["cream"]},
    {"cn": "兔", "en": ["rabbit"]},
    {"cn": "星星", "en": ["star"]},
    {"cn": "星空", "en": ["starry"]},
    {"cn": "梦", "en": ["dream"]},
]


def make_app(workdir, *, brands=None, aliases=None, records=None, glossary=None) -> AppSettings:
    app = AppSettings(data_dir=workdir / "data", content_dir=workdir / "content")
    app.ensure_dirs()
    (app.content_dir / "dress").mkdir(parents=True, exist_ok=True)
    app.brands_path.write_text(
        json.dumps({"brands": brands or BRANDS}, ensure_ascii=False), encoding="utf-8"
    )
    app.dress_aliases_path.write_text(
        json.dumps({"aliases": aliases or ALIASES}, ensure_ascii=False), encoding="utf-8"
    )
    app.dress_glossary_path.write_text(
        json.dumps({"terms": glossary or GLOSSARY}, ensure_ascii=False), encoding="utf-8"
    )
    lines = [json.dumps(record, ensure_ascii=False) for record in (records or [])]
    app.lolibrary_catalog_path.write_text(
        "\n".join(lines) + ("\n" if lines else ""), encoding="utf-8"
    )
    return app


def record(**overrides) -> dict:
    data = {
        "slug": "btssb-mille-feuille-royal-pour-la-princesse-jsk",
        "url": "https://lolibrary.org/items/btssb-mille-feuille-royal-pour-la-princesse-jsk",
        "title": "Mille-feuille Royal pour la Princesse JSK",
        "brand": "Baby, the Stars Shine Bright",
        "category": "JSK",
        "product_number": "B46HC310",
        "release_year": "2021",
        "original_price": "¥297000 JPY",
        "colors": ["Pink", "White"],
        "aliases": [],
    }
    data.update(overrides)
    return data


# --------------------------------------------------------------------- 识别


def test_series_alias_resolves_jp_brand(workdir):
    app = make_app(workdir)
    query = dress.resolve_dress("皇家千层公主什么时候发售", RecallContext(settings=app))

    assert query is not None
    assert query.brand == "Baby, the Stars Shine Bright"
    assert query.region == "JP"
    assert query.series_en == "Mille-feuille Royal pour la Princesse"
    assert query.source == "series_alias"
    assert query.is_specific is True
    assert "Mille-feuille Royal pour la Princesse" in query.lolibrary_query
    assert query.brand not in query.lolibrary_query


def test_short_brand_alias_is_jp(workdir):
    app = make_app(workdir)
    query = dress.resolve_dress("AP 的 Memorial Cake JSK 多少钱", RecallContext(settings=app))

    assert query is not None
    assert query.brand == "Angelic Pretty"
    assert query.region == "JP"
    assert query.is_specific is False
    assert query.social_query


def test_known_typo_alias_resolves(workdir):
    """「黄家」是「皇家」的常见同音误写；别名表要能接住。"""
    app = make_app(workdir)
    query = dress.resolve_dress("黄家千层公主价格", RecallContext(settings=app))

    assert query is not None
    assert query.series_en == "Mille-feuille Royal pour la Princesse"
    assert query.region == "JP"


def test_latte_alias_variants_resolve(workdir):
    """中文名顺序不同、只写简称、带品牌、只写英文名，都要能命中。"""
    app = make_app(workdir)
    ctx = RecallContext(settings=app)
    for question in (
        "甜心梦幻拿铁",
        "梦幻甜心拿铁",
        "Baby 甜心梦幻拿铁",
        "baby的甜心拿铁",
        "Sweetheart Dreamy Latte",
    ):
        query = dress.resolve_dress(question, ctx)
        assert query is not None, question
        assert query.brand == "Baby, the Stars Shine Bright", question
        assert query.region == "JP", question
        assert query.series_en == "Sweetheart Dreamy Latte", question
        assert query.source == "series_alias", question
        assert query.verified is True, question


def test_bare_unknown_name_goes_to_model(workdir):
    app = make_app(workdir)
    query = dress.resolve_dress("星月夜之梦", RecallContext(settings=app))

    assert query is not None
    assert query.source == "pending_model"
    assert query.is_specific is False


def test_bare_chitchat_not_triggered(workdir):
    app = make_app(workdir)
    assert dress.resolve_dress("今天好累", RecallContext(settings=app)) is None


def test_brand_plus_bare_name_goes_to_model(workdir):
    app = make_app(workdir)
    query = dress.resolve_dress("Baby 秋日私语", RecallContext(settings=app))

    assert query is not None
    assert query.source == "pending_model"
    assert query.brand == "Baby, the Stars Shine Bright"
    assert query.region == "JP"


def test_series_alias_tolerates_one_char_typo(workdir):
    """没进别名表的一字之差也要能匹配，并标明是按近似名匹配的。"""
    app = make_app(workdir)
    query = dress.resolve_dress("皇家千晨公主价格", RecallContext(settings=app))

    assert query is not None
    assert query.series_en == "Mille-feuille Royal pour la Princesse"
    assert any("近似中文名" in note for note in query.notes)


def test_catalog_chinese_alias_resolves_cn(workdir):
    app = make_app(
        workdir,
        records=[record(
            slug="cn-four-seasons-flower-op",
            url="https://lolibrary.org/items/cn-four-seasons-flower-op",
            title="Four Seasons Flower OP",
            brand="Sheep Orchid",
            category="OP",
            product_number="",
            release_year="2025",
            original_price="CN¥598",
            colors=["Pink"],
            aliases=["四时花OP"],
        )],
    )
    query = dress.resolve_dress("四时花OP 多少钱", RecallContext(settings=app))

    assert query is not None
    assert query.region == "CN"
    assert query.brand == "Sheep Orchid"
    assert query.series_en == "Four Seasons Flower OP"
    assert query.source.startswith("catalog_")


def test_care_question_does_not_trigger(workdir):
    app = make_app(workdir)
    assert dress.resolve_dress("四时花怎么洗", RecallContext(settings=app)) is None


def test_generic_dress_question_goes_to_model(workdir):
    """没有品牌/系列实体时不再直接放弃：交给模型兜底一次。"""
    app = make_app(workdir)
    query = dress.resolve_dress("这条裙子多少钱", RecallContext(settings=app))

    assert query is not None
    assert query.source == "pending_model"
    assert query.region == "UNKNOWN"


def test_unknown_chinese_name_goes_to_model(workdir):
    app = make_app(workdir)
    query = dress.resolve_dress("星月夜之梦价格", RecallContext(settings=app))

    assert query is not None
    assert query.source == "pending_model"
    assert query.is_specific is False


def test_short_non_dress_price_question_not_triggered(workdir):
    """两字名词的普通问价不白调模型。"""
    app = make_app(workdir)
    assert dress.resolve_dress("快递多少钱", RecallContext(settings=app)) is None


def test_unknown_region_queries_both_sides(workdir):
    app = make_app(workdir)
    query = dress.resolve_dress("Mystery 的裙子多少钱", RecallContext(settings=app))

    assert query is not None
    assert query.region == "UNKNOWN"
    assert dress.dress_channels(query.to_dict(), ["web"]) == ["xhs", "weibo", "web"]


def test_dress_channels_by_region():
    assert dress.dress_channels({"region": "JP"}, ["xhs", "web"]) == []
    assert dress.dress_channels({"region": "CN"}, []) == ["xhs", "weibo"]
    assert dress.dress_channels({"region": "OTHER"}, ["web"]) == ["xhs", "weibo", "web"]


# --------------------------------------------------------------------- 档案整合


def test_profile_from_local_archive(workdir):
    app = make_app(workdir, records=[record()])
    ctx = RecallContext(settings=app)
    query = dress.resolve_dress("皇家千层公主什么时候发售", ctx)
    result = dress.build_profile(query.to_dict(), [], ctx, online=False)

    profile = result["profile"]
    assert profile["brand"] == "Baby, the Stars Shine Bright"
    assert profile["region"] == "JP"
    assert profile["release"] == "2021"
    assert profile["price"] == "¥297000 JPY"
    assert profile["style"] == "JSK"
    assert profile["colors"] == ["Pink", "White"]
    assert profile["missing"] == []
    assert result["evidence"]["channel"] == "dress"
    assert result["evidence"]["source_kind"] == "third_party"
    assert "历史原价" in result["evidence"]["text"]


def test_cn_profile_extracts_social_fields(workdir):
    app = make_app(workdir)
    ctx = RecallContext(settings=app)
    query = dress.resolve_dress("四时花 预约价是多少", ctx)
    evidence = [{
        "channel": "xhs",
        "title": "四时花 预约",
        "text": "四时花预约价 ¥398，2026年5月发售，有粉色和黑色",
        "url": "https://xhs/1",
    }]

    result = dress.build_profile(query.to_dict(), evidence, ctx, online=False)
    profile = result["profile"]

    assert profile["region"] == "CN"
    assert "398" in profile["price"]
    assert profile["price_kind"] == "social_unverified"
    assert profile["release"] == "2026-05"
    assert "粉" in profile["colors"]
    assert "黑" in profile["colors"]
    assert result["evidence"]["source_kind"] == "realtime"


def test_price_extraction_does_not_swallow_years(workdir):
    app = make_app(workdir)
    ctx = RecallContext(settings=app)
    query = dress.resolve_dress("四时花 预约价是多少", ctx)
    evidence = [{
        "channel": "weibo",
        "title": "四时花",
        "text": "2026年5月发售，定价 398元，定金 50 元",
        "url": "https://weibo/1",
    }]

    profile = dress.build_profile(query.to_dict(), evidence, ctx, online=False)["profile"]

    assert profile["release"] == "2026-05"
    assert "2026" not in (profile["price"] or "")
    assert "398" in profile["price"]


def test_model_hint_fills_missing_series(workdir):
    app = make_app(workdir)
    ctx = RecallContext(settings=app)
    query = dress.resolve_dress("Baby 缎带与齿轮 预约价多少", ctx).to_dict()
    hint = {
        "brand": "Baby, the Stars Shine Bright",
        "region": "JP",
        "series": "Mille-feuille Royal pour la Princesse",
        "series_cn": "皇家千层公主",
        "confidence": "high",
    }

    merged = dress.merge_model_hint(query, hint, ctx)

    assert merged["series_en"] == "Mille-feuille Royal pour la Princesse"
    assert merged["is_specific"] is True
    assert "Mille-feuille Royal pour la Princesse" in merged["lolibrary_query"]


def test_low_confidence_model_hint_is_ignored(workdir):
    app = make_app(workdir)
    ctx = RecallContext(settings=app)
    query = dress.resolve_dress("Baby 缎带与齿轮 预约价多少", ctx).to_dict()
    merged = dress.merge_model_hint(
        query,
        {"brand": "Baby", "region": "JP", "series": "Guess", "confidence": "low"},
        ctx,
    )

    assert merged["series_en"] == ""


def test_pending_query_merged_with_model_hint(workdir):
    app = make_app(workdir)
    ctx = RecallContext(settings=app)
    pending = dress.resolve_dress("星月夜之梦价格", ctx).to_dict()

    merged = dress.merge_model_hint(
        pending,
        {
            "brand": "Baby, the Stars Shine Bright",
            "region": "JP",
            "series": "Starry Night Dream",
            "series_cn": "星月夜之梦",
            "confidence": "high",
        },
        ctx,
    )

    assert merged["source"] == "model"
    assert merged["brand"] == "Baby, the Stars Shine Bright"
    assert merged["series_en"] == "Starry Night Dream"
    assert merged["verified"] is False
    assert any("待 Lolibrary" in note for note in merged["notes"])


def test_learned_alias_written_and_used(workdir):
    app = make_app(workdir)
    ctx = RecallContext(settings=app)

    assert dress.remember_series_alias(
        "星月夜之梦", "Starry Night Dream", "baby", ctx,
        verified_url="https://lolibrary.org/items/starry-night",
    )
    entries = json.loads(app.learned_aliases_path.read_text(encoding="utf-8"))["aliases"]
    assert entries and entries[0]["cn"] == "星月夜之梦"

    query = dress.resolve_dress("星月夜之梦价格", ctx)
    assert query is not None
    assert query.brand == "Baby, the Stars Shine Bright"
    assert query.region == "JP"
    assert query.series_en == "Starry Night Dream"
    assert query.source == "series_alias"
    assert query.verified is True

    # 同一个中文名只保留一条，重复学习时更新英文名。
    assert dress.remember_series_alias("星月夜之梦", "Starry Night Dream v2", "baby", ctx)
    entries = json.loads(app.learned_aliases_path.read_text(encoding="utf-8"))["aliases"]
    same = [item for item in entries if item["cn"] == "星月夜之梦"]
    assert len(same) == 1
    assert same[0]["en"] == "Starry Night Dream v2"


def test_learned_alias_requires_known_brand(workdir):
    app = make_app(workdir)
    ctx = RecallContext(settings=app)
    assert not dress.remember_series_alias("星月夜之梦", "Starry Night Dream", "nope", ctx)
    assert not app.learned_aliases_path.exists()


def _model_query(workdir, *, series="Mille-feuille Royal pour la Princesse"):
    app = make_app(workdir)
    ctx = RecallContext(settings=app)
    query = {
        "question": "皇家千层公主价格",
        "brand": "Baby, the Stars Shine Bright",
        "brand_id": "baby",
        "region": "JP",
        "series_en": series,
        "series_cn": "皇家千层公主",
        "product_number": "",
        "confidence": "medium",
        "source": "model",
        "is_specific": True,
        "verified": False,
        "notes": ["模型识别，待 Lolibrary 档案核实"],
    }
    return app, ctx, query


def _detail(**overrides):
    data = {
        "url": "https://lolibrary.org/items/btssb-royal",
        "title": "Mille-feuille Royal pour la Princesse OP",
        "brand": "Baby, the Stars Shine Bright",
        "category": "OP",
        "product_number": "B46HC311",
        "release_year": "2021",
        "original_price": "¥357500 JPY",
        "colors": ("Pink", "White"),
    }
    data.update(overrides)
    return lolibrary_online.ArchiveDetail(**data)


def test_model_result_verified_by_archive(workdir, monkeypatch):
    app, ctx, query = _model_query(workdir)
    monkeypatch.setattr(lolibrary_online, "lookup", lambda *a, **k: ([_detail()], ""))

    result = dress.build_profile(query, [], ctx, online=True)
    profile = result["profile"]

    assert profile["verified"] is True
    assert profile["source"] == "model_verified"
    assert profile["brand"] == "Baby, the Stars Shine Bright"
    assert profile["region"] == "JP"
    assert profile["release"] == "2021"
    assert profile["price"] == "¥357500 JPY"
    assert profile["style"] == "OP"
    assert profile["colors"] == ["Pink", "White"]
    assert result["evidence"]["source_kind"] == "third_party"
    assert any("已用 Lolibrary" in note for note in profile["notes"])

    learned = json.loads(app.learned_aliases_path.read_text(encoding="utf-8"))["aliases"]
    assert any(item["cn"] == "皇家千层公主" and item["brand"] == "baby" for item in learned)


def test_model_result_unverified_is_labeled(workdir, monkeypatch):
    _app, ctx, query = _model_query(workdir)
    monkeypatch.setattr(
        lolibrary_online, "lookup", lambda *a, **k: ([], "Lolibrary 没有搜到裙子条目")
    )

    result = dress.build_profile(query, [], ctx, online=True)
    profile = result["profile"]

    assert profile["verified"] is False
    assert profile["source"] == "model"
    assert profile["confidence"] == "low"
    assert profile["release"] == ""
    assert profile["price"] == ""
    assert result["evidence"]["source_kind"] == "realtime"
    assert any("未在 Lolibrary" in note for note in profile["notes"])


def test_model_result_wrong_archive_not_used(workdir, monkeypatch):
    _app, ctx, query = _model_query(workdir)
    monkeypatch.setattr(
        lolibrary_online,
        "lookup",
        lambda *a, **k: ([_detail(title="Unrelated Skirt", brand="Other Brand")], ""),
    )

    profile = dress.build_profile(query, [], ctx, online=True)["profile"]

    assert profile["verified"] is False
    assert profile["release"] == ""
    assert profile["url"] == ""
    assert profile["title"] == "Mille-feuille Royal pour la Princesse"


def test_glossary_split_and_candidates(workdir):
    app = make_app(workdir)
    terms = dress.load_glossary(app)
    words, unmatched = dress.split_keywords("甜心梦幻拿铁", terms)

    assert words == ["sweetheart", "dreamy", "latte"]
    assert unmatched == ""
    candidates = dress.glossary_candidates("甜心梦幻拿铁", terms)
    assert candidates[0] == "sweetheart dreamy latte"
    assert candidates[1] == "latte dreamy sweetheart"


def test_glossary_resolution_verifies_and_caches(workdir, monkeypatch):
    app = make_app(workdir)
    ctx = RecallContext(settings=app)
    pending = dress.resolve_dress("草莓奶油兔价格", ctx).to_dict()
    detail = lolibrary_online.ArchiveDetail(
        url="https://lolibrary.org/items/strawberry-cream-rabbit-jsk",
        title="Strawberry Cream Rabbit JSK",
        brand="Baby, the Stars Shine Bright",
        category="JSK",
        product_number="B1JS1",
        release_year="2024",
        original_price="¥30000 JPY",
        colors=("Pink",),
    )
    monkeypatch.setattr(lolibrary_online, "lookup", lambda *a, **k: ([detail], ""))

    updated, trace = dress.resolve_by_glossary(pending, ctx)

    assert updated["source"] == "glossary_verified"
    assert updated["verified"] is True
    assert updated["brand"] == "Baby, the Stars Shine Bright"
    assert updated["series_en"] == "strawberry cream rabbit"
    assert trace["matched_title"] == "Strawberry Cream Rabbit JSK"
    learned = json.loads(app.learned_aliases_path.read_text(encoding="utf-8"))["aliases"]
    assert any(item["cn"] == "草莓奶油兔" for item in learned)


def test_web_reverse_lookup_extracts_and_verifies(workdir, monkeypatch):
    app = make_app(workdir)
    app.search_enabled = True
    ctx = RecallContext(settings=app)
    pending = dress.resolve_dress("星月夜之梦价格", ctx).to_dict()
    monkeypatch.setattr(
        web_search,
        "search",
        lambda *a, **k: [web_search.SearchResult(
            title="星月夜之梦",
            url="https://example.com/post",
            snippet="Baby 的 Starry Night Dream JSK，2024 年款",
        )],
    )
    detail = lolibrary_online.ArchiveDetail(
        url="https://lolibrary.org/items/starry-night-dream-jsk",
        title="Starry Night Dream JSK",
        brand="Baby, the Stars Shine Bright",
        category="JSK",
        release_year="2024",
        colors=("Blue",),
    )
    monkeypatch.setattr(lolibrary_online, "lookup", lambda *a, **k: ([detail], ""))

    updated, trace = dress.resolve_by_web(pending, ctx)

    assert updated["source"] == "search_verified"
    assert updated["verified"] is True
    assert updated["series_en"] == "Starry Night Dream JSK"
    assert trace["matched_title"] == "Starry Night Dream JSK"
    assert "Starry Night Dream JSK" in trace["candidates"]


def test_parse_dress_reply_protocol():
    hint = parse_dress_reply(json.dumps({
        "brand": "Baby",
        "region": "jp",
        "series": "Mille-feuille Royal pour la Princesse",
        "series_cn": "皇家千层公主",
        "confidence": "HIGH",
    }, ensure_ascii=False))
    assert hint.region == "JP"
    assert hint.confidence == "high"

    hint = parse_dress_reply(json.dumps({"region": "火星", "confidence": "也许"}))
    assert hint.region == ""
    assert hint.confidence == "low"

    with pytest.raises(PlannerOutputError):
        parse_dress_reply("not json")


# --------------------------------------------------------------------- 在线解析


SEARCH_HTML = """
<div class="card">
  <p title="Test Series JSK"><a href="https://lolibrary.org/items/test-jsk">Test Series JSK</a></p>
  <p class="text-muted small mb-0" title="テストシリーズ">テストシリーズ</p>
  <p class="text-muted itemnum mb-0" title="B1JS1">B1JS1</p>
  <a href="https://lolibrary.org/brands/test-brand">Test Brand</a>
  <a href="https://lolibrary.org/categories/jsk" class="category">JSK</a>
  <a href="https://lolibrary.org/items/test-jsk">View Item</a>
</div>
"""

ITEM_HTML = """
<div>
  <p>Released in <span class="text-regular">2021</span></p>
  <p>Product number: <span class="text-regular">B1JS1</span></p>
  <span title="Japanese Yen (JPY) - ¥">Originally listed for
    <span class="text-regular">¥12000 JPY</span></span>
  <div>Official colors<br />-pink lame<br />-black<br /><br />Notes here</div>
</div>
"""


def test_online_lookup_parses_cards_and_item():
    def fetcher(url):
        if "/search?" in url:
            return SEARCH_HTML
        if url.endswith("/test-jsk"):
            return ITEM_HTML
        raise AssertionError(f"unexpected url: {url}")

    items, error = lolibrary_online.lookup(
        "Test Series", brand="Test Brand", fetcher=fetcher
    )

    assert error == ""
    assert len(items) == 1
    item = items[0]
    assert item.title == "Test Series JSK"
    assert item.brand == "Test Brand"
    assert item.category == "JSK"
    assert item.release_year == "2021"
    assert item.original_price == "¥12000 JPY"
    assert item.product_number == "B1JS1"
    assert item.colors == ("Pink", "Black")


def test_online_lookup_degrades_on_network_error():
    def fetcher(url):
        raise TimeoutError("boom")

    items, error = lolibrary_online.lookup("Test Series", fetcher=fetcher)

    assert items == []
    assert "Lolibrary 搜索失败" in error
