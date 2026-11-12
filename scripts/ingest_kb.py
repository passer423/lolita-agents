"""把 data/kb/*.md 转成上游管线认识的 posts.jsonl。

这是整个改造里最省力的一步：上游的清洗、切分、BGE、混合检索、五道闸门、引用校验
全部只吃 `Post`，所以只要把自有知识库喂成 `Post`，下游一行都不用改。

两条刻意的约束：

* **知识库是纯文本，不是 Markdown。** 上游的 ``clean_text`` 会吃掉 ``#`` 和空行
  （为微博语料设计的），写 ``## 标题`` 只会让井号留成噪声。所以格式就写
  「首行是标题，后面是段落」。
* **id 必须唯一。** 上游的 ``chunk_id`` 是 ``post_id#序号``，id 撞了会在向量库里
  互相覆盖，而且不报错——这是最难查的一类问题，所以这里直接拦下来。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import bridge  # noqa: E402
from app.config import AppSettings  # noqa: E402
from app.config import settings as default_settings


def split_front_matter(text: str) -> tuple[dict[str, str], str]:
    """文件头是 ``key: value`` 若干行，遇到单独一行 ``---`` 开始正文。"""
    meta: dict[str, str] = {}
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if line.strip() == "---":
            return meta, "\n".join(lines[index + 1 :]).strip()
        if ":" in line:
            key, _, value = line.partition(":")
            meta[key.strip().lower()] = value.strip()
    return meta, text.strip()           # 没有 front-matter 就把整个文件当正文


def iter_entries(kb_dir: Path) -> list[tuple[Path, dict[str, str], str]]:
    return [
        (path, *split_front_matter(path.read_text(encoding="utf-8")))
        for path in sorted(kb_dir.rglob("*.md"))
    ]


def ingest(app: AppSettings | None = None, *, verbose: bool = True) -> Path:
    """写 posts.jsonl 并返回路径。不建索引——那是 build_kb_index.py 的事。"""
    app = app or default_settings
    app.ensure_dirs()

    upstream = bridge.upstream_settings(app)
    upstream.ensure_dirs()

    from src.schema import Post  # noqa: PLC0415 - 必须先装 sys.path

    entries = iter_entries(app.kb_dir)
    if not entries:
        raise SystemExit(f"知识库是空的：{app.kb_dir} 下没有 .md 文件")

    posts: list[Post] = []
    seen: dict[str, Path] = {}
    dropped: list[tuple[str, int]] = []

    for path, meta, body in entries:
        post_id = meta.get("id") or path.stem
        if post_id in seen:
            raise SystemExit(
                f"id 重复：{post_id}\n  来自 {seen[post_id].name} 与 {path.name}\n"
                "id 会拼成 chunk_id，重复会让向量库里互相覆盖且不报错。"
            )
        seen[post_id] = path

        title = meta.get("title", "")
        # 标题并进正文：检索时「JSK」这类关键词必须能被 BM25 打到
        text = f"{title}\n{body}".strip() if title else body
        if len(text) < upstream.chunk_min_len:
            dropped.append((post_id, len(text)))
            continue

        posts.append(
            Post(
                id=post_id,
                text=text,
                author="知识库",
                created_at=meta.get("updated", ""),
                url="",
                likes=0,
                source="kb",
            )
        )

    if not posts:
        raise SystemExit(f"所有条目都短于 {upstream.chunk_min_len} 字，没有可入库的内容")

    target = upstream.raw_posts_path
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8", newline="\n") as handle:
        for post in posts:
            handle.write(json.dumps(post.to_dict(), ensure_ascii=False) + "\n")

    if verbose:
        print(f"知识库：{len(entries)} 个文件 → {len(posts)} 条入库")
        print(f"写入  ：{target}")
        for post_id, length in dropped:
            print(f"  ⚠ 跳过 {post_id}：只有 {length} 字（下限 {upstream.chunk_min_len}）")
    return target


def main() -> int:
    ingest(default_settings)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
