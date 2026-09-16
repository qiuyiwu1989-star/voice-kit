"""命令行。

    python -m voice_kit.testkit run --suite voice_kit/testkit/suites/qiuqiu_8rules.json \
        --service https://voice.qiuyiwu.com --key $VOICEKIT_KEY --md out.md --html out.html

    python -m voice_kit.testkit list            # 自带的套件
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from .report import to_html, to_markdown
from .runner import run_suite
from .scenario import load_suite
from .target import ServiceTarget

SUITES_DIR = Path(__file__).parent / "suites"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="voice_kit.testkit")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    r = sub.add_parser("run")
    r.add_argument("--suite", required=True, help="套件 JSON 路径，或自带套件名（不带 .json）")
    r.add_argument("--service", required=True, help="voice-kit 服务地址")
    r.add_argument("--key", default=os.environ.get("VOICEKIT_KEY", ""), help="项目钥匙")
    r.add_argument("--only", nargs="*", help="只跑这些用例 id")
    r.add_argument("--md"); r.add_argument("--html")
    a = ap.parse_args(argv)

    if a.cmd == "list":
        for p in sorted(SUITES_DIR.glob("*.json")):
            s = load_suite(p)
            print(f"{p.stem:28s} {len(s.scenarios):3d} 条  {s.name}")
        return 0

    path = Path(a.suite)
    if not path.exists() and (SUITES_DIR / f"{a.suite}.json").exists():
        path = SUITES_DIR / f"{a.suite}.json"
    if not a.key:
        print("缺项目钥匙：--key 或 VOICEKIT_KEY", file=sys.stderr)
        return 2
    su = load_suite(path)
    rep = run_suite(su, ServiceTarget(a.service, a.key), only=a.only)
    md = to_markdown(rep)
    if a.md:
        Path(a.md).write_text(md, encoding="utf-8")
    if a.html:
        Path(a.html).write_text(to_html(rep), encoding="utf-8")
    print(md if not a.md else f"{su.name}: {'通过' if rep.ok else str(rep.n_fail) + ' 条失败'} → {a.md}")
    return 0 if rep.ok else 1


if __name__ == "__main__":
    sys.exit(main())
