// Existing endpoints supply agent work and per-project routing receipts.
// Mutations require an explicit local Run once, exact-diff approval or Cancel.
import { receiptSummary, referenceTokens } from './request-receipts.js';
export { receiptSummary } from './request-receipts.js';
let activitySequence = 0;
export function appliedAndVerified(run) {
  return run.state === 'completed' && run.proposal?.state === 'applied'
    && run.result?.implementation === 'applied_verified'
    && /^[a-f0-9]{64}$/.test(run.result?.applied_sha256 || '')
    && run.result.applied_sha256 === run.proposal.after_sha256;
}
function agentLabel(run) {
  if (appliedAndVerified(run)) return 'Change applied · file verified';
  if (run.proposal?.state === 'applied') return 'Applied · verification needs attention';
  if (run.state === 'completed') return ({
    report: 'Report ready', inspect: 'Inspection complete', review: 'Review complete', automation: 'Workflow draft ready',
  })[run.mode] || 'Result ready';
  return ({
    queued: 'Queued', planning: 'Planning', reading: 'Reading sources', preparing: 'Preparing a change',
    delegating: 'Specialists working', needs_revision: 'Vega requested revisions', review_blocked: 'Vega review blocked',
    reviewing: 'Reviewing', verifying: 'Checking', awaiting_approval: 'Needs your review',
    applying: 'Applying the reviewed change', failed: 'Needs attention', interrupted: 'Interrupted',
    expired: 'Expired', cancelled: 'Cancelled',
  })[run.state] || String(run.state || 'Unknown').replaceAll('_', ' ');
}
const requestLabel = state => ({
  awaiting_input: 'Needs details', queued: 'Queued for routing', routing: 'Being routed',
  handled: 'Routed', stale_target: 'Destination needs review', cancelled: 'Cancelled',
  partially_cancelled: 'Partly cancelled',
})[state] || 'Routing status unavailable';
const routeLabel = state => ({
  queued: 'Queued', routing: 'Being routed', handled: 'Routed', cancelled: 'Cancelled',
  awaiting_clarification: 'Destination needs review',
})[state] || 'Receipt unavailable';

export function initWorkActivity({ root, api, project = null, pollMs = 5000, visible = () => true, allowManualStart = false } = {}) {
  const doc = root.ownerDocument, win = doc.defaultView;
  const el = (tag, text = '', cls = '') => {
    const node = doc.createElement(tag);
    node.textContent = text;
    if (cls) node.className = cls;
    return node;
  };
  const button = (label, action, cls = '') => {
    const node = el('button', label, cls);
    node.type = 'button'; node.onclick = action;
    return node;
  };
  let disposed = false, runs = [], selected = null, busy = false, loading = null, sequence = 0;
  let reviewedKey = null, displayedKey = null, listSignature = '', detailSignature = '', actionError = '';
  let requests = [], workPaused = false, tab = 'agents', requestSignature = '';
  let codex = null, codexSignature = '';
  let projectActivity = {}, projectSignature = '', historyOpen = false;
  let detailRunId = null, disclosures = [];
  const disclosureStates = new Map();
  const expandedRequests = new Set();
  const uncertain = new Map();
  const active = () => !disposed && !doc.hidden && visible();
  const inScope = run => !project || run.project === project;
  const inRequestScope = request => !project || request.targets?.some(target => target.alias === project);
  const requestState = request => {
    if (!project) {
      if (request.routes.some(route => !request.targets.some(target => target.alias === route.task
        && target.thread_id === route.thread_id))) return 'stale_target';
      return request.state;
    }
    const target = request.targets.find(item => item.alias === project);
    const route = request.routes.find(item => item.task === project && item.thread_id === target?.thread_id);
    if (!route) return request.state === 'awaiting_input' ? request.state : 'stale_target';
    return route.connection_changed || route.state === 'awaiting_clarification' ? 'stale_target' : route.state;
  };
  const stateLabel = state => String(state || 'Unknown').replaceAll('_', ' ');
  const reviewKey = run => JSON.stringify([run.id, run.project, run.proposal?.id, run.proposal?.path,
    run.proposal?.revision, run.proposal?.review_hash, run.proposal?.diff]);
  const receiptKey = run => JSON.stringify([run.state, reviewKey(run), run.manual_started_at || null]);
  const short = (value, length = 52) => String(value || '').length > length ? String(value).slice(0, length - 1) + '…' : String(value || '');
  const workTitle = run => ({ inspect: 'Inspection', report: 'Project report', review: 'File review', change: 'Change review', automation: 'Workflow draft' })[run.mode]
    || (run.proposal ? 'Change review' : 'Work result');
  const fileName = run => String(run.path || run.proposal?.path || '').split(/[\\/]/).filter(Boolean).at(-1) || '';
  const createdTime = (run, compact = false) => {
    if (typeof run.created_at !== 'number' || !Number.isFinite(run.created_at) || run.created_at <= 0) return '';
    const date = new Date(run.created_at * 1000);
    return Number.isFinite(date.getTime()) ? date.toLocaleString(undefined, {
      month: 'short', day: 'numeric', ...(compact ? {} : { year: 'numeric' }),
      hour: '2-digit', minute: '2-digit',
    }) : '';
  };
  const reviewable = run => run.state === 'awaiting_approval' && run.proposal?.can_apply === true
    && (run.delegation_version !== 1 || run.vega_review?.status === 'accepted')
    && run.proposal.state === 'awaiting_approval' && !!run.proposal.diff?.trim()
    && typeof run.proposal.review_hash === 'string' && !!run.proposal.review_hash.trim()
    && Number.isSafeInteger(run.proposal.revision) && run.proposal.revision >= 0;

  root.classList.add('work-activity', 'team-panel');
  const prefix = `vega-activity-${++activitySequence}`;
  const icon = path => {
    const svg = doc.createElementNS('http://www.w3.org/2000/svg', 'svg');
    for (const [key, value] of Object.entries({ viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor',
      'stroke-width': '1.7', 'stroke-linecap': 'round', 'stroke-linejoin': 'round', 'aria-hidden': 'true' })) svg.setAttribute(key, value);
    const line = doc.createElementNS('http://www.w3.org/2000/svg', 'path'); line.setAttribute('d', path);
    svg.append(line); return svg;
  };
  const count = el('span', 'Checking…', 'activity-badge'); count.id = `${prefix}-status`; count.setAttribute('role', 'status');
  const trigger = button('', () => open(), 'activity-trigger');
  trigger.setAttribute('aria-label', 'Vega work'); trigger.title = 'Agents, skills and project requests';
  trigger.setAttribute('aria-describedby', count.id); trigger.setAttribute('aria-haspopup', 'dialog');
  trigger.setAttribute('aria-expanded', 'false'); trigger.setAttribute('aria-controls', `${prefix}-sheet`);
  trigger.append(icon('M8 7a4 4 0 1 0 8 0 4 4 0 0 0-8 0M4 21v-2a8 8 0 0 1 16 0v2M4 8a3 3 0 0 0 0 6M20 8a3 3 0 0 1 0 6'),
    icon('m12 3 2.4 6.6L21 12l-6.6 2.4L12 21l-2.4-6.6L3 12l6.6-2.4Z'), count);
  const sheet = el('dialog', '', 'activity-sheet'); sheet.id = `${prefix}-sheet`;
  sheet.setAttribute('aria-labelledby', `${prefix}-title`);
  const heading = el('h2', 'Vega work'); heading.id = `${prefix}-title`;
  const header = el('header', '', 'activity-heading');
  const closeButton = button('×', () => close(), 'activity-close');
  closeButton.setAttribute('aria-label', 'Close activity'); closeButton.title = 'Close activity';
  const refreshButton = button('↻', () => refresh(), 'activity-refresh');
  refreshButton.setAttribute('aria-label', 'Refresh activity'); refreshButton.title = 'Refresh activity';
  header.append(heading, refreshButton, closeButton);
  const pause = el('p', 'Automatic work is paused.', 'activity-pause');
  pause.setAttribute('role', 'status'); pause.hidden = true;
  const tabs = el('div', '', 'activity-tabs');
  tabs.setAttribute('role', 'tablist'); tabs.setAttribute('aria-label', 'Work activity type');
  const agentTab = button('Agents & skills', () => setTab('agents'));
  const requestTab = button('Project requests', () => setTab('requests'));
  const codexTab = button('Codex session', () => setTab('codex'));
  const tabNodes = [['agents', agentTab], ['requests', requestTab], ['codex', codexTab]];
  for (const [name, node] of tabNodes) {
    node.id = `${prefix}-${name}-tab`; node.setAttribute('role', 'tab');
    node.setAttribute('aria-controls', `${prefix}-${name}-pane`);
  }
  tabs.append(agentTab, requestTab, codexTab);
  tabs.onkeydown = event => {
    if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key) || busy) return;
    event.preventDefault();
    const choices = tabNodes.filter(([, node]) => !node.hidden);
    const index = choices.findIndex(([name]) => name === tab);
    const next = event.key === 'Home' ? choices[0] : event.key === 'End' ? choices.at(-1)
      : choices[(index + (event.key === 'ArrowLeft' ? -1 : 1) + choices.length) % choices.length];
    setTab(next[0]); next[1].focus();
  };
  const error = el('p', '', 'team-error'); error.setAttribute('role', 'status');
  const list = el('label', '', 'activity-run-picker'), select = el('select');
  select.setAttribute('aria-label', 'Work run');
  list.append(el('span', 'Recent work'), select);
  select.onchange = async () => {
    if (busy || !active()) return;
    if (selected !== select.value) reviewedKey = null;
    selected = select.value; sequence++; draw(); await readRun(selected);
  };
  const detail = el('section', '', 'team-detail');
  detail.setAttribute('aria-label', 'Selected work');
  const skills = el('details', '', 'team-skills'); skills.append(el('summary', 'Available skills'));
  const agentsCatalog = el('details', '', 'team-skills'); agentsCatalog.append(el('summary', 'Available agents'));
  const agentPane = el('div'), requestPane = el('div'), codexPane = el('div', '', 'activity-codex');
  const currentProjects = el('section', '', 'activity-current-projects');
  const requestHistory = el('section');
  for (const [name, node] of [['agents', agentPane], ['requests', requestPane], ['codex', codexPane]]) {
    node.id = `${prefix}-${name}-pane`; node.setAttribute('role', 'tabpanel');
    node.setAttribute('aria-labelledby', `${prefix}-${name}-tab`);
  }
  agentPane.append(list, detail, agentsCatalog, skills);
  requestPane.append(currentProjects, requestHistory);
  const scroll = el('div', '', 'activity-scroll');
  scroll.append(pause, tabs, error, agentPane, requestPane, codexPane);
  sheet.append(header, scroll);
  root.replaceChildren(trigger, sheet);

  function setTab(next) {
    if (busy || !active() || !['agents', 'requests', 'codex'].includes(next) || next === 'codex' && !codex) return;
    if (tab !== next) {
      tab = next; reviewedKey = null; detailSignature = '';
    }
    draw(); position();
  }
  function drawRequests() {
    const projectsKey = JSON.stringify(Object.entries(projectActivity).map(([alias, value]) => [alias, { ...value, checked_at: null }]));
    if (projectsKey !== projectSignature) {
      projectSignature = projectsKey;
      const expanded = new Set([...currentProjects.querySelectorAll?.('details[open]') || []].map(node => node.dataset.project));
      currentProjects.replaceChildren(el('h3', 'Current project activity'));
      const entries = Object.entries(projectActivity).filter(([alias]) => !project || alias === project);
      entries.sort((a, b) => (b[1].observed_at || 0) - (a[1].observed_at || 0));
      for (const [alias, activity] of entries) {
        const card = el('details', '', 'activity-project-request');
        card.dataset.project = alias; card.open = expanded.has(alias);
        const summary = el('summary');
        summary.append(el('strong', activity.label_project || alias),
          el('span', activity.label || 'Chat update unavailable', 'activity-request-state'));
        card.append(summary);
        if (activity.observed_at) card.append(el('p', `Last activity: ${new Date(activity.observed_at * 1000).toLocaleString()}`, 'team-muted'));
        if (!activity.available) card.append(el('p', 'Live update unavailable · saved text may be old.', 'team-muted'));
        if (activity.latest_update?.text) card.append(el('p', activity.latest_update.text, 'activity-receipt'));
        else card.append(el('p', 'No public chat update is available yet.', 'team-muted'));
        currentProjects.append(card);
      }
      if (!entries.length) currentProjects.append(el('p', 'Current project activity is unavailable.', 'team-muted'));
    }
    const signature = JSON.stringify(requests);
    if (requestSignature === signature) return;
    requestSignature = signature;
    requestHistory.replaceChildren();
    if (!requests.length) {
      requestHistory.append(el('p', 'No project routing requests.', 'team-muted'));
      return;
    }
    const past = el('details', '', 'activity-past-requests');
    past.open = historyOpen;
    past.addEventListener('toggle', () => { historyOpen = past.open; });
    const historical = requests.filter(request => ['handled', 'cancelled'].includes(requestState(request)));
    past.append(el('summary', `Past deliveries · ${historical.length}`),
      el('p', 'Saved delivery receipts · current activity above.', 'team-muted'));
    const pending = el('section');
    pending.append(el('h3', 'Pending requests'));
    if (historical.length === requests.length) pending.append(el('p', 'No requests waiting for delivery.', 'team-muted'));
    requestHistory.append(pending, past);
    const timestamp = value => Number.isFinite(value) && value > 0
      ? new Date(value * 1000).toLocaleString() : '';
    for (const request of requests) {
      const card = el('details', '', 'activity-project-request');
      card.dataset.requestId = request.id; card.open = expandedRequests.has(request.id);
      card.addEventListener('toggle', () => {
        if (card.open) expandedRequests.add(request.id); else expandedRequests.delete(request.id);
      });
      const targets = request.targets.filter(target => !project || target.alias === project);
      const names = targets.map(target => target.label || target.alias).join(', ') || 'Project request';
      const summary = el('summary');
      summary.append(el('strong', short(names, 80)), el('span', requestLabel(requestState(request)), 'activity-request-state'));
      card.append(summary, el('p', timestamp(request.created_at), 'team-muted'));
      if (request.text) card.append(el('p', short(request.text.replaceAll('**', ''), 130)));
      if (request.question?.fields?.length) {
        const questions = el('ul', '', 'activity-request-questions');
        for (const field of request.question.fields) questions.append(el('li', field.label));
        card.append(questions);
      }
      for (const target of targets) {
        const route = request.routes.find(item => item.task === target.alias && item.thread_id === target.thread_id);
        const item = el('section', '', 'activity-route');
        item.append(el('h4', target.label || target.alias),
          el('p', route?.connection_changed ? 'Connection changed' : routeLabel(route?.state), 'team-state'));
        const milestones = el('ol', '', 'activity-route-steps');
        milestones.setAttribute('aria-label', `Routing steps for ${target.label || target.alias}`);
        for (const [label, done] of [
          ['Saved', true], ['Queued', !!route], ['Routed', route?.state === 'handled'],
        ]) {
          const step = el('li', `${done ? '✓' : '○'} ${label}`);
          step.dataset.complete = String(done); milestones.append(step);
        }
        item.append(milestones);
        if (route?.receipt) {
          item.append(el('p', route.connection_changed ? route.blocker
            : receiptSummary(route.receipt, route.state), 'activity-receipt'));
          const refs = el('div', '', 'activity-references');
          const preview = el('section', '', 'activity-reference-preview'); preview.hidden = true;
          for (const token of referenceTokens(route.receipt)) {
            const label = token.startsWith('prior') ? 'Earlier request' : token.startsWith('turn') ? 'Linked chat' : token.split('/').at(-1);
            const link = button(label, async () => {
              preview.hidden = false; preview.replaceChildren(el('p', 'Loading reference…', 'team-muted'));
              try {
                const value = await api(`/api/team/requests/${request.id}/reference?ref=${encodeURIComponent(token)}`);
                if (disposed) return;
                preview.replaceChildren(el('h4', value.label || label),
                  button('Close preview', () => { preview.hidden = true; }),
                  el('p', value.state || (value.available ? 'Reference preview' : 'Reference unavailable'), 'team-muted'));
                if (value.recorded_at) preview.append(el('p', timestamp(value.recorded_at), 'team-muted'));
                preview.append(el('div', value.text || 'No content available.', 'activity-artifact-text'));
              } catch (failure) { preview.replaceChildren(el('p', failure.message, 'team-error')); }
            }, 'activity-reference');
            link.title = token; refs.append(link);
          }
          const full = el('details');
          full.append(el('summary', 'Full receipt'), el('p', route.receipt, 'activity-receipt'));
          full.append(el('p', `Picked up: ${route.claimed_at ? timestamp(route.claimed_at) : 'Not recorded'}`, 'team-muted'),
            el('p', 'Project acceptance: not independently verified.', 'team-muted'));
          const recorded = route.receipt_recorded_at ?? (route.state === 'handled' ? route.handled_at : null);
          if (recorded) full.append(el('p', `Routing receipt: ${timestamp(recorded)}`, 'team-muted'));
          item.append(refs, preview, full);
        }
        card.append(item);
      }
      const prompt = el('details', '', 'activity-request');
      prompt.append(el('summary', 'Original request'), el('p', request.text || 'No prompt supplied yet.'));
      const receipt = el('p', `Request ${request.id}`, 'team-muted');
      card.append(prompt, receipt); (historical.includes(request) ? past : pending).append(card);
    }
  }

  function remember(run, expectedId) {
    if (!run || typeof run.id !== 'string' || !inScope(run) || expectedId && run.id !== expectedId) {
      throw new Error('The server returned no matching work receipt.');
    }
    const prior = runs.find(item => item.id === run.id);
    if (prior && prior.project !== run.project) throw new Error('The receipt did not match this project.');
    if (prior && (Number(run.updated_at) < Number(prior.updated_at)
      || Number(run.proposal?.revision) < Number(prior.proposal?.revision))) return prior;
    runs = runs.some(item => item.id === run.id)
      ? runs.map(item => item.id === run.id ? run : item) : [...runs, run];
    if (uncertain.has(run.id) && uncertain.get(run.id) !== receiptKey(run)) {
      uncertain.delete(run.id); actionError = '';
    }
    return run;
  }

  function draw() {
    const waiting = runs.filter(run => run.state === 'awaiting_approval').length;
    const working = runs.filter(run => ['planning','reading','delegating','preparing','reviewing','verifying','applying','running'].includes(run.state)).length;
    const queuedAgents = runs.filter(run => run.state === 'queued').length;
    const revisions = runs.filter(run => ['needs_revision','review_blocked'].includes(run.state)).length;
    const queued = requests.filter(request => ['queued', 'routing'].includes(requestState(request))).length;
    const needsDetails = requests.filter(request => ['awaiting_input', 'stale_target', 'partially_cancelled'].includes(requestState(request))).length;
    count.textContent = [working ? `${working} working` : '', waiting ? `${waiting} to review` : '',
      queuedAgents ? `${queuedAgents} queued` : '', revisions ? `${revisions} need changes` : '',
      needsDetails ? `${needsDetails} need details` : '', queued ? `${queued} routing` : ''].filter(Boolean).join(' · ')
      || (runs.length + requests.length ? `${runs.length + requests.length} recent` : 'No work yet');
    if (codex?.available && codex.state === 'working') count.textContent = 'Codex working';
    trigger.dataset.state = waiting || needsDetails || revisions ? 'review' : working || queued ? 'working' : 'idle';
    pause.hidden = !workPaused;
    agentTab.textContent = `Agents & skills${runs.length ? ` · ${runs.length}` : ''}`;
    requestTab.textContent = `Project requests${requests.length ? ` · ${requests.length}` : ''}`;
    codexTab.hidden = !codex;
    for (const [name, node] of tabNodes) {
      node.setAttribute('aria-selected', String(tab === name));
      node.tabIndex = tab === name ? 0 : -1; node.disabled = busy;
    }
    agentPane.hidden = tab !== 'agents'; requestPane.hidden = tab !== 'requests'; codexPane.hidden = tab !== 'codex';
    drawCodex();
    drawRequests();
    error.textContent = actionError;
    const run = runs.find(item => item.id === selected) || runs.find(item => item.state === 'awaiting_approval') || runs[0];
    selected = run?.id ?? null;
    list.hidden = runs.length < 2; select.disabled = busy;
    const options = runs.map(item => ({
      id: item.id,
      label: [short(item.label || item.project, 28), short(fileName(item) || workTitle(item), 32),
        agentLabel(item), createdTime(item, true)].filter(Boolean).join(' · '),
    }));
    const nextList = JSON.stringify([selected, options]);
    if (listSignature !== nextList) {
      listSignature = nextList; select.replaceChildren();
      for (const item of options) {
        const option = el('option', item.label);
        option.value = item.id; select.append(option);
      }
      select.value = selected || '';
    }
    if (!run) {
      detail.replaceChildren(el('p', 'Work started in conversation will appear here.', 'team-muted'));
      detailSignature = ''; displayedKey = null; reviewedKey = null;
      detailRunId = null; disclosures = []; disclosureStates.clear();
      if (sheet.open) position();
      return;
    }
    const key = reviewKey(run);
    if (displayedKey !== key || !reviewable(run)) reviewedKey = null;
    displayedKey = key;
    const signature = JSON.stringify([run, busy, uncertain.has(run.id)]);
    if (detailSignature === signature) return;
    detailSignature = signature;
    const sameRun = detailRunId === run.id, scrollTop = scroll.scrollTop || 0;
    const focusedDisclosure = sameRun ? disclosures.find(item => item.summary === doc.activeElement)?.key : null;
    if (detailRunId) disclosureStates.set(detailRunId, new Map(disclosures.map(item => [item.key, item.node.open])));
    while (disclosureStates.size > 30) disclosureStates.delete(disclosureStates.keys().next().value);
    detailRunId = run.id; disclosures = [];
    const disclosure = (key, label, cls = '', open = false) => {
      const node = el('details', '', cls), summary = el('summary', label);
      node.open = disclosureStates.get(run.id)?.get(key) ?? open;
      node.append(summary); disclosures.push({ key, node, summary }); return node;
    };
    detail.replaceChildren(el('h3', fileName(run) || workTitle(run)),
      el('p', `${short(run.label || run.project)} · ${agentLabel(run)}`, 'team-state'));
    if (createdTime(run)) {
      const time = el('time', `${workTitle(run)} · ${createdTime(run)}`, 'activity-run-time');
      time.setAttribute('datetime', new Date(run.created_at * 1000).toISOString()); detail.append(time);
    }
    const reviewSummary = run.delegation_version === 1 ? run.vega_review?.summary : null;
    const reviewFindings = run.delegation_version === 1 ? run.vega_review?.findings || [] : [];
    if (run.error && run.error !== reviewSummary) detail.append(el('p', run.error, 'team-error'));
    if (allowManualStart && run.can_start && run.state === 'queued') {
      const start = button('Run once', () => mutate(run, 'start', {}), 'team-primary');
      start.disabled = busy || uncertain.has(run.id);
      const purpose = run.mode === 'change' ? 'Prepare a change to this file. Review and apply it separately.' : 'Run this read-only task.';
      detail.append(el('p', `${purpose}${workPaused ? ' Automatic work stays paused.' : ''}`, 'team-muted'), start);
    } else if (run.manual_started_at && ['queued','planning','reading','delegating','reviewing','verifying'].includes(run.state)) {
      detail.append(el('p', 'This task was started once by you.', 'team-muted'));
    }
    if (run.delegation_version === 1) {
      const review = el('section', '', 'activity-vega-review');
      const verdict = run.vega_review?.status
        || (['failed','cancelled','interrupted','expired'].includes(run.state) ? 'not_run' : 'pending');
      review.dataset.verdict = verdict;
      review.append(el('h4', 'Vega review'), el('strong', ({
        accepted: 'Accepted', needs_revision: 'Needs revisions', blocked: 'Review blocked',
        pending: 'Pending specialist results', not_run: 'Not run',
      })[verdict] || 'Review unavailable'));
      if (run.vega_review?.summary) review.append(el('p', run.vega_review.summary));
      if (reviewFindings.length) {
        const findings = disclosure('review-findings', `Findings · ${reviewFindings.length}`, 'activity-findings');
        const items = el('ul');
        for (const finding of reviewFindings) items.append(el('li', finding));
        findings.append(items); review.append(findings);
      }
      for (const change of run.vega_review?.required_changes || []) review.append(el('p', change, 'team-error'));
      if (verdict === 'accepted' && run.proposal?.state !== 'applied' && run.proposal) {
        review.append(el('p', 'Reviewed by Vega. Approve the diff to apply.', 'team-muted'));
      }
      detail.append(review);
    }
    // Keep the exact change and its consent together, before optional details.
    if (run.proposal) {
      const proposal = run.proposal, diff = el('pre', proposal.diff || 'No reviewable diff is available.', 'team-diff');
      diff.tabIndex = 0; diff.setAttribute('role', 'region'); diff.setAttribute('aria-label', `Exact diff for ${proposal.path}`);
      detail.append(el('h4', proposal.path), el('p', proposal.summary || ''), diff);
      const actions = el('div', '', 'activity-actions');
      if (reviewable(run)) {
        const review = el('label', '', 'team-consent'), check = el('input'); check.type = 'checkbox';
        check.checked = reviewedKey === key; check.disabled = busy || uncertain.has(run.id);
        review.append(check, doc.createTextNode(' I reviewed this exact diff.'));
        const apply = button('Apply reviewed change', () => {
          const current = runs.find(item => item.id === run.id);
          if (!check.checked || reviewedKey !== key || !current || reviewKey(current) !== key || !reviewable(current)) return;
          return mutate(current, 'apply', {
            approved: true, expected_revision: proposal.revision, review_hash: proposal.review_hash,
          });
        }, 'team-primary');
        apply.disabled = !check.checked || check.disabled;
        check.onchange = () => { reviewedKey = check.checked ? key : null; apply.disabled = !check.checked || busy || uncertain.has(run.id); };
        detail.append(review); actions.append(apply);
      }
      if (run.can_cancel) {
        const cancel = button('Cancel work', () => mutate(run, 'cancel', {}));
        cancel.disabled = busy || uncertain.has(run.id); actions.append(cancel);
      }
      detail.append(actions);
    } else if (run.can_cancel) {
      const cancel = button('Cancel work', () => mutate(run, 'cancel', {}));
      cancel.disabled = busy || uncertain.has(run.id); detail.append(cancel);
    }
    const staleChecks = run.result?.functional_checks?.state === 'passed' && !run.checks_current;
    if (run.check_recipe) {
      const checks = run.result?.functional_checks;
      if (checks) detail.append(el('p', staleChecks ? 'Source changed. Run checks again.' : checks.detail,
        checks.state === 'passed' ? 'team-muted' : 'team-error'));
      if (!run.checks_current) {
        const verify = button(`Run ${run.check_recipe.label.toLowerCase()}`, () => mutate(run, 'check', {
          expected_sha256: run.result.applied_sha256,
        }), 'team-primary');
        verify.disabled = busy || uncertain.has(run.id);
        detail.append(verify);
      }
    }
    if (run.agents?.length) {
      const agents = el('section', '', 'activity-agent-results');
      agents.append(el('h4', 'Specialist work'));
      for (const agent of run.agents) {
        const card = disclosure(`agent:${agent.id}`, '', 'activity-agent');
        const summary = disclosures.at(-1).summary;
        summary.append(el('strong', agent.label || agent.role), el('span', stateLabel(agent.state), 'activity-request-state'));
        card.append(el('p', agent.task || '', 'team-muted'));
        if (agent.error) card.append(el('p', agent.error, 'team-error'));
        if (agent.result?.summary) card.append(el('p', agent.result.summary));
        if (agent.result?.artifact) {
          const artifact = agent.result.artifact;
          const output = disclosure(`artifact:${agent.id}`, `Draft · ${artifact.title}`, 'activity-agent-artifact');
          const isCode = ['automation_workflow', 'change_proposal'].includes(artifact.kind);
          output.append(el(isCode ? 'pre' : 'div', artifact.body, 'activity-artifact-text'));
          card.append(output);
        }
        if (agent.result?.citations?.length) {
          const sources = disclosure(`sources:${agent.id}`, 'Source evidence');
          for (const citation of agent.result.citations) {
            sources.append(el('p', `${citation.path || citation.source_id} · lines ${citation.start_line}–${citation.end_line}`, 'team-muted'));
            if (citation.excerpt) sources.append(el('pre', citation.excerpt, 'activity-artifact-text'));
            if (citation.excerpt_truncated) sources.append(el('p', 'Shortened source preview.', 'team-muted'));
          }
          card.append(sources);
        }
        card.append(el('p', `${agent.model_requests ?? 0} model request${agent.model_requests === 1 ? '' : 's'}`, 'team-muted'));
        agents.append(card);
      }
      detail.append(agents);
    }
    const more = disclosure('more', 'Work details', 'activity-more');
    const request = disclosure('request', 'Request', 'activity-request');
    request.append(el('p', run.goal));
    if (run.path || run.proposal?.path) request.append(el('p', run.path || run.proposal.path, 'team-muted'));
    more.append(request);
    const steps = disclosure('steps', `Agents & skills used · ${run.nodes?.length || 0}`, 'activity-steps');
    const nodes = el('ol', '', 'team-nodes');
    nodes.setAttribute('aria-label', 'Agent and skill activity');
    for (const node of run.nodes || []) {
      const item = el('li', '', 'team-node'); item.dataset.parent = node.parent_id || '';
      item.append(el('strong', short(node.role)), el('span',
        [node.skill, node.execution, stateLabel(node.state)].filter(Boolean).join(' · '), 'team-muted'));
      if (node.summary) item.append(el('p', node.summary));
      nodes.append(item);
    }
    steps.append(nodes); more.append(steps);
    if (run.result) {
      const additionalFindings = (run.result.findings || []).filter(finding => !reviewFindings.includes(finding));
      if (run.result.summary !== reviewSummary || additionalFindings.length || appliedAndVerified(run)) {
        const result = disclosure('result', 'Result', 'activity-result', run.state === 'completed');
        if (run.result.summary !== reviewSummary) result.append(el('p',
          staleChecks ? 'Change applied. Previous checks need a fresh run.' : run.result.summary || ''));
        const findings = el('ul');
        for (const finding of additionalFindings) findings.append(el('li', finding));
        result.append(findings);
        if (appliedAndVerified(run)) result.append(el('p', 'Applied and hash-verified.', 'team-state'));
        (run.state === 'completed' ? detail : more).append(result);
      }
      if (run.result.verification?.length || run.result.functional_tests_run === false) {
        const checks = disclosure('checks', 'Checks performed', 'activity-checks');
        for (const check of run.result.verification || []) checks.append(el('p', `${check.state}: ${check.detail}`, 'team-muted'));
        if (run.result.functional_tests_run === false) checks.append(el('p', 'Functional tests were not run.', 'team-muted'));
        more.append(checks);
      }
    }
    const usage = disclosure('usage', 'Usage & receipts', 'team-usage');
    usage.append(el('p',
      `${run.usage?.model_requests ?? 0}/${run.usage?.max_requests ?? 4} model requests${run.model ? ` · ${run.model}` : ''}`, 'team-muted'));
    for (const response of run.responses || []) usage.append(el('p', `Delivery: ${response.state}`, 'team-muted'));
    more.append(usage); detail.append(more);
    if (sheet.open) {
      position();
      if (focusedDisclosure) disclosures.find(item => item.key === focusedDisclosure)?.summary.focus({ preventScroll: true });
      scroll.scrollTop = sameRun ? scrollTop : 0;
    }
  }

  async function readRun(id) {
    if (!active() || busy) return;
    const version = ++sequence;
    try {
      const data = await api(`/api/team/runs/${encodeURIComponent(id)}`);
      if (!active() || version !== sequence) return;
      remember(data.run, id); draw();
    } catch (e) {
      if (active() && version === sequence) error.textContent = actionError || `Activity could not be refreshed. ${e.message}`;
    }
  }

  async function mutate(run, action, body) {
    if (!sheet.open || tab !== 'agents' || !active() || busy || uncertain.has(run.id)) return;
    const current = runs.find(item => item.id === run.id);
    if (!current || selected !== current.id || receiptKey(current) !== receiptKey(run)
      || action === 'cancel' && !current.can_cancel || action === 'apply' && !reviewable(current)
      || action === 'start' && (!allowManualStart || !current.can_start || current.state !== 'queued')) return;
    busy = true; sequence++; reviewedKey = null; actionError = ''; draw();
    try {
      const data = await api(`/api/team/runs/${encodeURIComponent(run.id)}/${action}`, { method: 'POST', body });
      if (!active()) return;
      if (data.run?.project !== run.project) throw new Error('The receipt did not match this project.');
      remember(data.run, run.id);
    } catch (e) {
      if (!active()) return;
      actionError = e.message || 'The work request failed.';
      if (![400, 403, 409].includes(e.status)) {
        uncertain.set(run.id, receiptKey(run));
        actionError = `The ${action} outcome is unconfirmed. Refresh activity to check the receipt. ${actionError}`;
      }
    } finally {
      busy = false;
      if (active()) { draw(); await readRun(run.id); }
    }
  }

  function refresh() {
    if (!active() || busy) return Promise.resolve();
    if (loading) return loading;
    const version = ++sequence;
    loading = (async () => {
      refreshButton.disabled = true;
      try {
        const data = await api(project ? `/api/team/projects/${encodeURIComponent(project)}` : '/api/team');
        if (!active() || version !== sequence) return;
        if (!Array.isArray(data.runs)) throw new Error('The server returned no activity catalog.');
        const incoming = data.runs.filter(inScope);
        const selectedOutsideWindow = sheet.open && tab === 'agents' && data.runs.length >= 30
          ? runs.find(run => run.id === selected && inScope(run) && !incoming.some(item => item.id === run.id)) : null;
        for (const run of incoming) remember(run);
        runs = incoming.map(run => runs.find(item => item.id === run.id));
        if (selectedOutsideWindow) runs.push(selectedOutsideWindow);
        if (data.requests !== undefined && (!Array.isArray(data.requests)
          || data.requests.some(request => !request || typeof request.id !== 'string'
            || !Array.isArray(request.targets) || !Array.isArray(request.routes)))) {
          throw new Error('The server returned incomplete project routing receipts.');
        }
        requests = (data.requests || []).filter(inRequestScope);
        projectActivity = data.project_activity || {};
        codex = data.codex_activity && (!project || data.codex_activity.project === project) ? data.codex_activity : null;
        if (tab === 'codex' && !codex) tab = 'agents';
        workPaused = data.work_paused === true;
        if (tab !== 'codex' && !runs.length && requests.length) tab = 'requests';
        const skillSignature = JSON.stringify(data.skills || []);
        if (skills.dataset.signature !== skillSignature) {
          skills.dataset.signature = skillSignature; skills.replaceChildren(el('summary', 'Available skills'));
          for (const skill of data.skills || []) skills.append(el('p', `${skill.label}: ${skill.description}`, 'team-muted'));
          if (!data.skills?.length) skills.append(el('p', 'No skills reported.', 'team-muted'));
        }
        const agentSignature = JSON.stringify(data.agents || []);
        if (agentsCatalog.dataset.signature !== agentSignature) {
          agentsCatalog.dataset.signature = agentSignature;
          agentsCatalog.replaceChildren(el('summary', `Available agents · ${data.agents?.length || 0}`));
          for (const agent of data.agents || []) {
            agentsCatalog.append(el('p', `${agent.label}: ${agent.description}`, 'team-muted'));
          }
          if (!data.agents?.length) agentsCatalog.append(el('p', 'No specialist catalog reported.', 'team-muted'));
        }
        draw();
        if (sheet.open && tab === 'agents' && selected) await readRun(selected);
      } catch (e) {
        if (active() && version === sequence) {
          if ([401, 403].includes(e.status)) {
            runs = []; requests = []; projectActivity = {}; codex = null; reviewedKey = null; detailSignature = ''; requestSignature = '';
            draw();
          }
          else {
            projectActivity = Object.fromEntries(Object.entries(projectActivity).map(([alias, value]) => [alias, {
              ...value, available: false, label: 'Chat update unavailable',
            }]));
            drawRequests();
            if (codex) {
            codex = { ...codex, available: false, state: 'unavailable', label: 'Session unavailable',
              source_error: 'Activity could not be refreshed. Retained updates may be old.' };
            drawCodex();
            }
          }
          count.textContent = runs.length ? 'Refresh needed' : 'Activity unavailable';
          error.textContent = actionError || `Activity could not be refreshed. ${e.message}`;
        }
      } finally { refreshButton.disabled = false; loading = null; }
    })();
    return loading;
  }
  function drawCodex() {
    const signature = JSON.stringify(codex && { ...codex, checked_at: null });
    if (signature === codexSignature) return;
    codexSignature = signature;
    const expanded = codexPane.querySelector?.('details')?.open || false;
    const scrollTop = scroll.scrollTop;
    codexPane.replaceChildren();
    if (!codex) return;
    const time = value => Number.isFinite(value) && value > 0 ? new Date(value * 1000).toLocaleString() : 'Not observed';
    codexPane.append(el('h3', codex.chat_title || 'Codex session'),
      el('p', codex.label || 'Session unavailable', 'activity-codex-state'),
      el('p', `Last activity: ${time(codex.observed_at)}`, 'team-muted'));
    if (codex.source_error) codexPane.append(el('p', codex.source_error, 'team-error'));
    if (!codex.available && !codex.source_error) codexPane.append(el('p', 'A current session update is not available.', 'team-muted'));
    if (codex.latest_update?.text) codexPane.append(
      el('p', `Public update: ${time(codex.latest_update.at)}`, 'team-muted'),
      el('div', codex.latest_update.text, 'activity-artifact-text'));
    const updates = el('details'); updates.open = expanded;
    updates.append(el('summary', 'Recent activity'));
    const list = el('ol', '', 'activity-codex-events');
    for (const event of [...(codex.updates || [])].reverse().slice(0, 24)) {
      const item = el('li');
      item.append(el('time', time(event.at), 'team-muted'), el('p', event.text));
      list.append(item);
    }
    updates.append(list); codexPane.append(updates);
    codexPane.append(el('p', codex.note || 'Observed from the linked local chat.', 'team-muted'));
    scroll.scrollTop = scrollTop;
  }
  function position() {
    if (!sheet.open) return;
    const anchor = trigger.getBoundingClientRect();
    const viewport = win.visualViewport;
    const topEdge = viewport?.offsetTop || 0, leftEdge = viewport?.offsetLeft || 0;
    const height = viewport?.height || win.innerHeight, width = viewport?.width || win.innerWidth;
    const top = Math.max(topEdge + 12, Math.min(anchor.bottom + 8, topEdge + height - 180));
    sheet.style.left = `${Math.max(leftEdge + 12, Math.min(anchor.right - sheet.offsetWidth, leftEdge + width - sheet.offsetWidth - 12))}px`;
    sheet.style.top = `${top}px`;
    sheet.style.maxHeight = `${Math.max(120, Math.min(680, topEdge + height - top - 12))}px`;
  }
  function open(options = {}) {
    if (!active()) return Promise.resolve();
    if (options.tab) setTab(options.tab);
    if (!sheet.open) {
      sheet.showModal(); trigger.setAttribute('aria-expanded', 'true'); position(); closeButton.focus();
    }
    return refresh();
  }
  async function openRun(identity) {
    if (!active() || !/^[a-f0-9-]{36}$/.test(identity)) return;
    await open({ tab: 'agents' });
    const response = await api(`/api/team/runs/${identity}`);
    if (!active() || !response.run || !inScope(response.run)) return;
    remember(response.run); selected = identity; tab = 'agents'; reviewedKey = null;
    listSignature = ''; detailSignature = ''; draw(); position();
  }
  function close() { if (sheet.open) sheet.close(); }
  const onClose = () => {
    trigger.setAttribute('aria-expanded', 'false');
    reviewedKey = null; detailSignature = ''; draw();
    if (active()) trigger.focus({ preventScroll: true });
  };
  const onBackdrop = event => {
    if (event.target !== sheet) return;
    const rect = sheet.getBoundingClientRect();
    if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) close();
  };
  sheet.addEventListener('close', onClose); sheet.addEventListener('click', onBackdrop);
  sheet.addEventListener('toggle', position, true);
  win.addEventListener('resize', position);
  win.visualViewport?.addEventListener('resize', position);
  const timer = pollMs > 0 ? win.setInterval(() => { if (active() && !busy) void refresh(); }, pollMs) : null;
  const ready = refresh();
  return { ready, refresh, open, openRun, close, destroy() {
    disposed = true; sequence++; reviewedKey = null;
    close();
    if (timer !== null) win.clearInterval(timer);
    sheet.removeEventListener('close', onClose); sheet.removeEventListener('click', onBackdrop);
    sheet.removeEventListener('toggle', position, true);
    win.removeEventListener('resize', position);
    win.visualViewport?.removeEventListener('resize', position);
  } };
}

// Keep the /team entry point and older imports compatible.
export const initTeam = initWorkActivity;

export function localTeamApi(fetchImpl = globalThis.fetch) {
  return async (path, { method = 'GET', body } = {}) => {
    const reference = /^\/api\/team\/requests\/[a-f0-9-]{36}\/reference\?ref=[A-Za-z0-9_.%/-]+$/.test(path);
    if (!reference && !/^\/api\/team(?:\/projects\/[a-z0-9-]+|\/runs(?:\/[a-f0-9-]{36}(?:\/(?:apply|cancel|start|check))?)?)?$/.test(path)) throw new Error('Unsupported activity endpoint.');
    if (reference && method !== 'GET') throw new Error('Reference previews are read-only.');
    if (path.startsWith('/api/team/projects/') && method !== 'GET') throw new Error('Project activity is read-only.');
    const headers = { Accept: 'application/json' };
    if (method !== 'GET') {
      const session = await fetchImpl('/api/session', { cache: 'no-store' });
      if (!session.ok) throw new Error('The private Vega session is unavailable.');
      headers['X-Vega-CSRF'] = (await session.json()).csrf; headers['Content-Type'] = 'application/json';
    }
    const activityOnly = method === 'GET' && (path === '/api/team' || path.startsWith('/api/team/projects/'));
    const response = await fetchImpl(activityOnly ? `${path}?activity=1` : path, { method, headers, cache: 'no-store',
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
      signal: AbortSignal.timeout(method === 'POST' && path.endsWith('/check') ? 95000 : 20000) });
    const data = await response.json();
    if (!response.ok) { const error = new Error(data.error || 'The activity request failed.'); error.status = response.status; throw error; }
    return data;
  };
}
