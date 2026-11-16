# AGENTS.md

本文件写给在本仓库里动手的人（也写给替你动手的 agent）。它只讲一件事：**改动怎么进仓库**。

内容层面的规矩（数据来源、可信度、内容边界）在 [README](README.md) 里，这里不重复。

## 本仓库的 Git 协作协议

### 适用范围

本仓库默认**直接在 `main` 上开发**：所有提交直接落 `main`，不再维护长期并行的功能分支。需要试验或大范围改动时，可以开临时分支（`feat/xxx`、`fix/xxx`），但要及时合并回 `main`，不长期保留。

### 开工前

- 先 `git status` 确认工作区干净，有未提交改动就先说明情况再继续。
- 确认 `.env` 存在，缺了就从模板复制（Windows `copy .env.example .env`，macOS/Linux `cp .env.example .env`），再把 `LOLITA_RAG_ROOT` 改成上游 lolita-rag 仓库的真实路径。
- 缺 `LOLITA_RAG_ROOT` 时 `pytest` 会失败（当前是 `tests/test_api.py::test_status_reports_channels_without_key` 1 项），报错形似代码故障，实际是环境没配；`.env` 不进 git（理由见「提交」一节）。
- `git fetch` 后直接在 `main` 上工作；确需试验时再开临时分支（`feat/xxx`、`fix/xxx`），完成后立即合并回 `main`。
- 本仓库同一时刻只跑一个写代码的任务：多个会话共享同一工作目录，并发会互相踩 Git 状态和文件。

### 提交

- 只添加与任务相关的文件，不要用 `git add -A` 或 `git add .`。
- 绝不提交 `.env`、密钥、凭据、`data/` 下的运行时数据与索引（这些本来就在 .gitignore 里，漏网时以不提交为准）。
- 提交信息说明"为什么改"而不是"改了哪些行"；一个任务产生一个或几个语义化提交。

### 推送与合并

- 推送前必须跑测试和 lint，全绿才推。
- 默认直接提交并推送 `main`；临时分支用完即合并，不长期并行。
- 永不 force push。
- 推送或测试失败时停下来报告，不要反复重试硬推。

推送前要跑的命令：

必跑——单元测试（全量）：

```bash
python -m pytest
```

必跑——lint（增量口径：只对本次改动的文件要求全绿，整仓历史遗留不阻塞本次推送）。规则写在 `pyproject.toml`（ruff），工具在 `requirements-dev.txt` 里声明；没装过就先装开发依赖，再把本次改动的文件逐个列进去跑（下面是示例，替换成你实际改的文件）：

```bash
python -m pip install -r requirements-dev.txt
python -m ruff check app/main.py scripts/smoke.py
```

也可以直接 `python -m ruff check .` 看全貌，但整仓不为 0 不作为阻塞条件（历史遗留由单独任务清理）。

提交前（改动涉及召回、合并层或知识库内容时必跑）——检索回归门禁：

```bash
python -m eval.run --subset quick --min-recall1 0.66 --min-cite1 0.75
```

12 条金标子集（own 4 / real 5 / lolibrary 3），本机 14–18 秒（含模型装载）。退出码非 0 就别提交：报告会直接列出排名掉的条目，在 `eval/reports/<时间戳>/report.md`。只想看数字、不判红绿时去掉 `--min-*`。

PR CI 里的 `offline-eval-gate` **只做结构性冒烟，不代表检索质量**：托管 runner 没有 real / lolibrary 两份不进 git 的索引，这一步用 hash 向量现场重建 own 索引，只跑 quick 里 4 条 own（`--subset quick-offline`），守的是金标格式、索引能建、own 管线不断。真实检索质量由本机 quick / terms 门禁保证；另有 `.github/workflows/eval-daily.yml` 可在有数据、能下载模型的 self-hosted runner 上用真实 BGE 重跑 quick + terms 留痕，但它**不再定时触发**，只能在 runner 在线时手动 `gh workflow run eval-daily.yml`：runner 不常驻（跑之前自己起 `F:\actions-runner\run.cmd`），离线时 dispatch 出去的 job 会一直停在 queued，不会自动开跑。模型不可用时该 workflow 直接失败，不会降级成 hash 凑数。手工触发的离线门禁用 `.github/workflows/eval-gate.yml`。

可选——端到端冒烟，需要服务在跑（另开一个终端 `python -m app.main`，默认 http://127.0.0.1:8787）：

```bash
python scripts/smoke.py
```

它只测首页、`/api/chat/status`、未配 key 时 `/api/chat` 的 503、`/api/follows`，不需要模型 key；服务不在默认地址时用 `python scripts/smoke.py --base-url http://127.0.0.1:<port>`。

### 冲突处理

冲突分两类，处理方式不同：

- 机械型：锁文件、生成物、纯格式化差异、只是相邻行号偏移。可以自动解决，但必须在 PR 描述里写明"这里我按什么依据解的"。
- 语义型：同一段逻辑被两边各改了一版。不要自动合并，停下来把两边的差异和判断依据摆出来，等人确认。

不许用"选一边、丢掉另一边"的方式糊过语义冲突：测试往往还能过，错误会被静默埋进去。

### 完成后回报

分支名、提交列表、测试与 lint 结果、PR 链接，以及过程中任何一次降级或跳过检查的原因。

## Agent skills

### Issue tracker

Issues and specs live as GitHub issues in `passer423/lolita-agents`, managed with the `gh` CLI. See `docs/agents/issue-tracker.md`.

### Triage labels

Default vocabulary: `needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context layout: `GLOSSARY.md` + `docs/adr/` at the repo root. See `docs/agents/domain.md`.
