"""UserSig 签名测试（不联网）。

签错的代价特别高：客户端进房只会返回一个 -3319 之类的数字，
既不告诉你是密钥错了、顺序错了，还是 base64 变体错了。把规则钉进测试。
"""
import base64
import hashlib
import hmac
import json
import sys
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from voice_kit.providers.trtc import (TRTCConversation, decode_user_sig,
                                      make_user_sig)

APPID = 1400000000
KEY = "sdksecretkeyfortest"
UID = "child-abc"
NOW = 1757000000


def sig(**kw):
    return make_user_sig(APPID, KEY, UID, _now=NOW, **kw)


def test_roundtrip_decodes_to_expected_doc():
    d = decode_user_sig(sig())
    assert d["TLS.ver"] == "2.0"
    assert d["TLS.identifier"] == UID
    assert d["TLS.sdkappid"] == APPID
    assert d["TLS.time"] == NOW
    assert d["TLS.expire"] == 86400


def test_signature_content_is_exact():
    """签名对象是四行固定顺序的 key:value，顺序错了服务端就不认。"""
    d = decode_user_sig(sig())
    content = (f"TLS.identifier:{UID}\n"
               f"TLS.sdkappid:{APPID}\n"
               f"TLS.time:{NOW}\n"
               f"TLS.expire:86400\n")
    expect = base64.b64encode(
        hmac.new(KEY.encode(), content.encode(), hashlib.sha256).digest()).decode()
    assert d["TLS.sig"] == expect

    # 反证：换行顺序调一下必须签不出同一个值，证明上面不是恒真
    wrong = (f"TLS.sdkappid:{APPID}\n"
             f"TLS.identifier:{UID}\n"
             f"TLS.time:{NOW}\n"
             f"TLS.expire:86400\n")
    assert d["TLS.sig"] != base64.b64encode(
        hmac.new(KEY.encode(), wrong.encode(), hashlib.sha256).digest()).decode()


def test_uses_base64_variant_not_standard():
    """TLSSigAPIv2 把 + / = 换成 * - _。用标准 base64 服务端解不开。"""
    s = sig()
    assert not set("+/=") & set(s), f"出现了标准 base64 字符: {s[:60]}"


def test_is_zlib_compressed():
    """不是裸 JSON——先 zlib 再 base64，顺序反了也解不开。"""
    raw = base64.b64decode(sig().translate(str.maketrans("*-_", "+/=")))
    assert raw[:2] == b"\x78\x9c", "不是 zlib 流"
    assert json.loads(zlib.decompress(raw))["TLS.identifier"] == UID


def test_different_user_different_sig():
    a = make_user_sig(APPID, KEY, "child-a", _now=NOW)
    b = make_user_sig(APPID, KEY, "child-b", _now=NOW)
    assert a != b


def test_wrong_key_changes_sig():
    """这条是在说：SDKSecretKey 就是身份本身。谁有它谁能冒充任何孩子。"""
    a = make_user_sig(APPID, KEY, UID, _now=NOW)
    b = make_user_sig(APPID, "another-key", UID, _now=NOW)
    assert decode_user_sig(a)["TLS.sig"] != decode_user_sig(b)["TLS.sig"]


# ---------------------------------------------------------------- 构造检查

def test_requires_all_four_credentials():
    """SDKSecretKey 和云 API 的 SecretKey 是两个东西，漏一个就该当场报错，
    而不是等到进房失败拿一个数字错误码。"""
    with pytest.raises(ValueError, match="SDKSecretKey"):
        TRTCConversation("sid", "skey", 1400000000, "")


def test_start_payload_points_llm_at_us(monkeypatch):
    """整个方案的关键：LLMConfig.APIUrl 必须指向我们自己带闸门的端点。
    这条断言在的意义是——以后有人"顺手"换成直连某个云模型时，测试会红。"""
    c = TRTCConversation("sid", "skey", APPID, KEY)
    seen = {}

    def fake_call(action, payload):
        seen["action"], seen["payload"] = action, payload
        return {"TaskId": "task-1"}

    monkeypatch.setattr(c, "_call", fake_call)
    tid = c.start("room-1", llm_api_url="https://ours/v1/chat/completions",
                  llm_api_key="k", target_user_id="child-abc",
                  system_prompt="你是球球老师。")

    assert tid == "task-1"
    assert seen["action"] == "StartAIConversation"
    llm = json.loads(seen["payload"]["LLMConfig"])
    assert llm["APIUrl"] == "https://ours/v1/chat/completions"
    assert llm["LLMType"] == "openai"
    assert llm["Streaming"] is True, "语音场景没有非流式这个选项"
    assert seen["payload"]["AgentConfig"]["TargetUserId"] == "child-abc"
    # 机器人自己的 UserSig 必须是签好的，不是空的
    assert decode_user_sig(seen["payload"]["AgentConfig"]["UserSig"])["TLS.sdkappid"] == APPID


def test_session_id_is_sent_when_given(monkeypatch):
    """SessionId 是腾讯的去重口子。不传它，同一个房间能起出多个互不知情的
    任务——真踩过：两个球球各自 ASR→LLM→TTS，同一句话回答两遍还不一样。"""
    c = TRTCConversation("sid", "skey", APPID, KEY)
    seen = {}
    monkeypatch.setattr(c, "_call", lambda a, p: (seen.update(p), {"TaskId": "t"})[1])
    c.start("room-1", llm_api_url="u", llm_api_key="k",
            target_user_id="child", session_id="room-1")
    assert seen["SessionId"] == "room-1"


def test_bot_user_id_must_not_be_derived_from_room(monkeypatch):
    """机器人 userId 按房间号写死的话，同房间重试必撞 TaskExist。
    这条钉住"调用方要给一个每次都不同的 bot id"这个约定。"""
    c = TRTCConversation("sid", "skey", APPID, KEY)
    ids = []
    monkeypatch.setattr(c, "_call",
                        lambda a, p: (ids.append(p["AgentConfig"]["UserId"]), {"TaskId": "t"})[1])
    for bot in ("bot-aaa", "bot-bbb"):
        c.start("same-room", llm_api_url="u", llm_api_key="k",
                target_user_id="child", bot_user_id=bot)
    assert ids[0] != ids[1]


def test_describe_by_session_includes_sdkappid(monkeypatch):
    """按 session 查时 SdkAppId 是必填的。漏了会收到一句毫无线索的
    `InvalidParameter.TaskId: ciphertext less than 16:[]`——实测过。"""
    c = TRTCConversation("sid", "skey", APPID, KEY)
    seen = {}
    monkeypatch.setattr(c, "_call", lambda a, p: (seen.update(p), {"Status": "InProgress"})[1])
    c.describe(session_id="room-1")
    assert seen == {"SdkAppId": APPID, "SessionId": "room-1"}


def test_session_running_reads_status(monkeypatch):
    c = TRTCConversation("sid", "skey", APPID, KEY)
    monkeypatch.setattr(c, "_call", lambda a, p: {"Status": "InProgress", "TaskId": ""})
    assert c.session_running("room-1") is True
    monkeypatch.setattr(c, "_call", lambda a, p: {"Status": "Stopped", "TaskId": ""})
    assert c.session_running("room-1") is False
