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
    "size": "尺码",
    "color": "喜欢的颜色",
    "brand": "常买品牌",
    "preference": "偏好",
}

#: 渲染顺序：身材数据在前（查裙子时要拿来比），软偏好放最后。
KIND_ORDER = ("bust", "waist", "hips", "size", "color", "brand", "preference")

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

#: 三围只认显式带 cm/厘米/公分的陈述；没有单位的数字（「我穿170」）不当围度。
_BODY_MARKERS = {"bust": ("胸围",), "waist": ("腰围",), "hips": ("臀围",)}
_CM_VALUE = re.compile(r"(\d{2,3}(?:\.\d+)?)\s*(?:cm|厘米|公分)", re.IGNORECASE)
_THREE_SIZES = re.compile(
    r"三围\s*[:：]?\s*(\d{2,3}(?:\.\d+)?)\s*[/、,，]\s*(\d{2,3}(?:\.\d+)?)"
    r"\s*[/、,，]\s*(\d{2,3}(?:\.\d+)?)"
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


def _body_facts(clause: str) -> list[tuple[str, str]]:
    """从一句话里取三围：优先「三围 85/70/90」，其次逐部位「胸围 85cm」。"""
    triple = _THREE_SIZES.search(clause)
    if triple:
        return [
            ("bust", _cm_text(triple.group(1))),
            ("waist", _cm_text(triple.group(2))),
            ("hips", _cm_text(triple.group(3))),
        ]
    found: list[tuple[str, str]] = []
    for kind, markers in _BODY_MARKERS.items():
        for marker in markers:
            index = clause.find(marker)
            if index < 0:
                continue
            match = _CM_VALUE.search(clause[index: index + 12])
            if match:
                found.append((kind, _cm_text(match.group(1))))
            break
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
    for sentence in _SENTENCE_SPLIT.split(str(text or "")):
        if not sentence.strip() or _is_question(sentence):
            continue
        first_person = any(marker in sentence for marker in _FIRST_PERSON)
        other_person = any(marker in sentence for marker in _OTHER_PERSON)

        for clause in (part.strip() for part in _CLAUSE_SPLIT.split(sentence)):
            if not clause or _is_question(clause):
                continue

            size = find_size(clause)
            if size:
                add("size", size, clause)

            if not first_person:
                continue

            # 三围与颜色必须是「本人」的：朋友/家人的数字不写进本人档案。
            if not other_person:
                for kind, value in _body_facts(clause):
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
