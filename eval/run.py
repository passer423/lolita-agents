"""RAG 检索评测：离线跑金标集，输出逐条结果、汇总表与配置对比。

一条命令：

    python -m eval.run

默认只跑核心通道（own / real / lolibrary），不联网、不调模型；实时通道
（xhs / weibo / web）在 `--online` 时才运行。

产出目录（默认 `eval/reports/<时间戳>/`）：

* `results.csv`   逐条结果（配置 × 问题 × 视图），可直接用 Excel 打开；
* `summary.csv`   汇总表（配置 × 视图 × 通道）；
* `summary.json`  机器可读的全量结果，供下次 `--compare` 对账；
* `report.md`     人读报告：汇总、配置间变好/变差的条目、跨运行 diff；
* `config_delta.csv` 同一次运行内各配置相对 baseline 的逐条变化。

跨运行对比：

    python -m eval.run --compare latest

`latest` 取 `--out` 目录下除本次外最新的一份 `summary.json`，逐条比较
baseline 的 MRR / hit@10 / 引用准确率，直接列出变好与变差的条目。

## 指标口径

* **recall@k / hit@k**：排在前 k 的证据里是否命中金标。每条问题只有一个目标
  通道，channel 视图只在该通道内部排序；merged 视图在合并后的完整证据列表里
  排序，用来比较合并策略。
* **MRR**：第一条相关证据的倒数排名；没有命中记 0。
* **引用准确率**：离线口径下按前 `--cite-k` 条证据生成引用（与未配模型时
  `graph._forced_answer` 的逐条列证据行为一致），被引用证据命中金标的比例。
  这是检索层能否把可支撑答案的证据送进引用位的直接度量；不是 LLM 自评。
  默认 `--cite-k 1`：只问「第一条被引用的证据是否真的支撑答案」。同时保留旧
  口径（固定前 3 条、ref 精确匹配）与「前 3 条 + ref 家族」两列，便于把口径
  差异与检索效果分开看。
* 相关性判定：优先看 `expected_refs`（chunk_id / lolibrary slug）。同一文档的
  不同切片（`xxx#0` / `xxx#1`）与同一系列的不同条目（slug 至少共享 3 段前缀）
  视为同一 ref 家族；没有固定 ref 的实时通道用 `support_terms` 是否出现在证据
  文本里。

## 边界

本脚本只读 `app/` 的公开召回入口（`channels.recall`、`bridge.search_real`），
不改业务逻辑；合并层对比复用 `graph._normalize`，保证 baseline 与线上行为一致。
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import bridge  # noqa: E402 - 先保证 ROOT 在 sys.path
from app.agent import channels  # noqa: E402
from app.agent.graph import _normalize  # noqa: E402 - 复用线上合并顺序
from app.config import settings as default_settings  # noqa: E402

METRIC_KS = (1, 3, 5, 10)
EVIDENCE_TEXT_LIMIT = 300
# 旧口径固定看前 3 条；保留它是为了在报告里和新口径并列对照，不再作为默认。
LEGACY_CITATION_K = 3
DEFAULT_CITE_K = 1
DEFAULT_OUT = ROOT / "eval" / "reports"

CONFIGS: dict[str, dict[str, Any]] = {
    # 线上默认：top_k=3、real 只保留向量路认可、合并层按证据效力排序。
    "baseline": {"top_k": 3, "real_filter": "semantic", "merge": "normalize"},
    "top_k1": {"top_k": 1, "real_filter": "semantic", "merge": "normalize"},
    "top_k5": {"top_k": 5, "real_filter": "semantic", "merge": "normalize"},
    "top_k10": {"top_k": 10, "real_filter": "semantic", "merge": "normalize"},
    # 对比 real 通道的过滤策略：semantic（线上）vs all（不过滤纯 bm25）。
    "real_all": {"top_k": 3, "real_filter": "all", "merge": "normalize"},
    # 对比合并层排序前后：none = 按通道召回顺序直接拼接；round_robin = 轮流取。
    "merge_none": {"top_k": 3, "real_filter": "semantic", "merge": "none"},
    "merge_round_robin": {"top_k": 3, "real_filter": "semantic", "merge": "round_robin"},
}

RESULT_FIELDS = [
    "config",
    "item_id",
    "channel",
    "view",
    "status",
    "reason",
    "query",
    "rank",
    *[f"hit@{k}" for k in METRIC_KS],
    "mrr",
    "citation_accuracy",
    "citation_accuracy@3",
    "citation_accuracy@3_family",
    "answer_supported",
    "cited_count",
    "expected_refs",
    "top_refs",
]

SUMMARY_FIELDS = [
    "config",
    "view",
    "channel",
    "n",
    "skipped",
    *[f"recall@{k}" for k in METRIC_KS],
    "mrr",
    "citation_accuracy",
    "citation_accuracy@3",
    "citation_accuracy@3_family",
    "answer_supported_rate",
]


def load_golden(path: Path) -> list[dict]:
    items: list[dict] = []
    seen: set[str] = set()
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"{path}:{lineno} 不是合法 JSON：{exc}") from exc
        item_id = str(item.get("id") or "")
        if not item_id or item_id in seen:
            raise SystemExit(f"{path}:{lineno} id 缺失或重复：{item_id!r}")
        seen.add(item_id)
        channel = item.get("channel")
        if channel not in channels.ALL_CHANNELS:
            raise SystemExit(f"{path}:{lineno} 未知通道：{channel!r}")
        if not str(item.get("query") or "").strip():
            raise SystemExit(f"{path}:{lineno} query 为空")
        refs = item.get("expected_refs") or []
        terms = item.get("support_terms") or []
        if not isinstance(refs, list) or not isinstance(terms, list):
            raise SystemExit(f"{path}:{lineno} expected_refs / support_terms 必须是列表")
        if not refs and not terms:
            raise SystemExit(f"{path}:{lineno} 既没有 expected_refs 也没有 support_terms")
        item["expected_refs"] = refs
        item["support_terms"] = terms
        items.append(item)
    if not items:
        raise SystemExit(f"{path} 里没有金标条目")
    return items


def clip(text: str) -> str:
    text = (text or "").strip()
    return text if len(text) <= EVIDENCE_TEXT_LIMIT else text[:EVIDENCE_TEXT_LIMIT] + "…"


def hit_evidence(channel: str, hit: dict) -> dict:
    """把上游 hit 转成与 channels._kb_evidence 同构的证据字典。"""
    return {
        "channel": channel,
        "source_kind": channels.CHANNEL_SOURCE_KIND[channel],
        "ref": hit.get("chunk_id") or "",
        "title": hit.get("author") or ("自有知识库" if channel == "own" else "过往网友讨论"),
        "text": clip(hit.get("text") or ""),
        "url": hit.get("url") or "",
        "score": hit.get("score"),
        "retriever": hit.get("retriever") or "",
    }


class Retriever:
    """带缓存的召回器：同一 (通道, 查询, 配置) 只跑一次，配置对比时复用。"""

    def __init__(self, *, online: bool) -> None:
        self.online = online
        self._cache: dict[tuple, tuple[list[dict], str]] = {}
        self._settings: dict[int, Any] = {}

    def settings_for(self, top_k: int):
        if top_k not in self._settings:
            self._settings[top_k] = default_settings.model_copy(update={"top_k": top_k})
        return self._settings[top_k]

    def recall(self, channel: str, query: str, config: dict[str, Any]) -> tuple[list[dict], str]:
        top_k = int(config["top_k"])
        real_filter = config["real_filter"] if channel == "real" else "semantic"
        # lolibrary 不读 top_k，缓存键里去掉它，避免同一份档案被重复扫描。
        cache_top_k = 0 if channel == "lolibrary" else top_k
        key = (channel, query, cache_top_k, real_filter)
        if key in self._cache:
            return self._cache[key]

        if channel in channels.SUPPLEMENTARY_CHANNELS and not self.online:
            result = ([], "offline：实时通道默认关闭，加 --online 才跑")
        else:
            result = self._recall_uncached(channel, query, top_k, real_filter)
        self._cache[key] = result
        return result

    def _recall_uncached(
        self, channel: str, query: str, top_k: int, real_filter: str
    ) -> tuple[list[dict], str]:
        app = self.settings_for(top_k)
        if channel == "real" and real_filter == "all":
            # 线上 real 通道会丢掉纯 bm25 命中；这里取原始结果，用于对比过滤策略。
            try:
                hits = bridge.search_real(query, app, top_k=top_k)
            except Exception as exc:  # noqa: BLE001 - 记录原因，不中断整轮
                return [], f"{type(exc).__name__}: {str(exc)[:160]}"
            return [hit_evidence("real", hit) for hit in hits], ""
        try:
            ctx = channels.RecallContext(settings=app)
            return channels.recall(channel, query, ctx), ""
        except channels.ChannelUnavailable as exc:
            return [], str(exc)


def merge_evidence(
    per_channel: dict[str, list[dict]],
    supplementary: list[dict],
    strategy: str,
) -> list[dict]:
    """按配置指定的策略合并证据。"""
    if strategy == "normalize":
        return _normalize(
            [*per_channel.get("own", []), *per_channel.get("real", []),
             *per_channel.get("lolibrary", []), *supplementary],
            tuple(item.get("channel") for item in supplementary),
        )
    if strategy == "none":
        merged = [*per_channel.get("own", []), *per_channel.get("real", []),
                  *per_channel.get("lolibrary", [])]
        return [*merged, *supplementary]
    # round_robin：核心通道轮流取一条，补查证据仍放末尾（与 none 保持可比）。
    lists = [per_channel.get(name, []) for name in channels.CORE_CHANNELS]
    merged: list[dict] = []
    index = 0
    while any(index < len(items) for items in lists):
        for items in lists:
            if index < len(items):
                merged.append(items[index])
        index += 1
    return [*merged, *supplementary]


def _ref_doc(ref: str) -> str:
    """ref 的「文档」部分：chunk id 去掉 ``#n``，lolibrary slug 原样返回。"""
    return str(ref or "").split("#", 1)[0]


def _ref_tokens(ref: str) -> list[str]:
    return [token for token in _ref_doc(ref).split("-") if token]


def same_ref_family(expected: str, candidate: str) -> bool:
    """同一文档的不同切片，或同一系列的不同条目。

    两种形态分别处理：

    * chunk ref ``4580969816785987#0`` / ``#1``：``#`` 前是文档 id，相等即同一份
      资料，属于同一个 ref 家族；
    * lolibrary slug ``iw-white-lily-op-2026`` / ``iw-white-lily-op-world-edition``：
      没有 ``#`` 分段，用 slug 前缀判断系列——至少共享 3 段（``iw-white-lily-op``）
      才算同系列。只共享品牌段（``ap-``）不算，否则会把同品牌不同款误判成命中。
    """
    expected_doc, candidate_doc = _ref_doc(expected), _ref_doc(candidate)
    if not expected_doc or not candidate_doc:
        return False
    if expected_doc == candidate_doc:
        return True
    expected_tokens, candidate_tokens = _ref_tokens(expected_doc), _ref_tokens(candidate_doc)
    common = 0
    for expected_token, candidate_token in zip(expected_tokens, candidate_tokens, strict=False):
        if expected_token != candidate_token:
            break
        common += 1
    return common >= 3


def is_relevant(item: dict, evidence: dict, *, family: bool = False) -> bool:
    """证据是否命中金标；``family=True`` 时同文档切片 / 同系列条目也算命中。"""
    refs = set(item["expected_refs"])
    if refs:
        ref = str(evidence.get("ref") or "")
        if ref in refs:
            return True
        return family and any(same_ref_family(expected, ref) for expected in refs)
    haystack = f"{evidence.get('title', '')} {evidence.get('text', '')}".lower()
    return any(term.lower() in haystack for term in item["support_terms"])


def score_listing(item: dict, ranked: list[dict], cite_k: int = DEFAULT_CITE_K) -> dict[str, Any]:
    first_rank: int | None = None
    for rank, evidence in enumerate(ranked, start=1):
        if is_relevant(item, evidence):
            first_rank = rank
            break
    # 新口径：只引用前 cite_k 条，同文档/同系列算命中。
    cited = ranked[:cite_k]
    supported = sum(1 for evidence in cited if is_relevant(item, evidence, family=True))
    # 旧口径的对照列：固定前 3 条，ref 必须精确相等。
    legacy_cited = ranked[:LEGACY_CITATION_K]
    legacy_supported = sum(1 for evidence in legacy_cited if is_relevant(item, evidence))
    family_supported = sum(
        1 for evidence in legacy_cited if is_relevant(item, evidence, family=True)
    )
    return {
        "rank": first_rank,
        **{f"hit@{k}": int(first_rank is not None and first_rank <= k) for k in METRIC_KS},
        "mrr": (1.0 / first_rank) if first_rank else 0.0,
        "citation_accuracy": (supported / len(cited)) if cited else 0.0,
        "citation_accuracy@3": (legacy_supported / len(legacy_cited)) if legacy_cited else 0.0,
        "citation_accuracy@3_family": (
            family_supported / len(legacy_cited) if legacy_cited else 0.0
        ),
        "answer_supported": int(supported > 0),
        "cited_count": len(cited),
        "top_refs": " | ".join(str(e.get("ref") or "") for e in ranked[:10]),
    }


def run_config(
    items: list[dict],
    config_name: str,
    config: dict[str, Any],
    retriever: Retriever,
    cite_k: int = DEFAULT_CITE_K,
) -> list[dict]:
    rows: list[dict] = []
    for item in items:
        target = item["channel"]
        supplementary: list[dict] = []
        # 实时通道离线/不可用时，channel 与 merged 两个视图都应记为跳过：
        # 没有目标通道的证据，用其它通道的结果评这条问题没有意义。
        if target in channels.SUPPLEMENTARY_CHANNELS:
            supplementary, supplementary_error = retriever.recall(target, item["query"], config)
            if supplementary_error:
                rows.append(_skipped_row(config_name, item, "channel", supplementary_error))
                rows.append(_skipped_row(config_name, item, "merged", supplementary_error))
                continue

        per_channel: dict[str, list[dict]] = {}
        errors: dict[str, str] = {}
        for name in channels.CORE_CHANNELS:
            evidence, error = retriever.recall(name, item["query"], config)
            per_channel[name] = evidence
            if error:
                errors[name] = error

        merged = merge_evidence(per_channel, supplementary, config["merge"])

        # channel 视图：只看目标通道内部排序。
        if target in channels.SUPPLEMENTARY_CHANNELS:
            channel_listing = supplementary
            channel_error = ""
        else:
            channel_listing = per_channel.get(target, [])
            channel_error = errors.get(target, "")
        if channel_error:
            channel_row = _skipped_row(config_name, item, "channel", channel_error)
        else:
            channel_row = _scored_row(config_name, item, "channel", channel_listing, cite_k)
        rows.append(channel_row)

        # merged 视图：核心通道全挂才算跳过；单通道失败仍可评估其余证据。
        if not merged and len(errors) == len(channels.CORE_CHANNELS):
            merged_error = "所有核心通道都不可用：" + "；".join(sorted(errors.values()))
            rows.append(_skipped_row(config_name, item, "merged", merged_error))
        else:
            rows.append(_scored_row(config_name, item, "merged", merged, cite_k))
    return rows


def _scored_row(
    config_name: str, item: dict, view: str, ranked: list[dict], cite_k: int = DEFAULT_CITE_K
) -> dict:
    return {
        "config": config_name,
        "item_id": item["id"],
        "channel": item["channel"],
        "view": view,
        "status": "ok",
        "reason": "",
        "query": item["query"],
        **score_listing(item, ranked, cite_k),
        "expected_refs": " | ".join(item["expected_refs"]),
    }


def _skipped_row(config_name: str, item: dict, view: str, reason: str) -> dict:
    return {
        "config": config_name,
        "item_id": item["id"],
        "channel": item["channel"],
        "view": view,
        "status": "skipped",
        "reason": reason,
        "query": item["query"],
        "rank": "",
        **{f"hit@{k}": "" for k in METRIC_KS},
        "mrr": "",
        "citation_accuracy": "",
        "citation_accuracy@3": "",
        "citation_accuracy@3_family": "",
        "answer_supported": "",
        "cited_count": "",
        "expected_refs": " | ".join(item["expected_refs"]),
        "top_refs": "",
    }


def summarize(rows: list[dict], configs: list[str]) -> list[dict]:
    grouped: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    for row in rows:
        grouped[(row["config"], row["view"], "*")].append(row)
        if row["view"] == "channel":
            grouped[(row["config"], row["view"], row["channel"])].append(row)

    summary: list[dict] = []
    for config in configs:
        keys = sorted(
            (key for key in grouped if key[0] == config),
            key=lambda key: (key[1], key[2]),
        )
        for key in keys:
            group = grouped[key]
            evaluated = [row for row in group if row["status"] == "ok"]
            summary.append(
                {
                    "config": config,
                    "view": key[1],
                    "channel": key[2],
                    "n": len(evaluated),
                    "skipped": len(group) - len(evaluated),
                    **{
                        f"recall@{k}": _mean(evaluated, f"hit@{k}")
                        for k in METRIC_KS
                    },
                    "mrr": _mean(evaluated, "mrr"),
                    "citation_accuracy": _mean(evaluated, "citation_accuracy"),
                    "citation_accuracy@3": _mean(evaluated, "citation_accuracy@3"),
                    "citation_accuracy@3_family": _mean(
                        evaluated, "citation_accuracy@3_family"
                    ),
                    "answer_supported_rate": _mean(evaluated, "answer_supported"),
                }
            )
    return summary


def _mean(rows: list[dict], field: str) -> float:
    values = [float(row[field]) for row in rows if row[field] != ""]
    return (sum(values) / len(values)) if values else 0.0


def config_deltas(rows: list[dict], configs: list[str]) -> list[dict]:
    base = {(row["item_id"], row["view"]): row for row in rows if row["config"] == "baseline"}
    deltas: list[dict] = []
    for config in configs:
        if config == "baseline":
            continue
        current = {(row["item_id"], row["view"]): row for row in rows if row["config"] == config}
        for key in sorted(set(base) & set(current)):
            old, new = base[key], current[key]
            delta_mrr = _as_float(new["mrr"]) - _as_float(old["mrr"])
            delta_hit = _as_float(new["hit@10"]) - _as_float(old["hit@10"])
            delta_cite = _as_float(new["citation_accuracy"]) - _as_float(
                old["citation_accuracy"]
            )
            deltas.append(
                {
                    "config": config,
                    "item_id": key[0],
                    "view": key[1],
                    "channel": new["channel"],
                    "old_rank": old["rank"],
                    "new_rank": new["rank"],
                    "delta_mrr": delta_mrr,
                    "delta_hit@10": delta_hit,
                    "delta_citation_accuracy": delta_cite,
                    "direction": _classify(delta_hit, delta_mrr, delta_cite),
                }
            )
    return deltas


def _classify(delta_hit: float, delta_mrr: float, delta_cite: float) -> str:
    """先看召回排名，只有排名不变时才归因到引用准确率。"""
    rank_change = delta_hit * 10 + delta_mrr
    if rank_change > 1e-9:
        return "improved"
    if rank_change < -1e-9:
        return "regressed"
    if delta_cite > 1e-9:
        return "citation_improved"
    if delta_cite < -1e-9:
        return "citation_regressed"
    return "same"


def _as_float(value: Any) -> float:
    return float(value) if value not in ("", None) else 0.0


def load_previous(path_arg: str, out_root: Path, current_dir: Path) -> Path | None:
    if not path_arg:
        return None
    if path_arg != "latest":
        target = Path(path_arg)
        return target if target.exists() else None
    candidates = sorted(
        (path for path in out_root.glob("*/summary.json") if path.parent != current_dir),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    return candidates[0] if candidates else None


def _is_legacy_report(previous: dict) -> bool:
    """口径变更前的报告：只有 citation_accuracy，没有 citation_accuracy@3。"""
    rows = previous.get("results") or []
    return bool(rows) and "citation_accuracy@3" not in rows[0]


def cross_run_diff(previous: dict, current_results: list[dict]) -> list[dict]:
    old_rows = previous.get("results") or []
    # 口径变更前的报告只有 citation_accuracy，那个值就是旧口径（前 3 条、精确匹配）；
    # 新报告另存 citation_accuracy@3。两边先对齐到同一口径，避免把口径差异当成效果变化。
    previous_legacy = _is_legacy_report(previous)
    old = {
        (row["item_id"], row["view"]): row
        for row in old_rows
        if row.get("config") == "baseline"
    }
    new = {
        (row["item_id"], row["view"]): row
        for row in current_results
        if row.get("config") == "baseline"
    }
    diffs: list[dict] = []
    for key in sorted(set(old) & set(new)):
        before, after = old[key], new[key]
        delta_mrr = _as_float(after["mrr"]) - _as_float(before["mrr"])
        delta_hit = _as_float(after["hit@10"]) - _as_float(before["hit@10"])
        after_cite = after["citation_accuracy@3" if previous_legacy else "citation_accuracy"]
        delta_cite = _as_float(after_cite) - _as_float(before["citation_accuracy"])
        if before["status"] != after["status"]:
            direction = "status_changed"
        else:
            direction = _classify(delta_hit, delta_mrr, delta_cite)
        diffs.append(
            {
                "item_id": key[0],
                "view": key[1],
                "channel": after["channel"],
                "old_status": before["status"],
                "new_status": after["status"],
                "old_rank": before["rank"],
                "new_rank": after["rank"],
                "old_mrr": before["mrr"],
                "new_mrr": after["mrr"],
                "delta_mrr": delta_mrr,
                "delta_hit@10": delta_hit,
                "delta_citation_accuracy": delta_cite,
                "direction": direction,
            }
        )
    return diffs


def write_csv(path: Path, rows: list[dict], fields: list[str]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    field: (f"{row[field]:.4f}" if isinstance(row.get(field), float) else row.get(field, ""))
                    for field in fields
                }
            )


def fmt(value: Any, digits: int = 3) -> str:
    if value in ("", None):
        return "-"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="lolita-agents RAG 检索评测")
    parser.add_argument("--golden", default=str(ROOT / "eval" / "golden.jsonl"))
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="报告根目录")
    parser.add_argument(
        "--configs",
        default=",".join(CONFIGS),
        help="要跑的配置，逗号分隔；可选：" + ", ".join(CONFIGS),
    )
    parser.add_argument(
        "--cite-k",
        type=int,
        default=DEFAULT_CITE_K,
        help=f"引用准确率只看前 k 条证据（默认 {DEFAULT_CITE_K}）",
    )
    parser.add_argument("--online", action="store_true", help="同时跑 xhs / weibo / web（联网）")
    parser.add_argument(
        "--compare",
        default="",
        help="对比历史 summary.json；传 latest 自动找 --out 下最新一份",
    )
    args = parser.parse_args(argv)

    configs = [name.strip() for name in args.configs.split(",") if name.strip()]
    unknown = [name for name in configs if name not in CONFIGS]
    if unknown:
        raise SystemExit(f"未知配置：{', '.join(unknown)}；可选：{', '.join(CONFIGS)}")
    if "baseline" not in configs:
        configs.insert(0, "baseline")
    if args.cite_k < 1:
        raise SystemExit("--cite-k 必须 >= 1")

    golden_path = Path(args.golden)
    items = load_golden(golden_path)
    retriever = Retriever(online=args.online)

    generated_at = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_root = Path(args.out)
    out_dir = out_root / generated_at
    out_dir.mkdir(parents=True, exist_ok=True)

    all_rows: list[dict] = []
    for config in configs:
        print(f"跑配置 {config} ...")
        all_rows.extend(run_config(items, config, CONFIGS[config], retriever, args.cite_k))

    summary = summarize(all_rows, configs)
    deltas = config_deltas(all_rows, configs)

    previous_path = load_previous(args.compare, out_root, out_dir)
    previous = (
        json.loads(previous_path.read_text(encoding="utf-8")) if previous_path else {}
    )
    diffs = cross_run_diff(previous, all_rows) if previous else []

    write_csv(out_dir / "results.csv", all_rows, RESULT_FIELDS)
    write_csv(out_dir / "summary.csv", summary, SUMMARY_FIELDS)
    write_csv(out_dir / "config_delta.csv", deltas, [
        "config", "item_id", "view", "channel", "old_rank", "new_rank",
        "delta_mrr", "delta_hit@10", "delta_citation_accuracy", "direction",
    ])
    if diffs:
        write_csv(out_dir / "diff.csv", diffs, [
            "item_id", "view", "channel", "old_status", "new_status",
            "old_rank", "new_rank", "old_mrr", "new_mrr",
            "delta_mrr", "delta_hit@10", "delta_citation_accuracy", "direction",
        ])

    payload = {
        "generated_at": generated_at,
        "golden_path": str(golden_path),
        "golden_items": len(items),
        "online": args.online,
        "citation_k": args.cite_k,
        "citation_k_legacy": LEGACY_CITATION_K,
        "configs": {name: CONFIGS[name] for name in configs},
        "summary": summary,
        "results": all_rows,
        "config_deltas": deltas,
        "previous_report": str(previous_path) if previous_path else "",
        "cross_run_diff": diffs,
    }
    (out_dir / "summary.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    report = _render_report(
        golden_path=golden_path,
        items=items,
        configs=configs,
        rows=all_rows,
        summary=summary,
        deltas=deltas,
        diffs=diffs,
        previous_path=previous_path,
        online=args.online,
        generated_at=generated_at,
        cite_k=args.cite_k,
        previous_legacy=_is_legacy_report(previous),
    )
    (out_dir / "report.md").write_text(report, encoding="utf-8")

    print()
    for row in summary:
        if row["view"] == "merged" and row["channel"] == "*":
            print(
                f"{row['config']:<18} n={row['n']:<3} skip={row['skipped']:<3} "
                f"recall@1={row['recall@1']:.3f} recall@10={row['recall@10']:.3f} "
                f"MRR={row['mrr']:.3f} cite@{args.cite_k}={row['citation_accuracy']:.3f} "
                f"(旧口径 cite@3={row['citation_accuracy@3']:.3f})"
            )
    print(f"\n报告目录：{out_dir}")
    return 0


def _render_report(
    *,
    golden_path: Path,
    items: list[dict],
    configs: list[str],
    rows: list[dict],
    summary: list[dict],
    deltas: list[dict],
    diffs: list[dict],
    previous_path: Path | None,
    online: bool,
    generated_at: str,
    cite_k: int = DEFAULT_CITE_K,
    previous_legacy: bool = False,
) -> str:
    lines = [
        "# RAG 检索评测报告",
        "",
        f"- 生成时间：{generated_at}",
        f"- 金标集：`{golden_path}`（{len(items)} 条）",
        f"- 配置：{', '.join(configs)}",
        f"- 实时通道：{'开启（--online）' if online else '关闭（离线核心通道）'}",
        f"- 引用口径：新口径取前 {cite_k} 条证据、同文档/同系列 ref 家族算命中；"
        f"旧口径（前 {LEGACY_CITATION_K} 条、ref 精确匹配）保留为对照列",
        "",
        "## 汇总（merged 视图）",
        "",
        f"| 配置 | n | skipped | recall@1 | recall@3 | recall@5 | recall@10 | MRR | 引用@{cite_k} | 引用@3（旧口径） | 答案有支撑 |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for row in summary:
        if row["view"] != "merged" or row["channel"] != "*":
            continue
        lines.append(
            f"| {row['config']} | {row['n']} | {row['skipped']} | "
            f"{fmt(row['recall@1'])} | {fmt(row['recall@3'])} | {fmt(row['recall@5'])} | "
            f"{fmt(row['recall@10'])} | {fmt(row['mrr'])} | "
            f"{fmt(row['citation_accuracy'])} | {fmt(row['citation_accuracy@3'])} | "
            f"{fmt(row['answer_supported_rate'])} |"
        )

    lines += [
        "",
        "## baseline 分通道（channel 视图）",
        "",
        f"| 通道 | n | skipped | recall@1 | recall@3 | recall@5 | recall@10 | MRR | 引用@{cite_k} | 引用@3（旧口径） |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for row in summary:
        if row["config"] != "baseline" or row["view"] != "channel" or row["channel"] == "*":
            continue
        lines.append(
            f"| {row['channel']} | {row['n']} | {row['skipped']} | "
            f"{fmt(row['recall@1'])} | {fmt(row['recall@3'])} | {fmt(row['recall@5'])} | "
            f"{fmt(row['recall@10'])} | {fmt(row['mrr'])} | "
            f"{fmt(row['citation_accuracy'])} | {fmt(row['citation_accuracy@3'])} |"
        )

    lines += _citation_caliber_section(rows, summary, cite_k)

    lines += ["", "## 配置对比：哪些条目变好 / 变差（相对 baseline）", ""]
    for config in configs:
        if config == "baseline":
            continue
        changed = [row for row in deltas if row["config"] == config and row["direction"] != "same"]
        improved = [row for row in changed if row["direction"] == "improved"]
        regressed = [row for row in changed if row["direction"] == "regressed"]
        cite_improved = [row for row in changed if row["direction"] == "citation_improved"]
        cite_regressed = [row for row in changed if row["direction"] == "citation_regressed"]
        lines.append(
            f"- **{config}**：排名变好 {len(improved)}、排名变差 {len(regressed)}；"
            f"仅引用变好 {len(cite_improved)}、仅引用变差 {len(cite_regressed)}"
            + ("" if changed else "（无变化）")
        )
        if improved:
            lines.append(
                "  - 排名变好：" + "、".join(
                    f"{row['item_id']}[{row['view']}] {row['old_rank']}→{row['new_rank']}"
                    for row in improved[:20]
                )
            )
        if regressed:
            lines.append(
                "  - 排名变差：" + "、".join(
                    f"{row['item_id']}[{row['view']}] {row['old_rank']}→{row['new_rank']}"
                    for row in regressed[:20]
                )
            )
        if cite_improved:
            lines.append(
                "  - 仅引用变好：" + "、".join(
                    f"{row['item_id']}[{row['view']}]" for row in cite_improved[:20]
                )
            )
        if cite_regressed:
            lines.append(
                "  - 仅引用变差：" + "、".join(
                    f"{row['item_id']}[{row['view']}]" for row in cite_regressed[:20]
                )
            )

    lines += ["", "## 跨运行对比", ""]
    if previous_path is None:
        lines.append("未指定 `--compare`（或找不到可对比的历史报告），本次不生成跨运行 diff。")
    else:
        lines.append(f"对比基线：`{previous_path}`（仅 baseline 配置）")
        if previous_legacy:
            lines.append(
                "对比基线生成于引用口径变更前：引用列已按旧口径（cite@3、精确匹配）"
                "逐条对齐，避免把口径差异当成检索变化。"
            )
        lines.append("")
        improved = [row for row in diffs if row["direction"] == "improved"]
        regressed = [row for row in diffs if row["direction"] == "regressed"]
        cite_improved = [row for row in diffs if row["direction"] == "citation_improved"]
        cite_regressed = [row for row in diffs if row["direction"] == "citation_regressed"]
        status_changed = [row for row in diffs if row["direction"] == "status_changed"]
        lines.append(
            f"- 排名变好：{len(improved)} 条；排名变差：{len(regressed)} 条；"
            f"仅引用变好：{len(cite_improved)} 条；仅引用变差：{len(cite_regressed)} 条"
        )
        if improved:
            lines.append(
                "  - 变好：" + "、".join(
                    f"{row['item_id']}[{row['view']}] rank {row['old_rank']}→{row['new_rank']}"
                    for row in improved[:30]
                )
            )
        if regressed:
            lines.append(
                "  - 变差：" + "、".join(
                    f"{row['item_id']}[{row['view']}] rank {row['old_rank']}→{row['new_rank']}"
                    for row in regressed[:30]
                )
            )
        if status_changed:
            lines.append(
                "- 状态变化：" + "、".join(
                    f"{row['item_id']} {row['old_status']}→{row['new_status']}"
                    for row in status_changed[:30]
                )
            )

    lines += [
        "",
        "## 逐条结果矩阵（baseline rank）",
        "",
        "| 条目 | 通道 | 问题 | channel rank | merged rank | channel hit@10 | merged hit@10 |",
        "|---|---|---|---|---|---|---|",
    ]
    baseline = [row for row in rows if row["config"] == "baseline"]
    by_key = {(row["item_id"], row["view"]): row for row in baseline}
    for item in items:
        channel_row = by_key.get((item["id"], "channel"), {})
        merged_row = by_key.get((item["id"], "merged"), {})
        lines.append(
            f"| {item['id']} | {item['channel']} | {item['query']} | "
            f"{_rank_text(channel_row)} | {_rank_text(merged_row)} | "
            f"{_hit_text(channel_row)} | {_hit_text(merged_row)} |"
        )

    skipped = [row for row in rows if row["status"] == "skipped"]
    if skipped:
        lines += ["", "## 跳过项", "", "| 配置 | 条目 | 视图 | 原因 |", "|---|---|---|---|"]
        for row in skipped:
            reason = str(row["reason"]).replace("|", "/")
            lines.append(f"| {row['config']} | {row['item_id']} | {row['view']} | {reason} |")

    lines += [
        "",
        "## 文件",
        "",
        "- `results.csv`：逐条结果（配置 × 问题 × 视图）",
        "- `summary.csv`：汇总指标",
        "- `config_delta.csv`：各配置相对 baseline 的逐条变化",
        "- `diff.csv`：跨运行逐条变化（仅 `--compare` 时生成）",
        "- `summary.json`：全量机器可读结果",
        "",
    ]
    return "\n".join(lines)


def _citation_caliber_section(rows: list[dict], summary: list[dict], cite_k: int) -> list[str]:
    """把旧口径、只换家族匹配、新默认口径三种数字并排，并拆开差异来源。"""
    lines = [
        "",
        "## 引用口径变化（baseline）",
        "",
        "三种口径的区别只在「引用窗口」和「ref 是否允许家族匹配」两处：",
        "",
        f"- **旧口径 `cite@{LEGACY_CITATION_K}`**：固定取前 {LEGACY_CITATION_K} 条证据，"
        "ref 必须与金标完全相等。上一版报告用的是这一口径。",
        f"- **`cite@{LEGACY_CITATION_K}+家族`**：窗口不变，但同一文档的其它切片"
        "（`xxx#1`）与同系列条目（slug 至少共享 3 段前缀）算命中。",
        f"- **新口径 `cite@{cite_k}`**：窗口收到前 {cite_k} 条，家族匹配保留（当前默认）。",
        "",
        f"| 视图 | n | 旧口径 cite@{LEGACY_CITATION_K} | "
        f"cite@{LEGACY_CITATION_K}+家族 | 新口径 cite@{cite_k} | 旧→新 |",
        "|---|---|---|---|---|---|",
    ]
    base = {
        (row["view"], row["channel"]): row
        for row in summary
        if row["config"] == "baseline"
    }
    for view, channel in (("merged", "*"), ("channel", "*")):
        row = base.get((view, channel))
        if not row or not row["n"]:
            continue
        label = "merged" if view == "merged" else "channel（全部通道）"
        lines.append(
            f"| {label} | {row['n']} | {fmt(row['citation_accuracy@3'])} | "
            f"{fmt(row['citation_accuracy@3_family'])} | {fmt(row['citation_accuracy'])} | "
            f"{row['citation_accuracy'] - row['citation_accuracy@3']:+.3f} |"
        )

    merged_row = base.get(("merged", "*"))
    if merged_row and merged_row["n"]:
        window_gain = merged_row["citation_accuracy"] - merged_row["citation_accuracy@3_family"]
        family_gain = (
            merged_row["citation_accuracy@3_family"] - merged_row["citation_accuracy@3"]
        )
        lines += [
            "",
            "差异来源（merged）：",
            "",
            f"- **引用窗口 {LEGACY_CITATION_K} → {cite_k}**："
            f"{merged_row['citation_accuracy@3_family']:.3f} → "
            f"{merged_row['citation_accuracy']:.3f}（{window_gain:+.3f}）。"
            f"旧口径下引用 3 条证据，只有 1 条命中金标时仍记 1/3；"
            f"新口径只问「第 1 条被引用的证据是否成立」。",
            f"- **ref 家族匹配**：{merged_row['citation_accuracy@3']:.3f} → "
            f"{merged_row['citation_accuracy@3_family']:.3f}（{family_gain:+.3f}）。"
            "同一文档的相邻切片、同系列档案从此不再算成引用错误。",
        ]

    changed = [
        row
        for row in rows
        if row["config"] == "baseline"
        and row["view"] == "merged"
        and row["status"] == "ok"
        and _as_float(row["citation_accuracy"]) != _as_float(row["citation_accuracy@3"])
    ]
    if changed:
        lines += [
            "",
            f"逐条变化（baseline merged，新口径与旧口径不同的条目，共 {len(changed)} 条）：",
            "",
            f"| 条目 | 旧 cite@{LEGACY_CITATION_K} | 新 cite@{cite_k} | "
            "金标 ref | 前 3 条证据 ref |",
            "|---|---|---|---|---|",
        ]
        for row in changed:
            top3 = " / ".join(str(row["top_refs"]).split(" | ")[:3])
            lines.append(
                f"| {row['item_id']} | {fmt(row['citation_accuracy@3'])} | "
                f"{fmt(row['citation_accuracy'])} | {row['expected_refs'] or '-'} | {top3} |"
            )
    return lines


def _rank_text(row: dict) -> str:
    if not row:
        return "-"
    if row["status"] == "skipped":
        return "skip"
    return str(row["rank"]) if row["rank"] not in ("", None) else "miss"


def _hit_text(row: dict) -> str:
    if not row or row["status"] == "skipped":
        return "-"
    return str(row["hit@10"])


if __name__ == "__main__":
    raise SystemExit(main())
