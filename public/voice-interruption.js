// Local, bounded speech-onset detection on the echo-cancelled microphone signal.
// This is not speaker identification and it never opens a provider connection.
export class VegaSpeechInterruption {
  constructor({ threshold = 0.046, onsetMs = 350, maxGapMs = 250 } = {}) {
    this.threshold = threshold; this.onsetMs = onsetMs; this.maxGapMs = maxGapMs;
    this.noiseFloor = 0.005;
    this.reset();
  }
  reset() {
    this.onset = null; this.lastTimestamp = null; this.latched = false; this.holdUntil = 0;
  }
  playbackStarted(timestampMs) {
    // Speech that began before output cannot be an echo of that new output.
    if (this.onset !== null) return;
    // Give browser echo cancellation a short settling interval.
    this.holdUntil = timestampMs + 200;
  }
  push({ level, timestampMs, speechProbability }) {
    if (!Number.isFinite(level) || level < 0 || level > 1
        || !Number.isFinite(timestampMs) || timestampMs < 0) return null;
    if (this.lastTimestamp !== null && (timestampMs < this.lastTimestamp
        || timestampMs - this.lastTimestamp > this.maxGapMs)) this.onset = null;
    this.lastTimestamp = timestampMs;
    if (this.latched) return null;
    const threshold = Math.max(this.threshold, Math.min(0.085, this.noiseFloor * 2.8));
    const neural = Number.isFinite(speechProbability);
    if (neural ? speechProbability < .55 : level < threshold) {
      this.onset = null;
      this.noiseFloor = this.noiseFloor * 0.95 + Math.min(level, 0.03) * 0.05;
      return null;
    }
    if (timestampMs < this.holdUntil) { this.onset = null; return null; }
    this.onset ??= timestampMs;
    if (timestampMs - this.onset < this.onsetMs) return null;
    this.latched = true;
    return this.onset;
  }
}
