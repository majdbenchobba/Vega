const SYNTHESIS_FAILED = 'The spoken reply could not be prepared. Check the saved voice connection and try again.';
const PLAYBACK_FAILED = 'The browser could not play that spoken reply. Try playing it again.';
const PLAYBACK_BLOCKED = 'The browser blocked voice playback. Click or tap the page, then try playing the reply again.';

export class VegaVoicePlayback {
  constructor({
    post,
    onError,
    onStarted = () => {},
    createAudio = url => new Audio(url),
    createObjectURL = blob => URL.createObjectURL(blob),
    revokeObjectURL = url => URL.revokeObjectURL(url),
  }) {
    this.post = post;
    this.onError = onError;
    this.onStarted = onStarted;
    this.createAudio = createAudio;
    this.createObjectURL = createObjectURL;
    this.revokeObjectURL = revokeObjectURL;
    this.generation = 0;
    this.audio = null;
    this.audioUrl = null;
  }

  stop() {
    this.generation++;
    const audio = this.audio;
    const url = this.audioUrl;
    this.audio = null;
    this.audioUrl = null;

    // Invalidate ownership and detach events before releasing a media resource:
    // pause/load can reject play(), and queued callbacks may still arrive.
    const cleanup = [];
    if (audio) {
      audio.onended = null;
      audio.onerror = null;
      cleanup.push(
        () => audio.pause(),
        () => audio.removeAttribute('src'),
        () => audio.load(),
      );
    }
    if (url !== null) cleanup.push(() => this.revokeObjectURL(url));
    for (const release of cleanup) {
      try { release(); }
      catch { /* Continue cleanup without reporting a cancelled resource. */ }
    }
  }

  async speak(updateId, { onEnded = () => {}, onFailed = () => {} } = {}) {
    this.stop();
    const generation = this.generation;
    let audio = null;
    const current = () => this.generation === generation && this.audio === audio;
    const fail = message => {
      if (!current()) return;
      this.stop();
      this.onError(message);
      onFailed(message);
    };

    let result;
    try {
      result = await this.post('/api/voice/speak', { update_id: updateId });
    } catch {
      fail(SYNTHESIS_FAILED);
      return;
    }
    if (!current()) return;

    try {
      if (typeof result?.audio !== 'string' || !result.audio.trim()) {
        fail(PLAYBACK_FAILED);
        return;
      }
      const binary = atob(result.audio);
      const bytes = Uint8Array.from(binary, character => character.charCodeAt(0));
      this.audioUrl = this.createObjectURL(new Blob([bytes], { type: result.mime }));
      audio = this.createAudio(this.audioUrl);
      this.audio = audio;
      audio.onended = () => {
        if (!current()) return;
        this.stop();
        onEnded();
      };
      audio.onerror = () => fail(PLAYBACK_FAILED);
      await audio.play();
    } catch (error) {
      fail(error?.name === 'NotAllowedError' ? PLAYBACK_BLOCKED : PLAYBACK_FAILED);
      return;
    }
    if (current()) this.onStarted();
  }
}
