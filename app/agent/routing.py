"""模型分级路由：**规则先判**，规则拿不准才问一次轻量模型。

## 为什么不是「先问模型」

路由本身是为了省钱。如果每次都先发一次大模型调用再决定用哪个模型，省下的钱
要先付一笔判断费，简单问题还多等一轮。所以顺序是：

1. **纯规则**：命中「多路补查」「综合推理词」「单路补查」「短问题」之一就直接
   定档，不联网、不花钱；
2. 规则判不出来（无补查、无综合词、问题还长），才用**便宜档**发一次极小的
   判断请求（``max_tokens`` 个位数）；
3. 这一步**永远不许拖垮回答**：未启用、调用失败、超时、输出看不懂，一律退回
   便宜档——路由是优化，不是关键路径。

## 判定档位的两条直觉

* **多路补查 → 强档**：xhs / weibo / web 同时开，证据来自三个平台，冲突、
  时效、口径差异都要在回答里消化，这是便宜档最容易糊弄过去的地方。
* **综合推理词 → 强档**：「对比」「哪个好」「为什么」这类问题要跨证据推理，
  不是把一条证据复述出来；反之「蕾丝怎么洗」只要照着知识库说。

## 可解释

每次判定都带 ``rule``（命中了哪条规则）与 ``features``（命中了什么特征：
通道、意图、长度、综合词），并写进日志与轨迹。看到 ``tier=strong`` 时，
能立刻回答「为什么」——这对以后调阈值、做 A/B 都是前提。

## 只作用于 writer

planner 的检索词提取永远是短输出、便宜档就够；给它分级只会多一份复杂度，
省不下钱。所以档位只决定**写作**调用用哪个模型。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from app.agent.planner import plan_channels, plan_intents
from app.config import AppSettings

logger = logging.getLogger(__name__)

TIER_CHEAP = "cheap"
TIER_STRONG = "strong"
MODE_AUTO = "auto"
MODES = (MODE_AUTO, TIER_CHEAP, TIER_STRONG)

# 需要跨证据推理的信号。刻意只收「确实要综合」的说法：「怎么样」「是不是」
# 这类太常见，收了会把大半问题推向强档，分级就失去意义。
SYNTHESIS_MARKERS = (
    "对比", "区别", "哪个好", "怎么选", "搭配", "为什么", "优缺点",
    "值不值", "推荐", "分析", "还是", "以及", "顺便",
)

# 短问题且无补查：当成简单问答。30 字是经验值——再长通常已经包含条件/背景，
# 交给轻量判断比硬规则更稳。
SIMPLE_MAX_CHARS = 30

JUDGE_MAX_TOKENS = 8
JUDGE_TIMEOUT = 5.0

JUDGE_PROMPT = """判断下面这个问题回答起来是简单还是复杂，只输出一个词。

- cheap：单一来源/一条事实就能答，不需要比较或推理
- strong：需要跨来源对比、综合多条信息或多步推理

问题：{question}

只输出 cheap 或 strong。
"""


@dataclass(frozen=True)
class RouteDecision:
    """一次路由的结果。``as_dict`` 进响应与轨迹，``calls`` 只进账本。"""

    tier: str
    model: str
    rule: str
    features: tuple[str, ...]
    reason: str
    predicted_channels: tuple[str, ...] = ()
    mode: str = MODE_AUTO
    # 路由器自己花掉的调用（轻量判断），形状与 usage["calls_detail"] 一致。
    calls: tuple[dict, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict:
        return {
            "mode": self.mode,
            "tier": self.tier,
            "model": self.model,
            "rule": self.rule,
            "features": list(self.features),
            "reason": self.reason,
            "predicted_channels": list(self.predicted_channels),
        }


def normalize_mode(raw) -> str:
    """把配置里的 mode 收敛到三个合法值；写错了按 auto 处理并留一条 warning。"""
    mode = str(raw or MODE_AUTO).strip().lower()
    if mode in MODES:
        return mode
    logger.warning("未知的 MODEL_ROUTING_MODE=%r，按 auto 处理", raw)
    return MODE_AUTO


def _model_for(tier: str, app: AppSettings) -> str:
    """档位对应的模型名；该档没配就回落 LLM_MODEL（只用一家模型也能跑）。"""
    base = (app.llm_model or "").strip()
    if tier == TIER_STRONG:
        return (app.llm_model_strong or "").strip() or base
    return (app.llm_model_cheap or "").strip() or base


def _synthesis_hits(text: str) -> list[str]:
    return [marker for marker in SYNTHESIS_MARKERS if marker in text]


def _features(text: str, channels: list[str], hits: list[str]) -> tuple[str, ...]:
    """给日志看的路由依据：长度、补查通道、意图、命中的综合词。"""
    features = [f"chars={len(text)}", f"channels={','.join(channels) or '-'}"]
    intents = plan_intents(text)
    if intents:
        features.append(f"intents={','.join(intents)}")
    if hits:
        features.append(f"synthesis={','.join(hits)}")
    return tuple(features)


def classify_question(question: str) -> tuple[str | None, tuple[str, ...], str, str]:
    """纯规则定档。返回 ``(tier|None, features, rule, reason)``；判不出来时 tier 为 None。"""
    text = (question or "").strip()
    channels = plan_channels(text)
    hits = _synthesis_hits(text)
    features = _features(text, channels, hits)

    if len(channels) >= 2:
        return (
            TIER_STRONG,
            features,
            "multi_channel",
            f"命中 {len(channels)} 路补查（{'/'.join(channels)}），证据需要跨源消化",
        )
    if hits:
        return TIER_STRONG, features, "synthesis", f"命中综合推理词：{'、'.join(hits)}"
    if len(channels) == 1:
        return (
            TIER_CHEAP,
            features,
            "single_channel",
            f"只命中 1 路补查（{channels[0]}），不需要跨源综合",
        )
    if len(text) <= SIMPLE_MAX_CHARS:
        return TIER_CHEAP, features, "simple_qa", f"无补查且问题较短（{len(text)} 字）"
    return None, features, "ambiguous", "无补查、未命中综合词、问题较长，规则判不出来"


class LLMTierJudge:
    """轻量判断：走便宜档，只要一个词。任何异常都由调用方兜底。"""

    def __init__(self, app: AppSettings):
        from openai import OpenAI

        self._client = OpenAI(
            api_key=app.llm_api_key.strip(),
            base_url=app.llm_base_url.strip(),
            timeout=JUDGE_TIMEOUT,
        )
        self._model = _model_for(TIER_CHEAP, app)
        self.usage = {
            "calls": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "calls_detail": [],
        }

    @property
    def model(self) -> str:
        return self._model

    def _record(self, response) -> None:
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
                "role": "router",
                "model": self._model,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
            }
        )

    def judge(self, question: str) -> str:
        response = self._client.chat.completions.create(
            model=self._model,
            messages=[{"role": "user", "content": JUDGE_PROMPT.format(question=question)}],
            temperature=0,
            max_tokens=JUDGE_MAX_TOKENS,
        )
        self._record(response)
        text = (response.choices[0].message.content or "").strip().lower()
        # 看不清就按便宜档：判断错了顶多慢一点/差一点，不该让路由去赌。
        return TIER_STRONG if TIER_STRONG in text else TIER_CHEAP


def _judge_calls(judge) -> tuple[dict, ...]:
    usage = getattr(judge, "usage", None)
    if not isinstance(usage, dict):
        return ()
    detail = usage.get("calls_detail")
    if isinstance(detail, list) and detail:
        return tuple(
            {**item, "role": item.get("role") or "router"}
            for item in detail
            if isinstance(item, dict)
        )
    if int(usage.get("calls", 0) or 0) <= 0:
        return ()
    return (
        {
            "role": "router",
            "model": getattr(judge, "model", "") or "",
            "prompt_tokens": int(usage.get("prompt_tokens", 0) or 0),
            "completion_tokens": int(usage.get("completion_tokens", 0) or 0),
        },
    )


def _ask_judge(judge, question: str) -> str:
    """允许注入 ``judge(question)`` 函数或带 ``judge`` 方法的对象。"""
    ask = getattr(judge, "judge", judge)
    return str(ask(question) or "").strip().lower()


def _build_judge(app: AppSettings):
    try:
        return LLMTierJudge(app)
    except Exception as exc:  # noqa: BLE001 - 客户端建不起来就退回规则
        logger.warning("轻量路由判断不可用，按规则兜底：%s", exc)
        return None


def decide(question: str, settings: AppSettings, *, judge=None) -> RouteDecision:
    """定档并解析模型名。**永不抛异常**：路由失败最多退回便宜档。"""
    text = (question or "").strip()
    mode = normalize_mode(settings.model_routing_mode)
    channels = tuple(plan_channels(text))
    features = _features(text, list(channels), _synthesis_hits(text))
    calls: tuple[dict, ...] = ()

    if mode in (TIER_CHEAP, TIER_STRONG):
        decision = RouteDecision(
            tier=mode,
            model=_model_for(mode, settings),
            rule="forced",
            features=(*features, f"mode={mode}"),
            reason=f"MODEL_ROUTING_MODE 强制走{mode}档",
            predicted_channels=channels,
            mode=mode,
        )
    else:
        tier, features, rule, reason = classify_question(text)
        if tier is None:
            tier, rule, reason, calls = _fallback_or_judge(text, settings, judge)
        decision = RouteDecision(
            tier=tier,
            model=_model_for(tier, settings),
            rule=rule,
            features=features,
            reason=reason,
            predicted_channels=channels,
            mode=mode,
            calls=calls,
        )

    logger.info(
        "模型路由 mode=%s tier=%s model=%s rule=%s features=%s reason=%s",
        decision.mode,
        decision.tier,
        decision.model,
        decision.rule,
        ",".join(decision.features),
        decision.reason,
    )
    return decision


def _fallback_or_judge(
    question: str,
    settings: AppSettings,
    judge,
) -> tuple[str, str, str, tuple[dict, ...]]:
    """规则判不出来时的出路。返回 ``(tier, rule, reason, calls)``。"""
    if not settings.model_routing_llm:
        return (
            TIER_CHEAP,
            "ambiguous_default",
            "规则判不出来且未启用轻量判断，保守走便宜档",
            (),
        )

    active = judge if judge is not None else _build_judge(settings)
    if active is None:
        return (
            TIER_CHEAP,
            "llm_unavailable",
            "轻量判断不可用（模型客户端初始化失败），保守走便宜档",
            (),
        )

    try:
        picked = _ask_judge(active, question)
    except Exception as exc:  # noqa: BLE001 - 判断失败绝不影响回答
        logger.warning("轻量路由判断失败，按规则兜底：%s", exc)
        return (
            TIER_CHEAP,
            "llm_failed",
            "轻量判断失败/超时，保守走便宜档",
            _judge_calls(active),
        )

    calls = _judge_calls(active)
    if picked == TIER_STRONG:
        return (
            TIER_STRONG,
            "llm",
            "规则判不出来，轻量判断认为需要综合推理",
            calls,
        )
    return (
        TIER_CHEAP,
        "llm",
        "规则判不出来，轻量判断认为便宜档够用",
        calls,
    )
