"""把「只写在图里的文字」变成一条独立证据。

## 触发（按需，不全量）

必须同时满足：

1. 问题是价格 / 尺码 / 预约这类**要具体数值**的问法；
2. 至少一条带图的小红书证据满足下列之一：
   * 正文有「标在图上 / 见图 / 价格表 / 尺码表 / 看图」这类线索；
   * 正文过短（≤40 字）且带图，价格表只能是图；
   * **兜底**：所有证据正文里都没有价格形态的数字（¥ / 元 / 「价格：数字」），
     说明答案很可能只在图里——此时候选放宽到全部带图帖子。

其余情况完全走原路径，不加延迟。

## 为什么是独立证据

OCR 文字不拼进原帖正文、也不覆盖它：单独一条 ``source_kind="image_text"``，
标题写明「OCR，未校验」，``url`` 指回原帖，另附图片 URL。混进正文等于把
不同来源并成一条，既放大归因错误，也让句级支撑校验分不清来源。

## 预算与降级

最多 2 条证据、每条最多 3 张图，整段 OCR 有总预算；超预算放弃剩余图片。
没装 OCR、超时、图过大、下载失败都只让这一条证据缺席，不影响整体回答。
"""

from __future__ import annotations

import logging
import re
import time
from typing import TYPE_CHECKING, NamedTuple

if TYPE_CHECKING:
    from app.agent.channels import RecallContext

logger = logging.getLogger(__name__)

# 需要具体数值的问法。刻意不收泛化的「怎么样 / 好不好」：那些问题看图也答不出数字。
NUMERIC_MARKERS = (
    "价格", "多少钱", "定金", "尾款", "预约价", "现货价", "全款", "售价", "贵不贵",
    "尺码", "码数", "尺寸", "胸围", "腰围", "肩宽", "袖长", "裙长", "衣长", "什么码",
    "预约", "抽选", "抽签", "补款",
)

# 正文明确把答案指向图片的线索。实测原帖写的是「对应的价格标图上」，
# 所以既要收「标在图上」，也要收「标图上」。
IMAGE_CLUES = (
    "标在图上", "标图上", "标在图里", "见图", "看图", "图里写", "图上写",
    "图上标", "价格表", "价目表", "尺码表", "参数表", "看图片", "配图",
)

SHORT_TEXT_LIMIT = 40
# 单条图上文字证据的上限。一次最多 3 张图，900 字足够放下价格表的主要行，
# 又不至于把 prompt 撑大——这条证据只在少数问题里出现。
IMAGE_TEXT_LIMIT = 900

# 价格形态的数字：¥/￥ 前缀、金额单位、或「价格/定金/尾款：数字」。
# 只认这些，不把日期、尺码、楼层号当成价格——否则兜底永远不触发。
_PRICE_NUMBER_RE = re.compile(
    r"[¥￥]\s*\d"
    r"|(?:价格|售价|定价|定金|尾款|预约价|现货价|全款|原价|现价)\s*[:：]?\s*\d{2,5}"
    r"|\d{2,5}\s*元"
)


def numeric_intent(question: str) -> bool:
    text = question or ""
    return any(marker in text for marker in NUMERIC_MARKERS)


def _has_images(item: dict) -> bool:
    if item.get("channel") != "xhs":
        return False
    urls = [str(url) for url in (item.get("image_urls") or []) if str(url).strip()]
    return int(item.get("image_count") or 0) > 0 and bool(urls)


def _has_clue(item: dict) -> bool:
    text = str(item.get("text") or "")
    if any(clue in text for clue in IMAGE_CLUES):
        return True
    return len(text.strip()) <= SHORT_TEXT_LIMIT


def has_price_number(evidence: list[dict]) -> bool:
    """所有证据正文里是否已经出现价格形态的数字。"""
    return any(
        _PRICE_NUMBER_RE.search(str(item.get("text") or "")) for item in evidence
    )


def _rank(item: dict) -> int:
    """同分候选里优先标题就写着价格表/尺码表的帖子。"""
    title = str(item.get("title") or "")
    if any(key in title for key in ("价格表", "价目表", "尺码表", "参数表")):
        return 2
    if any(key in title for key in ("价格", "尺码", "定金", "尾款", "预约")):
        return 1
    return 0


# lolita 价格口径：预约价 = 定金 + 尾款，是同一件裙子的两个付款阶段；
# 现货价是另一回事。四列表的含义固定为 定金 / 尾款 / 预约价 / 现货价。
PRICE_COLUMNS = ("定金", "尾款", "预约价", "现货价")
# 表头别名：图上可能写「预约」「现货」而不是全称。
_HEADER_TOKEN_RE = re.compile(r"定金|尾款|预约价|预约|现货价|现货")
_ROW_NUMBER_RE = re.compile(r"(?<!\d)\d{2,5}(?!\d)")
# 手写价目表会有几块钱的取整/抹零，超过这个差就不认列。
PRICE_SUM_TOLERANCE = 2
# 用户明确问全表时才展开所有档位；默认只答主档位（本体/长款）。
_FULL_TABLE_MARKERS = (
    "全表", "所有", "全部", "各款", "每个", "各个", "整张表", "价目表全", "各型号",
)
# 判断「用户问的是哪一档」用的词。命中就把该档位当作回答范围，
# 否则只给主档位，避免把边夹、裙撑的数字一起倒出来。
_TIER_KEYS = ("长款", "短款", "围裙", "边夹", "发带", "裙撑", "本体", "OP", "JSK", "SK")


class PriceRow(NamedTuple):
    label: str
    deposit: int
    balance: int
    reserve: int
    spot: int
    basis: str
    validated: bool


def _header_columns(line: str) -> list[str] | None:
    """从表头行取出列名顺序；四个列名齐了才算表头。"""
    names: list[str] = []
    for token in _HEADER_TOKEN_RE.findall(line):
        name = "预约价" if token == "预约" else ("现货价" if token == "现货" else token)
        if name not in names:
            names.append(name)
    if len(names) == 4 and set(names) == set(PRICE_COLUMNS):
        return names
    return None


def _row_numbers(line: str) -> list[int]:
    return [int(token) for token in _ROW_NUMBER_RE.findall(line)]


def _row_label(line: str) -> str:
    """行首的型号/款式名：去掉行尾四个价格数字。"""
    return re.sub(r"(?:\s*\d{2,5}){4}\s*$", "", line).strip(" ：:")


def parse_price_rows(text: str) -> list[PriceRow]:
    """把 OCR 行解析成价格行；表头给列名，算术校验决定能不能这样断言。

    ``validated`` 是唯一开关：只有「定金 + 尾款 ≈ 预约价」成立时，才允许
    按列名整理成自然语言。对不上就只保留原行，不推断列归属。
    """
    lines = text.splitlines()
    header: list[str] | None = None
    for line in lines:
        header = _header_columns(line)
        if header:
            break

    rows: list[PriceRow] = []
    for line in lines:
        numbers = _row_numbers(line)
        if len(numbers) != 4 or _header_columns(line):
            continue
        if header:
            mapping = dict(zip(header, numbers, strict=True))
            basis = "表头"
        else:
            mapping = dict(zip(PRICE_COLUMNS, numbers, strict=True))
            basis = "算术校验"
        deposit = mapping["定金"]
        balance = mapping["尾款"]
        reserve = mapping["预约价"]
        rows.append(
            PriceRow(
                label=_row_label(line),
                deposit=deposit,
                balance=balance,
                reserve=reserve,
                spot=mapping["现货价"],
                basis=basis,
                validated=abs(deposit + balance - reserve) <= PRICE_SUM_TOLERANCE,
            )
        )
    return rows


def price_sentence(row: PriceRow) -> str:
    """整理后的价格表述：预约价由定金 + 尾款组成，现货价另说。"""
    sentence = (
        f"预约价 {row.reserve} 元，由定金 {row.deposit} 元"
        f"加尾款 {row.balance} 元组成；现货价 {row.spot} 元。"
    )
    return f"{row.label}：{sentence}" if row.label else sentence


def _primary(rows: list[PriceRow]) -> PriceRow:
    """主档位：优先「本体」，其次「长款」，再退到第一行。"""

    def rank(row: PriceRow) -> int:
        if "本体" in row.label:
            return 3
        if "长款" in row.label:
            return 2
        return 1

    return max(rows, key=rank)


def _mentioned(question: str, rows: list[PriceRow]) -> list[PriceRow]:
    text = (question or "").upper()
    return [
        row
        for row in rows
        if row.label and any(key.upper() in text and key.upper() in row.label.upper() for key in _TIER_KEYS)
    ]


def format_price_text(question: str, text: str) -> str:
    """把能校验的价格行整理成中文句；校验不了就原样返回。

    * 有「⟦自然语言⟧」整理句时不再堆裸数字，也不再列所有 SKU；
    * 用户没明确问全表，只给主档位（或用户点名的那一档）；
    * 一行都校验不了时退回原行，交给提示词要求「对照原图」。
    """
    rows = parse_price_rows(text)
    validated = [row for row in rows if row.validated]
    if not validated:
        return text

    mentioned = _mentioned(question, rows)
    if mentioned:
        picked = [row for row in mentioned if row.validated]
        if not picked:
            return text
    else:
        picked = [_primary(validated)]

    lines = [price_sentence(row) for row in picked]
    rest = [row for row in validated if row not in picked]
    if rest and any(marker in (question or "") for marker in _FULL_TABLE_MARKERS):
        lines.extend(price_sentence(row) for row in rest)
    elif rest:
        labels = list(dict.fromkeys(row.label for row in rest if row.label))
        if labels:
            lines.append(f"其余档位（{'、'.join(labels[:4])}）未展开；未校验，需对照原图。")
    return "\n".join(lines)


# 证据里的整理句，写作层后处理用它判断等式是否成立、并据此替换错句。
_ORGANIZED_RE = re.compile(
    r"^(?:(?P<label>[^：\n]{1,40})：)?"
    r"预约价 (?P<reserve>\d{2,5}) 元，由定金 (?P<deposit>\d{2,5}) 元"
    r"加尾款 (?P<balance>\d{2,5}) 元组成；现货价 (?P<spot>\d{2,5}) 元。"
)


def organized_rows(text: str) -> list[dict]:
    """解析证据文本里已经整理好的价格句（给写作层 demote 用）。"""
    rows: list[dict] = []
    for raw in str(text or "").splitlines():
        line = re.sub(r"^图\s*\d+\s*[：:]\s*", "", raw.strip())
        match = _ORGANIZED_RE.match(line)
        if not match:
            continue
        rows.append(
            {
                "line": line,
                "label": (match.group("label") or "").strip(),
                "deposit": int(match.group("deposit")),
                "balance": int(match.group("balance")),
                "reserve": int(match.group("reserve")),
                "spot": int(match.group("spot")),
            }
        )
    return rows


def _build(item: dict, urls: list[str], parts: list[str]) -> dict:
    header = (
        "（OCR，未校验：价格数字来自原图识别，可能有误，以原图为准。"
        "「预约价 X 元，由定金 A 元加尾款 B 元组成；现货价 Y 元」这种整理句，"
        "只在定金 + 尾款 ≈ 预约价时才会出现；没有整理句的原始行不保证列对齐，"
        "只能原样引用，不得推断列名）"
    )
    text = header + "\n" + "\n".join(parts)
    if len(text) > IMAGE_TEXT_LIMIT:
        text = text[:IMAGE_TEXT_LIMIT] + "…"
    title = f"{item.get('title') or '原帖'}｜图上文字（OCR，未校验）"
    post_url = str(item.get("url") or "")
    return {
        "channel": "image_text",
        "source_kind": "image_text",
        # 原帖 URL 会被普通证据占用，去重键靠 ref 与 graph._evidence_key 区分。
        "ref": f"{post_url}#image_text",
        "title": title,
        "text": text,
        "url": post_url,
        # 图 URL 不进写作提示词，只给前端核对原图用。
        "image_url": urls[0],
        "image_urls": urls,
    }


def collect(question: str, evidence: list[dict], ctx: RecallContext) -> list[dict]:
    """按需识别配图文字，返回独立的 image_text 证据（可能为空）。"""
    app = ctx.settings
    if not numeric_intent(question):
        return []
    if not app.ocr_enabled:
        return []

    with_images = [item for item in evidence if _has_images(item)]
    if not with_images:
        return []

    candidates = [item for item in with_images if _has_clue(item)]
    if not candidates:
        # 兜底：正文既没有线索、也没给出任何价格数字时，答案很可能只在图里。
        # 反过来，别的证据已经写出 ¥578 时就不该再花预算去识别。
        if has_price_number(evidence):
            return []
        candidates = with_images

    from app.services import ocr

    ready, reason = ocr.readiness(app)
    if not ready:
        logger.info("图片文字识别跳过：%s", reason)
        return []

    candidates.sort(key=_rank, reverse=True)
    max_items = max(0, int(app.ocr_max_items))
    per_item = max(0, int(app.ocr_images_per_item))
    budget = float(app.ocr_total_budget or 0)
    deadline = time.monotonic() + budget if budget > 0 else None

    results: list[dict] = []
    for item in candidates[:max_items]:
        if deadline is not None and time.monotonic() >= deadline:
            logger.info("图片文字识别总预算 %.1fs 用尽，放弃剩余图片", budget)
            break
        used: list[str] = []
        parts: list[str] = []
        for url in [str(url) for url in (item.get("image_urls") or [])][:per_item]:
            if deadline is not None and time.monotonic() >= deadline:
                break
            text = ocr.text_from_url(url, app)
            if not text or not text.strip():
                continue
            used.append(url)
            parts.append(f"图 {len(used)}：{format_price_text(question, text.strip())}")
        if parts:
            results.append(_build(item, used, parts))
    return results
