"""知识库索引接口：状态与重载。

服务启动时只装载一次索引（见 ``bridge._ensure_loaded``），所以重建索引后必须
通知它换掉旧的那份；``scripts/build_kb_index.py --notify`` 打的就是
``POST /api/kb/reload``。索引没建好时这两个接口都返回 200 + ``ready=false``：
这是「还没跑建索引」，不是服务故障，不该让前端看到 500。
"""

from __future__ import annotations

from fastapi import APIRouter

from app import bridge
from app.config import settings

router = APIRouter(prefix="/api/kb", tags=["kb"])


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
