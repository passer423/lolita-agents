"""答案层质量评估：faithfulness（答案是否被证据支撑）。

检索层指标（recall@1 / cite@1 / definition@1）只衡量「找没找到」；本脚本补上
生成层：对每条金标跑完整问答（graph.run，真实 writer），再用 LLM 裁判判断
「答案中的事实性陈述是否都被证据支撑」。

用法：

    python eval/answer_eval.py [--ids id1,id2] [--limit N] [--subset contracts]

- 默认评估 golden.jsonl 里带 evidence_must_any 契约的条目（可判支撑性）。
- 需要在线 LLM（.env 配 LLM_API_KEY）；离线时明确失败，不降级。
- 报告落 eval/reports/answer-eval-<时间戳>/（gitignore 内）。

判定口径（裁判提示词里固定）：
- supported=true：答案所有事实性陈述都能在证据里找到出处；「没有证据」的
  诚实拒答也算 supported。
- supported=false：出现证据之外的编造、或与证据矛盾的关键事实。

不是 correctness（答案对不对取决于语料），只问「有没有据」。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.agent.channels import RecallContext  # noqa: E402
from app.agent.graph import run as graph_run  # noqa: E402
from app.agent.planner import DeepSeekPlanner  # noqa: E402
from app.agent.writer import DeepSeekWriter  # noqa: E402
from app.config import settings  # noqa: E402

JUDGE_SYSTEM = "你是严谨的评测裁判，只依据给定证据判断答案是否有据可依，不靠你自己的知识。"

JUDGE_PROMPT = """## 问题
{question}

## 证据（编号即答案里的 [n]）
{evidence}

## 待评答案
{answer}

判断这个答案的 faithfulness：
1. 逐句找出答案中的事实性陈述（数字、时间、名称、结论、归因）。
2. 每条陈述在证据里找出处。证据支持或证据明确写出才算「有据」。
3. 答案说「语料里没有/不确定」而证据确实没有，算有据（诚实拒答）。
4. 编造证据里没有的事实、或与证据矛盾，算无据。

只输出一行 JSON：
{{"supported": true/false, "violations": ["无据的陈述，无则空"], "reason": "一句话"}}
"""


def _load_golden(path: Path) -> list[dict]:
    items = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return [i for i in items if i.get("channel") in ("own", "real", "lolibrary")]


def _evidence_block(evidence: list[dict]) -> str:
    lines = []
    for idx, ev in enumerate(evidence, 1):
        text = (ev.get("text") or "").replace("\n", " ")[:400]
        lines.append(f"[{idx}] ({ev.get('channel')}) {text}")
    return "\n".join(lines) or "（无证据）"


def _judge(client, model: str, question: str, evidence: list[dict], answer: str) -> dict:
    prompt = JUDGE_PROMPT.format(
        question=question,
        evidence=_evidence_block(evidence),
        answer=answer[:2000],
    )
    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": JUDGE_SYSTEM},
            {"role": "user", "content": prompt},
        ],
        temperature=0,
        max_tokens=300,
    )
    raw = (response.choices[0].message.content or "").strip()
    # 容忍模型在 JSON 前后加话
    start, end = raw.find("{"), raw.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(raw[start : end + 1])
        except json.JSONDecodeError:
            pass
    return {"supported": None, "violations": [], "reason": f"裁判输出不可解析：{raw[:200]}"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ids", default="", help="逗号分隔的金标 id，默认全部带契约条目")
    parser.add_argument("--limit", type=int, default=0, help="最多评估多少条（0=不限）")
    parser.add_argument("--golden", type=Path, default=ROOT / "eval" / "golden.jsonl")
    args = parser.parse_args()

    if not settings.llm_api_key:
        print("错误：答案层评估需要 LLM_API_KEY（.env），不降级。", file=sys.stderr)
        return 2

    items = _load_golden(args.golden)
    if args.ids:
        wanted = {x.strip() for x in args.ids.split(",") if x.strip()}
        items = [i for i in items if i["id"] in wanted]
    else:
        items = [i for i in items if i.get("evidence_must_any")]
    if args.limit > 0:
        items = items[: args.limit]
    if not items:
        print("没有可评估的条目。", file=sys.stderr)
        return 2

    from openai import OpenAI

    client = OpenAI(api_key=settings.llm_api_key.strip(), base_url=settings.llm_base_url.strip(), timeout=120)
    ctx = RecallContext(settings=settings)
    planner = DeepSeekPlanner(settings)
    writer = DeepSeekWriter(settings)

    rows = []
    for item in items:
        started = time.perf_counter()
        result = graph_run(item["query"], ctx=ctx, planner=planner, writer=writer)
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        answer = result.get("answer") or ""
        evidence = result.get("evidence") or []
        verdict = _judge(client, settings.llm_model_cheap or settings.llm_model, item["query"], evidence, answer)
        row = {
            "id": item["id"],
            "query": item["query"],
            "refused": result.get("refused", False),
            "answer_chars": len(answer),
            "evidence_count": len(evidence),
            "elapsed_ms": elapsed_ms,
            "supported": verdict.get("supported"),
            "violations": verdict.get("violations") or [],
            "reason": verdict.get("reason") or "",
        }
        rows.append(row)
        print(
            f'{row["id"]:<14} supported={row["supported"]} refused={row["refused"]} '
            f'{row["elapsed_ms"]}ms ev={row["evidence_count"]} {row["reason"][:60]}'
        )

    total = len(rows)
    ok = sum(1 for r in rows if r["supported"] is True)
    unknown = sum(1 for r in rows if r["supported"] is None)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = ROOT / "eval" / "reports" / f"answer-eval-{stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)

    summary = {
        "n": total,
        "supported": ok,
        "not_supported": sum(1 for r in rows if r["supported"] is False),
        "unparseable": unknown,
        "faithfulness": round(ok / total, 3) if total else 0.0,
        "mean_elapsed_ms": int(sum(r["elapsed_ms"] for r in rows) / total) if total else 0,
    }
    (out_dir / "summary.json").write_text(
        json.dumps({"summary": summary, "rows": rows}, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    lines = [
        "# 答案层 faithfulness 评估",
        "",
        f"- n = {total}（带内容契约金标，graph.run 全流程 + LLM 裁判）",
        f"- supported = {summary['supported']} / {total}（faithfulness = {summary['faithfulness']:.3f}）",
        f"- 裁判不可解析 = {summary['unparseable']}",
        f"- 端到端均值延迟 = {summary['mean_elapsed_ms']}ms",
        "",
        "## 未获支撑的条目",
        "",
    ]
    for r in rows:
        if r["supported"] is not True:
            lines.append(f"- **{r['id']}** {r['reason']}")
            for v in r["violations"]:
                lines.append(f"  - {v}")
    (out_dir / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nfaithfulness={summary['faithfulness']:.3f} ({ok}/{total})  报告：{out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
