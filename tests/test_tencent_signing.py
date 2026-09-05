"""腾讯 provider 的签名测试（不联网）。

为什么单独测签名：接入腾讯实时语音识别时最容易卡住的就是这里——
它用的是 **HMAC-SHA1 + base64**，跟云 API 3.0 的 TC3-HMAC-SHA256 完全是两套。
参数漏排序、路径带了协议头、signature 没 urlencode，都会得到一个
毫无线索的 401。把规则钉进测试，下次换人接不用再踩一遍。
"""
import base64
import hashlib
import hmac
import re
import sys
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from voice_kit.providers.tencent import TencentProvider, _tc3_headers

P = dict(secret_id="AKIDtesttesttesttest", secret_key="secretkeyfortest", app_id="1300000000")


def _provider():
    return TencentProvider(**P)


# ---------------------------------------------------------------- 实时 ASR 签名

def test_realtime_url_shape():
    url = _provider()._realtime_url("voice-1")
    assert url.startswith("wss://asr.cloud.tencent.com/asr/v2/1300000000?")
    assert "signature=" in url
    for must in ("secretid=", "timestamp=", "expired=", "nonce=", "engine_model_type=", "voice_id=voice-1"):
        assert must in url, f"缺参数: {must}"


def test_query_params_are_sorted():
    """参数必须字典序——签名对象和实际 URL 的顺序必须一致，否则 401。"""
    url = _provider()._realtime_url("v")
    qs = url.split("?", 1)[1].rsplit("&signature=", 1)[0]
    keys = [kv.split("=")[0] for kv in qs.split("&")]
    assert keys == sorted(keys), f"query 没排序: {keys}"


def test_signature_is_hmac_sha1_over_path_and_query():
    """签名对象是「去掉协议头的完整 URL」，算法是 HMAC-SHA1 + base64。
    这条把两处最容易搞错的地方钉死：不是 SHA256，不带 wss://。"""
    p = _provider()
    url = p._realtime_url("v")
    body, sig_enc = url.split("&signature=")
    sig = urllib.parse.unquote(sig_enc)
    signed_target = body[len("wss://"):]          # 去掉协议头
    expect = base64.b64encode(
        hmac.new(P["secret_key"].encode(), signed_target.encode(), hashlib.sha1).digest()
    ).decode()
    assert sig == expect, "签名算法或签名对象不对"
    # 反证：换成 SHA256 必须对不上，证明上面不是恒真
    wrong = base64.b64encode(
        hmac.new(P["secret_key"].encode(), signed_target.encode(), hashlib.sha256).digest()
    ).decode()
    assert sig != wrong


def test_signature_is_url_encoded():
    """base64 会产生 + / =，不转义就会在 query 里被解析错。"""
    url = _provider()._realtime_url("v")
    raw_sig = url.split("&signature=")[1]
    assert "+" not in raw_sig, "signature 里的 + 没转义"


def test_nonce_and_voice_id_vary():
    a = _provider()._realtime_url("v1")
    b = _provider()._realtime_url("v2")
    assert a != b
    assert "voice_id=v1" in a and "voice_id=v2" in b


# ---------------------------------------------------------------- 流式 TTS 签名

TTS_TEXT = "你好，我是球球"


def _tts_url(text=TTS_TEXT):
    return _provider()._stream_tts_url(text, 101016, "sess-1", "pcm")


def test_stream_tts_url_shape():
    url = _tts_url()
    assert url.startswith("wss://tts.cloud.tencent.com/stream_ws?")
    for must in ("Action=TextToStreamAudioWS", "AppId=1300000000",
                 "SessionId=sess-1", "VoiceType=101016", "Codec=pcm", "Signature="):
        assert must in url, f"缺参数: {must}"


def test_stream_tts_signature_has_get_prefix():
    """TTS 的签名串比 ASR 多一个 "GET" 前缀——最容易漏的三个字母。"""
    url = _tts_url()
    sig = urllib.parse.unquote(url.split("&Signature=")[1])
    qs = url[len("wss://tts.cloud.tencent.com/stream_ws?"):].rsplit("&Signature=", 1)[0]
    raw_qs = "&".join(f"{k}={urllib.parse.unquote(v)}"
                      for k, v in (p.split("=", 1) for p in qs.split("&")))

    def sign(target):
        return base64.b64encode(
            hmac.new(P["secret_key"].encode(), target.encode(), hashlib.sha1).digest()).decode()

    assert sig == sign(f"GETtts.cloud.tencent.com/stream_ws?{raw_qs}")
    # 反证：漏掉 GET 前缀必须签不出同一个值，否则上面那条是恒真的
    assert sig != sign(f"tts.cloud.tencent.com/stream_ws?{raw_qs}")


def test_stream_tts_signs_raw_text_not_encoded():
    """签名用原始中文，URL 里才编码。先编码再签会 401——这是第二个常见坑。"""
    url = _tts_url()
    assert "Text=%E4%BD%A0%E5%A5%BD" in url, "URL 里的中文应该被编码"
    sig = urllib.parse.unquote(url.split("&Signature=")[1])
    params = {"Action": "TextToStreamAudioWS", "AppId": P["app_id"], "Codec": "pcm",
              "SampleRate": "16000", "SecretId": P["secret_id"], "SessionId": "sess-1",
              "Speed": "0", "Text": TTS_TEXT, "VoiceType": "101016", "Volume": "0"}
    ts = dict(p.split("=", 1) for p in
              url.split("?", 1)[1].rsplit("&Signature=", 1)[0].split("&"))
    params["Timestamp"], params["Expired"] = ts["Timestamp"], ts["Expired"]
    qs = "&".join(f"{k}={params[k]}" for k in sorted(params))
    expect = base64.b64encode(
        hmac.new(P["secret_key"].encode(),
                 f"GETtts.cloud.tencent.com/stream_ws?{qs}".encode(), hashlib.sha1).digest()).decode()
    assert sig == expect, "签名不是对原始（未编码）参数值算的"


def test_stream_tts_signature_is_url_encoded():
    assert "+" not in _tts_url().split("&Signature=")[1], "Signature 里的 + 没转义"


def test_stream_tts_params_sorted():
    qs = _tts_url().split("?", 1)[1].rsplit("&Signature=", 1)[0]
    keys = [p.split("=")[0] for p in qs.split("&")]
    assert keys == sorted(keys), "参数没按字典序，签名对象和 URL 会对不上"


def test_one_shot_synthesize_still_exists():
    """流式连不上时要能降级——一次性接口不能因为有了流式就删掉。"""
    assert callable(getattr(_provider(), "synthesize", None))


# ---------------------------------------------------------------- 云 API 3.0 签名

def test_tc3_headers_shape():
    h = _tc3_headers(P["secret_id"], P["secret_key"], "asr.tencentcloudapi.com",
                     "asr", "SentenceRecognition", "2019-06-14", "{}")
    assert h["Authorization"].startswith("TC3-HMAC-SHA256 Credential=")
    assert "SignedHeaders=content-type;host;x-tc-action" in h["Authorization"]
    assert re.search(r"Signature=[0-9a-f]{64}$", h["Authorization"]), "签名应是 64 位十六进制"
    assert h["X-TC-Action"] == "SentenceRecognition"


def test_tc3_signature_depends_on_body():
    """body 变了签名必须变——否则说明 body 根本没进签名。"""
    args = (P["secret_id"], P["secret_key"], "asr.tencentcloudapi.com",
            "asr", "SentenceRecognition", "2019-06-14")
    a = _tc3_headers(*args, '{"a":1}')["Authorization"]
    b = _tc3_headers(*args, '{"a":2}')["Authorization"]
    assert a != b


# ---------------------------------------------------------------- 契约

def test_declares_streaming_support():
    """这是引入腾讯的**唯一**理由，必须显式可查询。"""
    assert _provider().supports_streaming is True


def test_requires_app_id():
    """AppId 是实时 WebSocket 必需的，缺了要立刻报错而不是运行时 401。"""
    import pytest
    with pytest.raises(ValueError):
        TencentProvider("sid", "skey", "")
