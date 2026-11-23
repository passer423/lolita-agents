# 部署方案：香港轻量云（已选）与备选路线

本文只写方案与可复制的配置，不含任何真实凭据。所有变量只写名字，值在服务器上填。

## 一、决策与推荐

**已选定：香港轻量云服务器（2 核 4G）+ 自有域名 + systemd + nginx + Let's Encrypt。**
香港节点不需要 ICP 备案，A 记录生效后当天可上线；2C4G 是本项目能稳定跑起来的最低配置（原因见 3.1）。

保留的备选：

- 只想临时给人看：本机 + cloudflared 隧道（第四节），关机即断。
- 将来要迁回大陆节点：配置基本复用，只多一步 ICP 备案（第五节）。
- 国内零成本方案：魔搭免费 Docker 创空间，一句话结论见第九节备忘。

## 二、当前仓库的部署基线（按实际代码核对）

| 事实 | 出处 / 含义 |
|---|---|
| 默认 `BIND_HOST=127.0.0.1`、`BIND_PORT=8787` | [app/config.py](../app/config.py) 的 `bind_host` / `bind_port` |
| 只有 `/api/*` 受鉴权中间件管；首页与静态资源公开 | [app/auth.py](../app/auth.py) 的 `PUBLIC_API_PATHS` |
| 前端页面不发送任何凭据头 | [app/static/chat.js](../app/static/chat.js) 的 `fetch` 只带 `Content-Type` |
| `AUTH_MODE=off` 时，限流与日配额依然生效（按 IP 分桶） | [app/auth.py](../app/auth.py) 的 `_allow_request` |
| 日配额只拦 `POST /api/chat` 与 `/api/chat/stream`，且模型必须在 `LLM_PRICE_TABLE` 里有价格 | [app/auth.py](../app/auth.py) 的 `COST_PATHS` / `_today_cost` |
| 两份索引都不进 git：`own` 在 `DATA_DIR/kbindex`，`real` 在上游 `lolita-rag/data/index` | README「知识库索引与上游路径」 |
| 上游 `data/raw`、`data/lolibrary` 同样不进 git，`real` 索引无法从公开仓库重建 | [.gitignore](../.gitignore)、上游 `lolita-rag` |
| 上游 embedding 默认 `BAAI/bge-small-zh-v1.5`，`embedding_allow_download=False` | 上游 `src/config.py` |
| 评测时 BGE 加载阶段 python 峰值内存约 968 MB（本机实测） | 上一轮评测内存采样 |

> 两个直接影响部署的结论：
> 1. **应用层 `AUTH_MODE=api_key/jwt` 会把网页锁死**（页面不带凭据，`/api/*` 全 401）。给面试官看页面要用 nginx basic auth，应用层保持 `off`。
> 2. **香港机器必须从本机 rsync 数据**：`../lolita-rag/data`（含 `index/`、`raw/`、`lolibrary` 相关缓存）与 `lolita-agents/data`（含 `kbindex/`、`app.db`），公开 clone 拿不到这些。

## 三、路线 C：香港轻量云 + 域名（执行方案）

### 3.1 规格：2C4G 是舒适档，2C2G 只在专项配置下可跑

| 组成 | 内存占用（实测/推算） |
|---|---|
| BGE 加载阶段 python 峰值 | 约 968 MB（实测，仅模型加载） |
| 加载后常驻（torch + 模型 + Chroma + uvicorn） | 约 1.2–1.8 GB |
| nginx + systemd + 系统基础 | 约 200–300 MB |
| 演示时 2–3 人并发问答 | 每路 SSE 额外数百 MB 波动 |

- **2C2G 按 2C4G 的默认参数跑会在首次问答时 OOM 或严重 swap**：968 MB 的加载峰值叠加常驻与系统开销，2 GB 没有余量，表现为第一次提问卡住甚至被 OOM killer 杀掉。
- **2C4G 是舒适配置**：模型常驻 + 2–3 并发 + 系统还有约 1 GB 余量；配合 2 GB swap 足以覆盖 pip 安装、索引重建这类短时峰值。
- **2C2G 只在"单次演示提问、无并发"下可跑**：必须按 3.14 的专项配置（2 GB swap、单 worker、串行请求、关 OCR/微博）执行，否则首问就会 OOM 或长时间 swap。没有并发余量，第二个同时提问的人会被 nginx 直接挡成 503。
- **要开 reranker / OCR / Playwright 就上 8G**：reranker 模型 1.08 GB、Chromium 每上下文 300–500 MB，4 GB 会互相挤。
- 磁盘 40 GB：venv 3–6 GB（torch 占大头）、索引 100 MB、模型缓存约 100 MB、swap 2 GB，留出日志与更新余量。若用默认 PyPI 装 torch 会多拉 2 GB+ CUDA 依赖，按 3.7 的 CPU 源安装。

### 3.2 线路：CN2 / 优化线路对国内访问的影响

- 香港轻量的"峰值带宽"相同不代表体验相同：普通国际 BGP 线路从大陆访问要绕路，晚高峰丢包与抖动明显；标注 **CN2 GIA / 回国优化** 的套餐延迟更稳（华南约 20–40 ms，华东约 30–50 ms，普通线路可能 50–100 ms 且波动大）。
- SSE 对丢包敏感：丢包会让逐字输出停顿甚至断开，所以演示用机优先选优化线路，而不是只看带宽数字。
- 下单前用 `mtr -rw <IP>`（或 itdog 多地 ping）从国内节点实测 5 分钟，看晚高峰是否有 5% 以上丢包；有就换线路。
- 轻量套餐常见 30 Mbps 峰值 + 每月流量包；本项目的对外流量主要是 SSE 文本，几百次问答远低于流量上限，图片/OCR 才会放大占用。

### 3.3 变量表（先填这里，再套命令）

| 变量 | 含义 | 示例/取值 |
|---|---|---|
| `${DOMAIN}` | 演示域名 | `demo.example.com` |
| `${EMAIL}` | 证书到期通知邮箱 | 你自己的邮箱 |
| `${SERVER_IP}` | 香港轻量公网 IP | 控制台查看 |
| `${SSH_USER}` | 初始 SSH 用户 | Ubuntu 镜像通常是 `ubuntu` |
| `${APP_DIR}` | 仓库目录 | `/srv/lolita-agents` |
| `${UPSTREAM_DIR}` | 上游目录 | `/srv/lolita-rag` |
| `${DATA_DIR}` | 运行时数据 | `${APP_DIR}/data` |
| `${APP_PORT}` | 应用监听端口 | `8787` |
| `${DEMO_USER}` | 演示口令用户名（可选） | `demo` |

### 3.4 系统初始化（Ubuntu 22.04 / 24.04）

2C2G 档直接用仓库里的脚本（幂等、失败即退出），等价于下面的手工步骤：

```bash
sudo bash deploy/setup-2c2g.sh
# 路径可覆盖：sudo APP_DIR=/srv/lolita-agents RUN_USER=lolita bash deploy/setup-2c2g.sh
```

脚本做四件事：基础包、运行用户与目录、2 GiB swap（含 fstab 与 `vm.swappiness=30`）、ufw 放行 22/80/443。手工执行时对应的命令是：

```bash
# 1) 基础包
sudo apt update && sudo apt -y upgrade
sudo timedatectl set-timezone Asia/Shanghai
sudo apt install -y git python3 python3-venv python3-pip build-essential \
  libgomp1 curl ca-certificates rsync ufw nginx certbot apache2-utils

# 2) 运行用户与目录
sudo useradd -m -s /bin/bash lolita
sudo mkdir -p /srv && sudo chown lolita:lolita /srv

# 3) swap（2 GB，防安装与模型加载峰值）
sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile
sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
sudo swapoff /swapfile && sudo swapon -a
echo 'vm.swappiness=30' | sudo tee /etc/sysctl.d/99-swap.conf && sudo sysctl --system

# 4) 防火墙（云厂商安全组也要放行同样端口）
sudo ufw allow 22,80,443/tcp
sudo ufw --force enable
```

验证：`free -h` 能看到 2.0Gi swap；`sudo ufw status` 只有 22/80/443。

### 3.5 域名解析

1. 在域名控制台添加 A 记录：主机 `@`（或 `demo`）→ `${SERVER_IP}`，TTL 300。
2. 国内注册商的域名需要**域名实名认证**（和 ICP 备案是两件事，香港节点不需要备案），实名通过后解析才生效。
3. 验证（本地执行）：

```bash
nslookup ${DOMAIN} 8.8.8.8
dig +short ${DOMAIN}          # 应返回 ${SERVER_IP}
```

4. 解析未生效前不要签证书，Let's Encrypt 校验会失败。

### 3.6 代码与上游数据

```bash
sudo -iu lolita
cd /srv
git clone <本仓地址> lolita-agents
git clone <上游 lolita-rag 地址> lolita-rag

# 本机执行：把不进 git 的运行时数据同步上去（索引、档案、用户库）
rsync -avz --progress ./data/            ${SSH_USER}@${SERVER_IP}:${APP_DIR}/data/
rsync -avz --progress ../lolita-rag/data/ ${SSH_USER}@${SERVER_IP}:${UPSTREAM_DIR}/data/
```

- 需要同步的关键内容：`lolita-agents/data/kbindex`（own 索引）、`data/app.db`（会话/关注项）、上游 `data/index`（real 索引，约 100 MB）与 `data/raw`（否则以后无法重建 real 索引）。
- `data/logs`、`data/cache` 可以不同步，减少传输量。
- 验证：服务器上 `du -sh ${APP_DIR}/data ${UPSTREAM_DIR}/data`，两边目录结构与本地一致。

### 3.7 Python 环境与模型预置

```bash
cd ${APP_DIR}
python3 -m venv .venv
.venv/bin/pip install -U pip
# CPU 版 torch，避免拉取 2 GB+ 的 CUDA 依赖
.venv/bin/pip install --index-url https://download.pytorch.org/whl/cpu torch
.venv/bin/pip install -r ${UPSTREAM_DIR}/requirements.txt
.venv/bin/pip install -r requirements.txt
# 2C2G 档用 deploy/env/2c2g.env.example（含压小取值与关闭开关），不要用通用 .env.example
cp deploy/env/2c2g.env.example .env && chmod 600 .env
```

模型（默认 `embedding_allow_download=False`，不会自动下载）：

```bash
# 方式一：从本机同步 HF 缓存（推荐，免依赖外网）
# 本机：rsync -avz ~/.cache/huggingface/hub/models--BAAI--bge-small-zh-v1.5 \
#   ${SSH_USER}@${SERVER_IP}:/home/lolita/.cache/huggingface/hub/

# 方式二：允许首次下载（用国内镜像）
EMBEDDING_ALLOW_DOWNLOAD=true HF_ENDPOINT=https://hf-mirror.com \
  .venv/bin/python -c "from sentence_transformers import SentenceTransformer as S; S('BAAI/bge-small-zh-v1.5')"
```

验证：`ls ~/.cache/huggingface/hub | grep bge-small` 存在；上面命令无报错。可选 reranker（1.08 GB）默认关闭，4G 机器不要开。

### 3.8 两份索引：构建或校验

已有索引（3.6 已 rsync）时只需校验，不必重建：

```bash
curl -s http://127.0.0.1:${APP_PORT}/api/kb/status
```

- `own.ready=true`，`chunks` 与 `content/kb` 规模相称（本机 22 个源文件 → 0.9 MB）。
- `real.ready=true`，上游存在 `data/index/chroma/` 与 `data/index/manifest.json`（本机共 100.4 MB）。
- `error` 为空，`built_at` 是预期日期。

需要重建时：

```bash
# own
cd ${APP_DIR} && .venv/bin/python scripts/build_kb_index.py --rebuild
# real（依赖上游 data/raw，公开仓库里没有）
cd ${UPSTREAM_DIR} && ${APP_DIR}/.venv/bin/python scripts/build_index.py --source raw
```

任何一项 `ready=false` 都先按 `error` 修，再继续后面步骤。

### 3.9 `.env`（只写变量名与建议值，密钥现场填）

2C2G 档直接用 [deploy/env/2c2g.env.example](../deploy/env/2c2g.env.example)：它已包含下面的通用项、2C2G 的压小取值与关闭开关，逐项带注释。本节余下内容用于核对字段含义。

```dotenv
BIND_HOST=127.0.0.1
BIND_PORT=8787
DATA_DIR=/srv/lolita-agents/data
LOLITA_RAG_ROOT=/srv/lolita-rag

LLM_API_KEY=<现场填，不要提交>
LLM_BASE_URL=https://api.deepseek.com/v1
LLM_MODEL=deepseek-chat
WRITER_MAX_TOKENS=1800

AUTH_MODE=off
RATE_LIMIT_REQUESTS=30
RATE_LIMIT_WINDOW=60
DAILY_QUOTA=20
LLM_PRICE_TABLE={"deepseek-chat": {"prompt_per_1k": 0.002, "completion_per_1k": 0.008}}
LLM_PRICE_CURRENCY=CNY

XHS_ENABLED=false
SEARCH_ENABLED=false
WEIBO_DIRECT_ENABLED=false
OCR_ENABLED=false
```

- `DATA_DIR` 用绝对路径，避免 systemd 工作目录变化导致找不到索引。
- 配额能生效的前提是模型在 `LLM_PRICE_TABLE` 里；未定价模型会被跳过配额。
- 小红书/搜索/微博直抓在服务器上不可用（见第七节），显式关掉，状态接口会带原因显示 not ready。
- 2C2G 档把 `WRITER_MAX_TOKENS` 降到 1200、`EVIDENCE_MAX_ITEMS` 降到 8、`AGENT_DEADLINE` 降到 45，并额外关掉 `WEB_BROWSER_ENABLED`；以 `deploy/env/2c2g.env.example` 为准。

### 3.10 systemd unit

完整文件在仓库里：[deploy/systemd/lolita-agents.service](../deploy/systemd/lolita-agents.service)（2C2G 档：单 worker、`--limit-concurrency 8`、`TimeoutStopSec=45`、可选 `MemoryHigh=1536M`）。

```bash
sudo cp deploy/systemd/lolita-agents.service /etc/systemd/system/lolita-agents.service
sudoedit /etc/systemd/system/lolita-agents.service   # 替换 __APP_DIR__ / __RUN_USER__ / __RUN_GROUP__
sudo systemctl daemon-reload
sudo systemctl enable --now lolita-agents
curl -s http://127.0.0.1:8787/api/health
sudo systemctl restart lolita-agents && sleep 3 && curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8787/api/health
```

- `--proxy-headers --forwarded-allow-ips=127.0.0.1`：让限流拿到 nginx 传的真实客户端 IP（uvicorn 默认信任回环上的 X-Forwarded-For，这里写死更明确）。
- 首次请求会加载 BGE（约 10–30 秒），systemd 不会因此重启；但健康检查别设太短。
- 2C4G 档可放宽 `--limit-concurrency`、把 `TimeoutStopSec` 调到 40；2C2G 档不要改这些值。

### 3.11 nginx 配置

完整文件在仓库里：

- [deploy/nginx/lolita.conf](../deploy/nginx/lolita.conf)：80 跳 443、SSE 参数、`/api/chat` 的连接与速率限制，其余路径不限流。
- [deploy/nginx/limits.conf](../deploy/nginx/limits.conf)：`limit_conn_zone` / `limit_req_zone` 定义，必须放在 `/etc/nginx/conf.d/`（http 上下文）。

```bash
sudo cp deploy/nginx/limits.conf /etc/nginx/conf.d/lolita-limits.conf
sudo cp deploy/nginx/lolita.conf /etc/nginx/sites-available/lolita.conf
sudoedit /etc/nginx/sites-available/lolita.conf      # 替换 __DOMAIN__
sudo ln -sf /etc/nginx/sites-available/lolita.conf /etc/nginx/sites-enabled/lolita.conf
sudo rm -f /etc/nginx/sites-enabled/default
```

- 证书签发前 `nginx -t` 会因 443 缺证书而失败，先按 3.12 签完再 reload。
- 2C2G 档的取值：`limit_conn chat_conn 1` + `limit_req burst=1 nodelay` 只作用于 `^/api/chat(/stream)?$`；SSE 读超时 90s（对应 `AGENT_DEADLINE=60`，调成 45 时改 75s）。
- 第二个同时提问返回 **503** 是预期保护，不要为了"看起来能并发"删掉限流。
- Ubuntu 24.04 的 nginx 1.24 支持 `listen 443 ssl http2`；nginx ≥1.25.1 改成 `listen 443 ssl;` + 单独一行 `http2 on;`。
- 口令文件生成（密码在终端输入，不写进仓库）：

```bash
sudo htpasswd -c /etc/nginx/.htpasswd __DEMO_USER__
sudo chmod 640 /etc/nginx/.htpasswd && sudo chown root:www-data /etc/nginx/.htpasswd
```

### 3.12 Let's Encrypt 证书与自动续期

首次签发用 standalone（此时 443 证书还不存在，`nginx -t` 必然失败）：

```bash
sudo systemctl stop nginx
sudo certbot certonly --standalone -d __DOMAIN__ \
  --email __EMAIL__ --agree-tos --no-eff-email \
  --pre-hook "systemctl stop nginx" --post-hook "systemctl start nginx"
sudo systemctl start nginx
```

`--pre-hook` / `--post-hook` 会写进续期配置，之后 `certbot renew` 会自己短暂停 nginx。

证书就位后再校验并加载 3.11 的完整配置：

```bash
sudo nginx -t
sudo systemctl reload nginx
curl -sI https://__DOMAIN__/api/health | head -n 3   # 期望 200
```

续期：

```bash
sudo systemctl list-timers | grep certbot       # 应有 certbot.timer
sudo certbot renew --dry-run                    # 必须成功
# 续期后自动 reload nginx
sudo tee /etc/letsencrypt/renewal-hooks/deploy/reload-nginx.sh >/dev/null <<'EOF'
#!/bin/sh
systemctl reload nginx
EOF
sudo chmod +x /etc/letsencrypt/renewal-hooks/deploy/reload-nginx.sh
```

### 3.13 上线验收（逐条打勾）

```bash
curl -s https://__DOMAIN__/api/health | python3 -m json.tool
curl -s https://__DOMAIN__/api/kb/status | python3 -m json.tool
```

- [ ] `indexes.own.ready=true` 且 `indexes.real.ready=true`
- [ ] `channels` 里 xhs/weibo/web 为 not ready 并带原因（预期）
- [ ] 浏览器打开首页，问一条 own 类问题，`[1]` 引用可点
- [ ] SSE 逐字输出（不是整段一次性出现）
- [ ] 超频返回 429；把 `DAILY_QUOTA` 临时调小能触发配额 429
- [ ] `systemctl restart lolita-agents` 后地址恢复
- [ ] 手机 4G/5G 各测一次（不只测办公室 Wi-Fi）
- [ ] `certbot renew --dry-run` 成功

### 3.14 低配档：2C2G（2 GiB）专项配置

目标：在 2 GiB 内存上**单次演示提问不 OOM**。代价要提前接受：同一时刻只服务一个人；首次提问 25–60 秒（加载模型 + swap 换页），之后 8–20 秒；任何并发、OCR、浏览器内核都会把它推过线。

可直接拷到服务器的文件都在 [deploy/](../deploy/README.md)：setup 脚本、`.env` 模板、systemd unit、nginx 两份配置；占位符替换清单与执行顺序见该目录的 README。下面只讲取值理由与验证口径。

#### 3.14.1 swap 2 GiB（创建 + 开机自动挂载）

已封装进 [deploy/setup-2c2g.sh](../deploy/setup-2c2g.sh)（幂等：已有 swap、fstab 条目、sysctl 都会跳过）。手工步骤等价于：`fallocate -l 2G /swapfile`（失败用 `dd`）→ `chmod 600` → `mkswap` → `swapon` → fstab 追加 `/swapfile none swap sw 0 0` → `vm.swappiness=30` 写入 `/etc/sysctl.d/99-swap.conf`。

验证（三条都要做）：

```bash
swapon --show                  # 应出现 /swapfile，SIZE 2G
free -h                        # Swap 行 total ≈ 2.0Gi
sudo swapoff /swapfile && sudo swapon -a && swapon --show   # 验证 fstab 能重新挂上
```

重启后复查一次 `swapon --show`，确认不是只在当前会话生效。

#### 3.14.2 systemd / uvicorn：单 worker、限制并发、留足停止时间

完整 unit 在 [deploy/systemd/lolita-agents.service](../deploy/systemd/lolita-agents.service)，安装：

```bash
sudo cp deploy/systemd/lolita-agents.service /etc/systemd/system/lolita-agents.service
sudoedit /etc/systemd/system/lolita-agents.service   # 替换 __APP_DIR__ / __RUN_USER__ / __RUN_GROUP__
sudo systemctl daemon-reload && sudo systemctl enable --now lolita-agents
```

- **为什么不能多 worker**：每个 worker 是独立进程，各自把 BGE 权重以匿名内存持有，单进程常驻约 1.2–1.8 GB；两个 worker 在 2 GiB 机器上必然 OOM。而且本项目的瓶颈是一次 LLM 调用 + 检索 I/O，不是 CPU 并行，单 worker 足够。
- `--limit-concurrency 8` 是兜底安全阀（页面加载会并行请求 html/js/css，不能压到 1–2，否则首屏就 503）；**对话串行化交给 nginx**（3.14.3）。
- `--timeout-graceful-shutdown 15` + `TimeoutStopSec=45`：给 SSE 收尾留时间，又不会让 `systemctl restart` 卡住；内存紧张时进程退出慢会连带 swap 抖动，45 秒是够用的上限。
- `MemoryHigh=1536M` 是可选项：让内核更早回收，防止 cgroup 内瞬间冲顶；若发现响应明显变慢，删掉这一行后 `daemon-reload`。

#### 3.14.3 nginx：对话串行到 1，SSE 参数按单次预算取值

完整配置在 [deploy/nginx/limits.conf](../deploy/nginx/limits.conf)（zone 定义，放 `/etc/nginx/conf.d/`）与 [deploy/nginx/lolita.conf](../deploy/nginx/lolita.conf)（server 段）。安装与校验见 3.11 和 deploy/README.md。

- `limit_conn chat_conn 1` + `limit_req zone=chat_req burst=1 nodelay` 只作用于 `~ ^/api/chat(/stream)?$`，页面静态资源、`/api/health`、`/api/kb/status` 不受限制，不会出现首屏加载被 503。
- 第二个同时提问会得到 **503**（nginx 主动挡），这是预期保护，不是崩溃；不要为了"看起来能并发"把 `limit_conn` 去掉。
- SSE 取 `proxy_buffering off`、`proxy_cache off`、`gzip off`、连接 5s、读写 90s；`AGENT_DEADLINE` 若调到 45 秒，读超时同步降到 75 秒，保持"预算 + 30 秒"的关系。
- 改完 `sudo nginx -t && sudo systemctl reload nginx`。

#### 3.14.4 这档必须关掉的功能（按内存开销取舍）

| 功能 | 内存/磁盘开销 | 2C2G 处置 |
|---|---|---|
| reranker（精排） | 模型 1.08 GB（磁盘）+ 常驻约 1 GB | **必须关**：`RERANKER_ENABLED=false`（默认已是 false，显式写上防止被环境变量带开）；**等待量化后的实测占用再决定是否开启** |
| OCR（rapidocr） | 包约 181 MB（磁盘）+ 识别时 onnxruntime/模型 300–500 MB | **默认关**：配置里 `ocr_enabled` 默认是 `true`，必须显式 `OCR_ENABLED=false` |
| 微博直抓（Playwright） | Chromium 约 300 MB（磁盘）+ 系统依赖约 200 MB + 每上下文 300–500 MB | **关**：`WEIBO_DIRECT_ENABLED=false`；它还需要搜索 key 才会触发 |
| 小红书 | Playwright/外部仓库 + cookie | **关**：`XHS_ENABLED=false`（服务器上本来就不可用） |
| 搜索通道 | 无本地模型，但需要 key 且配额已耗尽 | **关**：`SEARCH_ENABLED=false` |
| 无 key 网页来源 | 同样要起浏览器内核（Bing/360） | **关**：`WEB_BROWSER_ENABLED=false` |

**OCR 与微博直抓二选一，推荐 OCR**（若演示必须展示一项）：

- OCR 是按需触发、用完即释放，可以限流到 `OCR_MAX_ITEMS=2`、`OCR_IMAGES_PER_ITEM=1`、`OCR_TOTAL_BUDGET=6`，峰值 300–500 MB 且只在"价格标在图上"这类问题出现。
- 微博直抓要常驻浏览器内核，冷启动与页面渲染更重，还叠加登录态/风控不确定性；在 2 GiB 上和模型同时存在基本没有余量。
- 两者都不要与另一次对话同时发生；演示顺序上先做不触发 OCR 的题目，最后再演示 OCR。

2C2G 的全部覆盖项（含逐项注释）在 [deploy/env/2c2g.env.example](../deploy/env/2c2g.env.example)。核心是 `TOP_K=1`、`EVIDENCE_MAX_ITEMS=8`、`WRITER_MAX_TOKENS=1200`、`AGENT_DEADLINE=45`，以及上面表格里的关闭项；若演示必须保留 OCR，按该文件里注释掉的那组小预算启用。

#### 3.14.5 起服务后怎么确认没顶到天花板

```bash
PID=$(systemctl show -p MainPID --value lolita-agents)
ps -o pid,rss,vsz,etime,cmd -p "$PID"          # RSS 单位 KB
grep -E 'VmRSS|VmSwap' /proc/$PID/status
free -h; swapon --show
vmstat 1 5                                      # 看 si/so 是否持续非 0（持续换页=已经在悬崖边）
systemd-cgtop -1 -m --depth=1 | head            # cgroup 视角的当前内存
systemctl show -p NRestarts --value lolita-agents
```

判定标准（2 GiB 档）：

| 指标 | 健康 | 危险 |
|---|---|---|
| python RSS | 1.1–1.5 GB | > 1.7 GB |
| swap 已用 | 200–600 MB，空闲时稳定 | 持续增长 / 空闲也在换页 |
| `vmstat` si/so | 空闲时 ≈ 0 | 连续多秒非 0 |
| NRestarts | 0 | 任意增长（说明被 OOM/异常重启） |

并发验证（第 0 次先预热，避免把"冷启动"当成问题）：

```bash
# 预热一次，等流结束
curl -N -s http://127.0.0.1:8787/api/chat/stream \
  -H 'Content-Type: application/json' \
  -d '{"message":"JSK 和 OP 有什么区别"}'

# 终端 A：发起一条，保持 SSE
time curl -N -s https://__DOMAIN__/api/chat/stream -u __DEMO_USER__ \
  -H 'Content-Type: application/json' -d '{"message":"柄图是什么意思"}'

# 终端 B：同时再发一条，期望 503（nginx 挡），不是 502/超时
curl -s -o /dev/null -w '%{http_code}\n' https://__DOMAIN__/api/chat -u __DEMO_USER__ \
  -H 'Content-Type: application/json' -d '{"message":"裙撑长度怎么选"}'
```

期望：A 正常逐字输出；B 返回 503；两条都结束后 RSS/swap 回到上表健康区间，`NRestarts` 仍为 0，`dmesg -T | grep -i oom` 无输出。若 B 是 502/504 而不是 503，说明服务已经被打挂，按 3.14.6 处理。

#### 3.14.6 OOM 的判断依据与升配路径

**怎么确认是 OOM，而不是主动限流**：

```bash
systemctl show -p NRestarts --value lolita-agents        # 大于 0 且刚增长
journalctl -u lolita-agents -b --no-pager | grep -iE "killed|oom|signal 9"
dmesg -T | grep -iE "Out of memory|oom-kill"             # 应能看到 python 被 kill
```

- 主动限流：客户端拿到 **503**，服务仍在线，`NRestarts` 不变。
- OOM：客户端常见 502/504 或连接被重置，unit 自动重启，`NRestarts` +1，`dmesg` 有 oom-kill 记录。

**先做减法，再考虑升配**（顺序执行，每步后复测 3.14.5）：

1. 确认 `OCR_ENABLED=false`、`WEIBO_DIRECT_ENABLED=false`、`RERANKER_ENABLED=false`；若有残留的浏览器进程，`pgrep -af 'chrom|playwright'` 清掉。
2. 降 prompt 体积：`WRITER_MAX_TOKENS=1000`、`EVIDENCE_MAX_ITEMS=6`。
3. swap 从 2 GB 加到 4 GB（磁盘足够，40 GB 盘无压力）：

```bash
sudo swapoff /swapfile
sudo rm /swapfile
sudo fallocate -l 4G /swapfile && sudo chmod 600 /swapfile
sudo mkswap /swapfile && sudo swapon /swapfile
swapon --show
```

4. 仍然 OOM（首问就杀进程、swap 持续打满）→ 升配到 2C4G。

**升配路径**（轻量套餐）：轻量应用服务器的升配一般要求**先关机**，在控制台"升级套餐/变更配置"里选 2C4G，按剩余时长补差价；升级后公网 IP 通常保留（以控制台提示为准），带宽/流量档位可能变化。

```bash
# 升级并开机后逐项确认
swapon --show                     # swap 仍然挂载（fstab 没丢）
free -h                           # 看到 4 GiB
sudo systemctl restart lolita-agents
curl -s http://127.0.0.1:8787/api/health
```

升配后可以恢复 `WRITER_MAX_TOKENS=1800`、`EVIDENCE_MAX_ITEMS=12`、`AGENT_DEADLINE=60`，但仍不建议开 reranker；OCR 与微博直抓依旧不要同时开。

## 四、备选 A：本机临时预览（cloudflared 隧道）

- 隧道客户端跑在本机，从 `127.0.0.1:8787` 取流量，**不需要改 `BIND_HOST`**。
- quick tunnel 无鉴权，地址即入口；关机/断网即失效；暴露的是开发机。
- 需要口令就套一层带 basic auth 的 nginx，隧道指向那层端口。
- 只适合"今天先看一眼"，不作为面试正式链接。

## 五、备选 B：迁回大陆节点

配置与第三节完全复用，差异只有三点：

1. 绑定域名必须 ICP 备案：腾讯云初审 1–2 个工作日，管局审核最长 20 个工作日；要求境内节点、包年包月、购买时长 ≥3 个月、备案期间剩余有效期 ≥1 个月。
2. 备案前只能 `IP:端口` 访问（无 HTTPS）；80/443 上的未备案域名会被阻断。
3. 轻量价格：腾讯云 2 核 4G5M 活动 188 元/年（续费同价）、2 核 2G5M 三年约 398 元；阿里云轻量 2 核 2G 首年 38/68 元（续费约 459 元/年），ECS 经济型 99 元/年续费同价。域名与证书另计。

Docker 路线（无论香港还是大陆）仍属**未实测**：仓库有 [Dockerfile](../Dockerfile) 与 [docker-compose.yml](../docker-compose.yml)，但从未真实构建。首次必须验证：`chroma-hnswlib` 能否在 slim 镜像内编过、容器内 `import torch` 是否缺系统库、健康检查 `start_period` 是否够模型加载、`APP_UID` 与 `./data` 属主、宿主机能否访问 `/api/health`、`docker compose stop` 是否 30 秒内优雅退出；另需 Compose v2.17+ 才支持 `additional_contexts`。因此首选 systemd。

## 六、通道降级：公网展示时缺什么

| 通道 | 服务器上的实际状态 | 处理方式 |
|---|---|---|
| `own` / `real` / `lolibrary` | 索引与档案同步后可完整使用 | 演示主力 |
| `xhs` | **必然不可用**：需要登录态 cookie（与设备/风控绑定）或 Playwright 扫码登录；服务器无交互登录条件 | `XHS_ENABLED=false`，状态显示 not ready 带原因，不伪造结果 |
| `weibo` / `web` | 需要 `SEARCH_ENABLED=true` + `SEARCH_API_KEY`，且当前配额已耗尽（智谱 1113） | 关闭；`WEIBO_DIRECT_*` 只是 1113 后的应急直抓，没有搜索 key 根本不会触发 |
| OCR | 需 `rapidocr-onnxruntime`（约 181 MB） | 4G 机器关闭，避免与模型抢内存 |

微博直抓若要真开：需要 `playwright` + Chromium（`playwright install --with-deps chromium`，浏览器约 300 MB、系统依赖约 200 MB，每个上下文 300–500 MB 内存），且仍需搜索 key 才会走到 fallback。2C4G 同时跑 Chromium 与 BGE 风险高，**不建议为面试开启**。

## 七、开放前必做

1. **鉴权在边缘**：nginx basic auth 开一个共享账号给面试官；应用层保持 `AUTH_MODE=off`（否则网页 401）。
2. **限流/配额取值**：`RATE_LIMIT_REQUESTS=30`、`RATE_LIMIT_WINDOW=60`；`DAILY_QUOTA=20`（元）。按本项目单次问答约 2–6k token 估算，20 元约等于 400–800 次问答，足够面试期使用；`LLM_PRICE_TABLE` 必须包含当前模型，否则配额不计数。
3. **提前预热**：面试前 10 分钟访问一次，触发 BGE 加载，避免面试官点开时等 10–30 秒。
4. **备份**：`${APP_DIR}/data/app.db` 是唯一不可再生数据（会话与关注项），上线前与演示后各拷一份。
5. **凭据纪律**：`.env` 600 权限、不进 git；`.htpasswd` 640 root:www-data；任何截图/文档不出现 key、cookie、密码。
6. **展示题库**（只依赖 own/real 索引，避开实时价格与现货问题）：
   - `JSK 和 OP 有什么区别`（own-08，术语+定义）
   - `柄图是什么意思`（own-06，术语）
   - `蕾丝和雪纺怎么洗才不勾丝`（own-02，洗护长文）
   - `裙撑长度怎么选`（own-03，选购建议）
   - `AP 和 Baby 的水洗标是什么意思 不能湿洗怎么办`（real-03，真实语料）
   - `林檎之森 OP 再贩的蓝色绿色会掉色吗`（real-01，真实语料）
   不要用"现在多少钱/有没有现货"这类问题，实时通道已关闭，会得到"没有可靠证据"的正确回答但观感不好。
7. **香港节点合规**：无需 ICP 备案，但网站内容仍需合规，演示页面不要放与项目无关的内容。

## 八、必须由用户自己完成的事（我无法代办）

1. **下单购买**香港轻量云服务器（2 核 4G，Ubuntu 22.04/24.04，优先 CN2/回国优化线路，40 GB 磁盘）。
2. **云账号实名认证**（香港节点不做 ICP 备案，但云厂商要求账号实名）。
3. **购买域名**并完成域名实名认证（国内注册商要求；与备案无关）。
4. **添加 A 记录**：`@` 或 `demo` → 服务器公网 IP；生效后告诉我。
5. **提供访问方式**：公网 IP、SSH 端口、登录用户；建议给我一个临时 SSH 公钥授权，演示结束后撤销。
6. **安全组放行** 22/80/443；若控制台另有防火墙，同样放行。
7. **决定演示口令**：自己执行 `htpasswd` 设置，口令不要发在聊天里；需要转交面试官时单独告知。

## 九、备忘：国内零成本方案（本次不采用）

魔搭 Docker 创空间免费 CPU 档（约 2 vCPU / 16 GB，¥0，平台域名免备案，端口固定 7860）是唯一可行的国内零成本路线；其闲置休眠与 SSE 是否被代理缓冲未能从官方文档确认，属于必须实测项，故本次不采用。
