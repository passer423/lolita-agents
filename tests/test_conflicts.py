"""证据与答案冲突诊断的用例。

这里钉的是「规则在已知样本上的行为」，不是通用语义理解：真冲突必须报，
铺垫句、成因解释句、不同颜色主体、不相关未引用证据都不能报。唯一有实测
真例的是 real-01，其他用例是从它的近邻反例里抽出来的护栏。
"""

from __future__ import annotations

from app.agent import channels, conflicts, graph, trace
from app.agent.channels import RecallContext
from app.config import settings


def item(n, channel, ref, text, title=""):
    return {
        "n": n,
        "channel": channel,
        "source_kind": channels.CHANNEL_SOURCE_KIND[channel],
        "ref": ref,
        "title": title or ref,
        "text": text,
        "url": "",
    }


# --------------------------------------------------------------------- 真冲突


def test_real01_uncited_opposite_evidence_is_reported():
    """答案说原版会掉色，未引用的证据说新色绿色不掉色。"""
    evidence = [
        item(1, "xhs", "new-green", "林檎之森的蓝裙子掉色，新色绿色应该是不掉色的，颜色很清爽。"),
        item(2, "real", "official", "原版蓝色绿色搭配的红色面料下水会有浮色掉色，不推荐购买原版。"),
    ]
    block = conflicts.diagnose(
        "林檎之森 OP 再贩的蓝色绿色会掉色吗",
        "会掉色 [2]。官方提醒过，原版蓝色和绿色搭配的红色面料会有浮色掉色 [2]。",
        evidence,
    )

    assert block["conflict_count"] == 1
    conflict = block["conflicts"][0]
    assert conflict["evidence_n"] == 1
    assert conflict["cited"] is False
    assert conflict["answer_stance"] == "positive"
    assert conflict["evidence_stance"] == "negative"
    assert conflict["shared_subjects"] == ["绿色"]
    assert "原版" in conflict["scope_note"] and "新色" in conflict["scope_note"]
    assert "未被引用" in conflict["basis"]
    # 弱信号里也要能看到这条，且标明与答案相反。
    weak = {entry["evidence_n"]: entry for entry in block["uncited_claims"]}
    assert weak[1]["opposes_answer"] is True


def test_cited_opposite_evidence_is_reported():
    evidence = [item(1, "real", "dress-note", "这条裙子实测不掉色。")]
    block = conflicts.diagnose("这条裙子会掉色吗", "会掉色 [1]。", evidence)

    assert block["conflict_count"] == 1
    assert block["conflicts"][0]["cited"] is True
    assert "被答案引用" in block["conflicts"][0]["basis"]


# --------------------------------------------------------------------- 反例护栏


def test_fade_resolved_and_cause_sentences_do_not_conflict():
    """real-02：答案和证据都在说「浮色洗两次掉完」，不能因为前文提到浮色就报冲突。"""
    evidence = [
        item(
            1,
            "real",
            "shop-qc",
            "面料表层会有一层浮色，是印花机刚开机温度偏低造成的。"
            "普通洗涤剂手洗或机洗两次，裙子表面浮色基本掉完，再拿面纸擦基本不掉色。",
        )
    ]
    block = conflicts.diagnose(
        "珀茵特庄园的裙子有浮色怎么处理",
        "表面那层浮色用普通洗涤剂洗两次基本就掉完了，之后拿纸巾擦也不太掉色 [1]。"
        "那位网友还说，浮色是印花机温度偏低造成的 [1]。",
        evidence,
    )

    assert block["conflict_count"] == 0


def test_different_color_subjects_do_not_conflict():
    evidence = [item(1, "real", "light", "浅色款怎么洗都不掉色。")]
    block = conflicts.diagnose("深色款会掉色吗", "深色款会掉色 [1]。", evidence)

    assert block["conflict_count"] == 0


def test_question_clause_is_not_a_verdict():
    evidence = [item(1, "real", "ask", "有妹子问会不会掉色，还没人回。")]
    block = conflicts.diagnose("蓝色会掉色吗", "会掉色 [1]。", evidence)

    assert block["conflict_count"] == 0


def test_uncited_irrelevant_evidence_is_skipped():
    evidence = [
        item(1, "own", "own-fade", "蓝色裙子会掉色。"),
        item(2, "real", "unrelated", "另一条完全无关的裙子洗了掉色。"),
    ]
    block = conflicts.diagnose("蓝色裙子会掉色吗", "会掉色 [1]。", evidence)

    assert block["conflict_count"] == 0
    assert block["uncited_claims"] == []


def test_uncited_relevant_same_stance_is_only_a_weak_signal():
    """real-01 的 #3：高相关、未引用，但判断方向与答案一致。"""
    evidence = [
        item(1, "real", "official", "原版蓝色绿色会掉色。"),
        item(2, "xhs", "fourth", "林檎之森四子虽然会掉色，但是很上镜。"),
    ]
    block = conflicts.diagnose(
        "林檎之森蓝色绿色会掉色吗",
        "原版蓝色和绿色会掉色 [1]。",
        evidence,
    )

    assert block["conflict_count"] == 0
    assert [entry["evidence_n"] for entry in block["uncited_claims"]] == [2]
    assert block["uncited_claims"][0]["opposes_answer"] is False


def test_empty_answer_or_evidence_is_safe():
    evidence = [item(1, "own", "own", "会掉色。")]
    assert conflicts.diagnose("q", "", evidence)["conflict_count"] == 0
    assert conflicts.diagnose("q", "会掉色 [1]。", [])["conflict_count"] == 0


# --------------------------------------------------------------------- 接线


class _Planner:
    usage = {"calls": 0}

    def extract_query(self, question, history=None):
        return question

    def extract_dress(self, question, history=None):
        return {"brand": "", "region": "", "series": "", "series_cn": "", "confidence": "low"}


class _Writer:
    usage = {"calls": 1}

    def write(self, question, evidence, history=None, **_kwargs):
        return "原版蓝色和绿色会掉色 [1]。"


def test_graph_payload_and_trace_carry_conflict_diagnostics(monkeypatch):
    def recall(channel, question, ctx, *, query=""):
        if channel == "own":
            return [item(0, "own", "own-fade", "原版蓝色和绿色会掉色。")]
        if channel == "real":
            return [item(0, "real", "real-conflict", "林檎之森新色绿色应该是不掉色的。")]
        return []

    records: list[dict] = []
    monkeypatch.setattr(channels, "recall", recall)
    monkeypatch.setattr(trace, "append", lambda payload, *_a, **_k: records.append(payload))

    result = graph.run(
        "蓝色和绿色会不会掉色",
        ctx=RecallContext(settings=settings),
        planner=_Planner(),
        writer=_Writer(),
    )

    block = result["conflict_diagnostics"]
    assert block["conflict_count"] == 1
    assert block["conflicts"][0]["ref"] == "real-conflict"
    assert block["conflicts"][0]["cited"] is False
    assert records[0]["conflict_diagnostics"] == block
