"""应用入口。

挂载顺序有讲究：``/api/*`` 路由必须先注册，最后才把 ``/`` 挂成静态目录——
静态挂载会吞掉所有未匹配路径，注册晚了 API 就全部 404。
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app import __version__, bridge, db
from app.config import settings
from app.routers import chat, follows, kb

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("lolita-agents")

STATIC_DIR = Path(__file__).parent / "static"

# 挂载静态目录前必须先存在，否则导入期就抛错（uvicorn 启动失败）
settings.ensure_dirs()


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init_db(settings.db_path)
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
    yield


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

# 1) API 先注册
app.include_router(chat.router)
app.include_router(follows.router)
app.include_router(kb.router)


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
