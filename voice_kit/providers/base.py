"""供应商接口。

设计原则只有一条：**接口按"能力"分，不按"厂商"分。**

为什么重要——球球老师踩过的实账：
"要不要把 ASR 换成腾讯"这个问题前后问了两次，每次都要重新读一遍 asr.py 才能回答
"换了要动哪些地方"。根因是实现和调用揉在一起，没有一条线能指着说"换的是这个"。

三个能力，各自独立可换：
- ASRProvider：音频 → 文字
- TTSProvider：文字 → 音频（流式）
- StreamingASRProvider：音频流 → 增量文字（可选能力，不是所有厂商都有）

最后一个单独拎出来，是因为它不是"更好的 ASR"，是**另一种架构**：
一次性识别的耗时 ≈ 音频时长 × 0.5~0.6（球球老师生产实测，线性），
而流式的耗时与说话时长无关。谁支持谁不支持，调用方必须能问得出来，
所以有 supports_streaming 这个显式能力位——不要让调用方靠 try/except 去猜。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import AsyncIterator, Iterator, Optional, Protocol, runtime_checkable


@dataclass
class ASRResult:
    """识别结果。text 之外的字段都是可选的——不同厂商给的元信息不一样，
    缺的就是缺的，不要为了凑齐而编造（比如没有置信度就别填 1.0）。"""
    text: str
    confidence: Optional[float] = None
    duration_ms: Optional[float] = None      # 音频时长，用来算倍速比
    elapsed_ms: Optional[float] = None       # 识别耗时
    raw: dict = field(default_factory=dict)  # 原始响应，排查问题时不用回头改代码

    @property
    def realtime_factor(self) -> Optional[float]:
        """耗时 / 音频时长。<1 才有实时性可言；球球老师 MiMo 实测 0.55~0.59。"""
        if not self.duration_ms or not self.elapsed_ms:
            return None
        return round(self.elapsed_ms / self.duration_ms, 3)


@dataclass
class ASRPartial:
    """流式识别的中间结果。is_final=True 的那条才是可以拿去喂 LLM 的。"""
    text: str
    is_final: bool = False
    elapsed_ms: Optional[float] = None


@runtime_checkable
class ASRProvider(Protocol):
    """一次性识别：整段音频进，文字出。"""

    name: str

    def transcribe(self, audio: bytes, fmt: str = "wav", **kw) -> ASRResult:
        ...


@runtime_checkable
class StreamingASRProvider(Protocol):
    """流式识别：边送音频边出文字。

    注意这**不是** ASRProvider 的子类型——它的调用方式完全不同（要维持连接、
    要处理中间结果），硬套成继承只会让调用方以为可以互换。
    """

    name: str
    supports_streaming: bool

    async def stream(self, audio_chunks: AsyncIterator[bytes], *,
                     sample_rate: int = 16000, fmt: str = "pcm16",
                     **kw) -> AsyncIterator[ASRPartial]:
        ...


@runtime_checkable
class TTSProvider(Protocol):
    """合成：文字进，音频块流出。

    必须是流式（Iterator），不给一次性接口——因为"首块延迟"才是用户体感，
    而一次性接口结构上就没法暴露首块。球球老师实测：首块 469ms / 整句 756ms，
    如果只有一次性接口，你只会看到 756ms 并以为 TTS 很慢。
    """

    name: str
    sample_rate: int

    def stream(self, text: str, *, voice: Optional[str] = None,
               fmt: str = "pcm16", **kw) -> Iterator[bytes]:
        ...


class ProviderError(RuntimeError):
    """供应商侧的失败。带上 provider 名字——多供应商并存时，
    日志里只写"ASR 失败"等于没写。"""

    def __init__(self, provider: str, message: str, *, retryable: bool = False):
        super().__init__(f"[{provider}] {message}")
        self.provider = provider
        self.retryable = retryable
