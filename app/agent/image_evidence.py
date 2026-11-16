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
from typing import TYPE_CHECKING

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


def _build(item: dict, urls: list[str], parts: list[str]) -> dict:
    header = "（OCR，未校验：以下为图片文字的机器识别结果，可能有误，以原图为准）"
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
            parts.append(f"图 {len(used)}：{text.strip()}")
        if parts:
            results.append(_build(item, used, parts))
    return results
