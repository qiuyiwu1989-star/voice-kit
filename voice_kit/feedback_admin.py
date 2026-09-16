"""数据回流后台：`/admin/feedback`。和 `/admin/agents`、`/admin/memory` 同一张门票（admin.py 的 `_guard`）。

真源是 `agents/_feedback/<agent_id>/` 下的文件 + agent 自己的六层文件；这里是查看器和受审计的操作入口。

接口全部挂在 /admin/feedback/api/*：
    GET   /agents                                    左侧列表：近 7 天回合数、有问题占比、版本、待审提案
    GET   /agents/{id}?days=7                        趋势（按天）+ 分析列表 + 提案列表 + 用例集 + 版本历史
    GET   /agents/{id}/turns?days=7&flag=            有问题的回合（带前后一轮）
    POST  /agents/{id}/analyze {days}                开始分析（调模型；没模型就只出统计）
    GET   /agents/{id}/analyses/{aid}
    POST  /agents/{id}/propose {analysis_id}         出提案（调模型）
    GET   /agents/{id}/proposals/{pid}               提案 + 逐行 diff + 发布门槛
    PUT   /agents/{id}/proposals/{pid} {files}       人改提案文本（改完要重评）
    POST  /agents/{id}/proposals/{pid}/evaluate      旧版 vs 提案版（调模型，慢）
    POST  /agents/{id}/proposals/{pid}/publish {force, confirm, note}
    POST  /agents/{id}/proposals/{pid}/rollback {note}
    POST  /agents/{id}/cases {turn_ids}              加入用例集
    PATCH /agents/{id}/cases/{cid} {enabled|expect}  勾选 / 改期望
    DELETE /agents/{id}/cases/{cid}

儿童档位 agent：列表里显示「不采集」，其余接口 403 并说明原因。
"""
from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Callable, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from . import agents as A
from . import feedback as FB
from .admin import _guard, _root, render_page

_HTML = Path(__file__).parent / "feedback.html"


def _agent(agent_id: str) -> A.Agent:
    if agent_id.startswith(("_", ".")):
        raise HTTPException(404, f"没有 agent {agent_id}")
    d = _root() / agent_id
    if not d.is_dir():
        raise HTTPException(404, f"没有 agent {agent_id}")
    return A.load_agent(d)


def _adult(ag: A.Agent) -> A.Agent:
    why = FB.child_reason(ag)
    if why:
        raise HTTPException(403, why)
    return ag


def _call(fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except FB.NotCollected as e:
        raise HTTPException(403, str(e))
    except FB.GateBlocked as e:
        raise HTTPException(409, {"message": "没达到发布门槛", "reasons": e.reasons})
    except FB.Stale as e:
        raise HTTPException(409, str(e))
    except FileNotFoundError as e:
        raise HTTPException(404, f"找不到 {e}")
    except ValueError as e:
        raise HTTPException(400, str(e))


async def _body(request: Request) -> dict:
    try:
        b = await request.json()
    except Exception:
        b = {}
    if not isinstance(b, dict):
        raise HTTPException(400, "body 要是 JSON 对象")
    return b


def build_router(llm_factory: Optional[Callable[[], object]] = None) -> APIRouter:
    r = APIRouter()

    def llm():
        m = llm_factory() if llm_factory else None
        if m is None:
            raise HTTPException(503, "服务端没有配置模型，这一步做不了")
        return m

    @r.get("/admin/feedback", response_class=HTMLResponse)
    @r.get("/admin/feedback/", response_class=HTMLResponse)
    def page(request: Request):
        return render_page(_HTML, request)

    api = "/admin/feedback/api"

    @r.get(api + "/agents")
    def agents(request: Request, days: int = 7):
        _guard(request)
        root = _root()
        return {"enabled": FB.enabled(), "days": days,
                "agents": [FB.overview(root, a, days) for a in A.load_all(root)],
                "flags": {c: {"label": l, "severity": s} for c, (l, s) in FB.FLAGS.items()}}

    @r.get(api + "/agents/{agent_id}")
    def detail(agent_id: str, request: Request, days: int = 7):
        _guard(request)
        root, ag = _root(), _agent(agent_id)
        why = FB.child_reason(ag)
        base = {**ag.summary(), "collect": not why, "why": why, "enabled": FB.enabled(),
                "max_chars": FB.max_chars(ag), "rails_suite": A.RAILS_SUITE.get(ag.rails),
                "git": A.git_state(root), "history": A.history(root, ag.id)}
        if why:
            return base
        since, until = FB.window(days)
        ann = FB.annotate(FB.read_turns(root, ag.id, since, until), ag)
        changelog = ag.dir / "CHANGELOG.md"
        return {**base, "stats": FB.stats(ann, since, until),
                "analyses": FB.list_analyses(root, ag.id),
                "proposals": [{k: p.get(k) for k in ("id", "created_at", "status", "base_version", "published_version",
                                                     "analysis_id", "rev", "forced", "problems")}
                              | {"files": sorted(p.get("files") or {}), "evaluated": bool(p.get("evaluation")),
                                 "gate": FB.publish_gate(p)}
                              for p in FB.list_proposals(root, ag.id)],
                "cases": FB.load_cases(root, ag.id),
                "audit": FB.load_audit(root, ag.id),
                "changelog": changelog.read_text(encoding="utf-8") if changelog.exists() else ""}

    @r.get(api + "/agents/{agent_id}/turns")
    def turns(agent_id: str, request: Request, days: int = 7, flag: str = "", weak: bool = False):
        _guard(request)
        ag = _adult(_agent(agent_id))
        if flag and flag not in FB.FLAGS:
            raise HTTPException(400, f"不认识的 flag {flag}")
        return {"turns": FB.flagged_turns(_root(), ag, days=days, flag=flag, include_weak=weak)}

    @r.post(api + "/agents/{agent_id}/analyze")
    async def analyze(agent_id: str, request: Request):
        _guard(request)
        ag = _adult(_agent(agent_id))
        b = await _body(request)
        since, until = FB.window(int(b.get("days") or 7))
        m = llm_factory() if llm_factory else None       # 没模型也能分析：降级为只给统计
        return _call(FB.analyze, _root(), ag, since=since, until=until, llm=m)

    @r.get(api + "/agents/{agent_id}/analyses/{analysis_id}")
    def analysis(agent_id: str, analysis_id: str, request: Request):
        _guard(request)
        ag = _adult(_agent(agent_id))
        return _call(FB.load_analysis, _root(), ag.id, analysis_id)

    @r.post(api + "/agents/{agent_id}/propose")
    async def propose(agent_id: str, request: Request):
        _guard(request)
        ag = _adult(_agent(agent_id))
        b = await _body(request)
        an = _call(FB.load_analysis, _root(), ag.id, str(b.get("analysis_id") or ""))
        prop = _call(FB.propose, _root(), ag, an, llm=llm())
        return FB.proposal_view(prop)

    @r.get(api + "/agents/{agent_id}/proposals/{pid}")
    def proposal(agent_id: str, pid: str, request: Request):
        _guard(request)
        ag = _adult(_agent(agent_id))
        return FB.proposal_view(_call(FB.load_proposal, _root(), ag.id, pid))

    @r.put(api + "/agents/{agent_id}/proposals/{pid}")
    async def edit(agent_id: str, pid: str, request: Request):
        _guard(request)
        ag = _adult(_agent(agent_id))
        b = await _body(request)
        files = b.get("files")
        if not isinstance(files, dict) or not all(isinstance(v, str) for v in files.values()):
            raise HTTPException(400, "files 要是 {文件名: 全文}")
        return FB.proposal_view(_call(FB.edit_proposal, _root(), ag, pid, files))

    @r.post(api + "/agents/{agent_id}/proposals/{pid}/evaluate")
    def evaluate(agent_id: str, pid: str, request: Request):
        _guard(request)
        ag = _adult(_agent(agent_id))
        return FB.proposal_view(_call(FB.evaluate, _root(), ag, pid, llm=llm()))

    @r.post(api + "/agents/{agent_id}/proposals/{pid}/publish")
    async def publish(agent_id: str, pid: str, request: Request):
        _guard(request)
        ag = _adult(_agent(agent_id))
        b = await _body(request)
        force = bool(b.get("force"))
        if force and b.get("confirm") != "我确认要发布":
            raise HTTPException(400, "强制发布要勾「我确认要发布」")
        res = _call(FB.publish, _root(), ag, pid, force=force, note=str(b.get("note") or "")[:500])
        return {**FB.proposal_view(res), "commit": res.get("commit")}

    @r.post(api + "/agents/{agent_id}/proposals/{pid}/rollback")
    async def rollback(agent_id: str, pid: str, request: Request):
        _guard(request)
        ag = _agent(agent_id)
        b = await _body(request)
        res = _call(FB.rollback, _root(), ag, pid, note=str(b.get("note") or "")[:500])
        return {**FB.proposal_view(res), "commit": res.get("commit")}

    @r.post(api + "/agents/{agent_id}/cases")
    async def add_cases(agent_id: str, request: Request):
        _guard(request)
        ag = _adult(_agent(agent_id))
        b = await _body(request)
        ids = [str(i) for i in (b.get("turn_ids") or [])]
        if not ids:
            raise HTTPException(400, "缺 turn_ids")
        return {"cases": _call(FB.add_cases, _root(), ag, ids)}

    @r.patch(api + "/agents/{agent_id}/cases/{case_id}")
    async def patch_case(agent_id: str, case_id: str, request: Request):
        _guard(request)
        ag = _adult(_agent(agent_id))
        b = await _body(request)
        return {"cases": _call(FB.set_case, _root(), ag.id, case_id,
                               enabled=b.get("enabled"), expect=b.get("expect"))}

    @r.delete(api + "/agents/{agent_id}/cases/{case_id}")
    def delete_case(agent_id: str, case_id: str, request: Request):
        _guard(request)
        ag = _adult(_agent(agent_id))
        return {"cases": _call(FB.set_case, _root(), ag.id, case_id, delete=True)}

    return r
