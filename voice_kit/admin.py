"""Agent 后台：`/admin/agents`。文件的编辑器 + 校验器，不是另一份数据库。

门票：`VOICEKIT_ADMIN_TOKEN`。第一次带 `?k=<token>` 进来会种 cookie，之后页面里的
fetch 不用带。没配 token 时整个后台 404——不存在"默认开放"的后台。

接口全部挂在 /admin/agents/api/*：
    GET  /list                       所有 agent 摘要 + 校验结果数
    GET  /{id}                       六层原文 + 校验 + 评估卡 + 历史 + 报告
    PUT  /{id}/layer/{name}          写一层，body {text, bump: patch|minor|major|none}
    POST /{id}/validate              只校验，不写（编辑框里的草稿也能校验）
    POST /new                        新建一个 agent（从模板）
    POST /{id}/test                  用 testkit 跑一套用例，报告落 reports/
    GET  /dict                       平台字典（透镜能填什么、rails 有哪些）
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


def _detail(ag: A.Agent) -> dict:
    root = _root()
    issues = [asdict(i) for i in A.validate(ag)]
    return {
        **ag.summary(),
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
                "rails": A.RAILS, "models": A.MODELS, "default_model": A.DEFAULT_MODEL, "read_levels": list(A.READ_LEVELS), "kinds": list(A.KINDS),
                "bands": sorted(A.CHILD_BANDS) + ["parent"] + sorted(A.ADULT_BANDS)}

    @r.get("/api/list")
    def list_(request: Request):
        _guard(request)
        out = []
        for ag in A.load_all(_root()):
            iss = A.validate(ag)
            out.append({**ag.summary(),
                        "fails": sum(i.severity == "fail" for i in iss),
                        "warns": sum(i.severity == "warn" for i in iss),
                        "scorecard": A.scorecard(_root(), ag)})
        return {"root": str(_root()), "agents": out}

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
            iss = [asdict(i) for i in A.validate(draft)]
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
        return {**res, **_detail(_agent_or_404(agent_id))}

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
