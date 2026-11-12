"""用户数据层（SQLite）：偏好 / 关注项 / 会话记忆。

多路召回 agent 是**单用户自托管**服务（默认只监听本机），所以去掉账号体系：
* ``preferences`` 按「轴」存用户偏好（风格、品牌、价位带…），供召回个性化排序；
* ``follows`` 存关注项（品牌/系列/来源链接 + 上次状态），供"关注项更新时通知"；
* ``conversations`` 存会话记忆，供多轮上下文与长期记忆。

偏好为什么按"轴"存：``preferences(axis, values_json)`` 一行一个轴，加新轴
（颜色、价位带…）不需要改表结构。
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS preferences (
    axis        TEXT PRIMARY KEY,
    values_json TEXT NOT NULL DEFAULT '[]',
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS follows (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    subject    TEXT NOT NULL,
    source     TEXT NOT NULL,
    url        TEXT NOT NULL,
    last_state TEXT,
    updated_at TEXT NOT NULL
);

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


def init_db(db_path: Path) -> None:
    with connect(db_path) as conn:
        conn.executescript(SCHEMA)
        conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )


def _loads(raw: str) -> list[str]:
    try:
        parsed = json.loads(raw or "[]")
    except json.JSONDecodeError:
        return []
    return [str(item) for item in parsed] if isinstance(parsed, list) else []


# --------------------------------------------------------------------- 偏好


def get_preferences(db_path: Path) -> dict[str, list[str]]:
    """返回**所有**已设置的轴。没设过的轴不出现（调用方自己给默认）。"""
    data: dict[str, list[str]] = {}
    with connect(db_path) as conn:
        for row in conn.execute("SELECT axis, values_json FROM preferences").fetchall():
            data[row["axis"]] = _loads(row["values_json"])
    return data


def save_preferences(db_path: Path, axes: dict[str, list[str]]) -> dict[str, list[str]]:
    """只替换**传进来的轴**，没传的保持不变（避免"漏传一个字段清空其它偏好"）。"""
    with connect(db_path) as conn:
        for axis, values in axes.items():
            if values:
                conn.execute(
                    "INSERT INTO preferences (axis, values_json, updated_at) VALUES (?, ?, ?) "
                    "ON CONFLICT(axis) DO UPDATE SET values_json = excluded.values_json, "
                    "updated_at = excluded.updated_at",
                    (axis, json.dumps(values, ensure_ascii=False), now()),
                )
            else:
                conn.execute("DELETE FROM preferences WHERE axis = ?", (axis,))
    return get_preferences(db_path)


# --------------------------------------------------------------------- 关注项


def add_follow(db_path: Path, subject: str, source: str, url: str) -> dict:
    with connect(db_path) as conn:
        cursor = conn.execute(
            "INSERT INTO follows (subject, source, url, last_state, updated_at) VALUES (?, ?, ?, '', ?)",
            (subject, source, url, now()),
        )
        row = conn.execute("SELECT * FROM follows WHERE id = ?", (cursor.lastrowid,)).fetchone()
    return dict(row)


def list_follows(db_path: Path) -> list[dict]:
    with connect(db_path) as conn:
        return [dict(row) for row in conn.execute("SELECT * FROM follows ORDER BY id").fetchall()]


def update_follow_state(db_path: Path, follow_id: int, last_state: str) -> bool:
    with connect(db_path) as conn:
        cursor = conn.execute(
            "UPDATE follows SET last_state = ?, updated_at = ? WHERE id = ?",
            (last_state, now(), follow_id),
        )
        return cursor.rowcount > 0


def delete_follow(db_path: Path, follow_id: int) -> bool:
    with connect(db_path) as conn:
        return conn.execute("DELETE FROM follows WHERE id = ?", (follow_id,)).rowcount > 0


# --------------------------------------------------------------------- 会话记忆


def add_conversation(db_path: Path, role: str, content: str) -> None:
    with connect(db_path) as conn:
        conn.execute(
            "INSERT INTO conversations (role, content, created_at) VALUES (?, ?, ?)",
            (role, content, now()),
        )


def recent_conversations(db_path: Path, limit: int = 20) -> list[dict]:
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT role, content FROM conversations ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [{"role": row["role"], "content": row["content"]} for row in reversed(rows)]
