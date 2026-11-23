"""金标集结构校验：让 ``load_golden`` 的 SystemExit 在 CI 里生效。

这些用例不联网、不读索引：只把各种坏金标写进临时文件，确认报错能定位到
「哪一行、哪条 id、哪一项」。所以断言都看具体信息，不只断言"抛了异常"。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from eval.run import is_out_of_scope, load_golden


def _path(tmp_path: Path) -> Path:
    return tmp_path / "golden.jsonl"


def _write(tmp_path: Path, lines: list[object]) -> Path:
    path = _path(tmp_path)
    payload = [
        json.dumps(line, ensure_ascii=False) if isinstance(line, dict) else str(line)
        for line in lines
    ]
    path.write_text("\n".join(payload) + "\n", encoding="utf-8")
    return path


def _valid(**overrides: object) -> dict:
    item = {
        "id": "own-01",
        "channel": "own",
        "query": "裙子怎么洗",
        "expected_refs": ["care#0"],
    }
    item.update(overrides)
    return item


def _error(path: Path, lines: list[object]) -> str:
    with pytest.raises(SystemExit) as excinfo:
        load_golden(_write(path, lines))
    message = str(excinfo.value)
    assert str(path) in message, f"错误信息没带文件路径：{message!r}"
    return message


def test_accepts_minimal_golden_and_normalizes_contract(tmp_path: Path) -> None:
    items = load_golden(_write(tmp_path, [_valid(evidence_must_any=["轻装"], evidence_must_not=[])]))

    assert len(items) == 1
    assert items[0]["id"] == "own-01"
    assert items[0]["evidence_must_any"] == [["轻装"]]
    assert items[0]["evidence_must_not"] == []


def test_rejects_duplicate_id_with_line_number(tmp_path: Path) -> None:
    message = _error(tmp_path, [_valid(), _valid(query="另一条重复 id")])

    assert f"{_path(tmp_path)}:2" in message
    assert "id 缺失或重复" in message
    assert "'own-01'" in message


def test_scope_marking_keeps_total_and_skips_cn_when_jp() -> None:
    """线上只做日牌：国牌价格题与整条关闭通道题要单列，不删题、不改题面。"""
    path = Path(__file__).resolve().parent.parent / "eval" / "golden.jsonl"
    items = load_golden(path)

    assert len(items) == 60
    skipped = [item for item in items if is_out_of_scope(item, "jp")]
    assert {item["id"] for item in skipped} == {
        "price-01", "price-02", "price-03", "price-04",
        "xhs-01", "xhs-02", "xhs-03",
        "weibo-01", "weibo-02", "weibo-03",
    }
    assert len(skipped) == 10
    assert all(item.get("query") for item in skipped)
    # all 模式恢复国牌题；整条通道题仍然跳过。
    cn_only = [item for item in items if item.get("scope") == "cn"]
    assert all(not is_out_of_scope(item, "all") for item in cn_only)


def test_rejects_missing_id(tmp_path: Path) -> None:
    message = _error(tmp_path, [_valid(id="")])

    assert f"{_path(tmp_path)}:1" in message
    assert "id 缺失或重复" in message


def test_rejects_empty_refs(tmp_path: Path) -> None:
    message = _error(tmp_path, [_valid(id="own-99", expected_refs=[], support_terms=[])])

    assert f"{_path(tmp_path)}:1" in message
    assert "既没有 expected_refs 也没有 support_terms" in message


def test_rejects_unknown_channel(tmp_path: Path) -> None:
    message = _error(tmp_path, [_valid(id="bad-channel", channel="xiaohongshu")])

    assert f"{_path(tmp_path)}:1" in message
    assert "未知通道" in message
    assert "'xiaohongshu'" in message


def test_rejects_empty_query(tmp_path: Path) -> None:
    message = _error(tmp_path, [_valid(query="   ")])

    assert f"{_path(tmp_path)}:1" in message
    assert "query 为空" in message


def test_rejects_invalid_json_with_line_number(tmp_path: Path) -> None:
    message = _error(tmp_path, ["{不是 JSON"])

    assert f"{_path(tmp_path)}:1" in message
    assert "不是合法 JSON" in message


def test_reports_actual_line_when_blank_lines_precede(tmp_path: Path) -> None:
    message = _error(tmp_path, ["", "", _valid(channel="weibo2")])

    assert f"{_path(tmp_path)}:3" in message
    assert "未知通道" in message


def test_rejects_must_any_not_a_list(tmp_path: Path) -> None:
    message = _error(tmp_path, [_valid(id="own-bad-any", evidence_must_any="轻装")])

    assert f"{_path(tmp_path)}:1" in message
    assert "evidence_must_any 必须是列表" in message


def test_rejects_must_any_group_not_a_list(tmp_path: Path) -> None:
    message = _error(tmp_path, [_valid(id="own-bad-group", evidence_must_any=[["轻装"], "日常"])])

    assert f"{_path(tmp_path)}:1" in message
    assert "evidence_must_any 的每组必须是列表" in message


def test_rejects_must_any_empty_group(tmp_path: Path) -> None:
    message = _error(tmp_path, [_valid(id="own-empty-group", evidence_must_any=[["  "]])])

    assert f"{_path(tmp_path)}:1" in message
    assert "evidence_must_any 有空组" in message


def test_rejects_must_not_not_a_string_list(tmp_path: Path) -> None:
    message = _error(tmp_path, [_valid(id="own-bad-not", evidence_must_not=["堆周边", ""])])

    assert f"{_path(tmp_path)}:1" in message
    assert "evidence_must_not 必须是非空字符串列表" in message


def test_rejects_empty_golden_file(tmp_path: Path) -> None:
    message = _error(tmp_path, [])

    assert f"{_path(tmp_path)} 里没有金标条目" in message
