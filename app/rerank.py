"""精排的 int8 ONNX 后端（2 GiB 内存部署用）。

上游的 ``Reranker`` 默认走 ``sentence_transformers.CrossEncoder``（PyTorch fp32）。
``bge-reranker-base`` 的 fp32 权重就有 1.11 GB，和 BGE 同驻 2 GiB 机器必然 OOM
（本机实测整条链路峰值 1.68 GB）。这里提供一条**质量等价**的替代：

* ``OnnxCrossEncoder`` —— 用 ``tokenizers.Tokenizer``（Rust 实现，常驻约 250 MB，
  比 ``AutoTokenizer`` 省约 70 MB）编码，用 int8 动态量化的 ONNX（约 266 MB）
  打分。候选按小 batch 前向（实测 batch=4 比 16 更快、峰值更低：短文本的 padding
  浪费比并行度更值钱）。
* ``LazyOnnxReranker`` —— 接口对齐 ``CrossEncoder.predict`` 的代理：第一次打分时
  才加载，闲置 ``idle_seconds`` 秒后卸载并 ``gc.collect()``，把常驻内存还回去。
  冷加载约 0.3–0.6 秒，只影响「闲置后的第一问」，之后的查询不再付这份成本。
* ``shared_onnx_reranker`` —— 模块级单例。``own`` / ``real`` 两个 bundle 共用同一份
  模型；否则两份 266 MB 会把省下来的内存又吃回去。

失败语义与上游一致：加载/打分失败都抛 ``RuntimeError``，由上游 ``Reranker.rerank``
捕获后按原顺序返回。精排是加分项，不能把检索本身拖死。
"""

from __future__ import annotations

import gc
import logging
import threading
import time
from collections.abc import Callable, Iterable, Iterator, Sequence
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_MAX_LENGTH = 512
DEFAULT_PAD_ID = 1
DEFAULT_PAD_TOKEN = "<pad>"


def resolve_reranker_model(app: Any) -> Path:
    """精排模型目录。留空时取上游仓库自带的 ``data/models/bge-reranker-base``。"""
    explicit = str(getattr(app, "reranker_model", "") or "").strip()
    if explicit:
        return Path(explicit)
    return Path(app.lolita_rag_root) / "data" / "models" / "bge-reranker-base"


def resolve_onnx_path(app: Any) -> Path:
    """int8 ONNX 文件。留空时取 ``<模型目录>/onnx/model.int8.onnx``。"""
    explicit = getattr(app, "reranker_onnx_path", None)
    if explicit:
        return Path(explicit)
    return resolve_reranker_model(app) / "onnx" / "model.int8.onnx"


def _iter_batches(items: Sequence[Any], size: int) -> Iterator[Sequence[Any]]:
    step = max(1, int(size or 1))
    for start in range(0, len(items), step):
        yield items[start : start + step]


class OnnxCrossEncoder:
    """只依赖 ``tokenizers`` + ``onnxruntime`` 的 cross-encoder 打分器。

    ``predict`` 与 ``sentence_transformers.CrossEncoder`` 同名同序：返回与
    ``pairs`` 一一对应的 logits，调用方（上游 ``Reranker``）负责 sigmoid 与排序。
    """

    def __init__(
        self,
        model_dir: Path | str,
        onnx_path: Path | str,
        *,
        max_length: int = DEFAULT_MAX_LENGTH,
        threads: int | None = None,
    ):
        import numpy as np
        import onnxruntime as ort
        from tokenizers import Tokenizer

        model_dir = Path(model_dir)
        onnx_path = Path(onnx_path)
        tokenizer_file = model_dir / "tokenizer.json"
        if not tokenizer_file.is_file():
            raise FileNotFoundError(f"缺少 tokenizer.json：{tokenizer_file}")
        if not onnx_path.is_file():
            raise FileNotFoundError(f"缺少 int8 ONNX 模型：{onnx_path}")

        self._np = np
        self.max_length = int(max_length or DEFAULT_MAX_LENGTH)
        # Rust tokenizer：ids 与 HF 完全一致，常驻内存比 AutoTokenizer 少约 70 MB。
        tokenizer = Tokenizer.from_file(str(tokenizer_file))
        tokenizer.enable_truncation(max_length=self.max_length)
        tokenizer.enable_padding(pad_id=DEFAULT_PAD_ID, pad_token=DEFAULT_PAD_TOKEN)
        self.tokenizer = tokenizer

        options = ort.SessionOptions()
        if threads:
            options.intra_op_num_threads = int(threads)
        options.inter_op_num_threads = 1
        self.session = ort.InferenceSession(
            str(onnx_path), sess_options=options, providers=["CPUExecutionProvider"]
        )
        self.input_names = [item.name for item in self.session.get_inputs()]

    def predict(self, pairs: Iterable[tuple[str, str]], batch_size: int = 16) -> list[float]:
        items = [(str(query), str(text)) for query, text in pairs]
        if not items:
            return []
        np = self._np
        scores: list[float] = []
        for batch in _iter_batches(items, batch_size):
            encodings = self.tokenizer.encode_batch([(query, text) for query, text in batch])
            ids = np.asarray([encoding.ids for encoding in encodings], dtype=np.int64)
            mask = np.asarray([encoding.attention_mask for encoding in encodings], dtype=np.int64)
            feed = {"input_ids": ids, "attention_mask": mask}
            if "token_type_ids" in self.input_names:
                feed["token_type_ids"] = np.zeros_like(ids)
            logits = self.session.run(None, feed)[0]
            # bge-reranker 只有一个标签；用 [:, 0] 而不是 reshape(-1)，
            # 避免将来多标签导出时把 batch 维度悄悄拉平。
            scores.extend(np.asarray(logits).reshape(len(batch), -1)[:, 0].tolist())
        return scores

    def close(self) -> None:
        """丢掉 session 与 tokenizer，让 GC 能真正回收内存。"""
        self.session = None
        self.tokenizer = None


class LazyOnnxReranker:
    """按需加载 / 闲置卸载的 reranker 代理。

    ``loader`` 可注入（测试无需真的读 266 MB 模型）；默认构造 ``OnnxCrossEncoder``。
    加载失败会记成永久失败，之后的查询直接抛错交给上游降级，不反复重试拖慢请求。
    """

    def __init__(
        self,
        model_dir: Path | str,
        onnx_path: Path | str,
        *,
        idle_seconds: float = 120.0,
        max_length: int = DEFAULT_MAX_LENGTH,
        loader: Callable[[], Any] | None = None,
    ):
        self.model_dir = Path(model_dir)
        self.onnx_path = Path(onnx_path)
        self.idle_seconds = float(idle_seconds)
        self.max_length = int(max_length or DEFAULT_MAX_LENGTH)
        self._loader = loader
        self._model: Any = None
        self._failed = False
        self._load_count = 0
        self._lock = threading.Lock()
        self._timer: threading.Timer | None = None
        self._idle_gen = 0

    # ---------------------------------------------------------------- 状态（测试/排查）
    @property
    def loaded(self) -> bool:
        return self._model is not None

    @property
    def load_count(self) -> int:
        return self._load_count

    # ---------------------------------------------------------------- 加载 / 卸载
    def _build(self) -> Any:
        if self._loader is not None:
            return self._loader()
        return OnnxCrossEncoder(self.model_dir, self.onnx_path, max_length=self.max_length)

    def _get_model(self) -> Any:
        if self._model is not None:
            return self._model
        if self._failed:
            raise RuntimeError("精排模型此前加载失败，本次按原顺序返回")
        started = time.perf_counter()
        try:
            self._model = self._build()
        except Exception as exc:  # noqa: BLE001 - 转成可读错误交给上游降级
            self._failed = True
            raise RuntimeError(f"精排模型加载失败：{exc}") from exc
        self._load_count += 1
        logger.info(
            "精排模型已加载（ONNX int8）：%s（%.2fs）",
            self.onnx_path,
            time.perf_counter() - started,
        )
        return self._model

    def _cancel_timer(self) -> None:
        timer = self._timer
        self._timer = None
        if timer is not None:
            timer.cancel()

    def _schedule_unload(self) -> None:
        # 代次让「已经触发但还在等锁」的旧 Timer 失效，避免刚热起来就被旧定时器卸掉。
        self._idle_gen += 1
        generation = self._idle_gen
        self._cancel_timer()
        if self.idle_seconds <= 0:
            return
        timer = threading.Timer(self.idle_seconds, self._unload_if_idle, args=(generation,))
        timer.daemon = True
        self._timer = timer
        timer.start()

    def _unload_if_idle(self, generation: int) -> None:
        with self._lock:
            if generation != self._idle_gen or self._model is None:
                return
            self._timer = None
            close = getattr(self._model, "close", None)
            self._model = None
            if callable(close):
                close()
            gc.collect()
            logger.info("精排模型已闲置 %.0f 秒，卸载释放内存", self.idle_seconds)

    def unload(self) -> None:
        """主动卸载（测试与进程收尾用）。"""
        with self._lock:
            self._idle_gen += 1
            self._cancel_timer()
            if self._model is not None:
                close = getattr(self._model, "close", None)
                self._model = None
                if callable(close):
                    close()
                gc.collect()

    # ---------------------------------------------------------------- 打分
    def predict(self, pairs: Iterable[tuple[str, str]], batch_size: int | None = None) -> list[float]:
        items = list(pairs)
        if not items:
            return []
        with self._lock:
            model = self._get_model()
            self._idle_gen += 1
            self._cancel_timer()
            try:
                return model.predict(items, batch_size=int(batch_size or 16))
            finally:
                self._schedule_unload()


_shared: dict[tuple[str, str], LazyOnnxReranker] = {}
_shared_lock = threading.Lock()


def shared_onnx_reranker(app: Any, *, loader: Callable[[], Any] | None = None) -> LazyOnnxReranker | None:
    """取（或创建）进程级共享的精排代理；模型文件/依赖缺失时返回 ``None``。

    ``own`` 与 ``real`` 两个索引共用一份模型——各自构造会把 266 MB 翻倍。
    """
    model_dir = resolve_reranker_model(app)
    onnx_path = resolve_onnx_path(app)
    if not onnx_path.is_file() or not (model_dir / "tokenizer.json").is_file():
        return None
    if loader is None:
        from importlib.util import find_spec

        if find_spec("onnxruntime") is None or find_spec("tokenizers") is None:
            logger.warning("未安装 onnxruntime / tokenizers，精排无法使用 int8 后端")
            return None
    key = (str(model_dir.resolve()), str(onnx_path.resolve()))
    with _shared_lock:
        existing = _shared.get(key)
        if existing is not None:
            return existing
        proxy = LazyOnnxReranker(
            model_dir,
            onnx_path,
            idle_seconds=float(getattr(app, "reranker_idle_unload_seconds", 120.0)),
            loader=loader,
        )
        _shared[key] = proxy
        return proxy
