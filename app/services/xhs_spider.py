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
import os
import shutil
import sys
import threading
from pathlib import Path

from app.config import AppSettings
from app.services.xhs_search import XhsUnavailable

logger = logging.getLogger(__name__)

_EXPLORE_URL = (
    "https://www.xiaohongshu.com/explore/{note_id}?xsec_token={token}&xsec_source=pc_search"
)

# 搜索结果卡片通常只有标题、没有正文；详情接口才有 desc。只对前几条取详情，
# 每条多一次请求，再多就只是拖慢整体预算。
_DETAIL_LIMIT = 3

# 进程 PATH 可能是「装 Node 之前」的快照（服务先于 Node 启动），此时 PyExecJS
# 会退化到 Windows 的 JScript，签名跑不起来。补上常见安装目录即可。
_NODE_DIRS = (
    Path(r"C:\Program Files\nodejs"),
    Path(os.environ.get("LOCALAPPDATA") or "") / "Programs" / "nodejs",
)


def _spider_root(app: AppSettings) -> Path:
    return Path(app.xhs_spider_root).expanduser().resolve()


def _ensure_node_on_path() -> None:
    if shutil.which("node"):
        return
    for node_dir in _NODE_DIRS:
        if (node_dir / "node.exe").exists():
            os.environ["PATH"] = f"{node_dir}{os.pathsep}{os.environ.get('PATH', '')}"
            return


def readiness(app: AppSettings) -> tuple[bool, str]:
    """Spider_XHS 是否就绪，并给出不可用的具体原因（给状态接口用）。

    只判本 provider 的依赖；``xhs_enabled`` 由 ``xhs_search.readiness`` 统一判。
    """
    if not str(app.xhs_cookie).strip():
        return False, "缺少 XHS_COOKIE（Spider_XHS 需要登录态）"
    if not (_spider_root(app) / "apis").is_dir():
        return False, f"找不到 Spider_XHS 目录：{_spider_root(app)}（检查 XHS_SPIDER_ROOT）"
    _ensure_node_on_path()
    if shutil.which("node") is None:
        return False, "找不到 Node.js（Spider_XHS 的签名需要它）"
    return True, ""


def available(app: AppSettings) -> bool:
    """Spider_XHS 是否就绪（``xhs_enabled`` 由 xhs_search.available 统一判）。

    Node 是硬依赖：签名在 Spider_XHS 里靠 Node 跑 JS，缺了必然失败，
    这里先判掉，让状态接口如实显示「不可用」而不是等到召回时才报错。
    """
    return readiness(app)[0]


def _load_api(app: AppSettings):
    """把 Spider_XHS 根注入 sys.path 后导入 PC API，失败抛 XhsUnavailable。"""
    _ensure_node_on_path()
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
    note_id = str(note.get("id") or note.get("note_id") or card.get("note_id") or "")
    token = str(note.get("xsec_token") or card.get("xsec_token") or "")
    url = _EXPLORE_URL.format(note_id=note_id, token=token) if note_id else ""
    return {"title": title, "url": url, "content": content, "comments": []}


def _fill_detail(api, item: dict) -> None:
    """用详情接口补正文。失败就保留卡片内容——单条详情失败不该拖垮整路召回。"""
    if not item.get("url"):
        return
    try:
        success, _msg, payload = api.get_note_info(item["url"])
    except Exception as exc:  # noqa: BLE001 - 风控/网络失败都只影响这一条
        logger.info("小红书笔记详情失败 %s：%s", item["url"], exc)
        return
    if not success or not isinstance(payload, dict):
        return
    items = (payload.get("data") or {}).get("items") or []
    card = (items[0] or {}).get("note_card") if items else None
    if not isinstance(card, dict):
        return
    title = str(card.get("title") or "").strip()
    content = str(card.get("desc") or "").strip()
    item["title"] = title or item["title"]
    item["content"] = content or item["content"]


def _fetch_notes(query: str, app: AppSettings, limit: int) -> list[dict]:
    """真正调 Spider_XHS：一次搜索 + 前几条详情。"""
    XHS_Apis, XHSPcAuth = _load_api(app)
    try:
        auth = XHSPcAuth.from_cookie(str(app.xhs_cookie).strip())
        api = XHS_Apis(auth)
        success, msg, notes = api.search_some_note(query, limit)
    except Exception as exc:  # noqa: BLE001 - 网络/签名/风控失败都当不可用
        raise XhsUnavailable(f"Spider_XHS 搜索失败：{exc}") from exc

    if not success:
        raise XhsUnavailable(f"Spider_XHS 搜索失败：{msg}")

    results: list[dict] = []
    for note in notes:
        if not isinstance(note, dict):
            continue
        if note.get("model_type") != "note" and not note.get("note_card"):
            continue
        item = _note_to_dict(note)
        if len(results) < _DETAIL_LIMIT:
            _fill_detail(api, item)
        results.append(item)
    return results


def fetch_notes(
    query: str,
    app: AppSettings,
    limit: int,
    *,
    timeout: float | None = None,
) -> list[dict]:
    """调 Spider_XHS 搜索，返回 notes 结构；不可用或超时抛 XhsUnavailable。

    Spider_XHS 的 requests 用的是它自己的 ``REQUEST_TIMEOUT``，不认本应用的
    ``XHS_TIMEOUT``；而一次召回是「搜索 + 最多 3 次详情」，串起来最坏能到
    一分钟。所以在这一层兜总预算：超时丢结果，和 Playwright 分支行为一致。
    """
    seconds = float(timeout or 0)
    if seconds <= 0:
        return _fetch_notes(query, app, limit)

    box: dict[str, object] = {}

    def worker() -> None:
        try:
            box["value"] = _fetch_notes(query, app, limit)
        except Exception as exc:  # noqa: BLE001 - 原样带回主线程
            box["error"] = exc

    thread = threading.Thread(target=worker, name="xhs-spider", daemon=True)
    thread.start()
    thread.join(seconds)
    if thread.is_alive():
        raise XhsUnavailable(f"小红书搜索超时（{seconds:g}s）")
    error = box.get("error")
    if isinstance(error, Exception):
        raise error
    value = box.get("value")
    return value if isinstance(value, list) else []
