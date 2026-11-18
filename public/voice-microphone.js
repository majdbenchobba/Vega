// One microphone graph per owner-enabled voice session. No network access.
import { microphonePreference } from './microphone-preference.js';

export class VegaMicrophone {
  constructor({
    getUserMedia = constraints => navigator.mediaDevices.getUserMedia(constraints),
    createContext = () => new AudioContext(),
    createNode = context => new AudioWorkletNode(context, 'vega-pcm', {
      numberOfInputs: 1, numberOfOutputs: 1, outputChannelCount: [1],
    }),
    now = () => performance.now(),
    processingMode = 'conversation',
    deviceId,
    preference = microphonePreference,
    createSpeechDetector = null,
  } = {}) {
    this.getUserMedia = getUserMedia; this.createContext = createContext;
    this.createNode = createNode; this.now = now;
    this.playbackCheck = processingMode === 'playback-check';
    this.unprocessedCheck = processingMode === 'unprocessed-check';
    this.deviceId = typeof deviceId === 'string' ? deviceId : null;
    this.preference = preference;
    this.createSpeechDetector = createSpeechDetector;
    this.generation = 0; this.record = null; this.starting = null;
  }
  async start(onFrame, onFailure, onActivationNeeded = () => {}) {
    if (this.record?.ready) return true;
    if (this.starting) return this.starting.promise;
    const generation = ++this.generation;
    const record = { generation, ready: false, media: null, context: null, source: null, node: null, gain: null, detector: null };
    this.record = record;
    const owns = () => this.record === record && this.generation === generation;
    const failed = () => {
      if (!owns()) return;
      this.stop();
      onFailure(new Error('The microphone stopped. Resume listening when it is available.'));
    };
    const starting = { generation, promise: null };
    this.starting = starting;
    starting.promise = (async () => {
      try {
        // Read on acquisition, so a setup selection made after construction is
        // used by the next live graph too. Explicit manual selections win.
        const deviceId = this.deviceId ?? this.preference.read().deviceId;
        const media = await this.getUserMedia({
          audio: {
            echoCancellation: !(this.playbackCheck || this.unprocessedCheck),
            noiseSuppression: !(this.playbackCheck || this.unprocessedCheck),
            autoGainControl: !this.unprocessedCheck,
            ...(deviceId ? { deviceId: { exact: deviceId } } : {}),
          }, video: false,
        });
        if (!owns()) { media.getTracks().forEach(track => track.stop()); return false; }
        record.media = media;
        const tracks = media.getTracks();
        for (const track of tracks) track.onended = failed;
        if (!tracks.length || tracks.some(track => track.readyState === 'ended')) { failed(); return false; }
        if (this.createSpeechDetector) {
          record.detector = this.createSpeechDetector();
          const loaded = await record.detector.start(
            frame => { if (owns() && record.ready) onFrame(frame); },
            error => { if (owns()) { this.stop(); onFailure(error); } },
          );
          if (!loaded || !owns()) return false;
        }
        record.context = this.createContext();
        await record.context.audioWorklet.addModule('/assets/vegaPcmWorklet.js');
        if (!owns()) return false;
        record.source = record.context.createMediaStreamSource(media);
        record.node = this.createNode(record.context);
        record.gain = record.context.createGain(); record.gain.gain.value = 0;
        record.source.connect(record.node); record.node.connect(record.gain);
        record.gain.connect(record.context.destination);
        record.node.onprocessorerror = failed;
        record.node.port.onmessage = event => {
          if (!owns() || !record.ready) return;
          const data = event.data;
          if (!(data?.pcm instanceof ArrayBuffer) || !data.pcm.byteLength || data.pcm.byteLength > 6400
              || data.pcm.byteLength % 2 || !Number.isFinite(data.level)) return;
          const frame = {
            pcm: new Uint8Array(data.pcm), level: Math.min(1, Math.max(0, data.level)),
            timestampMs: this.now(),
          };
          if (record.detector) record.detector.push(frame);
          else onFrame(frame);
        };
        const resumed = record.context.resume();
        if (record.context.state === 'suspended') onActivationNeeded();
        await resumed;
        if (!owns()) return false;
        record.ready = true;
        return true;
      } catch (error) {
        if (!owns()) return false;
        this.stop();
        if (error?.name === 'OverconstrainedError' || error?.name === 'NotFoundError') {
          throw new Error('Selected microphone unavailable. Choose an input in Voice setup.');
        }
        throw error;
      } finally {
        if (this.starting === starting) this.starting = null;
      }
    })();
    return starting.promise;
  }
  resume() {
    const context = this.record?.context;
    if (context?.state === 'suspended') return context.resume();
  }
  inputInfo() {
    const track = this.record?.media?.getTracks().find(item => !item.kind || item.kind === 'audio');
    if (!track) return null;
    let settings = {};
    try { settings = track.getSettings?.() || {}; } catch { /* Some browsers cannot report applied settings. */ }
    // Transient UI feedback only. Never expose device/group identifiers.
    const info = { label: typeof track.label === 'string' ? track.label.slice(0, 160) : '' };
    for (const key of ['echoCancellation', 'noiseSuppression', 'autoGainControl']) {
      if (typeof settings[key] === 'boolean') info[key] = settings[key];
    }
    return info;
  }
  stop() {
    ++this.generation;
    const record = this.record;
    this.record = null; this.starting = null;
    if (!record) return;
    record.ready = false;
    const cleanup = [];
    if (record.detector) cleanup.push(() => record.detector.stop());
    if (record.node) {
      record.node.port.onmessage = null; record.node.onprocessorerror = null;
      cleanup.push(
        () => record.node.port.postMessage({ type: 'stop' }),
        () => record.node.disconnect(),
      );
    }
    if (record.source) cleanup.push(() => record.source.disconnect());
    if (record.gain) cleanup.push(() => record.gain.disconnect());
    for (const track of record.media?.getTracks() || []) {
      track.onended = null;
      cleanup.push(() => track.stop());
    }
    if (record.context) cleanup.push(() => { void Promise.resolve(record.context.close()).catch(() => {}); });
    for (const release of cleanup) {
      try { release(); } catch { /* Release the remaining microphone resources. */ }
    }
  }
}
