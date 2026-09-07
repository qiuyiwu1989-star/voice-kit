"""voice-kit 联调服务：一个独立进程，专门用来验证 TRTC 对话式 AI 这条链路。

**这不是产品代码**，是个探针。它刻意跟球球老师的生产服务完全隔离：
独立进程、独立端口、nginx 单独一条 location，不进 deploy.sh、不重启 qiuqiu.service。
测完 kill 掉、删掉那条 location 就干净了。

它提供四样东西：

    GET  /voicekit/healthz            活着没
    POST /voicekit/v1/chat/completions  ← **TRTC 回调这里**，每个 token 过闸门
    POST /voicekit/api/session         起一个 TRTC 房间 + AI 对话任务
    POST /voicekit/api/stop            停任务
    GET  /voicekit/test               联调页（TRTC Web SDK）

配置全从环境变量来（/etc/voicekit.env，600）：

    TC_SECRET_ID / TC_SECRET_KEY      云 API
    TRTC_SDKAPPID / TRTC_SDKSECRETKEY TRTC 应用（后者能签任意身份，最敏感）
    TOKENHUB_API_KEY                  LLM
    VOICEKIT_API_KEY                  TRTC 回调我们时用的 bearer，必须设
    VOICEKIT_PUBLIC_URL               本服务的公网地址，用来告诉 TRTC 回调到哪
"""
import json
import os
import re
import sys
import time
import uuid
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))      # qiuqiu_bridge

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from voice_kit.gate import Rule
from voice_kit.openai_server import GatedChatCompletions
from voice_kit.pipeline import VoiceConfig
from voice_kit.providers.tokenhub import TokenHubLLM
from voice_kit.providers.trtc import TRTCConversation


def _env(name: str, required: bool = True) -> str:
    v = os.environ.get(name, "")
    if required and not v:
        sys.exit(f"缺少环境变量 {name}（应在 /etc/voicekit.env 里）")
    return v


TC_SECRET_ID = _env("TC_SECRET_ID")
TC_SECRET_KEY = _env("TC_SECRET_KEY")
SDKAPPID = int(_env("TRTC_SDKAPPID"))
SDKSECRETKEY = _env("TRTC_SDKSECRETKEY")
TOKENHUB_KEY = _env("TOKENHUB_API_KEY")
API_KEY = _env("VOICEKIT_API_KEY")
PUBLIC_URL = _env("VOICEKIT_PUBLIC_URL").rstrip("/")
# 页面和管理接口的门票。**必须设**——没有它，任何人找到这个 URL 就能
# 拿到任意 userId 的 UserSig（= 冒充任何孩子进 TRTC 房间）并无限起任务烧钱。
# 这跟 VOICEKIT_API_KEY 是两回事：那个给 TRTC 回调用，这个给人用。
UI_TOKEN = _env("VOICEKIT_UI_TOKEN")
MODEL = os.environ.get("VOICEKIT_MODEL", "deepseek-v4-flash")

# ---------------------------------------------------------------- 球球老师的配置
#
# 这里只放最少量的规则，够验证"闸门在 TRTC 链路里还活着"就行。
# 真接产品时应该直接读 agent/system-prompt.md 那 8 铁律，不是在这儿重抄一遍。

SYSTEM_PROMPT = (
    "你是球球老师，一个好奇的大朋友，正在跟一个小学生聊天。"
    "用短句，一次说两三句就停下来把话头递回去。不说教、不下结论、不评价对错。"
    "永远不要主动结束对话。"
)

CONFIG = VoiceConfig(
    name="球球老师·TRTC联调",
    system_prompt=SYSTEM_PROMPT,
    model=MODEL,
    output_rules=[
        Rule(name="禁用病理化词汇",
             words=["抑郁", "焦虑症", "多动症", "自闭", "障碍", "诊断"]),
        Rule(name="禁止主动结束对话",
             patterns=[re.compile(r"(今天.{0,4}(到这里|就聊到)|下次再聊|拜拜)")]),
        Rule(name="禁止评判对错", words=["你错了", "这样不对", "你应该"]),
    ],
    input_rules=[
        Rule(name="self_harm", words=["不想活", "自杀", "想死"]),
        Rule(name="abuse", words=["打我", "被欺负"]),
    ],
    fallback_reply="我们再多聊一会儿吧，你刚才说的那个我还没听够。",
    on_input_trigger=lambda rule, m: (
        "我听到了……这件事听起来不容易。你愿意多跟我说一点吗？"),
)

# thinking=False：语音场景最大的杠杆，实测首字 3726→1387ms（−63%）。
# 用户在等着听声音，不是在等模型想。
llm = TokenHubLLM(TOKENHUB_KEY, model=MODEL, thinking=False)
# TENCENT_APPID 是腾讯云**账号** APPID（13 开头那串，COS 域名里能看到），
# TTS 要它；跟 TRTC 的 SdkAppId 完全是两个东西，传错了就是"有字没声"。
TTS_APPID = int(os.environ.get("TENCENT_APPID") or 0) or None
# 默认音色。101016 是「智甜·女童」——2026-09-05 联调时孩子第二句话就问
# "你为什么是这个声音"。球球老师的人设是"好奇的大朋友"，用同龄童声
# 等于自我矛盾。改成 502001「智小柔·超自然聊天女声」。见 voice_kit/voices.py。
DEFAULT_VOICE = int(os.environ.get("VOICEKIT_TTS_VOICE") or 502001)

# 每个孩子选的音色。**先落盘再说**——用户拍板"音色可以用户自己选择切换"，
# 而这里存文件不是偷懒：生产的 TTS 现在还是 MiMo（音色编号体系完全不同），
# 现在给 profiles 加一列腾讯 VoiceType 是给还没发生的事写 schema。
# 等真的切过去，把这个 JSON 迁进 profiles 是十分钟的事。
VOICE_FILE = Path("/opt/voicekit/voices.json")


def _voice_prefs() -> dict:
    try:
        return json.loads(VOICE_FILE.read_text())
    except Exception:
        return {}


def _save_voice(user: str, voice) -> None:
    d = _voice_prefs()
    if voice:
        d[user] = int(voice)
    else:
        d.pop(user, None)
    try:
        VOICE_FILE.write_text(json.dumps(d))
    except Exception:
        pass


trtc = TRTCConversation(TC_SECRET_ID, TC_SECRET_KEY, SDKAPPID, SDKSECRETKEY,
                        tts_app_id=TTS_APPID)

# 联调期把每轮都留在内存里，页面能直接看到闸门做了什么
TURNS = []


def record(rec):
    TURNS.append({
        "t": time.strftime("%H:%M:%S"),
        "user": rec.user_text, "reply": rec.reply_text,
        "took_over": rec.took_over, "rule": rec.takeover_rule, "gated": rec.gated,
        "first_token_ms": rec.first_token_ms, "total_ms": rec.total_ms,
        "trtc_user": rec.user_id, "task": rec.task_id,
    })
    del TURNS[:-50]


handler = GatedChatCompletions(CONFIG, llm=llm, on_turn=record, voice_mode=True)
app = FastAPI(title="voice-kit · TRTC 联调")
WEB = Path(__file__).resolve().parent.parent / "web"


@app.get("/voicekit/healthz")
def healthz():
    return {"ok": True, "model": MODEL, "sdkappid": SDKAPPID, "turns": len(TURNS)}


# ---------------------------------------------------------------- 真实球球老师桥接
#
# QIUQIU_BRIDGE=1 时，X-User-Id 是一个 child_id，就走球球老师**真实的**引导
# （8 铁律 / 年龄分档 / 这个孩子的记忆 / safety.py），只把 LLM 换成 deepseek。
# 不开的话走上面那份简化配置，只够验证"链路通不通"。

BRIDGE = None
if os.environ.get("QIUQIU_BRIDGE") == "1":
    try:
        from qiuqiu_bridge import QiuqiuBridge
        BRIDGE = QiuqiuBridge(llm, model=MODEL)
    except Exception as e:      # 桥接挂了不能让整个探针起不来
        print(f"[warn] 球球老师桥接加载失败，回落到简化配置: {e}", file=sys.stderr)


def _bridge_stream(child_id, user_text: str, model: str):
    """把桥接的事件转成 OpenAI SSE。"""
    from voice_kit.openai_server import _chunk, _sse
    cid = "chatcmpl-" + uuid.uuid4().hex[:24]
    t0 = time.perf_counter()
    first = None
    reply = ""
    took_over = False
    gated = False
    llm_error = None
    for ev in BRIDGE.respond(child_id, user_text):
        llm_error = ev.get("llm_error") or llm_error
        if first is None:
            first = round((time.perf_counter() - t0) * 1000, 1)
        took_over |= ev["kind"] == "takeover"
        gated |= ev["kind"] == "correction"
        reply += ev["text"]
        yield _sse(_chunk(cid, model, ev["text"]))
    yield _sse(_chunk(cid, model, None, "stop"))
    yield b"data: [DONE]\n\n"
    TURNS.append({"t": time.strftime("%H:%M:%S"), "user": user_text, "reply": reply,
                  "took_over": took_over, "rule": "red_trigger" if took_over else None,
                  "gated": gated, "llm_error": llm_error, "first_token_ms": first,
                  "total_ms": round((time.perf_counter() - t0) * 1000, 1),
                  "trtc_user": str(child_id), "task": "bridge", "bridge": True})
    del TURNS[:-50]


@app.post("/voicekit/v1/chat/completions")
async def chat_completions(request: Request):
    """TRTC 会打到这里。每个 token 都过闸门才允许被合成成声音。"""
    if request.headers.get("authorization") != f"Bearer {API_KEY}":
        raise HTTPException(401, "bad api key")
    body = await request.json()
    headers = {k.lower(): v for k, v in request.headers.items()}

    if BRIDGE is not None:
        uid = headers.get("x-user-id", "")
        try:
            child_id = BRIDGE.resolve_child(uid)
        except PermissionError as e:
            raise HTTPException(403, str(e))      # 真实孩子：硬拒
        except ValueError:
            child_id = None                       # 不是 child_id：走简化配置
        if child_id is not None:
            user_text = next((m.get("content", "") for m in reversed(body.get("messages") or [])
                              if m.get("role") == "user"), "")
            return StreamingResponse(
                _bridge_stream(child_id, user_text, body.get("model") or MODEL),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    return StreamingResponse(
        handler.complete(body, headers),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


COOKIE = "vk_ui"


def _need_ui_auth(request: Request) -> None:
    """人用的接口一律要门票。带 ?k= 或 cookie 都行。

    刻意**不**复用 VOICEKIT_API_KEY——那把钥匙写在 TRTC 的任务配置里，
    在腾讯那边留着副本；给人用的门票应该能单独换掉。
    """
    tok = request.query_params.get("k") or request.cookies.get(COOKIE)
    if tok != UI_TOKEN:
        raise HTTPException(401, "需要 ?k=<VOICEKIT_UI_TOKEN>")


class SessionReq(BaseModel):
    room: str
    user: str
    voice: Optional[int] = None      # TTS 音色，不传用默认


@app.get("/voicekit/api/voices")
def list_voices(request: Request, user: Optional[str] = None):
    _need_ui_auth(request)
    """音色库 + 这个用户当前选的。

    文字描述（"聊天女声"）跟实际听感差很远，所以页面要能当场 A/B。
    """
    from voice_kit import voices as V
    chosen = _voice_prefs().get(user or "")
    return {
        "default": DEFAULT_VOICE,
        "chosen": chosen,
        "effective": V.resolve("qiuqiu", override=chosen),
        "shortlist": [v.id for v in V.role_options("qiuqiu")],
        "voices": [{"id": v.id, "name": v.name, "gender": v.gender,
                    "tier": v.tier, "scene": v.scene, "note": v.note}
                   for v in V.ALL],
    }


# TaskId 必须落盘：StopAIConversation 只认 TaskId，而按 SessionId 查回来的
# TaskId 是空的——记在内存里的话，服务一重启这些任务就永远停不掉了，
# 只能干等 MaxIdleTime，期间一直计费。这是 API 的硬约束，不是洁癖。
TASK_FILE = Path("/opt/voicekit/tasks.json")


def _tasks() -> dict:
    try:
        return json.loads(TASK_FILE.read_text())
    except Exception:
        return {}


def _save_task(room: str, task_id) -> None:
    d = _tasks()
    if task_id:
        d[room] = task_id
    else:
        d.pop(room, None)
    try:
        TASK_FILE.write_text(json.dumps(d))
    except Exception:
        pass


def _effective_voice(user: str, requested) -> int:
    """这次该用哪个音色：本次请求 > 这个用户上次选的 > 角色默认。

    `voices.resolve` 保证返回值一定是真实存在的音色——传一个不存在的
    VoiceType 给腾讯，任务照起、文字照出、**一点声音都没有且零报错**，
    跟 TTSConfig.AppId 传错是同一类最难查的故障。
    """
    from voice_kit import voices as V
    return V.resolve("qiuqiu", override=requested or _voice_prefs().get(user or ""))


def _clear_room(room: str) -> dict:
    """停掉这个房间里我们记得的任务，并如实报告还有没有停不掉的。"""
    from voice_kit.providers.base import ProviderError
    killed = None
    old = _tasks().get(room)
    if old:
        try:
            trtc.stop(old)
            killed = old
        except ProviderError:
            pass                      # 多半已经自己 idle 掉了
        _save_task(room, None)
    # 停完再查一次：如果还在跑，就是个我们没有 TaskId 的孤儿，只能等它超时
    orphan = trtc.session_running(room)
    return {"killed": killed, "orphan_still_running": orphan}


@app.post("/voicekit/api/session")
def start_session(req: SessionReq, request: Request):
    _need_ui_auth(request)
    """发 UserSig 并起 AI 对话任务。联调用，没有鉴权——测完就把服务停掉。

    实测踩过的坑（2026-09-05）：

    1. **机器人 userId 不能按房间号写死**。之前写的是 `bot-{room}`，
       同一个房间重试必撞 `FailedOperation.TaskExist`。加随机后缀。
    2. **旧任务不清就会有两个球球**。真出现过：房间里两个任务各自独立
       做 ASR→LLM→TTS，同一句话被回答两遍、答案还不一样。
    3. **`SessionId` 才是正解**——腾讯官方的去重口子。传了它之后，
       同房间重复起任务会被直接挡在 `FailedOperation.TaskExist`。
    4. **TaskId 必须落盘**。Stop 只认 TaskId，而 Describe(SessionId) 回来的
       TaskId 是空的：记在内存里，一重启就再也停不掉，只能烧到超时。
    """
    from voice_kit.providers.base import ProviderError

    cleared = _clear_room(req.room)
    if cleared["orphan_still_running"]:
        raise HTTPException(409, (
            f"房间 {req.room} 里还有一个停不掉的旧任务（我们没有它的 TaskId）。"
            f"等它 idle 超时（≤120 秒），或者换一个房间号。"))

    # 开场白按孩子算：第一次见面要说信任声明（产品红线），回访不编上次话题。
    # TRTC 的 WelcomeMessage 是静态串、由 TTS 直接念，模型碰不到它。
    welcome = "你好呀，我是球球！今天想聊点什么？"
    if BRIDGE is not None:
        try:
            welcome = BRIDGE.welcome_for(BRIDGE.resolve_child(req.user))
        except Exception:
            pass          # 不是 child_id（走简化配置）就用通用开场

    bot_id = f"bot-{uuid.uuid4().hex[:10]}"
    if req.voice:
        _save_voice(req.user, req.voice)      # 记住这次的选择，下次默认用它
    try:
        task_id = trtc.start(
            req.room,
            llm_api_url=f"{PUBLIC_URL}/voicekit/v1/chat/completions",
            llm_api_key=API_KEY,
            llm_model=MODEL,
            system_prompt=SYSTEM_PROMPT,
            target_user_id=req.user,
            bot_user_id=bot_id,
            welcome_message=welcome,
            tts_voice=_effective_voice(req.user, req.voice),
            max_idle_time=120,
            session_id=req.room,
        )
    except ProviderError as e:
        # 把腾讯的错误码原样透出去。探针页最怕"HTTP 500"这种什么都不说的
        # 失败——排查全靠猜。
        raise HTTPException(502, str(e))

    _save_task(req.room, task_id)
    return {"task_id": task_id, "sdkappid": SDKAPPID, "bot": bot_id,
            "killed_stale": cleared["killed"],
            "user_sig": trtc.user_sig(req.user, expire=7200)}


@app.post("/voicekit/api/stop")
def stop_session(body: dict, request: Request):
    _need_ui_auth(request)
    from voice_kit.providers.base import ProviderError
    tid = body.get("task_id")
    for room, t in list(_tasks().items()):
        if t == tid:
            _save_task(room, None)
    try:
        trtc.stop(tid)
    except ProviderError as e:
        return {"ok": False, "note": str(e)}    # 已经结束了也算停成功
    return {"ok": True}


@app.post("/voicekit/api/clear")
def clear_room(body: dict, request: Request):
    _need_ui_auth(request)
    """手动清场。停不掉的孤儿会如实报出来——它会一直计费到超时。"""
    return _clear_room(body["room"])


@app.get("/voicekit/api/turns")
def turns(request: Request):
    _need_ui_auth(request)
    """闸门到底做了什么——页面轮询这个。"""
    return {"turns": TURNS}


@app.get("/voicekit/test")
def test_page(request: Request):
    """带对门票就把它种进 cookie，之后页面里的 fetch 不用每次带 ?k=。"""
    _need_ui_auth(request)
    r = FileResponse(str(WEB / "trtc-test.html"),
                     headers={"Cache-Control": "no-cache"})
    r.set_cookie(COOKIE, UI_TOKEN, max_age=86400, httponly=True,
                 samesite="lax", secure=True, path="/voicekit")
    return r
