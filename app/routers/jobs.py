"""后台任务接口：查状态、重试、取消。

任务本身由 ``app.services.job_runner`` 在后台线程执行；这里只读队列、不做业务。
接口形态与 ``POST /api/follows/check`` 的 202 配套：拿 ``job_id`` 后轮询
``GET /api/jobs/{id}``，直到 ``succeeded / failed / interrupted``。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from app import jobs
from app.config import settings
from app.services import job_runner

router = APIRouter(prefix="/api/jobs", tags=["jobs"])


def public(job: dict) -> dict:
    """对外的任务视图。``payload`` 只回显 kind 需要的少量参数，不泄内部结构。"""
    return {
        "job_id": job["id"],
        "kind": job["kind"],
        "status": job["status"],
        "attempts": job["attempts"],
        "max_attempts": job["max_attempts"],
        "progress": job.get("progress") or {},
        "result": job.get("result"),
        "error": job.get("error") or "",
        "created_at": job.get("created_at"),
        "started_at": job.get("started_at"),
        "finished_at": job.get("finished_at"),
        "poll_url": f"/api/jobs/{job['id']}",
    }


def _runner(request: Request):
    runner = getattr(request.app.state, "job_runner", None) or job_runner.current()
    if runner is None:
        raise HTTPException(
            status_code=503,
            detail="后台队列未启动：请重启服务，或设 QUEUE_MODE=inline 用同步模式",
        )
    return runner


@router.get("")
def list_jobs(
    request: Request,
    kind: str | None = Query(None),
    status: str | None = Query(None),
    limit: int = Query(20, ge=1, le=200),
) -> dict:
    if status and status not in jobs.STATUSES:
        raise HTTPException(status_code=422, detail=f"未知状态：{status}")
    runner = job_runner.current()
    records = jobs.list_jobs(
        settings.db_path, kind=kind, status=status, limit=limit
    )
    return {
        "worker_alive": bool(runner and runner.alive),
        "count": len(records),
        "jobs": [public(job) for job in records],
    }


@router.get("/{job_id}")
def get_job(job_id: int) -> dict:
    job = jobs.get(settings.db_path, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return public(job)


@router.post("/{job_id}/retry")
def retry_job(job_id: int, request: Request) -> JSONResponse:
    """手动重跑：克隆成新任务。中断/失败都不会自动重跑，靠这里显式触发。"""
    job = jobs.get(settings.db_path, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    if job["status"] in jobs.ACTIVE_STATUSES:
        raise HTTPException(status_code=409, detail="任务还在排队或运行中，不能重试")
    runner = _runner(request)
    try:
        created, is_new = runner.enqueue(
            job["kind"],
            idempotency_key=f"{job['idempotency_key']}:retry:{job['id']}",
            payload=job.get("payload") or {},
            max_attempts=int(job["max_attempts"]),
            timeout_seconds=int(job["timeout_seconds"]),
        )
    except jobs.QueueUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return JSONResponse(
        {**public(created), "deduplicated": not is_new}, status_code=202
    )


@router.delete("/{job_id}")
def cancel_job(job_id: int) -> dict:
    job = jobs.get(settings.db_path, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    if not jobs.cancel(settings.db_path, job_id):
        raise HTTPException(
            status_code=409, detail="只能取消排队中的任务（运行中的会跑完当前项）"
        )
    return public(jobs.get(settings.db_path, job_id) or job)
