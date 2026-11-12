"""测试基座：把数据目录指到临时目录，确保测试永远碰不到真实数据。

两条纪律（沿用上游踩过的坑）：

* 测试开始前把 ``DATA_DIR`` 指到 ``tests/.data``，并显式关掉 LLM 和实时通道——
  不这样，"跑一次测试"就会往真实查询日志 / 真实搜索 key 上灌几十条。
* ``settings`` 是模块级单例；改了 ``data_dir`` 后 fixture 结束要还原，避免污染别的用例。
"""

from __future__ import annotations

import itertools
import os
import shutil
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_RUN_ID = f"pid{os.getpid()}"
TEST_DATA = ROOT / "tests" / ".data" / _RUN_ID
WORK_TMP = ROOT / "tests" / ".tmp" / _RUN_ID

_counter = itertools.count(1)


def _prepare() -> None:
    for path in (TEST_DATA, WORK_TMP):
        shutil.rmtree(path, ignore_errors=True)
        path.mkdir(parents=True, exist_ok=True)
    os.environ["DATA_DIR"] = str(TEST_DATA)
    os.environ["LLM_API_KEY"] = ""
    os.environ["SEARCH_ENABLED"] = "false"
    os.environ["XHS_ENABLED"] = "false"


_prepare()


@pytest.fixture()
def workdir() -> Path:
    path = WORK_TMP / f"case{next(_counter):04d}"
    shutil.rmtree(path, ignore_errors=True)
    path.mkdir(parents=True, exist_ok=True)
    return path


@pytest.fixture()
def data_dir():
    """把 settings.data_dir 指到临时目录，结束后还原。"""
    from app.config import settings

    original = settings.data_dir
    unique = TEST_DATA / f"data{next(_counter):04d}"
    unique.mkdir(parents=True, exist_ok=True)
    settings.data_dir = unique
    settings.ensure_dirs()
    try:
        yield unique
    finally:
        settings.data_dir = original


@pytest.fixture()
def client(data_dir):
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as test_client:
        yield test_client
