"""可选访问控制：API key / JWT、限流、按天成本配额。

重点验证三件事：

* 默认 ``AUTH_MODE=off`` 的行为与以前一致（不拦请求）；
* 开了鉴权后，公开路径仍然公开、错误配置在构造期就报错；
* 配额读的是 ``trace.cost_summary`` 那份账本，不是另一套计量。
"""

from __future__ import annotations

import base64
import time
from datetime import datetime, timedelta

import jwt
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.agent import trace
from app.auth import (
    JWT_ALGORITHM,
    AccessControl,
    mint_token,
    verify_token,
)
from app.auth import (
    router as auth_router,
)
from app.config import AppSettings


def _build(cfg: AppSettings) -> FastAPI:
    app = FastAPI()
    app.add_middleware(AccessControl, settings=cfg)

    @app.get("/")
    def index() -> dict:
        return {"home": True}

    @app.get("/chat.js")
    def asset() -> dict:
        return {"asset": True}

    @app.get("/api/health")
    def health() -> dict:
        return {"ok": True}

    @app.get("/api/ping")
    def ping() -> dict:
        return {"ok": True}

    @app.get("/api/chat")
    def chat_read() -> dict:
        return {"read": True}

    @app.post("/api/chat")
    def chat() -> dict:
        return {"answer": True}

    return app


def _settings(**overrides) -> AppSettings:
    base = {
        "auth_mode": "off",
        "auth_api_keys": "",
        "auth_jwt_secret": "",
        "rate_limit_requests": 0,
        "rate_limit_window": 60.0,
        "daily_quota": 0.0,
    }
    base.update(overrides)
    return AppSettings(**base)


# --------------------------------------------------------------------- 默认关闭


def test_off_mode_keeps_current_behavior(tmp_path):
    client = TestClient(_build(_settings(data_dir=tmp_path)))
    assert client.get("/api/ping").status_code == 200
    assert client.get("/api/chat").status_code == 200
    assert client.post("/api/chat").status_code == 200


def test_invalid_mode_fails_fast():
    with pytest.raises(RuntimeError, match="AUTH_MODE"):
        AccessControl(lambda s, r, c: None, settings=_settings(auth_mode="oauth"))


# --------------------------------------------------------------------- API key


def test_api_key_required_and_accepted_both_headers(tmp_path):
    cfg = _settings(auth_mode="api_key", auth_api_keys="k1:alice, k2", data_dir=tmp_path)
    client = TestClient(_build(cfg))

    missing = client.get("/api/ping")
    assert missing.status_code == 401
    assert "WWW-Authenticate" in missing.headers

    assert client.get("/api/ping", headers={"X-API-Key": "wrong"}).status_code == 401
    assert client.get("/api/ping", headers={"X-API-Key": "k1"}).status_code == 200
    assert (
        client.get("/api/ping", headers={"Authorization": "Bearer k2"}).status_code == 200
    )


def test_api_key_mode_without_keys_fails_fast():
    with pytest.raises(RuntimeError, match="AUTH_API_KEYS"):
        AccessControl(lambda s, r, c: None, settings=_settings(auth_mode="api_key"))


def test_public_paths_stay_public(tmp_path):
    cfg = _settings(auth_mode="api_key", auth_api_keys="k1", data_dir=tmp_path)
    client = TestClient(_build(cfg))
    assert client.get("/").status_code == 200
    assert client.get("/chat.js").status_code == 200
    assert client.get("/api/health").status_code == 200


# --------------------------------------------------------------------- JWT


def test_jwt_roundtrip_and_expiry(tmp_path):
    secret = "s" * 40
    cfg = _settings(auth_mode="jwt", auth_jwt_secret=secret, data_dir=tmp_path)
    client = TestClient(_build(cfg))

    token = mint_token(cfg, subject="alice")
    assert verify_token(token, cfg) == "alice"
    assert (
        client.get("/api/ping", headers={"Authorization": f"Bearer {token}"}).status_code
        == 200
    )

    expired = jwt.encode(
        {
            "sub": "alice",
            "exp": int((datetime.now() - timedelta(seconds=5)).timestamp()),
        },
        secret,
        algorithm=JWT_ALGORITHM,
    )
    assert client.get("/api/ping", headers={"Authorization": f"Bearer {expired}"}).status_code == 401

    # 篡改必须改到字节本身：base64 末尾字符可能只影响 padding 位，
    # 换字符常常解出同一串字节，那样的"篡改"前提就不成立。
    header, payload, signature = token.split(".")
    raw = base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4))
    flipped = bytes([raw[0] ^ 0x01]) + raw[1:]
    tampered_sig = base64.urlsafe_b64encode(flipped).rstrip(b"=").decode("ascii")
    assert tampered_sig != signature
    assert (
        base64.urlsafe_b64decode(tampered_sig + "=" * (-len(tampered_sig) % 4))
        != raw
    )
    tampered = f"{header}.{payload}.{tampered_sig}"
    assert client.get("/api/ping", headers={"Authorization": f"Bearer {tampered}"}).status_code == 401


def test_jwt_mode_without_secret_fails_fast():
    with pytest.raises(RuntimeError, match="AUTH_JWT_SECRET"):
        AccessControl(
            lambda s, r, c: None,
            settings=_settings(auth_mode="jwt", auth_api_keys="k1"),
        )


def test_token_endpoint_exchanges_key(monkeypatch):
    from app.config import settings as default_settings

    monkeypatch.setattr(default_settings, "auth_mode", "jwt")
    monkeypatch.setattr(default_settings, "auth_api_keys", "k1:alice")
    monkeypatch.setattr(default_settings, "auth_jwt_secret", "s" * 40)
    monkeypatch.setattr(default_settings, "auth_jwt_ttl", 600)

    app = FastAPI()
    app.include_router(auth_router)
    client = TestClient(app)

    ok = client.post("/api/auth/token", json={"api_key": "k1"})
    assert ok.status_code == 200
    body = ok.json()
    assert body["token_type"] == "Bearer"
    assert body["expires_in"] == 600
    assert verify_token(body["access_token"], default_settings) == "alice"

    assert client.post("/api/auth/token", json={"api_key": "nope"}).status_code == 401
    assert client.post("/api/auth/token", headers={"X-API-Key": "k1"}).status_code == 200

    monkeypatch.setattr(default_settings, "auth_mode", "api_key")
    assert client.post("/api/auth/token", json={"api_key": "k1"}).status_code == 404


# --------------------------------------------------------------------- 限流


def test_rate_limit_is_per_key_and_sets_retry_after(tmp_path):
    cfg = _settings(
        auth_mode="api_key",
        auth_api_keys="k1:alice, k2:bob",
        rate_limit_requests=2,
        rate_limit_window=60.0,
        data_dir=tmp_path,
    )
    client = TestClient(_build(cfg))

    for _ in range(2):
        assert client.get("/api/ping", headers={"X-API-Key": "k1"}).status_code == 200

    limited = client.get("/api/ping", headers={"X-API-Key": "k1"})
    assert limited.status_code == 429
    assert int(limited.headers["Retry-After"]) >= 1
    assert "频繁" in limited.json()["detail"]

    # 另一个 key 有自己的桶，不受影响
    assert client.get("/api/ping", headers={"X-API-Key": "k2"}).status_code == 200


def test_rate_limit_window_slides(tmp_path):
    cfg = _settings(
        auth_mode="api_key",
        auth_api_keys="k1",
        rate_limit_requests=1,
        rate_limit_window=0.3,
        data_dir=tmp_path,
    )
    client = TestClient(_build(cfg))
    assert client.get("/api/ping", headers={"X-API-Key": "k1"}).status_code == 200
    assert client.get("/api/ping", headers={"X-API-Key": "k1"}).status_code == 429
    time.sleep(0.35)
    assert client.get("/api/ping", headers={"X-API-Key": "k1"}).status_code == 200


# --------------------------------------------------------------------- 配额


def _write_cost(tmp_path, *, cost: float | None, mode: str = "smart") -> AppSettings:
    cfg = _settings(data_dir=tmp_path)
    item = {"calls": 1, "prompt_tokens": 10, "completion_tokens": 5}
    if cost is not None:
        item["cost"] = cost
    trace.append({"cost": {"mode": mode, "by_model": {"deepseek-chat": item}}}, app=cfg)
    return cfg


def test_quota_reuses_trace_cost_and_blocks_chat(tmp_path):
    cfg = _write_cost(tmp_path, cost=0.5)
    cfg.daily_quota = 0.4
    client = TestClient(_build(cfg))

    # 账本确实来自 trace.cost_summary(days=1)
    summary = trace.cost_summary(days=1, app=cfg)
    assert summary["totals"]["cost"] == pytest.approx(0.5)

    blocked = client.post("/api/chat")
    assert blocked.status_code == 429
    assert int(blocked.headers["Retry-After"]) >= 1
    assert "配额" in blocked.json()["detail"]

    # 只拦花钱的 POST chat：读接口与其它 API 不受影响
    assert client.get("/api/chat").status_code == 200
    assert client.get("/api/ping").status_code == 200


def test_quota_below_limit_allows_chat(tmp_path):
    cfg = _write_cost(tmp_path, cost=0.1)
    cfg.daily_quota = 1.0
    client = TestClient(_build(cfg))
    assert client.post("/api/chat").status_code == 200


def test_unpriced_models_do_not_block(tmp_path):
    cfg = _write_cost(tmp_path, cost=None)
    cfg.daily_quota = 0.0001
    client = TestClient(_build(cfg))
    # cost=None 表示「没配价格」，不是「花了 0 元」；不应按 0 通过，也不该误拦。
    assert trace.cost_summary(days=1, app=cfg)["totals"]["cost"] is None
    assert client.post("/api/chat").status_code == 200
