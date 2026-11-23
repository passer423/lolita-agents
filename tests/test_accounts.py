"""账号体系：注册/登录/登出、接口门禁、跨账号隔离、会话过期。"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

from fastapi.testclient import TestClient

from app import accounts, db
from app.config import settings
from app.main import app


def _fresh(data_dir):
    return TestClient(app)


def test_register_sets_httponly_cookie_and_me_works(client):
    me = client.get("/api/auth/me")
    assert me.status_code == 200
    assert me.json()["account"]["username"] == "tester"

    cookie = client.post(
        "/api/auth/logout"
    )  # 清掉，再从登录接口拿一次 Set-Cookie
    assert cookie.status_code == 200

    response = client.post(
        "/api/auth/login",
        json={"username": "tester", "password": "test-password-123"},
    )
    assert response.status_code == 200
    header = response.headers["set-cookie"]
    assert "HttpOnly" in header
    assert "SameSite=lax" in header
    assert client.get("/api/auth/me").status_code == 200


def test_password_is_stored_as_hash_not_plaintext(client):
    row = db.get_account_by_username(settings.db_path, "tester")
    assert row is not None
    assert row["password_hash"].startswith("scrypt$")
    assert "test-password-123" not in row["password_hash"]
    assert accounts.verify_password("test-password-123", row["password_hash"])
    assert not accounts.verify_password("wrong-password", row["password_hash"])


def test_register_rejects_duplicate_and_weak_password(client):
    duplicate = client.post(
        "/api/auth/register",
        json={"username": "TESTER", "password": "test-password-123"},
    )
    assert duplicate.status_code == 409

    weak = _fresh(settings.data_dir).post(
        "/api/auth/register", json={"username": "weakuser", "password": "123"}
    )
    assert weak.status_code == 422


def test_login_rejects_wrong_password_without_leaking_existence(client):
    wrong = _fresh(settings.data_dir).post(
        "/api/auth/login",
        json={"username": "tester", "password": "not-the-password"},
    )
    unknown = _fresh(settings.data_dir).post(
        "/api/auth/login",
        json={"username": "nobody-here", "password": "not-the-password"},
    )
    assert wrong.status_code == 401
    assert unknown.status_code == 401
    assert wrong.json()["detail"] == unknown.json()["detail"]


def test_logout_invalidates_server_side_session(client):
    assert client.get("/api/threads").status_code == 200
    assert client.post("/api/auth/logout").status_code == 200
    assert client.get("/api/threads").status_code == 401
    assert client.get("/api/auth/me").status_code == 401


def test_anonymous_cannot_reach_user_endpoints(data_dir):
    anon = _fresh(data_dir)
    assert anon.post("/api/chat", json={"message": "hi"}).status_code == 401
    assert anon.post("/api/chat/stream", json={"message": "hi"}).status_code == 401
    assert anon.get("/api/chat/history").status_code == 401
    assert anon.get("/api/threads").status_code == 401
    assert anon.get("/api/follows").status_code == 401
    assert anon.get("/api/jobs").status_code == 401
    assert anon.get("/api/chat/runs").status_code == 401
    # 白名单：探针与登录页要能判断模型是否配好
    assert anon.get("/api/chat/status").status_code == 200
    assert anon.get("/api/health").status_code == 200


def test_threads_are_not_visible_across_accounts(client):
    thread = db.create_thread(settings.db_path, client.account["id"], "Alice 的对话")
    db.add_thread_exchange(
        settings.db_path, client.account["id"], thread["id"], "只看我", "只有我能看到"
    )

    bob = _fresh(settings.data_dir)
    registered = bob.post(
        "/api/auth/register",
        json={"username": "bob", "password": "bob-password-123"},
    )
    assert registered.status_code == 201

    assert bob.get("/api/threads").json() == {"threads": []}
    assert bob.get("/api/chat/history").json() == {"messages": []}
    assert bob.get(f"/api/threads/{thread['id']}/messages").status_code == 404
    assert bob.delete(f"/api/threads/{thread['id']}").status_code == 404
    # 猜 id 提问也不行：不能往别人的对话里追加
    assert bob.post(
        "/api/chat", json={"message": "插一句", "conversation_id": thread["id"]}
    ).status_code == 404

    mine = client.get("/api/threads").json()["threads"]
    assert [item["id"] for item in mine] == [thread["id"]]
    assert client.get(f"/api/threads/{thread['id']}/messages").json()["messages"]


def test_thread_crud_over_http(client):
    created = client.post("/api/threads")
    assert created.status_code == 201
    thread = created.json()["thread"]
    assert thread["title"] == "新对话"

    listed = client.get("/api/threads").json()["threads"]
    assert listed[0]["id"] == thread["id"]
    assert client.get(f"/api/threads/{thread['id']}/messages").json()["messages"] == []

    assert client.delete(f"/api/threads/{thread['id']}").status_code == 200
    assert client.get("/api/threads").json() == {"threads": []}
    assert client.delete(f"/api/threads/{thread['id']}").status_code == 404


def test_expired_session_returns_401_and_does_not_work(client):
    past = (datetime.now() - timedelta(seconds=5)).isoformat(timespec="seconds")
    with db.connect(settings.db_path) as conn:
        conn.execute("UPDATE account_sessions SET expires_at = ?", (past,))

    assert client.get("/api/auth/me").status_code == 401
    assert client.get("/api/threads").status_code == 401


def test_login_in_new_browser_restores_history(client):
    thread = db.create_thread(settings.db_path, client.account["id"], "跨浏览器")
    db.add_thread_exchange(
        settings.db_path, client.account["id"], thread["id"], "问题", "回答"
    )

    another = _fresh(settings.data_dir)
    assert another.get("/api/threads").status_code == 401
    assert another.post(
        "/api/auth/login",
        json={"username": "tester", "password": "test-password-123"},
    ).status_code == 200
    listed = another.get("/api/threads").json()["threads"]
    assert [item["id"] for item in listed] == [thread["id"]]
    messages = another.get(f"/api/threads/{thread['id']}/messages").json()["messages"]
    assert [(m["role"], m["content"]) for m in messages] == [
        ("user", "问题"),
        ("assistant", "回答"),
    ]


def test_username_rules_give_actionable_messages(client):
    cases = [
        ("", "请填写用户名"),
        ("   ", "请填写用户名"),
        ("ab", "至少 3 个字符"),
        ("a" * 33, "最多 32 个字符"),
        ("has space", "只能包含中文、字母、数字"),
        ("!!!", "只能包含中文、字母、数字"),
        ("___", "至少要包含一个中文、字母或数字"),
    ]
    for name, hint in cases:
        response = client.post(
            "/api/auth/register",
            json={"username": name, "password": "test-password-123"},
        )
        assert response.status_code == 422, (name, response.text)
        assert hint in response.json()["detail"], (name, response.json())

    # 中文用户名可以注册；首尾空白会被去掉
    chinese = client.post(
        "/api/auth/register",
        json={"username": "  洛丽塔迷  ", "password": "test-password-123"},
    )
    assert chinese.status_code == 201
    assert chinese.json()["account"]["username"] == "洛丽塔迷"


def test_old_email_account_still_logs_in_after_migration(tmp_path):
    """v5 的 email 列改名成 username：旧邮箱账号原样可登录，不需要重置密码。"""
    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE accounts ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "email TEXT NOT NULL UNIQUE COLLATE NOCASE, "
        "password_hash TEXT NOT NULL, created_at TEXT NOT NULL, "
        "last_seen_at TEXT NOT NULL)"
    )
    conn.execute(
        "INSERT INTO accounts (email, password_hash, created_at, last_seen_at) "
        "VALUES (?, ?, ?, ?)",
        (
            "old@example.com",
            accounts.hash_password("old-password-123"),
            "2026-01-01T00:00:00",
            "2026-01-01T00:00:00",
        ),
    )
    conn.commit()
    conn.close()

    db.init_db(path)

    row = db.get_account_by_username(path, "OLD@example.com")
    assert row is not None
    assert row["username"] == "old@example.com"
    account, _ = accounts.login(path, "old@example.com", "old-password-123")
    assert account["username"] == "old@example.com"


def test_follows_are_isolated_between_accounts(client):
    db.add_follow(settings.db_path, client.account["id"], "Alice 的关注", "web")

    bob = _fresh(settings.data_dir)
    bob.post(
        "/api/auth/register",
        json={"username": "bob-follow", "password": "bob-password-123"},
    )
    assert bob.get("/api/follows").json()["follows"] == []
    assert [f["subject"] for f in client.get("/api/follows").json()["follows"]] == [
        "Alice 的关注"
    ]
