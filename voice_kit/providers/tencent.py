"""腾讯云语音：实时流式 ASR（WebSocket）+ 一句话识别 + TTS。

为什么要它——不是因为"腾讯更快"。那个假设已经被实测证伪两次（见 README）：
- 同云更快？腾讯**流式**端点 155ms，比 MiMo 的 119ms 还慢
- 引擎更快？腾讯一句话识别 4609ms vs MiMo 4721ms，差 2%

真正的理由只有一条：**MiMo 没有音频流式输入，腾讯有。**

一次性识别的耗时 ≈ 音频时长 × 0.56（线性，实测），孩子说 10 秒就要等 6 秒。
流式识别是边说边认，说完时基本已经认完——耗时与说话时长**无关**。
这不是"换个更快的供应商"，是换一种架构。

两套鉴权，别搞混：
- 一句话识别 / TTS → 云 API 3.0，TC3-HMAC-SHA256 签名，只要 SecretId/Key
- 实时语音识别   → WebSocket，**HMAC-SHA1 + base64**，还要 AppId

后者是老一代签名，跟前者完全不同（这是接入时最容易卡住的地方）。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import random
import time
import urllib.parse
from datetime import datetime, timezone
from typing import Iterator, Optional

import httpx

from .base import ASRPartial, ASRResult, ProviderError

# ============================================================================
# 云 API 3.0 签名（一句话识别 / TTS 用）
# ============================================================================


def _tc3_headers(secret_id: str, secret_key: str, host: str, service: str,
                 action: str, version: str, body: str, region: str = "ap-guangzhou") -> dict:
    ts = int(time.time())
    date = datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")
    ct = "application/json; charset=utf-8"
    canonical = "\n".join([
        "POST", "/", "",
        f"content-type:{ct}\nhost:{host}\nx-tc-action:{action.lower()}\n",
        "content-type;host;x-tc-action",
        hashlib.sha256(body.encode()).hexdigest(),
    ])
    scope = f"{date}/{service}/tc3_request"
    to_sign = "\n".join(["TC3-HMAC-SHA256", str(ts), scope,
                         hashlib.sha256(canonical.encode()).hexdigest()])
    k = hmac.new(("TC3" + secret_key).encode(), date.encode(), hashlib.sha256).digest()
    k = hmac.new(k, service.encode(), hashlib.sha256).digest()
    k = hmac.new(k, b"tc3_request", hashlib.sha256).digest()
    sig = hmac.new(k, to_sign.encode(), hashlib.sha256).hexdigest()
    return {
        "Content-Type": ct, "Host": host, "X-TC-Action": action,
        "X-TC-Version": version, "X-TC-Timestamp": str(ts), "X-TC-Region": region,
        "Authorization": (f"TC3-HMAC-SHA256 Credential={secret_id}/{scope}, "
                          f"SignedHeaders=content-type;host;x-tc-action, Signature={sig}"),
    }


class TencentProvider:
    """腾讯云 ASR + TTS。

    supports_streaming=True —— 这是它相对 MiMo 唯一真正的优势，
    也是引入它的唯一理由。
    """

    name = "tencent"
    supports_streaming = True

    def __init__(self, secret_id: str, secret_key: str, app_id: int | str, *,
                 region: str = "ap-guangzhou",
                 asr_engine: str = "16k_zh",
                 tts_voice: int = 101016,        # 智瑜，女声，适合儿童场景
                 sample_rate: int = 16000,
                 timeout: float = 60.0):
        if not (secret_id and secret_key and app_id):
            raise ValueError("TencentProvider 需要 secret_id / secret_key / app_id")
        self.secret_id = secret_id
        self.secret_key = secret_key
        self.app_id = str(app_id)
        self.region = region
        self.asr_engine = asr_engine
        self.tts_voice = tts_voice
        self.sample_rate = sample_rate
        self._http = httpx.Client(timeout=httpx.Timeout(timeout, connect=10.0),
                                  limits=httpx.Limits(max_keepalive_connections=8,
                                                      max_connections=16))

    def _call(self, host: str, service: str, action: str, version: str, payload: dict) -> dict:
        body = json.dumps(payload)
        hdr = _tc3_headers(self.secret_id, self.secret_key, host, service,
                           action, version, body, self.region)
        r = self._http.post(f"https://{host}/", content=body, headers=hdr)
        r.raise_for_status()
        d = r.json().get("Response", {})
        if "Error" in d:
            e = d["Error"]
            raise ProviderError(self.name, f"{e.get('Code')}: {e.get('Message')}")
        return d

    # ---------------------------------------------------------------- 一次性 ASR

    def transcribe(self, audio: bytes, fmt: str = "wav", **kw) -> ASRResult:
        """一句话识别（≤60 秒）。留着做对照组和兜底——
        流式连接建不起来时还能降级到这条，不至于整个语音功能挂掉。"""
        if not audio or len(audio) < 200:
            return ASRResult(text="")
        t0 = time.perf_counter()
        d = self._call("asr.tencentcloudapi.com", "asr", "SentenceRecognition", "2019-06-14", {
            "EngSerViceType": self.asr_engine,
            "SourceType": 1,
            "VoiceFormat": fmt,
            "Data": base64.b64encode(audio).decode(),
            "DataLen": len(audio),
        })
        from .mimo import wav_duration_ms
        return ASRResult(text=(d.get("Result") or "").strip(),
                         duration_ms=wav_duration_ms(audio),
                         elapsed_ms=round((time.perf_counter() - t0) * 1000, 1),
                         raw=d)

    # ---------------------------------------------------------------- 流式 ASR

    def _realtime_url(self, voice_id: str) -> str:
        """实时语音识别的 WebSocket 地址。

        ⚠️ 这里用的是 **HMAC-SHA1 + base64**，跟上面 TC3-HMAC-SHA256 完全不同。
        签名对象是"去掉协议头的完整 URL（含已排序的 query）"。参数必须按字典序，
        否则签出来的名字对不上——这是接入时最常见的 401 来源。
        """
        params = {
            "secretid": self.secret_id,
            "timestamp": str(int(time.time())),
            "expired": str(int(time.time()) + 86400),
            "nonce": str(random.randint(100000, 999999)),
            "engine_model_type": self.asr_engine,
            "voice_id": voice_id,
            "voice_format": "1",          # 1=pcm
            "needvad": "1",
            "filter_dirty": "0",
            "filter_modal": "0",
            "filter_punc": "0",
            "convert_num_mode": "1",
        }
        qs = "&".join(f"{k}={params[k]}" for k in sorted(params))
        path = f"asr.cloud.tencent.com/asr/v2/{self.app_id}"
        sig = base64.b64encode(
            hmac.new(self.secret_key.encode(), f"{path}?{qs}".encode(), hashlib.sha1).digest()
        ).decode()
        return f"wss://{path}?{qs}&signature={urllib.parse.quote(sig)}"

    async def stream(self, audio_chunks, *, sample_rate: int = 16000,
                     fmt: str = "pcm16", **kw):
        """边送音频边收文字。

        产出 ASRPartial：is_final=False 是中间结果（可以显示但别拿去喂 LLM），
        is_final=True 才是这一句的定稿。

        依赖 websockets 包（可选依赖——不用流式就不用装）。
        """
        try:
            import websockets
        except ImportError as e:
            raise ProviderError(self.name,
                                "流式 ASR 需要 websockets 包：pip install websockets") from e

        voice_id = f"vk{int(time.time() * 1000)}{random.randint(100, 999)}"
        url = self._realtime_url(voice_id)
        t0 = time.perf_counter()
        try:
            async with websockets.connect(url, open_timeout=10, close_timeout=5) as ws:
                greeting = json.loads(await ws.recv())
                if greeting.get("code") != 0:
                    raise ProviderError(self.name,
                                        f"握手失败 code={greeting.get('code')} {greeting.get('message','')}")

                async def send_all():
                    async for chunk in audio_chunks:
                        if chunk:
                            await ws.send(chunk)
                    await ws.send(json.dumps({"type": "end"}))

                import asyncio
                sender = asyncio.create_task(send_all())
                try:
                    while True:
                        raw = await ws.recv()
                        msg = json.loads(raw)
                        if msg.get("code") != 0:
                            raise ProviderError(self.name,
                                                f"识别失败 code={msg.get('code')} {msg.get('message','')}")
                        result = msg.get("result") or {}
                        text = (result.get("voice_text_str") or "").strip()
                        # slice_type: 0=一段开始 1=一段中间 2=一段结束（定稿）
                        if text:
                            yield ASRPartial(text=text,
                                             is_final=result.get("slice_type") == 2,
                                             elapsed_ms=round((time.perf_counter() - t0) * 1000, 1))
                        if msg.get("final") == 1:
                            break
                finally:
                    sender.cancel()
        except ProviderError:
            raise
        except Exception as e:
            raise ProviderError(self.name, f"流式 ASR 失败: {e}") from e

    # ---------------------------------------------------------------- TTS

    def synthesize(self, text: str, *, voice: Optional[int] = None, **kw) -> bytes:
        """一次性合成（云 API 3.0 TextToVoice，返回完整 WAV）。

        保留它有两个用处：流式连不上时降级，以及做流式的对照组。
        """
        d = self._call("tts.tencentcloudapi.com", "tts", "TextToVoice", "2019-08-23", {
            "Text": text[:1000],
            "SessionId": f"vk{int(time.time()*1000)}",
            "VoiceType": voice or self.tts_voice,
            "Codec": "wav",
            "SampleRate": self.sample_rate,
        })
        return base64.b64decode(d.get("Audio", ""))

    def _stream_tts_url(self, text: str, voice: int, session_id: str,
                        codec: str) -> str:
        """实时语音合成的 WebSocket 地址。

        ⚠️ 跟实时 ASR 同属 HMAC-SHA1 家族，但**签名串多了一个 "GET" 前缀**，
        而且 Text 要参与签名。两处都很容易抄错：
            ASR:  asr.cloud.tencent.com/asr/v2/<appid>?<sorted qs>
            TTS:  GETtts.cloud.tencent.com/stream_ws?<sorted qs>
                  ^^^ 就是这三个字母

        另一个坑：**签名用的是未编码的原始参数值**（中文 Text 原样参与），
        而拼进最终 URL 时才做 urlencode。先编码再签会 401。
        """
        now = int(time.time())
        params = {
            "Action": "TextToStreamAudioWS",
            "AppId": self.app_id,
            "SecretId": self.secret_id,
            "Timestamp": str(now),
            "Expired": str(now + 86400),
            "SessionId": session_id,
            "Text": text,
            "VoiceType": str(voice),
            "Codec": codec,
            "SampleRate": str(self.sample_rate),
            "Volume": "0",
            "Speed": "0",
        }
        qs = "&".join(f"{k}={params[k]}" for k in sorted(params))
        sig = base64.b64encode(
            hmac.new(self.secret_key.encode(),
                     f"GETtts.cloud.tencent.com/stream_ws?{qs}".encode(),
                     hashlib.sha1).digest()
        ).decode()
        encoded = "&".join(f"{k}={urllib.parse.quote(params[k], safe='')}"
                           for k in sorted(params))
        return (f"wss://tts.cloud.tencent.com/stream_ws?{encoded}"
                f"&Signature={urllib.parse.quote(sig, safe='')}")

    def stream_tts(self, text: str, *, voice: Optional[int] = None,
                   codec: str = "pcm", **kw) -> Iterator[bytes]:
        """流式合成：边合成边吐音频块，首块不用等整句合完。

        为什么值得做（之前判断"先不做"是错的）：一次性合成的耗时随文本长度
        线性增长，跟一次性 ASR 是同一个毛病。球球老师的回复经常是两三句话，
        整句合完再播 = 用户干等。流式的首块与后文长度无关。

        默认 `codec="pcm"`——裸 PCM16 可以直接往播放器里灌，不用等 WAV 头。
        要落盘或走 `<audio>` 传 `codec="mp3"`。

        连不上就抛 ProviderError，由调用方决定降级到 `synthesize()`；
        这里**不自动降级**，否则一次静默的性能回退可能几个月都发现不了。
        """
        try:
            from websockets.sync.client import connect
        except ImportError as e:      # pragma: no cover - 环境问题不进测试
            raise ProviderError(self.name,
                                "流式 TTS 需要 websockets>=13：pip install websockets") from e

        url = self._stream_tts_url(text, voice or self.tts_voice,
                                   f"vk{int(time.time()*1000)}", codec)
        try:
            with connect(url, open_timeout=10, close_timeout=5) as ws:
                while True:
                    msg = ws.recv()
                    if isinstance(msg, bytes):
                        if msg:
                            yield msg
                        continue
                    # 文本帧只有两种：出错，或者结束标记
                    d = json.loads(msg)
                    if d.get("code"):
                        raise ProviderError(
                            self.name, f"流式 TTS {d['code']}: {d.get('message')}")
                    if d.get("final"):
                        break
        except ProviderError:
            raise
        except Exception as e:
            raise ProviderError(self.name, f"流式 TTS 失败: {e}") from e

    # 跟 MimoProvider 的 stream() 同名同义，方便互换
    stream_synthesize = stream_tts

    def close(self) -> None:
        self._http.close()
