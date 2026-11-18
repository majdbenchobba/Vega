import { requestLabel, requestSummary, referenceTokens } from '../request-receipts.js';
import { initWorkspace } from './workspace.js';
import { icon, projectMark } from './visuals.js';
export { initWorkspace };

const SECTIONS = [
  ['done', 'Done', '✓', 'Nothing marked done yet.'],
  ['working', 'Working on', '◉', 'No current work reported.'],
  ['next', 'Next', '→', 'No next steps reported.'],
  ['blockers', 'Blockers', '!', 'No blockers reported.'],
  ['suggestions', 'Suggestions', '✧', 'No suggestions reported.'],
];
const RESPONSE_NOTES = {
  delivered: 'The server reports delivery to Telegram. This does not confirm that you read the response.',
  queued: 'The response is queued. Complete delivery has not been recorded.',
  partially_delivered: 'Only part of this response has been delivered. Review the remaining part receipts.',
  failed: 'Delivery failed. Review the error and individual part receipts below.',
  uncertain: 'Delivery could not be confirmed. A routing receipt is not proof of response delivery.',
  suppressed: 'Delivery was suppressed. This response is not reported as delivered.',
};

const REOPEN = 'Close this Mini App and reopen it from the paired Telegram account.';
const valueText = value => typeof value === 'string' ? value : JSON.stringify(value, null, 2) ?? '';
const hasText = value => typeof value === 'string' && value.trim().length > 0;
const routeUnavailable = project => project?.connection?.can_route === false;
const segment = value => {
  if (!hasText(value) || value === '.' || value === '..') throw new Error('The server returned an invalid project or request identifier.');
  return encodeURIComponent(value);
};
const webApp = telegram => telegram?.WebApp ?? telegram;
export function miniActivityPath(path) {
  const reference = path.match(/^\/api\/team\/requests\/([a-f0-9-]{36})\/reference\?ref=([^&]+)$/);
  return reference ? `/mini/api/requests/${reference[1]}/references/${reference[2]}`
    : path.replace('/api/team', '/mini/api/team');
}

export class ApiError extends Error {
  constructor(message, status = 0) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
  }
}

/** The only production transport. Authentication stays in this request header. */
export function createApi({ telegram, fetch: fetchImpl = globalThis.fetch, timeoutMs = 20000 } = {}) {
  return async (path, { method = 'GET', body } = {}) => {
    const initData = webApp(telegram)?.initData;
    if (!hasText(initData)) throw new ApiError(REOPEN, 401);
    if (!/^\/mini\/api\/(?:workspace|chats\/[a-f0-9-]{36}|chat-prompts|chat-attachments(?:\/[a-f0-9-]{36}\/remove)?|projects(?:\/[^/?#]+)?|requests(?:\/[^/?#]+(?:\/(?:cancel|answer|steer|references\/[^/?#]+))?)?|team(?:\/projects\/[a-z0-9-]+|\/runs(?:\/[a-f0-9-]{36}(?:\/(?:cancel|apply))?)?)?)$/.test(path)) {
      throw new ApiError('Unsupported Mini App endpoint.');
    }
    if (method !== 'GET' && method !== 'POST') throw new ApiError('Unsupported request method.');
    if (path.includes('/chat-attachments') && method !== 'POST') throw new ApiError('Use an explicit attachment action.');
    if ((path.endsWith('/workspace') || path.includes('/chats/')) && method !== 'GET') throw new ApiError('Chat reads are read-only.');
    if (path.includes('/references/') && method !== 'GET') throw new ApiError('Reference previews are read-only.');
    if (path.startsWith('/mini/api/team/projects/') && method !== 'GET') throw new ApiError('Project activity is read-only.');
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), path === '/mini/api/chat-attachments' ? Math.max(timeoutMs, 60000) : timeoutMs);
    try {
      const response = await fetchImpl(path, {
        method,
        headers: {
          Accept: 'application/json',
          'X-Telegram-Init-Data': initData,
          ...(body === undefined ? {} : { 'Content-Type': 'application/json' }),
        },
        ...(body === undefined ? {} : { body: JSON.stringify(body) }),
        credentials: 'omit',
        cache: 'no-store',
        redirect: 'error',
        referrerPolicy: 'no-referrer',
        signal: controller.signal,
      });
      let data;
      try { data = await response.json(); } catch {
        throw new ApiError(
          response.status === 401 || response.status === 403 ? REOPEN : 'The server did not return a readable receipt.',
          response.status,
        );
      }
      if (!response.ok) {
        throw new ApiError(hasText(data?.error) ? data.error : `The server returned HTTP ${response.status}.`, response.status);
      }
      if (!data || typeof data !== 'object' || Array.isArray(data)) throw new ApiError('The server returned an invalid response.');
      return data;
    } catch (error) {
      if (error instanceof ApiError) throw error;
      throw new ApiError(controller.signal.aborted
        ? 'The connection timed out. The server may still have received the request.'
        : 'The connection failed. Check your connection and refresh receipts.');
    } finally {
      clearTimeout(timer);
    }
  };
}

function checkProject(project) {
  if (!project || !hasText(project.alias) || !hasText(project.thread_id) || !hasText(project.label)
      || !project.checklist || SECTIONS.some(([key]) => !Array.isArray(project.checklist[key]))) {
    throw new ApiError('The server returned an incomplete project. Refresh to try again.');
  }
  segment(project.alias);
  return project;
}

function checkRequest(request) {
  if (!request || !hasText(request.id) || !hasText(request.state)
      || !Number.isSafeInteger(request.revision) || request.revision < 0
      || typeof request.text !== 'string' || typeof request.can_cancel !== 'boolean'
      || !Array.isArray(request.targets)
      || request.targets.some(target => !target || !hasText(target.alias) || !hasText(target.thread_id))
      || !Array.isArray(request.routes) || request.routes.some(route => !route || typeof route !== 'object')
      || (request.question != null && (!Array.isArray(request.question.fields)
        || request.question.fields.some(field => !field || !['targets', 'text'].includes(field.name)
          || !hasText(field.label) || field.required !== true)))) {
    throw new ApiError('The server returned an incomplete request receipt. Refresh receipts before retrying.');
  }
  segment(request.id);
  return request;
}

function matchesAttempt(request, attempt) {
  return request.text === attempt.text && request.targets.length === 1
    && request.targets[0].alias === attempt.target.alias
    && request.targets[0].thread_id === attempt.target.thread_id;
}

const sameTarget = (left, right) => left.alias === right.alias && left.thread_id === right.thread_id;
const DEFAULT_STALE_SECONDS = 1800;
const FUTURE_TOLERANCE_SECONDS = 30;
const FRESHNESS_LABELS = {
  fresh: 'Recent report', stale: 'Stale report', error: 'Refresh unavailable', not_reported: 'Not reported',
};

export function timestampSeconds(value) {
  if (value == null || value === '' || typeof value === 'boolean') return null;
  let numeric;
  if (typeof value === 'number' || typeof value === 'string' && /^\d+(?:\.\d+)?$/.test(value.trim())) {
    numeric = Number(value);
    if (numeric >= 1e12) numeric /= 1000;
  } else if (typeof value === 'string' && /(?:Z|[+-]\d\d:\d\d)$/i.test(value)) {
    numeric = Date.parse(value) / 1000;
  }
  return Number.isFinite(numeric) && numeric > 0 ? numeric : null;
}

/** A successful HTTP read or a working thread does not make an old report fresh. */
export function projectFreshness(project, { now = Date.now() / 1000, error = '', offline = false } = {}) {
  const supplied = project?.freshness;
  const rawEvidence = supplied && Object.hasOwn(supplied, 'evidence_at')
    ? supplied.evidence_at : project?.checklist_observed_at;
  let evidenceAt = timestampSeconds(rawEvidence);
  let checkedAt = timestampSeconds(supplied?.checked_at);
  const threshold = timestampSeconds(supplied?.stale_after_seconds);
  const staleAfter = threshold && threshold <= 604800 ? threshold : DEFAULT_STALE_SECONDS;
  const invalidTime = rawEvidence != null && evidenceAt == null
    || supplied?.checked_at != null && checkedAt == null
    || evidenceAt != null && evidenceAt > now + FUTURE_TOLERANCE_SECONDS
    || checkedAt != null && checkedAt > now + FUTURE_TOLERANCE_SECONDS;
  if (evidenceAt > now + FUTURE_TOLERANCE_SECONDS) evidenceAt = null;
  if (checkedAt > now + FUTURE_TOLERANCE_SECONDS) checkedAt = null;
  let state;
  let reason = '';
  if (offline || error || supplied?.error || supplied?.state === 'error' || invalidTime) {
    state = 'error';
    reason = offline ? 'You’re offline. Last known information is shown.'
      : error || (hasText(supplied?.error) ? supplied.error : invalidTime
        ? 'The source timestamp is invalid. Freshness could not be verified.'
        : 'The source could not be refreshed. Last known information is shown.');
  } else if (evidenceAt == null || supplied?.state === 'not_reported') {
    state = 'not_reported';
    reason = 'No dated project report has been provided. Thread activity is shown separately.';
  } else if (!supplied || supplied.state !== 'fresh' || checkedAt == null
      || checkedAt + FUTURE_TOLERANCE_SECONDS < evidenceAt
      || now - evidenceAt > staleAfter || now - checkedAt > staleAfter) {
    state = 'stale';
    reason = checkedAt == null ? 'Source check not reported. The retained report has not been verified as current.'
      : 'Showing the last known report. No recent source evidence is available.';
  } else {
    state = 'fresh';
    reason = 'Report age is within the freshness window. It does not confirm project completion.';
  }
  return { state, label: FRESHNESS_LABELS[state], evidenceAt, checkedAt, staleAfter, reason };
}

function displayTime(value) {
  if (value == null || value === '') return 'Not reported';
  const numeric = typeof value === 'number' ? value : /^\d+(?:\.\d+)?$/.test(String(value)) ? Number(value) : null;
  const date = new Date(numeric === null ? value : numeric < 1e12 ? numeric * 1000 : numeric);
  return Number.isFinite(date.getTime()) ? date.toLocaleString() : 'Not reported';
}
function matchesAnswer(request, attempt) {
  return request.revision > attempt.revision && request.state !== 'awaiting_input'
    && request.text === attempt.text && request.targets.length === attempt.targets.length
    && attempt.targets.every(target => request.targets.some(other => sameTarget(target, other)));
}

/**
 * Mount a Mini App. Fixtures may inject api(path, {method, body}) => parsed JSON
 * and telegram (WebApp or {WebApp}); neither is read from URLs or page storage.
 * Only explicit localMode: true together with an injected API permits an unsigned
 * desktop mount. Its private host supplies authentication through that adapter.
 * The public bootstrap and createApi always require Telegram initData and send
 * it to the backend for signature validation.
 * root/document/window/crypto and pollIntervalMs allow isolated, dependency-free
 * DOM fixtures. Call destroy() when replacing a mounted fixture.
 */
export function init(options = {}) {
  const doc = options.document ?? options.root?.ownerDocument ?? globalThis.document;
  const root = options.root ?? doc?.getElementById('miniapp');
  if (!root) throw new Error('A Mini App root is required.');
  const win = options.window ?? doc.defaultView ?? globalThis;
  const localMode = options.localMode === true && typeof options.api === 'function';
  const telegram = localMode ? undefined : webApp(options.telegram ?? win.Telegram?.WebApp);
  const api = typeof options.api === 'function' ? options.api : createApi({ telegram, fetch: options.fetch ?? win.fetch?.bind(win) });
  const reopenMessage = localMode ? 'Reload the private desktop Projects page to reconnect to Vega.' : REOPEN;
  const crypto = options.crypto ?? win.crypto ?? globalThis.crypto;
  const pollIntervalMs = options.pollIntervalMs ?? 15000;
  const now = options.now ?? (() => Date.now() / 1000);
  const cleanups = [];
  const projects = new Map();
  const details = new Map();
  const drafts = new Map();
  const answerDrafts = new Map();
  const steerDrafts = new Map();
  const requests = new Map();
  const requestSnapshots = new Map();
  const homeReceipts = new Set();
  const receiptUI = new Map();
  const openReceipts = new Set();
  const referenceUI = new Map();
  const projectLoads = new Map();
  const projectSnapshots = new Map();
  let access = localMode || hasText(telegram?.initData) ? 'active' : 'missing';
  let online = win.navigator?.onLine !== false;
  let hostActive = telegram?.isActive !== false;
  let selected = null;
  let destroyed = false;
  let projectsLoaded = false;
  let projectListPromise = null;
  let requestListPromise = null;
  let requestsLoaded = false;
  let requestListError = '';
  let listError = '';
  let authError = '';
  let workPaused = false;
  let intervalMinutes = null;
  let timer = null;
  let activityPanel = null, activityLoading = false, activityGeneration = 0;
  let readSequence = 0;
  let listSnapshot = null;
  let listCheckedAt = null;
  let serverClock = null;

  const visible = () => doc.visibilityState !== 'hidden' && hostActive;
  const sourceNow = () => serverClock ? serverClock.serverAt + Math.max(0, now() - serverClock.localAt) : now();
  function responseSnapshot(data, sequence) {
    const snapshotAt = timestampSeconds(data?.snapshot_at);
    if (snapshotAt != null && (!serverClock || snapshotAt > serverClock.serverAt)) {
      serverClock = { serverAt: snapshotAt, localAt: now() };
    }
    return { snapshotAt, sequence, receivedAt: now() };
  }
  function isNewerSnapshot(next, previous) {
    if (!previous) return true;
    if (next.snapshotAt != null && previous.snapshotAt != null && next.snapshotAt !== previous.snapshotAt) {
      return next.snapshotAt > previous.snapshotAt;
    }
    if (next.snapshotAt == null && previous.snapshotAt != null) return false;
    return next.sequence >= previous.sequence;
  }
  function acceptProject(next, snapshot) {
    const alias = next.alias;
    const previous = projects.get(alias);
    const previousSnapshot = projectSnapshots.get(alias);
    if (!isNewerSnapshot(snapshot, previousSnapshot)) return false;
    if (!projects.has(alias) && projectsLoaded && listSnapshot && !isNewerSnapshot(snapshot, listSnapshot)) return false;
    const sameSource = previous && previous.thread_id === next.thread_id
      && previous.watch_thread_id === next.watch_thread_id;
    if (sameSource && next.latest_reply && previous.latest_reply
        && next.latest_reply.observed_at === previous.latest_reply.observed_at
        && typeof next.latest_reply.text !== 'string' && typeof previous.latest_reply.text === 'string') {
      next = {...next, latest_reply:{...next.latest_reply,text:previous.latest_reply.text}};
    }
    const previousTime = projectFreshness(previous, { now: sourceNow() }).evidenceAt;
    const nextTime = projectFreshness(next, { now: sourceNow() }).evidenceAt;
    if (sameSource && previousTime != null && (nextTime == null || nextTime < previousTime)) {
      // A newer HTTP snapshot can still contain an older or unavailable source.
      // Keep the original report, explicitly marked unverified, alongside the
      // new thread activity. Never promote its timestamp to the read time.
      const retained = projectFreshness(previous, { now: sourceNow() });
      next = {
        ...next, checklist: previous.checklist, checklist_observed_at: previous.checklist_observed_at,
        checklist_source: previous.checklist_source, evidence: previous.evidence,
        freshness: {
          state: 'error', evidence_at: retained.evidenceAt,
          checked_at: timestampSeconds(next.freshness?.checked_at),
          stale_after_seconds: retained.staleAfter,
          error: nextTime == null
            ? 'The latest source omitted its report. The last known report is retained.'
            : 'The latest source returned an older report. The newer known report is retained.',
        },
      };
    }
    projects.set(alias, next);
    projectSnapshots.set(alias, snapshot);
    const detail = details.get(alias);
    if (detail) {
      detail.project = next;
      if (!sameSource) detail.verified = false;
    }
    return true;
  }

  function freshness(project, error = '') {
    return projectFreshness(project, { now: sourceNow(), error, offline: !online });
  }
  function freshnessBadge(value) {
    return element('span', 'freshness-badge', value.label, { 'data-freshness': value.state });
  }
  function assessmentView(project, compact = false) {
    const assessment = project?.progress_assessment;
    if (hasText(project?.progress_assessment_error)) {
      const section = element(compact ? 'span' : 'section', compact ? 'assessment-preview' : 'progress-assessment');
      section.append(element(compact ? 'span' : 'h2', '', 'Progress unavailable'),
        element(compact ? 'span' : 'p', 'hint', project.progress_assessment_error));
      return section;
    }
    if (!assessment || typeof assessment !== 'object' || !hasText(assessment.display_progress)) return null;
    const section = element(compact ? 'span' : 'section', compact ? 'assessment-preview' : 'progress-assessment');
    const reportedDate = timestampSeconds(assessment.assessed_at);
    const invalidDate = reportedDate != null && reportedDate > sourceNow() + 30;
    const dated = invalidDate ? null : reportedDate;
    const unavailable = assessment.stale || assessment.source_error || project.progress_source_error
      || invalidDate || !online || !!listError || !!details.get(project.alias)?.error;
    const aged = assessment.automatic && dated != null && sourceNow() - dated > 15 * 60;
    const kind = unavailable || aged ? 'Last known'
      : assessment.automatic ? 'Auto-updated'
      : assessment.kind === 'measured' ? 'Measured'
      : assessment.kind === 'estimate' || assessment.display_progress.startsWith('≈') ? 'Estimate' : 'Reported';
    section.append(element(compact ? 'span' : 'h2', 'progress-title', `Progress: ${assessment.display_progress}`));
    if (typeof assessment.percent === 'number' && Number.isFinite(assessment.percent)
        && assessment.percent >= 0 && assessment.percent <= 100) {
      section.append(element('progress', 'project-progress-bar', null, {
        max:'100', value:String(assessment.percent),
        'aria-label':`${assessment.scope || project.label || 'Project'} progress`,
      }));
    }
    section.append(element(compact ? 'span' : 'p', 'hint progress-caption',
      `${kind} · Updated: ${dated == null ? 'Not reported' : displayTime(dated)}`));
    if (!compact) {
      if (assessment.basis_and_remaining != null) section.append(element('p', 'assessment-detail', valueText(assessment.basis_and_remaining)));
      if (assessment.automatic && assessment.summary) section.append(element('p', 'assessment-detail', assessment.summary));
      if (unavailable || aged) section.append(element('p', 'hint', 'Waiting for a fresh source update.'));
      if (assessment.source != null) {
        const source = typeof assessment.source === 'string' ? assessment.source
          : assessment.source.path ?? assessment.source.label ?? 'Registered project report';
        const detail = element('details', 'progress-source');
        detail.append(element('summary', '', 'Source'), element('p', 'hint', `Source: ${valueText(source)}`));
        section.append(detail);
      }
    }
    return section;
  }

  function element(tag, className = '', text = null, attributes = {}) {
    const node = doc.createElement(tag);
    if (className) node.className = className;
    if (text !== null) node.textContent = text;
    for (const [key, value] of Object.entries(attributes)) node.setAttribute(key, value);
    return node;
  }
  function text(node, value) {
    const next = String(value ?? '');
    if (node.textContent !== next) node.textContent = next;
  }
  function listen(node, event, handler) {
    node.addEventListener(event, handler);
    cleanups.push(() => node.removeEventListener(event, handler));
  }
  function button(label, action, className = '', focusKey = '') {
    const node = element('button', className, label, { type: 'button' });
    if (focusKey) node.dataset.focusKey = focusKey;
    // Detached cards can be collected with their listeners on each render.
    node.addEventListener('click', action);
    return node;
  }
  function safeTelegram(method, ...args) {
    try { if (typeof telegram?.[method] === 'function') telegram[method](...args); } catch { /* Optional host enhancement. */ }
  }
  function announce(message) { text(refs.announcement, message); }
  function draftFor(alias) {
    if (!drafts.has(alias)) drafts.set(alias, {
      text: '', thread: projects.get(alias)?.thread_id ?? '', attempt: null, notice: '', needsReview: false,
    });
    return drafts.get(alias);
  }
  function receiptState(id) {
    if (!receiptUI.has(id)) receiptUI.set(id, { loading: false, cancelling: false, uncertain: false, message: '' });
    return receiptUI.get(id);
  }
  function answerNeedsReceipt(id) {
    const attempt = answerDrafts.get(id)?.attempt;
    return !!attempt && (attempt.phase === 'sending' || !attempt.checked);
  }
  function answerFor(request) {
    if (!answerDrafts.has(request.id)) answerDrafts.set(request.id, {
      text: request.text, targets: request.targets.map(({ alias, thread_id }) => ({ alias, thread_id })),
      attempt: null, dirty: false,
    });
    return answerDrafts.get(request.id);
  }
  function preserveFocus(action) {
    const active = doc.activeElement;
    const key = root.contains(active) ? active?.dataset?.focusKey : null;
    const receiptId = key ? active?.dataset?.receiptId : null;
    const selection = key && typeof active.selectionStart === 'number'
      ? [active.selectionStart, active.selectionEnd] : null;
    action();
    if (key) {
      const candidates = [...root.querySelectorAll('[data-focus-key]')].filter(node => {
        for (let parent = node; parent; parent = parent.parentNode) if (parent.hidden) return false;
        return true;
      });
      let replacement = candidates.find(node => node.dataset.focusKey === key && !node.disabled);
      if (!replacement && key === 'send') replacement = refs.prompt;
      if (!replacement && /^(refresh|cancel|answer):/.test(key)) {
        const receiptKey = `receipt:${key.slice(key.indexOf(':') + 1)}`;
        replacement = candidates.find(node => node.dataset.focusKey === receiptKey);
      }
      if (!replacement && receiptId) replacement = candidates.find(node => node.dataset.focusKey === `receipt:${receiptId}`);
      if (replacement && !replacement.disabled && doc.activeElement !== replacement) {
        replacement.focus({ preventScroll: true });
        if (selection && replacement.setSelectionRange) replacement.setSelectionRange(...selection);
      }
    }
  }

  const refs = {};
  const header = element('header', 'app-header');
  const brand = element('div', 'brand');
  const brandCopy = element('div');
  brandCopy.append(element('span', 'brand-name', 'Vega'),
    element('small', '', localMode ? 'Private desktop projects' : 'Project companion'));
  brand.append(element('img', 'vega-icon', null, { src:'/mini/vega-icon.svg',alt:'' }), brandCopy);
  refs.connection = element('span', 'connection');
  header.append(brand, refs.connection);
  refs.announcement = element('p', 'sr-only', '', { role: 'status', 'aria-live': 'polite', 'aria-atomic': 'true' });
  refs.offline = element('p', 'banner offline-banner', 'You’re offline. Drafts stay in this window. Reconnect, then refresh receipts before retrying a request.', { role: 'status' });
  refs.error = element('p', 'banner', '', { role: 'alert' });
  refs.gate = element('section', 'gate', null, { 'aria-labelledby': 'gate-title' });
  refs.gateTitle = element('h1', '', '', { id: 'gate-title', tabindex: '-1' });
  refs.gateText = element('p');
  refs.gateError = element('p', 'hint');
  refs.recoveredDrafts = element('div');
  refs.gate.append(element('img', 'vega-icon', null, { src:'/mini/vega-icon.svg',alt:'' }),
    element('p', 'eyebrow', 'YOUR PROJECTS, WITH YOU'), refs.gateTitle, refs.gateText, refs.gateError, refs.recoveredDrafts);
  refs.content = element('div');
  refs.paused = element('p', 'banner pause-banner', 'Work is paused. The project checklists and request receipts below show the last reported state.', { role: 'status' });
  refs.list = element('section', 'view', null, { 'aria-labelledby': 'projects-title' });
  const listHeading = element('div', 'view-heading');
  const listCopy = element('div');
  refs.listTitle = element('h1', '', 'Your projects', { id: 'projects-title', tabindex: '-1' });
  listCopy.append(element('p', 'eyebrow', 'THE BIG PICTURE'), refs.listTitle,
    element('p', 'subtitle', 'See what’s moving. Give the next direction.'));
  refs.refreshList = button('Refresh', () => refresh(), 'quiet-button', 'refresh-list');
  listHeading.append(listCopy, refs.refreshList);
  refs.summary = element('div', 'summary-line');
  refs.listFreshness = element('div', 'freshness-summary', '', { role: 'status' });
  const homeSection = element('section', 'receipts-section home-questions', null, { 'aria-labelledby': 'questions-title' });
  refs.homeRequests = element('div', 'request-list');
  refs.homeEmpty = element('p', 'empty');
  homeSection.append(element('h2', '', 'Questions & receipts', { id: 'questions-title' }),
    element('p', 'hint', 'Answer requests waiting for input here. Their updated receipts remain here; follow implementation progress in each project checklist.'),
    refs.homeEmpty, refs.homeRequests);
  const search = element('div', 'search-field');
  refs.search = element('input', '', null, { type: 'search', id: 'project-search', placeholder: 'Project name or alias', autocomplete: 'off' });
  search.append(element('label', '', 'Find a project', { for: 'project-search' }), refs.search);
  refs.projectList = element('ul', 'project-list', null, { 'aria-label': 'Projects' });
  refs.listEmpty = element('p', 'empty');
  refs.list.append(listHeading, refs.summary, refs.listFreshness, homeSection, search, refs.projectList, refs.listEmpty);
  refs.detail = element('section', 'view', null, { 'aria-labelledby': 'project-title' });
  refs.back = button('← All projects', () => showProjects(), 'back-button', 'back');
  const detailHeading = element('div', 'view-heading');
  const detailCopy = element('div');
  refs.alias = element('p', 'eyebrow');
  refs.projectTitle = element('h1', '', '', { id: 'project-title', tabindex: '-1' });
  refs.projectStatus = element('p', 'project-status');
  detailCopy.append(refs.alias, refs.projectTitle, refs.projectStatus);
  refs.refreshDetail = button('Refresh', () => refresh(), 'quiet-button', 'refresh-detail');
  detailHeading.append(detailCopy, refs.refreshDetail);
  refs.projectMeta = element('details', 'project-meta meta');
  refs.projectFreshness = element('section', 'project-freshness', '', { role: 'status' });
  refs.assessment = element('div');
  refs.detailLoading = element('p', 'loading', 'Refreshing project and receipts…', { role: 'status' });
  refs.detailError = element('p', 'banner', '', { role: 'alert' });
  refs.checklists = element('div', 'checklist-grid');
  refs.activity = element('div', 'work-activity', null, { 'aria-label': 'Agents and skills work activity' });
  function activityTrigger() {
    const trigger = element('button', 'quiet-button activity-trigger', '◉ ✧', {
      type: 'button', 'aria-label': 'Agents & skills', title: 'Agents & skills', 'aria-haspopup': 'dialog',
    });
    trigger.onclick = () => loadActivity();
    return trigger;
  }
  refs.activity.append(activityTrigger());
  refs.composer = element('section', 'composer', null, { 'aria-labelledby': 'composer-title' });
  refs.target = element('div', 'composer-target hint');
  refs.form = element('form');
  refs.prompt = element('textarea', '', null, {
    id: 'project-prompt', name: 'prompt', rows: '4', required: '', placeholder: 'What should happen next?',
    'aria-describedby': 'prompt-help draft-status', 'data-focus-key': 'prompt',
  });
  refs.scopeNote = element('p', 'attempt-note');
  refs.attemptNote = element('p', 'attempt-note', '', { id: 'draft-status', role: 'status' });
  refs.retryKey = element('div', 'retry-key');
  refs.composerNotice = element('p', 'composer-notice', '', { role: 'status' });
  const composerActions = element('div', 'composer-actions');
  refs.rebind = button('Use current thread', () => reviewScope(), 'quiet-button', 'review-scope');
  refs.reconcile = button('Refresh receipts', () => loadProject(selected), 'quiet-button', 'reconcile');
  refs.send = element('button', 'primary', 'Send to project', { type: 'submit', 'data-focus-key': 'send' });
  composerActions.append(refs.rebind, refs.reconcile, refs.send);
  refs.form.append(element('label', '', 'Project prompt', { for: 'project-prompt' }), refs.prompt,
    refs.scopeNote, refs.attemptNote, refs.retryKey, refs.composerNotice, composerActions);
  refs.composer.append(element('h2', '', 'Give the next direction', { id: 'composer-title' }), refs.target,
    element('p', 'hint', 'A request receipt confirms routing status. Track implementation progress in the checklist above.', { id: 'prompt-help' }),
    refs.form, element('p', 'draft-note', 'Drafts are kept only while this window is open. Requests are sent only when you choose Send or Retry.'));
  const receiptsSection = element('section', 'receipts-section', null, { 'aria-labelledby': 'receipts-title' });
  refs.requestList = element('div', 'request-list');
  refs.requestEmpty = element('p', 'empty');
  receiptsSection.append(element('h2', '', 'Requests & receipts', { id: 'receipts-title' }),
    element('p', 'hint', 'Actual server responses, with a separate receipt for each route. Queued, routing, and handled are request states, not completion of project work.'),
    refs.requestEmpty, refs.requestList);
  refs.detail.append(refs.back, detailHeading, refs.detailLoading, refs.detailError, refs.assessment,
    refs.projectFreshness, refs.projectMeta, refs.checklists, refs.activity, refs.composer, receiptsSection);
  refs.footer = element('footer', 'footer');
  refs.content.append(refs.paused, refs.list, refs.detail, refs.footer);
  root.replaceChildren(header, refs.announcement, refs.offline, refs.error, refs.gate, refs.content);

  function resetActivity() {
    activityGeneration++; activityLoading = false;
    activityPanel?.destroy(); activityPanel = null;
    refs.activity.replaceChildren(activityTrigger());
  }
  async function loadActivity() {
    if (destroyed || access !== 'active' || !selected || !online || !visible() || activityLoading) return;
    if (activityPanel) return activityPanel.open();
    const alias = selected, generation = ++activityGeneration;
    activityLoading = true;
    try {
      const module = await (options.loadActivity ? options.loadActivity() : import('/mini/team-panel.js'));
      if (destroyed || generation !== activityGeneration || access !== 'active' || selected !== alias) return;
      if (!doc.querySelector('link[data-team-style]')) {
        const style = element('link'); style.rel = 'stylesheet'; style.href = '/mini/team-panel.css';
        style.setAttribute('data-team-style', 'true'); doc.head.append(style);
      }
      activityPanel = module.initWorkActivity({
        root: refs.activity, project: alias,
        api: (path, config) => call(miniActivityPath(path), config),
        visible: () => !destroyed && access === 'active' && online && visible() && selected === alias,
      });
      await activityPanel.ready;
      if (!destroyed && generation === activityGeneration && access === 'active' && selected === alias) await activityPanel.open();
    } catch (error) {
      if (!destroyed && generation === activityGeneration) {
        refs.activity.append(element('p', 'hint', `Activity could not be loaded. ${error?.message || ''}`));
      }
    } finally { if (generation === activityGeneration) activityLoading = false; }
  }

  function render() {
    if (destroyed) return;
    preserveFocus(() => {
      const active = access === 'active';
      refs.gate.hidden = active;
      refs.content.hidden = !active;
      refs.offline.hidden = online || !active;
      refs.error.hidden = (!listError && !requestListError) || !active;
      text(refs.error, [listError, requestListError].filter(Boolean).join('\n'));
      const refreshNeeded = !!listError || !!requestListError || !!details.get(selected)?.error;
      text(refs.connection, !active ? (localMode ? 'Local session expired' : 'Telegram required') : !online ? 'Offline'
        : refreshNeeded ? 'Refresh needed' : projectsLoaded ? (localMode ? 'Local connection' : 'Connected') : 'Connecting');
      refs.connection.dataset.offline = String(!online || refreshNeeded);
      if (!active) {
        text(refs.gateTitle, localMode ? 'Reconnect to desktop Projects'
          : access === 'missing' ? 'Open Vega from Telegram' : 'Reopen from your paired account');
        text(refs.gateText, access === 'missing'
          ? 'Open this Mini App from Vega in the paired Telegram account to view your projects. This window has no signed Telegram session.'
          : reopenMessage);
        text(refs.gateError, authError);
        refs.gateError.hidden = !authError;
        refs.recoveredDrafts.replaceChildren();
        for (const [alias, draft] of drafts) {
          if (!draft.text) continue;
          const recovered = element('section', 'recovered-draft');
          recovered.append(element('h2', '', `Draft: ${projects.get(alias)?.label ?? alias}`), element('pre', '', draft.text));
          if (draft.attempt) recovered.append(element('p', 'hint', `Unconfirmed retry key: ${draft.attempt.key}`));
          refs.recoveredDrafts.append(recovered);
        }
        for (const [id, draft] of answerDrafts) {
          if (draft.dirty || draft.attempt) refs.recoveredDrafts.append(retainedAnswer(id, draft));
        }
        for (const [id, draft] of steerDrafts) {
          if (!draft.text) continue;
          const recovered = element('section', 'recovered-draft');
          recovered.append(element('h2', '', 'Steering draft'), element('pre', '', draft.text),
            element('p', 'hint', `Original request: ${id}`));
          if (draft.attempt) recovered.append(element('p', 'hint', `Unconfirmed retry key: ${draft.attempt.key}`));
          refs.recoveredDrafts.append(recovered);
        }
        return;
      }
      refs.paused.hidden = !workPaused;
      refs.list.hidden = selected !== null;
      refs.detail.hidden = selected === null;
      refs.refreshList.disabled = !online || !!projectListPromise || !!requestListPromise;
      text(refs.refreshList, projectListPromise || requestListPromise ? 'Refreshing…' : 'Refresh');
      text(refs.footer, intervalMinutes == null ? 'Coordinator interval has not been reported.'
        : `Coordinator interval: ${intervalMinutes} minutes.${pollIntervalMs > 0 ? ' Projects and receipts refresh while this window is visible.' : ''} Report dates change only when new source evidence is received.`);
      // A request can appear at home and in a project. Keep only the active
      // cards mounted so labels and keyboard focus keys remain unique.
      if (selected === null) { refs.requestList.replaceChildren(); renderProjects(); }
      else { refs.homeRequests.replaceChildren(); renderProject(); }
    });
  }

  function renderProjects() {
    const query = refs.search.value.trim().toLocaleLowerCase();
    const visible = [...projects.values()].filter(project => `${project.label} ${project.alias}`.toLocaleLowerCase().includes(query));
    const blockers = [...projects.values()].reduce((sum, project) => sum + project.checklist.blockers.length, 0);
    refs.summary.replaceChildren(
      element('span', '', `${projects.size} ${projects.size === 1 ? 'project' : 'projects'}`),
      element('span', blockers ? 'blocker-count' : '', `${blockers} ${blockers === 1 ? 'blocker' : 'blockers'} reported`),
    );
    const freshnessCounts = { fresh: 0, stale: 0, error: 0, not_reported: 0 };
    for (const project of projects.values()) freshnessCounts[freshness(project, listError).state]++;
    refs.listFreshness.replaceChildren(element('p', '', `View checked: ${displayTime(listCheckedAt)}`));
    if (projectsLoaded) {
      refs.listFreshness.append(element('p', 'hint',
        `${freshnessCounts.fresh} recent · ${freshnessCounts.stale} stale · ${freshnessCounts.error} unavailable · ${freshnessCounts.not_reported} not reported. Each report keeps its original date.`));
    }
    refs.projectList.replaceChildren();
    for (const project of visible) {
      const item = element('li');
      const card = button('', () => openProject(project.alias), 'project-card', `project:${project.alias}`);
      card.setAttribute('aria-label', `Open project ${project.label}`);
      const heading = element('span', 'card-heading');
      heading.append(projectMark(doc, project),
        element('span', 'project-name', project.label), icon(doc, 'chevron'));
      const footer = element('span', 'card-footer');
      if (project.checklist.working.length) footer.append(element('span','',`${project.checklist.working.length} working`));
      if (project.checklist.blockers.length) footer.append(element('span','blocker-count',`! ${project.checklist.blockers.length} ${project.checklist.blockers.length === 1 ? 'blocker' : 'blockers'}`));
      const report = freshness(project, listError);
      const reportMeta = element('span', 'card-freshness');
      reportMeta.append(freshnessBadge(report),
        element('span', 'hint', `Last updated: ${displayTime(report.evidenceAt)}`));
      card.append(heading, reportMeta, element('span', 'project-status',
        routeUnavailable(project) ? 'Chat not connected' : `Thread / status: ${valueText(project.status) || 'Not reported'}`),
        element('span', 'card-preview', valueText(project.checklist.working[0]
          ?? project.checklist.blockers[0] ?? project.checklist.next[0] ?? '')), footer);
      const assessment = assessmentView(project, true);
      if (assessment) card.append(assessment);
      item.append(card);
      refs.projectList.append(item);
    }
    refs.listEmpty.hidden = visible.length > 0;
    text(refs.listEmpty, !projectsLoaded ? (projectListPromise ? 'Loading your projects…' : 'Projects could not be loaded. Refresh when connected.')
      : projects.size === 0 ? 'No projects are available for this account yet.'
        : 'No projects match your search.');
    refs.projectList.setAttribute('aria-busy', String(!!projectListPromise));
    const pending = [...requests.values()].filter(request => homeReceipts.has(request.id));
    refs.homeEmpty.hidden = pending.length > 0;
    text(refs.homeEmpty, requestsLoaded ? 'No requests are waiting for your input.'
      : requestListPromise || projectListPromise ? 'Checking for questions…' : 'Questions could not be loaded. Refresh to try again.');
    refs.homeRequests.setAttribute('aria-busy', String(!!requestListPromise));
    renderRequests(pending, refs.homeRequests);
  }

  function renderProject() {
    const detail = details.get(selected);
    const project = projects.get(selected) ?? detail?.project;
    const loading = projectLoads.has(selected);
    const draft = draftFor(selected);
    text(refs.alias, selected);
    text(refs.projectTitle, project?.label ?? selected);
    text(refs.projectStatus, routeUnavailable(project) ? 'Chat not connected'
      : project ? `Thread / status: ${valueText(project.status) || 'Not reported'}` : 'Project unavailable');
    refs.detailLoading.hidden = !loading;
    refs.detailError.hidden = !detail?.error;
    text(refs.detailError, detail?.error ?? '');
    refs.refreshDetail.disabled = !online || loading;
    text(refs.refreshDetail, loading ? 'Refreshing…' : 'Refresh');
    refs.projectMeta.replaceChildren(element('summary','','Details & sources'));
    const report = freshness(project, detail?.error || listError);
    refs.projectFreshness.dataset.freshness = report.state;
    refs.projectFreshness.replaceChildren(
      freshnessBadge(report),
      element('p', 'report-time', `Last updated: ${displayTime(report.evidenceAt)}`),
      ...(report.state === 'fresh' ? [] : [element('p', 'hint', report.reason)]),
      element('p', 'hint', `View checked: ${displayTime(projectSnapshots.get(selected)?.receivedAt)}`),
    );
    refs.assessment.replaceChildren();
    const assessment = assessmentView(project);
    if (assessment) refs.assessment.append(assessment);
    if (project?.latest_reply) {
      const reply = project.latest_reply;
      const recorded = element('details', 'progress-assessment');
      recorded.append(element('summary', '', `Latest recorded reply · ${displayTime(reply.observed_at)}`),
        element('p', 'hint', 'A recorded project reply. Its text is separate from the checklist and percentage assessment.'));
      if (reply.source_error) recorded.append(element('p', 'request-message', valueText(reply.source_error)));
      else if (typeof reply.text === 'string') {
        recorded.append(element('pre', 'recorded-reply', reply.text));
        if (reply.truncated) recorded.append(element('p', 'hint', 'This recorded reply exceeds the bounded view size and is truncated.'));
      } else {
        recorded.append(element('p', 'hint', 'Refresh this project to load the recorded reply text.'));
      }
      refs.assessment.append(recorded);
    }
    if (project) {
      if (project.connection?.source?.checked_at) refs.projectMeta.append(
        element('p', '', `History chat checked: ${displayTime(project.connection.source.checked_at)}`));
      if (project.connection?.destination?.checked_at) refs.projectMeta.append(
        element('p', '', `Destination checked: ${displayTime(project.connection.destination.checked_at)}`));
      refs.projectMeta.append(element('p', '', `Thread / status observed: ${displayTime(project.observed_at)}`));
      if (project.routing_owner) refs.projectMeta.append(element('p', '', `Routing owner: ${project.routing_owner}`));
      const thread = element('p');
      thread.append(element('span', '', 'Thread: '), element('code', '', project.thread_id));
      refs.projectMeta.append(thread);
      const sources = element('details', 'progress-sources');
      sources.append(element('summary', '', 'Progress sources'));
      sources.append(element('p', 'hint', `Report evidence: ${displayTime(report.evidenceAt)}`));
      sources.append(element('p', 'hint', `Source checked: ${displayTime(report.checkedAt)}`));
      sources.append(element('p', 'hint', project.checklist_source === 'coordinator_report'
        ? 'Coordinator status/evidence snapshot. See the distinctions below.'
        : project.checklist_source === 'task_report' ? 'Task-reported progress.' : 'No checklist report is available yet.'));
      for (const evidence of Array.isArray(project.evidence) ? project.evidence : []) {
        sources.append(element('p', 'hint', valueText(evidence)));
      }
      refs.projectMeta.append(sources);
    }
    refs.checklists.replaceChildren();
    for (const [key, title, mark, empty] of SECTIONS) {
      const items = project?.checklist[key] ?? [];
      const collapsed = key === 'done' || key === 'suggestions';
      const section = element(collapsed ? 'details' : 'section', 'checklist', null,
        { 'data-section': key, 'data-empty': String(!items.length), 'aria-labelledby': `section-${key}` });
      const heading = element(collapsed ? 'summary' : 'div', 'section-heading');
      heading.append(element('span', 'section-mark', mark, { 'aria-hidden': 'true' }),
        element('h2', '', title, { id: `section-${key}` }), element('span', 'section-count', String(items.length)));
      section.append(heading);
      if (items.length) {
        const list = element('ul');
        for (const item of items) list.append(element('li', '', valueText(item)));
        section.append(list);
      } else section.append(element('p', 'empty', project ? empty : 'Waiting for project data.'));
      refs.checklists.append(section);
    }
    renderComposer(project, detail, draft, loading);
    const visible = [...requests.values()].filter(request => request.targets.some(target => target.alias === selected));
    refs.requestEmpty.hidden = visible.length > 0;
    text(refs.requestEmpty, detail?.verified ? 'No request receipts have been reported for this project.' : 'Request receipts have not loaded yet.');
    renderRequests(visible, refs.requestList);
  }

  function renderComposer(project, detail, draft, loading) {
    const attempt = draft.attempt;
    if (!draft.text && !attempt && project) draft.thread = project.thread_id;
    const scopeChanged = draft.needsReview || !!(draft.text && project && draft.thread !== project.thread_id);
    refs.target.replaceChildren(element('span', '', `To: ${project?.label ?? selected}`));
    if (scopeChanged || attempt) refs.target.append(
      element('code', '', `Thread: ${attempt?.target.thread_id ?? draft.thread ?? 'unavailable'}`));
    if (refs.prompt.value !== draft.text) refs.prompt.value = draft.text;
    refs.prompt.readOnly = !!attempt;
    refs.prompt.disabled = !project;
    refs.scopeNote.hidden = !scopeChanged;
    text(refs.scopeNote, attempt
      ? 'This draft is tied to its original thread. Review receipts before taking another action; it cannot be retried against a different thread.'
      : `The project thread needs review. Your draft is kept. Current thread: ${project?.thread_id ?? 'unavailable'}.`);
    refs.rebind.hidden = !scopeChanged || !!attempt;
    refs.rebind.disabled = !online || loading || !detail?.verified || !!detail.error;
    refs.attemptNote.hidden = !attempt;
    text(refs.attemptNote, attempt?.phase === 'sending'
      ? 'Waiting for the server’s request receipt…'
      : attempt ? `${attempt.message}\nYour draft and retry key are kept. ${attempt.checked
        ? 'Receipts were refreshed. You may explicitly retry the same request with the same key.'
        : 'Refresh receipts to reconcile before retrying. The request will not be sent again automatically.'}` : '');
    refs.retryKey.hidden = !attempt;
    refs.retryKey.replaceChildren();
    if (attempt) refs.retryKey.append(element('span', '', 'Retry key'), element('code', '', attempt.key));
    const connectionNotice = routeUnavailable(project)
      ? 'This project’s chat was not found at the last check. Reconnect it before sending. Your draft is kept.' : '';
    refs.composerNotice.hidden = !draft.notice && !connectionNotice;
    text(refs.composerNotice, [connectionNotice, draft.notice].filter(Boolean).join('\n'));
    refs.reconcile.hidden = !attempt || attempt.phase === 'sending';
    refs.reconcile.disabled = !online || loading;
    text(refs.send, attempt?.phase === 'sending' ? 'Waiting for receipt…' : attempt ? 'Retry same request' : 'Send to project');
    refs.send.disabled = Boolean(!online || loading || !detail?.verified || !!detail.error || !draft.text.trim()
      || routeUnavailable(project) || scopeChanged || (attempt && (attempt.phase === 'sending' || !attempt.checked)));
    refs.form.setAttribute('aria-busy', String(attempt?.phase === 'sending'));
  }

  function metadata(parent, entries) {
    const list = element('dl', 'receipt-meta');
    for (const [name, value] of entries) {
      list.append(element('dt', '', name), element('dd', '', valueText(value) || 'Not reported'));
    }
    parent.append(list);
  }

  function disclosure(key, label) {
    const section = element('details', 'raw-receipt', null, { 'data-focus-key': key });
    section.open = openReceipts.has(key);
    section.append(element('summary', '', label, { 'data-focus-key': `summary:${key}` }));
    section.addEventListener('toggle', () => {
      if (section.open) openReceipts.add(key); else openReceipts.delete(key);
    });
    return section;
  }
  function referencesFor(request) {
    const body = [request.text, ...request.routes.map(route => route.receipt || '')].join('\n');
    return [
      ...(request.parent_request_id ? [{ kind: 'request', token: request.parent_request_id, label: 'Original request' }] : []),
      ...referenceTokens(body).slice(0, 8).map(token => ({
        kind: 'reference', token,
        label: token.startsWith('prior') ? 'Earlier request' : token.startsWith('turn') ? 'Linked chat' : token.split('/').at(-1),
      })),
    ];
  }
  function renderReferences(request, card) {
    const choices = referencesFor(request);
    const current = referenceUI.get(request.id);
    if (current && !choices.some(choice => choice.kind === current.kind && choice.token === current.token)) {
      referenceUI.delete(request.id);
    }
    if (!choices.length) return;
    const links = element('div', 'request-references');
    for (const choice of choices) {
      const link = button(choice.label, () => previewReference(request.id, choice),
        'quiet-button', `reference:${request.id}:${choice.kind}:${choice.token}`);
      link.title = choice.token;
      link.disabled = !online;
      links.append(link);
    }
    card.append(links);
    const preview = referenceUI.get(request.id);
    if (!preview?.open) return;
    const section = element('section', 'reference-preview', null, { 'aria-label': 'Reference preview' });
    section.append(element('h4', '', preview.value?.label || preview.label),
      button('Close preview', () => { preview.open = false; render(); }, 'quiet-button', `preview-close:${request.id}`));
    if (preview.loading) section.append(element('p', 'hint', 'Loading…'));
    if (preview.error) section.append(element('p', 'request-message', preview.error, { role: 'status' }));
    if (preview.value) {
      const value = preview.value;
      section.append(element('p', 'hint', value.state ? (preview.kind === 'request'
        ? requestLabel(value.state) : String(value.state).replaceAll('_', ' '))
        : value.available ? 'Saved reference' : 'Reference unavailable'));
      if (value.recorded_at) section.append(element('p', 'hint', displayTime(value.recorded_at)));
      section.append(element('pre', 'reference-text', value.text));
    }
    card.append(section);
  }
  async function previewReference(id, choice) {
    const request = requests.get(id);
    if (!request || !referencesFor(request).some(item => item.kind === choice.kind && item.token === choice.token)
        || !online || access !== 'active') return;
    const preview = { ...choice, open: true, loading: true, value: null, error: '' };
    referenceUI.set(id, preview);
    render();
    try {
      let value;
      if (choice.kind === 'request') {
        const data = await call(`/mini/api/requests/${segment(choice.token)}`);
        const parent = checkRequest(data.request);
        if (parent.id !== choice.token) throw new ApiError('The original request could not be verified.');
        value = { label: 'Original request', available: true, text: parent.text,
          state: parent.state, recorded_at: parent.created_at };
      } else {
        value = await call(`/mini/api/requests/${segment(id)}/references/${segment(choice.token)}`);
        if (typeof value.available !== 'boolean' || typeof value.text !== 'string'
            || typeof value.label !== 'string') throw new ApiError('The reference could not be read.');
      }
      if (destroyed || access !== 'active' || referenceUI.get(id) !== preview) return;
      preview.value = value;
    } catch (error) {
      if (referenceUI.get(id) === preview) preview.error = error?.message || 'Reference unavailable.';
    } finally {
      preview.loading = false;
      render();
    }
  }

  function renderResponses(request, parent) {
    if (!Array.isArray(request.responses) || request.responses.length === 0) return;
    const responses = element('section', 'response-receipts', null, { 'aria-label': 'Response delivery receipts' });
    responses.append(element('h4', '', 'Response delivery'));
    for (const response of request.responses) {
      if (!response || typeof response !== 'object') continue;
      const entry = element('div', 'response-receipt', null, { 'data-response-state': valueText(response.state) });
      entry.append(element('p', 'response-state', `Delivery state: ${valueText(response.state) || 'Not reported'}`),
        element('p', 'hint', RESPONSE_NOTES[response.state] ?? 'Review the server’s actual response delivery receipt.'));
      const deliveredParts = Number.isSafeInteger(response.delivered_parts) && response.delivered_parts >= 0
        ? response.delivered_parts : 'Not reported';
      const totalParts = Number.isSafeInteger(response.total_parts) && response.total_parts >= 0
        ? response.total_parts : 'Not reported';
      metadata(entry, [
        ['Response ID', response.id], ['Report', response.report_id],
        ['Parts delivered', `${deliveredParts} / ${totalParts}`],
        ['Source date', response.source?.kind === 'progress' ? 'Shown per project' : displayTime(response.source?.assessed_at)],
        ['Owner read', response.owner_read_confirmed === true ? 'Confirmed by owner' : 'Not confirmed'],
      ]);
      if (response.pending === true) entry.append(element('p', 'hint', 'Pending response content is retained.'));
      if (response.error) entry.append(element('p', 'request-message', valueText(response.error)));
      if (Array.isArray(response.parts) && response.parts.length) {
        const parts = element('ul', 'response-parts');
        for (const part of response.parts) {
          if (!part || typeof part !== 'object') continue;
          const index = Number.isSafeInteger(part.part_index) ? part.part_index + 1 : 'Not reported';
          parts.append(element('li', '', `Part ${index}: ${valueText(part.state) || 'Not reported'}`
            + (part.message_id != null ? ` · Telegram message ${valueText(part.message_id)}` : ' · No Telegram message receipt')));
        }
        entry.append(parts);
      }
      responses.append(entry);
    }
    parent.append(responses);
  }

  function targetsCurrent(targets) {
    return targets.every(target => projects.get(target.alias)?.thread_id === target.thread_id
      && !routeUnavailable(projects.get(target.alias)));
  }
  function canSteer(request, draft) {
    const ui = receiptState(request.id);
    if (destroyed || access !== 'active' || !online || ui.loading || ui.cancelling || ui.uncertain) return false;
    if (draft.attempt) return draft.attempt.phase !== 'sending' && draft.attempt.checked;
    return request.can_steer === true && projectsLoaded && !listError && !projectListPromise
      && request.targets.length > 0 && targetsCurrent(request.targets)
      && hasText(draft.text) && draft.text.length <= 12000;
  }
  function matchesSteer(request, id, attempt) {
    return request.request_key === attempt.key && request.parent_request_id === id
      && request.follow_up_kind === 'steer' && request.text === attempt.text
      && request.targets.length === attempt.targets.length
      && attempt.targets.every(target => request.targets.some(other => sameTarget(target, other)));
  }
  function reconcileSteer(request) {
    const id = request.parent_request_id;
    const draft = steerDrafts.get(id);
    if (!draft?.attempt || !matchesSteer(request, id, draft.attempt)) return;
    draft.attempt = null;
    draft.text = '';
    draft.open = false;
    draft.message = `Follow-up ${request.state}. Original request kept.`;
    announce(draft.message);
  }
  function renderSteer(request, draft) {
    const prefix = `steer-${segment(request.id)}`;
    const form = element('form', 'question steer-form', null, { 'aria-labelledby': `${prefix}-title` });
    form.append(element('h4', '', 'Steer this request', { id: `${prefix}-title` }),
      element('p', 'hint', `To: ${request.targets.map(target => target.label ?? target.alias).join(', ')}`),
      element('p', 'hint', 'Adds a queued follow-up. It does not interrupt work already running.'));
    const prompt = element('textarea', '', null, {
      id: `${prefix}-text`, rows: '3', maxlength: '12000', required: '',
      'aria-label': 'Steering prompt', 'data-focus-key': `steer-text:${request.id}`, 'data-receipt-id': request.id,
    });
    prompt.value = draft.text;
    prompt.readOnly = !!draft.attempt;
    const send = element('button', 'primary', '', { type: 'submit', 'data-focus-key': `steer-send:${request.id}` });
    const help = element('p', 'hint', '', { role: 'status' });
    function controls() {
      send.disabled = !canSteer(requests.get(request.id) ?? request, draft);
      text(send, draft.attempt?.phase === 'sending' ? 'Waiting for receipt…'
        : draft.attempt ? 'Retry same follow-up' : 'Send follow-up');
      text(help, draft.message || (!targetsCurrent(request.targets) ? 'The project connection changed. Refresh before sending.' : ''));
    }
    prompt.addEventListener('input', () => {
      if (draft.attempt) { prompt.value = draft.text; return; }
      draft.text = prompt.value;
      draft.message = '';
      controls();
    });
    form.addEventListener('submit', event => { event.preventDefault(); return submitSteer(request.id); });
    const close = button('Close', () => { draft.open = false; render(); }, 'quiet-button', `steer-close:${request.id}`);
    const actions = element('div', 'composer-actions');
    actions.append(close, send);
    form.append(prompt, help, actions);
    controls();
    return form;
  }
  function canAnswer(request, draft) {
    const ui = receiptState(request.id);
    const attempt = draft.attempt;
    return !destroyed && access === 'active' && online && request.state === 'awaiting_input'
      && projectsLoaded && !listError && !projectListPromise && !ui.loading && !ui.cancelling && !ui.uncertain
      && hasText(draft.text) && draft.text.length <= 12000 && draft.targets.length > 0 && draft.targets.length <= 32
      && targetsCurrent(draft.targets)
      && (!attempt || (attempt.phase !== 'sending' && attempt.checked && request.revision === attempt.revision));
  }
  function retainedAnswer(id, draft) {
    const section = element('section', 'recovered-draft');
    section.append(element('h4', '', 'Kept answer draft'), element('p', 'hint', `Request: ${id}`),
      element('pre', '', draft.text),
      element('p', 'hint', draft.targets.map(target => `${target.alias} · ${target.thread_id}`).join('\n')));
    if (draft.attempt) section.append(element('p', 'hint', `Submitted revision: ${draft.attempt.revision}. Refresh the receipt to reconcile.`));
    return section;
  }
  function renderAnswer(request) {
    const draft = answerFor(request);
    const ui = receiptState(request.id);
    const fields = new Map((request.question?.fields ?? []).map(field => [field.name, field.label]));
    const prefix = `answer-${segment(request.id)}`;
    const form = element('form', 'question answer-form', null, { 'aria-labelledby': `${prefix}-title` });
    form.append(element('h4', '', 'Input requested', { id: `${prefix}-title` }));
    const targets = element('fieldset', 'answer-targets');
    targets.append(element('legend', '', `${fields.get('targets') ?? 'Project targets'} (required)`));
    const choices = [...projects.values()];
    for (const target of draft.targets) {
      if (!choices.some(choice => sameTarget(choice, target))) choices.push(target);
    }
    for (const choice of choices) {
      const current = projects.get(choice.alias)?.thread_id === choice.thread_id;
      const unavailable = routeUnavailable(projects.get(choice.alias));
      const choiceId = `${prefix}-target-${segment(choice.alias)}-${segment(choice.thread_id)}`;
      const label = element('label', 'target-option', null, { for: choiceId, 'data-stale': String(!current) });
      const input = element('input', '', null, {
        id: choiceId, type: 'checkbox', name: 'targets',
        'data-focus-key': `answer-target:${choiceId}`, 'data-receipt-id': request.id,
      });
      input.value = choice.alias;
      input.checked = draft.targets.some(target => sameTarget(target, choice));
      input.disabled = !!draft.attempt || ui.cancelling;
      const copy = element('span');
      copy.append(element('span', 'target-name', choice.label ?? choice.alias),
        element('code', 'target-thread', `${choice.alias} · ${choice.thread_id}`));
      if (!current) copy.append(element('small', '', 'This thread is no longer current. Choose the current project or uncheck this selection.'));
      else if (unavailable) copy.append(element('small', '', 'Chat not connected. Reconnect this project or remove it from this request.'));
      label.append(input, copy);
      input.addEventListener('change', () => {
        if (draft.attempt || ui.cancelling || answerDrafts.get(request.id) !== draft
            || requests.get(request.id)?.state !== 'awaiting_input') return;
        draft.targets = input.checked
          ? [...draft.targets.filter(target => target.alias !== choice.alias), { alias: choice.alias, thread_id: choice.thread_id }]
          : draft.targets.filter(target => !sameTarget(target, choice));
        draft.dirty = true;
        if (!ui.uncertain) ui.message = '';
        render();
      });
      targets.append(label);
    }
    if (!choices.length) targets.append(element('p', 'empty', 'No projects are available. Refresh projects to check again.'));
    const prompt = element('textarea', '', null, {
      id: `${prefix}-text`, name: 'text', rows: '4', required: '', maxlength: '12000',
      'data-focus-key': `answer-text:${request.id}`, 'data-receipt-id': request.id,
      'aria-describedby': `${prefix}-help`,
    });
    prompt.value = draft.text;
    prompt.readOnly = !!draft.attempt;
    prompt.disabled = ui.cancelling;
    const help = element('p', 'hint', '', { id: `${prefix}-help`, role: 'status' });
    const send = element('button', 'primary', '', { type: 'submit', 'data-focus-key': `answer:${request.id}` });
    const edit = button('Edit answer', () => {
      if (!draft.attempt?.checked || draft.attempt.phase === 'sending'
          || requests.get(request.id)?.state !== 'awaiting_input' || ui.loading || ui.cancelling) return;
      draft.attempt = null;
      ui.message = 'Your answer draft is editable. Sending still checks the current request revision.';
      render();
      doc.getElementById(`${prefix}-text`)?.focus();
    }, 'quiet-button', `answer-edit:${request.id}`);
    function updateControls() {
      const attempt = draft.attempt;
      send.disabled = !canAnswer(requests.get(request.id) ?? request, draft);
      edit.hidden = !attempt?.checked || attempt.phase === 'sending';
      edit.disabled = ui.loading || ui.cancelling;
      text(send, attempt?.phase === 'sending' ? 'Waiting for receipt…' : attempt ? 'Retry same answer' : 'Send answer');
      text(help, attempt ? (attempt.phase === 'sending' ? 'Waiting for the server’s answer receipt…'
        : attempt.checked ? `Receipts refreshed. Retry sends the same answer with revision ${attempt.revision}. You can also edit the kept draft.`
          : 'Refresh receipt before retrying. Your exact answer and revision are kept.')
        : draft.targets.some(target => routeUnavailable(projects.get(target.alias))) ? 'A selected chat is not connected. Reconnect it or remove it from this request.'
        : !targetsCurrent(draft.targets) ? 'A selected thread changed. Choose its current project or remove the previous selection.'
          : !projectsLoaded || listError ? 'Refresh projects to verify the available project threads.'
            : !draft.targets.length ? 'Choose at least one project.'
              : draft.targets.length > 32 ? 'Choose up to 32 projects.'
                : !hasText(draft.text) ? 'Enter the project prompt.' : 'Your answer applies to the project threads shown above.');
      form.setAttribute('aria-busy', String(attempt?.phase === 'sending'));
    }
    prompt.addEventListener('input', () => {
      if (draft.attempt) { prompt.value = draft.text; return; }
      if (answerDrafts.get(request.id) !== draft || requests.get(request.id)?.state !== 'awaiting_input') return;
      draft.text = prompt.value;
      draft.dirty = true;
      if (!ui.uncertain) ui.message = '';
      updateControls();
    });
    form.addEventListener('submit', event => { event.preventDefault(); return submitAnswer(request.id); });
    const actions = element('div', 'composer-actions');
    actions.append(edit, send);
    form.append(targets, element('label', '', `${fields.get('text') ?? 'Project prompt'} (required)`, { for: `${prefix}-text` }),
      prompt, help, actions, element('p', 'draft-note', 'Answer drafts stay in this window. Sending an answer requests routing; it does not confirm implementation.'));
    updateControls();
    return form;
  }
  function renderRequests(visible, container) {
    visible.sort((a, b) => Number(b.state === 'awaiting_input') - Number(a.state === 'awaiting_input')
      || String(b.created_at ?? '').localeCompare(String(a.created_at ?? '')));
    container.replaceChildren();
    for (const request of visible) {
      const ui = receiptState(request.id);
      const card = element('article', 'request-card');
      const heading = element('div', 'request-heading');
      const state = element('span', 'state', requestLabel(request.state), {
        'data-state': request.state,
        'data-attention': String(['awaiting_input', 'partially_cancelled', 'stale_target'].includes(request.state)),
      });
      heading.append(element('h3', '', request.parent_request_id ? 'Follow-up' : 'Project request',
        { tabindex: '-1', 'data-focus-key': `receipt:${request.id}` }), state);
      const brief = request.text.trim().replace(/\s+/g, ' ');
      card.append(heading, element('p', 'hint', requestSummary(request.state)),
        element('p', 'request-text', brief.length > 140 ? brief.slice(0, 139) + '…' : brief),
        element('p', 'hint', request.targets.map(target => target.label || target.alias).join(', ') || 'No project selected'),
        element('p', 'hint', displayTime(request.created_at)));
      const evidence = disclosure(`details:${request.id}`, 'Details');
      evidence.append(element('h4', '', 'Original prompt'), element('pre', '', request.text));
      metadata(evidence, [['Request ID', request.id], ['Revision', request.revision], ['Created', displayTime(request.created_at)],
        ['Targets', request.targets.map(target => `${target.label ?? target.alias} (${target.alias}) · ${target.thread_id}`).join('\n')]]);
      if (request.parent_request_id) metadata(evidence, [['Original request ID', request.parent_request_id]]);
      if (request.state === 'awaiting_input') card.append(renderAnswer(request));
      else if (answerDrafts.get(request.id)?.dirty) card.append(retainedAnswer(request.id, answerDrafts.get(request.id)));
      if (request.routes.length) {
        const routes = element('ul', 'routes', null, { 'aria-label': 'Route receipts' });
        for (const route of request.routes) {
          const item = element('li', 'route');
          item.append(element('h4', '', valueText(route.label) || 'Route'));
          metadata(item, [['Update ID', route.update_id], ['Task', route.task], ['Thread', route.thread_id], ['State', route.state]]);
          metadata(item, [['Picked up', displayTime(route.claimed_at)],
            ['Routing receipt', displayTime(route.receipt_recorded_at ?? (route.state === 'handled' ? route.handled_at : null))],
            ['Project acceptance', 'Not independently verified']]);
          if (route.blocker) item.append(element('p', 'hint', route.blocker));
          item.append(element('p', 'receipt-label', 'Actual route receipt'));
          if (route.receipt == null) item.append(element('p', 'empty', 'No receipt was returned for this route.'));
          else item.append(element('pre', '', valueText(route.receipt)));
          routes.append(item);
        }
        evidence.append(routes);
      } else evidence.append(element('p', 'empty', 'No route receipts have been reported yet.'));
      for (const response of request.responses || []) {
        if (response?.state) card.append(element('p', 'hint', `Response: ${String(response.state).replaceAll('_', ' ')}`));
      }
      renderResponses(request, evidence);
      const raw = element('details', 'raw-receipt', null, { 'data-focus-key': `raw:${request.id}` });
      raw.open = openReceipts.has(request.id);
      const summary = element('summary', '', 'Full request receipt', { 'data-focus-key': `raw-summary:${request.id}` });
      raw.append(summary, element('pre', '', valueText(request)));
      raw.addEventListener('toggle', () => {
        if (raw.open) openReceipts.add(request.id); else openReceipts.delete(request.id);
      });
      evidence.append(raw);
      card.append(evidence);
      renderReferences(request, card);
      const actions = element('div', 'request-actions');
      const refreshButton = button(ui.loading ? 'Refreshing…' : 'Refresh receipt', () => refreshReceipt(request.id), 'quiet-button', `refresh:${request.id}`);
      refreshButton.setAttribute('aria-label', `Refresh receipt ${request.id}`);
      const answerAttempt = answerDrafts.get(request.id)?.attempt;
      const steerDraft = steerDrafts.get(request.id);
      refreshButton.disabled = !online || ui.loading || ui.cancelling || answerAttempt?.phase === 'sending'
        || steerDraft?.attempt?.phase === 'sending';
      actions.append(refreshButton);
      if (request.can_steer === true || steerDraft?.text || steerDraft?.attempt) {
        const steer = button('Steer', () => {
          const draft = steerDrafts.get(request.id) ?? { text: '', attempt: null, message: '' };
          draft.open = true;
          steerDrafts.set(request.id, draft);
          render();
          doc.getElementById(`steer-${segment(request.id)}-text`)?.focus({ preventScroll: true });
        }, 'quiet-button', `steer:${request.id}`);
        steer.disabled = !online || ui.loading || ui.cancelling;
        actions.append(steer);
      }
      if (request.can_cancel === true && !ui.uncertain) {
        const cancel = button(ui.cancelling ? 'Waiting for receipt…' : 'Cancel request',
          () => cancelRequest(request.id), 'danger-button', `cancel:${request.id}`);
        cancel.setAttribute('aria-label', `Cancel request ${request.id}`);
        cancel.disabled = !online || ui.loading || ui.cancelling || answerNeedsReceipt(request.id) || !!steerDraft?.attempt;
        actions.append(cancel);
      }
      card.append(actions);
      if (steerDraft?.open) card.append(renderSteer(request, steerDraft));
      else if (steerDraft?.message) card.append(element('p', 'request-message', steerDraft.message, { role: 'status' }));
      if (ui.message) card.append(element('p', 'request-message', ui.message, { role: 'status' }));
      container.append(card);
    }
  }

  function lockAuth(error) {
    access = 'reopen';
    resetActivity();
    authError = error?.message ?? '';
    if (timer !== null) win.clearInterval(timer);
    timer = null;
    render();
    refs.gateTitle.focus();
  }
  async function call(path, config) {
    if (destroyed || access !== 'active') throw new ApiError(reopenMessage, 401);
    if (!online) throw new ApiError('You’re offline. Reconnect and refresh receipts.');
    try {
      return await api(path, config);
    } catch (error) {
      if (error?.status === 401 || error?.status === 403) lockAuth(error);
      throw error;
    }
  }
  function remember(request, fromRead = false, snapshot = null) {
    const previous = requests.get(request.id);
    const previousSnapshot = requestSnapshots.get(request.id);
    const currentRead = !snapshot || !previousSnapshot
      || (snapshot.snapshotAt != null && previousSnapshot.snapshotAt != null
        ? isNewerSnapshot(snapshot, previousSnapshot) : snapshot.sequence >= previousSnapshot.sequence);
    if (!previous || request.revision > previous.revision || request.revision === previous.revision && currentRead) {
      requests.set(request.id, request);
      if (snapshot) requestSnapshots.set(request.id, snapshot);
      if (request.state === 'awaiting_input') homeReceipts.add(request.id);
      reconcileAnswer(request, fromRead);
      reconcileSteer(request);
    }
    return requests.get(request.id);
  }
  function reconcileAnswer(request, fromRead) {
    const draft = answerDrafts.get(request.id);
    const attempt = draft?.attempt;
    const ui = receiptState(request.id);
    if (!attempt) {
      if (draft?.dirty && request.state !== 'awaiting_input') {
        ui.message = 'This request no longer awaits input. Your answer draft is kept below.';
      }
      return;
    }
    if (matchesAnswer(request, attempt)) {
      answerDrafts.delete(request.id);
      ui.message = `Answer receipt received. State: ${request.state}. Revision: ${request.revision}.`;
      announce(ui.message);
    } else if (request.revision > attempt.revision || request.state !== 'awaiting_input') {
      draft.attempt = null;
      ui.message = 'The request changed. Your answer draft is kept. Review the current receipt before sending another answer.';
      announce(ui.message);
    } else if (fromRead && attempt.phase !== 'sending') attempt.checked = true;
  }
  function confirmAttempt(alias, request) {
    const draft = drafts.get(alias);
    const attempt = draft?.attempt;
    if (!attempt || !matchesAttempt(request, attempt)) return false;
    draft.attempt = null;
    if (request.state === 'stale_target') {
      draft.needsReview = true;
      draft.notice = 'The receipt reports a stale target. Your draft is kept. Refresh the project, then review its current thread.';
      const detail = details.get(alias);
      if (detail) detail.verified = false;
    } else {
      if (draft.text === attempt.text) draft.text = '';
      draft.notice = `Request receipt received: ${request.id}. State: ${request.state}.`;
    }
    announce(draft.notice);
    return true;
  }

  function loadProjects() {
    if (projectListPromise) return projectListPromise;
    if (access !== 'active' || !online || destroyed) { render(); return Promise.resolve(false); }
    const sequence = ++readSequence;
    projectListPromise = Promise.resolve().then(async () => {
      try {
        const data = await call('/mini/api/projects');
        if (!Array.isArray(data?.projects) || typeof data.work_paused !== 'boolean'
            || !Number.isFinite(data.coordinator_interval_minutes) || data.coordinator_interval_minutes <= 0) {
          throw new ApiError('The server returned an incomplete project list.');
        }
        const next = data.projects.map(checkProject);
        if (data.requests !== undefined && !Array.isArray(data.requests)) throw new ApiError('The server returned an incomplete question list.');
        const nextRequests = (data.requests ?? []).map(checkRequest);
        if (destroyed || access !== 'active') return false;
        const snapshot = responseSnapshot(data, sequence);
        if (!isNewerSnapshot(snapshot, listSnapshot)) throw new ApiError('An older project snapshot was returned. The newer known information is kept.');
        const aliases = new Set(next.map(project => project.alias));
        for (const alias of projects.keys()) {
          if (!aliases.has(alias) && isNewerSnapshot(snapshot, projectSnapshots.get(alias))) {
            const previous = projects.get(alias);
            projects.delete(alias);
            projectSnapshots.set(alias, snapshot);
            const detail = details.get(alias) ?? (selected === alias ? { project: previous } : null);
            if (detail) {
              detail.verified = false;
              detail.error = 'This project is no longer in the registered list. Its last known information and your draft are kept.';
              details.set(alias, detail);
            }
          }
        }
        for (const project of next) acceptProject(project, snapshot);
        listSnapshot = snapshot;
        listCheckedAt = snapshot.receivedAt;
        projectsLoaded = true;
        workPaused = data.work_paused;
        intervalMinutes = data.coordinator_interval_minutes;
        for (const request of nextRequests) remember(request, true, snapshot);
        if (data.requests !== undefined) requestsLoaded = true;
        listError = '';
        return true;
      } catch (error) {
        listError = `Could not refresh projects. ${error?.message ?? 'Please try again.'} Last loaded information is kept.`;
        return false;
      } finally {
        projectListPromise = null;
        render();
      }
    });
    render();
    return projectListPromise;
  }

  function loadRequests() {
    if (requestListPromise) return requestListPromise;
    if (access !== 'active' || !online || destroyed) { render(); return Promise.resolve(false); }
    const sequence = ++readSequence;
    requestListPromise = Promise.resolve().then(async () => {
      try {
        const data = await call('/mini/api/requests');
        if (!Array.isArray(data?.requests)) throw new ApiError('The server returned an incomplete request list.');
        const next = data.requests.map(checkRequest);
        if (destroyed || access !== 'active') return false;
        const snapshot = responseSnapshot(data, sequence);
        for (const request of next) remember(request, true, snapshot);
        requestsLoaded = true;
        requestListError = '';
        return true;
      } catch (error) {
        requestListError = `Could not refresh questions and receipts. ${error?.message ?? 'Please try again.'} Last loaded receipts and drafts are kept.`;
        return false;
      } finally {
        requestListPromise = null;
        render();
      }
    });
    render();
    return requestListPromise;
  }

  function loadProject(alias) {
    if (!alias || destroyed || access !== 'active' || !online) { render(); return Promise.resolve(false); }
    if (projectLoads.has(alias)) return projectLoads.get(alias);
    const sequence = ++readSequence;
    const promise = Promise.resolve().then(async () => {
      try {
        const data = await call(`/mini/api/projects/${segment(alias)}`);
        const project = checkProject(data?.project);
        if (project.alias !== alias || !Array.isArray(data.requests)) throw new ApiError('The returned project does not match this view.');
        const nextRequests = data.requests.map(checkRequest);
        if (nextRequests.some(request => !request.targets.some(target => target.alias === alias))) {
          throw new ApiError('The server returned a receipt for a different project.');
        }
        if (destroyed || access !== 'active') return false;
        const snapshot = responseSnapshot(data, sequence);
        const accepted = acceptProject(project, snapshot);
        const current = projects.get(alias);
        if (!current || current.thread_id !== project.thread_id
            || current.watch_thread_id !== project.watch_thread_id) {
          throw new ApiError('A newer project registry changed this target. Refresh to review the current thread.');
        }
        // Even when an older project body is discarded, actual receipt reads
        // are reconciled by their own revisions, never inferred from progress.
        details.set(alias, { project: current, verified: true, error: '',
          ...(accepted ? {} : { note: 'A newer project snapshot is already displayed.' }) });
        for (const item of nextRequests) {
          const request = remember(item, true, snapshot);
          // Text alone is not an identity. A lost POST can be reconciled here
          // only if the server's actual ID equals the retained retry UUID.
          const attempt = drafts.get(alias)?.attempt;
          if (attempt && request.id === attempt.key) confirmAttempt(alias, request);
        }
        const attempt = drafts.get(alias)?.attempt;
        if (attempt && attempt.phase !== 'sending') attempt.checked = true;
        return true;
      } catch (error) {
        const previous = details.get(alias);
        details.set(alias, {
          ...previous, verified: false,
          error: `Could not refresh this project. ${error?.message ?? 'Please try again.'} Your draft and last receipts are kept.`,
        });
        return false;
      } finally {
        projectLoads.delete(alias);
        render();
      }
    });
    projectLoads.set(alias, promise);
    render();
    return promise;
  }

  async function refresh() {
    const alias = selected;
    const results = await Promise.allSettled([loadProjects(), loadRequests(), ...(alias ? [loadProject(alias)] : [])]);
    if (results.every(result => result.status === 'fulfilled' && result.value)) {
      announce('Project view and receipts checked. Report dates show when source information was last updated.');
    }
  }
  function openProject(alias) {
    if (access !== 'active' || !projects.has(alias)) return Promise.resolve(false);
    if (selected !== alias) resetActivity();
    selected = alias;
    draftFor(alias);
    render();
    refs.projectTitle.focus({ preventScroll: true });
    return loadProject(alias);
  }
  function showProjects() {
    resetActivity();
    selected = null;
    render();
    refs.listTitle.focus({ preventScroll: true });
  }
  function reviewScope() {
    const detail = details.get(selected);
    const draft = drafts.get(selected);
    if (!draft || draft.attempt || !detail?.verified || detail.error || projectLoads.has(selected)) return;
    draft.thread = detail.project.thread_id;
    draft.needsReview = false;
    draft.notice = 'Current project thread selected. Review your draft before sending.';
    render();
    refs.prompt.focus();
  }

  async function submit() {
    const alias = selected;
    const detail = details.get(alias);
    const draft = drafts.get(alias);
    if (!alias || !draft || !detail?.verified || detail.error || projectLoads.has(alias)
        || access !== 'active' || !online || !draft.text.trim() || draft.needsReview
        || draft.thread !== detail.project.thread_id || routeUnavailable(detail.project)
        || routeUnavailable(projects.get(alias))) return;
    if (draft.attempt && (draft.attempt.phase === 'sending' || !draft.attempt.checked)) return;
    if (!draft.attempt) {
      if (typeof crypto?.randomUUID !== 'function') {
        draft.notice = `This client could not create a secure request key. Your draft is kept; reopen in an updated ${localMode ? 'browser' : 'Telegram client'}.`;
        render();
        return;
      }
      draft.attempt = {
        key: crypto.randomUUID(), text: draft.text,
        target: { alias, thread_id: detail.project.thread_id },
        phase: 'sending', checked: false, message: '',
      };
    }
    const attempt = draft.attempt;
    if (attempt.target.thread_id !== detail.project.thread_id) return;
    attempt.phase = 'sending';
    attempt.checked = false;
    draft.notice = '';
    render();
    try {
      const data = await call('/mini/api/requests', {
        method: 'POST',
        body: { request_key: attempt.key, text: attempt.text, targets: [{ ...attempt.target }] },
      });
      const request = checkRequest(data?.request);
      if (!matchesAttempt(request, attempt)) throw new ApiError('The returned receipt does not match this prompt and project.');
      if (destroyed || access !== 'active') return;
      const latest = remember(request);
      confirmAttempt(alias, latest);
    } catch (error) {
      // Never replay a write on reconnect, timeout, polling, or navigation.
      // Retrying is an explicit action after a successful receipt refresh.
      if (draft.attempt === attempt) {
        attempt.phase = 'uncertain';
        attempt.message = `Delivery is unconfirmed. ${error?.message ?? 'No matching receipt was received.'}`;
        announce('Delivery is unconfirmed. Your draft is kept. Refresh receipts before retrying.');
      }
    } finally {
      render();
    }
  }

  async function submitAnswer(id) {
    const request = requests.get(id);
    const draft = answerDrafts.get(id);
    if (!request || !draft || !canAnswer(request, draft)) return;
    const ui = receiptState(id);
    if (!draft.attempt) draft.attempt = {
      text: draft.text, targets: draft.targets.map(target => ({ ...target })),
      revision: request.revision, phase: 'sending', checked: false,
    };
    const attempt = draft.attempt;
    attempt.phase = 'sending';
    attempt.checked = false;
    draft.dirty = true;
    ui.message = '';
    render();
    try {
      const data = await call(`/mini/api/requests/${segment(id)}/answer`, {
        method: 'POST',
        body: { text: attempt.text, targets: attempt.targets.map(target => ({ ...target })), expected_revision: attempt.revision },
      });
      const returned = checkRequest(data?.request);
      if (returned.id !== id || !matchesAnswer(returned, attempt)) throw new ApiError('A matching answer receipt was not returned.');
      if (destroyed || access !== 'active') return;
      remember(returned);
    } catch (error) {
      // The request ID and expected revision guard retries. Reads may reconcile
      // a lost reply, but never submit an answer or change its saved payload.
      if (answerDrafts.get(id) === draft && draft.attempt === attempt) {
        attempt.phase = 'uncertain';
        const result = error?.status === 400 || error?.status === 409 ? 'Answer was not accepted.' : 'Answer is unconfirmed.';
        ui.message = `${result} ${error?.message ?? 'No matching receipt was received.'} Refresh this receipt before trying again.`;
        announce(ui.message);
      }
    } finally {
      render();
    }
  }

  async function submitSteer(id) {
    const parent = requests.get(id);
    const draft = steerDrafts.get(id);
    if (!parent || !draft || !canSteer(parent, draft)) return;
    if (!draft.attempt) {
      if (typeof crypto?.randomUUID !== 'function') {
        draft.message = 'Could not create a request key. Your draft is kept.';
        render();
        return;
      }
      draft.attempt = {
        key: crypto.randomUUID(), text: draft.text, targets: parent.targets.map(target => ({ ...target })),
        revision: parent.revision, phase: 'sending', checked: false,
      };
    }
    const attempt = draft.attempt;
    attempt.phase = 'sending';
    attempt.checked = false;
    draft.message = '';
    render();
    try {
      const data = await call(`/mini/api/requests/${segment(id)}/steer`, {
        method: 'POST', body: { request_key: attempt.key, text: attempt.text, expected_revision: attempt.revision },
      });
      const returned = checkRequest(data?.request);
      if (!matchesSteer(returned, id, attempt)) throw new ApiError('A matching follow-up receipt was not returned.');
      if (destroyed || access !== 'active') return;
      remember(returned);
    } catch (error) {
      if (draft.attempt === attempt) {
        if ([400, 409].includes(error?.status)) {
          draft.attempt = null;
          draft.message = `Follow-up not accepted. ${error.message} Your draft is kept.`;
        } else {
          attempt.phase = 'uncertain';
          draft.message = 'Follow-up unconfirmed. Refresh receipt before retrying. Your draft is kept.';
        }
        announce(draft.message);
      }
    } finally { render(); }
  }

  async function refreshReceipt(id) {
    const ui = receiptState(id);
    if (ui.loading || ui.cancelling || answerDrafts.get(id)?.attempt?.phase === 'sending'
        || steerDrafts.get(id)?.attempt?.phase === 'sending'
        || !online || access !== 'active') return false;
    ui.loading = true;
    const sequence = ++readSequence;
    render();
    try {
      const data = await call(`/mini/api/requests/${segment(id)}`);
      const returned = checkRequest(data?.request);
      if (returned.id !== id) throw new ApiError('The returned receipt has a different request ID.');
      if (destroyed || access !== 'active') return false;
      const previous = requests.get(id);
      if (previous && returned.revision < previous.revision) throw new ApiError('An older receipt was returned. Refresh again to reconcile.');
      const answerAttempt = answerDrafts.get(id)?.attempt;
      const request = remember(returned, true, responseSnapshot(data, sequence));
      const steerAttempt = steerDrafts.get(id)?.attempt;
      if (steerAttempt && steerAttempt.phase !== 'sending') steerAttempt.checked = true;
      ui.uncertain = false;
      if (!answerAttempt || answerDrafts.get(id)?.attempt === answerAttempt) {
        ui.message = `Receipt refreshed. State: ${request.state}. Revision: ${request.revision}.`;
      }
      announce(ui.message);
      return true;
    } catch (error) {
      ui.message = `Could not refresh this receipt. ${error?.message ?? 'Please try again.'}${ui.uncertain ? ' Cancellation remains unconfirmed.' : ''}`;
      return false;
    } finally {
      ui.loading = false;
      render();
    }
  }

  async function cancelRequest(id) {
    const request = requests.get(id);
    const ui = receiptState(id);
    if (!request?.can_cancel || ui.cancelling || ui.loading || ui.uncertain || answerNeedsReceipt(id) || steerDrafts.get(id)?.attempt
        || !online || access !== 'active') return;
    ui.cancelling = true;
    ui.message = '';
    render();
    try {
      const data = await call(`/mini/api/requests/${segment(id)}/cancel`, {
        method: 'POST', body: { expected_revision: request.revision },
      });
      const returned = checkRequest(data?.request);
      if (returned.id !== id || returned.revision < request.revision) throw new ApiError('A matching cancellation receipt was not returned.');
      if (destroyed || access !== 'active') return;
      const latest = remember(returned);
      ui.message = `Server receipt received. State: ${latest.state}. Revision: ${latest.revision}.`;
      announce(ui.message);
    } catch (error) {
      ui.uncertain = true;
      ui.message = `Cancellation is unconfirmed. ${error?.message ?? 'No matching receipt was received.'} Refresh this receipt before trying again.`;
      announce('Cancellation is unconfirmed. Refresh the receipt to reconcile.');
    } finally {
      ui.cancelling = false;
      render();
    }
  }

  listen(refs.search, 'input', () => preserveFocus(renderProjects));
  listen(refs.prompt, 'input', () => {
    if (!selected) return;
    const draft = draftFor(selected);
    if (draft.attempt) { refs.prompt.value = draft.text; return; }
    draft.text = refs.prompt.value;
    draft.notice = '';
    const detail = details.get(selected);
    renderComposer(projects.get(selected) ?? detail?.project, detail, draft, projectLoads.has(selected));
  });
  listen(refs.form, 'submit', event => { event.preventDefault(); return submit(); });
  if (typeof win.addEventListener === 'function') {
    listen(win, 'offline', () => { online = false; render(); });
    listen(win, 'online', () => { online = true; render(); if (visible()) return refresh(); });
    listen(win, 'pageshow', () => { render(); if (visible()) return refresh(); });
    listen(win, 'focus', () => { if (visible() && online) return refresh(); });
  }
  if (typeof doc.addEventListener === 'function') {
    listen(doc, 'visibilitychange', () => {
      render();
      if (visible() && access === 'active' && online) return refresh();
    });
  }
  if (typeof telegram?.onEvent === 'function') {
    const activated = () => { hostActive = true; render(); if (online && visible()) return refresh(); };
    const deactivated = () => { hostActive = false; };
    safeTelegram('onEvent', 'activated', activated);
    safeTelegram('onEvent', 'deactivated', deactivated);
    cleanups.push(() => {
      safeTelegram('offEvent', 'activated', activated);
      safeTelegram('offEvent', 'deactivated', deactivated);
    });
  }
  safeTelegram('ready');
  safeTelegram('expand');
  render();
  const ready = access === 'active' ? refresh() : Promise.resolve(false);
  if (access === 'active' && pollIntervalMs > 0 && typeof win.setInterval === 'function') {
    timer = win.setInterval(() => {
      if (!destroyed && online && access === 'active' && visible()) void refresh();
    }, pollIntervalMs);
  }
  return {
    ready, refresh, openProject, showProjects,
    destroy() {
      destroyed = true;
      resetActivity();
      if (timer !== null) win.clearInterval(timer);
      timer = null;
      for (const cleanup of cleanups) cleanup();
    },
  };
}

// A fixture imports this module into an unmarked root and calls init explicitly.
if (typeof document !== 'undefined' && document.getElementById('miniapp')?.hasAttribute('data-miniapp-autostart')) {
  const telegram = document.defaultView?.Telegram?.WebApp;
  const api = createApi({ telegram });
  initWorkspace({ root: document.getElementById('miniapp'), telegram, api,
    mountReports: root => init({ root, telegram, api, pollIntervalMs: 0 }) });
}
