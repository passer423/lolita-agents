"""补查规划：**规则决定查不查，模型决定用什么词查**。

## 为什么补查决策不看召回结果

三个核心通道的覆盖能力是**结构性、事先已知**的：

* ``own``       术语 / 洗护 / 尺码 / 新手常识 —— 不含价格、库存、发售状态
* ``real``      采集的历史讨论 —— 不含当前价格
* ``lolibrary`` 品牌档案 —— 只有历史原价，不代表现价

所以「这条裙子现在多少钱」这类问题，核心通道**在任何情况下**都给不出答案。
这是通道定义决定的，不是"这次恰好没找到"。

让模型看召回结果判断「覆盖没覆盖」，等于让它拿标题硬凑；而标题看起来相关，
会诱导它说「已覆盖」，于是漏查。**漏查是最不可见的失败**——用户不会知道
本来还有实拍和现价。

所以补查决策退化成关键词规则（:func:`plan_channels`）：确定、免费、可单测。

## 模型只做一件事：把问题压成检索词

「我想买 Baby 那条缎带与齿轮的裙子现在还有没有货啊」→「Baby 缎带与齿轮 现货」。

这件事只有模型能干，而且**只在决定补查时才调用**——问「蕾丝怎么洗」不必付这次钱。

## 为什么用 JSON-in-text 而不是原生 tool calling

DeepSeek 与其它 OpenAI 兼容端点对 function calling 的支持程度不一致（有的没有、
有的字段有差异）。JSON 协议在任何端点上都能跑，而且**测试里可以直接注入一个
脚本化 planner**，不必联网、不必有 key——编排与护栏因此可以逐条钉死。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from app.config import AppSettings

logger = logging.getLogger(__name__)

PLANNER_MAX_TOKENS = 200
PLANNER_TIMEOUT = 30.0
DRESS_TIMEOUT = 12.0

# 实时意图 → 要补的通道。命中哪一组，就把该组通道全开；多组命中按顺序去重合并。
#
# 通道选择只看「哪个平台最可能有这类信息」：
# * 交易类（价格 / 现货 / 预约）：小红书有实拍和转手，网页能搜到官网公告与代购；
# * 社交类（口碑 / 测评 / 避雷）：基本只在社交平台；
# * 官方动态（新款 / 上新）：官网公告最准。
INTENT_CHANNELS: dict[str, tuple[str, ...]] = {
    "transaction": ("xhs", "web"),
    "social": ("xhs", "weibo"),
    "news": ("web",),
}

# 触发词。**刻意写宽**：多查一路的代价是多两秒和一点噪声，漏查的代价是拿历史
# 原价冒充现价——后者用户看不出来，所以宁可多查。子串匹配，长词可省（"预约价"
# 已被 "预约" 覆盖）。
INTENT_MARKERS: dict[str, tuple[str, ...]] = {
    "transaction": (
        "价格", "多少钱", "预约", "定金", "现货", "再贩", "再版",
        "抽选", "掉落", "怎么买", "哪里买", "买得到", "还有货",
    ),
    "social": (
        "口碑", "评价", "测评", "种草", "避雷", "攻略", "翻车", "实拍", "开箱", "返图",
    ),
    "news": ("新款", "新作", "上新", "发售"),
}

# 通道固定顺序：让 route 的 step 在多次运行间稳定（并行执行，但展示要一致）。
_CHANNEL_ORDER = ("xhs", "weibo", "web")


class PlannerUnavailable(RuntimeError):
    """没有可用模型（未配置 key）。路由层翻译成 503。"""


class PlannerOutputError(RuntimeError):
    """模型返回的东西不是可用的检索词。"""


def plan_intents(question: str) -> list[str]:
    """问题命中了哪几类实时意图（transaction / social / news）。

    ``plan_channels`` 只关心要查哪些通道，但「要不要提议持续关注」需要知道
    意图本身（社交类只影响本次回答，不该建议长期关注）。
    """
    text = question or ""
    return [
        intent
        for intent, markers in INTENT_MARKERS.items()
        if any(marker in text for marker in markers)
    ]


def plan_channels(question: str) -> list[str]:
    """按问题命中的实时意图，返回要补查的通道；不需要补查返回空列表。

    纯函数：不联网、不读配置，可以直接单测。
    """
    picked: set[str] = set()
    for intent in plan_intents(question):
        picked.update(INTENT_CHANNELS[intent])
    return [name for name in _CHANNEL_ORDER if name in picked]


def build_query_prompt(question: str, history: list[dict] | None = None) -> str:
    """拼给模型看的检索词提取提示词。"""
    context = ""
    turns = (history or [])[-2:]
    if turns:
        lines = "\n".join(
            f"{'用户' if turn.get('role') == 'user' else '助手'}：{turn.get('content', '')}"
            for turn in turns
        )
        context = f"\n最近两轮对话（只用于理解「它」「这条」这类指代）：\n{lines}\n"
    return f"""把下面这个问题压成一个**检索词**，给小红书 / 微博 / 网页搜索用。

用户问题：
{question}
{context}
## 输出协议
只输出一个 JSON 对象，不要任何其它文字：

{{"query": "Baby 缎带与齿轮 现货"}}

## 规则
1. 保留品牌、系列名、款式（JSK / OP）、货号这些能定位商品的信息；
2. 去掉「请问」「我想知道」「能不能帮我看看」这类寒暄和语气词；
3. **不要**加「微博」「小红书」「测评」这类渠道词——那是调用方自己加的；
4. 控制在 30 字以内；
5. 确实提取不出更多信息，就原样返回用户问题。
"""


@dataclass(frozen=True, slots=True)
class DressHint:
    """模型对「这条裙子是什么」的兜底判断。"""

    brand: str = ""
    region: str = ""
    series: str = ""
    series_cn: str = ""
    confidence: str = "low"


def build_dress_prompt(question: str, history: list[dict] | None = None) -> str:
    """裙子识别提示词：只还原品牌/系列名，不碰价格、日期、配色。"""
    context = ""
    turns = (history or [])[-2:]
    if turns:
        lines = "\n".join(
            f"{'用户' if turn.get('role') == 'user' else '助手'}：{turn.get('content', '')}"
            for turn in turns
        )
        context = f"\n最近两轮对话（只用于理解「它」「这条」这类指代）：\n{lines}\n"
    return f"""识别下面问题里提到的那条 Lolita 裙子，只输出一个 JSON 对象，不要任何其它文字。

用户问题：
{question}
{context}
## 输出协议

{{"brand": "品牌官方英文或日文名", "region": "JP|CN|OTHER|", "series": "系列官方英文或日文名", "series_cn": "用户说的中文名", "confidence": "high|medium|low"}}

## 规则

1. 只有问题确实在说某条具体裙子时才填写；不是裙子问题就全部留空。
2. 中文译名要还原成官方名。例：
   「皇家千层公主」= Baby, the Stars Shine Bright 的 "Mille-feuille Royal pour la Princesse"；
   「甜心梦幻拿铁」= Baby, the Stars Shine Bright 的 "Sweetheart Dreamy Latte"。
   （Mille-feuille / Sweetheart Dreamy Latte 都是系列名，不是品牌。）
3. 不确定就留空，**不编造**品牌、系列、价格、日期、配色。
4. region 只能是 JP（日牌）、CN（国牌）、OTHER（其他地区）或空串。
5. confidence 用 high / medium / low；拿不准就用 low。
6. 只能确定品牌、确定不了系列时，series 留空；不要用「某系列」「未知款」占位。
"""


def parse_dress_reply(text: str) -> DressHint:
    """解析裙子识别结果；单独成函数便于不联网测试。"""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise PlannerOutputError(f"模型没有返回合法 JSON：{(text or '')[:80]}") from exc
    if not isinstance(data, dict):
        raise PlannerOutputError("裙子识别结果不是 JSON 对象")

    region = str(data.get("region") or "").strip().upper()
    if region not in ("JP", "CN", "OTHER", ""):
        region = ""
    confidence = str(data.get("confidence") or "").strip().lower()
    if confidence not in ("high", "medium", "low"):
        confidence = "low"
    return DressHint(
        brand=str(data.get("brand") or "").strip(),
        region=region,
        series=str(data.get("series") or "").strip(),
        series_cn=str(data.get("series_cn") or "").strip(),
        confidence=confidence,
    )


class DeepSeekPlanner:
    """检索词提取。走 OpenAI 兼容协议（默认 DeepSeek）。"""

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
        self.usage = {
            "calls": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            # 每次调用的明细：成本账本按它拆到模型（价格可能因模型而异）。
            "calls_detail": [],
        }

    @property
    def model(self) -> str:
        return self._model

    def _record_usage(self, response) -> None:
        prompt_tokens = 0
        completion_tokens = 0
        usage = getattr(response, "usage", None)
        if usage is not None:
            prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
            completion_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
        self.usage["calls"] += 1
        self.usage["prompt_tokens"] += prompt_tokens
        self.usage["completion_tokens"] += completion_tokens
        self.usage["calls_detail"].append(
            {
                "role": "planner",
                "model": self._model,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
            }
        )

    def extract_query(self, question: str, history: list[dict] | None = None) -> str:
        messages: list[dict] = [
            {"role": "user", "content": build_query_prompt(question, history)},
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

        self._record_usage(response)
        text = (response.choices[0].message.content or "").strip()
        return parse_query(text)

    def extract_dress(self, question: str, history: list[dict] | None = None) -> DressHint:
        """中文译名兜底：只在离线识别不到具体系列时调用。"""
        messages: list[dict] = [
            {"role": "user", "content": build_dress_prompt(question, history)},
        ]
        try:
            response = self._client.chat.completions.create(
                model=self._model,
                messages=messages,
                temperature=0,
                max_tokens=PLANNER_MAX_TOKENS,
                response_format={"type": "json_object"},
                timeout=DRESS_TIMEOUT,
            )
        except Exception as exc:  # noqa: BLE001 - 模型失败退回离线结果
            raise PlannerOutputError(f"裙子识别模型调用失败：{type(exc).__name__}: {exc}") from exc

        self._record_usage(response)
        text = (response.choices[0].message.content or "").strip()
        return parse_dress_reply(text)


def parse_query(text: str) -> str:
    """解析模型输出。单独成函数是为了能单测——不用发请求就能验协议。"""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise PlannerOutputError(f"模型没有返回合法 JSON：{text[:80]}") from exc
    if not isinstance(data, dict):
        raise PlannerOutputError("模型返回的不是 JSON 对象")

    query = data.get("query")
    if not isinstance(query, str) or not query.strip():
        raise PlannerOutputError("query 必须是非空字符串")
    return query.strip()
