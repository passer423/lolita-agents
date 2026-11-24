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
}

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
    """按部位取档案平铺尺寸，返回 ``(原始值, cm 区间)``；没写返回 ``("", None)``。"""
    titles = _PART_TITLES[part]
    for item in profile.get("measurements") or []:
        label = str(item.get("label") or "").strip()
        lowered = label.lower()
        if not any(title in lowered for title in titles):
            continue
        value = str(item.get("value") or "").strip()
        return value, _cm_range(value)
    return "", None


def _fit_section(profile: dict, facts: list[dict]) -> str:
    users = {part: _user_cm(facts, part) for part in PART_LABELS}
    if all(value is None for value in users.values()):
        return ""
    if not profile.get("measurements"):
        return "【按你的身材】这份档案没有写平铺尺寸，判断不了穿不穿得下，我不猜。"

    lines: list[str] = []
    verdicts: list[str] = []
    for part, part_label in PART_LABELS.items():
        user = users[part]
        if user is None:
            continue
        value, cm_range = _measurement(profile, part)
        if not value:
            lines.append(f"{part_label}：档案没写，这个部位不判断。")
            continue
        if cm_range is None:
            lines.append(f"{part_label}：档案写的是「{value}」，取不到 cm 数值，不判断。")
            continue

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
                f"档案 {_fmt(low)}~{_fmt(high)}cm，你 {user_text}cm 比下限小 "
                f"{_fmt(diff)}cm，在 {_fmt(LOOSE_MARGIN_CM)}cm 放松量内"
            )
        else:
            verdict = "合身"
            detail = f"档案 {_fmt(low)}~{_fmt(high)}cm，你 {user_text}cm 在范围内"
        verdicts.append(verdict)
        lines.append(f"{part_label}：{detail}，{verdict}（{value}）。")

    if "略小" in verdicts:
        overall = "整体略小、会偏紧"
    elif verdicts and all(verdict == "宽松" for verdict in verdicts):
        overall = "整体宽松"
    elif "宽松" in verdicts:
        overall = "整体合身偏宽松"
    else:
        overall = "整体合身"
    return "【按你的身材】" + "".join(lines) + f"{overall}。"


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


def advice(profile: dict, facts: list[dict]) -> str:
    """基于档案与长期记忆生成补充结论；没有可用信息时返回空串。

    只用于已解析出裙子档案的回答：调用方应跳过未核实/未解析的情况，
    避免把「猜的档案」当事实来比尺寸。
    """
    if not profile or not facts:
        return ""
    sections = [
        section
        for section in (_fit_section(profile, facts), _color_section(profile, facts))
        if section
    ]
    return "\n".join(sections)
