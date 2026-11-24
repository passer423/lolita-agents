"""应用入口。

挂载顺序有讲究：``/api/*`` 路由必须先注册，最后才把 ``/`` 挂成静态目录——
静态挂载会吞掉所有未匹配路径，注册晚了 API 就全部 404。
"""

from __future__ import annotations

import logging
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app import __version__, accounts, bridge, db
from app import auth as access_control
from app.agent import channels
from app.config import settings
from app.routers import chat, follows, jobs, kb, memories
from app.services import job_runner

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("lolita-agents")

STATIC_DIR = Path(__file__).parent / "static"

# 挂载静态目录前必须先存在，否则导入期就抛错（uvicorn 启动失败）
settings.ensure_dirs()


def _warm_reranker() -> None:
    """精排是懒加载的：检索 ``top_k=1`` 时不一定触发，这里显式打一次分。

    只预热 int8 ONNX 后端（默认）。torch 回退在 2 GiB 上本来就吃不下，
    让它按首次提问的自然路径加载，预热阶段不主动拉起。
    """
    if not settings.reranker_enabled:
        return
    from app.rerank import shared_onnx_reranker

    proxy = shared_onnx_reranker(settings)
    if proxy is None:
        logger.info("预热跳过精排：当前后端不是 int8 ONNX，首次提问时才加载")
        return
    proxy.predict([("预热", "预热")])


def _warm_models() -> None:
    """预热 BGE 与精排：各跑一次最小检索，**不调用 LLM、不写库**。

    两个索引分别装一次（own / real），精排是两者共享的同一份实例；
    任何一步失败只记日志，不抛给启动流程——预热是体验优化，不是可用性前提。
    """
    started = time.perf_counter()
    for source, search in (("own", bridge.search_kb), ("real", bridge.search_real)):
        try:
            search("预热", settings, top_k=1)
            logger.info("预热完成：%s 检索已加载（累计 %.1f 秒）", source, time.perf_counter() - started)
        except Exception as exc:  # noqa: BLE001 - 预热失败不能影响服务启动
            logger.warning("预热 %s 检索失败（不影响服务）：%s", source, exc)
    try:
        _warm_reranker()
        logger.info("预热完成：精排已加载（累计 %.1f 秒）", time.perf_counter() - started)
    except Exception as exc:  # noqa: BLE001 - 精排预热失败同样不影响服务
        logger.warning("预热精排失败（不影响服务）：%s", exc)
    logger.info("启动预热结束：%.1f 秒", time.perf_counter() - started)


@asynccontextmanager
async def lifespan(application: FastAPI):
    db.init_db(settings.db_path)
    try:
        removed = db.prune_user_data(settings.db_path)
        if any(removed.values()):
            logger.info(
                "启动清理：对话 %s 个、登录会话 %s 个", removed["threads"], removed["sessions"]
            )
    except Exception as exc:  # noqa: BLE001 - 清理失败不该挡住服务启动
        logger.warning("启动清理失败（不影响服务）：%s", exc)
    logger.info("数据目录：%s", settings.data_dir)
    logger.info("自有知识库索引：%s", settings.kb_index_dir)

    # 启动自检：上游路径失效是最常见的「看起来像代码坏了」，先把话说清楚。
    upstream = bridge.upstream_status(settings)
    if upstream["exists"]:
        logger.info("上游知识管线：%s", upstream["root"])
    else:
        logger.warning("%s", upstream["error"])
    for source in ("own", "real"):
        info = bridge.index_status(settings, source=source)
        if info["ready"]:
            logger.info("索引 %s 就绪：%s 个片段（%s）", source, info["chunks"], info["built_at"])
        else:
            logger.warning("索引 %s 未就绪：%s", source, info["error"] or "还没有建立")

    # 后台任务队列：建表、恢复上次未收尾的任务、按需起单线程。
    # 停机时只置停止标志并终止子进程；来不及收尾的任务留给下次启动标 interrupted。
    runner = job_runner.get_runner(settings)
    runner.start()
    application.state.job_runner = runner

    # 预热放后台线程：不阻塞启动，健康检查立刻可用。
    if settings.warmup_on_start:
        threading.Thread(target=_warm_models, name="warmup", daemon=True).start()
    try:
        yield
    finally:
        runner.stop()


class UTF8JSONResponse(JSONResponse):
    """显式声明 ``charset=utf-8``。

    JSON 按 RFC 8259 本来就是 UTF-8，浏览器也照这个处理；但**不带 charset 时**
    一些命令行工具（PowerShell 的 Invoke-WebRequest 默认按 ISO-8859-1）会把中文
    解成乱码，于是验证接口的人会以为服务坏了。上游 app.py 里踩过同一个坑，
    这里直接用同样的修法。
    """

    media_type = "application/json; charset=utf-8"


app = FastAPI(
    title="lolita-agents",
    version=__version__,
    description="Lolita 领域问答助手：多路召回 → 生成回答 → 关注项通知",
    lifespan=lifespan,
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
    default_response_class=UTF8JSONResponse,
)

# 账号门禁：除白名单外的 /api/* 一律要求登录（见 app/accounts.py）。
# 放在 AccessControl 内层：先过可选的 API key/JWT，再过账号会话。
app.add_middleware(accounts.AccountGate, settings=settings)
# 可选访问控制：默认 AUTH_MODE=off，行为与以前完全一致；配置错误会在构造期
# 直接抛错，而不是静默放行或静默拒绝。
app.add_middleware(access_control.AccessControl, settings=settings)

# 1) API 先注册
app.include_router(accounts.router)
app.include_router(chat.router)
app.include_router(chat.threads_router)
app.include_router(follows.router)
app.include_router(kb.router)
app.include_router(jobs.router)
app.include_router(memories.router)
app.include_router(access_control.router)


@app.get("/api/health", tags=["health"])
def health() -> dict:
    """给探针和排查用的汇总：版本 + 两份索引 + 每个召回通道的就绪状态。

    这里**不重新判断**就绪：索引直接取 ``bridge.index_status``，通道直接取
    ``channels.channel_status``。另写一套判断只会和聊天页显示的状态打架，
    排查时反而不知道该信哪一个。
    """
    return {
        "version": __version__,
        "indexes": {
            "own": bridge.index_status(settings, source="own"),
            "real": bridge.index_status(settings, source="real"),
        },
        "channels": channels.channel_status(settings),
    }


def _asset_version() -> str:
    """前端资源版本号：chat.js / style.css / sw.js 一改就换 URL。

    同端口曾经跑过 lo-life，它的 Service Worker 对 /chat.js、/style.css 是
    cache-first；只靠 HTTP 缓存头绕不过去，换 URL 才是最稳的失效方式。
    sw.js 也参与版本：清理工的 URL 变了，旧 worker 的缓存就拦不住新脚本。
    """
    stamps = [
        path.stat().st_mtime
        for path in (
            STATIC_DIR / "chat.js",
            STATIC_DIR / "style.css",
            STATIC_DIR / "sw.js",
        )
        if path.exists()
    ]
    return str(int(max(stamps))) if stamps else __version__


@app.get("/", include_in_schema=False)
def index() -> HTMLResponse:
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    version = _asset_version()
    html = html.replace('src="/chat.js"', f'src="/chat.js?v={version}"')
    html = html.replace('href="/style.css"', f'href="/style.css?v={version}"')
    html = html.replace("/sw.js?v=__ASSET_VERSION__", f"/sw.js?v={version}")
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


@app.middleware("http")
async def no_store_ui(request: Request, call_next):
    """前端外壳不缓存：本地服务改完就应该立刻生效，别让人靠 Ctrl+F5。"""
    response = await call_next(request)
    if request.url.path in ("/chat.js", "/style.css", "/sw.js"):
        response.headers["Cache-Control"] = "no-store"
    return response


# 2) 静态资源最后挂。
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="app")


def main() -> None:
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=settings.bind_host,
        port=settings.bind_port,
        reload=False,
    )


if __name__ == "__main__":
    main()
