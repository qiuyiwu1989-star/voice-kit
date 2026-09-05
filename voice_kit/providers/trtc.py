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
              interrupt_mode: int = 0, session_id: Optional[str] = None) -> str:
        """起一个 AI 对话任务，返回 TaskId。

        `llm_api_url` 指向我们自己带闸门的端点——**这是整个方案的关键**，
        没有它，儿童安全就得交给一个不知道 8 铁律是什么的云服务。

        `interrupt_mode`：0=自动（服务端语义打断），1=客户端控制。
        儿童场景建议先用 0，孩子说话本来就断断续续，服务端的 VAD 比我们手搓的稳。

        ⚠️ **`session_id` 一定要传**（一般就用房间号）。2026-09-05 实测踩过：
        不传的话，同一个房间里可以起出多个互不知情的任务，每个都独立
        做一遍 ASR→LLM→TTS，于是**房间里有两个球球抢着说话，还给出不同的
        回答**。用自己在内存里记 task_id 的办法挡不住——重启就忘了，
        别的进程起的更是看不见。SessionId 是腾讯官方的去重口子，
        还能拿它反查任务（`describe(session_id=...)`），是唯一可靠的抓手。
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
        if session_id:
            payload["SessionId"] = session_id
        return self._call("StartAIConversation", payload)["TaskId"]

    def stop(self, task_id: str) -> None:
        """停任务。

        ⚠️ **只认 TaskId，没有按 SessionId 停的变体**（2026-09-05 实测：
        带 SdkAppId+SessionId 调过去，回的是 `MissingParameter: ... TaskId`）。
        而按 session 查回来的 TaskId 又是空的。

        两件事合起来意味着一条硬约束：**TaskId 丢了，任务就停不掉了**，
        只能等它自己 idle 到 MaxIdleTime——期间一直计费。
        所以调用方必须把 TaskId **持久化**（落盘/入库），
        放内存里一重启就成了孤儿。这是这个 API 的性质，不是实现问题。
        """
        self._call("StopAIConversation", {"TaskId": task_id})

    def describe(self, task_id: Optional[str] = None,
                 session_id: Optional[str] = None) -> dict:
        """查任务状态。按 session 查时 **SdkAppId 是必填的**（漏了会得到一句
        莫名其妙的 `InvalidParameter.TaskId: ciphertext less than 16:[]`）。

        ⚠️ 按 session 查回来的 `TaskId` 是**空字符串**（2026-09-05 实测）。
        所以它只能告诉你"这个房间里还有没有任务在跑"，
        **不能**拿来把它停掉——见 `stop()` 的注释。
        """
        if task_id:
            return self._call("DescribeAIConversation", {"TaskId": task_id})
        if session_id:
            return self._call("DescribeAIConversation",
                              {"SdkAppId": self.sdk_app_id, "SessionId": session_id})
        raise ValueError("describe 需要 task_id 或 session_id")

    def session_running(self, session_id: str) -> bool:
        """这个房间里还有任务在跑吗。"""
        try:
            d = self.describe(session_id=session_id)
        except ProviderError:
            return False
        return str(d.get("Status", "")).lower() in ("inprogress", "running", "preparing")

    def close(self) -> None:
        self._http.close()
