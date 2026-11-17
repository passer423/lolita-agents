"""采集简历/报告用的规模与性能数字。

用法（在 lolita-agents 根目录）：

    python scripts/bench_numbers.py [--queries N] [--out PATH]

输出一段 Markdown：
- 索引规模：own / real 两份索引的 chunk 数、manifest 时间、构建耗时（若上游记录）
- 检索延迟：三核心通道各自跑 N 次真实金标 query 的 P50 / P95 / 均值（毫秒）
- 语料规模：上游 raw posts 数（若可读）

只读，不改任何索引。延迟含模型装载（首次调用）之外的开销已通过预热排除。
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.agent import channels  # noqa: E402
from app.config import settings  # noqa: E402


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    k = max(0, min(len(ordered) - 1, round(pct / 100 * (len(ordered) - 1))))
    return ordered[k]


def _index_stats() -> list[tuple[str, int, str]]:
    rows: list[tuple[str, int, str]] = []
    for name, source in (("own", "own"), ("real", "real")):
        try:
            manifest = {
                "own": ROOT / "data" / "kbindex" / "index" / "manifest.json",
                "real": Path(settings.lolita_rag_root) / "data" / "index" / "manifest.json",
            }[source]
            data = json.loads(manifest.read_text(encoding="utf-8"))
            built = data.get("built_at") or data.get("updated_at") or "-"
            rows.append((name, int(data.get("chunks") or data.get("chunk_count") or 0), str(built)))
        except Exception as exc:  # noqa: BLE001 - 缺索引不应中断整个采集
            rows.append((name, 0, f"unavailable: {exc}"))
    return rows


def _latency(golden: Path, per_channel: int) -> dict[str, dict[str, float]]:
    items = [json.loads(line) for line in golden.read_text(encoding="utf-8").splitlines() if line.strip()]
    queries = [i["query"] for i in items if i["channel"] in ("own", "real", "lolibrary")][:per_channel]
    ctx = channels.RecallContext(settings=settings)

    # 预热：装载索引与模型，不计入延迟
    if queries:
        for name in channels.CORE_CHANNELS:
            channels.recall(name, queries[0], ctx)

    out: dict[str, dict[str, float]] = {}
    for name in channels.CORE_CHANNELS:
        samples: list[float] = []
        for query in queries:
            start = time.perf_counter()
            channels.recall(name, query, ctx)
            samples.append((time.perf_counter() - start) * 1000)
        if samples:
            out[name] = {
                "n": len(samples),
                "p50": _percentile(samples, 50),
                "p95": _percentile(samples, 95),
                "mean": statistics.fmean(samples),
            }
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--queries", type=int, default=30, help="每个通道测多少条 query")
    parser.add_argument("--out", type=Path, default=None, help="输出 markdown 路径（默认打印）")
    args = parser.parse_args()

    lines: list[str] = ["## 规模与性能", ""]
    lines.append("### 索引规模")
    lines.append("")
    lines.append("| 索引 | chunks | built_at |")
    lines.append("|---|---|---|")
    for name, chunks, built in _index_stats():
        lines.append(f"| {name} | {chunks} | {built} |")
    lines.append("")

    raw = Path(settings.lolita_rag_root) / "data" / "raw" / "posts.jsonl"
    if raw.exists():
        count = sum(1 for _ in raw.open(encoding="utf-8"))
        lines.append(f"上游真实语料帖数：{count}")
        lines.append("")

    lines.append(f"### 检索延迟（{args.queries} 条金标 query，进程内，含预热排除）")
    lines.append("")
    lines.append("| 通道 | n | P50 (ms) | P95 (ms) | 均值 (ms) |")
    lines.append("|---|---|---|---|---|")
    latency = _latency(ROOT / "eval" / "golden.jsonl", args.queries)
    for name, row in latency.items():
        lines.append(
            f"| {name} | {row['n']} | {row['p50']:.1f} | {row['p95']:.1f} | {row['mean']:.1f} |"
        )
    text = "\n".join(lines) + "\n"

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8")
        print(f"written: {args.out}")
    else:
        print(text)


if __name__ == "__main__":
    main()
