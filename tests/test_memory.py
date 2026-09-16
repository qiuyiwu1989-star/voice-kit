"""用户侧记忆库 + 记录方案 + 代码级 agent 本地试运行。"""
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from fastapi.testclient import TestClient

from voice_kit import agents as A
from voice_kit import memory as M
from voice_kit.tenants import hash_key
from urllib.parse import quote
UID_H = {"X-User-Id": quote("小邱")}

REPO = Path(__file__).parent.parent


@pytest.fixture
def root(tmp_path):
    dst = tmp_path / "agents"; shutil.copytree(REPO / "agents", dst); return dst


class ScriptedLLM:
    def __init__(self): self.reply = "好的，您慢慢说。"; self.systems = []; self.extract = '[{"category":"constraint","content":"预算每学期三千以内","quote":"三千以内"},{"category":"bad","content":"x"}]'
    def stream(self, messages, **kw):
        sysm = kw.get("system") or ""; self.systems.append(sysm)
        r = self.extract if "事实抽取器" in sysm else ("按时间：第一场……" if "多场综合复盘" in sysm else self.reply)
        for i in range(0, len(r), 5): yield r[i:i + 5]


@pytest.fixture
def app(root, tmp_path, monkeypatch):
    tp = tmp_path / "t.json"; tp.write_text(json.dumps({"tenants": []}), encoding="utf-8")
    for k, v in [("TOKENHUB_API_KEY", "x"), ("TC_SECRET_ID", "x"), ("TC_SECRET_KEY", "x"), ("TENCENT_APPID", "1"),
                 ("TRTC_SDKAPPID", "1"), ("TRTC_SDKSECRETKEY", "k"), ("VOICEKIT_PUBLIC_URL", "https://voice.test"),
                 ("VOICEKIT_AGENTS_DIR", str(root)), ("VOICEKIT_ADMIN_TOKEN", "door"), ("VOICEKIT_UI_TOKEN", "go")]:
        monkeypatch.setenv(k, v)
    from voice_kit import service
    import voice_kit.providers.tokenhub as th
    fake = ScriptedLLM()
    monkeypatch.setattr(service, "TokenHubLLM", lambda *a, **k: fake, raising=False)
    monkeypatch.setattr(th, "TokenHubLLM", lambda *a, **k: fake)
    a = service.create_service(str(tp)); a.state.fake = fake; return a


def sse_text(r):
    return "".join(json.loads(l[5:])["choices"][0]["delta"].get("content") or "" for l in r.text.split("\n")
                   if l.strip().startswith("data:") and l.strip()[5:].strip() != "[DONE]")


# ---------------------------------------------------------------- 纯函数

def test_facts_merge_dedupes_and_counts(root):
    M.merge_facts(root, "talk", "小邱", [{"category": "constraint", "content": "预算三千", "quote": "三千"}], source_session="s1")
    M.merge_facts(root, "talk", "小邱", [{"category": "constraint", "content": "预算三千"}, {"category": "nope", "content": "x"}], source_session="s2")
    f = M.load_facts(root, "talk", "小邱")
    assert len(f) == 1 and f[0]["seen"] == 2 and f[0]["quote"] == "三千"
    assert M.safe_uid(" 小邱/../x ") == "小邱x"


def test_briefing_follows_lens(root):
    M.merge_facts(root, "talk", "u", [{"category": "constraint", "content": "预算三千"}, {"category": "relation", "content": "孩子上五年级"}])
    M.save_session(root, "talk", "u", {"session_id": "s1", "agent": "kefu-xiaoan", "at": "2026-09-14T10:00:00", "turns": [], "review": "上次：先接情绪。"})
    M.save_judgment(root, "talk", "u", "kefu-xiaoan", {"at": "x", "session_id": "s1", "text": "他要的是被听见。"})
    lens = {"read": {"facts": ["constraint"], "recap": "last_session", "judgments": "own"}}
    b = M.briefing(root, "talk", "u", "kefu-xiaoan", lens)
    assert "预算三千" in b and "孩子上五年级" not in b          # 透镜只读 constraint
    assert "上次" in b and "先接情绪" in b and "被听见" in b
    assert M.briefing(root, "talk", "nobody", "kefu-xiaoan", lens) == ""
    b2 = M.briefing(root, "talk", "u", "guwen-axun", {"read": {"facts": [], "recap": "none", "judgments": "own"}})
    assert "被听见" not in b2                                   # 判断按 agent 分片


# ---------------------------------------------------------------- 端到端

def test_review_writes_user_store_and_next_chat_gets_briefing(app, root):
    c = TestClient(app, cookies={"vk_ui": "go"}, headers=UID_H)
    turns = [{"t": "0:02", "role": "user", "text": "我们预算三千以内"}, {"t": "0:05", "role": "bot", "text": "好。", "first_ms": 1000}]
    r = c.post("/v1/agents/kefu-xiaoan/review", json={"session_id": "s1", "turns": turns})
    assert r.status_code == 200 and r.json()["uid"] == "小邱" and r.json()["facts"] == 1
    assert r.json()["product"] == "talk" and (root / "users" / "talk" / "小邱" / "facts.json").exists()
    facts = M.load_facts(root, "talk", "小邱"); assert facts[0]["content"] == "预算每学期三千以内"      # 非法类别被丢
    assert M.load_judgments(root, "talk", "小邱")["kefu-xiaoan"][0]["text"]
    assert M.session_summaries(root, "talk", "小邱")[0]["agent_name"] == "小安 · 安抚型客服"
    # 下一场：速览卡进 system prompt
    r = c.post("/v1/agents/kefu-xiaoan/chat/completions", json={"stream": True, "messages": [{"role": "user", "content": "又是我"}]})
    assert r.status_code == 200
    assert "预算每学期三千以内" in app.state.fake.systems[-1] and "你们聊过 1 次" in app.state.fake.systems[-1]
    # 没报名字就没有记忆，也不报错
    r = TestClient(app, cookies={"vk_ui": "go"}).post("/v1/agents/kefu-xiaoan/chat/completions", json={"stream": True, "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200 and "你记得的事实" not in app.state.fake.systems[-1]


def test_my_memory_and_multi_review(app, root):
    c = TestClient(app, cookies={"vk_ui": "go"}, headers=UID_H)
    for i in range(2):
        c.post("/v1/agents/chengjiao-dacheng/review", json={"session_id": f"s{i}", "turns": [{"t": "0:01", "role": "user", "text": f"第{i}场"}]})
    m = c.get("/v1/users/me/memory").json()
    assert m["uid"] == "小邱" and len(m["sessions"]) == 2 and "timeline" in m["methods"]
    r = c.post("/v1/users/me/review", json={"method": "timeline"})
    assert r.status_code == 200 and "按时间" in r.json()["text"] and len(r.json()["session_ids"]) == 2
    r = c.post("/v1/users/me/review", json={"method": "agent", "agent": "chengjiao-dacheng", "session_ids": ["s1"]})
    assert r.status_code == 200 and "Challenger" in app.state.fake.systems[-1] and r.json()["session_ids"] == ["s1"]
    assert len(c.get("/v1/users/me/memory").json()["reviews"]) == 2
    assert TestClient(app, cookies={"vk_ui": "go"}).get("/v1/users/me/memory").status_code == 400
    assert c.get("/me").status_code == 200 and "综合复盘" in c.get("/me").text


def test_code_level_agents_run_local_and_listed(app, root):
    """球球和邱老师的上游还没实现，先本地跑（run: local）。
    red-coach 2026-09-16 先走上游、同日又改成配置级，不在这条里。"""
    c = TestClient(app, cookies={"vk_ui": "go"})
    for aid, mark in (("qiuqiu", "球球老师"), ("qiu-laoshi", "邱老师")):
        r = c.post(f"/v1/agents/{aid}/chat/completions", json={"stream": True, "messages": [{"role": "user", "content": "你好"}]})
        assert r.status_code == 200 and sse_text(r) == "好的，您慢慢说。", aid
        assert mark in app.state.fake.systems[-1]
    from tests.test_agent_chat import make_host_bound
    make_host_bound(root)
    idx = c.get("/talk").text
    assert "球球老师" in idx and "本地试运行" in idx
    # 宿主型 agent 通用页不给直接开，链去宿主（2026-09-16）；RED 已改配置级，照常能开
    assert "宿主演示教练" in idx and "在 AI 教练里练" in idx
    assert "RED 融资叙事教练" in idx
    page = c.get("/talk/qiuqiu").text
    assert "mode: '__MODE__'" not in page and "mode: 'voice'" in page
    page2 = c.get("/talk/kefu-xiaoan").text
    assert "mode: 'video'" in page2 and 'id="uid"' in page2


def test_code_level_upstream_proxy_path(app, root, monkeypatch):
    """run 去掉 → 走上游代理；上游挂了要说人话，不能 500。"""
    import yaml
    p = root / "qiuqiu" / "manifest.yaml"; m = yaml.safe_load(p.read_text(encoding="utf-8")); m.pop("run", None)
    p.write_text(yaml.safe_dump(m, allow_unicode=True, sort_keys=False), encoding="utf-8")
    c = TestClient(app, cookies={"vk_ui": "go"})
    r = c.post("/v1/agents/qiuqiu/chat/completions", json={"stream": True, "messages": [{"role": "user", "content": "你好"}]})
    # 上游挂了说人话，不把技术错误塞进 agent 嘴里（原来是「上游没接上：上游 HTTP 404」，会被念出来）
    assert r.status_code == 200 and "连不上" in sse_text(r) and "HTTP" not in sse_text(r)
