"""D 组：关注项更新通知（形态甲）。

关注项按形态甲：扫描由前端触发，事件落库；通道不可用 ≠ 没有更新。
"""

from __future__ import annotations

import pytest

from app.agent import channels
from app.agent.channels import ChannelUnavailable
from app.services import follow_watch


def _evidence(url: str, title: str, text: str = "内容") -> dict:
    return {
        "channel": "web",
        "source_kind": "realtime",
        "ref": url,
        "title": title,
        "text": text,
        "url": url,
    }


# --------------------------------------------------------------------- 关注项


def test_follows_crud(client):
    assert client.get("/api/follows").json()["follows"] == []

    body = client.post(
        "/api/follows", json={"subject": "Baby 缎带与齿轮", "source": "web"}
    ).json()
    assert len(body["follows"]) == 1
    follow = body["follows"][0]
    assert follow["subject"] == "Baby 缎带与齿轮"
    assert follow["source"] == "web"

    # 同一关键词 + 同一来源不重复关注
    assert client.post(
        "/api/follows", json={"subject": "Baby 缎带与齿轮", "source": "web"}
    ).status_code == 409

    # 未知来源由 pydantic 拦下
    assert client.post(
        "/api/follows", json={"subject": "AP", "source": "量子"}
    ).status_code == 422

    assert client.delete(f"/api/follows/{follow['id']}").json()["follows"] == []
    assert client.delete(f"/api/follows/{follow['id']}").status_code == 404


def test_follows_sources_report_readiness(client):
    """关注面板的来源状态必须和召回通道同源：区分「没开」与「开了没就绪」。"""
    sources = {
        item["name"]: item for item in client.get("/api/follows").json()["sources"]
    }
    assert set(sources) == {"own", "real", "lolibrary", "xhs", "weibo", "web"}
    assert sources["web"]["enabled"] is False
    assert sources["web"]["ready"] is False
    assert "SEARCH_ENABLED" in sources["web"]["reason"]
    assert sources["own"]["enabled"] is True


def test_check_baseline_then_update_then_read(client, monkeypatch):
    state = {"items": [_evidence("https://a/1", "旧款")]}
    monkeypatch.setattr(channels, "recall", lambda *_a, **_k: state["items"])
    client.post("/api/follows", json={"subject": "AP 新款", "source": "web"})

    first = client.post("/api/follows/check").json()
    assert first["results"][0]["status"] == "baseline"
    assert first["events"] == []
    assert first["unread"] == 0

    state["items"] = [_evidence("https://a/1", "旧款"), _evidence("https://a/2", "新款")]
    second = client.post("/api/follows/check").json()
    assert second["results"][0]["status"] == "updated"
    assert "新增 1 条" in second["results"][0]["detail"]
    assert second["unread"] == 1

    event = client.get("/api/follows").json()["events"][0]
    assert event["subject"] == "AP 新款"
    assert event["read"] is False

    marked = client.post("/api/follows/events/read").json()
    assert marked["marked"] == 1
    assert marked["unread"] == 0


def test_channel_unavailable_is_not_no_update(client, monkeypatch):
    """通道不可用不能写成「没有更新」，也不能覆盖基线。"""
    monkeypatch.setattr(channels, "recall", lambda *_a, **_k: [_evidence("https://a/1", "旧款")])
    client.post("/api/follows", json={"subject": "AP 新款", "source": "web"})
    assert client.post("/api/follows/check").json()["results"][0]["status"] == "baseline"

    def boom(*_args, **_kwargs):
        raise ChannelUnavailable("搜索未配置：缺 SEARCH_API_KEY")

    monkeypatch.setattr(channels, "recall", boom)
    unavailable = client.post("/api/follows/check").json()
    assert unavailable["results"][0]["status"] == "unavailable"
    assert "SEARCH_API_KEY" in unavailable["results"][0]["detail"]
    assert unavailable["events"] == []

    # 基线还在：恢复通道后对比的是旧快照，而不是重新记基线
    monkeypatch.setattr(
        channels, "recall",
        lambda *_a, **_k: [_evidence("https://a/1", "旧款"), _evidence("https://a/2", "新款")],
    )
    recovered = client.post("/api/follows/check").json()
    assert recovered["results"][0]["status"] == "updated"


def test_check_timeout_does_not_touch_baseline(client, monkeypatch):
    """单项超时只报 timeout：不写事件、不写基线，下次扫描仍按旧快照对比。"""
    import time

    from app import db
    from app.config import settings

    monkeypatch.setattr(settings, "follow_item_timeout", 0.05)
    monkeypatch.setattr(settings, "follow_check_deadline", 10.0)

    def slow(*_args, **_kwargs):
        time.sleep(0.4)
        return [_evidence("https://a/1", "新款")]

    monkeypatch.setattr(channels, "recall", slow)
    follow = client.post(
        "/api/follows", json={"subject": "AP 新款", "source": "web"}
    ).json()["follows"][0]

    body = client.post("/api/follows/check").json()

    assert body["results"][0]["status"] == "timeout"
    assert body["partial"] is True
    assert body["events"] == []
    assert not db.get_follow(settings.db_path, follow["id"])["last_state"]


def test_check_total_deadline_skips_remaining_items(client, monkeypatch):
    """整轮预算用完时，后面的关注项标记 skipped，而不是继续排队等几分钟。"""
    import time

    from app.config import settings

    monkeypatch.setattr(settings, "follow_item_timeout", 10.0)
    monkeypatch.setattr(settings, "follow_check_deadline", 0.1)

    def slow(*_args, **_kwargs):
        time.sleep(0.3)
        return [_evidence("https://a/1", "新款")]

    monkeypatch.setattr(channels, "recall", slow)
    client.post("/api/follows", json={"subject": "AP 新款", "source": "web"})
    client.post("/api/follows", json={"subject": "Baby 新款", "source": "web"})

    body = client.post("/api/follows/check").json()

    statuses = [item["status"] for item in body["results"]]
    assert statuses == ["timeout", "skipped"]
    assert body["partial"] is True
    assert body["events"] == []


def test_delete_follow_removes_its_events(client, monkeypatch):
    monkeypatch.setattr(channels, "recall", lambda *_a, **_k: [_evidence("https://a/1", "旧款")])
    follow = client.post(
        "/api/follows", json={"subject": "Baby 新款", "source": "web"}
    ).json()["follows"][0]
    client.post("/api/follows/check")
    monkeypatch.setattr(channels, "recall", lambda *_a, **_k: [_evidence("https://a/2", "新款")])
    client.post("/api/follows/check")
    assert client.get("/api/follows").json()["unread"] == 1

    assert client.delete(f"/api/follows/{follow['id']}").json()["events"] == []


def test_diff_summary_reports_change_and_removal():
    old = [
        {"key": "u1", "title": "A", "text": "1"},
        {"key": "u2", "title": "B", "text": "2"},
    ]
    new = [{"key": "u1", "title": "A", "text": "9"}]

    summary = follow_watch._diff_summary(old, new)

    assert "内容有变化 1 条" in summary
    assert "减少 1 条" in summary
    assert "新增" not in summary


@pytest.mark.parametrize("raw", ["", "不是 JSON", '{"items": "坏"}'])
def test_bad_last_state_is_treated_as_no_baseline(raw):
    assert follow_watch._load_state(raw) is None
