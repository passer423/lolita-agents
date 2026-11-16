"""引用取证：真实链路跑一批金标问题，落盘回答 / 证据 / 引用，并做引用统计。

只读 ``app/``：调用 ``graph.run``、``DeepSeekPlanner``、``DeepSeekWriter``，
不改任何业务代码。用途是回答一个决策问题——**如果只能加一层引用校验，
该加「越界/不存在的硬错」还是「句级支撑」**。

为什么要同时记录两份回答：

* ``graph.verify`` 在「引用越界」或「有证据却一个编号都不标」时，会把整段回答
  换成原文清单（``_forced_answer``）。如果只看接口返回，会把写出层的硬错掩盖
  掉——我们看到的全是兜底文案。
* 所以这里用 :class:`RecordingWriter` 包住写作器，同时留下**模型原始输出**
  与**接口实际返回**，两者分开统计。

产出（默认 ``eval/forensics/<时间戳>/``）：

* ``raw.json``    逐题完整记录：问题、两份回答、写作器看到的编号证据、步骤、成本；
* ``stats.json``  可量化指标（每段引用数、长段单引用、越界、未引段落等）；
* ``audit.md``    逐条引用对齐表（句子 ↔ 证据原文），供人工判定支撑关系。

本脚本不判断「证据是否支撑那句话」——那必须人来读，规则判不出来。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.agent import graph  # noqa: E402 - 先保证 ROOT 在 sys.path
from app.agent.channels import RecallContext  # noqa: E402
from app.agent.planner import DeepSeekPlanner  # noqa: E402
from app.agent.writer import DeepSeekWriter  # noqa: E402
from app.config import settings as default_settings  # noqa: E402

# 12 条：own 5（怎么洗 / 尺码 / 定义与对比）、real 4（掉色 / 浮色处理 / 洗护 / 尺码）、
# lolibrary 3（定义型）。覆盖三种召回来源与「怎么洗」「是什么」两类问法。
#
# 选样限制（取证时实测）：非裙子类问题目前会被 ``dress_unresolved`` 短路成
# 「没认出是哪条裙子」，根本走不到写作层（例如「柄图是什么意思」「痛lo是什么意思」）。
# 这批样本只取**能到达写作层**的问题，否则统计的是兜底文案而不是引用行为。
DEFAULT_IDS = (
    "own-01",
    "own-02",
    "own-05",
    "own-08",
    "own-09",
    "real-01",
    "real-02",
    "real-04",
    "real-06",
    "lolibrary-01",
    "lolibrary-03",
    "lolibrary-08",
)

CITATION_RE = re.compile(r"\[(\d+)\]")
# 句子切分：只按句末标点（。！？!?）和换行切，**不切分号**。写作器习惯把 [n]
# 放在一长串「；」分句的末尾，按分号切会把前半截误判成「没引用」。代价是一句
# 里挂一个 [n] 就算整句有引用——位置覆盖不到「编号只说明最后一小句」的情况。
SENTENCE_RE = re.compile(r"[^。！？!?\n]+[。！？!?]?")
# 「整段几百字只有一个 [n]」：长段阈值取 100 字（聊天体一屏大约 3~5 行）。
LONG_PARAGRAPH_CHARS = 100
# 可能是「陈述了事实却没标来源」的段落下限；只是候选，是否算事实靠人判断。
CLAIM_PARAGRAPH_CHARS = 40


class RecordingWriter:
    """包住写作器，记下模型原始输出与它当时看到的证据。"""

    def __init__(self, inner: DeepSeekWriter) -> None:
        self._inner = inner
        self.calls: list[dict] = []

    @property
    def model(self) -> str:
        return self._inner.model

    @property
    def usage(self) -> dict:
        return self._inner.usage

    def write(self, question, evidence, history=None, *, model=None) -> str:
        text = self._inner.write(question, evidence, history, model=model)
        self.calls.append(
            {"model": model or self.model, "evidence": evidence, "text": text}
        )
        return text


def load_golden(path: Path) -> dict[str, dict]:
    items: dict[str, dict] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            item = json.loads(line)
            items[str(item["id"])] = item
    return items


def paragraphs_of(answer: str) -> list[str]:
    return [part.strip() for part in (answer or "").split("\n") if part.strip()]


def sentences_of(paragraph: str) -> list[str]:
    return [part.strip() for part in SENTENCE_RE.findall(paragraph) if part.strip()]


def tokens_of(text: str) -> list[int]:
    return [int(token) for token in CITATION_RE.findall(text or "")]


def evidence_brief(item: dict, limit: int = 300) -> dict:
    text = str(item.get("text") or "")
    if len(text) > limit:
        text = text[:limit] + "…"
    return {
        "n": item.get("n"),
        "channel": item.get("channel"),
        "source_kind": item.get("source_kind"),
        "ref": item.get("ref"),
        "title": item.get("title"),
        "url": item.get("url"),
        "text": text,
    }


def analyse_answer(answer: str, evidence: list[dict]) -> dict:
    """纯统计：不判断支撑关系，只把可疑位置摆出来。"""
    by_number = {int(item.get("n") or 0): item for item in evidence}
    count = len(evidence)

    paragraphs = []
    citations = []
    sentences = []
    for index, paragraph in enumerate(paragraphs_of(answer), start=1):
        tokens = tokens_of(paragraph)
        paragraphs.append(
            {
                "index": index,
                "chars": len(paragraph),
                "citations": len(tokens),
                "tokens": sorted(set(tokens)),
                "text": paragraph,
            }
        )
        for sentence in sentences_of(paragraph):
            sentence_tokens = tokens_of(sentence)
            sentences.append(
                {
                    "paragraph": index,
                    "chars": len(sentence),
                    "citations": len(sentence_tokens),
                    "text": sentence,
                }
            )
            for token in sentence_tokens:
                item = by_number.get(token)
                citations.append(
                    {
                        "paragraph": index,
                        "token": token,
                        "in_range": 1 <= token <= count,
                        "sentence": sentence,
                        "evidence_ref": (item or {}).get("ref", ""),
                        "evidence_title": (item or {}).get("title", ""),
                        "evidence_channel": (item or {}).get("channel", ""),
                    }
                )

    tokens = [c["token"] for c in citations]
    invalid = sorted({token for token in tokens if not 1 <= token <= count})
    uncited_paragraphs = [
        part for part in paragraphs
        if part["citations"] == 0 and part["chars"] >= CLAIM_PARAGRAPH_CHARS
    ]
    long_single = [
        part for part in paragraphs
        if part["chars"] >= LONG_PARAGRAPH_CHARS and part["citations"] <= 1
    ]
    return {
        "answer_chars": len(answer or ""),
        "paragraphs": paragraphs,
        "sentences": sentences,
        "citations": citations,
        "citation_tokens": tokens,
        "unique_tokens": sorted(set(tokens)),
        "invalid_tokens": invalid,
        "uncited_paragraphs": uncited_paragraphs,
        # 句级口径：段里有引用不代表每句都有。>=15 字才计入，避免把「会掉色。」这类
        # 短结论句也算成缺引用（它通常紧跟一句带引用的展开）。
        "uncited_sentences": [
            sentence for sentence in sentences
            if sentence["citations"] == 0 and sentence["chars"] >= 15
        ],
        "long_single_citation_paragraphs": long_single,
        "uncited_evidence": sorted(set(range(1, count + 1)) - set(tokens)),
        "evidence_count": count,
    }


def run_item(item: dict, golden_path: Path) -> dict:
    # 每条都新建 planner / writer：cost 块与 usage 是按实例累计的，复用会串账。
    planner = DeepSeekPlanner(default_settings)
    inner = DeepSeekWriter(default_settings)
    writer = RecordingWriter(inner)
    started = datetime.now()
    result = graph.run(
        item["query"],
        ctx=RecallContext(settings=default_settings),
        planner=planner,
        writer=writer,
        history=[],
    )
    raw_text = writer.calls[-1]["text"] if writer.calls else ""
    seen = writer.calls[-1]["evidence"] if writer.calls else result.get("evidence", [])
    returned = str(result.get("answer") or "")
    return {
        "id": item["id"],
        "channel": item["channel"],
        "query": item["query"],
        "expected_refs": item.get("expected_refs", []),
        "golden_note": item.get("note", ""),
        "started_at": started.isoformat(timespec="seconds"),
        "writer_called": bool(writer.calls),
        "writer_model": writer.calls[-1]["model"] if writer.calls else "",
        "raw_answer": raw_text,
        "answer": returned,
        "stopped_reason": result.get("stopped_reason", ""),
        "refused": result.get("refused"),
        "duration_ms": result.get("duration_ms"),
        "route": result.get("route"),
        "steps": result.get("steps", []),
        "cost": result.get("cost"),
        "usage": result.get("usage"),
        "evidence": [evidence_brief(entry) for entry in seen],
        "raw_stats": analyse_answer(raw_text, seen),
        "returned_stats": analyse_answer(returned, seen),
        "golden_path": str(golden_path),
    }


def aggregate(records: list[dict]) -> dict:
    raw = [r["raw_stats"] for r in records if r["writer_called"]]
    returned = [r["returned_stats"] for r in records]
    paragraphs = sum(len(stat["paragraphs"]) for stat in raw)
    sentences = sum(len(stat["sentences"]) for stat in raw)
    cited_sentences = sum(
        1 for stat in raw for sentence in stat["sentences"] if sentence["citations"] > 0
    )
    total_citations = sum(len(stat["citation_tokens"]) for stat in raw)
    long_single = [part for stat in raw for part in stat["long_single_citation_paragraphs"]]
    uncited = [part for stat in raw for part in stat["uncited_paragraphs"]]
    uncited_sentences = [
        sentence for stat in raw for sentence in stat["uncited_sentences"]
    ]
    invalid_raw = sum(len(stat["invalid_tokens"]) for stat in raw)
    invalid_returned = sum(len(stat["invalid_tokens"]) for stat in returned)
    replaced = [r["id"] for r in records if r["stopped_reason"] in
                ("citation_out_of_range", "no_citation")]
    flagged = [r["id"] for r in records if r["raw_stats"]["uncited_sentences"]]
    channel_hits: Counter = Counter()
    for record in records:
        for entry in record["evidence"]:
            channel_hits[str(entry.get("channel") or "")] += 1
    return {
        "items": len(records),
        "answers_with_writer": len(raw),
        "answers_replaced_by_verify": replaced,
        "answers_replaced_count": len(replaced),
        "answer_chars_total": sum(stat["answer_chars"] for stat in raw),
        "paragraphs_total": paragraphs,
        "paragraphs_with_citation": sum(
            1 for stat in raw for part in stat["paragraphs"] if part["citations"] > 0
        ),
        "paragraph_citation_rate": round(
            sum(1 for stat in raw for part in stat["paragraphs"] if part["citations"] > 0)
            / paragraphs, 4
        ) if paragraphs else 0.0,
        "sentences_total": sentences,
        "sentences_with_citation": cited_sentences,
        "sentence_citation_rate": round(cited_sentences / sentences, 4) if sentences else 0.0,
        "uncited_sentences": len(uncited_sentences),
        "answers_with_uncited_sentences": flagged,
        "citations_total": total_citations,
        "citations_per_answer": round(total_citations / len(raw), 2) if raw else 0.0,
        "citations_per_paragraph": round(total_citations / paragraphs, 2) if paragraphs else 0.0,
        "long_paragraphs_with_at_most_one_citation": len(long_single),
        "long_paragraph_share": round(len(long_single) / paragraphs, 4) if paragraphs else 0.0,
        "uncited_candidate_paragraphs": len(uncited),
        "invalid_tokens_in_raw": invalid_raw,
        "invalid_tokens_in_returned": invalid_returned,
        "evidence_total": sum(stat["evidence_count"] for stat in raw),
        "evidence_uncited": sum(len(stat["uncited_evidence"]) for stat in raw),
        "evidence_by_channel": dict(channel_hits),
        "stopped_reasons": dict(Counter(r["stopped_reason"] or "(none)" for r in records)),
        "cost_total": round(
            sum(float((r["cost"] or {}).get("total") or 0) for r in records), 6
        ),
        "cost_priced": all(bool((r["cost"] or {}).get("priced")) for r in records),
    }


def render_audit(records: list[dict], stats: dict) -> str:
    lines = [
        "# 引用取证：逐条对齐表",
        "",
        "本文件由 `python -m eval.citation_audit` 生成，供人工判定「证据是否支撑那句话」。",
        "统计只负责把可疑位置摆出来，支撑关系必须人读。",
        "",
        "## 汇总数字",
        "",
        f"- 条目：{stats['items']}（写作器实际产出 {stats['answers_with_writer']} 条）",
        f"- verify 整段替换：{stats['answers_replaced_count']} 条"
        f"（{', '.join(stats['answers_replaced_by_verify']) or '无'}）",
        f"- 段落 {stats['paragraphs_total']} 段，含引用 {stats['paragraphs_with_citation']} 段"
        f"（{stats['paragraph_citation_rate']:.1%}）",
        f"- 句子 {stats['sentences_total']} 句，含引用 {stats['sentences_with_citation']} 句"
        f"（{stats['sentence_citation_rate']:.1%}）；无引用且 ≥{CLAIM_PARAGRAPH_CHARS} 字的句子"
        f" {stats['uncited_sentences']} 句，分布在 {stats['answers_with_uncited_sentences'] or '无'}",
        f"- 引用 {stats['citations_total']} 处，平均每条回答 {stats['citations_per_answer']} 处、"
        f"每段 {stats['citations_per_paragraph']} 处",
        f"- 长段（≥{LONG_PARAGRAPH_CHARS} 字）且 ≤1 处引用："
        f"{stats['long_paragraphs_with_at_most_one_citation']} 段"
        f"（占全部段落 {stats['long_paragraph_share']:.1%}）",
        f"- 可能「陈述了事实却没标来源」的段落（≥{CLAIM_PARAGRAPH_CHARS} 字、0 引用）："
        f"{stats['uncited_candidate_paragraphs']} 段",
        f"- 越界编号：模型原文 {stats['invalid_tokens_in_raw']} 处，"
        f"接口返回 {stats['invalid_tokens_in_returned']} 处",
        f"- 证据 {stats['evidence_total']} 条，从未被引用 {stats['evidence_uncited']} 条",
        f"- 渠道分布：{stats['evidence_by_channel']}",
        "",
    ]

    for record in records:
        stat = record["raw_stats"]
        lines += [
            f"## {record['id']}（{record['channel']}）",
            "",
            f"**问题**：{record['query']}",
            "",
            f"- 停止原因：`{record['stopped_reason'] or '(none)'}`；"
            f"耗时 {record['duration_ms']} ms；证据 {stat['evidence_count']} 条",
            f"- 写作器原文 {stat['answer_chars']} 字，"
            f"{len(stat['paragraphs'])} 段，引用 {len(stat['citation_tokens'])} 处；"
            f"越界 {stat['invalid_tokens'] or '无'}",
            "",
            "**接口实际返回**",
            "",
            "```text",
            record["answer"] or "（空）",
            "```",
            "",
        ]
        if record["writer_called"] and record["raw_answer"] != record["answer"]:
            lines += [
                "**写作器原始输出（verify 之前）**",
                "",
                "```text",
                record["raw_answer"],
                "```",
                "",
            ]

        lines += [
            "**证据（写作器看到的编号）**",
            "",
            "| n | 渠道 | ref | 标题 | 原文（截断） |",
            "|---|---|---|---|---|",
        ]
        for entry in record["evidence"]:
            text = str(entry.get("text") or "").replace("|", "/").replace("\n", " ")
            lines.append(
                f"| {entry.get('n')} | {entry.get('channel')} | {entry.get('ref')} | "
                f"{str(entry.get('title') or '').replace('|', '/')} | {text} |"
            )
        lines.append("")
        lines += [
            "**引用逐条对齐**",
            "",
            "| 段 | 编号 | 原句 | 指向证据 |",
            "|---|---|---|---|",
        ]
        for citation in stat["citations"]:
            sentence = citation["sentence"].replace("|", "/")
            target = (
                f"`{citation['evidence_ref']}` {citation['evidence_title']}"
                if citation["in_range"]
                else "**越界**"
            )
            lines.append(
                f"| {citation['paragraph']} | [{citation['token']}] | {sentence} | {target} |"
            )
        if not stat["citations"]:
            lines.append("| - | - | （本条没有引用） | - |")
        lines.append("")

        if stat["uncited_paragraphs"]:
            lines += ["**无引用的段落（候选：陈述了事实却没标来源）**", ""]
            for part in stat["uncited_paragraphs"]:
                lines.append(f"- 第 {part['index']} 段（{part['chars']} 字）：{part['text']}")
            lines.append("")
        if stat["uncited_sentences"]:
            lines += ["**无引用的句子（≥15 字，句级覆盖的候选）**", ""]
            for sentence in stat["uncited_sentences"]:
                lines.append(f"- 第 {sentence['paragraph']} 段（{sentence['chars']} 字）：{sentence['text']}")
            lines.append("")
        if stat["uncited_evidence"]:
            lines += [
                f"**未被引用的证据编号**：{', '.join(map(str, stat['uncited_evidence']))}",
                "",
            ]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="引用取证：真实链路抽样 + 引用统计")
    parser.add_argument("--golden", default=str(ROOT / "eval" / "golden.jsonl"))
    parser.add_argument("--ids", default=",".join(DEFAULT_IDS), help="逗号分隔的金标 id")
    parser.add_argument("--out", default=str(ROOT / "eval" / "forensics"))
    parser.add_argument(
        "--from-raw",
        default="",
        help="不跑模型，直接从已有的 raw.json 重算统计（改统计口径时用，零成本）",
    )
    args = parser.parse_args(argv)

    if args.from_raw:
        raw_path = Path(args.from_raw)
        records = json.loads(raw_path.read_text(encoding="utf-8"))
        for record in records:
            record["raw_stats"] = analyse_answer(record["raw_answer"], record["evidence"])
            record["returned_stats"] = analyse_answer(record["answer"], record["evidence"])
        stats = aggregate(records)
        out_dir = raw_path.parent
        (out_dir / "stats.json").write_text(
            json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (out_dir / "audit.md").write_text(render_audit(records, stats), encoding="utf-8")
        print(f"重算完成：{out_dir}")
        print(json.dumps(stats, ensure_ascii=False, indent=2))
        return 0

    golden_path = Path(args.golden)
    golden = load_golden(golden_path)
    ids = [item.strip() for item in args.ids.split(",") if item.strip()]
    missing = [item_id for item_id in ids if item_id not in golden]
    if missing:
        raise SystemExit(f"金标集里没有：{', '.join(missing)}")

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = Path(args.out) / stamp
    out_dir.mkdir(parents=True, exist_ok=True)

    records: list[dict] = []
    for index, item_id in enumerate(ids, start=1):
        item = golden[item_id]
        print(f"[{index}/{len(ids)}] {item_id}：{item['query']}", flush=True)
        record = run_item(item, golden_path)
        records.append(record)
        (out_dir / "raw.json").write_text(
            json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(
            f"    → {record['duration_ms']} ms，证据 {len(record['evidence'])} 条，"
            f"引用 {len(record['raw_stats']['citation_tokens'])} 处，"
            f"停止原因 {record['stopped_reason'] or '(none)'}",
            flush=True,
        )

    stats = aggregate(records)
    (out_dir / "stats.json").write_text(
        json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (out_dir / "audit.md").write_text(render_audit(records, stats), encoding="utf-8")
    print(f"\n输出目录：{out_dir}")
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
