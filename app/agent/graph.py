"""多路召回的编排层：用 LangGraph 的 ``StateGraph`` 替代手写循环。

## 为什么从 loop.py 换成图

多路召回（ADR-0001）的核心是**并行扇出 + 条件补查**，这是一张有向图，不是一条
线性循环：

* 核心通道（own / real / lolibrary）**每次提问都并行召回**；
* 召回结果合并后，按**问题意图**（关键词规则）决定要不要追加实时通道
  （xhs / weibo / web）；模型只负责把问题压成检索词，不参与「查不查」；
* 追加的实时通道**再并行召回**，最后统一交给 writer 生成回答。

手写这套并行/条件逻辑（loop.py 的生成器）要自己管理扇出、等待、状态合并；
LangGraph 的 ``Send`` 扇出 + reducer 合并 + 条件边正好是它的原生能力。

## 护栏迁移（原 loop.py 的四条，逐条对应）

| 原护栏 | 迁移方式 |
|---|---|
| ``MAX_STEPS`` 步数硬上限 | 图的 ``recursion_limit``（每条边最多走一次，这是兜底上限） |
| ``MAX_EMPTY`` 连续空即停 | 图里**没有回边**，每个通道最多召回一次、空结果不重试 |
| 没证据不许作答 | ``answer`` 节点：无证据直接出拒答文案 |
| 引用越界即降级 | ``verify`` 节点：引用对不上就把回答换成逐字摘录（仅非流式；流式改为前端标红，见下） |

## 流式与图的关系

``answer`` / ``verify`` 也是图节点，但**流式路径**（``stream()``）会让它们跳过写答案：
写作需要真流式（逐段产出），而同步图节点里塞 generator 不干净。所以：

* 非流式：整张图跑完（含 answer/verify），``run()`` 直接取 ``state["answer"]``；
* 流式：图只跑到「召回 + 补查决策」结束（``stream=True`` 时 answer/verify 空转），
  再由 ``stream()`` 拿证据去 ``writer.stream``，并复用同一套 ``citation_problems`` 校验。

两种路径共用同一张图的召回编排，不会出现"网页和命令行行为不一致"。

``stream()`` 不等整张图跑完：图在后台线程里跑，各节点一完成就往 ``_Progress``
的队列里塞 step，主线程边收边转成 ``step`` 事件。召回阶段因此能逐通道显示状态，
而不是先空白几十秒再一次性补上。``stop`` 事件（客户端点「停止」）在下一个
chunk 边界收尾：召回循环与写作循环都会检查它，不再继续消费。

## 整体预算：到点就交卷

单通道其实都有超时（搜索的 urlopen、小红书子进程、LLM 客户端），但把它们串起来，
最坏仍可能接近两分钟。所以 ``run()`` / ``stream()`` 外面再套一层**整体 deadline**
（``AGENT_DEADLINE``，默认 60s）：到点不再等剩下的通道，把**已经查到的证据**交出去，
在 steps 里标一条 ``timeout``，并在回答开头说明「部分通道超时」——不是直接 500。

不能拿图的状态当"部分结果"：LangGraph 的并行节点要**整步结束**才交出状态，
xhs 卡住时，同一步里已经返回的 web 证据会被一起扣住。所以通道节点一返回就回填
:class:`_Progress`，超时时从那里取局部结果。

**取舍（重要）**：``graph.invoke()`` 是同步阻塞的，Python 没法从线程外面取消它。
超时后工作线程会继续跑到各通道自己的超时收尾——也就是"线程泄漏"。本服务是
本地单机、一次问答一个线程：泄漏的线程不写响应、不影响后续请求，最坏只是晚几秒
自然结束，可以接受；若将来做多租户 / 高并发，必须换成可取消的执行方式
（子进程或原生 async），不能照搬。
"""

from __future__ import annotations

import logging
import operator
import queue
import re
import threading
import time
from collections.abc import Iterator
from typing import Annotated, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from app.agent import channels, cost, dress, routing, trace
from app.agent.channels import ALL_CHANNELS, ChannelUnavailable, RecallContext
from app.agent.planner import PlannerOutputError, plan_channels, plan_intents
from app.agent.writer import sanitize_history

logger = logging.getLogger(__name__)

# 兜底上限：dress_resolve(1) + fanout_core(1) + 核心通道(3) + route(1)
# + fanout_after_route(1) + 补查通道(≤3) + dress_profile(1) + answer(1) + verify(1) ≈ 13。
# 留一倍余量。
MAX_RECURSION = 24

# 拒答识别要覆盖口语说法：回答本身是"没查到"时不该被当成"漏标来源"而整段降级。
_REFUSAL_MARKERS = (
    "没有找到", "没找到", "没查到", "查不到", "没有相关", "没有资料",
    "资料中没有", "暂无", "没有匹配", "没有这方面", "不清楚", "说不准",
)

_CHANNEL_ORDER = {name: index for index, name in enumerate(ALL_CHANNELS)}

# 证据效力等级 → 排序权重。自有内容在前，网友发言在后。
_KIND_ORDER = {"own_content": 0, "third_party": 1, "realtime": 2, "upstream_ugc": 3}

# 操作型问题（怎么洗 / 要不要细节图 / 闲鱼收）要的是踩过坑的人怎么说，
# 不是规范词条怎么定义。此时把 UGC 整体提到自有内容与档案前面。
_KIND_ORDER_OPERATIONAL = {
    "realtime": 0,
    "upstream_ugc": 1,
    "own_content": 2,
    "third_party": 3,
}

# 意图标签由 ``channels.recall`` 打在证据上：意图是问题级属性，但合并层只拿到
# 证据列表（评测也复用同一个 ``_normalize``，不能改成额外传问题）。
_INTENT_ORDERS = {"operational": _KIND_ORDER_OPERATIONAL}


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
    # 裙子档案：dress_resolve 写入识别结果，dress_profile 写入整合后的字段。
    dress_query: dict
    dress_profile: dict
    dress_unresolved: bool
    dress_unverified: bool
    answer: str
    stopped_reason: str
    # 流式路径标记：answer/verify 空转，答案交给 stream() 去 writer.stream
    stream: bool


def _evidence_key(item: dict) -> str:
    """去重键：优先 URL，其次 ref。

    URL 去掉锚点与末尾斜杠——同一篇文章被不同通道抓到时常只差这两处。
    两个字段都没有（例如 Lolibrary 档案里的聚合条目）就不去重：没有稳定
    标识时强行合并会误删不同条目。
    """
    url = str(item.get("url") or "").strip()
    if url:
        return url.split("#", 1)[0].rstrip("/")
    return str(item.get("ref") or "").strip()


def _intent_of(evidence: list[dict]) -> str:
    for item in evidence:
        intent = str(item.get("intent") or "")
        if intent:
            return intent
    return ""


def _rank(
    item: dict, extra: set[str], kind_order: dict[str, int]
) -> tuple[int, int, int]:
    return (
        0 if item.get("channel") in extra else 1,
        kind_order.get(item.get("source_kind"), 9),
        _CHANNEL_ORDER.get(item.get("channel"), 99),
    )


def _normalize(
    evidence: list[dict],
    supplementary: list[str] | tuple[str, ...] = (),
    limit: int = 0,
) -> list[dict]:
    """去重、排序并限量，让 ``[n]`` 编号既稳定、又把「为这个问题才去查的」放在最前。

    排序是三级规则：

    1. **本次补查的通道优先** —— 用户问价格时，小红书的实拍和网页的现货页，
       比知识库里的术语解释更该先看到；把它们统一压到末尾，等于用编号位置
       告诉读者「这条不重要」，而它恰恰是唯一回答问题的。
    2. **意图 + 来源效力** —— 默认自有内容 > 第三方档案 > 实时内容 > 网友发言；
       **操作型问题反过来**（UGC > 自有 > 档案）：问「怎么判断山正」时，
       具体求助帖比泛化的词条更贴近问题。
    3. **通道固定顺序** —— 同前两级相同时按通道定序，消除并行完成顺序的漂移。

    这是**规则排序，不是相关性排序**：各通道的分数不可比（BM25 / 向量相似度 /
    搜索排序是三套量纲），跨通道没法打一个统一的分。问题意图由召回层写在
    证据的 ``intent`` 字段上（``channels.question_intent``），所以评测复用
    本函数时也走同一套权重。

    去重按 URL/ref，保留排序更靠前的版本（来源效力更强的留下）。``limit > 0``
    时限制总条数，但补查通道**整体豁免**：它们是用户这个问题才触发的，不能被
    核心通道的长列表挤掉；豁免后允许略超上限。
    """
    extra = set(supplementary or ())
    kind_order = _INTENT_ORDERS.get(_intent_of(evidence), _KIND_ORDER)

    best: dict[str, dict] = {}
    anonymous: list[dict] = []
    for item in evidence:
        key = _evidence_key(item)
        if not key:
            anonymous.append(item)
            continue
        current = best.get(key)
        if current is None or _rank(item, extra, kind_order) < _rank(
            current, extra, kind_order
        ):
            best[key] = item

    ordered = sorted(
        list(best.values()) + anonymous,
        key=lambda item: _rank(item, extra, kind_order),
    )
    if limit <= 0 or len(ordered) <= limit:
        return ordered

    head = [item for item in ordered if item.get("channel") in extra]
    rest = [item for item in ordered if item.get("channel") not in extra]
    return head + rest[: max(limit - len(head), 0)]


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


def _dress_clarification(question: str) -> str:
    asked = (question or "").strip()
    if len(asked) > 40:
        asked = asked[:40] + "…"
    return (
        f"没认出「{asked}」是哪条裙子：本地别名、Lolibrary 档案和模型都没给出可核实的系列名。"
        "能补充品牌（日牌/国牌）、日文或英文原名，或者货号吗？"
    )


def _dress_unverified_answer(profile: dict) -> str:
    brand = profile.get("brand") or "未识别品牌"
    title = profile.get("title") or profile.get("title_cn") or "未识别系列"
    return (
        f"我猜可能是 {brand} 的「{title}」，但 Lolibrary 档案里没核实到这条，"
        "所以先不把价格、发售年份这些当事实。能补充品牌、日文/英文原名或货号吗？"
    )


def _citation_tokens(answer: str) -> list[int]:
    return [int(token) for token in re.findall(r"\[(\d+)\]", answer or "")]


def invalid_citations(answer: str, evidence_count: int) -> list[int]:
    """越界的引用编号（去重、升序）。流式路径用它**就地标记**，而不是整段替换。"""
    return sorted({n for n in _citation_tokens(answer) if not 1 <= n <= evidence_count})


def citation_problems(answer: str, evidence_count: int) -> list[str]:
    """回答里的 ``[n]`` 必须落在 1..证据条数 内；有证据却不标引用也算问题。

    "有证据却没标引用"只在回答**不像拒答**时才判为问题——模型说"没找到"时
    本来就不该有引用，硬要求会把它本来正确的拒答判成错误。
    """
    problems: list[str] = []
    tokens = _citation_tokens(answer)
    for token in invalid_citations(answer, evidence_count):
        problems.append(f"引用 [{token}] 越界（只有 {evidence_count} 条证据）")
    if evidence_count and not tokens and not any(mark in (answer or "") for mark in _REFUSAL_MARKERS):
        problems.append("有证据但回答没有标注来源")
    return problems


# --------------------------------------------------------------------- 整体预算


class _DeadlineExceeded(RuntimeError):
    """后台迭代在整体预算内没有交回下一段。"""


class _StopRequested(RuntimeError):
    """客户端已请求停止：在下一个 chunk 边界收尾。"""


# 队列里的收尾哨兵。放在模块层，identity 比较不受 pickle / 相等性影响。
_PROGRESS_DONE = object()

# 等进度事件的最长间隔。也是检查 stop 与 deadline 的粒度（0.25s 足够跟手）。
_POLL_SECONDS = 0.25


class _Progress:
    """线程安全的部分进度：通道/规划一返回就记账，供整体超时时接管。

    图的状态不能当"部分结果"：LangGraph 的并行节点要**整步结束**才交出状态，
    xhs 卡住时，同一步里已经返回的 web 证据也会被一起扣住。所以节点在返回前
    直接回填这里，超时时就能拿到"已经查到的"。

    ``events=True`` 时同一次记账还会塞进一个队列：流式路径据此**边跑边吐**
    step，而不是等整张图结束再一次性补。记账与入队在同一把锁里完成，
    队列顺序与 ``snapshot()["steps"]`` 完全一致，超时后用"已吐出多少条"
    就能接着补剩下的，不会重复。
    """

    def __init__(self, *, events: bool = False) -> None:
        self._lock = threading.Lock()
        self._queue: queue.Queue | None = queue.Queue() if events else None
        self._evidence: list[dict] = []
        self._steps: list[dict] = []
        self._recalled: list[str] = []
        self._supplementary: list[str] = []
        self._routed = False
        self._dress_query: dict = {}
        self._dress_profile: dict = {}

    def _record(self, steps: list[dict]) -> None:
        """记账并（可选）入队。调用方必须已持有锁。"""
        self._steps.extend(steps)
        if self._queue is not None:
            for step in steps:
                self._queue.put(step)

    def channel_done(self, name: str, evidence: list[dict], step: dict) -> None:
        with self._lock:
            self._evidence.extend(evidence)
            self._recalled.append(name)
            self._record([step])

    def dress_done(self, query: dict, step: dict) -> None:
        with self._lock:
            self._dress_query = dict(query or {})
            self._record([step])

    def profile_done(self, profile: dict, evidence: list[dict], step: dict) -> None:
        with self._lock:
            self._dress_profile = dict(profile or {})
            self._evidence.extend(evidence)
            self._record([step])

    def route_done(
        self, supplementary: list[str], step: dict, extra_steps: list[dict] | None = None
    ) -> None:
        with self._lock:
            self._supplementary = list(supplementary)
            self._routed = True
            self._record([*(extra_steps or []), step])

    def close(self) -> None:
        """图跑完了（或已失败）：叫醒等事件的一方。"""
        if self._queue is not None:
            self._queue.put(_PROGRESS_DONE)

    def next_step(self, timeout: float):
        """取下一个 step；超时抛 ``queue.Empty``。只给 ``events=True`` 用。"""
        if self._queue is None:
            raise RuntimeError("这个 _Progress 没有开事件队列")
        return self._queue.get(timeout=timeout)

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "evidence": list(self._evidence),
                "steps": list(self._steps),
                "recalled": list(self._recalled),
                "supplementary": list(self._supplementary),
                "routed": self._routed,
                "dress_query": dict(self._dress_query),
                "dress_profile": dict(self._dress_profile),
            }


def _deadline_of(ctx: RecallContext) -> float:
    """整体预算（秒）。<=0 表示不设上限（测试 / 调试）。"""
    return float(getattr(ctx.settings, "agent_deadline", 0) or 0)


def _timeout_step(snapshot: dict, deadline_s: float) -> dict:
    """超时证据：到哪一步为止、还差哪些通道，前端和轨迹都靠它解释"为什么不全"。"""
    recalled = set(snapshot.get("recalled", []))
    planned = [*channels.CORE_CHANNELS, *(snapshot.get("supplementary") or [])]
    pending = [name for name in planned if name not in recalled]
    if pending:
        phase = "recall"
    elif snapshot.get("routed"):
        phase = "answer"
    else:
        phase = "route"
    return {
        "kind": "timeout",
        "phase": phase,
        "pending": pending,
        "deadline_s": deadline_s,
        "reason": f"整体预算 {deadline_s:.0f}s 用尽，先返回已查到的内容",
    }


def _timeout_answer(evidence: list[dict], deadline_s: float) -> str:
    """超时时的兜底回答：已查到的证据 + 「部分通道超时」说明，不假装查全了。"""
    if not evidence:
        return (
            f"这次在 {deadline_s:.0f} 秒的整体预算内没等到可用的召回结果（部分通道超时），"
            "已经中止，没有继续硬等。可以换个问法或稍后再问。"
        )
    lines = [
        f"（这次超过 {deadline_s:.0f} 秒的整体预算，部分通道超时，没有等全部结果；"
        "下面只列已经查到的内容）",
        "",
    ]
    for index, item in enumerate(evidence, start=1):
        lines.append(f"[{index}] {item.get('title', '')}：{item.get('text', '')}")
    return "\n".join(lines)


def _partial_state(
    question: str,
    progress: _Progress,
    deadline_s: float,
    *,
    ctx: RecallContext,
) -> AgentState:
    """超时后把进度快照拼成一份"像样的"状态，交给共用的收尾逻辑。"""
    snapshot = progress.snapshot()
    evidence = list(snapshot["evidence"])
    steps = [*snapshot["steps"], _timeout_step(snapshot, deadline_s)]
    dress_profile = snapshot.get("dress_profile") or {}
    if snapshot.get("dress_query") and not dress_profile:
        # 到点还没跑整合节点：只用本地档案拼一份「品牌/地区 + 未查到」的卡片，
        # 不再发起在线请求（预算已经用完）。
        try:
            result = dress.build_profile(snapshot["dress_query"], evidence, ctx, online=False)
        except Exception as exc:  # noqa: BLE001 - 兜底失败不影响超时响应
            logger.warning("超时后整合裙子档案失败：%s", exc)
            result = {}
        if result:
            dress_profile = result["profile"]
            evidence.append(result["evidence"])
            steps.insert(
                -1,
                {
                    "kind": "dress_profile",
                    "brand": dress_profile.get("brand", ""),
                    "region": dress_profile.get("region", ""),
                    "timeout": True,
                },
            )
    normalized = _normalize(
        evidence,
        snapshot["supplementary"],
        limit=ctx.settings.evidence_max_items,
    )
    return {
        "question": question,
        "evidence": evidence,
        "steps": steps,
        "recalled": snapshot["recalled"],
        "supplementary": snapshot["supplementary"],
        "dress_query": snapshot.get("dress_query") or {},
        "dress_profile": dress_profile,
        "answer": _timeout_answer(normalized, deadline_s),
        "stopped_reason": "deadline",
    }


def _invoke_with_deadline(
    graph, inputs: dict, timeout_s: float | None
) -> tuple[dict | None, bool]:
    """跑整张图，最多等 ``timeout_s`` 秒；返回 ``(state, timed_out)``。

    超时时 ``state`` 为 None，调用方改用 :class:`_Progress` 的局部快照。

    ``timeout_s`` 为 None 表示不限制。超时后工作线程无法从外部取消，会继续跑到
    各通道自己的超时收尾（线程泄漏）——本地单机可接受，理由见模块 docstring。
    """
    config = {"recursion_limit": MAX_RECURSION}
    if timeout_s is None:
        return graph.invoke(inputs, config=config), False

    result: dict = {}
    failure: dict = {}
    done = threading.Event()

    def _work() -> None:
        try:
            result["state"] = graph.invoke(inputs, config=config)
        except BaseException as exc:  # noqa: BLE001 - 原样抛回调用线程
            failure["error"] = exc
        finally:
            done.set()

    threading.Thread(target=_work, name="agent-graph", daemon=True).start()
    if not done.wait(max(0.0, timeout_s)):
        return None, True
    if "error" in failure:
        raise failure["error"]
    return result.get("state", {}), False


def _iter_with_deadline(iterator, deadline_at: float, stop: threading.Event | None = None):
    """把阻塞式迭代（LLM 流）搬进后台线程，主线程最多等到 ``deadline_at``。

    同步生成器一旦卡在 ``next()`` 上就没法打断，只能由后台线程持有它；到点后
    主线程直接放弃等待（工作线程同样会泄漏，取舍见 ``_invoke_with_deadline``）。
    ``stop`` 置位时抛 :class:`_StopRequested`，让流在下一个 chunk 边界收尾。
    """
    events: queue.Queue = queue.Queue()

    def _pump() -> None:
        try:
            for item in iterator:
                events.put(("item", item))
        except BaseException as exc:  # noqa: BLE001 - 原样抛回调用线程
            events.put(("error", exc))
        finally:
            events.put(("done", None))

    threading.Thread(target=_pump, name="agent-writer", daemon=True).start()
    while True:
        if stop is not None and stop.is_set():
            raise _StopRequested
        remaining = deadline_at - time.monotonic()
        if remaining <= 0:
            raise _DeadlineExceeded
        try:
            kind, value = events.get(timeout=min(remaining, _POLL_SECONDS))
        except queue.Empty:
            continue
        if kind == "item":
            yield value
        elif kind == "error":
            raise value
        else:
            return


# --------------------------------------------------------------------- 图


def _channel_query(name: str, state: AgentState) -> str:
    """通道查询词：裙子档案命中时，Lolibrary 用拉丁名，社媒用中文名。"""
    question = state.get("question", "")
    dress_query = state.get("dress_query") or {}
    if name == "lolibrary":
        return str(dress_query.get("lolibrary_query") or state.get("query") or question)
    if name in dress.SOCIAL_CHANNELS:
        return str(dress_query.get("social_query") or state.get("query") or question)
    return str(state.get("query") or question)


def _extract_dress_hint(planner, question: str, history: list[dict] | None) -> dict | None:
    """模型兜底识别；planner 没有这个方法（测试替身）时静默跳过。"""
    extract = getattr(planner, "extract_dress", None)
    if extract is None:
        return None
    try:
        hint = extract(question, history)
    except PlannerOutputError as exc:
        logger.warning("裙子识别兜底失败：%s", exc)
        return None
    except Exception as exc:  # noqa: BLE001 - 兜底失败不影响离线结果
        logger.warning("裙子识别兜底异常：%s", exc)
        return None
    if isinstance(hint, dict):
        return hint
    return {
        "brand": getattr(hint, "brand", ""),
        "region": getattr(hint, "region", ""),
        "series": getattr(hint, "series", ""),
        "series_cn": getattr(hint, "series_cn", ""),
        "confidence": getattr(hint, "confidence", ""),
    }


def build_graph(
    planner,
    writer,
    ctx: RecallContext,
    *,
    stream: bool = False,
    progress: _Progress | None = None,
    route: routing.RouteDecision | None = None,
):
    """按 planner / writer / ctx 构造编译好的图。

    ``stream=True`` 时 answer / verify 空转，答案交给调用方流式写作。
    ``progress`` 用来接收**通道一返回就记账**的局部结果，供整体超时接管。
    ``route`` 是模型分级路由的结果：只用来给 writer 指定模型，不影响召回。
    """

    writer_model = route.model if route is not None else None

    def _channel_node(name: str):
        def recall_one(state: AgentState) -> dict:
            question = state["question"]
            query = _channel_query(name, state)
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
            if progress is not None:
                progress.channel_done(name, evidence, step)
            return {"evidence": evidence, "steps": [step], "recalled": [name]}

        return recall_one

    def fanout_core(state: AgentState) -> list[Send]:
        return [Send(name, state) for name in channels.CORE_CHANNELS]

    def dress_resolve(state: AgentState) -> dict:
        """先用本地别名/档案识别品牌地区；没有命中就不触发，不调模型。"""
        started = time.perf_counter()
        query = dress.resolve_dress(state["question"], ctx)
        if query is None:
            return {}
        data = query.to_dict()
        step = {
            "kind": "dress",
            "brand": data.get("brand", ""),
            "region": data.get("region", ""),
            "series": data.get("series_en") or data.get("series_cn", ""),
            "source": data.get("source", ""),
            "specific": data.get("is_specific", False),
            "pending": data.get("source") == "pending_model",
            "verified": data.get("verified", False),
            "ms": int((time.perf_counter() - started) * 1000),
        }
        if progress is not None:
            progress.dress_done(data, step)
        return {"dress_query": data, "steps": [step]}

    def route(state: AgentState) -> dict:
        """补查决策：**规则**定要不要查（不看召回结果），模型只提检索词。

        模型挂了**不影响补查**——query 退回空串，通道节点用原问句兜底。
        这是拆分带来的第二个好处：模型不再有权力阻止补查。
        """
        question = state["question"]
        intent = plan_channels(question)
        dress_query = dict(state.get("dress_query") or {})
        was_pending = dress_query.get("source") == "pending_model"
        search_traces: list[dict] = []
        if dress_query and not dress_query.get("series_en"):
            # 先用「拆词 → Lolibrary / 网页反查」做确定性检索，失败再让模型猜。
            dress_query, search_traces = dress.resolve_with_search(dress_query, ctx)
        needs_model = bool(dress_query) and (
            dress_query.get("source") == "pending_model"
            or (
                dress_query.get("source") != "model"
                and not dress_query.get("series_en")
            )
        )
        if needs_model:
            hint = _extract_dress_hint(planner, question, state.get("history"))
            if hint:
                dress_query = dress.merge_model_hint(dress_query, hint, ctx)
        # 已经进入模型兜底、但最终没拿到可核实的系列：反问用户，
        # 别让普通通道拿另一条裙子顶替（哪怕模型只认出了品牌）。
        dress_unresolved = bool(
            not dress_query.get("series_en")
            and (
                dress_query.get("source") == "pending_model"
                or (was_pending and dress_query.get("source") == "model")
            )
        )
        if dress_unresolved:
            dress_query = {}
        wanted = (
            []
            if dress_unresolved
            else (dress.dress_channels(dress_query, intent) if dress_query else intent)
        )

        query = ""
        error = ""
        if wanted:
            # 裙子档案已经有针对性检索词时直接用，省一次模型调用。
            if dress_query.get("social_query"):
                query = str(dress_query["social_query"])
            else:
                try:
                    query = planner.extract_query(question, state.get("history"))
                except PlannerOutputError as exc:
                    logger.warning("检索词提取失败，用原问句补查：%s", exc)
                    error = str(exc)[:160]

        step = {"kind": "route", "supplementary": wanted, "query": query}
        if dress_unresolved:
            step["dress_unresolved"] = True
        if dress_query:
            step.update(
                {
                    "dress_brand": dress_query.get("brand", ""),
                    "dress_region": dress_query.get("region", ""),
                    "dress_series": dress_query.get("series_en") or dress_query.get("series_cn", ""),
                    "dress_source": dress_query.get("source", ""),
                    "dress_verified": bool(dress_query.get("verified")),
                }
            )
        if error:
            step["error"] = error
        search_steps = [
            {"kind": "dress_search", **trace} for trace in search_traces if trace
        ]
        if progress is not None:
            progress.route_done(wanted, step, search_steps)
        return {
            "supplementary": wanted,
            "query": query,
            "dress_query": dress_query,
            "dress_unresolved": dress_unresolved,
            "steps": [*search_steps, step],
        }

    def fanout_after_route(state: AgentState) -> list[Send]:
        """route 之后的条件边：有补查就 Send 到实时通道，否则直接去整合档案。

        注意：``Send`` 只能在**条件边函数**里返回（节点里返回会报 InvalidUpdateError），
        所以这里不是图节点，而是 ``add_conditional_edges`` 的 path 函数。
        """
        names = [name for name in state.get("supplementary", []) if name in channels.SUPPLEMENTARY_CHANNELS]
        if not names:
            return [Send("dress_profile", state)]
        return [Send(name, state) for name in names]

    def dress_profile(state: AgentState) -> dict:
        """把召回结果整合成结构化档案；没有识别结果时直接跳过。"""
        query = state.get("dress_query")
        if not query:
            return {}
        started = time.perf_counter()
        try:
            result = dress.build_profile(query, state.get("evidence", []), ctx)
        except Exception as exc:  # noqa: BLE001 - 整合失败不该让整轮白跑
            logger.warning("裙子档案整合失败：%s", exc)
            return {}
        if not result:
            return {}
        profile = result["profile"]
        item = result["evidence"]
        step = {
            "kind": "dress_profile",
            "brand": profile.get("brand", ""),
            "region": profile.get("region", ""),
            "fields": {
                "release": bool(profile.get("release")),
                "price": bool(profile.get("price")),
                "style": bool(profile.get("style")),
                "colors": bool(profile.get("colors")),
            },
            "ms": int((time.perf_counter() - started) * 1000),
        }
        if progress is not None:
            progress.profile_done(profile, [item], step)
        unverified = bool(
            profile.get("source") == "model"
            and not profile.get("verified")
            and profile.get("region") != "CN"
            and profile.get("title")
        )
        return {
            "dress_profile": profile,
            "dress_unverified": unverified,
            "evidence": [item],
            "steps": [step],
        }

    def answer(state: AgentState) -> dict:
        if state.get("dress_unresolved"):
            return {
                "answer": _dress_clarification(state["question"]),
                "stopped_reason": "dress_unresolved",
            }
        if state.get("dress_unverified"):
            return {
                "answer": _dress_unverified_answer(state.get("dress_profile") or {}),
                "stopped_reason": "dress_unverified",
            }
        if state.get("stream"):
            return {}
        evidence = _normalize(
            state.get("evidence", []),
            state.get("supplementary"),
            limit=ctx.settings.evidence_max_items,
        )
        if not evidence:
            return {"answer": _forced_answer([], reason="no_evidence"), "stopped_reason": "no_evidence"}
        if writer is None:
            return {"answer": _forced_answer(evidence, reason="no_writer")}
        try:
            text = writer.write(
                state["question"],
                _numbered(evidence),
                state.get("history"),
                model=writer_model,
            )
        except Exception as exc:  # noqa: BLE001 - 写作失败不该让整轮白跑
            logger.warning("写作失败，退回原文清单：%s", exc)
            text = _forced_answer(evidence, reason="writer_failed")
        return {"answer": text}

    def verify(state: AgentState) -> dict:
        if state.get("stream"):
            return {}
        if state.get("dress_unresolved") or state.get("dress_unverified"):
            return {}
        evidence = _normalize(
            state.get("evidence", []),
            state.get("supplementary"),
            limit=ctx.settings.evidence_max_items,
        )
        text = state.get("answer", "")
        problems = citation_problems(text, len(evidence))
        if not problems:
            return {}
        # 整段换成原文摘录。这里敢换，是因为**答案还没交付**：非流式是一次性返回，
        # 用户没读过要删掉的那段。流式路径不能照抄——字已经逐段显示了，换掉等于
        # 读一半被打脸。两处刻意分叉，理由见 stream() 的同名处置，别当漏改抹平。
        reason = "citation_out_of_range" if any("越界" in item for item in problems) else "no_citation"
        return {"answer": _forced_answer(evidence, reason=reason), "stopped_reason": reason}

    graph = StateGraph(AgentState)
    for name in ALL_CHANNELS:
        graph.add_node(name, _channel_node(name))
    graph.add_node("dress_resolve", dress_resolve)
    graph.add_node("dress_profile", dress_profile)
    graph.add_node("route", route)
    graph.add_node("answer", answer)
    graph.add_node("verify", verify)

    # 先识别裙子档案，再并行扇出核心通道（核心 Lolibrary 用识别出的拉丁系列名）
    graph.add_edge(START, "dress_resolve")
    graph.add_conditional_edges("dress_resolve", fanout_core, list(channels.CORE_CHANNELS))
    for name in channels.CORE_CHANNELS:
        graph.add_edge(name, "route")
    # route 之后：有补查就追加实时通道，否则直接去整合档案
    graph.add_conditional_edges(
        "route", fanout_after_route, [*channels.SUPPLEMENTARY_CHANNELS, "dress_profile"]
    )
    for name in channels.SUPPLEMENTARY_CHANNELS:
        graph.add_edge(name, "dress_profile")
    graph.add_edge("dress_profile", "answer")
    graph.add_edge("answer", "verify")
    graph.add_edge("verify", END)

    return graph.compile()


# --------------------------------------------------------------------- 关注建议


# 只有「还在预约 / 未发售」这类问题才值得提议持续关注；「现货 / 已有货」不提。
_RELEASE_MARKERS = (
    "未发售", "未开售", "尚未发售", "尚未开售", "待发售", "即将发售",
    "预约", "定金", "抽选", "再贩", "发售",
)

# 从检索词里剥掉意图词，留下的才是「裙子本身」的名称（关注项关键词要能复用）。
_SUBJECT_NOISE = (
    "预约价", "多少钱", "多少", "什么时候", "有没有货", "还有货", "买得到", "怎么买",
    "哪里买", "价格", "预约", "定金", "现货", "再贩", "抽选", "掉落",
    "未发售", "未开售", "开售", "发售", "新款", "新作", "上新",
)


def _follow_subject(query: str, question: str) -> str:
    raw = (query or question or "").strip()
    text = raw
    for word in _SUBJECT_NOISE:
        text = text.replace(word, " ")
    text = " ".join(text.split())
    return (text or raw)[:60]


def _follow_suggestion(
    question: str, state: AgentState, evidence: list[dict], ctx: RecallContext
) -> dict | None:
    """回答下方是否该出现「持续关注」按钮。

    只对**未发售 / 预约中**的裙子提议：问题命中交易或上新意图，且问题或证据里
    出现预约 / 发售类字眼。要不要关注由用户点按钮决定——这是「用户表示需要」
    的落地，不靠模型猜意图。

    返回 ``{"subject", "source", "reason"}`` 或 None；前端只在有值时渲染按钮。
    """
    if not evidence:
        return None
    if not set(plan_intents(question)) & {"transaction", "news"}:
        return None
    pool = " ".join(
        [question, *(f"{item.get('title', '')} {item.get('text', '')}" for item in evidence)]
    )
    if not any(marker in pool for marker in _RELEASE_MARKERS):
        return None

    subject = _follow_subject(state.get("query") or question, question)
    if not subject:
        return None
    # 来源必须是**现在就绪**的实时通道：默认硬写 web 时，即使搜索没启用也会
    # 弹「持续关注」，用户点了也永远收不到更新。
    source = channels.follow_source(ctx.settings)
    if not source:
        return None
    return {
        "subject": subject,
        "source": source,
        "reason": "这条还在预约 / 未发售阶段，可以持续关注上新与补款",
    }


# --------------------------------------------------------------------- 收尾


def _merge_usage(planner, writer) -> dict:
    usage = dict(getattr(planner, "usage", {}) or {})
    if writer is not None and getattr(writer, "usage", None):
        usage["writer"] = writer.usage
    return usage


def _cost_block(
    planner, writer, settings, route: routing.RouteDecision | None
) -> dict:
    """这次 run 的成本账：按模型 + 路由策略（mode/tier）标注。"""
    return cost.usage_cost(
        planner,
        writer,
        settings,
        extra_calls=route.calls if route is not None else (),
        mode=route.mode if route is not None else routing.MODE_AUTO,
        tier=route.tier if route is not None else "",
    )


def _finish(
    question: str,
    state: AgentState,
    *,
    ctx: RecallContext,
    planner,
    writer,
    history: list[dict] | None,
    duration_ms: int,
    route: routing.RouteDecision | None = None,
) -> dict:
    """把图的结果收尾成完整响应（编号、轨迹）。"""
    evidence = _normalize(
        state.get("evidence", []),
        state.get("supplementary"),
        limit=ctx.settings.evidence_max_items,
    )
    cost_block = _cost_block(planner, writer, ctx.settings, route)
    payload = {
        "question": question,
        "answer": state.get("answer", ""),
        "refused": not evidence,
        "evidence": _numbered(evidence),
        "steps": state.get("steps", []),
        "stopped_reason": state.get("stopped_reason", ""),
        "follow_suggestion": _follow_suggestion(question, state, evidence, ctx),
        "dress": state.get("dress_profile") or None,
        "usage": _merge_usage(planner, writer),
        "route": route.as_dict() if route is not None else None,
        "cost": cost_block,
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
            "route": payload["route"],
            "cost": cost_block,
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
    """非流式：跑完整张图（含整体预算），返回最终响应。

    到点没跑完就交已查到的证据：不再发写作请求，回答直接列出证据并说明
    「部分通道超时」。这是**降级**，不是错误——调用方拿到的仍是正常结构。
    """
    question = (question or "").strip()
    if not question:
        raise ValueError("问题不能为空")

    started = time.perf_counter()
    budget = _deadline_of(ctx)
    deadline_at = time.monotonic() + budget if budget > 0 else None
    # 路由在召回之前定：规则判不出来时可能有一次极小判断，它也吃整体预算。
    route = routing.decide(question, ctx.settings)
    progress = _Progress()
    graph = build_graph(planner, writer, ctx, stream=False, progress=progress, route=route)
    state, timed_out = _invoke_with_deadline(
        graph,
        {
            "question": question,
            "history": sanitize_history(history),
            "evidence": [],
            "steps": [],
            "recalled": [],
            "stream": False,
            "dress_query": {},
            "dress_profile": {},
            "dress_unresolved": False,
            "dress_unverified": False,
        },
        None if deadline_at is None else max(0.0, deadline_at - time.monotonic()),
    )
    if timed_out:
        state = _partial_state(question, progress, budget, ctx=ctx)
    duration_ms = int((time.perf_counter() - started) * 1000)
    return _finish(
        question, state, ctx=ctx, planner=planner, writer=writer,
        history=history, duration_ms=duration_ms, route=route,
    )


def stream(
    question: str,
    *,
    ctx: RecallContext,
    planner,
    writer=None,
    history: list[dict] | None = None,
    stop: threading.Event | None = None,
) -> Iterator[dict]:
    """流式：边召回边吐 step，再吐答案增量，最后吐 final。

    事件类型：``step`` / ``delta`` / ``replace`` / ``final`` / ``error``。
    整体预算同时覆盖召回与写作：到点不再等剩余通道、也不再继续收字，直接用
    已查到的证据收尾（``replace`` + ``final``），不抛 500。

    ``stop`` 由调用方（SSE 路由）在客户端断开 / 点「停止」时置位：召回与写作
    都在下一个 chunk 边界收尾，不再继续消费上游结果。
    """
    question = (question or "").strip()
    if not question:
        yield {"type": "error", "detail": "问题不能为空"}
        return
    if stop is not None and stop.is_set():
        return

    started = time.perf_counter()
    budget = _deadline_of(ctx)
    deadline_at = time.monotonic() + budget if budget > 0 else None
    # 与 run() 同一套路由；轻量判断也吃整体预算。
    route = routing.decide(question, ctx.settings)
    progress = _Progress(events=True)
    graph = build_graph(planner, writer, ctx, stream=True, progress=progress, route=route)

    result: dict = {}
    failure: dict = {}

    def _work() -> None:
        try:
            result["state"] = graph.invoke(
                {
                    "question": question,
                    "history": sanitize_history(history),
                    "evidence": [],
                    "steps": [],
                    "recalled": [],
                    "stream": True,
                    "dress_query": {},
                    "dress_profile": {},
                    "dress_unresolved": False,
                    "dress_unverified": False,
                },
                config={"recursion_limit": MAX_RECURSION},
            )
        except BaseException as exc:  # noqa: BLE001 - 原样抛回调用线程
            failure["error"] = exc
        finally:
            progress.close()

    threading.Thread(target=_work, name="agent-graph", daemon=True).start()

    # 召回阶段：不再等整张图，谁先完成就先报谁的 step。
    timed_out = False
    emitted = 0
    while True:
        if stop is not None and stop.is_set():
            return
        wait = _POLL_SECONDS
        if deadline_at is not None:
            remaining = deadline_at - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            wait = min(wait, remaining)
        try:
            item = progress.next_step(wait)
        except queue.Empty:
            continue
        if item is _PROGRESS_DONE:
            break
        emitted += 1
        yield {"type": "step", **item}

    if stop is not None and stop.is_set():
        return

    if timed_out:
        state = _partial_state(question, progress, budget, ctx=ctx)
        # 已经吐过的 step 不再重复；`_partial_state` 在快照后面补了超时说明。
        for step in state.get("steps", [])[emitted:]:
            yield {"type": "step", **step}
    else:
        if "error" in failure:
            raise failure["error"]
        state = result.get("state", {})

    evidence = _normalize(
        state.get("evidence", []),
        state.get("supplementary"),
        limit=ctx.settings.evidence_max_items,
    )
    numbered = _numbered(evidence)

    if state.get("dress_unresolved") or state.get("dress_unverified"):
        text = state.get("answer") or _dress_clarification(question)
        yield {
            "type": "replace",
            "text": text,
            "reason": state.get("stopped_reason") or "dress_clarify",
        }
    elif timed_out:
        # 预算已经用完：不再发写作请求，把已查到的证据直接摊开。
        text = state.get("answer", "")
        yield {"type": "replace", "text": text, "reason": "deadline"}
    elif evidence and writer is not None:
        chunks: list[str] = []
        try:
            for delta in _iter_with_deadline(
                writer.stream(
                    question,
                    numbered,
                    sanitize_history(history),
                    model=route.model,
                ),
                deadline_at if deadline_at is not None else float("inf"),
                stop=stop,
            ):
                chunks.append(delta)
                yield {"type": "delta", "text": delta}
        except _StopRequested:
            # 客户端已经离开：不再收字、也不再补 final（没人收）。
            return
        except _DeadlineExceeded:
            logger.warning("流式写作超过整体预算 %.0fs，停止收字", budget)
            step = _timeout_step(progress.snapshot(), budget)
            state["steps"] = [*state.get("steps", []), step]
            state["stopped_reason"] = "deadline"
            yield {"type": "step", **step}
            text = _timeout_answer(evidence, budget)
            yield {"type": "replace", "text": text, "reason": "deadline"}
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

    if state.get("dress_unresolved") or state.get("dress_unverified"):
        # 反问/未核实答案没有引用编号，不能按「有证据没标来源」处理。
        problems, invalid = [], []
    else:
        problems = citation_problems(text, len(evidence))
        invalid = invalid_citations(text, len(evidence))
    if problems:
        # **不替换整段**。流式时用户已经把回答读完了，这时候换成原文清单等于
        # 读一半被打脸；而且原文清单一屏十几条，比原来那段更难用。
        # 改成把越界编号交给前端就地标红，并在答案下方说明——保留可读的答案，
        # 同时让"这条引用查无来源"肉眼可见。
        # 非流式在 verify 节点里是整段替换（还没交付，换掉无损）：两处分叉是
        # 由交付方式决定的，不是行为不一致的漏改。
        state["stopped_reason"] = ";".join(
            filter(None, [state.get("stopped_reason", ""), "citation_problems"])
        )

    duration_ms = int((time.perf_counter() - started) * 1000)
    cost_block = _cost_block(planner, writer, ctx.settings, route)
    payload = {
        "question": question,
        "answer": text,
        "refused": not evidence,
        "evidence": numbered,
        "steps": state.get("steps", []),
        "stopped_reason": state.get("stopped_reason", ""),
        "citation_problems": problems,
        "invalid_citations": invalid,
        "follow_suggestion": _follow_suggestion(question, state, evidence, ctx),
        "dress": state.get("dress_profile") or None,
        "usage": _merge_usage(planner, writer),
        "route": route.as_dict(),
        "cost": cost_block,
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
            "route": payload["route"],
            "cost": cost_block,
            "duration_ms": duration_ms,
            "streamed": True,
            "history_turns": len(history or []),
        },
        ctx.settings,
    )
    yield {"type": "final", **payload}
