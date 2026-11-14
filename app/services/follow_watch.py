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
"""

from __future__ import annotations

import json
import logging
import threading

from app import db
from app.agent import channels
from app.agent.channels import ChannelUnavailable, RecallContext
from app.config import AppSettings

logger = logging.getLogger(__name__)

MAX_SNAPSHOT_ITEMS = 8
MAX_EVENT_SUMMARY = 240

# 同一时刻只跑一轮扫描：前端轮询和手动点击可能撞在一起，串行化避免重复写事件。
_check_lock = threading.Lock()


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


def _check_one(follow: dict, app: AppSettings) -> dict:
    result = {
        "follow_id": follow["id"],
        "subject": follow["subject"],
        "source": follow["source"],
    }
    try:
        evidence = channels.recall(
            follow["source"],
            follow["subject"],
            RecallContext(settings=app),
        )
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


def check_all(app: AppSettings) -> dict:
    """扫描所有关注项。已有扫描在跑就直接返回 busy，不排队。"""
    if not _check_lock.acquire(blocking=False):
        return {
            "busy": True,
            "checked_at": db.now(),
            "results": [],
            "events": db.list_follow_events(app.db_path, limit=20),
            "unread": db.unread_follow_events(app.db_path),
        }
    try:
        follows = db.list_follows(app.db_path)
        results = [_check_one(follow, app) for follow in follows]
        return {
            "busy": False,
            "checked_at": db.now(),
            "results": results,
            "events": db.list_follow_events(app.db_path, limit=20),
            "unread": db.unread_follow_events(app.db_path),
        }
    finally:
        _check_lock.release()
