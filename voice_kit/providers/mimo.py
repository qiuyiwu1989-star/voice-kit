"""小米 MiMo：ASR + TTS 走同一个网关、同一个 key。

从球球老师生产代码抽取（2026-09）。**保留了两个踩坑修复**，这是这份实现真正的价值：

① WAV 分片拼接（_dewav_chunk）
   MiMo 流式 TTS 的每个 SSE 分片都是**独立完整的 WAV 文件**，各自带 44 字节头，
   不是常见的"首片带头、后续裸 PCM"。直接拼接会得到"好几个 WAV 粘在一起"的畸形
   文件——播放器只认第一个头声明的长度，播完往往就是第一句然后静默。
   球球老师线上真出过这个 bug：一个 469KB / 9.8 秒的回复，用 Python wave 模块解出来
   只有 0.16 秒。修法见 _dewav_chunk。

② 限速重试
   ASR 在整班同时说话时很容易撞 RPM 上限，而这类失败几秒内就能恢复。
   只对 429 退避重试，其它错误（比如音频格式不对）立即失败——不浪费时间。

已知特性（实测，不是文档）：
- 无音频流式输入。stream=True 只是把**文字**分片吐回，音频仍要整段传完。
  实测首字节 4679ms 反而比非流式全程 4246ms 更慢，对延迟零收益。
- 识别耗时 ≈ 音频时长 × 0.55~0.59，线性。孩子说得越久等得越久。
"""
from __future__ import annotations

import base64
import json
import struct
import time
from typing import Iterator, Optional

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from .base import ASRResult, ProviderError

_WAV_HEADER_LEN = 44
_WAV_UNKNOWN_SIZE = 0xFFFFFFFF   # WAV 流式"长度未知"的标准约定


def _is_wav_chunk(b: bytes) -> bool:
    return len(b) >= _WAV_HEADER_LEN and b[:4] == b"RIFF" and b[8:12] == b"WAVE"


def _dewav_chunk(chunk: bytes, is_first: bool) -> bytes:
    """见模块 docstring ①。第一片保留头但把声明长度改成"未知"，之后每片剥掉自己的头。"""
    if not _is_wav_chunk(chunk):
        return chunk
    if not is_first:
        return chunk[_WAV_HEADER_LEN:]
    header = bytearray(chunk[:_WAV_HEADER_LEN])
    struct.pack_into("<I", header, 4, _WAV_UNKNOWN_SIZE)
    struct.pack_into("<I", header, 40, _WAV_UNKNOWN_SIZE)
    return bytes(header) + chunk[_WAV_HEADER_LEN:]


from .wav import wav_duration_ms  # noqa: E402  —— 真源在 wav.py，别在这里再写一份


def pcm16_to_wav(pcm: bytes, sample_rate: int = 16000, channels: int = 1) -> bytes:
    """给裸 PCM 补 WAV 头。小程序录音、嵌入式采集缓冲给出来的都是无头裸流，
    而 ASR 那边认有头格式——统一在这里补，省得每个端各写一遍还各写各的错。"""
    n = len(pcm)
    byte_rate = sample_rate * channels * 2
    return (b"RIFF" + struct.pack("<I", 36 + n) + b"WAVEfmt " +
            struct.pack("<IHHIIHH", 16, 1, channels, sample_rate, byte_rate, channels * 2, 16) +
            b"data" + struct.pack("<I", n) + pcm)


def _is_rate_limited(e: BaseException) -> bool:
    return isinstance(e, httpx.HTTPStatusError) and e.response.status_code == 429


class MimoProvider:
    """ASR + TTS 合一：MiMo 两者共用同一个网关和 key。"""

    name = "mimo"
    supports_streaming = False          # 无音频流式输入，见模块 docstring

    def __init__(self, api_url: str, api_key: str, *,
                 asr_model: str = "mimo-v2.5-asr",
                 tts_model: str = "mimo-v2.5-tts",
                 tts_voice: str = "茉莉",
                 voice_directors: Optional[dict] = None,
                 sample_rate: int = 24000,
                 timeout: float = 90.0):
        if not api_url or not api_key:
            raise ValueError("MimoProvider 需要 api_url 和 api_key")
        self.api_url = api_url.strip()
        self.api_key = api_key
        self.asr_model = asr_model
        self.tts_model = tts_model
        self.tts_voice = tts_voice
        self.voice_directors = voice_directors or {}
        self.sample_rate = sample_rate
        # 复用连接池：电话模式每说一句就是一次请求，裸 post 每次都要重付一次
        # TCP+TLS 握手（实测该网关 TLS 约 73ms）。
        self._http = httpx.Client(timeout=httpx.Timeout(timeout, connect=10.0),
                                  limits=httpx.Limits(max_keepalive_connections=8,
                                                      max_connections=16))

    # ---------------------------------------------------------------- ASR

    @retry(retry=retry_if_exception(_is_rate_limited), stop=stop_after_attempt(3),
           wait=wait_exponential(multiplier=1, min=1, max=4), reraise=True)
    def _post(self, payload: dict, timeout: Optional[float] = None) -> dict:
        r = self._http.post(self.api_url, headers=self._headers(), json=payload, timeout=timeout)
        r.raise_for_status()
        return r.json()

    def _headers(self) -> dict:
        return {"api-key": self.api_key, "Content-Type": "application/json"}

    def transcribe(self, audio: bytes, fmt: str = "wav", **kw) -> ASRResult:
        if not audio or len(audio) < 200:
            return ASRResult(text="")           # 太短当误触，不浪费一次识别
        if fmt in ("pcm", "pcm16", "raw"):
            audio = pcm16_to_wav(audio, sample_rate=kw.get("sample_rate", 16000))
            fmt = "wav"
        payload = {
            "model": self.asr_model,
            "messages": [{"role": "user", "content": [
                {"type": "input_audio",
                 "input_audio": {"data": base64.b64encode(audio).decode(), "format": fmt}}]}],
            "asr_options": {"language": kw.get("language", "auto")},
        }
        t0 = time.perf_counter()
        try:
            d = self._post(payload)
        except Exception as e:
            raise ProviderError(self.name, f"ASR 失败: {e}", retryable=_is_rate_limited(e)) from e
        elapsed = (time.perf_counter() - t0) * 1000
        return ASRResult(
            text=(d["choices"][0]["message"].get("content") or "").strip(),
            duration_ms=wav_duration_ms(audio),
            elapsed_ms=round(elapsed, 1),
            raw=d,
        )

    # ---------------------------------------------------------------- TTS

    def _tts_payload(self, text: str, voice: Optional[str], fmt: str) -> dict:
        v = voice or self.tts_voice
        director = self.voice_directors.get(v, "")
        msgs = []
        if director:
            msgs.append({"role": "user", "content": director})
        msgs.append({"role": "assistant", "content": text})
        return {"model": self.tts_model, "messages": msgs,
                "audio": {"format": "wav" if fmt != "pcm16" else "wav", "voice": v},
                "stream": True}

    def stream(self, text: str, *, voice: Optional[str] = None,
               fmt: str = "pcm16", **kw) -> Iterator[bytes]:
        """流式合成。fmt=pcm16 给 Web Audio 无缝拼接；fmt=wav 给 <audio> 边下边播。

        注意 MiMo 两种都是发 wav 分片回来，pcm16 是我们这边剥头得到的——
        所以两条路共用同一个请求，只是解码方式不同。
        """
        payload = self._tts_payload(text, voice, fmt)
        first = True
        try:
            with self._http.stream("POST", self.api_url, headers=self._headers(),
                                   json=payload) as r:
                r.raise_for_status()
                for line in r.iter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    body = line[5:].strip()
                    if body == "[DONE]":
                        break
                    try:
                        obj = json.loads(body)
                    except Exception:
                        continue
                    audio = (obj.get("choices") or [{}])[0].get("delta", {}).get("audio")
                    if not (audio and audio.get("data")):
                        continue
                    chunk = base64.b64decode(audio["data"])
                    if fmt == "pcm16":
                        # 全部剥成裸 PCM——调用方自己知道采样率，不需要头
                        yield chunk[_WAV_HEADER_LEN:] if _is_wav_chunk(chunk) else chunk
                    else:
                        yield _dewav_chunk(chunk, first)
                    first = False
        except Exception as e:
            raise ProviderError(self.name, f"TTS 失败: {e}") from e

    def close(self) -> None:
        self._http.close()
