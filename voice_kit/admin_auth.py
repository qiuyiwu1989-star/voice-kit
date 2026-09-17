"""后台账号密码登录：会话 cookie、防爆破、审计、`/admin/login` 与 `/admin/logout`。

2026-09-17 用户拍板：「管理员门票设计的太复杂了，我们有一个登录的密钥就好了」。
门票（VOICEKIT_ADMIN_TOKEN）保留给脚本和自动化；人走这里的用户名 + 密码。

配置（都只进服务器 env，不进仓库）：
    VOICEKIT_ADMIN_USER               用户名，默认 qiuyiwu
    VOICEKIT_ADMIN_PASSWORD_HASH      密码哈希（python -m voice_kit.admin_password 生成）。
                                      没配 = 账号密码登录不可用，不是「放行」
    VOICEKIT_ADMIN_SESSION_SECRET     会话签名密钥。没配时从密码哈希派生——
                                      这样换密码（哈希变了）旧会话自然全部失效

会话 cookie `vk_admin_session` = `v1.<user>.<过期时间戳>.<HMAC>`，HMAC 里还揉进了密码哈希的指纹，
所以即使单独配了 SESSION_SECRET，换密码也会让旧会话失效。

防爆破（内存计数，**单进程**有效）：
  - 同一 IP 连续失败 5 次锁 15 分钟；锁定期间不校验密码，直接回「稍后再试」
  - 全站每分钟失败超过 30 次：所有人的账号密码登录临时拒绝（门票不受影响）
  局限：进程重启计数清零；多 worker / 多机时各算各的；IP 取 X-Real-IP（nginx 用 $remote_addr
  覆盖写入），服务若不经 nginx 直接暴露，这个头可被伪造——那时只剩全局限速兜底。
  全局限速本身也意味着：有人持续乱试时，真管理员也会暂时登不进（用门票仍可进）。

审计：每次登录尝试追加一行到 agents 目录下 `_admin_audit.jsonl`（`_` 开头，load_all 只认目录且跳过 `_`）。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import html as _html
import json
import os
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import parse_qs, quote, urlsplit

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .admin_password import is_valid_hash, verify_password

SESSION_COOKIE = "vk_admin_session"
SESSION_TTL = 7 * 86400
DEFAULT_USER = "qiuyiwu"
DEFAULT_NEXT = "/admin/agents"
AUDIT_FILE = "_admin_audit.jsonl"
MSG_BAD = "用户名或密码不对"
MSG_LOCKED = "用户名或密码不对，稍后再试"


# ---------------------------------------------------------------- 配置

def admin_user() -> str:
    return os.environ.get("VOICEKIT_ADMIN_USER", "").strip() or DEFAULT_USER


def password_hash() -> str:
    h = os.environ.get("VOICEKIT_ADMIN_PASSWORD_HASH", "").strip()
    return h if is_valid_hash(h) else ""


def login_enabled() -> bool:
    return bool(password_hash())


def _secret(pw_hash: str) -> bytes:
    s = os.environ.get("VOICEKIT_ADMIN_SESSION_SECRET", "").strip()
    if s:
        return s.encode("utf-8")
    return hashlib.sha256(b"voicekit-admin-session|" + pw_hash.encode("utf-8")).digest()


# ---------------------------------------------------------------- 会话

def _sign(user_b64: str, exp: int, pw_hash: str) -> str:
    fp = hashlib.sha256(pw_hash.encode("utf-8")).hexdigest()
    return hmac.new(_secret(pw_hash), f"v1|{user_b64}|{exp}|{fp}".encode("utf-8"), hashlib.sha256).hexdigest()


def make_session(user: str, now: float) -> str:
    pw_hash = password_hash()
    if not pw_hash:
        raise RuntimeError("没配 VOICEKIT_ADMIN_PASSWORD_HASH，不能发会话")
    ub = base64.urlsafe_b64encode(user.encode("utf-8")).decode("ascii").rstrip("=")
    exp = int(now) + SESSION_TTL
    return f"v1.{ub}.{exp}.{_sign(ub, exp, pw_hash)}"


def session_user(value: Optional[str], now: float) -> Optional[str]:
    """有效会话 → 用户名；其它（没配哈希、格式坏、签名不对、过期、用户名改了）→ None。"""
    pw_hash = password_hash()
    if not pw_hash or not value:
        return None
    parts = value.split(".")
    if len(parts) != 4 or parts[0] != "v1":
        return None
    _, ub, exp_s, sig = parts
    try:
        exp = int(exp_s)
        user = base64.urlsafe_b64decode(ub + "=" * (-len(ub) % 4)).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None
    if not hmac.compare_digest(sig, _sign(ub, exp, pw_hash)):
        return None
    if exp <= now or user != admin_user():
        return None
    return user


# ---------------------------------------------------------------- 防爆破

class LoginLimiter:
    def __init__(self, clock: Callable[[], float] = time.time, max_fail: int = 5, lock_s: int = 900,
                 global_max: int = 30, global_window: int = 60, max_ips: int = 10000):
        self.clock, self.max_fail, self.lock_s = clock, max_fail, lock_s
        self.global_max, self.global_window, self.max_ips = global_max, global_window, max_ips
        self._ips: dict = {}                 # ip -> [连续失败数, 锁到何时]
        self._global: deque = deque()
        self._lock = threading.Lock()

    def _prune(self, now: float):
        while self._global and self._global[0] <= now - self.global_window:
            self._global.popleft()
        if len(self._ips) > self.max_ips:    # 防止被海量 IP 撑爆内存：先丢没在锁的
            for ip in [k for k, v in self._ips.items() if v[1] <= now]:
                del self._ips[ip]

    def check(self, ip: str) -> Optional[str]:
        """None = 可以试；'locked' = 这个 IP 在锁；'global' = 全站失败太多。"""
        with self._lock:
            now = self.clock()
            self._prune(now)
            st = self._ips.get(ip)
            if st and st[1] > now:
                return "locked"
            if st and st[1] and st[1] <= now:          # 锁到期：清零重新计
                del self._ips[ip]
            if len(self._global) > self.global_max:
                return "global"
            return None

    def fail(self, ip: str):
        with self._lock:
            now = self.clock()
            self._global.append(now)
            st = self._ips.setdefault(ip, [0, 0.0])
            st[0] += 1
            if st[0] >= self.max_fail:
                st[0], st[1] = 0, now + self.lock_s

    def success(self, ip: str):
        with self._lock:
            self._ips.pop(ip, None)


LIMITER = LoginLimiter()


def client_ip(request: Request) -> str:
    return ((request.headers.get("x-real-ip") or "").strip()
            or (request.client.host if request.client else "") or "unknown")


def is_https(request: Request) -> bool:
    return (request.url.scheme == "https"
            or (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip().lower() == "https")


# ---------------------------------------------------------------- next / 审计

def safe_next(nxt: Optional[str]) -> str:
    """只放行站内 /admin/ 路径，防开放重定向（//evil.com、/\\evil、https://…、/admin/login 自身）。"""
    n = (nxt or "").strip()
    if (not n.startswith("/admin/") or n.startswith("//") or "\\" in n
            or any(ord(c) < 0x20 or ord(c) == 0x7f for c in n)):
        return DEFAULT_NEXT
    u = urlsplit(n)
    if u.scheme or u.netloc or u.path.startswith(("/admin/login", "/admin/logout")):
        return DEFAULT_NEXT
    return n


def login_url(request: Request) -> str:
    path = request.url.path + (f"?{request.url.query}" if request.url.query else "")
    return "/admin/login?next=" + quote(path, safe="")


def audit(root: Path, ip: str, user: str, result: str, now: float):
    try:
        root.mkdir(parents=True, exist_ok=True)
        rec = {"at": datetime.fromtimestamp(now).isoformat(timespec="seconds"), "ip": ip,
               "user": (user or "")[:64], "result": result}
        with open(root / AUDIT_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass                                  # 审计写不进去不挡登录，但也不让它把登录搞崩


# ---------------------------------------------------------------- 页面

_CSS = Path(__file__).parent / "static" / "vk-admin.css"

_PAGE = """<!DOCTYPE html>
<html lang="zh-CN" data-theme="dark">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="robots" content="noindex">
<title>登录 · voice-kit 后台</title>
<script>try{if(localStorage.getItem('vk-theme')==='light')document.documentElement.setAttribute('data-theme','light')}catch(e){}</script>
<style>__CSS__
body{overflow:auto}
.vk-login{min-height:100vh;display:grid;place-items:center;padding:24px}
.vk-login .vk-card{width:100%;max-width:360px;padding:28px 26px}
.vk-login h1{font-size:var(--vk-text-lg);margin:0 0 4px}
.vk-login form{display:flex;flex-direction:column;gap:14px;margin-top:20px}
.vk-login .vk-input{height:38px;font-size:var(--vk-text-base)}
.vk-login .vk-btn{height:38px;margin-top:4px}
.vk-login .err{margin:14px 0 0;color:var(--vk-err-ink);background:var(--vk-err-bg);border-radius:var(--vk-radius-sm);padding:8px 10px;font-size:var(--vk-text-sm)}
</style>
</head>
<body>
<main class="vk-login">
  <div class="vk-card">
    <h1>voice-kit 后台</h1>
    <p class="hint">__HINT__</p>
    __BODY__
  </div>
</main>
</body>
</html>"""


def _page(*, error: str = "", user: str = "", nxt: str = DEFAULT_NEXT, form: bool = True,
          hint: str = "用管理员账号登录。", status: int = 200) -> HTMLResponse:
    css = _CSS.read_text(encoding="utf-8") if _CSS.exists() else ""
    e = _html.escape
    body = (f'<p class="err" role="alert">{e(error)}</p>' if error else "")
    if form:
        body += (
            '<form method="post" action="/admin/login">'
            f'<input type="hidden" name="next" value="{e(nxt, quote=True)}">'
            '<div class="vk-field"><label for="u">用户名</label>'
            f'<input class="vk-input" id="u" name="username" autocomplete="username" autocapitalize="off" spellcheck="false" required value="{e(user, quote=True)}"{"" if user else " autofocus"}></div>'
            '<div class="vk-field"><label for="p">密码</label>'
            f'<input class="vk-input" id="p" name="password" type="password" autocomplete="current-password" required{" autofocus" if user else ""}></div>'
            '<button class="vk-btn vk-btn--primary" type="submit">登录</button>'
            '</form>')
    html = _PAGE.replace("__CSS__", css.replace("</", "<\\/")).replace("__HINT__", e(hint)).replace("__BODY__", body)
    return HTMLResponse(html, status, headers={"Cache-Control": "no-store", "X-Frame-Options": "DENY"})


def build_login_router(root: Callable[[], Path], token_enabled: Callable[[], bool]) -> APIRouter:
    r = APIRouter()

    def _clear(resp):
        resp.delete_cookie(SESSION_COOKIE, path="/")
        resp.delete_cookie("vk_admin", path="/")
        return resp

    @r.get("/admin/login", include_in_schema=False)
    def login_page(request: Request, next: Optional[str] = None):
        nxt = safe_next(next)
        if not login_enabled():
            if not token_enabled():
                raise HTTPException(404)
            return _page(form=False, status=503,
                         hint="服务器还没配置管理员密码（VOICEKIT_ADMIN_PASSWORD_HASH），账号密码登录暂不可用。")
        if session_user(request.cookies.get(SESSION_COOKIE), LIMITER.clock()):
            return RedirectResponse(nxt, status_code=302)
        return _page(nxt=nxt)

    @r.post("/admin/login", include_in_schema=False)
    async def login_submit(request: Request):
        if not login_enabled():
            if not token_enabled():
                raise HTTPException(404)
            return _page(form=False, status=503,
                         hint="服务器还没配置管理员密码（VOICEKIT_ADMIN_PASSWORD_HASH），账号密码登录暂不可用。")
        raw = (await request.body())[:8192]
        q = parse_qs(raw.decode("utf-8", "replace"), keep_blank_values=True)
        user = (q.get("username") or [""])[0].strip()
        pw = (q.get("password") or [""])[0]
        nxt = safe_next((q.get("next") or [""])[0])
        ip, now = client_ip(request), LIMITER.clock()

        block = LIMITER.check(ip)
        if block:
            audit(root(), ip, user, block, now)
            return _page(error=MSG_LOCKED, user=user, nxt=nxt, status=429)

        # 用户名错了也照样算一遍哈希：不让响应时间泄露「是用户名错还是密码错」
        pw_ok = verify_password(pw, password_hash())
        user_ok = hmac.compare_digest(user.encode("utf-8"), admin_user().encode("utf-8"))
        if not (pw_ok and user_ok):
            LIMITER.fail(ip)
            audit(root(), ip, user, "fail", now)
            return _page(error=MSG_BAD, user=user, nxt=nxt, status=401)

        LIMITER.success(ip)
        audit(root(), ip, user, "ok", now)
        resp = RedirectResponse(nxt, status_code=303)
        resp.set_cookie(SESSION_COOKIE, make_session(user, now), max_age=SESSION_TTL, path="/",
                        httponly=True, secure=is_https(request), samesite="lax")
        return resp

    @r.get("/admin/logout", include_in_schema=False)
    @r.post("/admin/logout", include_in_schema=False)
    def logout(request: Request):
        return _clear(RedirectResponse("/admin/login", status_code=303))

    return r
