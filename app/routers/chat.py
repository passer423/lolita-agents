"""助手（agent）接口：多路召回 → 一次性结果 + SSE 流式。

召回编排在 ``app/agent/graph.py``（LangGraph），这里只负责：
* 把 HTTP 请求翻译成 graph 的输入；
* 规划器/写作器的生命周期与错误映射（503 / 502）；
* SSE 分帧。

## 为什么流式用 POST + 手写 SSE，而不是 EventSource

``EventSource`` 只能发 GET，问题只能塞进 query string（会进浏览器历史和访问日志），
而且要带 ``Authorization`` 头也没法加。所以用 ``fetch`` 读 ``ReadableStream``，
自己按 ``data:`` 分帧——见 ``static/chat.js``。
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app import db
from app.agent import channels, graph, trace
from app.agent.channels import RecallContext
from app.agent.planner import DeepSeekPlanner, PlannerUnavailable
from app.agent.writer import MAX_HISTORY_TURNS, DeepSeekWriter, sanitize_history
from app.config import settings

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/chat", tags=["chat"])

# 召回 / 写作都安静超过这个时长就发一个 SSE 注释帧。
# 反向代理与手机网络会把长时间无字节的连接掐掉；心跳也让前端知道服务还活着。
HEARTBEAT_SECONDS = 10.0


def _sse(payload: dict) -> str:
    return "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"


class ChatTurn(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(..., min_length=1, max_length=500)


class ChatIn(BaseModel):
    message: str = Field(..., min_length=1, max_length=300)
    # 多轮：``None`` = 服务端从会话记忆里补最近几轮；显式 ``[]`` = 开新对话，
    # 不加载旧上下文（清空请用 DELETE /api/chat/history）。
    # **编号只在单轮内有效**：落库与回填时都会剥掉 [n]（见 sanitize_history）。
    history: list[ChatTurn] | None = Field(default=None, max_length=16)


def _make_planner(app=settings):
    """留成函数是为了测试能注入脚本化 planner——不必联网、不必有 key。"""
    return DeepSeekPlanner(app)


def _make_writer(app=settings):
    if not app.llm_enabled:
        raise PlannerUnavailable("写作需要模型：请先配置 LLM_API_KEY")
    return DeepSeekWriter(app)


def _history_of(payload: ChatIn, app=settings) -> list[dict]:
    """客户端没传历史（``None``）时从服务端会话记忆补最近几轮。

    显式传空列表是「开新对话」，不是「没传」——否则用户永远摆脱不掉旧上下文。
    两条路径都要过 ``sanitize_history``：落库的历史同样可能带上一轮的 [n]。
    """
    if payload.history is None:
        raw = db.recent_conversations(app.db_path, limit=MAX_HISTORY_TURNS)
    else:
        raw = [turn.model_dump() for turn in payload.history]
    return sanitize_history(raw)


def _remember(question: str, answer: str, app=settings) -> None:
    """把本轮问答写进会话记忆。失败只 warning：记不住不等于答不了。"""
    answer = (answer or "").strip()
    if not answer:
        return
    try:
        db.add_exchange(
            app.db_path,
            question,
            answer,
            keep=app.conversation_retention_messages,
        )
    except Exception as exc:  # noqa: BLE001 - 持久化失败不能影响已经给出的回答
        logger.warning("会话记忆写入失败（不影响回答）：%s", exc)


def _ctx(app=settings) -> RecallContext:
    return RecallContext(settings=app)


@router.get("/status")
def chat_status() -> dict:
    """前端靠它决定"能不能聊"、哪些召回通道开着。"""
    return {
        "enabled": settings.llm_enabled,
        "model": settings.llm_model if settings.llm_enabled else "",
        "hint": "" if settings.llm_enabled else (
            "需要在 .env 里配置 LLM_API_KEY"
            "（DeepSeek：LLM_BASE_URL=https://api.deepseek.com/v1、LLM_MODEL=deepseek-chat）"
        ),
        "channels": {
            "core": list(channels.CORE_CHANNELS),
            "supplementary": list(channels.SUPPLEMENTARY_CHANNELS),
            # 每通道分开报 enabled（配置开了）与 ready（现在真能召回），
            # 否则「没填 key」「没登录」都会被压成同一句「不可用」。
            "status": channels.channel_status(settings),
        },
        "stream": True,
    }


@router.post("")
def chat(payload: ChatIn) -> dict:
    try:
        planner = _make_planner(settings)
    except PlannerUnavailable as exc:
        # 503 而不是 500：这是"还没配好"，不是"服务坏了"
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    # 写作器拿不到不算致命：没有证据时走拒答文案，有证据时退回原文清单
    try:
        writer = _make_writer(settings)
    except PlannerUnavailable:
        writer = None

    try:
        result = graph.run(
            payload.message,
            ctx=_ctx(settings),
            planner=planner,
            writer=writer,
            history=_history_of(payload),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    _remember(payload.message, str(result.get("answer") or ""))
    return result


@router.post("/stream")
async def chat_stream(payload: ChatIn, request: Request):
    """SSE：召回阶段逐通道推进度，再推答案增量，最后推 final。

    图的消费放在后台线程（``graph.stream`` 是同步生成器），事件经
    ``asyncio.Queue`` 回到事件循环：这样等待期间还能发心跳、还能
    ``await request.is_disconnected()``，不会把整个事件循环堵住。

    客户端断开（点「停止」或关页面）时置位 ``stop``：graph 在下一个 chunk
    边界收尾，不再消费上游结果，也不再有线程继续逐字拉模型。
    """
    try:
        planner = _make_planner(settings)
    except PlannerUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    try:
        writer = _make_writer(settings)
    except PlannerUnavailable:
        writer = None

    history = _history_of(payload)

    async def generate():
        loop = asyncio.get_running_loop()
        events: asyncio.Queue = asyncio.Queue()
        stop = threading.Event()

        def deliver(item) -> None:
            loop.call_soon_threadsafe(events.put_nowait, item)

        def pump() -> None:
            try:
                for event in graph.stream(
                    payload.message,
                    ctx=_ctx(settings),
                    planner=planner,
                    writer=writer,
                    history=history,
                    stop=stop,
                ):
                    # 只在拿到 final 后落库：中途断开的半截回答不该进入下一轮上下文。
                    if event.get("type") == "final":
                        _remember(payload.message, str(event.get("answer") or ""))
                    deliver(("event", event))
                    if stop.is_set():
                        break
            except Exception as exc:  # noqa: BLE001 - 流已经开始，只能以事件形式报错
                logger.exception("流式回答失败")
                deliver(("error", f"{type(exc).__name__}: {exc}"))
            finally:
                deliver(("done", None))

        threading.Thread(target=pump, name="chat-sse", daemon=True).start()
        try:
            while True:
                if await request.is_disconnected():
                    logger.info("客户端已断开，停止本轮流式回答")
                    break
                try:
                    kind, value = await asyncio.wait_for(
                        events.get(), timeout=HEARTBEAT_SECONDS
                    )
                except asyncio.TimeoutError:
                    # SSE 注释帧：浏览器忽略，但连接不会因长时间无字节被中间层掐断。
                    yield ": keep-alive\n\n"
                    continue

                if kind == "done":
                    break
                if kind == "error":
                    yield _sse({"type": "error", "detail": value})
                    break
                yield _sse(value)
        finally:
            stop.set()

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # 反向代理（Tailscale / nginx）下不加这句会被缓冲成"最后一次性到达"
            "X-Accel-Buffering": "no",
        },
    )


@router.delete("/history")
def clear_history() -> dict:
    """清空服务端会话记忆（= 开新对话）。关注项与排查轨迹不受影响。"""
    return {"removed": db.clear_conversations(settings.db_path)}


@router.get("/runs")
def chat_runs(limit: int = Query(10, ge=1, le=100)) -> dict:
    """最近几次 run 的轨迹。排查"它为什么没找到"靠这个，而不是靠猜。

    轨迹里是完整的原始问题与回答，属于隐私数据——本服务默认只监听本机，
    按 README 用 Tailscale 开给手机时请自行注意。
    """
    records = trace.tail(limit, settings)
    return {"count": len(records), "runs": records}
