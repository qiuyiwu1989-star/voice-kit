// @ts-check
// convkit · 语音活动检测(VAD)录音机 —— 电话模式的"耳朵"。
// 免手动按键：孩子开口就录、停顿一下就自动把这段话切出来交给上层做 ASR。
// 自适应噪声地板 + 说话前 pre-roll 补头，输出 16k 单声道 WAV（后端 ASR 契约）。
// getUserMedia 打开回声消除/降噪/自动增益（借鉴 wendao）；支持 pause()——
// 球球说话时把麦克风"门"关上，避免把球球自己的声音又录进去形成回声/自问自答。
// 零依赖 ES module，浏览器可跑（ScriptProcessor 兼容微信内置浏览器/iOS Safari）。

const TARGET_RATE = 16000;

/** 浏览器能不能用这个录音机。 */
export function recorderSupported() {
  return typeof navigator !== 'undefined'
    && !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia)
    && !!(window.AudioContext || /** @type {any} */ (window).webkitAudioContext);
}

/**
 * @param {Object} opts
 * @param {(wav: ArrayBuffer, durMs: number) => void} opts.onSegment 一段话说完（含尾静音）时回调
 * @param {(info: {rms:number, speaking:boolean}) => void} [opts.onLevel] 每帧电平（做动效）
 * @param {number} [opts.silenceMs=800]   说完后静音多久算一句结束
 * @param {number} [opts.minSpeechMs=350] 短于此当误触丢弃
 * @param {number} [opts.maxSegmentMs=15000] 单段硬上限，防止一直说不停
 * @param {number} [opts.earlyPauseMs]   抢跑：静音到这个时长就先把音频吐出来（见 onEarlyPause）。
 *        不设则不抢跑。必须 < silenceMs 才有意义。
 * @param {(wav: ArrayBuffer) => void} [opts.onEarlyPause] 抢跑回调——此刻还不确定是"说完了"
 *        还是"只是喘口气"，上层可以先拿去跑 ASR，把网络往返和剩下的静音等待重叠掉。
 *        后续 onSegment 的第三个参数会告诉你这次抢跑到底作不作数。
 * @param {number} [opts.bargeInMs=0] 打断：球球说话期间，孩子连续出声多久就算插话。
 *        **0 = 关闭**（默认）。开之前请先真机试，理由见下面 watch 模式的注释。
 * @param {number} [opts.bargeInGain=3.2] 打断阈值 = 漏音电平 × 这个倍数。
 * @param {() => void} [opts.onBargeIn] 判定为插话时回调——上层只需要让球球闭嘴。
 *        插话开头那段音频录音器自己留着了，后面照常走 onSegment，不用拼。
 */
export function createRecorder(opts) {
  const silenceMs = opts.silenceMs != null ? opts.silenceMs : 800;
  const earlyPauseMs = opts.earlyPauseMs != null ? opts.earlyPauseMs : 0;
  const minSpeechMs = opts.minSpeechMs != null ? opts.minSpeechMs : 350;
  const maxSegmentMs = opts.maxSegmentMs != null ? opts.maxSegmentMs : 15000;
  const bargeInMs = opts.bargeInMs != null ? opts.bargeInMs : 0;
  const bargeInGain = opts.bargeInGain != null ? opts.bargeInGain : 3.2;
  const Ctor = window.AudioContext || /** @type {any} */ (window).webkitAudioContext;

  let ctx = null, stream = null, source = null, proc = null, mute = null;
  let running = false, gateOpen = true;
  let speaking = false, speechStartMs = 0, lastVoiceMs = 0;
  let earlyFired = false, earlyStale = false;   // 抢跑状态（见 earlyPauseMs）
  let noise = 0.006, inRate = 48000;
  // 打断检测用的是**另一条**地板线，见 onAudio 里 watch 分支的注释。
  let watch = false, leak = 0, bargeStart = 0, justBarged = false, settleUntil = 0;
  // 球球刚开口的头一段时间只用来**量漏音**，不做打断判定。
  // 没有这段，冷启动必然误触发：漏音地板的初值只要低于实际漏音，
  // 第一帧就过线，而地板又只在"没有插话嫌疑"时才学习——它永远学不到，
  // 于是球球每次一开口就把自己打断。（这两条正是测试 ① 和 ③ 抓到的。）
  // 物理上也成立：球球刚说第一个字时，孩子还没来得及插话。
  const BARGE_SETTLE_MS = 500;
  /** @type {Float32Array[]} */ let seg = [];
  /** @type {Float32Array[]} */ let preRoll = []; let preRollLen = 0;
  // 平时 ~0.1s 足够把开口瞬间的头补回来；开了打断就得留够**整个检测窗口**，
  // 否则判定成立的那一刻，孩子插话的前半句已经被丢掉了。
  const PREROLL_SAMPLES = Math.max(4800, Math.round((bargeInMs + 250) * 48));

  const now = () => (typeof performance !== 'undefined' && performance.now)
    ? performance.now() : Date.now();

  function onAudio(e) {
    if (!running) return;
    const input = e.inputBuffer.getChannelData(0);
    let sum = 0;
    for (let i = 0; i < input.length; i++) { const v = input[i]; sum += v * v; }
    const rms = Math.sqrt(sum / input.length);
    const t = now();

    // 麦克风门关着（球球在说话）
    if (!gateOpen) {
      if (opts.onLevel) opts.onLevel({ rms, speaking: false });
      if (!watch || !bargeInMs || !opts.onBargeIn) return;

      // ——— 打断检测 ———
      // 关键在参照物：球球说话时麦克风收到的**不是静音**，是 AEC 没消干净、
      // 漏回来的球球自己的声音。拿上面那条静音地板（noise）当阈值必然误触发，
      // 而且设备越差漏得越多、误触发越狠——恰恰是最该保守的那些机器。
      //
      // 所以这里单独跟一条"漏音地板"：它量的是"这台机器上，球球正在说话时
      // 麦克风本底有多高"。孩子要插话，得明显盖过**那个**，不是盖过安静。
      // 这样阈值会自己适应设备的 AEC 好坏和当前音量。
      const copyW = new Float32Array(input);
      preRoll.push(copyW); preRollLen += copyW.length;
      while (preRollLen > PREROLL_SAMPLES) { const r = preRoll.shift(); preRollLen -= r.length; }

      // 沉淀期：无条件学漏音，不判定
      if (t < settleUntil) {
        leak = leak ? leak * 0.8 + rms * 0.2 : rms;   // 第一帧直接当种子，收敛快
        return;
      }
      const bar = Math.max(0.020, leak * bargeInGain);
      if (rms > bar) {
        if (!bargeStart) bargeStart = t;
        // 要连续够久才算。一声咳嗽、一下桌子响都会瞬间过线——
        // 误打断比不能打断更糟：球球话说到一半自己停了，孩子只会觉得它坏了。
        else if (t - bargeStart >= bargeInMs) {
          // **自己无缝接上**，而不是丢一段 wav 给上层让它想办法。
          // 判定成立时孩子已经说了 bargeInMs 那么久了，那段话是他插话的
          // 开头——"等一下，我还想说…"。丢掉开头，转写就是残的。
          // 所以直接进入说话态，把 preRoll 当作这段话的头，
          // 后面照常走 VAD 和 onSegment，上层什么都不用拼。
          gateOpen = true; watch = false; justBarged = true;
          speaking = true; speechStartMs = bargeStart; lastVoiceMs = t;
          seg = preRoll.slice();
          bargeStart = 0;
          opts.onBargeIn();          // 上层只要做一件事：让球球闭嘴
        }
      } else {
        bargeStart = 0;
        leak = leak * 0.92 + rms * 0.08;   // 只在没有插话嫌疑时更新，否则地板会被插话本身抬高
      }
      return;
    }
    // 自适应噪声地板（仅非说话态更新）
    if (!speaking) noise = noise * 0.92 + rms * 0.08;
    const thresh = Math.max(0.010, noise * 2.2);
    if (opts.onLevel) opts.onLevel({ rms, speaking });

    const copy = new Float32Array(input);
    preRoll.push(copy); preRollLen += copy.length;
    while (preRollLen > PREROLL_SAMPLES) { const r = preRoll.shift(); preRollLen -= r.length; }

    if (rms > thresh) {
      if (!speaking) { speaking = true; speechStartMs = t; seg = preRoll.slice(); }
      // 抢跑之后又开口了 —— 说明刚才只是喘口气，那份抢跑的音频不完整、作废
      if (earlyFired) earlyStale = true;
      seg.push(copy); lastVoiceMs = t;
    } else if (speaking) {
      seg.push(copy);
      const quiet = t - lastVoiceMs;
      if (earlyPauseMs && !earlyFired && quiet > earlyPauseMs && opts.onEarlyPause
          && t - speechStartMs >= minSpeechMs) {
        earlyFired = true; earlyStale = false;
        opts.onEarlyPause(encode16kWav(seg, inRate));
      }
      if (quiet > silenceMs || t - speechStartMs > maxSegmentMs) endSegment(t);
    }
  }

  function endSegment(t) {
    const dur = t - speechStartMs; speaking = false;
    const chunks = seg; seg = [];
    const fired = earlyFired, stale = earlyStale;
    earlyFired = false; earlyStale = false;
    if (dur < minSpeechMs) return;         // 太短，误触
    const wav = encode16kWav(chunks, inRate);
    // earlyUsable：抢跑那份音频和这份的差别只是尾部多了点静音，转出来的文字一样，
    // 可以直接用抢跑的结果，省掉一次完整往返。中途又开口过就不行。
    if (opts.onSegment) opts.onSegment(wav, dur, { earlyUsable: fired && !stale });
  }

  async function start() {
    if (running) return;
    stream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
    });
    ctx = new Ctor(); inRate = ctx.sampleRate;
    source = ctx.createMediaStreamSource(stream);
    proc = ctx.createScriptProcessor(4096, 1, 1);
    proc.onaudioprocess = onAudio;
    // proc 必须接到 destination 才会持续回调；但直连会把麦克风原声放出来 → 反馈啸叫。
    // 中间串一个 gain=0 的静音节点：回调照常触发，扬声器里听不到麦克风。
    mute = ctx.createGain(); mute.gain.value = 0;
    source.connect(proc); proc.connect(mute); mute.connect(ctx.destination);
    running = true; gateOpen = true; speaking = false;
    seg = []; preRoll = []; preRollLen = 0;
    try { await ctx.resume(); } catch (e) { /* 某些浏览器无需 resume */ }
  }

  // 关麦门/重新开麦时抢跑状态也要清掉，否则上一轮的残留会污染下一轮的 earlyUsable 判断
  /** @param {boolean} [watchForBargeIn] 关麦期间是否继续盯着"孩子想插话" */
  function pause(watchForBargeIn) {
    gateOpen = false; speaking = false; seg = []; earlyFired = false; earlyStale = false;
    watch = !!watchForBargeIn && !!bargeInMs && !!opts.onBargeIn;
    bargeStart = 0; preRoll = []; preRollLen = 0;
    leak = 0; settleUntil = watch ? now() + BARGE_SETTLE_MS : 0;
  }
  function resume() {
    watch = false; bargeStart = 0;
    // 刚因为插话自己开的麦：这时上层正好会调一次 resume（interrupt → backToListen）。
    // 照常清空的话，恰恰会把插话的开头擦掉——那是这个功能唯一要保住的东西。
    if (justBarged) { justBarged = false; return; }
    gateOpen = true; speaking = false; seg = []; lastVoiceMs = now();
    earlyFired = false; earlyStale = false;
  }

  function stop() {
    running = false;
    try { if (proc) { proc.disconnect(); proc.onaudioprocess = null; } } catch (e) { /* ignore */ }
    try { if (mute) mute.disconnect(); } catch (e) { /* ignore */ }
    try { if (source) source.disconnect(); } catch (e) { /* ignore */ }
    try { if (stream) stream.getTracks().forEach((x) => x.stop()); } catch (e) { /* ignore */ }
    try { if (ctx) ctx.close(); } catch (e) { /* ignore */ }
    ctx = stream = source = proc = mute = null;
  }

  return { start, pause, resume, stop };
}

/** 把若干 Float32 帧拼起来、降采样到 16k、封成 WAV(PCM16 单声道)。 */
function encode16kWav(chunks, inRate) {
  let total = 0; for (const c of chunks) total += c.length;
  const flat = new Float32Array(total);
  let o = 0; for (const c of chunks) { flat.set(c, o); o += c.length; }
  const ratio = inRate / TARGET_RATE;
  const n = Math.max(1, Math.floor(flat.length / ratio));
  const pcm = new Int16Array(n);
  for (let i = 0; i < n; i++) {
    const s = flat[Math.floor(i * ratio)] || 0;
    pcm[i] = Math.max(-1, Math.min(1, s)) * 0x7FFF;
  }
  const b = new ArrayBuffer(44 + n * 2), dv = new DataView(b);
  const ws = (off, s) => { for (let i = 0; i < s.length; i++) dv.setUint8(off + i, s.charCodeAt(i)); };
  ws(0, 'RIFF'); dv.setUint32(4, 36 + n * 2, true); ws(8, 'WAVE'); ws(12, 'fmt ');
  dv.setUint32(16, 16, true); dv.setUint16(20, 1, true); dv.setUint16(22, 1, true);
  dv.setUint32(24, TARGET_RATE, true); dv.setUint32(28, TARGET_RATE * 2, true);
  dv.setUint16(32, 2, true); dv.setUint16(34, 16, true); ws(36, 'data'); dv.setUint32(40, n * 2, true);
  new Int16Array(b, 44).set(pcm);
  return b;
}
