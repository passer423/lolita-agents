"""real 通道的索引路径守门测试 + bridge 纯逻辑测试。

2026-11-16 的 self-hosted runner 事故：workflow 用 ``DATA_DIR`` 指向本仓 data，
而上游 ``Settings`` 是 pydantic-settings，会读这个环境变量，于是 real 通道被
拽到没有索引的目录，全天门禁红在「索引还没建」。第一个用例把那份契约钉住：
real 永远指上游仓库自己的 ``data/``，own 永远指本应用的 kb_index_dir。

其余用例覆盖：多意图拆分（保护词 / 比较不拆 / 谓语补齐 / 去重）、
检索合并去重与上限、上游缺失时的状态上报（不抛错）。
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

from app import bridge
from app.config import AppSettings


def _stub_upstream(root: Path) -> Path:
    """最小上游仓库：只提供 src/config.py 里的 Settings（故意不导出 PROJECT_ROOT）。"""
    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / "src" / "__init__.py").write_text("", encoding="utf-8")
    (root / "src" / "config.py").write_text(
        "class Settings:\n"
        "    def __init__(self, **kwargs):\n"
        "        self.__dict__.update(kwargs)\n",
        encoding="utf-8",
    )
    return root


def test_real_source_ignores_data_dir_env(tmp_path, monkeypatch):
    upstream = _stub_upstream(tmp_path / "lolita-rag")
    app = AppSettings(data_dir=tmp_path / "own-data", lolita_rag_root=upstream)

    # conftest 全局设了 DATA_DIR；这里再钉一个“污染值”，复现 runner 上的条件。
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "poison"))
    monkeypatch.setenv("LOLITA_RAG_ROOT", str(upstream))
    monkeypatch.setattr(sys, "path", list(sys.path))

    # src 不是安装包，前面的用例可能已经把别的 stub 缓存进 sys.modules。
    saved = {name: sys.modules.pop(name) for name in ("src", "src.config") if name in sys.modules}
    try:
        real = bridge.upstream_settings(app, source="real")
        assert Path(real.data_dir).resolve() == (upstream / "data").resolve()

        own = bridge.upstream_settings(app, source="own")
        assert Path(own.data_dir).resolve() == app.kb_index_dir.resolve()
    finally:
        for name in ("src", "src.config"):
            sys.modules.pop(name, None)
        sys.modules.update(saved)


# --------------------------------------------------------------------- 多意图拆分


def test_split_multi_intent_basic():
    """「烫金和蕾丝怎么洗」拆两半，前一半补上共享谓语「怎么洗」。"""
    assert bridge._split_multi_intent("烫金和蕾丝怎么洗") == ["烫金怎么洗", "蕾丝怎么洗"]


def test_split_multi_intent_comparison_not_split():
    """比较类问句不拆：拆开反而把「专门写对比的条目」打散。"""
    assert bridge._split_multi_intent("JSK 和 OP 有什么区别") == []
    assert bridge._split_multi_intent("雪纺还是棉麻哪个适合夏天") == []


def test_split_multi_intent_protects_compounds():
    """「和服」里的「和」不是连接词；连接词是「与」。"""
    assert bridge._split_multi_intent("和服与痛包怎么洗") == ["和服怎么洗", "痛包怎么洗"]


def test_split_multi_intent_no_predicate_kept_as_is():
    """分句各自带谓语时不补（避免「可以机洗可以机洗吗」）。"""
    assert bridge._split_multi_intent("烫金可以机洗和蕾丝只能手洗") == ["烫金可以机洗", "蕾丝只能手洗"]


def test_split_multi_intent_degenerate():
    """空串、单意图、过短分句都返回空列表 = 按原问句检索。"""
    assert bridge._split_multi_intent("") == []
    assert bridge._split_multi_intent("烫金怎么洗") == []
    assert bridge._split_multi_intent("烫金和洗") == []  # 「洗」只有一个字，太短


def test_split_multi_intent_latin_spacing():
    """拉丁词结尾补空格：「JSK是什么」不好读；后半个分句自带谓语则不再动。"""
    parts = bridge._split_multi_intent("JSK和OP分别是什么")
    assert parts == ["JSK 是什么", "OP是什么"]


def test_split_multi_intent_dedup():
    assert bridge._split_multi_intent("烫金和烫金怎么洗") == []


# --------------------------------------------------------------------- 检索合并


class _Hit:
    def __init__(self, chunk_id: str):
        self.chunk = SimpleNamespace(chunk_id=chunk_id)


class _Retriever:
    """按 query 返回固定 chunk_id 序列的桩。"""

    def __init__(self, mapping: dict[str, list[str]]):
        self.mapping = mapping
        self.calls: list[str] = []

    def search(self, query: str, top_k: int = 1):
        self.calls.append(query)
        return [_Hit(cid) for cid in self.mapping.get(query, [])[:top_k]]


def test_search_hits_no_intents_single_search():
    retriever = _Retriever({"烫金怎么洗": ["a", "b"]})
    bundle = SimpleNamespace(retriever=retriever)
    hits = bridge._search_hits("烫金怎么洗", bundle, top_k=2)
    assert [h.chunk.chunk_id for h in hits] == ["a", "b"]
    assert retriever.calls == ["烫金怎么洗"]


def test_search_hits_multi_intent_merges_and_dedups():
    retriever = _Retriever({
        "烫金和蕾丝怎么洗": ["both"],
        "烫金怎么洗": ["both", "tj"],
        "蕾丝怎么洗": ["ll"],
    })
    bundle = SimpleNamespace(retriever=retriever)
    hits = bridge._search_hits("烫金和蕾丝怎么洗", bundle, top_k=1)
    # 原问句 1 条 + 每个子问题各取 1 条；子问题的 top1 恰好又是「both」，去重后两条
    assert [h.chunk.chunk_id for h in hits] == ["both", "ll"]


def test_search_hits_caps_at_max_merged():
    retriever = _Retriever({
        "a和b以及c怎么洗": ["full"],
        "a怎么洗": ["a"],
        "b怎么洗": ["b"],
        "c怎么洗": ["c"],
    })
    bundle = SimpleNamespace(retriever=retriever)
    hits = bridge._search_hits("a和b以及c怎么洗", bundle, top_k=1)
    assert len(hits) <= bridge._MAX_MERGED_HITS


# --------------------------------------------------------------------- 状态上报


def test_upstream_status_missing_root_reports_not_exists(tmp_path):
    app = AppSettings(data_dir=tmp_path / "data", lolita_rag_root=tmp_path / "nowhere")
    status = bridge.upstream_status(app)
    assert status["exists"] is False
    assert "LOLITA_RAG_ROOT" in status["error"]


def test_install_upstream_missing_raises(tmp_path):
    import pytest

    with pytest.raises(bridge.UpstreamUnavailable, match="LOLITA_RAG_ROOT"):
        bridge._install_upstream(tmp_path / "nowhere")


def test_index_status_swallows_upstream_missing(tmp_path):
    """索引状态接口不抛错：前端要能显示「请先建索引」。"""
    app = AppSettings(data_dir=tmp_path / "data", lolita_rag_root=tmp_path / "nowhere")
    info = bridge.index_status(app, source="own")
    assert info["ready"] is False
    assert info["error"]


def test_reload_bundle_swallows_failure(tmp_path):
    """建索引脚本的收尾通知不能因为索引没建好而 500。"""
    app = AppSettings(data_dir=tmp_path / "data", lolita_rag_root=tmp_path / "nowhere")
    result = bridge.reload_bundle(app)
    assert result["sources"]["own"]["ready"] is False
    assert result["sources"]["real"]["ready"] is False
