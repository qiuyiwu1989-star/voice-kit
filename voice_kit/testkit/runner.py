"""跑一套用例，出一份报告。"""
from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import List, Optional

from .checks import CHECKS, Finding
from .scenario import Scenario, Suite
from .target import CallableTarget, Target, TurnResult

# 这些检查要闸门在场才有意义；CallableTarget 绕过了闸门
_GATE_CHECKS = {"takeover", "gated", "sse_ok"}


@dataclass
class CaseReport:
    scenario: Scenario
    result: TurnResult
    findings: List[Finding] = field(default_factory=list)

    @property
    def failed(self) -> bool:
        return any(f.severity == "fail" for f in self.findings)

    @property
    def warned(self) -> bool:
        return any(f.severity == "warn" for f in self.findings)


@dataclass
class SuiteReport:
    suite: Suite
    target: str
    cases: List[CaseReport]

    @property
    def n(self): return len(self.cases)
    @property
    def n_fail(self): return sum(c.failed for c in self.cases)
    @property
    def n_warn(self): return sum((not c.failed) and c.warned for c in self.cases)
    @property
    def ok(self) -> bool: return self.n_fail == 0

    def first_token_stats(self) -> Optional[dict]:
        xs = [c.result.first_token_ms for c in self.cases if c.result.first_token_ms is not None]
        if not xs:
            return None
        xs.sort()
        return {"n": len(xs), "p50": statistics.median(xs),
                "p90": xs[min(len(xs) - 1, int(round(0.9 * len(xs))) - 1)] if len(xs) > 1 else xs[0],
                "max": xs[-1]}


def run_case(sc: Scenario, su: Suite, target: Target) -> CaseReport:
    res = target.turn(sc.messages(), headers=sc.headers or None)
    findings: List[Finding] = []
    bypass_gate = isinstance(target, CallableTarget)
    for name in su.effective_checks(sc):
        fn = CHECKS.get(name)
        if fn is None:
            findings.append(Finding(name, f"未知检查器 {name!r}", severity="warn"))
            continue
        if bypass_gate and name in _GATE_CHECKS:
            findings.append(Finding(name, "目标绕过闸门，跳过", severity="skip"))
            continue
        findings.extend(fn(sc, su, res))
    # 用例声明了 expect_* 但套件没启用对应检查——自动补上，别让期望悄悄失效
    for flag, name in (("expect_takeover", "takeover"), ("expect_gated", "gated")):
        if getattr(sc, flag) is None or name in su.effective_checks(sc):
            continue
        if bypass_gate:
            findings.append(Finding(name, "目标绕过闸门，跳过", severity="skip"))
        else:
            findings.extend(CHECKS[name](sc, su, res))
    if sc.must_avoid and "must_avoid" not in su.effective_checks(sc):
        findings.extend(CHECKS["must_avoid"](sc, su, res))
    if sc.must_contain and "must_contain" not in su.effective_checks(sc):
        findings.extend(CHECKS["must_contain"](sc, su, res))
    return CaseReport(sc, res, findings)


def run_suite(su: Suite, target: Target, *, only: Optional[List[str]] = None) -> SuiteReport:
    cases = [run_case(sc, su, target) for sc in su.scenarios
             if not only or sc.id in only]
    return SuiteReport(su, getattr(target, "name", "?"), cases)
