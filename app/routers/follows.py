"""关注项接口（形态甲：页面内面板 + 打开时轮询）。

* 关注项本身只是「关键词 + 来源」；扫描在 ``services/follow_watch.py``。
* 扫描由前端触发，后端不常驻定时任务——理由见 follow_watch 的模块说明。
* 通道不可用与「没有更新」严格区分：前者返回 unavailable，不写事件、不动基线。
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app import db, jobs
from app.agent import channels
from app.config import settings
from app.routers.jobs import public as job_public
from app.services import follow_watch, job_runner

router = APIRouter(prefix="/api/follows", tags=["follows"])

FollowSource = Literal["own", "real", "lolibrary", "xhs", "weibo", "web"]

SOURCE_LABELS = {
    "own": "自有知识库",
    "real": "过往网友讨论",
    "lolibrary": "Lolibrary 档案",
    "xhs": "小红书",
    "weibo": "微博",
    "web": "网页搜索",
}


class FollowIn(BaseModel):
    subject: str = Field(..., min_length=1, max_length=60)
    source: FollowSource = "web"
    url: str = Field("", max_length=500)


def _snapshot() -> dict:
    follows = [
        {
            "id": follow["id"],
            "subject": follow["subject"],
            "source": follow["source"],
            "url": follow["url"],
            "updated_at": follow["updated_at"],
        }
        for follow in db.list_follows(settings.db_path)
    ]
    # 与 /api/chat/status 同源：关注项能用的来源必须和召回通道的就绪判断一致，
    # 否则会出现「聊天里说小红书不可用，关注面板却允许选」。
    status = channels.channel_status(settings)
    return {
        "follows": follows,
        "events": db.list_follow_events(settings.db_path, limit=20),
        "unread": db.unread_follow_events(settings.db_path),
        "sources": [
            {"name": name, "label": label, **status[name]}
            for name, label in SOURCE_LABELS.items()
        ],
    }


@router.get("")
def list_follows() -> dict:
    return _snapshot()


@router.post("")
def create_follow(payload: FollowIn) -> dict:
    subject = payload.subject.strip()
    if not subject:
        raise HTTPException(status_code=422, detail="关注关键词不能为空")
    try:
        db.add_follow(settings.db_path, subject, payload.source, payload.url.strip())
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _snapshot()


@router.delete("/{follow_id}")
def remove_follow(follow_id: int) -> dict:
    if not db.delete_follow(settings.db_path, follow_id):
        raise HTTPException(status_code=404, detail="关注项不存在")
    return _snapshot()


@router.post("/check")
def check_follows(
    wait: bool = Query(
        False,
        description="true 时同步执行并返回旧格式（脚本兼容）；默认入队后立刻返回 202",
    ),
):
    """扫描全部关注项。

    默认入队：立刻返回 202 + ``job_id``，用 ``GET /api/jobs/{id}`` 轮询结果。
    同一时刻只允许一个活跃扫描（幂等键 ``follows.scan``），重复提交返回同一个
    任务（200 + ``deduplicated=true``），不会重复写事件。

    ``?wait=1`` 或 ``QUEUE_MODE=inline`` 时走同步老路径，返回完整结果。
    """
    if wait or not job_runner.queue_enabled(settings):
        return follow_watch.check_all(settings)

    runner = job_runner.current()
    if runner is None or not runner.alive:
        raise HTTPException(
            status_code=503,
            detail=(
                "后台队列未运行：请重启服务，或设 QUEUE_MODE=inline，"
                "也可以临时用 POST /api/follows/check?wait=1"
            ),
        )
    try:
        job, created = runner.enqueue(
            "follows.scan",
            idempotency_key="follows.scan",
            max_attempts=2,
        )
    except jobs.QueueUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    payload = {**job_public(job), "deduplicated": not created}
    return JSONResponse(payload, status_code=202 if created else 200)


@router.post("/events/read")
def read_events() -> dict:
    marked = db.mark_follow_events_read(settings.db_path)
    return {**_snapshot(), "marked": marked}
