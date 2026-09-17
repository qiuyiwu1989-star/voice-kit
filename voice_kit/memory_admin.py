"""用户记忆后台：`/admin/memory`。和 `/admin/agents` 并列，同一张后台门票（admin.py 的 `_guard`）。

spec 014 §10.6（2026-09-16 用户拍板）：Agent 的定义管理 + 用户的记忆管理 = 一个统一后台，
AI 教练、球球老师等产品共用。真源是 `users/<product>/<uid>/` 下的文件，这里只是它的查看器和
受审计的修改入口——不是另一份数据库。

接口全部挂在 /admin/memory/api/*（不和 /admin/agents/api/{agent_id} 的通配撞）：
    GET    /products                                    产品命名空间 + 哪些只读
    GET    /users?product=talk                          用户列表（场数 / 事实数 / 最近 / 用过的 agent）
    GET    /users/{product}/{uid}                       详情：对话、事实、判断、综合复盘、审计
    GET    /users/{product}/{uid}/sessions/{file}       一场的逐句转写 + 该场回顾
    PATCH  /users/{product}/{uid}/facts/{index}         改事实 body {expect:{category,content}, category?, content?}
    DELETE /users/{product}/{uid}/facts/{index}         删事实 body {expect:{category,content}}
    DELETE /users/{product}/{uid}/judgments/{agent}/{index}  删判断 body {expect_at}
    DELETE /users/{product}/{uid}                       删整个用户 body {confirm: <uid>}
    GET    /users/{product}/{uid}/export                导出全部记忆 JSON

红线怎么落在这里：
- 事实 agent 中立：改事实的 body 里带 agent / agent_id 直接 400；也没有「按 agent 写事实」的路由。
- 判断只能删：判断没有 PATCH/PUT 路由（打过来是 405），memory.py 里也没有 update_judgment。
- 儿童产品（READONLY_PRODUCTS，目前 qiuqiu）：改 / 删 / 导出一律 403。
- 每次改 / 删 / 导出写一条审计到 users/<product>/_audit.jsonl。
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable, Iterable, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from . import agents as A
from . import memory as M
from .admin import _guard, _root, enabled, render_page

_HTML = Path(__file__).parent / "memory.html"


def _known_products(extra: Optional[Callable[[], Iterable[str]]]) -> list:
    out = {M.TALK}
    for a in A.load_all(_root()):
        p = (a.manifest.get("audience") or {}).get("product")
        if p:
            out.add(str(p))
    if extra:
        try:
            out |= set(extra())
        except Exception:
            pass
    return list(out)


def _product(product: str) -> str:
    try:
        p = M.safe_product(product)
    except ValueError:
        raise HTTPException(400, "product 不合法")
    if p != product:
        raise HTTPException(400, "product 不合法")
    return p


def _call(fn, *a, **kw):
    """memory.py 的异常 → HTTP 状态码。Conflict 是 ValueError 的子类，要先接。"""
    try:
        return fn(*a, **kw)
    except M.ReadOnlyProduct as e:
        raise HTTPException(403, str(e))
    except M.NotFound as e:
        raise HTTPException(404, str(e))
    except M.Conflict as e:
        raise HTTPException(409, str(e))
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


def build_router(known_products: Optional[Callable[[], Iterable[str]]] = None) -> APIRouter:
    r = APIRouter()

    @r.get("/admin", include_in_schema=False)
    def admin_home(request: Request):
        if not enabled():
            raise HTTPException(404)
        q = f"?{request.url.query}" if request.url.query else ""
        return RedirectResponse("/admin/agents" + q, status_code=307)

    @r.get("/admin/memory", response_class=HTMLResponse)
    @r.get("/admin/memory/", response_class=HTMLResponse)
    def page(request: Request):
        return render_page(_HTML, request)

    api = "/admin/memory/api"

    @r.get(api + "/products")
    def products(request: Request):
        _guard(request)
        root = _root()
        ps = M.list_products(root, known=_known_products(known_products))
        return {"products": ps, "readonly": sorted(M.READONLY_PRODUCTS), "default": M.TALK,
                "fact_categories": sorted(A.FACT_CATEGORIES)}

    @r.get(api + "/users")
    def users(request: Request, product: str = M.TALK):
        _guard(request)
        p = _product(product)
        return {"product": p, "readonly": p in M.READONLY_PRODUCTS, "users": M.list_users(_root(), p)}

    @r.get(api + "/users/{product}/{uid}")
    def detail(product: str, uid: str, request: Request):
        _guard(request)
        p, root = _product(product), _root()
        if not M.user_exists(root, p, uid):
            raise HTTPException(404, f"{p} 下没有用户 {uid}")
        facts = M.load_facts(root, p, uid)
        return {"summary": M.user_summary(root, p, uid), "readonly": p in M.READONLY_PRODUCTS,
                "fact_categories": sorted(A.FACT_CATEGORIES),
                "sessions": M.session_summaries(root, p, uid, n=100000),
                "facts": [{**f, "index": i} for i, f in enumerate(facts)],
                "judgments": M.load_judgments(root, p, uid),
                "reviews": M.load_reviews(root, p, uid, n=100000),
                "audit": M.load_audit(root, p, uid)}

    @r.get(api + "/users/{product}/{uid}/sessions/{file}")
    def session(product: str, uid: str, file: str, request: Request):
        _guard(request)
        return _call(M.load_session, _root(), _product(product), uid, file)

    @r.get(api + "/users/{product}/{uid}/export")
    def export(product: str, uid: str, request: Request):
        _guard(request)
        p = _product(product)
        if p in M.READONLY_PRODUCTS:
            # 儿童数据不从后台整包带出服务器。和「只读」同一个保守默认，等修改/导出流程定了再开。
            raise HTTPException(403, f"{p} 是儿童产品：导出流程还没定，先不开")
        data = _call(M.export_user, _root(), p, uid)
        name = f"memory-{p}-{M.safe_uid(uid)}.json"
        from urllib.parse import quote
        return JSONResponse(data, headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"})

    @r.patch(api + "/users/{product}/{uid}/facts/{index}")
    async def patch_fact(product: str, uid: str, index: int, request: Request):
        _guard(request)
        p = _product(product)
        b = await _body(request)
        if "agent" in b or "agent_id" in b:
            raise HTTPException(400, "事实层 agent 中立：改事实不能带 agent")
        extra = set(b) - {"expect", "category", "content"}
        if extra:
            raise HTTPException(400, f"不认识的字段：{sorted(extra)}")
        f = _call(M.update_fact, _root(), p, uid, index, expect=b.get("expect") or {},
                  category=b.get("category"), content=b.get("content"))
        return {"ok": True, "fact": f}

    @r.delete(api + "/users/{product}/{uid}/facts/{index}")
    async def delete_fact(product: str, uid: str, index: int, request: Request):
        _guard(request)
        p = _product(product)
        b = await _body(request)
        _call(M.delete_fact, _root(), p, uid, index, expect=b.get("expect") or {})
        return {"ok": True}

    @r.delete(api + "/users/{product}/{uid}/judgments/{agent_id}/{index}")
    async def delete_judgment(product: str, uid: str, agent_id: str, index: int, request: Request):
        _guard(request)
        p = _product(product)
        b = await _body(request)
        _call(M.delete_judgment, _root(), p, uid, agent_id, index, expect_at=str(b.get("expect_at") or ""))
        return {"ok": True}

    @r.delete(api + "/users/{product}/{uid}")
    async def delete_user(product: str, uid: str, request: Request):
        _guard(request)
        p = _product(product)
        b = await _body(request)
        counts = _call(M.delete_user, _root(), p, uid, confirm=str(b.get("confirm") or ""))
        return {"ok": True, "deleted": counts}

    return r
