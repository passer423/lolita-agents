"""精排 ONNX 后端：默认开启、懒加载/闲置卸载、共享单例与 bridge 接线。"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from app import bridge, rerank
from app.config import AppSettings
from app.rerank import LazyOnnxReranker, resolve_onnx_path, resolve_reranker_model, shared_onnx_reranker


class _FakeModel:
    """只实现 predict / close 的打分器桩。"""

    def __init__(self):
        self.closed = False
        self.calls: list[list[tuple[str, str]]] = []
        self.batch_sizes: list[int | None] = []

    def predict(self, pairs, batch_size=None):
        self.calls.append(list(pairs))
        self.batch_sizes.append(batch_size)
        return [float(index) for index in range(len(pairs))]

    def close(self):
        self.closed = True


def _write_model_files(root: Path) -> tuple[Path, Path]:
    model_dir = root / "data" / "models" / "bge-reranker-base"
    onnx_path = model_dir / "onnx" / "model.int8.onnx"
    onnx_path.parent.mkdir(parents=True, exist_ok=True)
    (model_dir / "tokenizer.json").write_text("{}", encoding="utf-8")
    onnx_path.write_bytes(b"fake-onnx")
    return model_dir, onnx_path


def _wait_until(predicate, timeout: float = 3.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


# --------------------------------------------------------------------- 配置与路径


def test_reranker_defaults_on():
    app = AppSettings(_env_file=None)
    assert app.reranker_enabled is True
    assert app.reranker_backend == "auto"
    assert app.reranker_batch_size == 4
    assert app.reranker_idle_unload_seconds > 0


def test_resolve_defaults_follow_lolita_root(tmp_path):
    app = AppSettings(_env_file=None, lolita_rag_root=tmp_path, reranker_model="", reranker_onnx_path=None)
    assert resolve_reranker_model(app) == tmp_path / "data" / "models" / "bge-reranker-base"
    assert resolve_onnx_path(app) == tmp_path / "data" / "models" / "bge-reranker-base" / "onnx" / "model.int8.onnx"


def test_explicit_paths_win(tmp_path):
    app = AppSettings(
        _env_file=None,
        lolita_rag_root=tmp_path,
        reranker_model=str(tmp_path / "custom"),
        reranker_onnx_path=tmp_path / "custom" / "int8.onnx",
    )
    assert resolve_reranker_model(app) == tmp_path / "custom"
    assert resolve_onnx_path(app) == tmp_path / "custom" / "int8.onnx"


# --------------------------------------------------------------------- 分批


def test_iter_batches_splits_and_keeps_order():
    assert [list(batch) for batch in rerank._iter_batches([1, 2, 3, 4, 5], 2)] == [[1, 2], [3, 4], [5]]
    assert [list(batch) for batch in rerank._iter_batches([1], 0)] == [[1]]


# --------------------------------------------------------------------- 懒加载代理


def test_lazy_loader_loads_once_and_forwards_batch_size():
    loads: list[int] = []
    model = _FakeModel()

    def loader():
        loads.append(1)
        return model

    proxy = LazyOnnxReranker("unused", "unused", idle_seconds=0, loader=loader)
    assert proxy.predict([("q", "d")], batch_size=4) == [0.0]
    assert proxy.predict([("q", "a"), ("q", "b")], batch_size=4) == [0.0, 1.0]
    assert len(loads) == 1
    assert proxy.load_count == 1
    assert model.batch_sizes == [4, 4]


def test_lazy_loader_skips_empty_input_without_loading():
    def loader():  # pragma: no cover - 不应被调用
        raise AssertionError("空候选不该加载模型")

    proxy = LazyOnnxReranker("unused", "unused", idle_seconds=0, loader=loader)
    assert proxy.predict([]) == []
    assert proxy.loaded is False


def test_idle_unload_then_reload():
    loads: list[_FakeModel] = []

    def loader():
        model = _FakeModel()
        loads.append(model)
        return model

    proxy = LazyOnnxReranker("unused", "unused", idle_seconds=0.05, loader=loader)
    proxy.predict([("q", "d")])
    assert proxy.loaded is True
    assert _wait_until(lambda: not proxy.loaded)
    assert loads[0].closed is True

    proxy.predict([("q", "d")])
    assert proxy.loaded is True
    assert len(loads) == 2


def test_non_positive_idle_keeps_model_resident():
    model = _FakeModel()
    proxy = LazyOnnxReranker("unused", "unused", idle_seconds=0, loader=lambda: model)
    proxy.predict([("q", "d")])
    time.sleep(0.1)
    assert proxy.loaded is True
    proxy.unload()
    assert proxy.loaded is False
    assert model.closed is True


def test_load_failure_is_not_retried():
    calls: list[int] = []

    def loader():
        calls.append(1)
        raise FileNotFoundError("模型文件缺失")

    proxy = LazyOnnxReranker("unused", "unused", idle_seconds=0, loader=loader)
    with pytest.raises(RuntimeError, match="加载失败"):
        proxy.predict([("q", "d")])
    with pytest.raises(RuntimeError, match="加载失败"):
        proxy.predict([("q", "d")])
    assert len(calls) == 1


# --------------------------------------------------------------------- 共享单例


def test_shared_reranker_is_singleton_per_model(tmp_path, monkeypatch):
    _write_model_files(tmp_path)
    app = AppSettings(_env_file=None, lolita_rag_root=tmp_path, reranker_model="", reranker_onnx_path=None)
    monkeypatch.setattr(rerank, "_shared", {})

    first = shared_onnx_reranker(app, loader=_FakeModel)
    second = shared_onnx_reranker(app, loader=_FakeModel)
    assert first is not None
    assert first is second


def test_shared_reranker_none_when_model_missing(tmp_path):
    app = AppSettings(_env_file=None, lolita_rag_root=tmp_path, reranker_model="", reranker_onnx_path=None)
    assert shared_onnx_reranker(app, loader=_FakeModel) is None


# --------------------------------------------------------------------- bridge 接线


class _FakeUpstreamReranker:
    def __init__(self, settings, model=None):
        self.settings = settings
        self.model = model

    @property
    def enabled(self):
        return True


def _install_fake_upstream_reranker(monkeypatch) -> type[_FakeUpstreamReranker]:
    """把 ``src.retriever.reranker`` 换成一个最小实现，避免依赖真实上游。"""
    src = ModuleType("src")
    src.__path__ = []  # type: ignore[attr-defined]
    retriever = ModuleType("src.retriever")
    retriever.__path__ = []  # type: ignore[attr-defined]
    module = ModuleType("src.retriever.reranker")
    module.Reranker = _FakeUpstreamReranker
    src.retriever = retriever  # type: ignore[attr-defined]
    retriever.reranker = module  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "src", src)
    monkeypatch.setitem(sys.modules, "src.retriever", retriever)
    monkeypatch.setitem(sys.modules, "src.retriever.reranker", module)
    return _FakeUpstreamReranker


def test_configure_reranker_uses_shared_onnx_proxy(tmp_path, monkeypatch):
    _write_model_files(tmp_path)
    app = AppSettings(_env_file=None, lolita_rag_root=tmp_path, reranker_model="", reranker_onnx_path=None)
    monkeypatch.setattr(rerank, "_shared", {})
    _install_fake_upstream_reranker(monkeypatch)
    settings = SimpleNamespace(reranker_enabled=True, reranker_batch_size=4)

    own = SimpleNamespace(retriever=SimpleNamespace(reranker=None))
    real = SimpleNamespace(retriever=SimpleNamespace(reranker=None))
    bridge._configure_reranker(own, settings, app)
    bridge._configure_reranker(real, settings, app)

    assert isinstance(own.retriever.reranker, _FakeUpstreamReranker)
    assert isinstance(own.retriever.reranker.model, LazyOnnxReranker)
    # own / real 必须共用同一份模型，否则 266 MB 会被算两遍
    assert own.retriever.reranker.model is real.retriever.reranker.model


def test_configure_reranker_disabled_removes_reranker(tmp_path, monkeypatch):
    app = AppSettings(
        _env_file=None, lolita_rag_root=tmp_path, reranker_enabled=False, reranker_model=""
    )
    _install_fake_upstream_reranker(monkeypatch)
    bundle = SimpleNamespace(retriever=SimpleNamespace(reranker="torch"))
    bridge._configure_reranker(bundle, SimpleNamespace(reranker_enabled=False), app)
    assert bundle.retriever.reranker is None


def test_configure_reranker_torch_backend_keeps_upstream(tmp_path, monkeypatch):
    app = AppSettings(
        _env_file=None, lolita_rag_root=tmp_path, reranker_backend="torch", reranker_model=""
    )
    _install_fake_upstream_reranker(monkeypatch)
    bundle = SimpleNamespace(retriever=SimpleNamespace(reranker=None))
    bridge._configure_reranker(bundle, SimpleNamespace(reranker_enabled=True), app)
    assert isinstance(bundle.retriever.reranker, _FakeUpstreamReranker)
    assert bundle.retriever.reranker.model is None


def test_configure_reranker_auto_falls_back_to_torch_without_onnx(tmp_path, monkeypatch):
    app = AppSettings(
        _env_file=None, lolita_rag_root=tmp_path, reranker_backend="auto", reranker_model=""
    )
    _install_fake_upstream_reranker(monkeypatch)
    bundle = SimpleNamespace(retriever=SimpleNamespace(reranker=None))
    bridge._configure_reranker(bundle, SimpleNamespace(reranker_enabled=True), app)
    assert isinstance(bundle.retriever.reranker, _FakeUpstreamReranker)
    assert bundle.retriever.reranker.model is None


def _stub_upstream(root: Path) -> Path:
    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / "src" / "__init__.py").write_text("", encoding="utf-8")
    (root / "src" / "config.py").write_text(
        "class Settings:\n"
        "    def __init__(self, **kwargs):\n"
        "        self.__dict__.update(kwargs)\n",
        encoding="utf-8",
    )
    return root


def test_upstream_settings_passes_reranker_defaults(tmp_path, monkeypatch):
    upstream = _stub_upstream(tmp_path / "lolita-rag")
    model_dir = upstream / "data" / "models" / "bge-reranker-base"
    model_dir.mkdir(parents=True)
    app = AppSettings(
        _env_file=None, data_dir=tmp_path / "own", lolita_rag_root=upstream, reranker_model=""
    )

    monkeypatch.setattr(sys, "path", list(sys.path))
    saved = {name: sys.modules.pop(name) for name in ("src", "src.config") if name in sys.modules}
    try:
        settings = bridge.upstream_settings(app, source="own")
        assert settings.reranker_enabled is True
        assert settings.reranker_batch_size == 4
        assert Path(settings.reranker_model) == model_dir.resolve()
    finally:
        for name in ("src", "src.config"):
            sys.modules.pop(name, None)
        sys.modules.update(saved)
