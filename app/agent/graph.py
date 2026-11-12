"""多路召回的编排层：用 LangGraph 的 ``StateGraph`` 替代手写循环。

## 为什么从 loop.py 换成图

多路召回（ADR-0001）的核心是**并行扇出 + 条件补查**，这是一张有向图，不是一条
线性循环：

* 核心通道（own / real / lolibrary）**每次提问都并行召回**；
* 召回结果合并后，planner 做**一次**判断：要不要追加实时通道（xhs / weibo / web）；
* 追加的实时通道**再并行召回**，最后统一交给 writer 生成回答。

手写这套并行/条件逻辑（loop.py 的生成器）要自己管理扇出、等待、状态合并；
LangGraph 的 ``Send`` 扇出 + reducer 合并 + 条件边正好是它的原生能力。

## 护栏迁移（原 loop.py 的四条，逐条对应）

| 原护栏 | 迁移方式 |
|---|---|
| ``MAX_STEPS`` 步数硬上限 | 图的 ``recursion_limit``（每条边最多走一次，这是兜底上限） |
| ``MAX_EMPTY`` 连续空即停 | 图里**没有回边**，每个通道最多召回一次、空结果不重试 |
| 没证据不许作答 | ``answer`` 节点：无证据直接出拒答文案 |
| 引用越界即降级 | ``verify`` 节点：引用对不上就把回答换成逐字摘录 |

## 流式与图的关系

``answer`` / ``verify`` 也是图节点，但**流式路径**（``stream()``）会让它们跳过写答案：
写作需要真流式（逐段产出），而同步图节点里塞 generator 不干净。所以：

* 非流式：整张图跑完（含 answer/verify），``run()`` 直接取 ``state["answer"]``；
* 流式：图只跑到「召回 + 补查决策」结束（``stream=True`` 时 answer/verify 空转），
  再由 ``stream()`` 拿证据去 ``writer.stream``，并复用同一套 ``citation_problems`` 校验。

两种路径共用同一张图的召回编排，不会出现"网页和命令行行为不一致"。
"""

from __future__ import annotations

import logging
import operator
import re
import time
from collections.abc import Iterator
from typing import Annotated, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from app.agent import channels, trace
from app.agent.channels import ALL_CHANNELS, ChannelUnavailable, RecallContext
from app.agent.planner import PlannerOutputError
from app.agent.writer import sanitize_history

logger = logging.getLogger(__name__)

# 兜底上限：fanout_core(1) + 核心通道(3) + route(1) + fanout_supplement(1)
# + 补查通道(≤3) + answer(1) + verify(1) ≈ 11。留一倍余量。
MAX_RECURSION = 24

# 拒答识别要覆盖口语说法：回答本身是"没查到"时不该被当成"漏标来源"而整段降级。
_REFUSAL_MARKERS = (
    "没有找到", "没找到", "没查到", "查不到", "没有相关", "没有资料",
    "资料中没有", "暂无", "没有匹配", "没有这方面", "不清楚", "说不准",
)

_CHANNEL_ORDER = {name: index for index, name in enumerate(ALL_CHANNELS)}


class AgentState(TypedDict, total=False):
    question: str
    history: list[dict]
    # 多路召回的证据，各通道用 reducer 追加；顺序由 _normalize 在收尾时按通道定序。
    evidence: Annotated[list[dict], operator.add]
    steps: Annotated[list[dict], operator.add]
    recalled: Annotated[list[str], operator.add]
    # planner 在 route 节点写入：要追加的实时通道 + 精简检索词（非 reducer，整值覆盖）
    supplementary: list[str]
    query: str
    answer: str
    stopped_reason: str
    # 流式路径标记：answer/verify 空转，答案交给 stream() 去 writer.stream
    stream: bool


def _normalize(evidence: list[dict]) -> list[dict]:
    """按通道固定顺序排列证据，让 ``[n]`` 编号在多次运行间稳定。

    各通道节点返回的整段列表被 reducer 按完成顺序拼接，导致跨运行顺序漂移；
    这里按通道顺序稳定排序（Python sort 稳定，同通道内原顺序保留）。
    """
    return sorted(evidence, key=lambda item: _CHANNEL_ORDER.get(item.get("channel"), 99))


def _numbered(evidence: list[dict]) -> list[dict]:
    return [{"n": offset + 1, **item} for offset, item in enumerate(evidence)]


def _forced_answer(evidence: list[dict], *, reason: str) -> str:
    if not evidence:
        return (
            "没有检索到能回答这个问题的内容。我不猜——可以换个问法，"
            "或者先把相关的知识库条目补进来。"
        )
    header = {
        "citation_out_of_range": "（模型给的引用编号对不上，下面直接列原文，不做归纳）",
        "no_citation": "（模型的回答没有标注来源，下面直接列原文）",
        "no_evidence": "（没有检索到可用的证据，下面列出所有召回内容供参考）",
    }.get(reason, "（下面是查到的事实）")
    lines = [header, ""]
    for index, item in enumerate(evidence, start=1):
        lines.append(f"[{index}] {item.get('title', '')}：{item.get('text', '')}")
    return "\n".join(lines)


def citation_problems(answer: str, evidence_count: int) -> list[str]:
    """回答里的 ``[n]`` 必须落在 1..证据条数 内；有证据却不标引用也算问题。

    "有证据却没标引用"只在回答**不像拒答**时才判为问题——模型说"没找到"时
    本来就不该有引用，硬要求会把它本来正确的拒答判成错误。
    """
    problems: list[str] = []
    tokens = re.findall(r"\[(\d+)\]", answer or "")
    for token in tokens:
        if not 1 <= int(token) <= evidence_count:
            problems.append(f"引用 [{token}] 越界（只有 {evidence_count} 条证据）")
    if evidence_count and not tokens and not any(mark in (answer or "") for mark in _REFUSAL_MARKERS):
        problems.append("有证据但回答没有标注来源")
    return problems


# --------------------------------------------------------------------- 图


def build_graph(planner, writer, ctx: RecallContext, *, stream: bool = False):
    """按 planner / writer / ctx 构造编译好的图。

    ``stream=True`` 时 answer / verify 空转，答案交给调用方流式写作。
    """

    def _channel_node(name: str):
        def recall_one(state: AgentState) -> dict:
            question = state["question"]
            query = state.get("query") or question
            started = time.perf_counter()
            try:
                evidence = channels.recall(name, question, ctx, query=query)
                error = ""
            except ChannelUnavailable as exc:
                evidence = []
                error = str(exc)
            elapsed_ms = int((time.perf_counter() - started) * 1000)

            if error:
                step = {
                    "kind": "skip",
                    "channel": name,
                    "reason": error[:160],
                    "ms": elapsed_ms,
                }
            else:
                step = {
                    "kind": "recall",
                    "channel": name,
                    "empty": not evidence,
                    "evidence": len(evidence),
                    "ms": elapsed_ms,
                }
            return {"evidence": evidence, "steps": [step], "recalled": [name]}

        return recall_one

    def fanout_core(state: AgentState) -> list[Send]:
        return [Send(name, state) for name in channels.CORE_CHANNELS]

    def route(state: AgentState) -> dict:
        core = _normalize(state.get("evidence", []))
        try:
            plan = planner.decide(state["question"], core, state.get("history"))
        except PlannerOutputError as exc:
            logger.warning("补查决策失败，退回仅核心通道：%s", exc)
            return {
                "supplementary": [],
                "query": "",
                "steps": [{"kind": "route", "supplementary": [], "query": "", "error": str(exc)[:160]}],
            }
        supplementary = [name for name in plan["supplementary"] if name in channels.SUPPLEMENTARY_CHANNELS]
        return {
            "supplementary": supplementary,
            "query": plan["query"],
            "steps": [{"kind": "route", "supplementary": supplementary, "query": plan["query"]}],
        }

    def fanout_after_route(state: AgentState) -> list[Send]:
        """route 之后的条件边：有补查就 Send 到实时通道，否则直接 Send 到 answer。

        注意：``Send`` 只能在**条件边函数**里返回（节点里返回会报 InvalidUpdateError），
        所以这里不是图节点，而是 ``add_conditional_edges`` 的 path 函数。
        """
        names = [name for name in state.get("supplementary", []) if name in channels.SUPPLEMENTARY_CHANNELS]
        if not names:
            return [Send("answer", state)]
        return [Send(name, state) for name in names]

    def answer(state: AgentState) -> dict:
        if state.get("stream"):
            return {}
        evidence = _normalize(state.get("evidence", []))
        if not evidence:
            return {"answer": _forced_answer([], reason="no_evidence"), "stopped_reason": "no_evidence"}
        if writer is None:
            return {"answer": _forced_answer(evidence, reason="no_writer")}
        try:
            text = writer.write(state["question"], _numbered(evidence), state.get("history"))
        except Exception as exc:  # noqa: BLE001 - 写作失败不该让整轮白跑
            logger.warning("写作失败，退回原文清单：%s", exc)
            text = _forced_answer(evidence, reason="writer_failed")
        return {"answer": text}

    def verify(state: AgentState) -> dict:
        if state.get("stream"):
            return {}
        evidence = _normalize(state.get("evidence", []))
        text = state.get("answer", "")
        problems = citation_problems(text, len(evidence))
        if not problems:
            return {}
        reason = "citation_out_of_range" if any("越界" in item for item in problems) else "no_citation"
        return {"answer": _forced_answer(evidence, reason=reason), "stopped_reason": reason}

    graph = StateGraph(AgentState)
    for name in ALL_CHANNELS:
        graph.add_node(name, _channel_node(name))
    graph.add_node("route", route)
    graph.add_node("answer", answer)
    graph.add_node("verify", verify)

    # 核心通道：START 并行扇出到 own / real / lolibrary
    graph.add_conditional_edges(START, fanout_core, list(channels.CORE_CHANNELS))
    for name in channels.CORE_CHANNELS:
        graph.add_edge(name, "route")
    # route 之后：有补查就追加实时通道，否则直接去 answer
    graph.add_conditional_edges("route", fanout_after_route, [*channels.SUPPLEMENTARY_CHANNELS, "answer"])
    for name in channels.SUPPLEMENTARY_CHANNELS:
        graph.add_edge(name, "answer")
    graph.add_edge("answer", "verify")
    graph.add_edge("verify", END)

    return graph.compile()


# --------------------------------------------------------------------- 收尾


def _merge_usage(planner, writer) -> dict:
    usage = dict(getattr(planner, "usage", {}) or {})
    if writer is not None and getattr(writer, "usage", None):
        usage["writer"] = writer.usage
    return usage


def _finish(
    question: str,
    state: AgentState,
    *,
    ctx: RecallContext,
    planner,
    writer,
    history: list[dict] | None,
    duration_ms: int,
) -> dict:
    """把图的结果收尾成完整响应（编号、轨迹）。"""
    evidence = _normalize(state.get("evidence", []))
    payload = {
        "question": question,
        "answer": state.get("answer", ""),
        "refused": not evidence,
        "evidence": _numbered(evidence),
        "steps": state.get("steps", []),
        "stopped_reason": state.get("stopped_reason", ""),
        "usage": _merge_usage(planner, writer),
        "duration_ms": duration_ms,
    }
    trace.append(
        {
            "question": question,
            "answer": payload["answer"][:400],
            "refused": payload["refused"],
            "stopped_reason": payload["stopped_reason"],
            "steps": payload["steps"],
            "evidence": len(evidence),
            "usage": payload["usage"],
            "duration_ms": duration_ms,
            "history_turns": len(history or []),
        },
        ctx.settings,
    )
    return payload


def run(
    question: str,
    *,
    ctx: RecallContext,
    planner,
    writer=None,
    history: list[dict] | None = None,
) -> dict:
    """非流式：跑完整张图，返回最终响应。"""
    question = (question or "").strip()
    if not question:
        raise ValueError("问题不能为空")

    started = time.perf_counter()
    graph = build_graph(planner, writer, ctx, stream=False)
    state = graph.invoke(
        {
            "question": question,
            "history": sanitize_history(history),
            "evidence": [],
            "steps": [],
            "recalled": [],
            "stream": False,
        },
        config={"recursion_limit": MAX_RECURSION},
    )
    duration_ms = int((time.perf_counter() - started) * 1000)
    return _finish(
        question, state, ctx=ctx, planner=planner, writer=writer,
        history=history, duration_ms=duration_ms,
    )


def stream(
    question: str,
    *,
    ctx: RecallContext,
    planner,
    writer=None,
    history: list[dict] | None = None,
) -> Iterator[dict]:
    """流式：先吐 step 事件（各通道召回 + 补查决策），再吐答案增量，最后吐 final。

    事件类型：``step`` / ``delta`` / ``replace`` / ``final`` / ``error``。
    """
    question = (question or "").strip()
    if not question:
        yield {"type": "error", "detail": "问题不能为空"}
        return

    started = time.perf_counter()
    graph = build_graph(planner, writer, ctx, stream=True)
    state = graph.invoke(
        {
            "question": question,
            "history": sanitize_history(history),
            "evidence": [],
            "steps": [],
            "recalled": [],
            "stream": True,
        },
        config={"recursion_limit": MAX_RECURSION},
    )
    duration_ms = int((time.perf_counter() - started) * 1000)

    for step in state.get("steps", []):
        yield {"type": "step", **step}

    evidence = _normalize(state.get("evidence", []))
    numbered = _numbered(evidence)

    if evidence and writer is not None:
        chunks: list[str] = []
        try:
            for delta in writer.stream(question, numbered, sanitize_history(history)):
                chunks.append(delta)
                yield {"type": "delta", "text": delta}
        except Exception as exc:  # noqa: BLE001
            logger.warning("流式写作失败：%s", exc)
            fallback = _forced_answer(evidence, reason="writer_failed")
            yield {"type": "replace", "text": fallback, "reason": "writer_failed"}
            text = fallback
        else:
            text = "".join(chunks)
    else:
        text = _forced_answer(evidence, reason="no_evidence")
        yield {"type": "delta", "text": text}

    problems = citation_problems(text, len(evidence))
    if problems:
        reason = "citation_out_of_range" if any("越界" in item for item in problems) else "no_citation"
        fixed = _forced_answer(evidence, reason=reason)
        yield {"type": "replace", "text": fixed, "reason": reason, "problems": problems}
        text = fixed
        state["stopped_reason"] = ";".join(filter(None, [state.get("stopped_reason", ""), reason]))

    payload = {
        "question": question,
        "answer": text,
        "refused": not evidence,
        "evidence": numbered,
        "steps": state.get("steps", []),
        "stopped_reason": state.get("stopped_reason", ""),
        "usage": _merge_usage(planner, writer),
        "duration_ms": duration_ms,
    }
    trace.append(
        {
            "question": question,
            "answer": text[:400],
            "refused": payload["refused"],
            "stopped_reason": payload["stopped_reason"],
            "steps": payload["steps"],
            "evidence": len(evidence),
            "usage": payload["usage"],
            "duration_ms": duration_ms,
            "streamed": True,
            "history_turns": len(history or []),
        },
        ctx.settings,
    )
    yield {"type": "final", **payload}
