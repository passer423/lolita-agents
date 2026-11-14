# 用 LangGraph 编排多路召回

多路召回（ADR-0001）把问答拆成「核心通道并行召回 → planner 按需补查 → 结果层合并 → 作答」。这套流程有状态、有条件分支、有并行扇出，手写 while 循环会越写越难评审。决定：**agent 编排层用 LangGraph 的 `StateGraph` 表达，替代 `app/agent/loop.py` 的手写循环**；`langchain-core` 提供工具与消息基础设施，模型节点走 OpenAI 兼容端点。

## Considered Options

- **继续手写 `loop.py` 生成器循环**：零依赖、流式已跑通，但「核心并行 + 条件补查 + 合并」的图结构全压进一个函数，护栏和业务交织，后续加通道、加中断恢复都要在循环里打补丁。
- **LangGraph `StateGraph`**：显式状态 + 节点/边/条件边 + 动态并行扇出（`Send`）+ checkpoint 持久化，正好对得上「有状态的编排」。图可视化后，评审「为什么这一步走了这个通道」不再靠读代码。
- **其它 agent 框架（AutoGen / CrewAI 等）**：多智能体协作模型，本项目是单 agent 的确定性召回流程，引入的是多余的心智负担。

## 决策细节

- 状态集中在一个 `AgentState`：`question` / `evidence` / `steps` / `stopped_reason` / `recalled`（已召回通道）。节点是纯函数，便于用脚本化 planner 注入测试。
- **核心通道并行扇出**用 `Send` 到三个通道节点（`own` / `real` / `lolibrary`），结果写回 `evidence`。
- **planner 补查是条件边**：合并核心结果后，`route` 节点判断是否 `xhs` / `weibo` / `web` 追加；不可用则跳过不猜。
- 工具保持「一源一通道、只读」，包装成 `langchain-core` 的 `Tool`；planner 的 JSON-in-text 协议保留（OpenAI 兼容端点对 function calling 支持不一致，见 `planner.py` 注释）。
- 流式复用同一张图：`astream_events` 吐 step / delta / final，非流式走 `invoke`。

## Consequences

- `loop.py` 的手写循环由图定义替代；`MAX_STEPS` / `MAX_EMPTY` / 引用越界 / 「没证据不许作答」四条护栏**逐条迁移**，用 `recursion_limit` + 状态计数器实现，不能丢。
- 轨迹（`trace.py`）改从 LangGraph 的状态/事件流取，不再从手写生成器取；评测的「轨迹级判定」读 `state_history`。
- 新增依赖：`langgraph` + `langchain-core`（+ `langchain-openai` 作模型节点）。
- checkpoint 能力为「关注项」的会话级状态持久化留了口子，但 v1 只用在单轮问答，不引入多轮中断恢复。
