"""real 通道的索引路径守门测试。

2026-11-16 的 self-hosted runner 事故：workflow 用 ``DATA_DIR`` 指向本仓 data，
而上游 ``Settings`` 是 pydantic-settings，会读这个环境变量，于是 real 通道被
拽到没有索引的目录，全天门禁红在「索引还没建」。这里把那份契约钉住：
real 永远指上游仓库自己的 ``data/``，own 永远指本应用的 kb_index_dir。
"""

from __future__ import annotations

import sys
from pathlib import Path

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
