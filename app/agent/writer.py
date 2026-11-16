"""把"决策"和"写作"拆开，顺便让答案可以真流式。

## 为什么要拆

原来 planner 在 ``{"action":"final","answer":"..."}`` 里把答案一起写在 JSON 里。
这有两个问题：

1. **没法流式**——用户要看到的是答案一个个字出现，而不是一个 JSON 字符串。
2. **贵**——planner 的 system prompt 里塞着四个工具的完整 JSON Schema（约 3.4k tokens），
   让它写答案等于每次都替这些 schema 付一次钱。

拆开之后：planner 只说"我调查完了"，写作单独一次调用，只带证据（约 0.6k tokens）。
写作的提示词也能写得更死——没有工具可用，它只能照着证据写。

## 兼容

planner 的 final 动作如果**自己带了答案**，就直接用（测试里的脚本化 planner 走这条路，
不必联网）。真实模型被要求不带答案，于是走流式写作。
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterator
from urllib.parse import urlsplit

from app.config import AppSettings

logger = logging.getLogger(__name__)

WRITER_TIMEOUT = 60.0

# 多轮：最多带几轮历史、每条多长。上下文是有限资源，历史无限增长就是给自己制造噪声。
MAX_HISTORY_TURNS = 8
MAX_HISTORY_CHARS = 500

# 引用编号只在**本轮**内有效。历史里的 ``[n]`` 指向上一轮的证据，
# 带进本轮会让模型照着旧编号引用，指向完全不同的来源。
_CITATION_RE = re.compile(r"\[(\d+)\]")

# 证据效力等级 → 给模型看的标签。与 channels.CHANNEL_SOURCE_KIND 一一对应。
# 注意：档案不是官方。Lolibrary 是社区整理的发售资料库，可能有错漏，用户看到
# 「官方」会当成品牌确认过的事实，所以两者必须分开称呼。
SOURCE_KIND_LABELS = {
    "own_content": "自有知识库",
    "third_party": "档案",
    "realtime": "网络实时内容",
    "upstream_ugc": "网友发言",
    "image_text": "图上文字（OCR，未校验）",
}

# 能当规范的来源。其余一律只作线索——这是「一源一通道」在提示词层的落地：
# 通道层分开了，结果层又混成一锅，等于白分。
_AUTHORITATIVE_KINDS = frozenset({"own_content", "third_party"})

# --------------------------------------------------------------------- 来源归因
#
# 同样的 ``upstream_ugc`` 里既有买家发言，也有店家贴出来的质检说明（real-02：
# 「圆点点小姐洋服社」的质检报告被写成「有网友提到」）。效力等级只回答「能不能
# 当规范」，回答不了「该叫谁」——称谓必须再看来源域。

_SHOP_HOSTS = (
    "taobao.com", "tmall.com", "jd.com", "1688.com", "pinduoduo.com",
    "yangkeduo.com", "weidian.com", "youzan.com",
)
_SHOP_MARKERS = (
    "旗舰店", "专营店", "官方店", "店铺", "店家", "商家", "掌柜", "客服",
    "洋服社", "工作室", "商品页", "质检报告", "淘宝", "天猫",
)
# 「官方」只留给品牌自有渠道。档案馆、博物馆、协会都不是品牌，不能借用这个词。
_OFFICIAL_MARKERS = ("官方公告", "官方说明", "品牌官方", "官方账号", "官网", "品牌官网")

# 称呼：非 UGC 来源绝不能用「网友」笼统指代，档案也不能抬成「官方」。
APPELLATION_OWN = "自有知识库"
APPELLATION_OFFICIAL = "官方"
APPELLATION_ARCHIVE = "档案（Lolibrary）"
APPELLATION_COMMUNITY_ARCHIVE = "档案（社区整理）"
APPELLATION_SHOP = "店家/商家"
APPELLATION_UGC = "网友发言"
APPELLATION_WEB = "网络内容"
# 标签故意不带「（OCR，未校验）」：标题里已经写了完整标注，标签只说明「是谁说的」。
APPELLATION_IMAGE_TEXT = "图上文字"


def _host_of(url: str) -> str:
    try:
        return (urlsplit(url or "").hostname or "").lower()
    except ValueError:
        return ""


def _looks_like_shop(item: dict) -> bool:
    host = _host_of(str(item.get("url") or ""))
    if any(host == name or host.endswith("." + name) for name in _SHOP_HOSTS):
        return True
    haystack = f"{item.get('title', '')} {str(item.get('text') or '')[:80]}"
    return any(marker in haystack for marker in _SHOP_MARKERS)


def _is_lolibrary(item: dict) -> bool:
    host = _host_of(str(item.get("url") or ""))
    return host.endswith("lolibrary.org") or "lolibrary" in str(item.get("title") or "").lower()


def _looks_like_official(item: dict) -> bool:
    haystack = f"{item.get('title', '')} {str(item.get('text') or '')[:80]}"
    return any(marker in haystack for marker in _OFFICIAL_MARKERS)


def source_appellation(item: dict) -> str:
    """证据该被怎么称呼：效力等级打底，来源域纠正「店家 / 官方 / 档案 / 网友」。

    等级只说「能不能当规范」，这里回答「是谁说的」。顺序很重要：店家页面上
    也常写「官方」，先判店铺再判官方，否则旗舰店会被叫成官方；档案则单独
    标成社区整理，不和品牌官方混用。
    """
    kind = item.get("source_kind")
    if kind == "own_content":
        return APPELLATION_OWN
    if kind == "third_party":
        return APPELLATION_ARCHIVE if _is_lolibrary(item) else APPELLATION_COMMUNITY_ARCHIVE
    # 必须排在店家伙官方判断之前：识别文本里可能恰好出现「店铺」「官方」字样，
    # 那只是图上的字，不能因此把这条证据改称店家或官方。
    if kind == "image_text":
        return APPELLATION_IMAGE_TEXT
    if _looks_like_shop(item):
        return APPELLATION_SHOP
    if _looks_like_official(item):
        return APPELLATION_OFFICIAL
    if kind == "upstream_ugc":
        return APPELLATION_UGC
    if kind == "realtime":
        return APPELLATION_WEB
    return "来源"


_CLUE_HEADER = (
    "【仅作线索·不得当作规范】\n"
    "以下是网友发言、店家页面、网络实时内容，以及原帖配图的机器识别文字。"
    "称呼要跟每条开头的标签走：\n"
    "「网友发言」→ 写「有网友提到」「出物的人说」；\n"
    "「店家/商家」→ 写「店家提到」「店铺页面写着」，绝不能写成「网友」；\n"
    "「档案（Lolibrary）」→ 社区整理的发售资料库，可能有错漏，写「档案记录」"
    "「Lolibrary 上记录」，不能写「官方」，也不能写「网友」；\n"
    "「官方」→ 品牌自有渠道，写「官方公告」「品牌官方说明」；\n"
    "「图上文字」→ 原帖配图的机器识别结果（OCR），只保证字符、不保证列对齐。"
    "lolita 价格口径：预约价 = 定金 + 尾款，二者是同一件裙子的两个付款阶段，现货价是另一回事；"
    "证据里出现「预约价 578 元，由定金 188 元加尾款 390 元组成；现货价 598 元」这种整理句时，"
    "说明该档位已通过算术校验：照这个句式回答并保留等式，不要堆一行裸数字，"
    "也不要罗列其他档位/配色/小物的数字；除非用户明确问全表，只答整理句里的主档位。"
    "没有整理句的原始行只能原样引用整行（保留原行顺序），绝不能把数字归纳成「预约价 390」这类"
    "带类型标签的断言，型号/列名看不出来就不要猜；只读到现货价就说明只读到现货价，"
    "不要用现货价冒充预约价。每个价格数字都要紧挨 [n]；最多在价格句后加一次"
    "「具体可点开参考来源核对」，不要重复；不要写「机器识别」「未校验」「以原图为准」"
    "这类提示，来源说明证据标题里已有，答案里不必再提。\n"
    "这些内容不得作为洗护方式、尺码、材质、工艺的规范结论；"
    "与【可作依据】冲突时，以后者为准。"
)

SYNTH_SYSTEM = """你是 Lolita 领域的问答助手，像微信里一个懂 Lolita 的朋友那样回话。

## 怎么说
1. **第一句就回答用户真正想问的那件事**。别复述问题、别绕圈子，也别用「根据资料显示」
   「综上所述」这类书面腔。
2. 说人话：短句、日常词。遇到 JSK/OP、烫金、雪纺这类词，顺手用半句大白话解释一下；
   默认对方是新手，别默认人家懂行话。
3. 像聊天一样写，不要 Markdown 标题和项目符号；只有确实要对比两三条信息时才分短行。
4. 答完就停：不寒暄、不客套、不问「还需要什么」、不写「希望对你有帮助」。
   简单问题 1～3 句，需要解释的最多 5 句。
5. 语气可以亲切（「可以呀」「最好别」「这个得注意」），但不卖萌、不用表情符号。

## 事实与来源
1. 只讲【证据】里的事。证据没写的，不补常识、不推测、不「顺便提醒」；
   尤其不要拿日期或经验推断状态（「应该还没开卖」「大概有货」「估计会掉色」）。
2. 关键事实（价格、尺码、日期、能不能洗、有没有货等）后面标来源编号，如 [1][2]；
   编号对应【证据】里的序号。**编号放在它真正支撑的那句话里**，不要只挂在段末——
   读者要能看出这一句是哪来的。**只能引用本轮【证据】里的编号**：历史对话不参与
   编号，其中的编号已失效并移除，不要沿用或猜测。
3. 价格、尺码、日期照证据原文说，别换算、别合并不同币种、别约等于。
4. 证据里没有、或者只沾一点边，就直接说「这个我这边没查到」「资料里没写」，
   可以顺带告诉用户换个什么问法；但不许硬编一个答案。
5. 证据互相打架时，说清楚两边分别怎么讲，别替用户拍板。
6. 证据分两类：【可作依据】是自有知识库与档案，可以直接陈述；
   【仅作线索】是网友发言、店家页面和网络内容，不能当规范。两者冲突时以【可作依据】为准。
7. **称呼跟着证据开头的标签走**：「网友发言」才写「有网友提到」「出物的人说」；
   「店家/商家」写「店家提到」「店铺页面写着」；「官方」只给品牌自有渠道，写「官方公告」；
   「档案（Lolibrary）」是社区整理的发售资料库，不是品牌官方、可能有错漏，写「档案记录」
   「Lolibrary 上记录」，并说明它是社区整理。
8. 禁止用「网友说」指代店家、官方或档案——把店家的话说成网友反馈，比不引用更危险；
   反过来也不能把社区档案抬成「官方」，那会让用户以为品牌确认过。
9. 「图上文字」是原帖配图的机器识别结果，只保证字符、不保证列对齐。lolita 价格口径：
   预约价 = 定金 + 尾款，二者是同一件裙子的两个付款阶段；现货价是另一回事。
   证据里出现「预约价 578 元，由定金 188 元加尾款 390 元组成；现货价 598 元」这类整理句时，
   说明该档位已通过算术校验：照这个句式回答并保留等式，不要堆一行裸数字，
   也不要罗列其他档位/配色/小物的数字；除非用户明确问全表，只答整理句里的主档位。
   没有整理句就只能原样引用整行，绝不能把数字归纳成「预约价 390」这类带类型标签的断言。
   回答要分开说预约价和现货价；只读到现货价就明说只读到现货价，不要用现货价冒充预约价。
   每个价格数字都要紧挨 [n]；最多在价格句后加一次「具体可点开参考来源核对」，
   不要重复；不要写「机器识别」「未校验」「以原图为准」这类提示，
   来源说明证据标题里已有，答案里不必再提。
"""


def _evidence_line(item: dict) -> str:
    """一条证据：``[n]（效力标签）标题：正文（来源 url）``。

    标签必须在**每条**上都出现，而不是只在分块标题上说一次——模型引用时看的是
    那一条，块标题离得远，容易被忽略。
    """
    label = source_appellation(item)
    line = f"[{item.get('n')}]（{label}）{item.get('title', '')}：{item.get('text', '')}"
    url = item.get("url")
    if url:
        line += f"（来源 {url}）"
    return line


def _evidence_block(evidence: list[dict]) -> str:
    """按效力分两块：可作依据 / 仅作线索。

    分块的目的是让"这是网友说的"成为**结构**而不是模型自己去记的一条规则。
    编号仍全局唯一（引用靠它定位），所以分块后编号可能不连续——这是有意的，
    编号是标识不是阅读顺序。
    """
    if not evidence:
        return "（没有任何证据）"
    authoritative = [item for item in evidence if item.get("source_kind") in _AUTHORITATIVE_KINDS]
    clues = [item for item in evidence if item.get("source_kind") not in _AUTHORITATIVE_KINDS]

    sections: list[str] = []
    if authoritative:
        sections.append(
            "【可作依据】\n" + "\n".join(_evidence_line(item) for item in authoritative)
        )
    if clues:
        sections.append(
            _CLUE_HEADER + "\n" + "\n".join(_evidence_line(item) for item in clues)
        )
    return "\n\n".join(sections)


# --------------------------------------------------------------------- 句级引用覆盖
#
# 硬错（编号越界 / 零引用）已有两道闸，实测 26 处引用零命中；真正漏掉的是
# 「一整句事实没有编号」。这里先给出「哪些句子没编号」的判定口径（告警与
# 下文补齐共用），**流式只用它告警、不改答案**——流式下替换等于话说一半被
# 打脸；非流式在补得动的时候补编号，见「句级引用补齐」一节。
#
# 口径按实测定：按 ``。！？`` 切句（不切分号——写作器习惯把 [n] 挂在分号链
# 末尾），≥15 字且句内没有 [n] 才算；元陈述、免责/降权、反问收尾一律豁免。
# 不加豁免会误标 7 句仅 1 句为真（14%），加豁免后 1/1，误报就是噪音。

MIN_CLAIM_CHARS = 15
_SENTENCE_RE = re.compile(r"[^。！？\n]+[。！？]?")

# 元陈述：在交代「资料里有什么」，本身不是在断言事实。
_META_MARKERS = (
    "只查到", "只记了", "只记", "资料里", "档案里只", "我这边只", "资料中没", "没写",
)
# 免责 / 降权：不是事实断言，而是提醒用户别把它当规范。
_DISCLAIMER_MARKERS = (
    "以实物为准", "为准", "个人感受", "个人经验", "仅供参考", "因人而异",
    "分歧可能来自",
    "这只是", "只能算", "具体还得", "具体还是得",
)
# 反问收尾：对话性的追问，不是结论。
_CLOSING_MARKERS = ("你想", "想了解", "哪方面", "可以说", "需要我", "要不要我")
# 拒答：本来就不该有引用（与 graph 的拒答识别同源，但不跨模块依赖）。
_REFUSAL_MARKERS = (
    "没找到", "没查到", "查不到", "不清楚", "说不准", "没有相关", "暂无", "没有找到",
)


def _exempt_sentence(sentence: str) -> bool:
    if any(marker in sentence for marker in _META_MARKERS):
        return True
    if any(marker in sentence for marker in _DISCLAIMER_MARKERS):
        return True
    if any(marker in sentence for marker in _REFUSAL_MARKERS):
        return True
    if sentence.endswith("？") and any(marker in sentence for marker in _CLOSING_MARKERS):
        return True
    # 「你想了解哪方面，可以说得具体点。」结尾是句号，但仍是反问收尾。
    return any(marker in sentence for marker in ("你想", "想了解", "哪方面"))


def _is_uncited_claim(sentence: str) -> bool:
    """≥15 字、句内没有 ``[n]``、也不属于豁免类型。"""
    if not sentence or _CITATION_RE.search(sentence):
        return False
    visible = _CITATION_RE.sub("", sentence).strip()
    return len(visible) >= MIN_CLAIM_CHARS and not _exempt_sentence(sentence)


def uncited_claim_sentences(answer: str, evidence: list[dict]) -> list[str]:
    """≥15 字、句内没有 ``[n]``、也不属于豁免类型的句子。

    没有证据时不判：那时回答本来就该是拒答，要求引用是把正确答案判成错误。
    """
    if not answer or not evidence:
        return []
    return [
        match.group().strip()
        for match in _SENTENCE_RE.finditer(answer)
        if _is_uncited_claim(match.group())
    ]


# --------------------------------------------------------------------- 句级引用补齐
#
# 告警只回答「这句没有编号」，不回答「这句到底有没有证据」。实测四份 forensics
# 快照（75 条真实回答、215 个可判定句）里的 34 条无引用长句：单条证据的内容
# 二元组覆盖率 ≥0.45 的 15 条，逐条人工核对全部成立；剩下 19 条里 9 条是免责/
# 降权、4 条是建议、1 条是无证据的定义句，其余是低于阈值的真漏。
#
# 所以这里只做两件事，且**只走非流式**：
# 1. 补：覆盖率过阈值就把那一句的编号补在句末标点前；
# 2. 收窄：没证据、又确实是定义式断言，句首加「按一般说法，」。
#    免责句和建议句不动——它们本来就不是事实断言，加引用是硬挂。
#
# 阈值 0.45 是实测定的：0.41 那档会把「求助帖」当成「要细节图」这条建议的
# 依据（real-11），挂错比漏补贵得多。宁可留空。

MIN_GROUNDING = 0.45

# 比对前先去掉标点、数字和虚词：留着它们，长句里"是…的…"这种骨架会把
# 覆盖率稀释到 0.2 左右，真正共享的内容词反而看不出来。
_MATCH_NOISE_RE = re.compile(r"[\s0-9\[\]（）()，。！？、；：…·「」“”\"'\-]")
_FUNCTION_CHARS = frozenset(
    "的是了在和与就都也而把被对从这那其之为以于等呢吧啊不没很更最又还只个给让"
)
_DIGITS_RE = re.compile(r"\d")

# 只在句**首**就下定义时才加限定词：定义夹在从句中间（「…的 OP [2]，而 OP 是
# one-piece 的缩写」）时，前缀会插进逗号后面，把句子写坏。
#
# 另外要求「几乎没有任何字面重合」（<0.2）才收窄：real-03 的「不能湿洗就是别
# 下水」实际由证据支撑，只是措辞太短没够 0.45，给它加「按一般说法」等于凭空
# 贬低一句正确的话。
_NARROW_PREFIX = "按一般说法，"
_NARROW_MAX_GROUNDING = 0.2
_DEFINITION_MARKERS = ("就是", "是指", "指的是", "的缩写")
_NARROW_HEAD_CHARS = 12


def _content_bigrams(text: str) -> set[str]:
    clean = _MATCH_NOISE_RE.sub("", _CITATION_RE.sub("", text or ""))
    clean = "".join(ch for ch in clean if ch not in _FUNCTION_CHARS)
    return {clean[index : index + 2] for index in range(len(clean) - 1)}


def grounding_score(sentence: str, text: str) -> float:
    """句子里的内容二元组有多少比例能在证据正文里找到（0～1）。"""
    sent = _content_bigrams(sentence)
    if not sent:
        return 0.0
    return len(sent & _content_bigrams(text)) / len(sent)


def supporting_evidence(sentence: str, evidence: list[dict]) -> int | None:
    """这句能被哪条证据支撑；没有过阈值的一律返回 ``None``。

    数字要额外对齐：句子写了「578」而证据里没有这个数，就算字面重合度再高
    也不算支撑——价格和日期是最容易被"就近挂一个"挂错的地方。
    """
    digits = set(_DIGITS_RE.findall(_CITATION_RE.sub("", sentence)))
    best_score = 0.0
    best_number: int | None = None
    for item in evidence:
        text = str(item.get("text") or "")
        score = grounding_score(sentence, text)
        if score < MIN_GROUNDING or score <= best_score:
            continue
        if digits and not digits <= set(_DIGITS_RE.findall(text)):
            continue
        number = int(item.get("n") or 0)
        if not number:
            continue
        best_score, best_number = score, number
    return best_number


def best_grounding(sentence: str, evidence: list[dict]) -> float:
    """这句和证据的最高字面重合度（不论是否过阈值）。"""
    return max(
        (grounding_score(sentence, str(item.get("text") or "")) for item in evidence),
        default=0.0,
    )


def _insert_citation(sentence: str, number: int) -> str:
    """编号放在句末标点之前，和模型自己写出的「… [1]。」保持同一形状。"""
    body = sentence.rstrip()
    tail = sentence[len(body) :]
    if body and body[-1] in "。！？!?":
        return f"{body[:-1].rstrip()} [{number}]{body[-1]}{tail}"
    return f"{body} [{number}]{tail}"


def _narrow(sentence: str, grounding: float) -> str | None:
    """无证据支撑、但句首就是定义的断言：加限定词；其余返回 ``None``。"""
    visible = _CITATION_RE.sub("", sentence)
    head = visible[: _NARROW_HEAD_CHARS + 4]
    if grounding >= _NARROW_MAX_GROUNDING:
        return None
    if _DIGITS_RE.search(visible):
        return None
    if not any(marker in head for marker in _DEFINITION_MARKERS):
        return None
    lead = sentence[: len(sentence) - len(sentence.lstrip())]
    return f"{lead}{_NARROW_PREFIX}{sentence.lstrip()}"


def fill_citations(
    answer: str, evidence: list[dict]
) -> tuple[str, list[dict], list[str]]:
    """给无引用长句补编号或收窄措辞；返回 ``(新文本, 补引用记录, 被收窄的句子)``。

    只处理 :func:`uncited_claim_sentences` 会告警的那类句子：句子已经有编号、
    太短、或是免责/元陈述时一律不碰。补上去的编号只可能来自本轮证据，
    且必须是内容真的支撑这句的那一条。
    """
    if not answer or not evidence:
        return answer, [], []
    filled: list[dict] = []
    narrowed: list[str] = []

    def _replace(match: re.Match) -> str:
        sentence = match.group()
        if not _is_uncited_claim(sentence):
            return sentence
        visible = _CITATION_RE.sub("", sentence).strip()
        number = supporting_evidence(sentence, evidence)
        if number is not None:
            filled.append({"sentence": visible[:80], "n": number})
            return _insert_citation(sentence, number)
        narrowed_sentence = _narrow(sentence, best_grounding(sentence, evidence))
        if narrowed_sentence is None:
            return sentence
        narrowed.append(visible[:80])
        return narrowed_sentence

    return _SENTENCE_RE.sub(_replace, answer), filled, narrowed


# --------------------------------------------------------------------- 归因后处理
#
# 提示词负责「别写错」，这里兜住模型仍可能写出的「网友」。只在**能确定**
# 该句引用的证据全是店家 / 官方时替换；同一句里还引了真 UGC 就不动——
# 分不清说的是谁时，宁可不改。

_ATTRIBUTION_REWRITES = (
    ("那位网友还", "{label}还"),
    ("那位网友", "{label}"),
    ("有网友提到", "{label}提到"),
    ("有网友说", "{label}说"),
    ("网友提到", "{label}提到"),
    ("网友说", "{label}说"),
    ("网友的说法", "{label}的说法"),
    ("网友", "{label}"),
)


def _evidence_numbers(evidence: list[dict]) -> dict[int, dict]:
    return {
        int(item.get("n") or index): item for index, item in enumerate(evidence, start=1)
    }


def fix_source_attribution(
    answer: str, evidence: list[dict]
) -> tuple[str, list[dict]]:
    """把非 UGC 来源上的「网友」改成正确称谓；返回 ``(新文本, 改动记录)``。

    只有整句引用的证据都属于同一类非 UGC 来源时才替换：混引时无从判断「网友」
    指的是哪一条，改写可能把一句正确的 UGC 归因也改坏。档案用「档案记录里」，
    不用「官方」——社区整理的资料不能说得像品牌确认过。
    """
    if not answer or not evidence:
        return answer, []
    numbered = _evidence_numbers(evidence)
    fixes: list[dict] = []
    parts: list[str] = []
    for match in _SENTENCE_RE.finditer(answer):
        sentence = match.group()
        if "网友" not in sentence:
            parts.append(sentence)
            continue
        cited = [int(token) for token in _CITATION_RE.findall(sentence)]
        labels = {source_appellation(numbered[n]) for n in cited if n in numbered}
        if not labels or APPELLATION_UGC in labels:
            parts.append(sentence)
            continue
        if labels == {APPELLATION_SHOP}:
            label = "店家"
        elif labels == {APPELLATION_OFFICIAL}:
            label = "官方"
        elif labels == {APPELLATION_ARCHIVE}:
            label = "Lolibrary 上"
        elif labels == {APPELLATION_COMMUNITY_ARCHIVE}:
            label = "档案记录里"
        elif labels == {APPELLATION_OWN}:
            label = "自有资料里"
        elif labels == {APPELLATION_IMAGE_TEXT}:
            label = "图上文字"
        else:
            parts.append(sentence)
            continue
        rewritten = sentence
        for old, template in _ATTRIBUTION_REWRITES:
            if old in rewritten:
                rewritten = rewritten.replace(old, template.format(label=label))
                if "网友" not in rewritten:
                    break
        if rewritten != sentence:
            fixes.append({"ref": cited[0] if cited else 0, "to": label})
            parts.append(rewritten)
        else:
            parts.append(sentence)
    return "".join(parts), fixes


# --------------------------------------------------------------------- 图上文字后处理
#
# OCR 只保证字符，不保证列对齐。实测模型会把「尾款 390」写成「预约价 390」，
# 后两列直接丢掉。提示词已经禁止这种归纳，这里再兜一层：**只有当某个
# 「类型 + 数字」在 OCR 原文中不是紧挨着出现时**才动。有校验后的整理句就
# 整句换成整理句（数字来自算术校验），没有就改回原样引用那一行。
# 不做数字换算、不补类型，也不碰没有引用图上文字的句子。

_OCR_PRICE_LABEL_RE = re.compile(
    r"(预约价|现货价|定金|尾款|全款|售价|原价|现价)\s*(?:是|为|约|[:：])?\s*(\d{2,5})"
)


def _ocr_rows(evidence: list[dict], numbers: dict[int, dict]) -> list[tuple[int, str]]:
    rows: list[tuple[int, str]] = []
    for number, item in numbers.items():
        if item.get("source_kind") != "image_text":
            continue
        for raw in str(item.get("text") or "").splitlines():
            line = raw.strip()
            # 「图 1：」是证据拼装前缀，不是图上原文；去掉后再判断是否空行。
            line = re.sub(r"^图\s*\d+\s*[：:]\s*", "", line)
            if not line or line.startswith("（OCR"):
                continue
            rows.append((number, line))
    return rows


def _organized_rows(
    evidence: list[dict], numbers: dict[int, dict]
) -> list[tuple[int, dict]]:
    """证据里已经通过算术校验的整理句，连同它的证据编号。"""
    from app.agent import image_evidence

    rows: list[tuple[int, dict]] = []
    for number, item in numbers.items():
        if item.get("source_kind") != "image_text":
            continue
        for row in image_evidence.organized_rows(str(item.get("text") or "")):
            rows.append((number, row))
    return rows


def _pick_organized(
    organized: list[tuple[int, dict]], sentence: str, ref: int
) -> dict | None:
    """优先取同一编号证据里的行；句子点名了型号就取那一行。"""
    if not organized:
        return None
    same_ref = [row for number, row in organized if number == ref]
    pool = same_ref or [row for _number, row in organized]
    for row in pool:
        label = row.get("label") or ""
        if label and label in sentence:
            return row
    return pool[0] if pool else None


def demote_ocr_price_labels(
    answer: str, evidence: list[dict]
) -> tuple[str, list[dict]]:
    """纠正模型对 OCR 数字的错误类型归纳；返回 ``(新文本, 改动记录)``。

    只处理同时满足的句子：出现「类型 + 数字」、该组合在 OCR 原文里并不相邻、
    且句子引用（或至少没有排除）图上文字证据。证据里有通过算术校验的整理句时，
    整句换成整理句（数字来自校验），否则改回原样引用那一行；两者都找不到时
    退成一句「不保证列对齐，请对照原图」，绝不保留错误的类型指派或等式。
    """
    if not answer or not evidence:
        return answer, []
    numbered = _evidence_numbers(evidence)
    image_numbers = {
        number
        for number, item in numbered.items()
        if item.get("source_kind") == "image_text"
    }
    if not image_numbers:
        return answer, []

    rows = _ocr_rows(evidence, numbered)
    organized = _organized_rows(evidence, numbered)
    joined = "".join(line.replace(" ", "").replace("\u3000", "") for _, line in rows)
    demotions: list[dict] = []
    parts: list[str] = []

    for match in _SENTENCE_RE.finditer(answer):
        sentence = match.group()
        pairs = _OCR_PRICE_LABEL_RE.findall(sentence)
        if not pairs:
            parts.append(sentence)
            continue
        unsupported = [
            (label, number) for label, number in pairs if f"{label}{number}" not in joined
        ]
        if not unsupported:
            parts.append(sentence)
            continue
        cited = {int(token) for token in _CITATION_RE.findall(sentence)}
        if cited and not (cited & image_numbers):
            parts.append(sentence)
            continue

        ref = min(cited & image_numbers) if cited & image_numbers else min(image_numbers)
        picked = _pick_organized(organized, sentence, ref)
        if picked is not None:
            # 用校验过的数字重写整句：等式如果本来就不成立，这里只输出成立的那一版。
            replacement = f"{picked['line'].rstrip('。')}[{ref}]。"
            quotes: list[str] = []
        else:
            quotes = []
            for _label, number in unsupported:
                for _row_number, line in rows:
                    if (
                        re.search(rf"(?<!\d){re.escape(number)}(?!\d)", line)
                        and line not in quotes
                    ):
                        quotes.append(line)
        if picked is None and quotes:
            replacement = "图上标着「" + "」；「".join(quotes[:3]) + f"」[{ref}]。"
        elif picked is None:
            replacement = f"图上文字是机器识别的，不保证列对齐[{ref}]。"
        demotions.append(
            {
                "sentence": sentence[:80],
                "unsupported": [f"{label}{number}" for label, number in unsupported],
                "replacement": replacement,
            }
        )
        parts.append(replacement)

    if not demotions:
        return answer, []
    return "".join(parts), demotions


# 答案里不再重复「未校验 / 以原图为准」：来源性质在证据标题和【仅作线索】块里。
# 只在确实引用了图上文字证据时，句末补一次「具体可点开参考来源核对」，让用户自查。
SOURCE_GUIDE = "具体可点开参考来源核对"
_UNVERIFIED_PAREN_RE = re.compile(r"（[^（）]{0,40}(?:未校验|以原图为准)[^（）]{0,20}）")
# 模型偶尔会自己补一句「这是机器识别结果，还没校验」：按分句删掉，价格句不动。
_CAVEAT_MARKERS = ("未校验", "还没校验", "以原图为准", "机器识别结果")


def _drop_caveat_clauses(answer: str) -> str:
    parts: list[str] = []
    for match in _SENTENCE_RE.finditer(answer):
        sentence = match.group()
        if any(marker in sentence for marker in _CAVEAT_MARKERS):
            clauses = re.split(r"(?<=[，,])", sentence)
            sentence = "".join(
                clause
                for clause in clauses
                if not any(marker in clause for marker in _CAVEAT_MARKERS)
            ).lstrip("，, ")
        parts.append(sentence)
    return "".join(parts)


def clean_source_annotations(answer: str) -> str:
    """去掉答案里的未校验/以原图为准旁注；[n] 在括号外，不受影响。"""
    if not answer:
        return answer
    answer = _UNVERIFIED_PAREN_RE.sub("", answer)
    answer = answer.replace("，以原图为准", "").replace("以原图为准", "")
    return _drop_caveat_clauses(answer)


def _keep_one_guide(answer: str) -> str:
    first = answer.find(SOURCE_GUIDE)
    if first < 0:
        return answer
    cut = first + len(SOURCE_GUIDE)
    return answer[:cut] + answer[cut:].replace(SOURCE_GUIDE, "")


def source_guide_suffix(answer: str, evidence: list[dict]) -> str:
    """仅当答案引用了图上文字证据、且还没写过引导时，返回要补的一句。"""
    if not answer or SOURCE_GUIDE in answer:
        return ""
    image_numbers = {
        int(item["n"])
        for item in evidence
        if item.get("source_kind") == "image_text" and item.get("n") is not None
    }
    cited = {int(token) for token in _CITATION_RE.findall(answer)}
    if not (image_numbers & cited):
        return ""
    return f" {SOURCE_GUIDE}。"


# --------------------------------------------------------------------- 诊断
#
# 诊断挂在 ``writer.usage`` 下：graph 已把 writer.usage 同时写进响应
# （``payload["usage"]["writer"]``）与轨迹，因此不必改 graph.py 就能让告警
# 在两个地方可见。这里不参与成本计算（cost.usage_cost 只读 calls/calls_detail）。


def _diagnostics_block(
    answer: str,
    evidence: list[dict],
    fixes: list[dict],
    *,
    streamed: bool,
    filled: list[dict] | None = None,
    narrowed: list[str] | None = None,
    demotions: list[dict] | None = None,
    consensus: dict | None = None,
) -> dict:
    flagged = uncited_claim_sentences(answer, evidence)
    return {
        "streamed": streamed,
        "citation_coverage": {
            "checked_evidence": len(evidence),
            "uncited_count": len(flagged),
            "uncited_sentences": [sentence[:80] for sentence in flagged],
            # 本轮补上去几条。覆盖率告警是补完之后的余量，两者一起看才解释得通。
            "filled_count": len(filled or []),
        },
        "citation_fills": filled or [],
        "narrowed_sentences": narrowed or [],
        "source_attribution_fixes": fixes,
        # OCR 归纳被换回原文行的记录：出现过就说明模型试图替数字指派列含义。
        "ocr_price_demotions": demotions or [],
        # 多帖数值比对：没有可比对象时 text 为空、groups 为 []。
        "number_consensus": consensus or {},
    }


def build_synth_messages(
    question: str,
    evidence: list[dict],
    history: list[dict] | None = None,
) -> list[dict]:
    evidence_text = _evidence_block(evidence)
    messages: list[dict] = [{"role": "system", "content": SYNTH_SYSTEM}]
    for turn in sanitize_history(history):
        messages.append({"role": turn["role"], "content": turn["content"]})
    messages.append(
        {
            "role": "user",
            "content": f"【证据】\n{evidence_text}\n\n【问题】\n{question}",
        }
    )
    return messages


# --------------------------------------------------------------------- 图片兜底提示
#
# 小红书笔记的价格/尺码常只写在配图里。本地 OCR 实测不可靠（见
# ``eval/ocr_accuracy.md``：行级完整率 46.8%，还有静默错值与不可复现），
# 所以这里**不读图里的数字**，只补一句固定提示。
#
# 措辞由代码给，不由提示词给：模型只看到「有 N 张图」时，很容易写成
# 「图中价格表显示…」，把「有图」说成「有价格」。这句话必须一字不差、
# 且全答案最多出现一次。

_IMAGE_HINT_PRICE = (
    "价格", "多少钱", "定金", "尾款", "预约价", "现货价", "全款", "售价", "贵不贵",
)
_IMAGE_HINT_SIZE = (
    "尺码", "码数", "尺寸", "胸围", "腰围", "肩宽", "袖长", "裙长", "衣长", "穿多大", "什么码",
)

# 正文里已经有对应数字时不必再提示「图里可能有」。
_PRICE_IN_TEXT_RE = re.compile(
    r"[¥￥]\s*\d|(?:价格|售价|定价|定金|尾款|预约价|现货价|全款)\s*[:：]?\s*\d{2,5}"
)
_SIZE_IN_TEXT_RE = re.compile(
    r"(?:尺码|码数|胸围|腰围|肩宽|袖长|裙长|衣长)\s*[:：]?\s*\d{1,3}"
)


def _image_hint_kind(question: str) -> str:
    text = question or ""
    price = any(marker in text for marker in _IMAGE_HINT_PRICE)
    size = any(marker in text for marker in _IMAGE_HINT_SIZE)
    if price and size:
        return "both"
    if price:
        return "price"
    if size:
        return "size"
    return ""


def _image_hint_target(evidence: list[dict]) -> dict | None:
    """挑一条带图证据来承载提示。

    同一个问题常有多条带图笔记，提示指向哪条很重要：标题写了「价格表/尺码表」
    的那条才值得让用户去点。没有这类标题时退回第一条带图的。
    """
    candidates = [item for item in evidence if int(item.get("image_count") or 0) > 0]
    if not candidates:
        return None

    def rank(item: dict) -> int:
        title = str(item.get("title") or "")
        if any(key in title for key in ("价格表", "价目表", "尺码表", "参数表")):
            return 2
        if any(key in title for key in ("价格", "尺码", "定金", "尾款", "预约")):
            return 1
        return 0

    return max(candidates, key=rank)


def image_hint(question: str, evidence: list[dict]) -> str:
    """价格/尺码图上才有的提示；四条同时满足才给，全答案最多一条。

    1. 问题问的是价格或尺码；
    2. 证据里至少一条带图（``image_count > 0``）；
    3. 证据正文没给出对应数字；
    4. **没有** ``image_text`` 证据——已经读到图上文字时，不能再补一句
       「图片里的文字没有被解析」，两句同时出现会自相矛盾。

    返回空串表示不加。句子里的「可能含」是刻意的：有图不等于有价格表，
    没有 OCR 就不该断言图里写了什么。
    """
    if any(item.get("source_kind") == "image_text" for item in evidence):
        return ""
    kind = _image_hint_kind(question)
    if not kind or not evidence:
        return ""
    item = _image_hint_target(evidence)
    if item is None:
        return ""

    haystack = "\n".join(str(entry.get("text") or "") for entry in evidence)
    wanted: list[str] = []
    if kind in ("price", "both") and not _PRICE_IN_TEXT_RE.search(haystack):
        wanted.append("价格")
    if kind in ("size", "both") and not _SIZE_IN_TEXT_RE.search(haystack):
        wanted.append("尺码")
    if not wanted:
        return ""

    if wanted == ["价格"]:
        label, target = "价格表", "价格"
    elif wanted == ["尺码"]:
        label, target = "尺码表", "尺码"
    else:
        label, target = "价格表/尺码表", "价格和尺码"
    count = int(item.get("image_count") or 0)
    number = item.get("n")
    subject = f"[{number}] 那条笔记" if number else "该帖"
    return (
        f"\n\n另外，{subject}含 {count} 张图，可能含{label}；"
        f"{target}{SOURCE_GUIDE}。"
    )


# --------------------------------------------------------------------- 多帖数值比对
#
# 价格/尺码问题召回的多条帖子常给出互相矛盾的数字，模型会把它们揉成一句，
# 用户看不出分歧。这里在写作之后补一小段纯代码生成的比对：
# * 只比同一 SKU、同一维度，且至少两条证据都给出数字的；
# * 数字全部从证据文本里字面抽取，生成过程不新增任何数字；
# * 同一数值出现在多数证据（≥3 条）时写「多帖一致」并列出编号，只是措辞，
#   不改证据等级；其余情况逐条列，不替用户拍板。

_CONSENSUS_PRICE_MARKERS = (
    "价格", "多少钱", "定金", "尾款", "预约价", "现货价", "全款", "售价", "贵不贵",
)
_CONSENSUS_SIZE_MARKERS = (
    "尺码", "码数", "尺寸", "胸围", "腰围", "肩宽", "袖长", "裙长", "衣长", "什么码", "穿多大",
)
_AMOUNT = r"(?:\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?|\d{2,5}(?:\.\d{1,2})?)"
_CONSENSUS_PRICE_RE = re.compile(
    rf"(?P<kind>预约价|现货价|定金|尾款|全款|售价|定价|原价|现价|价格)"
    rf"\s*(?:是|为|约|[:：])?\s*(?:[¥￥]|CNY|RMB)?\s*(?P<value>{_AMOUNT})"
    rf"\s*(?:元|CNY|RMB)?",
    re.IGNORECASE,
)
_CONSENSUS_CURRENCY_RE = re.compile(
    rf"(?:[¥￥]|CNY|RMB)\s*(?P<value>{_AMOUNT})", re.IGNORECASE
)
_CONSENSUS_SIZE_RE = re.compile(
    r"(?P<kind>尺码|码数|尺寸|胸围|腰围|肩宽|袖长|裙长|衣长)"
    r"\s*(?:是|为|约|[:：])?\s*(?P<value>S|M|L|XL|XXL|\d{2,3})\s*(?:cm|CM|厘米)?"
)
_CONSENSUS_SKU = (
    ("长围裙", "长围裙"), ("短围裙", "短围裙"),
    ("长款", "长款"), ("短款", "短款"),
    ("边夹", "边夹"), ("发带", "发带"), ("裙撑", "裙撑"), ("帽子", "帽子"),
    ("围裙", "围裙"), ("OP", "OP"), ("JSK", "JSK"),
)
_CONSENSUS_PRICE_KINDS = ("定金", "尾款", "预约价", "现货价")
UNKNOWN_SKU = "未标明型号"


def _consensus_topics(question: str) -> list[str]:
    text = question or ""
    topics: list[str] = []
    if any(marker in text for marker in _CONSENSUS_PRICE_MARKERS):
        topics.append("price")
    if any(marker in text for marker in _CONSENSUS_SIZE_MARKERS):
        topics.append("size")
    return topics


def _normalize_amount(raw: str) -> str:
    text = raw.replace(",", "")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text.lstrip("0") or "0"


def _sku_of(line: str) -> str:
    upper = line.upper()
    for key, name in _CONSENSUS_SKU:
        if key.upper() in upper:
            return name
    return UNKNOWN_SKU


def _subject_terms(question: str) -> list[str]:
    """从问题里剥掉数值意图词，剩下的就是「问的是哪条裙子」。"""
    text = question or ""
    markers = sorted(
        {*_CONSENSUS_PRICE_MARKERS, *_CONSENSUS_SIZE_MARKERS}, key=len, reverse=True
    )
    for marker in markers:
        text = text.replace(marker, " ")
    text = re.sub(r"[的和与及]", " ", text)
    return [
        term
        for term in re.split(r"[\s，。！？、,.!?：:；;（）()]+", text)
        if len(term) >= 2
    ]


def _mentions_subject(item: dict, terms: list[str]) -> bool:
    if not terms:
        return True
    haystack = f"{item.get('title') or ''}\n{item.get('text') or ''}"
    return any(term in haystack for term in terms)


def _claims_from_item(item: dict, topics: list[str]) -> list[dict]:
    """一条证据里的数值主张；每个数字都能在原文里字面找到。"""
    claims: list[dict] = []
    for raw in str(item.get("text") or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        sku = _sku_of(line)
        if "price" in topics:
            found: list[tuple[str, str]] = []
            for match in _CONSENSUS_PRICE_RE.finditer(line):
                found.append(
                    (match.group("kind"), _normalize_amount(match.group("value")))
                )
            known = {value for _kind, value in found}
            for match in _CONSENSUS_CURRENCY_RE.finditer(line):
                value = _normalize_amount(match.group("value"))
                if value not in known:
                    found.append(("价格", value))
                    known.add(value)
            if found:
                by_kind: dict[str, list[str]] = {}
                for kind, value in found:
                    values = by_kind.setdefault(kind, [])
                    if value not in values:
                        values.append(value)
                row = ""
                if all(
                    kind in by_kind and len(by_kind[kind]) == 1
                    for kind in _CONSENSUS_PRICE_KINDS
                ):
                    row = "/".join(by_kind[kind][0] for kind in _CONSENSUS_PRICE_KINDS)
                for kind, values in by_kind.items():
                    claims.append(
                        {
                            "topic": "price",
                            "sku": sku,
                            "kind": kind,
                            "values": tuple(values),
                            "display": row or "/".join(values),
                        }
                    )
        if "size" in topics:
            for match in _CONSENSUS_SIZE_RE.finditer(line):
                value = match.group("value").upper()
                claims.append(
                    {
                        "topic": "size",
                        "sku": sku,
                        "kind": match.group("kind"),
                        "values": (value,),
                        "display": value,
                    }
                )
    return claims


def _consensus_group_name(group: dict) -> str:
    return (
        group["kind"]
        if group["sku"] == UNKNOWN_SKU
        else f"{group['sku']}{group['kind']}"
    )


def _consensus_group_sentence(group: dict) -> str:
    name = _consensus_group_name(group)
    if group["high"]:
        refs = "".join(f"[{n}]" for n in group["refs"])
        return f"多帖一致（{refs}）：{name} {group['value']}"
    if len(group["items"]) == 1:
        entry = group["items"][0]
        return f"{name}：[{entry['n']}] 写 {entry['display']}"
    parts = [f"[{entry['n']}] 写 {entry['display']}" for entry in group["items"]]
    return f"{name}：" + "，".join(parts)


# 归因只能用证据里读得到的东西：帖子正文/标题里的年月、包邮或二手这类口径词、
# 价格栏位与款式名。读不到就明说「无法判断」，绝不编团次或日期。
_DATE_RE = re.compile(r"20\d{2}\s*(?:年|[-/.])\s*(?:\d{1,2}\s*月?)?")
_BASIS_TOKENS = ("包邮", "不包邮", "含运费", "不含运费", "含税", "不含税", "二手", "转单")
_PRICE_KINDS = frozenset(
    {
        "价格", "全款", "售价", "定价", "原价", "现价",
        "预约", "现货", "定金", "尾款", "预约价", "现货价",
    }
)


def _evidence_hints(item: dict) -> dict:
    text = f"{item.get('title') or ''} {item.get('text') or ''}"
    dates: list[str] = []
    for match in _DATE_RE.finditer(text):
        token = re.sub(r"\s+", "", match.group())
        if token not in dates:
            dates.append(token)
    return {"dates": dates, "bases": [t for t in _BASIS_TOKENS if t in text]}


def _kind_priority(kind: str) -> int:
    """预约价是主口径，冲突说明先讲它，再讲现货与分期。"""
    return {
        "预约价": 0, "预约": 1, "现货价": 2, "现货": 3, "定金": 4, "尾款": 5,
    }.get(kind, 9)


def _tier_in_question(question: str) -> str:
    upper = (question or "").upper()
    for key, name in _CONSENSUS_SKU:
        if name != UNKNOWN_SKU and key.upper() in upper:
            return name
    return ""


def _explain_price_differences(
    question: str, evidence: list[dict], collected: dict
) -> list[str]:
    """同一维度出现两个以上不同数值时，说明差异来自款式、时效还是计价口径。"""
    hints = {
        int(item["n"]): _evidence_hints(item)
        for item in evidence
        if item.get("n") is not None
    }
    focus = _tier_in_question(question)
    same_post: dict[int, list[tuple[str, list[str]]]] = {}
    cross_notes: list[str] = []
    ordered = sorted(
        collected.items(), key=lambda kv: (_kind_priority(kv[0][1]), kv[0][0])
    )

    for (sku, kind), by_number in ordered:
        if kind not in _PRICE_KINDS:
            continue
        if focus and sku != focus:
            continue
        values: list[str] = []
        for entry in by_number.values():
            for value in entry["values"]:
                if value not in values:
                    values.append(value)
        if len(values) < 2:
            continue
        name = (
            kind
            if kind in ("定金", "尾款") or sku == UNKNOWN_SKU
            else f"{sku}{kind}"
        )
        if len(by_number) == 1:
            number = next(iter(by_number))
            same_post.setdefault(number, []).append((name, values))
            continue
        dated = [
            (number, date)
            for number in sorted(by_number)
            for date in hints.get(number, {}).get("dates", [])
        ]
        bases = [
            (number, base)
            for number in sorted(by_number)
            for base in hints.get(number, {}).get("bases", [])
        ]
        if len({date for _n, date in dated}) >= 2:
            shown = "、".join(f"[{n}] 提到 {d}" for n, d in dated[:3])
            cross_notes.append(
                f"{name}这两条来自不同帖子，能读到的时间是 {shown}，"
                "可能是换批改价；只凭帖子里的信息无法确定哪个是当前价格。"
            )
        elif len({base for _n, base in bases}) >= 2:
            shown = "、".join(f"[{n}] 写 {b}" for n, b in bases[:3])
            cross_notes.append(
                f"{name}这两条来自不同帖子，{shown}，计价口径不同，不能直接当同一个价。"
            )
        else:
            cross_notes.append(
                f"{name}这两条来自不同帖子，都没写团次或日期，"
                "无法判断哪个更新、哪个是当前价格。"
            )

    notes: list[str] = []
    for number, entries in same_post.items():
        shown = "；".join(
            f"{name} " + " 与 ".join(f"{value} 元" for value in values)
            for name, values in entries[:2]
        )
        notes.append(
            f"同一条帖子（[{number}]）里，{shown}，"
            "帖子里没写哪个是改价后的，无法判断哪个是当前价格。"
        )
    notes.extend(cross_notes)

    if not focus:
        # 同一条帖子按款式分了不同价：这不是矛盾，是不同 SKU，得说清楚。
        per_post: dict[tuple[int, str], list[tuple[str, str]]] = {}
        for (sku, kind), by_number in collected.items():
            if kind not in _PRICE_KINDS or sku == UNKNOWN_SKU:
                continue
            for number, entry in by_number.items():
                for value in entry["values"]:
                    per_post.setdefault((number, kind), []).append((sku, value))
        for (number, kind), pairs in per_post.items():
            if len({sku for sku, _v in pairs}) < 2 or len({v for _s, v in pairs}) < 2:
                continue
            shown = "、".join(
                f"{sku}{kind} {value} 元[{number}]" for sku, value in pairs[:3]
            )
            notes.append(
                f"同一条帖子里，{shown}——这是不同款式，价格本来就不同，"
                "不是同一档位改价；按你要的款式对号入座。"
            )
            break

    if notes:
        notes = notes[:2]
        notes.append("下单前按卖家当前页面或最新一帖确认。")
    return notes


def number_consensus(question: str, evidence: list[dict]) -> dict:
    """多帖数值比对。没有可比对象时返回空文本，调用方什么都不加。"""
    topics = _consensus_topics(question)
    result: dict = {"triggered": False, "topics": topics, "groups": [], "text": ""}
    if not topics:
        return result

    terms = _subject_terms(question)
    collected: dict[tuple[str, str], dict[int, dict]] = {}
    for item in evidence:
        if not _mentions_subject(item, terms):
            continue
        number = item.get("n")
        if number is None:
            continue
        try:
            number = int(number)
        except (TypeError, ValueError):
            continue
        for claim in _claims_from_item(item, topics):
            key = (claim["sku"], claim["kind"])
            entry = collected.setdefault(key, {}).setdefault(
                number,
                {
                    "values": [],
                    "displays": [],
                    "source_kind": item.get("source_kind") or "",
                },
            )
            for value in claim["values"]:
                if value not in entry["values"]:
                    entry["values"].append(value)
            if claim["display"] not in entry["displays"]:
                entry["displays"].append(claim["display"])

    groups: list[dict] = []
    for (sku, kind), by_number in collected.items():
        if len(by_number) < 2:
            continue
        items = []
        for number, entry in sorted(by_number.items()):
            display = "/".join(entry["displays"][:4])
            if len(entry["displays"]) > 4:
                display += "…"
            items.append(
                {
                    "n": number,
                    "values": list(entry["values"]),
                    "display": display,
                    "source_kind": entry["source_kind"],
                    "ocr": entry["source_kind"] == "image_text",
                }
            )
        counts: dict[str, list[int]] = {}
        for entry in items:
            for value in entry["values"]:
                refs = counts.setdefault(value, [])
                if entry["n"] not in refs:
                    refs.append(entry["n"])
        total = len(items)
        majority, majority_refs = max(counts.items(), key=lambda item: len(item[1]))
        # 高可信：同一数值 ≥3 条证据，且占可比条数的多数。两条相等不算。
        high = len(majority_refs) >= 3 and len(majority_refs) * 2 > total
        groups.append(
            {
                "sku": sku,
                "kind": kind,
                "consistent": len(counts) == 1,
                "high": high,
                "value": majority if high else "",
                "refs": majority_refs if high else [],
                "counts": [
                    {"value": value, "count": len(refs), "refs": refs}
                    for value, refs in counts.items()
                ],
                "items": items[:5],
            }
        )

    attributions = (
        _explain_price_differences(question, evidence, collected)
        if "price" in topics
        else []
    )
    if not groups and not attributions:
        return result
    groups.sort(key=lambda group: (not group["high"], group["sku"], group["kind"]))
    groups = groups[:3]

    label = (
        "价格" if topics == ["price"]
        else "尺码" if topics == ["size"]
        else "价格和尺码"
    )
    sentences = [_consensus_group_sentence(group) for group in groups]
    all_high = bool(groups) and all(group["high"] for group in groups)
    conflict = any(len(group["counts"]) > 1 for group in groups)
    if all_high:
        text = f"关于{label}，" + "；".join(sentences) + "。"
    elif conflict:
        text = f"关于{label}，几条帖子对不上：" + "；".join(sentences) + "。"
        text += "".join(attributions) or "分歧可能来自型号、时效、是否二手或是否含税。"
    elif groups:
        text = f"关于{label}，几条帖子的说法：" + "；".join(sentences) + "。"
    else:
        text = "".join(attributions)

    result["triggered"] = True
    result["groups"] = groups
    result["attributions"] = attributions
    result["text"] = text
    return result


def sanitize_history(raw) -> list[dict]:
    """只留最近若干轮，截断过长内容，丢掉角色不对的项。

    客户端传什么都可能——历史是**不可信输入**，而且直接进上下文（成本与安全都在这里）。

    assistant 文本里的 ``[n]`` 会在这里剥掉：编号只在单轮内有效，上一轮的 ``[1]``
    和本轮的 ``[1]`` 很可能是两条完全不同的证据。留着它等于给模型一个指向错误
    来源的把手，比没有编号更糟。这里选择**剥离**而不是改写：任何"来源见上一轮"
    的占位在本轮都无意义，模型只需照本轮【证据】重新标注。
    """
    if not raw:
        return []
    cleaned: list[dict] = []
    for turn in raw:
        if not isinstance(turn, dict):
            continue
        role = turn.get("role")
        content = str(turn.get("content") or "").strip()
        if role not in ("user", "assistant") or not content:
            continue
        if role == "assistant":
            content = _CITATION_RE.sub("", content)
            content = re.sub(r"[ \t]{2,}", " ", content).strip()
            if not content:
                continue
        cleaned.append({"role": role, "content": content[:MAX_HISTORY_CHARS]})
    return cleaned[-MAX_HISTORY_TURNS:]


class DeepSeekWriter:
    """答案写作。``write`` 一次性返回，``stream`` 逐段产出。"""

    def __init__(self, app: AppSettings):
        from openai import OpenAI

        self._client = OpenAI(
            api_key=app.llm_api_key.strip(),
            base_url=app.llm_base_url.strip(),
            timeout=WRITER_TIMEOUT,
        )
        self._model = app.llm_model.strip()
        # 上限放配置：推理模型的推理 token 也计入这个预算，600 会截断长答案。
        self._max_tokens = max(1, int(app.writer_max_tokens))
        self.usage = {
            "calls": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            # 每次调用的明细：分级路由可能让同一实例先后用不同模型，账本按它拆。
            "calls_detail": [],
        }

    @property
    def model(self) -> str:
        return self._model

    def _record(self, usage, model: str | None = None) -> None:
        prompt_tokens = 0
        completion_tokens = 0
        if usage is not None:
            prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
            completion_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
        self.usage["calls"] += 1
        self.usage["prompt_tokens"] += prompt_tokens
        self.usage["completion_tokens"] += completion_tokens
        self.usage["calls_detail"].append(
            {
                "role": "writer",
                "model": model or self._model,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
            }
        )

    def write(
        self,
        question: str,
        evidence: list[dict],
        history: list[dict] | None = None,
        *,
        model: str | None = None,
    ) -> str:
        picked = model or self._model
        response = self._client.chat.completions.create(
            model=picked,
            messages=build_synth_messages(question, evidence, history),
            temperature=0.2,
            max_tokens=self._max_tokens,
        )
        self._record(getattr(response, "usage", None), picked)
        text = (response.choices[0].message.content or "").strip()
        text, fixes = fix_source_attribution(text, evidence)
        # OCR 只保证字符、不保证列对齐：模型一旦把数字归纳成「预约价 390」，
        # 就换回图上原文行，避免把尾款当预约价。
        text, demotions = demote_ocr_price_labels(text, evidence)
        # 只有非流式能补引用：流式的字已经交付给用户，回头改等于打脸。
        text, filled, narrowed = fill_citations(text, evidence)
        consensus = number_consensus(question, evidence)
        if consensus["text"]:
            text = text.rstrip() + "\n\n" + consensus["text"]
        # 答案里不留「未校验 / 以原图为准」旁注；引用指回证据，用户可自查。
        text = clean_source_annotations(text)
        text = text.rstrip() + source_guide_suffix(text, evidence)
        text += image_hint(question, evidence)
        # 引导整条回答最多一次：模型自己写过一次就不再补，重复的删掉。
        text = _keep_one_guide(text)
        self.usage["diagnostics"] = _diagnostics_block(
            text,
            evidence,
            fixes,
            streamed=False,
            filled=filled,
            narrowed=narrowed,
            demotions=demotions,
            consensus=consensus,
        )
        return text

    def stream(
        self,
        question: str,
        evidence: list[dict],
        history: list[dict] | None = None,
        *,
        model: str | None = None,
    ) -> Iterator[str]:
        """逐段产出。**调用方负责拼接**——这里不缓存整段，否则就不是流式了。

        唯一保留的是诊断用的副本：只用来在收尾时算句级覆盖告警，不参与、
        也不延迟任何一次 ``yield``。流式**不改写答案**（改写等于用户读到一半
        被替换），归因后处理只走非流式。
        """
        picked = model or self._model
        consensus = number_consensus(question, evidence)
        stream = self._client.chat.completions.create(
            model=picked,
            messages=build_synth_messages(question, evidence, history),
            temperature=0.2,
            max_tokens=self._max_tokens,
            stream=True,
            stream_options={"include_usage": True},
        )
        seen: list[str] = []
        for chunk in stream:
            usage = getattr(chunk, "usage", None)
            if usage is not None:
                self._record(usage, picked)
            choices = getattr(chunk, "choices", None) or []
            if not choices:
                continue
            delta = getattr(choices[0].delta, "content", None)
            if delta:
                seen.append(delta)
                yield delta
        # 比对段放在模型输出之后、提示之前：不回改已输出的字。
        if consensus["text"]:
            block = "\n\n" + consensus["text"]
            seen.append(block)
            yield block
        # 引用了图上文字时，句末补一次自查引导；模型已经写过就不重复。
        guide = source_guide_suffix("".join(seen), evidence)
        if guide:
            seen.append(guide)
            yield guide
        # 提示放在流末尾一次给出：先让模型把能说的话说完，再补「图里可能有」。
        hint = image_hint(question, evidence)
        if hint and SOURCE_GUIDE in "".join(seen):
            # 引导整条回答最多一次：模型正文里已经写过，提示里那半句整段去掉。
            hint = re.sub(
                r"；[^；。]*" + re.escape(SOURCE_GUIDE) + "。", "。", hint
            )
        if hint:
            seen.append(hint)
            yield hint
        self.usage["diagnostics"] = _diagnostics_block(
            "".join(seen), evidence, [], streamed=True, consensus=consensus
        )
