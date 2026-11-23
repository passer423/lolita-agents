"""「解析裙名」工具：复用 dress 的本地别名 / 模型候选 / 档案校验。

编排层只通过这里产出页面可见的解析轨迹，避免同一个概念散落在三个节点里。
工具**不自己猜名字**：本地命中直接用；模型候选必须回本地档案或 Lolibrary
核验，未通过就保留为「未通过」，不采用、不展示成已识别。
"""

from __future__ import annotations

from app.agent import dress

#: 工具名（步骤里的 ``tool`` 字段，便于日志/调试检索）。
TOOL_NAME = "resolve_dress_name"
#: 页面上显示的名字。
LABEL = "解析裙名"
#: 步骤类型；前端按它渲染单独一行。
STEP_KIND = "dress_name"


def build_trace(
    *,
    candidate: str,
    brand: str = "",
    verified: bool = False,
    rejected: tuple[str, ...] | list[str] = (),
    source: str = "",
    name_source: str = "",
    reason: str = "",
) -> dict:
    """一条解析轨迹。``candidate`` 是模型/本地给出的候选全名。"""
    rows: list[dict] = []
    if candidate:
        rows.append({"name": candidate, "brand": brand, "verified": bool(verified)})
    for name in rejected:
        name = str(name or "").strip()
        if name and name != candidate:
            rows.append({"name": name, "brand": brand, "verified": False})
    trace = {
        "kind": STEP_KIND,
        "tool": TOOL_NAME,
        "label": LABEL,
        "triggered": True,
        "candidates": rows,
        "verified": bool(verified),
        "result": "passed" if verified else "failed",
        "source": source,
        "name_source": name_source,
    }
    if reason:
        trace["reason"] = reason
    return trace


def trace_from_query(query: dict | None) -> dict | None:
    """本地/检索已经确认的识别结果 → 轨迹；没有系列名就返回 None（不出这一行）。"""
    if not query:
        return None
    name = str(query.get("series_en") or query.get("series_cn") or "").strip()
    if not name:
        return None
    return build_trace(
        candidate=name,
        brand=str(query.get("brand") or ""),
        verified=bool(query.get("verified")),
        source=str(query.get("source") or ""),
        name_source=str(query.get("name_source") or ""),
    )


def merge_hint(
    query: dict,
    hint: dict,
    ctx,
) -> tuple[dict, dict | None]:
    """把模型候选并回解析结果，同时返回页面轨迹；未触发时轨迹为 None。"""
    trace: dict = {}
    merged = dress.merge_model_hint(query, hint, ctx, trace=trace)
    candidate = str(trace.get("candidate") or "").strip()
    if not candidate:
        return merged, None
    return merged, build_trace(
        candidate=candidate,
        brand=str(trace.get("brand") or ""),
        verified=bool(trace.get("verified")),
        source="model",
        name_source="model",
        reason=str(trace.get("reason") or ""),
    )
