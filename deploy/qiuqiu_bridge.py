"""把球球老师**真实的**引导逻辑接到 TRTC 上——不是重写一份简化版。

## 为什么不用 voice-kit 自己的闸门

生产版 `agent.stream_message` 的输出闸门（holdback=24、通话模式整句下发、
内部标记截断、`safety.check_output_violations`）跟 voice-kit 的 `OutputGate`
是同一套设计。真接产品时**重写一遍毫无意义还会分叉**——8 铁律、年龄分档、
课程导航、家长信号这些东西的真源只有一个，就是 `/opt/qiuqiu/backend`。

所以这里的做法是：**复用球球老师的一切，只换掉 LLM 那一次调用**。

    get_system_prompt(grade, age)      ← 8 铁律 + 年龄分档          复用
    container._build_user_context()    ← 画像/课程导航/回顾/事实/家长信号  复用
    _rule7_nudge(history)              ← 铁律#7 动态插话            复用
    _input_mode_note("call")           ← 通话模式的说话方式          复用
    safety.detect_red_trigger()        ← 输入侧红线，命中不调 LLM     复用
    safety.check_output_violations()   ← 输出侧违规检测              复用
    _INTERNAL_MARKER_RE                ← 截掉 [采样] 之类            复用
    db.save_turn / next_turn_index     ← 对话落库，报告有米下锅        复用
    ────────────────────────────────────────────────────────────
    self.client.messages.stream(...)   ← MiMo/Anthropic 协议        **换成 TokenHub deepseek**

## 语音场景比文字场景多出来的风险

1. **内部标记会被念出来。** 文字模式下 `[采样]` 漏出去只是难看，
   语音模式下 TTS 会一字一句念给孩子听。所以标记截断必须在 yield 之前，
   一个字都不能漏。
2. **`correction` 事件废掉了。** 生产版发现整段违规还能非流式重生一次、
   用 correction 整段替换；语音里前面的句子早就说出口了，收不回。
   这里只保留"违规就不下发"，末尾兜底换成安全话术——**不假装能撤回**。

## 安全护栏

只允许 `is_test = true` 的孩子。真实孩子的档案一律拒绝——
这是个探针，不该往 24 个真实孩子的对话记录里写东西。
"""
import os
import re
import sys
import threading
import time
from typing import Iterator, Optional
from uuid import UUID

QIUQIU_BACKEND = os.environ.get("QIUQIU_BACKEND", "/opt/qiuqiu/backend")
QIUQIU_ENV = os.environ.get("QIUQIU_ENV_FILE", "/etc/qiuqiu.env")


def _load_qiuqiu_env() -> None:
    """球球老师的配置（DATABASE_URL 等）在它自己的 env 文件里。
    用 setdefault：本进程已有的同名变量优先，别让它覆盖我们的。"""
    try:
        for line in open(QIUQIU_ENV):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k, v)
    except FileNotFoundError:
        pass


_load_qiuqiu_env()
if QIUQIU_BACKEND not in sys.path:
    sys.path.insert(0, QIUQIU_BACKEND)

import agent as qq_agent          # noqa: E402
import db as qq_db                # noqa: E402
import safety as qq_safety        # noqa: E402

from qq_text import fallback_for as _fallback_for       # noqa: E402
from qq_text import humanize_topic as _humanize_topic    # noqa: E402

HOLDBACK = 24                     # 跟生产版一致，不另立门户


class QiuqiuBridge:
    """一个 TRTC 房间 = 一个孩子的一次通话。"""

    def __init__(self, llm, *, model: str, allow_real_children: bool = False):
        self.llm = llm
        self.model = model
        self.allow_real_children = allow_real_children
        self._sessions = {}       # child_id -> session_id

    # ---------------------------------------------------------------- 准入

    def resolve_child(self, trtc_user_id: str) -> UUID:
        """TRTC 的 userId 就是 child_id（联调期这样约定，产品里应该查绑定表）。"""
        try:
            cid = UUID(trtc_user_id)
        except (ValueError, TypeError):
            raise ValueError(f"TRTC userId 不是合法的 child_id: {trtc_user_id!r}")

        prof = qq_db.get_profile(cid) if hasattr(qq_db, "get_profile") else None
        if prof is None:
            from sqlalchemy import text as T
            with qq_db.conn() as c:
                prof = c.execute(T("SELECT nickname, is_test FROM profiles "
                                   "WHERE child_id = :c AND deleted_at IS NULL"),
                                 {"c": str(cid)}).mappings().first()
        if not prof:
            raise ValueError(f"没有这个孩子: {cid}")
        if not prof.get("is_test") and not self.allow_real_children:
            # 硬拦。探针不该往真实孩子的对话记录里写东西。
            raise PermissionError(
                f"{prof.get('nickname')} 不是测试账号，联调探针拒绝接入真实孩子的档案")
        return cid

    def session_for(self, child_id: UUID) -> UUID:
        """拿这个孩子当前该用的 session。

        **不要只靠进程内的字典**——服务一重启就全忘了，同一个孩子会被开出
        一堆碎会话，后台采样按 session 聚合，碎了就分析不出东西。
        （跟 TaskId 那次是同一类错误：状态放内存 = 重启即孤儿。）

        真源是库：先问 `get_resumable_session`（生产端点就是这么做的），
        没有可续的才新开。
        """
        sid = self._sessions.get(child_id)
        if sid is not None:
            return sid
        try:
            prev = qq_db.get_resumable_session(child_id)
            if prev:
                sid = prev["session_id"]
        except Exception:
            sid = None
        if sid is None:
            sid = qq_agent.get_container(child_id).start_new_session()
        self._sessions[child_id] = sid
        return sid

    def end_session(self, child_id: UUID) -> None:
        self._sessions.pop(child_id, None)

    # ---------------------------------------------------------------- 开场白

    def welcome_for(self, child_id: UUID) -> str:
        """这个孩子该听到的第一句话。

        TRTC 的 `WelcomeMessage` 是起任务时传的**静态字符串**，会被 TTS
        一字不差念出来——模型完全不参与。所以两件事必须在这里做对：

        1. **第一次见面必须说信任声明**。这不是话术，是产品红线：
           CLAUDE.md 写明"孩子第一次对话时听到的信任声明"，
           `agent/system-prompt.md` L50-57 有原文。文字端靠模型执行，
           语音端模型碰不到这句，只能由我们发出去。
        2. **回访时绝不编上次聊了什么**。宁可说一句笼统的，也不要猜——
           猜错就是"我记得你的小仓鼠"那种幻觉，而且是从第一句就开始骗人，
           比聊到一半编还糟。只有库里真有 `topics_explored` 才提。
        """
        cont = qq_agent.get_container(child_id)
        cont._profile_summary = None
        p = cont.profile_summary
        name = p.get("nickname") or "同学"

        if (p.get("total_turns") or 0) < 1:
            return (
                f"你好呀，{name}，我是球球老师。"
                "我是一个特别喜欢问问题的人——不是老师那种问题，是好奇你怎么想的那种。"
                "我们聊的话会被整理成一份报告给你爸爸妈妈看，里面会引用你说过的一些话，"
                "让他们更懂你——但球球老师只会挑让你觉得被理解、被欣赏的话，"
                "不会挑出来说你的不是。你可以放心跟我说。"
                "今天想聊点啥呀？")

        topics = p.get("topics_explored") or []
        if isinstance(topics, str):
            topics = [topics]
        topic = _humanize_topic(topics[-1]) if topics else ""
        if topic:
            return (f"嘿，是你呀，我等你好久了～上次我们聊到{topic}，"
                    "我一直记着呢。今天想聊点啥？")
        return "嘿，是你呀，我等你好久了～今天想聊点啥？"

    # ---------------------------------------------------------------- 轮后分析

    def _analyze_later(self, child_id: UUID, session_id: UUID) -> None:
        """认知采样 + 洞察卡，另开线程跑。

        这一步不做，TRTC 上的对话就只是聊天记录——**产不出报告**，
        画像、潜能赛道、家长端那份"看见孩子"的东西全都没有。
        生产端点用 BackgroundTasks 做同一件事（`main._background_analyze`）。

        必须异步：采样自己要调一次 LLM，几秒起步；同步跑就等于每轮
        对话后面挂一条几秒的尾巴，孩子那边会听见明显的卡顿。
        异常全吞——分析失败不能影响对话本身。
        """
        def run():
            try:
                import sampling as qq_sampling
                qq_sampling.maybe_sample(child_id, session_id)
            except Exception:
                pass
            try:
                import report as qq_report
                qq_report.maybe_generate_insight(child_id, session_id)
            except Exception:
                pass

        threading.Thread(target=run, daemon=True,
                         name=f"analyze-{child_id}").start()

    # ---------------------------------------------------------------- 一轮

    def respond(self, child_id: UUID, user_text: str) -> Iterator[dict]:
        """产出 {"kind": "delta"|"takeover"|"correction", "text": ...}。

        跟生产版 `stream_message` 走同一条路，只有 LLM 那一步换成了 TokenHub。
        """
        container = qq_agent.get_container(child_id)
        container._profile_summary = None
        session_id = self.session_for(child_id)
        container._current_session_id = session_id

        history = qq_db.get_session_history(session_id, child_id=child_id, tail=True)
        next_idx = qq_db.next_turn_index(session_id)
        user_conv_id = qq_db.save_turn(
            child_id=child_id, session_id=session_id, turn_index=next_idx,
            speaker="user", content=user_text, input_mode="call")

        # ① 输入侧红线：命中就接管，**不调 LLM**。这条在语音里同样成立。
        red = qq_safety.detect_red_trigger(user_text)
        if red.triggered:
            reply = qq_safety.safe_bot_reply_on_red_trigger()
            qq_db.save_turn(child_id=child_id, session_id=session_id,
                            turn_index=next_idx + 1, speaker="bot", content=reply)
            try:
                qq_db.record_signal(child_id=child_id, conv_id=user_conv_id,
                                    session_id=session_id, signal_type="red_trigger",
                                    detail=red.category if hasattr(red, "category") else "")
            except Exception:
                pass          # 记录失败不能让孩子那边断音
            self._analyze_later(child_id, session_id)
            yield {"kind": "takeover", "text": reply, "rule": "red_trigger"}
            return

        # ② 真实的 system prompt：8 铁律 + 年龄分档 + 这个孩子的全部上下文
        prof = container.profile_summary
        system_prompt = qq_agent.get_system_prompt(prof.get("grade"), age=prof.get("age"))
        full_system = (system_prompt + "\n\n---\n\n" + container._build_user_context()
                       + qq_agent._rule7_nudge(history)
                       + qq_agent._input_mode_note("call"))
        messages = container._load_session_history(session_id)

        # ③ 流式 + 闸门。逻辑照抄生产版，但去掉 correction ——
        #    语音里说出口就收不回，不假装能撤回。
        # cache_key 用 child_id：这个孩子的 38k 字 system prompt 跨轮跨会话
        # 一字不变，正是前缀缓存最理想的形状。用 session_id 当 cache_key 是错的
        # ——每开一次新会话缓存就作废，等于没开。
        # thinking=False 是语音场景最大的杠杆：实测首字 3726→1387ms（−63%）。
        # 球球老师自己的代码里本来就有 `_thinking_off(call_mode)` 做这件事
        # （给 MiMo 用的 extra_body），这里是同一个决定换到 OpenAI 协议上。
        buffer, flushed, llm_failed = "", 0, None
        try:
            stream = self.llm.stream(_to_openai(messages), system=full_system,
                                     model=self.model, thinking=False,
                                     cache_key=f"child-{child_id}",
                                     session_id=str(session_id))
            for tok in stream:
                buffer += tok
                m = qq_agent._INTERNAL_MARKER_RE.search(buffer)
                if m:
                    # 内部标记出现 = 给孩子看的正文到此为止。语音下尤其要紧：
                    # 漏出去就是 TTS 把 "[采样]" 念出来。
                    buffer = buffer[:m.start()]
                    flushed = min(flushed, len(buffer))
                    break
                visible_end = len(buffer) - HOLDBACK
                done_at = qq_agent._last_sentence_end(buffer)   # 通话模式：整句可提前下发
                if done_at > visible_end:
                    visible_end = done_at
                if visible_end > flushed and not qq_safety.check_output_violations(
                        buffer[:visible_end]).has_violation:
                    yield {"kind": "delta", "text": buffer[flushed:visible_end]}
                    flushed = visible_end
        except Exception as e:
            # LLM 挂了（额度用完、网关 402、连接断……）。**绝不能让流直接死**——
            # 2026-09-05 实测过一次：TokenHub 免费额度耗尽返回 402，异常一路
            # 抛穿 StreamingResponse，客户端拿到 incomplete chunked read，
            # 孩子那边就是"话说一半突然静音"。比说错话更糟：孩子会以为是自己
            # 说了什么不该说的。生产版 stream_message 有兜底，桥接漏了。
            llm_failed = str(e)[:160]

        if llm_failed and not buffer[:flushed].strip():
            # 一个字都没来得及说 → 用一句听起来像"人卡了一下"的话兜住
            reply = "嗯……我这边卡了一下，你刚才说的能再讲一遍吗？"
            qq_db.save_turn(child_id=child_id, session_id=session_id,
                            turn_index=next_idx + 1, speaker="bot", content=reply)
            yield {"kind": "correction", "text": reply, "llm_error": llm_failed}
            return
        if llm_failed:
            # 已经说了一半 → 接一句自然的收口，不要硬断
            yield {"kind": "correction", "text": "……我这边卡了一下，你接着说？",
                   "llm_error": llm_failed}

        tail = buffer[flushed:]
        if tail:
            v = qq_safety.check_output_violations(buffer)
            if v.has_violation:
                # 前面的句子已经说出口了，只能用一句安全话术收尾——
                # 这正是语音相对文字**退化**的地方，如实体现，不粉饰。
                # 但至少让这句话接得上：按违反的规则挑，不用通用废话。
                tail = _fallback_for(v.violated_rules)
                yield {"kind": "correction", "text": tail,
                       "violated": v.violated_rules}
            else:
                yield {"kind": "delta", "text": tail}

        reply = (buffer[:flushed] + tail) if tail else buffer[:flushed]
        # 落库前再剥一次内部标记：流式那一路是"遇到标记就截断"，
        # 但标记也可能整块一次到达、恰好落在最后一片里。
        reply = qq_agent._strip_internal_markers(reply)
        if not reply.strip():
            reply = "嗯，我想想……（我卡了一下，再说一次好吗？）"
            yield {"kind": "correction", "text": reply}
        qq_db.save_turn(child_id=child_id, session_id=session_id,
                        turn_index=next_idx + 1, speaker="bot", content=reply)
        # 采样 + 洞察卡。不跑这一步，TRTC 上的对话产不出任何报告。
        self._analyze_later(child_id, session_id)


def _to_openai(messages) -> list:
    """球球老师存的是 Anthropic 风格的 messages；TokenHub 走 OpenAI 协议。
    两边的 role 命名恰好一致（user/assistant），content 可能是 list of blocks。"""
    out = []
    for m in messages:
        c = m.get("content")
        if isinstance(c, list):
            c = "".join(b.get("text", "") for b in c if isinstance(b, dict))
        out.append({"role": m.get("role", "user"), "content": c or ""})
    return out
