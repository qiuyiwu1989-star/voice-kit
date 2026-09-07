"""voice-kit 公共服务：一套语音能力，多个项目共用。

## 设计上最重要的一个决定：端点做成 OpenAI 协议的形状

    POST /v1/chat/completions      带闸门的对话（OpenAI 兼容）
    POST /v1/audio/transcriptions  ASR（OpenAI 兼容）
    POST /v1/audio/speech          TTS（OpenAI 兼容）
    POST /v1/trtc/session          实时语音：发 UserSig + 起 AI 对话任务
    GET  /v1/voices                这个项目能选的音色

前三个刻意长成 OpenAI 的样子，**因为那意味着接入成本接近零**：
任何项目把现成的 OpenAI SDK 的 base_url 指过来就能用，
不用学新协议、不用写 adapter——而且**白拿闸门**。

这也是为什么 TRTC / Pipecat / Dify / Coze 这些编排框架能直接挂上来：
它们都支持"换成自己的 /v1/chat/completions"。

## 每个项目自己的人格和护栏

由 `Authorization: Bearer <项目钥匙>` 决定用哪套配置（见 tenants.py）。
球球老师严格（8 铁律 + 可信度分层），AI 教练中等，个人站的 agent 可以不设。
**同一份代码，不同 config**——这是 voice-kit 从第一天起的主张。

## 不做的事

不做用量计费、不做限流分级、不做管理后台。现在是自用的几个项目，
那些东西没有真实需求就是负担。真到了需要的时候再加，
而 tenants.json 里已经留了 id，加计量不用改结构。
"""
import json
import os
import time
import uuid
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Header, HTTPException, Request, UploadFile, File, Form
from fastapi.responses import JSONResponse, Response, StreamingResponse

from .gate import InputGate, OutputGate
from .openai_server import _chunk, _sse
from .tenants import Registry, Tenant
from . import voices as V


def _need(name: str) -> str:
    v = os.environ.get(name, "")
    if not v:
        raise SystemExit(f"缺少环境变量 {name}")
    return v


def create_service(tenants_path: Optional[str] = None) -> FastAPI:
    from .providers.tencent import TencentProvider
    from .providers.tokenhub import TokenHubLLM
    from .providers.trtc import TRTCConversation

    registry = Registry(Path(tenants_path or os.environ.get(
        "VOICEKIT_TENANTS", "/opt/voicekit/tenants.json")))

    llm = TokenHubLLM(_need("TOKENHUB_API_KEY"))
    tc_id, tc_key = _need("TC_SECRET_ID"), _need("TC_SECRET_KEY")
    tencent = TencentProvider(tc_id, tc_key, os.environ.get("TENCENT_APPID") or 0)
    tencent.tts_app_id = int(os.environ.get("TENCENT_APPID") or 0) or None
    trtc = TRTCConversation(tc_id, tc_key,
                            int(os.environ.get("TRTC_SDKAPPID") or 0) or 1,
                            os.environ.get("TRTC_SDKSECRETKEY") or "x",
                            tts_app_id=tencent.tts_app_id)
    public_url = os.environ.get("VOICEKIT_PUBLIC_URL", "").rstrip("/")

    app = FastAPI(title="voice-kit", description="共用语音能力：ASR / LLM(带闸门) / TTS")

    # ------------------------------------------------------------ 鉴权

    def who(authorization: Optional[str]) -> Tenant:
        registry.maybe_reload()      # 加项目 = 改配置文件，不用重启
        raw = (authorization or "").removeprefix("Bearer ").strip()
        t = registry.by_key(raw)
        if not t:
            raise HTTPException(401, "无效的项目钥匙")
        return t

    def gates(t: Tenant):
        out = OutputGate(t.output_rules, fallback=t.fallback_reply) \
            if t.output_rules else None
        inp = InputGate(t.input_rules) if t.input_rules else None
        return out, inp

    # ------------------------------------------------------------ 对话

    @app.post("/v1/chat/completions")
    async def chat(request: Request, authorization: str = Header(None)):
        """带闸门的对话。**每个 token 都过这个项目自己的护栏才允许出去。**"""
        t = who(authorization)
        body = await request.json()
        messages = list(body.get("messages") or [])
        user_text = next((m.get("content", "") for m in reversed(messages)
                          if m.get("role") == "user"), "")
        out_gate, in_gate = gates(t)
        model = body.get("model") or t.model
        cid = "chatcmpl-" + uuid.uuid4().hex[:24]
        # 前缀缓存要一个跨轮稳定的 id；调用方给了就用它的
        cache_key = body.get("prompt_cache_key") or request.headers.get("x-session-id")

        def stream():
            # ① 输入闸门：命中红线直接接管，**根本不调 LLM**
            if in_gate:
                rule, matched = in_gate.check(user_text)
                if rule:
                    reply = t.on_input_trigger_reply or t.fallback_reply
                    yield _sse(_chunk(cid, model, reply))
                    yield _sse(_chunk(cid, model, None, "stop"))
                    yield b"data: [DONE]\n\n"
                    return
            # ② LLM + 输出闸门
            system = body.get("system") or t.system_prompt
            toks = llm.stream(messages, system=system, model=model,
                              thinking=t.thinking, cache_key=cache_key,
                              session_id=request.headers.get("x-session-id"),
                              max_tokens=body.get("max_tokens", t.max_tokens))
            if not out_gate:
                for tok in toks:
                    yield _sse(_chunk(cid, model, tok))
            else:
                # voice_mode：语音里说出口就收不回，所以不抄"整句提前下发"的近路
                for ev in out_gate.filter(toks, low_latency=False):
                    yield _sse(_chunk(cid, model, ev["text"]))
            yield _sse(_chunk(cid, model, None, "stop"))
            yield b"data: [DONE]\n\n"

        if not body.get("stream", True):
            # 语音场景不该走这条，但文字类项目（个人站的 agent）会需要
            text = "".join(
                (json.loads(p[6:])["choices"][0]["delta"].get("content") or "")
                for p in (x.decode().strip() for x in stream())
                if p.startswith("data:") and p[6:].strip() != "[DONE]")
            return JSONResponse({
                "id": cid, "object": "chat.completion", "model": model,
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant", "content": text}}]})

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache",
                                          "X-Accel-Buffering": "no"})

    # ------------------------------------------------------------ ASR

    @app.post("/v1/audio/transcriptions")
    async def transcribe(file: UploadFile = File(...),
                         authorization: str = Header(None)):
        who(authorization)
        audio = await file.read()
        fmt = (file.filename or "a.wav").rsplit(".", 1)[-1].lower()
        r = tencent.transcribe(audio, fmt if fmt in ("wav", "mp3", "pcm") else "wav")
        return {"text": r.text, "duration_ms": r.duration_ms,
                "elapsed_ms": r.elapsed_ms}

    # ------------------------------------------------------------ TTS

    @app.post("/v1/audio/speech")
    async def speech(request: Request, authorization: str = Header(None)):
        t = who(authorization)
        body = await request.json()
        text = (body.get("input") or body.get("text") or "").strip()
        if not text:
            raise HTTPException(400, "input 不能为空")
        voice = V.resolve(t.voice_role, override=body.get("voice") or t.tts_voice)
        audio = tencent.synthesize(text, voice=voice)
        return Response(content=audio, media_type="audio/wav",
                        headers={"X-Voice-Type": str(voice)})

    @app.get("/v1/voices")
    def list_voices(authorization: str = Header(None)):
        """这个项目能选的音色。**刻意只给候选不给全部 21 个**——
        选型约束（不要童声、不要解说腔）是产品判断，不该扔给调用方。"""
        t = who(authorization)
        return {"role": t.voice_role,
                "default": V.resolve(t.voice_role, override=t.tts_voice),
                "options": [{"id": v.id, "name": v.name, "gender": v.gender,
                             "tier": v.tier, "scene": v.scene, "note": v.note}
                            for v in V.role_options(t.voice_role)]}

    # ------------------------------------------------------------ 实时语音

    @app.post("/v1/trtc/session")
    async def trtc_session(request: Request, authorization: str = Header(None)):
        """起一路实时语音：发 UserSig + 让 AI 进房，LLM 回调到本服务。

        **回调带的是这个项目自己的钥匙**，所以 TRTC 那边跑的仍然是
        这个项目的人格和护栏——编排交出去了，安全没交出去。
        """
        t = who(authorization)
        body = await request.json()
        room = (body.get("room") or "").strip()
        user = (body.get("user") or "").strip()
        if not room or not user:
            raise HTTPException(400, "room 和 user 必填")
        if not public_url:
            raise HTTPException(500, "服务端没配 VOICEKIT_PUBLIC_URL，TRTC 无法回调")

        raw_key = (authorization or "").removeprefix("Bearer ").strip()
        task_id = trtc.start(
            room,
            llm_api_url=f"{public_url}/v1/chat/completions",
            llm_api_key=raw_key,
            llm_model=t.model,
            system_prompt=t.system_prompt,
            target_user_id=user,
            bot_user_id=f"bot-{uuid.uuid4().hex[:10]}",
            welcome_message=body.get("welcome") or "",
            tts_voice=V.resolve(t.voice_role, override=body.get("voice") or t.tts_voice),
            max_idle_time=int(body.get("max_idle_time", 120)),
            session_id=room,
        )
        return {"task_id": task_id, "sdkappid": trtc.sdk_app_id,
                "user_sig": trtc.user_sig(user, expire=7200)}

    @app.post("/v1/trtc/stop")
    async def trtc_stop(request: Request, authorization: str = Header(None)):
        who(authorization)
        body = await request.json()
        trtc.stop(body["task_id"])
        return {"ok": True}

    # ------------------------------------------------------------ 运维

    @app.get("/healthz")
    def healthz():
        registry.maybe_reload()
        return {"ok": True, "tenants": len(registry.all()),
                "ts": int(time.time())}

    @app.get("/v1/me")
    def me(authorization: str = Header(None)):
        """调用方自检：我的钥匙对不对、我拿到的是哪套配置。
        接入时第一个该调的接口——比对着文档猜快得多。"""
        t = who(authorization)
        return {"id": t.id, "name": t.name, "model": t.model,
                "thinking": t.thinking,
                "output_rules": [r.name for r in t.output_rules],
                "input_rules": [r.name for r in t.input_rules],
                "voice_role": t.voice_role,
                "has_system_prompt": bool(t.system_prompt)}

    return app
