"""把页面脚本在假 DOM 里真跑一遍顶层。node 不在就跳过——但 CI 机器上应该有。"""
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).parent.parent
STUB = REPO / "tests" / "js" / "dom_stub.mjs"

PLACEHOLDERS = {"__MODE__": "voice", "__AGENT__": "qiuqiu", "__SHORT__": "球球", "__NAME__": "球球老师",
                "__VOICE__": "502001", "__INITIAL__": "球", "__BLURB__": "",
                "__IDENT__": '{"zh": {"name": "球球老师", "short": "球球", "blurb": "", "initial": "球"}, "en": {"name": "Qiuqiu", "short": "Qiuqiu", "blurb": "", "initial": "Q"}}'}


@pytest.mark.skipif(not shutil.which("node"), reason="没有 node")
@pytest.mark.parametrize("page,kind", [("talk.html", "module"), ("me.html", "classic")])
def test_page_script_loads_in_stub_dom(tmp_path, page, kind):
    src = (REPO / "voice_kit" / page).read_text(encoding="utf-8")
    for k, v in PLACEHOLDERS.items():
        src = src.replace(k, v)
    tag = '<script type="module">' if kind == "module" else "<script>"
    js = re.search(re.escape(tag) + r"(.*?)</script>", src, re.S).group(1)
    js = (js.replace('from "/talk/lib/recorder.js"', 'from "./recorder_stub.mjs"')
            .replace('from "/talk/lib/speechQueue.js"', 'from "./sq_stub.mjs"'))
    (tmp_path / "recorder_stub.mjs").write_text("export function createRecorder(){ return {start(){},pause(){},resume(){},stop(){}}; } export function recorderSupported(){ return false; }")
    (tmp_path / "sq_stub.mjs").write_text("export function createSpeechQueue(){ return {push(){},end(){},stop(){}}; } export function pcm16PostFetcher(){ return () => {}; } export function speechSupported(){ return false; }")
    run = tmp_path / "run.mjs"
    run.write_text(STUB.read_text(encoding="utf-8") + "\n" + js + "\nconsole.log('LOADED_OK'); setTimeout(() => process.exit(0), 50);\n", encoding="utf-8")
    r = subprocess.run(["node", str(run)], capture_output=True, text=True, timeout=30, cwd=str(tmp_path))
    assert r.returncode == 0 and "LOADED_OK" in r.stdout, (r.stdout[-400:], r.stderr[-1200:])
