"""对话轨迹的写入与轮转。

轨迹是 JSONL 追加文件，不轮转就会随使用无限增长（每次 run 一条，含完整问答）。
轮转只保留最近 N 个文件，``tail`` 始终读当前文件。
"""

from __future__ import annotations

from app.agent import trace
from app.config import AppSettings


def _app(tmp_path, **kwargs) -> AppSettings:
    return AppSettings(data_dir=tmp_path, **kwargs)


def test_append_rotates_and_keeps_bounded_files(tmp_path):
    app = _app(tmp_path, trace_max_bytes=200, trace_keep_files=2)

    for index in range(30):
        trace.append({"i": index, "text": "x" * 60}, app)

    path = trace.trace_path(app)
    assert path.exists()
    assert path.with_name(f"{path.name}.1").exists()
    assert path.with_name(f"{path.name}.2").exists()
    # keep_files=2：不产生第 3 个归档
    assert not path.with_name(f"{path.name}.3").exists()


def test_tail_reads_latest_records_after_rotation(tmp_path):
    app = _app(tmp_path, trace_max_bytes=10_000, trace_keep_files=2)

    trace.append({"i": 1}, app)
    trace.append({"i": 2}, app)

    assert [record["i"] for record in trace.tail(1, app)] == [2]


def test_rotation_disabled_keeps_single_file(tmp_path):
    app = _app(tmp_path, trace_max_bytes=0, trace_keep_files=2)

    for index in range(5):
        trace.append({"i": index, "text": "x" * 60}, app)

    path = trace.trace_path(app)
    assert path.exists()
    assert not path.with_name(f"{path.name}.1").exists()
