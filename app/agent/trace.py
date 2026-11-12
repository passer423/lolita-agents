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
from datetime import datetime
from pathlib import Path

from app.config import AppSettings
from app.config import settings as default_settings

logger = logging.getLogger(__name__)


def trace_path(app: AppSettings | None = None) -> Path:
    app = app or default_settings
    return app.data_dir / "logs" / "chat_runs.jsonl"


def append(record: dict, app: AppSettings | None = None) -> Path | None:
    """追加一行。任何异常都吞掉并打 warning。"""
    path = trace_path(app)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"ts": datetime.now().isoformat(timespec="seconds"), **record}
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
