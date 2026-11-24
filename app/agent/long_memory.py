"""长期记忆：显式偏好 / 尺码 / 品牌这类稳定事实。

与 ``thread_messages`` 的短期窗口**分开存**：短期窗口只带最近几轮问答，
会被保留条数挤掉；这里的稳定事实按账号单独存一行，跨对话可用，直到被用户
删除或一年没再被确认才过期。

为什么只用规则、不调模型：

* 写入必须「用户明确说过」——偏好要第一人称 + 偏好词，尺码要尺码语境，
  品牌要第一人称 + 购买/偏好词；疑问句一律不写。模型抽取很容易把
  「AP 最近有什么上新」这类提问写成「用户偏好 AP」，一旦写进长期记忆，
  以后每次回答都会带着它，错得比短期窗口更久。
* 成本：每条消息过一遍抽取模型，等于给每轮对话加一次调用，不划算。

读取时渲染成一条带 :data:`MARKER` 的用户消息，由调用方与压缩后的短期历史
一起交给 planner / writer；两种来源在提示词里可区分，不混成一段。
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from app import db
from app.agent import dress
from app.agent.fit import COLOR_ALIASES
from app.agent.history_memory import find_size
from app.config import AppSettings

logger = logging.getLogger(__name__)

#: 长期记忆消息的起始标记；与短期历史的压缩标记区分开。
MARKER = "【长期记忆（稳定信息，来自本人此前明确说过的话，不是新问题）】"

#: 每类最多渲染几条、单条事实最长多少字。稳定信息只用于理解偏好，
#: 不该挤占证据与问题本身的篇幅。
MAX_PER_KIND = 3
MAX_FACT_CHARS = 40

KIND_LABELS = {
    "bust": "胸围",
    "waist": "腰围",
    "hips": "臀围",
    "height": "身高",
    "weight": "体重",
    "size": "尺码",
    "color": "喜欢的颜色",
    "brand": "常买品牌",
    "preference": "偏好",
}

#: 渲染顺序：身材数据在前（查裙子时要拿来比），软偏好放最后。
KIND_ORDER = (
    "bust",
    "waist",
    "hips",
    "height",
    "weight",
    "size",
    "color",
    "brand",
    "preference",
)

_FIRST_PERSON = ("我", "本人", "自己")
#: 「我朋友胸围…」里的数字不是本人的，不写进本人档案。
_OTHER_PERSON = ("朋友", "闺蜜", "同事", "她", "他", "妈妈", "姐姐", "妹妹")
_PREFERENCE_MARKERS = (
    "喜欢",
    "不喜欢",
    "偏好",
    "最爱",
    "种草",
    "只想要",
    "只要",
    "不穿",
    "不要",
)
_BRAND_MARKERS = ("喜欢", "最爱", "常买", "只买", "只穿", "常穿", "专买", "粉", "偏好", "买")
_QUESTION_MARKERS = (
    "吗",
    "呢",
    "？",
    "?",
    "有没有",
    "是不是",
    "多少",
    "怎么",
    "什么",
    "哪款",
    "哪些",
    "能不能",
)
_CLAUSE_SPLIT = re.compile(r"[，。；;,.!?！？\n]")
_SENTENCE_SPLIT = re.compile(r"[。；;!?！？\n]")

#: 三围/身高/体重只认显式陈述。单位可以省略（「腰围90」按 cm），
#: 但数字必须紧跟部位词并且落在合理范围，避免把裙子的尺寸记成人的。
_BODY_MARKERS = {"bust": ("胸围",), "waist": ("腰围",), "hips": ("臀围",)}
_BODY_VALUE = re.compile(
    r"^\s*[:：]?\s*(?:是|为|约|大约|大概)?\s*(\d{2,3}(?:\.\d+)?)\s*"
    r"(cm|厘米|公分)?\s*(?:左右)?\s*$",
    re.IGNORECASE,
)
_HEIGHT_VALUE = re.compile(
    r"身高\s*[:：]?\s*(?:是|为|约|大约|大概)?\s*(\d{2,3}(?:\.\d+)?)\s*"
    r"(cm|厘米|公分)?",
    re.IGNORECASE,
)
_WEIGHT_VALUE = re.compile(
    r"体重\s*[:：]?\s*(?:是|为|约|大约|大概)?\s*(\d{2,3}(?:\.\d+)?)\s*"
    r"(kg|公斤|千克|斤)?",
    re.IGNORECASE,
)
_THREE_SIZES = re.compile(
    r"三围\s*[:：]?\s*(\d{2,3}(?:\.\d+)?)\s*[/、,，]\s*(\d{2,3}(?:\.\d+)?)"
    r"\s*[/、,，]\s*(\d{2,3}(?:\.\d+)?)"
)

#: 只有「身高160，腰围90」这种纯身体数据句，才允许省略第一人称。
#: 句子里出现裙子/款式等词时不能当本人数据。
_MEASURE_TOKENS = (
    "身高",
    "体重",
    "胸围",
    "腰围",
    "臀围",
    "三围",
    "厘米",
    "公分",
    "公斤",
    "千克",
    "我",
    "的",
    "是",
    "为",
    "约",
    "大约",
    "大概",
    "左右",
    "cm",
    "CM",
    "kg",
    "KG",
    "，",
    ",",
    "、",
    "：",
    ":",
    " ",
    "/",
)

#: 颜色只认「我 + 喜欢/偏爱… + 颜色词」；否定句与疑问句都不写。
_COLOR_PREFERENCE = ("喜欢", "偏爱", "最爱", "爱", "控")
_COLOR_NEGATIVE = ("不喜欢", "不爱", "讨厌", "不要", "不想要", "无感", "忌")


def _alias_pattern(alias: str) -> re.Pattern[str] | None:
    alias = alias.strip()
    if not alias:
        return None
    escaped = re.escape(alias)
    if re.search(r"[A-Za-z]", alias):
        return re.compile(rf"(?<![A-Za-z]){escaped}(?![A-Za-z])", re.IGNORECASE)
    return re.compile(escaped)


def _brand_patterns(app: AppSettings) -> list[tuple[re.Pattern[str], str]]:
    patterns: list[tuple[re.Pattern[str], str]] = []
    for brand in dress.load_brands(app):
        for alias in (brand.name, *brand.aliases):
            pattern = _alias_pattern(alias)
            if pattern is not None:
                patterns.append((pattern, brand.name))
    return patterns


def _is_question(clause: str) -> bool:
    return any(marker in clause for marker in _QUESTION_MARKERS)


def _cm_text(value: str) -> str:
    if "." in value:
        value = value.rstrip("0").rstrip(".")
    return f"{value}cm"


def _weight_text(value: float) -> str:
    text = str(int(value)) if value == int(value) else f"{value:g}"
    return f"{text}kg"


def _plausible(value: str, low: float, high: float) -> bool:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    return low <= number <= high


def _measurement_only(sentence: str) -> bool:
    """整句只有身体数据（可省略「我」）：「身高160，腰围90」。"""
    rest = sentence
    for token in _MEASURE_TOKENS:
        rest = rest.replace(token, "")
    rest = re.sub(r"[\d.]", "", rest)
    return not rest.strip()


_BODY_ONLY_PUNCT = re.compile(r"[\s,，。.、；;：:！!？?~～/]+")


def is_body_data(text: str) -> bool:
    """整条消息只有身体数据，没有别的内容。

    「我身高160cm，体重150斤」「身高160，腰围90」为 True；只要还夹着裙名、
    问法或其他实词（「这件穿得下吗」）就是 False。调用方用它把「报数据」和
    「报裙名」分开：数据不是裙子问题，不该被当成裸裙名去搜。
    """
    raw = str(text or "").strip()
    if not raw:
        return False
    rest = raw
    for token in (*_MEASURE_TOKENS, "斤", "本人", "自己"):
        rest = rest.replace(token, "")
    rest = _BODY_ONLY_PUNCT.sub("", rest)
    rest = re.sub(r"[\d.]", "", rest)
    return not rest.strip()


def _body_facts(clause: str) -> list[tuple[str, str]]:
    """从一句话里取三围：优先「三围 85/70/90」，其次逐部位「胸围 85cm」。

    单位可省略，但数字必须紧跟部位词且落在 40–150cm；「我胸围90的裙子」
    这种描述裙子尺寸的句子不会被当成本人数据。
    """
    triple = _THREE_SIZES.search(clause)
    if triple:
        values = [triple.group(1), triple.group(2), triple.group(3)]
        if all(_plausible(value, 40, 150) for value in values):
            return [
                ("bust", _cm_text(values[0])),
                ("waist", _cm_text(values[1])),
                ("hips", _cm_text(values[2])),
            ]

    found: list[tuple[str, str]] = []
    for kind, markers in _BODY_MARKERS.items():
        for marker in markers:
            index = clause.find(marker)
            if index < 0:
                continue
            match = _BODY_VALUE.search(clause[index + len(marker):])
            if match and _plausible(match.group(1), 40, 150):
                found.append((kind, _cm_text(match.group(1))))
            break
    return found


def _height_weight_facts(clause: str) -> list[tuple[str, str]]:
    """身高按 cm、体重按 kg；「斤」折半。范围明显不对的不记。"""
    found: list[tuple[str, str]] = []
    height = _HEIGHT_VALUE.search(clause)
    if height and _plausible(height.group(1), 120, 220):
        found.append(("height", _cm_text(height.group(1))))
    weight = _WEIGHT_VALUE.search(clause)
    if weight:
        try:
            value = float(weight.group(1))
        except ValueError:
            value = 0.0
        if (weight.group(2) or "").strip() == "斤":
            value /= 2
        if 30 <= value <= 150:
            found.append(("weight", _weight_text(value)))
    return found


def _liked_color(clause: str) -> str:
    """「我 + 喜欢 + 颜色词」才算；否定与疑问已在调用处排除。"""
    if any(marker in clause for marker in _COLOR_NEGATIVE):
        return ""
    if not any(marker in clause for marker in _COLOR_PREFERENCE):
        return ""
    for name in sorted(COLOR_ALIASES, key=len, reverse=True):
        if name in clause:
            return name
    return ""


def _mentions_color(clause: str) -> bool:
    return any(name in clause for name in COLOR_ALIASES)


def extract(text: str, app: AppSettings) -> list[dict]:
    """从一条用户消息里抽稳定事实；只认显式陈述，疑问句不写。

    返回 ``[{"kind": ..., "value": ..., "source": ...}]``，同一条消息内
    同 kind + value 只出现一次。
    """
    brand_patterns = _brand_patterns(app)
    facts: list[dict] = []
    seen: set[tuple[str, str]] = set()

    def add(kind: str, value: str, source: str) -> None:
        value = value.strip()
        key = (kind, value)
        if not value or key in seen:
            return
        seen.add(key)
        facts.append(
            {
                "kind": kind,
                "value": value[:MAX_FACT_CHARS],
                "source": source[:MAX_FACT_CHARS],
            }
        )

    # 「我胸围85cm，腰围70cm」里第一人称只出现在第一个分句；先按句切，
    # 让整句共享同一个「我」，再按逗号分句判断各自的事实。
    #
    # 整句带问号不代表整句是疑问：「我身高160，腰围90，这件穿得下吗？」
    # 前两个分句是陈述。疑问只按分句排除（下面那层），否则最常见的
    # 「报数据 + 问能不能穿」句式会把数据整句丢掉。
    for sentence in _SENTENCE_SPLIT.split(str(text or "")):
        if not sentence.strip():
            continue
        first_person = any(marker in sentence for marker in _FIRST_PERSON)
        other_person = any(marker in sentence for marker in _OTHER_PERSON)
        self_report = first_person or _measurement_only(sentence)

        for clause in (part.strip() for part in _CLAUSE_SPLIT.split(sentence)):
            if not clause or _is_question(clause):
                continue

            size = find_size(clause)
            if size:
                add("size", size, clause)

            if not self_report:
                continue

            # 三围/身高/体重与颜色必须是「本人」的：朋友/家人的数字不写进本人档案。
            if not other_person:
                for kind, value in _body_facts(clause) + _height_weight_facts(clause):
                    add(kind, value, clause)

            liked_color = "" if other_person else _liked_color(clause)
            if liked_color:
                add("color", liked_color, clause)

            if any(marker in clause for marker in _BRAND_MARKERS):
                for pattern, canonical in brand_patterns:
                    if pattern.search(clause):
                        add("brand", canonical, clause)

            # 提到颜色的那句不重复写泛化偏好：颜色要么已记成「喜欢的颜色」，
            # 要么是「不喜欢/讨厌」，后者不写（更不该在查裙子时当喜欢来提醒）。
            if not _mentions_color(clause) and any(
                marker in clause for marker in _PREFERENCE_MARKERS
            ):
                add("preference", clause, clause)

    return facts


def render(facts: list[dict]) -> dict | None:
    """把事实渲染成一条用户消息；没有事实返回 None。"""
    if not facts:
        return None
    lines = [MARKER]
    for kind in KIND_ORDER:
        values: list[str] = []
        for fact in facts:
            if fact.get("kind") != kind:
                continue
            value = str(fact.get("value") or "").strip()
            if value and value not in values:
                values.append(value)
            if len(values) >= MAX_PER_KIND:
                break
        if values:
            lines.append(f"- {KIND_LABELS[kind]}：" + "；".join(values))
    return {"role": "user", "content": "\n".join(lines)} if len(lines) > 1 else None


def facts_for(
    db_path: Path,
    account_id: int | None,
    *,
    max_age_days: float = db.MEMORY_MAX_AGE_DAYS,
) -> list[dict]:
    """读取某账号未过期的稳定事实；读失败只当没有记忆。

    与 :func:`block_for` 共用同一条读取路径：渲染成提示词和拿来比对尺寸
    必须是同一批事实，不能一处过期一处没过期。
    """
    if not account_id:
        return []
    try:
        return db.list_memories(db_path, account_id, max_age_days=max_age_days)
    except Exception as exc:  # noqa: BLE001 - 记忆读不到不能影响本轮回答
        logger.warning("长期记忆读取失败（不影响回答）：%s", exc)
        return []


def block_for(
    db_path: Path,
    account_id: int | None,
    *,
    max_age_days: float = db.MEMORY_MAX_AGE_DAYS,
) -> dict | None:
    """读取某账号未过期的稳定事实并渲染；没有记忆返回 None。"""
    return render(facts_for(db_path, account_id, max_age_days=max_age_days))


def remember(
    db_path: Path, account_id: int | None, text: str, app: AppSettings
) -> list[dict]:
    """抽取并写入一条用户消息里的稳定事实；写失败只记日志。"""
    if not account_id:
        return []
    try:
        facts = extract(text, app)
    except Exception as exc:  # noqa: BLE001 - 抽取/读品牌表失败不能影响回答
        logger.warning("长期记忆抽取失败（不影响回答）：%s", exc)
        return []
    for fact in facts:
        try:
            db.upsert_memory(
                db_path, account_id, fact["kind"], fact["value"], fact["source"]
            )
        except Exception as exc:  # noqa: BLE001 - 写失败不能影响回答
            logger.warning("长期记忆写入失败（不影响回答）：%s", exc)
    return facts
