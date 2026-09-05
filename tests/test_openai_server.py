"""OpenAI 端点测试：闸门在"被别人的编排框架调用"这个形态下必须一样管用。

这是接 TRTC 的前提——如果闸门只在我们自己的 pipeline 里成立，
那把编排交出去就等于把儿童安全交出去。
"""
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from voice_kit.gate import Rule
from voice_kit.openai_server import GatedChatCompletions, TurnRecord
from voice_kit.pipeline import VoiceConfig


class FakeLLM:
    def __init__(self, reply, rewrite="换个说法。"):
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


def kid_config(**kw):
    return VoiceConfig(
        name="球球老师", system_prompt="你是球球老师。",
        output_rules=KID_RULES,
        input_rules=[Rule(name="self_harm", words=["不想活了"])],
        fallback_reply="我们再多聊一会儿吧。",
        on_input_trigger=lambda rule, m: "我听到了……你愿意多说一点吗？",
        **kw)


def run(handler, text, headers=None):
    """跑一轮，把 SSE 解回 (完整文本, 事件列表)。"""
    body = {"model": "test-model", "stream": True,
            "messages": [{"role": "user", "content": text}]}
    raw = b"".join(handler.complete(body, headers or {}))
    events, out = [], ""
    for line in raw.decode().split("\n\n"):
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if payload == "[DONE]":
            events.append("[DONE]")
            continue
        obj = json.loads(payload)
        events.append(obj)
        out += (obj["choices"][0]["delta"].get("content") or "")
    return out, events


# ---------------------------------------------------------------- 协议形状

def test_emits_valid_openai_sse():
    """TRTC 按 OpenAI 协议解析——形状不对，整条链路直接哑掉。"""
    h = GatedChatCompletions(kid_config(), llm=FakeLLM("红石机关像迷宫。"))
    out, events = run(h, "我在玩我的世界")
    assert out == "红石机关像迷宫。"
    assert events[-1] == "[DONE]"
    assert events[-2]["choices"][0]["finish_reason"] == "stop"
    first = events[0]
    assert first["object"] == "chat.completion.chunk"
    assert first["id"].startswith("chatcmpl-")
    assert first["model"] == "test-model"


def test_non_stream_is_refused():
    """语音场景首字就是体感延迟，一次性接口结构上藏不住它。"""
    from fastapi.testclient import TestClient
    from voice_kit.openai_server import create_app
    app = create_app(kid_config(), llm=FakeLLM("你好"))
    c = TestClient(app)
    r = c.post("/v1/chat/completions",
               json={"model": "m", "stream": False, "messages": []})
    assert r.status_code == 400


def test_api_key_is_enforced():
    """这个端点会被公网直接调，没有 key 就是把模型和 prompt 白送。"""
    from fastapi.testclient import TestClient
    from voice_kit.openai_server import create_app
    app = create_app(kid_config(), llm=FakeLLM("你好"), api_key="s3cret")
    c = TestClient(app)
    body = {"model": "m", "stream": True, "messages": [{"role": "user", "content": "hi"}]}
    assert c.post("/v1/chat/completions", json=body).status_code == 401
    ok = c.post("/v1/chat/completions", json=body,
                headers={"Authorization": "Bearer s3cret"})
    assert ok.status_code == 200


# ---------------------------------------------------------------- 闸门还在

def test_violation_never_reaches_the_wire():
    """被拦下的词一个字都不能出现在 SSE 里——出去了就已经被合成成声音了。"""
    llm = FakeLLM("我觉得你可能有点抑郁。", rewrite="我们再多聊聊这件事吧。")
    h = GatedChatCompletions(kid_config(), llm=llm,
                             rewriter=lambda t, v: "".join(llm.stream([])))
    out, _ = run(h, "我最近很难过")
    assert "抑郁" not in out


def test_input_redline_takes_over_without_calling_llm():
    llm = FakeLLM("随便什么回复")
    h = GatedChatCompletions(kid_config(), llm=llm)
    out, _ = run(h, "我不想活了")
    assert out == "我听到了……你愿意多说一点吗？"
    assert llm.calls == 0, "红线命中时绝不能调 LLM"


def test_voice_mode_disables_low_latency_shortcut():
    """low_latency 是拿安全垫换延迟。语音里换掉的是"可能已经说出口"，
    所以 voice_mode 必须把它关掉——这条是硬约束，不是调优选项。"""
    cfg = kid_config(low_latency=True)
    voice = GatedChatCompletions(cfg, llm=FakeLLM("x"), voice_mode=True)
    text = GatedChatCompletions(cfg, llm=FakeLLM("x"), voice_mode=False)

    seen = {}

    def spy(name, gate):
        orig = gate.filter

        def wrapped(tokens, *, low_latency=False):
            seen[name] = low_latency
            return orig(tokens, low_latency=low_latency)
        gate.filter = wrapped

    spy("voice", voice.output_gate)
    spy("text", text.output_gate)
    run(voice, "你好")
    run(text, "你好")
    assert seen["voice"] is False, "语音模式下不该抄近路"
    assert seen["text"] is True, "文字模式下该保留这个优化"


def test_no_gate_config_passes_everything():
    """成人产品可以不设闸门——显式选择，不是默认。"""
    h = GatedChatCompletions(VoiceConfig(name="agent"), llm=FakeLLM("风险在于……"))
    out, _ = run(h, "分析一下")
    assert "风险" in out


# ---------------------------------------------------------------- 拿回对话记录

def test_turn_record_carries_trtc_identity():
    """X-User-Id 是我们把 TRTC 房间里那个人映射回 child_id 的唯一钩子。
    丢了它，报告和可信度分层就无米下锅。"""
    got = []
    h = GatedChatCompletions(kid_config(), llm=FakeLLM("恐龙很有意思！"),
                             on_turn=got.append)
    run(h, "讲讲恐龙", {"X-Task-Id": "task-1", "X-Room-Id": "room-9",
                        "X-User-Id": "child-abc"})
    assert len(got) == 1
    r: TurnRecord = got[0]
    assert (r.task_id, r.room_id, r.user_id) == ("task-1", "room-9", "child-abc")
    assert r.user_text == "讲讲恐龙"
    assert r.reply_text == "恐龙很有意思！"
    assert r.first_token_ms is not None and r.total_ms is not None


def test_turn_record_marks_takeover():
    got = []
    h = GatedChatCompletions(kid_config(), llm=FakeLLM("x"), on_turn=got.append)
    run(h, "我不想活了")
    assert got[0].took_over and got[0].takeover_rule == "self_harm"


def test_on_turn_failure_does_not_break_the_conversation():
    """落库挂了不能让孩子那边断音——这条比记录本身更重要。"""
    def boom(rec):
        raise RuntimeError("db down")

    h = GatedChatCompletions(kid_config(), llm=FakeLLM("没事的。"), on_turn=boom)
    out, _ = run(h, "你好")
    assert out == "没事的。"


# ---------------------------------------------------------------- 配置错误

def test_gate_without_fallback_is_rejected_at_construction():
    with pytest.raises(ValueError, match="fallback_reply"):
        GatedChatCompletions(
            VoiceConfig(name="bad", output_rules=KID_RULES), llm=FakeLLM("x"))
