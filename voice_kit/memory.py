"""用户侧记忆库 + 沟通记录（spec 014 §9.2 的最小落地）。

    users/<uid>/
      sessions/<ts>-<agent>-<sid>.json   ① 原始对话（逐句、时间戳、每轮延迟）+ 该场回顾
      facts.json                         ③ 事实：只有一份，agent 中立，平台从对话里抽
      judgments/<agent>.json             ④ 判断：按 agent 分片，各 agent 的回顾落这里
      reviews/<ts>-<method>.json         多场综合复盘

三条硬规则（CLAUDE.md 红线 / spec 014）：
- 事实只有一份且 agent 中立——抽取用平台的提示词，不用 agent 的人设。
- 判断按 agent 分片，互不覆盖。
- agent 不查库：平台按透镜装配成「速览卡」塞进 system prompt（`briefing()`）。

uid 现在来自用户端自报的名字（X-User-Id）。没有登录态，只够内测；正式用要换成产品的登录身份。
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Iterable, List, Optional

from .agents import FACT_CATEGORIES

_SAFE = re.compile(r"[^A-Za-z0-9_\-一-鿿]")


def safe_uid(uid: str) -> str:
    uid = _SAFE.sub("", (uid or "").strip())[:40]
    return uid or ""


def user_dir(root: Path, uid: str) -> Path:
    return Path(root) / "users" / safe_uid(uid)


# ---------------------------------------------------------------- ① 原始对话

def save_session(root: Path, uid: str, rec: dict) -> Path:
    d = user_dir(root, uid) / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{rec.get('agent', 'x')}-{rec.get('session_id', 'x')}.json"
    p.write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
    return p


def load_sessions(root: Path, uid: str, *, agent: Optional[str] = None, n: int = 200) -> List[dict]:
    d = user_dir(root, uid) / "sessions"
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


def session_summaries(root: Path, uid: str, **kw) -> List[dict]:
    out = []
    for r in load_sessions(root, uid, **kw):
        turns = r.get("turns") or []
        out.append({"session_id": r.get("session_id"), "agent": r.get("agent"), "agent_name": r.get("agent_name"),
                    "at": r.get("at"), "turns": len(turns),
                    "user_turns": sum(1 for t in turns if t.get("role") != "bot"),
                    "opening": next((t.get("text", "")[:60] for t in turns if t.get("role") != "bot"), ""),
                    "review": (r.get("review") or "")[:160]})
    return out


# ---------------------------------------------------------------- ③ 事实（agent 中立）

def load_facts(root: Path, uid: str) -> List[dict]:
    p = user_dir(root, uid) / "facts.json"
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text(encoding="utf-8")).get("facts", [])
    except Exception:
        return []


def merge_facts(root: Path, uid: str, new: Iterable[dict], *, source_session: str = "") -> List[dict]:
    """去重合并：同类别同内容只留一条；再次出现就把 seen 加一（可信度分层的雏形：说过两次 ≠ 说过一次）。"""
    d = user_dir(root, uid); d.mkdir(parents=True, exist_ok=True)
    facts = load_facts(root, uid)
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
            row = {"category": cat, "content": content, "quote": (f.get("quote") or "")[:120],
                   "seen": 1, "first_at": datetime.now().strftime("%Y-%m-%d"),
                   "last_at": datetime.now().strftime("%Y-%m-%d"), "session": source_session}
            facts.append(row); idx[key] = row
    (d / "facts.json").write_text(json.dumps({"facts": facts}, ensure_ascii=False, indent=1), encoding="utf-8")
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

def save_judgment(root: Path, uid: str, agent_id: str, rec: dict) -> None:
    d = user_dir(root, uid) / "judgments"; d.mkdir(parents=True, exist_ok=True)
    p = d / f"{agent_id}.json"
    arr = []
    if p.exists():
        try:
            arr = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            arr = []
    arr.append(rec)
    p.write_text(json.dumps(arr[-50:], ensure_ascii=False, indent=1), encoding="utf-8")


def load_judgments(root: Path, uid: str, agent_id: Optional[str] = None) -> dict:
    d = user_dir(root, uid) / "judgments"
    if not d.is_dir():
        return {}
    out = {}
    for p in d.glob("*.json"):
        if agent_id and p.stem != agent_id:
            continue
        try:
            out[p.stem] = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
    return out


# ---------------------------------------------------------------- 速览卡：按透镜装配

def briefing(root: Path, uid: str, agent_id: str, lens: dict, *, agent_name: str = "") -> str:
    """会话开始时给 agent 的「我记得的他」。读什么由透镜决定；agent 自己不查库。"""
    uid = safe_uid(uid)
    if not uid:
        return ""
    rd = (lens or {}).get("read") or {}
    want = set(rd.get("facts") or [])
    facts = [f for f in load_facts(root, uid) if not want or f.get("category") in want]
    facts.sort(key=lambda f: (-int(f.get("seen", 1)), f.get("last_at", "")))
    sessions = load_sessions(root, uid, n=20)
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
        js = load_judgments(root, uid, None if rd.get("judgments") == "all" else agent_id)
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


def save_review(root: Path, uid: str, rec: dict) -> Path:
    d = user_dir(root, uid) / "reviews"; d.mkdir(parents=True, exist_ok=True)
    p = d / f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{rec.get('method', 'x')}.json"
    p.write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
    return p


def load_reviews(root: Path, uid: str) -> List[dict]:
    d = user_dir(root, uid) / "reviews"
    if not d.is_dir():
        return []
    out = []
    for p in sorted(d.glob("*.json"), reverse=True)[:20]:
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except Exception:
            continue
    return out
