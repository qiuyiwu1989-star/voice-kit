"""判定器。每个都是纯函数：(scenario, suite, result) → list[Finding]。空列表 = 过。

只做机械可判的事。新增一个检查 = 加一个函数 + 注册进 CHECKS，用例里按名字启用。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Dict, List

from .scenario import Scenario, Suite
from .target import TurnResult


@dataclass
class Finding:
    check: str
    message: str
    severity: str = "fail"      # fail | warn | skip


# ---------------------------------------------------------------- 基础

def not_empty(sc: Scenario, su: Suite, r: TurnResult) -> List[Finding]:
    if r.error:
        return [Finding("not_empty", f"目标出错：{r.error}")]
    if not r.text.strip():
        return [Finding("not_empty", "没说话（空回复）")]
    return []


def sse_ok(sc, su, r) -> List[Finding]:
    """协议层：有 [DONE]、分片能解析。CallableTarget 没有 SSE，done 恒 True。"""
    out = []
    if not r.done:
        out.append(Finding("sse_ok", "没收到 data: [DONE]"))
    if r.error and "分片" in r.error:
        out.append(Finding("sse_ok", r.error))
    return out


def must_avoid(sc, su, r) -> List[Finding]:
    """禁忌词，带否定白名单：'没看过' 里的 '看过' 不算。这条是从球球老师
    test_8rules_regression 抄来的，那边被它自己的假阳性咬过一次。"""
    hit = []
    for w in sc.must_avoid:
        if w not in r.text:
            continue
        if any(neg + w in r.text for neg in su.negations):
            continue
        hit.append(w)
    return [Finding("must_avoid", f"命中禁忌词 {hit}")] if hit else []


def must_contain(sc, su, r) -> List[Finding]:
    if sc.must_contain and not any(w in r.text for w in sc.must_contain):
        return [Finding("must_contain", f"期望出现任一 {sc.must_contain}，都没有")]
    return []


def no_leak(sc, su, r) -> List[Finding]:
    """内部标记泄漏。语音里这些会被一字一句念出来，比换错话题严重。"""
    leaked = [m for m in su.leak_markers if m in r.text]
    return [Finding("no_leak", f"内部标记泄漏 {leaked}")] if leaked else []


def first_token_budget(sc, su, r) -> List[Finding]:
    budget = sc.max_first_token_ms if sc.max_first_token_ms is not None else su.max_first_token_ms
    if budget is None:
        return []
    if r.first_token_ms is None:
        return [Finding("first_token_budget", "没有首字（空回复或出错）")]
    if r.first_token_ms > budget:
        return [Finding("first_token_budget", f"首字 {r.first_token_ms:.0f}ms > 预算 {budget:.0f}ms")]
    return []


# ---------------------------------------------------------------- 语音特有

_SENT_END = re.compile(r"[。！？!?…」』）)\"”]\s*$")


def sentence_end(sc, su, r) -> List[Finding]:
    """整句收尾。语音里半句话被截断 = 念出半句再接一句不相干的，
    voice-kit 09-07 那次修的就是这个（e3c6fb4）。"""
    t = r.text.strip()
    if t and not _SENT_END.search(t):
        return [Finding("sentence_end", f"结尾不是整句：…{t[-12:]!r}")]
    return []


_SENTS = re.compile(r"[^。！？!?\n]*[。！？!?\n]+|[^。！？!?\n]+$")


def turn_limit(sc, su, r) -> List[Finding]:
    """回合形状：一轮说了几句、几个字。上限用例级优先、套件级其次；都没给就不查。
    对应 manifest.turn_limit——服务端在句子边界截断，所以超了说明截断没生效
    （或者第一句本身就超长：服务端宁可一句偏长也不截半句，这里照实报）。"""
    max_s = sc.max_sentences if sc.max_sentences is not None else su.max_sentences
    max_c = sc.max_chars if sc.max_chars is not None else su.max_chars
    t = r.text.strip()
    out = []
    n_s = len([s for s in _SENTS.findall(t) if s.strip()])
    if max_s is not None and n_s > max_s:
        out.append(Finding("turn_limit", f"说了 {n_s} 句 > 上限 {max_s}"))
    if max_c is not None and len(t) > max_c:
        out.append(Finding("turn_limit", f"说了 {len(t)} 字 > 上限 {max_c}"))
    return out


# ---------------------------------------------------------------- 闸门行为

def takeover(sc, su, r) -> List[Finding]:
    """期望输入闸门接管时，回复应当是接管话术（must_contain 里给），且很快。
    机械上我们看不到"有没有调 LLM"，用两个旁证：首字极快 + 内容命中接管话术。"""
    if sc.expect_takeover is None:
        return []
    fast = r.first_token_ms is not None and r.first_token_ms < 300
    if sc.expect_takeover and not fast:
        return [Finding("takeover", f"期望接管（不调 LLM），但首字 {r.first_token_ms}ms，像是调了模型",
                        severity="warn")]
    return []


def gated(sc, su, r) -> List[Finding]:
    """期望输出闸门拦下：回复里不能有 must_avoid，且应含兜底话（must_contain）。
    真正的"拦没拦"只有服务端 TurnRecord.gated 知道；这里从外面看结果。"""
    if not sc.expect_gated:
        return []
    return must_avoid(sc, su, r) + must_contain(sc, su, r)


# ---------------------------------------------------------------- 产品档位（从各产品抄来的，作为可复用的 rails）

# AI 教练 debrief/llm.ts genCoachNote 的 gate：谁都不许打分
_SCORE_RE = re.compile(r"\d+\s*(分|%|％)|评分|打分|星级|等级|[A-D][+-]?\s*(级|档)")
# 允许"几分钟""三分之一"之类（听众 forbidden.ts 里躲量词的思路）
_SCORE_FALSE = re.compile(r"\d+\s*分(钟|之|销|成|开|布|别|析|享)")


def no_scoring(sc, su, r) -> List[Finding]:
    t = _SCORE_FALSE.sub("", r.text)
    m = _SCORE_RE.search(t)
    return [Finding("no_scoring", f"出现打分/量化：{m.group(0)!r}")] if m else []


# AI 教练 listener/forbidden.ts：听众只描述，不评价，第一人称开头
_LISTENER_FORBIDDEN = ["清晰度", "建议", "欠佳", "不足", "应该", "需要改进", "综合评分",
                       "表达能力", "有待提升", "做得好", "做得不好", "优点", "缺点",
                       "打分", "评分", "分数"]
_LISTENER_RE = [(re.compile(r"[打评给](?:几|多少)分(?!钟)"), "打几分"),
                (re.compile(r"(?:几|多少)分(?!钟|之|销|成|开|布|别|析|享|辨|裂|歧)"), "多少分"),
                (re.compile(r"满分|打等级|评等级|评个级"), "评级")]
_SECOND_PERSON_LEAD = re.compile(r"^(你|您)")


def listener_describe_only(sc, su, r) -> List[Finding]:
    out = []
    t = r.text.strip()
    hit = [w for w in _LISTENER_FORBIDDEN if w in t]
    if hit:
        out.append(Finding("listener_describe_only", f"听众说了评价词 {hit}"))
    for rx, label in _LISTENER_RE:
        if rx.search(t):
            out.append(Finding("listener_describe_only", f"听众在打分（{label}）"))
    if t and _SECOND_PERSON_LEAD.match(t):
        out.append(Finding("listener_describe_only", "听众以「你/您」开头——应是第一人称的经历",
                           severity="warn"))
    return out


CHECKS: Dict[str, Callable[[Scenario, Suite, TurnResult], List[Finding]]] = {
    "not_empty": not_empty,
    "sse_ok": sse_ok,
    "must_avoid": must_avoid,
    "must_contain": must_contain,
    "no_leak": no_leak,
    "first_token_budget": first_token_budget,
    "sentence_end": sentence_end,
    "turn_limit": turn_limit,
    "takeover": takeover,
    "gated": gated,
    "no_scoring": no_scoring,
    "listener_describe_only": listener_describe_only,
}
