# lolita-agents

Lolita 垂直领域的问答 agent：**多路召回 → 生成回答 → 记忆偏好 → 关注项更新通知**。跑在你自己机器上的本地优先 Web 应用（单页聊天）。

核心流程：每次提问并行召回**自有知识库 / 真实语料 / Lolibrary 档案**三个核心通道，召回结果合并后由 planner **一次判断**要不要追加**小红书 / 微博 / 网页搜索**三个实时通道，最后交给 writer 生成带 `[n]` 引用的回答。

编排层用 **LangGraph** 的 `StateGraph` 实现（[ADR-0002](docs/adr/0002-用LangGraph编排多路召回.md)）；召回层坚持**一源一通道、互不混写**（[ADR-0001](docs/adr/0001-多路召回一源一通道.md)）。

## 六个召回通道

| 通道 | 来源 | 时机 | 是否落盘 |
|---|---|---|---|
| `own` | 自有知识库 `content/kb` | 每次并行 | 入索引 |
| `real` | 上游真实语料索引 | 每次并行 | 入索引 |
| `lolibrary` | Lolibrary 档案 `data/lolibrary/catalog.jsonl` | 每次并行 | 入缓存（可重建） |
| `xhs` | 小红书（Playwright，可选） | planner 按需追加 | 否（临时证据） |
| `weibo` | 微博实时（网页搜索限定微博域） | planner 按需追加 | 否（临时证据） |
| `web` | 网页搜索（博查 / 智谱，可选） | planner 按需追加 | 否（临时证据） |

实时通道的结果**只作本次回答的临时证据**，不落盘、不入索引。每条证据带 `channel` 来源 + 原始链接。

## 快速开始

```bash
# 0) 装依赖
python -m pip install -r requirements.txt

# 1) 配置
copy .env.example .env

# 2) 建自有知识库索引（依赖上游 lolita-rag）
python scripts/build_kb_index.py

# 3) 起服务
python -m app.main            # http://127.0.0.1:8787
```

没有 `LLM_API_KEY` 时，`/api/chat/status` 会明确禁用聊天，`/api/chat` 返回 503。补查通道（xhs / weibo / web）按 `.env` 里各自开关与凭据决定是否可用，缺了就跳过、不猜数据。

## 结构

```
lolita-agents/
├── app/
│   ├── config.py          应用配置（DATA_DIR / 召回与补查开关）
│   ├── bridge.py          ★ 与上游管线的桥：search_kb（own）/ search_real（real）双索引
│   ├── db.py              SQLite：preferences / follows / conversations / meta
│   ├── main.py            FastAPI 入口（chat 路由 + 静态挂载）
│   ├── agent/             ★ LangGraph 编排 + 召回 + 规划 + 写作
│   │   ├── graph.py         StateGraph：核心通道扇出 → route → 补查扇出 → answer → verify
│   │   ├── channels.py      六通道统一入口 recall(channel, question, ctx)
│   │   ├── planner.py       单次规划：判断补查通道 + 提取检索词（JSON-in-text）
│   │   ├── writer.py        生成回答 + 引用编号
│   │   └── trace.py         轨迹落盘
│   ├── services/          web_search / xhs_search / xhs_spider / imagefetch
│   ├── routers/chat.py     POST /api/chat、POST /api/chat/stream、GET /api/chat/runs
│   └── static/            index.html + chat.js（单页聊天，零依赖）
├── content/kb/            ★ 自有知识库（纯文本 md，进 git）
├── data/                  ← 不进 git，运行时数据（kbindex / lolibrary / app.db / traces）
├── scripts/
│   ├── ingest_kb.py       md → posts.jsonl（喂给上游）
│   ├── build_kb_index.py  调上游 build_index
│   ├── import_lolibrary.py Lolibrary 档案 → catalog.jsonl
│   └── xhs_search.py      小红书浏览器自动化（--login / --query）
├── docs/adr/              架构决策记录
└── tests/                 pytest（agent / 接口）
```

## 编排（LangGraph）

```
START ──fanout──▶ own ─┐
             ├─▶ real ─┼──▶ route ──(有补查)──fanout──▶ xhs ─┐
             └─▶ lolibrary┘                              ├─▶ weibo ─┼─▶ answer ─▶ verify ─▶ END
                                                          └─▶ web  ──┘   (没补查直接 answer)
```

- 核心通道每次提问都并行召回；`Send` 在条件边函数里扇出（图节点里返回 `Send` 会报 `InvalidUpdateError`）。
- planner 在 `route` 节点做**一次**判断：要补哪些实时通道 + 精简检索词；失败就退回仅核心通道。
- 护栏：`recursion_limit` 兜底步数；图里**没有回边**，每个通道最多召回一次；`answer` 无证据直接拒答；`verify` 引用越界就把回答降级成逐字摘录。

## 边界与纪律

- **微博 UGC 与自有内容永不混用。** `real` 语料只进索引、不落盘、不对外。
- **实时通道结果不入索引。** 小红书 6 小时缓存只是避免同一 query 反复启动浏览器。
- **没证据不许作答。** 召回为空时直接拒答，不猜。
- 本服务**不读上游 `.env`**（构造上游 `Settings` 时 `_env_file=None`）。

## 测试

```bash
python -m pytest tests/ -q
```

`tests/conftest.py` 把 `DATA_DIR` 指到 `tests/.data` 并关闭 LLM / 搜索 / 小红书，**绝不碰真实 `data/`**。
