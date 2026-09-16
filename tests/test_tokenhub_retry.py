"""连接复用重试：只在还没吐字时重来，吐过字就绝不重试。

这条边界如果搞错，孩子会听到同一句话说两遍——语音里比报错更糟。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from voice_kit.providers.base import ProviderError
from voice_kit.providers.tokenhub import TokenHubLLM


class FakeStream:
    def __init__(self, chunks, fail_after=None, status=200):
        self.chunks, self.fail_after, self.status_code = chunks, fail_after, status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return b"err"

    def iter_lines(self):
        for i, c in enumerate(self.chunks):
            if self.fail_after is not None and i == self.fail_after:
                raise RuntimeError("Server disconnected without sending a response.")
            yield c


def _line(text):
    return 'data: {"choices":[{"delta":{"content":"%s"}}]}' % text


def make(monkeypatch, responses):
    llm = TokenHubLLM("k")
    calls = {"n": 0}

    def fake_stream(*a, **kw):
        r = responses[calls["n"]]
        calls["n"] += 1
        return r
    monkeypatch.setattr(llm._http, "stream", fake_stream)
    return llm, calls


def test_retries_once_when_nothing_emitted(monkeypatch):
    """连接一上来就断（一个字都没吐）——重连一次，调用方无感。"""
    llm, calls = make(monkeypatch, [
        FakeStream([_line("x")], fail_after=0),
        FakeStream([_line("你好"), _line("呀")]),
    ])
    assert "".join(llm.stream([{"role": "user", "content": "hi"}])) == "你好呀"
    assert calls["n"] == 2


def test_does_not_retry_after_emitting(monkeypatch):
    """已经吐过字再断——**绝不重试**，否则孩子会听到同一句说两遍。"""
    llm, calls = make(monkeypatch, [
        FakeStream([_line("你好"), _line("呀")], fail_after=1),
        FakeStream([_line("完全不同的一句")]),
    ])
    got = []
    with pytest.raises(ProviderError):
        for t in llm.stream([{"role": "user", "content": "hi"}]):
            got.append(t)
    assert got == ["你好"]
    assert calls["n"] == 1, "吐过字之后不该有第二次调用"


def test_http_error_is_not_retried(monkeypatch):
    """4xx/5xx 是业务错误，重试只是白等。"""
    llm, calls = make(monkeypatch, [FakeStream([], status=429), FakeStream([_line("x")])])
    with pytest.raises(ProviderError, match="HTTP 429"):
        list(llm.stream([{"role": "user", "content": "hi"}]))
    assert calls["n"] == 1


def test_thinking_off_sets_enable_thinking(monkeypatch):
    """语音场景最大的杠杆：实测首字 3726→1387ms。这条钉住字段名别被改掉。

    2026-09-14 改：默认写法从 DeepSeek 专用的 `enable_thinking:false` 换成 TokenHub 通用的
    `thinking:{type:disabled}`——实测对 DeepSeek 同样生效，而 GLM/混元/MiniMax 只认通用写法
    （用旧写法它们照样思考，64 个 token 全被吃掉一个字没出）。旧写法保留为 thinking_style="enable_thinking"。"""
    llm = TokenHubLLM("k")
    seen = {}

    def fake_stream(method, url, *, headers, json, **kw):
        seen.update(json)
        seen["_headers"] = headers
        return FakeStream([_line("好")])
    monkeypatch.setattr(llm._http, "stream", fake_stream)
    list(llm.stream([{"role": "user", "content": "hi"}], thinking=False,
                    cache_key="child-1", session_id="s-1"))
    assert seen["thinking"] == {"type": "disabled"}
    assert "enable_thinking" not in seen
    assert seen["prompt_cache_key"] == "child-1"
    assert seen["_headers"]["X-Session-ID"] == "s-1"


def test_thinking_on_by_default_sends_nothing(monkeypatch):
    """默认不发这个字段——别的模型未必认，不该强加。"""
    llm = TokenHubLLM("k")
    seen = {}
    monkeypatch.setattr(llm._http, "stream",
                        lambda *a, **kw: (seen.update(kw["json"]), FakeStream([_line("好")]))[1])
    list(llm.stream([{"role": "user", "content": "hi"}]))
    assert "enable_thinking" not in seen
