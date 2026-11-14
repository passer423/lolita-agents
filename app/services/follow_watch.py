"""关注项扫描（形态甲：页面内触发，不做后端常驻定时任务）。

## 为什么不是常驻定时任务

本服务没有账号、没有 ``sw.js``、也没有通知承载面：后端即便定时扫出事件，用户
关着页面也收不到，只是往库里堆没人看的事件。所以扫描的触发权交给前端的
「检查更新」/打开页面时的轮询——页面关掉就停。这是**明确的取舍**，不假装实时。

## 什么叫「更新」

每次扫描把该关注项当前召回的结果存成快照（``follows.last_state``），下次扫描对比：

* 第一次扫描只记基线，不发通知（没有旧状态，说「更新了」是编的）；
* 新增 / 内容变化 / 减少都算更新，写一条 ``follow_events``；
* 通道不可用**不**算「没有更新」，也不更新基线——返回 unavailable，让界面显示原因。

快照只存前 8 条、每条正文截 300 字（与通道证据的上限一致），避免 last_state 膨胀。

## 时间预算

单项召回与整轮扫描都有上限（``FOLLOW_ITEM_TIMEOUT`` / ``FOLLOW_CHECK_DEADLINE``）。
超时的项状态为 ``timeout``、预算耗尽后未跑的项为 ``skipped``，两者都不写事件、
不动基线——「这轮没查完」和「没有更新」是两件事。
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
from collections.abc import Callable

from app import db
from app.agent import channels
from app.agent.channels import ChannelUnavailable, RecallContext
from app.config import AppSettings

logger = logging.getLogger(__name__)

MAX_SNAPSHOT_ITEMS = 8
MAX_EVENT_SUMMARY = 240

# 同一时刻只跑一轮扫描：前端轮询和手动点击可能撞在一起，串行化避免重复写事件。
_check_lock = threading.Lock()


class RecallTimeout(RuntimeError):
    """单项召回超过预算。

    超时既不是「没有更新」，也不是「通道不可用」：底下的请求可能还在跑，
    但结果已经不能用了（再等下去会把整轮扫描拖死）。调用方只报状态，不写基线。
    """


class ScanBusy(RuntimeError):
    """已有一轮扫描在跑。

    队列模式下一个 worker 串行执行，正常情况下不会撞上；``?wait=1`` 的同步
    调用可能和后台任务撞在一起，此时照旧返回 ``busy``，不排队、不重复写事件。
    """


def _recall_with_timeout(
    follow: dict,
    app: AppSettings,
    timeout: float | None,
) -> list[dict]:
    """在独立线程里跑召回，最多等 ``timeout`` 秒。

    线程无法安全强杀，超时后让它自己结束；关键是**结果丢弃、基线不写**。
    ``channels.recall`` 的底层请求都有自己的超时，这里只是兜住最坏情况。
    """
    box: queue.Queue = queue.Queue(maxsize=1)

    def worker() -> None:
        try:
            evidence = channels.recall(
                follow["source"],
                follow["subject"],
                RecallContext(settings=app),
            )
            box.put(("ok", evidence))
        except Exception as exc:  # noqa: BLE001 - 原样带回调用方分类
            box.put(("error", exc))

    thread = threading.Thread(
        target=worker,
        name=f"follow-{follow['id']}",
        daemon=True,
    )
    thread.start()
    try:
        kind, payload = box.get(timeout=timeout)
    except queue.Empty as exc:
        raise RecallTimeout(
            f"召回超过 {timeout:g} 秒未返回" if timeout else "召回超时"
        ) from exc
    if kind == "error":
        raise payload
    return payload


def _items(evidence: list[dict]) -> list[dict]:
    items: list[dict] = []
    seen: set[str] = set()
    for item in evidence:
        key = str(item.get("url") or item.get("ref") or item.get("title") or "").strip()
        if not key or key in seen:
            continue
        seen.add(key)
        items.append(
            {
                "key": key,
                "title": str(item.get("title") or key)[:120],
                "text": str(item.get("text") or "")[:300],
            }
        )
        if len(items) >= MAX_SNAPSHOT_ITEMS:
            break
    return items


def _load_state(raw: str | None) -> list[dict] | None:
    """解析上次快照。空 / 坏数据都当作「没有基线」，不要猜。"""
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        return None
    items: list[dict] = []
    for item in data["items"]:
        if isinstance(item, dict) and item.get("key"):
            items.append(
                {
                    "key": str(item["key"]),
                    "title": str(item.get("title") or item["key"]),
                    "text": str(item.get("text") or ""),
                }
            )
    return items


def _describe(prefix: str, items: list[dict]) -> str:
    titles = [str(item.get("title") or item.get("key")) for item in items[:3]]
    suffix = "…" if len(items) > 3 else ""
    return f"{prefix} {len(items)} 条：{'、'.join(titles)}{suffix}"


def _diff_summary(old: list[dict], new: list[dict]) -> str:
    old_map = {item["key"]: item for item in old}
    new_map = {item["key"]: item for item in new}
    added = [item for key, item in new_map.items() if key not in old_map]
    removed = [item for key, item in old_map.items() if key not in new_map]
    changed = [
        item
        for key, item in new_map.items()
        if key in old_map
        and (item.get("title") != old_map[key].get("title")
             or item.get("text") != old_map[key].get("text"))
    ]

    parts: list[str] = []
    if added:
        parts.append(_describe("新增", added))
    if changed:
        parts.append(_describe("内容有变化", changed))
    if removed:
        parts.append(f"减少 {len(removed)} 条")
    return "；".join(parts)[:MAX_EVENT_SUMMARY]


def _check_one(
    follow: dict,
    app: AppSettings,
    *,
    timeout: float | None = None,
) -> dict:
    result = {
        "follow_id": follow["id"],
        "subject": follow["subject"],
        "source": follow["source"],
    }
    try:
        evidence = _recall_with_timeout(follow, app, timeout)
    except RecallTimeout as exc:
        # 超时可能只是这一轮慢；基线保持原样，下次扫描继续和旧快照比。
        return {
            **result,
            "status": "timeout",
            "detail": f"{exc}；未写事件、基线未变",
        }
    except ChannelUnavailable as exc:
        # 通道不可用 ≠ 没有更新：不写事件、不动基线，把原因原样带回界面。
        return {**result, "status": "unavailable", "detail": str(exc)[:200]}
    except Exception as exc:  # noqa: BLE001 - 单条关注失败不拖垮整轮扫描
        logger.warning("关注项 %s 扫描失败：%s: %s", follow["id"], type(exc).__name__, exc)
        return {
            **result,
            "status": "error",
            "detail": f"{type(exc).__name__}: {exc}"[:200],
        }

    items = _items(evidence)
    old = _load_state(follow.get("last_state"))
    db.update_follow_state(
        app.db_path, follow["id"], json.dumps({"items": items}, ensure_ascii=False)
    )

    if old is None:
        return {
            **result,
            "status": "baseline",
            "detail": f"已记录 {len(items)} 条作为基线，下次检查开始对比",
        }

    summary = _diff_summary(old, items)
    if not summary:
        return {**result, "status": "unchanged", "detail": f"{len(items)} 条，无变化"}

    event = db.add_follow_event(app.db_path, follow["id"], summary)
    return {**result, "status": "updated", "detail": summary, "event_id": event["id"]}


def _status_counts(results: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in results:
        status = str(item.get("status") or "")
        counts[status] = counts.get(status, 0) + 1
    return counts


def scan(
    app: AppSettings,
    *,
    progress: Callable[[dict], None] | None = None,
    stop: threading.Event | None = None,
) -> dict:
    """扫描所有关注项。队列的 ``follows.scan`` 任务直接调这里。

    ``progress`` 每查完一项收到一次快照：``total / done / current / statuses``。
    这些数都是**已经发生的事实**，没有预估、没有百分比——剩余项要多久取决于
    通道超时，队列不编。

    ``stop`` 置位后在下一项之前收尾：剩下的项标 ``skipped``，基线不动，结果里
    ``interrupted=true``。调用方（job_runner）会把任务标成 ``interrupted``。
    """
    if not _check_lock.acquire(blocking=False):
        raise ScanBusy("已有一轮关注项扫描在进行")
    try:
        follows = db.list_follows(app.db_path)
        deadline = (
            time.monotonic() + app.follow_check_deadline
            if app.follow_check_deadline > 0
            else None
        )
        results: list[dict] = []
        interrupted = False
        total = len(follows)
        for index, follow in enumerate(follows, 1):
            if stop is not None and stop.is_set():
                interrupted = True
                results.append(
                    {
                        "follow_id": follow["id"],
                        "subject": follow["subject"],
                        "source": follow["source"],
                        "status": "skipped",
                        "detail": "服务停机中断，未检查；基线未变",
                    }
                )
            else:
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    results.append(
                        {
                            "follow_id": follow["id"],
                            "subject": follow["subject"],
                            "source": follow["source"],
                            "status": "skipped",
                            "detail": "本轮扫描总预算已用完，未检查；基线未变",
                        }
                    )
                else:
                    timeout = (
                        app.follow_item_timeout if app.follow_item_timeout > 0 else None
                    )
                    if remaining is not None:
                        timeout = remaining if timeout is None else min(timeout, remaining)
                    results.append(_check_one(follow, app, timeout=timeout))
            if progress is not None:
                progress(
                    {
                        "total": total,
                        "done": index,
                        "current": follow["subject"],
                        "statuses": _status_counts(results),
                    }
                )
        return {
            "busy": False,
            "checked_at": db.now(),
            "results": results,
            "partial": any(
                item["status"] in ("timeout", "skipped") for item in results
            ),
            "interrupted": interrupted,
            "events": db.list_follow_events(app.db_path, limit=20),
            "unread": db.unread_follow_events(app.db_path),
        }
    finally:
        _check_lock.release()


def check_all(app: AppSettings) -> dict:
    """同步入口（``?wait=1`` 与 ``QUEUE_MODE=inline`` 用）。

    已有扫描在跑就直接返回 busy，不排队——这里保持旧接口的形状，前端/脚本
    不需要知道队列的存在。
    """
    try:
        return scan(app)
    except ScanBusy:
        return {
            "busy": True,
            "checked_at": db.now(),
            "results": [],
            "partial": False,
            "events": db.list_follow_events(app.db_path, limit=20),
            "unread": db.unread_follow_events(app.db_path),
        }

