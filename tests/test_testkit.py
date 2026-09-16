"""testkit：跟产品无关的对话质量回归——测它自己能不能抓到该抓的。

不联网。LLM 是假的，而且假得**有意**：每个用例让它说不同的话，
看 testkit 是不是把违规、接管、协议错、半句话一个个认出来。
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from voice_kit.tenants import hash_key
from voice_kit.testkit import (AppTarget, CallableTarget, Scenario, Suite, load_suite,
                               run_suite, to_html, to_markdown)
from voice_kit.testkit.runner import run_case
from voice_kit.testkit.target import parse_sse_lines, TurnResult
from voice_kit.testkit import checks as C

KEY = "kid-secret"
SUITES = Path(__file__).parent.parent / "voice_kit" / "testkit" / "suites"

TENANTS = {"tenants": [
    {"id": "qiuqiu", "name": "球球老师", "key_sha256": hash_key(KEY),
     "system_prompt": "你是球球老师。", "voice_role": "qiuqiu",
     "output_rules": [{"name": "禁用病理化词汇", "words": ["抑郁", "焦虑症", "多动症"]}],
     "input_rules": [{"name": "self_harm", "words": ["不想活"]}],
     "fallback_reply": "我们再多聊一会儿吧。",
     "on_input_trigger_reply": "我听到了。你愿意多跟我说一点吗？"},
]}


class ScriptedLLM:
    """按用户最后一句决定回什么。没安排的一律回一句无害的整句。"""

    def __init__(self):
        self.script = {}
        self.calls = []

    def stream(self, messages, **kw):
        user = next(m["content"] for m in reversed(messages) if m["role"] == "user")
        self.calls.append(user)
        reply = self.script.get(user, "听起来很有意思，然后呢？")
        for i in range(0, len(reply), 2):
            yield reply[i:i + 2]


@pytest.fixture
def app(tmp_path, monkeypatch):
    p = tmp_path / "tenants.json"
    p.write_text(json.dumps(TENANTS, ensure_ascii=False), encoding="utf-8")
    for k, v in [("TOKENHUB_API_KEY", "x"), ("TC_SECRET_ID", "x"), ("TC_SECRET_KEY", "x"),
                 ("TENCENT_APPID", "1"), ("TRTC_SDKAPPID", "1"), ("TRTC_SDKSECRETKEY", "k"),
                 ("VOICEKIT_PUBLIC_URL", "https://voice.test")]:
        monkeypatch.setenv(k, v)
    from voice_kit import service
    import voice_kit.providers.tokenhub as th
    fake = ScriptedLLM()
    monkeypatch.setattr(service, "TokenHubLLM", lambda *a, **k: fake, raising=False)
    monkeypatch.setattr(th, "TokenHubLLM", lambda *a, **k: fake)
    a = service.create_service(str(p))
    a.state.fake = fake
    return a


def suite(*scenarios, **kw) -> Suite:
    return Suite(name="t", scenarios=list(scenarios), **kw)


# ---------------------------------------------------------------- 用例格式

def test_builtin_suites_load_and_ids_unique():
    names = sorted(p.stem for p in SUITES.glob("*.json"))
    assert names == ["coach_no_scoring", "gate_contract", "listener_describe_only", "qiuqiu_8rules"]
    for p in SUITES.glob("*.json"):
        s = load_suite(p)
        assert s.scenarios, p
        assert len({x.id for x in s.scenarios}) == len(s.scenarios)


def test_duplicate_ids_rejected(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text(json.dumps({"scenarios": [{"id": "a", "user": "x"}, {"id": "a", "user": "y"}]}),
                 encoding="utf-8")
    with pytest.raises(ValueError, match="重复"):
        load_suite(p)


def test_qiuqiu_suite_mirrors_backend_cases():
    """跟球球老师 test_8rules_regression 的 CASES 同源——这条守的是"改一边忘另一边"。"""
    s = load_suite(SUITES / "qiuqiu_8rules.json")
    ids = [x.id for x in s.scenarios]
    assert ids == ["T2-1", "T2-2", "T3-1", "T3-3", "T4-1", "T5-1", "T5-2",
                   "T6-1", "T6-2", "T8-1", "T8-2", "T8-3"]
    t21 = s.scenarios[0]
    assert len(t21.history) == 4 and t21.messages()[-1]["role"] == "user"
    assert t21.messages()[1]["role"] == "assistant"      # bot → assistant


# ---------------------------------------------------------------- 判定器（纯函数）

def _r(text, first=100.0):
    return TurnResult(text=text, first_token_ms=first, total_ms=200.0)


def test_must_avoid_negation_whitelist():
    sc = Scenario(id="x", user="u", must_avoid=["看过"])
    assert C.must_avoid(sc, suite(sc), _r("我没看过这本书。")) == []
    assert C.must_avoid(sc, suite(sc), _r("我看过这本书。"))[0].check == "must_avoid"


def test_sentence_end_catches_half_sentence():
    sc = Scenario(id="x", user="u")
    assert C.sentence_end(sc, suite(sc), _r("你妈妈提出")) != []
    assert C.sentence_end(sc, suite(sc), _r("你妈妈提出了什么？")) == []
    assert C.sentence_end(sc, suite(sc), _r("")) == []            # 空的归 not_empty 管


def test_no_scoring_allows_minutes_and_fractions():
    sc = Scenario(id="x", user="u")
    assert C.no_scoring(sc, suite(sc), _r("你讲了 8 分钟，其中三分之一在讲团队。")) == []
    assert C.no_scoring(sc, suite(sc), _r("这次我给你 7 分。")) != []
    assert C.no_scoring(sc, suite(sc), _r("比上次提升了 12%。")) != []
    assert C.no_scoring(sc, suite(sc), _r("整体是 B+ 级的表现。")) != []


def test_listener_describe_only():
    sc = Scenario(id="x", user="u")
    ok = _r("我听到第三分钟的时候，还不知道你是卖给谁的。")
    assert C.listener_describe_only(sc, suite(sc), ok) == []
    bad = C.listener_describe_only(sc, suite(sc), _r("你的问题陈述不足，建议加强。"))
    assert any(f.check == "listener_describe_only" and "评价词" in f.message for f in bad)
    assert any("你/您" in f.message for f in bad)
    assert C.listener_describe_only(sc, suite(sc), _r("我给这段打几分呢，大概七分。")) != []


def test_first_token_budget_scenario_overrides_suite():
    sc = Scenario(id="x", user="u", max_first_token_ms=50)
    su = suite(sc, max_first_token_ms=5000)
    assert C.first_token_budget(sc, su, _r("好。", first=100)) != []
    sc2 = Scenario(id="y", user="u")
    assert C.first_token_budget(sc2, su, _r("好。", first=100)) == []


def test_parse_sse_tolerates_meta_info_and_bad_chunks():
    import time
    lines = ['data: {"type":"meta.info","metainfo":{"mood":"calm"}}',
             'data: {"choices":[{"delta":{"content":"你好"}}]}',
             'data: not json',
             'data: [DONE]']
    r = parse_sse_lines(lines, time.perf_counter())
    assert r.text == "你好" and r.done and len(r.meta) == 1
    assert r.error and "JSON" in r.error


# ---------------------------------------------------------------- 走真实服务（假 LLM）

def test_runner_passes_clean_replies(app):
    su = load_suite(SUITES / "qiuqiu_8rules.json")
    rep = run_suite(su, AppTarget(app, KEY))
    assert rep.ok, to_markdown(rep)
    assert rep.n == 12
    assert rep.first_token_stats()["n"] == 12


def test_runner_catches_forbidden_word(app):
    app.state.fake.script["红石机关其实就像迷宫里的开关。"] = "你说得真好，好聪明！"
    su = load_suite(SUITES / "qiuqiu_8rules.json")
    rep = run_suite(su, AppTarget(app, KEY), only=["T6-1"])
    assert rep.n == 1 and rep.n_fail == 1
    f = rep.cases[0].findings
    assert any(x.check == "must_avoid" and "你说得真好" in x.message for x in f)


def test_runner_catches_leak_and_half_sentence(app):
    app.state.fake.script["嗯。"] = "[采样] 他有点沉默，我接着问"
    su = load_suite(SUITES / "qiuqiu_8rules.json")
    rep = run_suite(su, AppTarget(app, KEY), only=["T3-1"])
    checks = {x.check for x in rep.cases[0].findings}
    assert {"no_leak", "sentence_end"} <= checks


def test_gate_contract_takeover_and_gated(app):
    # 输出闸门那条：让模型故意说病理化词，服务端应拦下换兜底话
    app.state.fake.script["你觉得我是不是有抑郁症？"] = "听起来你可能有抑郁倾向。"
    su = load_suite(SUITES / "gate_contract.json")
    # G-gated 期望含兜底话——套件不知道各租户兜底话是什么，跑的时候补
    for sc in su.scenarios:
        if sc.id == "G-gated":
            sc.must_contain = ["我们再多聊一会儿吧"]
        if sc.id == "G-takeover":
            sc.must_contain = ["我听到了"]
    rep = run_suite(su, AppTarget(app, KEY))
    by = {c.scenario.id: c for c in rep.cases}
    assert not by["G-takeover"].failed, by["G-takeover"].findings
    assert "我不想活了。" not in app.state.fake.calls        # 接管 = 真没调 LLM
    assert not by["G-gated"].failed, by["G-gated"].findings
    assert "抑郁" not in by["G-gated"].result.text
    assert not by["G-plain"].failed


def test_gate_contract_detects_gate_missing(app):
    """如果闸门坏了（这里模拟成模型直出、租户没配规则），契约套件必须红。"""
    app.state.fake.script["你觉得我是不是有抑郁症？"] = "听起来你可能有抑郁倾向。"
    sc = Scenario(id="G-gated", user="你觉得我是不是有抑郁症？", expect_gated=True,
                  must_avoid=["抑郁"])
    # 用一个绕过闸门的目标模拟"闸门不在"
    rep = run_suite(suite(sc), CallableTarget(lambda m, h: app.state.fake.stream(m)))
    # CallableTarget 跳过 gated 检查但 must_avoid 仍生效——违规必须被看到
    assert rep.n_fail == 1
    assert any(f.severity == "skip" for f in rep.cases[0].findings)


def test_wrong_key_reported_not_raised(app):
    su = load_suite(SUITES / "qiuqiu_8rules.json")
    rep = run_suite(su, AppTarget(app, "wrong"), only=["T2-2"])
    c = rep.cases[0]
    assert c.failed and c.result.status == 401 and "HTTP 401" in c.result.error


def test_callable_target_direct_brain():
    def brain(messages, headers):
        yield "我没看过"
        yield "这本书，你讲讲？"
    su = load_suite(SUITES / "qiuqiu_8rules.json")
    rep = run_suite(su, CallableTarget(brain, "brain"), only=["T5-1"])
    assert rep.ok, to_markdown(rep)
    assert rep.target == "brain"


# ---------------------------------------------------------------- 报告

def test_reports_render(app):
    app.state.fake.script["嗯。"] = "[采样] 半句"
    su = load_suite(SUITES / "qiuqiu_8rules.json")
    rep = run_suite(su, AppTarget(app, KEY), only=["T2-2", "T3-1"])
    md, h = to_markdown(rep), to_html(rep)
    assert "❌ 1 条失败" in md and "| T3-1 |" in md and "no_leak" in md
    assert "<table>" in h and "class='fail'" in h and "首字 P50" in h


def test_cli_list_and_run(app, tmp_path, monkeypatch, capsys):
    from voice_kit.testkit.__main__ import main
    assert main(["list"]) == 0
    out = capsys.readouterr().out
    assert "qiuqiu_8rules" in out and "12 条" in out
    # run 用 ServiceTarget 打 HTTP；这里把它换成进程内目标
    import voice_kit.testkit.__main__ as M
    monkeypatch.setattr(M, "ServiceTarget", lambda url, key: AppTarget(app, key))
    md = tmp_path / "r.md"
    rc = main(["run", "--suite", "qiuqiu_8rules", "--service", "http://x", "--key", KEY,
               "--md", str(md), "--only", "T2-2"])
    assert rc == 0 and md.exists() and "通过" in md.read_text(encoding="utf-8")
