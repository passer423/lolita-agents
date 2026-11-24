"""用户数据层（SQLite）：账号 / 登录会话 / 对话线程 / 关注项。

一个进程只开一个 ``data/app.db``（WAL）。表分三类：

* ``accounts`` + ``account_sessions``：用户名账号与登录态（只存密码哈希与
  token 的 SHA-256，不存明文）；
* ``chat_threads`` + ``thread_messages``：一个账号下的多个对话及其消息；
* ``follows`` + ``follow_events``：关注项，按 ``account_id`` 隔离。

**旧数据不删也不展示**：``follows.account_id`` 为 NULL 的历史行、以及早期
``conversations`` 表（全局单会话）都原样留在库里，任何账号都查不到它们。
这样迁移不会因为「先删再建」丢数据，也不会把别人的历史塞给新账号。

增长是有界的：每个账号保留最近 20 个对话、每个对话 100 条消息、全库最多
500 个对话、90 天没更新的对话清掉；过期/超量的登录会话在启动与写入后节流
清理（见 ``prune_user_data``）。
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

SCHEMA_VERSION = 7

#: 每账号保留的对话数、每对话保留的消息数、全库对话上限、闲置过期天数。
KEEP_THREADS_PER_ACCOUNT = 20
KEEP_MESSAGES_PER_THREAD = 100
MAX_THREADS_TOTAL = 500
THREAD_MAX_AGE_DAYS = 90
KEEP_SESSIONS_PER_ACCOUNT = 10

#: 长期记忆（显式偏好 / 尺码 / 品牌）：每账号最多几条、多久没再被确认就过期。
#: 都是稳定信息，过期比对话宽松得多；上限只为防单账号把库灌满。
KEEP_MEMORIES_PER_ACCOUNT = 30
MEMORY_MAX_AGE_DAYS = 365

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    last_seen_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS account_sessions (
    token_hash   TEXT PRIMARY KEY,
    account_id   INTEGER NOT NULL,
    created_at   TEXT NOT NULL,
    expires_at   TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS account_sessions_account
    ON account_sessions(account_id, last_seen_at);

CREATE TABLE IF NOT EXISTS chat_threads (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id    INTEGER NOT NULL,
    title         TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    message_count INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS chat_threads_account
    ON chat_threads(account_id, updated_at);

CREATE TABLE IF NOT EXISTS thread_messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id  INTEGER NOT NULL,
    role       TEXT NOT NULL,
    content    TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS thread_messages_thread
    ON thread_messages(thread_id, id);

CREATE TABLE IF NOT EXISTS follows (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER,
    subject    TEXT NOT NULL,
    source     TEXT NOT NULL,
    last_state TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS follow_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    follow_id  INTEGER NOT NULL,
    summary    TEXT NOT NULL,
    created_at TEXT NOT NULL,
    read       INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS follow_events_unread ON follow_events(read, id);

-- 长期记忆：稳定事实（偏好 / 尺码 / 品牌），与 thread_messages 的短期窗口分开。
-- 同一账号同一 kind + value 只留一行，重复出现时更新 updated_at 并累加 hits。
CREATE TABLE IF NOT EXISTS long_memories (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL,
    kind       TEXT NOT NULL,
    value      TEXT NOT NULL,
    source     TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    hits       INTEGER NOT NULL DEFAULT 1
);

CREATE UNIQUE INDEX IF NOT EXISTS long_memories_unique
    ON long_memories(account_id, kind, value);

CREATE INDEX IF NOT EXISTS long_memories_account
    ON long_memories(account_id, updated_at);

-- 迁移前的全局单会话表。保留旧行，但代码不再读写（新账号看不到）。
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


def _ago(days: float) -> str:
    return (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")


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


def _migrate_follows(conn: sqlite3.Connection) -> None:
    """``follows`` 从「全局一张表」迁到「按账号隔离」。

    旧库的 ``url`` 列是 NOT NULL 且无人写入（保留会挡住新插入），先按老办法
    换成新表；再加 ``account_id``。旧行的 ``account_id`` 保持 NULL——没有账号
    能查到它们，但数据没丢。
    """
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(follows)")}
    if "url" in columns:
        conn.execute(
            "CREATE TABLE follows_new ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, subject TEXT NOT NULL, "
            "source TEXT NOT NULL, last_state TEXT, updated_at TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO follows_new (id, subject, source, last_state, updated_at) "
            "SELECT id, subject, source, last_state, updated_at FROM follows"
        )
        conn.execute("DROP TABLE follows")
        conn.execute("ALTER TABLE follows_new RENAME TO follows")
        columns = {"id", "subject", "source", "last_state", "updated_at"}

    if "account_id" not in columns:
        conn.execute("ALTER TABLE follows ADD COLUMN account_id INTEGER")

    # 旧索引是 (subject, source) 全局唯一；换成账号内唯一。
    conn.execute("DROP INDEX IF EXISTS follows_unique")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS follows_account_unique "
        "ON follows(account_id, subject, source)"
    )


def _migrate_accounts(conn: sqlite3.Connection) -> None:
    """``accounts`` 的登录标识从邮箱换成用户名（v5 → v6）。

    只把列改名、旧值原样保留：早期用邮箱注册的账号不用改数据也还能登录——
    登录接口不校验用户名字符集，把原来那串邮箱填进用户名即可，密码不变。
    """
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(accounts)")}
    if "email" in columns and "username" not in columns:
        conn.execute("ALTER TABLE accounts RENAME COLUMN email TO username")


def init_db(db_path: Path) -> None:
    with connect(db_path) as conn:
        conn.executescript(SCHEMA)
        _migrate_accounts(conn)
        _migrate_follows(conn)
        conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )


# --------------------------------------------------------------------- 账号


def create_account(db_path: Path, username: str, password_hash: str) -> dict:
    stamp = now()
    with connect(db_path) as conn:
        try:
            cursor = conn.execute(
                "INSERT INTO accounts (username, password_hash, created_at, last_seen_at) "
                "VALUES (?, ?, ?, ?)",
                (username, password_hash, stamp, stamp),
            )
        except sqlite3.IntegrityError as exc:
            raise ValueError("这个用户名已经注册过了") from exc
        row = conn.execute(
            "SELECT id, username, created_at, last_seen_at FROM accounts WHERE id = ?",
            (cursor.lastrowid,),
        ).fetchone()
    return dict(row)


def get_account_by_username(db_path: Path, username: str) -> dict | None:
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM accounts WHERE username = ? COLLATE NOCASE", (username,)
        ).fetchone()
    return dict(row) if row else None


def get_account(db_path: Path, account_id: int) -> dict | None:
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT id, username, created_at, last_seen_at FROM accounts WHERE id = ?",
            (account_id,),
        ).fetchone()
    return dict(row) if row else None


def touch_account(db_path: Path, account_id: int) -> None:
    with connect(db_path) as conn:
        conn.execute(
            "UPDATE accounts SET last_seen_at = ? WHERE id = ?", (now(), account_id)
        )


# --------------------------------------------------------------- 登录会话


def create_session(
    db_path: Path, account_id: int, token_hash: str, *, days: int = 30
) -> dict:
    stamp = now()
    expires = (datetime.now() + timedelta(days=days)).isoformat(timespec="seconds")
    with connect(db_path) as conn:
        conn.execute(
            "INSERT INTO account_sessions "
            "(token_hash, account_id, created_at, expires_at, last_seen_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (token_hash, account_id, stamp, expires, stamp),
        )
    return {"expires_at": expires}


def get_session(db_path: Path, token_hash: str, *, moment: str | None = None) -> dict | None:
    """按 token 哈希取会话 + 账号；过期会话当作不存在（不自动删，交给清理）。"""
    moment = moment or now()
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT s.token_hash, s.account_id, s.expires_at, s.last_seen_at, "
            "a.username "
            "FROM account_sessions s JOIN accounts a ON a.id = s.account_id "
            "WHERE s.token_hash = ? AND s.expires_at > ?",
            (token_hash, moment),
        ).fetchone()
    return dict(row) if row else None


def touch_session(db_path: Path, token_hash: str, *, moment: str | None = None) -> None:
    moment = moment or now()
    with connect(db_path) as conn:
        conn.execute(
            "UPDATE account_sessions SET last_seen_at = ? WHERE token_hash = ?",
            (moment, token_hash),
        )


def delete_session(db_path: Path, token_hash: str) -> bool:
    with connect(db_path) as conn:
        return (
            conn.execute(
                "DELETE FROM account_sessions WHERE token_hash = ?", (token_hash,)
            ).rowcount
            > 0
        )


# --------------------------------------------------------------------- 关注项


def add_follow(db_path: Path, account_id: int, subject: str, source: str) -> dict:
    with connect(db_path) as conn:
        try:
            cursor = conn.execute(
                "INSERT INTO follows (account_id, subject, source, last_state, updated_at) "
                "VALUES (?, ?, ?, '', ?)",
                (account_id, subject, source, now()),
            )
        except sqlite3.IntegrityError as exc:
            # 同一个「关键词 + 来源」重复关注没有意义：会让扫描和通知都翻倍。
            raise ValueError("已经关注过这个关键词和来源") from exc
        row = conn.execute(
            "SELECT * FROM follows WHERE id = ?", (cursor.lastrowid,)
        ).fetchone()
    return dict(row)


def list_follows(db_path: Path, account_id: int) -> list[dict]:
    with connect(db_path) as conn:
        return [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM follows WHERE account_id = ? ORDER BY id", (account_id,)
            ).fetchall()
        ]


def get_follow(db_path: Path, account_id: int, follow_id: int) -> dict | None:
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM follows WHERE id = ? AND account_id = ?",
            (follow_id, account_id),
        ).fetchone()
    return dict(row) if row else None


def update_follow_state(
    db_path: Path, account_id: int, follow_id: int, last_state: str
) -> bool:
    with connect(db_path) as conn:
        cursor = conn.execute(
            "UPDATE follows SET last_state = ?, updated_at = ? "
            "WHERE id = ? AND account_id = ?",
            (last_state, now(), follow_id, account_id),
        )
        return cursor.rowcount > 0


def delete_follow(db_path: Path, account_id: int, follow_id: int) -> bool:
    """删关注项时一并删掉它的事件，避免界面上留下「无主」更新。"""
    with connect(db_path) as conn:
        owned = conn.execute(
            "SELECT 1 FROM follows WHERE id = ? AND account_id = ?",
            (follow_id, account_id),
        ).fetchone()
        if owned is None:
            return False
        conn.execute("DELETE FROM follow_events WHERE follow_id = ?", (follow_id,))
        return (
            conn.execute("DELETE FROM follows WHERE id = ?", (follow_id,)).rowcount > 0
        )


# --------------------------------------------------------------------- 关注事件


def add_follow_event(
    db_path: Path, account_id: int, follow_id: int, summary: str
) -> dict:
    with connect(db_path) as conn:
        owned = conn.execute(
            "SELECT 1 FROM follows WHERE id = ? AND account_id = ?",
            (follow_id, account_id),
        ).fetchone()
        if owned is None:
            raise ValueError("关注项不属于当前账号")
        cursor = conn.execute(
            "INSERT INTO follow_events (follow_id, summary, created_at, read) "
            "VALUES (?, ?, ?, 0)",
            (follow_id, summary, now()),
        )
        row = conn.execute(
            "SELECT * FROM follow_events WHERE id = ?", (cursor.lastrowid,)
        ).fetchone()
    return dict(row)


def list_follow_events(
    db_path: Path, account_id: int, *, unread_only: bool = False, limit: int = 20
) -> list[dict]:
    """事件连同关注项一起返回：前端只拿事件也要知道「是哪条关注更新了」。"""
    clause = "AND e.read = 0" if unread_only else ""
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT e.id, e.follow_id, e.summary, e.created_at, e.read, "
            "f.subject, f.source "
            "FROM follow_events e JOIN follows f ON f.id = e.follow_id "
            f"WHERE f.account_id = ? {clause} ORDER BY e.id DESC LIMIT ?",
            (account_id, limit),
        ).fetchall()
    return [{**dict(row), "read": bool(row["read"])} for row in rows]


def mark_follow_events_read(db_path: Path, account_id: int) -> int:
    with connect(db_path) as conn:
        return conn.execute(
            "UPDATE follow_events SET read = 1 WHERE read = 0 AND follow_id IN "
            "(SELECT id FROM follows WHERE account_id = ?)",
            (account_id,),
        ).rowcount


def unread_follow_events(db_path: Path, account_id: int) -> int:
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM follow_events e "
            "JOIN follows f ON f.id = e.follow_id "
            "WHERE e.read = 0 AND f.account_id = ?",
            (account_id,),
        ).fetchone()
    return int(row["n"])


# ------------------------------------------------------------------- 长期记忆


def upsert_memory(
    db_path: Path, account_id: int, kind: str, value: str, source: str = ""
) -> dict:
    """写入一条稳定事实；同账号同 kind + value 重复出现时只累加 hits。

    ``updated_at`` 每次都刷新——「多久没再被确认」是过期依据，重复说一次
    就等于重新确认一次。``created_at`` / 首次的 ``source`` 保留不动。
    """
    stamp = now()
    with connect(db_path) as conn:
        conn.execute(
            "INSERT INTO long_memories "
            "(account_id, kind, value, source, created_at, updated_at, hits) "
            "VALUES (?, ?, ?, ?, ?, ?, 1) "
            "ON CONFLICT(account_id, kind, value) DO UPDATE SET "
            "updated_at = excluded.updated_at, hits = hits + 1",
            (account_id, kind, value, source, stamp, stamp),
        )
        row = conn.execute(
            "SELECT * FROM long_memories "
            "WHERE account_id = ? AND kind = ? AND value = ?",
            (account_id, kind, value),
        ).fetchone()
    return dict(row)


def list_memories(
    db_path: Path,
    account_id: int,
    *,
    limit: int = KEEP_MEMORIES_PER_ACCOUNT,
    max_age_days: float = MEMORY_MAX_AGE_DAYS,
) -> list[dict]:
    """按最近确认排序的稳定事实；``max_age_days <= 0`` 表示不按时间过滤。"""
    clause = ""
    params: list = [account_id]
    if max_age_days and max_age_days > 0:
        clause = " AND updated_at >= ?"
        params.append(_ago(max_age_days))
    params.append(max(1, int(limit)))
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT id, kind, value, source, created_at, updated_at, hits "
            f"FROM long_memories WHERE account_id = ?{clause} "
            "ORDER BY updated_at DESC, id DESC LIMIT ?",
            params,
        ).fetchall()
    return [dict(row) for row in rows]


def delete_memory(db_path: Path, account_id: int, memory_id: int) -> bool:
    with connect(db_path) as conn:
        return (
            conn.execute(
                "DELETE FROM long_memories WHERE id = ? AND account_id = ?",
                (memory_id, account_id),
            ).rowcount
            > 0
        )


def clear_memories(db_path: Path, account_id: int) -> int:
    with connect(db_path) as conn:
        return conn.execute(
            "DELETE FROM long_memories WHERE account_id = ?", (account_id,)
        ).rowcount


# --------------------------------------------------------------------- 对话线程


def create_thread(db_path: Path, account_id: int, title: str = "") -> dict:
    stamp = now()
    with connect(db_path) as conn:
        cursor = conn.execute(
            "INSERT INTO chat_threads (account_id, title, created_at, updated_at, "
            "message_count) VALUES (?, ?, ?, ?, 0)",
            (account_id, (title or "").strip()[:60], stamp, stamp),
        )
        row = conn.execute(
            "SELECT * FROM chat_threads WHERE id = ?", (cursor.lastrowid,)
        ).fetchone()
    return dict(row)


def list_threads(db_path: Path, account_id: int, limit: int = KEEP_THREADS_PER_ACCOUNT) -> list[dict]:
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT id, title, created_at, updated_at, message_count "
            "FROM chat_threads WHERE account_id = ? "
            "ORDER BY updated_at DESC, id DESC LIMIT ?",
            (account_id, limit),
        ).fetchall()
    return [dict(row) for row in rows]


def get_thread(db_path: Path, account_id: int, thread_id: int) -> dict | None:
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM chat_threads WHERE id = ? AND account_id = ?",
            (thread_id, account_id),
        ).fetchone()
    return dict(row) if row else None


def latest_thread(db_path: Path, account_id: int) -> dict | None:
    threads = list_threads(db_path, account_id, limit=1)
    return threads[0] if threads else None


def set_thread_title(db_path: Path, account_id: int, thread_id: int, title: str) -> bool:
    with connect(db_path) as conn:
        return (
            conn.execute(
                "UPDATE chat_threads SET title = ? WHERE id = ? AND account_id = ?",
                (title.strip()[:60], thread_id, account_id),
            ).rowcount
            > 0
        )


def add_thread_exchange(
    db_path: Path,
    account_id: int,
    thread_id: int,
    question: str,
    answer: str,
    *,
    keep: int = KEEP_MESSAGES_PER_THREAD,
) -> bool:
    """把一问一答写进指定对话；对话必须属于该账号。

    一问一答同事务写入：只落 user 不落 assistant 会让下一轮上下文出现
    「用户说了话但助手没回应」的断片。``keep > 0`` 时只保留本对话最近这么多条。
    """
    stamp = now()
    with connect(db_path) as conn:
        owned = conn.execute(
            "SELECT 1 FROM chat_threads WHERE id = ? AND account_id = ?",
            (thread_id, account_id),
        ).fetchone()
        if owned is None:
            return False
        conn.executemany(
            "INSERT INTO thread_messages (thread_id, role, content, created_at) "
            "VALUES (?, ?, ?, ?)",
            [
                (thread_id, "user", question, stamp),
                (thread_id, "assistant", answer, stamp),
            ],
        )
        if keep > 0:
            conn.execute(
                "DELETE FROM thread_messages WHERE thread_id = ? AND id NOT IN "
                "(SELECT id FROM thread_messages WHERE thread_id = ? "
                "ORDER BY id DESC LIMIT ?)",
                (thread_id, thread_id, keep),
            )
        conn.execute(
            "UPDATE chat_threads SET updated_at = ?, "
            "message_count = (SELECT COUNT(*) FROM thread_messages WHERE thread_id = ?) "
            "WHERE id = ?",
            (stamp, thread_id, thread_id),
        )
    return True


def thread_messages(
    db_path: Path, account_id: int, thread_id: int, limit: int = KEEP_MESSAGES_PER_THREAD
) -> list[dict]:
    with connect(db_path) as conn:
        owned = conn.execute(
            "SELECT 1 FROM chat_threads WHERE id = ? AND account_id = ?",
            (thread_id, account_id),
        ).fetchone()
        if owned is None:
            return []
        rows = conn.execute(
            "SELECT role, content, created_at FROM thread_messages "
            "WHERE thread_id = ? ORDER BY id DESC LIMIT ?",
            (thread_id, max(1, int(limit))),
        ).fetchall()
    return [dict(row) for row in reversed(rows)]


def delete_thread(db_path: Path, account_id: int, thread_id: int) -> int:
    """删对话与它的消息；返回删掉的消息条数（对话不存在时为 0）。"""
    with connect(db_path) as conn:
        owned = conn.execute(
            "SELECT 1 FROM chat_threads WHERE id = ? AND account_id = ?",
            (thread_id, account_id),
        ).fetchone()
        if owned is None:
            return 0
        removed = conn.execute(
            "DELETE FROM thread_messages WHERE thread_id = ?", (thread_id,)
        ).rowcount
        conn.execute("DELETE FROM chat_threads WHERE id = ?", (thread_id,))
    return removed


def clear_threads(db_path: Path, account_id: int) -> int:
    """清空本账号全部对话，返回删掉的消息条数。关注项不受影响。"""
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT id FROM chat_threads WHERE account_id = ?", (account_id,)
        ).fetchall()
        ids = [row["id"] for row in rows]
        if not ids:
            return 0
        removed = 0
        for start in range(0, len(ids), 400):
            chunk = ids[start : start + 400]
            placeholders = ",".join("?" for _ in chunk)
            removed += conn.execute(
                f"DELETE FROM thread_messages WHERE thread_id IN ({placeholders})", chunk
            ).rowcount
            conn.execute(
                f"DELETE FROM chat_threads WHERE id IN ({placeholders})", chunk
            )
    return removed


# --------------------------------------------------------------------- 清理


def _delete_threads(conn: sqlite3.Connection, ids: list[int]) -> int:
    """分批删，避开 SQLite 的 999 个绑定参数上限（旧库可能积很多对话）。"""
    if not ids:
        return 0
    removed = 0
    for start in range(0, len(ids), 400):
        chunk = ids[start : start + 400]
        placeholders = ",".join("?" for _ in chunk)
        conn.execute(
            f"DELETE FROM thread_messages WHERE thread_id IN ({placeholders})", chunk
        )
        removed += conn.execute(
            f"DELETE FROM chat_threads WHERE id IN ({placeholders})", chunk
        ).rowcount
    return removed


def prune_user_data(db_path: Path, *, max_age_days: int = THREAD_MAX_AGE_DAYS) -> dict:
    """把用户数据收敛到固定上限；返回删了多少（便于日志与测试）。

    * 超过 ``max_age_days`` 没更新的对话；
    * 每个账号只留最近 ``KEEP_THREADS_PER_ACCOUNT`` 个对话；
    * 全库只留最近 ``MAX_THREADS_TOTAL`` 个对话（防止注册大量账号把磁盘塞满）；
    * 过期登录会话，以及每个账号只留最近 ``KEEP_SESSIONS_PER_ACCOUNT`` 个；
    * 长期记忆：一年没再确认的过期，每账号只留 ``KEEP_MEMORIES_PER_ACCOUNT`` 条；
    * 孤儿消息（对话已删但消息残留）。
    """
    stamp = now()
    removed = {"threads": 0, "sessions": 0, "memories": 0}
    with connect(db_path) as conn:
        stale = [
            row["id"]
            for row in conn.execute(
                "SELECT id FROM chat_threads WHERE updated_at < ?", (_ago(max_age_days),)
            ).fetchall()
        ]
        removed["threads"] += _delete_threads(conn, stale)

        account_ids = [
            row[0]
            for row in conn.execute(
                "SELECT account_id FROM chat_threads "
                "UNION SELECT account_id FROM account_sessions "
                "UNION SELECT account_id FROM long_memories"
            ).fetchall()
        ]
        for account_id in account_ids:
            extra = [
                row["id"]
                for row in conn.execute(
                    "SELECT id FROM chat_threads WHERE account_id = ? "
                    "ORDER BY updated_at DESC, id DESC LIMIT -1 OFFSET ?",
                    (account_id, KEEP_THREADS_PER_ACCOUNT),
                ).fetchall()
            ]
            removed["threads"] += _delete_threads(conn, extra)

        overflow = [
            row["id"]
            for row in conn.execute(
                "SELECT id FROM chat_threads ORDER BY updated_at DESC, id DESC "
                "LIMIT -1 OFFSET ?",
                (MAX_THREADS_TOTAL,),
            ).fetchall()
        ]
        removed["threads"] += _delete_threads(conn, overflow)

        conn.execute(
            "DELETE FROM thread_messages WHERE thread_id NOT IN "
            "(SELECT id FROM chat_threads)"
        )

        # 长期记忆：一年没再被确认的事实过期；每账号只留最近若干条。
        removed["memories"] += conn.execute(
            "DELETE FROM long_memories WHERE updated_at < ?", (_ago(MEMORY_MAX_AGE_DAYS),)
        ).rowcount
        for account_id in account_ids:
            removed["memories"] += conn.execute(
                "DELETE FROM long_memories WHERE id IN ("
                "SELECT id FROM long_memories WHERE account_id = ? "
                "ORDER BY updated_at DESC, id DESC LIMIT -1 OFFSET ?)",
                (account_id, KEEP_MEMORIES_PER_ACCOUNT),
            ).rowcount

        removed["sessions"] += conn.execute(
            "DELETE FROM account_sessions WHERE expires_at <= ?", (stamp,)
        ).rowcount
        for account_id in account_ids:
            extra_sessions = [
                row["token_hash"]
                for row in conn.execute(
                    "SELECT token_hash FROM account_sessions WHERE account_id = ? "
                    "ORDER BY last_seen_at DESC LIMIT -1 OFFSET ?",
                    (account_id, KEEP_SESSIONS_PER_ACCOUNT),
                ).fetchall()
            ]
            if extra_sessions:
                placeholders = ",".join("?" for _ in extra_sessions)
                removed["sessions"] += conn.execute(
                    f"DELETE FROM account_sessions WHERE token_hash IN ({placeholders})",
                    extra_sessions,
                ).rowcount
    return removed
