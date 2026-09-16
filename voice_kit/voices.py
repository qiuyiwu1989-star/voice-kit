"""腾讯 TTS 音色库（中文），按"给谁听"分组。

## 为什么单独做一份

音色不是参数，是**角色的一部分**。孩子对声音的认知比大人以为的强得多——
2026-09-05 联调时，孩子第二句话就问「你为什么是这个声音呢？你能换个声音吗」，
当时用的是默认的 `101016 智甜（女童）`。

那个默认值本身就是个错误：球球老师的人设是「一个好奇的**大朋友**」，
不是同龄小孩、也不是老师。用女童声等于人设自相矛盾——
孩子会觉得"跟我一样大的人在装懂"。

## 三档的区别

    超自然大模型音色（502xxx / 60xxxx）  最自然，支持 24k 采样率，聊天场景专门调过
    大模型音色（501xxx / 601xxx）        次之，部分支持情感（EmotionCategory）
    精品音色（101xxx）                   传统拼接式，只到 16k，成本低

语音陪伴类产品应该用第一档：孩子每天听几十分钟，机械感是会累积的。

⚠️ 下面的编号整理自官方文档，**没有逐个试听过**。真选之前请用
联调页的音色下拉当场 A/B——文字描述（"聊天女声"）跟实际听感差得很远。
"""
from dataclasses import dataclass
from typing import List, Optional


@dataclass(frozen=True)
class Voice:
    id: int
    name: str
    gender: str            # 男 / 女 / 男童 / 女童
    tier: str              # 超自然 / 大模型 / 精品
    scene: str
    note: str = ""


# ---------------------------------------------------------------- 超自然大模型

NATURAL: List[Voice] = [
    Voice(502001, "智小柔", "女", "超自然", "聊天女声"),
    Voice(502003, "智小敏", "女", "超自然", "聊天女声"),
    Voice(502004, "智小满", "女", "超自然", "营销女声", "偏推销腔，儿童场景慎用"),
    Voice(502005, "智小解", "男", "超自然", "解说男声", "解说腔，像纪录片旁白"),
    Voice(502006, "智小悟", "男", "超自然", "聊天男声"),
    Voice(502007, "智小虎", "男童", "超自然", "聊天童声", "童声——跟「大朋友」人设冲突"),
    Voice(602004, "暖心阿灿", "男", "超自然", "聊天男声"),
    Voice(602005, "专业梓欣", "女", "超自然", "聊天女声", "偏正式，可能有距离感"),
    Voice(603000, "懂事少年", "男童", "超自然", "特色男声", "少年感，比纯童声大一点"),
    Voice(603003, "随和老李", "男", "超自然", "聊天男声", "年纪偏大"),
    Voice(603004, "温柔小柠", "女", "超自然", "聊天女声"),
]

# ---------------------------------------------------------------- 大模型

LARGE: List[Voice] = [
    Voice(501000, "智斌", "男", "大模型", "阅读男声"),
    Voice(501001, "智兰", "女", "大模型", "资讯女声"),
    Voice(501002, "智菊", "女", "大模型", "阅读女声"),
    Voice(501003, "智宇", "男", "大模型", "阅读男声"),
    Voice(501004, "月华", "女", "大模型", "聊天女声"),
    Voice(501005, "飞镜", "男", "大模型", "聊天男声"),
    # 下面三个是 AI 教练 lib/agent/spec.ts 里真调验过的（2026-09-10，StartAIConversation 收、中英文同一个人）。
    # 不在表里的 id 会被 resolve 静默换成默认嗓子——两个「老板」就成了同一个声音（2026-09-16 发现），所以补进来。
    # 名字是按音色特点起的描述名，不是腾讯官方名。
    Voice(501006, "清亮男声", "男", "大模型", "聊天男声", "年轻一些，适合同侪 / 面试官"),
    Voice(501008, "沉稳男声", "男", "大模型", "聊天男声", "稳，压得住场"),
    Voice(601002, "对话男声", "男", "大模型", "聊天男声", "60 档音质更细，更口语"),
]

# ---------------------------------------------------------------- 精品（传统）

PREMIUM: List[Voice] = [
    Voice(101015, "智萌", "男童", "精品", "男童声"),
    Voice(101016, "智甜", "女童", "精品", "女童声", "**当前默认，建议换掉**"),
    Voice(101013, "智辉", "男", "精品", "新闻男声"),
    Voice(101055, "智付", "女", "精品", "通用女声"),
]

ALL: List[Voice] = NATURAL + LARGE + PREMIUM
BY_ID = {v.id: v for v in ALL}


# ---------------------------------------------------------------- 选型建议

# 球球老师的候选：人设是"好奇的大朋友"——比孩子大、但不是老师。
# 所以排除童声（同龄感）、排除解说/资讯腔（距离感）、排除老年音色。
QIUQIU_SHORTLIST = [502001, 502003, 603004, 502006, 602004, 603000]

# 三条硬约束，写在这里免得下次有人凭"好听"选：
#   1. 不要童声——人设是大朋友，同龄声会让"被看见"变成"被同龄人评价"
#   2. 不要解说/资讯腔——那是单向广播的声音，这个产品是双向对话
#   3. 优先超自然档——孩子每天听几十分钟，机械感会累积


def shortlist() -> List[Voice]:
    return [BY_ID[i] for i in QIUQIU_SHORTLIST if i in BY_ID]


def get(voice_id: int) -> Optional[Voice]:
    return BY_ID.get(voice_id)


def describe(voice_id: int) -> str:
    v = BY_ID.get(voice_id)
    return f"{v.name}（{v.gender}·{v.tier}·{v.scene}）" if v else f"未知音色 {voice_id}"


# ---------------------------------------------------------------- 角色 → 默认音色
#
# 音色属于**角色**，不属于产品。同一个产品里可以有多个角色（球球老师、
# 未来的学科助手、家长端的顾问），各自该有各自的声音。
# 所以这张表按角色而不是按产品分。
#
# 用户可以覆盖（见 resolve()）——2026-09-07 用户拍板"音色可以用户自己选择
# 切换，或者后台未来不同的角色有默认的音色配置"。两件事都在这里落地：
# 表是默认，override 是用户选择，resolve 决定谁赢。

ROLE_DEFAULTS = {
    "qiuqiu":        502001,   # 球球老师 · 智小柔（女·超自然·聊天）——"好奇的大朋友"
    "qiuqiu_male":   502006,   # 球球老师男声版 · 智小悟，给偏好男声的孩子
    "coach":         602004,   # AI 教练 · 暖心阿灿（男·超自然）
    "assistant":     501004,   # 通用助手 · 月华（女·大模型·聊天）
}
FALLBACK_VOICE = 502001


def resolve(role: str = "qiuqiu", override=None) -> int:
    """定这次该用哪个音色。

    优先级：**用户的选择 > 角色默认 > 兜底**。

    `override` 无效时**静默回落到角色默认**，不抛异常——音色是体验问题，
    不该因为一个坏值让孩子那边完全没声音。但坏值也不能被当成有效值传给
    腾讯：传一个不存在的 VoiceType，任务照起、文字照出、**一点声音都没有**
    且零报错（2026-09-05 在 TTSConfig.AppId 上踩过同一类坑）。
    """
    if override is not None:
        try:
            vid = int(override)
        except (TypeError, ValueError):
            vid = None
        if vid in BY_ID:
            return vid
    return ROLE_DEFAULTS.get(role, FALLBACK_VOICE)


def role_options(role: str = "qiuqiu"):
    """给这个角色可以选的音色列表——默认那个排在最前。

    刻意**不返回全部 21 个**：让家长在 21 个里挑，等于把选型约束
    （不要童声/不要解说腔）扔给不知情的人。候选是产品判断，不是配置项。
    """
    default = ROLE_DEFAULTS.get(role, FALLBACK_VOICE)
    ids = [default] + [i for i in QIUQIU_SHORTLIST if i != default]
    return [BY_ID[i] for i in ids if i in BY_ID]


# ---------------------------------------------------------------- 英文（2026-09-14 用户要中英都能用）
# 腾讯标准英文音色。超自然那批是中文模型，念英文会走调；中英混说时仍用中文音色（它们能读英文单词）。
EN_VOICES = [
    Voice(101050, "WeJack", "男", "精品", "英文男声"),
    Voice(101051, "WeRose", "女", "精品", "英文女声"),
]
for _v in EN_VOICES:
    BY_ID.setdefault(_v.id, _v)


def options_for(lang: str = "zh", default=None):
    """给用户端的音色候选：默认那个排最前，然后按语言给。lang: zh / en / mixed。"""
    if lang == "en":
        ids = [v.id for v in EN_VOICES]
    else:
        ids = list(QIUQIU_SHORTLIST) + [603003]
    if default is not None:
        try:
            d = int(default)
            ids = [d] + [i for i in ids if i != d]
        except (TypeError, ValueError):
            pass
    out = []
    for i in ids:
        v = BY_ID.get(i)
        if v:
            out.append({"id": v.id, "name": v.name, "gender": v.gender, "tier": v.tier,
                        "scene": v.scene, "lang": "en" if v in EN_VOICES else "zh"})
    return out
