// Silero's 16 kHz streaming convention: 512 samples plus 64 context samples.
// The inference adapter is supplied by the local worker; this module has no I/O.
export class SileroStream {
  constructor({ session, Tensor }) {
    this.session = session; this.Tensor = Tensor;
    this.state = new Tensor('float32', new Float32Array(256), [2, 1, 128]);
    this.rate = new Tensor('int64', new BigInt64Array([16000n]), []);
    this.context = new Float32Array(64);
    this.pending = new Float32Array(512); this.used = 0; this.lastProbability = 0;
  }
  async process(pcm) {
    if (!(pcm instanceof Uint8Array) || !pcm.length || pcm.length % 2 || pcm.length > 6400) {
      throw new Error('Invalid voice frame.');
    }
    const view = new DataView(pcm.buffer, pcm.byteOffset, pcm.byteLength);
    let probability = 0, completed = false;
    for (let offset = 0; offset < view.byteLength; offset += 2) {
      this.pending[this.used++] = view.getInt16(offset, true) / 32768;
      if (this.used !== 512) continue;
      const input = new Float32Array(576);
      input.set(this.context); input.set(this.pending, 64);
      const result = await this.session.run({
        input: new this.Tensor('float32', input, [1, 576]), state: this.state, sr: this.rate,
      });
      const next = result.output?.data?.[0];
      if (!Number.isFinite(next) || next < 0 || next > 1
          || result.stateN?.data?.length !== 256) throw new Error('Invalid speech detection result.');
      this.state = result.stateN;
      this.context.set(this.pending.subarray(448));
      this.used = 0; this.lastProbability = next;
      probability = Math.max(probability, next); completed = true;
    }
    return completed ? probability : this.lastProbability;
  }
}
