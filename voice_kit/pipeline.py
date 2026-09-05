"""把三段串起来：ASR → LLM（带闸门）→ TTS。

这个文件是 voice-kit 的"一套模块"真正兑现的地方——**换产品只换 config，不改代码**。

    球球老师（儿童）    严格闸门 + 可信度分层 + mimo 模型（8铁律已验证）
    qiuyiwu.com agent  轻量闸门 + deepseek-v4-flash（更快）
    AI 教练            中等闸门 + 自定义规则

三条产线共用同一份编排代码，差异全在 VoiceConfig 里。

## 为什么闸门在 pipeline 里而不是在 LLM provider 里

因为闸门是**产品策略**，不是**供应商能力**。同一个模型在儿童产品和成人产品里
该有完全不同的输出约束。把它塞进 provider 会导致每换一个供应商就要重写一遍规则。

## 一轮的真实构成（球球老师生产实测，2026-09）

    ASR（腾讯流式）    说完后 89ms      ← 与说话长度无关
    LLM 首字           2392~4337ms      ← 现在最大的一段
    TTS 首块           674ms
    ─────────────────────────────────
    孩子说完 → 听到第一声  ≈ 3.2~5.1s

**优化顺序应该照着这张表来，不是照直觉。** 我们一开始以为瓶颈是 ASR，
实测发现换供应商省不了（腾讯一句话识别 4609ms vs MiMo 4721ms，差 2%），
真正的杠杆是"流式"这个架构；而 LLM 首字才是现在最大的一块。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Iterator, Optional, Sequence

from .gate import InputGate, OutputGate, Rule


@dataclass
class VoiceConfig:
    """一个产品的语音人格与安全策略。改产品 = 改这个，不改代码。"""

    name: str
    system_prompt: str = ""
    model: Optional[str] = None                    # None = 用 provider 默认
    # 输出闸门规则。空 = 不设闸门（成人产品可以，儿童产品**绝不可以**）
    output_rules: Sequence[Rule] = field(default_factory=tuple)
    # 输入闸门：命中后**接管**而不是过滤——直接返回写死的回应，不调 LLM
    input_rules: Sequence[Rule] = field(default_factory=tuple)
    on_input_trigger: Optional[Callable[[str, str], str]] = None
    # 通话模式：完整句子跳过安全垫立刻下发（检测一个字不少，只是不多等）
    low_latency: bool = True
    tts_voice: Optional[str] = None
    holdback: Optional[int] = None
    fallback_reply: str = ""

    def __post_init__(self):
        if self.output_rules and not self.fallback_reply:
            # 有闸门却没兜底 = 命中时用户看到空白。宁可吵一句也不要静默失败。
            raise ValueError(
                f"[{self.name}] 设了 output_rules 就必须给 fallback_reply——"
                "否则违规被拦下时用户会看到一片空白")


class VoicePipeline:
    """ASR → LLM（闸门）→ TTS 的编排。

    三个 provider 都是可选的：只做文字对话就不传 asr/tts，
    只做朗读就只传 tts。不强迫每个产品都用全套。
    """

    def __init__(self, config: VoiceConfig, *, llm=None, asr=None, tts=None,
                 rewriter: Optional[Callable] = None):
        self.config = config
        self.llm = llm
        self.asr = asr
        self.tts = tts
        self.output_gate = (
            OutputGate(config.output_rules, holdback=config.holdback,
                       rewriter=rewriter or self._default_rewriter,
                       fallback=config.fallback_reply)
            if config.output_rules else None
        )
        self.input_gate = InputGate(config.input_rules) if config.input_rules else None

    # ---------------------------------------------------------------- 单段

    def transcribe(self, audio: bytes, fmt: str = "wav"):
        if not self.asr:
            raise RuntimeError(f"[{self.config.name}] 没有配置 ASR provider")
        return self.asr.transcribe(audio, fmt)

    def speak(self, text: str) -> Iterator[bytes]:
        """朗读一段文字，流式吐音频块。

        ⚠️ 这里**必须**先找 `stream_tts` 再找 `stream`，顺序不能反。
        一个 provider 可能同时做 ASR 和 TTS（腾讯就是），那时 `stream` 是
        **流式 ASR**（吃音频、吐文字的 async generator）。按 `stream` 优先
        会拿到 ASR 那个方法，然后在 `b"".join()` 处炸掉——真踩过。
        """
        if not self.tts:
            raise RuntimeError(f"[{self.config.name}] 没有配置 TTS provider")
        stream = (getattr(self.tts, "stream_tts", None)
                  or getattr(self.tts, "stream_synthesize", None)
                  or getattr(self.tts, "stream", None))
        if stream is None:
            raise RuntimeError(
                f"[{self.config.name}] TTS provider {type(self.tts).__name__} "
                "没有 stream_tts / stream 方法")
        return stream(text, voice=self.config.tts_voice)

    # ---------------------------------------------------------------- 一轮

    def respond(self, user_text: str, history: Sequence[dict] = ()) -> Iterator[dict]:
        """一轮对话，产出事件流。

        事件：{"kind": "delta"|"correction"|"takeover", "text": ...}

        takeover = 输入闸门命中，**根本没调 LLM**。
        这是比"让模型小心点"强得多的保证——最危险的时刻不给模型发挥空间。
        """
        if self.input_gate:
            rule, matched = self.input_gate.check(user_text)
            if rule:
                reply = (self.config.on_input_trigger(rule, matched)
                         if self.config.on_input_trigger else self.config.fallback_reply)
                yield {"kind": "takeover", "text": reply, "rule": rule, "matched": matched}
                return

        if not self.llm:
            raise RuntimeError(f"[{self.config.name}] 没有配置 LLM provider")

        messages = list(history) + [{"role": "user", "content": user_text}]
        tokens = self.llm.stream(messages, system=self.config.system_prompt,
                                 model=self.config.model)
        if not self.output_gate:
            for t in tokens:
                yield {"kind": "delta", "text": t}
            return
        yield from self.output_gate.filter(tokens, low_latency=self.config.low_latency)

    def _default_rewriter(self, text: str, violation) -> Optional[str]:
        """命中违规时让模型自己重写，并明确告诉它违反了什么。

        实测比"再生成一次"有效得多——不说清违反了哪条，模型很可能再犯同一个。
        """
        if not self.llm:
            return None
        try:
            fixed = "".join(self.llm.stream(
                [{"role": "assistant", "content": text},
                 {"role": "user", "content": "（请按下面的要求重写你上一句）"}],
                system=(self.config.system_prompt +
                        "\n\n⚠️ 你刚才那版回复违反了：" + "；".join(violation.violated_rules) +
                        f"（命中：{', '.join(violation.matched)}）。"
                        "重写这一句，绝不出现这些说法，语气保持不变，只输出给用户看的那一句。"),
                model=self.config.model, max_tokens=512))
            return fixed.strip() or None
        except Exception:
            return None   # 重写失败就走 fallback，绝不放原文出去
