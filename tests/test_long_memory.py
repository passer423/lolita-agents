"""长期记忆：显式偏好/尺码/品牌的规则抽取、去重、隔离、过期与注入。"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

from app import db
from app.agent import graph, long_memory
from app.config import settings


@pytest.fixture()
def memory_settings(workdir, monkeypatch):
    """内容目录指到临时目录：品牌表只放一个测试品牌，不依赖工作区数据。"""
    content = workdir / "content"
    (content / "dress").mkdir(parents=True)
    (content / "brands.json").write_text(
        json.dumps(
            {
                "brands": [
                    {
                        "id": "baby",
                        "name": "Baby, the Stars Shine Bright",
                        "region": "JP",
                        "aliases": ["Baby", "BTSSB"],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(settings, "content_dir", content)
    return settings


def _db(tmp_path):
    path = tmp_path / "app.db"
    db.init_db(path)
    return path


def _account(path, username="owner"):
    return db.create_account(path, username, "scrypt$1$1$1$x$y")


# --------------------------------------------------------------------- 抽取


def test_extracts_size_color_and_brand(memory_settings):
    facts = long_memory.extract("我穿M码，我喜欢粉色，我常买 Baby 的裙子", memory_settings)
    pairs = {(fact["kind"], fact["value"]) for fact in facts}
    assert ("size", "M") in pairs
    assert ("color", "粉色") in pairs
    # 颜色已经记成「喜欢的颜色」，不再重复写一条泛化偏好。
    assert ("preference", "我喜欢粉色") not in pairs
    assert ("brand", "Baby, the Stars Shine Bright") in pairs


def test_extracts_body_measurements_with_explicit_cm(memory_settings):
    facts = long_memory.extract("我胸围85cm，腰围70cm，臀围90cm", memory_settings)
    assert {(fact["kind"], fact["value"]) for fact in facts} == {
        ("bust", "85cm"),
        ("waist", "70cm"),
        ("hips", "90cm"),
    }

    triple = long_memory.extract("我三围 85/70/90cm", memory_settings)
    assert {(fact["kind"], fact["value"]) for fact in triple} == {
        ("bust", "85cm"),
        ("waist", "70cm"),
        ("hips", "90cm"),
    }


def test_extracts_height_weight_and_unitless_body(memory_settings):
    # 单位可省略：纯身体数据句按 cm 记。
    facts = long_memory.extract("身高160，腰围90", memory_settings)
    assert {(fact["kind"], fact["value"]) for fact in facts} == {
        ("height", "160cm"),
        ("waist", "90cm"),
    }

    facts = long_memory.extract("我身高160cm，体重55kg", memory_settings)
    assert {(fact["kind"], fact["value"]) for fact in facts} == {
        ("height", "160cm"),
        ("weight", "55kg"),
    }

    # 斤折半。
    facts = long_memory.extract("我体重110斤", memory_settings)
    assert [(fact["kind"], fact["value"]) for fact in facts] == [("weight", "55kg")]


def test_dress_dimensions_without_self_subject_are_not_stored(memory_settings):
    # 描述裙子尺寸、第三人称、疑问句都不算本人数据。
    for text in ("我胸围90的裙子", "她身高160cm", "这条腰围90cm吗", "裙长85cm"):
        assert long_memory.extract(text, memory_settings) == [], text


def test_body_facts_survive_a_trailing_fit_question(memory_settings):
    # 「报数据 + 问能不能穿」是主用法：陈述分句照记，只排除疑问分句。
    facts = long_memory.extract("我胸围95cm，腰围74cm，臀围98cm，这件穿得下吗？", memory_settings)
    assert {(fact["kind"], fact["value"]) for fact in facts} == {
        ("bust", "95cm"),
        ("waist", "74cm"),
        ("hips", "98cm"),
    }

    facts = long_memory.extract("我身高160cm，体重55kg，这件穿得下吗？", memory_settings)
    assert {(fact["kind"], fact["value"]) for fact in facts} == {
        ("height", "160cm"),
        ("weight", "55kg"),
    }


def test_is_body_data_only_matches_pure_measurements():
    assert long_memory.is_body_data("我身高160cm，体重150斤")
    assert long_memory.is_body_data("身高160，腰围90")
    assert long_memory.is_body_data("本人胸围85cm")
    # 夹了裙名、问法或别的实词就不是纯数据：不能用来拦裙子识别。
    assert not long_memory.is_body_data("我身高160cm，体重150斤，这条穿得下吗")
    assert not long_memory.is_body_data("Krakatuk Military OP 我身高160")
    assert not long_memory.is_body_data("")


def test_negative_or_question_colors_are_not_stored(memory_settings):
    assert long_memory.extract("我不喜欢粉色", memory_settings) == []
    assert long_memory.extract("粉色适合我吗", memory_settings) == []


def test_questions_and_third_person_are_not_stored(memory_settings):
    for text in (
        "AP 最近有什么上新",
        "M码有现货吗",
        "我喜欢这款吗？",
        "她喜欢粉色",
        "Baby 的裙子怎么洗",
    ):
        assert long_memory.extract(text, memory_settings) == [], text


def test_size_without_context_is_ignored(memory_settings):
    # 只有数字/字母、没有「尺码 / 穿 / 码」语境，不当成尺码。
    assert long_memory.extract("我买了两条", memory_settings) == []
    assert long_memory.extract("我穿170", memory_settings) == []


def test_same_message_dedupes(memory_settings):
    facts = long_memory.extract("我喜欢粉色，我喜欢粉色", memory_settings)
    assert [fact["value"] for fact in facts] == ["粉色"]


# --------------------------------------------------------------------- 存储


def test_remember_dedupes_and_counts_hits(tmp_path, memory_settings):
    path = _db(tmp_path)
    account = _account(path)

    long_memory.remember(path, account["id"], "我穿M码", memory_settings)
    long_memory.remember(path, account["id"], "我穿 M 码", memory_settings)

    rows = db.list_memories(path, account["id"])
    assert [(row["kind"], row["value"], row["hits"]) for row in rows] == [
        ("size", "M", 2)
    ]


def test_memories_are_isolated_between_accounts(tmp_path, memory_settings):
    path = _db(tmp_path)
    alice = _account(path, "alice")
    bob = _account(path, "bob")
    long_memory.remember(path, alice["id"], "我喜欢粉色", memory_settings)

    rows = db.list_memories(path, alice["id"])
    assert [row["value"] for row in rows] == ["粉色"]
    assert db.list_memories(path, bob["id"]) == []
    assert not db.delete_memory(path, bob["id"], rows[0]["id"])
    assert db.delete_memory(path, alice["id"], rows[0]["id"])
    assert db.list_memories(path, alice["id"]) == []


def test_old_facts_are_hidden_and_pruned(tmp_path, memory_settings):
    path = _db(tmp_path)
    account = _account(path)
    long_memory.remember(path, account["id"], "我穿M码", memory_settings)

    stale = (datetime.now() - timedelta(days=db.MEMORY_MAX_AGE_DAYS + 1)).isoformat(
        timespec="seconds"
    )
    with db.connect(path) as conn:
        conn.execute("UPDATE long_memories SET updated_at = ?", (stale,))

    assert db.list_memories(path, account["id"]) == []
    assert db.prune_user_data(path)["memories"] == 1
    assert db.list_memories(path, account["id"], max_age_days=0) == []


def test_render_marks_source_kind(tmp_path):
    block = long_memory.render(
        [
            {"kind": "preference", "value": "我喜欢蕾丝"},
            {"kind": "brand", "value": "Angelic Pretty"},
            {"kind": "color", "value": "粉色"},
            {"kind": "size", "value": "M"},
            {"kind": "hips", "value": "90cm"},
            {"kind": "height", "value": "160cm"},
            {"kind": "weight", "value": "55kg"},
            {"kind": "bust", "value": "85cm"},
            {"kind": "waist", "value": "70cm"},
        ]
    )
    assert block is not None
    assert block["role"] == "user"
    assert block["content"].startswith(long_memory.MARKER)
    assert "胸围：85cm" in block["content"]
    assert "腰围：70cm" in block["content"]
    assert "臀围：90cm" in block["content"]
    assert "身高：160cm" in block["content"]
    assert "体重：55kg" in block["content"]
    assert "尺码：M" in block["content"]
    assert "喜欢的颜色：粉色" in block["content"]
    assert "常买品牌：Angelic Pretty" in block["content"]
    assert "偏好：我喜欢蕾丝" in block["content"]
    # 身材数据排在偏好前面：查裙子时先看到要拿来比的数字。
    content = block["content"]
    assert content.index("胸围") < content.index("喜欢的颜色") < content.index("偏好")
    assert long_memory.render([]) is None


def test_block_for_missing_account_or_db_is_none(tmp_path):
    assert long_memory.block_for(tmp_path / "none.db", None) is None
    assert long_memory.block_for(tmp_path / "missing", 1) is None


# --------------------------------------------------------------------- 注入


def test_long_memory_prepends_and_survives_truncation(data_dir, memory_settings):
    """长期记忆排在最近历史之前，不会像更早的轮次一样被截掉。"""
    from app.agent.channels import RecallContext

    db.init_db(settings.db_path)
    account = _account(settings.db_path)
    long_memory.remember(settings.db_path, account["id"], "我穿M码", memory_settings)

    ctx = RecallContext(settings=settings, account_id=account["id"])
    raw = [
        {"role": "user", "content": f"第{i}个问题"}
        for i in range(graph.HISTORY_FETCH_MESSAGES + 4)
    ]

    turns = graph._history_for_context(raw, ctx)

    assert turns[0]["role"] == "user"
    assert turns[0]["content"].startswith(long_memory.MARKER)
    assert "尺码：M" in turns[0]["content"]
    # 短期窗口仍然只保留最近 HISTORY_FETCH_MESSAGES 条。
    assert len(turns) == graph.HISTORY_FETCH_MESSAGES + 1


def test_run_records_explicit_facts_for_account(data_dir, memory_settings, monkeypatch):
    from app.agent.channels import RecallContext

    db.init_db(settings.db_path)
    account = _account(settings.db_path)
    ctx = RecallContext(settings=settings, account_id=account["id"])

    class FakeGraph:
        def invoke(self, state, config=None):
            return {}

    monkeypatch.setattr(graph, "build_graph", lambda *args, **kwargs: FakeGraph())
    monkeypatch.setattr(graph, "_finish", lambda *args, **kwargs: {})

    graph.run("我喜欢黑色", ctx=ctx, planner=object())

    facts = db.list_memories(settings.db_path, account["id"])
    assert ("color", "黑色") in {
        (fact["kind"], fact["value"]) for fact in facts
    }


def test_body_data_only_gets_a_confirmation_instead_of_recall(memory_settings):
    """纯身材数据不是裙子问题：直接确认已记住，不进召回。"""
    from app.agent.channels import RecallContext

    db.init_db(memory_settings.db_path)
    account = _account(memory_settings.db_path, "body-only-run")
    ctx = RecallContext(settings=memory_settings, account_id=account["id"])

    result = graph.run("我身高160cm，体重150斤", ctx=ctx, planner=object())

    assert result["answer"].startswith("【记住你的身材数据】")
    assert "身高 160cm" in result["answer"]
    assert "体重 75kg" in result["answer"]
    assert "仅供参考" in result["answer"]
    memory_steps = [step for step in result["steps"] if step["kind"] == "memory"]
    assert memory_steps and memory_steps[0]["summary"] == "身高 160cm、体重 75kg"
    stored = {
        (fact["kind"], fact["value"])
        for fact in db.list_memories(memory_settings.db_path, account["id"])
    }
    assert ("height", "160cm") in stored
    assert ("weight", "75kg") in stored


def test_body_data_only_streams_a_confirmation_and_final(memory_settings):
    from app.agent.channels import RecallContext

    db.init_db(memory_settings.db_path)
    account = _account(memory_settings.db_path, "body-only-stream")
    ctx = RecallContext(settings=memory_settings, account_id=account["id"])

    events = list(graph.stream("我身高160cm，体重150斤", ctx=ctx, planner=object()))

    steps = [event for event in events if event["type"] == "step"]
    assert steps and steps[0]["kind"] == "memory"
    deltas = [event for event in events if event["type"] == "delta"]
    assert deltas and deltas[0]["text"].startswith("【记住你的身材数据】")
    final = [event for event in events if event["type"] == "final"][-1]
    assert final["answer"] == deltas[0]["text"]
    assert final["steps"][0]["kind"] == "memory"


def test_memory_reply_handles_fit_question_without_a_dress(monkeypatch):
    facts = [
        {"kind": "height", "value": "160cm"},
        {"kind": "weight", "value": "75kg"},
    ]
    # 数据 + 问合身、但这句里没有裙名：确认数据并要裙名。
    monkeypatch.setattr(graph.dress, "resolve_dress", lambda *args, **kwargs: None)
    text, step = graph._memory_reply("我身高160cm，体重150斤，这条穿得下吗", facts, None)
    assert step is not None and step["status"] == "stored"
    assert "要看哪条裙子" in text

    # 点名了裙子就不拦：正常流程里合身工具会按档案算。
    monkeypatch.setattr(graph.dress, "resolve_dress", lambda *args, **kwargs: object())
    text, step = graph._memory_reply("我身高160cm，Krakatuk 穿得下吗", facts, None)
    assert text == "" and step is None

    # 只是顺带报了数据，问的是别的事：不拦。
    text, step = graph._memory_reply("我身高160cm，最近有什么上新", facts, None)
    assert text == "" and step is None

    # 有三围时确认语不说「仅供参考」：那是实测结论。
    text, step = graph._memory_reply(
        "我胸围85cm，腰围68cm，臀围90cm",
        [
            {"kind": "bust", "value": "85cm"},
            {"kind": "waist", "value": "68cm"},
            {"kind": "hips", "value": "90cm"},
        ],
        None,
    )
    assert step is not None and step["status"] == "stored"
    assert "按这个三围判断" in text
    assert "仅供参考" not in text
