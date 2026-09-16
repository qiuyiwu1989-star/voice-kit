"""wav 工具 + 识别路径不依赖 mimo/tenacity。"""
import builtins
import importlib
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from voice_kit.providers.wav import wav_duration_ms, is_wav


def _wav(seconds=0.5, rate=16000):
    n = int(rate * seconds); pcm = b"\x00\x00" * n
    return (b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVEfmt " +
            struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16) + b"data" + struct.pack("<I", len(pcm)) + pcm)


def test_wav_duration():
    assert is_wav(_wav()) and wav_duration_ms(_wav(0.5)) == 500.0
    assert wav_duration_ms(_wav(1.0, 24000)) == 1000.0
    assert wav_duration_ms(b"not a wav") is None and wav_duration_ms(b"") is None


def test_tencent_transcribe_survives_without_tenacity(monkeypatch):
    """服务器 venv 没有 tenacity（2026-09-14 线上 500）。识别路径不许碰 mimo。"""
    real_import = builtins.__import__
    def no_tenacity(name, *a, **k):
        if name == "tenacity" or name.startswith("tenacity."):
            raise ModuleNotFoundError("No module named 'tenacity'")
        return real_import(name, *a, **k)
    monkeypatch.setattr(builtins, "__import__", no_tenacity)
    for m in list(sys.modules):
        if m.startswith("voice_kit.providers.mimo"):
            monkeypatch.delitem(sys.modules, m)
    from voice_kit.providers.tencent import TencentProvider
    tp = TencentProvider("id", "key", 1)
    monkeypatch.setattr(tp, "_call", lambda *a, **k: {"Result": "你好"})
    r = tp.transcribe(_wav(0.5))
    assert r.text == "你好" and r.duration_ms == 500.0
