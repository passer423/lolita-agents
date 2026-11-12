"""小红书搜索：Spider_XHS provider（逆向签名，免浏览器）。

与 Playwright 方案并存，由 ``XHS_PROVIDER`` 切换。Spider_XHS 是外部仓库
（平级目录，不进本仓 git），通过 ``sys.path`` 导入；签名算法在 Spider_XHS 内
本地纯算（需 Node.js + ``npm install``）。

边界（与 xhs_search.py 一致）：

* 结果只当**用户笔记线索**，不是官方事实；实时通道结果只作本次回答的临时证据，
  不落盘、不入索引（缓存与结果转换统一在 xhs_search.py 处理）。
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from app.config import AppSettings
from app.services.xhs_search import XhsUnavailable

logger = logging.getLogger(__name__)

_EXPLORE_URL = "https://www.xiaohongshu.com/explore/{note_id}?xsec_token={token}"


def _spider_root(app: AppSettings) -> Path:
    return Path(app.xhs_spider_root).expanduser().resolve()


def available(app: AppSettings) -> bool:
    """Spider_XHS 是否就绪（``xhs_enabled`` 由 xhs_search.available 统一判）。"""
    if not str(app.xhs_cookie).strip():
        return False
    if not (_spider_root(app) / "apis").is_dir():
        return False
    return True


def _load_api(app: AppSettings):
    """把 Spider_XHS 根注入 sys.path 后导入 PC API，失败抛 XhsUnavailable。"""
    root = _spider_root(app)
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    try:
        from apis.xhs_pc_apis import XHS_Apis  # noqa: PLC0415
        from xhs_utils.xhs_pc import XHSPcAuth  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001 - 依赖缺失时给调用方可读原因
        raise XhsUnavailable(f"Spider_XHS 依赖缺失，无法导入：{exc}") from exc
    return XHS_Apis, XHSPcAuth


def _note_to_dict(note: dict) -> dict:
    """把 Spider_XHS 搜索返回的 note item 转成 xhs_search 的 notes 结构。"""
    card = note.get("note_card") or {}
    title = str(card.get("display_title") or card.get("title") or "").strip()
    content = str(card.get("desc") or card.get("describe") or "").strip()
    note_id = str(note.get("id") or note.get("note_id") or "")
    token = str(note.get("xsec_token") or "")
    url = _EXPLORE_URL.format(note_id=note_id, token=token) if note_id else ""
    return {"title": title, "url": url, "content": content, "comments": []}


def fetch_notes(query: str, app: AppSettings, limit: int) -> list[dict]:
    """调 Spider_XHS 搜索，返回 notes 结构；不可用时抛 XhsUnavailable。"""
    XHS_Apis, XHSPcAuth = _load_api(app)
    try:
        auth = XHSPcAuth.from_cookie(str(app.xhs_cookie).strip())
        api = XHS_Apis(auth)
        success, msg, notes = api.search_some_note(query, limit)
    except Exception as exc:  # noqa: BLE001 - 网络/签名/风控失败都当不可用
        raise XhsUnavailable(f"Spider_XHS 搜索失败：{exc}") from exc

    if not success:
        raise XhsUnavailable(f"Spider_XHS 搜索失败：{msg}")

    return [
        _note_to_dict(note)
        for note in notes
        if isinstance(note, dict) and note.get("model_type") == "note"
    ]
