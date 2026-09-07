"""音色解析：用户选择 > 角色默认 > 兜底。

为什么值得测：传一个不存在的 VoiceType 给腾讯，**任务照起、文字照出、
一点声音都没有，而且零报错**——跟 TTSConfig.AppId 传错是同一类故障，
最难查。所以这里必须保证"永远返回一个真实存在的音色 id"。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from voice_kit import voices as V


# ---------------------------------------------------------------- 优先级

def test_role_default_when_no_override():
    assert V.resolve("qiuqiu") == V.ROLE_DEFAULTS["qiuqiu"]
    assert V.resolve("coach") == V.ROLE_DEFAULTS["coach"]


def test_user_choice_beats_role_default():
    assert V.resolve("qiuqiu", override=502006) == 502006
    assert V.resolve("qiuqiu", override="502006") == 502006     # 表单传来的是字符串


def test_unknown_role_falls_back():
    assert V.resolve("还没定义的角色") == V.FALLBACK_VOICE


# ---------------------------------------------------------------- 坏值不能漏出去

def test_bad_override_falls_back_silently():
    """音色是体验问题，不该因为一个坏值让孩子那边完全没声音。
    但坏值也绝不能被当成有效值传下去。"""
    for bad in ["abc", None, "", -1, 999999, 3.5, {}, []]:
        got = V.resolve("qiuqiu", override=bad)
        assert got == V.ROLE_DEFAULTS["qiuqiu"], f"{bad!r} 没有回落"


def test_resolve_always_returns_a_real_voice():
    """最关键的一条：无论输入什么，返回的 id 必须真实存在。
    没有这条，上面那些回落测试都只是"看起来对"。"""
    inputs = [None, "abc", 999999, 502006, "502001", -5, 0]
    roles = ["qiuqiu", "coach", "assistant", "不存在的角色", ""]
    for r in roles:
        for i in inputs:
            assert V.resolve(r, override=i) in V.BY_ID


def test_every_role_default_is_a_real_voice():
    """角色表自己也可能写错编号——那种错误会一路静默到"没声音"。"""
    for role, vid in V.ROLE_DEFAULTS.items():
        assert vid in V.BY_ID, f"角色 {role} 的默认音色 {vid} 不在音色库里"
    assert V.FALLBACK_VOICE in V.BY_ID


# ---------------------------------------------------------------- 候选列表

def test_options_put_the_default_first():
    opts = V.role_options("qiuqiu")
    assert opts[0].id == V.ROLE_DEFAULTS["qiuqiu"]


def test_options_are_curated_not_everything():
    """**刻意不返回全部 21 个。** 让家长在全部音色里挑，等于把选型约束
    （不要童声、不要解说腔）扔给不知情的人。候选是产品判断，不是配置项。"""
    opts = V.role_options("qiuqiu")
    assert 0 < len(opts) < len(V.ALL)
    assert all(v.tier == "超自然" for v in opts), "候选里混进了非超自然档"


def test_no_peer_child_voice_in_options():
    """人设是"好奇的大朋友"。同龄童声会把"被看见"变成"被同龄人评价"——
    联调时孩子第二句话就问出了这个问题（当时默认是 101016 智甜·女童）。

    ⚠️ 「懂事少年」（603000·男童）是**有意保留**的：官方归类是男童，
    但实际是少年感，比纯童声大一档，仍在"大朋友"这一侧。
    所以这条只排除纯童声，不是排除所有带"童"字的。"""
    KEEP = {603000}          # 懂事少年——少年感，不是同龄童声
    for v in V.role_options("qiuqiu"):
        if v.id in KEEP:
            continue
        assert "童" not in v.gender, f"候选里混进了童声 {v.name}（{v.gender}）"


def test_options_have_no_duplicates():
    ids = [v.id for v in V.role_options("qiuqiu")]
    assert len(ids) == len(set(ids))
