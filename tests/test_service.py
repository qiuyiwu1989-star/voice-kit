"""公共服务：多个项目共用一套能力，而护栏各归各的。

不联网——把 provider 全换成假的，测的是**编排和隔离**，不是供应商。
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from fastapi.testclient import TestClient

from voice_kit.tenants import hash_key

KID_KEY, ADULT_KEY = "kid-secret", "adult-secret"

TENANTS = {"tenants": [
    {"id": "qiuqiu", "name": "球球老师", "key_sha256": hash_key(KID_KEY),
     "system_prompt": "你是球球老师。", "voice_role": "qiuqiu",
     "output_rules": [{"name": "禁用病理化词汇", "words": ["抑郁"]},
                      {"name": "不卖焦虑", "words": ["风险"]}],
     "input_rules": [{"name": "self_harm", "words": ["不想活"]}],
     "fallback_reply": "我们再多聊一会儿吧。",
     "on_input_trigger_reply": "我听到了……你愿意多说一点吗？"},
    {"id": "coach", "name": "AI 教练", "key_sha256": hash_key(ADULT_KEY),
     "system_prompt": "你是教练。", "voice_role": "coach"},
]}


class FakeLLM:
    def __init__(self):
        self.reply = "这里有风险，我们慢慢聊。"
        self.calls = 0

    def stream(self, messages, **kw):
        self.calls += 1
        for i in range(0, len(self.reply), 3):
            yield self.reply[i:i + 3]


@pytest.fixture
def app(tmp_path, monkeypatch):
    p = tmp_path / "tenants.json"
    p.write_text(json.dumps(TENANTS, ensure_ascii=False), encoding="utf-8")
    for k, v in [("TOKENHUB_API_KEY", "x"), ("TC_SECRET_ID", "x"),
                 ("TC_SECRET_KEY", "x"), ("TENCENT_APPID", "1359505647"),
                 ("TRTC_SDKAPPID", "1400000000"), ("TRTC_SDKSECRETKEY", "k"),
                 ("VOICEKIT_PUBLIC_URL", "https://voice.test")]:
        monkeypatch.setenv(k, v)

    from voice_kit import service
    fake = FakeLLM()
    monkeypatch.setattr(service, "TokenHubLLM", lambda *a, **k: fake, raising=False)
    import voice_kit.providers.tokenhub as th
    monkeypatch.setattr(th, "TokenHubLLM", lambda *a, **k: fake)
    a = service.create_service(str(p))
    a.state.fake = fake
    return a


def sse_text(resp):
    out = ""
    for line in resp.text.split("\n"):
        line = line.strip()
        if not line.startswith("data:") or line[5:].strip() == "[DONE]":
            continue
        out += json.loads(line[5:])["choices"][0]["delta"].get("content") or ""
    return out


def chat(c, key, text, **kw):
    body = {"stream": True, "messages": [{"role": "user", "content": text}]}
    body.update(kw)
    return c.post("/v1/chat/completions", json=body,
                  headers={"Authorization": f"Bearer {key}"})


# ---------------------------------------------------------------- 隔离

def test_same_sentence_blocked_for_kid_allowed_for_adult(app):
    """**整套设计的核心承诺。** 同一个模型说同一句话，
    儿童项目拦下、成人项目放行——靠的是钥匙，不是代码分支。"""
    c = TestClient(app)
    kid = sse_text(chat(c, KID_KEY, "问题"))
    adult = sse_text(chat(c, ADULT_KEY, "问题"))

    # 真正的契约只有两条：违规词一个字都不能到达用户；兜底话要出现。
    assert "风险" not in kid, "违规词漏出去了——这是唯一不能接受的失败"
    assert "我们再多聊一会儿吧。" in kid
    assert "风险" in adult, "成人项目不该被儿童规则误伤"

    assert kid.endswith("我们再多聊一会儿吧。")


def test_violation_mid_sentence_never_leaks_half_a_clause(app):
    """**2026-09-07 线上抓到的真实缺陷，这条测试是它的墓碑。**

    公网实测里儿童端收到过：

        "失眠确实让人困扰，也容易引起家人的担忧。你妈妈提出" + "我们再多聊一会儿吧"
                                                    ↑ 从句子中间断掉

    闸门按字符放行，违规词出现在第二句中段时，"你妈妈提出"已经出去了。
    文字端只是难看；**语音端就是念出半句话再接一句不相干的**，
    而且说出口收不回——这正是儿童场景最不能接受的那类破绽。

    修法是按整句放行：违规的那一句整句不发，前面干净的整句照常发。
    """
    c = TestClient(app)
    app.state.fake.reply = "失眠确实让人困扰。你妈妈说的那个风险，我们慢慢聊。"
    kid = sse_text(chat(c, KID_KEY, "睡不着"))

    assert "风险" not in kid
    # 干净的**整句**要留下——不能因为后面有违规就把整段吞掉
    assert "失眠确实让人困扰。" in kid
    # 违规那句的任何片段都不许漏，尤其是它的开头
    assert "你妈妈说的" not in kid, "半句话漏出去了——语音里就是念半句再换话题"
    assert kid == "失眠确实让人困扰。我们再多聊一会儿吧。"


def test_text_without_any_punctuation_still_gets_through(app):
    """按整句放行不能变成"没有句号就永远不发"——那是死锁。"""
    c = TestClient(app)
    app.state.fake.reply = "好的没问题"
    assert sse_text(chat(c, KID_KEY, "嗯")) == "好的没问题"


def test_wrong_key_is_401(app):
    c = TestClient(app)
    assert chat(c, "nope", "你好").status_code == 401
    assert c.post("/v1/chat/completions", json={"messages": []}).status_code == 401


def test_me_reports_the_right_config(app):
    """接入时第一个该调的接口——比对着文档猜快得多。"""
    c = TestClient(app)
    kid = c.get("/v1/me", headers={"Authorization": f"Bearer {KID_KEY}"}).json()
    adult = c.get("/v1/me", headers={"Authorization": f"Bearer {ADULT_KEY}"}).json()
    assert kid["id"] == "qiuqiu" and kid["output_rules"] == ["禁用病理化词汇", "不卖焦虑"]
    assert adult["id"] == "coach" and adult["output_rules"] == []


# ---------------------------------------------------------------- 闸门

def test_red_line_takes_over_without_calling_llm(app):
    """最危险的一刻不给模型发挥空间。"""
    c = TestClient(app)
    before = app.state.fake.calls
    got = sse_text(chat(c, KID_KEY, "我不想活了"))
    assert got == "我听到了……你愿意多说一点吗？"
    assert app.state.fake.calls == before, "红线命中时绝不能调 LLM"


def test_adult_has_no_input_gate(app):
    c = TestClient(app)
    before = app.state.fake.calls
    sse_text(chat(c, ADULT_KEY, "我不想活了"))
    assert app.state.fake.calls == before + 1, "成人项目不该被儿童红线拦住"


# ---------------------------------------------------------------- 协议形状

def test_response_is_openai_shaped(app):
    """长成 OpenAI 的样子，别人拿现成 SDK 指过来就能用——
    这是"接入成本接近零"的全部理由。"""
    c = TestClient(app)
    r = chat(c, ADULT_KEY, "你好")
    first = json.loads(r.text.split("\n\n")[0][5:])
    assert first["object"] == "chat.completion.chunk"
    assert first["id"].startswith("chatcmpl-")
    assert "delta" in first["choices"][0]
    assert r.text.rstrip().endswith("[DONE]")


def test_non_stream_returns_a_message(app):
    """文字类项目（个人站的 agent）会用非流式。"""
    c = TestClient(app)
    r = chat(c, ADULT_KEY, "你好", stream=False)
    d = r.json()
    assert d["object"] == "chat.completion"
    assert "风险" in d["choices"][0]["message"]["content"]


# ---------------------------------------------------------------- 音色

def test_voices_are_per_role_and_curated(app):
    c = TestClient(app)
    kid = c.get("/v1/voices", headers={"Authorization": f"Bearer {KID_KEY}"}).json()
    adult = c.get("/v1/voices", headers={"Authorization": f"Bearer {ADULT_KEY}"}).json()
    assert kid["role"] == "qiuqiu" and adult["role"] == "coach"
    assert kid["default"] != adult["default"], "两个角色不该是同一个默认音色"
    assert 0 < len(kid["options"]) < 21, "只给候选，不给全部"


# ---------------------------------------------------------------- 运维

def test_healthz_is_open(app):
    """监控要用，不能要钥匙。"""
    assert TestClient(app).get("/healthz").json()["ok"] is True


def test_new_tenant_without_restart(app, tmp_path):
    """加项目 = 改配置文件。这是"公共能力"和"探针"的分界线之一。"""
    import os
    c = TestClient(app)
    assert chat(c, "third-key", "你好").status_code == 401
    p = tmp_path / "tenants.json"
    data = json.loads(p.read_text(encoding="utf-8"))
    data["tenants"].append({"id": "site", "name": "个人站",
                            "key_sha256": hash_key("third-key")})
    p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    os.utime(p, (0, 0))
    assert chat(c, "third-key", "你好").status_code == 200
