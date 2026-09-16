// @ts-check
// convkit · 句级流式语音队列 —— 一句一句地合成+播放，无缝拼接。
// 关键收益：模型吐出第一句就开读，不等整段；后面句子的合成与前面句子的播放重叠。
// 所有段共用一个 AudioContext 和 nextTime 游标，段与段之间衔接顺滑。
// 移植自 wendao/lib/speechQueue.ts，改成零依赖原生 JS（ES module），并把「怎么拿音频」
// 抽成注入的 fetchAudio，好适配不同后端（球球 GET / wendao POST 都能用）。

/**
 * @typedef {Object} SpeechQueue
 * @property {(text: string) => void} push  追加一段待读文本（一句/一小节）
 * @property {() => void} end   声明不再有新文本，播完触发 onDrain
 * @property {() => void} stop  立即停止并释放
 */

/** 浏览器是否支持 Web Audio（模组能不能用）。 */
export function speechSupported() {
  return typeof window !== 'undefined' &&
    !!(window.AudioContext || /** @type {any} */ (window).webkitAudioContext);
}

/**
 * 默认的取音频实现：约定后端为 GET `${base}?format=pcm16&text=...&voice=...`，
 * 返回 application/octet-stream 的原始 PCM16 裸字节流 + 响应头 x-sample-rate。
 * 这正是球球老师 `/api/tts/stream?format=pcm16` 的契约。别的后端可自己传 fetchAudio。
 * @param {string} base
 * @returns {(text: string, voice: string|undefined, signal: AbortSignal) => Promise<Response>}
 */
export function pcm16GetFetcher(base) {
  return (text, voice, signal) => {
    const u = new URL(base, location.origin);
    u.searchParams.set('format', 'pcm16');
    u.searchParams.set('text', text);
    if (voice) u.searchParams.set('voice', voice);
    return fetch(u.toString(), { signal });
  };
}

/**
 * 推荐用这个：POST + body，要朗读的文本不进 URL。
 * GET 版会把正文写进服务器访问日志（日志会轮转、进备份，没有权限控制和审计），
 * 对话内容不该出现在那里。契约与 zaowuyun-voice 模组一致。
 * @param {string} base
 * @returns {(text: string, voice: string|undefined, signal: AbortSignal) => Promise<Response>}
 */
export function pcm16PostFetcher(base) {
  return (text, voice, signal) => fetch(base, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(voice ? { text, voice, format: 'pcm16' } : { text, format: 'pcm16' }),
    signal,
  });
}

/**
 * 建一个句级流式语音队列。
 * @param {Object} opts
 * @param {(text: string, voice: string|undefined, signal: AbortSignal) => Promise<Response>} opts.fetchAudio
 *        怎么去拿一段文本的 PCM16 流（见 pcm16GetFetcher）。响应必须是原始 PCM16 裸字节；
 *        采样率从响应头 x-sample-rate 读，缺省 24000。
 * @param {string} [opts.voice]
 * @param {() => void} [opts.onStart]  第一声真正播出时
 * @param {() => void} [opts.onDrain]  全部播完（自然结束）时
 * @returns {SpeechQueue}
 */
export function createSpeechQueue(opts) {
  const Ctor = window.AudioContext || /** @type {any} */ (window).webkitAudioContext;
  // 起播抖动缓冲：越大越不容易卡，代价是第一声晚一点。0.35s 人耳基本无感。
  const LEAD_SEC = opts.leadSec != null ? opts.leadSec : 0.35;
  // 每个音频节点至少这么长，避免把很碎的网络分片一片一个节点地排下去。
  const MIN_PIECE_SEC = opts.minPieceSec != null ? opts.minPieceSec : 0.25;
  // 同时在途的 TTS 请求上限：1 个在播 + 1 个预取。
  // 原来是"push 几句就同时抓几句"，正在播的那句要跟后面几句抢带宽，
  // 分片更容易晚到——为了消句间空隙反而把句内弄卡了（日志实锤同秒 3 个请求）。
  const MAX_INFLIGHT = opts.maxInflight != null ? opts.maxInflight : 2;
  let inflight = 0;
  let underruns = 0;   // 数据晚到、被迫重排的次数，诊断用
  /** @type {AudioContext|null} */
  let ctx = null;
  let nextTime = 0;
  let started = false, stopped = false, ended = false, pending = 0;
  /** @type {Promise<void>} */
  let chain = Promise.resolve();
  /** @type {AudioBufferSourceNode[]} */
  const sources = [];
  /** @type {AbortController[]} */
  const controllers = [];

  function cleanup() {
    if (ctx) { ctx.close().catch(() => {}); ctx = null; }
  }

  function maybeDrain() {
    if (!ended || pending > 0 || stopped) return;
    const c = ctx;
    const waitMs = c ? Math.max(0, (nextTime - c.currentTime) * 1000) + 90 : 0;
    setTimeout(() => {
      if (stopped) return;
      if (opts.onDrain) opts.onDrain();
      cleanup();
    }, waitMs);
  }

  async function synth(resPromise) {
    if (stopped) return;
    let res;
    try {
      res = await resPromise;
    } catch { return; }
    if (stopped || !res || !res.ok || !res.body) return;

    const rate = Number(res.headers.get('x-sample-rate')) || 24000;
    if (!ctx) ctx = new Ctor();
    const c = ctx;
    try { await c.resume(); } catch { /* 某些浏览器无需 resume */ }
    if (!started) {
      started = true;
      // 起播前的抖动缓冲。原来只有 0.12s——手机网络上任何一个分片晚到超过 120ms，
      // nextTime 就落到 currentTime 后面，被迫改排到"现在+0.02"，产生一个能听见的
      // 断点，而且之后每次晚到都再断一次（实测：电话模式明显卡顿）。
      // 换成 LEAD_SEC：起播晚一点点（听不出来），但能吃掉常见的网络抖动。
      nextTime = c.currentTime + LEAD_SEC;
      if (opts.onStart) opts.onStart();
    }

    const reader = res.body.getReader();
    /** @type {Uint8Array|null} */
    let leftover = null;
    /** 攒够一定时长再排一个节点：网络分片可能很碎，一片一个节点会让时序非常紧，
     *  任何一点抖动都变成可听见的断点。 */
    /** @type {Uint8Array[]} */
    let pend = [];
    let pendBytes = 0;
    const minBytes = Math.floor(rate * 2 * MIN_PIECE_SEC);   // int16 = 2 字节/采样

    const flush = () => {
      if (!pendBytes) return;
      const merged = new Uint8Array(pendBytes);
      let o = 0;
      for (const p of pend) { merged.set(p, o); o += p.length; }
      pend = []; pendBytes = 0;

      const int16 = new Int16Array(merged.buffer, 0, merged.length / 2);
      const f32 = new Float32Array(int16.length);
      for (let i = 0; i < int16.length; i++) f32[i] = int16[i] / 32768;

      const buf = c.createBuffer(1, f32.length, rate);
      buf.copyToChannel(f32, 0);
      const src = c.createBufferSource();
      src.buffer = buf;
      src.connect(c.destination);
      // nextTime 落后于当前时间 = 数据晚到、已经断了一次。记一笔便于诊断。
      if (nextTime < c.currentTime) underruns++;
      const at = Math.max(nextTime, c.currentTime + 0.02);
      src.start(at);
      nextTime = at + buf.duration;
      sources.push(src);
    };

    for (;;) {
      let chunk;
      try { chunk = await reader.read(); } catch { break; }
      if (stopped) { reader.cancel().catch(() => {}); break; }
      if (chunk.done) break;
      let bytes = chunk.value;
      if (!bytes || bytes.length === 0) continue;
      if (leftover) {
        const merged = new Uint8Array(leftover.length + bytes.length);
        merged.set(leftover); merged.set(bytes, leftover.length);
        bytes = merged; leftover = null;
      }
      const usable = bytes.length - (bytes.length % 2); // int16 对齐，奇数尾字节留到下块
      if (usable < bytes.length) leftover = bytes.slice(usable);
      if (usable === 0) continue;

      pend.push(bytes.slice(0, usable));
      pendBytes += usable;
      if (pendBytes >= minBytes) flush();
    }
    flush();   // 收尾：不足一片的也要播，否则每句最后一小截被吞掉
  }

  return {
    push(text) {
      const t = (text || '').trim();
      if (!t || stopped) return;
      pending++;
      // 提前发请求能把 TTS 的固定启动开销和上一句的播放重叠掉；但**不能无限提前**，
      // 否则正在播的那句要跟后面所有句子抢带宽，分片晚到就卡（见 MAX_INFLIGHT）。
      // 播放顺序始终由 chain 串行保证（谁先 push 谁先排上 nextTime），不会串句。
      const fire = () => {
        const ac = new AbortController();
        controllers.push(ac);
        inflight++;
        return Promise.resolve()
          .then(() => opts.fetchAudio(t, opts.voice, ac.signal))
          .catch(() => null);
      };
      const early = inflight < MAX_INFLIGHT ? fire() : null;
      chain = chain
        .then(() => synth(early || fire()))
        .catch(() => {})
        .then(() => {
          inflight = Math.max(0, inflight - 1);
          pending--;
          maybeDrain();
        });
    },
    /** 诊断：数据晚到、被迫重排的次数。>0 说明还在卡，可以调大 leadSec。 */
    stats() { return { underruns }; },
    end() { ended = true; maybeDrain(); },
    stop() {
      if (stopped) return;
      stopped = true;
      for (const a of controllers) { try { a.abort(); } catch { /* ignore */ } }
      for (const s of sources) { try { s.stop(); } catch { /* ignore */ } }
      cleanup();
    },
  };
}
