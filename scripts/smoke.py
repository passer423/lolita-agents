"""端到端冒烟：确认服务起得来、关键接口还在。

覆盖首页、``/api/chat/status``、未配 key 时 ``/api/chat`` 的 503、``/api/follows``。
用法（先另开一个终端把服务跑起来）：

    python -m app.main
    python scripts/smoke.py

服务不在默认端口时：

    python scripts/smoke.py --base-url http://127.0.0.1:8787

只依赖标准库，不引入测试框架——冒烟要能在只装了运行依赖的机器上跑。
有失败项时退出码为 1，方便 CI / 本地脚本串联。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

DEFAULT_BASE_URL = "http://127.0.0.1:8787"
TIMEOUT_SECONDS = 15.0
HOME_TITLE = "<title>Lolita 问答助手</title>"


def request(
    base_url: str,
    path: str,
    *,
    method: str = "GET",
    payload: dict | None = None,
) -> tuple[int, str]:
    """发一个请求，4xx/5xx 也当正常返回（冒烟要断言状态码，而不是抛异常）。"""
    url = base_url.rstrip("/") + path
    body = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except urllib.error.URLError as exc:
        raise RuntimeError(
            f"连不上 {url}（{exc.reason}）；先另开终端跑 python -m app.main"
        ) from exc


def parse_json(body: str, path: str) -> dict:
    try:
        data = json.loads(body)
    except json.JSONDecodeError as exc:
        raise AssertionError(f"{path} 返回的不是 JSON：{body[:200]!r}") from exc
    if not isinstance(data, dict):
        raise AssertionError(f"{path} 期望 JSON 对象，实际是 {type(data).__name__}")
    return data


def check_home(base_url: str) -> str:
    status, body = request(base_url, "/")
    assert status == 200, f"期望 200，实际 {status}"
    assert HOME_TITLE in body, "首页里没有预期的标题"
    assert "/chat.js?v=" in body, "首页没有注入前端资源版本号（缓存失效逻辑可能坏了）"
    return "200，标题与资源版本号都在"


def check_chat_status(base_url: str) -> dict:
    path = "/api/chat/status"
    status, body = request(base_url, path)
    assert status == 200, f"期望 200，实际 {status}"
    data = parse_json(body, path)
    assert isinstance(data.get("enabled"), bool), "缺少 enabled 布尔字段"
    channels = data.get("channels")
    assert isinstance(channels, dict), "缺少 channels 对象"
    assert isinstance(channels.get("status"), dict), "channels 里缺少 status"
    return data


def check_chat_503(base_url: str) -> str:
    path = "/api/chat"
    status, body = request(base_url, path, method="POST", payload={"message": "冒烟测试"})
    assert status == 503, f"未配 key 时期望 503，实际 {status}"
    data = parse_json(body, path)
    assert data.get("detail"), "503 响应缺少 detail（没有告诉调用方缺什么）"
    return f"503，detail={data['detail']}"


def check_follows(base_url: str) -> str:
    path = "/api/follows"
    status, body = request(base_url, path)
    assert status == 200, f"期望 200，实际 {status}"
    data = parse_json(body, path)
    follows = data.get("follows")
    sources = data.get("sources")
    assert isinstance(follows, list), "缺少 follows 数组"
    assert isinstance(sources, list) and sources, "缺少 sources（来源可用性）"
    return f"200，{len(follows)} 个关注项、{len(sources)} 个来源"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="lolita-agents 端到端冒烟")
    parser.add_argument(
        "--base-url",
        default=os.environ.get("SMOKE_BASE_URL", DEFAULT_BASE_URL),
        help=f"服务地址，默认 {DEFAULT_BASE_URL}（也可用环境变量 SMOKE_BASE_URL）",
    )
    args = parser.parse_args(argv)
    base_url = args.base_url

    if hasattr(sys.stdout, "reconfigure"):
        # 与 UTF8JSONResponse 同理：Windows 控制台默认编码会把中文输出打成乱码。
        sys.stdout.reconfigure(encoding="utf-8")

    print(f"冒烟目标：{base_url}")
    failures = 0

    def run(label: str, func) -> None:
        nonlocal failures
        try:
            print(f"PASS  {label}：{func()}")
        except (AssertionError, RuntimeError) as exc:
            failures += 1
            print(f"FAIL  {label}：{exc}")

    run("首页", lambda: check_home(base_url))

    status_data: dict = {}

    def status_case() -> str:
        status_data.update(check_chat_status(base_url))
        return f"enabled={status_data['enabled']}"

    run("/api/chat/status", status_case)

    if not status_data:
        print("SKIP  /api/chat 503：依赖 /api/chat/status，未取到结果")
    elif status_data["enabled"]:
        # 配了 key 还硬打 /api/chat 会真的走模型、烧额度，所以跳过。
        print("SKIP  /api/chat 503：已配置 LLM_API_KEY，跳过以避免消耗额度")
    else:
        run("未配置 key 时 /api/chat 返回 503", lambda: check_chat_503(base_url))

    run("/api/follows", lambda: check_follows(base_url))

    if failures:
        print(f"\n{failures} 项失败")
        return 1
    print("\n全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
