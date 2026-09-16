"""用户侧记忆库 + 沟通记录（spec 014 §9.2 的最小落地）。

    users/<product>/<uid>/
      sessions/<ts>-<agent>-<sid>.json   ① 原始对话（逐句、时间戳、每轮延迟）+ 该场回顾
      facts.json                         ③ 事实：只有一份，agent 中立，平台从对话里抽
      judgments/<agent>.json             ④ 判断：按 agent 分片，各 agent 的回顾落这里
      reviews/<ts>-<method>.json         多场综合复盘
    users/<product>/_audit.jsonl         后台对记忆的每一次改/删/导出

三条硬规则（CLAUDE.md 红线 / spec 014）：
- 事实只有一份且 agent 中立——抽取用平台的提示词，不用 agent 的人设；后台改事实也不带 agent。
- 判断按 agent 分片，互不覆盖；判断是 agent 的原话记录，**只能删，不能改写**（改写等于伪造）。
  所以这里故意没有 update_judgment。
- agent 不查库：平台按透镜装配成「速览卡」塞进 system prompt（`briefing()`）。

## 为什么按产品分命名空间（2026-09-16）

AI 教练的成年用户和球球老师的孩子共用这一个库。uid 是自报名字，两边都有人叫「小明」——
不分层就会把成年人的事实装进孩子的速览卡。product 由调用方的租户决定（`Tenant.memory_product`），
通用 /talk 页（UI 门票）是 `talk`。每个函数都显式收 product，不用全局——哪一层读写的是谁，一眼看得出。

旧的平铺目录 `users/<uid>/`（09-14 到 09-16 的 v0）只可能来自 /talk，所以只在 product=talk 时兼容：
读的时候新路径没有就读旧路径；写之前先把旧目录挪进 `users/talk/<uid>/`，免得一个人分两处。
`migrate_legacy()` 一次挪完全部。

uid 现在来自用户端自报的名字（X-User-Id）。没有登录态，只够内测；正式用要换成产品的登录身份。
"""
from __future__ import annotations

import json
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Iterable, List, Optional

from .agents import FACT_CATEGORIES

TALK = "talk"                                  # 通用 /talk 页（UI 门票）的命名空间
# 后台只读的产品。儿童数据的修改流程（家长知情同意、child_facts 真源在球球 Postgres）还没定，先只读。
READONLY_PRODUCTS = frozenset({"qiuqiu"})

_SAFE = re.compile(r"[^A-Za-z0-9_\-一-鿿]")
_PRODUCT = re.compile(r"[^a-z0-9_\-]")
_AGENT = re.compile(r"^[A-Za-z0-9_\-]{1,60}$")
_TS = re.compile(r"^\d{8}-\d{6}-[^/\\]+\.json$")


class ReadOnlyProduct(PermissionError):
    """这个产品的记忆后台只读。"""


class Conflict(ValueError):
    """要改的那条已经不是你看到的那条了（并发抽取、别人先改了）。"""


class NotFound(LookupError):
    pass


def safe_uid(uid: str) -> str:
    uid = _SAFE.sub("", (uid or "").strip())[:40]
    return uid or ""


def safe_product(product: str) -> str:
    """产品名只许小写字母数字和 _-，且不以 _ 开头（_audit.jsonl 这类平台文件用 _ 前缀）。空的直接炸：
    不许悄悄落到某个默认命名空间——那正是这次要堵的串库。"""
    p = _PRODUCT.sub("", (product or "").strip().lower())[:40].lstrip("_")
    if not p:
        raise ValueError("product 不能为空")
    return p


def users_root(root: Path) -> Path:
    return Path(root) / "users"


def product_dir(root: Path, product: str) -> Path:
    return users_root(root) / safe_product(product)


def user_dir(root: Path, product: str, uid: str) -> Path:
    return product_dir(root, product) / safe_uid(uid)


# ---------------------------------------------------------------- v0 平铺目录的兼容

def legacy_dir(root: Path, uid: str) -> Path:
    return users_root(root) / safe_uid(uid)


def _legacy_parts(d: Path) -> List[Path]:
    """d 下面哪些东西是 v0 平铺布局（users/<uid>/）留下的。

    判据只用新布局里不可能出现的形状：uid 过 safe_uid 不会带「.」，所以产品目录下不会直接有
    facts.json；用户目录下的 sessions/ reviews/ 才装时间戳文件，judgments/ 才装 <agent>.json。
    这样旧 uid 恰好叫 talk/coach、和产品目录同名时，也分得清哪些是旧数据、哪些是新用户。"""
    d = Path(d)
    if not d.is_dir():
        return []
    out = []
    if (d / "facts.json").is_file():
        out.append(d / "facts.json")
    for name in ("sessions", "reviews"):
        x = d / name
        if x.is_dir() and any(c.is_file() and _TS.match(c.name) for c in x.iterdir()):
            out.append(x)
    j = d / "judgments"
    if j.is_dir() and any(c.is_file() and c.suffix == ".json" and c.name != "facts.json" for c in j.iterdir()):
        out.append(j)
    return out


def _read_json(p: Path, default):
    try:
        return json.loads(Path(p).read_text(encoding="utf-8"))
    except Exception:
        return default


def _write_json(p: Path, obj) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(p)          # 原子替换：后台改到一半进程挂了，不留半个 facts.json


def _move_into(src: Path, dst: Path) -> None:
    """把一个旧部件挪到新位置；新位置已有东西就合并，不覆盖。"""
    if not dst.exists():
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        return
    if src.is_file():                               # facts.json：按 (类别, 内容) 并集
        have = _read_json(dst, {}).get("facts", [])
        keys = {(f.get("category"), f.get("content")) for f in have}
        extra = [f for f in _read_json(src, {}).get("facts", []) if (f.get("category"), f.get("content")) not in keys]
        _write_json(dst, {"facts": have + extra})
        src.unlink()
        return
    for c in list(src.iterdir()):
        t = dst / c.name
        if not t.exists():
            shutil.move(str(c), str(t))
        elif src.name == "judgments" and c.suffix == ".json":
            arr = _read_json(t, []) + _read_json(c, [])
            arr.sort(key=lambda r: str((r or {}).get("at", "")))
            _write_json(t, arr[-50:])
            c.unlink()
        else:                                        # 同名时间戳文件 = 同一场，留新位置那份
            c.unlink() if c.is_file() else shutil.rmtree(c)
    src.rmdir()


def migrate_legacy_user(root: Path, uid: str) -> bool:
    """把 users/<uid>/ 挪进 users/talk/<uid>/。v0 只有 /talk 写过记忆，所以一律归 talk。"""
    uid = safe_uid(uid)
    if not uid:
        return False
    old = legacy_dir(root, uid)
    parts = _legacy_parts(old)
    if not parts:
        return False
    new = user_dir(root, TALK, uid)
    new.mkdir(parents=True, exist_ok=True)
    for part in parts:
        _move_into(part, new / part.name)
    try:
        old.rmdir()             # 空了就删；它要是恰好也是某个产品目录、里面还有别人，就留着
    except OSError:
        pass
    return True


def migrate_legacy(root: Path) -> List[str]:
    """一次挪完全部旧目录。幂等：跑第二遍什么都不做。返回挪了哪些 uid。"""
    ur = users_root(root)
    if not ur.is_dir():
        return []
    todo = [d.name for d in ur.iterdir() if d.is_dir() and _legacy_parts(d)]
    return [u for u in todo if migrate_legacy_user(root, u)]


def _read_dir(root: Path, product: str, uid: str) -> Path:
    new = user_dir(root, product, uid)
    if safe_product(product) == TALK and not new.exists() and _legacy_parts(legacy_dir(root, uid)):
        return legacy_dir(root, uid)
    return new


def _write_dir(root: Path, product: str, uid: str) -> Path:
    if not safe_uid(uid):
        raise ValueError("uid 不能为空")
    if safe_product(product) == TALK:
        migrate_legacy_user(root, uid)      # 先挪再写，免得一个人分两处
    d = user_dir(root, product, uid)
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------------------------------------------------------------- ① 原始对话

def save_session(root: Path, product: str, uid: str, rec: dict) -> Path:
    d = _write_dir(root, product, uid) / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{rec.get('agent', 'x')}-{rec.get('session_id', 'x')}.json"
    p.write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
    return p


def load_sessions(root: Path, product: str, uid: str, *, agent: Optional[str] = None, n: int = 200) -> List[dict]:
    d = _read_dir(root, product, uid) / "sessions"
    if not d.is_dir():
        return []
    out = []
    for p in sorted(d.glob("*.json"), reverse=True)[:n]:
        try:
            r = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        if agent and r.get("agent") != agent:
            continue
        r["_file"] = p.name
        out.append(r)
    return out


def load_session(root: Path, product: str, uid: str, file: str) -> dict:
    """按文件名取一场（后台点开看逐句）。文件名只认时间戳形状，挡路径穿越。"""
    if not _TS.match(file or ""):
        raise NotFound("没有这一场")
    p = _read_dir(root, product, uid) / "sessions" / file
    if not p.is_file():
        raise NotFound("没有这一场")
    r = _read_json(p, None)
    if not isinstance(r, dict):
        raise NotFound("这一场读不出来")
    r["_file"] = file
    return r


def session_summaries(root: Path, product: str, uid: str, **kw) -> List[dict]:
    out = []
    for r in load_sessions(root, product, uid, **kw):
        turns = r.get("turns") or []
        out.append({"session_id": r.get("session_id"), "agent": r.get("agent"), "agent_name": r.get("agent_name"),
                    "at": r.get("at"), "turns": len(turns), "file": r.get("_file"),
                    "user_turns": sum(1 for t in turns if t.get("role") != "bot"),
                    "opening": next((t.get("text", "")[:60] for t in turns if t.get("role") != "bot"), ""),
                    "review": (r.get("review") or "")[:160]})
    return out


# ---------------------------------------------------------------- ③ 事实（agent 中立）

def load_facts(root: Path, product: str, uid: str) -> List[dict]:
    p = _read_dir(root, product, uid) / "facts.json"
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text(encoding="utf-8")).get("facts", [])
    except Exception:
        return []


def merge_facts(root: Path, product: str, uid: str, new: Iterable[dict], *, source_session: str = "") -> List[dict]:
    """去重合并：同类别同内容只留一条；再次出现就把 seen 加一（可信度分层的雏形：说过两次 ≠ 说过一次）。"""
    d = _write_dir(root, product, uid)
    facts = load_facts(root, product, uid)
    idx = {(f.get("category"), f.get("content")): f for f in facts}
    for f in new:
        cat = f.get("category"); content = (f.get("content") or "").strip()
        if cat not in FACT_CATEGORIES or not content:
            continue
        key = (cat, content)
        if key in idx:
            idx[key]["seen"] = int(idx[key].get("seen", 1)) + 1
            idx[key]["last_at"] = datetime.now().strftime("%Y-%m-%d")
            if f.get("quote") and not idx[key].get("quote"):
                idx[key]["quote"] = f["quote"]
        else:
            # 故意不存 f 里的 agent 之类字段：事实不属于任何 agent
            row = {"category": cat, "content": content, "quote": (f.get("quote") or "")[:120],
                   "seen": 1, "first_at": datetime.now().strftime("%Y-%m-%d"),
                   "last_at": datetime.now().strftime("%Y-%m-%d"), "session": source_session}
            facts.append(row); idx[key] = row
    _write_json(d / "facts.json", {"facts": facts})
    return facts


EXTRACT_PROMPT = (
    "你是平台的事实抽取器，不是任何一个角色。从下面这场对话里抽出关于「对方」（用户）的事实，"
    "只抽对方自己说出来的，不抽 agent 说的，不推断、不评价、不诊断。\n"
    "每条：category ∈ {event 发生过的事, preference 明确的好恶, constraint 现实约束（时间/预算/规定）, "
    "relation 关系（家人/同事/客户）, attempt 试过什么}，content 一句话（第三人称，≤30 字），quote 原话片段（≤40 字）。\n"
    "最多 8 条。没有就返回空数组。只输出 JSON 数组，不要别的字。"
)


def extract_facts(llm, turns: List[dict], *, model: Optional[str] = None) -> List[dict]:
    """调平台提示词抽事实。模型不听话（输出非 JSON）就当没抽到——宁缺毋滥。"""
    transcript = "\n".join(f"{'agent' if t.get('role') == 'bot' else '对方'}：{t.get('text', '')}" for t in turns if t.get("text"))
    if not transcript.strip():
        return []
    try:
        raw = "".join(llm.stream([{"role": "user", "content": transcript}], system=EXTRACT_PROMPT,
                                 model=model, thinking=False, max_tokens=800))
    except Exception:
        return []
    m = re.search(r"\[.*\]", raw, re.S)
    if not m:
        return []
    try:
        arr = json.loads(m.group(0))
    except Exception:
        return []
    return [f for f in arr if isinstance(f, dict)]



# ---------------------------------------------------------------- ④ 判断（按 agent 分片）

def save_judgment(root: Path, product: str, uid: str, agent_id: str, rec: dict) -> None:
    if not _AGENT.match(agent_id or ""):
        raise ValueError(f"agent id 不合法：{agent_id!r}")
    d = _write_dir(root, product, uid) / "judgments"; d.mkdir(parents=True, exist_ok=True)
    p = d / f"{agent_id}.json"
    arr = _read_json(p, []) if p.exists() else []
    if not isinstance(arr, list):
        arr = []
    arr.append(rec)
    _write_json(p, arr[-50:])


def load_judgments(root: Path, product: str, uid: str, agent_id: Optional[str] = None) -> dict:
    d = _read_dir(root, product, uid) / "judgments"
    if not d.is_dir():
        return {}
    out = {}
    for p in sorted(d.glob("*.json")):
        if agent_id and p.stem != agent_id:
            continue
        try:
            out[p.stem] = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
    return out


# ---------------------------------------------------------------- 速览卡：按透镜装配

def briefing(root: Path, product: str, uid: str, agent_id: str, lens: dict, *, agent_name: str = "") -> str:
    """会话开始时给 agent 的「我记得的他」。读什么由透镜决定；agent 自己不查库。
    只读本产品命名空间——教练那边同名的成年人，不会进孩子的速览卡。"""
    uid = safe_uid(uid)
    if not uid:
        return ""
    rd = (lens or {}).get("read") or {}
    want = set(rd.get("facts") or [])
    facts = [f for f in load_facts(root, product, uid) if not want or f.get("category") in want]
    facts.sort(key=lambda f: (-int(f.get("seen", 1)), f.get("last_at", "")))
    sessions = load_sessions(root, product, uid, n=20)
    mine = [s for s in sessions if s.get("agent") == agent_id]
    lines = [f"# 关于对方（{uid}）——你脑子里的记忆，自然地用，不要提「记录」「档案」这类词",
             f"- 你们聊过 {len(mine)} 次；对方在平台上一共聊过 {len(sessions)} 次"]
    if facts:
        lines.append("## 你记得的事实")
        for f in facts[:10]:
            q = f"（他说：「{f['quote']}」）" if f.get("quote") else ""
            lines.append(f"- [{f['category']}] {f['content']}{q}" + ("（说过不止一次）" if int(f.get("seen", 1)) > 1 else ""))
    if rd.get("recap", "none") != "none" and mine:
        last = mine[0]
        if last.get("review"):
            lines.append(f"## 上次（{last.get('at', '')[:10]}）你的回顾")
            lines.append(last["review"][:400])
    if rd.get("judgments", "own") in ("own", "all"):
        js = load_judgments(root, product, uid, None if rd.get("judgments") == "all" else agent_id)
        for aid, arr in js.items():
            if arr:
                lines.append(f"## {'你' if aid == agent_id else aid}上次的判断")
                lines.append((arr[-1].get("text") or "")[:300])
    return "\n".join(lines) if len(lines) > 2 else ""


# ---------------------------------------------------------------- 多场综合复盘（方法可选）

REVIEW_METHODS = {
    "agent": "用这个 agent 的方法论综合看几场：对方要什么变了没有、方法论走到哪一步、反复没落地的是哪句、下一次先做什么。",
    "timeline": "按时间顺序看变化：每场一行「日期 · 对方的状态/诉求 · 结果」，最后一段说趋势——在变好、原地、还是在退。不评价人。",
    "themes": "按主题归纳：把几场里反复出现的主题列出来（最多 5 个），每个主题下引用 1–2 句原话，最后说哪个主题最该先处理。",
}


def multi_review(llm, sessions: List[dict], *, method: str = "timeline",
                 agent_system_prompt: str = "", model: Optional[str] = None) -> str:
    if not sessions:
        return ""
    method = method if method in REVIEW_METHODS else "timeline"
    blocks = []
    for s in reversed(sessions):        # 旧 → 新
        turns = "\n".join(f"  [{t.get('t', '')}] {'agent' if t.get('role') == 'bot' else '对方'}：{t.get('text', '')}"
                          for t in (s.get("turns") or []) if t.get("text"))
        blocks.append(f"## {s.get('at', '')[:16]} · 和 {s.get('agent_name') or s.get('agent')}\n{turns}\n" +
                      (f"当场回顾：{s['review'][:300]}\n" if s.get("review") else ""))
    system = (agent_system_prompt + "\n\n" if method == "agent" and agent_system_prompt else
              "你是平台的复盘助手，不代表任何一个角色。口语、短句、引用原话、不打分、不用数字评价人。\n\n")
    system += "# 现在做的是多场综合复盘\n" + REVIEW_METHODS[method]
    return "".join(llm.stream([{"role": "user", "content": "\n\n".join(blocks)[-12000:]}],
                              system=system, model=model, thinking=False, max_tokens=900))




def save_review(root: Path, product: str, uid: str, rec: dict) -> Path:
    d = _write_dir(root, product, uid) / "reviews"; d.mkdir(parents=True, exist_ok=True)
    p = d / f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{rec.get('method', 'x')}.json"
    p.write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
    return p


def load_reviews(root: Path, product: str, uid: str, *, n: int = 20) -> List[dict]:
    d = _read_dir(root, product, uid) / "reviews"
    if not d.is_dir():
        return []
    out = []
    for p in sorted(d.glob("*.json"), reverse=True)[:n]:
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except Exception:
            continue
    return out


# ================================================================ 后台：看、改、删、导出、审计
# 只给 /admin/memory 用。所有改/删都经 check_writable + 写审计；判断只有删，没有改。

def check_writable(product: str) -> None:
    if safe_product(product) in READONLY_PRODUCTS:
        raise ReadOnlyProduct(f"{safe_product(product)} 是儿童产品：修改流程还没定，后台先只读")


def user_exists(root: Path, product: str, uid: str) -> bool:
    if not safe_uid(uid):
        return False
    d = _read_dir(root, product, uid)
    if d == legacy_dir(root, uid) and safe_product(product) == TALK and not user_dir(root, product, uid).exists():
        return bool(_legacy_parts(d))
    return d.is_dir()


def _require_user(root: Path, product: str, uid: str) -> str:
    uid = safe_uid(uid)
    if not user_exists(root, product, uid):
        raise NotFound(f"{safe_product(product)} 下没有用户 {uid}")
    return uid


def list_products(root: Path, known: Iterable[str] = ()) -> List[str]:
    """磁盘上有的产品命名空间 ∪ 已知产品（agent manifest / 租户里声明的）。"""
    out = set()
    for k in known:
        try:
            out.add(safe_product(k))
        except ValueError:
            pass
    ur = users_root(root)
    if ur.is_dir():
        for d in ur.iterdir():
            if not d.is_dir():
                continue
            parts = set(_legacy_parts(d))
            if parts:
                out.add(TALK)
            if d.name.startswith("_") or _PRODUCT.sub("", d.name) != d.name:
                continue                    # 中文名之类只可能是旧 uid 目录，不是产品
            if (d / "_audit.jsonl").exists() or any(c.is_dir() and c not in parts for c in d.iterdir()):
                out.add(d.name)
    return sorted(out)


def user_summary(root: Path, product: str, uid: str) -> dict:
    sessions = load_sessions(root, product, uid, n=100000)
    facts = load_facts(root, product, uid)
    js = load_judgments(root, product, uid)
    reviews = load_reviews(root, product, uid, n=100000)
    agents = {s.get("agent") for s in sessions if s.get("agent")} | {k for k, v in js.items() if v}
    times = [s.get("at") for s in sessions] + [r.get("at") for r in reviews] + \
            [j.get("at") for arr in js.values() if isinstance(arr, list) for j in arr if isinstance(j, dict)] + \
            [f.get("last_at") for f in facts]
    times = [t for t in times if isinstance(t, str) and t]
    return {"uid": safe_uid(uid), "product": safe_product(product), "sessions": len(sessions), "facts": len(facts),
            "judgments": sum(len(v) for v in js.values() if isinstance(v, list)), "reviews": len(reviews),
            "agents": sorted(agents), "last_at": max(times) if times else "",
            "legacy": _read_dir(root, product, uid) == legacy_dir(root, uid) and not user_dir(root, product, uid).exists()}


def list_users(root: Path, product: str) -> List[dict]:
    pd = product_dir(root, product)
    uids = set()
    if pd.is_dir():
        parts = set(_legacy_parts(pd))
        uids |= {c.name for c in pd.iterdir() if c.is_dir() and c not in parts and safe_uid(c.name) == c.name}
    if safe_product(product) == TALK and users_root(root).is_dir():
        uids |= {d.name for d in users_root(root).iterdir() if d.is_dir() and _legacy_parts(d)}
    rows = [user_summary(root, product, u) for u in uids]
    rows.sort(key=lambda r: r["last_at"], reverse=True)
    return rows


# ---------------------------------------------------------------- 审计

def _clip(s, n=80) -> str:
    s = str(s or "")
    return s if len(s) <= n else s[:n] + "…"


def audit(root: Path, product: str, *, action: str, uid: str, target: str = "",
          before=None, after=None, actor: str = "admin") -> dict:
    rec = {"at": datetime.now().isoformat(timespec="seconds"), "actor": actor, "action": action,
           "product": safe_product(product), "uid": safe_uid(uid), "target": target,
           "before": before, "after": after}
    p = product_dir(root, product) / "_audit.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return rec


def load_audit(root: Path, product: str, uid: Optional[str] = None, *, n: int = 500) -> List[dict]:
    p = product_dir(root, product) / "_audit.jsonl"
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        try:
            r = json.loads(line)
        except Exception:
            continue
        if uid is None or r.get("uid") == safe_uid(uid):
            out.append(r)
    return list(reversed(out))[:n]


# ---------------------------------------------------------------- 事实：改 / 删（不带 agent）

def _fact_brief(f: dict) -> dict:
    return {"category": f.get("category"), "content": _clip(f.get("content"))}


def _pick_fact(facts: List[dict], index: int, expect: dict) -> dict:
    if not isinstance(index, int) or not 0 <= index < len(facts):
        raise NotFound("没有这条事实")
    f = facts[index]
    exp = expect or {}
    if (f.get("category"), f.get("content")) != (exp.get("category"), exp.get("content")):
        # 页面打开之后又抽进来新事实、或者别人先改了——按位置改会改错条
        raise Conflict("这条事实已经变了，刷新再改")
    return f


def update_fact(root: Path, product: str, uid: str, index: int, *, expect: dict,
                category: Optional[str] = None, content: Optional[str] = None, actor: str = "admin") -> dict:
    """改一条事实的内容/类别。签名里没有 agent——事实层 agent 中立，谁都不能以某个 agent 的名义写。"""
    check_writable(product)
    uid = _require_user(root, product, uid)
    facts = load_facts(root, product, uid)
    f = _pick_fact(facts, index, expect)
    new_cat = f.get("category") if category is None else category
    new_content = f.get("content") if content is None else (content or "").strip()
    if new_cat not in FACT_CATEGORIES:
        raise ValueError(f"类别只能是 {sorted(FACT_CATEGORIES)}")
    if not new_content:
        raise ValueError("内容不能为空；要删就用删除")
    if any(i != index and (g.get("category"), g.get("content")) == (new_cat, new_content) for i, g in enumerate(facts)):
        raise Conflict("已经有一条一模一样的事实了")
    before = _fact_brief(f)
    f["category"], f["content"] = new_cat, new_content
    f["edited_at"] = datetime.now().isoformat(timespec="seconds")
    d = _write_dir(root, product, uid)
    _write_json(d / "facts.json", {"facts": facts})
    audit(root, product, action="fact.update", uid=uid, target=f"facts[{index}]",
          before=before, after=_fact_brief(f), actor=actor)
    return f


def delete_fact(root: Path, product: str, uid: str, index: int, *, expect: dict, actor: str = "admin") -> None:
    check_writable(product)
    uid = _require_user(root, product, uid)
    facts = load_facts(root, product, uid)
    f = _pick_fact(facts, index, expect)
    facts.pop(index)
    d = _write_dir(root, product, uid)
    _write_json(d / "facts.json", {"facts": facts})
    audit(root, product, action="fact.delete", uid=uid, target=f"facts[{index}]", before=_fact_brief(f), actor=actor)


# ---------------------------------------------------------------- 判断：只能删

def delete_judgment(root: Path, product: str, uid: str, agent_id: str, index: int, *,
                    expect_at: str, actor: str = "admin") -> None:
    """删一条判断。**没有 update_judgment**：判断是 agent 当时的原话，后台改写等于替 agent 伪造读法。"""
    check_writable(product)
    uid = _require_user(root, product, uid)
    if not _AGENT.match(agent_id or ""):
        raise NotFound("没有这个 agent 的判断")
    arr = load_judgments(root, product, uid, agent_id).get(agent_id)
    if not isinstance(arr, list) or not isinstance(index, int) or not 0 <= index < len(arr):
        raise NotFound("没有这条判断")
    j = arr[index] if isinstance(arr[index], dict) else {}
    if str(j.get("at", "")) != str(expect_at or ""):
        raise Conflict("这条判断已经变了，刷新再删")
    arr.pop(index)
    p = _write_dir(root, product, uid) / "judgments" / f"{agent_id}.json"
    if arr:
        _write_json(p, arr)
    elif p.exists():
        p.unlink()
    audit(root, product, action="judgment.delete", uid=uid, target=f"judgments/{agent_id}[{index}]",
          before={"agent": agent_id, "at": j.get("at"), "session_id": j.get("session_id"), "text": _clip(j.get("text"))},
          actor=actor)


# ---------------------------------------------------------------- 整个用户：删 / 导出

def delete_user(root: Path, product: str, uid: str, *, confirm: str, actor: str = "admin") -> dict:
    """删掉这个用户在本产品下的全部记忆。confirm 必须一字不差地等于 uid——手滑点不掉一个人。
    审计里只留计数，不留内容：删了就是删了。"""
    check_writable(product)
    uid = safe_uid(uid)
    if not uid or confirm != uid:
        raise ValueError("确认用的 uid 不对：要一字不差地输入这个用户的 uid")
    uid = _require_user(root, product, uid)
    s = user_summary(root, product, uid)
    new = user_dir(root, product, uid)
    if new.is_dir():
        shutil.rmtree(new)
    if safe_product(product) == TALK:           # 旧布局里的那份也一起删，不然删完又从兼容读里冒出来
        old = legacy_dir(root, uid)
        for part in _legacy_parts(old):
            part.unlink() if part.is_file() else shutil.rmtree(part)
        try:
            old.rmdir()
        except OSError:
            pass
    counts = {k: s[k] for k in ("sessions", "facts", "judgments", "reviews")}
    audit(root, product, action="user.delete", uid=uid, target="user", before=counts, actor=actor)
    return counts


def export_user(root: Path, product: str, uid: str, *, actor: str = "admin") -> dict:
    """这个用户在本产品下的全部记忆，原样。导出本身也记审计——数据出了服务器，要有痕迹。"""
    uid = _require_user(root, product, uid)
    data = {"product": safe_product(product), "uid": uid,
            "exported_at": datetime.now().isoformat(timespec="seconds"),
            "sessions": load_sessions(root, product, uid, n=100000),
            "facts": load_facts(root, product, uid),
            "judgments": load_judgments(root, product, uid),
            "reviews": load_reviews(root, product, uid, n=100000)}
    audit(root, product, action="user.export", uid=uid, target="user",
          after={k: len(data[k]) for k in ("sessions", "facts", "reviews")}, actor=actor)
    data["audit"] = load_audit(root, product, uid)
    return data
