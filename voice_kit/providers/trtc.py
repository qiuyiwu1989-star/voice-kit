"""腾讯 TRTC 对话式 AI：起/停 AI 对话任务 + 签 UserSig。

## 这一层在整个方案里的位置

TRTC 负责 **传输 + 编排**（VAD / 语义打断 / 句级流式合成 / WebRTC 抖动缓冲），
我们负责 **安全**（`openai_server.py` 那个带闸门的 `/v1/chat/completions`）。
分界线就是 `LLMConfig.APIUrl`——它指向我们，所以每个 token 都还过我们的闸门。

## 三套完全不同的密钥，别搞混（接入卡壳基本都卡在这）

| 用途 | 密钥 | 算法 |
|---|---|---|
| 云 API（Start/Stop 任务） | SecretId + SecretKey | TC3-HMAC-SHA256 |
| UserSig（进房身份） | **SDKSecretKey** | HMAC-SHA256 → zlib → base64 变体 |
| 实时 ASR/TTS WebSocket | SecretKey | HMAC-SHA1（见 tencent.py） |

第二个和第一个不是同一个 key——SDKSecretKey 在 TRTC 控制台的应用里，
它能签出**任意 userid** 的身份，泄露等于任何人都能冒充任何孩子进房间。

## 实测（2026-09-05，SdkAppId 1600160922）

    DescribeAIConversation(假TaskId) → InvalidParameter.TaskId
        不是 AuthFailure 也不是"未开通" → 鉴权和服务开通都过了
    StartAIConversation → TaskId ✅ → StopAIConversation ✅
        **不需要预先买 AI 智能语音套餐（475元/月）**，按量即可先验证
"""
import base64
import datetime
import hashlib
import hmac
import json
import time
import zlib
from typing import Optional

import httpx

from .base import ProviderError

_HOST = "trtc.tencentcloudapi.com"
_SERVICE = "trtc"
_VERSION = "2019-07-22"

# TLSSigAPIv2 用的 base64 变体：+ / = 分别换成 * - _
_B64_TRANS = str.maketrans("+/=", "*-_")


def make_user_sig(sdk_app_id: int, sdk_secret_key: str, user_id: str,
                  expire: int = 86400, *, _now: Optional[int] = None) -> str:
    """签 UserSig（TLSSigAPIv2）。

    顺序不能变：先 HMAC-SHA256 出 sig，塞进 JSON，再 zlib 压缩，最后 base64 变体。
    少一步或换个顺序，客户端进房时只会得到一个 -3319 之类的数字，毫无线索。
    """
    now = int(time.time()) if _now is None else _now
    content = (f"TLS.identifier:{user_id}\n"
               f"TLS.sdkappid:{sdk_app_id}\n"
               f"TLS.time:{now}\n"
               f"TLS.expire:{expire}\n")
    sig = base64.b64encode(
        hmac.new(sdk_secret_key.encode(), content.encode(), hashlib.sha256).digest()
    ).decode()
    doc = {"TLS.ver": "2.0", "TLS.identifier": user_id, "TLS.sdkappid": sdk_app_id,
           "TLS.expire": expire, "TLS.time": now, "TLS.sig": sig}
    packed = zlib.compress(json.dumps(doc).encode())
    return base64.b64encode(packed).decode().translate(_B64_TRANS)


def decode_user_sig(sig: str) -> dict:
    """把 UserSig 解回来——只用于测试和排障，生产别调。"""
    raw = base64.b64decode(sig.translate(str.maketrans("*-_", "+/=")))
    return json.loads(zlib.decompress(raw))


class TRTCConversation:
    """起/停 AI 对话任务。"""

    name = "trtc"

    def __init__(self, secret_id: str, secret_key: str, sdk_app_id: int,
                 sdk_secret_key: str, *, region: str = "ap-guangzhou",
                 timeout: float = 30.0):
        if not (secret_id and secret_key and sdk_app_id and sdk_secret_key):
            raise ValueError("TRTCConversation 需要 secret_id / secret_key / "
                             "sdk_app_id / sdk_secret_key（后者是 SDKSecretKey，"
                             "跟云 API 的 SecretKey 不是同一个）")
        self.secret_id = secret_id
        self.secret_key = secret_key
        self.sdk_app_id = int(sdk_app_id)
        self.sdk_secret_key = sdk_secret_key
        self.region = region
        self._http = httpx.Client(timeout=httpx.Timeout(timeout, connect=10.0))

    # ---------------------------------------------------------------- 云 API

    def _call(self, action: str, payload: dict) -> dict:
        body = json.dumps(payload)
        ts = int(time.time())
        date = datetime.datetime.fromtimestamp(
            ts, datetime.timezone.utc).strftime("%Y-%m-%d")
        ct = "application/json; charset=utf-8"
        canonical = "\n".join([
            "POST", "/", "",
            f"content-type:{ct}\nhost:{_HOST}\nx-tc-action:{action.lower()}\n",
            "content-type;host;x-tc-action",
            hashlib.sha256(body.encode()).hexdigest()])
        scope = f"{date}/{_SERVICE}/tc3_request"
        to_sign = "\n".join(["TC3-HMAC-SHA256", str(ts), scope,
                             hashlib.sha256(canonical.encode()).hexdigest()])
        k = hmac.new(("TC3" + self.secret_key).encode(), date.encode(), hashlib.sha256).digest()
        k = hmac.new(k, _SERVICE.encode(), hashlib.sha256).digest()
        k = hmac.new(k, b"tc3_request", hashlib.sha256).digest()
        sig = hmac.new(k, to_sign.encode(), hashlib.sha256).hexdigest()
        r = self._http.post(f"https://{_HOST}/", content=body, headers={
            "Content-Type": ct, "Host": _HOST, "X-TC-Action": action,
            "X-TC-Version": _VERSION, "X-TC-Timestamp": str(ts),
            "X-TC-Region": self.region,
            "Authorization": (f"TC3-HMAC-SHA256 Credential={self.secret_id}/{scope}, "
                              f"SignedHeaders=content-type;host;x-tc-action, "
                              f"Signature={sig}")})
        d = r.json().get("Response", {})
        if "Error" in d:
            e = d["Error"]
            raise ProviderError(self.name, f"{action} {e.get('Code')}: {e.get('Message')}")
        return d

    # ---------------------------------------------------------------- 任务

    def user_sig(self, user_id: str, expire: int = 86400) -> str:
        return make_user_sig(self.sdk_app_id, self.sdk_secret_key, user_id, expire)

    def start(self, room_id: str, *, llm_api_url: str, llm_api_key: str,
              llm_model: str = "voice-kit", system_prompt: str = "",
              target_user_id: str, bot_user_id: str = "qiuqiu-bot",
              welcome_message: str = "", tts_voice: int = 101016,
              tts_secret_id: Optional[str] = None,
              tts_secret_key: Optional[str] = None,
              language: str = "zh", max_idle_time: int = 60,
              interrupt_mode: int = 0) -> str:
        """起一个 AI 对话任务，返回 TaskId。

        `llm_api_url` 指向我们自己带闸门的端点——**这是整个方案的关键**，
        没有它，儿童安全就得交给一个不知道 8 铁律是什么的云服务。

        `interrupt_mode`：0=自动（服务端语义打断），1=客户端控制。
        儿童场景建议先用 0，孩子说话本来就断断续续，服务端的 VAD 比我们手搓的稳。
        """
        payload = {
            "SdkAppId": self.sdk_app_id,
            "RoomId": room_id,
            "RoomIdType": 1,                      # 1 = 字符串房间号
            "AgentConfig": {
                "UserId": bot_user_id,
                "UserSig": self.user_sig(bot_user_id),
                "TargetUserId": target_user_id,
                "MaxIdleTime": max_idle_time,
                "WelcomeMessage": welcome_message,
                "InterruptMode": interrupt_mode,
            },
            "STTConfig": {"Language": language},
            "LLMConfig": json.dumps({
                "LLMType": "openai",
                "Model": llm_model,
                "APIKey": llm_api_key,
                "APIUrl": llm_api_url,
                "Streaming": True,                # 语音场景没有第二个选项
                "SystemPrompt": system_prompt,
            }),
            "TTSConfig": json.dumps({
                "TTSType": "tencent",
                "AppId": str(self.sdk_app_id),
                "SecretId": tts_secret_id or self.secret_id,
                "SecretKey": tts_secret_key or self.secret_key,
                "VoiceType": tts_voice,
            }),
        }
        return self._call("StartAIConversation", payload)["TaskId"]

    def stop(self, task_id: str) -> None:
        self._call("StopAIConversation", {"TaskId": task_id})

    def describe(self, task_id: str) -> dict:
        return self._call("DescribeAIConversation", {"TaskId": task_id})

    def close(self) -> None:
        self._http.close()
