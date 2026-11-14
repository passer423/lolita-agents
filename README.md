# lolita-agents

Lolita 垂直领域的问答 agent：**多路召回 → 生成回答 → 关注项更新通知**。跑在你自己机器上的本地优先 Web 应用（单页聊天）。

核心流程：每次提问并行召回**自有知识库 / 真实语料 / Lolibrary 档案**三个核心通道，召回结果合并后由 planner **一次判断**要不要追加**小红书 / 微博 / 网页搜索**三个实时通道，最后交给 writer 生成带 `[n]` 引用的回答。

编排层用 **LangGraph** 的 `StateGraph` 实现（[ADR-0002](docs/adr/0002-用LangGraph编排多路召回.md)）；召回层坚持**一源一通道、互不混写**（[ADR-0001](docs/adr/0001-多路召回一源一通道.md)）。

## 六个召回通道

| 通道 | 来源 | 时机 | 是否落盘 |
|---|---|---|---|
| `own` | 自有知识库 `content/kb` | 每次并行 | 入索引 |
| `real` | 上游真实语料索引 | 每次并行 | 入索引 |
| `lolibrary` | Lolibrary 档案 `data/lolibrary/catalog.jsonl` | 每次并行 | 入缓存（可重建） |
| `xhs` | 小红书（Playwright 或 Spider_XHS，可选） | planner 按需追加 | 否（临时证据） |
| `weibo` | 微博实时（网页搜索限定微博域） | planner 按需追加 | 否（临时证据） |
| `web` | 网页搜索（博查 / 智谱，可选） | planner 按需追加 | 否（临时证据） |

实时通道的结果**只作本次回答的临时证据**，不落盘、不入索引。每条证据带 `channel` 来源 + 原始链接。

`lolibrary` 通道做的是**关键词匹配**，不是语义检索：只认品牌（`AP` / `Baby` / `BTSSB` / `AATP` / `IW` 等缩写，以及「安琪」「海盗」「无辜世界」这类中文叫法，见 `app/agent/channels.py` 的 `BRAND_ALIASES`）、货号和拉丁词。中文系列名通过 `content/dress/aliases.json` 的译名表或模型兜底还原；未覆盖的社区叫法查不到就是空。

档案不被 git 跟踪（`data/`），换机器后按品牌重建：

```bash
python scripts/import_lolibrary.py --brand angelic-pretty --brand baby-the-stars-shine-bright \
    --pages 2 --no-detail --write
```

## 裙子档案工具

问到某条具体裙子时，`dress` 工具会先识别品牌与地区，再按地区选来源：

| 识别结果 | 查询 |
|---|---|
| 日牌 | Lolibrary 档案（本地优先；字段不全时在线补条目详情；不补查社媒） |
| 国牌 | 小红书 + 微博（Lolibrary 核心通道仍会跑，只作身份补充） |
| 未识别 / 其他地区 | 两边都查，并明确标「地区未识别」 |

识别顺序：商品编号 → 档案标题/中文别名 → `content/dress/aliases.json` 系列译名（如「皇家千层公主」→ Baby 的 Mille-feuille Royal pour la Princesse、「甜心梦幻拿铁」→ Sweetheart Dreamy Latte）→ `content/brands.json` 品牌别名；以上都没命中时，**让模型兜底识别中文名**，再用 Lolibrary 搜索校验品牌与标题。系列译名支持一字容错（皇/黄这类同音误写）；只写名字、不带价格/发售等词也会先查别名、再交给模型。品牌表与译名表都在 `content/` 下，可手动补——模型对冷门社区叫法不稳定，常用名字仍应优先补进别名表。

别名和模型之间还有一层**拆词检索**：`content/dress/glossary.json` 把中文意象拆成英文词（甜心→sweetheart、梦幻→dreamy、拿铁→latte），拼 1-2 个候选去 Lolibrary 搜，标题 token 对得上才算命中；仍不中且配置了 `SEARCH_ENABLED` / `XHS_ENABLED`，再用中文名搜网页/小红书，从结果里抽英文系列名回 Lolibrary 校验。搜索结果只作线索，最终字段仍以 Lolibrary 档案为准；召回过程里会显示「关键词检索 / 网页反查 → 命中 / 未命中」。

校验通过后，映射会写入 `data/dress/learned_aliases.json`（运行时缓存，不进 git），下次同一叫法直接离线命中。没通过校验的结果会明确标「模型识别，未在 Lolibrary 档案中核实」，不冒充档案事实；删掉这个缓存文件即可重置学到的别名。

返回字段：品牌、地区、发售年份、历史原价、款式、配色、来源链接。**历史原价不是现价**；小红书/微博抽出的价格、日期、配色一律标「网友提到，未核实」；多来源冲突时并列说明，不替用户拍板。

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

## 知识库索引与上游路径

`own` 与 `real` 是**两份独立索引**，状态必须分开看：

| 索引 | 位置 | 谁建 |
|---|---|---|
| `own` | `DATA_DIR/kbindex`（默认 `data/kbindex`） | 本仓 `python scripts/build_kb_index.py` |
| `real` | 上游 `lolita-rag/data/index` | 上游 `python scripts/build_index.py --source raw`（或它自己的 `daily_update` / `sync_brands`） |

`LOLITA_RAG_ROOT` 必须指向一个**固定的** lolita-rag checkout。不要填临时工作目录（例如 `...\temp_xxx\...`）：目录被清理后 `real` 与自有索引的构建都会失败，而报错看起来像代码故障。换机器或挪目录后按这个顺序做：

1. 改 `.env` 的 `LOLITA_RAG_ROOT`；
2. `python scripts/build_kb_index.py --rebuild` 重建自有索引；加 `--notify` 会顺带通知正在跑的服务重载；
3. 起服务后看 `GET /api/kb/status`：每份索引的 `ready` / `chunks` / `built_at` / `error`，以及上游的 `root` / `exists` 都在这里；
4. 不想重启服务时 `POST /api/kb/reload` 手动重载（`build_kb_index.py --notify` 自动做这一步）。

`real` 是可选的：上游没有索引时该通道返回空，不影响其它通道；但 `own` 的构建本身也依赖上游代码，所以 `LOLITA_RAG_ROOT` 不能省。

## 结构

```
lolita-agents/
├── app/
│   ├── config.py          应用配置（DATA_DIR / 召回与补查开关）
│   ├── bridge.py          ★ 与上游管线的桥：search_kb（own）/ search_real（real）双索引
│   ├── db.py              SQLite：follows / follow_events / conversations / meta
│   ├── main.py            FastAPI 入口（chat / follows / kb 路由 + 静态挂载）
│   ├── agent/             ★ LangGraph 编排 + 召回 + 规划 + 写作
│   │   ├── graph.py         StateGraph：核心通道扇出 → route → 补查扇出 → answer → verify
│   │   ├── channels.py      六通道统一入口 recall(channel, question, ctx)
│   │   ├── dress.py         裙子档案识别（品牌/译名/字段抽取）
│   │   ├── planner.py       单次规划：判断补查通道 + 提取检索词（JSON-in-text）
│   │   ├── writer.py        生成回答 + 引用编号
│   │   └── trace.py         轨迹落盘
│   ├── services/          web_search / xhs_search / xhs_spider / imagefetch /
│   │                      follow_watch / lolibrary_online
│   ├── routers/           chat（问答）/ follows（关注项）/ kb（索引状态与重载）
│   └── static/            index.html + chat.js（单页聊天，零依赖）
├── content/kb/            ★ 自有知识库（纯文本 md，进 git）
├── data/                  ← 不进 git，运行时数据（kbindex / lolibrary / app.db / traces）
├── scripts/
│   ├── ingest_kb.py       md → posts.jsonl（喂给上游）
│   ├── build_kb_index.py  调上游 build_index
│   ├── import_lolibrary.py Lolibrary 档案 → catalog.jsonl
│   ├── probe_search.py    排查「这条为什么没召回」（直连实时通道，不读索引）
│   ├── smoke.py           端到端冒烟（需服务已启动）
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
- 补查决策走**关键词规则**（`planner.plan_channels`）：不看召回结果，只看问题命中了哪类实时意图。模型只负责把问题压成检索词，且默认不补查时一次都不调用；它挂了也不影响补查。
- 护栏：`recursion_limit` 兜底步数；图里**没有回边**，每个通道最多召回一次；`answer` 无证据直接拒答；引用越界时非流式降级成逐字摘录，流式**就地标出越界编号**（不打断阅读，答案留在原地）。
- **整体预算**：`AGENT_DEADLINE`（默认 60s）包住整轮召回 + 写作。单通道各有超时（搜索 urlopen / 小红书子进程 / LLM 客户端），但总预算才是「最坏两分钟」的护栏：到点返回已查到的证据 + 「部分通道超时」说明，不报 500。
- 整体预算的已知取舍：`graph.invoke()` 是同步阻塞的，Python 无法从线程外取消它；超时后工作线程会继续跑到各通道自己的超时收尾（线程泄漏）。本地单机、一次问答一个线程可以接受（泄漏线程不写响应、不影响后续请求）；将来若做多租户 / 高并发，必须换成可取消的执行方式（子进程或原生 async）。

### 证据分源（一源一通道在结果层的落地）

每条证据带 `source_kind`，写作按效力分两块渲染：

| source_kind | 来源 | 写作用法 |
|---|---|---|
| `own_content` | 自有知识库 | 可作规范 |
| `third_party` | Lolibrary 档案 | 可作事实（不含现价） |
| `realtime` | 小红书 / 微博 / 网页搜索 | 时效信息可用，须标来源 |
| `upstream_ugc` | 上游采集的微博发言 | **只能写「有网友提到」，不得当规范** |

排序也是三级规则（本次补查的通道 → 来源效力 → 通道顺序），不是跨通道相关性打分——BM25 / 向量相似度 / 搜索排序是三套量纲，没法合成一个分。

合并层（`graph._normalize`）同时做三件事：

- **去重**：同一 URL（忽略锚点与末尾斜杠）或同一 `ref` 只留一条；两者都没有的条目不合并——没有稳定标识时强行合并会误删不同档案。
- **保留更强版本**：同一条被多个通道抓到时，留下 `source_kind` 效力更高的那版（自有内容 > 第三方档案 > 实时 > 网友发言）。
- **限量**：合并后最多 `EVIDENCE_MAX_ITEMS` 条（默认 12，`<=0` 不限制）。补查通道**整体豁免**——它们是用户这个问题才触发的，允许略超上限，不能被核心通道的长列表挤掉。

### real 通道为什么只认向量路

`real` 只保留 `retriever` 含 `vector` 的命中（纯 BM25 命中 = 字面重合，不构成「语料里有答案」），`TOP_K` 默认 1。这条保守策略做过一次 20 条代表性问题（16 条 real 金标 + 4 条同类问题）的对比：严格（k=1 + 向量路）在 16 条金标上 **0 条空召回**；放宽到「k=3 + 含 BM25」只在空召回时补上关键词重合的噪声——问「AP 茶会限定二手现在什么价」捞到「来茶会舞一曲」的视频描述，问「国牌大码现货有哪些推荐」捞到「急收大码」的出物帖，都不是答案。结论：**保留严格策略**，不为空召回放宽；真实语料是他人发言，宁可漏也不把道听途说当事实。

## 关注项：页面内检查（形态甲）

- 存储：`follows`（关键词 + 来源 + 上次快照）与 `follow_events`（更新事件 + 未读标记）。
- 入口：**不固定展示、不提供手动新增**。只有当用户问的是未发售 / 预约中的裙子，回答下方才会出现「持续关注」提议（`follow_suggestion`：问题命中交易/上新意图 + 问题或证据含预约/发售字眼）；用户点按钮才建关注项，面板也才出现。
- 触发：**每天第一次打开页面时自动检查一次**（本地日期记在 localStorage），页面一直开着也不会重复请求；随时可手动「检查更新」。**后端没有常驻定时任务**，页面关掉就停——这是形态甲的明确取舍，不假装实时。
- 判定：第一次检查只记基线；之后新增 / 内容变化 / 减少各写一条事件。
- 通道不可用 **≠** 没有更新：不写事件、不覆盖基线，界面显示原因（与「skip 不是 empty」同一条纪律）。
- 接口：`GET/POST /api/follows`、`DELETE /api/follows/{id}`、`POST /api/follows/check`、`POST /api/follows/events/read`。

## 会话记忆

- 存储：`data/app.db` 的 `conversations` 表，一问一答同事务写入。
- 回填：请求**不传** `history` 时，服务端自动补最近几轮；显式传 `history: []` 表示开新对话，不加载旧上下文。
- 编号：落库与回填都会剥掉 assistant 内容里的 `[n]`——编号只在单轮内有效，上一轮的 `[1]` 指向的是上一轮证据。
- 保留：只留最近 `CONVERSATION_RETENTION_MESSAGES` 条消息（默认 100 条 ≈ 50 轮，一问一答算 2 条），超出即删最旧的；设为 `<=0` 关闭自动清理。
- 清理：`DELETE /api/chat/history` 清空会话记忆（返回删除条数），不影响关注项与排查轨迹。
- 只落最终回答：流式回答中途断开时不写库，半截回答不会进入下一轮上下文。

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

## 后台任务队列

关注扫描与索引重建是分钟级操作，过去会把 HTTP 请求一直挂着。现在它们进
**SQLite 任务表**（`data/app.db` 的 `jobs`），由一个后台线程串行执行：

- 不引入 Redis：单用户本地服务已经有 `app.db`（WAL），任务状态没理由放第二个
  常驻服务里；RQ 的 worker 依赖 fork，Windows 上跑不起来。
- **单线程串行**：xhs 会起浏览器子进程、建索引吃 CPU/内存，并发只会互相拖慢。
- 对话（`/api/chat`、`/api/chat/stream`）**保持同步**：SSE 已有逐通道进度与
  “停止”，实时证据也只对本次回答有效，入队只会让首字更慢。

接口：

| 接口 | 行为 |
|---|---|
| `POST /api/follows/check` | 默认 `202 {job_id, poll_url}`；重复提交返回同一个任务（`200` + `deduplicated:true`） |
| `POST /api/follows/check?wait=1` | 同步执行，返回旧格式（脚本兼容） |
| `GET /api/jobs/{id}` | 状态、`attempts`、`progress`、`result`、`error` |
| `GET /api/jobs?kind=&status=&limit=` | 任务列表（含 `worker_alive`） |
| `POST /api/jobs/{id}/retry` | 手动重跑（克隆成新任务） |
| `DELETE /api/jobs/{id}` | 取消排队中的任务 |
| `POST /api/kb/rebuild` | 后台重建索引；**默认关闭**（`KB_REBUILD_API=false`） |

幂等与重试：

- 同一 `idempotency_key` 只允许一个 `queued/running` 任务（SQLite 部分唯一索引
  兜底，并发提交也只会产生一个）。
- `follows.scan` 最多 2 次：仅当失败属于临时故障、且**尚未写入任何一项结果**
  时按 30s → 90s 退避重试；已落库的结果不重跑，避免重复写事件。
- `kb.rebuild` 不自动重试：失败多为确定性原因（上游路径、磁盘、模型文件）。
- 服务重启时 `running` 一律标 `interrupted`（“服务重启中断”），**不自动重跑**；
  需要时用 `POST /api/jobs/{id}/retry`。停机时正在跑的任务也按这个规则收尾。

进度只报真实值：关注扫描报 `done/total/current`（每查完一项更新），索引重建
拿不到内部进度，只报 `phase`（starting / building / reloading），没有百分比。

`QUEUE_MODE=inline`（或 `QUEUE_ENABLED=false`）时全部退回同步执行；worker 线程
没起来时接口返回 `503`，不会静默改走同步。单个任务被取消、失败、中断都能在
`GET /api/jobs` 里看到原因。

索引重建的 HTTP 触发默认关闭：查过上游 `build_index` 的写盘逻辑，**不是整体
原子**——单个文件（chunks / pickle / embedding 缓存）是「临时文件 + os.replace」，
但 Chroma 是增量持久化、manifest 最后写且直接 `write_text`，中途被杀会留下
半成品索引。日常仍用 `python scripts/build_kb_index.py` 或
`docker compose run --rm app python scripts/build_kb_index.py`；确认原子性后再把
`KB_REBUILD_API` 打开。

## 容器化部署（Docker）

镜像只装运行依赖与应用代码；`data/`、`.env`、上游 lolita-rag 全部以挂载方式接进来，不烤进镜像。

前置：Docker + Compose v2（BuildKit）、与默认布局一致的 `../lolita-rag` checkout，以及填好 `LLM_API_KEY` 的 `.env`。

```bash
mkdir -p data                      # 首次：给容器内非 root 用户一个可写的数据目录
docker compose up -d --build
docker compose ps                  # STATUS 一栏应显示 healthy
curl http://127.0.0.1:8787/api/health
```

- **数据目录**：宿主机 `./data` → 容器 `/data`（`DATA_DIR`）。`kbindex`、`app.db`、lolibrary 档案、轨迹都在这里，容器重建不丢。还没有 `own` 索引时先建一次：
  ```bash
  docker compose run --rm app python scripts/build_kb_index.py
  ```
- **配置**：`.env` 只读挂载到 `/app/.env`；容器内 `LOLITA_RAG_ROOT` 固定为 `/opt/lolita-rag`，对应宿主机 `${LOLITA_RAG_ROOT:-../lolita-rag}`。上游目录只读挂载，镜像里没有它的代码与索引。
- **上游依赖**：构建时通过 BuildKit 命名上下文 `rag` 读取上游的 `requirements.txt`（只读清单，不拷源码）。上游不在默认位置时：
  ```bash
  LOLITA_RAG_CTX=/srv/lolita-rag LOLITA_RAG_ROOT=/srv/lolita-rag docker compose up -d --build
  ```
- **健康检查**：镜像与 Compose 都打 `GET /api/health`，返回版本、`own`/`real` 索引状态与六个通道的就绪情况。它只报状态，索引未建也返回 200，因此这是存活检查而不是就绪门槛。查看容器状态：
  ```bash
  docker inspect --format '{{.State.Health.Status}}' $(docker compose ps -q app)
  ```
- **端口**：默认只发布到 `127.0.0.1:8787`。要给手机/局域网访问，把 compose 里 ports 的 `127.0.0.1:` 去掉，并自行加访问控制。

### 停止与优雅停机

`docker compose stop` / `down` 发送 SIGTERM。镜像以 `uvicorn --timeout-graceful-shutdown 20` 启动：先停止接新连接，再最多等 20 秒让进行中的请求（含 SSE 问答）收尾；Compose 的 `stop_grace_period` 是 30 秒，留出余量。应用 lifespan 没有需要额外关闭的全局资源（SQLite 每次请求开关连接），所以不需要自己写信号处理。

### 可选通道

小红书 Playwright / Spider_XHS 的依赖没有装进镜像（与 `requirements.txt` 的可选策略一致）。需要时在镜像里另装，或保持 `XHS_ENABLED=false`；其余通道不受影响。
