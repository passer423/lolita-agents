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

### 3.1 规格：为什么必须是 2C4G，而不是 2C2G

| 组成 | 内存占用（实测/推算） |
|---|---|
| BGE 加载阶段 python 峰值 | 约 968 MB（实测，仅模型加载） |
| 加载后常驻（torch + 模型 + Chroma + uvicorn） | 约 1.2–1.8 GB |
| nginx + systemd + 系统基础 | 约 200–300 MB |
| 演示时 2–3 人并发问答 | 每路 SSE 额外数百 MB 波动 |

- **2C2G 会在首次问答时 OOM 或严重 swap**：968 MB 的加载峰值叠加常驻与系统开销，2 GB 没有余量，表现为第一次提问卡住甚至被 OOM killer 杀掉。
- **2C4G 是下限**：模型常驻 + 2–3 并发 + 系统还有约 1 GB 余量；配合 2 GB swap 足以覆盖 pip 安装、索引重建这类短时峰值。
- **要开 reranker / OCR / Playwright 就上 8G**：reranker 模型 1.08 GB、Chromium 每上下文 300–500 MB，4 GB 会互相挤。
- 磁盘 40 GB：venv 3–6 GB（torch 占大头）、索引 100 MB、模型缓存约 100 MB、swap 2 GB，留出日志与更新余量。若用默认 PyPI 装 torch 会多拉 2 GB+ CUDA 依赖，按 4.2 的 CPU 源安装。

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

```bash
# 1) 基础包
sudo apt update && sudo apt -y upgrade
sudo timedatectl set-timezone Asia/Shanghai
sudo apt install -y git python3.11 python3.11-venv python3-pip build-essential \
  libgomp1 curl ca-certificates rsync ufw nginx certbot apache2-utils

# 2) 运行用户与目录
sudo useradd -m -s /bin/bash lolita
sudo mkdir -p /srv && sudo chown lolita:lolita /srv

# 3) swap（2 GB，防安装与模型加载峰值）
sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile
sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
echo 'vm.swappiness=20' | sudo tee /etc/sysctl.d/99-swap.conf && sudo sysctl -p /etc/sysctl.d/99-swap.conf

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
python3.11 -m venv .venv
.venv/bin/pip install -U pip
# CPU 版 torch，避免拉取 2 GB+ 的 CUDA 依赖
.venv/bin/pip install --index-url https://download.pytorch.org/whl/cpu torch
.venv/bin/pip install -r ${UPSTREAM_DIR}/requirements.txt
.venv/bin/pip install -r requirements.txt
cp .env.example .env && chmod 600 .env
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

### 3.10 systemd unit（可复制）

`/etc/systemd/system/lolita-agents.service`：

```ini
[Unit]
Description=lolita-agents demo
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=lolita
Group=lolita
WorkingDirectory=/srv/lolita-agents
EnvironmentFile=/srv/lolita-agents/.env
ExecStart=/srv/lolita-agents/.venv/bin/uvicorn app.main:app \
  --host 127.0.0.1 --port 8787 \
  --proxy-headers --forwarded-allow-ips=127.0.0.1 \
  --timeout-graceful-shutdown 20
Restart=always
RestartSec=3
TimeoutStopSec=40
LimitNOFILE=65535

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now lolita-agents
curl -s http://127.0.0.1:8787/api/health
sudo systemctl restart lolita-agents && sleep 3 && curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8787/api/health
```

- `--proxy-headers --forwarded-allow-ips=127.0.0.1`：让限流拿到 nginx 传的真实客户端 IP（uvicorn 默认信任回环上的 X-Forwarded-For，这里写死更明确）。
- 首次请求会加载 BGE（约 10–30 秒），/systemd 不会因此重启；但健康检查别设太短。

### 3.11 nginx server 段（可复制）

```nginx
map $http_upgrade $connection_upgrade {
    default upgrade;
    ''      close;
}

server {
    listen 80;
    server_name __DOMAIN__;

    location /.well-known/acme-challenge/ {
        root /var/www/certbot;
    }
    location / {
        return 301 https://$host$request_uri;
    }
}

server {
    listen 443 ssl http2;
    server_name __DOMAIN__;

    ssl_certificate     /etc/letsencrypt/live/__DOMAIN__/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/__DOMAIN__/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_session_cache shared:SSL:10m;

    # 面试演示口令（可选但建议）：浏览器自动附带，页面与 SSE 都能用
    auth_basic "lolita demo";
    auth_basic_user_file /etc/nginx/.htpasswd;

    client_max_body_size 8m;

    location / {
        proxy_pass http://127.0.0.1:8787;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header Connection $connection_upgrade;

        # SSE 逐字返回的关键：不缓冲、不压缩、超时大于单次问答预算
        proxy_buffering off;
        proxy_cache off;
        gzip off;
        proxy_read_timeout 120s;
        proxy_send_timeout 120s;
    }
}
```

- Ubuntu 24.04 的 nginx 1.24 支持 `listen 443 ssl http2`；nginx ≥1.25.1 改成 `listen 443 ssl;` + 单独一行 `http2 on;`。
- `AGENT_DEADLINE` 默认 60 秒，读超时取 120 秒留余量；若调大预算，同步调大这里。
- 口令文件生成（密码在终端输入，不写进仓库）：

```bash
sudo htpasswd -c /etc/nginx/.htpasswd __DEMO_USER__
sudo chmod 640 /etc/nginx/.htpasswd && sudo chown root:www-data /etc/nginx/.htpasswd
```

### 3.12 Let's Encrypt 证书与自动续期

首次签发走 webroot（此时 443 段还没启用，先只放 80 段）：

```bash
sudo mkdir -p /var/www/certbot
sudo certbot certonly --webroot -w /var/www/certbot -d __DOMAIN__ \
  --email __EMAIL__ --agree-tos --no-eff-email
```

然后放入 3.11 的完整配置：

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
