"""与上游知识管线的桥。

为什么是「导入」而不是「复制代码」：
`lolita-rag` 的清洗、切分、BGE 向量、混合检索、五道闸门、答案校验全都在真实数据上
标定过（见它的 README）。把那些代码复制过来，等于把标定依据一起丢掉——第一次调阈值
就没人知道原来为什么是 0.44。所以这里只做三件事：

1. 把上游仓库加进 ``sys.path``，直接 ``import src.xxx``；
2. 构造一份**只属于本应用**的上游 ``Settings``（换数据目录、换密钥）；
3. 暴露 ``query_kb()`` 给路由用。

三条刻意的纪律：

* **不读上游的 .env**（``_env_file=None``）。上游 .env 里有 ``WEIBO_COOKIE``，
  一旦被读进来，这个对外提供服务的进程里就握着一份账号会话——上游 README 已经
  踩过「密钥进日志」，这里把出口直接堵死。
* **不调上游的 ``ask()`` / ``retrieve()``**。它们会写 ``queries.jsonl``，而那是
  上游唯一的「真实用户查询」信号源；工具层往里灌数据会污染它的拒答率统计。
  所以这里只拼装同样的三步：``search`` → ``Generator.answer`` → ``verify_and_degrade``。
* **``verify_and_degrade`` 一个都不能少**。它是把「模型可能编造」变成「物理上不可能」
  的那道门（不达标就换成逐字摘录）。小程序/网页面对的是一堆陌生人，这道门比上游
  自用的时候更重要。
"""

from __future__ import annotations

import logging
import re
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.config import AppSettings
from app.config import settings as default_settings

logger = logging.getLogger(__name__)


class UpstreamUnavailable(RuntimeError):
    """上游管线不可用（路径不对 / 依赖没装 / 索引没建）。带可执行的原因给前端。"""


class IndexNotReady(UpstreamUnavailable):
    """索引还没建。这不是故障，是「还没跑建索引」。"""


# --------------------------------------------------------------------- 装载


def _install_upstream(root: Path) -> None:
    """把上游仓库挂到 sys.path 上。

    上游用的是 ``from src.config import ...`` 这种绝对导入，所以必须让 ``src``
    这个包名可解析。**不能**把上游的 ``src`` 复制/软链到本仓：那样一旦上游更新，
    两份代码就会悄悄分叉。
    """
    root = Path(root)
    if not (root / "src" / "config.py").exists():
        raise UpstreamUnavailable(
            f"找不到上游知识管线：{root}（期望存在 src/config.py）。"
            "请在 .env 里把 LOLITA_RAG_ROOT 改成本机 lolita-rag 的真实路径。"
        )
    text = str(root)
    if text not in sys.path:
        sys.path.insert(0, text)


def upstream_settings(app: AppSettings | None = None, *, source: str = "own"):
    """构造一份只属于本应用的上游 Settings。

    只覆盖「与应用有关」的几项；检索阈值、闸门参数、融合方式一律沿用上游默认，
    因为那些是量出来的，这里没有依据改。

    ``source`` 决定索引来源（多路召回的一源一通道，见 ADR-0001）：

    * ``own`` —— 指向本仓自有知识库索引（``data_dir=kb_index_dir``）；
    * ``real`` —— 指向上游仓库自带的真实语料索引（**不覆盖 data_dir**，用上游默认）。
    """
    app = app or default_settings
    _install_upstream(Path(app.lolita_rag_root))
    from src.config import Settings  # noqa: PLC0415 - 必须先装 sys.path

    app.ensure_dirs()
    common = {
        # 关键：不读上游 .env，避免把 WEIBO_COOKIE 带进对外服务的进程
        "_env_file": None,
        "llm_api_key": app.llm_api_key.strip(),
        "llm_base_url": app.llm_base_url.strip(),
        "llm_model": app.llm_model.strip(),
        "top_k": app.top_k,
        # 工具层不进上游的真实查询日志（见模块 docstring 第二条）
        "query_log_enabled": False,
        # 锁定 embedding 行为：只用本地缓存，绝不因为联网下载把请求卡住几十秒
        "embedding_backend": "auto",
        "embedding_allow_download": False,
    }
    if source == "real":
        return Settings(**common)
    return Settings(data_dir=app.kb_index_dir, **common)


@dataclass(slots=True)
class _State:
    bundles: dict[str, Any] = field(default_factory=dict)
    settings: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    lock: threading.Lock = field(default_factory=threading.Lock)


_state = _State()


def _ensure_loaded(app: AppSettings | None = None, *, source: str = "own"):
    """按 ``source`` 装载索引（own / real），首次装载后缓存复用。

    ``own`` 与 ``real`` 是两个互不相干的索引，分别缓存，避免「自有知识库重建」
    把上游真实索引一起丢掉（或反过来）。
    """
    if source in _state.bundles:
        return _state.bundles[source]
    with _state.lock:
        if source in _state.bundles:      # 双检：并发首请求只装一次
            return _state.bundles[source]
        try:
            settings = upstream_settings(app, source=source)
            from src.pipeline.runtime import load_retriever

            bundle = load_retriever(settings)
            _state.bundles[source] = bundle
            _state.settings[source] = settings
            _state.error = ""
            logger.info("知识索引已装载（%s）：%s 个片段", source, len(bundle.chunks))
        except UpstreamUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001 - 转成可读信息给前端
            from src.pipeline.runtime import IndexMissingError

            if isinstance(exc, IndexMissingError):
                _state.error = str(exc)
                raise IndexNotReady(
                    "知识库索引还没有建立。先运行：python scripts/build_kb_index.py"
                ) from exc
            _state.error = f"{type(exc).__name__}: {exc}"
            raise UpstreamUnavailable(f"知识索引装载失败：{_state.error}") from exc
        return _state.bundles[source]


def reload_bundle(app: AppSettings | None = None) -> dict:
    """知识库重建后调用：丢掉旧索引并立刻重载 ``own``。

    索引没建好时**不抛错**：这个接口是建索引脚本的收尾通知（见
    ``scripts/build_kb_index.py --notify``），重建失败时调用方需要看到
    ``ready=false`` 与原因，而不是一个只有状态码的 500。

    返回两个来源的状态；``real`` 只清缓存、等下次查询再装载——它不由本仓
    的建索引脚本重建，没必要在这里多读一遍 14k 片段。
    """
    with _state.lock:
        _state.bundles.clear()
        _state.settings.clear()
        _state.error = ""
    try:
        _ensure_loaded(app, source="own")
    except UpstreamUnavailable as exc:
        logger.info("重载自有索引失败：%s", exc)
    return {
        "sources": {
            "own": index_status(app, source="own"),
            "real": index_status(app, source="real"),
        },
    }


# --------------------------------------------------------------------- 查询


def index_status(app: AppSettings | None = None, *, source: str = "own") -> dict:
    """给前端看的索引状态。索引没建也不抛错——前端要能显示「请先建索引」。

    ``source`` 与召回通道同名（``own`` / ``real``）：两者是两份独立索引，
    状态必须分开报，否则「自有知识库建好了」会掩盖「上游真实语料没装」。
    """
    app = app or default_settings
    info: dict[str, Any] = {
        "source": source,
        "ready": False,
        "chunks": 0,
        "posts": 0,
        "embedder": "",
        "built_at": "",
        "llm_enabled": app.llm_enabled,
        "error": "",
    }
    try:
        # 顺序不能反：upstream_settings() 负责把上游装进 sys.path，
        # 先 import src.* 会在「第一次请求」上直接 ModuleNotFoundError（实测踩过）。
        settings_ = upstream_settings(app, source=source)

        from src.pipeline.runtime import read_manifest

        manifest = read_manifest(settings_)
        info.update(
            chunks=int(manifest.get("chunks") or 0),
            posts=int(manifest.get("posts") or 0),
            embedder=str(manifest.get("embedder") or ""),
            built_at=str(manifest.get("built_at") or ""),
        )
        info["ready"] = info["chunks"] > 0
    except Exception as exc:  # noqa: BLE001
        info["error"] = f"{type(exc).__name__}: {exc}"
    return info


# --------------------------------------------------------------------- 多意图


# 知识库一条 = 一个主题，TOP_K 默认 1 是为了不让同话题邻居变成噪声。但
# 「烫金和蕾丝怎么洗」天然有两个主题，只取一条必然漏掉一半；所以这里不改
# TOP_K 的全局默认，而是在检索前把问句拆成子问题，各自取一条再合并。
#
# 拆分的边界必须保守：
# * 比较类问句（「JSK 和 OP 有什么区别」）不拆。知识库里往往有一条专门写对比的
#   条目，拆成「JSK 有什么区别」「OP 有什么区别」反而把这条打散；
# * 只拆「、」「以及」「和」「与」这类并列连接词，不拆逗号——逗号更可能只是
#   一句话中间的停顿（「你好，我想问一下……」）；
# * 「和风」「和服」「参与」「赠与」这类词里的「和/与」不是连接词，先保护起来。

_MAX_MERGED_HITS = 5
_COMPARISON_MARKERS = ("区别", "差别", "哪个好", "哪个更", "哪个适合", "对比", "还是", "vs")
_CONJUNCTIONS = re.compile(r"、|以及|和|与")
_PROTECTED_COMPOUNDS = (
    "和风", "和服", "和纸", "和尚", "和平", "柔和", "暖和",
    "参与", "给与", "赠与", "与其", "与众不同",
)
_PREDICATE_MARKERS = (
    "是什么", "为什么", "怎么样", "怎么办", "怎么", "如何", "多少",
    "哪些", "哪个", "哪", "什么", "要不要", "是不是", "有没有",
    "能不能", "可不可以", "是否", "能否", "可否", "可以", "适合",
    "推荐", "需要", "值得",
)


def _protect_compounds(text: str) -> str:
    for index, word in enumerate(_PROTECTED_COMPOUNDS):
        text = text.replace(word, f"\x00{index}\x00")
    return text


def _restore_compounds(text: str) -> str:
    for index, word in enumerate(_PROTECTED_COMPOUNDS):
        text = text.replace(f"\x00{index}\x00", word)
    return text


def _shared_predicate(text: str) -> str:
    """从最后一个分句里取出所有子问题共享的谓语。

    「烫金和蕾丝怎么洗」拆开后，前一半只有「烫金」，必须补上「怎么洗」才是一句
    完整的问题。取最早出现的疑问词到句尾，就是「怎么洗」「是什么」这类谓语。
    """
    starts = [text.find(marker) for marker in _PREDICATE_MARKERS if marker in text]
    return text[min(starts):] if starts else ""


def _append_predicate(part: str, predicate: str) -> str:
    # 分句自己已经带了谓语（「烫金可以机洗」）就不要再补，否则会拼出
    # 「烫金可以机洗可以机洗吗」这种重复。
    if any(marker in part for marker in _PREDICATE_MARKERS):
        return part
    # 「JSK」是拉丁词，直接拼成「JSK是什么」不好读；补一个空格，检索前上游会
    # 把字母与中文之间的空格去掉，所以不影响召回。
    if part and part[-1].isascii() and part[-1].isalnum():
        return f"{part} {predicate}"
    return f"{part}{predicate}"


def _split_multi_intent(question: str) -> list[str]:
    """把一个问句拆成若干子问题；不是多意图时返回空列表。

    返回空列表表示「按原问句整体检索」，调用方不要把它当成错误。
    """
    text = " ".join((question or "").split())
    if not text:
        return []

    lowered = text.lower()
    if any(marker in lowered for marker in _COMPARISON_MARKERS) and "分别" not in text:
        return []

    protected = _protect_compounds(text)
    parts: list[str] = []
    for raw in _CONJUNCTIONS.split(protected):
        part = _restore_compounds(raw).strip(" \t，,。？?！!、")
        part = part.replace("分别", "").strip()
        if part:
            parts.append(part)

    if len(parts) < 2 or any(len(part) < 2 for part in parts):
        return []

    predicate = _shared_predicate(parts[-1])
    if predicate:
        parts = [_append_predicate(part, predicate) for part in parts]

    result: list[str] = []
    seen: set[str] = set()
    for part in parts:
        if part not in seen:
            seen.add(part)
            result.append(part)
    return result if len(result) >= 2 else []


def _search_hits(
    text: str,
    bundle: Any,
    top_k: int,
    *,
    subqueries: list[str] | None = None,
) -> list[Any]:
    """按多意图检索并合并结果。没有多意图时就是一次普通检索。"""
    full_hits = list(bundle.retriever.search(text, top_k=top_k))
    intents = subqueries if subqueries is not None else _split_multi_intent(text)
    if not intents:
        return full_hits

    # 原问句取 1 条（它可能命中一条同时覆盖两个主题的条目），每个子问题再各取 1 条。
    # 上限是「原问句 + 所有子问题」，但最多 _MAX_MERGED_HITS，避免 prompt 被撑爆。
    limit = min(max(top_k, len(intents) + 1), _MAX_MERGED_HITS)
    hits: list[Any] = []
    seen: set[str] = set()

    groups = [full_hits[:1]]
    groups += [bundle.retriever.search(intent, top_k=1) for intent in intents]
    for group in groups:
        for hit in group:
            chunk_id = hit.chunk.chunk_id
            if chunk_id in seen:
                continue
            seen.add(chunk_id)
            hits.append(hit)
            if len(hits) >= limit:
                break
        if len(hits) >= limit:
            break

    return hits


def search_kb(text: str, app: AppSettings | None = None, *, top_k: int | None = None) -> list[dict]:
    """**只检索，不生成**。给「挂一条相关知识」这类场景用。

    刻意不走 ``Generator``：这里要回答的问题只是"知识库里有没有对得上的条目"。
    让模型再把原文复述一遍既慢，又给了它一次编造的机会——调用方只想在卡片下面
    挂一行「相关知识：JSK 和 OP 有什么区别」。返回原文命中最诚实。

    多意图问句会拆成子问题分别检索并合并（见 ``_split_multi_intent``）；
    比较类问句保持整体检索。
    """
    app = app or default_settings
    text = (text or "").strip()
    if not text:
        return []

    bundle = _ensure_loaded(app, source="own")
    from src.pipeline.ask import hits_to_dicts  # noqa: PLC0415 - 必须先装好 sys.path

    return hits_to_dicts(_search_hits(text, bundle, top_k or 1))


def search_real(text: str, app: AppSettings | None = None, *, top_k: int | None = None) -> list[dict]:
    """**只检索**上游真实语料索引（多路召回的 ``real`` 通道）。

    与 ``search_kb`` 的差别只在索引来源：这里用上游仓库自带的真实语料索引
    （不含本仓 ``content/kb``），回答「真实数据里有没有对得上的条目」。
    多意图问句同样会拆子问题分别检索再合并。
    """
    app = app or default_settings
    text = (text or "").strip()
    if not text:
        return []

    bundle = _ensure_loaded(app, source="real")
    from src.pipeline.ask import hits_to_dicts  # noqa: PLC0415 - 必须先装好 sys.path

    return hits_to_dicts(_search_hits(text, bundle, top_k or 1))


def query_kb(question: str, app: AppSettings | None = None, *, top_k: int | None = None) -> dict:
    """知识问答：检索 → 生成 → 运行时校验降级。

    返回结构直接给前端消费：``answer / hits / degraded / problems / terms``。
    **空检索是正常结果**，不是错误——上游五道闸门存在的意义就是让「语料里没有」
    能成立。前端必须把它显示成「知识库里暂时没有」，而不是「出错了」。

    多意图问句会先拆成子问题分别检索再合并；``subqueries`` 会随响应返回，
    方便前端和排查时看出实际拆成了什么。
    """
    app = app or default_settings
    question = (question or "").strip()
    if not question:
        raise ValueError("问题不能为空")

    bundle = _ensure_loaded(app)
    settings = _state.settings

    from src.generator.llm import Generator
    from src.generator.verify import verify_and_degrade
    from src.pipeline.ask import hits_to_dicts
    from src.retriever.bm25 import tokenize

    subqueries = _split_multi_intent(question)
    hits = _search_hits(question, bundle, top_k or app.top_k, subqueries=subqueries)
    answer, outcome = verify_and_degrade(
        question, Generator(settings).answer(question, hits), list(hits), settings
    )
    if outcome.degraded:
        logger.warning("答案已降级为原文摘录：%s", outcome.note)

    return {
        "question": question,
        "answer": answer.text,
        # used_llm=False + degraded=False 说明走的是「未配置 Key」的抽取式兜底
        "used_llm": answer.used_llm,
        "note": answer.note,
        "degraded": bool(outcome.degraded),
        "problems": list(outcome.problems),
        "count": len(hits),
        "terms": tokenize(question),
        "subqueries": subqueries,
        "hits": hits_to_dicts(hits),
    }
