"""多轮历史的压缩要点：把原文换成可喂检索/写作的结构化摘要。

为什么不用模型压缩：每条消息都要过一遍压缩模型，等于把省下的上下文又花回去，
还会引入「把没出现过的品牌/价格写成要点」的风险。这里只做**可解释的抽取**：

* 品牌 / 系列：命中 ``content/brands.json`` 与 ``content/dress/aliases.json``；
* 货号 / 尺码：严格正则，要求有「货号」「码」这类语境；
* 偏好 / 约束：只截取包含标记词的短句；
* 指代：只绑定到此前真实出现过的实体，没有前文就不给映射；
* 最近 1 轮原文：按需保留，用于还原用户原话。

抽不到任何字段、历史为空、或压缩后并不比原文短时返回 ``None``，
调用方原样使用 raw history——这就是「压缩失败退回现有行为」。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.agent import dress
from app.config import AppSettings

#: 压缩消息的起始标记；planner 据此切换提示词标签，不把它当成用户原话。
MARKER = "【对话历史要点（压缩，仅供理解指代，不是新问题）】"

#: 适配 planner（单条 400 字）与 writer（单条 500 字）的截断上限。
MAX_CHARS = 380
MAX_RECENT_TURNS = 1
RECENT_TURN_CHARS = 80
MESSAGE_MAX_CHARS = 500

_PRONOUNS = ("该系列", "那条", "这条", "那款", "这款", "那件", "这件", "它")
_PREFERENCE_MARKERS = ("喜欢", "想要", "希望", "偏好", "不要", "不喜欢", "只想要", "种草")
_CONSTRAINT_MARKERS = (
    "预算",
    "以内",
    "不超过",
    "必须",
    "只能",
    "只接受",
    "现货",
    "预约",
    "包邮",
    "定金",
    "尾款",
)
_CLAUSE_SPLIT = re.compile(r"[，。；;,.!?！？\n]")
_CITATION_RE = re.compile(r"\[\d+\]")
#: 带这些词的分句是在提问，不是「用户提出的约束」。
_QUESTION_MARKERS = ("吗", "呢", "？", "?", "有没有", "是不是", "多少")
_SKU_RE = re.compile(r"\b(?=[A-Z0-9-]*\d{3})[A-Z][A-Z0-9-]{3,}\b")
_SKU_LABEL_RE = re.compile(r"货号\s*[:：]?\s*([A-Za-z0-9-]{4,})")
_SIZE_RE = re.compile(
    r"(?:尺码|码数|尺寸)\s*[:：]?\s*(XXL|XL|LL|L|M|S|均码|free)\b|"
    r"穿\s*(XXL|XL|LL|L|M|S|均码)\b|"
    r"(XXL|XL|LL|L|M|S)\s*码|"
    r"(\d{1,2})\s*码",
    re.IGNORECASE,
)


@dataclass
class Digest:
    brands: list[tuple[str, int]] = field(default_factory=list)
    series: list[tuple[str, int]] = field(default_factory=list)
    skus: list[tuple[str, int]] = field(default_factory=list)
    sizes: list[tuple[str, int]] = field(default_factory=list)
    preferences: list[tuple[str, int]] = field(default_factory=list)
    constraints: list[tuple[str, int]] = field(default_factory=list)
    references: list[tuple[str, str, int]] = field(default_factory=list)
    recent: list[tuple[str, str]] = field(default_factory=list)

    def has_points(self) -> bool:
        return bool(
            self.brands
            or self.series
            or self.skus
            or self.sizes
            or self.preferences
            or self.constraints
            or self.references
        )

    def render(self) -> str:
        """生成一条压缩消息；超长时按优先级丢字段，系列与指代最后丢。"""
        for drop_recent in (False, True):
            for drop_brands in (False, True):
                for drop_prefs in (False, True):
                    for drop_constraints in (False, True):
                        for drop_skus in (False, True):
                            for drop_sizes in (False, True):
                                text = self._render_once(
                                    drop_recent=drop_recent,
                                    drop_brands=drop_brands,
                                    drop_prefs=drop_prefs,
                                    drop_constraints=drop_constraints,
                                    drop_skus=drop_skus,
                                    drop_sizes=drop_sizes,
                                )
                                if len(text) <= MAX_CHARS:
                                    return text
        return self._render_once(
            drop_recent=True,
            drop_brands=True,
            drop_prefs=True,
            drop_constraints=True,
            drop_skus=True,
            drop_sizes=True,
        )[:MAX_CHARS]

    def _render_once(
        self,
        *,
        drop_recent: bool,
        drop_brands: bool,
        drop_prefs: bool,
        drop_constraints: bool,
        drop_skus: bool,
        drop_sizes: bool,
    ) -> str:
        lines = [MARKER]
        facts: list[str] = []
        if self.brands and not drop_brands:
            facts.append("品牌：" + "；".join(name for name, _ in self.brands[:3]))
        if self.series:
            facts.append(
                "系列："
                + "；".join(f"{name}（第{turn}轮）" for name, turn in self.series[:3])
            )
        if self.skus and not drop_skus:
            facts.append("货号：" + "；".join(name for name, _ in self.skus[:2]))
        if self.sizes and not drop_sizes:
            facts.append("尺码：" + "；".join(name for name, _ in self.sizes[:1]))
        if facts:
            lines.append("- " + "；".join(facts))
        if self.preferences and not drop_prefs:
            lines.append(
                "- 偏好：" + "；".join(text for text, _ in self.preferences[:2])
            )
        if self.constraints and not drop_constraints:
            lines.append("- 约束：" + "；".join(text for text, _ in self.constraints[:2]))
        if self.references:
            lines.append(
                "- 指代："
                + "；".join(
                    f"「{pronoun}」→ {name}" for pronoun, name, _ in self.references[:3]
                )
            )
        if self.recent and not drop_recent:
            lines.append("- 最近原文：" + "；".join(
                f"{role}：{text}" for role, text in self.recent
            ))
        return "\n".join(lines)


def _clean(history: list[dict] | None) -> list[dict]:
    cleaned: list[dict] = []
    for turn in history or []:
        if not isinstance(turn, dict):
            continue
        role = turn.get("role")
        content = _CITATION_RE.sub("", str(turn.get("content") or "")).strip()
        if role not in ("user", "assistant") or not content:
            continue
        cleaned.append({"role": role, "content": content[:MESSAGE_MAX_CHARS]})
    return cleaned


def _turn_numbers(messages: list[dict]) -> list[int]:
    number = 0
    numbers: list[int] = []
    for message in messages:
        if message["role"] == "user":
            number += 1
        numbers.append(number)
    return numbers


def _alias_pattern(alias: str) -> re.Pattern[str] | None:
    alias = alias.strip()
    if not alias:
        return None
    escaped = re.escape(alias)
    if re.search(r"[A-Za-z]", alias):
        return re.compile(rf"(?<![A-Za-z]){escaped}(?![A-Za-z])", re.IGNORECASE)
    return re.compile(escaped)


def _scan_aliases(
    messages: list[dict],
    numbers: list[int],
    patterns: list[tuple[re.Pattern[str], str]],
    limit: int,
) -> list[tuple[str, int]]:
    found: list[tuple[str, int]] = []
    seen: set[str] = set()
    for message, number in zip(messages, numbers, strict=False):
        text = message["content"]
        for pattern, canonical in patterns:
            if canonical in seen or not pattern.search(text):
                continue
            seen.add(canonical)
            found.append((canonical, number))
            if len(found) >= limit:
                return found
    return found


def _clauses(
    messages: list[dict],
    numbers: list[int],
    markers: tuple[str, ...],
    limit: int,
    *,
    skip_questions: bool = False,
):
    found: list[tuple[str, int]] = []
    seen: set[str] = set()
    for message, number in zip(messages, numbers, strict=False):
        if message["role"] != "user":
            continue
        for clause in _CLAUSE_SPLIT.split(message["content"]):
            clause = clause.strip()
            if not clause or not any(marker in clause for marker in markers):
                continue
            if skip_questions and any(
                marker in clause for marker in _QUESTION_MARKERS
            ):
                continue
            snippet = clause[:40]
            if snippet in seen:
                continue
            seen.add(snippet)
            found.append((snippet, number))
            if len(found) >= limit:
                return found
    return found


def _extract(history: list[dict], app: AppSettings) -> Digest:
    messages = _clean(history)
    numbers = _turn_numbers(messages)
    digest = Digest()

    brand_patterns: list[tuple[re.Pattern[str], str]] = []
    for brand in dress.load_brands(app):
        for alias in (brand.name, *brand.aliases):
            pattern = _alias_pattern(alias)
            if pattern is not None:
                brand_patterns.append((pattern, brand.name))
    digest.brands = _scan_aliases(messages, numbers, brand_patterns, limit=3)

    series_patterns: list[tuple[re.Pattern[str], str]] = []
    for alias in dress.load_series_aliases(app):
        canonical = alias.en or alias.cn
        for name in (alias.en, alias.cn):
            pattern = _alias_pattern(name)
            if pattern is not None and canonical:
                series_patterns.append((pattern, canonical))
    digest.series = _scan_aliases(messages, numbers, series_patterns, limit=3)

    for message, number in zip(messages, numbers, strict=False):
        text = message["content"]
        for label_match in _SKU_LABEL_RE.finditer(text):
            digest.skus.append((label_match.group(1), number))
            break
        for sku_match in _SKU_RE.finditer(text):
            sku = sku_match.group(0)
            if all(sku != existing for existing, _ in digest.skus):
                digest.skus.append((sku, number))
                break
        if len(digest.skus) >= 2:
            break

    for message, number in zip(messages, numbers, strict=False):
        size_match = _SIZE_RE.search(message["content"])
        if size_match:
            size = next(group for group in size_match.groups() if group)
            digest.sizes.append((str(size).upper(), number))
            break

    digest.preferences = _clauses(messages, numbers, _PREFERENCE_MARKERS, limit=2)
    digest.constraints = _clauses(
        messages, numbers, _CONSTRAINT_MARKERS, limit=2, skip_questions=True
    )

    # 指代只绑定「此前轮次」出现过的实体；同一轮里没有前文就不猜。
    # 同轮命中品牌和系列时选系列——「它」更常指那条裙子，而不是品牌。
    entities = [(name, turn, 1) for name, turn in digest.series]
    entities += [(name, turn, 0) for name, turn in digest.brands]
    for message, number in zip(messages, numbers, strict=False):
        if message["role"] != "user":
            continue
        for pronoun in _PRONOUNS:
            if pronoun not in message["content"]:
                continue
            prior = [entity for entity in entities if entity[1] < number]
            if not prior:
                continue
            antecedent = max(prior, key=lambda entity: (entity[1], entity[2]))[0]
            if antecedent and all(pronoun != existing for existing, _, _ in digest.references):
                digest.references.append((pronoun, antecedent, number))

    user_turns = [index for index, message in enumerate(messages) if message["role"] == "user"]
    for index in user_turns[-MAX_RECENT_TURNS:]:
        for follow in range(index, min(index + 2, len(messages))):
            message = messages[follow]
            role = "用户" if message["role"] == "user" else "助手"
            text = message["content"]
            if len(text) > RECENT_TURN_CHARS:
                text = text[:RECENT_TURN_CHARS] + "…"
            digest.recent.append((role, text))

    return digest


def compress(
    history: list[dict] | None, app: AppSettings
) -> list[dict] | None:
    """把历史压成一条要点消息；不值得压缩时返回 None（调用方用原文）。"""
    messages = _clean(history)
    if not messages:
        return None
    digest = _extract(messages, app)
    if not digest.has_points():
        return None
    text = digest.render()
    raw_chars = sum(len(message["content"]) for message in messages)
    if len(text) >= raw_chars:
        return None
    return [{"role": "user", "content": text}]
