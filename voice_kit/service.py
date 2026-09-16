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

import re

from .gate import InputGate, OutputGate
from .openai_server import _chunk, _sse
from .tenants import Registry, Tenant
from . import hotwords as HW
from . import voices as V


# 句界故意**不含** `…`：中文的 `……` 多半是"话说到一半"（"我想想……"），
# 在那儿放行正是这个修复要消灭的那种半句话。
# 两个客户端实现（球球 web/lib、zaowuyun-voice）也都不把它当句界——
# 2026-09-07 建共享向量时对出来的，三边现在一致。
_SENTENCE_END = re.compile(r"[。！？!?\n]+")
# 舞台提示：整段被括号包住、不长、不含句末标点。「（笑）」「（先接住情绪，不问情境）」「[思考]」
_STAGE_RE = re.compile(r"[（(\[【][^（()\[\]【】。！？!?]{1,40}[）)\]】]")


def _strip_stage(text: str) -> str:
    return _STAGE_RE.sub("", text).lstrip(" 　\t")


def by_sentence(tokens):
    """把 token 流重组成**完整句子**再往下走。

    为什么必须这么做（2026-09-07 线上实测抓到的）：闸门是按字符放行的，
    违规词出现在句子中段时，前半句早就发出去了，接上兜底话变成——

        "失眠确实让人困扰…你妈妈提出" + "我们再多聊一会儿吧"
                          ↑ 从句子中间断掉

    文字端只是难看，**语音端就是念出半句话再接一句不相干的**。
    按整句放行之后，违规的那一句整句不发，兜底话接得上。

    代价是首字要等第一个句号——但语音场景本来就是整句合成，
    TTS 也要等整句才开始，所以这个代价在语音里几乎为零。
    生产版球球老师（`agent._last_sentence_end`）用的就是同一条策略。
    """
    buf = ""
    for tok in tokens:
        buf += tok
        while True:
            m = _SENTENCE_END.search(buf)
            # 标点正好落在缓冲区末尾时**先别吐**——后面可能还跟着标点。
            # 逐 token 喂的时候看不见"？？！"是一串，先吐会切成
            # ["真的吗？", "？", "！"]，语音里就是单独念一个问号。
            # 等下一个字来确认标点串结束；流结束时由下面的收尾兜住。
            # （两个客户端实现拿的是累积全文，天然没这个问题——
            #   2026-09-07 共享向量把这处差异对了出来。）
            if not m or m.end() == len(buf):
                break
            yield buf[:m.end()]
            buf = buf[m.end():]
    if buf.strip():
        yield buf


def _need(name: str) -> str:
    v = os.environ.get(name, "")
    if not v:
        raise SystemExit(f"缺少环境变量 {name}")
    return v


def create_service(tenants_path: Optional[str] = None) -> FastAPI:
    from .providers.tencent import TencentProvider
    from .providers.tokenhub import TokenHubLLM, DEFAULT_BASE_URL
    from .providers.trtc import TRTCConversation

    registry = Registry(Path(tenants_path or os.environ.get(
        "VOICEKIT_TENANTS", "/opt/voicekit/tenants.json")))

    # 网关可换：默认腾讯 TokenHub；本地试聊可指到小米 MiMo（api-key 头、不发 enable_thinking）
    llm = TokenHubLLM(_need("TOKENHUB_API_KEY"),
                      base_url=os.environ.get("TOKENHUB_BASE_URL") or DEFAULT_BASE_URL,
                      model=os.environ.get("TOKENHUB_MODEL") or "deepseek-v4-flash",
                      auth_header=os.environ.get("TOKENHUB_AUTH_HEADER") or "bearer",
                      thinking_field=(os.environ.get("TOKENHUB_THINKING_FIELD", "1") != "0"),
                      thinking_style=os.environ.get("TOKENHUB_THINKING_STYLE") or "thinking")
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
                # 没闸门就直接透传，保留逐 token 的最低延迟
                for tok in toks:
                    yield _sse(_chunk(cid, model, tok))
            else:
                # 有闸门就**按整句**过——半句话被拦下会让兜底话接在句子中间，
                # 语音里那就是念出半句再接一句不相干的。
                #
                # 必须配 low_latency=True：闸门内部还有字符级安全垫，
                # 不关掉它整句仍会被切在第 N 个字，这个修复就白做了。
                # 关掉是安全的——安全垫防的是"违禁词被截成两半"（"抑"已显示、
                # "郁"还在路上），而句号处不可能截断一个词。
                for ev in out_gate.filter(by_sentence(toks), low_latency=True):
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
    async def transcribe(request: Request, file: UploadFile = File(...),
                         hotwords: str = Form(""), lang: str = Form("zh"),
                         authorization: str = Header(None)):
        """转写。**带这个项目的热词**——孩子说的多半是专有名词。

        `hotwords` 是本次会话**额外**的词（孩子的名字、他刚提到的宠物），
        逗号分隔。这类词比任何静态词表都值钱：词表覆盖"大多数孩子会说
        什么"，它覆盖"这个孩子会说什么"。超预算时先砍静态的。
        """
        # 用户端（/talk）拿的是 UI 门票不是租户钥匙：Chrome 自带识别在国内走不通（送 Google），
        # 所以浏览器把 VAD 切好的 wav 送到这里用腾讯识别。2026-09-14 用户真机撞到 "network" 才改的。
        if _ui_authed(request) or _admin_authed(request):
            voice_role = "assistant"
        else:
            voice_role = who(authorization).voice_role
        audio = await file.read()
        fmt = (file.filename or "a.wav").rsplit(".", 1)[-1].lower()
        extra = [HW.Hotword(w.strip(), 11)
                 for w in hotwords.split(",") if w.strip()]
        # 语言 → 引擎。英文和中英混说都走 16k_zh-PY（中英粤混合）；热词只对中文引擎有意义
        engine = "16k_zh" if (lang or "zh").startswith("zh") else "16k_zh-PY"
        r = tencent.transcribe(audio, fmt if fmt in ("wav", "mp3", "pcm") else "wav",
                               hotwords=HW.build(voice_role, extra) if engine == "16k_zh" else "",
                               engine=engine)
        return {"text": r.text, "duration_ms": r.duration_ms,
                "elapsed_ms": r.elapsed_ms}

    # ------------------------------------------------------------ TTS

    @app.post("/v1/audio/speech")
    async def speech(request: Request, authorization: str = Header(None)):
        body = await request.json()
        text = (body.get("input") or body.get("text") or "").strip()
        if not text:
            raise HTTPException(400, "input 不能为空")
        # 用户端拿 UI 门票：音色由页面带来（= agent manifest 的 voice），角色默认兜底。
        # 系统嗓子（Chrome 挑到 Eddy）太差，2026-09-14 用户一听就说不对，所以朗读也走服务器。
        if _ui_authed(request) or _admin_authed(request):
            voice = V.resolve("assistant", override=body.get("voice"))
        else:
            t = who(authorization)
            voice = V.resolve(t.voice_role, override=body.get("voice") or t.tts_voice)
        audio = tencent.synthesize(text, voice=voice)
        return Response(content=audio, media_type="audio/wav",
                        headers={"X-Voice-Type": str(voice)})

    @app.post("/v1/audio/speech/stream")
    async def speech_stream(request: Request, authorization: str = Header(None)):
        """流式合成：裸 PCM16 分块，首块不等整句。契约与球球 `/api/tts/stream?format=pcm16` 一致
        （响应头 x-sample-rate），所以球球的 speechQueue.js 原样能播。

        一次性接口实测：3 个字 2.1s、43 个字 7.4s——随文本线性涨，对话里等不起。
        流式连不上时**退回一次性**并在头里标出来（x-tts-mode），别让一次静默的回退藏几个月。
        """
        body = await request.json()
        text = (body.get("input") or body.get("text") or "").strip()
        if not text:
            raise HTTPException(400, "text 不能为空")
        if _ui_authed(request) or _admin_authed(request):
            voice = V.resolve("assistant", override=body.get("voice"))
        else:
            t = who(authorization)
            voice = V.resolve(t.voice_role, override=body.get("voice") or t.tts_voice)
        rate = getattr(tencent, "sample_rate", 16000)

        def gen():
            try:
                for chunk in tencent.stream_tts(text, voice=voice, codec="pcm"):
                    yield chunk
            except Exception:
                # 还没吐过字节才能退（吐过再退会把同一句播两遍）；这里 stream_tts 失败都在首块前
                wav = tencent.synthesize(text, voice=voice)
                yield wav[44:] if wav[:4] == b"RIFF" else wav

        return StreamingResponse(gen(), media_type="application/octet-stream",
                                 headers={"x-sample-rate": str(rate), "X-Voice-Type": str(voice),
                                          "Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

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

    # ------------------------------------------------------------ 配置级 agent：直接用六层文件对话
    # spec 014 §6 第 1 步的一半：注册表里的 agent 不经 tenants，人设从 agents/<id>/ 装配，
    # 铁律从 rails 档位注入。鉴权：后台门票（试聊用），或所属产品的租户钥匙。
    from . import agents as AG
    from . import memory as MEM
    from .admin import _authed as _admin_authed, _root as _agents_root

    # 用户端门票：给真人用的页面不该拿后台门票。`VOICEKIT_UI_TOKEN`，?k= 种 cookie vk_ui。
    # 生产上应换成各产品自己的登录态 + 租户钥匙；这把票是"能让真人今天就用上"的最小方案。
    def _ui_authed(request: Request) -> bool:
        tok = os.environ.get("VOICEKIT_UI_TOKEN", "").strip()
        if not tok:
            return False
        given = request.query_params.get("k") or request.cookies.get("vk_ui") or ""
        import hmac as _hmac
        return bool(given) and _hmac.compare_digest(given, tok)

    def _hdr_uid(request: Request) -> str:
        """X-User-Id 是 URL 编码的（HTTP 头只能 ASCII，名字多半是中文）。"""
        import urllib.parse as _up
        return _up.unquote(request.headers.get("x-user-id") or "")

    def _runs_local(ag: AG.Agent) -> bool:
        """配置级永远本地；代码级默认走上游，manifest `run: local` 时按六层文件本地跑（上游没就绪的过渡态）。"""
        up = (ag.manifest.get("upstream") or {}).get("url") or ""
        return ag.kind != "code" or str(ag.manifest.get("run") or "").lower() == "local" or not up.startswith("http")

    def _proxy_upstream(ag: AG.Agent, body: dict, request: Request, uid: str):
        """把一轮转给代码级 agent 的上游（OpenAI 协议），头里带身份；每句仍过本 agent 档位的闸门。"""
        up = ag.manifest.get("upstream") or {}
        key = os.environ.get(str(up.get("key_env") or "")) or ""
        headers = {"Content-Type": "application/json", "X-Agent-Id": ag.id,
                   "X-Session-Id": request.headers.get("x-session-id") or "",
                   "X-User-Id": uid, "X-Room-Id": request.headers.get("x-room-id") or ""}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        asm = AG.assemble(ag)
        out_gate = OutputGate(asm["output_rules"], fallback=asm["fallback_reply"]) if asm["output_rules"] else None
        model = str(ag.manifest.get("model") or "upstream")
        cid = "chatcmpl-" + uuid.uuid4().hex[:24]

        def toks():
            import httpx as _hx
            with _hx.Client(timeout=_hx.Timeout(60.0, connect=10.0)) as c, \
                 c.stream("POST", up["url"], headers=headers, json={**body, "stream": True}) as r:
                if r.status_code != 200:
                    raise RuntimeError(f"上游 HTTP {r.status_code}")
                for line in r.iter_lines():
                    if not line.startswith("data:") or line[5:].strip() == "[DONE]":
                        continue
                    try:
                        d = json.loads(line[5:])["choices"][0]["delta"].get("content") or ""
                    except Exception:
                        continue
                    if d:
                        yield d

        def gen():
            try:
                src = out_gate.filter(by_sentence(toks()), low_latency=True) if out_gate else ({"text": t} for t in by_sentence(toks()))
                for ev in src:
                    t = _strip_stage(ev["text"])
                    if t:
                        yield _sse(_chunk(cid, model, t))
            except Exception as e:
                yield _sse(_chunk(cid, model, f"（上游没接上：{e}）"))
            yield _sse(_chunk(cid, model, None, "stop"))
            yield b"data: [DONE]\n\n"
        return gen()

    def _agent_auth(request: Request, authorization: Optional[str], ag: AG.Agent) -> None:
        if _admin_authed(request) or _ui_authed(request):
            return
        registry.maybe_reload()
        raw = (authorization or "").removeprefix("Bearer ").strip()
        t = registry.by_key(raw) if raw else None
        product = (ag.manifest.get("audience") or {}).get("product")
        if not t or (product and t.id != product):
            raise HTTPException(401, "需要后台门票或该 agent 所属产品的租户钥匙")

    @app.get("/v1/agents")
    def list_agents(request: Request, authorization: str = Header(None)):
        """注册表清单。**用户门票也认** —— `/talk` 首页给持票人列的就是这一份，
        两个口径不一样的话，宿主产品（AI 教练的 Agent 中心）会拿到 401 然后
        静默退成空表，页面上表现为「只有一个 agent」，而错因在鉴权（2026-09-16 踩到）。"""
        if not (_admin_authed(request) or _ui_authed(request)):
            who(authorization)
        return {"agents": [a.summary() for a in AG.load_all(_agents_root())]}

    @app.post("/v1/agents/{agent_id}/chat/completions")
    async def agent_chat(agent_id: str, request: Request, authorization: str = Header(None)):
        d = _agents_root() / agent_id
        if not d.is_dir():
            raise HTTPException(404, f"没有 agent {agent_id}")
        ag = AG.load_agent(d)
        _agent_auth(request, authorization, ag)
        fails = [i for i in AG.validate(ag) if i.severity == "fail"]
        if fails:
            raise HTTPException(409, f"这个 agent 没过静态审查：{fails[0].where} {fails[0].message}")
        body = await request.json()
        messages = list(body.get("messages") or [])
        uid = MEM.safe_uid(_hdr_uid(request))
        # 代码级且 run != local → 交给上游（球球 backend / 教练 orchestrate），闸门仍在这里
        if not _runs_local(ag):
            return StreamingResponse(_proxy_upstream(ag, body, request, uid), media_type="text/event-stream",
                                     headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
        asm = AG.assemble(ag)
        # 记忆装配器 v0：按透镜给 agent 一张「我记得的他」。agent 自己不查库。
        brief = MEM.briefing(_agents_root(), uid, ag.id, ag.lens, agent_name=ag.name) if uid else ""
        if brief:
            asm["system_prompt"] += "\n\n" + brief
        lang = (body.get("lang") or "zh").lower()
        if lang.startswith("en"):
            asm["system_prompt"] += "\n\n# Language\nReply in English. Keep it spoken and short."
        elif lang == "mixed":
            asm["system_prompt"] += "\n\n# 语言\n对方中英混说，你用对方这一句的主要语言回答。"
        user_text = next((m.get("content", "") for m in reversed(messages)
                          if m.get("role") == "user"), "")
        out_gate = OutputGate(asm["output_rules"], fallback=asm["fallback_reply"]) if asm["output_rules"] else None
        in_gate = InputGate(asm["input_rules"]) if asm["input_rules"] else None
        # 模型跟着 agent 走：manifest.model > 请求体 > 网关默认。不同 agent 用不同模型就是这一行。
        model = asm["model"] or body.get("model") or getattr(llm, "model", "deepseek-v4-flash")
        cid = "chatcmpl-" + uuid.uuid4().hex[:24]
        # 前缀缓存的 key 按 agent 不按会话：同一个 agent 的 system prompt 每轮一字不变，
        # 跨会话共享前缀才有命中；X-Session-ID 只用来把同一场路由到同一台推理机。
        cache_key = body.get("prompt_cache_key") or f"agent-{agent_id}"

        def stream():
            if in_gate:
                rule, _m = in_gate.check(user_text)
                if rule:
                    yield _sse(_chunk(cid, model, asm["takeover_reply"]))
                    yield _sse(_chunk(cid, model, None, "stop"))
                    yield b"data: [DONE]\n\n"
                    return
            toks = llm.stream(messages, system=asm["system_prompt"], model=model, thinking=False,
                              cache_key=cache_key, session_id=request.headers.get("x-session-id"),
                              max_tokens=body.get("max_tokens", 1024),
                              reasoning_effort=asm.get("reasoning_effort"))
            # 括号里的舞台提示（「（先接住情绪，不问情境）」）是策略文件泄进正文，语音里会被念出来。
            # 按句剥，剥完还剩字才下发。TRTC 那边对应 FilterBracketsContent。
            sents = (_strip_stage(ev["text"]) for ev in out_gate.filter(by_sentence(toks), low_latency=True)) \
                if out_gate else (_strip_stage(t) for t in by_sentence(toks))
            for text in sents:
                if text:
                    yield _sse(_chunk(cid, model, text))
            yield _sse(_chunk(cid, model, None, "stop"))
            yield b"data: [DONE]\n\n"

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.get("/talk/{agent_id}")
    def talk_page(agent_id: str, request: Request):
        """给真人用的通话页：浏览器自带语音识别 + 合成，走同一个 agent 接口。"""
        d = _agents_root() / agent_id
        if not d.is_dir():
            raise HTTPException(404, f"没有 agent {agent_id}")
        if not (_ui_authed(request) or _admin_authed(request)):
            from fastapi.responses import HTMLResponse as _H
            return _H("<meta charset=utf-8><p>需要门票：地址后面加 <code>?k=&lt;VOICEKIT_UI_TOKEN&gt;</code></p>", 401)
        ag = AG.load_agent(d)
        html = (Path(__file__).parent / "talk.html").read_text(encoding="utf-8")
        blurb = str(ag.manifest.get("blurb") or ag.manifest.get("archetype") or "")
        # 对话里只显示名字，不带「· 直达型成交」这种类型后缀（2026-09-14 用户要求）
        short = str(ag.manifest.get("short_name") or ag.name.split("·")[0].strip() or ag.name)
        html = (html.replace("__NAME__", ag.name).replace("__SHORT__", short).replace("__BLURB__", blurb)
                    .replace("__AGENT__", ag.id).replace("__INITIAL__", short[:1])
                    .replace("__VOICE__", str(ag.manifest.get("voice") or ""))
                    .replace("__MODE__", str(ag.manifest.get("mode") or "video")))
        from fastapi.responses import HTMLResponse as _H
        resp = _H(html)
        if request.query_params.get("k") and _ui_authed(request):
            resp.set_cookie("vk_ui", request.query_params["k"], httponly=True, samesite="lax", max_age=30 * 86400)
        return resp

    @app.get("/talk/lib/recorder.js")
    def talk_recorder_js():
        """VAD 录音机（从球球老师 web/lib/recorder.js 抽来的，零依赖 ES module）。
        改动请两边同步——球球那份是真源，这里是它在共性模块的落地。"""
        from fastapi.responses import Response as _R
        return _R((Path(__file__).parent / "static" / "recorder.js").read_text(encoding="utf-8"),
                  media_type="application/javascript", headers={"Cache-Control": "no-cache"})

    @app.get("/talk/lib/speechQueue.js")
    def talk_speechqueue_js():
        """句级流式播放（从球球老师 web/lib/speechQueue.js 抽来，零依赖）。真源在球球，改动两边同步。"""
        from fastapi.responses import Response as _R
        return _R((Path(__file__).parent / "static" / "speechQueue.js").read_text(encoding="utf-8"),
                  media_type="application/javascript", headers={"Cache-Control": "no-cache"})

    @app.get("/talk")
    def talk_index(request: Request):
        """挑一个 agent。"""
        if not (_ui_authed(request) or _admin_authed(request)):
            from fastapi.responses import HTMLResponse as _H
            return _H("<meta charset=utf-8><p>需要门票：地址后面加 <code>?k=&lt;VOICEKIT_UI_TOKEN&gt;</code></p>", 401)
        k = request.query_params.get("k", "")
        items = "".join(
            f'<li><a href="/talk/{a.id}{"?k=" + k if k else ""}"><b>{a.name}</b>'
            f'<small>{a.manifest.get("blurb") or a.manifest.get("archetype") or ""} · {a.manifest.get("model") or "默认模型"}'
            f'{" · 本地试运行" if a.kind == "code" and _runs_local(a) else ""}{" · 上游" if a.kind == "code" and not _runs_local(a) else ""}</small></a></li>'
            for a in AG.load_all(_agents_root()) if not [i for i in AG.validate(a) if i.severity == "fail"])
        from fastapi.responses import HTMLResponse as _H
        return _H(f"""<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>选一个聊</title><style>body{{font:16px/1.6 -apple-system,"PingFang SC",sans-serif;margin:0;padding:24px;background:#F5F6F8;color:#1B2028}}
h1{{font-size:20px}} ul{{list-style:none;padding:0;display:grid;gap:10px;max-width:520px}} li a{{display:block;background:#fff;border:1px solid #DCE0E6;border-radius:12px;padding:14px 16px;text-decoration:none;color:inherit}}
li a b{{display:block}} li a small{{color:#7B8494}}
@media (prefers-color-scheme:dark){{body{{background:#13161B;color:#E7EAF0}} li a{{background:#1A1E25;border-color:#2A2F38}}}}</style>
<h1>选一个聊</h1><ul>{items}</ul>""")

    @app.post("/v1/agents/{agent_id}/review")
    async def agent_review(agent_id: str, request: Request, authorization: str = Header(None)):
        """结束并回顾：agent 用它自己的方法论看这场对话。非流式。顺手把会话落盘（被用 = 这里数）。"""
        d = _agents_root() / agent_id
        if not d.is_dir():
            raise HTTPException(404, f"没有 agent {agent_id}")
        ag = AG.load_agent(d)
        _agent_auth(request, authorization, ag)
        body = await request.json()
        turns = [t for t in (body.get("turns") or []) if isinstance(t, dict) and t.get("text")]
        if not turns:
            raise HTTPException(400, "没有对话可回顾")
        asm = AG.assemble(ag)
        transcript = "\n".join(f"[{t.get('t', '')}] {'你' if t.get('role') == 'bot' else '对方'}：{t['text']}" for t in turns)
        prompt = (asm["system_prompt"] + "\n\n# 现在不是在对话，是在回顾\n"
                  "下面是你刚才和对方的完整对话。用你的方法论复盘，只写四段，每段两三句，口语：\n"
                  "1. 对方要什么（引用他的原话）\n2. 这场走到了你方法论的哪一步，哪一步跳过了\n"
                  "3. 哪一句没落地（引用你自己的原话）\n4. 下一次先做哪一件事\n"
                  "不打分，不用数字评价人，不客套。")
        out_gate = OutputGate(asm["output_rules"], fallback=asm["fallback_reply"]) if asm["output_rules"] else None
        toks = llm.stream([{"role": "user", "content": transcript}], system=prompt, model=asm["model"],
                          thinking=False, max_tokens=700, reasoning_effort=asm.get("reasoning_effort"))
        text = "".join(ev["text"] for ev in out_gate.filter(toks, low_latency=True)) if out_gate else "".join(toks)
        rec = {"session_id": body.get("session_id") or uuid.uuid4().hex[:8], "agent": ag.id, "version": ag.version,
               "model": asm["model"], "at": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
               "turns": turns, "review": text}
        AG.save_session(_agents_root(), ag.id, rec)
        uid = MEM.safe_uid(_hdr_uid(request) or body.get("uid") or "")
        facts_n = 0
        if uid:
            root = _agents_root()
            MEM.save_session(root, uid, {**rec, "agent_name": ag.name})
            # ③ 事实：平台抽，agent 中立；④ 判断：这个 agent 的回顾落它自己的分片
            new_facts = MEM.extract_facts(llm, turns, model=asm["model"])
            facts_n = len(MEM.merge_facts(root, uid, new_facts, source_session=rec["session_id"])) if new_facts else len(MEM.load_facts(root, uid))
            MEM.save_judgment(root, uid, ag.id, {"at": rec["at"], "session_id": rec["session_id"], "text": text})
        return {"review": text, "session_id": rec["session_id"], "uid": uid, "facts": facts_n}

    @app.get("/me")
    def me_page(request: Request):
        if not (_ui_authed(request) or _admin_authed(request)):
            from fastapi.responses import HTMLResponse as _H
            return _H("<meta charset=utf-8><p>需要门票：地址后面加 <code>?k=&lt;VOICEKIT_UI_TOKEN&gt;</code></p>", 401)
        from fastapi.responses import HTMLResponse as _H
        return _H((Path(__file__).parent / "me.html").read_text(encoding="utf-8"))

    def _uid_or_400(request: Request) -> str:
        if not (_ui_authed(request) or _admin_authed(request)):
            raise HTTPException(401, "需要门票")
        uid = MEM.safe_uid(_hdr_uid(request) or request.query_params.get("u") or "")
        if not uid:
            raise HTTPException(400, "缺 X-User-Id（在对话页「我是」里填个名字）")
        return uid

    @app.get("/v1/users/me/memory")
    def my_memory(request: Request):
        uid = _uid_or_400(request); root = _agents_root()
        return {"uid": uid, "sessions": MEM.session_summaries(root, uid), "facts": MEM.load_facts(root, uid),
                "judgments": {k: v[-3:] for k, v in MEM.load_judgments(root, uid).items()},
                "reviews": MEM.load_reviews(root, uid), "methods": MEM.REVIEW_METHODS}

    @app.post("/v1/users/me/review")
    async def my_review(request: Request):
        """多场综合复盘。method: timeline / themes / agent（用某个 agent 的方法论）。"""
        uid = _uid_or_400(request); root = _agents_root()
        body = await request.json()
        method = str(body.get("method") or "timeline")
        want = set(body.get("session_ids") or [])
        sessions = [s for s in MEM.load_sessions(root, uid) if not want or s.get("session_id") in want][:12]
        if not sessions:
            raise HTTPException(400, "没有可复盘的场次")
        sys_prompt, model = "", None
        if method == "agent":
            aid = str(body.get("agent") or sessions[0].get("agent") or "")
            d = root / aid
            if not d.is_dir():
                raise HTTPException(400, f"没有 agent {aid}")
            ag = AG.load_agent(d); asm = AG.assemble(ag); sys_prompt, model = asm["system_prompt"], asm["model"]
        text = MEM.multi_review(llm, sessions, method=method, agent_system_prompt=sys_prompt, model=model)
        labels = {"timeline": "按时间看变化", "themes": "按主题归纳", "agent": "用 agent 的方法论"}
        rec = {"at": __import__("datetime").datetime.now().isoformat(timespec="seconds"), "method": method,
               "method_label": labels.get(method, method), "agent": body.get("agent") if method == "agent" else None,
               "session_ids": [s.get("session_id") for s in sessions], "text": text}
        MEM.save_review(root, uid, rec)
        return rec

    @app.get("/v1/agents/{agent_id}/voices")
    def agent_voices(agent_id: str, request: Request, lang: str = "zh", authorization: str = Header(None)):
        """用户端能选的音色：agent 默认排最前，再按语言给候选。"""
        d = _agents_root() / agent_id
        if not d.is_dir():
            raise HTTPException(404)
        ag = AG.load_agent(d)
        _agent_auth(request, authorization, ag)
        # manifest 的 voice 可以写角色名（"coach"）也可以写编号（603003）。
        # **回给调用方的必须是解析后的编号** —— 角色名传给前端，选择器匹配不上任何一项，
        # 于是显示第一个、实际合成又用另一个（2026-09-16：RED 的嗓子显示成儿童音色）。
        raw = ag.manifest.get("voice")
        default = V.resolve(str(raw) if raw is not None else "assistant", override=raw)
        return {"default": default, "options": V.options_for(lang, default=default if lang != "en" else None)}

    @app.get("/v1/agents/{agent_id}/prompt")
    def agent_prompt(agent_id: str, request: Request, authorization: str = Header(None)):
        """看装配出来的 system prompt——试聊时对着它调人设。"""
        d = _agents_root() / agent_id
        if not d.is_dir():
            raise HTTPException(404)
        ag = AG.load_agent(d)
        _agent_auth(request, authorization, ag)
        asm = AG.assemble(ag)
        return {"system_prompt": asm["system_prompt"], "rails": ag.rails, "model": asm["model"],
                "output_rules": [r.name for r in asm["output_rules"]],
                "input_rules": [r.name for r in asm["input_rules"]]}

    # ------------------------------------------------------------ Agent 后台
    # 只在配了 VOICEKIT_ADMIN_TOKEN 时存在；没配就 404。真源是 agents/ 目录的文件。
    from .admin import build_router
    app.include_router(build_router())

    return app
