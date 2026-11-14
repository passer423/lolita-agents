"""成本账本：把每次模型调用的 token 换算成钱，并聚合进一次 run 的响应里。

## 价格为什么不写在代码里

不同模型、不同供应商、不同时段的单价都不一样，写死等于给以后埋一个悄悄算错
账的坑。价格表在配置里（``LLM_PRICE_TABLE``，单位「元 / 1K token」），这里只
负责换算与汇总。

## 没配价格的模型怎么算

不猜、不按 0 计。这类模型进 ``unpriced_models``，``total`` 只累计**能算出价钱**
的部分，``priced`` 明确告诉调用方这份账是不全的。把未知价格当 0 会让账本
看起来便宜，比缺数字更危险。

## 为什么按「每次调用」而不是只按总量

一次 run 里 planner、writer、路由器可能用不同模型（分级路由的核心），只有按
调用记下 model 才能拆到 ``by_model``。调用明细由 planner / writer 自己维护
（``usage["calls_detail"]``）；没有明细的测试替身按汇总值补一条，账仍然对得上。
"""

from __future__ import annotations

from app.config import AppSettings
from app.config import settings as default_settings

_UNKNOWN_MODEL = "unknown"


def estimate_cost(
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    price_table: dict | None,
) -> float | None:
    """按每 1K token 单价估算一次调用的费用；模型不在表里返回 None。"""
    entry = (price_table or {}).get(model)
    if not isinstance(entry, dict):
        return None
    try:
        prompt_price = float(entry.get("prompt_per_1k", 0) or 0)
        completion_price = float(entry.get("completion_per_1k", 0) or 0)
    except (TypeError, ValueError):
        return None
    return round(
        prompt_tokens / 1000 * prompt_price + completion_tokens / 1000 * completion_price,
        6,
    )


def _calls_from_usage(usage, *, role: str, model: str = "") -> list[dict]:
    """从一份 usage 里取出调用明细；没有明细就按汇总值补一条。

    测试替身往往只有 ``{"calls": 1}``，补一条 0 token 的调用能让 ``calls`` 与
    金额的逻辑保持一致（钱是 0，但确实发生了一次调用）。
    """
    if not isinstance(usage, dict):
        return []
    detail = usage.get("calls_detail")
    if isinstance(detail, list) and detail:
        calls: list[dict] = []
        for item in detail:
            if not isinstance(item, dict):
                continue
            calls.append({**item, "role": item.get("role") or role})
        return calls

    calls_count = int(usage.get("calls", 0) or 0)
    if calls_count <= 0:
        return []
    return [
        {
            "role": role,
            "model": model or _UNKNOWN_MODEL,
            "prompt_tokens": int(usage.get("prompt_tokens", 0) or 0),
            "completion_tokens": int(usage.get("completion_tokens", 0) or 0),
        }
    ]


def usage_cost(
    planner,
    writer=None,
    settings: AppSettings | None = None,
    *,
    extra_calls=(),
    mode: str = "",
    tier: str = "",
) -> dict:
    """把 planner / writer / 路由器自身的调用聚合成一份成本账。

    ``mode`` 与 ``tier`` 是**这次 run 的路由策略**，记在账本顶层：以后做 A/B
    时，按 mode 分组就能回答「便宜档策略到底省了多少、答案差多少」。
    """
    app = settings or default_settings
    base_model = (app.llm_model or "").strip()

    calls = _calls_from_usage(
        getattr(planner, "usage", None),
        role="planner",
        model=getattr(planner, "model", "") or base_model,
    )
    if writer is not None:
        calls += _calls_from_usage(
            getattr(writer, "usage", None),
            role="writer",
            model=getattr(writer, "model", "") or base_model,
        )
    calls += [{**item} for item in extra_calls if isinstance(item, dict)]

    by_model: dict[str, dict] = {}
    unpriced: set[str] = set()
    total = 0.0
    for call in calls:
        model = str(call.get("model") or _UNKNOWN_MODEL)
        prompt_tokens = int(call.get("prompt_tokens", 0) or 0)
        completion_tokens = int(call.get("completion_tokens", 0) or 0)
        bucket = by_model.setdefault(
            model,
            {
                "calls": 0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "cost": 0.0,
                "priced": True,
            },
        )
        bucket["calls"] += 1
        bucket["prompt_tokens"] += prompt_tokens
        bucket["completion_tokens"] += completion_tokens

        amount = estimate_cost(
            model, prompt_tokens, completion_tokens, app.llm_price_table
        )
        if amount is None:
            bucket["priced"] = False
            unpriced.add(model)
        else:
            bucket["cost"] += amount
            total += amount

    for bucket in by_model.values():
        bucket["cost"] = round(bucket["cost"], 6) if bucket["priced"] else None

    return {
        "mode": mode,
        "tier": tier,
        "currency": app.llm_price_currency,
        "calls": sum(item["calls"] for item in by_model.values()),
        "prompt_tokens": sum(item["prompt_tokens"] for item in by_model.values()),
        "completion_tokens": sum(item["completion_tokens"] for item in by_model.values()),
        "total": round(total, 6),
        "priced": not unpriced,
        "unpriced_models": sorted(unpriced),
        "by_model": by_model,
    }
