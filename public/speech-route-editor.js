// Changes are reviewed against a server-owned proposal. Tests use only the
// saved revision and never autoplay. No microphone request occurs on load.
const fields = ['transcriptionLanguage','speechModelId','voice','awsRegion','awsProfile'];
const labels = { transcriptionLanguage:'Transcription language', speechModelId:'Speech model', voice:'Voice', awsRegion:'AWS region', awsProfile:'AWS profile' };
const languageLabels = { 'en-US':'English (US)', 'fr-FR':'French', 'it-IT':'Italian',
  'ar-SA':'Arabic (Modern Standard)', 'ar-AE':'Arabic (Gulf)', auto:'Detect English, French or Italian' };

export class SpeechRouteEditor {
  constructor({ get, post, beforeSave, onSaved, onMicrophone }) {
    this.get = get; this.post = post; this.beforeSave = beforeSave; this.onSaved = onSaved; this.onMicrophone = onMicrophone;
    this.panel = document.getElementById('speech-route-controls');
    this.form = document.getElementById('speech-route-form');
    this.dialog = document.getElementById('speech-review-dialog');
    this.status = document.getElementById('speech-route-status');
    this.testStatus = document.getElementById('speech-test-status');
    this.audio = document.getElementById('speech-test-audio');
    this.ready = false; this.loading = false; this.busy = false; this.stale = false;
    this.current = null; this.catalog = null; this.proposal = null;
    this.testId = null; this.testGeneration = 0; this.testFlight = null; this.audioUrl = null;
    this.form.addEventListener('submit', event => { event.preventDefault(); void this.review().catch(error => this.error(error)); });
    this.form.addEventListener('input', event => {
      if (event.target.name === 'speechModelId') this.fillVoices(this.form.elements.voice.value);
      this.setStatus(this.dirty() ? 'Draft changes are not saved. Review them before saving or testing.' : 'The saved voice route is unchanged.');
      this.controls();
    });
    document.getElementById('speech-route-reload').addEventListener('click', () => void this.reload().catch(error => this.error(error)));
    document.getElementById('speech-review-cancel').addEventListener('click', () => void this.discardReview());
    document.getElementById('speech-review-save').addEventListener('click', () => void this.save().catch(error => this.error(error)));
    this.dialog.addEventListener('cancel', () => void this.discardReview());
    document.getElementById('speech-check-connection').addEventListener('click', () => void this.test('connection'));
    document.getElementById('speech-generate-sample').addEventListener('click', () => void this.test('voice'));
    document.getElementById('speech-test-stop').addEventListener('click', () => void this.stopTest());
    document.getElementById('speech-test-microphone').addEventListener('click', () => {
      if (!this.dirty() && !this.stale) void this.onMicrophone().catch(error => this.error(error));
    });
    this.audio.addEventListener('play', () => { this.testStatus.textContent = 'Playing the saved-route sample. Check that you hear the expected voice.'; });
    this.audio.addEventListener('ended', () => { this.testStatus.textContent = 'Sample finished. Microphone testing is separate.'; });
  }
  values() { return Object.fromEntries(fields.map(key => [key, this.form.elements.namedItem(key).value.trim()])); }
  dirty() {
    if (!this.current?.route) return false;
    const values = this.values();
    return fields.some(key => values[key] !== this.current.route[key]);
  }
  setStatus(message) { this.status.textContent = message; }
  error(error) {
    this.setStatus(error.message);
    const target = document.getElementById('speech-review-error');
    target.textContent = this.dialog.open ? error.message : '';
  }
  controls() {
    const changed = this.dirty();
    document.getElementById('speech-route-review').disabled = !this.ready || this.busy || this.stale || !changed;
    for (const id of ['speech-check-connection','speech-generate-sample','speech-test-microphone']) {
      document.getElementById(id).disabled = !this.ready || this.busy || this.stale || changed;
    }
    if (this.current?.microphone_disabled) document.getElementById('speech-test-microphone').disabled = true;
    document.getElementById('speech-route-reload').disabled = this.busy;
    document.getElementById('speech-test-stop').disabled = !this.testId && this.testFlight === null && !this.audioUrl;
    document.getElementById('speech-review-save').disabled = this.busy;
  }
  fillVoices(preferred) {
    const model = this.catalog?.speechModels.find(item => item.modelId === this.form.elements.speechModelId.value);
    const select = this.form.elements.voice;
    select.replaceChildren(new Option('Choose a compatible voice', ''));
    for (const voice of model?.voices || []) select.add(new Option(voice, voice));
    select.value = model?.voices.includes(preferred) ? preferred : '';
    document.getElementById('speech-region-hint').textContent = model?.regions
      ? 'Supported regions: ' + model.regions.join(', ') : 'Use the AWS region configured for this speech route.';
  }
  fill(speech) {
    this.current = speech;
    const modelSelect = this.form.elements.speechModelId;
    modelSelect.replaceChildren();
    for (const model of this.catalog.speechModels) modelSelect.add(new Option(model.label, model.modelId));
    const languages = this.form.elements.transcriptionLanguage;
    languages.replaceChildren();
    for (const language of this.catalog.languages) languages.add(new Option(languageLabels[language] || language, language));
    for (const key of fields.filter(key => key !== 'voice')) this.form.elements.namedItem(key).value = speech.route[key];
    this.fillVoices(speech.route.voice);
    this.stale = false; this.ready = true;
    this.setStatus('The saved voice route is unchanged. Tests use only the saved route.');
    this.controls();
  }
  async initialize() {
    if (this.loading || this.ready) return;
    this.loading = true; this.panel.hidden = false;
    try {
      const info = await this.get('/api/voice/config');
      this.catalog = info.catalog;
      if (!info.speech.configured) throw new Error(info.speech.error || 'The saved speech route is unavailable.');
      this.fill(info.speech);
      document.getElementById('speech-test-phrase').textContent = info.testPhrase;
    } catch (error) { this.error(error); }
    finally { this.loading = false; this.controls(); }
  }
  async reload() {
    await this.stopTest(false);
    await this.discardReview();
    const info = await this.get('/api/voice/config');
    this.catalog = info.catalog;
    if (!info.speech.configured) throw new Error(info.speech.error || 'The saved speech route is unavailable.');
    this.fill(info.speech);
  }
  observe(speech) {
    if (!this.ready || !speech?.revision || speech.revision === this.current.revision) return;
    if (this.stale && this.externalRevision === speech.revision) return;
    this.externalRevision = speech.revision;
    void this.stopTest(false);
    void this.discardReview();
    if (this.dirty()) {
      this.stale = true;
      this.setStatus('The saved route changed elsewhere. Your draft is preserved; reload the saved route before reviewing.');
      this.controls();
    } else {
      this.fill(speech);
      this.setStatus('The saved route was updated elsewhere.');
    }
  }
  async review() {
    if (!this.form.reportValidity() || this.busy || this.stale) return;
    this.busy = true; this.controls();
    try {
      const proposal = await this.post('/api/voice/config/propose', { values: this.values(), expected_revision: this.current.revision });
      this.proposal = proposal;
      const body = document.getElementById('speech-review-changes'); body.replaceChildren();
      for (const change of proposal.changes) {
        const row = document.createElement('tr');
        for (const value of [labels[change.field] || change.field, change.before, change.after]) {
          const cell = document.createElement('td'); cell.textContent = value; row.append(cell);
        }
        body.append(row);
      }
      if (!proposal.changes.length) { await this.discardReview(); this.setStatus('There are no route changes to save.'); return; }
      document.getElementById('speech-review-error').textContent = '';
      this.dialog.showModal();
    } finally { this.busy = false; this.controls(); }
  }
  async discardReview() {
    const proposal = this.proposal; this.proposal = null;
    if (this.dialog.open) this.dialog.close();
    if (proposal) await this.post('/api/voice/config/discard', { proposal_id: proposal.proposal_id }).catch(() => {});
  }
  async save() {
    const proposal = this.proposal;
    if (!proposal || this.busy) return;
    this.busy = true; this.controls();
    try {
      await this.stopTest(false);
      this.beforeSave();
      const result = await this.post('/api/voice/config/commit', { proposal_id: proposal.proposal_id });
      this.proposal = null; this.dialog.close();
      this.fill(result.speech);
      this.onSaved(result.speech);
      this.setStatus(result.unchanged ? 'The saved route was already current.' : 'Reviewed route saved. No speech test or microphone capture was started.');
    } finally { this.busy = false; this.controls(); }
  }
  clearAudio() {
    this.audio.pause(); this.audio.removeAttribute('src'); this.audio.load(); this.audio.hidden = true;
    if (this.audioUrl) URL.revokeObjectURL(this.audioUrl);
    this.audioUrl = null;
  }
  async stopTest(showStatus = true) {
    this.testGeneration++;
    const id = this.testId; this.testId = null;
    if (this.testFlight !== null) { this.testFlight = null; this.busy = false; }
    this.clearAudio();
    if (id) {
      try { await this.post('/api/voice/test/stop', { test_id: id }); }
      catch { if (showStatus) this.testStatus.textContent = 'Stopped locally. The provider cancellation could not be confirmed.'; this.controls(); return; }
    }
    if (showStatus) this.testStatus.textContent = 'Test stopped. No sample will play automatically.';
    this.controls();
  }
  async test(kind) {
    if (!this.ready || this.dirty() || this.stale || this.busy) return;
    await this.stopTest(false);
    const generation = this.testGeneration;
    const revision = this.current.revision;
    this.testFlight = generation;
    this.busy = true; this.controls();
    this.testStatus.textContent = kind === 'connection' ? 'Checking the saved AWS profile…' : 'Generating a sample with the saved route…';
    try {
      const prepared = await this.post('/api/voice/test/prepare', { kind, expected_revision: revision });
      if (generation !== this.testGeneration) { await this.post('/api/voice/test/stop', { test_id: prepared.test_id }); return; }
      this.testId = prepared.test_id; this.controls();
      const result = await this.post('/api/voice/test/run', { test_id: prepared.test_id });
      if (generation !== this.testGeneration || result.cancelled || result.revision !== this.current.revision) return;
      this.testId = null;
      if (kind === 'connection') {
        this.testStatus.textContent = `Credentials for ${this.current.route.awsProfile} are available. Microphone and playback checks remain separate.`;
      } else {
        const binary = atob(result.audio), bytes = Uint8Array.from(binary, character => character.charCodeAt(0));
        this.audioUrl = URL.createObjectURL(new Blob([bytes], { type: result.mime }));
        this.audio.src = this.audioUrl; this.audio.hidden = false;
        this.testStatus.textContent = `Sample generated with ${this.current.route.voice}. Press Play to check the sound; nothing plays automatically.`;
      }
    } catch (error) {
      if (generation === this.testGeneration) this.testStatus.textContent = error.message;
    } finally {
      if (this.testFlight === generation) { this.testId = null; this.testFlight = null; this.busy = false; }
      this.controls();
    }
  }
}
