"""WAV 头的小工具。零依赖——**别在这里 import 任何 provider**。

2026-09-14 线上事故：tencent.transcribe 从 mimo 借 wav_duration_ms，而 mimo 一 import 就要
tenacity，服务器 venv 没装 → 每次识别 500。识别路径不该因为一个没用到的供应商模块挂掉。
"""
import struct
from typing import Optional

WAV_HEADER_LEN = 44


def is_wav(audio: bytes) -> bool:
    return len(audio) >= WAV_HEADER_LEN and audio[:4] == b"RIFF" and audio[8:12] == b"WAVE"


def wav_duration_ms(audio: bytes) -> Optional[float]:
    """从 WAV 头算真实时长。只记字节数不够——不同采样率算出来的秒数天差地别。"""
    try:
        if not is_wav(audio):
            return None
        channels, rate = struct.unpack_from("<HI", audio, 22)
        bits = struct.unpack_from("<H", audio, 34)[0]
        per_sec = rate * channels * max(1, bits // 8)
        return round((len(audio) - WAV_HEADER_LEN) / per_sec * 1000, 1) if per_sec else None
    except Exception:
        return None
