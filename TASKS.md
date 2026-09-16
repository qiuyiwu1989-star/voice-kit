# voice-kit backlog

按"不做会出事"排序，不按"做起来爽"排序。

## 共性模块（2026-09-14 用户拍板：两个产品共用，见 球球老师/specs/014）

- [x] **testkit**：跟产品无关的对话质量回归（`voice_kit/testkit/`，18 个自测，2026-09-14）。
      四套用例自带。**还没对真模型跑过**——下一步用 `run --service` 打线上租户看真实通过率。
- [x] **agents/ 人设库 + 后台**（2026-09-14）：四个种子 agent 六层文件、静态审查（第一关）、
      `/admin/agents` 编辑/校验/升版本/跑套件。19 个自测。**平台字典是 v0**：判断维度是我先枚举的
      10 个，要跟球球的采样维度和教练的判据对一遍。
- [x] **配置级 agent 直接对话**（2026-09-14）：`POST /v1/agents/{id}/chat/completions`，六层装配 + rails 档位注入闸门。
      后台加「试聊」标签。三个客户沟通 agent（小安 / 阿询 / 大成）等用户亲测。**还没打过真模型**：
      本地走 MiMo OpenAI 兼容口（api-key 头、不发 enable_thinking），第一声是用户发的，报错看页面。
- [x] **LLM 统一走腾讯 TokenHub，模型按 agent 选**（2026-09-14 用户拍板，不用 MiMo）。关思考改成通用的
      `thinking:{type:disabled}`；模型表 + 校验；三个客户 agent 各配一个模型。选型见 docs/模型选型-TokenHub-2026-09-14.md。
- [x] **用户端 v0**（2026-09-14）：`/talk/{agent}`，浏览器自带语音识别 + 合成，句级朗读、可打断、连续对话，
      文字兜底。门票 `VOICEKIT_UI_TOKEN`。**这是本机就能用的最短路径，不是产品形态**：识别靠 Chrome/Safari，
      嗓子是系统 TTS。真产品走 TRTC 那条（要部署到服务器，用户拍板）。
- [x] **用户端 v0.2「视频对话」**（2026-09-14）：摄像头画面 + 本机录像、带时间戳逐句字幕、agent 插话、
      识别语言独立开关、「结束并回顾」由 agent 用自己的方法论出四段回顾；会话落 `agents/<id>/sessions/`，
      评估卡「被用」从此有数（场数 / 平均来回 / 真实首字）。**录像只在本机**，没上传——上传要先定保留期和删除权。
- [x] **用户端识别改走服务器**（2026-09-14 用户真机撞到 Chrome 识别 "network" 错——它送 Google，国内不通）：
      球球的 VAD 录音机（web/lib/recorder.js）抽进 `voice_kit/static/`，浏览器切段送 `/v1/audio/transcriptions`
      （UI 门票可用），腾讯识别。可打断（录音机自带 bargeIn）。**recorder.js 真源仍在球球，改动两边同步。**
- [x] **对话性能第一轮**（2026-09-14）：停顿 800→700ms；静音 300ms 抢跑识别（recorder 自带 earlyPause），
      识别往返和剩余等待重叠；首句超过 18 字先在逗号切一刀提前出声；前缀缓存 key 按 agent 共享；
      小安 hy3→glm-5.2（首字 3.2→1.3s）；页面底部显示每轮「识别/首字/出声/全句」，并落进会话记录。
- [x] **用户端 v0.3**（2026-09-14 用户反馈）：画面下面 agent 卡片（头像/名字/一句话/在听·在想·在说）+ 控制条 +
      时长/来回/识别/首字/出声；对话每人头像，名字不带类型后缀；打字框在对话下面。
      **「听我讲完」模式**：说「别插嘴/听我说完」自动进入（也有按钮），只记不回，说「我说完了」或停 4 秒再回；
      句尾是「呃/那个/然后」这类没说完的信号多等 1.5s；识别里的「呃」「呢」清掉。
      依据：用户真机对话里说了「你别插嘴」，agent 答应了却在下一个停顿又接——停顿不等于说完（AI 教练 D51 同一课）。
- [x] **用户端 v0.4**（2026-09-14）：语言开关（中文 / English / 中英混说）一个键管识别引擎（16k_zh / 16k_zh-PY）、
      回答语言（进 system prompt）、默认音色；音色可选（中文候选 + 英文 WeJack/WeRose），按 agent 记在本机。
      **识别断句修复**：用户「呃」一下就被切成「女。」「我现在，我。」——① 迟疑判断改在清理语气词之前做（之前先清后判，
      信号被自己抹掉）；② 片段短/结尾迟疑/没收尾 → 等 1.5s；③ 有后文就把两段 **音频拼起来重识别**，不是文字硬接；
      ④ 停顿判定回到 800ms。根治仍是流式识别（下一条）。
- [x] **用户端 v0.5 + 记忆库 v0**（2026-09-14）：
      · 模式：视频 / 仅语音 / 文字，manifest `mode:` 定默认，页面可切；仅语音不开摄像头不录像。
      · 代码级 agent `run: local`：上游没就绪时按六层文件本地跑（球球老师 / RED 教练 / 邱老师三个人设都能试）；
        `run` 去掉即走上游代理（OpenAI 协议 + X-User-Id/X-Agent-Id 头，闸门仍在本地，上游挂了说人话）。
      · 记忆库 `voice_kit/memory.py`：users/<uid>/ 下 ① sessions ③ facts（平台抽、agent 中立、重复出现 seen+1）
        ④ judgments 按 agent 分片；`briefing()` 按透镜装配速览卡进 system prompt；`/me` 页：场次、事实、
        多场综合复盘（timeline / themes / agent 三种方法）。uid 来自用户端自报名字（X-User-Id，URL 编码），只够内测。
- [ ] 记忆库 v1：uid 换成产品登录态；事实的可信度分层（corroborated 跨场重复 / trusted 家长确认）接上；
      判断测试（spec 012 §5.1）的跑器；事实抽取要用例守着（现在是纯 prompt）。
- [ ] **对话性能第二轮：流式识别**。服务端 `tencent.stream()` 已有（WebSocket 到腾讯实时 ASR），缺一个
      FastAPI WebSocket 中继 + 浏览器持续送 16k PCM。收益：说完→拿到文字从 ≈0.7s+残余 降到 ≈0.1s。
      难点在打断/关麦的门控要从 recorder 挪到流式路径上，且我这边没法在浏览器里真测——要用户配合真机验。
- [ ] 识别延迟：一次性识别 ≈ 说话时长 × 0.58（生产实测线性）。真要快得接腾讯流式 ASR（tencent.py 已有 WebSocket 版），
      那是 TRTC 之前就能做的一步（DECISIONS 09-07 的顺序第一位）。
- [ ] 用户端 v1（TRTC）：部署 voice-kit 到服务器 → `/v1/trtc/session` 起房 → 客户端进房。服务器上有 TRTC 凭据。
- [x] **用户端朗读改走服务器流式 TTS**（2026-09-14 用户一听系统嗓子 Eddy 就说不对）：`/v1/audio/speech/stream`
      裸 PCM16 分块 + x-sample-rate，契约与球球 `/api/tts/stream` 一致，球球的 speechQueue.js 抽进 static 直接播。
      一次性接口实测 3 字 2.1s / 43 字 7.4s（随文本线性），所以必须流式。流式挂了退回一次性并在头里标 x-tts-mode。
      三个客户 agent 音色分开：小安 603004 温柔小柠 / 阿询 502006 智小悟 / 大成 603003 随和老李。
- [ ] **在服务器上重量一次首字**：本机跨境 3–5s，服务器 9-05 是 1.3s。绝对值以服务器为准。
- [ ] hy3 本机一次 SSL 断连、hy4-preview 两次 429：小安用 hy3 前先确认稳定，不稳就换 glm-5.2。
- [ ] 后台的「准」「被用」两栏还是空位：判断测试跑器没有；埋点没接。
- [ ] 生产部署后台：VOICEKIT_ADMIN_TOKEN 进 /etc/voicekit.env；agents/ 目录要在服务器上是 git 工作副本，
      不然「历史」一栏永远空、改了也没法回滚。
- [ ] testkit 用例同源守卫：球球 `test_8rules_regression.CASES` 改了要同步 `suites/qiuqiu_8rules.json`，
      现在靠人记；接进 CI 前写个脚本比对。
- [ ] 注册表加 `upstream: {url, key}`，代理支持转发到代码级 agent
- [ ] 从 AI 教练搬：中性 RealtimeAdapter 接口、RealtimeTask 台账 + sweep、TaskExist 当刷新、
      自定义 TTS 指 MiMo、recordToBucket 必填
- [ ] 会话两种模式：对话（每轮回）/ 独白（只转写不回，TurnDetectionMode=1）
- [ ] 三个默认值显式写死：FilterOneWord=false、Timeout≥8、History=0
- [ ] 回调路由 901–910 → agent 钩子；先实测被打断时 905 给的是不是截断文本
- [ ] `say` / `voice` / `switch`
- [ ] 听众作为配置级 agent 入库，rails 档位 = 教练的 forbidden 表；教练的"不打分"也做成档位
- [ ] 探针 `/opt/voicekit`（8010）改造成模块的部署单元，或拆掉重部——二选一，别两个都留

## 部署记录

- **2026-09-14 已部署** voice.qiuyiwu.com：整句放行（e3c6fb4）、热词、agents/ 人设库、后台 `/admin/agents`、
  用户端 `/talk`、配置级 agent 对话 `/v1/agents/{id}/chat/completions`、TokenHub 通用关思考。
  方式：rsync 到 /tmp 暂存 → sudo 放进 /opt/voicekit-svc（root 目录）→ 补 `/etc/voicekit-svc.env` 三个变量
  （VOICEKIT_ADMIN_TOKEN / VOICEKIT_UI_TOKEN 在服务器上 openssl 生成 / VOICEKIT_AGENTS_DIR）→ restart。
  旧代码备份在 `/opt/voicekit-svc/voice_kit.bak-<时间>`。**服务器上 agents/ 不是 git 工作副本**，后台改了
  没有历史、没法回滚——下次部署前先把 voice-kit 推上远端并在服务器 clone。
- [x] ~~`e3c6fb4` 整句放行还没上线~~ 已随本次部署上线。

## 要用户拍板的

- [x] **服务端 reaper：拍板了，而且不用自己设计**（2026-09-14）。AI 教练那边已经有
      `RealtimeTask` 台账 + `realtime:sweep`（阈值 25 分钟，默认只看，`--stop` 才停），
      搬过来即可。原问题（七天 925 分钟、房间忘了关）保留在下面备查。
      客户端那一份仍只有客户端能退，三道闸不动。

- [ ] **TRTC 计费复盘：七天 925 分钟，其中绝大部分是"房间忘了关"**
      （2026-09-09 查账）。全程只起过 8 个 AI 任务，摊下来每个 115 分钟，
      而 MaxIdleTime 是 60-120 秒。已在联调页加了三道自动退房闸门，
      文档也写进去了。但**服务端仍然没有兜底**：`/v1/trtc/session` 起完任务
      就不管了，调用方忘了停就一直烧。要不要加个 reaper（超过 N 分钟的任务
      服务端主动 Stop）？注意 reaper 只能救机器人那一份，
      **客户端赖在房间里的那一份只有客户端自己能退**。

- [ ] **红线（自伤）现在只配给了球球老师**。`coach` 和 `site` 没有输入闸门，
      "我不想活了"会直接进模型。实测那两个租户模型答得还算稳妥（site 甚至
      给了热线），但**"稳妥"是运气不是保证**——模型换一版就可能不一样。
      自伤不是儿童独有的场景，成人项目同样会遇到。
      要不要给所有租户都配一条兜底的 self_harm 输入闸门？
      （这是我配 tenants 时的取舍，不该由我默认替用户决定。）

## 该收的尾

- [ ] **拆掉联调探针** `/opt/voicekit`（8010）+ nginx 的 `/voicekit/` location。
      公共服务上线后它没用了，留着白占端口和注意力。
- [ ] **voice-kit 没有 git remote**——只活在移动硬盘上一份。
      磁盘坏了就全没了。球球老师有远端，这个没有。
- [ ] 球球老师自己还没改用公共服务（它走的是自己的 `backend/llm_openai.py`）。
      **不急**：现在这样两边独立，公共服务挂了不影响生产。
      真要合并得先想清楚多一跳网络值不值。

## 明确不做

计量计费、限流分级、管理后台。自用的几个项目，没有真实需求前那些是负担。
`tenants.json` 留了 id，真需要时不用改结构。
