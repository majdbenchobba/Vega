// A bounded local worker. Stops discard pending audio and terminate model state.
export class VegaSpeechDetector {
  constructor({
    createWorker = () => new Worker('/assets/speech-detector-worker.js', { type: 'module' }),
    setTimer = (...args) => globalThis.setTimeout(...args),
    clearTimer = timer => globalThis.clearTimeout(timer),
  } = {}) {
    this.createWorker = createWorker; this.setTimer = setTimer; this.clearTimer = clearTimer;
    this.worker = null; this.pending = new Map(); this.sequence = 0; this.expected = 0;
    this.starting = null; this.ready = false; this.timer = null;
  }
  start(onFrame, onFailure) {
    if (this.ready) return Promise.resolve(true);
    if (this.starting) return this.starting.promise;
    this.onFrame = onFrame; this.onFailure = onFailure;
    this.sequence = 0; this.expected = 0; this.ready = false;
    const worker = this.createWorker();
    this.worker = worker;
    let resolve, reject;
    const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
    this.starting = { promise, resolve, reject };
    const fail = () => {
      if (this.worker !== worker) return;
      const starting = this.starting;
      this.starting = null;
      this.stop();
      const error = new Error('Speech detection could not start. Resume listening to reconnect.');
      if (starting) starting.reject(error);
      else onFailure(error);
    };
    this.timer = this.setTimer(fail, 15000);
    worker.onerror = fail;
    worker.onmessage = ({ data }) => {
      if (this.worker !== worker) return;
      if (data?.type === 'ready' && this.starting) {
        this.clearTimer(this.timer); this.timer = null; this.ready = true;
        const starting = this.starting; this.starting = null; starting.resolve(true);
        return;
      }
      if (data?.type !== 'frame' || !this.ready || data.sequence !== this.expected
          || !this.pending.has(data.sequence) || !(data.pcm instanceof ArrayBuffer)
          || !Number.isFinite(data.speechProbability) || data.speechProbability < 0
          || data.speechProbability > 1) { fail(); return; }
      const frame = this.pending.get(data.sequence);
      if (data.pcm.byteLength !== frame.byteLength) { fail(); return; }
      this.pending.delete(data.sequence); ++this.expected;
      this.onFrame({ pcm: new Uint8Array(data.pcm), level: frame.level,
        timestampMs: frame.timestampMs, speechProbability: data.speechProbability });
    };
    return promise;
  }
  push(frame) {
    if (!this.ready || !this.worker) return;
    if (this.pending.size >= 12) {
      const onFailure = this.onFailure;
      this.stop(); onFailure(new Error('Speech detection fell behind. Resume listening to reconnect.'));
      return;
    }
    const sequence = this.sequence++;
    this.pending.set(sequence, {
      level: frame.level, timestampMs: frame.timestampMs, byteLength: frame.pcm.byteLength,
    });
    // Keep the caller's buffer ownership intact. Only this copy goes to the worker.
    const pcm = frame.pcm.slice().buffer;
    try { this.worker.postMessage({ type: 'frame', sequence, pcm }, [pcm]); }
    catch {
      const onFailure = this.onFailure;
      this.stop(); onFailure(new Error('Speech detection stopped. Resume listening to reconnect.'));
    }
  }
  stop() {
    this.clearTimer(this.timer); this.timer = null; this.ready = false;
    const starting = this.starting; this.starting = null;
    const worker = this.worker; this.worker = null;
    if (worker) { worker.onmessage = null; worker.onerror = null; worker.terminate(); }
    this.pending.clear(); starting?.resolve(false);
  }
}
