"""闸门测试。

重点不是"规则命中了吗"（那是调用方的规则的事），而是**机制对不对**：
违规内容到底有没有被挡在输出流之外。这是整个 voice-kit 的核心承诺，
所以每条都带变异验证——把闸门拆掉必须失败，否则测试是假的。
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from voice_kit.gate import InputGate, OutputGate, Rule

# 用球球老师真实的规则做样本
RULES = [
    Rule(name="禁用病理化词汇", words=["抑郁", "焦虑症", "ADHD", "诊断"]),
    Rule(name="禁止主动结束对话",
         words=["下次再聊", "拜拜"],
         patterns=[re.compile(r"今天.{0,4}(到这里|不聊了|就聊到)")]),
    Rule(name="禁止空夸",
         whole_text_patterns=[re.compile(r"^你真棒$"), re.compile(r"^好棒$")]),
]


def tokens(s, n=3):
    """把一段话切成小块，模拟模型逐字吐出来。"""
    return (s[i:i + n] for i in range(0, len(s), n))


def collect(gate, text, **kw):
    out = list(gate.filter(tokens(text), **kw))
    delta = "".join(e["text"] for e in out if e["kind"] == "delta")
    corr = [e["text"] for e in out if e["kind"] == "correction"]
    return delta, corr


# ---------------------------------------------------------------- 核心承诺

def test_clean_text_passes_through_intact():
    gate = OutputGate(RULES)
    delta, corr = collect(gate, "你说的红石机关像迷宫，这个比喻挺准的。")
    assert delta == "你说的红石机关像迷宫，这个比喻挺准的。"
    assert not corr


def test_violation_never_reaches_the_delta_stream():
    """最要紧的一条：违禁词一个字都不能出现在 delta 里。"""
    gate = OutputGate(RULES, rewriter=lambda t, v: "我们再多聊一会儿吧。")
    delta, corr = collect(gate, "我觉得你可能有点抑郁，我们下次再聊。")
    assert "抑郁" not in delta, f"违禁词漏进输出流了: {delta!r}"
    assert corr == ["我们再多聊一会儿吧。"]


def test_holdback_prevents_half_word_leak():
    """安全垫存在的意义：违禁词跨 token 边界时，前半个字也不能先发出去。"""
    gate = OutputGate(RULES)
    # "抑" 和 "郁" 会落在不同 token 里
    delta, _ = collect(gate, "他有点抑郁的样子。", )
    assert "抑" not in delta


def test_rewriter_result_is_itself_checked():
    """重写结果也要过闸门——模型完全可能第二次还是说错。"""
    gate = OutputGate(RULES, rewriter=lambda t, v: "你可能有焦虑症。")  # 重写还是违规
    delta, corr = collect(gate, "我觉得你有点抑郁。")
    assert "抑郁" not in delta
    assert corr and "焦虑症" not in corr[0], "重写结果没有被复检"


def test_no_rewriter_still_blocks():
    """没给 rewriter 时，宁可什么都不说，也不能把违规原文放出去。"""
    gate = OutputGate(RULES)
    delta, corr = collect(gate, "你可能有点抑郁哦。")
    assert "抑郁" not in delta
    assert all("抑郁" not in c for c in corr)


# ---------------------------------------------------------------- 变异验证

def test_mutation_removing_the_gate_would_fail():
    """把检查拆掉，违禁词就会漏出去——证明上面那些断言不是恒真的。"""
    class NoGate(OutputGate):
        def check(self, text):
            from voice_kit.gate import Violation
            return Violation(False)

    leaky = NoGate(RULES)
    delta, _ = collect(leaky, "我觉得你可能有点抑郁。")
    assert "抑郁" in delta, "拆掉闸门后本该漏出来——说明测试没有真的在测闸门"


# ---------------------------------------------------------------- 低延迟模式

def test_low_latency_flushes_complete_sentences_early():
    """通话模式：完整句子不必等安全垫，但检测一个字都不能少。"""
    gate = OutputGate(RULES, holdback=24)
    text = "这个想法很有意思。"          # 比 holdback 短，普通模式下会被全压住
    normal_delta, _ = collect(gate, text)
    fast_delta, _ = collect(gate, text, low_latency=True)
    assert fast_delta == text
    # 普通模式下这句会被压到收尾才发；低延迟模式在流中途就发了
    assert len(fast_delta) >= len(normal_delta)


def test_low_latency_still_blocks_violations():
    gate = OutputGate(RULES, holdback=24, rewriter=lambda t, v: "换个说法。")
    delta, corr = collect(gate, "今天就聊到这里吧。", low_latency=True)
    assert "今天就聊到这里" not in delta
    assert corr == ["换个说法。"]


# ---------------------------------------------------------------- 规则形态

def test_pattern_catches_natural_variants():
    gate = OutputGate(RULES, rewriter=lambda t, v: "ok")
    for variant in ["今天就到这里吧", "今天不聊了", "今天就聊到这儿"]:
        v = gate.check(variant)
        assert v.has_violation, f"变体没抓到: {variant}"


def test_whole_text_pattern_only_matches_whole_sentence():
    """'你真棒' 该禁；'你真棒，因为你自己想出了办法' 不该禁。"""
    gate = OutputGate(RULES)
    assert gate.check("你真棒").has_violation
    assert gate.check("你真棒！").has_violation, "尾部标点不该让它逃掉"
    assert not gate.check("你真棒，因为你自己想出了办法").has_violation


def test_auto_holdback_covers_longest_word():
    gate = OutputGate([Rule(name="x", words=["一个很长的违禁短语"])])
    assert gate.holdback >= len("一个很长的违禁短语")


# ---------------------------------------------------------------- 输入闸门

def test_input_gate_reports_category_and_match():
    ig = InputGate([
        Rule(name="self_harm", words=["不想活了"]),
        Rule(name="abuse", patterns=[re.compile(r"(爸爸|妈妈).{0,8}打我")]),
    ])
    assert ig.check("我今天有点累") == (None, None)
    assert ig.check("我不想活了")[0] == "self_harm"
    name, hit = ig.check("爸爸今天又打我了")
    assert name == "abuse" and "打我" in hit
