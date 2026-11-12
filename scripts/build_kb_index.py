"""建/更新知识库索引（复用上游 build_index）。

用法：
    python scripts/build_kb_index.py            # 增量（只编码新增/变更的条目）
    python scripts/build_kb_index.py --rebuild  # 全量重建（换 embedding 模型后必须）
    python scripts/build_kb_index.py --notify   # 建完通知正在跑的服务重载索引

为什么默认增量：知识库改一条就全量重编码这个操作，第一次觉得无所谓，
改到第二十次就会开始不敢改内容——而内容才是这个产品真正的价值。
"""

from __future__ import annotations

import argparse
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for candidate in (ROOT, Path(__file__).resolve().parent):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from ingest_kb import ingest  # noqa: E402

from app import bridge  # noqa: E402
from app.config import settings  # noqa: E402


def notify_server(port: int) -> None:
    """建完索引后通知本机服务重载（服务没在跑就静默跳过）。

    与上游 ``daily_update`` 跑完主动 ``POST /api/reload`` 是同一套做法：
    服务是**启动时装载一次**索引的，不通知它就会一直用旧的那份，
    而界面上「建了索引但搜不到新条目」是最容易让人怀疑自己的一条 bug。
    """
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/kb/reload", method="POST", data=b""
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            print(f"已通知本机服务重载索引（HTTP {response.status}）")
    except (urllib.error.URLError, OSError) as exc:
        print(f"跳过通知（服务未运行或不可达）：{exc}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="建知识库索引")
    parser.add_argument("--rebuild", action="store_true", help="全量重建")
    parser.add_argument("--notify", action="store_true", help="建完通知本机服务重载")
    args = parser.parse_args(argv)

    ingest(settings)
    upstream = bridge.upstream_settings(settings)

    from src.pipeline.build_index import build_index

    stats = build_index(source="raw", settings=upstream, rebuild=args.rebuild)
    print()
    print(stats.render())

    if args.notify:
        notify_server(settings.bind_port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
