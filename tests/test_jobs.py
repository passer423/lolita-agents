"""后台任务队列：持久化、幂等、重试退避、恢复与接口形态。

这些用例刻意覆盖「文档里写了但没验证」的部分：

* 幂等靠 SQLite 部分唯一索引兜底，**并发提交**也只能产生一个任务；
* 失败重试有次数与退避，次数用完落 failed；
* 进程留下的 running 在启动时标 interrupted，不自动重跑；
* ``/api/follows/check`` 默认 202 + 轮询，``?wait=1`` 保持旧格式；
* 进度只报真实发生过的数，没有百分比。
"""

from __future__ import annotations

import threading
import time

import pytest

from app import jobs
from app.agent import channels
from app.config import settings
from app.services import job_runner


def _evidence(url: str, title: str = "新款") -> dict:
    return {
        "channel": "web",
        "source_kind": "realtime",
        "ref": url,
        "title": title,
        "text": "内容",
        "url": url,
    }


def _wait_job(client, job_id: int, timeout: float = 10.0) -> dict:
    deadline = time.monotonic() + timeout
    body: dict = {}
    while time.monotonic() < deadline:
        body = client.get(f"/api/jobs/{job_id}").json()
        if body["status"] in ("succeeded", "failed", "interrupted"):
            return body
        time.sleep(0.05)
    raise AssertionError(f"任务 {job_id} 在 {timeout}s 内没有结束：{body}")


# --------------------------------------------------------------------- 存取层


def test_enqueue_dedupes_active_key(data_dir):
    jobs.init(settings.db_path)
    first, created = jobs.enqueue(
        settings.db_path, "follows.scan", idempotency_key="follows.scan"
    )
    second, created_again = jobs.enqueue(
        settings.db_path, "follows.scan", idempotency_key="follows.scan"
    )
    assert created is True
    assert created_again is False
    assert first["id"] == second["id"]

    # 终态后幂等键释放：下一轮扫描是新的任务
    jobs.succeed(settings.db_path, first["id"], {"ok": True})
    third, created_third = jobs.enqueue(
        settings.db_path, "follows.scan", idempotency_key="follows.scan"
    )
    assert created_third is True
    assert third["id"] != first["id"]


def test_concurrent_enqueue_creates_exactly_one_job(data_dir):
    """并发/重复提交由唯一索引兜底，不是「先查再插」碰运气。"""
    jobs.init(settings.db_path)
    results: list[tuple[int, bool]] = []
    lock = threading.Lock()

    def submit() -> None:
        job, created = jobs.enqueue(
            settings.db_path, "follows.scan", idempotency_key="follows.scan"
        )
        with lock:
            results.append((int(job["id"]), created))

    threads = [threading.Thread(target=submit) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(results) == 8
    assert len({job_id for job_id, _ in results}) == 1
    assert sum(1 for _, created in results if created) == 1


def test_retry_uses_backoff_then_fails_when_exhausted(data_dir):
    jobs.init(settings.db_path)
    job, _ = jobs.enqueue(
        settings.db_path,
        "follows.scan",
        idempotency_key="follows.scan",
        max_attempts=2,
    )

    claimed = jobs.claim(settings.db_path)
    assert claimed is not None
    assert claimed["attempts"] == 1

    after_first = jobs.fail(settings.db_path, job["id"], "网络抖动", retryable=True)
    assert after_first is not None
    assert after_first["status"] == "queued"
    # 30s 退避：现在不应该被领取到
    assert jobs.claim(settings.db_path) is None

    retried = jobs.claim(settings.db_path, now=after_first["scheduled_at"])
    assert retried is not None
    assert retried["attempts"] == 2

    after_second = jobs.fail(settings.db_path, job["id"], "还是失败", retryable=True)
    assert after_second is not None
    assert after_second["status"] == "failed"
    assert "还是失败" in after_second["error"]


def test_non_retryable_failure_stops_immediately(data_dir):
    jobs.init(settings.db_path)
    job, _ = jobs.enqueue(
        settings.db_path,
        "kb.rebuild",
        idempotency_key="kb.rebuild:full",
        max_attempts=3,
    )
    jobs.claim(settings.db_path)
    failed = jobs.fail(settings.db_path, job["id"], "上游路径错", retryable=False)
    assert failed is not None
    assert failed["status"] == "failed"
    assert failed["attempts"] == 1


def test_recover_running_marks_interrupted_without_rerun(data_dir):
    jobs.init(settings.db_path)
    job, _ = jobs.enqueue(
        settings.db_path, "follows.scan", idempotency_key="follows.scan"
    )
    jobs.claim(settings.db_path)

    assert jobs.recover_running(settings.db_path) == 1
    recovered = jobs.get(settings.db_path, job["id"])
    assert recovered is not None
    assert recovered["status"] == "interrupted"
    assert recovered["attempts"] == 1
    # 不会自动回到队列
    assert jobs.list_jobs(settings.db_path, status="queued") == []


def test_cancel_only_queued_jobs(data_dir):
    jobs.init(settings.db_path)
    job, _ = jobs.enqueue(
        settings.db_path, "follows.scan", idempotency_key="follows.scan"
    )
    assert jobs.cancel(settings.db_path, job["id"]) is True
    assert jobs.cancel(settings.db_path, job["id"]) is False
    assert jobs.get(settings.db_path, job["id"])["status"] == "interrupted"


# --------------------------------------------------------------------- 接口层


def test_follows_check_returns_202_then_dedupes(client, monkeypatch):
    """默认入队：第一次 202，重复提交 200 + 同一 job_id，轮询拿结果。"""
    monkeypatch.setattr(
        channels,
        "recall",
        lambda *_a, **_k: (time.sleep(0.4), [_evidence("https://a/1")])[1],
    )
    client.post("/api/follows", json={"subject": "AP 新款", "source": "web"})

    first = client.post("/api/follows/check")
    assert first.status_code == 202
    body = first.json()
    assert body["status"] in ("queued", "running")
    assert body["poll_url"] == f"/api/jobs/{body['job_id']}"
    assert body["deduplicated"] is False

    second = client.post("/api/follows/check")
    assert second.status_code == 200
    assert second.json()["job_id"] == body["job_id"]
    assert second.json()["deduplicated"] is True

    done = _wait_job(client, body["job_id"])
    assert done["status"] == "succeeded"
    assert done["result"]["results"][0]["status"] == "baseline"
    # 进度只报真实发生过的数，没有百分比
    assert done["progress"]["total"] == 1
    assert done["progress"]["done"] == 1
    assert not any("percent" in key or "pct" in key for key in done["progress"])


def test_follows_check_wait_keeps_legacy_shape(client):
    body = client.post("/api/follows/check?wait=1").json()
    assert body["busy"] is False
    assert "results" in body
    assert "unread" in body


def test_follows_check_falls_back_inline_without_queue(client, monkeypatch):
    monkeypatch.setattr(job_runner, "queue_enabled", lambda app=None: False)
    body = client.post("/api/follows/check").json()
    assert body["busy"] is False
    assert "results" in body


def test_follows_check_503_when_worker_is_down(client, monkeypatch):
    from app.routers import follows as follows_router

    monkeypatch.setattr(follows_router.job_runner, "current", lambda: None)
    response = client.post("/api/follows/check")
    assert response.status_code == 503
    assert "QUEUE_MODE=inline" in response.json()["detail"]


def test_jobs_list_and_404(client):
    assert client.get("/api/jobs").json()["worker_alive"] is True
    assert client.get("/api/jobs/999999").status_code == 404
    assert client.post("/api/jobs/999999/retry").status_code == 404
    assert client.delete("/api/jobs/999999").status_code == 404


def test_kb_rebuild_is_disabled_by_default(client):
    response = client.post("/api/kb/rebuild", json={"rebuild": True})
    assert response.status_code == 403
    assert "KB_REBUILD_API" in response.json()["detail"]


def test_kb_rebuild_enqueues_when_enabled(client, monkeypatch):
    from app.routers import kb as kb_router

    monkeypatch.setattr(kb_router, "kb_rebuild_enabled", lambda app=None: True)
    monkeypatch.setitem(
        job_runner.HANDLERS, "kb.rebuild", lambda job, ctx: {"fake": True}
    )

    response = client.post("/api/kb/rebuild", json={"rebuild": True})
    assert response.status_code == 202
    done = _wait_job(client, response.json()["job_id"])
    assert done["status"] == "succeeded"
    assert done["result"] == {"fake": True}


def test_retry_endpoint_requeues_failed_job(client, monkeypatch):
    # 先停 worker，避免它在测试准备任务时抢先领取；准备完再启动。
    runner = job_runner.current()
    assert runner is not None
    runner.stop()

    job, _ = jobs.enqueue(
        settings.db_path,
        "kb.rebuild",
        idempotency_key="kb.rebuild:full",
        payload={"rebuild": True},
    )
    jobs.claim(settings.db_path)
    jobs.fail(settings.db_path, job["id"], "失败", retryable=False)

    # 换掉真实处理函数，避免重试时真的去建索引
    monkeypatch.setitem(
        job_runner.HANDLERS, "kb.rebuild", lambda item, ctx: {"retried": True}
    )
    runner.start()

    response = client.post(f"/api/jobs/{job['id']}/retry")
    assert response.status_code == 202
    assert response.json()["job_id"] != job["id"]
    done = _wait_job(client, response.json()["job_id"])
    assert done["result"] == {"retried": True}


@pytest.mark.parametrize("limit", [0, 201])
def test_jobs_list_rejects_bad_limit(client, limit):
    assert client.get(f"/api/jobs?limit={limit}").status_code == 422
