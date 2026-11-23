"""Playwright 浏览器自动选择：本机 Edge 优先，其次 Playwright 自带的 Chromium。

公网 Linux 服务器上没有 Edge，旧代码写死 ``channel="msedge"`` 会让微博直抓
直接失败。这里把「用哪个浏览器」收在一处，两个抓取脚本共用：

* 先试本机 Edge（Windows 桌面默认路径，视觉/风控表现与原实现一致）；
* 失败再试 Playwright 自带的 Chromium（Linux 上 ``playwright install chromium``
  之后就有）；
* 两者都没有时抛 :class:`BrowserMissing`，由脚本输出 ``browser_missing`` 和
  可执行的原因，而不是让调用方看到一条没有细节的 traceback。
"""

from __future__ import annotations

BROWSER_CHOICES = ("msedge", "chromium")

# 与旧实现保持一致：微博/必应都受益于去掉自动化特征。
LAUNCH_ARGS = ["--disable-blink-features=AutomationControlled"]


class BrowserMissing(RuntimeError):
    """本机既没有 Edge，也没有 Playwright Chromium。"""


def _launch_one(playwright, channel: str):
    if channel == "chromium":
        return playwright.chromium.launch(headless=True, args=LAUNCH_ARGS)
    return playwright.chromium.launch(
        channel=channel,
        headless=True,
        args=LAUNCH_ARGS,
    )


def launch_browser(playwright) -> tuple[object, str]:
    """按 Edge → Chromium 的顺序启动，返回 ``(browser, 实际通道名)``。"""
    errors: list[str] = []
    for channel in BROWSER_CHOICES:
        try:
            return _launch_one(playwright, channel), channel
        except Exception as exc:  # noqa: BLE001 - 逐个通道探测，最后统一报缺失
            errors.append(f"{channel}: {type(exc).__name__}: {str(exc)[:120]}")
    detail = "；".join(errors) if errors else "未尝试任何浏览器"
    raise BrowserMissing(
        "找不到可用浏览器：需要本机 Edge，或先运行 "
        f"python -m playwright install chromium（{detail}）"
    )
