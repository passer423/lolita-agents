"""助手（agent）接口：多路召回 → 一次性结果 + SSE 流式，外加对话管理。

召回编排在 ``app/agent/graph.py``（LangGraph），这里只负责：
* 登录账号与对话线程的解析（一个账号下多个对话，互不可见）；
* 把 HTTP 请求翻译成 graph 的输入；
* 规划器/写作器的生命周期与错误映射（503 / 502）；
* SSE 分帧与落库。

## 对话怎么存

``chat_threads`` 一行一个对话（标题取首问摘要），``thread_messages`` 存消息。
首问时客户端不传 ``conversation_id``，服务端建线程并在 SSE **第一帧**发
``{"type":"conversation","id":..,"title":..}``，前端据此把新对话挂到侧栏。
之后的轮次带 ``conversation_id``，模型上下文从该线程的消息里取——不再相信
浏览器传来的历史。

## 为什么流式用 POST + 手写 SSE，而不是 EventSource

``EventSource`` 只能发 GET，问题只能塞进 query string（会进浏览器历史和访问日志），
而且要带 ``Authorization`` 头也没法加。所以用 ``fetch`` 读 ``ReadableStream``，
自己按 ``data:`` 分帧——见 ``static/chat.js``。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator

from app import accounts, db
from app.agent import channels, graph, trace
from app.agent.channels import RecallContext
from app.agent.planner import DeepSeekPlanner, PlannerUnavailable
from app.agent.writer import MAX_HISTORY_TURNS, DeepSeekWriter, sanitize_history
from app.config import settings

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/chat", tags=["chat"])
threads_router = APIRouter(prefix="/api/threads", tags=["chat"])

# 召回 / 写作都安静超过这个时长就发一个 SSE 注释帧。
# 反向代理与手机网络会把长时间无字节的连接掐掉；心跳也让前端知道服务还活着。
HEARTBEAT_SECONDS = 10.0

DEFAULT_THREAD_TITLE = "新对话"


def _sse(payload: dict) -> str:
    return "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"


def _title_from(question: str) -> str:
    """首问摘要当标题：折叠空白、截 30 字（含中文标点），足够辨认。"""
    text = " ".join((question or "").split())
    return text[:30] or DEFAULT_THREAD_TITLE


class ChatTurn(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(..., min_length=1, max_length=500)


class ChatIn(BaseModel):
    message: str = Field(..., min_length=1, max_length=300)
    # 服务端会话记忆优先：带了 conversation_id 就从该对话取最近几轮。
    # 显式传 history 仍支持（老客户端 / 测试），但不会再被浏览器端当作唯一来源。
    # **编号只在单轮内有效**：落库与回填时都会剥掉 [n]（见 sanitize_history）。
    history: list[ChatTurn] | None = Field(default=None, max_length=16)
    conversation_id: int | None = Field(default=None, ge=1)

    @field_validator("message")
    @classmethod
    def _reject_mojibake(cls, value: str) -> str:
        """只剩一串问号的消息：几乎一定是请求体的中文在发送前就丢了。

        真事：PowerShell 5 的 ``Invoke-WebRequest`` 用 Content-Type 的默认
        charset（ISO-8859-1）编码字符串体，``裙撑长度怎么选`` 发出去变成
        ``???????``。服务端收到的就是七个问号，召回当然是空——但接口回的是
        「没有检索到能回答这个问题的内容」，把编码问题伪装成知识库问题，
        排查方向全被带偏。

        阈值故意收窄：只有「连续 3 个以上半角问号、且没有别的字符」才拦。
        单个 ``?``、两个 ``?``、纯标点、纯 emoji、全角 ``？？？`` 都照常放行
        ——那些是正常输入，替换后的形态是连续半角问号且长度与原文一致。
        """
        if re.fullmatch(r"\?{3,}", value.strip()):
            raise ValueError(
                "消息里只有问号，没有可检索的文字。请求体疑似不是 UTF-8 编码："
                "中文在发送前被替换成了 ?，服务端无法还原。"
                "请用 UTF-8 发送 JSON（curl 加 --data-binary @body.json；"
                "PowerShell 先把字符串转成 UTF-8 字节再传给 -Body；浏览器不受影响）。"
            )
        return value


def _make_planner(app=settings):
    """留成函数是为了测试能注入脚本化 planner——不必联网、不必有 key。"""
    return DeepSeekPlanner(app)


def _make_writer(app=settings):
    if not app.llm_enabled:
        raise PlannerUnavailable("写作需要模型：请先配置 LLM_API_KEY")
    return DeepSeekWriter(app)


def _ensure_thread(account: dict, conversation_id: int | None, question: str) -> dict:
    """拿到本轮要写入的对话；``None`` 时新建，标题直接取首问摘要。

    指定了别人的 conversation_id 一律 404：不能让它变成"猜 id 就能读别人会话"。
    """
    if conversation_id is None:
        return db.create_thread(settings.db_path, account["id"], _title_from(question))
    thread = db.get_thread(settings.db_path, account["id"], conversation_id)
    if thread is None:
        raise HTTPException(status_code=404, detail="对话不存在或不属于当前账号")
    if not thread["title"] or thread["title"] == DEFAULT_THREAD_TITLE:
        db.set_thread_title(settings.db_path, account["id"], thread["id"], _title_from(question))
        thread["title"] = _title_from(question)
    return thread


def _history_of(payload: ChatIn, account: dict, thread: dict) -> list[dict]:
    """模型上下文：优先服务端线程，显式 ``history``（含 ``[]``）则照用。

    显式传空列表是「本轮不带上下文」，不是「没传」——保留这个语义是为了
    兼容老客户端；新前端只传 conversation_id，由服务端取历史。
    两条路径都要过 ``sanitize_history``：历史里可能带上一轮的 [n]。
    """
    if payload.history is not None:
        raw = [turn.model_dump() for turn in payload.history]
    else:
        raw = db.thread_messages(
            settings.db_path, account["id"], thread["id"], limit=MAX_HISTORY_TURNS
        )
    return sanitize_history(raw)


def _remember(account: dict, thread: dict, question: str, answer: str) -> None:
    """把本轮问答写进线程。失败只 warning：记不住不等于答不了。"""
    answer = (answer or "").strip()
    if not answer:
        return
    try:
        db.add_thread_exchange(
            settings.db_path,
            account["id"],
            thread["id"],
            question,
            answer,
            keep=settings.conversation_retention_messages,
        )
    except Exception as exc:  # noqa: BLE001 - 持久化失败不能影响已经给出的回答
        logger.warning("对话写入失败（不影响回答）：%s", exc)


def _ctx(account: dict) -> RecallContext:
    return RecallContext(settings=settings, account_id=account["id"])


def _thread_payload(thread: dict) -> dict:
    return {
        "id": thread["id"],
        "title": thread["title"] or DEFAULT_THREAD_TITLE,
        "created_at": thread["created_at"],
        "updated_at": thread["updated_at"],
        "message_count": thread.get("message_count", 0),
    }


@router.get("/status")
def chat_status() -> dict:
    """前端靠它决定"能不能聊"、哪些召回通道开着。

    这是公开接口（登录页也要判断模型是否配好），不含任何用户数据。
    """
    return {
        "enabled": settings.llm_enabled,
        # model 是真实调用标识，留着给探针/排障用；前端只展示 display_name。
        "model": settings.llm_model if settings.llm_enabled else "",
        "display_name": settings.app_display_name.strip() or "lolitaagent",
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
def chat(payload: ChatIn, request: Request) -> dict:
    account = accounts.account_of(request)
    # 先校验对话归属，再检查模型是否配好：拿别人的 id 应该 404，
    # 而不是因为服务没配 key 被 503 盖住（也避免把「存在性」泄露成另一种错）。
    thread = (
        _ensure_thread(account, payload.conversation_id, payload.message)
        if payload.conversation_id is not None
        else None
    )
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

    if thread is None:
        thread = _ensure_thread(account, None, payload.message)
    history = _history_of(payload, account, thread)

    try:
        result = graph.run(
            payload.message,
            ctx=_ctx(account),
            planner=planner,
            writer=writer,
            history=history,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    _remember(account, thread, payload.message, str(result.get("answer") or ""))
    accounts.maybe_prune(settings)
    result["conversation_id"] = thread["id"]
    result["conversation_title"] = thread["title"]
    return result


@router.post("/stream")
async def chat_stream(payload: ChatIn, request: Request):
    """SSE：先发 conversation 帧，再逐通道进度、答案增量，最后 final。

    图的消费放在后台线程（``graph.stream`` 是同步生成器），事件经
    ``asyncio.Queue`` 回到事件循环：这样等待期间还能发心跳、还能
    ``await request.is_disconnected()``，不会把整个事件循环堵住。

    客户端断开（点「停止」或关页面）时置位 ``stop``：graph 在下一个 chunk
    边界收尾，不再消费上游结果，也不再有线程继续逐字拉模型。
    """
    account = accounts.account_of(request)
    thread = (
        _ensure_thread(account, payload.conversation_id, payload.message)
        if payload.conversation_id is not None
        else None
    )
    try:
        planner = _make_planner(settings)
    except PlannerUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    try:
        writer = _make_writer(settings)
    except PlannerUnavailable:
        writer = None

    if thread is None:
        thread = _ensure_thread(account, None, payload.message)
    history = _history_of(payload, account, thread)
    thread_payload = _thread_payload(thread)

    async def generate():
        # 第一帧就把对话身份告诉前端：新建时它还没有 id，靠这帧挂到侧栏。
        yield _sse({"type": "conversation", **thread_payload})

        loop = asyncio.get_running_loop()
        events: asyncio.Queue = asyncio.Queue()
        stop = threading.Event()

        def deliver(item) -> None:
            loop.call_soon_threadsafe(events.put_nowait, item)

        def pump() -> None:
            try:
                for event in graph.stream(
                    payload.message,
                    ctx=_ctx(account),
                    planner=planner,
                    writer=writer,
                    history=history,
                    stop=stop,
                ):
                    # 只在拿到 final 后落库：中途断开的半截回答不该进入下一轮上下文。
                    if event.get("type") == "final":
                        _remember(
                            account, thread, payload.message, str(event.get("answer") or "")
                        )
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
            # 流结束（成功或失败）时顺手做一次节流清理；不阻塞收尾。
            accounts.maybe_prune(settings)

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


@router.get("/history")
def chat_history(
    request: Request,
    limit: int = Query(MAX_HISTORY_TURNS, ge=1, le=MAX_HISTORY_TURNS),
    conversation_id: int | None = Query(None, ge=1),
) -> dict:
    """某个对话最近的几条消息，按时间正序。

    不传 ``conversation_id`` 时取当前账号最近的对话（兼容老前端）。
    只回 ``role``/``content``：历史消息没有证据与召回过程，前端按普通文本渲染，
    ``[n]`` 不做成可点角标。
    """
    account = accounts.account_of(request)
    if conversation_id is None:
        latest = db.latest_thread(settings.db_path, account["id"])
        if latest is None:
            return {"messages": []}
        conversation_id = latest["id"]
    messages = db.thread_messages(
        settings.db_path, account["id"], conversation_id, limit=limit
    )
    # 保持老响应形状（role/content）：created_at 是内部字段，前端回填不需要。
    return {
        "messages": [{"role": item["role"], "content": item["content"]} for item in messages]
    }


@router.delete("/history")
def clear_history(
    request: Request, conversation_id: int | None = Query(None, ge=1)
) -> dict:
    """删掉一个对话（传 id）或本账号全部对话（不传）。关注项不受影响。"""
    account = accounts.account_of(request)
    if conversation_id is not None:
        return {"removed": db.delete_thread(settings.db_path, account["id"], conversation_id)}
    return {"removed": db.clear_threads(settings.db_path, account["id"])}


@router.get("/runs")
def chat_runs(request: Request, limit: int = Query(10, ge=1, le=100)) -> dict:
    """最近几次 run 的轨迹，只含当前账号。排查"它为什么没找到"靠这个。"""
    account = accounts.account_of(request)
    records = trace.tail(limit, settings, account_id=account["id"])
    return {"count": len(records), "runs": records}


# ------------------------------------------------------------------ 对话管理


@threads_router.get("")
def list_threads(request: Request) -> dict:
    account = accounts.account_of(request)
    threads = db.list_threads(settings.db_path, account["id"])
    return {"threads": [_thread_payload(item) for item in threads]}


@threads_router.post("", status_code=201)
def create_thread(request: Request) -> dict:
    account = accounts.account_of(request)
    thread = db.create_thread(settings.db_path, account["id"], DEFAULT_THREAD_TITLE)
    accounts.maybe_prune(settings)
    return {"thread": _thread_payload(thread)}


@threads_router.get("/{thread_id}/messages")
def thread_messages(
    thread_id: int,
    request: Request,
    limit: int = Query(db.KEEP_MESSAGES_PER_THREAD, ge=1, le=db.KEEP_MESSAGES_PER_THREAD),
) -> dict:
    account = accounts.account_of(request)
    thread = db.get_thread(settings.db_path, account["id"], thread_id)
    if thread is None:
        raise HTTPException(status_code=404, detail="对话不存在或不属于当前账号")
    messages = db.thread_messages(
        settings.db_path, account["id"], thread_id, limit=limit
    )
    return {"thread": _thread_payload(thread), "messages": messages}


@threads_router.delete("/{thread_id}")
def remove_thread(thread_id: int, request: Request) -> dict:
    account = accounts.account_of(request)
    if db.get_thread(settings.db_path, account["id"], thread_id) is None:
        raise HTTPException(status_code=404, detail="对话不存在或不属于当前账号")
    return {"removed": db.delete_thread(settings.db_path, account["id"], thread_id)}
