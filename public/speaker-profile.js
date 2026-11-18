// Standalone local enrollment UI. Construction/import never starts capture or
// transport. The caller supplies transport and owns automatic-voice policy.
import { VegaMicrophone } from './voice-microphone.js';
import { microphonePreference } from './microphone-preference.js';

const API = '/api/voice/owner';
const SAMPLE_RATE = 16000;
const MAX_SECONDS = 12;
const BYTES_PER_SECOND = SAMPLE_RATE * 2;
const MAX_AUDIO_BYTES = MAX_SECONDS * BYTES_PER_SECOND;
const CHECKS = [
  {
    key: 'owner', label: 'Your live voice',
    help: 'Speak alone using your saved language and microphone.',
    details: 'Use the language you enrolled with.',
  },
  {
    key: 'other', label: 'Another consenting speaker',
    help: 'Stay silent while a consenting person speaks.',
    details: 'Any language. Use a live person; you can defer this check if nobody is available.',
  },
  {
    key: 'media', label: 'Chess / media audio',
    help: 'Stay silent while spoken media plays nearby.',
    details: 'Play spoken audio through speakers.',
  },
  {
    key: 'overlap', label: 'Overlapping voices',
    help: 'Speak while another voice or spoken media plays.',
    details: 'Use spoken media or a consenting person.',
  },
  {
    key: 'playback', label: 'Vega voice playback',
    help: 'Stay silent while Vega’s sample plays.',
    details: 'Generate a sample below, then press Play during the check.',
  },
];
const PROMPTS = [
  "Today I'm taking a little time to set up my workspace. I can speak at a comfortable pace, pause between sentences, and keep my voice relaxed.",
  'When I plan a project, I start with one useful step. Clear notes help me remember what matters, while a short break gives me room to think.',
  "I use Vega to talk through ideas and keep track of things. My voice can sound different from day to day, so I'm speaking naturally instead of performing.",
];
const STATES = { pending: 'Not checked', passed: 'Passed', failed: 'Failed' };
const SHORT_PROBE_PHRASES = [
  'Hi Vega', 'Hey Vega, how are you?', 'Yes, that sounds good', 'Please check my projects',
];
const INCONCLUSIVE = new Set(['ambiguous_speech', 'insufficient_speech', 'poor_quality', 'incomplete_speech_coverage']);
const needsProfileReview = result => result?.passed === false && result.reason === 'speaker_mismatch'
  && result.diagnostics?.failure_stage === 'recording_to_profile';
const PROFILE_RECOVERY = 'This recording passed the speech checks, but did not match your saved voice profile. '
  + 'If you were the only speaker, re-enroll using this microphone. '
  + 'Your saved voice profile is unchanged; automatic voice stays disabled.';
const inconclusiveCheck = (_key, result) => result?.passed === false
  && result.decision === 'unknown' && INCONCLUSIVE.has(result.reason);
const greetingNeedsDiagnosis = state => state?.greeting?.setup_policy !== 'three-clear-references-v1'
  && !state?.greeting?.enrolled
  && state?.greeting?.last_check?.reason === 'inconsistent_greeting'
  && state.greeting.last_check.diagnostics?.candidate_recordings >= 4
  && state.greeting.last_check.diagnostics?.accepted_recordings < 2;
let sequence = 0;

const text = value => typeof value === 'string' ? value : '';
const errorText = error => text(error?.message) || text(error?.detail) || 'The local request could not be completed.';
const abortError = () => Object.assign(new Error('Recording cancelled.'), { name: 'AbortError' });
const revision = value => Number.isSafeInteger(value) && value >= 0;
const sessionId = value => typeof value === 'string' && value.length > 0 && value.length <= 200;

function readStatus(value) {
  const profile = value?.profile;
  if (!value || typeof value.runtime?.ready !== 'boolean' || !revision(profile?.revision)
      || ['enrolled', 'enabled', 'validated'].some(key => typeof profile[key] !== 'boolean')
      || typeof value.can_enable !== 'boolean' || !text(value.consent_text).trim()
      || (value.can_defer_other_check !== undefined && typeof value.can_defer_other_check !== 'boolean')
      || (profile.other_check_deferred !== undefined && typeof profile.other_check_deferred !== 'boolean')
      || !Number.isFinite(value.min_speech_seconds) || value.min_speech_seconds < 3
      || !Number.isFinite(value.enrollment_seconds) || value.enrollment_seconds < value.min_speech_seconds
      || value.min_speech_seconds > MAX_SECONDS || value.required_samples !== 3
      || CHECKS.some(({ key }) => !Object.hasOwn(STATES, profile.validation?.[key]))) {
    throw new Error('Voice-profile status is incomplete or unsupported. Refresh status before continuing.');
  }
  return {
    ...value, runtime: { ready: value.runtime.ready, error: text(value.runtime.error) },
    profile: { ...profile, validation: { ...profile.validation } },
    enrollment_seconds: Math.min(MAX_SECONDS, value.enrollment_seconds),
  };
}

function readEnrollment(value) {
  if (!sessionId(value?.id) || value.required !== 3
      || !Number.isInteger(value.collected) || value.collected < 0 || value.collected > 3) {
    throw new Error('The enrollment response is incomplete. No further clip will be recorded.');
  }
  return { id: value.id, collected: value.collected, required: value.required };
}

function base64PCM(bytes) {
  let binary = '';
  for (let offset = 0; offset < bytes.length; offset += 8192) {
    binary += String.fromCharCode(...bytes.subarray(offset, offset + 8192));
  }
  return btoa(binary);
}

function inputDescription(input, playbackCheck = false, unprocessed = false) {
  if (!input) return '';
  const microphone = text(input.label).trim() || 'Browser-selected microphone';
  if (!playbackCheck) return `Microphone: ${microphone}`;
  const filtering = [input.echoCancellation, input.noiseSuppression, ...(unprocessed ? [input.autoGainControl] : [])];
  return `Microphone: ${microphone}. ${filtering.some(value => value === true)
    ? 'The browser still reports audio filtering on; it may alter the recording.'
    : filtering.every(value => value === false)
      ? `Browser echo cancellation and noise suppression${unprocessed ? ', plus automatic gain,' : ''} are off. Device effects may still apply.`
      : 'The browser did not confirm that playback filtering is off.'}`;
}

function comparisonText(result) {
  if (!result || typeof result.passed !== 'boolean') return 'No saved result for this input mode.';
  const labels = {
    matched: 'Matched the saved profile.',
    speaker_mismatch: 'Did not match the saved profile.',
    ambiguous_speech: 'Detector uncertain; voice matching did not run.',
    insufficient_speech: 'Too little speech detected; voice matching did not run.',
    multiple_or_overlapping_speakers: 'Possible mixed speech; voice matching did not run.',
    inconsistent_speakers: 'Sections did not agree with the complete recording.',
    poor_quality: 'Input quality was insufficient.',
  };
  const parts = [labels[result.reason] || (result.passed ? 'Passed.' : 'Check did not pass.')];
  const diagnostic = result.diagnostics || {};
  if (diagnostic.failure_stage === 'window_to_profile') {
    parts[0] = 'The complete recording matched; a shorter section did not.';
  }
  const measured = value => typeof value === 'number' && Number.isFinite(value);
  if (measured(diagnostic.speech_seconds)) {
    parts.push(`${diagnostic.speech_seconds.toFixed(2)}s speech${measured(diagnostic.uncertain_seconds)
      ? ` · ${diagnostic.uncertain_seconds.toFixed(2)}s uncertain` : ''}.`);
  }
  const matching = diagnostic.matching;
  if (measured(matching?.reference_similarity_min) && measured(matching?.window_similarity_min)
      && measured(diagnostic.threshold)) {
    parts.push(`Similarity: complete recording ${matching.reference_similarity_min.toFixed(3)} · lowest section ${
      matching.window_similarity_min.toFixed(3)} · required ${diagnostic.threshold.toFixed(3)}. These are scores, not percentages.`);
  } else if (measured(diagnostic.similarity) && measured(diagnostic.threshold)) {
    parts.push(`Lowest similarity ${diagnostic.similarity.toFixed(3)} · required ${diagnostic.threshold.toFixed(3)} (not a percentage).`);
  }
  return parts.join('\n');
}

// The existing worklet produces mono s16le at 16 kHz. Frames are transient;
// both successful and cancelled paths stop the graph and release copies.
function recordMicrophone({ seconds, caseKey, signal, onProgress, deviceId, unprocessed = false, countInSeconds = 0 }) {
  return new Promise((resolve, reject) => {
    if (signal.aborted) { reject(abortError()); return; }
    const duration = Math.min(MAX_SECONDS, Math.max(1, seconds));
    const limit = Math.floor(duration * BYTES_PER_SECOND / 2) * 2;
    const microphone = new VegaMicrophone({
      processingMode: unprocessed ? 'unprocessed-check'
        : ['media', 'overlap', 'playback'].includes(caseKey) ? 'playback-check' : 'conversation',
      deviceId,
    });
    const countIn = countInSeconds === 1 ? 1 : 0;
    let preparingBytes = countIn * BYTES_PER_SECOND;
    let frames = [], size = 0, settled = false, activationPending = false, timer;
    const progress = level => onProgress({
      phase: preparingBytes ? 'count-in' : 'recording',
      countInSeconds: Math.ceil(preparingBytes / BYTES_PER_SECOND),
      elapsedSeconds: size / BYTES_PER_SECOND, level, input: microphone.inputInfo(),
    });
    const finish = error => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      signal.removeEventListener('abort', abort);
      microphone.stop();
      let result;
      if (!error && size) {
        result = new Uint8Array(size);
        let offset = 0;
        for (const frame of frames) { result.set(frame, offset); offset += frame.length; }
      }
      for (const frame of frames) frame.fill(0);
      frames = [];
      if (error || !result) reject(error || new Error('No audio was captured. Check the microphone before recording again.'));
      else resolve(result);
    };
    const abort = () => finish(abortError());
    signal.addEventListener('abort', abort, { once: true });
    // Bound permission/worklet setup separately; late permission is closed by
    // VegaMicrophone.stop(), never promoted into a new recording.
    timer = setTimeout(() => finish(new Error(activationPending
      ? 'The browser needs microphone audio activation. Click Record again to retry this clip.'
      : 'Microphone setup timed out. Click Record again when the microphone is ready.')), 12000);
    void microphone.start(({ pcm, level }) => {
      if (settled || signal.aborted || !(pcm instanceof Uint8Array) || pcm.length % 2) return;
      // Warm the actual input before the short speaking cue. These frames are
      // discarded, not retained as part of the greeting or sent for analysis.
      const skip = Math.min(preparingBytes, pcm.length);
      preparingBytes -= skip;
      const remaining = Math.min(pcm.length - skip, limit - size);
      if (remaining > 0) { frames.push(pcm.slice(skip, skip + remaining)); size += remaining; }
      progress(level);
      if (size >= limit) finish();
    // resume() can still report "suspended" until its promise settles normally.
    // Keep setup bounded, but do not close the graph just for that pending state.
    }, finish, () => { activationPending = true; })
      .then(started => {
        if (settled) return;
        if (!started || signal.aborted) { finish(abortError()); return; }
        clearTimeout(timer);
        progress();
        timer = setTimeout(() => finish(countIn && size < limit
          ? new Error('The microphone did not capture the full greeting. Your accepted clips are kept; retry this clip.')
          : undefined), (duration + countIn + (countIn ? 1 : 0)) * 1000);
      })
      .catch(finish);
  });
}

/**
 * get(path)/post(path, body) return decoded JSON and must not retry mutations.
 * Every POST, including playback-sample, must include its resulting status.
 * Recording hooks are awaited and paired once, even on cancellation/errors.
 * before({caseLabel, signal}) may return a token; after receives that token,
 * caseLabel, signal and cancelled. Only the parent decides whether to resume.
 * recordAudio receives {seconds, caseKey, caseLabel, signal, onProgress}; progress is
 * elapsed seconds (or {elapsedSeconds}). Its Uint8Array is cleared after use.
 * load() resolves to current status or null; cancelRecording()/dispose()
 * return promises for capture handoff and single-attempt session cleanup.
 */
export class SpeakerProfilePanel {
  constructor(container, { get, post, onBeforeRecording, onAfterRecording, recordAudio = recordMicrophone,
    enumerateDevices = () => navigator.mediaDevices.enumerateDevices(),
    preference = microphonePreference, onMicrophoneChanged = () => {} } = {}) {
    if (!container?.ownerDocument || typeof container.append !== 'function') {
      throw new TypeError('SpeakerProfilePanel needs a container element.');
    }
    if ([get, post, onBeforeRecording, onAfterRecording, recordAudio].some(fn => typeof fn !== 'function')) {
      throw new TypeError('SpeakerProfilePanel needs get, post, recording hooks and a recordAudio function.');
    }
    this.container = container;
    this.document = container.ownerDocument;
    this.window = this.document.defaultView || globalThis;
    this.get = get; this.post = post;
    this.onBeforeRecording = onBeforeRecording; this.onAfterRecording = onAfterRecording;
    this.recordAudio = recordAudio;
    this.enumerateDevices = enumerateDevices;
    this.microphonePreference = preference; this.onMicrophoneChanged = onMicrophoneChanged;
    this.prefix = `vega-speaker-${++sequence}`;
    this.epoch = 0; this.loadSequence = 0; this.keySequence = 0;
    this.status = null; this.fresh = false; this.disposed = false;
    this.loading = null; this.busy = null; this.enrollment = null;
    this.enrollmentMode = 'enroll';
    this.cleanup = new Map(); this.cleanupPending = 0; this.cancelPromise = null;
    this.unresolvedStart = false; this.errors = []; this.notice = '';
    this.diagnostic = null;
    this.diagnosticRequest = null;
    this.devicesLoading = false;
    this.devices = [];
    this.results = new Map(); this.deleteReview = null; this.sampleReady = false;
    this.sampleUrl = null;
    this.playbackVersion = 0; this.playPending = false; this.playing = false;
    this.listeners = [];
    this._build();
    this._render();
  }

  _node(tag, content, className) {
    const node = this.document.createElement(tag);
    if (content !== undefined) node.textContent = content;
    if (className) node.className = className;
    return node;
  }
  _listen(node, event, handler) {
    node.addEventListener(event, handler);
    this.listeners.push(() => node.removeEventListener(event, handler));
  }
  _button(label, action, handler, primary = false) {
    const node = this._node('button', label, `sp-button${primary ? ' sp-primary' : ''}`);
    node.type = 'button'; node.dataset.action = action;
    this._listen(node, 'click', event => {
      event.preventDefault();
      if (!this.disposed && !node.disabled) return handler();
    });
    return node;
  }
  _check(label, name) {
    const wrapper = this._node('label', undefined, 'sp-consent');
    const input = this._node('input');
    input.type = 'checkbox'; input.name = name; input.id = `${this.prefix}-${name}`;
    wrapper.htmlFor = input.id;
    const labelNode = this._node('span', label);
    wrapper.append(input, labelNode);
    this._listen(input, 'change', () => this._render());
    return { wrapper, input, labelNode };
  }
  _section(title, name) {
    const section = this._node('section', undefined, 'sp-card');
    const heading = this._node('h3', title);
    heading.id = `${this.prefix}-${name}`; heading.tabIndex = -1;
    section.setAttribute('aria-labelledby', heading.id);
    section.append(heading);
    return { section, heading };
  }
  _live(role = 'status') {
    const node = this._node('p', '', role === 'alert' ? 'sp-error' : 'sp-status');
    node.setAttribute('role', role);
    node.setAttribute('aria-live', role === 'alert' ? 'assertive' : 'polite');
    node.setAttribute('aria-atomic', 'true');
    return node;
  }

  _build() {
    this.element = this._node('section', undefined, 'speaker-profile-panel');
    this.heading = this._node('h2', 'Owner voice');
    this.heading.id = `${this.prefix}-title`; this.heading.tabIndex = -1;
    this.element.setAttribute('aria-labelledby', this.heading.id);
    const header = this._node('header', undefined, 'sp-header');
    const intro = this._node('div');
    intro.append(this.heading);
    this.refreshButton = this._button('Refresh', 'refresh', () => this.load());
    header.append(intro, this.refreshButton);
    this.body = this._node('div', undefined, 'sp-body');
    this.summary = this._node('div', undefined, 'sp-summary');
    this.runtimeLabel = this._node('strong', 'Status not loaded', 'sp-badge');
    this.profileLabel = this._node('p', 'Load status to see your local profile.');
    this.sampleCount = this._node('p', '', 'sp-hint');
    this.runtimeError = this._live('alert');
    this.summary.append(this.runtimeLabel, this.profileLabel, this.sampleCount, this.runtimeError);
    this.message = this._live(); this.errorNode = this._live('alert');
    this.dismissButton = this._button('Dismiss message', 'dismiss-error', () => { this.errors = []; this._render(); });
    this.body.append(this.summary, this.message, this.errorNode, this.dismissButton);
    this.diagnosticRegion = this._node('div', undefined, 'sp-notice');
    this.diagnosticText = this._node('p');
    this.diagnosticClear = this._button('Clear local diagnostic', 'diagnostic-clear', async () => {
      if (!this.diagnostic?.id || this.busy) return;
      try {
        const result = await this.post(API + '/diagnostic/clear', { id: this.diagnostic.id });
        this._applyDiagnostic(result.diagnostic);
        this._applyStatus(result.status);
      } catch (error) { this._error(errorText(error)); }
      this._render();
    });
    this.diagnosticRegion.append(this.diagnosticText, this.diagnosticClear);
    this.body.append(this.diagnosticRegion);

    const { section: greetings } = this._section('Short voice samples', 'greetings');
    const greetingConsent = this._check('Allow local greeting recordings and encrypted voice templates. Discard raw audio after analysis.', 'greeting-consent');
    this.greetingConsent = greetingConsent.input;
    this.greetingConsentLabel = greetingConsent.labelNode;
    this.greetingStatus = this._node('p', '', 'sp-hint');
    this.greetingStart = this._button('Set up short greetings', 'greeting-start', () => this._startEnrollment('greeting'));
    this.greetingPromote = this._button('Use saved samples', 'greeting-use-for-voice', () => this._useShortSamples());
    greetings.append(this.greetingStatus, greetingConsent.wrapper, this.greetingStart, this.greetingPromote);
    this.greetingProbeDetails = this._node('details', undefined, 'sp-help');
    this.greetingProbeSummary = this._node('summary', 'Diagnose short voice');
    this.greetingProbeDetails.append(this.greetingProbeSummary);
    this.greetingProbeDetails.append(this._node('p',
      'Say one short phrase at each cue: four phrases in 12 seconds.'));
    const probeConsent = this._check(
      'Save one encrypted clip on this PC only. Delete after diagnosis or 30 minutes while Vega runs.',
      'greeting-probe-consent');
    this.greetingProbeConsent = probeConsent.input;
    this.greetingProbeButton = this._button('Record diagnostic · 12s', 'greeting-probe', () => this._greetingProbe());
    this.greetingProbeDetails.append(probeConsent.wrapper, this.greetingProbeButton);
    greetings.append(this.greetingProbeDetails);
    this.body.append(greetings);

    const { section: enrollment, heading: enrollmentHeading } = this._section('Voice recordings', 'enroll');
    this.enrollmentSection = enrollment;
    this.enrollmentHeading = enrollmentHeading;
    const enrollmentOverview = this._node('div', undefined, 'sp-enrollment-overview');
    this.enrollmentHelp = this._node('p');
    enrollmentOverview.append(this.enrollmentHelp);
    this.reenrollNote = this._node('p',
      'Your saved voice changes only when you save a replacement.', 'sp-hint');
    this.consentText = this._node('p', 'Load the local consent text before starting.', 'sp-consent-text');
    this.consentText.id = `${this.prefix}-consent-text`;
    const enrollConsent = this._check('I consent to local voice matching and encrypted templates. Raw recordings are discarded.', 'enrollment-consent');
    this.enrollConsent = enrollConsent.input;
    this.enrollConsent.setAttribute('aria-describedby', this.consentText.id);
    this.startButton = this._button('Start enrollment', 'enroll-start', () => this._startEnrollment(), true);
    enrollmentOverview.append(this.reenrollNote);
    const enrollmentConsent = this._node('div', undefined, 'sp-enrollment-consent');
    const consentDetails = this._node('details', undefined, 'sp-help');
    consentDetails.append(this._node('summary', 'Recording details'), this.consentText);
    enrollmentConsent.append(consentDetails, enrollConsent.wrapper, this.startButton);
    const enrollmentSetup = this._node('div', undefined, 'sp-enrollment-setup');
    enrollmentSetup.append(enrollmentOverview, enrollmentConsent);
    this.replaceDetails = this._node('details', undefined, 'sp-replace');
    this.replaceSummary = this._node('summary', 'Replace saved voice…');
    this.replaceDetails.append(this.replaceSummary, enrollmentSetup);
    enrollment.append(this.replaceDetails);
    this.activeHelp = this._node('p');
    enrollment.append(this.activeHelp);
    this.clips = PROMPTS.map((prompt, index) => {
      const item = this._node('li', undefined, 'sp-clip');
      const title = this._node('h4', `Clip ${index + 1}`);
      const state = this._node('span', 'Not recorded', 'sp-badge');
      const words = this._node('blockquote', prompt, 'sp-reading');
      words.id = `${this.prefix}-clip-${index + 1}`;
      const button = this._button(`Record clip ${index + 1}`, `record-clip-${index + 1}`, () => this._recordEnrollment(index));
      const feedback = this._live();
      feedback.id = `${this.prefix}-clip-${index + 1}-feedback`;
      button.setAttribute('aria-describedby', `${words.id} ${feedback.id}`);
      item.append(title, state, words, button, feedback);
      return { item, state, words, button, feedback, outcome: null };
    });
    const clipList = this.clipList = this._node('ol', undefined, 'sp-clips');
    clipList.setAttribute('aria-label', 'Three enrollment clips');
    clipList.append(...this.clips.map(clip => clip.item));
    this.progressRegion = this._node('div', undefined, 'sp-progress');
    this.progressRegion.tabIndex = -1;
    this.progressRegion.setAttribute('role', 'group');
    this.progressRegion.setAttribute('aria-label', 'Recording progress and cancellation');
    this.progressText = this._live();
    this.progressText.id = `${this.prefix}-progress`;
    this.progress = this._node('progress'); this.progress.max = MAX_SECONDS; this.progress.value = 0;
    this.progress.setAttribute('aria-labelledby', this.progressText.id);
    this.inputText = this._node('p', '', 'sp-hint');
    this.inputMeter = this._node('meter');
    this.inputMeter.min = 0; this.inputMeter.max = 1; this.inputMeter.value = 0;
    this.inputMeter.setAttribute('aria-label', 'Microphone input level');
    this.inputLevelText = this._node('p', '', 'sp-hint');
    this.progressRegion.append(this.progressText, this.progress, this.inputText, this.inputMeter, this.inputLevelText);
    this.cancelButton = this._button('Cancel setup', 'cancel-recording', () => this.cancelRecording());
    this.commitButton = this._button('Save voice profile', 'enroll-commit', () => this._commit(), true);
    this.greetingDiagnose = this._button('End setup & diagnose', 'greeting-diagnose',
      () => this._diagnoseGreeting(), true);
    const enrollmentActions = this._node('div', undefined, 'sp-actions');
    enrollmentActions.append(this.commitButton, this.greetingDiagnose, this.cancelButton);
    this.progressHome = enrollment;
    this.cancelHome = enrollmentActions;
    enrollment.append(clipList, this.progressRegion, enrollmentActions);
    this.body.append(enrollment);

    const { section: checks, heading: checksHeading } = this._section('Voice checks', 'checks');
    this.checksHeading = checksHeading;
    const testConsent = this._check('I consent to these local microphone checks; anyone else being recorded has agreed.', 'test-consent');
    this.testConsent = testConsent.input;
    this.testConsentHint = this._node('p', 'Tick consent to record.', 'sp-hint');
    checks.append(testConsent.wrapper);
    checks.append(this.testConsentHint);
    const microphoneOptions = this._node('details', undefined, 'sp-microphone-options');
    microphoneOptions.append(this._node('summary', 'Microphone options'));
    const deviceLabel = this._node('label', 'Microphone');
    this.deviceSelect = this._node('select', undefined, 'sp-device-select');
    this.deviceSelect.name = 'microphone-device'; this.deviceSelect.id = `${this.prefix}-microphone-device`;
    const savedInput = this.microphonePreference.read();
    deviceLabel.htmlFor = this.deviceSelect.id;
    const defaultDevice = this._node('option', 'Browser default'); defaultDevice.value = '';
    this.deviceSelect.append(defaultDevice);
    if (savedInput.deviceId) {
      const savedDevice = this._node('option', savedInput.label || 'Saved microphone');
      savedDevice.value = savedInput.deviceId; this.deviceSelect.append(savedDevice);
    }
    this.deviceSelect.value = savedInput.deviceId;
    this.deviceRefresh = this._button('Find microphones', 'refresh-microphones', () => this._loadDevices());
    this.deviceHint = this._node('p', 'Used for setup and live listening.', 'sp-hint');
    this._listen(this.deviceSelect, 'change', () => {
      const deviceId = this.deviceSelect.value;
      const label = this.devices?.find(item => item.deviceId === deviceId)?.label || '';
      const saved = this.microphonePreference.write({ deviceId, label });
      this.onMicrophoneChanged(deviceId);
      this.deviceHint.textContent = saved
        ? 'Microphone saved. Resume listening when ready.'
        : 'Selected for this tab. Browser settings prevent saving it.';
    });
    microphoneOptions.append(deviceLabel, this.deviceSelect, this.deviceRefresh, this.deviceHint);
    checks.append(microphoneOptions);
    this.diagnosticSetup = this._node('details', undefined, 'sp-diagnostic-setup');
    this.diagnosticSetup.append(this._node('summary', 'Diagnose a failed owner check'));
    this.diagnosticSetup.append(this._node('p',
      'Keep one failed check encrypted on this PC only. Delete after diagnosis or 30 minutes while Vega runs.'));
    const diagnosticConsent = this._check(
      'I agree to keep one failed owner-check clip locally for this diagnosis.', 'owner-diagnostic-consent');
    this.diagnosticConsent = diagnosticConsent.input;
    this.diagnosticArm = this._button('Prepare one local diagnostic', 'diagnostic-owner-arm', async () => {
      if (!this._ready() || !this.diagnosticConsent.checked || !this.status.profile.enrolled) return;
      const request = this.diagnosticRequest ||= this._sampleKey();
      return this._run('diagnostic-arm', async () => {
        const result = await this.post(API + '/diagnostic/arm', {
          purpose: 'owner_check', consent: true, consent_version: 'one-failed-owner-check-v1', request_id: request,
        });
        this._applyDiagnostic(result.diagnostic);
        this._applyStatus(result.status);
        this.diagnosticConsent.checked = false;
        if (result.diagnostic?.state === 'armed' && result.diagnostic?.purpose === 'owner_check') {
          this.notice = 'Ready. Record your voice check once.';
          this.diagnosticRequest = null;
        } else this.notice = 'This diagnostic consent is no longer armed. Refresh its status before preparing another.';
      });
    });
    this.diagnosticSetup.append(diagnosticConsent.wrapper, this.diagnosticArm);
    checks.append(this.diagnosticSetup);
    const cases = this._node('div', undefined, 'sp-cases');
    this.caseControls = new Map();
    for (const item of CHECKS) {
      const card = this._node('section', undefined, 'sp-case');
      const title = this._node('h4', item.label);
      title.id = `${this.prefix}-check-${item.key}`;
      card.setAttribute('aria-labelledby', title.id); card.dataset.case = item.key;
      const badge = this._node('span', 'Not checked', 'sp-badge');
      const help = this._node('p', item.help);
      help.id = `${title.id}-help`;
      const button = this._button('Record check',
        `record-test-${item.key}`, () => this._test(item.key));
      button.setAttribute('aria-label', `Record ${item.label.toLowerCase()} check`);
      button.setAttribute('aria-describedby', help.id);
      const result = this._live();
      const input = this._node('p', '', 'sp-hint');
      input.hidden = true;
      const details = this._node('details', undefined, 'sp-help');
      const fullResult = this._node('p');
      details.append(this._node('summary', 'Details'), this._node('p', item.details), fullResult, input);
      card.append(title, badge, help, button, result, details);
      this.caseControls.set(item.key, { card, badge, button, result, input, fullResult });
      if (item.key === 'owner') {
        this.reviewEnrollmentButton = this._button('Update voice profile…', 'review-reenrollment', () => {
          if (!this._idle() || !this._ready() || this.deleteReview || !this.status?.profile.enrolled || this.enrollment) return;
          this.replaceDetails.open = true;
          this.replaceSummary.focus();
        });
        card.append(this.reviewEnrollmentButton);
        this.ownerComparisonDetails = this._node('details', undefined, 'sp-comparison-results');
        this.ownerComparisonDetails.append(this._node('summary', 'Compare recent checks'));
        this.ownerComparisonRows = new Map();
        for (const [mode, label] of [['standard', 'Normal input'], ['unprocessed', 'Without browser filters']]) {
          const row = this._node('div', undefined, 'sp-comparison-row');
          const heading = this._node('strong', label);
          const description = this._node('p');
          const recorded = this._node('small');
          row.append(heading, recorded, description);
          this.ownerComparisonDetails.append(row);
          this.ownerComparisonRows.set(mode, { description, recorded });
        }
        this.ownerComparisonDetails.append(this._node('small', 'The latest check counts.'));
        card.append(this.ownerComparisonDetails);
        const comparison = this._node('details', undefined, 'sp-input-comparison');
        comparison.append(this._node('summary', 'Try without browser filters'));
        comparison.append(this._node('p',
          'Test with browser audio filters off. Pause videos and music.'));
        this.unprocessedButton = this._button('Record without browser filters', 'record-test-owner-unprocessed',
          () => this._test('owner', { unprocessed: true }));
        comparison.append(this.unprocessedButton);
        card.append(comparison);
      }
      cases.append(card);
    }
    const playback = this._node('div', undefined, 'sp-playback');
    playback.append(this._node('h4', 'Vega playback sample'));
    const cloudConsent = this._check('I agree to the existing AWS speech charges for generating this sample.', 'cloud-speech-consent');
    this.cloudConsent = cloudConsent.input;
    this.generateButton = this._button('Generate sample', 'generate-sample', () => this._generateSample());
    this.playButton = this._button('Play sample', 'play-sample', () => this._playSample());
    this.stopButton = this._button('Stop sample', 'stop-sample', () => this._stopPlayback());
    this.audio = this._node('audio');
    this.audio.preload = 'none'; this.audio.autoplay = false;
    this.audio.setAttribute('aria-label', 'Optional Vega voice sample');
    this.audio.hidden = true;
    this._listen(this.audio, 'ended', () => { this.playing = false; this._render(); });
    this._listen(this.audio, 'error', () => {
      if (!this.sampleReady || this.disposed) return;
      this._error('The sample could not be played. No check result was inferred.');
      this._stopPlayback();
    });
    const playbackActions = this._node('div', undefined, 'sp-actions');
    playbackActions.append(this.generateButton, this.playButton, this.stopButton);
    playback.append(cloudConsent.wrapper, playbackActions, this.audio);
    checks.append(cases, playback);
    this.body.append(checks);

    const { section: management } = this._section('Listening', 'manage');
    this.enableHelp = this._node('p', 'Complete enrollment and all five real-owner checks before enabling automatic voice.');
    const enableConsent = this._check('I consent to use this validated local profile for automatic voice.', 'enable-consent');
    this.enableConsent = enableConsent.input;
    this.enableConsentLabel = enableConsent.labelNode;
    this.enableButton = this._button('Enable owner voice', 'enable', () => this._setting('enable'), true);
    this.disableButton = this._button('Disable owner voice', 'disable', () => this._setting('disable'));
    this.deleteButton = this._button('Delete voice profile…', 'delete', () => this._openDelete());
    this.deleteButton.className += ' sp-danger';
    const managementActions = this._node('div', undefined, 'sp-actions');
    managementActions.append(this.enableButton, this.disableButton, this.deleteButton);
    management.append(this.enableHelp, enableConsent.wrapper, managementActions);
    this.body.append(management);

    this.dialog = this._node('dialog', undefined, 'sp-delete-dialog');
    this.dialog.hidden = true;
    this.dialog.setAttribute('role', 'alertdialog'); this.dialog.setAttribute('aria-modal', 'true');
    const dialogTitle = this._node('h3', 'Delete the local voice profile?');
    dialogTitle.id = `${this.prefix}-delete-title`;
    const dialogText = this._node('p', 'Deletes your saved voice and unfinished setup. Enrollment and checks will be needed again.');
    dialogText.id = `${this.prefix}-delete-description`;
    this.dialog.setAttribute('aria-labelledby', dialogTitle.id);
    this.dialog.setAttribute('aria-describedby', dialogText.id);
    this.dialogStatus = this._live();
    this.keepButton = this._button('Keep profile', 'delete-cancel', () => this._closeDelete());
    this.confirmDeleteButton = this._button('Delete voice profile', 'delete-confirm', () => this._delete());
    this.confirmDeleteButton.className += ' sp-danger';
    const dialogActions = this._node('div', undefined, 'sp-actions');
    dialogActions.append(this.keepButton, this.confirmDeleteButton);
    this.dialog.append(dialogTitle, dialogText, this.dialogStatus, dialogActions);
    this._listen(this.dialog, 'cancel', event => { event.preventDefault(); this._closeDelete(); });
    this._listen(this.dialog, 'keydown', event => {
      if (!this.deleteReview) return;
      if (event.key === 'Escape') { event.preventDefault(); this._closeDelete(); }
      if (event.key === 'Tab') {
        const buttons = [this.keepButton, this.confirmDeleteButton].filter(button => !button.disabled);
        const first = buttons[0], last = buttons.at(-1);
        if (event.shiftKey && this.document.activeElement === first) { event.preventDefault(); last.focus(); }
        else if (!event.shiftKey && this.document.activeElement === last) { event.preventDefault(); first.focus(); }
      }
    });
    this.element.append(header, this.body, this.dialog);
    this.container.append(this.element);
  }

  _idle() { return !this.busy && !this.cleanupPending && !this.loading; }
  _ready() {
    return this.fresh && this.status?.runtime.ready && !this.status.runtime.error && !this.unresolvedStart;
  }
  _canEnable() {
    const state = this.status;
    return this._canDeferOther() || Boolean(this._ready() && state.can_enable && state.profile.enrolled
      && state.profile.validated && !state.profile.enabled
      && CHECKS.every(({ key }) => state.profile.validation[key] === 'passed'));
  }
  _canDeferOther() {
    const state = this.status;
    return Boolean(this._ready() && state.can_defer_other_check === true && state.profile.enrolled
      && !state.profile.enabled && state.profile.validation.other === 'pending'
      && CHECKS.every(({ key }) => key === 'other' || state.profile.validation[key] === 'passed'));
  }
  _error(message) {
    if (!this.disposed && !this.errors.includes(message)) this.errors.push(message);
    this._render();
  }
  _clipFeedback(op, message, failed = false) {
    const clip = this.clips[op.clipIndex];
    if (!clip || this.disposed) return;
    clip.feedback.textContent = message;
    clip.feedback.className = failed ? 'sp-error' : 'sp-status';
    clip.outcome = failed ? 'failed' : 'passed';
  }
  _applyStatus(value) {
    const next = readStatus(value);
    if (next.diagnostic) this.diagnostic = next.diagnostic;
    if (this.status && next.profile.revision < this.status.profile.revision) {
      throw new Error('An older profile status arrived. Refresh before continuing.');
    }
    if (this.status?.profile.revision !== next.profile.revision
        || this.status?.can_defer_other_check !== next.can_defer_other_check) {
      this.enableConsent.checked = false;
    }
    // A check can advance the revision without replacing the voice profile.
    // In particular, starting the playback check must not stop its sample.
    if (this.status?.profile.enrolled !== next.profile.enrolled) this._clearSample();
    if (this.status?.consent_text !== next.consent_text) this.enrollConsent.checked = false;
    this.status = next; this.fresh = true;
    return next;
  }

  _applyDiagnostic(value) {
    if (!value || typeof value !== 'object') return;
    // Mutation receipts only describe the grant. Preserve capabilities from
    // the full status, but never carry an old grant id or consent into a new
    // receipt. Dropping capabilities here hid the cues during actual capture.
    const capabilities = {};
    for (const key of ['owner_check_supported', 'owner_check_consent_version',
      'greeting_probe_supported', 'greeting_probe_consent_version']) {
      if (Object.hasOwn(this.diagnostic || {}, key)) capabilities[key] = this.diagnostic[key];
    }
    this.diagnostic = { ...capabilities, ...value };
  }

  async load() {
    if (this.disposed || this.busy || this.cleanupPending) return null;
    if (this.loading) return this.loading.promise;
    const task = { epoch: this.epoch, sequence: ++this.loadSequence };
    this.loading = task;
    task.promise = (async () => {
      try {
        const value = await this.get(API);
        if (this.disposed || task.epoch !== this.epoch || task.sequence !== this.loadSequence) return null;
        const priorRevision = this.status?.profile.revision;
        const current = this._applyStatus(value);
        if (priorRevision !== undefined && priorRevision !== current.profile.revision) {
          this.results.clear();
          if (this.enrollment) {
            this._error('The profile changed outside this setup. The unfinished enrollment is being cancelled; review the current profile before starting again.');
            void this._cancelPending();
          }
        }
        return current;
      } catch (error) {
        if (!this.disposed && task.epoch === this.epoch && task.sequence === this.loadSequence) {
          this.fresh = false; this._error(errorText(error));
        }
        return null;
      } finally {
        if (this.loading === task) this.loading = null;
        this._render();
      }
    })();
    this._render();
    return task.promise;
  }

  _current(op) { return !this.disposed && !op.cancelled && this.busy === op && this.epoch === op.epoch; }
  async _loadDevices() {
    if (this.disposed || !this._idle() || this.devicesLoading) return;
    this.devicesLoading = true; this._render();
    try {
      const devices = await this.enumerateDevices();
      if (this.disposed) return;
      this.devices = devices.filter(item => item.kind === 'audioinput' && text(item.deviceId)).slice(0, 32);
      const selected = this.deviceSelect.value;
      const options = [{ deviceId: '', label: 'Browser default' }, ...this.devices];
      this.deviceSelect.replaceChildren(...options.map((device, index) => {
        const option = this._node('option', text(device.label).slice(0, 160) || `Microphone ${index}`);
        option.value = device.deviceId; return option;
      }));
      // Preserve a selected device if it disappeared; do not silently switch inputs.
      if (selected && !options.some(item => item.deviceId === selected)) {
        const missing = this._node('option', 'Previously selected microphone (unavailable)');
        missing.value = selected; this.deviceSelect.append(missing);
      }
      this.deviceSelect.value = selected || '';
      this.deviceHint.textContent = this.devices.length
        ? 'Used for setup and live listening.'
        : 'No microphones were listed. Check your browser microphone permission and device connection.';
    } catch {
      if (!this.disposed) this.deviceHint.textContent = 'Microphones could not be listed. Check your browser microphone permission.';
    } finally {
      this.devicesLoading = false; this._render();
    }
  }
  _run(kind, execute, caseLabel = '') {
    if (this.disposed || !this._idle() || (this.deleteReview && kind !== 'delete')) return Promise.resolve(false);
    const op = {
      kind, caseLabel, epoch: ++this.epoch, controller: new AbortController(),
      origin: this.document.activeElement, cancelled: false, elapsed: 0, phase: 'Preparing…',
      expectedRevision: this.status.profile.revision, lastAction: '', session: null,
      deviceId: this.deviceSelect.value || '',
    };
    this.busy = op;
    op.promise = (async () => {
      let completed = false;
      try {
        await execute(op);
        completed = true;
      } catch (error) {
        const unknownStart = /\/(?:enroll|greeting|test)\/start$/.test(op.lastAction) && !op.session;
        if (!this.disposed && (this._current(op) || unknownStart || (op.lastAction && error?.name !== 'AbortError'))) {
          op.error = error;
          if (error?.name !== 'AbortError') {
            this._clipFeedback(op, errorText(error), true);
            this._error(errorText(error));
          }
          if (op.lastAction) this.fresh = false;
          if (unknownStart) {
            this.unresolvedStart = true;
            this._error('The start outcome is unknown. It will not be retried. Refresh status and resolve unfinished setup before recording again.');
          }
        }
        if (op.session && (op.session.type === 'test' || op.lastAction)) {
          if (op.session.type !== 'test') this.enrollment = null;
          await this._cancelSession(op.session);
        }
      } finally {
        if (op.beforeStarted && !op.afterCalled) {
          op.afterCalled = true;
          op.phase = 'Finishing microphone handoff…';
          // Publish the server's accepted count before awaiting the parent.
          // Keep busy set until the handoff finishes so no second graph starts.
          this._render();
          try {
            await this.onAfterRecording({
              caseLabel, token: op.token, signal: op.controller.signal,
              cancelled: op.cancelled || op.controller.signal.aborted, error: op.error,
            });
          } catch (error) {
            completed = false;
            this.fresh = false;
            const message = `Automatic voice remains under parent policy: ${errorText(error)}`;
            this._clipFeedback(op, message, true);
            this._error(message);
          }
        }
        completed = completed && this._current(op);
        const restoreFocus = op.forceFocus || [op.origin, this.cancelButton, this.progressRegion].includes(this.document.activeElement);
        if (this.busy === op) this.busy = null;
        this._render();
        if (!this.disposed && !op.cancelled && !this.deleteReview && op.focus && restoreFocus) {
          op.focus.focus();
        }
      }
      return completed;
    })();
    this._render();
    if (caseLabel && this._current(op)) this.progressRegion.focus();
    return op.promise;
  }

  async _post(op, action, body) {
    op.lastAction = action;
    const result = await this.post(API + action, body);
    if (op.kind === 'greeting-probe' && action === '/diagnostic/arm'
        && result?.diagnostic?.purpose === 'greeting_probe' && sessionId(result.diagnostic.id)) {
      op.probeGrant = result.diagnostic;
    }
    if (['/enroll/start', '/greeting/start'].includes(action) && sessionId(result?.enrollment?.id)) {
      op.session = { type: action.split('/')[1], id: result.enrollment.id };
    }
    if (action === '/test/start' && sessionId(result?.test?.id)) {
      op.session = { type: 'test', id: result.test.id };
    }
    if (['/enroll/start', '/greeting/start', '/test/start'].includes(action) && !op.session) {
      throw new Error('The start response did not identify its session. Its outcome cannot be confirmed.');
    }
    if (!this._current(op)) {
      if (op.session) await this._cancelSession(op.session);
      return null;
    }
    this._applyStatus(result?.status);
    if (result?.diagnostic) this._applyDiagnostic(result.diagnostic);
    return result;
  }

  _startEnrollment(mode = 'enroll') {
    const consent = mode === 'greeting' ? this.greetingConsent : this.enrollConsent;
    if (!this._ready() || !consent.checked || this.enrollment) return;
    consent.checked = false;
    this._clearSample();
    this.enrollmentMode = mode;
    return this._run('enroll-start', async op => {
      const generalVoice = mode === 'greeting' && this.status?.greeting?.voice_consent_version;
      const result = await this._post(op, `/${mode}/start`, { consent: true, expected_revision: op.expectedRevision,
        ...(generalVoice ? { consent_version: generalVoice } : {}) });
      if (!result) return;
      const enrollment = readEnrollment(result.enrollment);
      if (this.status.profile.enabled || enrollment.collected !== 0) {
        throw new Error('Setup did not return a fresh, paused enrollment. Refresh status before continuing.');
      }
      this.enrollment = enrollment;
      for (const clip of this.clips) {
        clip.outcome = null; clip.feedback.textContent = '';
      }
      this.notice = mode === 'greeting'
        ? generalVoice ? 'Record three short phrases, then save.'
          : 'Say “Hi Vega” once in each short clip below, then save. Your original samples and checks are kept.'
        : 'Setup started. Automatic voice is disabled. Choose Record for each of the three clips.';
      op.focus = this.clips[0].button;
    });
  }

  _useShortSamples() {
    if (!this._ready() || !this.greetingConsent.checked || this.enrollment
        || !this.status?.greeting?.voice_consent_version) return;
    this.greetingConsent.checked = false;
    return this._run('short-profile-scope', async op => {
      const result = await this._post(op, '/greeting/use-for-voice', {
        consent: true, consent_version: this.status.greeting.voice_consent_version,
        expected_revision: op.expectedRevision,
      });
      if (!result) return;
      if (!this.status.greeting?.general_voice) throw new Error('Short voice recognition was not enabled.');
      this.notice = 'Saved samples added to your voice profile.';
    });
  }

  _greetingProbe() {
    if (!this._ready() || !this.greetingProbeConsent.checked || this.enrollment
        || !this.diagnostic?.greeting_probe_supported) return;
    this.greetingProbeConsent.checked = false;
    return this._run('greeting-probe', async op => {
      op.greetingProbe = true;
      this.greetingProbeDetails.open = true;
      let grant = null, saved = false;
      try {
        const armed = await this._post(op, '/diagnostic/arm', {
          consent: true, purpose: 'greeting_probe',
          consent_version: this.diagnostic.greeting_probe_consent_version,
          request_id: this._sampleKey(),
        });
        grant = armed?.diagnostic || op.probeGrant;
        if (!armed) return;
        if (grant?.state !== 'armed' || grant.purpose !== 'greeting_probe' || !sessionId(grant.id)) {
          throw new Error('The one-time diagnostic could not be prepared. No recording started.');
        }
        const started = await this._post(op, '/greeting/start', {
          consent: true, expected_revision: this.status.profile.revision,
        });
        if (!started) return;
        await this._before(op);
        const audio = await this._capture(op);
        const result = await this._post(op, '/greeting/probe', {
          enrollment_id: op.session.id, sample_key: this._sampleKey(),
          audio, sample_rate: SAMPLE_RATE,
        });
        if (!result) return;
        saved = result.diagnostic_only === true && result.diagnostic?.state === 'stored'
          && result.diagnostic.id === grant.id && result.diagnostic.purpose === 'greeting_probe';
        if (!saved) throw new Error('The diagnostic was not saved. Your voice profile and checks are unchanged.');
        await this._cancelSession(op.session);
        this.notice = 'Diagnostic saved locally. Tell Codex it is ready; no more takes are needed now.';
        op.focus = this.greetingProbeButton;
      } finally {
        if (!saved && grant?.id) {
          const cleared = await this.post(API + '/diagnostic/clear', { id: grant.id });
          if (!this.disposed) this._applyStatus(cleared.status);
        }
      }
    }, 'short-greeting diagnostic');
  }

  async _before(op) {
    op.phase = `Preparing microphone for ${op.caseLabel}…`;
    op.beforeStarted = true;
    this._render();
    if (op.greetingProbe) this.progressRegion.scrollIntoView?.({ block: 'nearest' });
    op.token = await this.onBeforeRecording({ caseLabel: op.caseLabel, signal: op.controller.signal });
    if (!this._current(op)) throw abortError();
    op.beforeReady = true;
  }
  async _capture(op) {
    if (!this._current(op)) throw abortError();
    op.phase = `Preparing microphone for ${op.caseLabel}…`;
    this._render();
    const greeting = op.session?.type === 'greeting';
    const seconds = op.greetingProbe ? 12 : greeting ? 3 : this.status.enrollment_seconds;
    let audio = await this.recordAudio({
      seconds, caseKey: op.caseKey, caseLabel: op.caseLabel, signal: op.controller.signal,
      countInSeconds: greeting ? 1 : 0,
      deviceId: op.deviceId, unprocessed: Boolean(op.unprocessed),
      onProgress: progress => {
        if (!this._current(op)) return;
        const elapsed = typeof progress === 'number' ? progress : progress?.elapsedSeconds;
        if (!Number.isFinite(elapsed)) return;
        const phase = progress?.phase === 'count-in'
          ? `Get ready — ${progress.countInSeconds || 1}…`
          : op.greetingProbe
            ? `Recording — Say “${SHORT_PROBE_PHRASES[Math.min(3, Math.floor(elapsed / 3))]}” · ${Math.min(4, Math.floor(elapsed / 3) + 1)} of 4`
            : `Recording ${op.caseLabel}${greeting ? ' — Speak now' : ''}`;
        const phaseChanged = op.phase !== phase;
        op.phase = phase;
        const next = Math.min(seconds, Math.max(0, Math.floor(elapsed)));
        if (progress && typeof progress === 'object') {
          if (Number.isFinite(progress.level)) {
            op.level = Math.min(1, Math.max(0, progress.level));
            op.peakLevel = Math.max(op.peakLevel || 0, op.level);
          }
          if (progress.input && typeof progress.input === 'object') {
            op.inputDescription = inputDescription(progress.input,
              op.unprocessed || ['media', 'overlap', 'playback'].includes(op.caseKey), op.unprocessed);
          }
        }
        op.elapsed = next;
        if (phaseChanged) this._render();
        else this._renderProgress();
      },
    });
    try {
      if (!this._current(op)) throw abortError();
      if (!(audio instanceof Uint8Array) || audio.length % 2
          || audio.length > (greeting && !op.greetingProbe ? 4 * BYTES_PER_SECOND : MAX_AUDIO_BYTES)
          || audio.length < (op.greetingProbe ? 11.5 : greeting ? .6 : this.status.min_speech_seconds) * BYTES_PER_SECOND) {
        throw new Error(greeting
          ? 'The greeting recording was incomplete. Retry this clip after “Speak now”; nothing was submitted.'
          : 'The clip must contain 3–12 seconds of mono 16 kHz PCM audio. Record a new clip; nothing was submitted.');
      }
      const encoded = base64PCM(audio);
      op.phase = `Checking ${op.caseLabel} locally…`;
      this._render();
      return encoded;
    } finally {
      if (audio instanceof Uint8Array) audio.fill(0);
      audio = null;
    }
  }
  _sampleKey() {
    return this.window.crypto?.randomUUID?.() || `${this.prefix}-${Date.now()}-${++this.keySequence}`;
  }

  _recordEnrollment(index) {
    if (!this._ready() || !this.enrollment || this.enrollment.collected !== index || index >= 3) return;
    const enrollment = { ...this.enrollment };
    return this._run('enroll-record', async op => {
      op.clipIndex = index;
      this.clips[index].outcome = null;
      this.clips[index].feedback.textContent = '';
      op.session = { type: this.enrollmentMode, id: enrollment.id };
      const sampleKey = this._sampleKey();
      await this._before(op);
      const audio = await this._capture(op);
      if (!this._current(op)) return;
      const result = await this._post(op, `/${op.session.type}/sample`, {
        enrollment_id: enrollment.id, sample_key: sampleKey, audio, sample_rate: SAMPLE_RATE,
      });
      if (!result) return;
      const next = readEnrollment(result.enrollment);
      if (next.id !== enrollment.id || typeof result.accepted !== 'boolean'
          || next.collected !== enrollment.collected + (result.accepted ? 1 : 0)) {
        throw new Error('The clip response does not match this enrollment. Refresh status before continuing.');
      }
      this.enrollment = next;
      if (op.session.type === 'greeting' && Number.isInteger(result.reselected_recordings)
          && result.reselected_recordings > 0) {
        for (const clip of this.clips.slice(0, enrollment.collected)) {
          clip.outcome = 'passed';
          clip.feedback.className = 'sp-status';
          clip.feedback.textContent = 'Selected from your consistent takes.';
        }
      }
      const detail = text(result.detail).trim() || (result.accepted
        ? 'The local server accepted this clip.'
        : op.session.type === 'greeting'
          ? 'The local server did not accept this greeting. Wait for “Speak now”, then say “Hi Vega” once. Earlier accepted clips are kept.'
          : 'The local server did not accept this clip. Record again in a quiet place with at least six seconds of clear speech from you alone.');
      const collecting = op.session.type === 'greeting' && result.reason === 'inconsistent_greeting';
      const diagnosisNeeded = collecting && greetingNeedsDiagnosis(this.status);
      this.notice = '';
      this._clipFeedback(op, diagnosisNeeded ? 'Matching needs diagnosis. End setup below.'
        : collecting ? 'Clear take. Try once more.' : detail, !result.accepted);
      if (collecting) {
        this.clips[index].outcome = 'collecting';
        this.clips[index].feedback.className = 'sp-status';
      } else if (!result.accepted) this._error(detail);
      op.focus = diagnosisNeeded ? this.diagnostic?.greeting_probe_supported ? this.greetingDiagnose : this.cancelButton
        : next.collected === 3 ? this.commitButton : this.clips[next.collected].button;
    }, `enrollment clip ${index + 1}`);
  }

  _commit() {
    if (!this._ready() || this.enrollment?.collected !== 3) return;
    const enrollment = this.enrollment;
    return this._run('enroll-commit', async op => {
      op.session = { type: this.enrollmentMode, id: enrollment.id };
      const greeting = this.enrollmentMode === 'greeting';
      const result = await this._post(op, `/${op.session.type}/commit`, {
        enrollment_id: enrollment.id, expected_revision: op.expectedRevision,
      });
      if (!result) return;
      this.enrollment = null;
      op.session = null;
      this._clearSample();
      if (greeting) {
        if (result.committed === false && result.setup_finished && result.needs_review) {
          this.notice = result.retained_previous
            ? 'New samples need review. Your previous short profile is still active.'
            : 'Samples kept for review. Short voice recognition is not ready.';
          op.focus = this.greetingStart;
          return;
        }
        if (!this.status.greeting?.enrolled) throw new Error('Saving the short greetings could not be confirmed.');
        this.notice = this.status.greeting?.voice_consent_version
          ? this.status.greeting.general_voice ? 'Short samples saved. Try a short phrase.'
            : 'Samples saved. Matching needs review.'
          : this.status.profile.enabled
          ? 'Greetings saved. Resume listening and try “Hi Vega”.'
          : this._canEnable() ? 'Greetings saved. Enable owner voice below, then Resume listening.'
            : 'Greetings saved. Complete the voice checks below.';
        op.focus = this.status.profile.enabled ? this.greetingStart
          : this._canEnable() ? this.enableButton : this.checksHeading;
        return;
      }
      if (!this.status.profile.enrolled || this.status.profile.enabled || this.status.profile.validated) {
        throw new Error('Saving did not return a disabled, unvalidated profile. Refresh status before continuing.');
      }
      this.results.clear(); this.testConsent.checked = false;
      this.notice = 'Voice saved. Run the checks below.';
      op.focus = this.checksHeading;
    });
  }

  _test(caseKey, { unprocessed = false } = {}) {
    if (!this._ready() || !this.status.profile.enrolled || this.enrollment || !this.testConsent.checked
        || !CHECKS.some(item => item.key === caseKey)) return;
    return this._run('test-record', async op => {
      op.caseKey = caseKey;
      op.unprocessed = unprocessed && caseKey === 'owner';
      const sampleKey = this._sampleKey();
      await this._before(op);
      const started = await this._post(op, '/test/start', { case: caseKey, expected_revision: op.expectedRevision,
        ...(op.unprocessed ? { input_mode: 'unprocessed' } : {}),
      });
      if (!started) return;
      if (!sessionId(started.test?.id) || started.test.case !== caseKey) {
        throw new Error('The check response does not match the requested sound source.');
      }
      if (op.unprocessed && started.test.input_mode !== 'unprocessed') {
        throw new Error('The local host has not loaded the unprocessed-check update. Refresh Vega before trying this check.');
      }
      const audio = await this._capture(op);
      if (!this._current(op)) return;
      const finished = await this._post(op, '/test/finish', {
        test_id: started.test.id, sample_key: sampleKey, audio, sample_rate: SAMPLE_RATE,
      });
      if (!finished) return;
      const result = finished.result;
      if (result?.case !== caseKey || typeof result.passed !== 'boolean' || !text(result.decision)) {
        throw new Error('The local check result is incomplete. No pass result was inferred.');
      }
      this.results.set(caseKey, {
        case: caseKey, passed: result.passed, decision: result.decision,
        reason: text(result.reason), detail: text(result.detail), diagnostics: result.diagnostics,
        inputDescription: op.inputDescription,
        input_mode: result.input_mode,
        checked_at: result.checked_at,
      });
      if (caseKey === 'owner') {
        this.results.set(`owner:${result.input_mode === 'unprocessed' ? 'unprocessed' : 'standard'}`,
          this.results.get(caseKey));
      }
      if (result.diagnostic) this._applyDiagnostic(result.diagnostic);
      op.session = null;
      this.notice = `${CHECKS.find(item => item.key === caseKey).label}: ${result.passed ? 'passed'
        : inconclusiveCheck(caseKey, result) ? 'inconclusive; automatic voice stays disabled' : 'failed'} according to the local check.`;
      op.focus = this.caseControls.get(caseKey).button;
    }, CHECKS.find(item => item.key === caseKey).label);
  }

  _cancelSession(session) {
    const key = `${session.type}:${session.id}`;
    if (this.cleanup.has(key)) return this.cleanup.get(key);
    const epoch = this.epoch;
    this.cleanupPending += 1;
    const body = session.type === 'test' ? { test_id: session.id } : { enrollment_id: session.id };
    const promise = (async () => {
      try {
        const result = await this.post(`${API}/${session.type}/cancel`, body);
        if (!this.disposed && this.epoch === epoch) this._applyStatus(result?.status);
      } catch (error) {
        if (!this.disposed) {
          this.fresh = false; this.unresolvedStart = true;
          this._error(`Cancellation could not be confirmed; it will not be retried. ${errorText(error)}`);
        }
      } finally {
        this.cleanupPending -= 1;
        this._render();
      }
    })();
    this.cleanup.set(key, promise);
    this._render();
    return promise;
  }

  _cancelPending() {
    if (this.cancelPromise) return this.cancelPromise;
    ++this.epoch; ++this.loadSequence;
    this.loading = null;
    const op = this.busy;
    if (op) { op.cancelled = true; op.controller.abort(); }
    const promises = [];
    if (this.enrollment) {
      promises.push(this._cancelSession({ type: this.enrollmentMode, id: this.enrollment.id }));
      this.enrollment = null;
    }
    if (op?.session) promises.push(this._cancelSession(op.session));
    if (op?.promise) promises.push(op.promise);
    this.enrollConsent.checked = false;
    this.greetingConsent.checked = false;
    this.greetingProbeConsent.checked = false;
    this._stopPlayback();
    this.notice = 'Cancelling setup…';
    this.cancelPromise = Promise.allSettled(promises).then(() => {
      this.cancelPromise = null;
      if (!this.disposed) {
        this.notice = this._ready() ? 'Setup cancelled.' : '';
        this._render();
      }
    });
    this._render();
    return this.cancelPromise;
  }
  cancelRecording() {
    return this.disposed ? this.cancelPromise || Promise.resolve() : this._cancelPending();
  }

  async _diagnoseGreeting() {
    if (!this._idle() || !this._ready() || this.deleteReview || !this.enrollment
        || this.enrollmentMode !== 'greeting' || !greetingNeedsDiagnosis(this.status)
        || !this.diagnostic?.greeting_probe_supported) return;
    const cancellation = this.cancelRecording();
    const epoch = this.epoch;
    await cancellation;
    // Only reveal the next action after the server confirms the old setup ended.
    if (this.disposed || this.epoch !== epoch || !this._idle() || !this._ready() || this.enrollment) return;
    this.greetingProbeDetails.open = true;
    this.greetingProbeDetails.scrollIntoView?.({ block: 'nearest' });
    (this.greetingProbeConsent.disabled ? this.greetingProbeSummary : this.greetingProbeConsent).focus();
  }

  _setting(action) {
    if (!this.fresh || this.enrollment) return;
    if (action === 'enable' && (!this._canEnable() || !this.enableConsent.checked)) return;
    if (action === 'disable' && !this.status.profile.enabled) return;
    return this._run(action, async op => {
      const body = { expected_revision: op.expectedRevision };
      if (action === 'enable') {
        body.consent = true;
        if (this._canDeferOther()) body.defer_other_check = true;
      }
      this.enableConsent.checked = false;
      const result = await this._post(op, `/${action}`, body);
      if (!result) return;
      this.notice = this.status.profile.enabled && this.status.profile.other_check_deferred
        ? 'Owner voice is enabled. The other-person check is deferred, not passed.'
        : `Owner voice is ${this.status.profile.enabled ? 'enabled' : 'disabled'}, as reported by the local profile.`;
    });
  }

  _generateSample() {
    if (!this._ready() || !this.status.profile.enrolled || this.enrollment || !this.cloudConsent.checked) return;
    this.cloudConsent.checked = false;
    return this._run('generate-sample', async op => {
      const result = await this._post(op, '/playback-sample', {
        expected_revision: op.expectedRevision, consent_to_cloud_speech: true,
      });
      if (!result) return;
      if (result.mime !== 'audio/mpeg' || !text(result.audio) || result.audio.length > 4 * 1024 * 1024
          || !/^[A-Za-z0-9+/]+={0,2}$/.test(result.audio) || result.audio.length % 4) {
        throw new Error('The generated playback sample is invalid. It has not been played.');
      }
      this._clearSample();
      const bytes = Uint8Array.from(atob(result.audio), character => character.charCodeAt(0));
      this.sampleUrl = URL.createObjectURL(new Blob([bytes], { type: result.mime }));
      this.audio.src = this.sampleUrl;
      this.sampleReady = true;
      this.notice = this.status.profile.enabled
        ? 'Sample generated using AWS speech. Nothing is playing. Disable owner voice or start the playback check before choosing Play sample.'
        : 'Sample generated using AWS speech. Nothing is playing; choose Play sample when ready.';
      op.focus = this.playButton;
    });
  }
  async _playSample() {
    if (this.disposed || !this.sampleReady || this.deleteReview || this.playPending || this.playing) return;
    if (this.status?.profile.enabled && !(this.busy?.kind === 'test-record' && this.busy.beforeReady && !this.busy.cancelled)) return;
    const version = ++this.playbackVersion, source = this.audio.src;
    this.playPending = true;
    this._render();
    try {
      await this.audio.play();
      if (this.disposed || !this.sampleReady || this.audio.src !== source || version !== this.playbackVersion) return;
      this.playing = true;
    } catch (error) {
      if (!this.disposed && this.sampleReady && this.audio.src === source && version === this.playbackVersion) {
        this._error(`The sample did not play. ${errorText(error)}`);
      }
    } finally {
      if (version === this.playbackVersion) this.playPending = false;
    }
    this._render();
  }
  _stopPlayback() {
    ++this.playbackVersion;
    this.playPending = false; this.playing = false;
    try { this.audio.pause(); } catch { /* No active media resource. */ }
    this._render();
  }
  _clearSample() {
    ++this.playbackVersion;
    this.sampleReady = false; this.playing = false; this.playPending = false;
    const sampleUrl = this.sampleUrl;
    this.sampleUrl = null;
    try { this.audio.pause(); this.audio.removeAttribute('src'); this.audio.load(); }
    catch { /* Continue clearing panel state even when browser media cleanup fails. */ }
    finally { if (sampleUrl) URL.revokeObjectURL(sampleUrl); }
  }

  _openDelete() {
    if (this.disposed || !this.status || this.deleteReview) return;
    this.deleteReview = { revision: null, opener: this.document.activeElement };
    void this._cancelPending();
    this.dialog.hidden = false;
    if (typeof this.dialog.showModal === 'function') this.dialog.showModal();
    else this.dialog.setAttribute('open', '');
    this.body.inert = true;
    this._render();
    this.keepButton.focus();
  }
  _closeDelete({ restoreFocus = true, force = false } = {}) {
    const review = this.deleteReview;
    if (!review || (!force && this.busy?.kind === 'delete')) return;
    this.deleteReview = null;
    if (typeof this.dialog.close === 'function') this.dialog.close();
    else this.dialog.removeAttribute('open');
    this.dialog.hidden = true; this.body.inert = false;
    this._render();
    if (restoreFocus && !this.disposed) {
      const target = review.opener && !review.opener.disabled ? review.opener : this.heading;
      target.focus();
    }
  }
  _delete() {
    const review = this.deleteReview;
    if (!review || !this._idle() || !this.fresh || review.revision !== this.status.profile.revision) return;
    return this._run('delete', async op => {
      const result = await this._post(op, '/delete', { expected_revision: review.revision, confirm: true });
      if (!result) return;
      if (this.status.profile.enrolled || this.status.profile.enabled) {
        throw new Error('Deletion was not confirmed by the local profile. Refresh status before continuing.');
      }
      this.enrollment = null; this.unresolvedStart = false; this.results.clear();
      this.testConsent.checked = false; this.enableConsent.checked = false;
      this.notice = 'The local voice profile was deleted. Owner voice is disabled.';
      this.deleteReview = null;
      if (typeof this.dialog.close === 'function') this.dialog.close();
      else this.dialog.removeAttribute('open');
      this.dialog.hidden = true; this.body.inert = false;
      op.focus = this.enrollmentHeading;
      op.forceFocus = true;
    });
  }

  _renderProgress() {
    if (this.disposed) return;
    const op = this.busy;
    const host = op?.greetingProbe ? this.greetingProbeDetails : op?.caseKey ? this.caseControls.get(op.caseKey).card
      : Number.isInteger(op?.clipIndex) ? this.clips[op.clipIndex].item : this.progressHome;
    if (this.progressRegion.parentNode !== host) host.append(this.progressRegion);
    const cancelHost = op?.caseLabel ? this.progressRegion : this.cancelHome;
    if (this.cancelButton.parentNode !== cancelHost) cancelHost.append(this.cancelButton);
    this.progressRegion.hidden = !op?.caseLabel;
    if (!op?.caseLabel) return;
    const seconds = op.greetingProbe ? 12 : op.session?.type === 'greeting' ? 3 : this.status?.enrollment_seconds || MAX_SECONDS;
    this.progress.max = seconds; this.progress.value = op.elapsed;
    const progressText = op.cancelled ? 'Stopping recording…'
      : `${op.phase}${op.phase.startsWith('Recording ') ? ` — ${op.elapsed} / ${seconds} seconds` : ''}`;
    if (this.progressText.textContent !== progressText) this.progressText.textContent = progressText;
    this.progress.setAttribute('aria-valuetext', `${op.elapsed} of ${seconds} seconds`);
    const recording = !op.cancelled && op.phase.startsWith('Recording ');
    this.inputText.textContent = op.inputDescription || '';
    this.inputText.hidden = !op.inputDescription;
    this.inputMeter.hidden = !recording || !Number.isFinite(op.level);
    // Display signal strength only; an audio level cannot certify speech or identity.
    this.inputMeter.value = Math.min(1, (op.level || 0) * 5);
    this.inputLevelText.hidden = this.inputMeter.hidden;
    this.inputLevelText.textContent = (op.peakLevel || 0) < .005 && op.elapsed >= 2
      ? 'Very little sound is reaching the microphone. Check its selection and input volume.'
      : 'Input level · sound reaching the microphone';
  }
  _render() {
    if (this.disposed || !this.element) return;
    const state = this.status, idle = this._idle(), modal = Boolean(this.deleteReview);
    const ready = this._ready(), blocked = !idle || modal;
    const diagnosisNeeded = greetingNeedsDiagnosis(state);
    this.element.setAttribute('aria-busy', String(Boolean(this.busy || this.loading || this.cleanupPending)));
    this.refreshButton.disabled = !idle || modal;
    this.message.textContent = diagnosisNeeded && this.notice === state.greeting.last_check.detail ? '' : this.notice;
    const diagnosticState = this.diagnostic?.state;
    this.diagnosticRegion.hidden = !['armed', 'stored', 'needs_review'].includes(diagnosticState);
    this.diagnosticText.textContent = this.diagnostic?.purpose === 'greeting_probe'
      ? diagnosticState === 'armed'
        ? 'Diagnostic ready to record.'
        : 'Diagnostic saved on this PC · expires after 30 minutes while Vega runs.'
      : diagnosticState === 'armed'
      ? `Diagnostic ready for your next ${this.diagnostic?.purpose === 'owner_check' ? 'voice check' : 'enrollment clip'}.`
      : diagnosticState === 'needs_review'
        ? 'Clear the earlier diagnostic before recording another.'
        : 'Failed check saved on this PC · expires after 30 minutes while Vega runs.';
    this.diagnosticClear.disabled = blocked || !this.diagnostic?.id || this.diagnostic.can_clear === false;
    this.diagnosticSetup.hidden = !this.diagnostic?.owner_check_supported || !state?.profile.enrolled;
    this.diagnosticConsent.disabled = blocked || ['armed', 'stored', 'needs_review'].includes(diagnosticState);
    this.diagnosticArm.disabled = !ready || !state?.profile.enrolled || !this.diagnosticConsent.checked
      || ['armed', 'stored', 'needs_review'].includes(diagnosticState);
    this.errorNode.textContent = this.errors.join('\n');
    this.dismissButton.hidden = this.errors.length === 0; this.dismissButton.disabled = modal;
    this.runtimeLabel.textContent = this.loading ? 'Loading local status…'
      : !this.fresh ? 'Status needs refresh' : state?.runtime.ready ? 'Local runtime ready' : 'Local runtime unavailable';
    this.runtimeLabel.dataset.state = state?.runtime.ready && this.fresh ? 'passed' : 'pending';
    this.runtimeError.textContent = state?.runtime.error || '';
    this.profileLabel.textContent = !state ? 'Loading profile…'
      : !state.profile.enrolled ? 'No saved voice.'
        : `Profile saved · listening ${state.profile.enabled ? 'on' : 'off'}`;
    const count = Number.isInteger(state?.profile.samples) ? state.profile.samples
      : Array.isArray(state?.profile.samples) ? state.profile.samples.length : null;
    this.sampleCount.textContent = this.enrollment
      ? `${this.enrollment.collected}/3 clips accepted`
      : count === null ? '' : `${count} saved ${count === 1 ? 'sample' : 'samples'}`;
    this.consentText.textContent = state?.consent_text || 'Load the local consent text before starting.';
    this.reenrollNote.hidden = !state?.profile.enrolled;
    this.enrollConsent.disabled = blocked || !ready || Boolean(this.enrollment);
    this.startButton.textContent = state?.profile.enrolled ? 'Start replacement' : 'Start enrollment';
    this.startButton.disabled = blocked || !ready || !this.enrollConsent.checked || Boolean(this.enrollment);
    const greeting = Boolean(this.enrollment && this.enrollmentMode === 'greeting');
    const generalVoice = Boolean(state?.greeting?.voice_consent_version);
    const greetingSetupReady = state?.greeting?.can_setup === undefined
      ? state?.profile.validated || state?.profile.other_check_deferred
      : state.greeting.can_setup === true;
    this.greetingStatus.textContent = state?.greeting?.general_voice ? 'Short voice profile ready.'
      : state?.greeting?.enrolled ? 'Short samples saved.'
      : diagnosisNeeded ? 'Matching needs diagnosis.'
        : 'Short greetings not set up.';
    this.greetingConsent.disabled = blocked || !ready || !state?.greeting || Boolean(this.enrollment)
      || !greetingSetupReady;
    this.greetingStart.disabled = this.greetingConsent.disabled || !this.greetingConsent.checked || diagnosisNeeded;
    this.greetingStart.textContent = generalVoice ? 'Add short samples' : 'Set up short greetings';
    if (generalVoice) this.greetingConsentLabel.textContent = state.greeting.voice_consent_text;
    this.greetingPromote.hidden = !generalVoice || !state?.greeting?.enrolled || state?.greeting?.general_voice;
    this.greetingPromote.disabled = this.greetingConsent.disabled || !this.greetingConsent.checked;
    this.greetingProbeDetails.hidden = !this.diagnostic?.greeting_probe_supported || !state?.profile.enrolled;
    this.greetingProbeConsent.disabled = blocked || !ready || Boolean(this.enrollment)
      || ['armed', 'stored', 'needs_review'].includes(diagnosticState)
      || !greetingSetupReady;
    this.greetingProbeButton.disabled = this.greetingProbeConsent.disabled || !this.greetingProbeConsent.checked;
    if (diagnosisNeeded && !this.greetingDiagnosisNeeded) this.greetingProbeDetails.open = true;
    this.greetingDiagnosisNeeded = diagnosisNeeded;
    this.enrollmentHeading.textContent = greeting ? generalVoice ? 'Record short phrases' : 'Record short greetings'
      : this.enrollment ? 'Record your voice' : 'Voice profile';
    this.enrollmentHelp.textContent = 'Record three 12-second clips. Speak for at least six seconds in your usual language; the examples are optional.';
    this.activeHelp.textContent = greeting
      ? diagnosisNeeded ? this.diagnostic?.greeting_probe_supported
        ? 'Setup paused. Choose “End setup & diagnose” below.' : 'Setup paused. Cancel setup to stop.'
        : generalVoice ? 'Wait for “Speak now”. Use your usual voice; examples are optional.'
          : 'Say “Hi Vega” after “Speak now”. Record three takes, then save.'
      : this.enrollmentHelp.textContent;
    this.activeHelp.hidden = !this.enrollment;
    this.replaceDetails.hidden = Boolean(this.enrollment);
    this.replaceSummary.textContent = state?.profile.enrolled ? 'Replace saved voice…' : 'Set up your voice';
    if (state && (this.enrollmentProfileSaved !== state.profile.enrolled
        || this.wasEnrolling && !this.enrollment)) {
      this.replaceDetails.open = !state.profile.enrolled;
      this.enrollmentProfileSaved = state.profile.enrolled;
    }
    this.wasEnrolling = Boolean(this.enrollment);
    this.clipList.hidden = !this.enrollment;
    this.cancelHome.hidden = !this.enrollment;
    this.commitButton.textContent = greeting ? generalVoice ? 'Save short samples' : 'Save short greetings' : 'Save voice profile';
    this.commitButton.hidden = greeting && diagnosisNeeded;
    this.greetingDiagnose.hidden = !greeting || !diagnosisNeeded || !this.diagnostic?.greeting_probe_supported;
    this.greetingDiagnose.disabled = blocked || !ready;
    for (const [index, clip] of this.clips.entries()) {
      const collected = this.enrollment?.collected || 0;
      const stored = !this.enrollment && state?.profile.enrolled && index < count;
      if (!this.enrollment && clip.outcome === 'passed') {
        clip.outcome = null;
        clip.feedback.textContent = '';
      }
      const active = this.busy?.kind === 'enroll-record' && this.busy.clipIndex === index;
      clip.state.textContent = stored ? 'Saved' : index < collected
        ? greeting && state?.greeting?.setup_policy === 'three-clear-references-v1' ? 'Recorded' : 'Accepted'
        : diagnosisNeeded && greeting && collected === index ? 'Needs diagnosis'
        : clip.outcome === 'collecting' ? 'Another take needed'
          : clip.outcome === 'failed' ? 'Not accepted'
          : active ? (this.busy.phase.startsWith('Recording ') ? 'Recording…'
            : this.busy.phase.startsWith('Checking ') ? 'Checking locally…' : 'Preparing…') : 'Not recorded';
      clip.state.dataset.state = stored || index < collected ? 'passed' : clip.outcome === 'failed' ? 'failed' : 'pending';
      if (greeting && diagnosisNeeded && collected === index) {
        clip.feedback.textContent = 'Matching needs diagnosis. End setup below.';
        clip.feedback.className = 'sp-status';
      }
      clip.words.textContent = greeting ? generalVoice
        ? ['Yes, that sounds good.', 'Please show my projects.', 'Let’s continue.'][index] : 'Hi Vega.' : PROMPTS[index];
      clip.button.textContent = `Record clip ${index + 1} · ${greeting ? 3 : state?.enrollment_seconds || MAX_SECONDS}s`;
      clip.button.disabled = blocked || !ready || !this.enrollment || collected !== index || greeting && diagnosisNeeded;
    }
    this.commitButton.disabled = blocked || !ready || this.enrollment?.collected !== 3;
    this.cancelButton.textContent = this.busy?.kind === 'test-record' ? 'Cancel check' : 'Cancel setup';
    this.cancelButton.disabled = modal || (!this.enrollment && !this.busy?.caseLabel && this.busy?.kind !== 'enroll-start');
    this.testConsent.disabled = blocked || !ready || !state?.profile.enrolled || Boolean(this.enrollment);
    this.deviceSelect.disabled = blocked || this.devicesLoading || Boolean(this.enrollment);
    this.deviceRefresh.disabled = blocked || this.devicesLoading;
    this.unprocessedButton.disabled = this.testConsent.disabled || !this.testConsent.checked;
    this.testConsentHint.hidden = this.testConsent.checked || !ready || !state?.profile.enrolled || Boolean(this.enrollment);
    for (const { key } of CHECKS) {
      const control = this.caseControls.get(key), value = state?.profile.validation[key] || 'pending';
      control.badge.textContent = STATES[value]; control.badge.dataset.state = value;
      if (key === 'other' && value === 'pending' && state?.profile.other_check_deferred) {
        control.badge.textContent = 'Not checked · deferred';
      }
      control.button.disabled = blocked || !ready || !state?.profile.enrolled || Boolean(this.enrollment) || !this.testConsent.checked;
      const candidate = this.results.get(key) || state?.last_checks?.[key]
        || (state?.last_check?.case === key ? state.last_check : null);
      const result = value !== 'pending' && candidate?.passed === (value === 'passed') ? candidate : null;
      const inconclusive = result && (!result.passed && result.reason === 'insufficient_speech' || inconclusiveCheck(key, result));
      if (inconclusive) {
        control.badge.textContent = 'Inconclusive';
        control.badge.dataset.state = 'pending';
      }
      const uncertain = result?.diagnostics?.uncertain_seconds;
      const detail = inconclusiveCheck(key, result) && result.reason === 'ambiguous_speech'
        ? `Vega could not assess this recording confidently${Number.isFinite(uncertain) && uncertain > 0
          ? ` (${uncertain.toFixed(2)} seconds uncertain)` : ''}. ${key === 'owner'
            ? 'Your voice was not compared with the saved profile.'
            : 'The audio was ignored; speaker matching did not run.'} Your three saved samples are kept.`
        : inconclusiveCheck(key, result) && result.reason === 'insufficient_speech' && result.diagnostics?.input_rms >= .005
          ? 'The microphone captured sound; too little clear speech was detected. Use continuous spoken dialogue through speakers close to the microphone. Check the input meter while recording.'
        : key === 'owner' && needsProfileReview(result) ? PROFILE_RECOVERY
        : text(result?.detail);
      control.fullResult.textContent = result
        ? `${result.input_mode === 'unprocessed' ? 'Without browser filters · ' : ''}${result.passed ? 'Check passed.' : inconclusive ? 'Check inconclusive.' : 'This check did not pass.'} ${detail}`
        : value === 'failed' ? 'The earlier check did not retain a detailed reason. Run this check again to see why it fails.' : '';
      const brief = {
        speaker_mismatch: 'Voice did not match. Use the saved language and microphone.',
        ambiguous_speech: 'Unclear speech. Try again in a quiet room.',
        insufficient_speech: 'Too little speech. Speak for longer.',
        multiple_or_overlapping_speakers: 'Possible overlapping voices. Try again when quiet.',
        inconsistent_speakers: 'Voice varied too much. Try speaking naturally.',
        poor_quality: 'Recording unclear. Check your microphone.',
        incomplete_speech_coverage: 'Recording incomplete. Try again.',
      };
      control.result.textContent = result
        ? result.passed ? key === 'owner' ? 'Voice matched.' : 'Ignored, as expected.'
          : brief[result.reason] || 'Check did not pass. See Details.'
        : value === 'failed' ? 'Try this check again.' : '';
      control.result.className = value === 'failed' && !inconclusive ? 'sp-error' : 'sp-status';
      control.input.textContent = text(result?.inputDescription);
      control.input.hidden = !control.input.textContent;
      if (key === 'owner') {
        this.reviewEnrollmentButton.hidden = !state?.profile.enrolled || !needsProfileReview(result);
        this.reviewEnrollmentButton.disabled = blocked || !ready || Boolean(this.enrollment);
      }
    }
    const ownerCurrent = state?.profile.enrolled && state.profile.validation.owner !== 'pending';
    const ownerLatest = this.results.get('owner') || state?.last_checks?.owner;
    let hasComparison = false;
    for (const [mode, row] of this.ownerComparisonRows) {
      const comparison = ownerCurrent ? this.results.get(`owner:${mode}`) || state?.owner_comparisons?.[mode]
        || ((ownerLatest?.input_mode || 'standard') === mode ? ownerLatest : null) : null;
      hasComparison ||= Boolean(comparison);
      row.description.textContent = comparisonText(comparison);
      row.recorded.textContent = Number.isFinite(comparison?.checked_at)
        ? new Date(comparison.checked_at * 1000).toLocaleString() : '';
    }
    this.ownerComparisonDetails.hidden = !hasComparison;
    this.cloudConsent.disabled = blocked || !ready || !state?.profile.enrolled || Boolean(this.enrollment);
    this.generateButton.disabled = this.cloudConsent.disabled || !this.cloudConsent.checked;
    this.playButton.disabled = modal || !this.sampleReady || this.playPending || this.playing || Boolean(this.loading)
      || Boolean(this.busy && this.busy.kind !== 'test-record')
      || Boolean(state?.profile.enabled && !(this.busy?.kind === 'test-record' && this.busy.beforeReady && !this.busy.cancelled));
    this.stopButton.disabled = modal || !this.sampleReady;
    this.enableConsent.disabled = blocked || !this._canEnable() || Boolean(this.enrollment);
    this.enableConsentLabel.textContent = this._canDeferOther() || state?.profile.other_check_deferred
      ? 'Enable voice with four passed checks. I agree to defer the untested other-person check.'
      : 'I consent to use this validated local profile for automatic voice.';
    this.enableButton.disabled = this.enableConsent.disabled || !this.enableConsent.checked;
    this.disableButton.disabled = blocked || !this.fresh || !state?.profile.enabled || Boolean(this.enrollment);
    this.deleteButton.disabled = modal || !state || (!state.profile.enrolled && !this.enrollment && this.busy?.kind !== 'enroll-start')
      || this.busy?.kind === 'delete';
    this.enableHelp.textContent = state?.profile.enabled
      ? state.profile.other_check_deferred
         ? 'Listening on · other-person check deferred.'
         : 'Listening on.'
      : this._canDeferOther()
       ? 'Four checks passed. You can test another person later.'
      : this._canEnable()
       ? 'Ready to enable.'
       : 'Complete the checks above to enable voice.';
    if (this.deleteReview) {
      const review = this.deleteReview;
      if (review.revision === null && idle && this.fresh) review.revision = state.profile.revision;
      const stale = review.revision !== null && review.revision !== state.profile.revision;
      this.dialogStatus.textContent = !idle ? 'Stopping or finishing current work before deletion…'
        : !this.fresh ? 'Close this dialog and refresh status before reviewing deletion.'
          : stale ? 'The profile changed. Close this dialog and review deletion again.' : 'Choose Delete voice profile to confirm.';
      this.confirmDeleteButton.disabled = !idle || !this.fresh || review.revision === null || stale;
      this.keepButton.disabled = this.busy?.kind === 'delete';
    }
    this._renderProgress();
  }

  dispose() {
    if (this.disposed) return this.cancelPromise || Promise.resolve();
    const pending = this._cancelPending();
    this._closeDelete({ restoreFocus: false, force: true });
    this.disposed = true;
    this._clearSample();
    for (const remove of this.listeners) remove();
    this.listeners = [];
    this.element.remove();
    return pending;
  }
}
