"""把 ``bge-reranker-base`` 导出为 ONNX 并做 int8 动态量化。

为什么需要这一步：精排默认开启（``RERANKER_ENABLED=true``），而 fp32 torch 权重
1.11 GB，和 BGE 同驻 2 GiB 机器必然 OOM。int8 量化后权重约 266 MB，配合
``app/rerank.py`` 的懒加载/闲置卸载，2 GiB 才有余量。

用法（模型已在上游仓库的 data/models 下时，不需要任何参数）::

    python scripts/export_reranker_onnx.py            # 缺什么补什么
    python scripts/export_reranker_onnx.py --check    # 顺便和 torch 对比分数
    python scripts/export_reranker_onnx.py --force    # 重新导出并量化

产物默认写到 ``<模型目录>/onnx/model.int8.onnx``。导出过程需要约 1.4 GB 磁盘
（fp32 中间文件 1.1 GB + int8 266 MB），量化完成后 fp32 文件可以删掉。
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import settings as default_settings  # noqa: E402


def _default_model_dir() -> Path:
    return Path(default_settings.lolita_rag_root) / "data" / "models" / "bge-reranker-base"


def _export_fp32(model_dir: Path, out_path: Path, *, max_length: int, opset: int) -> None:
    import torch
    from transformers import AutoModelForSequenceClassification, AutoTokenizer

    print(f"[1/2] 导出 fp32 ONNX：{out_path}")
    tokenizer = AutoTokenizer.from_pretrained(str(model_dir), local_files_only=True)
    model = AutoModelForSequenceClassification.from_pretrained(
        str(model_dir), local_files_only=True
    ).eval()

    sample = tokenizer(
        ["痛lo和日常lo有什么区别？", "这条裙子怎么洗？"],
        ["痛lo是偏痛风格的日常搭配。", "建议手洗，避免机洗。"],
        padding=True,
        truncation=True,
        max_length=min(max_length, 64),
        return_tensors="pt",
    )

    class _LogitsOnly(torch.nn.Module):
        def __init__(self, inner: torch.nn.Module):
            super().__init__()
            self.inner = inner

        def forward(self, input_ids, attention_mask):  # noqa: ANN001 - torch 导出要求具名
            return self.inner(input_ids=input_ids, attention_mask=attention_mask).logits

    out_path.parent.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    torch.onnx.export(
        _LogitsOnly(model),
        (sample["input_ids"], sample["attention_mask"]),
        str(out_path),
        input_names=["input_ids", "attention_mask"],
        output_names=["logits"],
        dynamic_axes={
            "input_ids": {0: "batch", 1: "seq"},
            "attention_mask": {0: "batch", 1: "seq"},
            "logits": {0: "batch"},
        },
        opset_version=opset,
        dynamo=False,
    )
    print(f"      fp32 {out_path.stat().st_size / 1024 / 1024:.1f} MB，"
          f"耗时 {time.perf_counter() - started:.1f}s")


def _quantize_int8(fp32_path: Path, int8_path: Path) -> None:
    from onnxruntime.quantization import QuantType, quantize_dynamic

    print(f"[2/2] int8 动态量化：{int8_path}")
    started = time.perf_counter()
    quantize_dynamic(str(fp32_path), str(int8_path), weight_type=QuantType.QInt8)
    print(f"      int8 {int8_path.stat().st_size / 1024 / 1024:.1f} MB，"
          f"耗时 {time.perf_counter() - started:.1f}s")


def _check(model_dir: Path, int8_path: Path, *, max_length: int) -> bool:
    """用几对真实短文本对比 torch 与 int8 的分数，确认量化没有破坏排序。"""
    import numpy as np
    import onnxruntime as ort
    import torch
    from transformers import AutoModelForSequenceClassification, AutoTokenizer

    query = "痛lo和日常lo有什么区别？"
    texts = [
        "痛lo是偏痛风格的日常搭配，会更甜、更有主题性。",
        "日常lo是适合每天穿的轻量款，配色低调。",
        "这条裙子建议手洗，避免机洗和暴晒。",
    ]
    pairs = [(query, text) for text in texts]

    tokenizer = AutoTokenizer.from_pretrained(str(model_dir), local_files_only=True)
    model = AutoModelForSequenceClassification.from_pretrained(
        str(model_dir), local_files_only=True
    ).eval()
    encoded = tokenizer(
        [pair[0] for pair in pairs],
        [pair[1] for pair in pairs],
        padding=True,
        truncation=True,
        max_length=max_length,
        return_tensors="pt",
    )
    with torch.no_grad():
        torch_logits = model(**encoded).logits.reshape(-1).tolist()

    session = ort.InferenceSession(str(int8_path), providers=["CPUExecutionProvider"])
    ort_logits = session.run(
        None,
        {
            "input_ids": encoded["input_ids"].numpy().astype(np.int64),
            "attention_mask": encoded["attention_mask"].numpy().astype(np.int64),
        },
    )[0].reshape(-1).tolist()

    diff = max(abs(a - b) for a, b in zip(torch_logits, ort_logits, strict=True))
    order_ok = sorted(range(len(texts)), key=lambda i: -torch_logits[i]) == sorted(
        range(len(texts)), key=lambda i: -ort_logits[i]
    )
    print("      torch logits :", [round(value, 4) for value in torch_logits])
    print("      int8  logits :", [round(value, 4) for value in ort_logits])
    print(f"      最大绝对误差 {diff:.4f}；排序一致：{order_ok}")
    return order_ok and diff < 0.5


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-dir", type=Path, default=_default_model_dir())
    parser.add_argument("--out", type=Path, default=None, help="默认 <模型目录>/onnx/model.int8.onnx")
    parser.add_argument("--max-length", type=int, default=512)
    parser.add_argument("--opset", type=int, default=14)
    parser.add_argument("--force", action="store_true", help="已有产物也重新导出")
    parser.add_argument("--check", action="store_true", help="量化后与 torch 对比分数")
    args = parser.parse_args()

    model_dir: Path = args.model_dir
    if not (model_dir / "config.json").is_file():
        print(f"模型目录不对：{model_dir}（缺 config.json）", file=sys.stderr)
        return 2

    out_path: Path = args.out or (model_dir / "onnx" / "model.int8.onnx")
    fp32_path = out_path.with_name("model.onnx")

    if args.force or not fp32_path.is_file():
        _export_fp32(model_dir, fp32_path, max_length=args.max_length, opset=args.opset)
    else:
        print(f"[1/2] 复用已有 fp32：{fp32_path}")

    if args.force or not out_path.is_file():
        _quantize_int8(fp32_path, out_path)
    else:
        print(f"[2/2] 复用已有 int8：{out_path}")

    print(f"完成：{out_path}（{out_path.stat().st_size / 1024 / 1024:.1f} MB）")
    if args.check:
        return 0 if _check(model_dir, out_path, max_length=args.max_length) else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
