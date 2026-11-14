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
SOURCE_KIND_LABELS = {
    "own_content": "自有知识库",
    "third_party": "第三方档案",
    "realtime": "网络实时内容",
    "upstream_ugc": "网友发言",
}

# 能当规范的来源。其余一律只作线索——这是「一源一通道」在提示词层的落地：
# 通道层分开了，结果层又混成一锅，等于白分。
_AUTHORITATIVE_KINDS = frozenset({"own_content", "third_party"})

_CLUE_HEADER = (
    "【仅作线索·不得当作规范】\n"
    "以下是网友发言或网络实时内容。引用时写成「有网友提到」「网上看到」，"
    "不得作为洗护方式、尺码、材质、工艺的规范结论；与【可作依据】冲突时，以后者为准。"
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
   编号对应【证据】里的序号。**只能引用本轮【证据】里的编号**：历史对话不参与
   编号，其中的编号已失效并移除，不要沿用或猜测。
3. 价格、尺码、日期照证据原文说，别换算、别合并不同币种、别约等于。
4. 证据里没有、或者只沾一点边，就直接说「这个我这边没查到」「资料里没写」，
   可以顺带告诉用户换个什么问法；但不许硬编一个答案。
5. 证据互相打架时，说清楚两边分别怎么讲，别替用户拍板。
6. 证据分两类：【可作依据】是自有知识库与官方档案，可以直接陈述；
   【仅作线索】是网友发言和网络内容，只能写成「有网友提到」「网上看到」，
   不能当规范。两者冲突时以【可作依据】为准。
"""


def _evidence_line(item: dict) -> str:
    """一条证据：``[n]（效力标签）标题：正文（来源 url）``。

    标签必须在**每条**上都出现，而不是只在分块标题上说一次——模型引用时看的是
    那一条，块标题离得远，容易被忽略。
    """
    label = SOURCE_KIND_LABELS.get(item.get("source_kind"), "来源")
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
        return (response.choices[0].message.content or "").strip()

    def stream(
        self,
        question: str,
        evidence: list[dict],
        history: list[dict] | None = None,
        *,
        model: str | None = None,
    ) -> Iterator[str]:
        """逐段产出。**调用方负责拼接**——这里不缓存整段，否则就不是流式了。"""
        picked = model or self._model
        stream = self._client.chat.completions.create(
            model=picked,
            messages=build_synth_messages(question, evidence, history),
            temperature=0.2,
            max_tokens=WRITER_MAX_TOKENS,
            stream=True,
            stream_options={"include_usage": True},
        )
        for chunk in stream:
            usage = getattr(chunk, "usage", None)
            if usage is not None:
                self._record(usage, picked)
            choices = getattr(chunk, "choices", None) or []
            if not choices:
                continue
            delta = getattr(choices[0].delta, "content", None)
            if delta:
                yield delta
