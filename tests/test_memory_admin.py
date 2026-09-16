"""用户记忆：按产品分命名空间 + 旧目录兼容 + 统一后台 /admin/memory（2026-09-16）。"""
import json
import shutil
import sys
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from fastapi.testclient import TestClient

from voice_kit import memory as M
from voice_kit.tenants import Registry, hash_key

REPO = Path(__file__).parent.parent
FACT = {"category": "constraint", "content": "预算三千以内", "quote": "三千以内"}


@pytest.fixture
def root(tmp_path):
    dst = tmp_path / "agents"; shutil.copytree(REPO / "agents", dst); return dst


class FakeLLM:
    def __init__(self): self.systems = []; self.extract = json.dumps([FACT], ensure_ascii=False)
    def stream(self, messages, **kw):
        sysm = kw.get("system") or ""; self.systems.append(sysm)
        r = self.extract if "事实抽取器" in sysm else "好的。"
        for i in range(0, len(r), 5): yield r[i:i + 5]


@pytest.fixture
def app(root, tmp_path, monkeypatch):
    tp = tmp_path / "t.json"
    tp.write_text(json.dumps({"tenants": [
        {"id": "coach", "name": "AI 教练", "key_sha256": hash_key("ck")},
        {"id": "qiuqiu", "name": "球球", "key_sha256": hash_key("qk")},
    ]}), encoding="utf-8")
    for k, v in [("TOKENHUB_API_KEY", "x"), ("TC_SECRET_ID", "x"), ("TC_SECRET_KEY", "x"), ("TENCENT_APPID", "1"),
                 ("TRTC_SDKAPPID", "1"), ("TRTC_SDKSECRETKEY", "k"), ("VOICEKIT_PUBLIC_URL", "https://voice.test"),
                 ("VOICEKIT_AGENTS_DIR", str(root)), ("VOICEKIT_ADMIN_TOKEN", "door"), ("VOICEKIT_UI_TOKEN", "go")]:
        monkeypatch.setenv(k, v)
    from voice_kit import service
    import voice_kit.providers.tokenhub as th
    fake = FakeLLM()
    monkeypatch.setattr(service, "TokenHubLLM", lambda *a, **k: fake, raising=False)
    monkeypatch.setattr(th, "TokenHubLLM", lambda *a, **k: fake)
    a = service.create_service(str(tp)); a.state.fake = fake; return a


def admin(app):
    return TestClient(app, headers={"Authorization": "Bearer door"})


def seed(root, product, uid, *, agent="kefu-xiaoan"):
    M.save_session(root, product, uid, {"session_id": "s1", "agent": agent, "agent_name": "小安",
                                        "at": "2026-09-15T10:00:00", "review": "他要的是被听见。",
                                        "turns": [{"t": "0:01", "role": "user", "text": "我们预算三千以内"},
                                                  {"t": "0:03", "role": "bot", "text": "好。"}]})
    M.merge_facts(root, product, uid, [FACT, {"category": "relation", "content": "孩子上五年级"}], source_session="s1")
    M.save_judgment(root, product, uid, agent, {"at": "2026-09-15T10:05:00", "session_id": "s1", "text": "先接情绪。"})
    M.save_review(root, product, uid, {"at": "2026-09-15T11:00:00", "method": "timeline", "session_ids": ["s1"], "text": "在变好"})


# ---------------------------------------------------------------- 命名空间

def test_same_uid_in_coach_and_talk_do_not_see_each_other(root):
    M.merge_facts(root, "coach", "小明", [FACT])
    M.merge_facts(root, "talk", "小明", [{"category": "relation", "content": "孩子上五年级"}])
    assert [f["content"] for f in M.load_facts(root, "coach", "小明")] == ["预算三千以内"]
    assert [f["content"] for f in M.load_facts(root, "talk", "小明")] == ["孩子上五年级"]
    assert (root / "users" / "coach" / "小明" / "facts.json").exists()
    b = M.briefing(root, "qiuqiu", "小明", "qiuqiu", {"read": {"facts": []}})
    assert b == ""                                           # 孩子那层没有这个人，教练的事实进不来
    assert "预算三千以内" not in M.briefing(root, "talk", "小明", "kefu-xiaoan", {"read": {"facts": []}})
    assert [u["uid"] for u in M.list_users(root, "coach")] == ["小明"]
    from voice_kit import agents as A
    assert "users" not in {a.id for a in A.load_all(root)}      # 记忆库和 agent 同住一个根，但它不是 agent
    with pytest.raises(ValueError):
        M.load_facts(root, "", "小明")                       # 没有产品就炸，不许悄悄落进默认层
    assert M.safe_product("../Coach") == "coach"


def test_tenant_product_field_defaults_to_id(tmp_path):
    tp = tmp_path / "t.json"
    tp.write_text(json.dumps({"tenants": [{"id": "coach", "name": "c", "key_sha256": "a"},
                                          {"id": "coach-staging", "name": "s", "key_sha256": "b", "product": "coach"}]}))
    reg = Registry(tp)
    assert reg.by_id("coach").memory_product == "coach"
    assert reg.by_id("coach-staging").memory_product == "coach"


def test_tenant_key_writes_into_its_product_and_reads_only_there(app, root):
    turns = [{"t": "0:01", "role": "user", "text": "我们预算三千以内"}]
    coach = TestClient(app, headers={"Authorization": "Bearer ck", "X-User-Id": quote("小明")})
    r = coach.post("/v1/agents/kefu-xiaoan/review", json={"session_id": "c1", "turns": turns})
    assert r.status_code == 200 and r.json()["product"] == "coach"
    assert (root / "users" / "coach" / "小明" / "sessions").is_dir()
    assert not (root / "users" / "talk" / "小明").exists()
    # 通用 /talk 页（UI 门票）同名的人 → talk 层，看不到教练那边的
    talk = TestClient(app, cookies={"vk_ui": "go"}, headers={"X-User-Id": quote("小明")})
    m = talk.get("/v1/users/me/memory").json()
    assert m["product"] == "talk" and m["sessions"] == [] and m["facts"] == []
    talk.post("/v1/agents/kefu-xiaoan/chat/completions", json={"stream": True, "messages": [{"role": "user", "content": "hi"}]})
    assert "预算三千以内" not in app.state.fake.systems[-1]
    # 教练带钥匙读自己的：看得到；速览卡也进了
    m = coach.get("/v1/users/me/memory").json()
    assert m["product"] == "coach" and len(m["sessions"]) == 1 and m["facts"][0]["content"] == "预算三千以内"
    coach.post("/v1/agents/kefu-xiaoan/chat/completions", json={"stream": True, "messages": [{"role": "user", "content": "又是我"}]})
    assert "预算三千以内" in app.state.fake.systems[-1]
    # 钥匙不对、也没门票：401
    bad = TestClient(app, headers={"Authorization": "Bearer nope", "X-User-Id": quote("小明")})
    assert bad.get("/v1/users/me/memory").status_code == 401


# ---------------------------------------------------------------- 旧的平铺目录 users/<uid>/

def _legacy(root, uid, *, fact="旧事实"):
    d = root / "users" / uid
    (d / "sessions").mkdir(parents=True)
    (d / "sessions" / "20260914-100000-kefu-xiaoan-s0.json").write_text(json.dumps(
        {"session_id": "s0", "agent": "kefu-xiaoan", "at": "2026-09-14T10:00:00", "turns": [{"role": "user", "text": "旧的"}]},
        ensure_ascii=False), encoding="utf-8")
    (d / "judgments").mkdir()
    (d / "judgments" / "kefu-xiaoan.json").write_text(json.dumps([{"at": "2026-09-14T10:01:00", "text": "旧判断"}], ensure_ascii=False), encoding="utf-8")
    (d / "facts.json").write_text(json.dumps({"facts": [{"category": "event", "content": fact, "seen": 1}]}, ensure_ascii=False), encoding="utf-8")
    return d


def test_legacy_flat_dir_is_read_under_talk_only(root):
    _legacy(root, "老王")
    assert M.load_facts(root, "talk", "老王")[0]["content"] == "旧事实"
    assert len(M.load_sessions(root, "talk", "老王")) == 1 and M.load_judgments(root, "talk", "老王")["kefu-xiaoan"]
    assert M.load_facts(root, "coach", "老王") == []            # 旧数据只可能来自 /talk
    rows = M.list_users(root, "talk")
    assert [r["uid"] for r in rows] == ["老王"] and rows[0]["legacy"] and rows[0]["sessions"] == 1
    assert "老王" not in M.list_products(root) and "talk" in M.list_products(root)


def test_first_write_migrates_legacy_so_one_person_is_not_split(root):
    old = _legacy(root, "老王")
    M.merge_facts(root, "talk", "老王", [FACT])
    assert not old.exists()
    new = root / "users" / "talk" / "老王"
    assert {f["content"] for f in M.load_facts(root, "talk", "老王")} == {"旧事实", "预算三千以内"}
    assert (new / "sessions" / "20260914-100000-kefu-xiaoan-s0.json").exists()
    assert M.load_judgments(root, "talk", "老王")["kefu-xiaoan"][0]["text"] == "旧判断"
    assert M.migrate_legacy(root) == []                          # 幂等


def test_migrate_legacy_merges_and_handles_uid_named_like_a_product(root):
    seed(root, "talk", "阿强")                                  # 新布局里已有一个 talk 用户
    _legacy(root, "talk", fact="叫 talk 的旧用户")               # 旧 uid 恰好叫 talk，和产品目录同一个目录
    _legacy(root, "阿强", fact="阿强的旧事实")                    # 新旧两边都有 → 合并不覆盖
    moved = M.migrate_legacy(root)
    assert set(moved) == {"talk", "阿强"}
    assert M.load_facts(root, "talk", "talk")[0]["content"] == "叫 talk 的旧用户"
    got = {f["content"] for f in M.load_facts(root, "talk", "阿强")}
    assert got == {"预算三千以内", "孩子上五年级", "阿强的旧事实"}
    js = M.load_judgments(root, "talk", "阿强")["kefu-xiaoan"]
    assert [j["text"] for j in js] == ["旧判断", "先接情绪。"]    # 按时间合并
    assert sorted(u["uid"] for u in M.list_users(root, "talk")) == ["talk", "阿强"]
    assert not (root / "users" / "阿强").exists() and not (root / "users" / "talk" / "facts.json").exists()
    assert M.migrate_legacy(root) == []


# ---------------------------------------------------------------- 后台：鉴权

def test_memory_admin_requires_admin_ticket(app, root, monkeypatch):
    c = TestClient(app)
    assert c.get("/admin/memory").status_code == 401
    assert c.get("/admin/memory/api/users").status_code == 401
    # 用户端门票不是后台门票
    assert TestClient(app, cookies={"vk_ui": "go"}).get("/admin/memory/api/users").status_code == 401
    assert TestClient(app, headers={"Authorization": "Bearer ck"}).get("/admin/memory/api/users").status_code == 401
    r = c.get("/admin/memory?k=door")
    assert r.status_code == 200 and "用户记忆" in r.text and "/admin/static/vk-shell.js" in r.text and "vk_admin" in r.cookies
    assert c.get("/admin/memory/api/users").status_code == 200   # cookie 生效
    # Agent 后台照常，且有入口过来
    assert "/admin/static/vk-shell.js" in admin(app).get("/admin/agents").text     # 导航由共享外壳渲染
    assert admin(app).get("/admin/agents/api/list").status_code == 200
    assert c.get("/admin", follow_redirects=False).headers["location"].startswith("/admin/agents")
    monkeypatch.delenv("VOICEKIT_ADMIN_TOKEN")
    assert TestClient(app).get("/admin/memory/api/users").status_code == 404


def test_products_and_user_list(app, root):
    seed(root, "coach", "小明"); seed(root, "talk", "小明", agent="guwen-axun"); seed(root, "qiuqiu", "朵朵", agent="qiuqiu")
    c = admin(app)
    p = c.get("/admin/memory/api/products").json()
    assert {"coach", "talk", "qiuqiu"} <= set(p["products"]) and p["readonly"] == ["qiuqiu"]
    rows = c.get("/admin/memory/api/users", params={"product": "coach"}).json()["users"]
    last = rows[0].pop("last_at")
    assert last >= "2026-09-15T11:00:00"          # 场次/复盘/判断/事实里最晚的那个（事实抽取日是今天）
    assert rows == [{"uid": "小明", "product": "coach", "sessions": 1, "facts": 2, "judgments": 1, "reviews": 1,
                     "agents": ["kefu-xiaoan"], "legacy": False}]
    assert c.get("/admin/memory/api/users", params={"product": "talk"}).json()["users"][0]["agents"] == ["guwen-axun"]
    assert c.get("/admin/memory/api/users", params={"product": "../x"}).status_code == 400
    d = c.get(f"/admin/memory/api/users/coach/{quote('小明')}").json()
    assert d["facts"][0]["index"] == 0 and d["judgments"]["kefu-xiaoan"][0]["text"] == "先接情绪。"
    assert d["reviews"][0]["text"] == "在变好" and d["readonly"] is False
    s = c.get(f"/admin/memory/api/users/coach/{quote('小明')}/sessions/{d['sessions'][0]['file']}").json()
    assert [t["text"] for t in s["turns"]] == ["我们预算三千以内", "好。"] and s["review"] == "他要的是被听见。"
    assert c.get(f"/admin/memory/api/users/coach/{quote('小明')}/sessions/..%2Ffacts.json").status_code == 404
    assert c.get("/admin/memory/api/users/coach/nobody").status_code == 404


# ---------------------------------------------------------------- 事实：改 / 删 + 审计

def test_edit_and_delete_fact_write_audit(app, root):
    seed(root, "coach", "小明")
    c, base = admin(app), f"/admin/memory/api/users/coach/{quote('小明')}"
    exp = {"category": "constraint", "content": "预算三千以内"}
    r = c.patch(base + "/facts/0", json={"expect": exp, "content": "预算每学期五千以内", "category": "constraint"})
    assert r.status_code == 200 and r.json()["fact"]["content"] == "预算每学期五千以内"
    f = M.load_facts(root, "coach", "小明")[0]
    assert f["content"] == "预算每学期五千以内" and f["seen"] == 1 and "agent" not in f
    # 看到的已经不是那条了 → 409，不按位置改错条
    assert c.patch(base + "/facts/0", json={"expect": exp, "content": "x"}).status_code == 409
    # 事实 agent 中立：带 agent 字段一律拒
    r = c.patch(base + "/facts/0", json={"expect": {"category": "constraint", "content": "预算每学期五千以内"},
                                         "content": "y", "agent": "kefu-xiaoan"})
    assert r.status_code == 400 and "agent 中立" in r.json()["detail"]
    assert c.patch(base + "/facts/0", json={"expect": {"category": "constraint", "content": "预算每学期五千以内"},
                                            "category": "mood"}).status_code == 400
    # 改成和另一条一模一样 → 409
    assert c.patch(base + "/facts/0", json={"expect": {"category": "constraint", "content": "预算每学期五千以内"},
                                            "category": "relation", "content": "孩子上五年级"}).status_code == 409
    r = c.request("DELETE", base + "/facts/1", json={"expect": {"category": "relation", "content": "孩子上五年级"}})
    assert r.status_code == 200 and len(M.load_facts(root, "coach", "小明")) == 1
    assert c.request("DELETE", base + "/facts/5", json={"expect": {}}).status_code == 404
    au = c.get(base).json()["audit"]
    assert [a["action"] for a in au] == ["fact.delete", "fact.update"]            # 新的在前
    upd = au[1]
    assert upd["before"] == {"category": "constraint", "content": "预算三千以内"}
    assert upd["after"] == {"category": "constraint", "content": "预算每学期五千以内"}
    assert upd["target"] == "facts[0]" and upd["actor"] == "admin" and upd["uid"] == "小明" and upd["at"]
    assert (root / "users" / "coach" / "_audit.jsonl").read_text(encoding="utf-8").count("\n") == 2
    # 失败的操作不写审计；别的用户看不到这个人的审计
    seed(root, "coach", "小红")
    assert c.get(f"/admin/memory/api/users/coach/{quote('小红')}").json()["audit"] == []
    # 用户列表里的 _audit.jsonl 不是用户
    assert sorted(u["uid"] for u in c.get("/admin/memory/api/users?product=coach").json()["users"]) == ["小明", "小红"]


# ---------------------------------------------------------------- 判断：只能删

def test_judgments_can_only_be_deleted_never_rewritten(app, root):
    seed(root, "coach", "小明")
    M.save_judgment(root, "coach", "小明", "kefu-xiaoan", {"at": "2026-09-16T09:00:00", "session_id": "s2", "text": "第二条"})
    c, base = admin(app), f"/admin/memory/api/users/coach/{quote('小明')}/judgments/kefu-xiaoan"
    for method in ("PATCH", "PUT", "POST"):
        assert c.request(method, base + "/0", json={"text": "伪造的读法"}).status_code == 405, method
    assert not hasattr(M, "update_judgment")
    assert c.request("DELETE", base + "/0", json={"expect_at": "wrong"}).status_code == 409
    r = c.request("DELETE", base + "/0", json={"expect_at": "2026-09-15T10:05:00"})
    assert r.status_code == 200
    assert [j["text"] for j in M.load_judgments(root, "coach", "小明")["kefu-xiaoan"]] == ["第二条"]
    c.request("DELETE", base + "/0", json={"expect_at": "2026-09-16T09:00:00"})
    assert M.load_judgments(root, "coach", "小明") == {}
    au = M.load_audit(root, "coach", "小明")
    assert [a["action"] for a in au] == ["judgment.delete", "judgment.delete"]
    assert au[1]["before"]["text"] == "先接情绪。" and au[1]["before"]["agent"] == "kefu-xiaoan"


# ---------------------------------------------------------------- 儿童产品只读

def test_qiuqiu_is_read_only_in_admin(app, root):
    seed(root, "qiuqiu", "朵朵", agent="qiuqiu")
    before = sorted(str(p.relative_to(root)) + p.read_text(encoding="utf-8") if p.is_file() else str(p)
                    for p in (root / "users" / "qiuqiu").rglob("*"))
    c, base = admin(app), f"/admin/memory/api/users/qiuqiu/{quote('朵朵')}"
    d = c.get(base)
    assert d.status_code == 200 and d.json()["readonly"] is True and d.json()["facts"]    # 能看
    exp = {"category": "constraint", "content": "预算三千以内"}
    assert c.patch(base + "/facts/0", json={"expect": exp, "content": "x"}).status_code == 403
    assert c.request("DELETE", base + "/facts/0", json={"expect": exp}).status_code == 403
    assert c.request("DELETE", base + "/judgments/qiuqiu/0", json={"expect_at": "2026-09-15T10:05:00"}).status_code == 403
    assert c.request("DELETE", base, json={"confirm": "朵朵"}).status_code == 403
    assert c.get(base + "/export").status_code == 403
    after = sorted(str(p.relative_to(root)) + p.read_text(encoding="utf-8") if p.is_file() else str(p)
                   for p in (root / "users" / "qiuqiu").rglob("*"))
    assert before == after                                       # 一个字节没动，连审计都没写
    with pytest.raises(M.ReadOnlyProduct):
        M.delete_fact(root, "qiuqiu", "朵朵", 0, expect=exp)
    assert "只读" in c.get("/admin/memory").text
    # 平台采样链路照常写（只读只管后台）
    M.merge_facts(root, "qiuqiu", "朵朵", [{"category": "event", "content": "迷上折纸"}])


# ---------------------------------------------------------------- 删除整个用户 / 导出

def test_delete_user_needs_exact_uid_confirmation(app, root):
    seed(root, "coach", "小明"); seed(root, "talk", "小明")
    c, base = admin(app), f"/admin/memory/api/users/coach/{quote('小明')}"
    assert c.request("DELETE", base, json={}).status_code == 400
    assert c.request("DELETE", base, json={"confirm": "小"}).status_code == 400
    assert c.request("DELETE", base, json={"confirm": "小明 "}).status_code == 400
    assert (root / "users" / "coach" / "小明").is_dir()
    r = c.request("DELETE", base, json={"confirm": "小明"})
    assert r.status_code == 200 and r.json()["deleted"] == {"sessions": 1, "facts": 2, "judgments": 1, "reviews": 1}
    assert not (root / "users" / "coach" / "小明").exists()
    assert c.get(base).status_code == 404
    assert M.load_facts(root, "talk", "小明")                     # 别的产品下的同名人不受影响
    au = M.load_audit(root, "coach", "小明")
    assert au[0]["action"] == "user.delete" and au[0]["before"]["facts"] == 2
    assert "预算" not in json.dumps(au, ensure_ascii=False)       # 删了就是删了：审计里只有条数
    assert c.request("DELETE", base, json={"confirm": "小明"}).status_code == 404


def test_delete_legacy_talk_user_removes_old_dir_too(app, root):
    _legacy(root, "老王")
    c = admin(app)
    assert c.request("DELETE", f"/admin/memory/api/users/talk/{quote('老王')}", json={"confirm": "老王"}).status_code == 200
    assert not (root / "users" / "老王").exists() and M.list_users(root, "talk") == []


def test_export_is_complete_and_audited(app, root):
    seed(root, "coach", "小明")
    c = admin(app)
    base = f"/admin/memory/api/users/coach/{quote('小明')}"
    c.patch(base + "/facts/1", json={"expect": {"category": "relation", "content": "孩子上五年级"}, "content": "孩子上六年级"})
    r = c.get(base + "/export")
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
    d = r.json()
    assert d["product"] == "coach" and d["uid"] == "小明"
    assert d["sessions"][0]["turns"][0]["text"] == "我们预算三千以内" and d["sessions"][0]["review"]
    assert {f["content"] for f in d["facts"]} == {"预算三千以内", "孩子上六年级"}
    assert d["judgments"]["kefu-xiaoan"][0]["text"] == "先接情绪。"
    assert d["reviews"][0]["text"] == "在变好"
    assert [a["action"] for a in d["audit"]] == ["user.export", "fact.update"]
    assert c.get("/admin/memory/api/users/coach/nobody/export").status_code == 404
