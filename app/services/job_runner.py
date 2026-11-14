"""后台任务执行器：一个线程，串行领取 ``app.jobs`` 里的任务。

为什么单线程：单用户本地服务，同一时刻只需要一件事在后台跑。xhs 会起浏览器
子进程、索引重建吃 CPU/内存，并发只会互相拖慢；串行也让「同一时间只有一轮
关注扫描」这件事由队列天然保证。

任务处理函数签名 ``handler(job, ctx) -> dict``，返回值落进 ``result``。可以报
进度的任务自己调 ``ctx.progress(...)``；**拿不到真实进度就不要写百分比**，
只报 ``phase``（见 kb.rebuild）。

停机语义：``stop()`` 置位停止标志并终止当前子进程；正在跑的关注扫描会在
**项间**收尾。来不及收尾的任务留在 ``running``，下次启动由
``jobs.recover_running`` 标成 ``interrupted``——不自动重跑。
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
from collections.abc import Callable
from typing import Any

from app import bridge, jobs
from app.config import APP_ROOT, AppSettings
from app.config import settings as default_settings

logger = logging.getLogger(__name__)

POLL_SECONDS = 1.0
SCAN_EXTRA_SECONDS = 60.0
DEFAULT_KB_TIMEOUT = 1800


class JobTransient(RuntimeError):
    """临时故障：值得按退避再试一次（网络/锁冲突/另一轮扫描正在跑）。"""


class JobTimeout(RuntimeError):
    """超过任务自己的上限。重跑通常还是同样结果，不自动重试。"""


class JobInterrupted(RuntimeError):
    """服务停机打断。"""


def queue_mode(app: AppSettings | None = None) -> str:
    app = app or default_settings
    return str(getattr(app, "queue_mode", "async") or "async").strip().lower()


def queue_enabled(app: AppSettings | None = None) -> bool:
    """``QUEUE_MODE=inline`` 或 ``QUEUE_ENABLED=false`` 时退回同步执行。"""
    app = app or default_settings
    if not bool(getattr(app, "queue_enabled", True)):
        return False
    return queue_mode(app) != "inline"


class JobContext:
    """给处理函数的上下文：进度、停止标志、子进程句柄。"""

    def __init__(
        self,
        job: dict,
        app: AppSettings,
        *,
        stop_event: threading.Event,
        timeout: float,
    ) -> None:
        self.job = job
        self.app = app
        self.stop_event = stop_event
        self.timeout = timeout
        self._process: subprocess.Popen | None = None
        self._lock = threading.Lock()

    # ---------------------------------------------------------------- 进度
    def progress(self, **fields: Any) -> None:
        """写进度快照。调用方负责只放真实值（例如 ``phase``），不要编百分比。"""
        try:
            jobs.set_progress(self.app.db_path, int(self.job["id"]), fields)
        except Exception as exc:  # noqa: BLE001 - 进度写不进去不能让任务失败
            logger.warning("任务 %s 进度写入失败：%s", self.job.get("id"), exc)

    # ---------------------------------------------------------------- 停机
    @property
    def stop_requested(self) -> bool:
        return self.stop_event.is_set()

    def attach_process(self, process: subprocess.Popen | None) -> None:
        with self._lock:
            self._process = process

    def cancel_process(self, *, grace: float = 5.0) -> None:
        """SIGTERM 当前子进程，宽限期后 kill。用于停机与超时。"""
        with self._lock:
            process = self._process
        if process is None or process.poll() is not None:
            return
        try:
            process.terminate()
            process.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            process.kill()
        except OSError:
            pass


# --------------------------------------------------------------------- 处理函数


def _handle_follows_scan(job: dict, ctx: JobContext) -> dict:
    from app.services import follow_watch

    def progress(fields: dict) -> None:
        ctx.progress(**fields)

    try:
        return follow_watch.scan(ctx.app, progress=progress, stop=ctx.stop_event)
    except follow_watch.ScanBusy as exc:
        # 同步入口（?wait=1）正在跑同一件事：按临时故障退避再试，别直接判失败。
        raise JobTransient(str(exc)) from exc


def _handle_kb_rebuild(job: dict, ctx: JobContext) -> dict:
    """跑建索引脚本（子进程），成功后让服务进程重载索引。

    走子进程而不是 in-process 调 ``build_index``：torch/BGE 的内存随子进程退出
    释放，不会常驻在服务里；中断/超时也能真的杀掉。

    上游的写盘**不是整体原子**的：单个文件是「临时文件 + os.replace」，但 Chroma
    是增量持久化、manifest 最后写且直接 ``write_text``。所以这个任务默认不开放
    HTTP 触发（``KB_REBUILD_API=false``），失败/中断可能留下半成品索引，需要人
    看一眼再决定要不要重跑。
    """
    payload = job.get("payload") or {}
    rebuild = bool(payload.get("rebuild"))
    ctx.progress(phase="starting")

    command = [sys.executable, "scripts/build_kb_index.py"]
    if rebuild:
        command.append("--rebuild")
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    logger.info("任务 %s：开始建索引 %s", job.get("id"), command[1:])

    process = subprocess.Popen(
        command,
        cwd=str(APP_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    ctx.attach_process(process)
    ctx.progress(phase="building")
    try:
        output, _ = process.communicate(timeout=ctx.timeout)
    except subprocess.TimeoutExpired as exc:
        ctx.cancel_process()
        process.communicate()
        raise JobTimeout(f"建索引超过 {ctx.timeout:g} 秒，已终止") from exc
    finally:
        ctx.attach_process(None)

    tail = "\n".join((output or "").strip().splitlines()[-20:])
    if ctx.stop_requested:
        raise JobInterrupted("服务停机中断，索引可能未完成")
    if process.returncode != 0:
        # 建索引失败多为确定性原因（路径错 / 磁盘满 / 模型缺失），不做自动重试。
        raise RuntimeError(
            f"建索引失败（exit {process.returncode}）：{tail[-1200:] or '无输出'}"
        )

    ctx.progress(phase="reloading")
    reload_result = bridge.reload_bundle(ctx.app)
    return {"exit_code": 0, "log_tail": tail, "reload": reload_result}


HANDLERS: dict[str, Callable[[dict, JobContext], dict]] = {
    "follows.scan": _handle_follows_scan,
    "kb.rebuild": _handle_kb_rebuild,
}


# --------------------------------------------------------------------- 执行器


class JobRunner:
    def __init__(self, app: AppSettings | None = None) -> None:
        self.app = app or default_settings
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._current: JobContext | None = None
        self._lock = threading.Lock()

    # ---------------------------------------------------------------- 生命周期
    @property
    def alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self) -> None:
        """建表、恢复未完成任务、按需起线程。重复调用安全（测试会反复进出 lifespan）。"""
        jobs.init(self.app.db_path)
        recovered = jobs.recover_running(self.app.db_path)
        if recovered:
            logger.warning("上次退出时有 %s 个任务未收尾，已标为 interrupted", recovered)
        jobs.prune(self.app.db_path)
        if not queue_enabled(self.app):
            logger.info("后台任务队列未启用（QUEUE_MODE=%s）", queue_mode(self.app))
            return
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop = threading.Event()
            self._thread = threading.Thread(
                target=self._loop, name="job-runner", daemon=True
            )
            self._thread.start()
        logger.info("后台任务队列已启动")

    def stop(self, *, timeout: float = 10.0) -> None:
        self._stop.set()
        context = self._current
        if context is not None:
            context.cancel_process()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout)
            if thread.is_alive():
                logger.warning("后台任务未在 %.0f 秒内收尾，留给下次启动恢复", timeout)

    # ---------------------------------------------------------------- 入队
    def enqueue(
        self,
        kind: str,
        *,
        idempotency_key: str,
        payload: dict | None = None,
        max_attempts: int = 1,
        timeout_seconds: int | None = None,
    ) -> tuple[dict, bool]:
        if not self.alive:
            raise jobs.QueueUnavailable("后台队列未运行")
        if timeout_seconds is None:
            timeout_seconds = self.default_timeout(kind)
        return jobs.enqueue(
            self.app.db_path,
            kind,
            idempotency_key=idempotency_key,
            payload=payload,
            max_attempts=max_attempts,
            timeout_seconds=timeout_seconds,
        )

    def default_timeout(self, kind: str) -> int:
        if kind == "kb.rebuild":
            return int(getattr(self.app, "kb_rebuild_timeout", DEFAULT_KB_TIMEOUT))
        deadline = float(getattr(self.app, "follow_check_deadline", 120.0) or 0)
        return int(deadline + SCAN_EXTRA_SECONDS) if deadline > 0 else 600

    # ---------------------------------------------------------------- 主循环
    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                job = jobs.claim(self.app.db_path)
            except Exception as exc:  # noqa: BLE001 - 库暂时锁住不该让线程退出
                logger.warning("领取任务失败：%s", exc)
                job = None
            if job is None:
                self._stop.wait(POLL_SECONDS)
                continue
            self._execute(job)
            try:
                jobs.prune(self.app.db_path)
            except Exception as exc:  # noqa: BLE001
                logger.warning("清理任务记录失败：%s", exc)

    def _execute(self, job: dict) -> None:
        kind = str(job.get("kind") or "")
        handler = HANDLERS.get(kind)
        timeout = float(job.get("timeout_seconds") or 900)
        context = JobContext(job, self.app, stop_event=self._stop, timeout=timeout)
        self._current = context
        logger.info("任务 %s 开始：%s", job["id"], kind)
        try:
            if handler is None:
                raise JobTransient(f"未知任务类型：{kind}")
            result = handler(job, context)
            if context.stop_requested:
                jobs.interrupt(self.app.db_path, int(job["id"]), "服务停机中断")
            else:
                jobs.succeed(self.app.db_path, int(job["id"]), result)
                logger.info("任务 %s 完成：%s", job["id"], kind)
        except JobInterrupted as exc:
            jobs.interrupt(self.app.db_path, int(job["id"]), str(exc) or "服务停机中断")
        except JobTimeout as exc:
            jobs.fail(self.app.db_path, int(job["id"]), str(exc), retryable=False)
            logger.warning("任务 %s 超时：%s", job["id"], exc)
        except Exception as exc:  # noqa: BLE001 - 任务失败只影响这一条
            retryable = isinstance(
                exc, (JobTransient, TimeoutError, ConnectionError)
            )
            jobs.fail(
                self.app.db_path,
                int(job["id"]),
                f"{type(exc).__name__}: {exc}",
                retryable=retryable,
            )
            logger.warning(
                "任务 %s 失败（%s 可重试）：%s: %s",
                job["id"],
                "是" if retryable else "否",
                type(exc).__name__,
                exc,
            )
        finally:
            self._current = None


_runner: JobRunner | None = None


def get_runner(app: AppSettings | None = None) -> JobRunner:
    global _runner
    if _runner is None:
        _runner = JobRunner(app or default_settings)
    return _runner


def current() -> JobRunner | None:
    return _runner


def reset_runner() -> None:
    """测试用：丢掉单例（先 stop）。"""
    global _runner
    if _runner is not None:
        _runner.stop()
    _runner = None
