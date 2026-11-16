"""模型分级路由：**规则先判**，规则拿不准才问一次轻量模型。

## 判据（按实测结论重新对齐）

强档只花在两类题上：

1. **洗护/安全**：答错会伤衣服，强档更愿意说「以标签为准 / 这条只是网友经验」，
   而不是把道听途说写成规范；
2. **证据稀薄的具体条目**：只给了品牌/系列/长英文名，档案往往只有一行标题，
   强档会明确说「没查到」，便宜档容易顺着名字补设定。

其余一律便宜档——包括「综合词」和「多路补查」。14 题实测里，这两类题的证据
通常已经够 cheap 写出完整答案；把它们推强档只是白花钱（见
``eval/forensics/routing-cost`` 的误判清单）。

## 为什么不是「先问模型」

路由本身是为了省钱。如果每次都先发一次大模型调用再决定用哪个模型，省下的钱
要先付一笔判断费，简单问题还多等一轮。所以顺序是：

1. **纯规则**：洗护、稀薄条目、补查通道、短问题，命中即定档，不联网、不花钱；
2. 规则判不出来（无补查、无洗护、无具体条目、问题还长），才用**便宜档**发一次
   极小的判断请求；
3. 这一步**永远不许拖垮回答**：未启用、调用失败、超时、输出看不懂，一律退回
   便宜档——路由是优化，不是关键路径。

## 只作用于 writer

planner 的检索词提取永远是短输出、便宜档就够；给它分级只会多一份复杂度，
省不下钱。所以档位只决定**写作**调用用哪个模型。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from app.agent.channels import CARE_MARKERS
from app.agent.planner import plan_channels, plan_intents
from app.config import AppSettings

logger = logging.getLogger(__name__)

TIER_CHEAP = "cheap"
TIER_STRONG = "strong"
MODE_AUTO = "auto"
MODES = (MODE_AUTO, TIER_CHEAP, TIER_STRONG)

# 短问题且无补查：当成简单问答。30 字是经验值——再长通常已经包含条件/背景，
# 交给轻量判断比硬规则更稳。
SIMPLE_MAX_CHARS = 30

# 「具体条目」：两个以上长拉丁词（角色名/系列名）或一个货号。只写品类缩写
# （JSK / OP / KC）不算——那是概念题，知识库就能答。
_LATIN_WORD = re.compile(r"[A-Za-z][A-Za-z0-9-]+")
_PRODUCT_NUMBER = re.compile(
    r"\b(?:[A-Za-z]{1,4}\d{2,}[A-Za-z0-9-]*|\d{2,}[A-Za-z]\d[A-Za-z0-9-]*)\b"
)
_INFORMATION_MARKERS = (
    "是什么", "什么意思", "定义", "含义", "指什么", "代表什么", "区别",
    "哪年", "哪一年", "什么时候", "发售", "价格", "多少钱", "材质", "面料",
    "货号", "型号", "系列",
)

JUDGE_MAX_TOKENS = 8
JUDGE_TIMEOUT = 5.0

JUDGE_PROMPT = """判断下面这个问题回答起来是简单还是复杂，只输出一个词。

- cheap：单条明确资料或常识就能答
- strong：资料很可能很少或不确定，或涉及洗护安全，需要克制、不能猜

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


def _care_hits(text: str) -> list[str]:
    return [marker for marker in CARE_MARKERS if marker in text]


def _named_item(text: str) -> bool:
    """是否指向一条具体条目：两个以上长拉丁词，或带明显货号。"""
    words = [word for word in _LATIN_WORD.findall(text) if len(word) >= 5]
    return len(words) >= 2 or bool(_PRODUCT_NUMBER.search(text))


def _sparse_named_question(text: str) -> bool:
    """具体条目 + 在问身份/字段：档案常常只有一行标题，属于证据稀薄。"""
    if not _named_item(text):
        return False
    return any(marker in text for marker in _INFORMATION_MARKERS)


def _features(text: str, channels: list[str], care: list[str], named: bool) -> tuple[str, ...]:
    """给日志看的路由依据：长度、补查通道、意图、洗护词、具体条目。"""
    features = [f"chars={len(text)}", f"channels={','.join(channels) or '-'}"]
    intents = plan_intents(text)
    if intents:
        features.append(f"intents={','.join(intents)}")
    if care:
        features.append(f"care={','.join(care)}")
    if named:
        features.append("named_item")
    return tuple(features)


def classify_question(question: str) -> tuple[str | None, tuple[str, ...], str, str]:
    """纯规则定档。返回 ``(tier|None, features, rule, reason)``；判不出来时 tier 为 None。"""
    text = (question or "").strip()
    channels = plan_channels(text)
    care = _care_hits(text)
    named = _sparse_named_question(text)
    features = _features(text, channels, care, named)

    if care:
        return (
            TIER_STRONG,
            features,
            "care_safety",
            f"洗护/安全类（命中：{'、'.join(care)}），宁可少说也不能猜错",
        )
    if named:
        return (
            TIER_STRONG,
            features,
            "sparse_named_item",
            "在问一条具体条目的身份/字段，档案很可能只有标题，强档更愿意承认没查到",
        )
    if channels:
        return (
            TIER_CHEAP,
            features,
            "channel_recall",
            f"命中补查通道（{'/'.join(channels)}），通常能带回多段证据，便宜档足够",
        )
    if len(text) <= SIMPLE_MAX_CHARS:
        return TIER_CHEAP, features, "simple_qa", f"无补查且问题较短（{len(text)} 字）"
    return None, features, "ambiguous", "无补查、无洗护、无具体条目，规则判不出来"


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
    features = _features(
        text, list(channels), _care_hits(text), _sparse_named_question(text)
    )
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
