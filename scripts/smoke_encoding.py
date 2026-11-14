"""真实进程冒烟：请求体编码坏了的时候，服务必须自己说清楚。

## 跑什么

1. 自己起一个 uvicorn 子进程（不依赖你事先开服务），等到 ``/api/health`` 就绪；
2. 按 **PowerShell 5 `Invoke-WebRequest` 的默认行为**发一个请求：字符串体用
   ISO-8859-1 编码，``裙撑长度怎么选`` 到服务端时已经是 ``???????``；
   断言返回 **422**，且 detail 里指出 UTF-8；
3. 再按 UTF-8 发同一条问题，断言 **200** 且 ``evidence`` 非空、答案不是
   「没有检索到能回答这个问题的内容」；
4. 收尾杀掉子进程。

## 为什么这脚本不进 CI 的 pytest

- 它要**真实的索引**（own 14 片段 + real 14314 片段）和**能加载的向量模型**；
  CI 的 `tests/conftest.py` 把 `DATA_DIR` 指到临时目录、关掉所有外部依赖，
  第 3 步（召回）在那里永远是空，断言没法成立。
- 它要**真的占一个端口**、起一个常驻进程，跑一次十几秒；pytest 里起进程的
  用例在 Windows 上容易被端口/句柄问题拖成 flaky。
- 第 2 步（422）本身是纯 Pydantic 校验，已经由
  ``tests/test_api.py::test_chat_rejects_message_replaced_by_question_marks``
  在进程内覆盖；这个脚本补的是「真 uvicorn + 真 HTTP + 真索引」这一层。

所以它归到 ``scripts/`` 手跑，和 ``scripts/smoke.py`` 一个档位：

    python scripts/smoke_encoding.py
    python scripts/smoke_encoding.py --base-url http://127.0.0.1:8787   # 用已有服务

只依赖标准库。失败退出码 1。
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BASE_URL = "http://127.0.0.1:8787"
QUESTION = "裙撑长度怎么选"
# ISO-8859-1 的替换结果：每个中文字符一个半角问号，长度与原文相同。
MANGLED = "?" * len(QUESTION)
WAIT_SECONDS = 90.0


def request(
    base_url: str,
    path: str,
    *,
    method: str = "GET",
    body: bytes | None = None,
) -> tuple[int, str]:
    headers = {"Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(
        base_url.rstrip("/") + path, data=body, headers=headers, method=method
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_ready(base_url: str, process: subprocess.Popen) -> None:
    deadline = time.time() + WAIT_SECONDS
    while time.time() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"uvicorn 提前退出（code={process.returncode}）")
        try:
            status, _ = request(base_url, "/api/health")
            if status == 200:
                return
        except (urllib.error.URLError, ConnectionError, TimeoutError):
            pass
        time.sleep(0.5)
    raise RuntimeError(f"等 {WAIT_SECONDS:.0f}s 服务仍未就绪：{base_url}")


def check_mojibake_rejected(base_url: str) -> str:
    body = json.dumps({"message": MANGLED}).encode("utf-8")
    status, text = request(base_url, "/api/chat", method="POST", body=body)
    assert status == 422, f"期望 422（编码错），实际 {status}：{text[:200]}"
    assert "UTF-8" in text, f"422 的 detail 没指出编码问题：{text[:300]}"
    return f"422，detail 指出了 UTF-8（发送的是 {MANGLED!r}）"


def check_utf8_recalls(base_url: str) -> str:
    status, text = request(base_url, "/api/chat/status")
    assert status == 200, f"/api/chat/status 期望 200，实际 {status}"
    if not json.loads(text).get("enabled"):
        return "SKIP：没有配置 LLM_API_KEY，召回断言需要写作器，跳过"

    body = json.dumps({"message": QUESTION}, ensure_ascii=False).encode("utf-8")
    status, text = request(base_url, "/api/chat", method="POST", body=body)
    assert status == 200, f"UTF-8 请求期望 200，实际 {status}：{text[:200]}"
    payload = json.loads(text)
    evidence = payload.get("evidence") or []
    assert evidence, f"UTF-8 请求召回为空：{text[:300]}"
    assert "没有检索到能回答这个问题的内容" not in (payload.get("answer") or ""), (
        "UTF-8 请求被判成了「查不到」"
    )
    steps = [
        f"{step.get('channel')}={step.get('ms')}ms/{step.get('evidence')}条"
        for step in payload.get("steps") or []
        if step.get("kind") == "recall"
    ]
    return f"200，evidence={len(evidence)} 条，{', '.join(steps)}"


def run_checks(base_url: str) -> int:
    failures = 0
    for label, func in (
        ("乱码体被 422 拦下", check_mojibake_rejected),
        ("UTF-8 请求能召回", check_utf8_recalls),
    ):
        try:
            print(f"PASS  {label}：{func(base_url)}")
        except AssertionError as exc:
            failures += 1
            print(f"FAIL  {label}：{exc}")
    return failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="请求体编码冒烟（真实 uvicorn）")
    parser.add_argument(
        "--base-url",
        default=os.environ.get("SMOKE_BASE_URL"),
        help="用已启动的服务；不传则自己起一个 uvicorn 子进程",
    )
    args = parser.parse_args(argv)

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    if args.base_url:
        print(f"冒烟目标：{args.base_url}（已存在的服务）")
        failures = run_checks(args.base_url)
    else:
        port = free_port()
        base_url = f"http://127.0.0.1:{port}"
        print(f"起 uvicorn：127.0.0.1:{port}（首次请求要加载向量模型，慢一点正常）")
        process = subprocess.Popen(
            [
                sys.executable, "-m", "uvicorn", "app.main:app",
                "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning",
            ],
            cwd=str(ROOT),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            wait_ready(base_url, process)
            print(f"就绪，开始冒烟：{base_url}")
            failures = run_checks(base_url)
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()

    if failures:
        print(f"\n{failures} 项失败")
        return 1
    print("\n全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
