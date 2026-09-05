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
                 max_tokens: int = 1024, temperature: Optional[float] = None):
        if not api_key:
            raise ValueError("TokenHubLLM 需要 api_key")
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.max_tokens = max_tokens
        self.temperature = temperature
        self._http = httpx.Client(timeout=httpx.Timeout(timeout, connect=10.0),
                                  limits=httpx.Limits(max_keepalive_connections=8,
                                                      max_connections=16))

    def stream(self, messages: Sequence[dict], *, system: Optional[str] = None,
               model: Optional[str] = None, **kw) -> Iterator[str]:
        """逐 token 吐出正文。

        messages 用 OpenAI 格式；system 单独传（会被插到最前），
        因为大多数调用方的 system prompt 是独立管理的一大块。
        """
        msgs = ([{"role": "system", "content": system}] if system else []) + list(messages)
        payload = {
            "model": model or self.model,
            "messages": msgs,
            "stream": True,
            "max_tokens": kw.get("max_tokens", self.max_tokens),
        }
        if self.temperature is not None:
            payload["temperature"] = self.temperature
        payload.update({k: v for k, v in kw.items() if k not in ("max_tokens",)})

        try:
            with self._http.stream("POST", f"{self.base_url}/chat/completions",
                                   headers={"Authorization": f"Bearer {self.api_key}",
                                            "Content-Type": "application/json"},
                                   json=payload) as r:
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
                        yield text
        except ProviderError:
            raise
        except Exception as e:
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
