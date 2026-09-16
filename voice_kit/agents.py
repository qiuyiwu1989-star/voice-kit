"""Agent 注册表：真源是 git 里的文件，这里只负责读、校验、写回。

    agents/<id>/
      manifest.yaml     身份（平台改）
      persona.md        人格
      method.md         方法论
      playbook.md       策略
      lens.yaml         记忆透镜（作者声明，平台校验）
      reports/          testkit 跑出来的报告（评估卡的"守线/快"两栏从这里来）

铁律不在这个目录里——它是 rails 档位，由平台按受众注入（见 RAILS）。

`validate()` 就是「入职四关」的第一关：静态审查。全自动、秒级、不调模型。
"""
from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import yaml

LAYERS = ("manifest.yaml", "persona.md", "method.md", "playbook.md", "lens.yaml")
TEXT_LAYERS = ("persona.md", "method.md", "playbook.md")

# ---------------------------------------------------------------- 平台字典（v0：先枚举出来，改这里要同步 spec 014）

FACT_CATEGORIES = {"event", "preference", "constraint", "relation", "attempt"}   # = child_facts.category
SIGNAL_DIMS = {"problem_solving", "persistence", "curiosity", "expression", "social",
               "self_awareness", "arrival", "narrative", "delivery",
               # 客户沟通场景（2026-09-14 加）
               "intent", "objection", "urgency", "trust"}
JUDGMENT_DIMS = SIGNAL_DIMS | {"synthesis"}         # synthesis：总编那种收敛判断
RECAP_MODES = {"none", "last_session", "cross_occasion"}
JUDGMENT_READ = {"own", "all", "none"}
READ_LEVELS = ("L0", "L1", "L2")
KINDS = ("code", "config")
CHILD_BANDS = {"preschool", "low", "mid", "high"}
ADULT_BANDS = {"adult"}

# TokenHub 上可给对话用的模型（2026-09-14 对着「模型列表」+「深度思考」文档核的，延迟是本机实测）。
# voice: 能否用于实时语音——关得掉思考且首字可控。关不掉的只能给"后台分析"类 agent。
MODELS: Dict[str, dict] = {
    "deepseek-v4-flash": {"vendor": "DeepSeek", "think_off": True, "voice": True, "note": "球球生产在用；前缀缓存要带 prompt_cache_key"},
    "deepseek-v4-pro":   {"vendor": "DeepSeek", "think_off": True, "voice": True, "note": "同系更强，贵"},
    "hy3":               {"vendor": "腾讯混元", "think_off": True, "voice": True, "note": "thinking disabled 即可；别配 reasoning_effort=low，那会把思考重新打开、正文为空"},
    "hy4-preview":       {"vendor": "腾讯混元", "think_off": True, "voice": True, "note": "预览版，实测常 429 busy"},
    "glm-5.2":           {"vendor": "智谱", "think_off": True, "voice": True, "note": ""},
    "glm-5.3":           {"vendor": "智谱", "think_off": False, "voice": False, "note": "关不掉思考，只能 reasoning_effort=low"},
    "glm-5.3-flash":     {"vendor": "智谱", "think_off": False, "voice": False, "note": "带 thinking 参数会 400，且关不掉思考"},
    "kimi-k3":           {"vendor": "Moonshot", "think_off": False, "voice": False, "note": "关不掉思考"},
    "minimax-m3":        {"vendor": "MiniMax", "think_off": True, "voice": True, "note": "thinking disabled 生效；不关时会把 <think> 混进正文（provider 已剥）"},
}
DEFAULT_MODEL = "deepseek-v4-flash"

RAILS: Dict[str, dict] = {
    "child_strict": {"audience": "child", "desc": "8 铁律 · 病理化词 · 自伤接管"},
    "child_parent": {"audience": "parent", "desc": "不病理化 · 不下诊断 · 引用原话只能是精选正向证据 · 必须标明数字分身"},
    "listener_forbidden": {"audience": "adult", "desc": "只描述不评价 · 第一人称 · 16 个评价词"},
    "no_scoring": {"audience": "adult", "desc": "可以判断好坏，不量化一个人"},
    "adult_basic": {"audience": "adult", "desc": "面向成人客户的底线：不编造价格与承诺 · 不贬低同行 · 不索要证件卡号 · 自伤接管"},
}

# 档位 → 闸门规则。这是"铁律从 prompt 搬进运行时"落地的那一层：
# 人设文件里一个字都不用写，agent 也改不了。规则形状与 tenants.json 一致。
RAILS_RULES: Dict[str, dict] = {
    "child_strict": {
        "output_rules": [
            {"name": "禁用病理化词汇", "words": ["抑郁", "焦虑症", "多动症", "自闭", "心理问题"]},
            {"name": "不下诊断", "words": ["诊断", "病症", "障碍"]},
        ],
        "input_rules": [{"name": "self_harm", "words": ["不想活", "自杀", "死了算了"]}],
        "fallback_reply": "我们再多聊一会儿吧，你刚才说的那个我还没听够。",
        "takeover_reply": "我听到了。你愿意多跟我说一点吗？我在。",
    },
    "child_parent": {
        "output_rules": [
            {"name": "禁用病理化词汇", "words": ["抑郁", "焦虑症", "多动症", "自闭"]},
            {"name": "不下诊断", "words": ["诊断为", "确诊", "患有"]},
        ],
        "input_rules": [],
        "fallback_reply": "这一点我想换个说法再讲。",
        "takeover_reply": "",
    },
    "listener_forbidden": {
        "output_rules": [{"name": "听众不评价", "words": ["清晰度", "建议", "欠佳", "不足", "应该", "需要改进",
                                                      "综合评分", "表达能力", "有待提升", "做得好", "做得不好",
                                                      "优点", "缺点", "打分", "评分", "分数"]}],
        "input_rules": [],
        "fallback_reply": "我刚才走神了一下，你再说一遍那句？",
        "takeover_reply": "",
    },
    "no_scoring": {
        "output_rules": [{"name": "不打分", "words": ["满分", "评分", "打分", "星级"],
                          "patterns": [r"\d+\s*(分(?!钟)|%|％)", r"[A-D][+-]?\s*(级|档)"]}],
        "input_rules": [],
        "fallback_reply": "这个我不用数字说，我直接说哪一页没落地。",
        "takeover_reply": "",
    },
    "adult_basic": {
        "output_rules": [
            {"name": "不编造承诺", "words": ["保证退款", "百分之百有效", "绝对没问题", "包过", "稳赚"]},
            {"name": "不贬低同行", "words": ["他们家是骗子", "同行都是垃圾"]},
            {"name": "不索要敏感信息", "words": ["身份证号", "银行卡号", "卡的密码", "验证码发我"]},
        ],
        "input_rules": [{"name": "self_harm", "words": ["不想活", "自杀"]}],
        "fallback_reply": "这句我得换个说法，稍等。",
        "takeover_reply": "我听到了，这比我们聊的任何事都重要。你现在安全吗？如果需要，可以拨打心理援助热线 12356。",
    },
}


def rails_rules(rails: str) -> dict:
    """把档位翻成 gate.Rule。未知档位 = 空规则（validate 会先把它拦下，这里只兜底）。"""
    from .gate import Rule
    raw = RAILS_RULES.get(rails) or {}

    def mk(items):
        return [Rule(name=r["name"], words=tuple(r.get("words") or ()),
                     patterns=tuple(re.compile(p) for p in (r.get("patterns") or ()))) for r in items]
    return {"output_rules": mk(raw.get("output_rules") or []),
            "input_rules": mk(raw.get("input_rules") or []),
            "fallback_reply": raw.get("fallback_reply", ""),
            "takeover_reply": raw.get("takeover_reply") or raw.get("fallback_reply", "")}


def assemble(ag: "Agent") -> dict:
    """六层 → 一次调用要的东西：system prompt + 闸门规则。

    顺序刻意是 人格 → 方法论 → 策略：先是谁，再怎么看，最后怎么做。
    铁律不进 prompt（它在闸门里），但给模型一句提醒有档位在——
    不是靠这句话守线，是让它少撞闸门、少吃兜底话。
    """
    parts = [f"# 你是「{ag.name}」"]
    for layer in TEXT_LAYERS:
        t = (ag.texts.get(layer) or "").strip()
        if t:
            parts.append(t)
    r = RAILS.get(ag.rails)
    if r:
        parts.append(f"# 平台底线（由系统闸门执行，你只需知道）\n{r['desc']}。违反的句子不会被送出，会被替换成兜底话。")
    parts.append("# 输出要求\n口语、短句、一次只说一件事。不要输出任何括号内的动作描写或内部标记。")
    return {"system_prompt": "\n\n".join(parts), **rails_rules(ag.rails),
            "model": str(ag.manifest.get("model") or DEFAULT_MODEL),
            "reasoning_effort": ag.manifest.get("reasoning_effort")}


# 松绑措辞：出现在人格/策略里就是想绕闸门。逐字比对 + 几个正则。
LOOSEN_WORDS = ["忽略上面", "忽略之前", "忽略以上", "不受限制", "没有任何限制", "可以说任何",
                "无视规则", "不用遵守", "jailbreak", "ignore previous", "ignore all rules"]
LOOSEN_RE = [re.compile(r"(忽略|无视|不用管|抛开).{0,6}(铁律|规则|限制|红线)"),
             re.compile(r"(可以|允许).{0,4}(诊断|打分|评分)")]


@dataclass
class Issue:
    where: str          # 文件名或字段
    message: str
    severity: str = "fail"      # fail | warn


@dataclass
class Agent:
    id: str
    dir: Path
    manifest: dict
    lens: dict
    texts: Dict[str, str] = field(default_factory=dict)     # persona.md 等
    missing: List[str] = field(default_factory=list)
    parse_errors: List[Issue] = field(default_factory=list)

    @property
    def name(self) -> str: return str(self.manifest.get("name") or self.id)
    @property
    def version(self) -> str: return str(self.manifest.get("version") or "0.0.0")
    @property
    def kind(self) -> str: return str(self.manifest.get("kind") or "")
    @property
    def rails(self) -> str: return str(self.manifest.get("rails") or "")
    @property
    def read_level(self) -> str: return str(self.manifest.get("read_level") or "")

    def summary(self) -> dict:
        a = self.manifest.get("audience") or {}
        return {"id": self.id, "name": self.name, "version": self.version, "kind": self.kind,
                "rails": self.rails, "read_level": self.read_level,
                "product": a.get("product"), "bands": a.get("bands"),
                "status": self.manifest.get("status", ""),
                "archetype": self.manifest.get("archetype", ""),
                "model": self.manifest.get("model"),
                "mode": self.manifest.get("mode") or "video",
                "blurb": self.manifest.get("blurb") or "",
                # 这个 agent 服务宿主产品的哪些「壳」。REP 靠它把 slug 解析成 agent，
                # 不用在自己库里加列（spec 009 §4 原来打算加 Shell.agentId，这样就不用了）
                "shells": list(self.manifest.get("shells") or [])}


# ---------------------------------------------------------------- 读

def load_agent(d: Path) -> Agent:
    d = Path(d)
    ag = Agent(id=d.name, dir=d, manifest={}, lens={})
    for layer in LAYERS:
        p = d / layer
        if not p.exists():
            ag.missing.append(layer)
            continue
        raw = p.read_text(encoding="utf-8")
        if layer.endswith(".yaml"):
            try:
                obj = yaml.safe_load(raw) or {}
                if not isinstance(obj, dict):
                    raise ValueError("顶层不是映射")
            except Exception as e:
                ag.parse_errors.append(Issue(layer, f"YAML 解析失败：{e}"))
                obj = {}
            if layer == "manifest.yaml":
                ag.manifest = obj
            else:
                ag.lens = obj
        else:
            ag.texts[layer] = raw
    return ag


def load_all(root: Path) -> List[Agent]:
    root = Path(root)
    if not root.exists():
        return []
    return sorted((load_agent(p) for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")),
                  key=lambda a: a.id)


# ---------------------------------------------------------------- 第一关：静态审查

def validate(ag: Agent, *, author_level: str = "L0") -> List[Issue]:
    out: List[Issue] = list(ag.parse_errors)
    for m in ag.missing:
        out.append(Issue(m, "缺这一层的文件"))
    m = ag.manifest
    if not m:
        return out

    # 身份
    if str(m.get("id") or "") != ag.id:
        out.append(Issue("manifest.yaml", f"id 与目录名不一致：{m.get('id')!r} ≠ {ag.id!r}"))
    if not re.fullmatch(r"\d+\.\d+\.\d+", str(m.get("version") or "")):
        out.append(Issue("manifest.yaml", "version 要是 x.y.z"))
    if m.get("kind") not in KINDS:
        out.append(Issue("manifest.yaml", f"kind 只能是 {KINDS}"))
    if m.get("read_level") not in READ_LEVELS:
        out.append(Issue("manifest.yaml", f"read_level 只能是 {READ_LEVELS}"))
    elif READ_LEVELS.index(m["read_level"]) < READ_LEVELS.index(author_level):
        out.append(Issue("manifest.yaml",
                         f"read_level {m['read_level']} 高于作者等级 {author_level}（数字越小权限越大）"))

    # 受众 ↔ 闸门档位
    aud = m.get("audience") or {}
    bands = set(aud.get("bands") or [])
    rails = m.get("rails")
    if not bands:
        out.append(Issue("manifest.yaml", "audience.bands 不能为空"))
    unknown = bands - CHILD_BANDS - ADULT_BANDS - {"parent"}
    if unknown:
        out.append(Issue("manifest.yaml", f"未知年龄档 {sorted(unknown)}"))
    if rails not in RAILS:
        out.append(Issue("manifest.yaml", f"rails 档位 {rails!r} 不存在，可选 {sorted(RAILS)}"))
    else:
        want = RAILS[rails]["audience"]
        if bands & CHILD_BANDS and want != "child":
            out.append(Issue("manifest.yaml", f"儿童受众必须用儿童档位，现在是 {rails}（{want}）"))
        if "parent" in bands and want != "parent":
            out.append(Issue("manifest.yaml", f"家长端受众要用 child_parent，现在是 {rails}"))

    # 模型：配置级 agent 由平台代调，模型必须在表里且能用于语音；代码级自己管模型，这里只提醒
    model = m.get("model")
    if model and model not in MODELS:
        out.append(Issue("manifest.yaml", f"model {model!r} 不在 TokenHub 可用表里，可选 {sorted(MODELS)}",
                         "warn" if m.get("kind") == "code" else "fail"))
    elif model and not MODELS[model]["voice"]:
        out.append(Issue("manifest.yaml", f"model {model} 关不掉思考，不能用于实时对话（{MODELS[model]['note']}）",
                         "warn" if m.get("kind") == "code" else "fail"))
    if m.get("reasoning_effort") and m.get("reasoning_effort") not in ("low", "medium", "high", "max", "none"):
        out.append(Issue("manifest.yaml", "reasoning_effort 只能是 low/medium/high/max/none"))

    if m.get("run") not in (None, "local", "upstream"):
        out.append(Issue("manifest.yaml", "run 只能是 local / upstream（不填 = 代码级走上游）"))
    if m.get("shells") is not None and not isinstance(m.get("shells"), list):
        out.append(Issue("manifest.yaml", "shells 要是一个列表（宿主产品的壳标识）"))
    if m.get("mode") not in (None, "video", "voice", "text"):
        out.append(Issue("manifest.yaml", "mode 只能是 video / voice / text"))

    # 代码级要有上游
    if m.get("kind") == "code":
        up = m.get("upstream") or {}
        if not str(up.get("url") or "").startswith("http"):
            out.append(Issue("manifest.yaml", "代码级 agent 必须有 upstream.url"))
    if m.get("kind") == "config" and m.get("upstream"):
        out.append(Issue("manifest.yaml", "配置级 agent 不该有 upstream", "warn"))

    # 透镜
    lens = ag.lens
    if lens:
        rd, wr = lens.get("read") or {}, lens.get("write") or {}
        bad = set(rd.get("facts") or []) - FACT_CATEGORIES
        if bad:
            out.append(Issue("lens.yaml", f"read.facts 有平台字典外的类别 {sorted(bad)}，可选 {sorted(FACT_CATEGORIES)}"))
        bad = set(rd.get("signals") or []) - SIGNAL_DIMS
        if bad:
            out.append(Issue("lens.yaml", f"read.signals 有字典外的维度 {sorted(bad)}"))
        if rd.get("recap", "none") not in RECAP_MODES:
            out.append(Issue("lens.yaml", f"read.recap 只能是 {sorted(RECAP_MODES)}"))
        if rd.get("judgments", "own") not in JUDGMENT_READ:
            out.append(Issue("lens.yaml", f"read.judgments 只能是 {sorted(JUDGMENT_READ)}"))
        bad = set(wr.get("judgments") or []) - JUDGMENT_DIMS
        if bad:
            out.append(Issue("lens.yaml", f"write.judgments 有字典外的维度 {sorted(bad)}"))
        if wr.get("facts"):
            out.append(Issue("lens.yaml", "write.facts 不允许：事实层 agent 中立（CLAUDE.md 红线）"))
        if m.get("read_level") in ("L1", "L2") and rd.get("quotes"):
            out.append(Issue("lens.yaml", "L1/L2 不能读原话（quotes）"))

    # 松绑措辞
    for layer in TEXT_LAYERS:
        t = ag.texts.get(layer, "")
        if not t.strip() and layer in ag.texts:
            out.append(Issue(layer, "文件是空的", "warn"))
        for w in LOOSEN_WORDS:
            if w.lower() in t.lower():
                out.append(Issue(layer, f"含松绑措辞「{w}」"))
        for rx in LOOSEN_RE:
            mm = rx.search(t)
            if mm:
                out.append(Issue(layer, f"含松绑措辞「{mm.group(0)}」"))
    return out


# ---------------------------------------------------------------- 写

def bump(version: str, part: str = "patch") -> str:
    a, b, c = (int(x) for x in version.split("."))
    if part == "major":
        return f"{a + 1}.0.0"
    if part == "minor":
        return f"{a}.{b + 1}.0"
    return f"{a}.{b}.{c + 1}"


def save_layer(root: Path, agent_id: str, layer: str, text: str, *, bump_part: Optional[str] = "patch") -> dict:
    """写一层，顺手升版本。manifest 自己被改时不再额外升。返回新版本号。"""
    if layer not in LAYERS:
        raise ValueError(f"未知层 {layer}")
    d = Path(root) / agent_id
    if not d.is_dir():
        raise FileNotFoundError(agent_id)
    if layer.endswith(".yaml"):
        obj = yaml.safe_load(text) or {}
        if not isinstance(obj, dict):
            raise ValueError("YAML 顶层要是映射")
    (d / layer).write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8")
    mp = d / "manifest.yaml"
    if layer != "manifest.yaml" and bump_part and mp.exists():
        m = yaml.safe_load(mp.read_text(encoding="utf-8")) or {}
        m["version"] = bump(str(m.get("version") or "0.0.0"), bump_part)
        m["updated_at"] = datetime.now().strftime("%Y-%m-%d")
        mp.write_text(yaml.safe_dump(m, allow_unicode=True, sort_keys=False), encoding="utf-8")
        return {"version": m["version"]}
    m = yaml.safe_load(mp.read_text(encoding="utf-8")) if mp.exists() else {}
    return {"version": str((m or {}).get("version") or "")}


def new_agent(root: Path, agent_id: str, name: str, *, kind: str = "config",
              rails: str = "child_strict", bands: Optional[List[str]] = None,
              product: str = "") -> Path:
    if not re.fullmatch(r"[a-z][a-z0-9-]{1,40}", agent_id):
        raise ValueError("id 只能是小写字母、数字、连字符")
    d = Path(root) / agent_id
    if d.exists():
        raise FileExistsError(agent_id)
    d.mkdir(parents=True)
    manifest = {"id": agent_id, "name": name, "version": "0.1.0", "kind": kind,
                "status": "draft", "archetype": "",
                "audience": {"product": product, "bands": bands or ["low", "mid", "high"]},
                "read_level": "L2", "rails": rails,
                "layers": ["persona.md", "method.md", "playbook.md"],
                "created_at": datetime.now().strftime("%Y-%m-%d")}
    if kind == "code":
        manifest["upstream"] = {"url": "https://", "hooks": ""}
    (d / "manifest.yaml").write_text(yaml.safe_dump(manifest, allow_unicode=True, sort_keys=False), encoding="utf-8")
    (d / "persona.md").write_text(f"# {name} · 人格\n\n说话方式、性格、自称、口头禅。这是皮肤。\n", encoding="utf-8")
    (d / "method.md").write_text(f"# {name} · 方法论\n\n相信什么、用什么坐标系看人。这是读法。\n", encoding="utf-8")
    (d / "playbook.md").write_text(f"# {name} · 策略\n\n开场、追问、何时换话题、一次会话的目标、怎么收尾。\n", encoding="utf-8")
    (d / "lens.yaml").write_text(yaml.safe_dump(
        {"read": {"facts": ["event", "preference"], "signals": [], "recap": "last_session", "judgments": "own"},
         "write": {"judgments": []}}, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return d


# ---------------------------------------------------------------- 历史与报告

def history(root: Path, agent_id: str, n: int = 20) -> List[dict]:
    """git log 这个目录。没有 git 或不是仓库就空列表，不报错。"""
    d = Path(root) / agent_id
    try:
        r = subprocess.run(["git", "log", f"-{n}", "--date=short", "--format=%h%x09%ad%x09%s", "--", str(d)],
                           cwd=str(Path(root)), capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return []
    if r.returncode != 0:
        return []
    out = []
    for line in r.stdout.splitlines():
        parts = line.split("\t", 2)
        if len(parts) == 3:
            out.append({"hash": parts[0], "date": parts[1], "subject": parts[2]})
    return out


def reports(root: Path, agent_id: str) -> List[dict]:
    d = Path(root) / agent_id / "reports"
    if not d.is_dir():
        return []
    out = []
    for p in sorted(d.glob("*.json"), reverse=True):
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except Exception:
            continue
    return out


def save_report(root: Path, agent_id: str, rep: dict) -> Path:
    d = Path(root) / agent_id / "reports"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{rep.get('suite', 'suite')}.json"
    p.write_text(json.dumps(rep, ensure_ascii=False, indent=1), encoding="utf-8")
    return p


def save_session(root: Path, agent_id: str, rec: dict) -> Path:
    """一场对话（带时间戳的逐句 + 回顾）。评估卡"被用"从这里数。"""
    d = Path(root) / agent_id / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{rec.get('session_id', 'x')}.json"
    p.write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
    return p


def sessions(root: Path, agent_id: str, n: int = 50) -> List[dict]:
    d = Path(root) / agent_id / "sessions"
    if not d.is_dir():
        return []
    out = []
    for p in sorted(d.glob("*.json"), reverse=True)[:n]:
        try:
            r = json.loads(p.read_text(encoding="utf-8"))
            out.append({"session_id": r.get("session_id"), "at": r.get("at"), "version": r.get("version"),
                        "model": r.get("model"), "turns": len(r.get("turns") or []),
                        "user_turns": sum(1 for t in (r.get("turns") or []) if t.get("role") != "bot"),
                        "first_ms": [t.get("first_ms") for t in (r.get("turns") or []) if t.get("first_ms")],
                        "review": (r.get("review") or "")[:200]})
        except Exception:
            continue
    return out


def scorecard(root: Path, ag: Agent) -> dict:
    """评估卡。守线和快从 reports 来；准和被用要接埋点，这里先留空位。"""
    reps = reports(root, ag.id)
    latest = {}
    for r in reps:
        latest.setdefault(r.get("suite"), r)         # 按套件取最新一份
    guard = [r for r in latest.values() if r.get("n")]
    n = sum(r["n"] for r in guard); nf = sum(r.get("n_fail", 0) for r in guard)
    p50 = [r["first_token"]["p50"] for r in guard if r.get("first_token")]
    p90 = [r["first_token"]["p90"] for r in guard if r.get("first_token")]
    return {
        "version": ag.version,
        "guard": {"pass_rate": (round((n - nf) / n * 100, 1) if n else None), "n": n, "fail": nf,
                  "suites": sorted(latest)},
        "speed": {"p50": (max(p50) if p50 else None), "p90": (max(p90) if p90 else None)},
        "accuracy": None,       # 判断测试，未接
        "usage": _usage(root, ag.id),
    }


def _usage(root: Path, agent_id: str) -> Optional[dict]:
    """被用：真实会话数、平均来回数、真实首字。没有会话就 None。"""
    ss = sessions(root, agent_id, n=500)
    if not ss:
        return None
    user_turns = [s["user_turns"] for s in ss]
    firsts = sorted(x for s in ss for x in s["first_ms"] if isinstance(x, (int, float)))
    return {"sessions": len(ss), "avg_user_turns": round(sum(user_turns) / len(ss), 1),
            "p50_first_ms": (firsts[len(firsts) // 2] if firsts else None),
            "last_at": ss[0]["at"]}
