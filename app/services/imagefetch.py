"""最小取图/网络工具：只保留多路召回需要的那一小块。

完整版（抓页面、选图、下载、落 media）属于被裁剪掉的应用层四页 PWA；
这里只保留 ``ssl_context()`` 与 ``fetch_html()``——国内搜索 provider 和
Lolibrary 在线补详情都需要可信 CA + 一个只读 GET。
"""

from __future__ import annotations

import ssl
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

FETCH_TIMEOUT = 8.0
FETCH_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) lolita-agents/0.1"


class FetchError(RuntimeError):
    """页面取不到（网络 / HTTP / 超时）。调用方降级，不把失败当事实缺失。"""


def ssl_context() -> ssl.SSLContext:
    """给 HTTPS 用的 SSL 上下文。**必须校验证书**，只是要给它一个可信的 CA 包。

    为什么不能直接用默认值：这台机器上的 Python（Anaconda 版）里，
    ``ssl.get_default_verify_paths()`` 指向一个不存在的路径，于是任何 https 请求
    都会 ``CERTIFICATE_VERIFY_FAILED``，而症状长得像"连不上"——真正的原因跟网络无关。

    也**不会**退化成 ``CERT_NONE``：关掉证书校验等于把请求变成"谁劫持都认"。
    """
    try:
        import certifi  # noqa: PLC0415 - 可选依赖

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:  # noqa: BLE001 - 没装就退回系统默认
        return ssl.create_default_context()


def fetch_html(url: str, *, timeout: float = FETCH_TIMEOUT) -> str:
    """只读 GET 一个页面。失败抛 :class:`FetchError`，由调用方决定怎么降级。"""
    request = Request(
        url,
        headers={"User-Agent": FETCH_UA, "Accept-Language": "en-US,en;q=0.9"},
    )
    try:
        with urlopen(request, timeout=timeout, context=ssl_context()) as response:  # noqa: S310 - 只读 GET
            charset = response.headers.get_content_charset() or "utf-8"
            return response.read().decode(charset, errors="replace")
    except HTTPError as exc:
        raise FetchError(f"HTTP {exc.code}：{url}") from exc
    except URLError as exc:
        raise FetchError(f"连不上：{exc.reason}（{url}）") from exc
    except TimeoutError as exc:
        raise FetchError(f"超时：{url}") from exc

