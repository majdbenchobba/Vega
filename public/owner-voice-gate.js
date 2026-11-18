// Speaker checks are local. This gate is not authentication; the host rechecks
// the complete automatic utterance before allowing transcription or messages.
export class OwnerVoiceGate {
  constructor({ post, minimumEvidenceMs = 3000 }) {
    this.post = post;
    this.defaultMinimumEvidenceMs = minimumEvidenceMs;
    this.minimumEvidenceMs = minimumEvidenceMs;
    this.ready = false;
    this.revision = null;
    this.generation = 0;
    this.binding = '';
  }
  configure(status) {
    const profile = status?.profile;
    const deferredOther = profile?.other_check_deferred === true
      && profile.validation?.other === 'pending'
      && ['owner', 'media', 'overlap', 'playback'].every(key => profile.validation?.[key] === 'passed');
    const ready = status?.runtime?.ready === true && profile?.enabled === true
      && (profile.validated === true || deferredOther);
    const revision = Number.isSafeInteger(profile?.revision) ? profile.revision : null;
    // Older hosts retain the longer gate. A new host must advertise support;
    // elapsed time alone never grants an interruption.
    this.minimumEvidenceMs = status?.short_utterance?.version === 'short-utterance-v1'
      && status.short_utterance.min_speech_seconds === 0.3
      ? 300 : this.defaultMinimumEvidenceMs;
    const binding = JSON.stringify([ready, revision, status?.runtime?.model_key, this.minimumEvidenceMs]);
    const changed = binding !== this.binding;
    if (changed) this.generation++;
    this.binding = binding;
    this.ready = ready && revision !== null;
    this.revision = revision;
    return changed;
  }
  reset() { this.generation++; }
  async check(frames) {
    const generation = this.generation, revision = this.revision;
    if (!this.ready || !Array.isArray(frames) || !frames.length) return { owner: false };
    const bytes = frames.reduce((sum, frame) => sum + (frame.pcm?.byteLength || 0), 0);
    if (bytes < this.minimumEvidenceMs * 32 || bytes > 30 * 32000) return { owner: false };
    const pcm = new Uint8Array(bytes);
    let offset = 0;
    for (const frame of frames) {
      if (!(frame.pcm instanceof Uint8Array) || !frame.pcm.length || frame.pcm.length % 2) return { owner: false };
      pcm.set(frame.pcm, offset); offset += frame.pcm.length;
    }
    let binary = '';
    for (const byte of pcm) binary += String.fromCharCode(byte);
    try {
      const result = await this.post('/api/voice/owner/check', {
        audio: btoa(binary), sample_rate: 16000, expected_revision: revision,
      });
      return {
        owner: generation === this.generation && this.ready && revision === this.revision
          && result?.decision === 'owner' && result?.profile_revision === revision,
      };
    } catch {
      return { owner: false };
    }
  }
}
