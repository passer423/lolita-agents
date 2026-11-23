"""对话线程的落库、归属隔离、保留与清理。"""

from __future__ import annotations

from datetime import datetime, timedelta

from app import db


def _db(tmp_path):
    path = tmp_path / "app.db"
    db.init_db(path)
    return path


def _account(path, username="owner"):
    return db.create_account(path, username, "scrypt$1$1$1$x$y")


def test_exchange_keeps_user_and_assistant_together(tmp_path):
    path = _db(tmp_path)
    account = _account(path)
    thread = db.create_thread(path, account["id"], "怎么洗")

    assert db.add_thread_exchange(path, account["id"], thread["id"], "怎么洗", "装洗衣袋")

    messages = db.thread_messages(path, account["id"], thread["id"])
    assert [(m["role"], m["content"]) for m in messages] == [
        ("user", "怎么洗"),
        ("assistant", "装洗衣袋"),
    ]


def test_retention_keeps_only_latest_messages(tmp_path):
    path = _db(tmp_path)
    account = _account(path)
    thread = db.create_thread(path, account["id"])

    for index in range(5):
        db.add_thread_exchange(
            path, account["id"], thread["id"], f"q{index}", f"a{index}", keep=4
        )

    assert [(m["role"], m["content"]) for m in db.thread_messages(
        path, account["id"], thread["id"], limit=100
    )] == [
        ("user", "q3"),
        ("assistant", "a3"),
        ("user", "q4"),
        ("assistant", "a4"),
    ]


def test_retention_zero_means_no_trim(tmp_path):
    path = _db(tmp_path)
    account = _account(path)
    thread = db.create_thread(path, account["id"])

    for index in range(5):
        db.add_thread_exchange(
            path, account["id"], thread["id"], f"q{index}", f"a{index}", keep=0
        )

    assert len(db.thread_messages(path, account["id"], thread["id"], limit=100)) == 10


def test_threads_are_isolated_between_accounts(tmp_path):
    path = _db(tmp_path)
    alice = _account(path, "alice@example.com")
    bob = _account(path, "bob@example.com")
    thread = db.create_thread(path, alice["id"], "Alice 的问题")
    db.add_thread_exchange(path, alice["id"], thread["id"], "q", "a")

    assert [t["id"] for t in db.list_threads(path, alice["id"])] == [thread["id"]]
    assert db.list_threads(path, bob["id"]) == []
    assert db.get_thread(path, bob["id"], thread["id"]) is None
    assert db.thread_messages(path, bob["id"], thread["id"]) == []
    # 写别人线程、删别人线程都必须失败
    assert not db.add_thread_exchange(path, bob["id"], thread["id"], "偷看", "偷看")
    assert db.delete_thread(path, bob["id"], thread["id"]) == 0
    assert db.get_thread(path, alice["id"], thread["id"]) is not None


def test_delete_and_clear_return_removed_message_counts(tmp_path):
    path = _db(tmp_path)
    account = _account(path)
    first = db.create_thread(path, account["id"], "一")
    second = db.create_thread(path, account["id"], "二")
    db.add_thread_exchange(path, account["id"], first["id"], "q1", "a1")
    db.add_thread_exchange(path, account["id"], second["id"], "q2", "a2")

    assert db.delete_thread(path, account["id"], first["id"]) == 2
    assert db.delete_thread(path, account["id"], first["id"]) == 0
    assert db.clear_threads(path, account["id"]) == 2
    assert db.list_threads(path, account["id"]) == []
    assert db.clear_threads(path, account["id"]) == 0


def test_prune_keeps_latest_threads_and_drops_stale(tmp_path, monkeypatch):
    path = _db(tmp_path)
    account = _account(path)
    monkeypatch.setattr(db, "KEEP_THREADS_PER_ACCOUNT", 3)
    threads = [db.create_thread(path, account["id"], f"t{i}") for i in range(5)]

    # 手动把最早两条改成 100 天前，验证「闲置过期」也会删
    old = (datetime.now() - timedelta(days=100)).isoformat(timespec="seconds")
    with db.connect(path) as conn:
        conn.execute(
            "UPDATE chat_threads SET updated_at = ? WHERE id IN (?, ?)",
            (old, threads[0]["id"], threads[1]["id"]),
        )

    removed = db.prune_user_data(path)

    kept = [t["id"] for t in db.list_threads(path, account["id"], limit=100)]
    assert removed["threads"] == 2          # 两条闲置过期
    assert kept == [threads[4]["id"], threads[3]["id"], threads[2]["id"]]


def test_prune_drops_expired_sessions(tmp_path):
    path = _db(tmp_path)
    account = _account(path)
    expired = (datetime.now() - timedelta(days=1)).isoformat(timespec="seconds")
    with db.connect(path) as conn:
        conn.execute(
            "INSERT INTO account_sessions "
            "(token_hash, account_id, created_at, expires_at, last_seen_at) "
            "VALUES ('dead', ?, ?, ?, ?)",
            (account["id"], expired, expired, expired),
        )

    removed = db.prune_user_data(path)

    assert removed["sessions"] == 1
    assert db.get_session(path, "dead") is None


def test_init_db_preserves_legacy_follows_but_hides_them(tmp_path):
    """旧关注项（account_id 为 NULL）保留在库里，但任何账号都查不到。"""
    import sqlite3

    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE follows ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, subject TEXT NOT NULL, "
        "source TEXT NOT NULL, url TEXT NOT NULL, last_state TEXT, "
        "updated_at TEXT NOT NULL);"
        "INSERT INTO follows (subject, source, url, last_state, updated_at) "
        "VALUES ('旧关注', 'web', 'https://old.example', '', '2026-01-01T00:00:00');"
    )
    conn.commit()
    conn.close()

    db.init_db(path)

    with db.connect(path) as check:
        rows = check.execute("SELECT subject, account_id FROM follows").fetchall()
    assert [(row["subject"], row["account_id"]) for row in rows] == [("旧关注", None)]

    account = _account(path)
    assert db.list_follows(path, account["id"]) == []
    db.add_follow(path, account["id"], "新关注", "web")
    assert [f["subject"] for f in db.list_follows(path, account["id"])] == ["新关注"]
