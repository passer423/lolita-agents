"""Lolibrary 条目页详情回填的 sidecar 读写。

`data/lolibrary/catalog.jsonl` 是导入时生成的档案，保持只读；批量回填的
Bust/Waist/着丈、配色、发售价写到同目录的 `details.jsonl`，一行一条，
按 url 覆盖。这样回填可断点续跑，也不会破坏原目录结构。

读取带 mtime/size 缓存：每次查裙子都读 624 行 sidecar 没必要。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from app.config import AppSettings

logger = logging.getLogger(__name__)

SIDECAR_NAME = "details.jsonl"

_CACHE: dict[tuple[str, float, int], dict[str, dict]] = {}


def path_for(settings: AppSettings) -> Path:
    return Path(settings.data_dir) / "lolibrary" / SIDECAR_NAME


def load(settings: AppSettings) -> dict[str, dict]:
    """按 url / slug 索引 sidecar；文件不存在或读坏就当没有。"""
    path = path_for(settings)
    try:
        stat = path.stat()
    except OSError:
        return {}
    key = (str(path), stat.st_mtime, stat.st_size)
    cached = _CACHE.get(key)
    if cached is not None:
        return cached

    entries: dict[str, dict] = {}
    try:
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, dict):
                    continue
                url = str(record.get("url") or "")
                slug = str(record.get("slug") or "")
                if url:
                    entries[url] = record
                if slug:
                    entries[slug] = record
    except OSError as exc:
        logger.warning("Lolibrary sidecar 读取失败：%s", exc)
        return {}

    _CACHE.clear()
    _CACHE[key] = entries
    return entries


def find(settings: AppSettings, *, url: str = "", slug: str = "") -> dict | None:
    entries = load(settings)
    if url and url in entries:
        return entries[url]
    if slug and slug in entries:
        return entries[slug]
    return None
