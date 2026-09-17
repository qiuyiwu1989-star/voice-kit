"""统一后台外壳（2026-09-17）：共享 CSS/JS 的门票、三页的引用、旧内联导航删干净、儿童只读不渲染写操作。

规范：docs/统一后台设计规范.md §3.11、§4、§5 R5、§8.8。
"""
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from fastapi.testclient import TestClient

from voice_kit import admin as ADM
from voice_kit.tenants import hash_key

REPO = Path(__file__).parent.parent
VK = REPO / "voice_kit"
PAGES = {"/admin/agents": "admin.html", "/admin/memory": "memory.html", "/admin/feedback": "feedback.html"}


@pytest.fixture
def root(tmp_path):
    dst = tmp_path / "agents"; shutil.copytree(REPO / "agents", dst); return dst


@pytest.fixture
def app(root, tmp_path, monkeypatch):
    tp = tmp_path / "t.json"
    tp.write_text(json.dumps({"tenants": [{"id": "coach", "name": "AI 教练", "key_sha256": hash_key("ck")}]}), encoding="utf-8")
    for k, v in [("TOKENHUB_API_KEY", "x"), ("TC_SECRET_ID", "x"), ("TC_SECRET_KEY", "x"), ("TENCENT_APPID", "1"),
                 ("TRTC_SDKAPPID", "1"), ("TRTC_SDKSECRETKEY", "k"), ("VOICEKIT_PUBLIC_URL", "https://voice.test"),
                 ("VOICEKIT_AGENTS_DIR", str(root)), ("VOICEKIT_ADMIN_TOKEN", "door"), ("VOICEKIT_UI_TOKEN", "go")]:
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("VOICEKIT_ENV", raising=False)
    from voice_kit import service
    return service.create_service(str(tp))


# ---------------------------------------------------------------- 静态资源要门票

def test_static_assets_need_admin_ticket(app, monkeypatch):
    anon = TestClient(app)
    for name in ("vk-admin.css", "vk-shell.js"):
        assert anon.get(f"/admin/static/{name}").status_code == 401
        # 用户端门票不是后台门票
        assert TestClient(app, cookies={"vk_ui": "go"}).get(f"/admin/static/{name}").status_code == 401
    c = TestClient(app, headers={"Authorization": "Bearer door"})
    css = c.get("/admin/static/vk-admin.css?v=x")
    assert css.status_code == 200 and css.headers["content-type"].startswith("text/css") and "--vk-accent" in css.text
    js = c.get("/admin/static/vk-shell.js")
    assert js.status_code == 200 and "javascript" in js.headers["content-type"] and "window.VK" in js.text
    assert "max-age" in css.headers.get("cache-control", "")
    # 白名单外一律 404：后台路径不替别的静态文件开门
    for bad in ("recorder.js", "..%2Fadmin.py", "vk-admin.css.bak"):
        assert c.get(f"/admin/static/{bad}").status_code == 404, bad
    # 首次带 ?k= 进页面种下的 cookie 也能取资源（浏览器实际走的路）
    b = TestClient(app)
    assert b.get("/admin/memory?k=door").status_code == 200
    assert b.get("/admin/static/vk-shell.js").status_code == 200
    monkeypatch.delenv("VOICEKIT_ADMIN_TOKEN")
    assert TestClient(app).get("/admin/static/vk-admin.css").status_code == 404


def test_pages_get_asset_version_and_env_injected(app, monkeypatch):
    c = TestClient(app, headers={"Authorization": "Bearer door"})
    v = ADM.asset_version()
    assert re.fullmatch(r"[0-9a-f]{10}", v)
    for path in PAGES:
        html = c.get(path).text
        assert "{{" not in html, path
        assert f'href="/admin/static/vk-admin.css?v={v}"' in html, path
        assert f'src="/admin/static/vk-shell.js?v={v}"' in html, path
        assert 'data-vk-env="线上"' in html, path          # 默认线上
    monkeypatch.setenv("VOICEKIT_ENV", '本地"><script>')
    html = c.get("/admin/feedback").text
    assert 'data-vk-env="本地&quot;&gt;&lt;script&gt;"' in html   # 转义过，不能借环境变量注入


def test_asset_version_follows_content(monkeypatch, tmp_path):
    for n in ADM.ADMIN_ASSETS:
        (tmp_path / n).write_text("a", encoding="utf-8")
    monkeypatch.setattr(ADM, "_STATIC", tmp_path)
    v1 = ADM.asset_version()
    (tmp_path / "vk-admin.css").write_text("b", encoding="utf-8")
    assert ADM.asset_version() != v1


# ---------------------------------------------------------------- 三页都用共享外壳，旧的各自一份删干净

@pytest.mark.parametrize("page", sorted(PAGES.values()))
def test_page_uses_shared_shell_and_drops_old_inline_chrome(page):
    html = (VK / page).read_text(encoding="utf-8")
    assert '<link rel="stylesheet" href="/admin/static/vk-admin.css?v={{VK_ASSET_V}}">' in html
    assert '<script src="/admin/static/vk-shell.js?v={{VK_ASSET_V}}"></script>' in html
    assert 'id="vkShell"' in html and 'data-vk-env="{{VK_ENV}}"' in html
    # 旧的内联导航和旧令牌
    assert 'nav class="top"' not in html and "<nav" not in html
    assert "--ink" not in html and "--paper" not in html and ":root{" not in html
    # 颜色只在 vk-admin.css 里定义（§8.8）：页面不再有 <style>，也不写 #hex
    assert "<style" not in html
    assert not re.search(r"#[0-9a-fA-F]{6}\b|#[0-9a-fA-F]{3}\b(?![0-9a-zA-Z_-])(?=[;\s\"')])", html), page
    # 零外部资源、不用 emoji
    assert not re.search(r"https?://", html), page
    assert not re.search("[\U0001F300-\U0001FAFF]", html), page


def test_shell_js_theme_defaults_dark_and_remembers_manual_choice():
    js = (VK / "static" / "vk-shell.js").read_text(encoding="utf-8")
    css = (VK / "static" / "vk-admin.css").read_text(encoding="utf-8")
    # 默认暗色，不跟随系统
    assert "prefers-color-scheme" not in css and "prefers-color-scheme" not in js
    assert ":root[data-theme=\"light\"]" in css
    root_block = css.split(":root{", 1)[1].split("}", 1)[0]
    assert "--vk-bg:#07070F" in root_block and "color-scheme:dark" in root_block
    # 手动选择存 localStorage，读写都包 try
    assert "localStorage.getItem(THEME_KEY)" in js and "localStorage.setItem(THEME_KEY" in js
    for m in re.finditer(r"localStorage\.(getItem|setItem)\(", js):
        before = js[max(0, m.start() - 20):m.start()]
        assert "try{" in before, before
    for page in PAGES.values():
        assert '<html lang="zh-CN" data-theme="dark">' in (VK / page).read_text(encoding="utf-8")
    assert not re.search(r"https?://", css + js)


# ---------------------------------------------------------------- 儿童只读：写操作不渲染（R5）

def _node_run(tmp_path, page, extra_setup, probe):
    if not shutil.which("node"):
        pytest.skip("没有 node")
    src = (VK / page).read_text(encoding="utf-8")
    js = re.search(r"<script>(.*?)</script>", src, re.S).group(1)
    shell = (VK / "static" / "vk-shell.js").read_text(encoding="utf-8")
    stub = """
const store = new Map();
const mk = (sel) => { const el = { sel, style:{}, classList:{toggle(){},add(){},remove(){},contains(){return false;}}, hidden:false, value:'', textContent:'', innerHTML:'', className:'',
  disabled:false, dataset:{}, appendChild(){}, insertAdjacentHTML(){}, setAttribute(){}, getAttribute(){ return null; }, removeAttribute(){},
  querySelector(){ return mk(); }, querySelectorAll(){ return []; }, remove(){}, focus(){}, scrollIntoView(){}, addEventListener(){} };
  el.parentElement = { hidden:false }; return el; };
globalThis.document = { querySelector: (s) => { if(!store.has(s)) store.set(s, mk(s)); return store.get(s); }, querySelectorAll: () => [], createElement: () => mk() };
globalThis.window = globalThis;
globalThis.innerWidth = 1280;
globalThis.localStorage = { getItem(){ return null; }, setItem(){} };
globalThis.location = { pathname:'/admin/x', search:'', hash:'', href:'http://t/admin/x', host:'t', origin:'http://t' };
globalThis.history = { replaceState(){} };
""" + extra_setup
    run = tmp_path / "run.mjs"
    run.write_text(stub + "\n" + shell + "\n" + js + "\n" + probe, encoding="utf-8")
    r = subprocess.run(["node", str(run)], capture_output=True, text=True, timeout=30, cwd=str(tmp_path))
    assert r.returncode == 0, (r.stdout[-600:], r.stderr[-1500:])
    return r.stdout


MEM_FIXTURE = {
    "summary": {"uid": "朵朵", "product": "qiuqiu", "sessions": 1, "facts": 1, "judgments": 1, "reviews": 1,
                "last_at": "2026-09-15T10:00:00", "agents": ["qiuqiu"], "legacy": False},
    "fact_categories": ["constraint", "event"],
    "sessions": [{"file": "s1.json", "at": "2026-09-15T10:00:00", "agent": "qiuqiu", "agent_name": "球球", "user_turns": 3, "opening": "你好"}],
    "facts": [{"index": 0, "category": "event", "content": "迷上折纸", "quote": "我喜欢折纸", "seen": 1, "first_at": "09-15", "last_at": "09-15"}],
    "judgments": {"qiuqiu": [{"at": "2026-09-15T10:05:00", "session_id": "s1", "text": "对手工有持续兴趣"}]},
    "reviews": [{"at": "2026-09-15T10:06:00", "method": "m", "text": "复盘", "session_ids": ["s1"]}],
    "audit": [{"at": "2026-09-15T11:00:00", "actor": "admin", "action": "update_fact", "target": "facts/0",
               "before": {"category": "event", "content": "折纸"}, "after": {"category": "event", "content": "迷上折纸"}}],
}
DANGER_MARKERS = ['data-act="edit-fact"', 'data-act="fact-more"', 'data-act="judg-more"', 'id="btnExport"',
                  'id="btnDelUser"', "删除这个用户", "导出记忆", "vk-danger-zone"]


def test_memory_child_readonly_renders_no_write_controls(tmp_path):
    setup = "globalThis.fetch = async () => ({ ok:true, status:200, json: async () => ({ products:['talk','qiuqiu'], readonly:['qiuqiu'], default:'talk', fact_categories:['event'], users:[] }) });"
    probe = f"""
const FIX = {json.dumps(MEM_FIXTURE, ensure_ascii=False)};
setTimeout(() => {{
  const tabs = ['facts','judgments','sessions','reviews','audit','overview'];
  const ro = tabs.map(t => userHtml({{...FIX, readonly:true}}, t)).join('\\n@@\\n');
  const rw = tabs.map(t => userHtml({{...FIX, readonly:false, summary:{{...FIX.summary, product:'coach'}}}}, t)).join('\\n@@\\n');
  EDIT = 0; const roEditing = panelHtml({{...FIX, readonly:true}}, 'facts');
  console.log(JSON.stringify({{ro, rw, roEditing}}));
  process.exit(0);
}}, 30);
"""
    out = json.loads(_node_run(tmp_path, "memory.html", setup, probe).strip().splitlines()[-1])
    for m in DANGER_MARKERS:
        assert m not in out["ro"], m                  # 儿童：不渲染，不是置灰
        assert m in out["rw"], m                      # 成人：确实有（防止断言空转）
    assert 'data-act="save-fact"' not in out["roEditing"]   # 就算编辑状态被弄脏也不出输入框
    assert "儿童数据 · 只读" in out["ro"] and "修改流程还没定" in out["ro"]
    assert "迷上折纸" in out["ro"]                    # 能看


def test_feedback_child_agent_renders_no_tabs_or_actions(tmp_path):
    setup = "globalThis.fetch = async () => ({ ok:true, status:200, json: async () => ({ enabled:true, agents:[], flags:{} }) });"
    probe = """
setTimeout(() => {
  CUR = { id:'qiuqiu', name:'球球老师', version:'1.0.0', rails:'child_strict', collect:false, why:'儿童档位：不记原话、不分析、不出提案' };
  render();
  console.log(JSON.stringify({ html: document.querySelector('#detail').innerHTML }));
  process.exit(0);
}, 30);
"""
    html = json.loads(_node_run(tmp_path, "feedback.html", setup, probe).strip().splitlines()[-1])["html"]
    assert "不采集" in html and "vk-alert--child" in html
    for m in ('role="tablist"', "btnPub", "btnForce", "btnAnalyze", "btnRollback", "data-days"):
        assert m not in html, m


def test_feedback_force_publish_keeps_backend_contract_and_three_steps():
    """强制发布：勾选 + 必填理由 + 输入提案 id 确认；发给后端的字段和以前一字不差。"""
    src = (VK / "feedback.html").read_text(encoding="utf-8")
    assert "JSON.stringify({force:true, confirm:'我确认要发布', note: note.value})" in src
    assert 'id="forceChk"' in src and 'id="forceNote"' in src and "typed: p.id" in src
    assert "fb.disabled = !(chk.checked && note.value.trim())" in src


def test_memory_page_keeps_backend_contract():
    src = (VK / "memory.html").read_text(encoding="utf-8")
    assert "JSON.stringify({confirm: ok.value})" in src and "typed: s.uid" in src
    assert "expect:{category:f.category, content:f.content}" in src
    assert "JSON.stringify({expect_at: j.at||''})" in src


def test_root_redirects_to_admin(app_client_for_root=None):
    """打开中台域名根路径直接去后台，而不是 404（2026-09-17）。"""
    import os
    from fastapi.testclient import TestClient
    from voice_kit import service
    os.environ.setdefault("TOKENHUB_API_KEY", "x")
    for k in ("TC_SECRET_ID", "TC_SECRET_KEY", "TRTC_SDKSECRETKEY"):
        os.environ.setdefault(k, "x")
    os.environ.setdefault("TENCENT_APPID", "1"); os.environ.setdefault("TRTC_SDKAPPID", "1")
    os.environ.setdefault("VOICEKIT_PUBLIC_URL", "https://voice.test")
    import json, tempfile
    tp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False); json.dump({"tenants": []}, tp); tp.close()
    r = TestClient(service.create_service(tp.name), follow_redirects=False).get("/")
    assert r.status_code == 302 and r.headers["location"] == "/admin/agents"
