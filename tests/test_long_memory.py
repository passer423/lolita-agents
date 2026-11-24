"""长期记忆：显式偏好/尺码/品牌的规则抽取、去重、隔离、过期与注入。"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

from app import db
from app.agent import graph, long_memory
from app.config import settings


@pytest.fixture()
def memory_settings(workdir, monkeypatch):
    """内容目录指到临时目录：品牌表只放一个测试品牌，不依赖工作区数据。"""
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
    monkeypatch.setattr(settings, "content_dir", content)
    return settings


def _db(tmp_path):
    path = tmp_path / "app.db"
    db.init_db(path)
    return path


def _account(path, username="owner"):
    return db.create_account(path, username, "scrypt$1$1$1$x$y")


# --------------------------------------------------------------------- 抽取


def test_extracts_size_preference_and_brand(memory_settings):
    facts = long_memory.extract("我穿M码，我喜欢粉色，我常买 Baby 的裙子", memory_settings)
    pairs = {(fact["kind"], fact["value"]) for fact in facts}
    assert ("size", "M") in pairs
    assert ("preference", "我喜欢粉色") in pairs
    assert ("brand", "Baby, the Stars Shine Bright") in pairs


def test_questions_and_third_person_are_not_stored(memory_settings):
    for text in (
        "AP 最近有什么上新",
        "M码有现货吗",
        "我喜欢这款吗？",
        "她喜欢粉色",
        "Baby 的裙子怎么洗",
    ):
        assert long_memory.extract(text, memory_settings) == [], text


def test_size_without_context_is_ignored(memory_settings):
    # 只有数字/字母、没有「尺码 / 穿 / 码」语境，不当成尺码。
    assert long_memory.extract("我买了两条", memory_settings) == []
    assert long_memory.extract("我穿170", memory_settings) == []


def test_same_message_dedupes(memory_settings):
    facts = long_memory.extract("我喜欢粉色，我喜欢粉色", memory_settings)
    assert [fact["value"] for fact in facts] == ["我喜欢粉色"]


# --------------------------------------------------------------------- 存储


def test_remember_dedupes_and_counts_hits(tmp_path, memory_settings):
    path = _db(tmp_path)
    account = _account(path)

    long_memory.remember(path, account["id"], "我穿M码", memory_settings)
    long_memory.remember(path, account["id"], "我穿 M 码", memory_settings)

    rows = db.list_memories(path, account["id"])
    assert [(row["kind"], row["value"], row["hits"]) for row in rows] == [
        ("size", "M", 2)
    ]


def test_memories_are_isolated_between_accounts(tmp_path, memory_settings):
    path = _db(tmp_path)
    alice = _account(path, "alice")
    bob = _account(path, "bob")
    long_memory.remember(path, alice["id"], "我喜欢粉色", memory_settings)

    rows = db.list_memories(path, alice["id"])
    assert [row["value"] for row in rows] == ["我喜欢粉色"]
    assert db.list_memories(path, bob["id"]) == []
    assert not db.delete_memory(path, bob["id"], rows[0]["id"])
    assert db.delete_memory(path, alice["id"], rows[0]["id"])
    assert db.list_memories(path, alice["id"]) == []


def test_old_facts_are_hidden_and_pruned(tmp_path, memory_settings):
    path = _db(tmp_path)
    account = _account(path)
    long_memory.remember(path, account["id"], "我穿M码", memory_settings)

    stale = (datetime.now() - timedelta(days=db.MEMORY_MAX_AGE_DAYS + 1)).isoformat(
        timespec="seconds"
    )
    with db.connect(path) as conn:
        conn.execute("UPDATE long_memories SET updated_at = ?", (stale,))

    assert db.list_memories(path, account["id"]) == []
    assert db.prune_user_data(path)["memories"] == 1
    assert db.list_memories(path, account["id"], max_age_days=0) == []


def test_render_marks_source_kind(tmp_path):
    block = long_memory.render(
        [
            {"kind": "size", "value": "M"},
            {"kind": "brand", "value": "Angelic Pretty"},
            {"kind": "preference", "value": "我喜欢粉色"},
        ]
    )
    assert block is not None
    assert block["role"] == "user"
    assert block["content"].startswith(long_memory.MARKER)
    assert "尺码：M" in block["content"]
    assert "常买品牌：Angelic Pretty" in block["content"]
    assert "偏好：我喜欢粉色" in block["content"]
    assert long_memory.render([]) is None


def test_block_for_missing_account_or_db_is_none(tmp_path):
    assert long_memory.block_for(tmp_path / "none.db", None) is None
    assert long_memory.block_for(tmp_path / "missing", 1) is None


# --------------------------------------------------------------------- 注入


def test_long_memory_prepends_and_survives_truncation(data_dir, memory_settings):
    """长期记忆排在最近历史之前，不会像更早的轮次一样被截掉。"""
    from app.agent.channels import RecallContext

    db.init_db(settings.db_path)
    account = _account(settings.db_path)
    long_memory.remember(settings.db_path, account["id"], "我穿M码", memory_settings)

    ctx = RecallContext(settings=settings, account_id=account["id"])
    raw = [
        {"role": "user", "content": f"第{i}个问题"}
        for i in range(graph.HISTORY_FETCH_MESSAGES + 4)
    ]

    turns = graph._history_for_context(raw, ctx)

    assert turns[0]["role"] == "user"
    assert turns[0]["content"].startswith(long_memory.MARKER)
    assert "尺码：M" in turns[0]["content"]
    # 短期窗口仍然只保留最近 HISTORY_FETCH_MESSAGES 条。
    assert len(turns) == graph.HISTORY_FETCH_MESSAGES + 1


def test_run_records_explicit_facts_for_account(data_dir, memory_settings, monkeypatch):
    from app.agent.channels import RecallContext

    db.init_db(settings.db_path)
    account = _account(settings.db_path)
    ctx = RecallContext(settings=settings, account_id=account["id"])

    class FakeGraph:
        def invoke(self, state, config=None):
            return {}

    monkeypatch.setattr(graph, "build_graph", lambda *args, **kwargs: FakeGraph())
    monkeypatch.setattr(graph, "_finish", lambda *args, **kwargs: {})

    graph.run("我喜欢黑色", ctx=ctx, planner=object())

    facts = db.list_memories(settings.db_path, account["id"])
    assert ("preference", "我喜欢黑色") in {
        (fact["kind"], fact["value"]) for fact in facts
    }
