"""对话轨迹：一次 agent run 一行 JSONL。

为什么必须有：agent 的行为是**多步**的，只看最终答案无法回答"它为什么没找到"。
有了轨迹，才能回答这些问题：走了几步、调了哪个工具、参数是什么、结果空不空、
是模型自己收手还是被护栏强制结束。

两条纪律（和上游查询日志一致）：

* **写失败绝不影响回答**——轨迹是观测手段，不该成为故障源。
* **不写进上游的 ``queries.jsonl``**——那是唯一不受污染的"真实查询"信号源，
  agent 的内部检索灌进去会把上游的拒答率统计搞坏。
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import date, datetime, timedelta
from pathlib import Path

from app.config import AppSettings
from app.config import settings as default_settings

logger = logging.getLogger(__name__)

# 多个请求线程可能同时写轨迹；轮转是「查大小 + 改名」两步，必须串行。
_append_lock = threading.Lock()


def trace_path(app: AppSettings | None = None) -> Path:
    app = app or default_settings
    return app.data_dir / "logs" / "chat_runs.jsonl"


def _rotate(path: Path, max_bytes: int, keep_files: int) -> None:
    """当前文件超过 ``max_bytes`` 时轮转：``.1`` 最新，依次覆盖更旧的。

    ``keep_files`` 是归档数量；多余的旧档直接丢掉，否则「轮转」只是换个地方
    继续无限增长。任何 OSError 都交给调用方吞掉（观测失败不能影响回答）。
    """
    if max_bytes <= 0 or keep_files <= 0:
        return
    try:
        if path.stat().st_size < max_bytes:
            return
    except OSError:
        return

    for index in range(keep_files, 0, -1):
        source = path if index == 1 else path.with_name(f"{path.name}.{index - 1}")
        target = path.with_name(f"{path.name}.{index}")
        if source.exists():
            os.replace(source, target)
    # 以前 keep 设得更大时留下的归档，一并清掉
    path.with_name(f"{path.name}.{keep_files + 1}").unlink(missing_ok=True)


def append(record: dict, app: AppSettings | None = None) -> Path | None:
    """追加一行；必要时先轮转。任何异常都吞掉并打 warning。"""
    app = app or default_settings
    path = trace_path(app)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"ts": datetime.now().isoformat(timespec="seconds"), **record}
        with _append_lock:
            _rotate(path, app.trace_max_bytes, app.trace_keep_files)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
        return path
    except Exception as exc:  # noqa: BLE001 - 观测失败不能影响回答
        logger.warning("轨迹写入失败（不影响回答）：%s", exc)
        return None


def tail(limit: int = 20, app: AppSettings | None = None) -> list[dict]:
    """给排查用：读最近 N 条。"""
    path = trace_path(app)
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()[-limit:]
    records: list[dict] = []
    for line in lines:
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records


# --------------------------------------------------------------------- 成本汇总


def _trace_files(path: Path, app: AppSettings) -> list[Path]:
    """当前文件 + 还在保留的归档。轮转只是换文件，账不能跟着丢。"""
    files: list[Path] = []
    for index in range(max(int(app.trace_keep_files or 0), 0) + 1):
        candidate = path if index == 0 else path.with_name(f"{path.name}.{index}")
        if candidate.exists():
            files.append(candidate)
    return files


def _empty_bucket() -> dict:
    return {
        "calls": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "cost": 0.0,
        "priced": True,
    }


def _accumulate(bucket: dict, item: dict) -> None:
    for key in ("calls", "prompt_tokens", "completion_tokens"):
        bucket[key] += int(item.get(key, 0) or 0)
    amount = item.get("cost")
    if amount is None:
        bucket["priced"] = False
    else:
        bucket["cost"] += float(amount)


def _finalize(bucket: dict) -> dict:
    bucket["cost"] = round(bucket["cost"], 6) if bucket["priced"] else None
    return bucket


def cost_summary(days: int = 30, app: AppSettings | None = None) -> dict:
    """按「日期 × 模型」汇总轨迹里的成本账；``days <= 0`` 表示不按时间过滤。

    数据源是每次 run 写进 ``chat_runs.jsonl`` 的 ``cost`` 块（已按模型拆好），
    所以这里只做分组求和，不重新算价——价格改了也不会把历史账改掉。
    没配价格的模型在明细里 ``cost`` 是 None，这里同样保持 None，不伪装成 0。
    """
    app = app or default_settings
    path = trace_path(app)
    cutoff = None
    if days > 0:
        cutoff = (datetime.now() - timedelta(days=days - 1)).date()

    rows: dict[tuple[str, str], dict] = {}
    by_model: dict[str, dict] = {}
    by_mode: dict[str, dict] = {}
    totals = _empty_bucket()

    for file in _trace_files(path, app):
        try:
            lines = file.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict):
                continue
            block = record.get("cost")
            if not isinstance(block, dict):
                continue

            stamp = str(record.get("ts") or "")[:10]
            if cutoff is not None:
                try:
                    if date.fromisoformat(stamp) < cutoff:
                        continue
                except ValueError:
                    continue

            models = block.get("by_model")
            if not isinstance(models, dict):
                continue
            mode = str(block.get("mode") or "unknown")
            for model, item in models.items():
                if not isinstance(item, dict):
                    continue
                row = rows.setdefault(
                    (stamp, model), {**_empty_bucket(), "date": stamp, "model": model}
                )
                _accumulate(row, item)
                _accumulate(by_model.setdefault(model, _empty_bucket()), item)
                _accumulate(by_mode.setdefault(mode, _empty_bucket()), item)
                _accumulate(totals, item)

    return {
        "days": days,
        "currency": app.llm_price_currency,
        "rows": [
            _finalize(row) for _, row in sorted(rows.items(), key=lambda kv: kv[0])
        ],
        "by_model": {model: _finalize(bucket) for model, bucket in by_model.items()},
        "by_mode": {mode: _finalize(bucket) for mode, bucket in by_mode.items()},
        "totals": _finalize(totals),
    }
