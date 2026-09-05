"""可信度分层 + 诱导识别。

起因是一次真实事件：一个叛逆期的孩子（Tony）在对话里把话题引向摩托车。
叛逆期的孩子会**故意对 AI 使坏**——试探边界、编故事、诱导 AI 说不该说的话。
这不是异常，这是这个年龄段的正常行为，产品必须假设它会发生。

两个**必须分开**的问题：

┌─ 诱导（manipulation）────────────────────────────────────────┐
│ 孩子想让 AI 说不该说的话。                                     │
│ 可检测——诱导有句式特征（"假装你是…"、"所以你也觉得…对吧"）。   │
│ 对策：识别 + 不顺着走。                                        │
└──────────────────────────────────────────────────────────────┘

┌─ 编造（fabrication）─────────────────────────────────────────┐
│ 孩子吹牛/说谎。                                                │
│ **不可检测**——AI 没有任何办法判断"我骑过摩托车"是真是假。      │
│ 对策：**不要试图判断真假**，而是不让单次陈述直接升级成"事实"。 │
└──────────────────────────────────────────────────────────────┘

第二条是这个模块真正的设计主张。做错的方式是加一个"说谎检测器"——
那只会产生一个自信的错误分类器，比没有更糟（把说真话的孩子标成撒谎，
是这个产品能犯的最严重的错误之一）。

正确的方式是**分层采信**：

    自述一次        → unverified   可以聊，不写进报告结论
    跨会话重复出现  → corroborated 可以进报告
    家长/老师确认   → trusted      可以当事实
    高风险主题      → needs_review 无论真假都要人看

最后一条是关键。摩托车这个例子：
- 如果是真的 → 这是**安全信号**，家长必须知道
- 如果是编的 → 这是**行为信号**，也值得关注
**两种情况都需要人来判断，都不该被 AI 静默写进报告当成既定事实。**
所以不需要判断真假，只需要认出"这条不该自动通过"。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional, Pattern, Sequence

# ============================================================================
# 诱导识别
# ============================================================================

# 只放**句式**特征，不放主题词。
# 主题词（摩托车、抽烟…）属于下面的高风险主题，两者作用不同：
# 句式说明"孩子在试图操纵 AI"，主题说明"这条内容需要人看"。
MANIPULATION_PATTERNS: list[tuple[str, Pattern]] = [
    # 间隔放到 12——"你现在是我的同学，不是老师"中间隔了 7 个字，卡 6 会漏
    ("角色注入", re.compile(r"(假装|假设|扮演|你现在是|你不是).{0,12}(你是|一个|老师|机器人|人类|同学)")),
    ("规则绕过", re.compile(r"(忽略|不用管|不要管|别管).{0,8}(规则|之前|上面|设定|限制)")),
    ("越权试探", re.compile(r"你(敢不敢|能不能|会不会|可不可以).{0,10}(说脏话|骂人|说坏话|不听话)")),
    ("背书诱导", re.compile(r"(所以|那)?你也(觉得|认为|同意).{0,20}(对吧|是吧|对不对|吧)")),
    ("替我背锅", re.compile(r"(是你说的|你让我|你说可以|你同意了)")),
    ("假设脱责", re.compile(r"如果我(说|做|告诉你).{0,12}(你会|你能|会怎样|会怎么样)")),
]


@dataclass
class ManipulationResult:
    detected: bool
    kind: Optional[str] = None
    matched: Optional[str] = None

    def __bool__(self) -> bool:
        return self.detected


def detect_manipulation(text: str) -> ManipulationResult:
    """识别孩子是否在尝试操纵 AI。

    命中**不等于**要惩罚孩子——试探边界是这个年龄的正常行为。
    正确的反应是"不顺着走 + 不说教"，而不是"你怎么可以这样"。
    调用方拿到结果后应该往 system prompt 注入一句提醒，而不是拒绝对话。
    """
    t = (text or "").strip()
    for kind, pat in MANIPULATION_PATTERNS:
        m = pat.search(t)
        if m:
            return ManipulationResult(True, kind, m.group(0))
    return ManipulationResult(False)


# ============================================================================
# 可信度分层
# ============================================================================

TIER_UNVERIFIED = "unverified"      # 孩子自述一次：可以聊，不进报告结论
TIER_CORROBORATED = "corroborated"  # 跨会话重复：可以进报告
TIER_TRUSTED = "trusted"            # 家长/老师确认：可当事实
TIER_NEEDS_REVIEW = "needs_review"  # 高风险：无论真假都要人看

# 高风险主题：命中即 needs_review，永不自动进报告。
# 判定标准不是"危险"，是"**如果是真的，大人必须知道；如果是编的，也值得关注**"。
# 两种情况都需要人，所以不需要判断真假。
HIGH_RISK_TOPICS: dict[str, Sequence[str]] = {
    "危险行为": ["摩托车", "电动车带人", "骑车上路", "翻墙", "爬窗", "天台", "打火机", "放火"],
    "成瘾物质": ["抽烟", "吸烟", "喝酒", "电子烟", "槟榔"],
    "暴力冲突": ["打架", "被打", "拿刀", "报复", "揍他"],
    "离家出走": ["离家出走", "不想回家", "跑出去", "不回去了"],
    "陌生人接触": ["见网友", "网上认识的", "加我微信", "叔叔给我"],
    "金钱异常": ["借钱", "网贷", "偷钱", "拿了钱"],
}


@dataclass
class Claim:
    """孩子（或家长/老师）陈述的一条内容。"""
    text: str
    category: str = "event"          # event / preference / constraint / relation / attempt
    source: str = "child"            # child / parent / teacher
    session_id: Optional[str] = None


@dataclass
class Verdict:
    tier: str
    reasons: list[str] = field(default_factory=list)
    risk_topic: Optional[str] = None
    corroborations: int = 1

    @property
    def may_enter_report(self) -> bool:
        """能不能写进给家长的报告结论。

        needs_review 永远不能自动进——它要么是安全事件要么是行为信号，
        两种都该由人来决定怎么跟家长说，不是让 AI 在报告里轻描淡写一句。
        """
        return self.tier in (TIER_CORROBORATED, TIER_TRUSTED)


def _hit_risk_topic(text: str) -> Optional[str]:
    for topic, words in HIGH_RISK_TOPICS.items():
        for w in words:
            if w in text:
                return topic
    return None


def assess(claim: Claim, *, prior_sessions: Sequence[str] = ()) -> Verdict:
    """判定一条陈述的采信层级。

    prior_sessions: 这条内容此前在哪些 session 里出现过（由调用方按语义或
    关键词匹配算出来）。**跨会话重复是唯一可靠的佐证信号**——
    孩子可能吹一次牛，但隔几天在不同话题下又说到同一件事，可信度就高得多。

    注意这里没有任何"说谎检测"。刻意不做——见模块 docstring。
    """
    reasons: list[str] = []

    risk = _hit_risk_topic(claim.text)
    if risk:
        return Verdict(TIER_NEEDS_REVIEW,
                       [f"命中高风险主题「{risk}」：无论真假都需要人判断"],
                       risk_topic=risk,
                       corroborations=len(set(prior_sessions)) + 1)

    if claim.source in ("parent", "teacher"):
        return Verdict(TIER_TRUSTED, [f"来源是{claim.source}，非孩子自述"])

    # 当前 session 不能给自己作证：同一次对话里说三遍，正是吹牛的典型形态，
    # 而不是"重复出现所以更可信"。必须是**不同**会话里各自提到才算佐证。
    priors = set(prior_sessions)
    priors.discard(claim.session_id)
    n = len(priors) + 1
    if n >= 2:
        reasons.append(f"跨 {n} 个 session 重复出现")
        return Verdict(TIER_CORROBORATED, reasons, corroborations=n)

    # 偏好类风险低——说错了也无害，不必卡着
    if claim.category == "preference":
        reasons.append("偏好类陈述，说错无害")
        return Verdict(TIER_CORROBORATED, reasons, corroborations=n)

    reasons.append("孩子单次自述，尚无旁证")
    return Verdict(TIER_UNVERIFIED, reasons, corroborations=n)


def report_prefix(verdict: Verdict) -> str:
    """给报告渲染层用：该怎么措辞才诚实。

    空字符串 = 可以直接陈述。否则必须带上这个前缀，
    不能把"孩子说他骑过摩托车"写成"他骑过摩托车"。
    """
    if verdict.tier == TIER_TRUSTED:
        return ""
    if verdict.tier == TIER_CORROBORATED:
        return ""
    if verdict.tier == TIER_UNVERIFIED:
        return "他提到过（我们还没有其他佐证）："
    return "⚠️ 这条需要您亲自确认："
