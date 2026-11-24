"""长期记忆接口：查看、逐条删除、全部清除本人的稳定事实。

记忆由 ``app/agent/long_memory.py`` 在对话后按规则写入，这里只做账号自己的
读写入口：

* 只返回当前登录账号的行，删除的 WHERE 条件也带 ``account_id``——别人的记忆
  既查不到，也删不掉（越权一律 404，不区分「不存在」与「不是你的」）；
* 返回 ``labels``（尺码 / 常买品牌 / 偏好），前端不再重复维护一份中文名；
* 读取窗口与回答时的注入一致（``db.list_memories`` 的默认上限与过期天数），
  保证「页面看到的」就是「回答会用的」。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from app import accounts, db
from app.agent.long_memory import KIND_LABELS
from app.config import settings

router = APIRouter(prefix="/api/memories", tags=["memories"])


def _snapshot(account_id: int) -> dict:
    memories = [
        {
            "id": row["id"],
            "kind": row["kind"],
            "value": row["value"],
            "source": row["source"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "hits": row["hits"],
        }
        for row in db.list_memories(settings.db_path, account_id)
    ]
    return {"memories": memories, "labels": KIND_LABELS}


@router.get("")
def list_memories(request: Request) -> dict:
    return _snapshot(accounts.account_of(request)["id"])


@router.delete("/{memory_id}")
def remove_memory(memory_id: int, request: Request) -> dict:
    account_id = accounts.account_of(request)["id"]
    if not db.delete_memory(settings.db_path, account_id, memory_id):
        raise HTTPException(status_code=404, detail="记忆不存在")
    return _snapshot(account_id)


@router.delete("")
def clear_memories(request: Request) -> dict:
    account_id = accounts.account_of(request)["id"]
    cleared = db.clear_memories(settings.db_path, account_id)
    return {**_snapshot(account_id), "cleared": cleared}
