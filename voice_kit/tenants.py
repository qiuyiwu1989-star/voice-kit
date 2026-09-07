"""多租户：一套服务，多个项目，各带各的人格和护栏。

## 为什么需要这一层

联调探针是单租户的——一份写死的 system prompt、一把所有人共用的钥匙、
配置全在 Python 里。那样每接一个新项目都要改代码重新部署，
而且**一把钥匙意味着没法单独吊销任何一个项目**。

公共能力必须在三件事上跟探针不同：

    每个项目有自己的钥匙   → 泄露/换人时只换那一把，不影响别人
    每个项目有自己的护栏   → 儿童产品严格、成人产品宽松，同一份代码
    配置在文件里不在代码里 → 加项目 = 改配置 + reload，不用发版

## 护栏是按项目配的，不是全局的

这一点是整个设计的核心。球球老师的 8 铁律不该套在 AI 教练身上，
反过来成人产品的宽松也绝不能漏到儿童产品。**所以规则跟着租户走。**

⚠️ 一条硬约束写在代码里：**配了 output_rules 就必须配 fallback_reply**。
否则违规被拦下时用户看到一片空白——静默失败比说错话更难查。

## 钥匙只存哈希

配置文件会进备份、会被 scp、会被人看一眼。存明文等于把所有项目的
凭据放在一个随手可见的地方。存 sha256 之后，泄露配置文件本身
不等于泄露钥匙。
"""
import hashlib
import hmac
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from .gate import Rule


def hash_key(raw: str) -> str:
    return hashlib.sha256((raw or "").encode()).hexdigest()


@dataclass
class Tenant:
    """一个项目在这套服务里的全部配置。"""

    id: str
    name: str
    key_sha256: str
    model: str = "deepseek-v4-flash"
    thinking: bool = False              # 语音场景默认关，见 tokenhub.py 的实测
    system_prompt: str = ""
    output_rules: List[Rule] = field(default_factory=list)
    input_rules: List[Rule] = field(default_factory=list)
    fallback_reply: str = ""
    on_input_trigger_reply: str = ""
    tts_voice: Optional[int] = None
    voice_role: str = "assistant"
    max_tokens: int = 1024
    # 可选：把这个租户的每一轮交给宿主应用处理（落库/采样/报告）
    hook: str = ""

    def check(self) -> None:
        if self.output_rules and not self.fallback_reply:
            raise ValueError(
                f"[{self.id}] 配了 output_rules 就必须配 fallback_reply——"
                "否则违规被拦下时用户看到一片空白，比说错话更难查")
        if self.input_rules and not (self.on_input_trigger_reply or self.fallback_reply):
            raise ValueError(f"[{self.id}] 配了 input_rules 就得有接管话术")


def _rules(raw) -> List[Rule]:
    """配置里的规则形状：{name, words[], patterns[]}。patterns 是正则字符串。"""
    out = []
    for r in raw or []:
        out.append(Rule(
            name=r.get("name") or "未命名规则",
            words=tuple(r.get("words") or ()),
            patterns=tuple(re.compile(p) for p in (r.get("patterns") or ())),
        ))
    return out


class Registry:
    """租户表。从 JSON 文件加载，支持热重载（加项目不用发版）。"""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._by_id: Dict[str, Tenant] = {}
        self._by_key: Dict[str, Tenant] = {}
        self._mtime = 0.0
        self.reload()

    def reload(self) -> int:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raw = {"tenants": []}
        by_id, by_key = {}, {}
        for t in raw.get("tenants", []):
            tenant = Tenant(
                id=t["id"], name=t.get("name") or t["id"],
                key_sha256=t["key_sha256"],
                model=t.get("model", "deepseek-v4-flash"),
                thinking=bool(t.get("thinking", False)),
                system_prompt=t.get("system_prompt", ""),
                output_rules=_rules(t.get("output_rules")),
                input_rules=_rules(t.get("input_rules")),
                fallback_reply=t.get("fallback_reply", ""),
                on_input_trigger_reply=t.get("on_input_trigger_reply", ""),
                tts_voice=t.get("tts_voice"),
                voice_role=t.get("voice_role", "assistant"),
                max_tokens=int(t.get("max_tokens", 1024)),
                hook=t.get("hook", ""),
            )
            tenant.check()          # 配错了在加载时就炸，不要等到线上有人调
            by_id[tenant.id] = tenant
            by_key[tenant.key_sha256] = tenant
        self._by_id, self._by_key = by_id, by_key
        try:
            self._mtime = self.path.stat().st_mtime
        except OSError:
            self._mtime = 0.0
        return len(by_id)

    def maybe_reload(self) -> None:
        """文件变了就重载。加一个项目 = 改配置文件，不用重启服务。"""
        try:
            m = self.path.stat().st_mtime
        except OSError:
            return
        if m != self._mtime:
            self.reload()

    def by_key(self, raw_key: str) -> Optional[Tenant]:
        """按钥匙找租户。

        用 `compare_digest` 逐个比而不是字典直接查——字典查的耗时跟命中
        与否有关，理论上能被用来探测。这里租户数量很少，全表比一遍不值钱。
        """
        if not raw_key:
            return None
        h = hash_key(raw_key)
        for k, t in self._by_key.items():
            if hmac.compare_digest(k, h):
                return t
        return None

    def by_id(self, tid: str) -> Optional[Tenant]:
        return self._by_id.get(tid)

    def all(self) -> List[Tenant]:
        return list(self._by_id.values())
