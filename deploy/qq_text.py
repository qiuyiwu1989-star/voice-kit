"""球球老师桥接里的纯文本处理——刻意跟重依赖分开。

`qiuqiu_bridge` 一 import 就会拉起整个球球老师后端（agent / db / safety，
连带 DATABASE_URL 和一堆配置）。这些函数本身跟那些东西毫无关系，
挤在一起的后果是**它们没法被单独测**——而它们恰恰是最需要测的那种代码：
纯逻辑、有边界条件、错了会直接被 TTS 念给孩子听。

所以拆出来。这个文件不 import 任何重东西。
"""
import re
from typing import Iterable, Optional

# 课程编码形如 "L1-22·折纸进阶挑战（龙到凤凰）"——那是给后台看的。
# 2026-09-05 联调实拍：开场白把 "L1-22" 原样念给孩子听了。
# 文字端只是难看，语音端 TTS 会一个字母一个字母念出来，直接暴露系统黑话。
_TOPIC_CODE = re.compile(r"^[A-Za-z]+\d*(?:[-_]\d+)?\s*[·:：\-]\s*")
_TRAILING_PAREN = re.compile(r"[（(][^）)]*[）)]\s*$")
_LEFTOVER_CODE = re.compile(r"[A-Za-z]{2,}\d|\d+[-_]\d+")

MAX_TOPIC_LEN = 20


def humanize_topic(raw: Optional[str]) -> str:
    """把话题串洗成能念出口的样子；洗不干净就返回空字符串。

    **返回空是正常结果，不是失败**——调用方应该退回一句笼统的开场白。
    宁可说"今天想聊点啥"，也不要念出 "L1-22"。

    多剥一轮是因为编码可能嵌套（"TOPIC2024-x·奇怪的东西" 剥一次还剩 "x·…"）。
    """
    if not raw:
        return ""
    t = str(raw).strip()
    for _ in range(3):                      # 嵌套编码，多剥几轮
        stripped = _TOPIC_CODE.sub("", t).strip()
        if stripped == t:
            break
        t = stripped
    t = _TRAILING_PAREN.sub("", t).strip()
    if not t or len(t) > MAX_TOPIC_LEN or _LEFTOVER_CODE.search(t):
        return ""
    if not re.search(r"[一-鿿]", t):   # 一个汉字都没有，多半还是编码
        return ""
    return t


# 闸门拦下后说什么。**必须按违反的规则分开写**——2026-09-05 实测教训：
# 孩子说"拜拜啦球球，我下次再来找你玩"，模型回了句违规的告别，
# 闸门拦下、换成通用兜底「我们换个话题聊聊吧」，结果答非所问——
# 孩子在道别，球球在转话题。
#
# 文字端可以非流式重生一次（生产版 stream_message 就是这么做的），
# 语音端不行：前面的句子已经出声了，收不回。所以只剩这一条路——
# 让兜底话术**知道自己在替什么话收场**。
FALLBACK_BY_RULE = {
    "铁律 #3：禁止主动结束对话":
        "你要是得走了就先去忙，我不走的——回来接着跟我说这件事好吗？",
    "禁用病理化词汇":
        "嗯，我听着呢。你愿意多讲讲那是什么感觉吗？我想听你自己怎么说。",
    "禁用防御型话术（卖潜能不卖避险）":
        "我更想知道你自己怎么看这件事——你觉得最有意思的地方在哪？",
    "称呼应使用'同学'":
        "我还是叫你同学吧。刚才那件事，你接着说？",
    "禁止标签化（应使用'倾向于'）":
        "我不太想给你贴一个标签——你自己觉得，你是那种什么样的人？",
    "铁律 #6：禁止空夸（必须带具体内容）":
        "先别急着让我夸——你跟我讲讲，这里面哪一步是最难的？",
}
FALLBACK_DEFAULT = "……我们换个话题聊聊吧，你刚才说的那个我还想再听听。"


def fallback_for(violated_rules: Optional[Iterable[str]]) -> str:
    """挑一句"接得上"的兜底。

    命中多条时取第一条能对上的——规则之间没有优先级之分，
    但至少要比一句放之四海皆准的废话强。
    """
    for rule in (violated_rules or []):
        if rule in FALLBACK_BY_RULE:
            return FALLBACK_BY_RULE[rule]
    return FALLBACK_DEFAULT
