"""报告：markdown 给 CI 和 PR，html 给人看。都不依赖任何库。"""
from __future__ import annotations

import html
from datetime import datetime

from .runner import SuiteReport


def _stats_line(rep: SuiteReport) -> str:
    s = rep.first_token_stats()
    if not s:
        return "首字：无数据"
    return f"首字 P50 {s['p50']:.0f}ms · P90 {s['p90']:.0f}ms · 最慢 {s['max']:.0f}ms（{s['n']} 条）"


def to_markdown(rep: SuiteReport) -> str:
    lines = [f"# {rep.suite.name} · {'✅ 通过' if rep.ok else '❌ ' + str(rep.n_fail) + ' 条失败'}",
             "",
             f"- 目标：`{rep.target}`",
             f"- 用例：{rep.n}，失败 {rep.n_fail}，警告 {rep.n_warn}",
             f"- {_stats_line(rep)}",
             f"- 时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}",
             ""]
    lines += ["| 用例 | 规则 | 结果 | 首字 | 问题 |", "|---|---|---|---|---|"]
    for c in rep.cases:
        mark = "❌" if c.failed else ("⚠️" if c.warned else "✅")
        ft = f"{c.result.first_token_ms:.0f}ms" if c.result.first_token_ms is not None else "—"
        probs = "<br>".join(f"{f.check}: {f.message}" for f in c.findings
                            if f.severity != "skip") or ""
        lines.append(f"| {c.scenario.id} | {c.scenario.rule} | {mark} | {ft} | {probs} |")
    lines += ["", "## 回复原文", ""]
    for c in rep.cases:
        lines += [f"**{c.scenario.id}** 「{c.scenario.user}」", "",
                  f"> {c.result.text.strip() or '（空）'}", ""]
    return "\n".join(lines)


def to_html(rep: SuiteReport) -> str:
    e = html.escape
    rows = []
    for c in rep.cases:
        cls = "fail" if c.failed else ("warn" if c.warned else "ok")
        ft = f"{c.result.first_token_ms:.0f}" if c.result.first_token_ms is not None else "—"
        probs = "<br>".join(f"<code>{e(f.check)}</code> {e(f.message)}" for f in c.findings
                            if f.severity != "skip")
        rows.append(f"<tr class='{cls}'><td>{e(c.scenario.id)}</td><td>{e(c.scenario.rule)}</td>"
                    f"<td>{e(c.scenario.user)}</td><td class='reply'>{e(c.result.text.strip())}</td>"
                    f"<td>{ft}</td><td>{probs}</td></tr>")
    status = "通过" if rep.ok else f"{rep.n_fail} 条失败"
    return f"""<!doctype html><meta charset="utf-8"><title>{e(rep.suite.name)}</title>
<style>
body{{font:14px/1.6 -apple-system,system-ui,sans-serif;margin:32px;color:#222}}
h1{{font-size:20px}} .sum{{color:#555;margin-bottom:16px}}
table{{border-collapse:collapse;width:100%}} th,td{{border-bottom:1px solid #e5e5e5;padding:8px;vertical-align:top;text-align:left}}
th{{background:#fafafa}} .reply{{max-width:38em;white-space:pre-wrap}}
tr.fail td:first-child{{border-left:4px solid #d33}} tr.warn td:first-child{{border-left:4px solid #e9a400}}
tr.ok td:first-child{{border-left:4px solid #2a9d5c}} code{{background:#f3f3f3;padding:0 4px}}
</style>
<h1>{e(rep.suite.name)} · {status}</h1>
<div class="sum">目标 <code>{e(rep.target)}</code> · 用例 {rep.n} · 失败 {rep.n_fail} · 警告 {rep.n_warn} · {e(_stats_line(rep))}
· {datetime.now().strftime('%Y-%m-%d %H:%M')}</div>
<table><tr><th>用例</th><th>规则</th><th>用户说</th><th>回复</th><th>首字 ms</th><th>问题</th></tr>
{''.join(rows)}</table>"""
