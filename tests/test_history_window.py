"""历史窗口：raw 兜底窗口 + 压缩要点优先的两条路径。"""

from __future__ import annotations

from app.agent import graph, history_memory, planner
from app.agent.channels import RecallContext
from app.agent.writer import (
    HISTORY_FETCH_MESSAGES,
    MAX_HISTORY_TURNS,
    build_synth_messages,
    sanitize_history,
)
from app.config import settings

_FILLER = "档案还记录了配色、尺码与发售价区间，可按货号回查。" * 5


def _history(turns: int) -> list[dict]:
    messages: list[dict] = []
    for index in range(1, turns + 1):
        messages.append({"role": "user", "content": f"第{index}轮用户"})
        messages.append({"role": "assistant", "content": f"第{index}轮助手"})
    return messages


def _compactable_history() -> list[dict]:
    return [
        {"role": "user", "content": "RAW-USER-1 Baby 的 Sweetheart Dreamy Latte 有哪些款式？"},
        {
            "role": "assistant",
            "content": f"RAW-BOT-1 Sweetheart Dreamy Latte 有 JSK 与 OP，货号 B50JS260。[1]{_FILLER}",
        },
        {"role": "user", "content": "RAW-USER-2 它一般什么价位？"},
        {"role": "assistant", "content": f"RAW-BOT-2 二手现货约 1500-2200 元。[1]{_FILLER}"},
        {"role": "user", "content": "RAW-USER-3 我想要粉色，预算 3000 以内"},
        {"role": "assistant", "content": f"RAW-BOT-3 粉色可收。[1]{_FILLER}"},
    ]


# ---------------------------------------------------------------- raw 兜底窗口


def test_tail_turns_keeps_recent_six():
    window = planner.tail_turns(_history(7), planner.EXPAND_HISTORY_TURNS)
    assert len(window) == 12
    assert window[0]["content"] == "第2轮用户"
    assert window[-1]["content"] == "第7轮助手"


def test_expand_prompt_sees_six_turns_but_not_seventh():
    prompt = planner.build_expand_prompt("sbw 是什么", _history(7))
    assert f"最近 {planner.EXPAND_HISTORY_TURNS} 轮对话" in prompt
    assert "第2轮用户" in prompt
    assert "第7轮用户" in prompt
    assert "第1轮用户" not in prompt


def test_query_prompt_sees_four_turns():
    prompt = planner.build_query_prompt("那它呢", _history(6))
    assert f"最近 {planner.QUERY_HISTORY_TURNS} 轮对话" in prompt
    assert "第3轮用户" in prompt
    assert "第6轮用户" in prompt
    assert "第2轮用户" not in prompt


def test_dress_prompt_sees_four_turns():
    prompt = planner.build_dress_prompt("那条裙子叫什么", _history(6))
    assert f"最近 {planner.DRESS_HISTORY_TURNS} 轮对话" in prompt
    assert "第3轮用户" in prompt
    assert "第2轮用户" not in prompt


def test_tail_turns_truncates_long_message():
    long = "长" * (planner.HISTORY_TURN_MAX_CHARS + 50)
    window = planner.tail_turns([{"role": "user", "content": long}], 1)
    assert len(window[0]["content"]) == planner.HISTORY_TURN_MAX_CHARS + 1
    assert window[0]["content"].endswith("…")


def test_sanitize_history_accepts_fetch_limit():
    messages = _history(8)
    assert len(sanitize_history(messages)) == MAX_HISTORY_TURNS
    assert len(sanitize_history(messages, limit=HISTORY_FETCH_MESSAGES)) == 12
    assert HISTORY_FETCH_MESSAGES == 12


# ---------------------------------------------------------------- 压缩要点路径


def test_planner_uses_compacted_points_instead_of_raw_window():
    compacted = history_memory.compress(_compactable_history(), settings)
    assert compacted is not None
    for build in (
        planner.build_query_prompt,
        planner.build_expand_prompt,
        planner.build_dress_prompt,
    ):
        prompt = build("它还有现货吗", compacted)
        assert "对话历史要点（压缩" in prompt
        assert history_memory.MARKER in prompt
        assert "Sweetheart Dreamy Latte" in prompt
        # 压缩后不再按轮数重复塞原文。
        assert "RAW-USER-1" not in prompt
        assert "最近 4 轮对话" not in prompt
        assert "最近 6 轮对话" not in prompt


def test_graph_compacts_history_before_entering_graph():
    ctx = RecallContext(settings=settings)
    memory = graph._history_for_context(_compactable_history(), ctx)
    assert len(memory) == 1
    assert memory[0]["content"].startswith(history_memory.MARKER)


def test_graph_falls_back_to_raw_when_not_worth_compressing():
    ctx = RecallContext(settings=settings)
    raw = [{"role": "user", "content": "今天天气怎么样"}]
    assert graph._history_for_context(raw, ctx) == raw
    assert graph._history_for_context([], ctx) == []


def test_writer_receives_compacted_points():
    ctx = RecallContext(settings=settings)
    compacted = graph._history_for_context(_compactable_history(), ctx)
    messages = build_synth_messages("它还有现货吗", [], compacted)
    joined = "\n".join(message["content"] for message in messages)
    assert history_memory.MARKER in joined
    assert "Sweetheart Dreamy Latte" in joined
    assert "RAW-USER-1" not in joined
