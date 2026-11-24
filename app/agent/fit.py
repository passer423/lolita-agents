"""按身材判断穿不穿得下 + 喜欢的颜色提醒。

长期记忆里有用户的三围（bust / waist / hips，显式带 cm），裙子档案里有
平铺尺寸（Bust / Waist / Hips，带 cm 区间）和配色。两者都是结构化字段，
所以这里只用规则比对，不调模型：模型很容易把「差 4cm」写成「应该没问题」，
而用户要的是能自己核对的数字。

判断口径：

* 档案上限比用户小 → 略小，差多少 cm 照实写；
* 档案下限比用户大超过 :data:`LOOSE_MARGIN_CM` → 宽松；
* 其余算合身（含布料放松量以内的小差距）。

档案没写尺寸就明说没写，不拿身高体重、版型或别人的试穿去猜。
"""

from __future__ import annotations

import re

#: 用户说的中文颜色 → Lolibrary 档案里的英文配色词（小写）。
#: 两边词表不同，靠这张表对上；没收录的颜色只是不提醒，不会误报。
COLOR_ALIASES: dict[str, tuple[str, ...]] = {
    "粉色": ("pink",),
    "粉": ("pink",),
    "玫红": ("pink", "red", "wine"),
    "蓝色": ("blue", "sax", "navy"),
    "蓝": ("blue", "sax", "navy"),
    "天蓝": ("sax", "sax blue", "blue"),
    "浅蓝": ("sax", "sax blue", "blue"),
    "藏青": ("navy", "blue"),
    "白色": ("white", "offwhite", "off-white", "ivory"),
    "白": ("white", "offwhite", "off-white", "ivory"),
    "黑色": ("black",),
    "黑": ("black",),
    "红色": ("red",),
    "红": ("red",),
    "酒红": ("wine", "burgundy", "red"),
    "紫色": ("purple", "lavender"),
    "紫": ("purple", "lavender"),
    "绿色": ("green", "mint"),
    "绿": ("green", "mint"),
    "薄荷绿": ("mint", "green"),
    "黄色": ("yellow",),
    "黄": ("yellow",),
    "灰色": ("gray", "grey"),
    "灰": ("gray", "grey"),
    "棕色": ("brown",),
    "棕": ("brown",),
    "米色": ("ivory", "cream", "beige"),
    "杏色": ("beige", "cream"),
    "金银": ("gold", "silver"),
    "金色": ("gold", "champagne"),
    "银色": ("silver",),
}

#: 中文部位名与档案字段名的对应。
PART_LABELS = {"bust": "胸围", "waist": "腰围", "hips": "臀围"}
_PART_TITLES = {
    "bust": ("bust", "胸围"),
    "waist": ("waist", "腰围"),
    "hips": ("hips", "hip", "臀围"),
    "length": ("length", "着丈", "总长", "裙长", "衣长"),
}

#: 带这些词的复合标签不是要判断的部位：`Sleeve Length` / `Bodice Length` /
#: `Handle Length` 都不是裙长，`Owner Bust` 是样品持有人的尺寸。档案里
#: 这类标签出现得比正牌标签还早，只按「包含」匹配会把胸衣长当裙长。
_PART_EXCLUDES = {
    "bust": ("underbust", "owner"),
    "waist": ("owner",),
    "hips": ("owner",),
    "length": ("sleeve", "bodice", "handle", "owner", "shoulder", "cuff"),
}

#: 小物类目：档案里的 Length / Width 是物件本身的大小，不是着丈，
#: 不能拿身高去推「裙长到哪」。
_SMALL_ITEM_CATEGORIES = (
    "hair accessory", "accessory", "jewelry", "bag", "socks", "tights",
    "shoes", "umbrella", "parasol", "gloves",
)

#: 档案尺寸标签 → 中文，供「档案里写了哪些尺寸」这句使用；没收录的保留原标签。
_MEASURE_LABELS = {
    "length": "长度",
    "width": "宽度",
    "height": "高度",
    "diameter": "直径",
    "bust": "胸围",
    "waist": "腰围",
    "hips": "臀围",
    "shoulder": "肩宽",
    "sleeve length": "袖长",
    "sole length": "鞋内长",
    "heel height": "跟高",
}

#: 明确在问「穿不穿得下」的说法。身高/体重只是数据，单独出现不算问句，
#: 否则「我身高160，最近有什么上新」也会被当成合身问题。
FIT_ASK_MARKERS = (
    "穿得下",
    "穿不下",
    "合身",
    "能不能穿",
    "能穿",
    "穿多大",
    "穿什么码",
    "选码",
    "码数",
    "尺码",
)

#: 提到这些词时才做合身/裙长判断，避免每条裙子回答都塞一段估算。
FIT_MARKERS = (*FIT_ASK_MARKERS, "身高", "体重")
LENGTH_MARKERS = ("裙长", "多长", "长度", "到哪", "及膝", "过膝", "及踝", "长不长", "短不短")

GUIDE_TEXT = (
    "【判断合身】想判断穿不穿得下，最好给我三围：胸围 / 腰围 / 臀围（cm）。"
    "只给身高和体重也能算，但只能估算，仅供参考。"
)

#: 平铺下限比身体大多少以内仍算合身——布料与版型有放松量，
#: 超过这个差距才算明显宽松。
LOOSE_MARGIN_CM = 8.0

_CM_RANGE = re.compile(
    r"(\d+(?:\.\d+)?)\s*(?:[-~–—～〜]\s*(\d+(?:\.\d+)?))?\s*cm\b",
    re.IGNORECASE,
)
_NUMBER = re.compile(r"(\d+(?:\.\d+)?)")


def _fmt(value: float) -> str:
    return str(int(value)) if value == int(value) else f"{value:g}"


def _range_text(low: float, high: float) -> str:
    return f"{_fmt(low)}cm" if low == high else f"{_fmt(low)}~{_fmt(high)}cm"


def _cm_range(value: str) -> tuple[float, float] | None:
    """从档案值里取 cm 区间；`88 - 94 cm (…in)` 取 ``(88, 94)``。"""
    match = _CM_RANGE.search(value or "")
    if not match:
        return None
    low = float(match.group(1))
    high = float(match.group(2)) if match.group(2) else low
    return low, high


def _user_cm(facts: list[dict], kind: str) -> float | None:
    for fact in facts:
        if fact.get("kind") != kind:
            continue
        match = _NUMBER.search(str(fact.get("value") or ""))
        if match:
            return float(match.group(1))
    return None


def _measurement(profile: dict, part: str) -> tuple[str, tuple[float, float] | None]:
    """按部位取档案平铺尺寸，返回 ``(原始值, cm 区间)``；没写返回 ``("", None)``。

    同一部位可能有多个标签（`Bodice Length` 与 `Length`、`Sleeve Length` 与
    `Length`），按「完全等于标题 > 以标题开头 > 包含标题」排序取最贴合的那个，
    并跳过袖长/胸衣长/样品持有人尺寸这类同名不同义的标签。
    """
    titles = _PART_TITLES[part]
    excludes = _PART_EXCLUDES.get(part, ())
    best: tuple[int, str, tuple[float, float] | None] | None = None
    for item in profile.get("measurements") or []:
        label = str(item.get("label") or "").strip()
        lowered = label.lower()
        if not label or any(bad in lowered for bad in excludes):
            continue
        if lowered in titles:
            rank = 0
        elif any(lowered.startswith(title) for title in titles):
            rank = 1
        elif any(title in lowered for title in titles):
            rank = 2
        else:
            continue
        if best is None or rank < best[0]:
            value = str(item.get("value") or "").strip()
            best = (rank, value, _cm_range(value))
    if best is None:
        return "", None
    return best[1], best[2]


def _category(profile: dict) -> str:
    return str(profile.get("style") or profile.get("category") or "").strip()


def _is_small_item(profile: dict) -> bool:
    """发饰/包/袜/鞋这类小物：有尺寸就照实说，但不按身高推裙长。"""
    category = _category(profile).lower()
    return bool(category) and any(marker in category for marker in _SMALL_ITEM_CATEGORIES)


def _has_body_part(profile: dict) -> bool:
    """档案里是否有能和身体比的部位（胸围/腰围/臀围）。"""
    return any(_measurement(profile, part)[1] is not None for part in PART_LABELS)


def _dimensions_text(profile: dict) -> str:
    rows: list[str] = []
    for item in profile.get("measurements") or []:
        label = str(item.get("label") or "").strip()
        value = str(item.get("value") or "").strip()
        if not label or not value:
            continue
        name = _MEASURE_LABELS.get(label.lower(), label)
        rows.append(f"{name} {value}")
    return "、".join(rows)


def _item_size_only_text(profile: dict) -> str:
    """小物档案只有物件尺寸、没有身体部位可比时的说明；不猜。"""
    category = _category(profile) or "小物"
    return (
        f"【按你的身材】这份档案是小物（{category}），只写了{_dimensions_text(profile)}，"
        "没有胸围/腰围/臀围这类身体部位可比，判断不了穿不穿得下，我不猜。"
    )


def _compare(part: str, user: float, profile: dict) -> tuple[str, str | None]:
    """把一个部位和档案比一次；返回 ``(结论行, 宽松/合身/略小)``。"""
    part_label = PART_LABELS[part]
    value, cm_range = _measurement(profile, part)
    if not value:
        return f"{part_label}：档案没写，这个部位不判断。", None
    if cm_range is None:
        return f"{part_label}：档案写的是「{value}」，取不到 cm 数值，不判断。", None

    low, high = cm_range
    user_text = _fmt(user)
    if user > high:
        diff = user - high
        verdict = "略小"
        detail = f"档案上限 {_fmt(high)}cm，比你（{user_text}cm）小 {_fmt(diff)}cm"
    elif user < low and low - user > LOOSE_MARGIN_CM:
        diff = low - user
        verdict = "宽松"
        detail = f"档案下限 {_fmt(low)}cm，比你（{user_text}cm）大 {_fmt(diff)}cm"
    elif user < low:
        diff = low - user
        verdict = "合身"
        detail = (
            f"档案 {_range_text(low, high)}，你 {user_text}cm 比下限小 "
            f"{_fmt(diff)}cm，在 {_fmt(LOOSE_MARGIN_CM)}cm 放松量内"
        )
    else:
        verdict = "合身"
        detail = f"档案 {_range_text(low, high)}，你 {user_text}cm 在范围内"
    return f"{part_label}：{detail}，{verdict}（{value}）。", verdict


def _overall(verdicts: list[str]) -> str:
    if not verdicts:
        # 一个部位都没比出来（档案缺这几项）时不能给「整体合身」的假结论。
        return ""
    if "略小" in verdicts:
        return "整体略小、会偏紧"
    if verdicts and all(verdict == "宽松" for verdict in verdicts):
        return "整体宽松"
    if "宽松" in verdicts:
        return "整体合身偏宽松"
    return "整体合身"


def _fit_section(profile: dict, facts: list[dict]) -> tuple[str, str]:
    users = {part: _user_cm(facts, part) for part in PART_LABELS}
    if all(value is None for value in users.values()):
        return "", ""
    if not profile.get("measurements"):
        return "【按你的身材】这份档案没有写平铺尺寸，判断不了穿不穿得下，我不猜。", ""

    lines: list[str] = []
    verdicts: list[str] = []
    for part, user in users.items():
        if user is None:
            continue
        line, verdict = _compare(part, user, profile)
        lines.append(line)
        if verdict:
            verdicts.append(verdict)
    overall = _overall(verdicts)
    text = "【按你的身材】" + "".join(lines)
    if overall:
        text += f"{overall}。"
    return text, overall


def _length_section(profile: dict, facts: list[dict]) -> str:
    """身高 + 着丈 → 裙长落点。阈值按肩点到膝/踝的人体比例估。"""
    height = _user_cm(facts, "height")
    if height is None:
        return ""
    value, cm_range = _measurement(profile, "length")
    if not cm_range:
        return ""
    low, high = cm_range
    length = (low + high) / 2
    ratio = length / height
    if ratio < 0.50:
        where = "膝上"
    elif ratio < 0.56:
        where = "及膝"
    elif ratio < 0.70:
        where = "过膝"
    elif ratio < 0.76:
        where = "及小腿"
    else:
        where = "及踝"
    shown = _fmt(low) if low == high else f"{_fmt(low)}~{_fmt(high)}"
    return (
        f"【裙长】按你 {_fmt(height)}cm 的身高，着丈 {shown}cm 大约到{where}"
        "（按肩点到裙摆估算，实际受腰线和版型影响）。"
    )


def _estimated_section(
    profile: dict, estimates: dict[str, float], height: float, weight: float
) -> tuple[str, str]:
    lines: list[str] = []
    verdicts: list[str] = []
    for part, user in estimates.items():
        line, verdict = _compare(part, user, profile)
        lines.append(line)
        if verdict:
            verdicts.append(verdict)
    overall = _overall(verdicts)
    values = "、".join(
        f"{PART_LABELS[part]} {_fmt(estimates[part])}cm" for part in PART_LABELS
    )
    return (
        f"【按身高体重估算，仅供参考】按 {_fmt(height)}cm / {_fmt(weight)}kg "
        f"估算三围：{values}（不是实测，不能当准确结论）。"
        + "".join(lines)
        + f"{overall}。",
        overall,
    )


def _estimate(planner, height: float, weight: float) -> dict[str, float] | None:
    """让模型按身高体重估三围；planner 不支持或结果越界都当失败。"""
    if planner is None:
        return None
    try:
        values = planner.estimate_measurements(height, weight)
    except Exception:  # noqa: BLE001 - 估不出来就退回反问，不猜
        return None
    estimates: dict[str, float] = {}
    for part in PART_LABELS:
        try:
            value = float(values.get(part))
        except (AttributeError, TypeError, ValueError):
            return None
        if not 40 <= value <= 150:
            return None
        estimates[part] = value
    return estimates


def _alias_hit(color: str, alias: str) -> bool:
    lowered = color.lower()
    if alias.isascii():
        return re.search(
            rf"(?<![a-z]){re.escape(alias)}(?![a-z])", lowered
        ) is not None
    return alias in lowered


def _color_section(profile: dict, facts: list[dict]) -> str:
    colors = [str(item) for item in (profile.get("colors") or []) if str(item).strip()]
    if not colors:
        return ""
    liked: list[str] = []
    for fact in facts:
        if fact.get("kind") != "color":
            continue
        name = str(fact.get("value") or "").strip()
        if not name or name in liked:
            continue
        aliases = COLOR_ALIASES.get(name) or (name,)
        if any(_alias_hit(color, alias) for color in colors for alias in aliases):
            liked.append(name)
    if not liked:
        return ""
    return "【配色偏好】这个款式有" + "、".join(liked) + "。"


def _wants_fit(question: str) -> bool:
    text = question or ""
    return any(marker in text for marker in FIT_MARKERS)


def asks_fit(question: str) -> bool:
    """是否明确在问「穿不穿得下」；只是报了身高体重不算。"""
    text = question or ""
    return any(marker in text for marker in FIT_ASK_MARKERS)


def _wants_length(question: str) -> bool:
    text = question or ""
    return any(marker in text for marker in LENGTH_MARKERS)


def is_fit_question(question: str) -> bool:
    return _wants_fit(question) or _wants_length(question)


def advice(profile: dict, facts: list[dict]) -> str:
    """基于档案与长期记忆生成补充结论；没有可用信息时返回空串。

    只用于已解析出裙子档案的回答：调用方应跳过未核实/未解析的情况，
    避免把「猜的档案」当事实来比尺寸。
    """
    if not profile or not facts:
        return ""
    fit_text, _overall_text = _fit_section(profile, facts)
    sections = [
        section for section in (fit_text, _color_section(profile, facts)) if section
    ]
    return "\n".join(sections)


def judge(
    profile: dict,
    facts: list[dict],
    question: str,
    planner=None,
) -> tuple[str, dict | None]:
    """具名工具「判断合身」：返回 ``(补充文本, 步骤)``。

    优先级：真实三围 > 身高体重估算（明确标注仅供参考）> 反问要数据。
    档案没写平铺尺寸时不做任何推断。``steps`` 里的 ``kind="fit"``
    由调用方并入轨迹，页面显示为「判断合身」。
    """
    if not profile:
        return "", None

    fit_question = is_fit_question(question)
    wants_fit = _wants_fit(question)
    wants_length = _wants_length(question)
    parts: list[str] = []
    status = ""
    summary = ""

    real = any(_user_cm(facts, part) is not None for part in PART_LABELS)
    height = _user_cm(facts, "height")
    weight = _user_cm(facts, "weight")
    measurements = profile.get("measurements") or []
    small = _is_small_item(profile)
    has_body = _has_body_part(profile)

    if not measurements:
        if real or height is not None or fit_question:
            parts.append(
                "【按你的身材】这份档案没有写平铺尺寸，判断不了穿不穿得下，我不猜。"
            )
            status, summary = "no_archive_data", "档案没写尺寸，不判断"
    elif small and not has_body:
        # 发饰/包/袜/鞋：档案里有尺寸就照实报，但没有身体部位可比时不猜结论。
        if real or height is not None or fit_question:
            parts.append(_item_size_only_text(profile))
            status, summary = "no_archive_data", "档案只有物件尺寸，不判断"
    elif real:
        text, overall = _fit_section(profile, facts)
        if text:
            parts.append(text)
        if overall:
            status, summary = "measured", f"按三围：{overall}"
        else:
            # 档案有尺寸，但不是胸/腰/臀：没有可比对的部位，不算「按三围判断」。
            status, summary = "no_archive_data", "档案没写对应部位，不判断"
    elif wants_fit and height is not None and weight is not None:
        estimates = _estimate(planner, height, weight)
        if estimates:
            text, overall = _estimated_section(profile, estimates, height, weight)
            parts.append(text)
            if overall:
                status, summary = "estimated", f"按身高体重估算（仅供参考）：{overall}"
            else:
                status, summary = "no_archive_data", "档案没写对应部位，不判断"
        elif wants_fit:
            parts.append(GUIDE_TEXT)
            status, summary = "need_data", "缺三围，已询问"
    elif wants_fit:
        parts.append(GUIDE_TEXT)
        status, summary = "need_data", "缺三围，已询问"

    # 小物的 Length 是物件本身大小，不能当裙长按身高推落点。
    if height is not None and (wants_length or wants_fit) and not small:
        length = _length_section(profile, facts)
        if length:
            parts.append(length)
            if not status:
                status, summary = "length_only", "按身高判断裙长"

    color = _color_section(profile, facts)
    if color:
        parts.append(color)

    text = "\n".join(parts)
    if not text or not status:
        # 只有配色命中时不产出「判断合身」步骤：它不是一个尺码结论。
        return text, None
    step = {
        "kind": "fit",
        "label": "判断合身",
        "status": status,
        "summary": summary,
    }
    return text, step

