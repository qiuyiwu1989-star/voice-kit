"""数据回流 → 策略迭代 → 版本升级（2026-09-16 用户要的「越聊越强」第一版）。

这里的「训练」**不是模型微调**，是：

    采集（每一轮）→ 信号（规则标出可疑回合）→ 发现（模型归纳「情况类型」+ 证据）
    → 提案（persona/method/playbook 的最小改动）→ 评测（旧版 vs 提案版跑同一批用例）
    → 人点「发布」（升 minor + CHANGELOG + git）→ 可回滚（恢复发布前快照，升 patch）

系统只提建议、给证据；**发布必须由后台里的人点确认**。

    agents/_feedback/<agent_id>/
      turns/<YYYYMMDD>.jsonl     每一轮一行（网关层采集，不依赖宿主产品调 review）
      analyses/<ts>.json         一次分析：统计 + 情况类型
      proposals/<id>.json        一个提案：新旧全文、对应哪条情况类型、评测结果、发布快照
      cases.json                 回归用例集（从真实回合生成，去标识，人勾选启用）
      _audit.jsonl               发布 / 强制发布 / 回滚 / 改提案 的审计

红线：
- **儿童档位不采**：audience.product 为 qiuqiu，或 rails 为 child_strict / child_parent 的 agent，
  本版一个字都不写进回流。整个模块对它们也不做分析、不出提案。
- **uid 不存明文**：只存加盐哈希；回复和用户原话里出现的 uid 也换成占位符。
- **去标识**：手机号、邮箱、身份证、银行卡、「我叫某某」写盘时就替换；送模型前再过一遍。
- **采集失败绝不影响对话**：吞异常，打一行日志。
- **评测不污染回流**：评测走进程内的装配 + 闸门，不经 HTTP、不读写记忆；另有 suppressed() 兜底。
- `_feedback/` 不进 git（.gitignore），也不是 agent（agents.load_all 跳过 `_` 开头的目录）。
"""
from __future__ import annotations

import contextlib
import contextvars
import difflib
import hashlib
import json
import os
import re
import statistics
import subprocess
import time
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable, Dict, Iterable, Iterator, List, Optional, Tuple

import yaml

from . import agents as A

FEEDBACK_DIR = "_feedback"
CHILD_RAILS = frozenset({"child_strict", "child_parent"})
CHILD_PRODUCTS = frozenset({"qiuqiu"})
DEFAULT_MAX_CHARS = 60
SLOW_FIRST_MS = 3000
# 提案不许动的 manifest 字段：身份（id/kind）、档位（rails/read_level）、受众，以及平台管的版本和上游
IDENTITY_FIELDS = frozenset({"id", "kind", "rails", "read_level", "audience", "version", "upstream", "run", "shells"})
PROPOSAL_FILES = A.TEXT_LAYERS

_SUPPRESS: contextvars.ContextVar[bool] = contextvars.ContextVar("vk_feedback_suppress", default=False)


class NotCollected(PermissionError):
    """儿童档位 agent：本版不采、不分析、不提案。"""


class GateBlocked(ValueError):
    def __init__(self, reasons: List[str]):
        super().__init__("；".join(reasons))
        self.reasons = reasons


class Stale(ValueError):
    """提案基于的文件已经被别人改过了。"""


# ---------------------------------------------------------------- 开关与档位

def enabled() -> bool:
    """整体开关。默认开；VOICEKIT_FEEDBACK=0/off/false/no 关掉。"""
    return (os.environ.get("VOICEKIT_FEEDBACK") or "1").strip().lower() not in ("0", "off", "false", "no")


def child_reason(ag: A.Agent) -> str:
    """儿童档位就返回原因（页面上直接显示），否则空串。"""
    product = str((ag.manifest.get("audience") or {}).get("product") or "")
    if product in CHILD_PRODUCTS:
        return f"所属产品 {product} 是儿童产品：本版不采集对话原文进回流。"
    if ag.rails in CHILD_RAILS:
        return f"闸门档位 {ag.rails} 面向儿童或家长端：本版不采集对话原文进回流。"
    return ""


def fb_dir(root: Path, agent_id: str) -> Path:
    if not re.fullmatch(r"[a-z][a-z0-9-]{1,60}", agent_id or ""):
        raise ValueError(f"agent id 不合法：{agent_id!r}")
    return Path(root) / FEEDBACK_DIR / agent_id


@contextlib.contextmanager
def suppressed():
    """这段代码里的对话不进回流（评测用）。"""
    tok = _SUPPRESS.set(True)
    try:
        yield
    finally:
        _SUPPRESS.reset(tok)


# ---------------------------------------------------------------- 去标识

_ID_CARD = re.compile(r"(?<![0-9A-Za-z])\d{17}[\dXx](?![0-9A-Za-z])")
_BANK = re.compile(r"(?<!\d)\d{16,19}(?!\d)")
_PHONE = re.compile(r"(?<!\d)(?:\+?86[- ]?)?1[3-9]\d[- ]?\d{4}[- ]?\d{4}(?!\d)")
_LANDLINE = re.compile(r"(?<!\d)0\d{2,3}-\d{7,8}(?!\d)")
_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_NAME = re.compile(r"(我叫|我的名字是|我的名字叫|本人叫)\s*([一-鿿]{2,3}|[A-Z][a-z]{1,15})")


def deidentify(text: str, names: Iterable[str] = ()) -> str:
    """把能认出一个人的东西换成占位符。顺序有讲究：身份证（18 位）先于银行卡（16–19 位）先于手机号。"""
    if not text:
        return text or ""
    t = str(text)
    for n in sorted({str(n) for n in names if n and len(str(n)) >= 2}, key=len, reverse=True):
        t = t.replace(n, "〈用户〉")
    t = _EMAIL.sub("〈邮箱〉", t)
    t = _ID_CARD.sub("〈证件号〉", t)
    t = _BANK.sub("〈卡号〉", t)
    t = _PHONE.sub("〈手机号〉", t)
    t = _LANDLINE.sub("〈电话〉", t)
    t = _NAME.sub(lambda m: m.group(1) + "〈姓名〉", t)
    return t


def uid_hash(uid: str) -> str:
    """不存 uid 明文。加盐是为了「小明」这类常见名字不能被字典反查。"""
    if not uid:
        return ""
    salt = os.environ.get("VOICEKIT_FEEDBACK_SALT") or "voice-kit-feedback-v1"
    return hashlib.sha256(f"{salt}:{uid}".encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------- 1. 采集

def log_turn(root: Path, ag: A.Agent, *, product: str = "", session_id: str = "", uid: str = "",
             user_text: str = "", reply: str = "", first_ms: Optional[float] = None,
             total_ms: Optional[float] = None, input_takeover: bool = False,
             output_gated: bool = False, used_fallback: bool = False,
             truncated: Optional[bool] = None, lang: str = "zh", aborted: bool = False) -> Optional[dict]:
    """追加一行。返回写下的记录；没写（关了/儿童/评测中/出错）返回 None。**绝不抛。**"""
    try:
        if not enabled() or _SUPPRESS.get() or child_reason(ag):
            return None
        names = [uid] if uid else []
        rec = {
            "id": uuid.uuid4().hex[:12],
            "at": datetime.now().isoformat(timespec="seconds"),
            "agent": ag.id, "version": ag.version, "product": product or "",
            "session_id": str(session_id or "")[:80], "uid_hash": uid_hash(uid),
            "user": deidentify(user_text, names), "reply": deidentify(reply, names),
            "first_ms": None if first_ms is None else round(float(first_ms)),
            "total_ms": None if total_ms is None else round(float(total_ms)),
            "chars": _chars(reply),
            "input_takeover": bool(input_takeover), "output_gated": bool(output_gated),
            "used_fallback": bool(used_fallback), "truncated": truncated,
            "lang": lang or "zh", "aborted": bool(aborted),
        }
        d = fb_dir(root, ag.id) / "turns"
        d.mkdir(parents=True, exist_ok=True)
        with (d / f"{datetime.now().strftime('%Y%m%d')}.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return rec
    except Exception as e:          # 回流是旁路：写不下去也不能让对话出错
        print(f"[feedback] 记录失败 {getattr(ag, 'id', '?')}: {type(e).__name__}: {e}", flush=True)
        return None


def _chars(s: str) -> int:
    return len(re.sub(r"\s", "", s or ""))


def _sse_delta(chunk) -> str:
    try:
        s = chunk.decode("utf-8", "ignore") if isinstance(chunk, (bytes, bytearray)) else str(chunk)
    except Exception:
        return ""
    out = ""
    for line in s.split("\n"):
        line = line.strip()
        if not line.startswith("data:") or line[5:].strip() == "[DONE]":
            continue
        try:
            out += json.loads(line[5:])["choices"][0]["delta"].get("content") or ""
        except Exception:
            continue
    return out


def tap_stream(stream_fn: Callable[[], Iterator], root: Path, ag: A.Agent, asm: dict, *, request=None,
               user_text: str = "", uid: str = "", lang: str = "zh", product: str = "",
               meta: Optional[dict] = None) -> Callable[[], Iterator]:
    """service.agent_chat 的钩子：包住 stream()，原样透传每个分片，结束时记一轮。

    不改 stream() 的结构——只在外面听。`meta` 留给别的线填（比如 red-shape 的 {"truncated": True}）。
    """
    def wrapped():
        t0 = time.perf_counter()
        first, reply, aborted = None, "", True
        try:
            for chunk in stream_fn():
                try:
                    d = _sse_delta(chunk)
                    if d:
                        if first is None:
                            first = (time.perf_counter() - t0) * 1000
                        reply += d
                except Exception:
                    pass
                yield chunk
            aborted = False
        finally:
            try:
                from .gate import InputGate
                takeover = bool(asm.get("input_rules")) and InputGate(asm["input_rules"]).check(user_text)[0] is not None
                fb = str(asm.get("fallback_reply") or "")
                used_fb = bool(fb) and not takeover and fb in reply
                sid = ""
                if request is not None:
                    sid = request.headers.get("x-session-id") or request.headers.get("x-room-id") or ""
                log_turn(root, ag, product=product, session_id=sid, uid=uid, user_text=user_text, reply=reply,
                         first_ms=first, total_ms=(time.perf_counter() - t0) * 1000,
                         input_takeover=takeover, output_gated=used_fb, used_fallback=used_fb,
                         truncated=(meta or {}).get("truncated"), lang=lang, aborted=aborted)
            except Exception as e:
                print(f"[feedback] 钩子失败: {type(e).__name__}: {e}", flush=True)
    return wrapped


def read_turns(root: Path, agent_id: str, since: date, until: date) -> List[dict]:
    d = fb_dir(root, agent_id) / "turns"
    out: List[dict] = []
    if not d.is_dir():
        return out
    for p in sorted(d.glob("*.jsonl")):
        try:
            day = datetime.strptime(p.stem, "%Y%m%d").date()
        except ValueError:
            continue
        if not (since <= day <= until):
            continue
        for line in p.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
                if isinstance(r, dict) and r.get("id"):
                    out.append(r)
            except Exception:
                continue
    out.sort(key=lambda r: r.get("at") or "")
    return out


# ---------------------------------------------------------------- 2. 信号

FLAGS: Dict[str, Tuple[str, str]] = {        # code → (中文说明, 严重度 high|medium|low)
    "too_long": ("回复超长", "medium"),
    "multi_question": ("一次问了多个问题", "medium"),
    "fallback": ("用了兜底句 / 输出闸门命中", "high"),
    "takeover": ("输入闸门接管（没调模型）", "medium"),
    "slow_first": ("首字超过 3 秒", "medium"),
    "stage_leak": ("括号动作描写漏出", "high"),
    "user_unhappy": ("用户下一句表达不满或没被理解", "high"),
    "user_repeat": ("用户下一句重复了上一句", "medium"),
    "truncated": ("回复被截断", "low"),
    "ended_after": ("会话在 agent 说完后结束（弱信号）", "low"),
}
PROBLEM_SEVERITIES = ("high", "medium")       # 「有问题回合」只算这两档，弱信号单列

UNHAPPY_WORDS = ["你没听懂", "没听懂", "不是这个意思", "我说的是", "你在说什么", "太长了", "别问了",
                 "听不懂", "答非所问", "说重点", "你没明白", "不对不对", "我不是问这个", "什么意思啊",
                 "啰嗦", "废话", "别说了", "你理解错了"]
_STAGE_LEAK = re.compile(r"[（(\[【][^（）()\[\]【】]{1,80}[）)\]】]|\*[^*\n]{1,30}\*")
_QUESTIONS = re.compile(r"[?？]+")


def max_chars(ag: Optional[A.Agent]) -> int:
    m = (ag.manifest if ag else {}) or {}
    for k in ("reply_max_chars", "max_reply_chars"):
        try:
            v = int(m.get(k))
            if v > 0:
                return v
        except (TypeError, ValueError):
            pass
    return DEFAULT_MAX_CHARS


def _norm(s: str) -> str:
    return re.sub(r"[\s，。！？、,.!?~…「」“”\"']", "", s or "")


def _flag(code: str, detail: str = "") -> dict:
    label, sev = FLAGS[code]
    return {"code": code, "label": label, "severity": sev, "detail": detail}


def flag_turn(turn: dict, next_turn: Optional[dict] = None, *, limit: int = DEFAULT_MAX_CHARS,
              session_last: bool = False, session_closed: bool = False) -> List[dict]:
    """纯规则。next_turn = 同一场里的下一轮（没有就 None）。"""
    out: List[dict] = []
    reply = turn.get("reply") or ""
    n = turn.get("chars") if isinstance(turn.get("chars"), int) else _chars(reply)
    if n > limit:
        out.append(_flag("too_long", f"{n} 字 > {limit}"))
    q = len(_QUESTIONS.findall(reply))
    if q >= 2:
        out.append(_flag("multi_question", f"{q} 个问句"))
    if turn.get("used_fallback") or turn.get("output_gated"):
        out.append(_flag("fallback"))
    if turn.get("input_takeover"):
        out.append(_flag("takeover"))
    fm = turn.get("first_ms")
    if isinstance(fm, (int, float)) and fm > SLOW_FIRST_MS:
        out.append(_flag("slow_first", f"{int(fm)}ms"))
    m = _STAGE_LEAK.search(reply)
    if m:
        out.append(_flag("stage_leak", m.group(0)))
    if turn.get("truncated") is True:
        out.append(_flag("truncated"))
    if next_turn:
        nu = next_turn.get("user") or ""
        hit = next((w for w in UNHAPPY_WORDS if w in nu), None)
        if hit:
            out.append(_flag("user_unhappy", f"下一句：「{nu[:40]}」"))
        a, b = _norm(turn.get("user") or ""), _norm(nu)
        if len(a) >= 4 and len(b) >= 4 and (a == b or difflib.SequenceMatcher(None, a, b).ratio() >= 0.8):
            out.append(_flag("user_repeat", f"下一句：「{nu[:40]}」"))
    elif session_last and session_closed:
        out.append(_flag("ended_after"))
    return out


def is_problem(flags: List[dict]) -> bool:
    return any(f["severity"] in PROBLEM_SEVERITIES for f in flags)


SESSION_GAP_S = 30 * 60


def _ts(r: dict) -> float:
    try:
        return datetime.fromisoformat(r.get("at")).timestamp()
    except Exception:
        return 0.0


def sessions_of(turns: List[dict]) -> List[List[dict]]:
    """按 session_id 分场；没有 session_id 的按 uid_hash + 30 分钟间隔切。"""
    groups: Dict[str, List[dict]] = {}
    for t in turns:
        key = t.get("session_id") or (f"u:{t['uid_hash']}" if t.get("uid_hash") else f"t:{t['id']}")
        groups.setdefault(key, []).append(t)
    out: List[List[dict]] = []
    for g in groups.values():
        g.sort(key=_ts)
        cur: List[dict] = []
        for t in g:
            if cur and _ts(t) - _ts(cur[-1]) > SESSION_GAP_S:
                out.append(cur); cur = []
            cur.append(t)
        if cur:
            out.append(cur)
    return out


def annotate(turns: List[dict], ag: Optional[A.Agent] = None, *, now: Optional[datetime] = None) -> List[dict]:
    """每一轮配上 flags / prev / next（同场前后一轮）。返回按时间排序的新列表。"""
    limit = max_chars(ag)
    now_ts = (now or datetime.now()).timestamp()
    out = []
    for s in sessions_of(turns):
        for i, t in enumerate(s):
            nxt = s[i + 1] if i + 1 < len(s) else None
            last = nxt is None
            closed = last and now_ts - _ts(t) > SESSION_GAP_S
            out.append({**t, "flags": flag_turn(t, nxt, limit=limit, session_last=last, session_closed=closed),
                        "prev": s[i - 1] if i > 0 else None, "next": nxt,
                        "history": s[max(0, i - 3):i], "session_turns": len(s)})
    out.sort(key=lambda r: r.get("at") or "")
    return out


def _pct(xs: List[float], p: float) -> Optional[float]:
    xs = sorted(x for x in xs if isinstance(x, (int, float)))
    if not xs:
        return None
    k = min(len(xs) - 1, max(0, int(round(p * len(xs))) - 1))
    return round(xs[k]) if p != 0.5 else round(statistics.median(xs))


def stats(annotated: List[dict], since: date, until: date) -> dict:
    days = []
    d = since
    while d <= until:
        days.append(d.isoformat()); d += timedelta(days=1)
    by_day = {k: [] for k in days}
    for t in annotated:
        k = (t.get("at") or "")[:10]
        if k in by_day:
            by_day[k].append(t)

    def summarize(ts: List[dict]) -> dict:
        n = len(ts)
        counts = {c: 0 for c in FLAGS}
        for t in ts:
            for f in {f["code"] for f in t["flags"]}:
                counts[f] += 1
        prob = sum(1 for t in ts if is_problem(t["flags"]))
        fm = [t.get("first_ms") for t in ts]
        return {"n": n, "problem": prob, "problem_rate": round(prob / n, 3) if n else None,
                "flags": counts, "flag_rates": {c: (round(v / n, 3) if n else None) for c, v in counts.items()},
                "first_p50": _pct(fm, 0.5), "first_p90": _pct(fm, 0.9),
                "avg_chars": round(sum(t.get("chars") or 0 for t in ts) / n, 1) if n else None}

    total = summarize(annotated)
    half = len(days) // 2
    early = [t for k in days[:half] for t in by_day[k]]
    late = [t for k in days[half:] for t in by_day[k]]
    se, sl = summarize(early), summarize(late)
    trend = {c: {"early": se["flag_rates"][c], "late": sl["flag_rates"][c]} for c in FLAGS}
    return {"since": since.isoformat(), "until": until.isoformat(), **total,
            "sessions": len({(t.get("session_id") or t.get("uid_hash") or t["id"]) for t in annotated}),
            "days": [{"day": k, **summarize(by_day[k])} for k in days], "trend": trend,
            "flag_meta": {c: {"label": l, "severity": s} for c, (l, s) in FLAGS.items()}}


def window(days: int = 7, until: Optional[date] = None) -> Tuple[date, date]:
    until = until or date.today()
    return until - timedelta(days=max(1, int(days)) - 1), until


def _slim(t: Optional[dict]) -> Optional[dict]:
    if not t:
        return None
    return {k: t.get(k) for k in ("id", "at", "user", "reply", "chars", "first_ms", "version")}


def flagged_turns(root: Path, ag: A.Agent, *, days: int = 7, flag: str = "", include_weak: bool = False,
                  limit: int = 300) -> List[dict]:
    since, until = window(days)
    ann = annotate(read_turns(root, ag.id, since, until), ag)
    out = []
    for t in reversed(ann):
        if flag:
            if not any(f["code"] == flag for f in t["flags"]):
                continue
        elif not (is_problem(t["flags"]) or (include_weak and t["flags"])):
            continue
        out.append({**_slim(t), "flags": t["flags"], "session_id": t.get("session_id"),
                    "prev": _slim(t["prev"]), "next": _slim(t["next"])})
        if len(out) >= limit:
            break
    return out


# ---------------------------------------------------------------- 模型调用

def _complete(llm, system: str, user: str, *, model: str, max_tokens: int = 2000) -> str:
    return "".join(llm.stream([{"role": "user", "content": user}], system=system, model=model,
                              thinking=False, max_tokens=max_tokens))


def parse_json_obj(text: str) -> Optional[dict]:
    """模型输出里抠 JSON 对象：先整体解析，再剥 ```json 围栏，再找最外层花括号。"""
    if not text:
        return None
    cands = [text.strip()]
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if m:
        cands.append(m.group(1).strip())
    i, j = text.find("{"), text.rfind("}")
    if 0 <= i < j:
        cands.append(text[i:j + 1])
    for c in cands:
        try:
            obj = json.loads(c)
            if isinstance(obj, dict):
                return obj
        except Exception:
            continue
    return None


def analysis_model(ag: A.Agent) -> str:
    m = str(ag.manifest.get("model") or "")
    return m if m in A.MODELS and A.MODELS[m]["think_off"] else A.DEFAULT_MODEL


# ---------------------------------------------------------------- 3. 发现

def _require_adult(ag: A.Agent) -> None:
    why = child_reason(ag)
    if why:
        raise NotCollected(why)


def _now_id() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4]


ANALYZE_SYSTEM = """你是对话策略分析员。下面是一个语音对话 agent 最近被规则标出来的「可能有问题的回合」（已去标识），
以及它现在的策略文件。你的任务是归纳「情况类型」：在什么情境下、agent 出了什么同一类的毛病。

要求：
- 只根据给出的回合归纳，不编造回合里没有的事。每一类至少 2 条证据，引用回合 id（方括号里的那串）。
- 标题写成「当……时，agent ……」，具体到情境，别写「回复质量不高」这类空话。
- related 写现在策略文件里相关的那一条（文件名 + 原句片段）；没有相关条目就写「策略里没有覆盖」。
- suggestion 写怎么改策略（一两句），不要写成对用户的话。
- 不打分，不评价用户。
只输出 JSON，不要任何别的字：
{"situations":[{"title":"","count":0,"codes":["flag code"],"evidence":["回合id"],"related":"","suggestion":""}]}"""


def _turn_block(t: dict, names=()) -> str:
    lines = [f"[{t['id']}] 标记：{'、'.join(f['label'] for f in t['flags'])}"]
    if t.get("prev"):
        lines.append(f"  上一轮 用户：{deidentify(t['prev'].get('user'), names)}")
        lines.append(f"  上一轮 agent：{deidentify(t['prev'].get('reply'), names)}")
    lines.append(f"  用户：{deidentify(t.get('user'), names)}")
    lines.append(f"  agent：{deidentify(t.get('reply'), names)}（{t.get('chars')} 字，首字 {t.get('first_ms')}ms）")
    if t.get("next"):
        lines.append(f"  下一轮 用户：{deidentify(t['next'].get('user'), names)}")
    return "\n".join(lines)


def _strategy_text(ag: A.Agent, chars: int = 6000) -> str:
    return "\n\n".join(f"## {k}\n{(ag.texts.get(k) or '').strip()[:chars]}" for k in PROPOSAL_FILES)


def analyze(root: Path, ag: A.Agent, *, since: date, until: date, llm=None, model: Optional[str] = None,
            max_samples: int = 40, save: bool = True) -> dict:
    """统计 + （可选）模型归纳。模型没配、调用失败或 JSON 坏了 → 降级为只给统计，不抛。"""
    _require_adult(ag)
    ann = annotate(read_turns(root, ag.id, since, until), ag)
    st = stats(ann, since, until)
    problems = [t for t in ann if is_problem(t["flags"])]
    # 取样：先高严重度，再按时间倒序；上限 max_samples
    sev = {"high": 0, "medium": 1, "low": 2}
    problems.sort(key=lambda t: (min(sev[f["severity"]] for f in t["flags"]), -_ts(t)))
    sample = problems[:max_samples]
    res = {"id": _now_id(), "agent": ag.id, "version": ag.version,
           "at": datetime.now().isoformat(timespec="seconds"),
           "since": since.isoformat(), "until": until.isoformat(), "stats": st,
           "sample_ids": [t["id"] for t in sample],
           "samples": {t["id"]: {**_slim(t), "flags": t["flags"], "prev": _slim(t["prev"]), "next": _slim(t["next"])}
                       for t in sample},
           "situations": [], "llm": {"status": "skipped", "reason": ""}}
    if not sample:
        res["llm"] = {"status": "skipped", "reason": "这段时间没有被标出问题的回合。"}
    elif llm is None:
        res["llm"] = {"status": "skipped", "reason": "没有配置模型（TOKENHUB_API_KEY），只给统计。"}
    else:
        mdl = model or analysis_model(ag)
        user = (f"# agent：{ag.name}（v{ag.version}）\n\n# 现在的策略文件\n{_strategy_text(ag)}\n\n"
                f"# 被标出来的回合（共 {len(problems)} 条，给你 {len(sample)} 条）\n\n"
                + "\n\n".join(_turn_block(t) for t in sample))
        raw = ""
        try:
            raw = _complete(llm, ANALYZE_SYSTEM, user, model=mdl, max_tokens=2500)
            obj = parse_json_obj(raw)
            if not obj or not isinstance(obj.get("situations"), list):
                raise ValueError("模型没有给出可解析的 JSON")
            ids = set(res["sample_ids"])
            sits = []
            for i, s in enumerate(obj["situations"]):
                if not isinstance(s, dict) or not str(s.get("title") or "").strip():
                    continue
                ev = [str(e) for e in (s.get("evidence") or []) if str(e) in ids][:5]
                if not ev:
                    continue            # 没有能对上号的证据 = 编的，不收
                codes = [c for c in (s.get("codes") or []) if c in FLAGS]
                try:
                    cnt = int(s.get("count") or len(ev))
                except (TypeError, ValueError):
                    cnt = len(ev)
                sits.append({"id": f"s{len(sits) + 1}", "title": str(s["title"]).strip()[:200],
                             "count": max(cnt, len(ev)), "codes": codes, "evidence": ev,
                             "related": str(s.get("related") or "")[:400],
                             "suggestion": str(s.get("suggestion") or "")[:600]})
            res["situations"] = sits
            res["llm"] = {"status": "ok", "model": mdl, "reason": "" if sits else "模型给出的类型都没有对得上的证据。"}
        except Exception as e:
            res["llm"] = {"status": "degraded", "model": mdl, "reason": f"{type(e).__name__}: {e}"[:300],
                          "raw": (raw or "")[:1000]}
    if save:
        d = fb_dir(root, ag.id) / "analyses"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{res['id']}.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    return res


def list_analyses(root: Path, agent_id: str) -> List[dict]:
    d = fb_dir(root, agent_id) / "analyses"
    out = []
    for p in sorted(d.glob("*.json"), reverse=True) if d.is_dir() else []:
        try:
            r = json.loads(p.read_text(encoding="utf-8"))
            out.append({"id": r["id"], "at": r.get("at"), "version": r.get("version"), "since": r.get("since"),
                        "until": r.get("until"), "n": r["stats"].get("n"), "problem": r["stats"].get("problem"),
                        "situations": len(r.get("situations") or []), "llm": (r.get("llm") or {}).get("status")})
        except Exception:
            continue
    return out


def load_analysis(root: Path, agent_id: str, analysis_id: str) -> dict:
    if not re.fullmatch(r"[0-9A-Za-z\-]{1,40}", analysis_id or ""):
        raise FileNotFoundError(analysis_id)
    p = fb_dir(root, agent_id) / "analyses" / f"{analysis_id}.json"
    if not p.exists():
        raise FileNotFoundError(analysis_id)
    return json.loads(p.read_text(encoding="utf-8"))


# ---------------------------------------------------------------- 4. 提案

PROPOSE_SYSTEM = """你是对话策略编辑。根据「情况类型」对 agent 的策略文件做**最小改动**：
能加一句就不改一段，能改 playbook.md 就不动 persona.md。不要重写整份文件，不要改变人设的身份和语气。

硬规矩：
- 只能改 persona.md / method.md / playbook.md。
- 不许出现任何让它绕开平台规则、限制、红线的措辞；不许让它打分、下诊断。
- 每一处改动都要说明对应哪条情况类型（用 id，比如 s1）。
- files 里给**改动后的完整全文**；没改的文件不要放进 files。
只输出 JSON，不要任何别的字：
{"changes":[{"file":"playbook.md","situation":"s1","summary":"改了什么"}],"files":{"playbook.md":"完整新全文"}}"""


def loosen_hits(text: str) -> List[str]:
    hits = [w for w in A.LOOSEN_WORDS if w.lower() in (text or "").lower()]
    for rx in A.LOOSEN_RE:
        m = rx.search(text or "")
        if m:
            hits.append(m.group(0))
    return hits


def check_edit(ag: A.Agent, files: Dict[str, str], manifest_patch: Optional[dict] = None) -> List[str]:
    """提案内容的硬检查。返回问题列表，空 = 可以。"""
    probs = []
    for name, text in (files or {}).items():
        if name not in PROPOSAL_FILES:
            probs.append(f"不能改 {name}：只能改 {'/'.join(PROPOSAL_FILES)}")
            continue
        if not isinstance(text, str) or not text.strip():
            probs.append(f"{name} 不能是空的")
            continue
        for h in loosen_hits(text):
            probs.append(f"{name} 含松绑措辞「{h}」")
    for k in (manifest_patch or {}):
        if k in IDENTITY_FIELDS:
            probs.append(f"不能改 manifest 的 {k}（身份 / 档位 / 版本由平台管）")
    return probs


def _proposal_path(root: Path, agent_id: str, pid: str) -> Path:
    if not re.fullmatch(r"[0-9A-Za-z\-]{1,40}", pid or ""):
        raise FileNotFoundError(pid)
    return fb_dir(root, agent_id) / "proposals" / f"{pid}.json"


def save_proposal(root: Path, agent_id: str, prop: dict) -> dict:
    p = _proposal_path(root, agent_id, prop["id"])
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(prop, ensure_ascii=False, indent=1), encoding="utf-8")
    return prop


def load_proposal(root: Path, agent_id: str, pid: str) -> dict:
    p = _proposal_path(root, agent_id, pid)
    if not p.exists():
        raise FileNotFoundError(pid)
    return json.loads(p.read_text(encoding="utf-8"))


def list_proposals(root: Path, agent_id: str) -> List[dict]:
    d = fb_dir(root, agent_id) / "proposals"
    out = []
    for p in sorted(d.glob("*.json"), reverse=True) if d.is_dir() else []:
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except Exception:
            continue
    return out


def diff_lines(old: str, new: str) -> List[dict]:
    a, b = (old or "").splitlines(), (new or "").splitlines()
    out = []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if op == "equal":
            out += [{"op": " ", "text": x} for x in a[i1:i2]]
        else:
            out += [{"op": "-", "text": x} for x in a[i1:i2]]
            out += [{"op": "+", "text": x} for x in b[j1:j2]]
    return out


def proposal_view(prop: dict) -> dict:
    """给页面：每个文件带上逐行 diff。"""
    return {**prop, "diffs": {k: diff_lines(v.get("old", ""), v.get("new", "")) for k, v in (prop.get("files") or {}).items()},
            "gate": publish_gate(prop)}


def propose(root: Path, ag: A.Agent, analysis: dict, *, llm, model: Optional[str] = None) -> dict:
    _require_adult(ag)
    sits = analysis.get("situations") or []
    if not sits:
        raise ValueError("这次分析没有情况类型（模型没跑或降级了），出不了提案。")
    mdl = model or analysis_model(ag)
    current = {k: ag.texts.get(k, "") for k in PROPOSAL_FILES}
    user = ("# 情况类型\n" + json.dumps(sits, ensure_ascii=False, indent=1) + "\n\n# 现在的文件（全文）\n"
            + "\n\n".join(f"=== {k} ===\n{v}" for k, v in current.items()))
    raw = _complete(llm, PROPOSE_SYSTEM, user, model=mdl, max_tokens=6000)
    obj = parse_json_obj(raw)
    prop = {"id": _now_id(), "agent": ag.id, "created_at": datetime.now().isoformat(timespec="seconds"),
            "analysis_id": analysis.get("id"), "base_version": ag.version, "model": mdl,
            "situations": sits, "status": "draft", "rev": 1, "files": {}, "changes": [],
            "manifest_patch": {}, "problems": [], "evaluation": None, "snapshot": None, "log": []}
    if not obj or not isinstance(obj.get("files"), dict):
        prop.update(status="rejected", problems=["模型没有给出可解析的 JSON 提案"], raw=(raw or "")[:2000])
        return save_proposal(root, ag.id, prop)
    files = {k: v for k, v in obj["files"].items() if isinstance(v, str)}
    patch = obj.get("manifest") if isinstance(obj.get("manifest"), dict) else {}
    probs = check_edit(ag, files, patch)
    files = {k: v for k, v in files.items() if k in PROPOSAL_FILES and v.strip() != current.get(k, "").strip()}
    if not files and not probs:
        probs.append("模型没有给出任何实际改动")
    prop["files"] = {k: {"old": current[k], "new": v if v.endswith("\n") else v + "\n"} for k, v in files.items()}
    prop["manifest_patch"] = patch
    sid = {s["id"] for s in sits}
    prop["changes"] = [{"file": str(c.get("file") or ""), "situation": str(c.get("situation") or ""),
                        "summary": str(c.get("summary") or "")[:300], "known": str(c.get("situation") or "") in sid}
                       for c in (obj.get("changes") or []) if isinstance(c, dict)]
    if probs:
        prop.update(status="rejected", problems=probs)      # 松绑 / 动身份 → 整个提案作废，不给人「挑着用」的机会
    return save_proposal(root, ag.id, prop)


def edit_proposal(root: Path, ag: A.Agent, pid: str, files: Dict[str, str], *, actor: str = "admin") -> dict:
    """人在后台改提案文本。改了就要重评（evaluation 清空、rev+1）。"""
    prop = load_proposal(root, ag.id, pid)
    if prop["status"] not in ("draft", "evaluated"):
        raise ValueError(f"提案状态是 {prop['status']}，不能再改")
    probs = check_edit(ag, files)
    if probs:
        raise ValueError("；".join(probs))
    for k, v in files.items():
        old = (prop["files"].get(k) or {}).get("old", ag.texts.get(k, ""))
        if v.strip() == old.strip():
            prop["files"].pop(k, None)
        else:
            prop["files"][k] = {"old": old, "new": v if v.endswith("\n") else v + "\n"}
    prop.update(status="draft", evaluation=None, rev=int(prop.get("rev") or 1) + 1)
    prop.setdefault("log", []).append({"at": datetime.now().isoformat(timespec="seconds"), "actor": actor,
                                       "action": "edit", "files": sorted(files)})
    audit(root, ag.id, action="edit_proposal", proposal=pid, actor=actor, detail={"files": sorted(files)})
    return save_proposal(root, ag.id, prop)


# ---------------------------------------------------------------- 5. 回归用例 + 评测

def cases_path(root: Path, agent_id: str) -> Path:
    return fb_dir(root, agent_id) / "cases.json"


def load_cases(root: Path, agent_id: str) -> List[dict]:
    p = cases_path(root, agent_id)
    try:
        v = json.loads(p.read_text(encoding="utf-8")) if p.exists() else []
        return v if isinstance(v, list) else []
    except Exception:
        return []


def _save_cases(root: Path, agent_id: str, cases: List[dict]) -> None:
    p = cases_path(root, agent_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(cases, ensure_ascii=False, indent=1), encoding="utf-8")


def case_from_turn(ag: A.Agent, t: dict) -> dict:
    """真实回合 → 回归用例。上下文去标识；期望是机械可判的几条。"""
    codes = [f["code"] for f in t.get("flags") or []]
    expect = {"max_chars": max_chars(ag), "max_questions": 1, "no_fallback": True, "no_stage": True}
    if ag.rails == "no_scoring":
        expect["no_scoring"] = True
    hist = []
    for h in t.get("history") or []:
        hist += [["user", deidentify(h.get("user"))], ["bot", deidentify(h.get("reply"))]]
    return {"id": "c-" + t["id"], "from_turn": t["id"], "enabled": True, "codes": codes,
            "history": hist, "user": deidentify(t.get("user")), "reply_then": deidentify(t.get("reply")),
            "expect": expect, "created_at": datetime.now().isoformat(timespec="seconds"), "version_then": t.get("version")}


def add_cases(root: Path, ag: A.Agent, turn_ids: List[str], *, days: int = 30) -> List[dict]:
    _require_adult(ag)
    since, until = window(days)
    ann = {t["id"]: t for t in annotate(read_turns(root, ag.id, since, until), ag)}
    cases = load_cases(root, ag.id)
    have = {c.get("from_turn") for c in cases}
    missing = [i for i in turn_ids if i not in ann]
    if missing:
        raise FileNotFoundError(f"最近 {days} 天里找不到回合 {missing}")
    for i in turn_ids:
        if i not in have:
            cases.append(case_from_turn(ag, ann[i]))
    _save_cases(root, ag.id, cases)
    return cases


def set_case(root: Path, agent_id: str, case_id: str, *, enabled: Optional[bool] = None,
             expect: Optional[dict] = None, delete: bool = False) -> List[dict]:
    cases = load_cases(root, agent_id)
    hit = [c for c in cases if c.get("id") == case_id]
    if not hit:
        raise FileNotFoundError(case_id)
    if delete:
        cases = [c for c in cases if c.get("id") != case_id]
    else:
        if enabled is not None:
            hit[0]["enabled"] = bool(enabled)
        if expect is not None:
            ok = {"max_chars", "max_questions", "no_fallback", "no_stage", "no_scoring"}
            hit[0]["expect"] = {k: v for k, v in expect.items() if k in ok}
    _save_cases(root, agent_id, cases)
    return cases


def check_case(case: dict, text: str, asm: dict) -> List[str]:
    """返回没过的检查（中文）。空 = 过。"""
    e = case.get("expect") or {}
    fails = []
    if not (text or "").strip():
        return ["空回复"]
    if e.get("max_chars") and _chars(text) > int(e["max_chars"]):
        fails.append(f"超过 {e['max_chars']} 字（{_chars(text)}）")
    if e.get("max_questions") is not None and len(_QUESTIONS.findall(text)) > int(e["max_questions"]):
        fails.append(f"问句多于 {e['max_questions']} 个")
    fb = str(asm.get("fallback_reply") or "")
    if e.get("no_fallback") and fb and fb in text:
        fails.append("用了兜底句")
    if e.get("no_stage") and _STAGE_LEAK.search(text):
        fails.append("括号动作描写漏出")
    if e.get("no_scoring"):
        from .testkit.checks import no_scoring
        from .testkit.target import TurnResult
        if no_scoring(None, None, TurnResult(text, None, 0)):
            fails.append("出现打分 / 量化")
    return fails


class AgentTarget:
    """进程内跑一个 agent：装配 → 输入闸门 → 模型 → 按句过输出闸门 → 剥舞台提示。

    和 service.agent_chat 同一条链路，但**不经 HTTP、不装记忆、不进回流**——评测不许污染自己的数据。
    """

    def __init__(self, ag: A.Agent, llm, name: str = ""):
        from .gate import InputGate, OutputGate
        self.ag, self.llm = ag, llm
        self.asm = A.assemble(ag)
        self.name = name or f"agent:{ag.id}@{ag.version}"
        self.in_gate = InputGate(self.asm["input_rules"]) if self.asm["input_rules"] else None
        self.out_gate = OutputGate(self.asm["output_rules"], fallback=self.asm["fallback_reply"]) \
            if self.asm["output_rules"] else None

    def turn(self, messages, *, headers=None, body_extra=None):
        from .service import _strip_stage, by_sentence, _hint_rewriter
        from .gate import limit_turn
        from .testkit.target import TurnResult
        t0 = time.perf_counter()
        text, first = "", None
        user = next((m.get("content", "") for m in reversed(messages) if m.get("role") == "user"), "")
        try:
            with suppressed():
                if self.in_gate and self.in_gate.check(user)[0]:
                    text = self.asm["takeover_reply"]
                    first = round((time.perf_counter() - t0) * 1000, 1)
                else:
                    toks = self.llm.stream(messages, system=self.asm["system_prompt"], model=self.asm["model"],
                                           thinking=False, cache_key=f"eval-{self.ag.id}", max_tokens=1024,
                                           reasoning_effort=self.asm.get("reasoning_effort"))
                    # 和线上一致：打分命中走重写（rails 有 rewrite_hint 时），一轮长度按 turn_limit 截断。
                    # 少了这两步，评测量到的就不是用户实际听到的（2026-09-16 合并 red-shape 时发现）。
                    if self.out_gate and self.asm.get("rewrite_hint"):
                        self.out_gate.rewriter = _hint_rewriter(self.llm, messages, system=self.asm["system_prompt"],
                                                                hint=self.asm["rewrite_hint"], model=self.asm["model"],
                                                                cache_key=f"eval-{self.ag.id}")
                    sents = (_strip_stage(ev["text"]) for ev in self.out_gate.filter(by_sentence(toks), low_latency=True)) \
                        if self.out_gate else (_strip_stage(t) for t in by_sentence(toks))
                    sents = limit_turn(sents, self.asm.get("turn_limit"), user, upstream=(toks,))
                    for s in sents:
                        if s:
                            if first is None:
                                first = round((time.perf_counter() - t0) * 1000, 1)
                            text += s
        except Exception as e:
            return TurnResult(text, first, round((time.perf_counter() - t0) * 1000, 1), done=False,
                              error=f"{type(e).__name__}: {e}")
        return TurnResult(text, first, round((time.perf_counter() - t0) * 1000, 1), done=True, chunks=1)


def _case_messages(c: dict) -> List[dict]:
    msgs = [{"role": "assistant" if r in ("bot", "assistant") else "user", "content": t} for r, t in c.get("history") or []]
    return msgs + [{"role": "user", "content": c.get("user") or ""}]


def _run_side(ag: A.Agent, llm, cases: List[dict], suite_name: Optional[str]) -> dict:
    tgt = AgentTarget(ag, llm)
    rows, firsts, chars, qs = [], [], [], []
    for c in cases:
        r = tgt.turn(_case_messages(c))
        fails = check_case(c, r.text, tgt.asm) + ([f"出错：{r.error}"] if r.error else [])
        rows.append({"id": c["id"], "reply": r.text, "fails": fails, "first_ms": r.first_token_ms})
        if r.first_token_ms is not None:
            firsts.append(r.first_token_ms)
        chars.append(_chars(r.text)); qs.append(len(_QUESTIONS.findall(r.text)))
    suite = None
    if suite_name:
        from .testkit import load_suite, run_suite
        from .testkit.__main__ import SUITES_DIR
        p = SUITES_DIR / f"{suite_name}.json"
        if p.exists():
            rep = run_suite(load_suite(p), tgt)
            for cr in rep.cases:
                chars.append(_chars(cr.result.text)); qs.append(len(_QUESTIONS.findall(cr.result.text)))
                if cr.result.first_token_ms is not None:
                    firsts.append(cr.result.first_token_ms)
            suite = {"name": suite_name, "n": rep.n, "n_fail": rep.n_fail,
                     "pass_rate": round((rep.n - rep.n_fail) / rep.n, 3) if rep.n else None,
                     "cases": [{"id": cr.scenario.id, "failed": cr.failed, "reply": cr.result.text,
                                "findings": [f"{f.check}: {f.message}" for f in cr.findings if f.severity == "fail"]}
                               for cr in rep.cases]}
    n = len(rows)
    passed = sum(1 for r in rows if not r["fails"])
    by_check: Dict[str, int] = {}
    for r in rows:
        for f in r["fails"]:
            key = re.sub(r"[（(].*$", "", re.sub(r"\d+", "N", f))
            by_check[key] = by_check.get(key, 0) + 1
    return {"version": ag.version, "cases": {"n": n, "passed": passed, "pass_rate": round(passed / n, 3) if n else None,
                                             "fail_by_check": by_check, "rows": rows},
            "suite": suite, "avg_chars": round(sum(chars) / len(chars), 1) if chars else None,
            "avg_questions": round(sum(qs) / len(qs), 2) if qs else None,
            "first_p50": _pct(firsts, 0.5), "first_p90": _pct(firsts, 0.9)}


def _hash_files(ag: A.Agent) -> str:
    h = hashlib.sha256()
    for k in PROPOSAL_FILES:
        h.update((ag.texts.get(k) or "").encode("utf-8"))
    return h.hexdigest()[:16]


def draft_agent(ag: A.Agent, prop: dict, tmp: Path) -> A.Agent:
    import shutil
    d = Path(tmp) / ag.id
    shutil.copytree(ag.dir, d, ignore=shutil.ignore_patterns("sessions", "reports"))
    for k, v in (prop.get("files") or {}).items():
        (d / k).write_text(v["new"], encoding="utf-8")
    if prop.get("manifest_patch"):
        m = yaml.safe_load((d / "manifest.yaml").read_text(encoding="utf-8")) or {}
        m.update({k: v for k, v in prop["manifest_patch"].items() if k not in IDENTITY_FIELDS})
        (d / "manifest.yaml").write_text(yaml.safe_dump(m, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return A.load_agent(d)


def evaluate(root: Path, ag: A.Agent, pid: str, *, llm) -> dict:
    """旧版（当前文件）和提案版各跑：启用的用例 + rails 对应的红线套件。结果存进提案。"""
    import tempfile
    _require_adult(ag)
    prop = load_proposal(root, ag.id, pid)
    if prop["status"] not in ("draft", "evaluated"):
        raise ValueError(f"提案状态是 {prop['status']}，不能评测")
    _check_fresh(ag, prop)
    cases = [c for c in load_cases(root, ag.id) if c.get("enabled")]
    suite = A.RAILS_SUITE.get(ag.rails)
    with suppressed(), tempfile.TemporaryDirectory() as td:
        new_ag = draft_agent(ag, prop, Path(td))
        fails = [i for i in A.validate(new_ag) if i.severity == "fail"]
        if fails:
            raise ValueError(f"提案版没过静态审查：{fails[0].where} {fails[0].message}")
        old = _run_side(ag, llm, cases, suite)
        new = _run_side(new_ag, llm, cases, suite)
    prop["evaluation"] = {"at": datetime.now().isoformat(timespec="seconds"), "rev": prop.get("rev", 1),
                          "base_hash": _hash_files(ag), "suite": suite, "n_cases": len(cases),
                          "old": old, "new": new}
    prop["status"] = "evaluated"
    save_proposal(root, ag.id, prop)
    return prop


# ---------------------------------------------------------------- 6. 发布与回滚

def _check_fresh(ag: A.Agent, prop: dict) -> None:
    for k, v in (prop.get("files") or {}).items():
        if (ag.texts.get(k) or "") != v.get("old", ""):
            raise Stale(f"{k} 在提案之后被改过了（当前 v{ag.version}）。重新分析出提案，别把别人的改动盖掉。")


def publish_gate(prop: dict) -> dict:
    """发布门槛：已评测（且评的就是这一版文本）、红线套件通过率不降、用例集通过率不降。"""
    reasons: List[str] = []
    ev = prop.get("evaluation")
    if prop.get("status") == "rejected":
        reasons.append("提案已作废：" + "；".join(prop.get("problems") or []))
    elif prop.get("status") in ("published", "rolled_back"):
        reasons.append(f"提案已经是 {prop['status']}")
    elif not prop.get("files"):
        reasons.append("提案里没有任何改动")
    if not ev:
        reasons.append("还没评测")
    elif ev.get("rev") != prop.get("rev"):
        reasons.append("评测之后提案又改过，要重新评测")
    else:
        o, n = ev["old"], ev["new"]
        if ev.get("suite"):
            op, np_ = (o.get("suite") or {}).get("pass_rate"), (n.get("suite") or {}).get("pass_rate")
            if op is None or np_ is None:
                reasons.append(f"红线套件 {ev['suite']} 没跑出结果")
            elif np_ < op:
                reasons.append(f"红线套件 {ev['suite']} 通过率下降：{op:.0%} → {np_:.0%}")
        if not ev.get("n_cases"):
            reasons.append("用例集是空的：先从「有问题的回合」里挑几条加进用例集再评测")
        else:
            op, np_ = o["cases"]["pass_rate"], n["cases"]["pass_rate"]
            if np_ < op:
                reasons.append(f"用例集通过率下降：{op:.0%} → {np_:.0%}")
    return {"ok": not reasons, "reasons": reasons}


def audit(root: Path, agent_id: str, *, action: str, proposal: str = "", actor: str = "admin", detail=None) -> None:
    d = fb_dir(root, agent_id)
    d.mkdir(parents=True, exist_ok=True)
    rec = {"at": datetime.now().isoformat(timespec="seconds"), "actor": actor, "action": action,
           "proposal": proposal, "detail": detail or {}}
    with (d / "_audit.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def load_audit(root: Path, agent_id: str, n: int = 200) -> List[dict]:
    p = fb_dir(root, agent_id) / "_audit.jsonl"
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return list(reversed(out))[:n]


def _pr(v) -> str:
    return "—" if v is None else f"{v:.0%}"


def _changelog(ag_dir: Path, version: str, title: str, lines: List[str]) -> None:
    p = ag_dir / "CHANGELOG.md"
    head = p.read_text(encoding="utf-8") if p.exists() else f"# {ag_dir.name} 版本记录\n\n由 /admin/feedback 发布 / 回滚时自动追加。\n"
    block = f"\n## v{version} · {datetime.now().strftime('%Y-%m-%d')} · {title}\n\n" + "\n".join(f"- {l}" for l in lines) + "\n"
    p.write_text(head.rstrip("\n") + "\n" + block, encoding="utf-8")


def _git_commit(root: Path, agent_id: str, message: str) -> Optional[dict]:
    """和 admin._autocommit 同一个开关（VOICEKIT_AGENTS_GIT_COMMIT）；多提交一个 CHANGELOG.md。"""
    if not A.git_env():
        return None
    st = A.git_state(root)
    if not st["repo"]:
        return {"ok": False, "reason": st["reason"]}
    d = Path(root) / agent_id
    paths = [str(d / l) for l in A.LAYERS] + [str(d / "CHANGELOG.md")]
    ident = ["-c", "user.name=voice-kit admin", "-c", "user.email=admin@voice-kit.local"]
    try:
        add = subprocess.run(["git"] + ident + ["add", "--"] + paths, cwd=str(root), capture_output=True, text=True, timeout=10)
        if add.returncode != 0:
            return {"ok": False, "reason": (add.stderr or add.stdout).strip()[:300]}
        cm = subprocess.run(["git"] + ident + ["commit", "-m", message, "--"] + paths,
                            cwd=str(root), capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as e:
        return {"ok": False, "reason": str(e)[:300]}
    if cm.returncode != 0:
        return {"ok": False, "reason": (cm.stdout + cm.stderr).strip()[:300]}
    head = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=str(root), capture_output=True, text=True, timeout=5)
    return {"ok": True, "hash": head.stdout.strip()}


def _write_manifest(mp: Path, m: dict) -> None:
    mp.write_text(yaml.safe_dump(m, allow_unicode=True, sort_keys=False), encoding="utf-8")


def publish(root: Path, ag: A.Agent, pid: str, *, force: bool = False, actor: str = "admin", note: str = "") -> dict:
    root = Path(root)
    _require_adult(ag)
    prop = load_proposal(root, ag.id, pid)
    if prop["status"] in ("rejected", "published", "rolled_back"):
        raise GateBlocked([f"提案状态是 {prop['status']}，不能发布"])
    if not prop.get("files"):
        raise GateBlocked(["提案里没有任何改动"])
    gate = publish_gate(prop)
    if not gate["ok"] and not force:
        raise GateBlocked(gate["reasons"])
    _check_fresh(ag, prop)
    d = ag.dir
    snapshot = {k: (d / k).read_text(encoding="utf-8") for k in ("manifest.yaml",) + PROPOSAL_FILES if (d / k).exists()}
    m = yaml.safe_load(snapshot["manifest.yaml"]) or {}
    old_v = str(m.get("version") or "0.0.0")
    try:
        for k, v in prop["files"].items():
            (d / k).write_text(v["new"], encoding="utf-8")
        m.update({k: v for k, v in (prop.get("manifest_patch") or {}).items() if k not in IDENTITY_FIELDS})
        m["version"] = A.bump(old_v, "minor")
        m["updated_at"] = datetime.now().strftime("%Y-%m-%d")
        _write_manifest(d / "manifest.yaml", m)
        fails = [i for i in A.validate(A.load_agent(d), other_shells=A.other_shell_owners(root, ag.id)) if i.severity == "fail"]
        if fails:
            raise ValueError(f"发布后没过静态审查，已恢复：{fails[0].where} {fails[0].message}")
    except Exception:
        for k, v in snapshot.items():
            (d / k).write_text(v, encoding="utf-8")
        raise
    ev = prop.get("evaluation") or {}
    o, n = ev.get("old") or {}, ev.get("new") or {}
    sit_titles = {s["id"]: s["title"] for s in prop.get("situations") or []}
    used = sorted({c["situation"] for c in prop.get("changes") or [] if c.get("situation")})
    lines = [f"从 v{old_v} 升到 v{m['version']}，提案 {pid}，分析 {prop.get('analysis_id')}",
             "改了：" + "；".join(f"{c['file']}：{c['summary']}" for c in prop.get("changes") or []) if prop.get("changes")
             else "改了：" + "、".join(prop["files"]),
             "依据的情况类型：" + ("；".join(f"{s} {sit_titles.get(s, '')}" for s in used) or "—")]
    if ev:
        lines.append(f"用例集通过率 {_pr((o.get('cases') or {}).get('pass_rate'))} → {_pr((n.get('cases') or {}).get('pass_rate'))}"
                     f"（{ev.get('n_cases')} 条）；平均字数 {o.get('avg_chars')} → {n.get('avg_chars')}；"
                     f"平均问句 {o.get('avg_questions')} → {n.get('avg_questions')}；首字 P50 {o.get('first_p50')} → {n.get('first_p50')}ms")
        if ev.get("suite"):
            lines.append(f"红线套件 {ev['suite']} 通过率 {_pr((o.get('suite') or {}).get('pass_rate'))} → {_pr((n.get('suite') or {}).get('pass_rate'))}")
    if force and not gate["ok"]:
        lines.append("⚠ 强制发布（未达门槛）：" + "；".join(gate["reasons"]) + (f"。说明：{note}" if note else ""))
    _changelog(d, m["version"], "回流提案发布", lines)
    prop.update(status="published", snapshot=snapshot, published_at=datetime.now().isoformat(timespec="seconds"),
                published_version=m["version"], from_version=old_v, forced=bool(force and not gate["ok"]),
                published_hash=_hash_files(A.load_agent(d)))
    prop.setdefault("log", []).append({"at": prop["published_at"], "actor": actor, "action": "publish",
                                       "forced": prop["forced"], "reasons": gate["reasons"], "note": note})
    audit(root, ag.id, action="force_publish" if prop["forced"] else "publish", proposal=pid, actor=actor,
          detail={"from": old_v, "to": m["version"], "gate_reasons": gate["reasons"], "note": note})
    save_proposal(root, ag.id, prop)
    commit = _git_commit(root, ag.id, f"{ag.id}: 回流提案发布 v{old_v} → v{m['version']}（{pid}）"
                                      + ("【强制】" if prop["forced"] else ""))
    return {**prop, "commit": commit}


def rollback(root: Path, ag: A.Agent, pid: str, *, actor: str = "admin", note: str = "") -> dict:
    root = Path(root)
    prop = load_proposal(root, ag.id, pid)
    if prop["status"] != "published" or not prop.get("snapshot"):
        raise ValueError(f"只有已发布的提案能回滚（现在是 {prop['status']}）")
    if prop.get("published_hash") and _hash_files(ag) != prop["published_hash"]:
        raise Stale("发布之后这个 agent 的文件又改过了——直接回滚会把之后的改动一起抹掉。先手工处理。")
    d = ag.dir
    cur_v = ag.version
    snap = prop["snapshot"]
    for k in PROPOSAL_FILES:
        if k in snap:
            (d / k).write_text(snap[k], encoding="utf-8")
    m = yaml.safe_load(snap["manifest.yaml"]) or {}
    m["version"] = A.bump(cur_v, "patch")
    m["updated_at"] = datetime.now().strftime("%Y-%m-%d")
    _write_manifest(d / "manifest.yaml", m)
    _changelog(d, m["version"], "回滚", [f"撤回 v{prop.get('published_version')}（提案 {pid}），内容恢复到 v{prop.get('from_version')} 的文件，"
                                         f"版本号往前走不倒退" + (f"。说明：{note}" if note else "")])
    prop.update(status="rolled_back", rolled_back_at=datetime.now().isoformat(timespec="seconds"),
                rolled_back_version=m["version"])
    prop.setdefault("log", []).append({"at": prop["rolled_back_at"], "actor": actor, "action": "rollback", "note": note})
    audit(root, ag.id, action="rollback", proposal=pid, actor=actor,
          detail={"from": cur_v, "to": m["version"], "restored": prop.get("from_version"), "note": note})
    save_proposal(root, ag.id, prop)
    commit = _git_commit(root, ag.id, f"{ag.id}: 回滚回流提案 {pid}，v{cur_v} → v{m['version']}")
    return {**prop, "commit": commit}


def overview(root: Path, ag: A.Agent, days: int = 7) -> dict:
    """后台左侧列表的一行。"""
    why = child_reason(ag)
    base = {"id": ag.id, "name": ag.name, "version": ag.version, "rails": ag.rails,
            "product": (ag.manifest.get("audience") or {}).get("product"),
            "collect": not why, "why": why}
    if why:
        return {**base, "n": 0, "problem_rate": None, "pending": 0}
    since, until = window(days)
    ann = annotate(read_turns(root, ag.id, since, until), ag)
    n = len(ann); prob = sum(1 for t in ann if is_problem(t["flags"]))
    pending = sum(1 for p in list_proposals(root, ag.id) if p.get("status") in ("draft", "evaluated"))
    return {**base, "n": n, "problem": prob, "problem_rate": round(prob / n, 3) if n else None, "pending": pending}


# ---------------------------------------------------------------- 命令行（给定时任务用）

def _llm_from_env():
    key = os.environ.get("TOKENHUB_API_KEY")
    if not key:
        return None
    from .providers.tokenhub import DEFAULT_BASE_URL, TokenHubLLM
    return TokenHubLLM(key, base_url=os.environ.get("TOKENHUB_BASE_URL") or DEFAULT_BASE_URL,
                       auth_header=os.environ.get("TOKENHUB_AUTH_HEADER") or "bearer",
                       thinking_field=(os.environ.get("TOKENHUB_THINKING_FIELD", "1") != "0"),
                       thinking_style=os.environ.get("TOKENHUB_THINKING_STYLE") or "thinking")


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="python -m voice_kit.feedback")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("analyze", help="分析一个 agent 最近几天的回流，结果落 _feedback/<id>/analyses/")
    a.add_argument("agent_id")
    a.add_argument("--days", type=int, default=7)
    a.add_argument("--root", default=os.environ.get("VOICEKIT_AGENTS_DIR") or str(Path(__file__).parent.parent / "agents"))
    a.add_argument("--no-llm", action="store_true", help="只出统计，不调模型")
    args = ap.parse_args(argv)
    root = Path(args.root)
    d = root / args.agent_id
    if not d.is_dir():
        print(f"没有 agent {args.agent_id}（{root}）")
        return 2
    ag = A.load_agent(d)
    since, until = window(args.days)
    try:
        res = analyze(root, ag, since=since, until=until, llm=None if args.no_llm else _llm_from_env())
    except NotCollected as e:
        print(str(e))
        return 3
    st = res["stats"]
    print(f"{ag.id} v{ag.version} {res['since']}~{res['until']}：{st['n']} 轮，有问题 {st['problem']}，"
          f"情况类型 {len(res['situations'])}，模型 {res['llm']['status']} {res['llm'].get('reason', '')}")
    print(f"→ {fb_dir(root, ag.id) / 'analyses' / (res['id'] + '.json')}")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
