"""用例格式。

一条用例 = 铺垫历史 + 用户这句话 + 对回复的期望。全部是数据，没有代码，
所以球球老师、AI 教练、第三方 agent 作者都能写，而且能 review、能 diff。

    {
      "id": "T6-1",
      "rule": "铁律#6 不空夸",                   # 给人看的
      "history": [["user", "..."], ["bot", "..."]],
      "user": "红石机关其实就像迷宫里的开关。",
      "must_avoid": ["你说得真好", "好聪明"],
      "must_contain": [],                          # 任一命中即可
      "expect_takeover": false,                    # 期望输入闸门接管（不调 LLM）
      "expect_gated": false,                       # 期望输出闸门至少拦一次
      "max_first_token_ms": 4000,
      "checks": ["no_leak", "sentence_end"]        # 额外启用的检查器名
    }

套件级别可以给所有用例统一的 `checks` / `leak_markers` / `max_first_token_ms`，
用例自己写的覆盖套件的。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Sequence, Tuple


@dataclass
class Scenario:
    id: str
    user: str
    rule: str = ""
    history: List[Tuple[str, str]] = field(default_factory=list)   # (role, text)，role ∈ user/bot
    must_avoid: List[str] = field(default_factory=list)
    must_contain: List[str] = field(default_factory=list)
    expect_takeover: Optional[bool] = None
    expect_gated: Optional[bool] = None
    max_first_token_ms: Optional[float] = None
    checks: List[str] = field(default_factory=list)
    headers: dict = field(default_factory=dict)      # 传给目标的额外头（X-User-Id 之类）

    def messages(self) -> List[dict]:
        """铺垫 + 这句，OpenAI 形状。bot 映射成 assistant。"""
        out = []
        for role, text in self.history:
            out.append({"role": "assistant" if role in ("bot", "assistant") else "user",
                        "content": text})
        out.append({"role": "user", "content": self.user})
        return out


@dataclass
class Suite:
    name: str
    scenarios: List[Scenario]
    checks: List[str] = field(default_factory=lambda: ["no_leak", "must_avoid",
                                                        "must_contain", "not_empty"])
    leak_markers: List[str] = field(default_factory=list)
    max_first_token_ms: Optional[float] = None
    # 否定白名单：禁忌词前面跟着这些字就不算命中（"我没看过"里的"看过"）
    negations: Sequence[str] = ("没", "不", "未", "没有")
    source: str = ""

    def effective_checks(self, sc: Scenario) -> List[str]:
        seen, out = set(), []
        for c in list(self.checks) + list(sc.checks):
            if c not in seen:
                seen.add(c)
                out.append(c)
        return out


def _scenario(raw: dict) -> Scenario:
    hist = [(h[0], h[1]) if isinstance(h, (list, tuple)) else (h["role"], h["text"])
            for h in raw.get("history", [])]
    return Scenario(
        id=str(raw["id"]), user=raw["user"], rule=raw.get("rule", ""), history=hist,
        must_avoid=list(raw.get("must_avoid", [])),
        must_contain=list(raw.get("must_contain", [])),
        expect_takeover=raw.get("expect_takeover"),
        expect_gated=raw.get("expect_gated"),
        max_first_token_ms=raw.get("max_first_token_ms"),
        checks=list(raw.get("checks", [])),
        headers=dict(raw.get("headers", {})),
    )


def load_suite(path) -> Suite:
    p = Path(path)
    raw = json.loads(p.read_text(encoding="utf-8"))
    if "scenarios" not in raw:
        raise ValueError(f"{p}: 套件缺 scenarios")
    ids = [str(s["id"]) for s in raw["scenarios"]]
    dup = {i for i in ids if ids.count(i) > 1}
    if dup:
        raise ValueError(f"{p}: 用例 id 重复 {sorted(dup)}")
    kw = {}
    for k in ("checks", "leak_markers", "max_first_token_ms", "negations"):
        if k in raw:
            kw[k] = raw[k]
    return Suite(name=raw.get("name", p.stem),
                 scenarios=[_scenario(s) for s in raw["scenarios"]],
                 source=str(p), **kw)
