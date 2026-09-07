"""句子切分器 · 共享行为契约（跨仓库）

同一份向量文件由三处实现各自跑：
    球球老师 web/lib/sentences.js   原生 JS，浏览器端
    zaowuyun-voice speechQueue.ts   TypeScript，npm 包
    voice-kit    service.by_sentence Python，服务端闸门里

**接口不一样，所以要适配，而不是假装一样。** takeSentences(full, from)
是"给我全文和上次消费点，返回新完成的整句"；by_sentence 是
"喂我 token 流，吐完整句子"。共享的是**句界画在哪**，那才是会漂的东西。

适配规则（下面 _segments 实现的就是这两条）：
  ① by_sentence 最后会把没有句末标点的尾巴也吐出来，takeSentences 留着不发
     → 末段不以句末标点结尾就丢掉
  ② by_sentence 不 trim，takeSentences trim
     → 逐段 strip，空段丢掉

2026-09-07 建这份契约时立刻抓到一处真分歧：我给 by_sentence 写的句界
含 `…`，两个客户端都不含。中文 `……` 多半是话说到一半，在那儿放行
正是"漏出半句话"。已改掉——**这条测试就是防它再漂回去的**。
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from voice_kit.service import by_sentence

VECTORS = json.loads(
    (Path(__file__).parent / "vectors" / "sentences.json").read_text(encoding="utf-8"))
ENDS = "。！？!?\n"


def _segments(full: str) -> list[str]:
    out = [s.strip() for s in by_sentence(iter(full))]
    if out and not out[-1].rstrip().endswith(tuple(ENDS)):
        out.pop()                       # ① 未完成的尾巴不算已放行
    return [s for s in out if s]        # ②


# from>0 是 takeSentences 的"增量消费"语义，by_sentence 没有对应概念——
# 跳过而不是硬凑一个假的等价物。
CORE = [c for c in VECTORS["core"] if c["from"] == 0]


@pytest.mark.parametrize("case", CORE, ids=[c["name"] for c in CORE])
def test_sentence_boundaries_match_the_shared_contract(case):
    assert _segments(case["full"]) == case["expect"]["segments"]


def test_ellipsis_stays_out_of_the_boundary_set():
    """单独钉死——这是这份契约实际抓到的那处分歧。"""
    from voice_kit.service import _SENTENCE_END
    assert not _SENTENCE_END.search("我想想……"), \
        "`…` 回到句界里了：中文 `……` 是话说到一半，在那儿放行就是漏半句话"


def test_every_core_case_is_actually_covered():
    """别让向量悄悄变空还一路绿灯。"""
    assert len(CORE) >= 10, f"只跑到 {len(CORE)} 条，向量文件是不是被改瘦了"
