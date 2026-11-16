"""证据与答案的冲突诊断。

句级引用覆盖只检查「这句话有没有挂 [n]」，看不出「挂对了编号、但结论与证据
相反」。这里补的是那一层：抽取答案与证据里对同一方面的判断，看方向是否相反，
并把可疑证据连同判据一起报出来。

**只做诊断，不改答案文本。** 答案一旦流给用户，自动改写等于话说一半被打脸；
而且「方向相反」里有一部分是版本/范围不同（例如原版 vs 新色），不是硬错，
该由人看判据决定，不该由规则悄悄改字。

口径按 12 条真实回答（``eval/forensics/20261114-170417/raw.json``）实测：
命中 real-01 的 #2（答案说「会掉色」，证据说「新色绿色不掉色」，且该证据
未被引用），其余 11 条零误报。样本里只有 1 个真冲突，所以精确率 1/1 只能
说明规则没在已知反例上乱报，不能当成统计意义上的准确率。

目前只覆盖**掉色 / 褪色 / 浮色**：这是唯一有实测真例的方面。扩展新方面前
先找到对应的真实冲突样本，否则只会增加没被验证过的误报面。
"""

from __future__ import annotations

import re

# 方面 → 词表。一个方面内的词共享同一套「会 / 不会」判断。
ASPECTS: dict[str, tuple[str, ...]] = {
    "color_fade": ("掉色", "褪色", "浮色"),
}
ASPECT_LABELS = {"color_fade": "掉色/褪色/浮色"}

# 未引用证据要进入诊断，至少要跟问题有这么多词面重合：证据列表里全是同一
# 主题的片段，完全不看相关性会把所有提到「掉色」的边角料都报出来。
MIN_RELEVANCE = 2

_SENTENCE_RE = re.compile(r"[^。！？\n]+[。！？]?")
_CLAUSE_RE = re.compile(r"[^。！？\n，,；;：:]+")

# 问句里的「会不会掉色」不是判断；祈使/防护句「以免掉色」也不是结论。
_QUESTION_RE = re.compile(r"(会不会|是否|能不能|可不可以|怎么办|怎么样|吗)")
_IMPERATIVE_RE = re.compile(r"(以免|以防|防止|避免[^。，]{0,4}(掉色|褪色|浮色)|减少|减轻|抗)")
_META_RE = re.compile(r"(关于|所谓|怎么说|如何)")
# 「浮色是印花机温度偏低造成的」在解释成因，不是对「会不会掉色」的结论。
_CAUSE_RE = re.compile(r"(掉色|褪色|浮色)(是|由)[^。，]{0,20}(造成|导致|引起|因为|由于)")

# 否定词只认紧贴方面的几个字：「新色绿色应该是不掉色的」命中「不」，
# 「无法避免会有浮色掉色」不命中。
_NEGATION_RE = re.compile(r"(不|没|无|未|别|非)(会|太|易|容易|怎么|再|曾|要|有)?$")

# 「浮色」是名词性短语：「浮色基本掉完」说的是问题已经解决，属于反向结论。
_FADE_RESOLVED_RE = re.compile(r"(掉完|掉光|没了|消失|去掉|不再有|基本掉)")

# 主体词只收颜色与版本这种能明确区分「说的是哪一批」的词；「色」单字太泛，
# 会把「深色」和「浅色」误判成同一主体。
_SUBJECT_TERMS = (
    "蓝色", "绿色", "红色", "黑色", "白色", "粉色", "紫色", "黄色", "灰色", "米色",
    "酒红", "浅绿", "浅蓝", "深蓝", "深绿", "深色", "浅色",
    "新色", "原版", "旧版", "初版", "再版", "新版", "再贩",
)
_OLD_VERSION = ("原版", "旧版", "初版")
_NEW_VERSION = ("新色", "再版", "新版", "再贩")

_LATIN_RE = re.compile(r"[a-z]{2,}")
_CJK_RUN_RE = re.compile(r"[\u4e00-\u9fff]{2,}")
_STOP_BIGRAMS = {
    "裙子", "怎么", "什么", "可以", "多少", "有没", "没有", "这条", "那条",
    "的吗", "了吗", "是不", "不是", "一个", "一下", "这个", "那个",
}

_CLAIM_TEXT_LIMIT = 80


def _subjects(text: str) -> tuple[str, ...]:
    return tuple(term for term in _SUBJECT_TERMS if term in text)


def _claims(text: str) -> list[dict]:
    """按句、按逗号切块，抽出带方面词的判断及其方向。"""
    found: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    for sentence in _SENTENCE_RE.finditer(text or ""):
        for clause_match in _CLAUSE_RE.finditer(sentence.group()):
            clause = clause_match.group().strip()
            if not clause:
                continue
            if _QUESTION_RE.search(clause) or _META_RE.search(clause) or _CAUSE_RE.search(clause):
                continue
            if _IMPERATIVE_RE.search(clause):
                continue
            for aspect, terms in ASPECTS.items():
                for term in terms:
                    index = clause.find(term)
                    if index < 0:
                        continue
                    prefix = clause[max(0, index - 6):index]
                    resolved = term == "浮色" and _FADE_RESOLVED_RE.search(clause)
                    stance = "negative" if resolved or _NEGATION_RE.search(prefix) else "positive"
                    claim_text = clause[:_CLAIM_TEXT_LIMIT]
                    key = (aspect, stance, claim_text)
                    if key in seen:
                        continue
                    seen.add(key)
                    found.append(
                        {
                            "aspect": aspect,
                            "term": term,
                            "stance": stance,
                            "text": claim_text,
                            "subjects": _subjects(clause),
                        }
                    )
    return found


def _bigrams(text: str) -> set[str]:
    grams: set[str] = set()
    for run in _CJK_RUN_RE.findall(text or ""):
        for index in range(len(run) - 1):
            gram = run[index:index + 2]
            if gram not in _STOP_BIGRAMS:
                grams.add(gram)
    return grams


def _relevance(question: str, text: str) -> dict:
    """问题与证据的词面重合；只用来给「未引用证据」设一道门槛。"""
    question_lower = (question or "").lower()
    text_lower = (text or "").lower()
    latin = sorted({token for token in _LATIN_RE.findall(question_lower) if token in text_lower})
    cjk = sorted(_bigrams(question) & _bigrams(text))
    return {"score": len(latin) + len(cjk), "shared": [*latin, *cjk]}


def _compatible(answer_subjects: tuple[str, ...], evidence_subjects: tuple[str, ...]) -> bool:
    """主体词有交集，或两边都没提主体词时，才认为说的是同一件事。"""
    if set(answer_subjects) & set(evidence_subjects):
        return True
    return not answer_subjects and not evidence_subjects


def _scope_note(answer_subjects: tuple[str, ...], evidence_subjects: tuple[str, ...]) -> str:
    old = [term for term in _OLD_VERSION if term in answer_subjects]
    new = [term for term in _NEW_VERSION if term in evidence_subjects]
    if old and new:
        return f"答案说的是「{'/'.join(old)}」，证据说的是「{'/'.join(new)}」，结论可能只适用于其中一个版本。"
    return ""


def _basis(conflict: dict) -> str:
    label = ASPECT_LABELS.get(conflict["aspect"], conflict["aspect"])
    answer_word = "会/存在" if conflict["answer_stance"] == "positive" else "不/没有"
    evidence_word = "会/存在" if conflict["evidence_stance"] == "positive" else "不/没有"
    parts = [
        f"同一方面「{label}」判断相反：答案说{answer_word}，"
        f"证据说{evidence_word}"
        + ("（证据被答案引用）" if conflict["cited"] else "（证据未被引用）")
    ]
    if conflict["shared_subjects"]:
        parts.append(f"主体词「{'/'.join(conflict['shared_subjects'])}」同时出现")
    if conflict["scope_note"]:
        parts.append(conflict["scope_note"].rstrip("。"))
    return "；".join(parts) + "。"


def _evidence_number(item: dict, index: int) -> int:
    try:
        return int(item.get("n") or index + 1)
    except (TypeError, ValueError):
        return index + 1


def diagnose(question: str, answer: str, evidence: list[dict]) -> dict:
    """返回一份可序列化的冲突诊断，直接挂到响应与轨迹里。

    结构::

        {
          "checked_evidence": 9,
          "uncited_evidence": 7,
          "conflict_count": 1,
          "conflicts": [{evidence_n, channel, ref, cited, aspect, ...}],
          "uncited_claims": [{evidence_n, claims, opposes_answer, relevance}]
        }
    """
    items = list(evidence or [])
    block = {
        "checked_evidence": len(items),
        "uncited_evidence": 0,
        "conflict_count": 0,
        "conflicts": [],
        "uncited_claims": [],
    }
    if not items or not (answer or "").strip():
        return block

    cited_numbers = {int(token) for token in re.findall(r"\[(\d+)\]", answer)}
    answer_claims = _claims(answer)
    # 同一方面在答案里可能出现多次（先铺陈「有浮色」，最后说「不太掉色」）。
    # 以最后一次判断为准；只拿与最终结论同向的句子去跟证据比对，避免把被后文
    # 修正过的铺垫句当成答案立场。
    answer_verdicts: dict[str, str] = {}
    for claim in answer_claims:
        answer_verdicts[claim["aspect"]] = claim["stance"]
    uncited_total = 0
    conflicts: list[dict] = []
    uncited_claims: list[dict] = []

    for index, item in enumerate(items):
        number = _evidence_number(item, index)
        text = str(item.get("text") or "")
        claims = _claims(text)
        cited = number in cited_numbers
        if not cited:
            uncited_total += 1
        if not claims:
            continue

        relevance = _relevance(question, text)
        if not cited and relevance["score"] < MIN_RELEVANCE:
            continue

        # 结论句通常在最后：证据前文可能在铺陈原因，最后一句才是判断。
        verdict = claims[-1]
        answer_stance = answer_verdicts.get(verdict["aspect"])
        if answer_stance is None or answer_stance == verdict["stance"]:
            opposite = []
        else:
            opposite = [
                claim
                for claim in answer_claims
                if claim["aspect"] == verdict["aspect"] and claim["stance"] == answer_stance
            ]
        # 主体词有交集优先（更能说明说的是同一批裙子），其次才是两边都没主体词。
        compatible = [
            claim
            for claim in opposite
            if _compatible(claim["subjects"], verdict["subjects"])
        ]
        compatible.sort(key=lambda claim: -len(set(claim["subjects"]) & set(verdict["subjects"])))

        if not compatible:
            if not cited:
                uncited_claims.append(
                    {
                        "evidence_n": number,
                        "channel": item.get("channel", ""),
                        "ref": item.get("ref", ""),
                        "claims": claims,
                        "verdict": verdict["stance"],
                        "opposes_answer": False,
                        "relevance": relevance,
                    }
                )
            continue

        answer_claim = compatible[0]
        shared = sorted(set(answer_claim["subjects"]) & set(verdict["subjects"]))
        conflict = {
            "evidence_n": number,
            "channel": item.get("channel", ""),
            "ref": item.get("ref", ""),
            "title": item.get("title", ""),
            "cited": cited,
            "aspect": verdict["aspect"],
            "aspect_label": ASPECT_LABELS.get(verdict["aspect"], verdict["aspect"]),
            "answer_stance": answer_claim["stance"],
            "evidence_stance": verdict["stance"],
            "answer_claim": answer_claim["text"],
            "evidence_claim": verdict["text"],
            "shared_subjects": shared,
            "scope_note": _scope_note(answer_claim["subjects"], verdict["subjects"]),
            "relevance": relevance,
        }
        conflict["basis"] = _basis(conflict)
        conflicts.append(conflict)

        if not cited:
            uncited_claims.append(
                {
                    "evidence_n": number,
                    "channel": item.get("channel", ""),
                    "ref": item.get("ref", ""),
                    "claims": claims,
                    "verdict": verdict["stance"],
                    "opposes_answer": True,
                    "relevance": relevance,
                }
            )

    block["uncited_evidence"] = uncited_total
    block["conflict_count"] = len(conflicts)
    block["conflicts"] = conflicts
    block["uncited_claims"] = uncited_claims
    return block
