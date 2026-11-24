"""按身材判断穿不穿得下 + 喜欢的颜色提醒。"""

from __future__ import annotations

from app import db
from app.agent import fit, graph
from app.agent.channels import RecallContext
from app.config import settings

PROFILE = {
    "title": "Lovely Princess JSK",
    "colors": ["Pink", "Black", "Sax", "White"],
    "measurements": [
        {"label": "Bust", "value": "91 - 101 cm (35.83 - 39.76 in)"},
        {"label": "Waist", "value": "74 - 86 cm (29.13 - 33.86 in)"},
        {"label": "Length", "value": "89 cm (35.04 in)"},
    ],
}

FACTS = [
    {"kind": "bust", "value": "85cm"},
    {"kind": "waist", "value": "70cm"},
    {"kind": "color", "value": "粉色"},
]


# --------------------------------------------------------------------- 尺寸


def test_loose_and_fit_verdicts_name_part_and_gap():
    text = fit.advice(PROFILE, FACTS)

    # 85 比档案下限 91 小 6cm，在 8cm 放松量内 → 合身；腰围同理。
    assert "胸围：档案 91~101cm，你 85cm 比下限小 6cm" in text
    assert "腰围：档案 74~86cm，你 70cm 比下限小 4cm" in text
    assert "整体合身" in text
    assert "这个款式有粉色" in text


def test_too_small_verdict_states_the_gap():
    profile = {
        "measurements": [{"label": "Waist", "value": "70 - 75 cm (27.55 - 29.52 in)"}]
    }
    text = fit.advice(profile, [{"kind": "waist", "value": "85cm"}])

    assert "略小" in text
    # 档案上限 75cm 比用户 85cm 小 10cm，差多少要写清楚。
    assert "档案上限 75cm" in text
    assert "比你（85cm）小 10cm" in text
    assert "整体略小" in text


def test_loose_verdict_states_the_gap():
    profile = {
        "measurements": [{"label": "Bust", "value": "104 - 110 cm (40.94 - 43.31 in)"}]
    }
    text = fit.advice(profile, [{"kind": "bust", "value": "85cm"}])

    assert "宽松" in text
    assert "档案下限 104cm" in text
    assert "比你（85cm）大 19cm" in text


def test_within_range_is_fit():
    profile = {
        "measurements": [{"label": "Bust", "value": "80 - 90 cm (31.5 - 35.43 in)"}]
    }
    text = fit.advice(profile, [{"kind": "bust", "value": "85cm"}])

    assert "你 85cm 在范围内" in text
    assert "合身" in text


def test_fullwidth_tilde_range_is_parsed():
    """日牌档案常用全角「～」写区间，不能只取到后一个数。"""
    profile = {
        "measurements": [{"label": "Bust", "value": "about 90～97cm"}]
    }
    text = fit.advice(profile, [{"kind": "bust", "value": "85cm"}])

    assert "档案 90~97cm" in text
    assert "比下限小 5cm" in text
    assert "合身" in text


def test_missing_measurements_says_so_without_guessing():
    text = fit.advice({"colors": [], "measurements": []}, FACTS)

    assert "没有写平铺尺寸" in text
    assert "判断不了" in text
    # 没有任何「合身 / 宽松 / 略小」的假结论。
    for verdict in ("合身", "宽松", "略小"):
        assert verdict not in text


def test_missing_single_part_is_named():
    profile = {
        "measurements": [{"label": "Bust", "value": "91 - 101 cm (35.83 - 39.76 in)"}]
    }
    text = fit.advice(profile, [{"kind": "bust", "value": "85cm"}, {"kind": "waist", "value": "70cm"}])

    assert "胸围" in text
    assert "腰围：档案没写" in text


def test_no_user_measurements_no_fit_section():
    assert fit.advice(PROFILE, [{"kind": "color", "value": "粉色"}]) == (
        "【配色偏好】这个款式有粉色。"
    )
    assert fit.advice(PROFILE, []) == ""
    assert fit.advice({}, FACTS) == ""


# --------------------------------------------------------------------- 配色


def test_liked_color_absent_is_silent():
    profile = {"colors": ["Black", "White"], "measurements": []}
    assert "粉色" not in fit.advice(profile, [{"kind": "color", "value": "粉色"}])


def test_liked_color_matches_english_alias():
    profile = {"colors": ["Sax Blue", "Ivory"], "measurements": []}
    text = fit.advice(profile, [{"kind": "color", "value": "天蓝"}])
    assert text == "【配色偏好】这个款式有天蓝。"


# --------------------------------------------------------------------- 接线


def test_finish_appends_note_for_resolved_dress(data_dir):
    db.init_db(settings.db_path)
    account = db.create_account(settings.db_path, "owner", "scrypt$1$1$1$x$y")
    for kind, value in (("bust", "85cm"), ("waist", "70cm"), ("color", "粉色")):
        db.upsert_memory(settings.db_path, account["id"], kind, value, "我胸围85cm")

    ctx = RecallContext(settings=settings, account_id=account["id"])
    state = {
        "answer": "这条是 Lovely Princess JSK。",
        "evidence": [],
        "steps": [],
        "dress_profile": PROFILE,
        "dress_unresolved": False,
        "dress_unverified": False,
    }

    payload = graph._finish(
        "这条裙子我穿得下吗",
        state,
        ctx=ctx,
        planner=object(),
        writer=None,
        history=[],
        duration_ms=1,
    )

    assert payload["answer"].startswith("这条是 Lovely Princess JSK。")
    assert "整体合身" in payload["answer"]
    assert "这个款式有粉色" in payload["answer"]

    # 未核实的档案不参与判断：不拿猜的尺寸下结论。
    unverified = dict(state, dress_unverified=True)
    payload = graph._finish(
        "这条裙子我穿得下吗",
        unverified,
        ctx=ctx,
        planner=object(),
        writer=None,
        history=[],
        duration_ms=1,
    )
    assert "整体合身" not in payload["answer"]
