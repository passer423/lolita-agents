"""成本核算：价格来自配置、按模型拆分、按天可查。"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

from app.agent import cost, trace
from app.config import settings

PRICES = {
    "cheap-x": {"prompt_per_1k": 1.0, "completion_per_1k": 2.0},
    "strong-x": {"prompt_per_1k": 2.0, "completion_per_1k": 4.0},
}


class FakePlanner:
    model = "cheap-x"

    def __init__(self, calls_detail=None, usage=None):
        self.usage = usage or {
            "calls": 1,
            "prompt_tokens": 600,
            "completion_tokens": 50,
            "calls_detail": calls_detail
            or [
                {
                    "role": "planner",
                    "model": "cheap-x",
                    "prompt_tokens": 600,
                    "completion_tokens": 50,
                }
            ],
        }


class FakeWriter:
    model = "strong-x"

    def __init__(self, usage=None):
        self.usage = usage or {
            "calls": 1,
            "prompt_tokens": 300,
            "completion_tokens": 100,
            "calls_detail": [
                {
                    "role": "writer",
                    "model": "strong-x",
                    "prompt_tokens": 300,
                    "completion_tokens": 100,
                }
            ],
        }


# --------------------------------------------------------------------- 单价


def test_estimate_cost_hits_price_table():
    assert cost.estimate_cost("cheap-x", 2000, 1000, PRICES) == pytest.approx(4.0)


def test_estimate_cost_returns_none_when_model_unpriced():
    assert cost.estimate_cost("unknown", 1000, 1000, PRICES) is None
    assert cost.estimate_cost("cheap-x", 1000, 1000, None) is None


def test_estimate_cost_ignores_broken_entry():
    assert cost.estimate_cost("bad", 1000, 1000, {"bad": "not-a-dict"}) is None


# --------------------------------------------------------------------- 聚合


def test_usage_cost_splits_by_model(monkeypatch):
    monkeypatch.setattr(settings, "llm_price_table", PRICES)
    monkeypatch.setattr(settings, "llm_price_currency", "CNY")

    block = cost.usage_cost(
        FakePlanner(),
        FakeWriter(),
        settings,
        extra_calls=[
            {
                "role": "router",
                "model": "cheap-x",
                "prompt_tokens": 10,
                "completion_tokens": 1,
            }
        ],
        mode="auto",
        tier="strong",
    )

    assert block["mode"] == "auto"
    assert block["tier"] == "strong"
    assert block["currency"] == "CNY"
    assert block["calls"] == 3
    assert block["priced"] is True
    assert block["total"] == pytest.approx(1.712)
    assert block["by_model"]["cheap-x"]["calls"] == 2
    assert block["by_model"]["cheap-x"]["cost"] == pytest.approx(0.712)
    assert block["by_model"]["strong-x"]["cost"] == pytest.approx(1.0)


def test_unpriced_model_is_not_counted_as_zero(monkeypatch):
    monkeypatch.setattr(settings, "llm_price_table", {"cheap-x": PRICES["cheap-x"]})
    writer = FakeWriter(
        usage={
            "calls": 1,
            "prompt_tokens": 300,
            "completion_tokens": 100,
            "calls_detail": [
                {
                    "role": "writer",
                    "model": "mystery-x",
                    "prompt_tokens": 300,
                    "completion_tokens": 100,
                }
            ],
        }
    )

    block = cost.usage_cost(FakePlanner(), writer, settings)

    assert block["priced"] is False
    assert block["unpriced_models"] == ["mystery-x"]
    assert block["by_model"]["mystery-x"]["cost"] is None
    # 只累计算得出来的那部分，而不是把未知模型当 0。
    assert block["total"] == pytest.approx(0.7)


def test_usage_cost_uses_aggregate_when_detail_missing(monkeypatch):
    """测试替身 / 旧 usage 没有 calls_detail 时，按汇总值补一条，账仍然对得上。"""
    monkeypatch.setattr(settings, "llm_price_table", PRICES)
    writer = FakeWriter(
        usage={"calls": 1, "prompt_tokens": 100, "completion_tokens": 50}
    )
    planner = FakePlanner(usage={"calls": 0, "prompt_tokens": 0, "completion_tokens": 0})

    block = cost.usage_cost(planner, writer, settings)

    assert block["calls"] == 1
    assert block["by_model"]["strong-x"]["prompt_tokens"] == 100


# --------------------------------------------------------------------- 按天


def _write_trace(data_dir, records, name="chat_runs.jsonl"):
    path = data_dir / "logs" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(item, ensure_ascii=False) for item in records) + "\n",
        encoding="utf-8",
    )
    return path


def _record(stamp, mode, models):
    by_model = {
        model: {
            "calls": calls,
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "cost": price,
            "priced": price is not None,
        }
        for model, (calls, prompt, completion, price) in models.items()
    }
    return {
        "ts": f"{stamp}T10:00:00",
        "cost": {"mode": mode, "currency": "CNY", "by_model": by_model},
    }


def test_cost_summary_groups_by_day_and_model(monkeypatch, data_dir):
    monkeypatch.setattr(settings, "trace_keep_files", 3)
    today = datetime.now().date()
    yesterday = (today - timedelta(days=1)).isoformat()

    _write_trace(
        data_dir,
        [
            _record(today.isoformat(), "auto", {"cheap-x": (2, 1000, 500, 2.0)}),
            _record(today.isoformat(), "strong", {"strong-x": (1, 100, 100, 0.3)}),
        ],
    )
    # 轮转归档也要算进账里，否则「换文件」就会被当成「没花钱」。
    _write_trace(
        data_dir,
        [_record(today.isoformat(), "auto", {"cheap-x": (1, 500, 0, 0.5)})],
        name="chat_runs.jsonl.1",
    )
    _write_trace(
        data_dir,
        [_record(yesterday, "auto", {"cheap-x": (1, 1000, 0, 1.0)})],
        name="chat_runs.jsonl.2",
    )

    summary = trace.cost_summary(days=1, app=settings)

    rows = {(row["date"], row["model"]): row for row in summary["rows"]}
    assert set(rows) == {(today.isoformat(), "cheap-x"), (today.isoformat(), "strong-x")}
    assert rows[(today.isoformat(), "cheap-x")]["calls"] == 3
    assert rows[(today.isoformat(), "cheap-x")]["cost"] == pytest.approx(2.5)
    assert rows[(today.isoformat(), "strong-x")]["cost"] == pytest.approx(0.3)
    assert summary["by_mode"]["strong"]["calls"] == 1
    assert summary["totals"]["calls"] == 4
    assert summary["totals"]["cost"] == pytest.approx(2.8)
    assert summary["currency"] == settings.llm_price_currency

    all_time = trace.cost_summary(days=0, app=settings)
    assert all_time["totals"]["calls"] == 5
    assert all_time["totals"]["cost"] == pytest.approx(3.8)


def test_cost_summary_keeps_unpriced_as_none(monkeypatch, data_dir):
    monkeypatch.setattr(settings, "trace_keep_files", 3)
    today = datetime.now().date().isoformat()
    _write_trace(data_dir, [_record(today, "auto", {"mystery-x": (1, 100, 100, None)})])

    summary = trace.cost_summary(days=1, app=settings)

    assert summary["totals"]["priced"] is False
    assert summary["totals"]["cost"] is None
    assert summary["rows"][0]["cost"] is None
