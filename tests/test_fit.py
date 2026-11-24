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


class FakePlanner:
    def __init__(self, values=None, error: bool = False):
        self.values = values or {"bust": 88, "waist": 70, "hips": 92}
        self.error = error
        self.calls: list[tuple[float, float]] = []

    def estimate_measurements(self, height_cm, weight_kg):
        self.calls.append((height_cm, weight_kg))
        if self.error:
            raise RuntimeError("estimate failed")
        return self.values


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


def test_length_prefers_exact_label_over_sleeve_and_bodice():
    """档案里 `Sleeve Length` / `Bodice Length` 常排在 `Length` 前面，不能当裙长。"""
    profile = {
        "measurements": [
            {"label": "Bodice Length", "value": "38 cm"},
            {"label": "Bust", "value": "90 cm"},
            {"label": "Length", "value": "99 cm"},
            {"label": "Sleeve Length", "value": "58 cm"},
        ]
    }
    text, _ = fit.judge(
        profile, [{"kind": "height", "value": "160cm"}, {"kind": "bust", "value": "90cm"}], "裙长到哪"
    )

    assert "着丈 99cm" in text
    assert "99cm 大约到过膝" in text


def test_length_uses_skirt_length_when_total_length_absent():
    profile = {
        "measurements": [
            {"label": "Sleeve Length", "value": "58 cm"},
            {"label": "Skirt Length", "value": "88 cm"},
        ]
    }
    text, step = fit.judge(profile, [{"kind": "height", "value": "160cm"}], "裙长到哪")

    assert "着丈 88cm" in text
    assert step is not None
    assert step["status"] == "length_only"


def test_owner_measurements_are_not_used():
    """`Owner Bust` 是样品持有人的尺寸，不是衣服平铺尺寸。"""
    profile = {"measurements": [{"label": "Owner Bust", "value": "86 cm"}]}
    text, step = fit.judge(profile, [{"kind": "bust", "value": "95cm"}], "我穿得下吗")

    assert "胸围：档案没写" in text
    assert step is not None
    assert step["status"] == "no_archive_data"


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


# --------------------------------------------------------------------- 具名工具


def test_judge_real_measurements_returns_fit_step():
    text, step = fit.judge(PROFILE, FACTS, "这条我穿得下吗")

    assert "【按你的身材】" in text
    assert "整体合身" in text
    assert step is not None
    assert step["kind"] == "fit"
    assert step["label"] == "判断合身"
    assert step["status"] == "measured"


def test_judge_estimated_is_labeled_reference_only():
    planner = FakePlanner({"bust": 88, "waist": 70, "hips": 92})
    facts = [{"kind": "height", "value": "160cm"}, {"kind": "weight", "value": "55kg"}]

    text, step = fit.judge(PROFILE, facts, "这条我穿得下吗", planner)

    assert planner.calls == [(160.0, 55.0)]
    # 估算结论必须单独一段、明确写「仅供参考」，不能和实测混在一起。
    assert "【按身高体重估算，仅供参考】" in text
    assert "不是实测" in text
    assert "【按你的身材】" not in text
    assert "整体合身" in text
    assert step is not None
    assert step["status"] == "estimated"
    assert "仅供参考" in step["summary"]


def test_judge_asks_for_data_when_nothing_stored():
    text, step = fit.judge(PROFILE, [], "这条我穿得下吗")

    assert "三围" in text
    assert "身高和体重" in text
    assert "只能估算" in text
    assert step is not None
    assert step["status"] == "need_data"


def test_judge_asks_for_data_even_if_estimate_fails():
    facts = [{"kind": "height", "value": "160cm"}, {"kind": "weight", "value": "55kg"}]

    text, step = fit.judge(PROFILE, facts, "这条我穿得下吗", FakePlanner(error=True))

    assert "三围" in text
    assert step is not None
    assert step["status"] == "need_data"


def test_judge_no_archive_measurements_never_guesses():
    text, step = fit.judge(
        {"measurements": [], "colors": ["Pink"]}, FACTS, "这条我穿得下吗"
    )

    assert "没有写平铺尺寸" in text
    for verdict in ("合身", "宽松", "略小"):
        assert verdict not in text
    assert step is not None
    assert step["status"] == "no_archive_data"


def test_judge_uses_height_for_skirt_length():
    facts = [{"kind": "height", "value": "160cm"}]

    text, step = fit.judge(PROFILE, facts, "这条裙子多长，到哪")

    # 89 / 160 ≈ 0.556 → 及膝
    assert "着丈 89cm" in text
    assert "大约到及膝" in text
    assert step is not None
    assert step["status"] == "length_only"


# --------------------------------------------------------------------- 小物


def test_small_item_with_waist_is_judged_like_a_dress():
    """小物档案里有腰围就跟裙子一样按实测比，不能因为类目被跳过。"""
    profile = {
        "style": "Accessory",
        "measurements": [
            {"label": "Length", "value": "23 cm"},
            {"label": "Waist", "value": "62-102 cm"},
        ],
    }

    text, step = fit.judge(profile, [{"kind": "waist", "value": "70cm"}], "这个穿得下吗")

    assert "腰围：档案 62~102cm，你 70cm 在范围内，合身" in text
    assert step is not None
    assert step["status"] == "measured"
    # 小物的 Length 是物件大小，不能拿身高推裙长落点。
    assert "【裙长】" not in text


def test_small_item_without_body_parts_reports_item_dimensions():
    """发饰只有长度/宽度：说清档案写了什么、比不了身体哪个部位，不猜。"""
    profile = {
        "style": "Hair Accessory",
        "measurements": [
            {"label": "Length", "value": "31 cm"},
            {"label": "Width", "value": "11 cm"},
        ],
    }

    text, step = fit.judge(
        profile,
        [{"kind": "bust", "value": "85cm"}, {"kind": "height", "value": "160cm"}],
        "这个我戴得下吗",
    )

    assert "这份档案是小物（Hair Accessory）" in text
    assert "只写了长度 31 cm、宽度 11 cm" in text
    assert "没有胸围/腰围/臀围" in text
    assert "我不猜" in text
    assert "【裙长】" not in text
    assert step is not None
    assert step["status"] == "no_archive_data"


def test_small_item_without_measurements_says_it_cannot_judge():
    profile = {"style": "Hair Accessory", "measurements": []}

    text, step = fit.judge(profile, FACTS, "这个我戴得下吗")

    assert "没有写平铺尺寸" in text
    assert "判断不了" in text
    assert step is not None
    assert step["status"] == "no_archive_data"


# --------------------------------------------------------------------- 接线


def test_finish_appends_fit_note_for_resolved_dress(data_dir):
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
    assert any(step.get("kind") == "fit" for step in payload["steps"])

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
