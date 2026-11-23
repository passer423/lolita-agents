"""Playwright 浏览器可用性：状态接口与直抓脚本共用同一套判断。

为什么不能只判 ``playwright`` 包：
* 托管 CI / 新服务器常常装了 Playwright 却没下载浏览器；
* 旧实现写死 ``channel="msedge"``，Linux 上即使装了 Chromium 也会失败。

这里只做**结构检测**（可执行文件是否存在），不启动浏览器：状态接口会被反复
轮询，启动一次 Chromium 要几百毫秒到几秒，还可能被风控。真正选择顺序
（Edge → Chromium）在 ``scripts/_browser.py`` 里执行，两处判断口径一致。
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import sys
from pathlib import Path

# 浏览器目录里常见的可执行文件相对路径。版本号目录（chromium-1234）由调用方展开。
_CHROMIUM_EXECUTABLES = (
    "chrome-win/chrome.exe",
    "chrome-win64/chrome.exe",
    "chrome-linux/chrome",
    "chrome-linux64/chrome",
    "chrome-mac/Chromium.app/Contents/MacOS/Chromium",
    "chrome-mac-arm64/Chromium.app/Contents/MacOS/Chromium",
    "chrome-headless-shell-win64/chrome-headless-shell.exe",
    "chrome-headless-shell-linux64/chrome-headless-shell",
    "chrome-headless-shell-mac-arm64/chrome-headless-shell",
    "chrome-headless-shell-mac-x64/chrome-headless-shell",
)

_EDGE_COMMANDS = ("msedge", "microsoft-edge", "microsoft-edge-stable")


class BrowserUnavailable(RuntimeError):
    """没有可用的 Playwright 浏览器（供脚本/服务捕获）。"""


def _playwright_roots() -> list[Path]:
    roots: list[Path] = []
    env = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "").strip()
    if env and env != "0":
        roots.append(Path(env))
    roots.append(Path.home() / ".cache" / "ms-playwright")
    if os.name == "nt":
        local = os.environ.get("LOCALAPPDATA")
        if local:
            roots.append(Path(local) / "ms-playwright")
    elif sys.platform == "darwin":
        roots.append(Path.home() / "Library" / "Caches" / "ms-playwright")
    # PLAYWRIGHT_BROWSERS_PATH=0：浏览器跟着包一起装。
    try:
        origin = importlib.util.find_spec("playwright")
    except (ImportError, ValueError):
        origin = None
    if origin and origin.origin:
        roots.append(Path(origin.origin).resolve().parent / "driver" / "package" / ".local-browsers")
    return roots


def _has_chromium(root: Path) -> bool:
    try:
        dirs = [path for path in root.glob("chromium*") if path.is_dir()]
    except OSError:
        return False
    for browser_dir in dirs:
        for relative in _CHROMIUM_EXECUTABLES:
            try:
                if (browser_dir / relative).exists():
                    return True
            except OSError:
                continue
    return False


def _has_edge() -> bool:
    if any(shutil.which(command) for command in _EDGE_COMMANDS):
        return True
    candidates: list[Path] = []
    if os.name == "nt":
        for base in (
            os.environ.get("PROGRAMFILES(X86)"),
            os.environ.get("PROGRAMFILES"),
            os.environ.get("LOCALAPPDATA"),
        ):
            if base:
                candidates.append(Path(base) / "Microsoft" / "Edge" / "Application" / "msedge.exe")
    else:
        candidates.append(Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"))
    return any(path.exists() for path in candidates)


def installed_browsers() -> list[str]:
    """按脚本里的选择顺序返回已安装通道（``msedge`` / ``chromium``）。"""
    found: list[str] = []
    if _has_edge():
        found.append("msedge")
    if any(_has_chromium(root) for root in _playwright_roots()):
        found.append("chromium")
    return found


def readiness() -> tuple[bool, str]:
    """是否至少有一个可用浏览器；不可用时给出可执行的原因。"""
    if importlib.util.find_spec("playwright") is None:
        return False, "未安装 playwright（pip install playwright）"
    browsers = installed_browsers()
    if browsers:
        return True, ""
    return False, (
        "找不到可用浏览器：需要本机 Edge，或先运行 "
        "python -m playwright install chromium"
    )
