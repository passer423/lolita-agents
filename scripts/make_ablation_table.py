import json
from pathlib import Path

p = Path(r"f:\DeepSeekProgram\lolita-agents\eval\reports\20261117-151504\summary.json")
d = json.loads(p.read_text(encoding="utf-8"))

lines = [
    "## 多路召回消融（subset=all，n=52，merged 视角，2026-11-17）",
    "",
    "| 配置 | 含义 | recall@1 | recall@10 | MRR | cite@1 |",
    "|---|---|---|---|---|---|",
]
meaning = {
    "baseline": "hybrid 召回 + 向量路过滤 + 合并层（默认）",
    "real_all": "real 通道不过滤纯 BM25 命中",
    "merge_none": "无合并层（直接拼接）",
    "merge_round_robin": "合并层换成轮询合并",
    "top_k1": "每通道只取 top1",
    "top_k5": "每通道取 top5",
    "top_k10": "每通道取 top10",
}
for r in d["summary"]:
    if r["config"] in meaning and r["view"] == "merged":
        lines.append(
            f'| {r["config"]} | {meaning[r["config"]]} | {r["recall@1"]:.3f} | '
            f'{r["recall@10"]:.3f} | {r["mrr"]:.3f} | {r["citation_accuracy"]:.3f} |'
        )

# 通道视图
lines += ["", "### 通道视图（baseline）", "", "| 通道 | n | recall@1 | MRR |", "|---|---|---|---|"]
for r in d["summary"]:
    if r["config"] == "baseline" and r["view"] == "channel" and r["n"]:
        lines.append(f'| {r["channel"]} | {r["n"]} | {r["recall@1"]:.3f} | {r["mrr"]:.3f} |')

out = Path(r"f:\DeepSeekProgram\lolita-agents\eval\reports\ablation.md")
out.write_text("\n".join(lines) + "\n", encoding="utf-8")
print("\n".join(lines))
