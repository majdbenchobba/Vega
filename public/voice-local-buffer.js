// Automatic voice is verified locally before transcription. Keep its bounded
// recording here instead of issuing an HTTP request for every 100 ms frame.
export class VegaLocalBuffer {
  constructor({ begin = async () => ({ ok: true }), finish, cancel, onFailure }) {
    this.begin = begin; this.admission = null;
    this.upload = finish; this.abort = cancel; this.onFailure = onFailure;
    this.frames = []; this.bytes = 0; this.started = false; this.closed = false;
    this.cancelled = false; this.failure = null; this.completion = null;
  }
  frame(pcm) {
    if (this.closed || this.failure) return;
    if (!pcm.byteLength || pcm.byteLength > 6400 || pcm.byteLength % 2) {
      return this.fail('Audio input returned an invalid frame.');
    }
    const owned = Uint8Array.from(pcm);
    this.frames.push(owned); this.bytes += owned.byteLength;
    if (!this.started && this.frames.length > 4) this.bytes -= this.frames.shift().byteLength;
    if (this.bytes > 30 * 16000 * 2) this.fail('This voice turn reached its 30-second limit. Please try a shorter sentence.');
  }
  start() {
    if (this.closed || this.started) return;
    this.started = true;
    this.admission = Promise.resolve().then(() => this.cancelled ? { ok: false } : this.begin())
      .catch(() => ({ ok: false, error: { message: 'The voice connection could not start. Resume listening to reconnect.' } }));
  }
  clear() {
    for (const frame of this.frames) frame.fill(0);
    this.frames = []; this.bytes = 0;
  }
  fail(message) {
    if (this.failure) return;
    this.failure = { code: 'VEGA_LOCAL_CAPTURE_FAILED', message };
    this.cancel(); this.onFailure(this.failure);
  }
  finish() {
    if (this.completion) return this.completion;
    this.closed = true;
    this.completion = (async () => {
      if (this.admission) {
        const result = await this.admission;
        if (!result.ok && !this.cancelled) {
          this.fail(result.error?.message || 'The voice connection could not start.');
        }
      }
      if (this.failure) return { ok: false, error: this.failure };
      if (!this.started || this.cancelled) {
        this.clear();
        return { ok: false, error: { code: 'VEGA_CAPTURE_CANCELLED', message: 'The voice turn ended.' } };
      }
      const pcm = new Uint8Array(this.bytes);
      let offset = 0;
      for (const frame of this.frames) { pcm.set(frame, offset); offset += frame.length; }
      this.clear();
      try { return await this.upload(pcm); }
      finally { pcm.fill(0); }
    })();
    return this.completion;
  }
  cancel() {
    if (this.cancelled) return;
    this.cancelled = true; this.closed = true; this.clear();
    void this.abort().catch(() => {});
  }
}
