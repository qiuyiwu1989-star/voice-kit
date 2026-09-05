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
import os
import re
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

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

llm = TokenHubLLM(TOKENHUB_KEY, model=MODEL)
trtc = TRTCConversation(TC_SECRET_ID, TC_SECRET_KEY, SDKAPPID, SDKSECRETKEY)

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


@app.post("/voicekit/v1/chat/completions")
async def chat_completions(request: Request):
    """TRTC 会打到这里。每个 token 都过闸门才允许被合成成声音。"""
    if request.headers.get("authorization") != f"Bearer {API_KEY}":
        raise HTTPException(401, "bad api key")
    body = await request.json()
    return StreamingResponse(
        handler.complete(body, dict(request.headers)),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


class SessionReq(BaseModel):
    room: str
    user: str


LIVE_TASKS = {}      # room -> task_id，用来在重试前把上一次的收干净


@app.post("/voicekit/api/session")
def start_session(req: SessionReq):
    """发 UserSig 并起 AI 对话任务。联调用，没有鉴权——测完就把服务停掉。

    两个踩过的坑（2026-09-05 实测）：

    1. **机器人 userId 不能按房间号写死**。之前写的是 `bot-{room}`，
       于是同一个房间重试必撞 `FailedOperation.TaskExist:
       robot userId repeat request in same room`。加随机后缀。
    2. 上一次的任务可能还活着（MaxIdleTime 没到）。所以起新任务前
       先把本进程记得的那个停掉——不然房间里会有两个机器人抢着说话。
    """
    from voice_kit.providers.base import ProviderError

    old = LIVE_TASKS.pop(req.room, None)
    if old:
        try:
            trtc.stop(old)
        except Exception:
            pass          # 它可能已经自己 idle 掉了，停不掉不是错

    bot_id = f"bot-{uuid.uuid4().hex[:10]}"
    try:
        task_id = trtc.start(
            req.room,
            llm_api_url=f"{PUBLIC_URL}/voicekit/v1/chat/completions",
            llm_api_key=API_KEY,
            llm_model=MODEL,
            system_prompt=SYSTEM_PROMPT,
            target_user_id=req.user,
            bot_user_id=bot_id,
            welcome_message="你好呀，我是球球！今天想聊点什么？",
            max_idle_time=120,
        )
    except ProviderError as e:
        # 把腾讯的错误码原样透出去。探针页最怕的就是"HTTP 500"这种
        # 什么都不说的失败——排查全靠猜。
        raise HTTPException(502, str(e))

    LIVE_TASKS[req.room] = task_id
    return {"task_id": task_id, "sdkappid": SDKAPPID, "bot": bot_id,
            "user_sig": trtc.user_sig(req.user, expire=7200)}


@app.post("/voicekit/api/stop")
def stop_session(body: dict):
    from voice_kit.providers.base import ProviderError
    tid = body.get("task_id")
    for room, t in list(LIVE_TASKS.items()):
        if t == tid:
            LIVE_TASKS.pop(room, None)
    try:
        trtc.stop(tid)
    except ProviderError as e:
        return {"ok": False, "note": str(e)}    # 已经结束了也算停成功
    return {"ok": True}


@app.get("/voicekit/api/turns")
def turns():
    """闸门到底做了什么——页面轮询这个。"""
    return {"turns": TURNS}


@app.get("/voicekit/test")
def test_page():
    return FileResponse(str(WEB / "trtc-test.html"),
                        headers={"Cache-Control": "no-cache"})
