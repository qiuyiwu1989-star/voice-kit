"""话题清洗和兜底选择的测试。

这两个函数错了的后果都是**直接被 TTS 念给孩子听**：
一个会念出 "L1-22" 这种系统编码，一个会在孩子道别时答非所问。
所以边界条件要钉死，不能靠"看起来没问题"。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "deploy"))

from qq_text import (FALLBACK_BY_RULE, FALLBACK_DEFAULT, MAX_TOPIC_LEN,
                     fallback_for, humanize_topic)


# ---------------------------------------------------------------- 话题清洗

def test_strips_curriculum_code():
    """实拍到的那一条：开场白把 L1-22 念了出来。"""
    assert humanize_topic("L1-22·折纸进阶挑战（龙到凤凰）") == "折纸进阶挑战"


def test_strips_colon_variants():
    assert humanize_topic("L3-07：家庭关系") == "家庭关系"
    assert humanize_topic("T5:同伴交往") == "同伴交往"


def test_leaves_clean_topic_alone():
    assert humanize_topic("秘密分享意愿") == "秘密分享意愿"


def test_nested_code_is_stripped_too():
    """编码可能嵌套，剥一次不够——这条以前会漏出 'x·奇怪的东西'。"""
    assert humanize_topic("TOPIC2024-x·奇怪的东西") == "奇怪的东西"


def test_returns_empty_when_cannot_clean():
    """**返回空是正常结果**，调用方退回笼统开场白。
    宁可说"今天想聊点啥"，也不要念出编码。"""
    assert humanize_topic("ABC123") == ""
    assert humanize_topic("L1-22") == ""
    assert humanize_topic("") == ""
    assert humanize_topic(None) == ""


def test_rejects_overlong_topic():
    """太长的多半是整段描述被塞进来了，念出来很怪。"""
    long_one = "折纸" * (MAX_TOPIC_LEN)
    assert humanize_topic(long_one) == ""


def test_rejects_pure_ascii():
    """一个汉字都没有，多半还是编码/英文标签，不适合直接念。"""
    assert humanize_topic("origami-advanced") == ""


def test_no_code_leaks_through_any_case():
    """反证：所有含编码的输入，输出里都不能再有编码痕迹。
    没有这条，上面几条可能只是恰好覆盖到了。"""
    import re
    cases = ["L1-22·折纸", "L3-07：家庭", "TOPIC2024-x·奇怪的东西",
             "A1-1·甲", "ZZ99：乙", "秘密分享意愿"]
    for c in cases:
        out = humanize_topic(c)
        assert not re.search(r"[A-Za-z]{2,}\d|\d+[-_]\d+", out), f"{c!r} → {out!r} 仍带编码"


# ---------------------------------------------------------------- 兜底话术

def test_goodbye_gets_a_goodbye_shaped_reply():
    """实测教训：孩子在道别，通用兜底回"我们换个话题聊聊吧"——答非所问。"""
    got = fallback_for(["铁律 #3：禁止主动结束对话"])
    assert got == FALLBACK_BY_RULE["铁律 #3：禁止主动结束对话"]
    assert "先去忙" in got and "我不走的" in got
    assert got != FALLBACK_DEFAULT


def test_each_known_rule_has_its_own_line():
    """每条规则的兜底都必须不一样——全一样就等于没分开写。"""
    outs = [fallback_for([r]) for r in FALLBACK_BY_RULE]
    assert len(set(outs)) == len(FALLBACK_BY_RULE)
    assert FALLBACK_DEFAULT not in outs


def test_unknown_rule_falls_back_to_default():
    assert fallback_for(["某条以后新增的规则"]) == FALLBACK_DEFAULT
    assert fallback_for([]) == FALLBACK_DEFAULT
    assert fallback_for(None) == FALLBACK_DEFAULT


def test_first_matching_rule_wins():
    got = fallback_for(["不认识的规则", "禁用病理化词汇", "铁律 #3：禁止主动结束对话"])
    assert got == FALLBACK_BY_RULE["禁用病理化词汇"]


def test_no_fallback_is_itself_a_violation():
    """兜底话术自己不能撞禁用词——否则拦下之后换的那句又违规，
    而语音端已经没有第二次机会了。"""
    banned = ["下次再聊", "拜拜", "再见", "今天就到这里", "祝你愉快",
              "小朋友", "你真棒", "抑郁", "焦虑", "风险"]
    for name, line in list(FALLBACK_BY_RULE.items()) + [("默认", FALLBACK_DEFAULT)]:
        hit = [w for w in banned if w in line]
        assert not hit, f"兜底话术「{name}」自己命中了禁用词 {hit}"
