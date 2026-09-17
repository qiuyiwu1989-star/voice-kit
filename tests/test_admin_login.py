"""后台账号密码登录（2026-09-17）：换掉「人要记门票」，门票只留给脚本。

规范：docs/后台登录.md。测试密码随便取，**不是**用户给的默认密码——那串密码不许出现在仓库任何文本里，
最后一条测试靠 sha256 比对来扫，本文件自己也不含它。
"""
import hashlib
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from fastapi.testclient import TestClient

from voice_kit import admin_auth as AUTH
from voice_kit import admin_password as PW
from voice_kit.tenants import hash_key

REPO = Path(__file__).parent.parent
TEST_PW = "correct horse battery"
TEST_HASH = PW.hash_password(TEST_PW)
# 用户给的默认密码（8 位数字）的 sha256。明文不进仓库，只比哈希。
FORBIDDEN_SHA256 = "98d5b93127d245212450d8536b98d7c10bcb8a4ce9b56753a366fe190ccc9ebe"


class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(AUTH, "LIMITER", AUTH.LoginLimiter(clock=c))
    return c


@pytest.fixture
def root(tmp_path):
    dst = tmp_path / "agents"; shutil.copytree(REPO / "agents", dst); return dst


def _make_app(tmp_path, root, monkeypatch, *, token="door", pw_hash=TEST_HASH):
    tp = tmp_path / "t.json"
    tp.write_text(json.dumps({"tenants": [{"id": "coach", "name": "AI 教练", "key_sha256": hash_key("ck")}]}), encoding="utf-8")
    for k, v in [("TOKENHUB_API_KEY", "x"), ("TC_SECRET_ID", "x"), ("TC_SECRET_KEY", "x"), ("TENCENT_APPID", "1"),
                 ("TRTC_SDKAPPID", "1"), ("TRTC_SDKSECRETKEY", "k"), ("VOICEKIT_PUBLIC_URL", "https://voice.test"),
                 ("VOICEKIT_AGENTS_DIR", str(root)), ("VOICEKIT_UI_TOKEN", "go")]:
        monkeypatch.setenv(k, v)
    for k, v in [("VOICEKIT_ADMIN_TOKEN", token), ("VOICEKIT_ADMIN_PASSWORD_HASH", pw_hash)]:
        if v:
            monkeypatch.setenv(k, v)
        else:
            monkeypatch.delenv(k, raising=False)
    for k in ("VOICEKIT_ADMIN_USER", "VOICEKIT_ADMIN_SESSION_SECRET", "VOICEKIT_ENV"):
        monkeypatch.delenv(k, raising=False)
    from voice_kit import service
    return service.create_service(str(tp))


@pytest.fixture
def app(tmp_path, root, monkeypatch, clock):
    return _make_app(tmp_path, root, monkeypatch)


def _client(app, **kw):
    return TestClient(app, follow_redirects=False, **kw)


def _login(c, user="qiuyiwu", pw=TEST_PW, nxt="/admin/memory", headers=None):
    return c.post("/admin/login", data={"username": user, "password": pw, "next": nxt}, headers=headers or {})


def _audit(root):
    p = root / AUTH.AUDIT_FILE
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines()] if p.exists() else []


# ---------------------------------------------------------------- 哈希

def test_password_hash_roundtrip_and_format():
    assert TEST_HASH.startswith("scrypt:") and "$" not in TEST_HASH and TEST_PW not in TEST_HASH
    assert PW.verify_password(TEST_PW, TEST_HASH)
    assert not PW.verify_password(TEST_PW + "x", TEST_HASH)
    assert PW.hash_password(TEST_PW) != TEST_HASH                       # 带盐：同一密码两次哈希不同
    pb = PW.hash_password(TEST_PW, "pbkdf2_sha256")
    assert pb.startswith("pbkdf2_sha256:") and PW.verify_password(TEST_PW, pb) and not PW.verify_password("no", pb)
    for bad in ("", "plain", "scrypt:1:2", "scrypt:16384:8:1:@@:@@", "md5:abc"):
        assert not PW.verify_password(TEST_PW, bad)                     # 格式坏 = 登不进，不是放行
    with pytest.raises(ValueError):
        PW.hash_password("")


def test_cli_stdin_outputs_env_line():
    r = subprocess.run([sys.executable, "-m", "voice_kit.admin_password", "--stdin"], input=TEST_PW + "\n",
                       capture_output=True, text=True, cwd=str(REPO), timeout=60)
    assert r.returncode == 0, r.stderr
    line = r.stdout.strip()
    assert line.startswith("VOICEKIT_ADMIN_PASSWORD_HASH=") and TEST_PW not in r.stdout
    assert PW.verify_password(TEST_PW, line.split("=", 1)[1])
    empty = subprocess.run([sys.executable, "-m", "voice_kit.admin_password", "--stdin"], input="\n",
                           capture_output=True, text=True, cwd=str(REPO), timeout=60)
    assert empty.returncode == 1 and "VOICEKIT_ADMIN_PASSWORD_HASH" not in empty.stdout


# ---------------------------------------------------------------- 登录

def test_login_success_sets_session_and_redirects(app, root):
    c = _client(app)
    page = c.get("/admin/login?next=/admin/feedback")
    assert page.status_code == 200 and 'name="username"' in page.text and 'type="password"' in page.text
    assert "登录" in page.text and "门票" not in page.text
    assert "<link" not in page.text and "<script src" not in page.text           # 零外部依赖：样式内联
    r = _login(c)
    assert r.status_code == 303 and r.headers["location"] == "/admin/memory"
    sc = r.headers["set-cookie"]
    assert AUTH.SESSION_COOKIE in sc and "HttpOnly" in sc and "samesite=lax" in sc.lower()
    assert "Max-Age=604800" in sc and "Secure" not in sc                # http 上不加 Secure
    assert c.get("/admin/memory/api/users").status_code == 200
    home = c.get("/admin/agents")
    assert home.status_code == 200 and 'data-vk-user="qiuyiwu"' in home.text
    assert _audit(root)[-1]["result"] == "ok" and _audit(root)[-1]["user"] == "qiuyiwu"


def test_secure_cookie_behind_https_proxy(app):
    r = _login(_client(app), headers={"X-Forwarded-Proto": "https"})
    assert r.status_code == 303 and "Secure" in r.headers["set-cookie"]


def test_failure_message_does_not_reveal_which_part_is_wrong(app, root):
    c = _client(app)
    wrong_user = _login(c, user="admin")
    wrong_pw = _login(c, pw="nope")
    assert wrong_user.status_code == wrong_pw.status_code == 401
    for r in (wrong_user, wrong_pw):
        assert AUTH.MSG_BAD in r.text and AUTH.SESSION_COOKIE not in r.headers.get("set-cookie", "")
    strip = lambda t: re.sub(r'value="[^"]*"', "", t)                    # 回填的用户名不算差异
    assert strip(wrong_user.text) == strip(wrong_pw.text)
    assert c.get("/admin/agents/api/list").status_code == 401
    assert [a["result"] for a in _audit(root)] == ["fail", "fail"]
    assert {a["user"] for a in _audit(root)} == {"admin", "qiuyiwu"}
    assert all(TEST_PW not in json.dumps(a) and "nope" not in json.dumps(a) for a in _audit(root))


def test_lockout_after_five_failures_then_unlock(app, clock, root):
    c = _client(app)
    for _ in range(5):
        assert _login(c, pw="bad").status_code == 401
    locked = _login(c)                                                  # 对的密码也不给进
    assert locked.status_code == 429 and AUTH.MSG_LOCKED in locked.text and "稍后再试" in locked.text
    assert _audit(root)[-1]["result"] == "locked"
    # 别的 IP 不受影响
    assert _login(c, headers={"X-Real-IP": "10.0.0.9"}).status_code == 303
    clock.t += 14 * 60
    assert _login(c).status_code == 429
    clock.t += 61
    assert _login(c).status_code == 303                                 # 15 分钟到了
    # 成功后计数清零：再错 4 次还不锁
    c2 = _client(app)
    for _ in range(4):
        _login(c2, pw="bad")
    assert _login(c2).status_code == 303


def test_global_rate_limit(clock):
    lim = AUTH.LoginLimiter(clock=clock)
    for i in range(31):
        lim.fail(f"10.1.{i}.1")
    assert lim.check("10.9.9.9") == "global"
    clock.t += 61
    assert lim.check("10.9.9.9") is None


def test_next_only_allows_admin_paths(app):
    assert AUTH.safe_next("/admin/memory?product=talk") == "/admin/memory?product=talk"
    for bad in ("https://evil.com/admin/", "//evil.com/admin/x", "/\\evil.com", "/admin\\..\\x", "/talk",
                "/admin/login?next=//evil", "/admin/logout", "", None, "javascript:alert(1)", "/admin/x\r\nSet-Cookie:a"):
        assert AUTH.safe_next(bad) == AUTH.DEFAULT_NEXT, bad
    r = _login(_client(app), nxt="https://evil.com/")
    assert r.status_code == 303 and r.headers["location"] == AUTH.DEFAULT_NEXT


def test_tampered_or_expired_cookie_rejected(app, clock):
    c = _client(app)
    _login(c)
    good = c.cookies.get(AUTH.SESSION_COOKIE)
    assert good and AUTH.session_user(good, clock.t) == "qiuyiwu"
    v, ub, exp, sig = good.split(".")
    other = AUTH.base64.urlsafe_b64encode(b"root").decode().rstrip("=")
    for bad in (f"{v}.{ub}.{int(exp) + 999999}.{sig}", f"{v}.{other}.{exp}.{sig}", good[:-1] + ("0" if good[-1] != "0" else "1"),
                "v1.x.1.y", "garbage"):
        assert _client(app, cookies={AUTH.SESSION_COOKIE: bad}).get("/admin/agents/api/list").status_code == 401, bad
    clock.t += AUTH.SESSION_TTL + 1
    assert _client(app, cookies={AUTH.SESSION_COOKIE: good}).get("/admin/agents/api/list").status_code == 401


def test_changing_password_hash_invalidates_sessions(app, monkeypatch):
    c = _client(app)
    _login(c)
    assert c.get("/admin/agents/api/list").status_code == 200
    monkeypatch.setenv("VOICEKIT_ADMIN_PASSWORD_HASH", PW.hash_password("another one"))
    assert c.get("/admin/agents/api/list").status_code == 401
    # 单独配了 SESSION_SECRET 也一样：签名里揉着哈希指纹
    monkeypatch.setenv("VOICEKIT_ADMIN_SESSION_SECRET", "s3cret")
    c2 = _client(app)
    assert _login(c2, pw="another one").status_code == 303
    assert c2.get("/admin/agents/api/list").status_code == 200
    monkeypatch.setenv("VOICEKIT_ADMIN_PASSWORD_HASH", TEST_HASH)
    assert c2.get("/admin/agents/api/list").status_code == 401


def test_logout_clears_session(app):
    c = _client(app)
    _login(c)
    r = c.get("/admin/logout")
    assert r.status_code == 303 and r.headers["location"] == "/admin/login"
    assert AUTH.SESSION_COOKIE not in c.cookies
    assert c.get("/admin/agents/api/list").status_code == 401


def test_ticket_still_works_for_scripts(app):
    assert _client(app, headers={"Authorization": "Bearer door"}).get("/admin/agents/api/list").status_code == 200
    c = _client(app)
    r = c.get("/admin/agents?k=door")
    assert r.status_code == 200 and 'data-vk-user=""' in r.text
    assert c.get("/admin/memory/api/users").status_code == 200
    assert _client(app).get("/admin/static/vk-shell.js?k=nope").status_code == 401


def test_anonymous_pages_redirect_apis_401(app):
    anon = _client(app)
    for page in ("/admin/agents", "/admin/memory?product=talk", "/admin/feedback"):
        r = anon.get(page)
        assert r.status_code == 302, page
        assert r.headers["location"].startswith("/admin/login?next=%2Fadmin%2F")
    assert anon.get("/admin/memory?product=talk").headers["location"] == "/admin/login?next=%2Fadmin%2Fmemory%3Fproduct%3Dtalk"
    for api in ("/admin/agents/api/list", "/admin/memory/api/users", "/admin/feedback/api/agents", "/admin/static/vk-admin.css"):
        r = anon.get(api)
        assert r.status_code == 401 and r.headers["content-type"].startswith("application/json"), api
    # 带着 next 登录完回到原页
    loc = anon.get("/admin/feedback").headers["location"]
    nxt = AUTH.safe_next(__import__("urllib.parse").parse.unquote(loc.split("next=", 1)[1]))
    assert _login(anon, nxt=nxt).headers["location"] == "/admin/feedback"


def test_no_hash_configured_login_unavailable_and_no_bypass(tmp_path, root, monkeypatch, clock):
    app = _make_app(tmp_path, root, monkeypatch, pw_hash="")
    c = _client(app)
    assert c.get("/admin/agents").status_code == 401                    # 老部署：维持原行为，不跳也不放行
    page = c.get("/admin/login")
    assert page.status_code == 503 and 'type="password"' not in page.text
    assert _login(c).status_code == 503 and AUTH.SESSION_COOKIE not in c.cookies
    # 伪造一个「没有哈希时签的」会话也进不来
    assert _client(app, cookies={AUTH.SESSION_COOKIE: "v1.cWl1eWl3dQ.9999999999.00"}).get("/admin/agents/api/list").status_code == 401
    # 门票和哈希都没配：整个后台（含登录页）404
    app2 = _make_app(tmp_path, root, monkeypatch, token="", pw_hash="")
    assert _client(app2).get("/admin/login").status_code == 404
    assert _client(app2).get("/admin/agents").status_code == 404


def test_login_only_deployment_without_ticket(tmp_path, root, monkeypatch, clock):
    app = _make_app(tmp_path, root, monkeypatch, token="")
    c = _client(app)
    assert c.get("/admin").status_code == 307 and c.get("/admin/agents").status_code == 302
    _login(c)
    assert c.get("/admin/agents").status_code == 200


def test_audit_file_is_not_an_agent(app, root):
    _login(_client(app), pw="bad")
    assert (root / AUTH.AUDIT_FILE).is_file()
    from voice_kit import agents as A
    assert "_admin_audit" not in {a.id for a in A.load_all(root)}
    assert "agents/_admin_audit.jsonl" in (REPO / ".gitignore").read_text(encoding="utf-8")


def test_shell_redirects_to_login_on_401_and_has_logout():
    js = (REPO / "voice_kit" / "static" / "vk-shell.js").read_text(encoding="utf-8")
    assert "/admin/login?next=" in js and "/admin/logout" in js and "退出登录" in js
    assert "门票有效" not in js and "门票过期" not in js
    for page in ("admin.html", "memory.html", "feedback.html"):
        assert 'data-vk-user="{{VK_USER}}"' in (REPO / "voice_kit" / page).read_text(encoding="utf-8")


# ---------------------------------------------------------------- 明文默认密码不许进仓库

def _repo_text_files():
    try:
        out = subprocess.run(["git", "ls-files", "-co", "--exclude-standard", "-z"], cwd=str(REPO),
                             capture_output=True, timeout=30, check=True).stdout
        files = [REPO / f for f in out.decode("utf-8").split("\0") if f]
    except (OSError, subprocess.SubprocessError):
        skip = {".git", ".venv", "venv", "__pycache__", "node_modules", ".pytest_cache"}
        files = [p for p in REPO.rglob("*") if p.is_file() and not (skip & set(p.relative_to(REPO).parts))]
    for p in files:
        if not p.is_file() or p.stat().st_size > 2_000_000:
            continue
        b = p.read_bytes()
        if b"\0" in b[:4096]:
            continue
        yield p, b.decode("utf-8", "ignore")


def test_default_password_plaintext_not_in_repo():
    assert hashlib.sha256(b"00000000").hexdigest() != FORBIDDEN_SHA256
    hits, n = [], 0
    for p, text in _repo_text_files():
        n += 1
        for tok in set(re.findall(r"(?<!\d)\d{8}(?!\d)", text)):
            if hashlib.sha256(tok.encode()).hexdigest() == FORBIDDEN_SHA256:
                hits.append(str(p.relative_to(REPO)))
    assert n > 20
    assert not hits, f"默认密码明文出现在：{hits}"
