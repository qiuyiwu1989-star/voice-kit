# voice-kit

跨项目复用的语音对话能力：**ASR → LLM → TTS**，带**输出闸门**。

从球球老师（qiuqiu.yongle.school，生产运行中）抽取。不是从零设计的框架，
是把一套已经在真实儿童用户身上跑了几个月、踩过坑改过的实现固化下来。

---

## 为什么不直接用 Pipecat / LiveKit

调研过（2026-09）。结论是**它们解决的不是我们最难的那个问题**：

| | Pipecat | LiveKit Agents | voice-kit |
|---|---|---|---|
| 供应商可插拔 | ✅ 强项 | ✅ | ✅ |
| WebRTC / 多方 / 视频 | 靠 transport | ✅ 强项 | ❌ 不做 |
| 国内供应商（MiMo/腾讯/火山） | 要自己写 adapter | 同左 | ✅ 原生 |
| **输出闸门（说出口前拦截）** | ❌ | ❌ | ✅ **核心** |

前两者假设"模型输出可信"，做儿童产品不能这么假设。而国内供应商在它们的生态里
本来就要自己写 adapter——那引入框架的成本反而高于收益。

**结论：接口按 Pipecat 的心智设计（provider 可插拔 + pipeline 可组装），实现用自己的。**
将来真要迁移不难，现在不用重写客户端编排。

---

## 三块

```
voice_kit/
  providers/          # 能力抽象：ASR / StreamingASR / TTS 各自独立可换
    base.py           # 接口 + ASRResult（自带 realtime_factor）
    mimo.py           # 小米 MiMo（ASR+TTS 同网关），生产验证过
  gate.py             # 输出闸门 + 输入闸门  ← 别人没有的那块
```

（客户端编排 `web/` —— VAD / ASR 抢跑 / 句级流式 / 可打断，待从球球老师抽取）

### 1. Provider：按能力分，不按厂商分

`StreamingASRProvider` 单独拎出来，因为它不是"更好的 ASR"，是**另一种架构**：

- 一次性识别耗时 ≈ 音频时长 × 0.55~0.59（**生产实测，线性**）
- 流式识别耗时与说话时长无关

调用方必须能**问得出来**谁支持谁不支持，所以有显式的 `supports_streaming`，
不要让人靠 try/except 猜。

### 2. 输出闸门：事前拦截，不是事后道歉

三层，全部从球球老师生产形态抽取：

```
① 安全垫 holdback   末尾 N 字先压住 —— 防违禁词吐一半（"抑"已显示，"郁"还在路上）
② 逐段闸门          只有检查通过的部分才下发 —— 违规内容根本不进输出流
③ 收尾全量检测       命中就整段重写，发 correction 让前端替换
```

**为什么必须事前**：说出去了就是说出去了。"我们记录了这次违规"不是答案。

这也正是**端到端语音大模型（Speech2Speech）目前不能用在儿童产品上的原因**——
音频和文字同时生成，等你看到文字，声音已经在播了。

闸门本身**不含任何具体规则**，规则由调用方注入。voice-kit 只负责机制。

### 3. 输入闸门：命中后接管，不是过滤

球球老师的做法：红线命中时**根本不调 LLM**，直接返回写死的软回应。
模型在最危险的时刻没有发挥空间——这比"让模型小心点"强得多。

---

## 用法

```python
from voice_kit.providers.mimo import MimoProvider
from voice_kit.gate import OutputGate, Rule

p = MimoProvider(api_url=..., api_key=...)

# TTS 流式：首块才是体感延迟
for chunk in p.stream("你好", fmt="pcm16"):
    play(chunk)

# ASR：结果自带倍速比，直接能回答"该不该上流式"
r = p.transcribe(wav, "wav")
print(r.text, r.realtime_factor)      # 0.559

# 输出闸门
gate = OutputGate(
    rules=[Rule(name="禁止主动结束对话", words=["下次再聊"])],
    rewriter=lambda text, v: call_llm_to_rewrite(text, v),
    fallback="我们再多聊一会儿吧。",
)
for ev in gate.filter(model_tokens(), low_latency=True):
    ...   # ev["kind"]: "delta" | "correction"
```

---

## 生产实测数据（2026-09，服务器在腾讯云）

用来判断"优化哪一段"，别凭感觉：

| | 数值 |
|---|---|
| ASR（MiMo，一次性） | **音频时长 × 0.559** — 线性，说 10 秒等 6 秒 |
| TTS 首块 | **674ms** |
| TTS 整句 | 1063ms |
| MiMo 网关 TLS 首字节 | 106~119ms |
| 腾讯 ASR 云API（非流式） | 4609ms vs MiMo 4721ms —— **基本一样** |

**两个已被证伪的假设**（省得别人再走一遍）：

1. ❌「腾讯同云所以更快」——腾讯**流式**端点 155ms 比 MiMo 119ms 还慢。
   只有非流式的云API 域名是 19ms（同地域），但那条路径不解决问题。
2. ❌「换供应商能提速」——腾讯一句话识别 4609ms vs MiMo 4721ms，差 2%。

✅ **真正的杠杆是「流式」这个架构本身，不是换谁。**
MiMo 的 `stream=true` 是**文字流式输出**（音频仍要整段传完），实测首字节
4679ms 反而比非流式全程 4246ms 更慢——对延迟零收益。

---

## 测试

```bash
python -m pytest tests/ -q
```

闸门的测试**带变异验证**——把闸门拆掉必须失败。否则测试是假的。

---

## 状态

- ✅ provider 抽象 + MiMo 实现（生产验证：WAV 拼接 RIFF 头=1，ASR 倍速比 0.559）
- ✅ 输出/输入闸门 + 12 例测试（含变异验证）
- ⬜ 腾讯 provider（流式 WebSocket，缺 AppId）
- ⬜ 客户端编排（VAD / 抢跑 / 句级流式 / 打断）从球球老师抽取
- ⬜ 球球老师切到 voice-kit（**在它稳定前不要动生产代码**）
