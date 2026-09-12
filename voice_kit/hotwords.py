"""ASR 热词：让孩子说的那些词别被听错。

## 为什么这是四个候选能力里该最先做的

孩子嘴里的高频词几乎全是专有名词——"蛋仔派对""洛克王国""迪迦奥特曼"。
通用 ASR 没见过这些，会转成一堆同音字。而错了不只是这一轮听不懂：

**错的转写会顺着写进认知观察报告。** 报告卖的就是"我们真的听懂了你的孩子"，
里面出现一个孩子根本没说过的词，家长立刻就不信了——比延迟高、
比音色难听严重得多，因为那动摇的是产品的立论。

热词是所有候选能力里**成本最低、链路最短**的一个：不用改架构、
不用买套餐、不依赖内测资格，就是请求里多一个参数。

## 用临时热词，不用云端热词表

腾讯两种都支持：
    hotword_id    先在控制台建表，拿 id 传进来
    hotword_list  每次请求直接传，云端不保留

**我们用后者。** 词表跟着代码走、跟着版本走、改了能 review、
回滚就是 git revert。建在控制台里的表是脱离版本控制的隐藏状态——
线上表现变了却在代码里找不到原因，那种债最难还。

（两个都传时只有 hotword_list 生效，所以不会打架。）

## 硬限制（腾讯的，不是我们的）

    每次最多 128 个词
    单词 ≤30 字符（≤10 汉字）
    权重 1-11，或 100

权重 11 = 超级热词；100 = 同音替换（只在 8k_zh / 16k_zh 引擎上有效，
我们用的正是 16k_zh）。

**但默认一个 11、一个 100 都不用。** 热词是有副作用的：调得越狠，
把本来对的词改错的概率越大。"球球"就是现成的例子——孩子说"求求你"，
权重拉满就会变成"球球你"。在拿到真实转写数据之前，
把旋钮拧到最大是拿孩子的对话在赌。先上保守值，等有数据再调。
"""
from dataclasses import dataclass
from typing import Iterable, List, Sequence, Tuple

MAX_WORDS = 128          # 腾讯上限
MAX_CHARS = 30           # 单词字符上限（≈10 汉字）
DYNAMIC_BUDGET = 28      # 给每次会话的动态词（孩子的名字、刚提到的东西）留位

# 实时 ASR 的热词是**塞在 WebSocket URL 的 query 里**的，所以除了腾讯的
# 128 词上限，还有一条我们自己必须守的线：URL 不能太长。
# 中文 percent-encode 后每个字 9 字节，填满 128 个十字词约 12000 字符——
# 握手会直接失败，而且失败得很难查（不是 401，是连不上）。
# 3000 给整条 URL 留了充裕余量（其余参数约 300 字符，nginx 默认行上限 8k）。
MAX_ENCODED = 3000


@dataclass(frozen=True)
class Hotword:
    word: str
    weight: int = 10

    def valid(self) -> bool:
        return bool(self.word) and len(self.word) <= MAX_CHARS \
            and "|" not in self.word and "," not in self.word \
            and (1 <= self.weight <= 11 or self.weight == 100)


def _w(weight: int, *words: str) -> List[Hotword]:
    return [Hotword(w, weight) for w in words]


# ---------------------------------------------------------------- 孩子的词

# 权重说明：10 = 明确的专有名词，认错了就是错；
#          6 = 日常词，只是想让它别被拆错，不值得压过上下文。
# 没有用 11 和 100，理由见模块开头。

GAMES = _w(10,
    "我的世界", "蛋仔派对", "蛋仔", "洛克王国", "迷你世界", "第五人格",
    "奥比岛", "摩尔庄园", "元梦之星", "王者荣耀", "和平精英", "原神",
    "光遇", "球球大作战", "迷你号", "创造模式", "生存模式", "红石",
)

ANIME = _w(10,
    "奥特曼", "迪迦", "赛罗", "泽塔", "欧布", "宝可梦", "皮卡丘",
    "海贼王", "火影忍者", "名侦探柯南", "哆啦A梦", "小马宝莉",
    "叶罗丽", "斗罗大陆", "熊出没", "喜羊羊", "猪猪侠", "铠甲勇士",
    "巴啦啦小魔仙", "汪汪队", "汤姆猫", "蜘蛛侠", "钢铁侠",
)

TOYS = _w(10,
    "乐高", "盲盒", "卡游", "奥特曼卡", "拼装", "手办", "积木",
    "四驱车", "溜溜球", "魔方",
)

# ⚠️ 这里刻意**只放 ASR 认不出的词**。
# 第一版我塞了"语文""数学""期中""跳绳"这些——通用模型本来就认得，
# 放进来纯属白占预算（热词是有字节上限的，见 MAX_ENCODED），
# 而且给常用词加权还可能把别的词误改成它。
# 热词的本分是补模型的盲区，不是把它已经会的再说一遍。
SCHOOL = _w(6,
    "大队长", "中队长", "手抄报", "眼保健操", "家委会", "错题本",
    "道法", "晨读",
)

AFTER_SCHOOL = _w(8,
    "少年宫", "奥数", "架子鼓", "轮滑", "琴行", "考级",
)

# 产品自己的名字。认错自己的名字是最难堪的一种错。
# 但**故意不给 11**："求求你"和"球球你"是真同音，
# 权重拉满会把孩子的求助语气改掉。
SELF = _w(10, "球球", "球球老师")

QIUQIU: List[Hotword] = SELF + GAMES + ANIME + TOYS + SCHOOL + AFTER_SCHOOL


# ---------------------------------------------------------------- 教练的词

COACH: List[Hotword] = _w(10,
    "复盘", "对齐", "拆解", "抓手", "闭环", "颗粒度", "北极星指标",
    "增长飞轮", "护城河", "第一性原理", "认知负荷", "心智模型",
) + _w(8,
    "目标拆解", "关键结果", "季度目标", "一对一", "向上管理",
    "跨部门", "优先级", "时间盒",
)


ROLE_WORDS = {"qiuqiu": QIUQIU, "coach": COACH, "assistant": []}


def build(role: str = "qiuqiu", extra: Sequence[Hotword] = (),
          *, max_encoded: int = MAX_ENCODED) -> str:
    """拼出 hotword_list 参数值；没有词就返回空串（调用方据此决定传不传）。

    extra 排在前面——那是**这次会话特有**的词（孩子的名字、他刚提到的
    宠物名字、同学的外号）。这类词比任何静态词表都值钱：静态词表能
    覆盖"大多数孩子会说什么"，而它覆盖"这个孩子会说什么"。
    所以超限时先砍静态的，不砍动态的。

    超限时从**尾部**砍，同理：尾部是静态词，头部是这个孩子的词。

    两条限制都要守：腾讯的 128 词，和我们自己的编码后长度
    （实时 ASR 把热词塞在 WebSocket URL 里，太长就连不上）。
    """
    import urllib.parse
    seen, out, size = set(), [], 0
    for h in list(extra) + list(ROLE_WORDS.get(role, [])):
        if not h.valid() or h.word in seen:
            continue
        piece = f"{h.word}|{h.weight}"
        grew = len(urllib.parse.quote(piece, safe="")) + (3 if out else 0)  # 3 = 编码后的逗号
        if size + grew > max_encoded or len(out) >= MAX_WORDS:
            break
        seen.add(h.word)
        out.append(h)
        size += grew
    return ",".join(f"{h.word}|{h.weight}" for h in out)


def static_budget(role: str = "qiuqiu") -> int:
    """静态词表之外**还剩多少字节**能给动态词。负数就是已经挤不下了。

    故意按字节而不是按词数算：真正先到的上限是 MAX_ENCODED，
    不是 128 词。按词数报余量会骗人——第一版就是这样，
    显示"还剩 8 位"，而实际上字节早就用光了，
    孩子自己的名字一个都塞不进去。
    """
    import urllib.parse
    used = len(urllib.parse.quote(build(role, max_encoded=MAX_ENCODED), safe=""))
    return MAX_ENCODED - used
