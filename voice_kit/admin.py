"""Agent 后台：`/admin/agents`。文件的编辑器 + 校验器，不是另一份数据库。

门票：`VOICEKIT_ADMIN_TOKEN`。第一次带 `?k=<token>` 进来会种 cookie，之后页面里的
fetch 不用带。没配 token 时整个后台 404——不存在"默认开放"的后台。

接口全部挂在 /admin/agents/api/*：
    GET  /list                       所有 agent 摘要 + 校验结果数
    GET  /{id}                       六层原文 + 校验 + 评估卡 + 入职四关 + 历史 + 报告
    PUT  /{id}/layer/{name}          写一层，body {text, bump: patch|minor|major|none}
    PUT  /{id}/shells                改壳绑定，body {shells: [...]}；撞壳在这里挡
    POST /{id}/validate              只校验，不写（编辑框里的草稿也能校验）
    POST /new                        新建一个 agent（从模板）
    POST /{id}/test                  用 testkit 跑一套用例，报告落 reports/
    GET  /dict                       平台字典（透镜能填什么、rails 有哪些）
    GET  /shells                     全库壳 → agent 的对照表 + 冲突
    GET  /export[?agent=id]          导出六层文件 tar.gz（服务器上没 git 时靠它带回本地）
"""
from __future__ import annotations

import hmac
import os
from dataclasses import asdict
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse

from . import agents as A

COOKIE = "vk_admin"
_HTML = Path(__file__).parent / "admin.html"


def _root() -> Path:
    return Path(os.environ.get("VOICEKIT_AGENTS_DIR") or Path(__file__).parent.parent / "agents")


def _token() -> str:
    return os.environ.get("VOICEKIT_ADMIN_TOKEN", "").strip()


def _authed(request: Request) -> bool:
    tok = _token()
    if not tok:
        return False
    given = (request.query_params.get("k") or request.cookies.get(COOKIE)
             or (request.headers.get("authorization") or "").removeprefix("Bearer ").strip())
    return bool(given) and hmac.compare_digest(given, tok)


def _guard(request: Request):
    if not _token():
        raise HTTPException(404)
    if not _authed(request):
        raise HTTPException(401, "需要后台门票")


def _agent_or_404(agent_id: str) -> A.Agent:
    d = _root() / agent_id
    if not d.is_dir():
        raise HTTPException(404, f"没有 agent {agent_id}")
    return A.load_agent(d)


def _autocommit(agent_id: str, message: str) -> Optional[dict]:
    """写盘之后顺手提交一笔——只有 VOICEKIT_AGENTS_GIT_COMMIT=1 且 agents/ 真是
    git 工作副本时才做。关着就返回 None，页面照常。

    提交失败**不影响保存**：文件已经写下去了，提交不上只是这次改动没留痕，
    页面会把原因显示出来。把保存也一起失败掉才是真的坏——人会以为没改成，
    再改一遍，于是磁盘上是新的、人以为是旧的。
    """
    if not A.git_env():
        return None
    st = A.git_state(_root())
    if not st["repo"]:
        return {"ok": False, "reason": st["reason"]}
    return A.git_commit(_root(), agent_id, message)


def _detail(ag: A.Agent) -> dict:
    root = _root()
    # 撞壳只有把全库放一起看才判得出来，所以这里要把「别人认领了哪些壳」喂给 validate
    issue_objs = A.validate(ag, other_shells=A.other_shell_owners(root, ag.id))
    issues = [asdict(i) for i in issue_objs]
    return {
        **ag.summary(),
        "onboarding": A.onboarding(root, ag, issues=issue_objs),
        "git": A.git_state(root),
        "layers": {
            "manifest.yaml": (ag.dir / "manifest.yaml").read_text(encoding="utf-8") if (ag.dir / "manifest.yaml").exists() else "",
            "lens.yaml": (ag.dir / "lens.yaml").read_text(encoding="utf-8") if (ag.dir / "lens.yaml").exists() else "",
            **{k: ag.texts.get(k, "") for k in A.TEXT_LAYERS},
        },
        "missing": ag.missing,
        "issues": issues,
        "ok": not any(i["severity"] == "fail" for i in issues),
        "scorecard": A.scorecard(root, ag),
        "history": A.history(root, ag.id),
        "reports": A.reports(root, ag.id)[:10],
        "sessions": A.sessions(root, ag.id, n=20),
        "notes": ag.manifest.get("notes", ""),
    }


def build_router() -> APIRouter:
    r = APIRouter(prefix="/admin/agents")

    @r.get("", response_class=HTMLResponse)
    @r.get("/", response_class=HTMLResponse)
    def page(request: Request):
        if not _token():
            raise HTTPException(404)
        if not _authed(request):
            return HTMLResponse("<meta charset=utf-8><p>需要门票：在地址后面加 <code>?k=&lt;VOICEKIT_ADMIN_TOKEN&gt;</code></p>", 401)
        resp = HTMLResponse(_HTML.read_text(encoding="utf-8"))
        if request.query_params.get("k"):
            resp.set_cookie(COOKIE, request.query_params["k"], httponly=True, samesite="lax", max_age=7 * 86400)
        return resp

    @r.get("/api/dict")
    def dict_(request: Request):
        _guard(request)
        return {"facts": sorted(A.FACT_CATEGORIES), "signals": sorted(A.SIGNAL_DIMS),
                "judgments": sorted(A.JUDGMENT_DIMS), "recap": sorted(A.RECAP_MODES),
                "rails": A.RAILS, "rails_suite": A.RAILS_SUITE, "gate_suite": A.GATE_SUITE,
                "models": A.MODELS, "default_model": A.DEFAULT_MODEL, "read_levels": list(A.READ_LEVELS), "kinds": list(A.KINDS),
                "bands": sorted(A.CHILD_BANDS) + ["parent"] + sorted(A.ADULT_BANDS)}

    @r.get("/api/list")
    def list_(request: Request):
        _guard(request)
        root = _root()
        ags = A.load_all(root)
        owners = A.shell_owners(ags)        # 全库算一次，别在循环里 load_all（N² 且慢）
        out = []
        for ag in ags:
            others = {}
            for sh, ids in owners.items():
                rest = [i for i in ids if i != ag.id]
                if rest:
                    others[sh] = rest[0]
            iss = A.validate(ag, other_shells=others)
            gates = A.onboarding(root, ag, issues=iss)
            out.append({**ag.summary(),
                        "fails": sum(i.severity == "fail" for i in iss),
                        "warns": sum(i.severity == "warn" for i in iss),
                        "onboarding": A.onboarding_summary(gates),
                        "scorecard": A.scorecard(root, ag)})
        return {"root": str(root), "agents": out, "git": A.git_state(root),
                "shells": A.shell_map(root)}

    @r.get("/api/shells")
    def shells_(request: Request):
        _guard(request)
        return A.shell_map(_root())

    @r.get("/api/export")
    def export(request: Request, agent: Optional[str] = None):
        """导出六层文件 tar.gz。

        服务器上 agents/ 不是 git 工作副本时（TASKS.md 记着），这是唯一能把改动
        带回本地提交的路。**只打六层文件**：sessions / reports / users 不进包。
        """
        _guard(request)
        try:
            blob = A.export_bundle(_root(), agent)
        except FileNotFoundError as e:
            raise HTTPException(404, f"没有 agent {e}")
        name = f"agents-{agent or 'all'}-{__import__('datetime').datetime.now().strftime('%Y%m%d-%H%M%S')}.tar.gz"
        return Response(blob, media_type="application/gzip",
                        headers={"Content-Disposition": f'attachment; filename="{name}"'})

    @r.post("/api/new")
    async def new(request: Request):
        _guard(request)
        b = await request.json()
        try:
            A.new_agent(_root(), b["id"], b.get("name") or b["id"], kind=b.get("kind", "config"),
                        rails=b.get("rails", "child_strict"), bands=b.get("bands"),
                        product=b.get("product", ""))
        except (ValueError, FileExistsError, KeyError) as e:
            raise HTTPException(400, f"{type(e).__name__}: {e}")
        return _detail(_agent_or_404(b["id"]))

    # 下面这条是通配。**所有固定路径的 GET 都必须注册在它前面**
    # （/dict /list /shells /export 都在上面）：FastAPI 按声明顺序匹配，
    # 放到它后面的话 "shells" 会先被当成 agent id，返回 404——
    # 看着像「后台没这个接口」，其实是路由被通配吃了。
    @r.get("/api/{agent_id}")
    def get(agent_id: str, request: Request):
        _guard(request)
        return _detail(_agent_or_404(agent_id))

    @r.post("/api/{agent_id}/validate")
    async def validate(agent_id: str, request: Request):
        """校验草稿：body {layers: {name: text}} 覆盖磁盘上的对应层再校验，不写盘。"""
        _guard(request)
        ag = _agent_or_404(agent_id)
        body = await request.json()
        import tempfile, shutil
        with tempfile.TemporaryDirectory() as td:
            d = Path(td) / agent_id
            shutil.copytree(ag.dir, d)
            for name, text in (body.get("layers") or {}).items():
                if name in A.LAYERS:
                    (d / name).write_text(text, encoding="utf-8")
            draft = A.load_agent(d)
            # 草稿里改了 shells 也要能看出撞壳——不然「保存」那一下才报错，白写一遍
            iss = [asdict(i) for i in A.validate(draft, other_shells=A.other_shell_owners(_root(), agent_id))]
        return {"issues": iss, "ok": not any(i["severity"] == "fail" for i in iss)}

    @r.put("/api/{agent_id}/layer/{layer}")
    async def put_layer(agent_id: str, layer: str, request: Request):
        _guard(request)
        _agent_or_404(agent_id)
        body = await request.json()
        text = body.get("text")
        if not isinstance(text, str):
            raise HTTPException(400, "缺 text")
        part = body.get("bump", "patch")
        try:
            res = A.save_layer(_root(), agent_id, layer, text,
                               bump_part=None if part == "none" else part)
        except ValueError as e:
            raise HTTPException(400, str(e))
        commit = _autocommit(agent_id, f"{agent_id}: 后台改 {layer} → v{res.get('version', '')}")
        return {**res, **_detail(_agent_or_404(agent_id)), "commit": commit}

    @r.put("/api/{agent_id}/shells")
    async def put_shells(agent_id: str, request: Request):
        """壳绑定：agent 声明自己服务宿主产品的哪些壳。

        以前只能手写 YAML，而且「一个壳不能被两个 agent 认领」只活在
        tests/test_agent_chat.py 的一条断言里——后台改 manifest 是绕过它的。
        现在 agents.save_shells 会再挡一次：接口是最后一道，前端可以绕，它不行。
        """
        _guard(request)
        _agent_or_404(agent_id)
        body = await request.json()
        shells = body.get("shells")
        if not isinstance(shells, list):
            raise HTTPException(400, "shells 要是一个列表")
        try:
            res = A.save_shells(_root(), agent_id, shells)
        except ValueError as e:
            raise HTTPException(409, str(e))     # 409：跟别人撞了，不是你写错了
        commit = _autocommit(agent_id, f"{agent_id}: 壳绑定改成 {res['shells'] or '（无）'}")
        return {**_detail(_agent_or_404(agent_id)), "commit": commit}

    @r.post("/api/{agent_id}/test")
    async def test(agent_id: str, request: Request):
        """跑 testkit。body {suite, service, key}。同步跑，慢是正常的（打真模型）。"""
        _guard(request)
        ag = _agent_or_404(agent_id)
        b = await request.json()
        from .testkit import load_suite, run_suite, to_markdown
        from .testkit.__main__ import SUITES_DIR
        from .testkit.target import ServiceTarget
        suite_name = b.get("suite") or ""
        p = SUITES_DIR / f"{suite_name}.json"
        if not p.exists():
            raise HTTPException(400, f"没有套件 {suite_name}")
        if not (b.get("service") and b.get("key")):
            raise HTTPException(400, "要 service 和 key")
        su = load_suite(p)
        rep = run_suite(su, ServiceTarget(b["service"], b["key"]))
        ft = rep.first_token_stats()
        saved = {"suite": suite_name, "agent": ag.id, "version": ag.version,
                 "target": rep.target, "n": rep.n, "n_fail": rep.n_fail, "n_warn": rep.n_warn,
                 "ok": rep.ok, "first_token": ft,
                 "at": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
                 "cases": [{"id": c.scenario.id, "failed": c.failed, "reply": c.result.text,
                            "findings": [f"{f.check}: {f.message}" for f in c.findings if f.severity != "skip"]}
                           for c in rep.cases],
                 "markdown": to_markdown(rep)}
        A.save_report(_root(), ag.id, saved)
        return saved

    return r
