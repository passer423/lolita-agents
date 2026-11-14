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

WRITER_MAX_TOKENS = 600
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
    "以下是网友发言、店家页面或网络实时内容。称呼要跟每条开头的标签走：\n"
    "「网友发言」→ 写「有网友提到」「出物的人说」；\n"
    "「店家/商家」→ 写「店家提到」「店铺页面写着」，绝不能写成「网友」；\n"
    "「档案（Lolibrary）」→ 社区整理的发售资料库，可能有错漏，写「档案记录」"
    "「Lolibrary 上记录」，不能写「官方」，也不能写「网友」；\n"
    "「官方」→ 品牌自有渠道，写「官方公告」「品牌官方说明」。\n"
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
# 「一整句事实没有编号」。这里只产出告警，**不改答案**——流式下替换等于
# 话说一半被打脸，非流式也没有必要为了告警牺牲可读性。
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


def uncited_claim_sentences(answer: str, evidence: list[dict]) -> list[str]:
    """≥15 字、句内没有 ``[n]``、也不属于豁免类型的句子。

    没有证据时不判：那时回答本来就该是拒答，要求引用是把正确答案判成错误。
    """
    if not answer or not evidence:
        return []
    flagged: list[str] = []
    for match in _SENTENCE_RE.finditer(answer):
        sentence = match.group().strip()
        if not sentence or _CITATION_RE.search(sentence):
            continue
        visible = _CITATION_RE.sub("", sentence).strip()
        if len(visible) < MIN_CLAIM_CHARS:
            continue
        if _exempt_sentence(sentence):
            continue
        flagged.append(sentence)
    return flagged


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
) -> dict:
    flagged = uncited_claim_sentences(answer, evidence)
    return {
        "streamed": streamed,
        "citation_coverage": {
            "checked_evidence": len(evidence),
            "uncited_count": len(flagged),
            "uncited_sentences": [sentence[:80] for sentence in flagged],
        },
        "source_attribution_fixes": fixes,
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
            max_tokens=WRITER_MAX_TOKENS,
        )
        self._record(getattr(response, "usage", None), picked)
        text = (response.choices[0].message.content or "").strip()
        text, fixes = fix_source_attribution(text, evidence)
        self.usage["diagnostics"] = _diagnostics_block(
            text, evidence, fixes, streamed=False
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
        stream = self._client.chat.completions.create(
            model=picked,
            messages=build_synth_messages(question, evidence, history),
            temperature=0.2,
            max_tokens=WRITER_MAX_TOKENS,
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
        self.usage["diagnostics"] = _diagnostics_block(
            "".join(seen), evidence, [], streamed=True
        )
