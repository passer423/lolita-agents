"""按需图片文字识别：触发条件、独立证据、未校验标注、降级、缓存与开关。"""

from __future__ import annotations

import time

import pytest

from app.agent import graph, image_evidence, writer
from app.agent.channels import RecallContext, _search_evidence
from app.config import AppSettings
from app.services import ocr, web_search

POST_URL = "https://www.xiaohongshu.com/explore/abc"
IMAGE_URLS = [
    "https://sns-img.qianfan.example/1.webp",
    "https://sns-img.qianfan.example/2.webp",
    "https://sns-img.qianfan.example/3.webp",
    "https://sns-img.qianfan.example/4.webp",
]
CLUE_TEXT = "一人血书求四时花再贩（存档价格表 对应的价格标图上"
NO_CLUE_TEXT = (
    "这条裙子的相关信息都放在下面了，大家可以自己点开查看具体内容，"
    "这里就不再多做说明了，谢谢大家一直以来的支持与喜欢，也欢迎在评论区交流。"
)


def _settings(tmp_path, **overrides) -> AppSettings:
    return AppSettings(
        data_dir=tmp_path / "data",
        content_dir=tmp_path / "content",
        **overrides,
    )


def _ctx(tmp_path, **overrides) -> RecallContext:
    # conftest 默认把 OCR 关掉以免测试真联网；这里的用例要显式打开。
    overrides.setdefault("ocr_enabled", True)
    return RecallContext(settings=_settings(tmp_path, **overrides))


def _xhs(text: str, *, title: str = "一人血书求四时花再贩", images: int = 3, url: str = POST_URL,
         urls: list[str] | None = None) -> dict:
    result = web_search.SearchResult(
        title=title,
        url=url,
        snippet=text,
        content=text,
        image_count=images,
        image_urls=list(urls if urls is not None else IMAGE_URLS[:images]),
    )
    return _search_evidence("xhs", [result])[0]


@pytest.fixture()
def ready(monkeypatch):
    monkeypatch.setattr(ocr, "readiness", lambda app: (True, ""))


def _stub_ocr(monkeypatch, text: str = "价格表\n天丝 OP 长款本体 188 390 578 598") -> list[str]:
    calls: list[str] = []

    def fake(url: str, app: AppSettings) -> str:
        calls.append(url)
        return text

    monkeypatch.setattr(ocr, "text_from_url", fake)
    return calls


# ---------- 触发条件 ----------


def test_trigger_on_clue_in_body(tmp_path, ready, monkeypatch):
    calls = _stub_ocr(monkeypatch)
    out = image_evidence.collect("四时花价格", [_xhs(CLUE_TEXT)], _ctx(tmp_path))
    assert len(out) == 1
    assert calls == IMAGE_URLS[:3]


def test_trigger_on_short_body_with_images(tmp_path, ready, monkeypatch):
    calls = _stub_ocr(monkeypatch)
    out = image_evidence.collect("四时花价格", [_xhs("四时花自存")], _ctx(tmp_path))
    assert len(out) == 1
    assert calls == IMAGE_URLS[:3]


def test_fallback_trigger_when_no_price_number_anywhere(tmp_path, ready, monkeypatch):
    calls = _stub_ocr(monkeypatch)
    out = image_evidence.collect("四时花价格", [_xhs(NO_CLUE_TEXT)], _ctx(tmp_path))
    assert len(out) == 1, "全篇没有价格数字时，带图帖子应作为兜底候选"
    assert calls == IMAGE_URLS[:3]


def test_fallback_skipped_when_other_evidence_has_price(tmp_path, ready, monkeypatch):
    calls = _stub_ocr(monkeypatch)
    other = {"channel": "web", "source_kind": "realtime", "title": "四时花", "text": "现货价：578 元"}
    out = image_evidence.collect("四时花价格", [_xhs(NO_CLUE_TEXT), other], _ctx(tmp_path))
    assert out == []
    assert calls == [], "别的证据已经写出价格，不应再花预算识别"


def test_never_trigger_for_non_numeric_question(tmp_path, ready, monkeypatch):
    calls = _stub_ocr(monkeypatch)
    out = image_evidence.collect("四时花怎么洗", [_xhs("四时花自存")], _ctx(tmp_path))
    assert out == []
    assert calls == []


def test_no_trigger_without_images(tmp_path, ready, monkeypatch):
    calls = _stub_ocr(monkeypatch)
    out = image_evidence.collect("四时花价格", [_xhs(CLUE_TEXT, images=0, urls=[])], _ctx(tmp_path))
    assert out == []
    assert calls == []


# ---------- 独立证据与编号 ----------


def test_image_text_is_independent_evidence(tmp_path, ready, monkeypatch):
    _stub_ocr(monkeypatch, "价格表\n天丝 OP 长款本体 188 390 578 598")
    post = _xhs(CLUE_TEXT)
    original_text = post["text"]
    out = image_evidence.collect("四时花价格", [post], _ctx(tmp_path))
    item = out[0]

    assert item["channel"] == "image_text"
    assert item["source_kind"] == "image_text"
    assert item["url"] == POST_URL
    assert item["ref"] == f"{POST_URL}#image_text"
    assert item["title"] == "一人血书求四时花再贩｜图上文字（OCR，未校验）"
    assert item["image_url"] == IMAGE_URLS[0]
    assert item["image_urls"] == IMAGE_URLS[:3]
    # 原帖正文不能被覆盖或拼接
    assert post["text"] == original_text
    assert "价格表" not in original_text or "OCR" not in post["text"]


def test_text_marks_ocr_unverified(tmp_path, ready, monkeypatch):
    _stub_ocr(monkeypatch)
    item = image_evidence.collect("四时花价格", [_xhs(CLUE_TEXT)], _ctx(tmp_path))[0]
    assert "OCR，未校验" in item["text"]
    assert "以原图为准" in item["text"]


def test_budget_two_evidence_three_images(tmp_path, ready, monkeypatch):
    calls = _stub_ocr(monkeypatch)
    posts = [
        _xhs(CLUE_TEXT, url=f"{POST_URL}-{index}") for index in range(3)
    ]
    out = image_evidence.collect("四时花价格", posts, _ctx(tmp_path))
    assert len(out) == 2
    assert len(calls) == 6


def test_graph_keeps_image_text_next_to_post(tmp_path, ready, monkeypatch):
    _stub_ocr(monkeypatch)
    post = _xhs(CLUE_TEXT)
    image = image_evidence.collect("四时花价格", [post], _ctx(tmp_path))[0]
    ordered = graph._normalize([post, image], ["xhs"])
    kinds = [item["source_kind"] for item in ordered]
    assert kinds.count("realtime") == 1
    assert kinds.count("image_text") == 1

    filler = [
        {"channel": "own", "source_kind": "own_content", "ref": f"own-{i}", "title": "t", "text": "x"}
        for i in range(30)
    ]
    cut = graph._normalize([*filler, post, image], ["xhs"], limit=12)
    assert any(item["source_kind"] == "image_text" for item in cut), "补查通道的派生证据应豁免截断"


# ---------- 写作层标注 ----------


def test_writer_marks_unverified_and_appellation(tmp_path, ready, monkeypatch):
    _stub_ocr(monkeypatch)
    image = image_evidence.collect("四时花价格", [_xhs(CLUE_TEXT)], _ctx(tmp_path))[0]
    image["n"] = 4

    assert writer.source_appellation(image) == writer.APPELLATION_IMAGE_TEXT
    block = writer._evidence_block([image])
    assert "【仅作线索·不得当作规范】" in block
    assert "图上文字（OCR，未校验）" in block
    assert "以原图为准" in block


def test_image_hint_suppressed_when_ocr_evidence_exists(tmp_path, ready, monkeypatch):
    _stub_ocr(monkeypatch)
    post = _xhs(CLUE_TEXT)
    image = image_evidence.collect("四时花价格", [post], _ctx(tmp_path))[0]
    assert writer.image_hint("四时花价格", [post]) != ""
    assert writer.image_hint("四时花价格", [post, {**image, "n": 4}]) == ""


# ---------- 降级与开关 ----------


def test_switch_off_skips_everything(tmp_path, monkeypatch):
    calls = _stub_ocr(monkeypatch)
    out = image_evidence.collect("四时花价格", [_xhs(CLUE_TEXT)], _ctx(tmp_path, ocr_enabled=False))
    assert out == []
    assert calls == []


def test_unavailable_engine_degrades_silently(tmp_path, monkeypatch):
    calls = _stub_ocr(monkeypatch)
    monkeypatch.setattr(ocr, "readiness", lambda app: (False, "未安装 rapidocr_onnxruntime"))
    out = image_evidence.collect("四时花价格", [_xhs(CLUE_TEXT)], _ctx(tmp_path))
    assert out == []
    assert calls == []


def test_download_failure_returns_none(tmp_path, monkeypatch):
    def boom(*args, **kwargs):
        raise ocr.OcrError("图片 HTTP 404")

    monkeypatch.setattr(ocr, "_download", boom)
    assert ocr.text_from_url("https://sns-img.qianfan.example/x.webp", _settings(tmp_path)) is None


def test_ocr_timeout_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(ocr, "_download", lambda *args, **kwargs: b"image-bytes")
    monkeypatch.setattr(ocr, "_recognize_bytes", lambda data: time.sleep(0.3) or "578")
    started = time.monotonic()
    assert ocr.text_from_url(
        "https://sns-img.qianfan.example/slow.webp",
        _settings(tmp_path, ocr_image_timeout=0.05),
    ) is None
    assert time.monotonic() - started < 0.5


def test_total_budget_stops_remaining_images(tmp_path, ready, monkeypatch):
    calls: list[str] = []

    def slow(url: str, app: AppSettings) -> str:
        calls.append(url)
        time.sleep(0.03)
        return "578"

    monkeypatch.setattr(ocr, "text_from_url", slow)
    posts = [_xhs(CLUE_TEXT, url=f"{POST_URL}-{index}") for index in range(2)]
    out = image_evidence.collect(
        "四时花价格", posts, _ctx(tmp_path, ocr_total_budget=0.01)
    )
    assert len(out) == 1
    assert calls == IMAGE_URLS[:1], "总预算用尽后不再识别剩余图片"


# ---------- 缓存 ----------


def test_ocr_result_cached_per_image_url(tmp_path, monkeypatch):
    calls = {"download": 0, "recognize": 0}

    def fake_download(*args, **kwargs):
        calls["download"] += 1
        return b"image-bytes"

    def fake_recognize(data: bytes) -> str:
        calls["recognize"] += 1
        return "价格表 578"

    monkeypatch.setattr(ocr, "_download", fake_download)
    monkeypatch.setattr(ocr, "_recognize_bytes", fake_recognize)
    app = _settings(tmp_path)

    assert ocr.text_from_url("https://sns-img.qianfan.example/c.webp", app) == "价格表 578"
    assert ocr.text_from_url("https://sns-img.qianfan.example/c.webp", app) == "价格表 578"
    assert calls == {"download": 1, "recognize": 1}


def test_cache_key_includes_engine_version(tmp_path, monkeypatch):
    app = _settings(tmp_path)
    monkeypatch.setattr(ocr, "engine_version", lambda: "1.0.0")
    first = ocr._cache_path(app, "https://sns-img.qianfan.example/c.webp")
    monkeypatch.setattr(ocr, "engine_version", lambda: "9.9.9")
    second = ocr._cache_path(app, "https://sns-img.qianfan.example/c.webp")
    assert first != second


def test_ocr_enabled_by_default():
    assert AppSettings.model_fields["ocr_enabled"].default is True


# ---------- 行还原与列错位防护 ----------


def _box(x: int, y: int, width: int = 20, height: int = 10) -> list[list[int]]:
    return [[x, y], [x + width, y], [x + width, y + height], [x, y + height]]


def test_group_lines_restores_row_order():
    """引擎顺序错乱时也要按 y 分行、行内按 x 从左到右。"""
    output = [
        (_box(120, 30), "390", 0.9),
        (_box(120, 0), "尾款", 0.9),
        (_box(0, 30), "长款本体", 0.9),
        (_box(0, 0), "定金", 0.9),
        (_box(60, 30), "188", 0.9),
    ]
    assert ocr._group_lines(output) == "定金 尾款\n长款本体 188 390"


OCR_TEXT = (
    "（OCR，未校验：按行拼接）\n"
    "图 1：定金 尾款 预约价 现货价\n"
    "长款本体 188 390 578 598\n"
    "短款本体 178 380 558 578"
)


def _image_item(text: str = OCR_TEXT, n: int = 4) -> dict:
    return {
        "n": n,
        "source_kind": "image_text",
        "channel": "image_text",
        "title": "四时花｜图上文字（OCR，未校验）",
        "text": text,
        "url": POST_URL,
    }


def test_demote_replaces_wrong_type_assertion_with_rows():
    answer = "图上标着棉 OP 短款预约价 390、现货价 578（OCR，未校验）[4]。"
    out, demotions = writer.demote_ocr_price_labels(answer, [_image_item()])

    assert "预约价 390" not in out
    assert "现货价 578" not in out
    assert "长款本体 188 390 578 598" in out
    assert "短款本体 178 380 558 578" in out
    assert "OCR，未校验" in out and "[4]" in out
    assert demotions and "预约价390" in demotions[0]["unsupported"]


def test_demote_keeps_verbatim_row_quote():
    answer = "图上标着「长款本体 188 390 578 598」（OCR，未校验）[4]。"
    out, demotions = writer.demote_ocr_price_labels(answer, [_image_item()])
    assert out == answer
    assert demotions == []


def test_demote_allows_label_when_it_is_literal_in_ocr():
    item = _image_item("（OCR，未校验）\n图 1：预约价 578")
    answer = "图上标着预约价 578（OCR，未校验）[4]。"
    out, demotions = writer.demote_ocr_price_labels(answer, [item])
    assert out == answer
    assert demotions == []


def test_demote_skips_sentence_citing_non_image_evidence():
    other = {"n": 1, "source_kind": "realtime", "channel": "web", "title": "官方", "text": "x"}
    answer = "官方页面写着预约价 390 [1]。"
    out, demotions = writer.demote_ocr_price_labels(answer, [_image_item(), other])
    assert out == answer
    assert demotions == []


def test_prompt_forbids_column_type_induction():
    for text in (writer._CLUE_HEADER, writer.SYNTH_SYSTEM):
        assert "不保证列对齐" in text
    assert "绝不能把数字归纳成" in writer.SYNTH_SYSTEM
    assert "绝不能把数字归纳成" in writer._CLUE_HEADER
