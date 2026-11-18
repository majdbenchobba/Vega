// Runs on Chromium's audio thread. No network, storage or provider access.
// Integrate fractional input samples so 44.1/48 kHz devices produce 16 kHz PCM
// without resetting the resampler at render-quantum boundaries.
class VegaPcmProcessor extends AudioWorkletProcessor {
  constructor() {
    super()
    this.ratio = sampleRate / 16000
    this.remaining = this.ratio
    this.sum = 0
    this.samples = new Int16Array(1600)
    this.index = 0
    this.energy = 0
    this.active = true
    this.port.onmessage = (event) => { if (event.data?.type === 'stop') this.active = false }
  }
  process(inputs) {
    if (!this.active) return false
    const channels = inputs[0]
    if (!channels?.length || !channels[0]?.length) return true
    for (let index = 0; index < channels[0].length; index++) {
      let amplitude = 0
      for (const channel of channels) amplitude += channel[index] ?? 0
      amplitude /= channels.length
      if (!Number.isFinite(amplitude)) amplitude = 0
      let offset = 0
      while (offset < 1) {
        const weight = Math.min(1 - offset, this.remaining)
        this.sum += amplitude * weight
        this.remaining -= weight
        offset += weight
        if (this.remaining > 1e-9) continue
        const sample = Math.max(-1, Math.min(1, this.sum / this.ratio))
        this.samples[this.index++] = Math.round(sample * (sample < 0 ? 32768 : 32767))
        this.energy += sample * sample
        this.sum = 0
        this.remaining = this.ratio
        if (this.index === this.samples.length) {
          const pcm = new ArrayBuffer(this.samples.length * 2)
          const view = new DataView(pcm)
          for (let i = 0; i < this.samples.length; i++) view.setInt16(i * 2, this.samples[i], true)
          this.port.postMessage({ pcm, level: Math.sqrt(this.energy / this.samples.length) }, [pcm])
          this.index = 0
          this.energy = 0
        }
      }
    }
    return true
  }
}
registerProcessor('vega-pcm', VegaPcmProcessor)
