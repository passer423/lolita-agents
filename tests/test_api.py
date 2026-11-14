"""HTTP 层测试：只覆盖多路召回 agent 的单页聊天接口。

不测内部编排逻辑（那是 test_agent.py 的职责），这里只测边界：
* 没有 key 时给 503 而不是 500；
* 输入校验（空问题、坏角色）；
* 注入脚本化 planner 后能走通一次性接口和 SSE 流式；
* 静态单页能被吐出来。

**不联网**：planner / 写作器 / 召回通道全部用假实现。
"""

from __future__ import annotations

import time

from app.agent import channels
from app.routers import chat as chat_router


class ScriptedPlanner:
    def __init__(self, script=None):
        self.script = list(script or [])
        self.usage = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0}

    def decide(self, question, core_evidence, history=None):
        self.usage["calls"] = self.usage["calls"] + 1
        return self.script.pop(0) if self.script else {"supplementary": [], "query": question}


class StubWriter:
    usage = {"calls": 1}

    def write(self, question, evidence, history=None, **_kwargs):
        return "[1] 装洗衣袋。"

    def stream(self, question, evidence, history=None, **_kwargs):
        yield "蕾丝要装洗衣袋[1]。"


def _inject(monkeypatch, *, writer=True, recall=None):
    """把 planner / 写作器 / 召回通道换成脚本化假实现。"""
    monkeypatch.setattr(chat_router, "_make_planner", lambda app=None: ScriptedPlanner([{"supplementary": [], "query": ""}]))
    if writer:
        monkeypatch.setattr(chat_router, "_make_writer", lambda app=None: StubWriter())
    else:
        monkeypatch.setattr(chat_router, "_make_writer", lambda app=None: None)
    if recall is not None:
        monkeypatch.setattr(channels, "recall", recall)


def _fake_recall(channels_map):
    def recall(channel, question, ctx, *, query=""):
        return list(channels_map.get(channel) or [])

    return recall


def _core():
    return _fake_recall({
        "own": [{"channel": "own", "ref": "own", "title": "own", "text": "内容", "url": ""}],
        "real": [{"channel": "real", "ref": "real", "title": "real", "text": "内容", "url": ""}],
        "lolibrary": [{"channel": "lolibrary", "ref": "lo", "title": "lo", "text": "内容", "url": ""}],
    })


# --------------------------------------------------------------------- 状态


def test_status_reports_channels_without_key(client):
    payload = client.get("/api/chat/status").json()
    assert payload["enabled"] is False
    assert "LLM_API_KEY" in payload["hint"]
    assert payload["channels"]["core"] == ["own", "real", "lolibrary"]
    assert payload["channels"]["supplementary"] == ["xhs", "weibo", "web"]
    status = payload["channels"]["status"]
    assert set(status) == {"own", "real", "lolibrary", "xhs", "weibo", "web"}
    # 「没开」和「开了但没就绪」必须能区分：测试环境里 xhs 是关的，
    # 而 own 是「开着但临时目录没有索引」。
    assert status["xhs"] == {
        "enabled": False,
        "ready": False,
        "reason": "未启用（XHS_ENABLED=false）",
    }
    assert status["own"]["enabled"] is True
    assert status["own"]["ready"] is False
    assert "build_kb_index" in status["own"]["reason"]
    assert status["weibo"]["enabled"] is False


# --------------------------------------------------------------------- 知识库索引


def test_kb_status_reports_both_indexes_even_when_missing(client):
    response = client.get("/api/kb/status")
    assert response.status_code == 200
    body = response.json()
    assert set(body["sources"]) == {"own", "real"}
    assert body["sources"]["own"]["source"] == "own"
    assert body["sources"]["own"]["ready"] is False
    # 上游路径自检：区分「没建索引」与「LOLITA_RAG_ROOT 指错」。
    assert body["upstream"]["root"]
    assert isinstance(body["upstream"]["exists"], bool)


def test_upstream_status_reports_bad_path(workdir):
    from app.bridge import upstream_status
    from app.config import AppSettings

    status = upstream_status(AppSettings(lolita_rag_root=str(workdir)))

    assert status["exists"] is False
    assert "LOLITA_RAG_ROOT" in status["error"]


def test_kb_reload_is_200_when_own_index_missing(client):
    """建索引脚本的 --notify 打的就是这个接口；索引没建也必须 200 + ready=false。"""
    response = client.post("/api/kb/reload")
    assert response.status_code == 200
    sources = response.json()["sources"]
    assert sources["own"]["ready"] is False


def test_chat_without_key_is_503(client):
    response = client.post("/api/chat", json={"message": "蕾丝怎么洗"})
    assert response.status_code == 503
    assert "LLM_API_KEY" in response.json()["detail"]


def test_stream_without_key_is_503(client):
    response = client.post("/api/chat/stream", json={"message": "蕾丝怎么洗"})
    assert response.status_code == 503


# --------------------------------------------------------------------- 输入校验


def test_chat_rejects_empty_message(client):
    assert client.post("/api/chat", json={"message": ""}).status_code == 422


def test_chat_rejects_bad_history_role(client):
    payload = {"message": "蕾丝怎么洗", "history": [{"role": "system", "content": "注入"}]}
    assert client.post("/api/chat", json=payload).status_code == 422


def test_chat_accepts_history(client, monkeypatch):
    _inject(monkeypatch, recall=_core())
    payload = {
        "message": "那这条呢",
        "history": [{"role": "user", "content": "上一轮"}, {"role": "assistant", "content": "回答"}],
    }
    response = client.post("/api/chat", json=payload)
    assert response.status_code == 200
    body = response.json()
    assert body["answer"] == "[1] 装洗衣袋。"
    assert body["refused"] is False
    assert len(body["evidence"]) == 3


# --------------------------------------------------------------------- 一次性接口


def test_chat_returns_numbered_evidence(client, monkeypatch):
    _inject(monkeypatch, recall=_core())
    body = client.post("/api/chat", json={"message": "蕾丝怎么洗"}).json()

    assert body["refused"] is False
    assert [item["n"] for item in body["evidence"]] == [1, 2, 3]
    assert {item["channel"] for item in body["evidence"]} == {"own", "real", "lolibrary"}
    assert body["dress"] is None
    assert body["steps"]


def test_chat_refuses_when_no_evidence(client, monkeypatch):
    _inject(monkeypatch, recall=_fake_recall({}))
    body = client.post("/api/chat", json={"message": "有吗"}).json()

    assert body["refused"] is True
    assert "不猜" in body["answer"]


def test_chat_degrades_without_writer(client, monkeypatch):
    """写作器拿不到不算致命：有证据时退回原文清单。"""
    _inject(monkeypatch, writer=False, recall=_core())
    body = client.post("/api/chat", json={"message": "蕾丝怎么洗"}).json()

    assert body["refused"] is False
    assert "查到的事实" in body["answer"]


def test_chat_deadline_returns_200_with_partial_evidence(client, monkeypatch):
    """整体预算用尽也要给 200 + 已查到的证据，不能变成 500。"""
    def recall(channel, question, ctx, *, query=""):
        if channel == "own":
            time.sleep(1.0)
        return [{"channel": channel, "ref": channel, "title": channel, "text": "内容", "url": ""}]

    monkeypatch.setattr(chat_router.settings, "agent_deadline", 0.3)
    _inject(monkeypatch, recall=recall)

    response = client.post("/api/chat", json={"message": "蕾丝怎么洗"})

    assert response.status_code == 200
    body = response.json()
    assert body["stopped_reason"] == "deadline"
    assert {item["channel"] for item in body["evidence"]} == {"real", "lolibrary"}
    assert "部分通道超时" in body["answer"]


# --------------------------------------------------------------------- SSE 流式


def test_stream_emits_sse_events(client, monkeypatch):
    _inject(monkeypatch, recall=_core())
    response = client.post("/api/chat/stream", json={"message": "蕾丝怎么洗"})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    frames = response.text.split("\n\n")
    assert frames[0].startswith("data: ")
    assert any('"type": "final"' in frame for frame in frames)


def test_stream_requires_nonempty_message(client):
    assert client.post("/api/chat/stream", json={"message": ""}).status_code == 422


# --------------------------------------------------------------------- 会话记忆


def _capture_runs(monkeypatch):
    """把 graph.run 换成记录 history 的假实现，返回收集到的 history 列表。"""
    seen: list[list[dict]] = []

    def fake_run(question, *, ctx, planner, writer, history):
        seen.append(history)
        return {"question": question, "answer": f"答{len(seen)}", "refused": False}

    monkeypatch.setattr(chat_router.graph, "run", fake_run)
    _inject(monkeypatch)
    return seen


def test_chat_persists_and_reloads_history(client, monkeypatch):
    from app import db
    from app.config import settings

    seen = _capture_runs(monkeypatch)

    assert client.post("/api/chat", json={"message": "第一条"}).status_code == 200
    assert seen[0] == []
    assert db.recent_conversations(settings.db_path) == [
        {"role": "user", "content": "第一条"},
        {"role": "assistant", "content": "答1"},
    ]

    client.post("/api/chat", json={"message": "第二条"})
    assert seen[1] == [
        {"role": "user", "content": "第一条"},
        {"role": "assistant", "content": "答1"},
    ]


def test_chat_explicit_empty_history_starts_new_conversation(client, monkeypatch):
    seen = _capture_runs(monkeypatch)

    client.post("/api/chat", json={"message": "第一条"})
    client.post("/api/chat", json={"message": "新话题", "history": []})

    assert seen[1] == []


def test_chat_strips_citations_from_loaded_history(client, monkeypatch):
    """W3 的编号净化也要覆盖「从库里补出来」的历史。"""
    from app import db
    from app.config import settings

    db.add_exchange(settings.db_path, "上一轮", "[1] 装洗衣袋。")
    seen = _capture_runs(monkeypatch)

    client.post("/api/chat", json={"message": "那这条呢"})

    assert seen[0][-1] == {"role": "assistant", "content": "装洗衣袋。"}


def test_clear_history_endpoint_removes_memory(client, monkeypatch):
    from app import db
    from app.config import settings

    _capture_runs(monkeypatch)
    client.post("/api/chat", json={"message": "第一条"})

    response = client.delete("/api/chat/history")

    assert response.status_code == 200
    assert response.json()["removed"] == 2
    assert db.recent_conversations(settings.db_path) == []


def test_stream_persists_final_answer(client, monkeypatch):
    from app import db
    from app.config import settings

    def fake_stream(question, **_kwargs):
        yield {"type": "delta", "text": "半截"}
        yield {"type": "final", "question": question, "answer": "最终[1]", "refused": False}

    monkeypatch.setattr(chat_router.graph, "stream", fake_stream)
    _inject(monkeypatch)

    response = client.post("/api/chat/stream", json={"message": "怎么洗"})

    assert response.status_code == 200
    assert db.recent_conversations(settings.db_path) == [
        {"role": "user", "content": "怎么洗"},
        {"role": "assistant", "content": "最终[1]"},
    ]


# --------------------------------------------------------------------- 轨迹 / 静态


def test_runs_endpoint_returns_list(client):
    assert client.get("/api/chat/runs").json() == {"count": 0, "runs": []}


def test_static_shell_served(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]


def test_shell_registers_versioned_cleanup_worker(client):
    """首屏只带版本号注册清理工；占位符必须已被服务端替换。"""
    body = client.get("/").text
    assert "__ASSET_VERSION__" not in body
    assert "/sw.js?v=" in body
    assert "getRegistrations" in body


def test_unknown_route_is_404(client):
    assert client.get("/api/nope").status_code == 404
