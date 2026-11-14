# 用 LangGraph 编排多路召回

> 注：落地后与本文件最初描述有偏差，已按实际实现修订（2026-11-14）：工具不是 langchain `Tool`，流式不走 `astream_events`，依赖只有 `langgraph`。

多路召回（ADR-0001）把问答拆成「核心通道并行召回 → planner 按需补查 → 结果层合并 → 作答」。这套流程有状态、有条件分支、有并行扇出，手写 while 循环会越写越难评审。决定：**agent 编排层用 LangGraph 的 `StateGraph` 表达，替代 `app/agent/loop.py` 的手写循环**；工具层保持只读普通函数，模型调用直接走 `openai` SDK 的 OpenAI 兼容端点。

## Considered Options

- **继续手写 `loop.py` 生成器循环**：零依赖、流式已跑通，但「核心并行 + 条件补查 + 合并」的图结构全压进一个函数，护栏和业务交织，后续加通道、加中断恢复都要在循环里打补丁。
- **LangGraph `StateGraph`**：显式状态 + 节点/边/条件边 + 动态并行扇出（`Send`）+ checkpoint 持久化，正好对得上「有状态的编排」。图可视化后，评审「为什么这一步走了这个通道」不再靠读代码。
- **其它 agent 框架（AutoGen / CrewAI 等）**：多智能体协作模型，本项目是单 agent 的确定性召回流程，引入的是多余的心智负担。

## 决策细节

- 状态集中在一个 `AgentState`：`question` / `evidence` / `steps` / `stopped_reason` / `recalled`（已召回通道）。节点是纯函数，便于用脚本化 planner 注入测试。
- **核心通道并行扇出**用 `Send` 到三个通道节点（`own` / `real` / `lolibrary`），结果写回 `evidence`。
- **planner 补查是条件边**：合并核心结果后，`route` 节点判断是否 `xhs` / `weibo` / `web` 追加；不可用则跳过不猜。
- 工具保持「一源一通道、只读」的普通函数（`channels.recall`），不包装成 langchain `Tool`；planner 的 JSON-in-text 协议保留（OpenAI 兼容端点对 function calling 支持不一致，见 `planner.py` 注释）。
- 流式复用同一张图：后台线程执行 `graph.invoke`，进度与增量经队列回传，由路由手写 SSE 分帧；非流式直接 `invoke`。没有使用 `astream_events`。
- **合并层**在 `graph._normalize` 一处完成去重、排序与限量：按 URL（忽略锚点/末尾斜杠）或 `ref` 去重并保留 `source_kind` 更强的版本；排序是三级规则（本次补查通道 → 来源效力 → 通道顺序）；总数受 `EVIDENCE_MAX_ITEMS` 限制，补查通道整体豁免。不做跨通道相关性打分——三套检索量纲不可比。
- **real 通道的保守策略保留**：只留 `retriever` 含 `vector` 的命中、`TOP_K=1`。20 条代表性问题（16 条 real 金标 + 4 条同类）对比过放宽方案（k=3 + 含 BM25）：金标上严格策略 0 条空召回，放宽只在空召回时补上纯关键词重合的噪声（茶会视频、出物帖），不构成答案。真实语料是他人发言，宁可漏也不把道听途说当事实。

## Consequences

- `loop.py` 的手写循环由图定义替代；`MAX_STEPS` / `MAX_EMPTY` / 引用越界 / 「没证据不许作答」四条护栏**逐条迁移**，用 `recursion_limit` + 状态计数器实现，不能丢。
- 轨迹（`trace.py`）在 `run` / `stream` 结束时由 graph 统一写 `trace.append`（问题、回答、各通道命中与耗时），不再从手写生成器边跑边取；评测的「轨迹级判定」读 `results.csv` 里的逐步记录。
- 新增依赖只有 `langgraph`；`langchain-core` / `langchain-openai` 不是直接依赖（模型调用走 `openai` SDK，见 `requirements.txt` 注释）。
- checkpoint 能力为「关注项」的会话级状态持久化留了口子，但 v1 只用在单轮问答，不引入多轮中断恢复。
