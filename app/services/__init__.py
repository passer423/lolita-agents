"""服务层：会联网 / 会写磁盘的逻辑放这里。

多路召回里的实时通道依赖这里：
* ``web_search.py`` 博查 / 智谱网页搜索（weibo / web 通道）
* ``xhs_search.py`` 小红书搜索（Playwright，xhs 通道）
* ``imagefetch.py`` 只保留 ``ssl_context()``（HTTPS 需要可信 CA 包）
"""
