"""最小取图/网络工具：只保留多路召回里网页/微博搜索需要的那一小块。

完整版（抓页面、选图、下载、落 media）属于被裁剪掉的应用层四页 PWA；
这里只保留 ``ssl_context()``——国内搜索 provider 走 HTTPS，需要给一个可信的 CA 包。
"""

from __future__ import annotations

import ssl


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
