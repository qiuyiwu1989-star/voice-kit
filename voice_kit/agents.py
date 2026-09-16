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
EN_FIELDS = ("name_en", "blurb_en")   # 可选的英文身份字段；人格/方法论层不翻，模型按对话语言自己说

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

# 壳标识：宿主产品（AI 教练）的 slug，会进 URL，所以跟 agent id 一个形状。
SHELL_RE = re.compile(r"[a-z][a-z0-9-]{1,60}")

# 档位 → 「产品红线套件」（入职第三关）。这张表就是第三关能不能自动跑的答案：
# 表里没有的档位 = 这一关还没有跑器，后台必须显示「还没做」。
# 故意不给 child_parent / adult_basic 随便指一个套件——凑满格比空着更坏。
RAILS_SUITE: Dict[str, str] = {
    "child_strict": "qiuqiu_8rules",
    "listener_forbidden": "listener_describe_only",
    "no_scoring": "coach_no_scoring",
}
GATE_SUITE = "gate_contract"        # 第二关：闸门契约，任何租户都得过

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
                          # 2026-09-16：百分号不再一律拦。RED 是融资教练，复述「毛利 40%」是正常对话，
                          # 原来那条会把这类句子整句换成兜底话。拦的是「量化一个人」：分数、等级、带评价动词的百分比。
                          "patterns": [r"\d+\s*分(?![钟钱之])", r"(你|讲得|表达|表现|这一遍).{0,8}(提升|进步|退步|下降|达到|完成度|得分).{0,4}\d+\s*(%|％)",
                                       r"[A-D][+-]?\s*(级|档)"]}],
        "input_rules": [],
        # 2026-09-16：原来是「这个我不用数字说，我直接说哪一页没落地。」——老状态机时代的话。
        # RED 2.0 不许报页码，而且说完就结束、答非所问（线上两场一字不差）。
        # 现在打分命中先按 rewrite_hint 重写一次，重写也不过才用这句；它要能独立成立、不提页码。
        "fallback_reply": "分数我不给。你把开头再讲一遍，我告诉你最该改的是哪一句。",
        "rewrite_hint": "对方要分数，不给分数，改为直接说最该改的一处，一两句。不出现任何数字、分数、等级，不提页码。",
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
            # 输出闸门命中后，给重写那次调用追加的一句指令。空 = 不重写，直接兜底（其他档位维持原样）。
            "rewrite_hint": raw.get("rewrite_hint", ""),
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
            "turn_limit": turn_limit(ag.manifest),
            "model": str(ag.manifest.get("model") or DEFAULT_MODEL),
            "reasoning_effort": ag.manifest.get("reasoning_effort")}


# manifest.turn_limit：一轮最多说几句、几个字。可选，没写就不限。
TURN_LIMIT_KEYS = {"sentences": 20, "chars": 1000, "feedback_sentences": 20, "feedback_chars": 1000}   # 键 → 上限


def turn_limit_issues(raw) -> List[str]:
    if raw is None:
        return []
    if not isinstance(raw, dict):
        return ["turn_limit 要是映射，比如 {sentences: 2, chars: 60}"]
    out = []
    unknown = set(raw) - set(TURN_LIMIT_KEYS)
    if unknown:
        out.append(f"turn_limit 有未知键 {sorted(unknown)}，可选 {sorted(TURN_LIMIT_KEYS)}")
    for k in ("sentences", "chars"):
        if k not in raw:
            out.append(f"turn_limit 缺 {k}")
    for k, cap in TURN_LIMIT_KEYS.items():
        if k not in raw:
            continue
        v = raw[k]
        if isinstance(v, bool) or not isinstance(v, int) or not 1 <= v <= cap:
            out.append(f"turn_limit.{k} 要是 1–{cap} 的整数，现在是 {v!r}")
    return out


def turn_limit(manifest: dict):
    """manifest → gate.TurnLimit；没写或写错都返回 None（写错由 validate 拦成 fail）。"""
    from .gate import TurnLimit
    raw = manifest.get("turn_limit")
    if raw is None or turn_limit_issues(raw):
        return None
    kw = {k: raw[k] for k in TURN_LIMIT_KEYS if k in raw}
    return TurnLimit(**kw)


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

    def _en(self, key: str) -> Optional[str]:
        v = self.manifest.get(key)
        if v is None:
            return None
        return str(v).strip() or None

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
                # 英文身份字段。没写就给 None 而不是空串、也不拿中文顶上——
                # 宿主产品用 `nameEn ?? name` 自己决定退回，模块替它退回的话，
                # 它就分不清「这个 agent 没有英文名」和「英文名恰好是中文」。
                "name_en": self._en("name_en"),
                "blurb_en": self._en("blurb_en"),
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


NON_AGENT_DIRS = frozenset({"users"})


def load_all(root: Path) -> List[Agent]:
    root = Path(root)
    if not root.exists():
        return []
    # users/ 是用户记忆库（memory.py），和 agent 目录同住一个根——它不是 agent。
    # 不跳过的话后台列表和 /v1/agents 里会多出一个六层全缺的「users」。
    # `_` 开头的同理：_feedback/ 是回流数据（feedback.py）；agent id 本来就不许以 _ 开头。
    return sorted((load_agent(p) for p in root.iterdir()
                   if p.is_dir() and not p.name.startswith((".", "_")) and p.name not in NON_AGENT_DIRS),
                  key=lambda a: a.id)


# ---------------------------------------------------------------- 第一关：静态审查

def shell_owners(ags: List[Agent]) -> Dict[str, List[str]]:
    """壳 → 认领它的 agent id 列表。正常每个壳只有一个；有两个就是冲突。"""
    out: Dict[str, List[str]] = {}
    for a in ags:
        for sh in a.summary()["shells"]:
            out.setdefault(str(sh), []).append(a.id)
    return out


def other_shell_owners(root: Path, exclude_id: str) -> Dict[str, str]:
    """除了 exclude_id 之外，磁盘上每个壳归谁。validate 拿它判「已被别人认领」。"""
    out: Dict[str, str] = {}
    for a in load_all(root):
        if a.id == exclude_id:
            continue
        for sh in a.summary()["shells"]:
            out.setdefault(str(sh), a.id)
    return out


def validate(ag: Agent, *, author_level: str = "L0",
             other_shells: Optional[Dict[str, str]] = None) -> List[Issue]:
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
    # 音色：数字 id 不在音色表里时，语音合成会静默换成默认嗓子，零报错（2026-09-16 两个「老板」差点同声）
    v = m.get("voice")
    if isinstance(v, int) or (isinstance(v, str) and v.strip().isdigit()):
        from . import voices as _V
        if int(v) not in _V.BY_ID:
            out.append(Issue("manifest.yaml", f"voice {v} 不在音色表里，合成时会被换成默认嗓子；先在 voices.py 登记", "warn"))
    ve = m.get("voice_en")
    if ve is not None:
        from . import voices as _V
        try:
            ok = int(ve) in _V.BY_ID
        except (TypeError, ValueError):
            ok = False
        if not ok:
            out.append(Issue("manifest.yaml", f"voice_en {ve!r} 不是音色表里的编号，英文场会退回按性别配的英文音色", "warn"))
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
    # 壳绑定。以前只有 tests/test_agent_chat.py 断言「一个壳不能被两个 agent 认领」——
    # 那条断言只在本地跑测试时才生效，后台改 manifest 是绕过它的。提升成校验规则：
    # 撞了壳，REP 那边把 slug 解析成哪个 agent 是随机的（谁先被 iterdir 到算谁），
    # 用户会看到「点开还是那个教练，但人设变了」这种没法复现的 bug。
    shells = m.get("shells")
    if shells is not None and not isinstance(shells, list):
        out.append(Issue("manifest.yaml", "shells 要是一个列表（宿主产品的壳标识）"))
    elif shells:
        seen = set()
        for sh in shells:
            if not isinstance(sh, str) or not SHELL_RE.fullmatch(sh):
                out.append(Issue("manifest.yaml", f"壳标识 {sh!r} 不合法：小写字母开头，只能小写字母/数字/连字符"))
                continue
            if sh in seen:
                out.append(Issue("manifest.yaml", f"壳 {sh} 在自己的 shells 里重复了"))
            seen.add(sh)
            owner = (other_shells or {}).get(sh)
            if owner:
                out.append(Issue("manifest.yaml", f"壳 {sh} 已被 {owner} 认领——一个壳只能归一个 agent"))
    # 英文身份字段（name_en / blurb_en）：可选——大多数 agent 只服务中文产品，没写不该报错。
    # 但写了就不能是空串：`name_en: ""` 会让宿主的 `nameEn ?? name` 取到空串，
    # 英文页面上卡片直接没名字，比显示中文名更糟。
    for key in EN_FIELDS:
        if key in m and not (isinstance(m[key], str) and m[key].strip()):
            out.append(Issue("manifest.yaml", f"{key} 写了就要是非空字符串（不需要英文就整行删掉）"))
    # 口径对齐：中文 name 用「名字 · 类型」，对话里只显示「·」前的名字（service 里 split("·")）。
    # 英文那边要用同一个分隔，否则英文页面上对话里会带着类型一起显示。
    # 中英同步：有中文没英文 → warn（不是 fail）。/talk 和宿主的英文界面上会退回中文，
    # 能用但不整齐；在后台亮一条警告，让「补英文」这件事看得见。
    if str(m.get("name") or "").strip() and "name_en" not in m:
        out.append(Issue("manifest.yaml", "缺英文：有 name 没有 name_en（英文界面会显示中文名）", "warn"))
    if str(m.get("blurb") or "").strip() and "blurb_en" not in m:
        out.append(Issue("manifest.yaml", "缺英文：有 blurb 没有 blurb_en（英文界面会显示中文简介）", "warn"))
    ne = m.get("name_en")
    if isinstance(ne, str) and ne.strip() and ("·" in str(m.get("name") or "")) != ("·" in ne):
        out.append(Issue("manifest.yaml", "name 和 name_en 要么都用「名字 · 类型」，要么都不用——"
                                          "对话里按「·」截短名", "warn"))
    for msg in turn_limit_issues(m.get("turn_limit")):
        out.append(Issue("manifest.yaml", msg))
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


def latest_reports(root: Path, agent_id: str) -> Dict[str, dict]:
    """每套用例最新的一份报告。"""
    latest: Dict[str, dict] = {}
    for r in reports(root, agent_id):
        latest.setdefault(str(r.get("suite") or ""), r)
    return latest


def scorecard(root: Path, ag: Agent) -> dict:
    """评估卡。守线和快从 reports 来；准是「判断证据」不是分数；被用从 sessions 来。"""
    latest = latest_reports(root, ag.id)
    guard = [r for r in latest.values() if r.get("n")]
    n = sum(r["n"] for r in guard); nf = sum(r.get("n_fail", 0) for r in guard)
    p50 = [r["first_token"]["p50"] for r in guard if r.get("first_token")]
    p90 = [r["first_token"]["p90"] for r in guard if r.get("first_token")]
    return {
        "version": ag.version,
        "guard": {"pass_rate": (round((n - nf) / n * 100, 1) if n else None), "n": n, "fail": nf,
                  "suites": sorted(latest)},
        "speed": {"p50": (max(p50) if p50 else None), "p90": (max(p90) if p90 else None)},
        "accuracy": accuracy(root, ag),
        "usage": _usage(root, ag.id),
    }


# ---------------------------------------------------------------- 「准」：判断证据，不是分数

def recaps(root: Path, agent_id: str, n: int = 5, *, chars: int = 1200) -> List[dict]:
    """最近 n 场的回顾原文（截断到 chars）。

    为什么是「回顾」不是「原话」：回顾是 agent 自己用方法论产出的判断，
    看它就能看出它在按自己的坐标系判断，还是在说放之四海皆准的套话。
    这里不展示孩子的逐句原话（CLAUDE.md 红线）——`turns` 一个字都不进来。
    后台本身还有 ADMIN_TOKEN 这道门，用途限安全审核 / 质量监控。
    """
    d = Path(root) / agent_id / "sessions"
    if not d.is_dir():
        return []
    out = []
    for p in sorted(d.glob("*.json"), reverse=True)[:n]:
        try:
            r = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        review = (r.get("review") or "").strip()
        if not review:
            continue
        out.append({"at": r.get("at"), "version": r.get("version"), "model": r.get("model"),
                    "user_turns": sum(1 for t in (r.get("turns") or []) if t.get("role") != "bot"),
                    "review": review[:chars], "truncated": len(review) > chars})
    return out


def accuracy(root: Path, ag: Agent) -> dict:
    """「准」这一栏。**故意没有分数**——判断测试（同一份对话喂两个 agent 比差异）
    的跑器还没写（TASKS.md / spec 014 §10.5）。编一个数出来比空着更坏：
    它会让人以为这一栏验过了。

    能摆出来的是两样可核对的东西：
      · 它 lens.yaml 里 write.judgments 声明的维度（它说自己按什么坐标系判断）
      · 最近几场的回顾原文（它实际判断出了什么）
    人对着这两样自己看：是在按方法论判断，还是在说套话。
    """
    dims = list(((ag.lens.get("write") or {}).get("judgments")) or [])
    rs = recaps(root, ag.id)
    return {
        "score": None,                      # 判断测试跑器未实现，永远是 None
        "status": "not_implemented",
        "why": "判断测试跑器还没写：需要同一份对话喂给两个 agent、比较产出判断的差异。",
        "dims": dims,
        "recaps": rs,
        "n_recaps": len(rs),
    }


# ---------------------------------------------------------------- 入职四关（spec 014）

# 状态只有四种，含义写死在这里，别在前端另外发明：
#   pass    这一关在当前版本上过了
#   fail    跑了，没过
#   todo    有跑器，但当前版本还没跑过（包括「跑过但那是旧版本」）
#   missing 跑器/套件还不存在——**这一关还没做**，不是通过
GATE_STATUS = ("pass", "fail", "todo", "missing")


def onboarding(root: Path, ag: Agent, *, issues: Optional[List[Issue]] = None) -> List[dict]:
    """入职四关的清单。

    死规矩：没实现的关卡显示 missing，绝不允许因为「没人拦」就算它过。
    第 2、3 关看的是 reports/ 里最新那份报告，并且**报告的版本要等于当前版本**——
    在 v1.0.0 上跑绿了、然后把 playbook 改了升到 v1.1.0，那一关就回到 todo。
    """
    if issues is None:
        issues = validate(ag)
    latest = latest_reports(root, ag.id)

    def from_suite(suite: Optional[str], name: str, why_missing: str) -> dict:
        if not suite:
            return {"name": name, "status": "missing", "suite": None, "detail": why_missing}
        rep = latest.get(suite)
        if not rep:
            return {"name": name, "status": "todo", "suite": suite,
                    "detail": f"套件 {suite} 存在，但这个 agent 还没跑过。"}
        rv = str(rep.get("version") or "")
        if rv and rv != ag.version:
            return {"name": name, "status": "todo", "suite": suite,
                    "detail": f"上次跑的是 v{rv}，当前是 v{ag.version}——改过之后没重跑。",
                    "at": rep.get("at")}
        ok = bool(rep.get("ok"))
        return {"name": name, "status": "pass" if ok else "fail", "suite": suite,
                "detail": (f"{rep.get('n')} 条全过" if ok else f"{rep.get('n_fail')} / {rep.get('n')} 条失败"),
                "at": rep.get("at")}

    n_fail = sum(1 for i in issues if i.severity == "fail")
    n_warn = sum(1 for i in issues if i.severity == "warn")
    gates = [{
        "key": "static",
        "name": "第一关 · 静态审查",
        "status": "fail" if n_fail else "pass",
        "detail": (f"{n_fail} 处不过" + (f"、{n_warn} 条警告" if n_warn else "")) if n_fail
                  else ("没有问题" + (f"（{n_warn} 条警告）" if n_warn else "")),
        "how": "后台自动，秒级，不调模型。",
    }]
    g2 = from_suite(GATE_SUITE, "第二关 · 闸门契约", "")
    g2.update({"key": "gate", "how": f"在「跑一套用例」里选 {GATE_SUITE}，要打真模型。"})
    gates.append(g2)

    suite3 = RAILS_SUITE.get(ag.rails)
    g3 = from_suite(suite3, "第三关 · 产品红线套件",
                    f"档位 {ag.rails or '（未填）'} 还没有对应的红线套件——这一关还没做。"
                    f"有套件的档位：{'、'.join(sorted(RAILS_SUITE))}。")
    g3.update({"key": "redline",
               "how": (f"在「跑一套用例」里选 {suite3}。" if suite3 else "先给这个档位写一套用例。")})
    gates.append(g3)

    acc = accuracy(root, ag)
    gates.append({
        "key": "judgment",
        "name": "第四关 · 判断测试",
        "status": "missing",            # 跑器不存在。这里写死，别让它有机会变成 pass
        "suite": None,
        "detail": acc["why"] + (f"（现在只能人看：{acc['n_recaps']} 场回顾、"
                                f"声明了 {len(acc['dims'])} 个判断维度）"),
        "how": "要实现：同一份对话喂给两个 agent，比较产出的判断差异。跑器没写，这一关一律算没做。",
    })
    return gates


def onboarding_summary(gates: List[dict]) -> dict:
    c = {s: 0 for s in GATE_STATUS}
    for g in gates:
        c[g["status"]] = c.get(g["status"], 0) + 1
    return {"passed": c["pass"], "total": len(gates), **c}


# ---------------------------------------------------------------- 壳绑定（后台可改）

def save_shells(root: Path, agent_id: str, shells: List[str]) -> dict:
    """把 shells 写回 manifest.yaml。冲突在这里挡一次——前端可以绕，接口不能。

    不升版本：壳绑定是部署关系，不是人设内容，跟「措辞改了要 patch」不是一回事。
    """
    root = Path(root)
    d = root / agent_id
    mp = d / "manifest.yaml"
    if not mp.exists():
        raise FileNotFoundError(agent_id)
    clean: List[str] = []
    for sh in shells:
        sh = str(sh).strip()
        if not sh:
            continue
        if not SHELL_RE.fullmatch(sh):
            raise ValueError(f"壳标识 {sh!r} 不合法：小写字母开头，只能小写字母/数字/连字符")
        if sh in clean:
            raise ValueError(f"壳 {sh} 重复了")
        clean.append(sh)
    owners = other_shell_owners(root, agent_id)
    for sh in clean:
        if sh in owners:
            raise ValueError(f"壳 {sh} 已被 {owners[sh]} 认领——一个壳只能归一个 agent")
    m = yaml.safe_load(mp.read_text(encoding="utf-8")) or {}
    if clean:
        m["shells"] = clean
    else:
        m.pop("shells", None)
    m["updated_at"] = datetime.now().strftime("%Y-%m-%d")
    mp.write_text(yaml.safe_dump(m, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return {"shells": clean}


def shell_map(root: Path) -> dict:
    """全库的壳 → agent。冲突单独列出来，页面上要能一眼看见撞了没有。"""
    ags = load_all(root)
    owners = shell_owners(ags)
    return {
        "bindings": [{"shell": sh, "agents": ids, "conflict": len(ids) > 1}
                     for sh, ids in sorted(owners.items())],
        "conflicts": sorted(sh for sh, ids in owners.items() if len(ids) > 1),
        "unbound": sorted(a.id for a in ags if not a.summary()["shells"]),
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


# ---------------------------------------------------------------- 落盘留痕：git 或导出

# 背景（TASKS.md 2026-09-14 部署记录）：服务器上 `/opt/voicekit-svc/agents/` 是 rsync 拷过去的，
# **不是 git 工作副本**。后果：后台「历史」一栏在线上永远是空的，改错了没法回滚，
# 而且下一次 rsync 部署会把线上改的悄悄盖掉——改动连存在过的痕迹都没有。
#
# 两条路，代码两条都给，部署时二选一（TASKS.md 末尾写了怎么选）：
#   A. 服务器上把 agents/ 变成 git 工作副本 + 打开 VOICEKIT_AGENTS_GIT_COMMIT=1
#      → 后台每次写盘顺手 commit 一笔，「历史」有了，回滚就是 git revert。
#   B. 做不到 A（没装 git / 不想给服务进程 git 权限）时：后台提供「导出改动」，
#      下载六层文件的 tar.gz 带回本地提交。**至少不让改动只活在那台机器上。**

def git_env() -> bool:
    """要不要在写盘后自动提交。默认关——不问自取地在别人机器上建提交是失礼的。"""
    import os
    return (os.environ.get("VOICEKIT_AGENTS_GIT_COMMIT") or "").strip().lower() in ("1", "true", "yes", "on")


def git_state(root: Path) -> dict:
    """agents/ 到底是不是 git 工作副本。页面靠它决定说什么话，别猜。"""
    root = Path(root)
    state = {"repo": False, "auto_commit": git_env(), "reason": "", "toplevel": ""}
    try:
        r = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=str(root),
                           capture_output=True, text=True, timeout=5)
    except FileNotFoundError:
        state["reason"] = "这台机器上没有 git。"
        return state
    except (OSError, subprocess.TimeoutExpired) as e:
        state["reason"] = f"git 跑不起来：{e}"
        return state
    if r.returncode != 0:
        state["reason"] = "agents/ 不在任何 git 仓库里——改动没有历史，也没法回滚。"
        return state
    state["repo"] = True
    state["toplevel"] = r.stdout.strip()
    if not state["auto_commit"]:
        state["reason"] = "是 git 工作副本，但没开自动提交（VOICEKIT_AGENTS_GIT_COMMIT=1）：改完记得手工提交。"
    return state


def git_commit(root: Path, agent_id: str, message: str) -> dict:
    """提交这一个 agent 目录。失败绝不抛——保存本身已经成功了，
    提交不上只是「这次改动没留痕」，页面提示一句即可，不能把保存也拖失败。

    `-c user.*` 是必须的：服务器上的服务账号多半没配 git 身份，
    没有它 commit 会以 "empty ident" 失败，而且这个错只在生产才出现。
    """
    root = Path(root)
    d = root / agent_id
    ident = ["-c", "user.name=voice-kit admin", "-c", "user.email=admin@voice-kit.local"]
    try:
        add = subprocess.run(["git"] + ident + ["add", "--"] + [str(d / l) for l in LAYERS],
                             cwd=str(root), capture_output=True, text=True, timeout=10)
        if add.returncode != 0:
            return {"ok": False, "reason": (add.stderr or add.stdout).strip()[:300]}
        # 带路径提交：暂存区里别人放了什么都不关我事（CLAUDE.md 并行会话纪律，同一个病）
        cm = subprocess.run(["git"] + ident + ["commit", "-m", message, "--"]
                            + [str(d / l) for l in LAYERS],
                            cwd=str(root), capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as e:
        return {"ok": False, "reason": str(e)[:300]}
    if cm.returncode != 0:
        txt = (cm.stdout + cm.stderr).strip()
        if "nothing to commit" in txt or "nothing added to commit" in txt or "no changes added" in txt:
            return {"ok": True, "noop": True, "reason": "内容没变，没产生提交。"}
        return {"ok": False, "reason": txt[:300]}
    head = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=str(root),
                          capture_output=True, text=True, timeout=5)
    return {"ok": True, "hash": head.stdout.strip() if head.returncode == 0 else ""}


def export_bundle(root: Path, agent_id: Optional[str] = None) -> bytes:
    """把六层文件打成 tar.gz，供「导出改动」下载。

    **只打六层文件**：sessions / reports / users 一个都不进包。
    那三个是真人对话和抽出来的事实，.gitignore 挡着它们进库，
    这里也一样挡——导出是给人带回去提交的，不是数据出口（CLAUDE.md 儿童数据红线）。
    """
    import io
    import tarfile
    root = Path(root)
    ids = [agent_id] if agent_id else [a.id for a in load_all(root)]
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for aid in ids:
            d = root / aid
            if not d.is_dir():
                raise FileNotFoundError(aid)
            for layer in LAYERS:
                p = d / layer
                if p.exists():
                    tf.add(str(p), arcname=f"agents/{aid}/{layer}")
    return buf.getvalue()
