"""后台任务队列（SQLite）。

为什么不是 Redis + RQ/arq：这是**单用户、本机自托管**服务，`data/app.db` 已经在用
SQLite（WAL），任务状态没有理由再放第二个常驻服务里。RQ 的 worker 依赖 fork，
Windows 上根本跑不起来；多一个 Redis 就多一处备份、一个「它没起」的故障面。
将来真要扩成多实例，只需换掉本模块的存取实现，路由与前端不用动。

本模块只管**存取**（建表 / 入队 / 领取 / 完成 / 失败 / 恢复），执行在
``app.services.job_runner``。三条纪律：

* **幂等键在库里兜底**：同一 ``idempotency_key`` 只允许一个 ``queued/running``
  任务（部分唯一索引），重复提交返回已有任务，不新建、不报错；
* **中断不自动重跑**：服务重启时 ``running`` 一律标 ``interrupted``。跑了一半的
  任务有没有副作用，队列自己不知道，让人看到「中断」并手动重试，比悄悄重跑诚实；
* **进度只报拿得到的**：``progress`` 是执行方写什么就是什么，本模块不补百分比。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from app import db

SCHEMA_VERSION = 1

ACTIVE_STATUSES = ("queued", "running")
TERMINAL_STATUSES = ("succeeded", "failed", "interrupted")
STATUSES = (*ACTIVE_STATUSES, *TERMINAL_STATUSES)

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    kind            TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    payload         TEXT NOT NULL DEFAULT '{}',
    status          TEXT NOT NULL,
    attempts        INTEGER NOT NULL DEFAULT 0,
    max_attempts    INTEGER NOT NULL DEFAULT 1,
    timeout_seconds INTEGER NOT NULL DEFAULT 900,
    progress        TEXT NOT NULL DEFAULT '{}',
    result          TEXT,
    error           TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL,
    scheduled_at    TEXT NOT NULL,
    started_at      TEXT,
    finished_at     TEXT,
    lease_until     TEXT
);

-- 幂等：同一键只允许一个活跃任务。靠库约束而不是「先查再插」，
-- 并发提交时第二条会被这里的 IntegrityError 挡下（用例见 tests/test_jobs.py）。
CREATE UNIQUE INDEX IF NOT EXISTS jobs_active_key ON jobs(idempotency_key)
    WHERE status IN ('queued', 'running');

CREATE INDEX IF NOT EXISTS jobs_pick ON jobs(status, scheduled_at, id);
"""


class QueueUnavailable(RuntimeError):
    """队列不可用（库写不进去 / 表没建）。调用方返回 503，不静默改走同步。"""


def init(db_path: Path) -> None:
    with db.connect(db_path) as conn:
        conn.executescript(SCHEMA)


def _future(seconds: float) -> str:
    return (datetime.now() + timedelta(seconds=max(0.0, seconds))).isoformat(
        timespec="seconds"
    )


def backoff_seconds(attempts: int, *, base: float = 30.0, cap: float = 300.0) -> float:
    """第 ``attempts`` 次失败后的等待：30s → 90s → 270s，封顶 5 分钟。

    ``attempts`` 是**已经跑过**的次数（领取时 +1），所以第一次失败后是 30s。
    """
    if attempts <= 1:
        return base
    return min(cap, base * (3 ** (attempts - 1)))


def _loads(raw: str | None, fallback: Any) -> Any:
    if not raw:
        return fallback
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return fallback


def _row(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    item = dict(row)
    item["payload"] = _loads(item.get("payload"), {})
    item["progress"] = _loads(item.get("progress"), {})
    item["result"] = _loads(item.get("result"), None)
    return item


def get(db_path: Path, job_id: int) -> dict | None:
    with db.connect(db_path) as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return _row(row)


def list_jobs(
    db_path: Path,
    *,
    kind: str | None = None,
    status: str | None = None,
    limit: int = 20,
) -> list[dict]:
    clauses: list[str] = []
    params: list[Any] = []
    if kind:
        clauses.append("kind = ?")
        params.append(kind)
    if status:
        clauses.append("status = ?")
        params.append(status)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(limit)
    with db.connect(db_path) as conn:
        rows = conn.execute(
            f"SELECT * FROM jobs {where} ORDER BY id DESC LIMIT ?", params
        ).fetchall()
    return [_row(row) for row in rows]  # type: ignore[misc]


def enqueue(
    db_path: Path,
    kind: str,
    *,
    idempotency_key: str,
    payload: dict | None = None,
    max_attempts: int = 1,
    timeout_seconds: int = 900,
    run_at: str | None = None,
) -> tuple[dict, bool]:
    """入队；返回 ``(任务, 是否新建)``。

    ``BEGIN IMMEDIATE`` 让并发提交串行通过唯一索引：两个请求同时进来，只有一个
    能插入成功，另一个读到同一个任务。重复提交不是错误——前端轮询期间重试、
    两个标签页同时打开都会走到这里。
    """
    now = db.now()
    payload_json = json.dumps(payload or {}, ensure_ascii=False)
    try:
        with db.connect(db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT * FROM jobs WHERE idempotency_key = ? "
                "AND status IN ('queued', 'running')",
                (idempotency_key,),
            ).fetchone()
            if existing is not None:
                return _row(existing), False
            try:
                cursor = conn.execute(
                    "INSERT INTO jobs (kind, idempotency_key, payload, status, "
                    "attempts, max_attempts, timeout_seconds, progress, result, error, "
                    "created_at, scheduled_at) "
                    "VALUES (?, ?, ?, 'queued', 0, ?, ?, '{}', NULL, '', ?, ?)",
                    (
                        kind,
                        idempotency_key,
                        payload_json,
                        max(1, int(max_attempts)),
                        max(1, int(timeout_seconds)),
                        now,
                        run_at or now,
                    ),
                )
            except sqlite3.IntegrityError:
                # 另一个进程在查与插之间抢先提交（单进程下走不到，留给多 worker）。
                row = conn.execute(
                    "SELECT * FROM jobs WHERE idempotency_key = ? "
                    "AND status IN ('queued', 'running')",
                    (idempotency_key,),
                ).fetchone()
                if row is None:  # pragma: no cover - 约束刚放下又消失，只能报错
                    raise QueueUnavailable("任务入队失败：幂等键冲突") from None
                return _row(row), False
            row = conn.execute(
                "SELECT * FROM jobs WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
    except sqlite3.Error as exc:
        raise QueueUnavailable(f"任务入队失败：{exc}") from exc
    return _row(row), True


def claim(db_path: Path, *, now: str | None = None) -> dict | None:
    """领取一个到期的排队任务：置 ``running``、``attempts + 1``、写租约。"""
    moment = now or db.now()
    with db.connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM jobs WHERE status = 'queued' AND scheduled_at <= ? "
            "ORDER BY scheduled_at, id LIMIT 1",
            (moment,),
        ).fetchone()
        if row is None:
            return None
        lease = _future(int(row["timeout_seconds"] or 900))
        conn.execute(
            "UPDATE jobs SET status = 'running', attempts = attempts + 1, "
            "started_at = ?, lease_until = ? WHERE id = ? AND status = 'queued'",
            (moment, lease, row["id"]),
        )
        updated = conn.execute("SELECT * FROM jobs WHERE id = ?", (row["id"],)).fetchone()
    return _row(updated)


def set_progress(db_path: Path, job_id: int, progress: dict) -> None:
    with db.connect(db_path) as conn:
        conn.execute(
            "UPDATE jobs SET progress = ? WHERE id = ? AND status = 'running'",
            (json.dumps(progress, ensure_ascii=False), job_id),
        )


def succeed(db_path: Path, job_id: int, result: dict | None = None) -> None:
    with db.connect(db_path) as conn:
        conn.execute(
            "UPDATE jobs SET status = 'succeeded', result = ?, error = '', "
            "finished_at = ?, lease_until = NULL WHERE id = ?",
            (json.dumps(result or {}, ensure_ascii=False), db.now(), job_id),
        )


def fail(
    db_path: Path,
    job_id: int,
    error: str,
    *,
    retryable: bool = False,
    backoff: float | None = None,
) -> dict | None:
    """失败落库；可重试且还有次数时回到 ``queued``（带退避），否则 ``failed``。"""
    row = get(db_path, job_id)
    if row is None:
        return None
    can_retry = retryable and int(row["attempts"]) < int(row["max_attempts"])
    with db.connect(db_path) as conn:
        if can_retry:
            wait = backoff_seconds(int(row["attempts"])) if backoff is None else backoff
            conn.execute(
                "UPDATE jobs SET status = 'queued', error = ?, scheduled_at = ?, "
                "started_at = NULL, lease_until = NULL WHERE id = ?",
                (error[:1000], _future(wait), job_id),
            )
        else:
            conn.execute(
                "UPDATE jobs SET status = 'failed', error = ?, finished_at = ?, "
                "lease_until = NULL WHERE id = ?",
                (error[:1000], db.now(), job_id),
            )
    return get(db_path, job_id)


def interrupt(db_path: Path, job_id: int, reason: str) -> dict | None:
    with db.connect(db_path) as conn:
        conn.execute(
            "UPDATE jobs SET status = 'interrupted', error = ?, finished_at = ?, "
            "lease_until = NULL WHERE id = ?",
            (reason[:1000], db.now(), job_id),
        )
    return get(db_path, job_id)


def cancel(db_path: Path, job_id: int) -> bool:
    """取消**排队中**的任务；已经在跑的不支持中途取消（扫描会跑完当前项）。"""
    with db.connect(db_path) as conn:
        cursor = conn.execute(
            "UPDATE jobs SET status = 'interrupted', error = '已取消（未执行）', "
            "finished_at = ? WHERE id = ? AND status = 'queued'",
            (db.now(), job_id),
        )
        return cursor.rowcount > 0


def recover_running(db_path: Path, reason: str = "服务重启中断") -> int:
    """把上次进程留下的 ``running`` 标成 ``interrupted``（不自动重跑）。"""
    with db.connect(db_path) as conn:
        cursor = conn.execute(
            "UPDATE jobs SET status = 'interrupted', error = ?, finished_at = ?, "
            "lease_until = NULL WHERE status = 'running'",
            (reason, db.now()),
        )
        return cursor.rowcount


def prune(db_path: Path, *, keep: int = 200) -> int:
    """只留最近 ``keep`` 条终态任务；排队/运行中的永远不动。"""
    with db.connect(db_path) as conn:
        cursor = conn.execute(
            "DELETE FROM jobs WHERE status IN ('succeeded', 'failed', 'interrupted') "
            "AND id NOT IN ("
            "  SELECT id FROM jobs WHERE status IN "
            "  ('succeeded', 'failed', 'interrupted') ORDER BY id DESC LIMIT ?"
            ")",
            (max(0, keep),),
        )
        return cursor.rowcount
