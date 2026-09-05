"""把 voice-kit 包成一个 OpenAI 协议端点，让别人的编排框架来调我们。

## 为什么要这个

腾讯 TRTC 对话式 AI（以及 Pipecat / LiveKit / Dify / Coze）都允许你把 LLM 换成
自己的 `/v1/chat/completions`。这一个口子决定了一件大事：

    编排（VAD / 打断 / 句级流式 / 传输）交给他们，
    安全（输出闸门 / 输入接管 / 可信度）留在我们手里。

不然就是二选一——要么自己从 VAD 手搓到打断（我们现在的 672 行），
要么把儿童安全交给一个不知道 8 铁律是什么的云服务。有了这个端点，两者兼得。

## 语音模式下闸门的语义变了（重要）

纯文字场景里，`OutputGate` 发现整段违规还能发一条 `correction` 事件把话收回来。
**语音场景收不回**——我们 yield 出去的 token 立刻被合成成声音播给孩子了。

所以这里强制 `voice_mode`：

  * 只下发**已经过闸门检查**的文本（`OutputGate.filter` 本来就是这么做的）
  * 关掉 `low_latency` 那条"完整句子提前下发"的捷径——它是拿安全垫换延迟，
    在语音里换的是"可能已经说出口"
  * 末尾若仍违规，只能追加澄清，不能撤回；所以宁可 holdback 大一点

残留风险要说清楚：逐段都合规、合起来才违规的情况，本机制挡不住。
这是流式 + 不可撤回的固有代价，不是实现缺陷。要彻底解决只能不流式。

## TRTC 会带这些头进来

    X-Task-Id / X-Request-Id / X-Sdk-App-Id / X-User-Id / X-Room-Id / X-Room-Id-Type

`X-User-Id` 是房间里那个孩子的 id——**这是我们拿回对话记录的钩子**。
报告、可信度分层、/admin 审核要的原话，都从 `on_turn` 回调里落库。
"""
#
# ⚠️ 这个模块**不能**加 `from __future__ import annotations`。
# FastAPI 是在 `create_app` 里懒加载的（voice-kit 核心不该硬依赖它），
# 于是 `Request` 是个局部名；延迟注解会把它变成字符串，pydantic 到模块
# 全局里找不着，直接 PydanticUndefinedAnnotation 炸掉。Python 3.9 实测。
#
import json
import time
import uuid
from dataclasses import dataclass
from typing import Callable, Iterator, Optional

from .gate import OutputGate, InputGate
from .pipeline import VoiceConfig


@dataclass
class TurnRecord:
    """一轮对话的完整记录，交给宿主应用落库。"""

    user_text: str
    reply_text: str
    model: str
    took_over: bool = False              # 输入闸门命中，没调 LLM
    takeover_rule: Optional[str] = None
    gated: bool = False                  # 输出闸门拦下过东西
    task_id: Optional[str] = None
    room_id: Optional[str] = None
    user_id: Optional[str] = None        # TRTC 房间里的用户 id → 映射到 child_id
    first_token_ms: Optional[float] = None
    total_ms: Optional[float] = None


def _sse(obj: dict) -> bytes:
    return b"data: " + json.dumps(obj, ensure_ascii=False).encode() + b"\n\n"


def _chunk(cid: str, model: str, delta: Optional[str], finish: Optional[str] = None) -> dict:
    d = {"content": delta} if delta is not None else {}
    return {
        "id": cid, "object": "chat.completion.chunk",
        "created": int(time.time()), "model": model,
        "choices": [{"index": 0, "delta": d, "finish_reason": finish}],
    }


class GatedChatCompletions:
    """OpenAI `/v1/chat/completions` 的语义，但每个 token 都过闸门。

    刻意不依赖 FastAPI——它只是个纯函数式的生成器，
    套 FastAPI / Starlette / Flask / 裸 ASGI 都行（见 `create_app`）。
    """

    def __init__(self, config: VoiceConfig, *, llm,
                 rewriter: Optional[Callable] = None,
                 on_turn: Optional[Callable[[TurnRecord], None]] = None,
                 voice_mode: bool = True):
        if config.output_rules and not config.fallback_reply:
            raise ValueError(f"[{config.name}] 有 output_rules 就必须有 fallback_reply")
        self.config = config
        self.llm = llm
        self.on_turn = on_turn
        self.voice_mode = voice_mode
        self.output_gate = (
            OutputGate(config.output_rules, holdback=config.holdback,
                       rewriter=rewriter, fallback=config.fallback_reply)
            if config.output_rules else None)
        self.input_gate = InputGate(config.input_rules) if config.input_rules else None

    # ---------------------------------------------------------------- 一轮

    def complete(self, body: dict, headers: Optional[dict] = None) -> Iterator[bytes]:
        """吃 OpenAI 请求体，吐 SSE 字节流。"""
        headers = {k.lower(): v for k, v in (headers or {}).items()}
        model = body.get("model") or self.config.model or "voice-kit"
        cid = "chatcmpl-" + uuid.uuid4().hex[:24]
        messages = list(body.get("messages") or [])
        user_text = next((m.get("content", "") for m in reversed(messages)
                          if m.get("role") == "user"), "")

        rec = TurnRecord(user_text=user_text, reply_text="", model=model,
                         task_id=headers.get("x-task-id"),
                         room_id=headers.get("x-room-id"),
                         user_id=headers.get("x-user-id"))
        t0 = time.perf_counter()

        try:
            for out in self._emit(cid, model, messages, user_text, rec, t0):
                yield out
        finally:
            rec.total_ms = round((time.perf_counter() - t0) * 1000, 1)
            if self.on_turn:
                # 落库失败绝不能让这轮对话崩掉——孩子还在等着听声音
                try:
                    self.on_turn(rec)
                except Exception:
                    pass

    def _emit(self, cid, model, messages, user_text, rec, t0) -> Iterator[bytes]:
        # ① 输入闸门：命中红线直接接管，**不调 LLM**
        if self.input_gate:
            rule, matched = self.input_gate.check(user_text)
            if rule:
                reply = (self.config.on_input_trigger(rule, matched)
                         if self.config.on_input_trigger else self.config.fallback_reply)
                rec.took_over, rec.takeover_rule, rec.reply_text = True, rule, reply
                rec.first_token_ms = round((time.perf_counter() - t0) * 1000, 1)
                yield _sse(_chunk(cid, model, reply))
                yield _sse(_chunk(cid, model, None, "stop"))
                yield b"data: [DONE]\n\n"
                return

        # ② LLM + 输出闸门
        tokens = self.llm.stream(messages, system=self.config.system_prompt, model=model)
        if not self.output_gate:
            for tok in tokens:
                if rec.first_token_ms is None:
                    rec.first_token_ms = round((time.perf_counter() - t0) * 1000, 1)
                rec.reply_text += tok
                yield _sse(_chunk(cid, model, tok))
        else:
            # voice_mode 下关掉 low_latency：那条捷径拿安全垫换延迟，
            # 而语音里换掉的是"可能已经说出口"
            low_latency = self.config.low_latency and not self.voice_mode
            for ev in self.output_gate.filter(tokens, low_latency=low_latency):
                if ev["kind"] == "correction":
                    rec.gated = True
                if rec.first_token_ms is None:
                    rec.first_token_ms = round((time.perf_counter() - t0) * 1000, 1)
                rec.reply_text += ev["text"]
                yield _sse(_chunk(cid, model, ev["text"]))

        yield _sse(_chunk(cid, model, None, "stop"))
        yield b"data: [DONE]\n\n"


# ---------------------------------------------------------------- ASGI 封装

def create_app(config: VoiceConfig, *, llm, on_turn=None, rewriter=None,
               voice_mode: bool = True, api_key: Optional[str] = None):
    """包成 FastAPI 应用。`api_key` 非空时要求 `Authorization: Bearer <key>`。

    ⚠️ 这个端点会被公网上的编排服务（TRTC 等）直接调用，**必须**设 api_key。
    不设就等于把你的模型和 prompt 挂在公网上白送。
    """
    from fastapi import FastAPI, HTTPException, Request
    from fastapi.responses import StreamingResponse

    handler = GatedChatCompletions(config, llm=llm, on_turn=on_turn,
                                   rewriter=rewriter, voice_mode=voice_mode)
    app = FastAPI(title=f"voice-kit · {config.name}")

    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request):
        if api_key and request.headers.get("authorization") != f"Bearer {api_key}":
            raise HTTPException(401, "bad api key")
        body = await request.json()
        if not body.get("stream", True):
            # 语音场景不该走这条；留着是为了让非流式调用方也能用，不是推荐路径
            raise HTTPException(400, "本端点只支持 stream=true（语音场景首字即体感延迟）")
        return StreamingResponse(
            handler.complete(body, dict(request.headers)),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.get("/healthz")
    def healthz():
        return {"ok": True, "product": config.name,
                "gate": bool(config.output_rules), "voice_mode": voice_mode}

    return app
