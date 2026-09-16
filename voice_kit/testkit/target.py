"""被测目标：三种形态，一个接口。

    ServiceTarget   线上/联调的 voice-kit 服务（HTTP，带项目钥匙）——测的是部署出来的东西
    AppTarget       进程内的 FastAPI app（TestClient）——CI 用，不联网
    CallableTarget  任意 python 函数 messages → token 迭代器——直接测一个 agent 的大脑，
                    比如球球老师的 container.stream_message 包一层

三者都返回 TurnResult：文本、首字毫秒、总毫秒、有没有 [DONE]、原始分片数。
首字和总时长都是**从这里**量的，包含网络；要拆开看得靠服务端埋点。
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable, Iterator, List, Optional, Protocol


@dataclass
class TurnResult:
    text: str
    first_token_ms: Optional[float]
    total_ms: float
    done: bool = True                 # 收到了 data: [DONE]
    chunks: int = 0
    status: Optional[int] = None
    error: Optional[str] = None
    raw_events: List[dict] = field(default_factory=list)   # 解析出来的分片（含 meta.info）

    @property
    def meta(self) -> List[dict]:
        return [e for e in self.raw_events if e.get("type") == "meta.info"]


class Target(Protocol):
    name: str

    def turn(self, messages: List[dict], *, headers: Optional[dict] = None,
             body_extra: Optional[dict] = None) -> TurnResult: ...


# ---------------------------------------------------------------- SSE 解析

def parse_sse_lines(lines: Iterable[str], t0: float) -> TurnResult:
    """把 OpenAI 风格的 SSE 行流解析成 TurnResult。协议不对也不抛，记进 error。"""
    text, first, chunks, done, events, err = "", None, 0, False, [], None
    for line in lines:
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if payload == "[DONE]":
            done = True
            continue
        try:
            obj = json.loads(payload)
        except json.JSONDecodeError:
            err = f"SSE 分片不是 JSON: {payload[:60]!r}"
            continue
        events.append(obj)
        if obj.get("type") == "meta.info":
            continue
        try:
            delta = obj["choices"][0]["delta"].get("content") or ""
        except (KeyError, IndexError, TypeError):
            err = f"分片缺 choices[0].delta: {payload[:60]!r}"
            continue
        chunks += 1
        if delta and first is None:
            first = round((time.perf_counter() - t0) * 1000, 1)
        text += delta
    return TurnResult(text=text, first_token_ms=first,
                      total_ms=round((time.perf_counter() - t0) * 1000, 1),
                      done=done, chunks=chunks, raw_events=events, error=err)


def _body(messages, extra):
    b = {"stream": True, "messages": messages}
    if extra:
        b.update(extra)
    return b


# ---------------------------------------------------------------- 三种目标

class ServiceTarget:
    """HTTP 打真实服务。用 httpx 流式读，首字才量得准。"""

    def __init__(self, base_url: str, api_key: str, *, timeout: float = 60.0):
        import httpx
        self.name = f"service:{base_url}"
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self._http = httpx.Client(timeout=httpx.Timeout(timeout, connect=10.0))

    def turn(self, messages, *, headers=None, body_extra=None) -> TurnResult:
        h = {"Authorization": f"Bearer {self.api_key}", **(headers or {})}
        t0 = time.perf_counter()
        try:
            with self._http.stream("POST", f"{self.base_url}/v1/chat/completions",
                                   json=_body(messages, body_extra), headers=h) as r:
                if r.status_code != 200:
                    body = r.read().decode(errors="replace")[:200]
                    return TurnResult("", None, round((time.perf_counter() - t0) * 1000, 1),
                                      done=False, status=r.status_code,
                                      error=f"HTTP {r.status_code}: {body}")
                res = parse_sse_lines(r.iter_lines(), t0)
                res.status = 200
                return res
        except Exception as e:  # 网络层的错也要进报告，不能让整套跑挂掉
            return TurnResult("", None, round((time.perf_counter() - t0) * 1000, 1),
                              done=False, error=f"{type(e).__name__}: {e}")


class AppTarget:
    """进程内 FastAPI app。CI 用。"""

    def __init__(self, app, api_key: str):
        from fastapi.testclient import TestClient
        self.name = "app"
        self.api_key = api_key
        self._c = TestClient(app)

    def turn(self, messages, *, headers=None, body_extra=None) -> TurnResult:
        h = {"Authorization": f"Bearer {self.api_key}", **(headers or {})}
        t0 = time.perf_counter()
        r = self._c.post("/v1/chat/completions", json=_body(messages, body_extra), headers=h)
        if r.status_code != 200:
            return TurnResult("", None, round((time.perf_counter() - t0) * 1000, 1),
                              done=False, status=r.status_code,
                              error=f"HTTP {r.status_code}: {r.text[:200]}")
        res = parse_sse_lines(r.text.split("\n"), t0)
        res.status = 200
        return res


class CallableTarget:
    """任意 `fn(messages, headers) -> Iterator[str]`。

    直接测 agent 的大脑，不经过 HTTP 和闸门——所以 `expect_gated`/`expect_takeover`
    这类闸门断言对它没意义（runner 会跳过并注明）。
    """

    def __init__(self, fn: Callable[..., Iterator[str]], name: str = "callable"):
        self.name = name
        self.fn = fn

    def turn(self, messages, *, headers=None, body_extra=None) -> TurnResult:
        t0 = time.perf_counter()
        text, first, n = "", None, 0
        try:
            for tok in self.fn(messages, headers or {}):
                n += 1
                if tok and first is None:
                    first = round((time.perf_counter() - t0) * 1000, 1)
                text += tok
        except Exception as e:
            return TurnResult(text, first, round((time.perf_counter() - t0) * 1000, 1),
                              done=False, chunks=n, error=f"{type(e).__name__}: {e}")
        return TurnResult(text, first, round((time.perf_counter() - t0) * 1000, 1),
                          done=True, chunks=n)
