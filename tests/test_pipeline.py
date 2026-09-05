"""编排测试：三个产品共用一份代码、只换 config，行为必须各自正确。

这是"一套模块"的核心承诺，所以每条都用假 provider 跑真实编排逻辑，
不联网、不花钱。
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from voice_kit.gate import Rule
from voice_kit.pipeline import VoiceConfig, VoicePipeline


class FakeLLM:
    """按预设脚本吐字的假模型。第二次调用（重写）返回 rewrite。"""

    def __init__(self, reply: str, rewrite: str = "换个说法。"):
        self.reply, self.rewrite, self.calls = reply, rewrite, 0

    def stream(self, messages, *, system=None, model=None, **kw):
        self.calls += 1
        text = self.reply if self.calls == 1 else self.rewrite
        for i in range(0, len(text), 3):
            yield text[i:i + 3]


KID_RULES = [
    Rule(name="禁用病理化词汇", words=["抑郁", "焦虑症"]),
    Rule(name="禁止主动结束对话", patterns=[re.compile(r"今天.{0,4}(到这里|就聊到)")]),
]
RED_RULES = [Rule(name="self_harm", words=["不想活了"])]


def kid_config(**kw):
    return VoiceConfig(
        name="球球老师", system_prompt="你是球球老师。",
        output_rules=KID_RULES, input_rules=RED_RULES,
        fallback_reply="我们再多聊一会儿吧。",
        on_input_trigger=lambda rule, m: "我听到了……你愿意多说一点吗？",
        **kw)


def collect(pipe, text):
    evs = list(pipe.respond(text))
    return ("".join(e["text"] for e in evs if e["kind"] == "delta"), evs)


# ---------------------------------------------------------------- 核心承诺

def test_clean_reply_passes_through():
    pipe = VoicePipeline(kid_config(), llm=FakeLLM("你说的红石机关像迷宫，这个比喻很准。"))
    delta, _ = collect(pipe, "我在玩我的世界")
    assert delta == "你说的红石机关像迷宫，这个比喻很准。"


def test_violation_never_reaches_output():
    llm = FakeLLM("我觉得你可能有点抑郁。", rewrite="我们再多聊聊这件事吧。")
    pipe = VoicePipeline(kid_config(), llm=llm)
    delta, evs = collect(pipe, "我最近很难过")
    assert "抑郁" not in delta
    corr = [e["text"] for e in evs if e["kind"] == "correction"]
    assert corr == ["我们再多聊聊这件事吧。"]
    assert llm.calls == 2, "应该调了第二次让模型重写"


def test_rewrite_that_still_violates_falls_back():
    """重写结果也要过闸门——模型完全可能第二次还是说错。"""
    llm = FakeLLM("你有点抑郁。", rewrite="你可能有焦虑症。")
    pipe = VoicePipeline(kid_config(), llm=llm)
    delta, evs = collect(pipe, "我难过")
    assert "抑郁" not in delta
    corr = [e["text"] for e in evs if e["kind"] == "correction"]
    assert corr == ["我们再多聊一会儿吧。"], "两次都违规就该走 fallback"


# ---------------------------------------------------------------- 输入接管

def test_input_trigger_takes_over_without_calling_llm():
    """最危险的时刻不给模型发挥空间——这比'让模型小心点'强得多。"""
    llm = FakeLLM("随便什么回复")
    pipe = VoicePipeline(kid_config(), llm=llm)
    evs = list(pipe.respond("我不想活了"))
    assert len(evs) == 1 and evs[0]["kind"] == "takeover"
    assert evs[0]["rule"] == "self_harm"
    assert llm.calls == 0, "红线命中时绝不能调 LLM"


def test_normal_input_does_not_take_over():
    llm = FakeLLM("恐龙很有意思！")
    pipe = VoicePipeline(kid_config(), llm=llm)
    delta, evs = collect(pipe, "给我讲讲恐龙")
    assert not any(e["kind"] == "takeover" for e in evs)
    assert llm.calls == 1


# ---------------------------------------------------------------- 一套模块，多个产品

def test_adult_product_can_run_without_gate():
    """成人产品可以不设闸门——但那是显式选择，不是默认。"""
    cfg = VoiceConfig(name="qiuyiwu.com agent", system_prompt="你是助手。",
                      model="deepseek-v4-flash")
    pipe = VoicePipeline(cfg, llm=FakeLLM("这个问题的风险在于……"))
    delta, _ = collect(pipe, "帮我分析")
    assert "风险" in delta, "成人产品不该被儿童规则误伤"


def test_same_code_different_config_gives_different_behavior():
    """一套模块的核心承诺：同一句话，儿童产品拦、成人产品放。"""
    line = "这里有风险，我们今天就聊到这里。"
    kid_rules = KID_RULES + [Rule(name="不卖焦虑", words=["风险"])]
    kid = VoicePipeline(
        VoiceConfig(name="kid", output_rules=kid_rules, fallback_reply="换个说法。"),
        llm=FakeLLM(line, rewrite="我们接着聊别的。"))
    adult = VoicePipeline(VoiceConfig(name="adult"), llm=FakeLLM(line))

    kid_delta, _ = collect(kid, "问题")
    adult_delta, _ = collect(adult, "问题")
    assert "风险" not in kid_delta
    assert adult_delta == line
    assert kid.config.name != adult.config.name


def test_config_rejects_gate_without_fallback():
    """有闸门没兜底 = 命中时用户看到空白。构造期就要拦住这个配置错误。"""
    with pytest.raises(ValueError, match="fallback_reply"):
        VoiceConfig(name="bad", output_rules=KID_RULES)


# ---------------------------------------------------------------- provider 方法歧义

class DualProvider:
    """既做 ASR 又做 TTS 的 provider（腾讯就是这样）。

    `stream` 是**流式 ASR**，`stream_tts` 才是 TTS——两个名字撞在一起。
    """

    def stream(self, audio_chunks, **kw):      # 流式 ASR，不是 TTS！
        raise AssertionError("speak() 抓到了 ASR 的 stream，不是 TTS 的")

    def stream_tts(self, text, *, voice=None, **kw):
        yield b"PCM:" + text.encode()


def test_speak_prefers_stream_tts_over_stream():
    """同时做 ASR/TTS 的 provider 上，speak() 不能抓到 ASR 的 stream。"""
    pipe = VoicePipeline(VoiceConfig(name="x"), tts=DualProvider())
    assert b"".join(pipe.speak("你好")) == "PCM:你好".encode()


def test_tts_without_any_stream_method_errors_clearly():
    class NoStream:
        pass

    pipe = VoicePipeline(VoiceConfig(name="x"), tts=NoStream())
    with pytest.raises(RuntimeError, match="没有 stream_tts"):
        pipe.speak("你好")


# ---------------------------------------------------------------- 缺件时的报错

def test_missing_provider_errors_clearly():
    pipe = VoicePipeline(VoiceConfig(name="x"))
    with pytest.raises(RuntimeError, match="没有配置 LLM"):
        list(pipe.respond("你好"))
    with pytest.raises(RuntimeError, match="没有配置 ASR"):
        pipe.transcribe(b"x" * 300)
    with pytest.raises(RuntimeError, match="没有配置 TTS"):
        pipe.speak("你好")
