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

import json
import logging
from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.agent import channels, graph, trace
from app.agent.channels import RecallContext
from app.agent.planner import DeepSeekPlanner, PlannerUnavailable
from app.agent.writer import DeepSeekWriter, sanitize_history
from app.config import settings

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/chat", tags=["chat"])


class ChatTurn(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(..., min_length=1, max_length=500)


class ChatIn(BaseModel):
    message: str = Field(..., min_length=1, max_length=300)
    # 多轮：客户端带上最近几轮。服务端不存会话——**编号只在单轮内有效**，
    # 所以历史里只留文字，不再引用上一轮的 [n]。
    history: list[ChatTurn] = Field(default_factory=list, max_length=16)


def _make_planner(app=settings):
    """留成函数是为了测试能注入脚本化 planner——不必联网、不必有 key。"""
    return DeepSeekPlanner(app)


def _make_writer(app=settings):
    if not app.llm_enabled:
        raise PlannerUnavailable("写作需要模型：请先配置 LLM_API_KEY")
    return DeepSeekWriter(app)


def _history_of(payload: ChatIn) -> list[dict]:
    return sanitize_history([turn.model_dump() for turn in payload.history])


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

    return result


@router.post("/stream")
def chat_stream(payload: ChatIn):
    """SSE：先推进度（各通道召回几条、补查决策），再推答案增量，最后推 final。"""
    try:
        planner = _make_planner(settings)
    except PlannerUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    try:
        writer = _make_writer(settings)
    except PlannerUnavailable:
        writer = None

    history = _history_of(payload)

    def generate():
        try:
            for event in graph.stream(
                payload.message,
                ctx=_ctx(settings),
                planner=planner,
                writer=writer,
                history=history,
            ):
                yield "data: " + json.dumps(event, ensure_ascii=False) + "\n\n"
        except Exception as exc:  # noqa: BLE001 - 流已经开始，只能以事件形式报错
            logger.exception("流式回答失败")
            yield "data: " + json.dumps(
                {"type": "error", "detail": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False
            ) + "\n\n"

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


@router.get("/runs")
def chat_runs(limit: int = Query(10, ge=1, le=100)) -> dict:
    """最近几次 run 的轨迹。排查"它为什么没找到"靠这个，而不是靠猜。

    轨迹里是完整的原始问题与回答，属于隐私数据——本服务默认只监听本机，
    按 README 用 Tailscale 开给手机时请自行注意。
    """
    records = trace.tail(limit, settings)
    return {"count": len(records), "runs": records}
