import { VegaVoice } from './voice.js';
import { SpeechRouteEditor } from './speech-route-editor.js';
import { initWorkActivity, localTeamApi } from './team-panel.js';
import { initComposerAttachments, renderMessageAttachments } from './composer-attachments.js';
import { MemoryPanel, memoryEditorProject } from './memory-panel.js';
import { applyVegaBriefingAcknowledgements } from './policy/renderer/vegaBriefing.js';
import { calendarViewRange, calendarGridDays, calendarWeekDays, calendarAgendaDays, calendarEventsForDay, moveCalendarAnchor, moveCalendarSeriesToDay, localDateValue, localDateTimeValue } from './policy/renderer/vegaCalendar.js';

const $ = id => document.getElementById(id);
const node = (tag, text, className) => { const element = document.createElement(tag); if (text !== undefined) element.textContent = text; if (className) element.className = className; return element; };
let csrf = '', companion = null, host = null, view = 'conversation', history = [];
let statusLoading = null, historyLoading = null, companionLoading = null, companionLoadId = 0;
let viewRefreshing = null, companionNeedsRefresh = true;
let sending = false, pending = null, activeReply = null, draftVoice = null, connectionFailed = false;
let calendarView = 'month', anchor = Date.now(), plans = [], selectedPlan = null, selectedMemory = null, dragged = null;
let memoryPanel = null, memoryRevision = null, memoryLoading = false;
let projectToolsPanel = null;
let workActivity = null;
let workOverview = null, n8nPanel = null;
let infrastructurePanel = null;
let composerAttachments = null, pendingUncertain = false;
let speakerProfilePanel = null, profileRecording = false;
let navigationRevision = 0, contextUpdates = Promise.resolve();
const explicitReadouts = new Set();
const messageNodes = new Map();
// Formatting is optional: the conversation stays usable if its parser asset
// cannot load. Raw message text remains the source for speech and routing.
let renderReplyBody = (container, text) => {
  if (container.textContent !== text) container.textContent = text;
};
const titles = { conversation: 'Conversation', work: 'Projects', infrastructure: 'Infrastructure', automations: 'Automations', briefing: 'Briefing', planning: 'Calendar', memory: 'Memory', tools: 'Project tools', settings: 'Settings' };
function voicePauseSaved() {
  try { return sessionStorage.getItem('vega-voice-paused') === 'true'; } catch { return false; }
}
function saveVoicePause(paused) {
  try {
    if (paused) sessionStorage.setItem('vega-voice-paused', 'true');
    else sessionStorage.removeItem('vega-voice-paused');
  } catch {}
}
function controlLabel(id, label) {
  const control = $(id);
  (control.querySelector('.layout-control-label') || control).textContent = label;
  control.setAttribute('aria-label', label); control.title = label;
}
const attachmentsAvailable = () => !connectionFailed && (host?.local_attachments_version ?? companion?.local_attachments_version) === 1;
function renderComposer() {
  const draft = composerAttachments?.draft;
  if (draft) { draft.locked = sending || pendingUncertain; composerAttachments.render(); }
  const fileCount = pendingUncertain ? pending?.attachment_ids?.length || 0 : draft?.entries.length || 0;
  const text = pendingUncertain ? pending?.text || '' : $('message').value;
  $('send').disabled = sending || (!text.trim() && !fileCount)
    || fileCount > 0 && (!attachmentsAvailable() || !pendingUncertain && !draft?.ready);
  $('message').readOnly = sending || pendingUncertain;
  const label = pendingUncertain ? 'Retry sending message' : 'Send message';
  $('send').setAttribute('aria-label', label); $('send').title = label;
  $('send').querySelector('span').textContent = pendingUncertain ? '↻' : '↑';
}
function workerAttention() {
  return Boolean(host?.document_error) || host?.delivery_health === 'needs-attention'
    || Object.values(host?.workers || {}).some(worker => !worker.alive || worker.state === 'recovering');
}
function fail(message, { source = 'general' } = {}) {
  $('error').textContent = message; $('error').hidden = !message;
  $('error').dataset.source = message ? source : '';
  const dialog = document.querySelector('dialog[open]');
  if (dialog && message) {
    let alert = dialog.querySelector('.dialog-error');
    if (!alert) { alert = node('p', '', 'error dialog-error'); alert.setAttribute('role', 'alert'); (dialog.querySelector('.activity-scroll') || dialog).prepend(alert); }
    alert.textContent = message;
    alert.dataset.source = source;
  }
}
function clearWarning(source) {
  if ($('error').dataset.source === source) fail('');
  document.querySelectorAll('.dialog-error').forEach(alert => {
    if (alert.dataset.source === source) alert.remove();
  });
}
function clearPlaybackWarning() {
  clearWarning('playback');
}
async function get(path) {
  const response = await fetch(path, { cache: 'no-store', signal: AbortSignal.timeout(20000) });
  const result = await response.json();
  if (!response.ok) { const error = new Error(result.error || 'The local Vega host is unavailable.'); error.status = response.status; throw error; }
  return result;
}
async function post(path, payload) {
  // Older saved drafts and voice turns share this transport. Only conversation
  // fields go out; a remembered manual project must never pin the next turn.
  if (path === '/api/chat') {
    const { text, request_id, voice_receipt, attachment_ids } = payload;
    payload = { text, request_id, ...(voice_receipt ? { voice_receipt } : {}),
      ...(attachment_ids?.length ? { attachment_ids } : {}) };
  }
  csrf = (await get('/api/session')).csrf;
  const response = await fetch(path, { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Vega-CSRF': csrf }, body: JSON.stringify(payload), signal: AbortSignal.timeout(path.includes('/voice/') ? 180000 : 25000) });
  const result = await response.json();
  if (!response.ok) { const error = new Error(result.error || 'This request could not be completed.'); error.status = response.status; throw error; }
  return result;
}
const voice = new VegaVoice({
  post, paused: voicePauseSaved(), onError: (message, options) => fail(message, options || { source: 'voice' }),
  onState: state => {
    renderPresence(state);
    if (state.phase === 'listening' && voice.microphoneReady) clearWarning('voice');
  },
  onPlaybackStarted: clearPlaybackWarning,
  onDraft: (text, receipt) => {
    if (sending || pendingUncertain) { fail('Retry the pending message before replacing its draft.'); return; }
    $('message').value = text; draftVoice = { text, receipt }; saveDraft(); renderComposer(); $('message').focus();
    fail('Dictation is ready to review. Edit it if needed, then use the send arrow.');
  },
  onSent: id => { activeReply = id; void refresh(); },
});
const routeEditor = new SpeechRouteEditor({
  get, post,
  beforeSave: endVoiceForRouteChange,
  onSaved: speech => {
    companion.speech = speech;
    voice.configure(companion.preferences, speech, companion.context);
    renderSpeechSummary(speech);
    void voice.maybeAutoStart({ retry: true });
  },
  onMicrophone: async () => { await selectView('conversation'); fail('Record a short phrase. Its transcript will appear as a draft for your review.'); voice.stop(); voice.start(true); },
});
function endVoiceForRouteChange() {
  voice.stop(); draftVoice = null;
  // An already submitted request must retain its exact retry payload.
}
function renderSpeechSummary(speech) {
  const route = speech.route;
  $('speech-route').textContent = route ? `${route.voice} · ${route.transcriptionLanguage} · AWS Transcribe + Polly (${route.awsRegion}, ${route.awsProfile})` : 'The saved speech connection needs setup.';
  $('speech-verification').textContent = speech.error || (speech.verified ? 'The configured speech round trip passed a test recording. Your microphone is checked when you record a voice turn.' : 'The selected route still needs a speech test.');
}
function renderPresence(state = voice.state) {
  const feedback = $('voice-feedback');
  if (feedback) {
    const text = voice.speakerNotice || ({
      transcribing: 'Checking your voice and transcribing…',
      sending: 'Sending your voice message…',
      waiting: 'Vega is preparing a reply…',
    })[state.phase] || '';
    if (feedback.textContent !== text) feedback.textContent = text;
    feedback.hidden = !text;
  }
  const active = state.phase !== 'idle';
  const ownerSetup = !voice.ownerGate.ready && (!active || !voice.review);
  const opening = (active && state.phase === 'listening' && !voice.microphoneReady) || Boolean(voice.autoStartPending);
  const blocked = !active && ['blocked', 'unsupported', 'unavailable'].includes(voice.startupStatus);
  const phase = voice.manualPlayback ? 'speaking' : opening ? 'thinking' : blocked ? 'attention' : active ? ({ waiting: 'thinking', sending: 'thinking' }[state.phase] || state.phase)
    : connectionFailed || host?.conversation_error || host?.delivery_error || workerAttention() ? 'attention' : host?.live_replies_pending ? 'thinking' : 'idle';
  $('presence').dataset.phase = phase;
  $('presence').dataset.voiceSession = active ? 'active' : 'inactive';
  $('presence-state').textContent = connectionFailed ? 'Reconnecting' : voice.activationNeeded ? 'Enable audio' : blocked ? 'Microphone unavailable'
    : opening ? (voice.startupStatus === 'permission' ? 'Microphone permission' : 'Connecting microphone')
    : !active && voice.pausedByOwner ? 'Paused'
    : ownerSetup && !voice.manualPlayback && phase === 'idle' ? 'Ready'
    : ({ idle: 'Ready', thinking: 'Thinking', listening: 'Listening', transcribing: 'Transcribing', speaking: 'Speaking', attention: 'Needs attention' })[phase] || phase;
  $('voice-start').hidden = ownerSetup || (active || Boolean(voice.autoStartPending)) && !voice.activationNeeded;
  $('voice-pause').hidden = !active && !voice.autoStartPending && !voice.manualPlayback;
  $('voice-owner-setup').hidden = !ownerSetup || !host?.speech?.owner_voice_required;
  $('voice-start').disabled = connectionFailed || !companion?.speech.configured || companion.speech.microphone_disabled;
  controlLabel('voice-start', voice.activationNeeded ? 'Enable audio' : voice.startupStatus === 'blocked' ? 'Enable microphone' : 'Resume listening');
  $('voice-detail').textContent = connectionFailed ? 'Reconnecting to Vega. Your draft is kept.'
    : voice.activationNeeded ? 'Click Enable audio to hear Vega.'
    : opening ? (voice.startupStatus === 'permission' ? 'Allow microphone access in the browser prompt to begin listening.' : 'Connecting to your microphone…')
    : active ? (voice.review ? 'Dictating a draft for review.' : 'Listening. Pause stops the microphone.')
    : voice.pausedByOwner ? 'Listening is paused until you resume it.'
    : ownerSetup ? 'Voice setup is unfinished. You can type here.'
    : voice.startupStatus === 'blocked' ? 'Microphone access is blocked. Allow it in this browser to resume listening.'
    : blocked ? 'The microphone is unavailable. Resume listening when it is ready.'
    : companion?.speech.microphone_disabled ? 'Microphone capture is disabled in this preview.'
    : companion?.preferences.values.background_listening ? 'Listening starts automatically when Vega opens.'
    : 'Automatic listening is off in Preferences.';
}
function button(text, action) {
  const item = node('button', text); item.type = 'button';
  item.addEventListener('click', () => void Promise.resolve().then(action).catch(error => fail(error.message)));
  return item;
}
function renderHistory() {
  const area = $('messages');
  const visible = area.getClientRects().length > 0;
  const nearBottom = visible && (!messageNodes.size || area.scrollHeight - area.scrollTop - area.clientHeight < 120);
  if (history.length && !messageNodes.size) area.replaceChildren();
  for (const item of history) {
    let box = messageNodes.get(item.id);
    if (!box) {
      box = node('article', undefined, 'message ' + item.role);
      box.dataset.source = item.source;
      box.append(node('div', item.role === 'user' ? 'You · ' + item.source.replaceAll('_', ' ') : 'Vega', 'who'));
      const body = node('div', undefined, 'body');
      renderReplyBody(body, item.text, { markdown: item.role === 'assistant' });
      const attachments = node('div', undefined, 'message-attachments'); attachments.hidden = true;
      box.append(body, attachments);
      messageNodes.set(item.id, box); area.append(box);
    } else renderReplyBody(box.querySelector('.body'), item.text, { markdown: item.role === 'assistant' });
    renderMessageAttachments(box.querySelector('.message-attachments'), item.attachments);
  }
  if (nearBottom) area.scrollTop = area.scrollHeight;
  const completed = activeReply && history.find(item => item.update_id === activeReply && item.role === 'assistant');
  if (completed) {
    const voiceTurn = [...voice.turns.values()].some(turn => turn.updateId === completed.update_id);
    if (completed.speak_requested && !voiceTurn && !explicitReadouts.has(completed.update_id)) {
      explicitReadouts.add(completed.update_id);
      void voice.speak(completed.update_id).catch(error => fail(error.message, { source: 'playback' }));
    }
    activeReply = null;
  }
  $('cancel-reply').hidden = !activeReply;
  voice.observe(history);
}
async function refreshStatus() {
  if (statusLoading) return statusLoading;
  const loading = (async () => {
    const first = !host, recovered = connectionFailed;
    try {
      try {
        host = await get('/api/status');
      } catch (error) {
        connectionFailed = true;
        $('activity').textContent = 'Host disconnected'; $('connection').textContent = 'Reconnecting';
        fail('Vega is reconnecting. Your draft is preserved.', { source: 'connection' });
        voice.stop(); renderPresence(); renderComposer();
        return null;
      }
      $('project-controls-link').hidden = !host.project_requests_available;
      $('project-tools-view').hidden = !host.project_tools_available;
      connectionFailed = false; clearWarning('connection');
      $('connection').textContent = host.isolated_preview ? 'Preview'
        : host.delivery_error || workerAttention() ? 'Connection needs attention'
        : host.online ? 'Connected' : 'Telegram reconnecting';
      $('activity').textContent = host.isolated_preview ? 'Isolated preview'
        : host.conversation_error || host.delivery_error || workerAttention()
        ? 'Needs attention — ' + (host.conversation_error || host.delivery_error || 'a background worker is recovering')
        : host.voice_notes_pending ? 'Transcribing a Telegram voice note…' : host.live_replies_pending ? 'Vega is thinking…' : 'Ready';
      const workers = Object.values(host.workers || {});
      $('diagnostics').textContent = `Codex ${host.windows.codex.state}. Recovery ${host.windows.watchdog_enabled ? 'enabled' : 'paused'}. Telegram ${host.online ? 'connected' : 'reconnecting'}. ${(host.deliveries.queued || 0) + (host.deliveries.document_queued || 0)} deliveries queued. ${workers.filter(worker => worker.alive).length}/${workers.length} workers active; ${workers.reduce((sum, worker) => sum + worker.failures, 0)} recoveries.`;
      renderPresence(); renderComposer();
      const ownerChanged = companion && JSON.stringify(companion.speech.owner_voice) !== JSON.stringify(host.speech?.owner_voice);
      const speechChanged = companion && host.speech?.revision && (companion.speech.revision !== host.speech.revision || ownerChanged);
      if (speechChanged) {
        if (companion.speech.revision !== host.speech.revision) endVoiceForRouteChange();
        companion.speech = host.speech;
        voice.configure(companion.preferences, host.speech, companion.context);
        renderSpeechSummary(host.speech);
        if (view === 'settings' && !profileRecording) void speakerProfilePanel?.load();
      }
      if (companion && !profileRecording && !recovered && !companionNeedsRefresh && (first || speechChanged)) void voice.maybeAutoStart({ retry: true });
      renderPresence(); renderComposer();
      if (host.speech?.error) $('speech-verification').textContent = host.speech.error;
      else if (host.speech?.verified) $('speech-verification').textContent = 'The configured speech round trip passed a test recording. Your microphone is checked when you record a voice turn.';
      return { recovered };
    } catch (error) {
      fail('This view could not finish refreshing. ' + error.message, { source: 'view-refresh' });
      return null;
    }
  })();
  statusLoading = loading;
  try { return await loading; } finally { if (statusLoading === loading) statusLoading = null; }
}
async function refreshHistory(statusRequest) {
  if (historyLoading) return historyLoading;
  const loading = (async () => {
    const result = await get('/api/messages').then(value => ({ value }), error => ({ error }));
    if (!await statusRequest || connectionFailed) return;
    if (result.error) {
      fail('Conversation could not refresh. Showing saved messages; retrying automatically.', { source: 'history' });
    } else {
      history = result.value; renderHistory(); clearWarning('history');
    }
  })();
  historyLoading = loading;
  try { return await loading; } finally { if (historyLoading === loading) historyLoading = null; }
}
async function refreshView() {
  const selection = navigationRevision;
  if (viewRefreshing?.selection === selection) return viewRefreshing.promise;
  const loading = (async () => {
    try {
      if (view === 'settings' && host.speech?.route_controls_available) {
        if (!routeEditor.ready && !routeEditor.loading) await routeEditor.initialize();
        if (selection !== navigationRevision) return;
        routeEditor.observe(host.speech);
      }
      if (view === 'memory' && host.selective_memory_available && !memoryLoading &&
          (!memoryPanel || host.memory_revision !== memoryRevision)) await loadMemory();
      if (selection === navigationRevision) clearWarning('view-refresh');
    } catch (error) {
      if (selection === navigationRevision) fail('This view could not finish refreshing. ' + error.message, { source: 'view-refresh' });
    }
  })();
  viewRefreshing = { selection, promise: loading };
  try { await loading; } finally { if (viewRefreshing?.promise === loading) viewRefreshing = null; }
}
async function refreshCompanion() {
  try { await loadCompanion(); if (!companionNeedsRefresh) clearWarning('preferences'); }
  catch (error) {
    if (companionNeedsRefresh && !connectionFailed) fail('Preferences could not refresh. Retrying automatically.', { source: 'preferences' });
  }
}
async function refresh() {
  const statusRequest = refreshStatus();
  const messages = refreshHistory(statusRequest);
  let preferences = (!companion || companionNeedsRefresh || connectionFailed) ? refreshCompanion() : null;
  const current = await statusRequest;
  if (current?.recovered && !preferences) preferences = refreshCompanion();
  // Each source owns its pending request. A slow optional read cannot block
  // subsequent status checks, duplicate requests, or reset the current page.
  await Promise.allSettled([messages, preferences, current ? refreshView() : null]);
}
function saveDraft() { try { sessionStorage.setItem('vega-draft', $('message').value); } catch {} }
async function send(text) {
  if (sending) return;
  const retryingUncertain = pendingUncertain;
  const files = composerAttachments?.draft;
  if (pendingUncertain && text !== pending.text) { fail('Retry the pending message before sending another.'); return; }
  const attachmentIds = pendingUncertain ? pending.attachment_ids || [] : files?.ids || [];
  if (pendingUncertain ? !pending.text.trim() && !attachmentIds.length : !text.trim() && !files?.entries.length) return;
  if ((files?.entries.length || attachmentIds.length) && (!attachmentsAvailable() || !pendingUncertain && !files.ready)) {
    $('send-status').textContent = attachmentsAvailable() ? 'Finish uploading or remove the files that need attention.' : 'Attachments will be available after Vega updates.';
    return;
  }
  if (!pendingUncertain) pending = {
    text, request_id: crypto.randomUUID(), ...(attachmentIds.length ? { attachment_ids: attachmentIds } : {}),
    ...(draftVoice?.text === text ? { voice_receipt: draftVoice.receipt } : {}),
  };
  sending = true; renderComposer(); fail(''); $('send-status').textContent = 'Sending…';
  try { sessionStorage.setItem('vega-pending', JSON.stringify(pending)); } catch {}
  try {
    const result = await post('/api/chat', pending);
    if (result.update_id === undefined || result.update_id === null) throw new Error('The send receipt was not confirmed.');
    activeReply = result.update_id;
    pending = null; pendingUncertain = false;
    try {
      sessionStorage.removeItem('vega-pending');
      $('message').value = sessionStorage.getItem('vega-next-draft') || '';
      sessionStorage.removeItem('vega-next-draft');
    } catch { $('message').value = ''; }
    draftVoice = null; saveDraft(); files?.committed(attachmentIds); $('send-status').textContent = '';
    await refresh();
  } catch (error) {
    const refused = !retryingUncertain && [400, 403, 413, 415, 422].includes(error.status);
    pendingUncertain = !refused;
    if (refused) { pending = null; try { sessionStorage.removeItem('vega-pending'); } catch {} }
    $('send-status').textContent = refused ? error.message : 'Send not confirmed. Retry sends this same message and files.';
  } finally { sending = false; renderComposer(); $('message').focus(); }
}
function setOptions(select, selected) {
  select.replaceChildren(new Option('Personal', ''));
  for (const [alias, label] of Object.entries(companion.projects)) select.add(new Option(label, alias));
  select.value = selected || '';
}
async function loadCompanion({ force = false } = {}) {
  if (companionLoading && !force) return companionLoading;
  const identity = ++companionLoadId;
  companionNeedsRefresh = true;
  const loading = (async () => {
    const result = await get('/api/companion');
    if (identity !== companionLoadId) return;
    const needsContext = result.context.view?.kind !== view;
    companion = result;
    companion.context = conversationContext({
      ...result.context, view: { ...result.context.view, kind: view },
    });
    renderComposer();
    voice.configure(companion.preferences, companion.speech, companion.context);
    fillPreferences();
    renderSpeechSummary(companion.speech);
    companionNeedsRefresh = false;
    if (needsContext) void syncViewContext(view, navigationRevision);
    if (host && !connectionFailed && !profileRecording) void voice.maybeAutoStart();
  })();
  companionLoading = loading;
  try { await loading; } finally { if (companionLoading === loading) companionLoading = null; }
}
function syncViewContext(next, revision) {
  // Keep server writes ordered and skip views already superseded by another
  // click. Content can load while the context request is in flight.
  contextUpdates = contextUpdates.then(async () => {
    if (!companion || revision !== navigationRevision) return;
    try {
      const context = conversationContext(await post('/api/context', { kind: next }));
      if (revision !== navigationRevision) return;
      companion.context = context; voice.context(context); clearWarning('context');
    } catch (error) {
      if (revision === navigationRevision) {
        fail('Page context has not synced yet. Reopen this view to retry.', { source: 'context' });
      }
    }
  });
  return contextUpdates;
}
async function selectView(next) {
  if (!Object.hasOwn(titles, next)) return;
  const selection = ++navigationRevision;
  if (view === 'settings' && next !== 'settings') await speakerProfilePanel?.cancelRecording();
  if (selection !== navigationRevision) return;
  if (next !== 'tools') projectToolsPanel?.hide();
  if (next !== 'work') workOverview?.hide();
  if (next !== 'infrastructure') infrastructurePanel?.hide();
  if (next !== 'automations') n8nPanel?.hide();
  if (next !== 'conversation') workActivity?.close();
  view = next;
  document.querySelector('.workspace').dataset.activeView = next;
  document.querySelectorAll('[data-panel]').forEach(panel => { panel.hidden = panel.dataset.panel !== next; });
  document.querySelectorAll('[data-view]').forEach(item => item.dataset.view === next ? item.setAttribute('aria-current', 'page') : item.removeAttribute('aria-current'));
  $('view-title').textContent = titles[next];
  const contextUpdate = syncViewContext(next, selection);
  try {
    if (next === 'briefing') await loadBriefing();
    else if (next === 'memory') await loadMemory();
    else if (next === 'planning') await loadPlanning();
    else if (next === 'settings') {
      if ($('voice-settings').open) await loadSpeakerProfile();
      if (companion?.speech.route_controls_available) void routeEditor.initialize().catch(error => {
        if (selection === navigationRevision) fail(error.message);
      });
    }
    else if (next === 'work') {
      if (!workOverview) {
        const { WorkOverview } = await import('./work-overview.js');
        if (selection !== navigationRevision) return;
        workOverview ??= new WorkOverview($('work-overview-workspace'), {
          get, post, onRun: id => workActivity.openRun(id),
          onRequests: () => workActivity.open({ tab: 'requests' }), onAutomations: () => selectView('automations'),
        });
      }
      if (view === 'work') await workOverview.show();
    }
    else if (next === 'infrastructure') {
      if (!infrastructurePanel) {
        const { InfrastructurePanel } = await import('./infrastructure.js');
        if (selection !== navigationRevision) return;
        infrastructurePanel ??= new InfrastructurePanel($('infrastructure-workspace'), { get, post });
      }
      if (view === 'infrastructure') await infrastructurePanel.show();
    }
    else if (next === 'automations') {
      if (!n8nPanel) {
        const { N8nPanel } = await import('./n8n-panel.js');
        if (selection !== navigationRevision) return;
        n8nPanel ??= new N8nPanel($('n8n-workspace'), { get, post, onRun: id => workActivity.openRun(id) });
      }
      if (view === 'automations') await n8nPanel.show();
    }
    else if (next === 'tools') {
      if (!projectToolsPanel) {
        const { ProjectToolsPanel } = await import('./project-tools.js');
        if (selection !== navigationRevision) return;
        projectToolsPanel ??= new ProjectToolsPanel($('project-tools-workspace'), { get, post });
      }
      if (view === 'tools') await projectToolsPanel.show();
    }
    void workActivity?.refresh();
    await contextUpdate;
  } catch (error) {
    if (selection === navigationRevision) throw error;
  }
}
function conversationContext(context) {
  return { ...context, view: { ...context.view, project: null } };
}
function mountWorkActivity() {
  workActivity ||= initWorkActivity({
    root: $('conversation-activity'), api: localTeamApi(),
    allowManualStart: true,
  });
}
function initializeConversationLayout() {
  // The legacy layout initializer requires the removed context selector.
  // Keep the existing layout classes and sidebar preference in this shell.
  const workspace = document.querySelector('.workspace'), sidebar = workspace.querySelector('.sidebar');
  const paths = {
    conversation: 'M5 5h14v11H9l-4 3Z',
    work: 'M3 9h5v6H3ZM16 3h5v6h-5ZM16 15h5v6h-5ZM8 12h4M12 6h4M12 18h4M12 6v12',
    infrastructure: 'M4 3h16v6H4ZM4 13h16v6H4ZM8 6h.01M8 16h.01M12 9v4M12 19v3',
    automations: 'M9 7H6a5 5 0 1 0 0 10h3l6-10h3a5 5 0 1 1 0 10h-3L9 7Z',
    briefing: 'M4 4h16v16H4ZM8 9h8M8 13h5M8 17h3',
    planning: 'M3 5h18v16H3ZM8 3v4M16 3v4M3 10h18M7 14h3M14 14h3',
    memory: 'M4 4h6v6H4ZM15 6h5v5h-5ZM7 16h6v5H7ZM10 7h5M7 10l3 6M17 11l-4 6',
    tools: 'M3 7h7l2 2h9v11H3ZM3 7V4h7l2 3M8 13l-2 2 2 2M16 13l2 2-2 2',
    settings: 'M4 7h16M4 17h16M8 4v6M16 14v6',
    microphone: 'M9 3h6v11H9ZM6 11a6 6 0 0 0 12 0M12 17v4M9 21h6',
    pause: 'M9 6v12M15 6v12',
    compact: 'M7 5H5v4M17 5h2v4M7 19H5v-4M17 19h2v-4M8 8h8v8H8Z',
    sidebar: 'M3 4h18v16H3ZM9 4v16',
  };
  function decorate(control, icon) {
    const label = node('span', undefined, 'layout-control-label');
    const name = control.getAttribute('aria-label') || control.textContent.trim();
    label.append(...control.childNodes);
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    for (const [key, value] of Object.entries({ class: 'layout-control-icon', viewBox: '0 0 24 24', fill: 'none',
      stroke: 'currentColor', 'stroke-width': '1.7', 'stroke-linecap': 'round', 'stroke-linejoin': 'round', 'aria-hidden': 'true', focusable: 'false' })) svg.setAttribute(key, value);
    const path = document.createElementNS(svg.namespaceURI, 'path'); path.setAttribute('d', paths[icon]);
    svg.append(path); control.append(svg, label);
    control.classList.add('layout-icon-control');
    control.setAttribute('aria-label', name); control.title = name;
  }
  for (const control of sidebar.querySelectorAll('[data-view]')) {
    decorate(control, control.dataset.view); control.classList.add('layout-view-button');
    document.querySelector(`[data-panel="${control.dataset.view}"]`).setAttribute('aria-label', titles[control.dataset.view]);
  }
  for (const [id, icon] of [['voice-start', 'microphone'], ['voice-pause', 'pause'], ['compact', 'compact']]) decorate($(id), icon);
  $('view-title').parentElement.classList.add('layout-sr-only');
  $('view-title').setAttribute('aria-live', 'polite'); $('view-title').setAttribute('aria-atomic', 'true');
  $('vega-title').classList.add('layout-sr-only');
  const tools = node('div', undefined, 'layout-sidebar-tools'), toggle = node('button', 'Collapse sidebar', 'layout-sidebar-toggle');
  toggle.type = 'button'; sidebar.id ||= 'vega-layout-sidebar';
  toggle.setAttribute('aria-controls', sidebar.id); decorate(toggle, 'sidebar');
  tools.append(toggle); sidebar.prepend(tools);
  let collapsed = true;
  const render = () => {
    workspace.dataset.sidebarCollapsed = String(collapsed);
    const label = collapsed ? 'Expand sidebar' : 'Collapse sidebar';
    toggle.querySelector('.layout-control-label').textContent = label;
    toggle.setAttribute('aria-label', label); toggle.title = label; toggle.setAttribute('aria-expanded', String(!collapsed));
    $('compact').setAttribute('aria-pressed', String(document.body.classList.contains('compact-mode')));
    $('presence-state').parentElement.title = $('presence-state').textContent;
    $('connection').parentElement.title = $('connection').textContent;
  };
  toggle.addEventListener('click', () => {
    collapsed = !collapsed; render();
  });
  sidebar.querySelector('nav').addEventListener('click', event => {
    if (event.target.closest('[data-view]')) { collapsed = true; render(); }
  });
  document.addEventListener('pointerdown', event => {
    if (!collapsed && !sidebar.contains(event.target)) { collapsed = true; render(); }
  });
  sidebar.addEventListener('keydown', event => {
    if (event.key === 'Escape' && !collapsed) { collapsed = true; render(); toggle.focus(); }
  });
  const hints = new MutationObserver(render);
  for (const id of ['presence-state', 'connection']) hints.observe($(id), { childList: true, characterData: true, subtree: true });
  hints.observe(document.body, { attributes: true, attributeFilter: ['class'] });
  render(); workspace.dataset.activeView = view; workspace.classList.add('layout-refined', 'floating-shell');
}
async function loadSpeakerProfile() {
  if (!host?.speech?.owner_voice_required) return;
  if (!speakerProfilePanel) {
    const { SpeakerProfilePanel } = await import('./speaker-profile.js');
    // Opening Settings and the disclosure can both arrive during the import.
    // Re-check after that await so both callers share one panel and capture owner.
    speakerProfilePanel ??= new SpeakerProfilePanel($('owner-voice-workspace'), {
      get, post,
      onMicrophoneChanged: deviceId => voice.changeMicrophone(deviceId),
      onBeforeRecording: async () => { profileRecording = true; voice.stop(); },
      onAfterRecording: async () => {
        try { await loadCompanion({ force: true }); }
        finally { profileRecording = false; }
        void voice.maybeAutoStart({ retry: true });
      },
    });
  }
  await speakerProfilePanel.load();
}
async function loadBriefing() {
  const data = await get('/api/briefing');
  const { briefing } = applyVegaBriefingAcknowledgements(data.briefing, data.acknowledgements);
  $('briefing-detail').textContent = briefing.detail;
  const cards = $('briefing-cards'); cards.replaceChildren();
  for (const item of briefing.items) {
    const card = node('article', undefined, 'card');
    card.append(node('span', item.kind, 'eyebrow'), node('h3', item.title), node('p', item.detail));
    const actions = node('div', undefined, 'toolbar');
    actions.append(button(item.actionLabel, () => {
      const kind = item.action.kind;
      return selectView(kind.includes('calendar') || kind.includes('reminder') ? 'planning' : kind.includes('settings') ? 'settings' : 'conversation');
    }), button('Mark reviewed', async () => {
      const previous = data.acknowledgements.find(value => value.signalKey === item.signalKey);
      await post('/api/briefing/ack', { signalKey: item.signalKey, revision: item.revision, expectedAcknowledgedRevision: previous?.revision || null });
      await loadBriefing();
    }));
    card.append(actions); cards.append(card);
  }
  if (!briefing.items.length) cards.append(node('p', 'Nothing new in the scopes you selected.', 'empty'));
}
function fillPreferences() {
  const form = $('preferences'), values = companion.preferences.values;
  $('memory-learning-controls').hidden = !companion.selective_memory_available || !Object.prototype.hasOwnProperty.call(values, 'learning_enabled');
  for (const [key, value] of Object.entries(values)) {
    const field = form.elements.namedItem(key);
    if (field) field.type === 'checkbox' ? field.checked = value : field.value = value;
  }
  const scopes = $('briefing-scopes'); scopes.replaceChildren();
  for (const [name, enabled] of Object.entries(values.briefing_scopes)) {
    const label = node('label', undefined, 'check'), input = node('input');
    input.type = 'checkbox'; input.setAttribute('role', 'switch'); input.name = 'scope_' + name; input.checked = enabled;
    label.append(input, document.createTextNode(name[0].toUpperCase() + name.slice(1))); scopes.append(label);
  }
}
async function loadMemory() {
  if (host?.selective_memory_available || companion?.selective_memory_available) {
    $('memory-legacy').hidden = true; $('memory-workspace').hidden = false;
    if (!memoryPanel) {
      memoryPanel = new MemoryPanel({
        root: $('memory-workspace'), get, post,
        onEdit: openMemory, onAdd: () => openMemory(), onSettings: () => selectView('settings'),
      });
      memoryPanel.scopeSelect.value = 'all';
    }
    memoryLoading = true;
    try { const loaded = await memoryPanel.load(); if (loaded) memoryRevision = host?.memory_revision; }
    finally { memoryLoading = false; }
    return;
  }
  const params = new URLSearchParams({ q: $('memory-search').value });
  const memories = await get('/api/memory?' + params);
  $('context-description').textContent = 'Vega finds relevant memories and project context from your conversation, using your preferences.';
  const cards = $('memory-cards'); cards.replaceChildren();
  for (const memory of memories) {
    const card = node('article', undefined, 'card');
    card.append(node('span', memory.project ? companion?.projects?.[memory.project] || memory.project : 'Personal', 'eyebrow'), node('h3', memory.title), node('p', memory.body), button('Review / edit', () => openMemory(memory)));
    cards.append(card);
  }
  if (!memories.length) cards.append(node('p', 'No matching notes in this scope. Add something you want Vega to remember.', 'empty'));
}
function openMemory(memory = null) {
  selectedMemory = memory;
  const form = $('memory-form'); form.reset();
  form.elements.id.value = memory?.id || ''; form.elements.expected_revision.value = memory?.revision || 0;
  form.elements.title.value = memory?.title || ''; form.elements.body.value = memory?.body || '';
  setOptions(form.elements.project, memoryEditorProject(memory));
  $('archive-memory').hidden = !memory;
  $('memory-dialog').querySelector('.dialog-error')?.remove();
  $('memory-dialog').showModal();
}
async function loadPlanning() {
  plans = await get('/api/planning');
  const range = calendarViewRange(calendarView, anchor);
  const allEvents = await get(`/api/calendar?from=${range.start}&to=${range.end}`);
  const events = allEvents;
  $('calendar-title').textContent = new Date(anchor).toLocaleDateString(undefined, { month: 'long', year: 'numeric' });
  const days = calendarView === 'month' ? calendarGridDays(anchor) : calendarView === 'week' ? calendarWeekDays(anchor) : calendarView === 'agenda' ? calendarAgendaDays(anchor) : [anchor];
  const calendar = $('calendar'); calendar.replaceChildren(); calendar.className = 'calendar ' + calendarView;
  for (const day of days) {
    const matches = calendarEventsForDay(events, day);
    if (calendarView === 'agenda' && !matches.length) continue;
    const cell = node('div', undefined, 'calendar-day' + (localDateValue(day) === localDateValue(Date.now()) ? ' today' : ''));
    cell.append(node('div', new Date(day).toLocaleDateString(undefined, { weekday: 'short', day: 'numeric' }), 'day-number'));
    for (const event of matches) {
      const item = button((event.all_day ? '' : new Date(event.start_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) + ' ') + event.title, () => openPlan(plans.find(value => value.id === event.id)));
      item.className = 'calendar-event'; item.draggable = true;
      item.addEventListener('dragstart', () => { dragged = { event, day }; });
      item.addEventListener('dragend', () => { dragged = null; });
      cell.append(item);
    }
    cell.addEventListener('dragover', event => { if (dragged) { event.preventDefault(); cell.classList.add('drag-target'); } });
    cell.addEventListener('dragleave', () => cell.classList.remove('drag-target'));
    cell.addEventListener('drop', event => {
      event.preventDefault(); cell.classList.remove('drag-target');
      if (!dragged) return;
      const original = plans.find(item => item.id === dragged.event.id);
      openPlan({ ...original, ...moveCalendarSeriesToDay(dragged.event, dragged.day, day) });
      $('plan-staged').textContent = 'Move staged. Review the new dates and save.';
      dragged = null;
    });
    calendar.append(cell);
  }
  if (!calendar.children.length) calendar.append(node('p', 'No events in this period.', 'empty'));
  const reminders = $('reminders'); reminders.replaceChildren();
  for (const reminder of plans.filter(item => item.kind === 'reminder').sort((a,b) => a.dueAt - b.dueAt)) {
    const card = node('article', undefined, 'card');
    card.append(node('h3', reminder.title), node('p', (reminder.completedAt ? 'Completed · ' : '') + new Date(reminder.dueAt).toLocaleString()));
    const actions = node('div', undefined, 'toolbar');
    actions.append(button(reminder.completedAt ? 'Reopen' : 'Complete', async () => {
      await post('/api/planning/complete', { id: reminder.id, expected_revision: reminder.revision, completed: !reminder.completedAt }); await loadPlanning();
    }), button('Review / edit', () => openPlan(reminder)));
    card.append(actions); reminders.append(card);
  }
  if (!reminders.children.length) reminders.append(node('p', 'No reminders in this scope.', 'empty'));
}
function planKind() {
  const reminder = $('plan-form').elements.kind.value === 'reminder';
  $('event-fields').hidden = reminder; $('end-label').hidden = reminder;
  $('plan-form').elements.end.required = !reminder;
}
function openPlan(plan = null) {
  selectedPlan = plan;
  const form = $('plan-form'); form.reset(); $('plan-staged').textContent = '';
  for (const key of ['id','title','note','kind','recurrence']) if (plan?.[key] !== undefined) form.elements.namedItem(key).value = plan[key];
  form.elements.expected_revision.value = plan?.revision || 0;
  form.elements.start.value = localDateTimeValue(plan?.dueAt || plan?.startAt || Date.now());
  form.elements.end.value = localDateTimeValue(plan?.endAt || Date.now() + 3600000);
  form.elements.allDay.checked = Boolean(plan?.allDay);
  form.elements.recurrenceUntil.value = plan?.recurrenceUntil ? localDateValue(plan.recurrenceUntil) : '';
  setOptions(form.elements.projectId, plan?.projectId);
  $('archive-plan').hidden = !plan; $('plan-dialog').querySelector('.dialog-error')?.remove(); planKind(); $('plan-dialog').showModal();
}
function listen(id, event, action) {
  $(id).addEventListener(event, e => {
    if (event === 'submit') e.preventDefault();
    void Promise.resolve().then(() => action(e)).catch(error => fail(error.message));
  });
}
const gettingStarted = $('getting-started-dialog');
const gettingStartedKey = 'vega-getting-started-v1';
function openGettingStarted() {
  if (!gettingStarted.open) gettingStarted.showModal();
}
gettingStarted.addEventListener('close', () => {
  try { localStorage.setItem(gettingStartedKey, 'seen'); }
  catch (error) { console.warn('Vega could not remember the getting-started guide state.', error); }
});
listen('open-getting-started', 'click', openGettingStarted);
listen('getting-started-projects', 'click', async () => {
  gettingStarted.close();
  await selectView('work');
});
listen('getting-started-settings', 'click', async () => {
  gettingStarted.close();
  await selectView('settings');
});
try {
  if (localStorage.getItem(gettingStartedKey) !== 'seen') openGettingStarted();
} catch (error) {
  console.warn('Vega could not read the getting-started guide state.', error);
  openGettingStarted();
}
listen('composer','submit', e => { e.preventDefault(); return send($('message').value); });
listen('message','input', () => { saveDraft(); renderComposer(); });
$('message').addEventListener('keydown', e => { if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); void send($('message').value).catch(error => fail(error.message)); } });
listen('cancel-reply','click', async () => { await post('/api/chat/cancel', { update_id: activeReply }); activeReply = null; voice.stop(); await refresh(); void voice.maybeAutoStart({ retry: true }); });
listen('voice-start','click', () => {
  fail('');
  if (voice.activationNeeded) { voice.resumeAudioContext(); return; }
  saveVoicePause(false); voice.start();
});
listen('voice-pause','click', () => { saveVoicePause(true); voice.pause(); });
listen('voice-owner-setup','click', async () => {
  $('voice-settings').open = true;
  await selectView('settings');
  $('owner-voice-workspace').scrollIntoView({ block: 'start' });
});
listen('voice-settings','toggle', () => {
  if ($('voice-settings').open && view === 'settings') return loadSpeakerProfile();
});
listen('compact','click', () => document.body.classList.toggle('compact-mode'));
document.querySelectorAll('[data-view]').forEach(item => item.addEventListener('click', () => void selectView(item.dataset.view).catch(error => fail(error.message))));
document.querySelectorAll('[data-command]').forEach(item => item.addEventListener('click', () => void selectView('conversation').then(() => send(item.dataset.command)).catch(error => fail(error.message))));
document.querySelectorAll('[data-close]').forEach(item => item.addEventListener('click', () => $(item.dataset.close).close()));
listen('new-memory','click', () => openMemory());
listen('memory-search','input', () => loadMemory());
listen('memory-form','submit', async e => {
  e.preventDefault(); const data = Object.fromEntries(new FormData(e.target)); data.project ||= null; data.expected_revision = Number(data.expected_revision);
  await post('/api/memory', data); $('memory-dialog').close(); await loadMemory();
});
listen('archive-memory','click', async () => { await post('/api/memory/archive', { id: selectedMemory.id, expected_revision: selectedMemory.revision }); $('memory-dialog').close(); await loadMemory(); });
listen('export-context','click', async () => {
  const data = await get('/api/export'), url = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' }));
  const link = node('a'); link.href = url; link.download = 'vega-context.json'; link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
});
listen('preferences','submit', async e => {
  e.preventDefault(); const form = e.target, values = structuredClone(companion.preferences.values);
  const wasAutomatic = values.background_listening;
  for (const key of Object.keys(values)) {
    if (key === 'briefing_scopes') { for (const name of Object.keys(values[key])) values[key][name] = form.elements.namedItem('scope_' + name).checked; }
    else { const field = form.elements.namedItem(key); if (field) values[key] = field.type === 'checkbox' ? field.checked : field.value; }
  }
  await post('/api/preferences', { values, expected_revision: companion.preferences.revision });
  if (!wasAutomatic && values.background_listening) { saveVoicePause(false); voice.pausedByOwner = false; }
  voice.stop(); await loadCompanion({ force: true }); $('preference-result').textContent = 'Preferences saved.';
});
listen('reload-preferences','click', () => loadCompanion({ force: true }));
listen('new-plan','click', () => openPlan());
$('plan-form').elements.kind.addEventListener('change', planKind);
listen('plan-form','submit', async e => {
  e.preventDefault(); const form = e.target, data = Object.fromEntries(new FormData(form));
  const start = new Date(data.start).getTime(), end = new Date(data.end).getTime();
  const payload = { id: data.id, expected_revision: Number(data.expected_revision), kind: data.kind, title: data.title, note: data.note,
    projectId: data.projectId || null, completedAt: selectedPlan?.completedAt || null, sourceUrl: selectedPlan?.sourceUrl || null,
    dueAt: start, startAt: start, endAt: end, timeZone: companion.preferences.values.timezone, allDay: form.elements.allDay.checked,
    recurrence: data.recurrence, recurrenceUntil: data.recurrenceUntil ? new Date(data.recurrenceUntil + 'T23:59:59').getTime() : null };
  await post('/api/planning', payload); $('plan-dialog').close(); await loadPlanning();
});
listen('archive-plan','click', async () => { await post('/api/planning/archive', { id: selectedPlan.id, expected_revision: selectedPlan.revision }); $('plan-dialog').close(); await loadPlanning(); });
document.querySelectorAll('[data-calendar-view]').forEach(item => item.addEventListener('click', () => {
  calendarView = item.dataset.calendarView;
  document.querySelectorAll('[data-calendar-view]').forEach(button => button.setAttribute('aria-pressed', String(button === item)));
  void loadPlanning().catch(error => fail(error.message));
}));
listen('calendar-prev','click', () => { anchor = moveCalendarAnchor(anchor, calendarView, -1); return loadPlanning(); });
listen('calendar-next','click', () => { anchor = moveCalendarAnchor(anchor, calendarView, 1); return loadPlanning(); });
listen('calendar-today','click', () => { anchor = Date.now(); return loadPlanning(); });
window.addEventListener('pagehide', () => {
  workActivity?.destroy(); workActivity = null;
  workOverview?.destroy(); workOverview = null; n8nPanel?.destroy(); n8nPanel = null;
  infrastructurePanel?.destroy(); infrastructurePanel = null;
  void speakerProfilePanel?.dispose(); voice.stop();
});
window.addEventListener('pageshow', event => {
  if (event.persisted) {
    mountWorkActivity();
    if (['work', 'automations'].includes(view)) void selectView(view);
    void loadCompanion({ force: true }).catch(error => fail(error.message));
    void refresh();
  }
});
document.addEventListener('pointerdown', () => voice.resumeAudioContext(), { passive: true });
document.addEventListener('keydown', () => voice.resumeAudioContext());
initializeConversationLayout();
mountWorkActivity();
void import('./reply-renderer.js').then(module => {
  renderReplyBody = module.renderReplyBody;
  if (history.length) renderHistory();
}).catch(() => { /* Keep the complete plain-text reply if formatting cannot load. */ });
try {
  $('message').value = sessionStorage.getItem('vega-draft') || '';
  const saved = JSON.parse(sessionStorage.getItem('vega-pending') || 'null');
  if (saved && typeof saved.text === 'string' && typeof saved.request_id === 'string') {
    pending = { text: saved.text, request_id: saved.request_id, ...(saved.voice_receipt ? { voice_receipt: saved.voice_receipt } : {}),
      ...(Array.isArray(saved.attachment_ids) && saved.attachment_ids.length ? { attachment_ids: saved.attachment_ids } : {}) };
    pendingUncertain = true;
    if ($('message').value && $('message').value !== pending.text) sessionStorage.setItem('vega-next-draft', $('message').value);
    $('message').value = pending.text;
    $('send-status').textContent = 'A previous send was not confirmed. Retry to check it.';
  }
} catch {}
composerAttachments = initComposerAttachments({
  composer: $('composer'), list: $('composer-attachments'), picker: $('attachment-picker'),
  addButton: $('attach-files'), status: $('attachment-status'), post, enabled: attachmentsAvailable, onChange: renderComposer,
});
renderComposer();
document.dispatchEvent(new Event('vega:ready'));
function startDashboardRefresh() {
  setInterval(() => { if (!document.hidden || activeReply || voice.state.phase !== 'idle') void refresh(); }, 4000);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) void refresh(); });
  void refresh();
}
startDashboardRefresh();
