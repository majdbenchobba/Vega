import { createVegaLiveVoiceState, reduceVegaLiveVoice, isVegaLiveVoiceActive } from './policy/renderer/vegaLiveVoice.js';
import { createVoiceActivityState, reduceVoiceActivity } from './policy/renderer/voiceActivity.js';
import { VegaPcmSender } from './policy/renderer/vegaPcmSender.js';
import { VegaLocalBuffer } from './voice-local-buffer.js';
import { vegaPlaybackRouteKey, completedVegaVoiceReply, createVegaVoiceSendReceipt } from './policy/renderer/vegaVoiceReply.js';
import { VegaMicrophone } from './voice-microphone.js';
import { VegaSpeechDetector } from './speech-detector.js';
import { VegaSpeechInterruption } from './voice-interruption.js';
import { VegaVoicePlayback } from './voice-playback.js';
import { OwnerVoiceGate } from './owner-voice-gate.js';

const INTERRUPTIBLE_PHASES = new Set(['transcribing', 'sending', 'waiting', 'speaking']);

const bytes64 = bytes => {
  let binary = '';
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary);
};

export class VegaVoice {
  constructor({ post, onState, onError, onDraft, onSent, onPlaybackStarted = () => {},
    microphone = new VegaMicrophone({ createSpeechDetector: () => new VegaSpeechDetector() }),
    createPlayback = options => new VegaVoicePlayback(options), ownerGate = null,
    now = () => performance.now(), paused = false,
    inputAvailable = () => Boolean(globalThis.navigator?.mediaDevices?.getUserMedia && globalThis.AudioWorkletNode),
    queryMicrophonePermission = () => navigator.permissions.query({ name: 'microphone' }) }) {
    this.post = post; this.onState = onState; this.onError = onError; this.onDraft = onDraft; this.onSent = onSent;
    this.state = createVegaLiveVoiceState(); this.captures = new Map(); this.turns = new Map();
    this.preferences = null; this.microphone = microphone; this.now = now;
    this.microphoneSession = null; this.microphoneFrames = []; this.pendingInterruption = null;
    this.captureAdmission = Promise.resolve(); this.pendingCaptureCleanup = new Map();
    this.interruption = new VegaSpeechInterruption();
    this.ownerGate = ownerGate || new OwnerVoiceGate({ post });
    this.ownerCandidate = null; this.ownerCheck = null; this.speakerNotice = '';
    this.manualPlayback = false;
    this.playback = createPlayback({
      post, onError: message => this.onError(message, { source: 'playback' }),
      onStarted: () => { this.interruption.playbackStarted(this.now()); onPlaybackStarted(); },
    });
    this.config = null; this.review = false; this.transcript = null;
    this.pausedByOwner = paused; this.inputAvailable = inputAvailable;
    this.queryMicrophonePermission = queryMicrophonePermission;
    this.autoStartGeneration = 0; this.autoStartAttempted = false; this.autoStartPending = null;
    this.microphoneReady = false; this.activationNeeded = false;
    this.startupStatus = paused ? 'paused' : 'idle';
  }
  configure(preferences, speech, context) {
    const previousRevision = this.preferences?.revision;
    const previousRoute = vegaPlaybackRouteKey(this.config);
    this.preferences = preferences;
    this.microphoneDisabled = Boolean(speech.microphone_disabled);
    this.config = speech.route ? { ...speech.route, configured: speech.configured } : null;
    const ownerChanged = this.ownerGate.configure(speech.owner_voice);
    if (previousRevision !== undefined && (
      previousRevision !== preferences.revision || previousRoute !== vegaPlaybackRouteKey(this.config)
      || this.microphoneDisabled || !this.config?.configured || ownerChanged && !this.review
    )) {
      this.stop();
      this.autoStartAttempted = false;
    }
    this.context(context);
    this.onState(this.state);
  }
  context(context) {
    if (!context) return;
    if (context.revision > (this.state.context?.revision ?? -1)) {
      this.microphoneFrames = []; this.pendingInterruption = null; this.interruption.reset();
      this.ownerGate.reset(); this.ownerCandidate = null; this.ownerCheck = null;
    }
    this.dispatch({ type: 'context-changed', context: {
      revision: context.revision,
      target: context.view.project ? { kind: 'project', projectId: context.view.project }
        : { kind: 'app-view', viewId: context.view.kind },
    } });
  }
  current(effect) {
    return this.state.phase !== 'idle' && this.state.sessionId === effect.sessionId && this.state.operationId === effect.operationId;
  }
  dispatch(event) {
    const transition = reduceVegaLiveVoice(this.state, event);
    this.state = transition.state;
    if (this.state.phase === 'idle') {
      if (this.microphoneSession !== null) this.microphone.stop();
      this.microphoneSession = null;
      this.microphoneFrames = []; this.pendingInterruption = null; this.interruption.reset();
      this.ownerGate.reset(); this.ownerCandidate = null; this.ownerCheck = null;
      this.microphoneReady = false; this.activationNeeded = false;
    } else if (this.state.phase === 'listening') this.interruption.reset();
    this.onState(this.state);
    for (const effect of transition.effects) {
      void this.effect(effect).catch(error => {
        if (!this.current(effect)) return;
        this.startupStatus = error.name === 'NotAllowedError' ? 'blocked' : 'unavailable';
        this.onError(error.message);
        this.dispatch({ type: 'operation-failed', sessionId: effect.sessionId, operationId: effect.operationId,
          failure: { code: 'VEGA_VOICE_FAILED', message: error.message }, recovery: 'stop' });
      });
    }
  }
  start(review = false, { automatic = false } = {}) {
    if (automatic && this.pausedByOwner) return false;
    if (isVegaLiveVoiceActive(this.state)) { this.resumeAudioContext(); return false; }
    if (!review && !this.ownerGate.ready) {
      this.startupStatus = 'owner-setup'; this.onState(this.state); return false;
    }
    ++this.autoStartGeneration;
    this.autoStartAttempted = true;
    if (!automatic) this.pausedByOwner = false;
    if (this.microphoneDisabled) { this.onError('Microphone access is disabled in this isolated preview.'); return; }
    if (!this.config?.configured) { this.onError('Configure the saved voice connection before starting.'); return; }
    if (!this.inputAvailable()) {
      this.startupStatus = 'unsupported';
      this.onError('This browser does not provide the microphone and audio worklet needed for live voice.'); return;
    }
    this.microphoneReady = false; this.activationNeeded = false;
    if (this.startupStatus !== 'permission') this.startupStatus = 'starting';
    this.review = review;
    this.dispatch({ type: 'start', sessionId: crypto.randomUUID() });
    return true;
  }
  stop() {
    ++this.autoStartGeneration; this.autoStartPending = null;
    this.startupStatus = this.pausedByOwner ? 'paused' : 'idle';
    this.dispatch({ type: 'stop' }); this.stopPlayback();
  }
  pause() {
    this.pausedByOwner = true;
    this.stop();
  }
  changeMicrophone(deviceId) {
    // Invalidate pending admission/transcripts before switching input. Resume
    // remains an explicit owner action; a selection never clears a pause.
    this.stop();
    this.microphone.deviceId = deviceId;
    this.autoStartAttempted = true;
  }
  resumeAudioContext() {
    if (!isVegaLiveVoiceActive(this.state) || this.pausedByOwner) return;
    const session = this.state.sessionId;
    void Promise.resolve(this.microphone.resume?.()).catch(error => {
      if (this.state.sessionId === session && this.state.phase !== 'idle') this.onError(error.message);
    });
  }
  interrupt() {
    if (this.state.phase !== 'idle') this.dispatch({ type: 'barge-in', sessionId: this.state.sessionId, operationId: this.state.operationId });
  }
  async maybeAutoStart({ retry = false } = {}) {
    if (!this.preferences?.values.background_listening || !this.config?.configured || this.microphoneDisabled
        || this.pausedByOwner || isVegaLiveVoiceActive(this.state)) return false;
    if (!this.ownerGate.ready) {
      this.startupStatus = 'owner-setup'; this.onState(this.state); return false;
    }
    if (this.autoStartPending) return this.autoStartPending;
    if (this.autoStartAttempted && !retry) return false;
    if (!this.inputAvailable()) {
      this.startupStatus = 'unsupported'; this.onState(this.state); return false;
    }
    this.autoStartAttempted = true;
    const generation = ++this.autoStartGeneration;
    const revision = this.preferences.revision;
    const route = vegaPlaybackRouteKey(this.config);
    const eligible = () => generation === this.autoStartGeneration && !this.pausedByOwner
      && this.preferences?.revision === revision && this.preferences.values.background_listening
      && this.config?.configured && !this.microphoneDisabled
      && this.ownerGate.ready
      && vegaPlaybackRouteKey(this.config) === route && this.state.phase === 'idle';
    this.startupStatus = 'checking';
    const pending = (async () => {
      let permission = null;
      try { permission = await this.queryMicrophonePermission(); } catch { /* getUserMedia remains the permission authority. */ }
      if (!eligible()) return false;
      if (permission?.state === 'denied') {
        this.startupStatus = 'blocked'; this.onState(this.state); return false;
      }
      // With permission already granted this is silent. Otherwise the browser
      // presents its own permission prompt; Vega never grants it on the owner's behalf.
      this.startupStatus = permission?.state === 'granted' ? 'starting' : 'permission';
      return this.start(false, { automatic: true });
    })();
    this.autoStartPending = pending;
    this.onState(this.state);
    try { return await pending; }
    finally {
      if (this.autoStartPending === pending) this.autoStartPending = null;
      this.onState(this.state);
    }
  }
  async apiResult(path, payload) {
    try { return { ok: true, value: await this.post(path, payload) }; }
    catch (error) { return { ok: false, error: { code: 'VEGA_TRANSPORT', message: error.message } }; }
  }
  queueCaptureAdmission(operation) {
    const result = this.captureAdmission.then(operation);
    this.captureAdmission = result.catch(() => {});
    return result;
  }
  async cancelCaptureNow(payload) {
    this.pendingCaptureCleanup.set(payload.capture_id, payload);
    const result = await this.post('/api/voice/stream/cancel', payload);
    if (this.pendingCaptureCleanup.get(payload.capture_id) === payload) this.pendingCaptureCleanup.delete(payload.capture_id);
    return result;
  }
  cancelCapture(payload) {
    this.pendingCaptureCleanup.set(payload.capture_id, payload);
    return this.queueCaptureAdmission(() => {
      const pending = this.pendingCaptureCleanup.get(payload.capture_id);
      return pending ? this.cancelCaptureNow(pending) : {};
    });
  }
  prepareCapture(effect, record) {
    return this.queueCaptureAdmission(async () => {
      if (!this.current(effect) || record.cancelled) return null;
      // An unacknowledged cancellation cannot race a replacement preparation.
      for (const pending of [...this.pendingCaptureCleanup.values()]) await this.cancelCaptureNow(pending);
      if (!this.current(effect) || record.cancelled) return null;
      const prepared = await this.post('/api/voice/prepare', { mode: this.review ? 'review' : 'automatic' });
      if (!this.current(effect) || record.cancelled) {
        await this.cancelCaptureNow({ capture_id: prepared.captureId, context_revision: effect.context.revision });
        return null;
      }
      return prepared;
    });
  }
  stopCapture(record, discard) {
    record.closed = true;
    record.pendingFrames = [];
    if (discard) { record.cancelled = true; record.sender?.cancel(); }
  }
  microphoneFrame(frame) {
    if (!isVegaLiveVoiceActive(this.state) || this.microphoneSession !== this.state.sessionId) return;
    const owned = { ...frame, pcm: Uint8Array.from(frame.pcm) };
    this.microphoneFrames.push(owned);
    while (this.microphoneFrames.length > 60
        || this.microphoneFrames.length > 1 && owned.timestampMs - this.microphoneFrames[0].timestampMs > 5000) {
      this.microphoneFrames.shift();
    }
    if (this.state.phase === 'listening') {
      const record = this.captures.get(this.state.operationId);
      if (!record || record.closed) return;
      if (!record.ready) {
        record.pendingFrames.push(owned);
        // Preparation can wait for cancellation or a busy local host. Retain
        // bounded pre-roll while displaying Connecting, without a 3 s cliff.
        while (record.pendingFrames.length > 30) record.pendingFrames.shift();
      } else this.captureFrame(record, owned);
      if (!this.manualPlayback) return;
    }
    if (this.review || !INTERRUPTIBLE_PHASES.has(this.state.phase) && !this.manualPlayback) return;
    const onset = this.interruption.push(owned);
    if (onset !== null && !this.ownerCandidate) this.ownerCandidate = { onset, retryAt: 0 };
    const candidate = this.ownerCandidate;
    if (!candidate || this.ownerCheck || owned.timestampMs < candidate.retryAt
        || owned.timestampMs - candidate.onset < this.ownerGate.minimumEvidenceMs) return;
    const check = {
      sessionId: this.state.sessionId, operationId: this.state.operationId,
      contextRevision: this.state.context.revision, profileRevision: this.ownerGate.revision,
    };
    const frames = this.microphoneFrames.filter(item => item.timestampMs >= candidate.onset - 250);
    this.ownerCheck = check;
    void this.ownerGate.check(frames).then(result => {
      if (this.ownerCheck !== check || !this.current(check)
          || this.state.context.revision !== check.contextRevision || !this.ownerGate.ready
          || this.ownerGate.revision !== check.profileRevision) return;
      if (!result.owner) { candidate.retryAt = this.now() + 2000; return; }
      this.pendingInterruption = {
        sessionId: check.sessionId, contextRevision: check.contextRevision, onset: candidate.onset,
        frames: this.microphoneFrames.filter(item => item.timestampMs >= candidate.onset - 250),
      };
      this.ownerCandidate = null;
      if (this.manualPlayback && this.state.phase === 'listening') {
        this.stopPlayback();
        this.dispatch({ type: 'operation-failed', sessionId: this.state.sessionId, operationId: this.state.operationId,
          failure: { code: 'OWNER_INTERRUPTION', message: 'Confirmed owner speech.' }, recovery: 'listen' });
      } else this.interrupt();
    }).finally(() => { if (this.ownerCheck === check) this.ownerCheck = null; });
  }
  captureFrame(record, frame) {
    if (record.closed || !this.current(record.effect)) return;
    record.sender.frame(frame.pcm);
    const transition = reduceVoiceActivity(record.activity, {
      timestampMs: frame.timestampMs, level: Number.isFinite(frame.speechProbability)
        ? frame.speechProbability : Math.min(1, Math.max(0, frame.level * 12)),
    });
    record.activity = transition.state;
    const event = type => ({
      type, sessionId: record.effect.sessionId, operationId: record.effect.operationId,
    });
    for (const signal of transition.events) {
      if (signal.type === 'speech-start') {
        this.speakerNotice = '';
        this.onState(this.state);
        record.sender.start();
      }
      else if (signal.type === 'end-of-utterance') this.dispatch(event('silence-end'));
      else if (signal.type === 'no-speech-timeout') this.dispatch({
        ...event('operation-failed'), failure: { code: 'NO_SPEECH', message: 'Listening for speech.' }, recovery: 'listen',
      });
    }
  }
  async effect(effect) {
    const event = type => ({ type, sessionId: effect.sessionId, operationId: effect.operationId });
    if (effect.type === 'capture-start') {
      this.microphoneReady = false;
      this.onState(this.state);
      if (!this.review && !this.ownerGate.ready) {
        this.stop(); this.startupStatus = 'owner-setup'; this.onState(this.state); return;
      }
      const interrupted = this.pendingInterruption;
      this.pendingInterruption = null;
      const preserved = interrupted?.sessionId === effect.sessionId && interrupted.contextRevision === effect.context.revision
        ? interrupted : null;
      const record = {
        effect, cancelled: false, closed: false, ready: false, sender: null,
        pendingFrames: preserved ? [...preserved.frames] : [], activity: createVoiceActivityState(),
      };
      this.captures.set(effect.operationId, record);
      const prepared = await this.prepareCapture(effect, record);
      if (!prepared) return;
      if (!this.current(effect) || record.cancelled) {
        await this.cancelCapture({ capture_id: prepared.captureId, context_revision: effect.context.revision });
        return;
      }
      record.captureId = prepared.captureId;
      const contextRevision = effect.context.revision;
      const base = { capture_id: prepared.captureId, context_revision: contextRevision };
      const adapter = {
        beginVegaVoiceStream: () => this.apiResult('/api/voice/stream/begin', base),
        pushVegaVoiceStream: (_conversation, _capture, sequence, bytes) => this.apiResult('/api/voice/stream/push', { ...base, sequence, audio: bytes64(bytes) }),
        finishVegaVoiceStream: (_conversation, _capture, sequence) => this.apiResult('/api/voice/stream/finish', { ...base, sequence }),
        cancelVegaVoiceTranscription: () => this.cancelCapture(base),
      };
      const onFailure = failure => {
        if (!this.current(effect)) return;
        this.onError(failure.message);
        this.dispatch({ ...event('operation-failed'), failure, recovery: 'stop' });
      };
      record.sender = prepared.buffered && !this.review ? new VegaLocalBuffer({
        begin: () => this.apiResult('/api/voice/stream/begin', { ...base, local_upload: true }),
        finish: bytes => this.apiResult('/api/voice/finish-local', {
          ...base, sample_rate: 16000, audio: bytes64(bytes),
        }),
        cancel: () => this.cancelCapture(base), onFailure,
      }) : new VegaPcmSender(adapter, 'vega', prepared.captureId, onFailure);
      this.microphoneSession = effect.sessionId;
      const opened = await this.microphone.start(
        frame => this.microphoneFrame(frame),
        error => {
          if (this.microphoneSession !== effect.sessionId || this.state.phase === 'idle') return;
          this.onError(error.message); this.stop();
          this.startupStatus = 'unavailable'; this.onState(this.state);
        },
        () => {
          if (!this.current(effect)) return;
          this.activationNeeded = true; this.onState(this.state);
        },
      );
      if (!opened || !this.current(effect) || record.cancelled) { this.stopCapture(record, true); return; }
      this.microphoneReady = true; this.activationNeeded = false; this.startupStatus = 'ready';
      this.onState(this.state);
      record.ready = true;
      if (preserved) {
        record.activity = {
          ...record.activity, phase: 'speech',
          captureStartedAtMs: record.pendingFrames[0]?.timestampMs ?? preserved.onset,
          speechStartedAtMs: preserved.onset,
        };
        record.sender.start();
      }
      const pendingFrames = record.pendingFrames;
      record.pendingFrames = [];
      for (const frame of pendingFrames) this.captureFrame(record, frame);
    } else if (effect.type === 'capture-stop') {
      const record = this.captures.get(effect.operationId);
      if (record) this.stopCapture(record, effect.discard);
      if (effect.discard) this.captures.delete(effect.operationId);
    } else if (effect.type === 'transcribe') {
      const record = this.captures.get(effect.captureOperationId);
      if (!record?.sender) throw new Error('The voice recording ended before capture was ready.');
      const result = await record.sender.finish();
      this.captures.delete(effect.captureOperationId);
      if (!this.current(effect)) return;
      if (!result.ok) throw new Error(result.error.message);
      if (result.value.ignored) {
        this.transcript = null;
        this.speakerNotice = ({
          insufficient_speech: 'Too little speech to recognize your voice. Please repeat.',
          multiple_or_overlapping_speakers: 'Possible overlapping speech. Please try again.',
          short_greeting_only: 'I recognized the greeting profile, but could not verify this command. Please repeat.',
          speaker_mismatch: 'I could not match that voice. Please try again.',
          short_greeting_setup_needed: 'Short voice setup needs attention. Open Voice setup.',
          inconsistent_profile: 'Your saved short profile needs attention. Open Voice setup.',
          ambiguous_speech: 'I could not hear that clearly. Please try again.',
          busy: 'The voice check was busy. Please try again.',
        })[result.value.reason] || 'I could not confirm that speech. Please try again.';
        this.dispatch({ ...event('operation-failed'),
          failure: { code: 'VOICE_IGNORED', message: this.speakerNotice }, recovery: 'listen' });
        return;
      }
      this.speakerNotice = '';
      this.transcript = result.value;
      if (this.review) {
        this.onDraft(result.value.text, result.value.receipt);
        this.stop(); return;
      }
      this.dispatch({ ...event('transcription-complete'), text: result.value.text });
    } else if (effect.type === 'transcription-cancel') {
      for (const record of this.captures.values()) this.stopCapture(record, true);
      this.captures.clear();
    } else if (effect.type === 'send') {
      const origin = {
        conversationId: 'vega', draftText: effect.text,
        playbackRouteKey: vegaPlaybackRouteKey(this.config),
        authorizationGeneration: this.preferences.revision,
      };
      const result = await this.post('/api/chat', { text: effect.text, voice_receipt: this.transcript.receipt, request_id: crypto.randomUUID() });
      if (!this.current(effect)) { await this.post('/api/chat/cancel', { update_id: result.update_id }); return; }
      this.turns.set(effect.operationId, { updateId: result.update_id, receipt: createVegaVoiceSendReceipt(origin, 'vega', effect.text, result.update_id) });
      this.onSent(result.update_id);
      this.dispatch(event('send-accepted'));
    } else if (effect.type === 'response-cancel') {
      const turn = this.turns.get(effect.operationId);
      if (turn) await this.post('/api/chat/cancel', { update_id: turn.updateId });
      this.turns.delete(effect.operationId);
    } else if (effect.type === 'speak') {
      if (!this.preferences.values.auto_speak) { this.dispatch(event('speech-complete')); return; }
      await this.speak(this.replyUpdateId,
        () => { if (this.current(effect)) this.dispatch(event('speech-complete')); },
        message => {
          if (this.current(effect)) this.dispatch({
            ...event('operation-failed'), failure: { code: 'VEGA_PLAYBACK_FAILED', message }, recovery: 'listen',
          });
        },
      );
    } else if (effect.type === 'speech-stop') {
      this.stopPlayback();
    }
  }
  observe(history) {
    if (this.state.phase !== 'waiting') return;
    const turn = this.turns.get(this.state.operationId);
    const reply = turn && history.find(item => item.role === 'assistant' && item.update_id === turn.updateId);
    if (!reply) return;
    const valid = completedVegaVoiceReply(
      turn.receipt, { conversationId: 'vega', runId: turn.updateId, assistantMessage: { ...reply, conversation_id: 'vega' } },
      'vega', vegaPlaybackRouteKey(this.config), this.preferences.revision, true,
    );
    if (!valid) { this.stop(); return; }
    this.turns.delete(this.state.operationId);
    this.replyUpdateId = turn.updateId;
    this.dispatch({ type: 'reply-ready', sessionId: this.state.sessionId, operationId: this.state.operationId, text: reply.text });
  }
  stopPlayback() {
    this.manualPlayback = false;
    this.manualPlaybackToken = null;
    this.playback.stop();
    this.onState(this.state);
  }
  stopSpokenReply() {
    this.stopPlayback();
    if (this.state.phase === 'speaking') this.dispatch({
      type: 'speech-complete', sessionId: this.state.sessionId, operationId: this.state.operationId,
    });
  }
  async speak(updateId, onEnded, onFailed) {
    const owner = { sessionId: this.state.sessionId, operationId: this.state.operationId };
    const token = Symbol('playback');
    this.manualPlaybackToken = token;
    this.manualPlayback = !onEnded && !onFailed;
    this.onState(this.state);
    const ended = () => {
      if (this.manualPlaybackToken === token) {
        this.manualPlayback = false; this.manualPlaybackToken = null; this.onState(this.state);
      }
    };
    return this.playback.speak(updateId, {
      onEnded: () => {
        ended();
        if (onEnded) onEnded();
        else if (this.state.phase === 'speaking' && this.current(owner)) this.dispatch({ type: 'speech-complete', ...owner });
      },
      onFailed: message => {
        ended();
        if (onFailed) onFailed(message);
        else if (this.state.phase === 'speaking' && this.current(owner)) this.dispatch({
          type: 'operation-failed', ...owner, failure: { code: 'VEGA_PLAYBACK_FAILED', message }, recovery: 'listen',
        });
      },
    });
  }
}
