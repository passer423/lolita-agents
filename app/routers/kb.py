"""知识库索引接口：状态、重载与后台重建。

服务启动时只装载一次索引（见 ``bridge._ensure_loaded``），所以重建索引后必须
通知它换掉旧的那份；``scripts/build_kb_index.py --notify`` 打的就是
``POST /api/kb/reload``。索引没建好时这两个接口都返回 200 + ``ready=false``：
这是「还没跑建索引」，不是服务故障，不该让前端看到 500。

``POST /api/kb/rebuild`` 默认**关闭**（``KB_REBUILD_API=false``）。原因不是实现
不了，而是上游写盘不是整体原子的：单个文件（chunks / pickle / 缓存）走
「临时文件 + os.replace」，但 Chroma 是增量持久化、manifest 最后写且直接
``write_text``。中途被杀会留下半成品索引，得人看一眼再决定重跑。日常仍用
``python scripts/build_kb_index.py`` 或 ``docker compose run``；确认原子性后
再把开关打开。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app import bridge, jobs
from app.config import settings
from app.routers.jobs import public as job_public
from app.services import job_runner

router = APIRouter(prefix="/api/kb", tags=["kb"])


class RebuildIn(BaseModel):
    """``rebuild=true`` 全量重建；false 走增量（只编码新增/变更条目）。"""

    rebuild: bool = True


def kb_rebuild_enabled(app=None) -> bool:
    """HTTP 触发开关，默认关闭；配置项就绪后由 ``KB_REBUILD_API`` 控制。"""
    app = app or settings
    return bool(getattr(app, "kb_rebuild_api", False))


@router.get("/status")
def kb_status() -> dict:
    """两份索引分开报：``own`` 是自有知识库，``real`` 是上游真实语料。

    ``upstream`` 是路径自检：索引坏了要能一眼分清是「没建」还是「上游路径指错」。
    """
    return {
        "upstream": bridge.upstream_status(settings),
        "sources": {
            "own": bridge.index_status(settings, source="own"),
            "real": bridge.index_status(settings, source="real"),
        },
    }


@router.post("/reload")
def reload_kb() -> dict:
    """丢掉进程内缓存的索引并重载自有索引；``real`` 等下次查询再懒加载。"""
    return bridge.reload_bundle(settings)


@router.post("/rebuild")
def rebuild_kb(request: Request, payload: RebuildIn | None = None):
    """后台重建索引：202 + job_id，用 ``GET /api/jobs/{id}`` 轮询。

    默认禁用（见模块说明）。打开后任务在子进程里跑，成功后由 worker 重载索引。
    """
    if not kb_rebuild_enabled(settings):
        raise HTTPException(
            status_code=403,
            detail=(
                "HTTP 触发索引重建默认关闭（KB_REBUILD_API=false）："
                "上游写盘不是整体原子，中断可能留下半成品索引。"
                "请用 python scripts/build_kb_index.py 或 docker compose run。"
            ),
        )
    runner = getattr(request.app.state, "job_runner", None) or job_runner.current()
    if runner is None or not runner.alive:
        raise HTTPException(status_code=503, detail="后台队列未运行：请重启服务")
    rebuild = bool(payload.rebuild) if payload is not None else True
    mode = "full" if rebuild else "incremental"
    try:
        job, created = runner.enqueue(
            "kb.rebuild",
            idempotency_key=f"kb.rebuild:{mode}",
            payload={"rebuild": rebuild},
            max_attempts=1,
        )
    except jobs.QueueUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    body = {**job_public(job), "deduplicated": not created}
    return JSONResponse(body, status_code=202 if created else 200)
