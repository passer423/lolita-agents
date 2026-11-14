"""会话记忆的落库、保留与清理。"""

from __future__ import annotations

from app import db


def _db(tmp_path):
    path = tmp_path / "app.db"
    db.init_db(path)
    return path


def test_add_exchange_keeps_user_and_assistant_together(tmp_path):
    path = _db(tmp_path)

    db.add_exchange(path, "怎么洗", "装洗衣袋")

    assert db.recent_conversations(path) == [
        {"role": "user", "content": "怎么洗"},
        {"role": "assistant", "content": "装洗衣袋"},
    ]


def test_retention_keeps_only_latest_messages(tmp_path):
    path = _db(tmp_path)

    for index in range(5):
        db.add_exchange(path, f"q{index}", f"a{index}", keep=4)

    assert db.recent_conversations(path, limit=100) == [
        {"role": "user", "content": "q3"},
        {"role": "assistant", "content": "a3"},
        {"role": "user", "content": "q4"},
        {"role": "assistant", "content": "a4"},
    ]


def test_retention_zero_means_no_trim(tmp_path):
    path = _db(tmp_path)

    for index in range(5):
        db.add_exchange(path, f"q{index}", f"a{index}", keep=0)

    assert len(db.recent_conversations(path, limit=100)) == 10


def test_clear_conversations_returns_removed_count(tmp_path):
    path = _db(tmp_path)
    db.add_exchange(path, "q1", "a1")
    db.add_exchange(path, "q2", "a2")

    assert db.clear_conversations(path) == 4
    assert db.recent_conversations(path) == []
    assert db.clear_conversations(path) == 0
