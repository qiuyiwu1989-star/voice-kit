"""TokenHub provider：关思考的写法、reasoning_effort、剥 <think>。不联网。"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from voice_kit.providers.tokenhub import TokenHubLLM, _strip_think


def test_strip_think_streaming_across_chunks():
    st = False
    out = ""
    for chunk in ["你好<thi", "nk>我在想", "什么</think>五年级", "数学<think>x</think>三千。"]:
        vis, st = _strip_think(chunk, st) if "<thi" not in chunk else (chunk.split("<thi")[0], True)
        out += vis
    # 上面的手工切分不完整，用完整的调用再验一遍
    st, out = False, ""
    for chunk in ["你好<think>我在想", "什么</think>五年级", "数学<think>x</think>三千。"]:
        vis, st = _strip_think(chunk, st); out += vis
    assert out == "你好五年级数学三千。" and st is False


def _capture(monkeypatch, llm):
    sent = {}
    class R:
        status_code = 200
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def iter_lines(self):
            yield 'data: {"choices":[{"delta":{"content":"<think>秘密</think>好"}}]}'
            yield "data: [DONE]"
    def stream(method, url, headers=None, json=None):
        sent["url"], sent["headers"], sent["json"] = url, headers, json
        return R()
    monkeypatch.setattr(llm._http, "stream", stream)
    return sent


def test_thinking_disabled_universal_style(monkeypatch):
    llm = TokenHubLLM("k", thinking=False)
    sent = _capture(monkeypatch, llm)
    out = "".join(llm.stream([{"role": "user", "content": "hi"}], model="hy3", reasoning_effort="low"))
    assert out == "好"                                             # <think> 被剥掉
    assert sent["json"]["thinking"] == {"type": "disabled"}
    assert "enable_thinking" not in sent["json"]
    assert sent["json"]["reasoning_effort"] == "low"
    assert sent["headers"]["Authorization"] == "Bearer k"


def test_thinking_disabled_legacy_style_and_api_key_header(monkeypatch):
    llm = TokenHubLLM("k", thinking=False, thinking_style="enable_thinking", auth_header="api-key")
    sent = _capture(monkeypatch, llm)
    "".join(llm.stream([{"role": "user", "content": "hi"}]))
    assert sent["json"]["enable_thinking"] is False and "thinking" not in sent["json"]
    assert sent["headers"]["api-key"] == "k" and "Authorization" not in sent["headers"]


def test_thinking_field_off_sends_nothing(monkeypatch):
    llm = TokenHubLLM("k", thinking=False, thinking_field=False)
    sent = _capture(monkeypatch, llm)
    "".join(llm.stream([{"role": "user", "content": "hi"}]))
    assert "thinking" not in sent["json"] and "enable_thinking" not in sent["json"]
