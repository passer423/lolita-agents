"""历史压缩要点：抽实体、绑指代、长度上限与「不值得压就退回原文」。"""

from __future__ import annotations

import json

import pytest

from app.agent import history_memory

_FILLER = "档案还记录了配色、尺码与发售价区间，可按货号回查。" * 5


@pytest.fixture()
def memory_settings(workdir, monkeypatch):
    """把内容目录指到临时目录：品牌/系列表不依赖工作区里未提交的条目。"""
    from app.config import settings

    content = workdir / "content"
    (content / "dress").mkdir(parents=True)
    (content / "brands.json").write_text(
        json.dumps(
            {
                "brands": [
                    {
                        "id": "baby",
                        "name": "Baby, the Stars Shine Bright",
                        "region": "JP",
                        "aliases": ["Baby", "BTSSB"],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    (content / "dress" / "aliases.json").write_text(
        json.dumps(
            {
                "aliases": [
                    {
                        "cn": "甜心拿铁",
                        "en": "Sweetheart Dreamy Latte",
                        "brand": "baby",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(settings, "content_dir", content)
    return settings


def _history() -> list[dict]:
    return [
        {"role": "user", "content": "Baby 的 Sweetheart Dreamy Latte 有哪些款式？"},
        {
            "role": "assistant",
            "content": f"Sweetheart Dreamy Latte 有 JSK 与 OP，JSK 货号 B50JS260。[1][2]{_FILLER}",
        },
        {"role": "user", "content": "它一般什么价位？"},
        {"role": "assistant", "content": f"二手现货大约 1500-2200 元。[1]{_FILLER}"},
        {"role": "user", "content": "那条有现货吗？"},
        {"role": "assistant", "content": f"近期二手平台有转单，官方已截定。[3]{_FILLER}"},
        {"role": "user", "content": "我想要粉色，预算 3000 以内，我穿 M 码"},
        {"role": "assistant", "content": f"粉色配色有粉白与粉黑，预算内可收。[1]{_FILLER}"},
    ]


def test_compresses_entities_and_pronouns(memory_settings):
    out = history_memory.compress(_history(), memory_settings)
    assert out is not None
    text = out[0]["content"]
    assert text.startswith(history_memory.MARKER)
    assert "品牌：Baby, the Stars Shine Bright" in text
    assert "系列：Sweetheart Dreamy Latte" in text
    assert "货号：B50JS260" in text
    assert "尺码：M" in text
    assert "偏好：我想要粉色" in text
    assert "约束：预算 3000 以内" in text
    # 「它 / 那条」优先指系列，而不是同轮命中的品牌。
    assert "「它」→ Sweetheart Dreamy Latte" in text
    assert "「那条」→ Sweetheart Dreamy Latte" in text
    assert len(text) <= history_memory.MAX_CHARS


def test_question_clauses_are_not_constraints(memory_settings):
    out = history_memory.compress(_history(), memory_settings)
    assert out is not None
    assert "那条有现货吗" not in out[0]["content"]


def test_citations_are_stripped(memory_settings):
    out = history_memory.compress(_history(), memory_settings)
    assert out is not None
    assert "[1]" not in out[0]["content"]


def test_empty_history_returns_none(memory_settings):
    assert history_memory.compress([], memory_settings) is None
    assert history_memory.compress(None, memory_settings) is None


def test_without_known_entities_returns_none(memory_settings):
    history = [{"role": "user", "content": "今天天气怎么样"}]
    assert history_memory.compress(history, memory_settings) is None


def test_pronoun_without_prior_entity_is_not_guessed(memory_settings):
    history = [{"role": "user", "content": "它多少钱"}]
    assert history_memory.compress(history, memory_settings) is None


def test_not_shorter_than_raw_returns_none(memory_settings):
    history = [{"role": "user", "content": "Baby 的裙子"}]
    assert history_memory.compress(history, memory_settings) is None


def test_output_is_capped(memory_settings):
    history = _history()
    history[0]["content"] = "Baby 的 Sweetheart Dreamy Latte " + ("超长系列描述" * 40)
    out = history_memory.compress(history, memory_settings)
    assert out is not None
    assert len(out[0]["content"]) <= history_memory.MAX_CHARS
