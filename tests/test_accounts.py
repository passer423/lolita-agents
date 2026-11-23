"""账号体系：注册/登录/登出、接口门禁、跨账号隔离、会话过期。"""

from __future__ import annotations

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
    assert me.json()["account"]["email"] == "tester@example.com"

    cookie = client.post(
        "/api/auth/logout"
    )  # 清掉，再从登录接口拿一次 Set-Cookie
    assert cookie.status_code == 200

    response = client.post(
        "/api/auth/login",
        json={"email": "tester@example.com", "password": "test-password-123"},
    )
    assert response.status_code == 200
    header = response.headers["set-cookie"]
    assert "HttpOnly" in header
    assert "SameSite=lax" in header
    assert client.get("/api/auth/me").status_code == 200


def test_password_is_stored_as_hash_not_plaintext(client):
    row = db.get_account_by_email(settings.db_path, "tester@example.com")
    assert row is not None
    assert row["password_hash"].startswith("scrypt$")
    assert "test-password-123" not in row["password_hash"]
    assert accounts.verify_password("test-password-123", row["password_hash"])
    assert not accounts.verify_password("wrong-password", row["password_hash"])


def test_register_rejects_duplicate_and_weak_password(client):
    duplicate = client.post(
        "/api/auth/register",
        json={"email": "TESTER@example.com", "password": "test-password-123"},
    )
    assert duplicate.status_code == 409

    weak = _fresh(settings.data_dir).post(
        "/api/auth/register", json={"email": "weak@example.com", "password": "123"}
    )
    assert weak.status_code == 422


def test_login_rejects_wrong_password_without_leaking_existence(client):
    wrong = _fresh(settings.data_dir).post(
        "/api/auth/login",
        json={"email": "tester@example.com", "password": "not-the-password"},
    )
    unknown = _fresh(settings.data_dir).post(
        "/api/auth/login",
        json={"email": "nobody@example.com", "password": "not-the-password"},
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
        json={"email": "bob@example.com", "password": "bob-password-123"},
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
        json={"email": "tester@example.com", "password": "test-password-123"},
    ).status_code == 200
    listed = another.get("/api/threads").json()["threads"]
    assert [item["id"] for item in listed] == [thread["id"]]
    messages = another.get(f"/api/threads/{thread['id']}/messages").json()["messages"]
    assert [(m["role"], m["content"]) for m in messages] == [
        ("user", "问题"),
        ("assistant", "回答"),
    ]


def test_follows_are_isolated_between_accounts(client):
    db.add_follow(settings.db_path, client.account["id"], "Alice 的关注", "web")

    bob = _fresh(settings.data_dir)
    bob.post(
        "/api/auth/register",
        json={"email": "bob-follow@example.com", "password": "bob-password-123"},
    )
    assert bob.get("/api/follows").json()["follows"] == []
    assert [f["subject"] for f in client.get("/api/follows").json()["follows"]] == [
        "Alice 的关注"
    ]
