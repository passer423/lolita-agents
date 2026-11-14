"""用户数据层（SQLite）：关注项 / 会话记忆。

多路召回 agent 是**单用户自托管**服务（默认只监听本机），所以去掉账号体系：
* ``follows`` 存关注项（关键词 + 来源 + 上次状态），供"关注项更新时通知"；
* ``conversations`` 存服务端会话记忆：客户端不传历史时由服务端补上，追问才能
  接住上一轮的上下文。保留条数由 ``CONVERSATION_RETENTION_MESSAGES`` 控制，
  ``DELETE /api/chat/history`` 可整体清空。
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

SCHEMA_VERSION = 4

SCHEMA = """
CREATE TABLE IF NOT EXISTS follows (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    subject    TEXT NOT NULL,
    source     TEXT NOT NULL,
    last_state TEXT,
    updated_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS follows_unique ON follows(subject, source);

CREATE TABLE IF NOT EXISTS follow_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    follow_id  INTEGER NOT NULL,
    summary    TEXT NOT NULL,
    created_at TEXT NOT NULL,
    read       INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS follow_events_unread ON follow_events(read, id);

CREATE TABLE IF NOT EXISTS conversations (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    role       TEXT NOT NULL,
    content    TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


@contextmanager
def connect(db_path: Path):
    """每次请求开一条连接：sqlite 打开很便宜，比维护连接池简单得多。

    ``WAL`` 让"读的同时在写"不会互相阻塞。
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        yield conn
        conn.commit()
    finally:
        conn.close()


def _migrate_follows_drop_url(conn: sqlite3.Connection) -> None:
    """旧库的 ``follows.url`` 是 NOT NULL 且无人写入，留着会让新插入失败。

    SQLite 的 ``DROP COLUMN`` 在旧版本里不可用，这里用「建新表→拷数据→换名」，
    保留 id 与已有行；唯一索引随旧表一起消失，换名后重建。
    """
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(follows)")}
    if "url" not in columns:
        return
    conn.execute(
        "CREATE TABLE follows_new ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "subject TEXT NOT NULL, source TEXT NOT NULL, "
        "last_state TEXT, updated_at TEXT NOT NULL)"
    )
    conn.execute(
        "INSERT INTO follows_new (id, subject, source, last_state, updated_at) "
        "SELECT id, subject, source, last_state, updated_at FROM follows"
    )
    conn.execute("DROP TABLE follows")
    conn.execute("ALTER TABLE follows_new RENAME TO follows")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS follows_unique ON follows(subject, source)"
    )


def init_db(db_path: Path) -> None:
    with connect(db_path) as conn:
        conn.executescript(SCHEMA)
        _migrate_follows_drop_url(conn)
        conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )


# --------------------------------------------------------------------- 关注项


def add_follow(db_path: Path, subject: str, source: str) -> dict:
    with connect(db_path) as conn:
        try:
            cursor = conn.execute(
                "INSERT INTO follows (subject, source, last_state, updated_at) "
                "VALUES (?, ?, '', ?)",
                (subject, source, now()),
            )
        except sqlite3.IntegrityError as exc:
            # 同一个「关键词 + 来源」重复关注没有意义：会让扫描和通知都翻倍。
            raise ValueError("已经关注过这个关键词和来源") from exc
        row = conn.execute("SELECT * FROM follows WHERE id = ?", (cursor.lastrowid,)).fetchone()
    return dict(row)


def list_follows(db_path: Path) -> list[dict]:
    with connect(db_path) as conn:
        return [dict(row) for row in conn.execute("SELECT * FROM follows ORDER BY id").fetchall()]


def get_follow(db_path: Path, follow_id: int) -> dict | None:
    with connect(db_path) as conn:
        row = conn.execute("SELECT * FROM follows WHERE id = ?", (follow_id,)).fetchone()
    return dict(row) if row else None


def update_follow_state(db_path: Path, follow_id: int, last_state: str) -> bool:
    with connect(db_path) as conn:
        cursor = conn.execute(
            "UPDATE follows SET last_state = ?, updated_at = ? WHERE id = ?",
            (last_state, now(), follow_id),
        )
        return cursor.rowcount > 0


def delete_follow(db_path: Path, follow_id: int) -> bool:
    """删关注项时一并删掉它的事件，避免界面上留下「无主」更新。"""
    with connect(db_path) as conn:
        conn.execute("DELETE FROM follow_events WHERE follow_id = ?", (follow_id,))
        return conn.execute("DELETE FROM follows WHERE id = ?", (follow_id,)).rowcount > 0


# --------------------------------------------------------------------- 关注事件


def add_follow_event(db_path: Path, follow_id: int, summary: str) -> dict:
    with connect(db_path) as conn:
        cursor = conn.execute(
            "INSERT INTO follow_events (follow_id, summary, created_at, read) VALUES (?, ?, ?, 0)",
            (follow_id, summary, now()),
        )
        row = conn.execute(
            "SELECT * FROM follow_events WHERE id = ?", (cursor.lastrowid,)
        ).fetchone()
    return dict(row)


def list_follow_events(
    db_path: Path, *, unread_only: bool = False, limit: int = 20
) -> list[dict]:
    """事件连同关注项一起返回：前端只拿事件也要知道「是哪条关注更新了」。"""
    clause = "WHERE e.read = 0" if unread_only else ""
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT e.id, e.follow_id, e.summary, e.created_at, e.read, "
            "f.subject, f.source "
            "FROM follow_events e LEFT JOIN follows f ON f.id = e.follow_id "
            f"{clause} ORDER BY e.id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [{**dict(row), "read": bool(row["read"])} for row in rows]


def mark_follow_events_read(db_path: Path) -> int:
    with connect(db_path) as conn:
        return conn.execute("UPDATE follow_events SET read = 1 WHERE read = 0").rowcount


def unread_follow_events(db_path: Path) -> int:
    with connect(db_path) as conn:
        row = conn.execute("SELECT COUNT(*) AS n FROM follow_events WHERE read = 0").fetchone()
    return int(row["n"])


# --------------------------------------------------------------------- 会话记忆


def add_exchange(db_path: Path, question: str, answer: str, *, keep: int = 0) -> None:
    """把一问一答写进会话记忆；``keep > 0`` 时只保留最近这么多条消息。

    一问一答必须同一次事务写入：只落 user 不落 assistant 会让下一轮上下文
    出现「用户说了话但助手没回应」的断片，模型会把它当成未完成的任务。
    """
    with connect(db_path) as conn:
        conn.executemany(
            "INSERT INTO conversations (role, content, created_at) VALUES (?, ?, ?)",
            [("user", question, now()), ("assistant", answer, now())],
        )
        if keep > 0:
            conn.execute(
                "DELETE FROM conversations WHERE id NOT IN "
                "(SELECT id FROM conversations ORDER BY id DESC LIMIT ?)",
                (keep,),
            )


def recent_conversations(db_path: Path, limit: int = 20) -> list[dict]:
    """按时间正序返回最近 ``limit`` 条消息；调用方负责再按上下文预算裁剪。"""
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT role, content FROM conversations ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [{"role": row["role"], "content": row["content"]} for row in reversed(rows)]


def clear_conversations(db_path: Path) -> int:
    """清空会话记忆，返回删掉的条数。轨迹与关注项不受影响。"""
    with connect(db_path) as conn:
        return conn.execute("DELETE FROM conversations").rowcount
