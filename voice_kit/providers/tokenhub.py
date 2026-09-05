"""腾讯 TokenHub：大模型服务平台，OpenAI 协议兼容。

base_url = https://tokenhub.tencentmaas.com/v1

为什么它在 voice-kit 里很关键——**它同时托管了很多家的模型**，包括
`mimo-v2.5-pro` 本身。这带来一个别处没有的能力：

    换网关 ≠ 换模型

球球老师实测（同一台服务器、同一个 prompt、同一个模型）：

    mimo-v2.5-pro 直连 MiMo      首字 4337ms
    mimo-v2.5-pro 走 TokenHub    首字 3282ms   (-24%)
    deepseek-v4-flash            首字 2392ms   (-45%)

前两行是**同一个模型**，所以行为不变——对儿童产品意味着 8 铁律那套安全回归
**不需要重跑**，白拿 24%。第三行是换模型，快得多，但全套安全护栏要重验。
把这两件事分开，是这个 provider 存在的意义。

⚠️ 选模型的一个坑（实测）：`hy4-preview` / `glm-5.3-flash` / `kimi-k2.6` / `hy3`
在 max_tokens=150 时**正文一个字都吐不出来**——thinking 把配额全吃光了。
这类推理型模型不适合语音对话（用户在等着听声音，不是在等它想）。
选型时务必先量"首字"，不要只看榜单分数。
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Iterator, Optional, Sequence

import httpx

from .base import ProviderError

DEFAULT_BASE_URL = "https://tokenhub.tencentmaas.com/v1"

# 实测适合语音对话的（会正常吐正文，不被 thinking 吃光）。
# 不是"最好的模型"清单，是"首字快且真的会说话"的清单。
VOICE_FRIENDLY_MODELS = ("deepseek-v4-flash", "mimo-v2.5-pro")


@dataclass
class LLMChunk:
    text: str
    first_ms: Optional[float] = None


class TokenHubLLM:
    """OpenAI 协议的流式对话。

    刻意只做**流式**——语音场景里"首字"就是体感延迟，一次性接口结构上
    没法暴露它。跟 TTSProvider 只给流式接口是同一个道理。
    """

    name = "tokenhub"

    def __init__(self, api_key: str, *, model: str = "deepseek-v4-flash",
                 base_url: str = DEFAULT_BASE_URL, timeout: float = 90.0,
                 max_tokens: int = 1024, temperature: Optional[float] = None,
                 thinking: bool = True):
        if not api_key:
            raise ValueError("TokenHubLLM 需要 api_key")
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.thinking = thinking
        self._http = httpx.Client(timeout=httpx.Timeout(timeout, connect=10.0),
                                  limits=httpx.Limits(max_keepalive_connections=8,
                                                      max_connections=16))

    def stream(self, messages: Sequence[dict], *, system: Optional[str] = None,
               model: Optional[str] = None, cache_key: Optional[str] = None,
               session_id: Optional[str] = None, thinking: Optional[bool] = None,
               **kw) -> Iterator[str]:
        """逐 token 吐出正文。

        messages 用 OpenAI 格式；system 单独传（会被插到最前），
        因为大多数调用方的 system prompt 是独立管理的一大块。

        ## cache_key / session_id：不传等于白白多等几秒

        TokenHub 支持前缀缓存（`deepseek-v4-flash` 在支持列表里），但**要你自己
        报身份**，否则它不知道哪些请求共享前缀：

            cache_key  → 请求体 `prompt_cache_key`，用 conversation_id 这类
                         **跨轮稳定**的 id（不是每轮都变的 session/turn id）
            session_id → HTTP 头 `X-Session-ID`，把同一用户的连续请求路由到
                         同一个推理实例，提升那台机器上的本地 KV Cache 命中

        球球老师的场景正是最理想的：38363 字的 8 铁律 system prompt 每轮一字不变，
        每轮变的只有末尾几百字。实测不传这两个字段时，同一 prompt 连发三次
        首字 4089/4991/6444ms，**看不到任何缓存效果**。

        ⚠️ 前缀缓存只认**前缀**。把每轮都变的东西（时间戳、轮次计数）放在
        system prompt 开头，会把整段前缀打断，缓存直接失效。

        ## thinking=False：语音场景最大的一个杠杆

        2026-09-05 实测（球球老师 38363 字 prompt，deepseek-v4-flash，各 3 次）：

            什么都不加（思考开）           首字中位 3726ms
            enable_thinking=False        首字中位 1387ms   ✅ −63%
            thinking={"type":"disabled"} 首字中位 1417ms   （也生效）

        对照组更说明问题：把 prompt 从 38363 字砍到 51 字，思考开着时
        3374→2654ms，**只省 720ms**。所以

            关思考 ≈ 2100ms   >>   把 prompt 砍掉 99% ≈ 700ms

        先关思考，再谈精简 prompt——顺序反了就是花大力气办小事。
        （语音场景本来也不该让模型"想"：用户在等着听声音。）
        """
        msgs = ([{"role": "system", "content": system}] if system else []) + list(messages)
        payload = {
            "model": model or self.model,
            "messages": msgs,
            "stream": True,
            "max_tokens": kw.get("max_tokens", self.max_tokens),
        }
        if cache_key:
            payload["prompt_cache_key"] = cache_key
        if (self.thinking if thinking is None else thinking) is False:
            payload["enable_thinking"] = False
        if self.temperature is not None:
            payload["temperature"] = self.temperature
        payload.update({k: v for k, v in kw.items() if k not in ("max_tokens",)})

        headers = {"Authorization": f"Bearer {self.api_key}",
                   "Content-Type": "application/json"}
        if session_id:
            headers["X-Session-ID"] = session_id

        # 连接复用的坑（2026-09-05 线上撞到）：长连接被对端关掉后，httpx 仍会
        # 拿它发下一个请求，得到 `RemoteProtocolError: Server disconnected
        # without sending a response`。探针脚本每次新建 Client 所以从没遇到，
        # 一放进常驻服务就必现。
        #
        # 重试的边界很重要：**只在一个 token 都还没吐出去的时候重试**。
        # 已经吐过字再重来，孩子那边会听到同一句话说两遍。
        emitted = False
        for attempt in (1, 2):
            try:
                with self._http.stream("POST", f"{self.base_url}/chat/completions",
                                       headers=headers, json=payload) as r:
                    if r.status_code != 200:
                        body = r.read().decode("utf-8", "ignore")[:200]
                        raise ProviderError(self.name, f"HTTP {r.status_code}: {body}")
                    for line in r.iter_lines():
                        if not line or not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            break
                        try:
                            obj = json.loads(data)
                        except Exception:
                            continue
                        delta = (obj.get("choices") or [{}])[0].get("delta", {})
                        text = delta.get("content") or ""
                        if text:
                            emitted = True
                            yield text
                return
            except ProviderError:
                raise
            except Exception as e:
                if attempt == 1 and not emitted:
                    continue          # 多半是复用了一条已经死掉的连接，重连再来
                raise ProviderError(self.name, f"对话失败: {e}") from e

    def measure_first_token(self, probe: str = "你好", *,
                            system: Optional[str] = None,
                            model: Optional[str] = None) -> float:
        """量首字延迟（毫秒）。

        选型必用——榜单分数不告诉你"孩子要等多久才听见声音"，
        而实测里有好几个高分模型在这项上直接出局（正文被 thinking 吃光）。
        """
        t0 = time.perf_counter()
        for _ in self.stream([{"role": "user", "content": probe}],
                             system=system, model=model, max_tokens=64):
            return (time.perf_counter() - t0) * 1000
        raise ProviderError(self.name,
                            f"{model or self.model} 没有吐出任何正文"
                            "（多半是 thinking 把 max_tokens 吃光了，不适合语音场景）")

    def close(self) -> None:
        self._http.close()
