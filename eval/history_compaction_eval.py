"""历史压缩前后的 token 对比：同一组真实多轮对话，逐个注入点统计。

用法::

    python eval/history_compaction_eval.py                # 用内置的 6 轮实录
    python eval/history_compaction_eval.py --turns x.json # 用外部 {"turns": [...]} 实录

统计对象是**真正发给模型的提示词**，不是摘要本身：

* ``query``  —— planner.build_query_prompt
* ``expand`` —— planner.build_expand_prompt
* ``dress``  —— planner.build_dress_prompt
* ``writer`` —— writer.build_synth_messages（证据留空，只比历史差异）

token 用本地 ``BAAI/bge-small-zh-v1.5`` 分词器真实计数；模型不在本地缓存时
直接报错，不拿字符数冒充 token。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.agent import history_memory, planner  # noqa: E402
from app.agent.writer import (  # noqa: E402
    HISTORY_FETCH_MESSAGES,
    build_synth_messages,
    sanitize_history,
)
from app.config import settings  # noqa: E402

#: 2026-11-23 在 8787 上真实跑出的 6 轮问答（问句 + 回答原文）。
_REAL_TURNS: list[dict] = [
    {
        "question": "Baby 的 Sweet Best Wishes 有哪些款式？",
        "answer": "Sweet Best Wishes 目前能查到的有两款 JSK（无袖连衣裙）：一款是 White / Gold 配色、货号 B50JS260，2026 年发售，发售价 ¥49280 JPY [1]；另一款是 sucker gingham 面料，货号 B50JS258，同样是 2026 年发售、原价 ¥49280 JPY，配色有 Black x White 和 Red x White [2]。",
    },
    {
        "question": "它一般什么价位？",
        "answer": "这条是网友反馈的掉色染色问题，不是价格信息，所以 Sweet Best Wishes 的价位我这边没查到。之前能确认的只有发售价：White / Gold 那款和 sucker gingham 那款都是 ¥49280 JPY。",
    },
    {
        "question": "那条有现货吗？",
        "answer": "有网友提到，黑金 Sweet Best Wishes JSK 可以点进抖店拿到现货[1]；另外，店铺页面写着「国内现货」的粉白、黑、黑边夹[2]。这两条都只是线索，具体有没有货，还得点进去看。",
    },
    {
        "question": "我想要粉色，预算 3000 以内",
        "answer": "粉色的话，lolibrary 上记录的 Sweet Best Wishes 只有 White / Gold 一个配色，发售价 ¥49280 JPY [1]，没有粉色款。3000 以内这个预算，这条裙子按发售价算也超了。",
    },
    {
        "question": "这个系列适合什么季节穿？",
        "answer": "这条是榛果可可泰迪的转发抽奖帖，有网友提到这次是长短款 JSK 和段段 SK，绒绒的质感很适合秋冬 [2]。不过这是网友的说法，不是官方对季节的说明。",
    },
    {
        "question": "按我刚才的偏好推荐一下",
        "answer": "你刚才说的偏好是粉色、预算 3000 以内，但这条证据里没有 Sweet Best Wishes 的配色、价格或现货信息，只有一段和裙子无关的网友闲聊，所以没法按这个偏好推荐。粉色款和 3000 以内的现货，我这边都没查到。",
    },
]


def _load_tokenizer():
    from transformers import AutoTokenizer

    try:
        return AutoTokenizer.from_pretrained(
            "BAAI/bge-small-zh-v1.5", local_files_only=True
        )
    except OSError as exc:  # pragma: no cover - 只在缺模型时触发
        raise SystemExit(
            "本地缓存里没有 BAAI/bge-small-zh-v1.5，无法做真实 token 计数："
            f"{exc}"
        ) from exc


def _history_at(turns: list[dict], index: int) -> list[dict]:
    history: list[dict] = []
    for turn in turns[:index]:
        history.append({"role": "user", "content": turn["question"]})
        history.append({"role": "assistant", "content": turn["answer"]})
    return history


def _prompts(question: str, history: list[dict]) -> dict[str, str]:
    writer_messages = build_synth_messages(question, [], history)
    return {
        "query": planner.build_query_prompt(question, history),
        "expand": planner.build_expand_prompt(question, history),
        "dress": planner.build_dress_prompt(question, history),
        "writer": "\n".join(message["content"] for message in writer_messages),
    }


def _history_blocks(history: list[dict]) -> dict[str, str]:
    """只取真正被注入的那段历史文本，排除固定提示词与系统消息。"""
    return {
        "query": planner.history_context(history, planner.QUERY_HISTORY_TURNS, "历史"),
        "expand": planner.history_context(history, planner.EXPAND_HISTORY_TURNS, "历史"),
        "dress": planner.history_context(history, planner.DRESS_HISTORY_TURNS, "历史"),
        "writer": "\n".join(
            turn["content"] for turn in sanitize_history(history)
        ),
    }


def _count(tokenizer, text: str) -> int:
    return len(tokenizer(text)["input_ids"])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--turns", type=Path, help="外部实录 JSON：{'turns': [...]}")
    args = parser.parse_args()
    turns = _REAL_TURNS
    if args.turns:
        turns = json.loads(args.turns.read_text(encoding="utf-8"))["turns"]

    tokenizer = _load_tokenizer()
    rows: list[dict] = []
    totals = {key: [0, 0] for key in ("query", "expand", "dress", "writer")}
    block_totals = {key: [0, 0] for key in ("query", "expand", "dress", "writer")}
    for index, turn in enumerate(turns):
        history = _history_at(turns, index)
        raw = sanitize_history(history, limit=HISTORY_FETCH_MESSAGES)
        compacted = history_memory.compress(raw, settings)
        used = compacted if compacted is not None else raw
        raw_prompts = _prompts(turn["question"], raw)
        compact_prompts = _prompts(turn["question"], used)
        raw_blocks = _history_blocks(raw)
        compact_blocks = _history_blocks(used)
        row = {
            "turn": index + 1,
            "triggered": compacted is not None,
            "raw_chars": sum(len(item["content"]) for item in raw),
            "compacted_chars": sum(len(item["content"]) for item in used),
            "points": {},
        }
        for key in ("query", "expand", "dress", "writer"):
            before = _count(tokenizer, raw_prompts[key])
            after = _count(tokenizer, compact_prompts[key])
            totals[key][0] += before
            totals[key][1] += after
            block_totals[key][0] += _count(tokenizer, raw_blocks[key])
            block_totals[key][1] += _count(tokenizer, compact_blocks[key])
            row["points"][key] = {"raw": before, "compacted": after}
        rows.append(row)

    print("轮次 触发 | query      | expand     | dress      | writer")
    print("-" * 66)
    for row in rows:
        cells = " | ".join(
            f"{point['raw']:4d}->{point['compacted']:<4d}"
            for point in row["points"].values()
        )
        print(
            f"{row['turn']:>3}   {'是' if row['triggered'] else '否'}  | {cells}"
            f"   (字符 {row['raw_chars']}->{row['compacted_chars']})"
        )
    print("-" * 66)
    for key, (before, after) in totals.items():
        saved = before - after
        ratio = f"{saved / before:.0%}" if before else "0%"
        print(f"合计 {key:<7} {before:5d} -> {after:5d}  token（省 {saved}，{ratio}）")
    total_before = sum(before for before, _ in totals.values())
    total_after = sum(after for _, after in totals.values())
    print(
        f"四处合计      {total_before:5d} -> {total_after:5d}  token"
        f"（省 {total_before - total_after}，"
        f"{(total_before - total_after) / total_before:.0%}）"
    )

    print()
    print("仅历史注入部分（不含固定提示词 / 系统消息）")
    print("-" * 66)
    for key, (before, after) in block_totals.items():
        saved = before - after
        ratio = f"{saved / before:.0%}" if before else "0%"
        print(f"合计 {key:<7} {before:5d} -> {after:5d}  token（省 {saved}，{ratio}）")
    block_before = sum(before for before, _ in block_totals.values())
    block_after = sum(after for _, after in block_totals.values())
    print(
        f"四处合计      {block_before:5d} -> {block_after:5d}  token"
        f"（省 {block_before - block_after}，"
        f"{(block_before - block_after) / block_before:.0%}）"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
