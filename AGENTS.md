# AGENTS.md

本文件写给在本仓库里动手的人（也写给替你动手的 agent）。它只讲一件事：**改动怎么进仓库**。

内容层面的规矩（数据来源、可信度、内容边界）在 [README](README.md) 里，这里不重复。

## 本仓库的 Git 协作协议

### 适用范围

本仓库默认模式为「分支 + PR」：任何会改变代码或内容的改动，都走特性分支并通过 PR 合并，不直接推送 main。

### 开工前

- 先 `git status` 确认工作区干净，有未提交改动就先说明情况再继续。
- 确认 `.env` 存在，缺了就从模板复制（Windows `copy .env.example .env`，macOS/Linux `cp .env.example .env`），再把 `LOLITA_RAG_ROOT` 改成上游 lolita-rag 仓库的真实路径。
- 缺 `LOLITA_RAG_ROOT` 时 `pytest` 会失败（当前是 `tests/test_api.py::test_status_reports_channels_without_key` 1 项），报错形似代码故障，实际是环境没配；`.env` 不进 git（理由见「提交」一节）。
- `git fetch` 后再开分支，分支名形如 `feat/xxx`、`fix/xxx`。
- 本仓库同一时刻只跑一个写代码的任务：多个会话共享同一工作目录，并发会互相踩 Git 状态和文件。

### 提交

- 只添加与任务相关的文件，不要用 `git add -A` 或 `git add .`。
- 绝不提交 `.env`、密钥、凭据、`data/` 下的运行时数据与索引（这些本来就在 .gitignore 里，漏网时以不提交为准）。
- 提交信息说明"为什么改"而不是"改了哪些行"；一个任务产生一个或几个语义化提交。

### 推送与合并

- 推送前必须跑测试和 lint，全绿才推。
- 只推 `feat/xxx` 这类特性分支，然后开 PR 交给人审阅合并。
- 永不 force push；永不直接 push 到 main 或 master。
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
