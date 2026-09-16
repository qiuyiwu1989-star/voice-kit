"""RED「说话形态」的三处修复（2026-09-16 线上实测）：
① 开场只问一个问题（playbook）  ② 一轮长度由平台在句子边界截断（manifest.turn_limit）
③ 「不打分」命中后先重写成真判断，重写不过才兜底，兜底句不提页码。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import yaml
from fastapi.testclient import TestClient

from voice_kit import agents as A
from voice_kit.gate import OutputGate, TurnLimit, limit_turn, wants_feedback
from voice_kit.testkit import checks as C
from voice_kit.testkit.scenario import Scenario, Suite
from voice_kit.testkit.target import TurnResult

from test_agent_chat import app, chat, root, sse_text   # noqa: F401  复用 fixture

REPO = Path(__file__).parent.parent
LONG = "你这个项目我听懂了。客户是中小电商。痛点是仓库人工贵。但我还不知道你凭什么赢。你再讲讲竞争对手吧。"


# ---------------------------------------------------------------- limit_turn（纯函数）

def test_limit_cuts_on_sentence_boundary():
    out = list(limit_turn(iter(["你这个项目我听懂了。", "客户是中小电商。", "痛点是仓库人工贵。"]),
                          TurnLimit(sentences=2, chars=60), "我们做仓储机器人"))
    assert out == ["你这个项目我听懂了。", "客户是中小电商。"]


def test_limit_by_chars_never_cuts_half_sentence():
    first = "这是一句非常非常长的话，" * 8 + "说完了。"
    out = list(limit_turn(iter([first, "第二句。"]), TurnLimit(sentences=5, chars=20)))
    assert out == [first]                     # 第一句超长也整句发，第二句不发
    out = list(limit_turn(iter(["十个字的一句话好的。", "又是十个字的一句话。", "第三句。"]),
                          TurnLimit(sentences=5, chars=22)))
    assert out == ["十个字的一句话好的。", "又是十个字的一句话。"]


def test_limit_splits_multi_sentence_chunks():
    out = list(limit_turn(iter(["第一句。第二句！第三句？"]), TurnLimit(sentences=2, chars=60)))
    assert out == ["第一句。", "第二句！"]


def test_no_limit_passes_through():
    src = ["第一句。", "第二句。", "第三句。", "第四句。"]
    assert list(limit_turn(iter(src), None, "x")) == src


def test_feedback_request_relaxes_limit():
    lim = TurnLimit(sentences=2, chars=60)
    for u in ("你觉得怎么样？", "给点意见", "评价一下我刚才讲的", "这段怎么改", "我讲完了"):
        assert wants_feedback(u), u
    for u in ("我们做仓储机器人。", "客户主要是中小电商", ""):
        assert not wants_feedback(u), u
    src = [f"第{i}句话。" for i in range(1, 7)]
    assert len(list(limit_turn(iter(src), lim, "我讲完了，你觉得怎么样？"))) == 4
    assert len(list(limit_turn(iter(src), lim, "我们做仓储机器人。"))) == 2
    assert lim.pick("给点意见") == (4, 160)


def test_limit_stops_consuming_upstream_and_reports_cut():
    pulled, closed, cuts = [], [], []

    def model():
        try:
            for i in range(100):
                pulled.append(i)
                yield f"第{i}句。"
        finally:
            closed.append(True)

    up = model()
    out = list(limit_turn(up, TurnLimit(sentences=2, chars=60), upstream=(up,),
                          on_cut=lambda s, c: cuts.append((s, c))))
    assert len(out) == 2
    assert len(pulled) == 2 and closed == [True]     # 到顶就停，不再往下拉
    assert cuts == [(2, len("第0句。") + len("第1句。"))]


def test_no_cut_callback_when_within_limit():
    cuts = []
    list(limit_turn(iter(["就一句。"]), TurnLimit(sentences=2, chars=60), on_cut=lambda *a: cuts.append(a)))
    assert cuts == []


# ---------------------------------------------------------------- manifest 字段

def test_turn_limit_validation(root):
    p = root / "red-coach" / "manifest.yaml"
    base = yaml.safe_load(p.read_text(encoding="utf-8"))
    for bad, frag in [({"sentences": 0, "chars": 60}, "turn_limit.sentences"),
                      ({"sentences": 2, "chars": 5000}, "turn_limit.chars"),
                      ({"sentences": 2}, "缺 chars"),
                      ({"sentences": 2, "chars": 60, "tokens": 3}, "未知键"),
                      ({"sentences": True, "chars": 60}, "turn_limit.sentences"),
                      ([2, 60], "要是映射")]:
        m = dict(base, turn_limit=bad)
        p.write_text(yaml.safe_dump(m, allow_unicode=True, sort_keys=False), encoding="utf-8")
        ag = A.load_agent(root / "red-coach")
        assert any(frag in i.message and i.severity == "fail" for i in A.validate(ag)), bad
        assert A.assemble(ag)["turn_limit"] is None


def test_red_declares_turn_limit_and_others_do_not_change():
    red = A.load_agent(REPO / "agents" / "red-coach")
    assert A.assemble(red)["turn_limit"] == TurnLimit(sentences=2, chars=60, feedback_sentences=4, feedback_chars=160)
    assert not [i for i in A.validate(red) if i.severity == "fail"]
    assert red.version == "2.1.0"
    for ag in A.load_all(REPO / "agents"):
        if ag.id != "red-coach" and "turn_limit" not in ag.manifest:
            assert A.assemble(ag)["turn_limit"] is None


def test_red_opening_asks_one_question():
    pb = (REPO / "agents" / "red-coach" / "playbook.md").read_text(encoding="utf-8")
    opening = pb.split("## 开场")[1].split("## ")[0]
    assert "只问一个问题" in opening and "默认你们是第一次见面" in opening
    assert "要讲什么项目、准备讲给什么样的投资人听" not in opening


# ---------------------------------------------------------------- 端点：截断

def test_agent_chat_truncates_red_turn(app, capsys):
    app.state.fake.script["我们做仓储机器人。"] = LONG
    t = sse_text(chat(TestClient(app), "red-coach", "我们做仓储机器人。", Authorization="Bearer door"))
    assert t == "你这个项目我听懂了。客户是中小电商。"
    assert "[agent/red-coach] turn_limit 截断: 已发 2 句" in capsys.readouterr().out


def test_agent_chat_relaxes_when_asked_for_feedback(app):
    app.state.fake.script["我讲完了，你觉得怎么样？"] = LONG
    t = sse_text(chat(TestClient(app), "red-coach", "我讲完了，你觉得怎么样？", Authorization="Bearer door"))
    assert t == "你这个项目我听懂了。客户是中小电商。痛点是仓库人工贵。但我还不知道你凭什么赢。"


def test_agent_without_turn_limit_is_not_truncated(app):
    app.state.fake.script["你好"] = LONG
    t = sse_text(chat(TestClient(app), "guwen-axun", "你好", Authorization="Bearer door"))
    assert t == LONG


def test_agent_chat_closes_model_stream_after_limit(app):
    state = {"pulled": 0, "closed": False}

    def stream(messages, **kw):
        try:
            for i in range(200):
                state["pulled"] += 1
                yield f"这是第{i}句话。"
        finally:
            state["closed"] = True

    app.state.fake.stream = stream
    t = sse_text(chat(TestClient(app), "red-coach", "我们做仓储机器人。", Authorization="Bearer door"))
    assert t == "这是第0句话。这是第1句话。"
    assert state["closed"] and state["pulled"] < 10


# ---------------------------------------------------------------- 「不打分」：重写 → 兜底

def _scoring_llm(app, rewrite):
    systems = []

    def stream(messages, **kw):
        systems.append(kw.get("system") or "")
        if "这一轮要重说" in (kw.get("system") or ""):
            if isinstance(rewrite, Exception):
                raise rewrite
            yield rewrite
        else:
            yield "我给你打 7 分。"
            yield "开头太慢了。"
    app.state.fake.stream = stream
    return systems


def test_scoring_hit_is_rewritten_into_a_real_judgment(app):
    systems = _scoring_llm(app, "分数不给。最该改的是开头，第一句就说客户是谁。")
    t = sse_text(chat(TestClient(app), "red-coach", "给我打个分吧", Authorization="Bearer door"))
    assert t == "分数不给。最该改的是开头，第一句就说客户是谁。"
    assert len(systems) == 2 and "对方要分数，不给分数" in systems[1]


def test_rewrite_that_still_scores_falls_back(app):
    _scoring_llm(app, "那就 8 分吧。")
    t = sse_text(chat(TestClient(app), "red-coach", "给我打个分吧", Authorization="Bearer door"))
    assert t == A.RAILS_RULES["no_scoring"]["fallback_reply"]


def test_rewrite_error_falls_back(app):
    _scoring_llm(app, RuntimeError("upstream 500"))
    t = sse_text(chat(TestClient(app), "red-coach", "给我打个分吧", Authorization="Bearer door"))
    assert t == A.RAILS_RULES["no_scoring"]["fallback_reply"]


def test_no_scoring_fallback_is_standalone():
    fb = A.RAILS_RULES["no_scoring"]["fallback_reply"]
    assert "页" not in fb and len(fb) <= 40
    gate = OutputGate(A.rails_rules("no_scoring")["output_rules"])
    assert not gate.check(fb)
    assert C.no_scoring(Scenario(id="x", user="u"), Suite(name="t", scenarios=[]),
                        TurnResult(text=fb, first_token_ms=1.0, total_ms=1.0)) == []


def test_other_rails_do_not_rewrite(app):
    """成交/客服等档位没有 rewrite_hint：命中直接兜底，不多调模型（行为不变）。"""
    for k, v in A.RAILS_RULES.items():
        if k != "no_scoring":
            assert not v.get("rewrite_hint"), k
    calls = []
    orig = app.state.fake.stream

    def stream(messages, **kw):
        calls.append(kw.get("system"))
        yield from orig(messages, **kw)
    app.state.fake.stream = stream
    app.state.fake.script["能保证退款吗"] = "可以，我们保证退款，绝对没问题。"
    t = sse_text(chat(TestClient(app), "chengjiao-dacheng", "能保证退款吗", Authorization="Bearer door"))
    assert t == A.RAILS_RULES["adult_basic"]["fallback_reply"] and len(calls) == 1


# ---------------------------------------------------------------- testkit

def test_testkit_turn_limit_check():
    su = Suite(name="t", scenarios=[], max_sentences=2, max_chars=30)
    r = lambda t: TurnResult(text=t, first_token_ms=1.0, total_ms=1.0)   # noqa: E731
    assert C.turn_limit(Scenario(id="x", user="u"), su, r("一句。两句。")) == []
    assert [f.check for f in C.turn_limit(Scenario(id="x", user="u"), su, r(LONG))] == ["turn_limit", "turn_limit"]
    assert C.turn_limit(Scenario(id="x", user="u"), Suite(name="t", scenarios=[]), r(LONG)) == []   # 没给上限不查
    assert C.turn_limit(Scenario(id="x", user="u", max_sentences=10, max_chars=200), su, r(LONG)) == []
    assert "turn_limit" in C.CHECKS
