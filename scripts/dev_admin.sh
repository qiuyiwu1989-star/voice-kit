#!/bin/sh
# 本地起后台 + 试聊。LLM 走腾讯 TokenHub（钥匙在 voice-kit/.env，gitignore，不打印）。
# 腾讯 TRTC/ASR/TTS 那几项是假的：本地只做文字试聊，只为让 create_service 起得来。
cd "$(dirname "$0")/.."
if [ -f .env ]; then set -a; . ./.env; set +a; fi
[ -n "$TOKENHUB_API_KEY" ] || { echo "没有 TOKENHUB_API_KEY：写进 voice-kit/.env 再跑"; exit 2; }
export TC_SECRET_ID="${TC_SECRET_ID:-dev}" TC_SECRET_KEY="${TC_SECRET_KEY:-dev}" TENCENT_APPID="${TENCENT_APPID:-1}" \
       TRTC_SDKAPPID="${TRTC_SDKAPPID:-1}" TRTC_SDKSECRETKEY="${TRTC_SDKSECRETKEY:-dev}" \
       VOICEKIT_PUBLIC_URL=http://127.0.0.1:8020 VOICEKIT_TENANTS=deploy/tenants.example.json \
       VOICEKIT_ADMIN_TOKEN="${VOICEKIT_ADMIN_TOKEN:-dev}" VOICEKIT_UI_TOKEN="${VOICEKIT_UI_TOKEN:-go}"
echo "后台：http://127.0.0.1:8020/admin/agents?k=$VOICEKIT_ADMIN_TOKEN"
echo "用户端：http://127.0.0.1:8020/talk?k=$VOICEKIT_UI_TOKEN   （手机同一 WiFi 用本机 IP 代替 127.0.0.1；麦克风只在 localhost 或 https 下可用）"
exec .venv/bin/uvicorn --factory voice_kit.service:create_service --port 8020
