"""单次规划：判断「要不要补查实时通道」，并提取召回意图。

## 为什么用 JSON-in-text 而不是原生 tool calling

DeepSeek 与其它 OpenAI 兼容端点对 function calling 的支持程度不一致（有的没有、
有的字段有差异）。JSON 协议在任何端点上都能跑，而且**测试里可以直接注入一个
脚本化 planner**，不必联网、不必有 key——图编排与护栏因此可以逐条钉死。

## planner 只做一次决策，不再「循环选工具」

多路召回的并行扇出交给 LangGraph（graph.py），planner 退化成**单次规划**：
核心通道（own/real/lolibrary）召回之后，判断要不要追加实时通道（xhs/weibo/web），
顺带从问句里提取一个精简检索词给实时通道用。

## 提示词里那几条硬规则是怎么来的

都是前几轮踩出来的：

* **空结果是有效结果** —— agent 最本能的错误是"换个说法再查一次，直到搜出东西"，
  这正好复现了上游花整篇 README 修掉的假阳性。核心通道空了，不能靠追加实时通道
  "骗"出结果。
* **实时通道只在问题确实需要时开** —— 价格、预约、新款、口碑、攻略、争议这些信息
  才有实时性；纯知识类问题（怎么洗、术语、历史）走核心通道就够了，多开一路是浪费
  且可能引入噪声。
"""

from __future__ import annotations

import json
import logging

from app.config import AppSettings

logger = logging.getLogger(__name__)

PLANNER_MAX_TOKENS = 400
PLANNER_TIMEOUT = 60.0

# 只有这些信息有「实时性」，核心通道（静态知识/档案）覆盖不到时才值得补查。
_REALTIME_MARKERS = (
    "价格", "预约", "预约价", "多少钱", "价格多少", "新款", "新作", "上新",
    "口碑", "评价", "测评", "种草", "避雷", "攻略", "怎么买", "哪里买",
    "现货", "现货价", "再贩", "再版", "抽选", "掉落",
)


class PlannerUnavailable(RuntimeError):
    """没有可用模型（未配置 key）。路由层翻译成 503。"""


class PlannerOutputError(RuntimeError):
    """模型返回的东西不是可用的规划。"""


def _core_summary_block(evidence: list[dict]) -> str:
    if not evidence:
        return "核心通道没有召回任何内容。"
    lines = []
    for item in evidence:
        title = item.get("title") or ""
        channel = item.get("channel") or ""
        lines.append(f"- [{channel}] {title}")
    return "核心通道已召回：\n" + "\n".join(lines)


def build_plan_prompt(question: str, core_evidence: list[dict]) -> str:
    """拼给模型看的单次规划提示词。"""
    summary = _core_summary_block(core_evidence)
    return f"""你是 Lolita 领域问答助手的**召回规划器**。

用户问题：
{question}

核心通道（自有知识库 own、上游真实语料 real、Lolibrary 档案 lolibrary）召回结果：
{summary}

## 你的任务
判断是否需要追加**实时通道**（xhs=小红书、weibo=微博、web=网页搜索），并给出精简检索词。

## 输出协议
只输出一个 JSON 对象，不要任何其它文字：

{{"supplementary": ["xhs", "weibo", "web"], "query": "Baby 缎带与齿轮 预约价"}}

* ``supplementary``：需要追加的实时通道名（从 xhs/weibo/web 里选，可以是空数组）。
* ``query``：给实时通道用的精简检索词（品牌/系列/货号/主题词）。没有就回填原问句。

## 硬规则
1. **空结果是有效结果**：核心通道空了，不代表要追加实时通道——它意味着"没有"。
2. 只有问题需要**实时信息**（价格、预约、新款、再贩、口碑、测评、攻略、争议、现货）
   且核心通道没覆盖时，才追加实时通道；纯知识类问题（怎么洗、术语、历史、尺码）一律不追加。
3. 不需要补查时，``supplementary`` 必须是 ``[]``，不要为了"多查一路"而开通道。
4. 最多开你确实需要的通道；别三个一起开。
"""


class DeepSeekPlanner:
    """走 OpenAI 兼容协议（默认 DeepSeek）。"""

    def __init__(self, app: AppSettings):
        if not app.llm_enabled:
            raise PlannerUnavailable(
                "助手需要配置模型：在 .env 里填 LLM_API_KEY"
                "（DeepSeek 用 https://api.deepseek.com/v1 + deepseek-chat）"
            )
        from openai import OpenAI

        self._client = OpenAI(
            api_key=app.llm_api_key.strip(),
            base_url=app.llm_base_url.strip(),
            timeout=PLANNER_TIMEOUT,
        )
        self._model = app.llm_model.strip()
        self.usage = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0}

    @property
    def model(self) -> str:
        return self._model

    def decide(self, question: str, core_evidence: list[dict], history: list[dict] | None = None) -> dict:
        messages: list[dict] = [
            {"role": "user", "content": build_plan_prompt(question, core_evidence)},
        ]
        try:
            response = self._client.chat.completions.create(
                model=self._model,
                messages=messages,
                temperature=0,
                max_tokens=PLANNER_MAX_TOKENS,
                response_format={"type": "json_object"},
            )
        except Exception as exc:  # noqa: BLE001 - 网络/鉴权失败要变成可读信息
            raise PlannerOutputError(f"模型调用失败：{type(exc).__name__}: {exc}") from exc

        self.usage["calls"] += 1
        usage = getattr(response, "usage", None)
        if usage is not None:
            self.usage["prompt_tokens"] += int(getattr(usage, "prompt_tokens", 0) or 0)
            self.usage["completion_tokens"] += int(getattr(usage, "completion_tokens", 0) or 0)

        text = (response.choices[0].message.content or "").strip()
        return parse_plan(text)


def parse_plan(text: str) -> dict:
    """解析模型输出。单独成函数是为了能单测——不用发请求就能验协议。"""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise PlannerOutputError(f"模型没有返回合法 JSON：{text[:80]}") from exc
    if not isinstance(data, dict):
        raise PlannerOutputError("模型返回的不是 JSON 对象")

    supplementary = data.get("supplementary", [])
    if supplementary is None:
        supplementary = []
    if not isinstance(supplementary, list) or not all(isinstance(x, str) for x in supplementary):
        raise PlannerOutputError("supplementary 必须是字符串数组")

    query = data.get("query")
    if query is None:
        query = ""
    if not isinstance(query, str):
        raise PlannerOutputError("query 必须是字符串")

    return {"supplementary": supplementary, "query": query.strip()}
