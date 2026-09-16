"""testkit：跟产品无关的对话质量回归。

## 它解决什么

球球老师有 `test_8rules_regression.py`，AI 教练有 `try:reply` / `forbidden.ts`，
两边各写了一套"喂几句话、看回复有没有踩线"。共性模块上线后 agent 可替换，
**每换一个 agent 就得重跑一遍同样的检查**——那套检查就不该再属于任何一个产品。

    用例（各产品自带）  ×  目标（服务 / 进程内 app / 任意 python 函数）  →  报告

## 三个部件

    scenario.py   用例格式。JSON，一条 = 铺垫历史 + 用户这句 + 期望
    target.py     被测目标。只要能"吃 messages 吐 token 流"就能测
    checks.py     判定。禁忌词（带否定白名单）、泄漏标记、不打分、听众只描述、
                  整句收尾、首字预算、接管、SSE 协议
    runner.py     跑 + 汇总；report.py 出 markdown / html

## 不做什么

不评价"答得好不好"——那要人或 LLM 裁判，且每个产品口径不同。
这里只做**机械可判**的部分：踩没踩线、漏没漏、慢不慢、协议对不对。
"""
from .scenario import Scenario, Suite, load_suite
from .target import Target, ServiceTarget, AppTarget, CallableTarget, TurnResult
from .runner import run_suite, SuiteReport, CaseReport
from .report import to_markdown, to_html

__all__ = ["Scenario", "Suite", "load_suite", "Target", "ServiceTarget", "AppTarget",
           "CallableTarget", "TurnResult", "run_suite", "SuiteReport", "CaseReport",
           "to_markdown", "to_html"]
