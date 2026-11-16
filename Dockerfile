# syntax=docker/dockerfile:1.7
#
# 多阶段：builder 只负责把依赖装进 venv，runtime 只拷 venv 和应用代码。
#
# 上游 lolita-rag 的**代码**不进镜像（运行时只读挂载），但它的依赖必须装。
# 本仓 requirements.txt 刻意不复制上游依赖清单（理由见该文件顶部注释），所以
# 这里用 BuildKit 命名上下文 `rag` 直接读上游的 requirements.txt：
#
#   docker compose build                                  # compose 已声明 rag 上下文
#   docker build --build-context rag=../lolita-rag -t lolita-agents .
#
# 只取它的 requirements.txt，不拷源码；运行时再把 checkout 挂到 /opt/lolita-rag。

FROM python:3.11-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# 少数依赖可能只有 sdist（chroma-hnswlib 等），builder 里留好编译工具；
# 它不会进入 runtime 镜像。
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# 先装上游依赖（体积大、变化少），再装本仓依赖：
# 本仓 requirements.txt 改动时不会让 sentence-transformers/torch 那层重装。
COPY --from=rag requirements.txt /tmp/rag-requirements.txt
RUN pip install -r /tmp/rag-requirements.txt

COPY requirements.txt /tmp/requirements.txt
RUN pip install -r /tmp/requirements.txt


FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    HOME=/home/app \
    DATA_DIR=/data \
    CONTENT_DIR=/app/content

# 非 root 运行。APP_UID/APP_GID 与宿主机数据目录的属主对齐（compose 会传同一个值），
# 否则 Linux 上绑定的 ./data 会写不进去。
# libgomp1 是 torch（sentence-transformers 带入）在 slim 镜像里的运行时依赖。
ARG APP_UID=1000
ARG APP_GID=1000
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid "${APP_GID}" app \
    && useradd --uid "${APP_UID}" --gid "${APP_GID}" --create-home --home-dir /home/app app

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv
COPY app ./app
COPY content ./content
COPY scripts ./scripts

RUN mkdir -p /data && chown -R app:app /app /data
USER app

EXPOSE 8787

# 用 Python 标准库探活，镜像里不需要 curl/wget；打的正是应用自己的 /api/health。
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8787/api/health', timeout=3).read()"

# SIGTERM：uvicorn 先停止接新连接，再最多等 20 秒让进行中的请求（含 SSE 问答）收尾。
# 不调用 app.main:main()，因为它没暴露 graceful timeout；这里用 CLI 显式指定。
# 容器内固定监听 8787；对宿主暴露哪个端口由 compose 的 ports 决定。
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8787", "--timeout-graceful-shutdown", "20"]
