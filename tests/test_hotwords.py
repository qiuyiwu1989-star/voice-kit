"""热词：拼得对、砍得对、不越界。

不联网。这里测的全是**会真的咬人的东西**：
腾讯的 128 词上限、我们自己的 URL 字节上限、以及超限时砍谁。
"""
import sys
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from voice_kit import hotwords as HW


def words_of(s: str):
    return [x.split("|")[0] for x in s.split(",")] if s else []


def test_format_is_what_tencent_expects():
    got = HW.build("qiuqiu")
    for piece in got.split(","):
        word, _, weight = piece.partition("|")
        assert word and weight.isdigit()
        assert 1 <= int(weight) <= 11 or int(weight) == 100
        assert len(word) <= HW.MAX_CHARS


def test_session_words_win_over_the_static_list():
    """**这条是热词的全部价值所在。**

    静态词表覆盖"大多数孩子会说什么"；这次会话的词覆盖"这个孩子会说
    什么"——他的名字、他的猫、他同桌的外号。后者值钱得多，
    所以挤不下的时候砍前者。
    """
    mine = [HW.Hotword(n, 11) for n in ("王梓萱", "咪咪", "小胖墩")]
    got = words_of(HW.build("qiuqiu", mine))
    assert got[:3] == ["王梓萱", "咪咪", "小胖墩"]


def test_url_length_is_bounded_even_when_flooded():
    """实时 ASR 把热词塞在 WebSocket URL 里。

    中文 percent-encode 后一个字 9 字节，128 个十字词约 12000 字符——
    握手会直接失败，而且失败得很难查：不是 401，是连不上。
    所以词数上限之外还必须守字节上限。
    """
    flood = [HW.Hotword("阿" * 10, 10) for _ in range(300)]
    got = HW.build("qiuqiu", flood)
    assert len(urllib.parse.quote(got, safe="")) <= HW.MAX_ENCODED
    assert len(words_of(got)) <= HW.MAX_WORDS


def test_there_is_real_room_left_for_a_child_s_own_name():
    """第一版这里是负的：静态词表把字节吃光了，孩子的名字一个都进不去，
    而按词数算的余量还显示"还剩 8 位"——**报表骗了人**。"""
    assert HW.static_budget("qiuqiu") > 300, "静态词表又胖了，先砍它"


def test_common_words_stay_out_of_the_list():
    """热词的本分是补模型的盲区，不是把它已经会的再说一遍。

    给常用词加权是双输：占掉字节预算，还可能把别的词误改成它。
    """
    got = words_of(HW.build("qiuqiu"))
    for common in ("语文", "数学", "英语", "跳绳", "期中", "听写"):
        assert common not in got, f"{common} 是通用词，ASR 本来就认得"


def test_bad_words_are_dropped_not_crashed():
    bad = [HW.Hotword("有|竖线", 10), HW.Hotword("有,逗号", 10),
           HW.Hotword("", 10), HW.Hotword("权重越界", 99),
           HW.Hotword("太长了" * 20, 10)]
    got = words_of(HW.build("qiuqiu", bad))
    assert not any(w.startswith(("有|", "有,", "太长了")) for w in got)
    assert "权重越界" not in got


def test_roles_get_different_vocabularies():
    kid, coach = words_of(HW.build("qiuqiu")), words_of(HW.build("coach"))
    assert "蛋仔派对" in kid and "蛋仔派对" not in coach
    assert "复盘" in coach and "复盘" not in kid
    assert HW.build("assistant") == "", "没配词表的角色不该硬塞"


def test_the_product_s_own_name_is_in_but_not_maxed():
    """认错自己的名字最难堪，所以要放；但**故意不给 11**——
    "求求你"和"球球你"是真同音，权重拉满会把孩子的求助语气改掉。"""
    got = HW.build("qiuqiu")
    assert "球球|10" in got
    assert "球球|11" not in got and "球球|100" not in got
