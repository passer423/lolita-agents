# 2C2G 低配档部署文件

目标：在 **2 vCPU / 2 GiB** 的香港轻量机上，单次演示提问不 OOM。代价是**同一时刻只服务一个人**：并发、OCR、浏览器内核都会把它推过线。

原理、阈值与 OOM 判定见 [docs/deploy.md](../docs/deploy.md) 的 3.14；本目录只放可直接拷到服务器的文件。

## 文件与目标路径

| 仓库文件 | 服务器路径 | 作用 |
|---|---|---|
| `setup-2c2g.sh` | 仓库内直接执行 | 依赖、用户、目录、2 GiB swap、swappiness、ufw |
| `env/2c2g.env.example` | `/srv/lolita-agents/.env` | 2C2G 档全部覆盖项（需替换占位符） |
| `systemd/lolita-agents.service` | `/etc/systemd/system/lolita-agents.service` | 单 worker、并发与超时、可选 MemoryHigh |
| `nginx/limits.conf` | `/etc/nginx/conf.d/lolita-limits.conf` | `limit_conn_zone` / `limit_req_zone` 定义 |
| `nginx/lolita.conf` | `/etc/nginx/sites-available/lolita.conf` | 80 跳 443、SSE、对话限流、静态资源不限流 |

## 占位符替换清单

| 占位符 | 含义 | 示例 |
|---|---|---|
| `__APP_DIR__` | 仓库目录（绝对路径） | `/srv/lolita-agents` |
| `__RUN_USER__` | 运行用户 | `lolita` |
| `__RUN_GROUP__` | 运行用户组，通常与用户同名 | `lolita` |
| `__LOLITA_RAG_DIR__` | 上游 lolita-rag checkout | `/srv/lolita-rag` |
| `__LLM_API_KEY__` | 模型服务 key，只填在服务器本地 | 不写进仓库 |
| `__DOMAIN__` | 演示域名 | `demo.example.com` |

`__EMAIL__` 只出现在下面的证书命令里，不落在配置文件中。

替换后自检（不应再有输出）：

```bash
grep -rn '__[A-Z_]*__' /srv/lolita-agents/.env /etc/systemd/system/lolita-agents.service /etc/nginx/sites-available/lolita.conf
```

## 执行顺序

### 0. 机器确认

```bash
free -h        # 期望 total ≈ 2.0Gi
nproc          # 2
lsb_release -a # Ubuntu 22.04 / 24.04
```

云厂商安全组放行 22/80/443；DNS 的 A 记录已指向这台机器。

### 1. 系统准备（swap、依赖、目录、防火墙）

```bash
cd /srv/lolita-agents
sudo bash deploy/setup-2c2g.sh
```

幂等，可重复跑；失败即退出。验证：

```bash
swapon --show                  # /swapfile，SIZE 2G
free -h                        # Swap total ≈ 2.0Gi
sudo swapoff /swapfile && sudo swapon -a && swapon --show   # fstab 能重新挂上
cat /proc/sys/vm/swappiness    # 30
sudo ufw status                # 22/80/443
```

重启后再确认一次 `swapon --show`，确保不是只在当前会话生效。

### 2. 代码与不进 git 的数据

```bash
sudo -iu lolita
cd /srv/lolita-agents
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 本机执行（不在服务器上）：同步运行时数据
rsync -avz --progress ./data/            <SSH_USER>@<SERVER_IP>:/srv/lolita-agents/data/
rsync -avz --progress ../lolita-rag/data/ <SSH_USER>@<SERVER_IP>:/srv/lolita-rag/data/
```

不进 git 的两份索引与档案缺一不可，否则 `own` / `real` 通道空召回。

### 3. 环境文件

```bash
sudo cp deploy/env/2c2g.env.example /srv/lolita-agents/.env
sudo chmod 600 /srv/lolita-agents/.env
sudo chown lolita:lolita /srv/lolita-agents/.env
sudoedit /srv/lolita-agents/.env    # 替换 __APP_DIR__ / __LOLITA_RAG_DIR__ / __LLM_API_KEY__
```

验证：`grep -n '__' /srv/lolita-agents/.env` 无输出。

### 4. systemd

```bash
sudo cp deploy/systemd/lolita-agents.service /etc/systemd/system/lolita-agents.service
sudoedit /etc/systemd/system/lolita-agents.service   # 替换 __APP_DIR__ / __RUN_USER__ / __RUN_GROUP__
sudo systemctl daemon-reload
sudo systemctl enable --now lolita-agents
systemctl status lolita-agents --no-pager
curl -s http://127.0.0.1:8787/api/health | head -c 400
```

首次请求会加载 BGE（约 10–30 秒），健康检查别设太短。

### 5. nginx

```bash
sudo cp deploy/nginx/limits.conf /etc/nginx/conf.d/lolita-limits.conf
sudo cp deploy/nginx/lolita.conf /etc/nginx/sites-available/lolita.conf
sudoedit /etc/nginx/sites-available/lolita.conf      # 替换 __DOMAIN__
sudo ln -sf /etc/nginx/sites-available/lolita.conf /etc/nginx/sites-enabled/lolita.conf
sudo rm -f /etc/nginx/sites-enabled/default
```

**首次签证书前，`lolita.conf` 里的 443 段会因缺少证书而让 `nginx -t` 失败**，所以先签证书、再校验配置：

```bash
sudo systemctl stop nginx
sudo certbot certonly --standalone -d __DOMAIN__ \
  --email __EMAIL__ --agree-tos --no-eff-email \
  --pre-hook "systemctl stop nginx" --post-hook "systemctl start nginx"
sudo systemctl start nginx
sudo nginx -t && sudo systemctl reload nginx
```

pre/post hook 会写进续期配置，`certbot renew` 时会自动短暂停 nginx，不用手工干预。验证：

```bash
sudo certbot renew --dry-run
curl -sI https://__DOMAIN__/api/health | head -n 3
```

### 6. 演示口令（建议）

```bash
sudo htpasswd -c /etc/nginx/.htpasswd __DEMO_USER__   # 密码在终端输入，不写进仓库
sudo chmod 640 /etc/nginx/.htpasswd && sudo chown root:www-data /etc/nginx/.htpasswd
sudo systemctl reload nginx
```

### 7. 上线自检

```bash
curl -s -u __DEMO_USER__ https://__DOMAIN__/api/health | python3 -m json.tool
curl -s -u __DEMO_USER__ https://__DOMAIN__/api/kb/status | python3 -m json.tool
```

- [ ] `indexes.own.ready=true`、`indexes.real.ready=true`
- [ ] 浏览器打开首页，问一条 own 类问题，`[1]` 引用可点，SSE 逐字输出
- [ ] 同一时刻第二个提问返回 **503**（nginx 主动挡，不是 502/504）
- [ ] `ps -o rss` 与 `/proc/<pid>/status` 的 `VmRSS` 在 1.1–1.5 GB
- [ ] `free -h` 的 swap 用量 200–600 MB 且空闲时稳定
- [ ] `systemctl show -p NRestarts --value lolita-agents` 为 0

RSS、swap、并发与 OOM 判定细节见 [docs/deploy.md](../docs/deploy.md) 3.14.5–3.14.6。

## 注意

- **不要开多 worker**：每个 worker 独立持有 BGE 权重（1.2–1.8 GB），2 GiB 上两个必然 OOM。
- **不要开 reranker / OCR / 微博直抓**：见 `env/2c2g.env.example` 注释；reranker 的开关保持关闭，等待量化后的实测占用再决定是否开启。
- 本目录所有文件都不含真实 key、cookie、密码或 IP；口令与 key 只在服务器本地填写。
