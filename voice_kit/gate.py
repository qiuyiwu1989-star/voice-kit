"""输出闸门：在内容到达用户**之前**拦住它。

这是 voice-kit 里最值钱的一块，也是市面上语音框架普遍没有的。
Pipecat/LiveKit 给的是"供应商可插拔"，但没人管"模型说出口的话该不该说"——
它们的假设是模型输出可信。做儿童产品不能这么假设。

从球球老师抽取（2026-09）。它在生产上的形态是三层：

  ① 安全垫（holdback）：末尾 N 个字先压住不发
     ——防止违禁词吐一半就被看到（"抑" 已经显示了，"郁" 还在路上）
  ② 逐段闸门：只有检查通过的部分才下发
     ——违规内容根本不会进入输出流，不是"发出去再撤回"
  ③ 收尾全量检测：命中就整段重写，发 correction 让前端替换

为什么必须是"事前"而不是"事后"：
说出去了就是说出去了。对儿童产品，"我们记录了这次违规"不是答案。
这也正是端到端语音大模型（Speech2Speech）目前不能用在这类产品上的原因——
音频和文字同时生成，等你看到文字，声音已经在播了。

闸门本身不含任何具体规则——规则由调用方注入（见 Rule）。
voice-kit 只负责"什么时候检查、检查不过怎么办"这套机制。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Iterator, Optional, Pattern, Sequence


@dataclass
class Rule:
    """一条输出规则。

    name 是给人看的（会进日志、会进给模型的重写指令），所以要写成
    "禁止主动结束对话"这种能直接读懂的句子，不要写 RULE_003。
    """
    name: str
    words: Sequence[str] = field(default_factory=tuple)       # 子串命中
    patterns: Sequence[Pattern] = field(default_factory=tuple)  # 正则命中
    # 整句匹配用（比如"空夸"：'你真棒' 要禁，但 '你真棒，因为…' 不该禁）
    whole_text_patterns: Sequence[Pattern] = field(default_factory=tuple)


@dataclass
class Violation:
    has_violation: bool
    violated_rules: list[str] = field(default_factory=list)
    matched: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return self.has_violation


# 收尾标点：判断"这句说完了吗"。说完的句子可以跳过安全垫立刻下发——
# 句子完整就不存在"吐一半"的风险，安全垫本来就是防截断的。
_SENTENCE_END = re.compile(r"[。！？!?…]['\"”’)）]*\s*$")
_TRAIL_PUNCT = re.compile(r"[！!。.~～\s]+$")


class OutputGate:
    """流式输出闸门。

    用法（同步生成器版）：
        gate = OutputGate(rules, holdback=24)
        for ev in gate.filter(model_token_stream()):
            ...   # ev.kind: "delta" | "correction"

    holdback 怎么定：取最长违禁词的长度即可，再多就是白等。
    球球老师用 24——那是拍脑袋定的，这里改成默认按规则自动算。
    """

    def __init__(self, rules: Sequence[Rule], *, holdback: Optional[int] = None,
                 rewriter: Optional[Callable[[str, Violation], Optional[str]]] = None,
                 fallback: str = ""):
        self.rules = list(rules)
        self.holdback = holdback if holdback is not None else self._auto_holdback()
        # rewriter：命中后把整段交给它重写（通常是再调一次 LLM 并告诉它违反了什么）。
        # 不给的话就只做拦截、不做补救——那样用户会看到半截话，所以强烈建议给。
        self.rewriter = rewriter
        # 重写失败或没给 rewriter 时用它兜底。默认空字符串——
        # **宁可什么都不说，也绝不把命中违规的原文放出去**。
        self.fallback = fallback

    def _auto_holdback(self) -> int:
        longest = 0
        for r in self.rules:
            for w in r.words:
                longest = max(longest, len(w))
        return max(4, min(longest + 2, 32))

    def check(self, text: str) -> Violation:
        rules_hit, matched = [], []
        for rule in self.rules:
            for w in rule.words:
                if w in text:
                    rules_hit.append(rule.name)
                    matched.append(w)
            for p in rule.patterns:
                m = p.search(text)
                if m:
                    rules_hit.append(rule.name)
                    matched.append(m.group(0))
            if rule.whole_text_patterns:
                stripped = _TRAIL_PUNCT.sub("", text.strip())
                for p in rule.whole_text_patterns:
                    if p.search(stripped):
                        rules_hit.append(rule.name)
                        matched.append(p.pattern)
        return Violation(bool(rules_hit), sorted(set(rules_hit)), matched)

    def filter(self, tokens: Iterator[str], *, low_latency: bool = False):
        """把模型的 token 流，过成"可以安全下发"的事件流。

        low_latency=True（通话模式）：已经收尾的完整句子跳过安全垫立刻下发。
        安全检测一个字都没少做——只是不再为一个已经完整的句子多等 N 个字。
        球球老师实测这条对"孩子等球球开口"的体感影响很直接。

        产出：{"kind":"delta","text":...} 逐步下发
             {"kind":"correction","text":...} 整段替换（命中违规且重写成功）
        """
        buffer, flushed = "", 0
        for tok in tokens:
            buffer += tok
            visible_end = len(buffer) - self.holdback
            if low_latency:
                m = _SENTENCE_END.search(buffer)
                if m:
                    visible_end = max(visible_end, m.end())
            if visible_end > flushed and not self.check(buffer[:visible_end]).has_violation:
                yield {"kind": "delta", "text": buffer[flushed:visible_end]}
                flushed = visible_end

        # 收尾：把压住的尾巴做最终检测
        if flushed < len(buffer):
            v = self.check(buffer)
            if v.has_violation:
                fixed = self.rewriter(buffer, v) if self.rewriter else None
                if fixed and not self.check(fixed).has_violation:
                    yield {"kind": "correction", "text": fixed}
                else:
                    # 重写失败 / 没给 rewriter / 重写结果自己也违规——
                    # 一律走兜底。绝不把命中违规的原文放出去，这条没有例外。
                    yield {"kind": "correction", "text": self.fallback}
            else:
                yield {"kind": "delta", "text": buffer[flushed:]}


class InputGate:
    """输入侧：检测用户说的话里有没有需要立刻改变行为的信号。

    跟 OutputGate 的关键区别——命中之后**不是过滤，是接管**：
    球球老师的做法是红线命中时根本不调 LLM，直接返回写死的软回应。
    模型在最危险的时刻没有发挥空间，这是比"让模型小心点"强得多的保证。
    """

    def __init__(self, rules: Sequence[Rule], on_trigger: Optional[Callable] = None):
        self.rules = list(rules)
        self.on_trigger = on_trigger

    def check(self, text: str) -> tuple[Optional[str], Optional[str]]:
        """返回 (规则名, 命中内容)；没命中返回 (None, None)。"""
        t = text.strip()
        for rule in self.rules:
            for w in rule.words:
                if w in t:
                    return rule.name, w
            for p in rule.patterns:
                m = p.search(t)
                if m:
                    return rule.name, m.group(0)
        return None, None
