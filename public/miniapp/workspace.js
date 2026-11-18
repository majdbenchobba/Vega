import { icon, projectMark } from './visuals.js';
import { AttachmentDraft, ATTACHMENT_LIMITS } from '../composer-attachments.js';
const labels = { active: 'Running', idle: 'Idle', notLoaded: 'Not running', needsAttention: 'Needs attention', unknown: 'Unknown' };
const receipts = { accepted: 'Sent to Codex', sending: 'Sending…', uncertain: 'Delivery unconfirmed' };
export function visibleChats(snapshot, { view = 'chats', project = null, search = '', running = false } = {}) {
  const query = search.trim().toLocaleLowerCase();
  return (snapshot?.chats ?? []).filter(chat =>
    (view !== 'independent' || chat.project_id == null)
    && (!project || chat.project_id === project)
    && (!running || chat.running)
    && (!query || chat.title.toLocaleLowerCase().includes(query)));
}
export function sameReceipt(receipt, attempt) {
  return receipt?.request_key === attempt?.request_key && receipt?.thread_id === attempt?.thread_id
    && receipt?.text === attempt?.text && Object.hasOwn(receipts, receipt?.state)
    && JSON.stringify((receipt?.attachments ?? []).map(a => a.id)) === JSON.stringify(attempt?.attachment_ids ?? []);
}

export function initWorkspace({ root, api, telegram, localMode = false, mountReports,
  pollIntervalMs = 5000, now = () => Date.now(), crypto = globalThis.crypto } = {}) {
  const doc = root.ownerDocument, win = doc.defaultView;
  root.setAttribute('data-current-workspace', '');
  const tg = telegram?.WebApp ?? telegram;
  const drafts = new Map(), messages = new Map(), receiptRows = new Map(), receiptNodes = new Map(), cleanups = [];
  const previews = new Map();
  let snapshot = null, view = 'projects', project = null, selected = null, generation = 0;
  let stopped = false, loading = false, error = '', lastOk = 0, timer, reportsApp;
  let pickerChat = null;
  let active = tg?.isActive !== false;
  let auth = localMode || !!tg?.initData;
  const el = (tag, className = '', text = '', attrs = {}) => {
    const node = doc.createElement(tag); node.className = className; node.textContent = text;
    for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value);
    return node;
  };
  const on = (node, name, fn) => { node.addEventListener(name, fn); cleanups.push(() => node.removeEventListener(name, fn)); };
  const button = (label, fn, className = '', symbol = null) => {
    const node = el('button', className, '', { type: 'button' });
    if (symbol) node.append(icon(doc, symbol));
    node.append(el('span', '', label)); node.addEventListener('click', fn); return node;
  };
  const available = () => !stopped && auth && active && doc.visibilityState !== 'hidden'
    && win.navigator?.onLine !== false && view !== 'reports';
  const current = () => snapshot?.chats.find(chat => chat.id === selected && chat.host_id === 'local' && chat.kind === 'codex');
  function draft(chat) {
    if (!drafts.has(chat.id)) {
      const d = { text: '', version: chat.target_version, attempt: null, notice: '' };
      drafts.set(chat.id, d);
      d.files = new AttachmentDraft({ crypto,
        enabled: () => available() && !error && !d.attempt,
        onChange: () => { if (!stopped && selected === chat.id) renderComposer(); },
        post: async (path, body) => {
          try {
            return await api(path.replace('/api/attachments', '/mini/api/chat-attachments'), {
              method: 'POST', body: path === '/api/attachments'
                ? { ...body, thread_id: chat.id, target_version: d.version } : body,
            });
          } catch (e) {
            if (e.status === 401 || e.status === 403) { auth = false; render(); }
            throw e;
          }
        },
      });
    }
    return drafts.get(chat.id);
  }
  const shell = el('div', 'workspace-shell');
  const header = el('header', 'workspace-header');
  const brand = el('div', 'brand');
  brand.append(el('img', 'vega-icon', '', { src: '/mini/vega-icon.svg', alt: '' }), el('strong', '', 'Vega'));
  const connection = el('span', 'connection', 'Connecting…', { role: 'status' });
  const refreshButton = button('Refresh', () => refresh(), 'quiet-button icon-button', 'refresh');
  refreshButton.setAttribute('aria-label', 'Refresh'); refreshButton.setAttribute('title', 'Refresh');
  refreshButton.children[1].className = 'sr-only';
  header.append(brand, connection, refreshButton);
  const nav = el('nav', 'workspace-tabs', '', { 'aria-label': 'Workspace views' });
  const tabs = new Map();
  for (const [id, label, symbol] of [['projects', 'Projects', 'grid'], ['chats', 'All chats', 'chats'], ['independent', 'Independent', 'chat'], ['reports', 'Reports', 'report']]) {
    const tab = button(label, () => changeView(id), '', symbol); tabs.set(id, tab); nav.append(tab);
  }
  const notice = el('p', 'banner', '', { role: 'status' });
  const body = el('div', 'workspace-body');
  const listing = el('section');
  const heading = el('div', 'view-heading');
  const title = el('h1', '', 'Projects');
  const count = el('p', 'hint');
  const back = button('Projects', () => { project = null; selected = null; generation++; render(); }, 'back-button', 'back');
  heading.append(title, count);
  const filters = el('div', 'workspace-filters');
  const search = el('input', '', '', { type: 'search', placeholder: 'Search projects or chats', 'aria-label': 'Search projects or chats' });
  const searchField = el('div', 'workspace-search');
  searchField.append(icon(doc, 'search'), search);
  const running = button('Running only', () => {
    running.setAttribute('aria-pressed', running.getAttribute('aria-pressed') !== 'true' ? 'true' : 'false'); renderList();
  }, '', 'activity');
  running.setAttribute('aria-pressed', 'false');
  filters.append(searchField, running);
  const grid = el('div', 'workspace-grid');
  listing.append(back, heading, filters, grid);
  const detail = el('section', 'chat-detail');
  const detailTop = el('div', 'view-heading');
  const chatTitle = el('h1'), chatState = el('span', 'chat-state'), chatIdentity = el('div', 'chat-identity');
  const chatMark = el('span', 'chat-mark');
  chatIdentity.append(chatMark, chatTitle);
  detailTop.append(chatIdentity, chatState);
  const chatBack = button('Chats', () => { selected = null; generation++; render(); }, 'back-button', 'back');
  const chatLog = el('div', 'chat-log', '', { 'aria-label': 'Recent messages' });
  const chatNote = el('p', 'hint');
  const form = el('form', 'chat-composer');
  const prompt = el('textarea', '', '', { rows: '3', maxlength: '12000', placeholder: 'Message this chat…', 'aria-label': 'Chat prompt' });
  const sendNote = el('p', 'hint', '', { role: 'status' });
  const actions = el('div', 'composer-actions');
  const fileList = el('div', 'workspace-attachments', '', { 'aria-label': 'Attached files' });
  const picker = el('input', '', '', { type: 'file', multiple: '', 'aria-label': 'Choose files or photos' });
  picker.hidden = true;
  const attach = button('Attach', () => { pickerChat = current(); picker.value = ''; picker.click(); }, '', 'attach');
  attach.setAttribute('title', 'Up to 5 files · 10 MB each · 25 MB total');
  const review = button('Use updated chat', () => {
    const chat = current(); if (chat && !draft(chat).attempt && !draft(chat).files.entries.length) { draft(chat).version = chat.target_version; renderComposer(); }
  });
  const edit = button('Keep editing', () => {
    const chat = current(); if (!chat) return;
    const d = draft(chat);
    if (d.attempt?.state === 'uncertain') {
      const hadFiles = !!d.attempt.attachment_ids?.length;
      d.files.committed(d.attempt.attachment_ids ?? []);
      d.attempt = null;
      d.notice = hadFiles ? 'Previous files stay with that attempt. Attach files again if needed.' : '';
      renderComposer();
    }
  });
  const send = el('button', 'primary', '', { type: 'submit' }), sendLabel = el('span', '', 'Send');
  send.append(icon(doc, 'send'), sendLabel);
  actions.append(attach, review, edit, send);
  form.append(prompt, fileList, picker, sendNote, actions);
  const receiptList = el('div', 'chat-receipts');
  detail.append(chatBack, detailTop, chatNote, chatLog, form, receiptList);
  const reportsRoot = el('div', 'workspace-reports');
  body.append(listing, detail, reportsRoot);
  shell.append(header, nav, notice, body); root.replaceChildren(shell);

  function status(node, chat) {
    node.textContent = labels[chat?.status] ?? 'Unknown';
    node.dataset.state = chat?.status ?? 'unknown';
    if (error || now() - lastOk > 15000) { node.textContent = 'Last known · ' + node.textContent; node.dataset.state = 'unknown'; }
  }
  function renderList() {
    const query = search.value.trim().toLocaleLowerCase();
    const onlyRunning = running.getAttribute('aria-pressed') === 'true';
    const chats = visibleChats(snapshot, { view, project, search: search.value, running: onlyRunning })
      .sort((a, b) => Number(b.pinned) - Number(a.pinned) || a.title.localeCompare(b.title) || a.id.localeCompare(b.id));
    const all = snapshot?.chats ?? [];
    const scope = visibleChats(snapshot, { view, project });
    count.textContent = snapshot ? `${error || now() - lastOk > 15000 ? 'Last known · ' : ''}${scope.length} ${scope.length === 1 ? 'chat' : 'chats'} · ${scope.filter(c => c.running).length} running` : '';
    back.hidden = !project;
    title.textContent = project ? snapshot?.projects.find(p => p.id === project)?.label ?? 'Project unavailable'
      : view === 'projects' ? 'Projects' : view === 'independent' ? 'Independent chats' : 'All chats';
    const fresh = !error && now() - lastOk < 15000;
    const signature = JSON.stringify([view, project, query, onlyRunning, fresh, snapshot?.projects,
      [...all].sort((a, b) => a.id.localeCompare(b.id)).map(c => [
        c.id, c.title, c.project_id, c.status, c.running, c.pinned, c.host_id, c.kind,
      ])]);
    if (grid.dataset.signature === signature) return;
    grid.dataset.signature = signature;
    const focusId = grid.contains(doc.activeElement) ? doc.activeElement?.dataset?.identity : null;
    grid.replaceChildren();
    if (view === 'projects' && !project) {
      for (const p of snapshot?.projects ?? []) {
        const children = all.filter(c => c.project_id === p.id);
        if (query && !p.label.toLocaleLowerCase().includes(query)
            && !children.some(c => c.title.toLocaleLowerCase().includes(query))) continue;
        if (onlyRunning && !children.some(c => c.running)) continue;
        const card = button('', () => { project = p.id; search.value = ''; render(); }, 'workspace-card project-card');
        card.dataset.identity = p.id;
        const identity = el('span', 'workspace-card-heading');
        identity.append(projectMark(doc, p), el('strong', '', p.label), icon(doc, 'chevron'));
        const meta = el('span', 'workspace-card-meta');
        meta.append(el('span', 'hint', `${children.length} ${children.length === 1 ? 'chat' : 'chats'}`),
          el('span', 'chat-state', `${children.filter(c => c.running).length} running`,
            { 'data-state': !error && now() - lastOk < 15000 && children.some(c => c.running) ? 'active' : 'idle' }));
        card.append(identity, meta);
        grid.append(card);
      }
    } else {
      for (const chat of chats) {
        const card = button('', () => openChat(chat.id), 'workspace-card');
        card.dataset.identity = chat.id;
        card.disabled = chat.host_id !== 'local' || chat.kind !== 'codex';
        const badge = el('span', 'chat-state'); status(badge, chat);
        const p = snapshot.projects.find(p => p.id === chat.project_id);
        const identity = el('span', 'workspace-card-heading'), meta = el('span', 'workspace-card-meta');
        identity.append(projectMark(doc, p ?? (chat.project_id == null ? null : {}), chat),
          el('strong', '', chat.title), icon(doc, 'chevron'));
        meta.append(el('span', 'hint', chat.project_id == null ? 'Independent' : p?.label ?? 'Project unavailable'), badge);
        card.append(identity, meta);
        grid.append(card);
      }
    }
    if (!grid.children.length) grid.append(el('p', 'empty', loading ? 'Loading…' : 'No matches.'));
    if (focusId) [...grid.children].find(c => c.dataset.identity === focusId)?.focus({ preventScroll: true });
  }
  function renderComposer() {
    const chat = current();
    const d = chat ? draft(chat) : selected ? drafts.get(selected) : null;
    if (d && prompt.value !== d.text) prompt.value = d.text;
    prompt.readOnly = !!d?.attempt;
    if (d) d.files.locked = !!d.attempt;
    attach.disabled = !chat?.can_send || !available() || !!error || !!d?.attempt
      || d?.version !== chat?.target_version || (d?.files.entries.length ?? 0) >= ATTACHMENT_LIMITS.count;
    send.disabled = !chat?.can_send || !auth || !available() || !!error || !(d?.text.trim() || d?.files.ids.length)
      || !d?.files.ready
      || !!d?.attempt || d?.version !== chat?.target_version;
    sendLabel.textContent = chat?.running ? 'Send follow-up' : 'Send';
    review.hidden = !chat || !d || d.version === chat.target_version || !!d.attempt;
    review.disabled = !!d?.files.entries.length;
    edit.hidden = d?.attempt?.state !== 'uncertain';
    sendNote.textContent = !chat ? 'This chat is no longer in the current list.'
      : d.version !== chat.target_version ? (d.files.entries.length ? 'The chat moved. Remove its files before updating the destination.' : 'The chat moved. Review its destination before sending.')
      : d.attempt?.state === 'uncertain' ? 'Delivery unconfirmed. Check the chat before sending again.'
      : d.attempt ? receipts[d.attempt.state] ?? 'Checking receipt…' : d.notice;
    renderFiles(d);
  }
  function renderFiles(d) {
    const entries = d?.files.entries ?? [];
    const liveKeys = new Set([...drafts.values()].flatMap(value => value.files.entries.map(f => f.key)));
    for (const [key, url] of previews) if (!liveKeys.has(key)) { win.URL?.revokeObjectURL(url); previews.delete(key); }
    const signature = JSON.stringify([selected, d?.files.snapshot(), !!d?.attempt, available(), error]);
    if (fileList.dataset.signature === signature) return;
    fileList.dataset.signature = signature;
    const focused = fileList.contains(doc.activeElement) ? doc.activeElement.getAttribute('aria-label') : null;
    fileList.replaceChildren(); fileList.hidden = !entries.length;
    for (const item of entries) {
      const row = el('div', 'workspace-attachment'), copy = el('div', 'attachment-copy');
      const allowedPreview = item.attachment?.kind === 'image' && item.phase === 'ready';
      if (allowedPreview && item.file && win.URL?.createObjectURL && !previews.has(item.key)) {
        previews.set(item.key, win.URL.createObjectURL(item.file));
      }
      row.append(previews.has(item.key) ? el('img', 'attachment-preview', '', { src: previews.get(item.key), alt: item.name }) : icon(doc, 'report'));
      copy.append(el('span', '', item.name), el('small', '', item.error || (item.phase === 'ready'
        ? `${Math.max(1, Math.ceil(item.size / 1024))} KB` : item.phase === 'removing' ? 'Removing…' : 'Uploading…')));
      row.append(copy);
      if (item.phase.endsWith('_error')) {
        const retry = button('Retry', () => fileAction(d, () => d.files.retry(item.key)));
        retry.disabled = !!d.attempt || !available() || !!error;
        retry.setAttribute('aria-label', 'Retry ' + item.name); row.append(retry);
      }
      const remove = button('×', () => fileAction(d, () => d.files.remove(item.key)));
      remove.setAttribute('aria-label', 'Remove ' + item.name);
      remove.disabled = !!d.attempt || item.busy || !available() || !!error;
      row.append(remove); fileList.append(row);
    }
    if (focused) (fileList.querySelectorAll('button').length && [...fileList.querySelectorAll('button')]
      .find(node => node.getAttribute('aria-label') === focused && !node.disabled) || attach).focus({ preventScroll: true });
  }
  async function fileAction(d, action) {
    try { d.notice = ''; await action(); }
    catch (e) { d.notice = e.message || 'The file could not be added.'; }
    if (!stopped) renderComposer();
  }
  function renderMessages() {
    const value = messages.get(selected);
    const beforeTop = chatLog.scrollTop;
    const nearBottom = chatLog.scrollHeight - chatLog.clientHeight - beforeTop < 60;
    const signature = JSON.stringify(value?.messages ?? []);
    if (chatLog.dataset.signature !== signature) {
      chatLog.dataset.signature = signature;
      chatLog.replaceChildren();
      for (const message of value?.messages ?? []) {
        const item = el('article', 'chat-message ' + message.role);
        item.append(el('small', '', message.role === 'user' ? 'You' : 'Codex'), el('p', '', message.text));
        chatLog.append(item);
      }
      chatLog.scrollTop = nearBottom ? chatLog.scrollHeight : beforeTop;
    }
    chatNote.textContent = !value ? 'Loading messages…' : value.has_more ? 'Recent messages' : '';
    const rows = [];
    for (const r of receiptRows.get(selected) ?? []) {
      const key = selected + ':' + r.request_key;
      let item = receiptNodes.get(key);
      if (!item) {
        item = el('details', 'receipt-card');
        item.append(el('summary'), el('p'));
        receiptNodes.set(key, item);
      }
      const signature = JSON.stringify([r.state, r.text, r.attachments]);
      if (item.dataset.signature !== signature) {
        item.dataset.signature = signature;
        item.children[0].replaceChildren(icon(doc, r.state === 'accepted' ? 'check' : 'clock'),
          el('span', '', receipts[r.state] ?? 'Receipt'));
        item.children[1].textContent = [r.text, ...(r.attachments ?? []).map(a => a.name)].filter(Boolean).join('\n');
      }
      rows.push(item);
    }
    // Keep open details and focus during polling, receipt updates and navigation.
    if (rows.length !== receiptList.children.length || rows.some((row, i) => row !== receiptList.children[i])) receiptList.replaceChildren(...rows);
  }
  function render() {
    if (stopped) return;
    const fresh = snapshot && !error && now() - lastOk < 15000;
    connection.textContent = !auth ? 'Reconnect Telegram' : win.navigator?.onLine === false ? 'Offline'
      : fresh ? 'Live · 5s' : snapshot ? 'Last known' : 'Connecting…';
    connection.dataset.state = fresh ? 'live' : 'offline';
    notice.textContent = !auth ? 'Open Vega from Telegram using your paired account.'
      : error || (snapshot?.unavailable ? 'Some hosts are unavailable.'
        : snapshot?.limited ? 'Showing the 50 most recent chats and pinned chats.' : '');
    notice.hidden = !notice.textContent;
    for (const [key, tab] of tabs) tab.setAttribute('aria-current', key === view ? 'page' : 'false');
    listing.hidden = !!selected || view === 'reports';
    detail.hidden = !selected || view === 'reports';
    reportsRoot.hidden = view !== 'reports';
    if (view === 'reports') return;
    renderList();
    if (selected) {
      const chat = current();
      chatTitle.textContent = chat?.title ?? drafts.get(selected)?.title ?? 'Chat unavailable';
      const markKey = JSON.stringify([chat?.id, chat?.project_id, chat?.host_id, chat?.kind]);
      if (chatMark.dataset.identity !== markKey) {
        chatMark.dataset.identity = markKey;
        chatMark.replaceChildren(projectMark(doc, snapshot?.projects.find(p => p.id === chat?.project_id)
          ?? (chat?.project_id ? {} : null), chat));
      }
      status(chatState, chat); renderMessages(); renderComposer();
    }
  }
  function changeView(next) {
    view = next; selected = null; project = null; generation++; search.value = '';
    if (next === 'reports' && !reportsApp) reportsApp = mountReports?.(reportsRoot);
    render(); if (next !== 'reports') void refresh();
  }
  async function openChat(id) {
    const chat = snapshot?.chats.find(c => c.id === id && c.can_send);
    if (!chat) return;
    selected = id; generation++;
    const d = draft(chat); d.title = chat.title; prompt.value = d.text;
    chatLog.dataset.signature = ''; chatLog.scrollTop = 0;
    render(); await refresh();
  }
  function rememberReceipt(r, d) {
    if (!sameReceipt(r, d.attempt)) throw new Error('The returned receipt does not match this prompt.');
    const rows = receiptRows.get(r.thread_id) ?? [];
    receiptRows.set(r.thread_id, [r, ...rows.filter(x => x.request_key !== r.request_key)].slice(0, 30));
    if (r.state === 'accepted') {
      if (d.text === d.attempt.text) d.text = '';
      d.files.committed(d.attempt.attachment_ids ?? []);
      d.attempt = null; d.notice = 'Sent to Codex.';
    } else d.attempt.state = r.state;
  }
  async function refresh() {
    if (!available() || loading) return;
    loading = true;
    const selection = selected, token = generation;
    try {
      const next = await api('/mini/api/workspace');
      if (!Array.isArray(next.projects) || !Array.isArray(next.chats)) throw new Error('The workspace could not be read.');
      if (stopped || !auth) return;
      snapshot = next; lastOk = now(); error = '';
      if (selection && token === generation && current()) {
        const value = await api(`/mini/api/chats/${encodeURIComponent(selection)}`);
        if (stopped || token !== generation || selected !== selection) return;
        if (value?.chat?.id !== selection || !Array.isArray(value.messages)) throw new Error('The returned chat did not match.');
        messages.set(selection, value);
        receiptRows.set(selection, value.receipts ?? []);
      }
      if ([...drafts.values()].some(d => d.attempt)) {
        const saved = await api('/mini/api/chat-prompts');
        for (const d of drafts.values()) {
          const r = saved.receipts?.find(row => sameReceipt(row, d.attempt));
          if (r) rememberReceipt(r, d);
        }
      }
    } catch (e) {
      error = e.status === 401 || e.status === 403 ? 'Reopen the Mini App to reconnect.' : 'Connection unavailable. Your draft is kept.';
      if (e.status === 401 || e.status === 403) auth = false;
    } finally { loading = false; render(); }
  }
  async function submit(event) {
    event.preventDefault();
    const chat = current(); if (!chat || send.disabled) return;
    const d = draft(chat);
    const attempt = { request_key: crypto.randomUUID(), thread_id: chat.id,
      target_version: d.version, text: d.text, state: 'sending' };
    if (d.files.ids.length) attempt.attachment_ids = [...d.files.ids];
    d.attempt = attempt; renderComposer();
    try {
      const { state, ...payload } = attempt;
      const value = await api('/mini/api/chat-prompts', { method: 'POST', body: payload });
      if (d.attempt === attempt) rememberReceipt(value.receipt, d);
    } catch (e) {
      if (d.attempt !== attempt) return;
      attempt.state = 'uncertain';
      if (e.status === 409 || e.status === 400 || e.status === 401 || e.status === 403) {
        d.attempt = null; d.notice = e.message;
      }
      if (e.status === 401 || e.status === 403) auth = false;
    } finally { render(); void refresh(); }
  }
  on(search, 'input', renderList);
  on(prompt, 'input', () => { const chat = current(); if (chat && !draft(chat).attempt) { draft(chat).text = prompt.value; renderComposer(); } });
  on(form, 'submit', submit);
  on(picker, 'change', () => {
    const chat = pickerChat, files = Array.from(picker.files ?? []);
    pickerChat = null;
    picker.value = '';
    if (chat && files.length && available() && !draft(chat).attempt) return fileAction(draft(chat), () => draft(chat).files.add(files));
  });
  on(form, 'paste', event => {
    const files = Array.from(event.clipboardData?.files ?? []), chat = current();
    if (!files.length) return;
    event.preventDefault();
    if (chat && !attach.disabled) return fileAction(draft(chat), () => draft(chat).files.add(files));
  });
  on(doc, 'visibilitychange', () => { render(); if (available()) void refresh(); });
  on(win, 'online', () => refresh()); on(win, 'offline', () => { error = 'Offline. Your draft is kept.'; render(); });
  if (tg?.onEvent) {
    const activate = () => { active = true; void refresh(); }, deactivate = () => { active = false; };
    tg.onEvent('activated', activate); tg.onEvent('deactivated', deactivate);
    cleanups.push(() => { tg.offEvent?.('activated', activate); tg.offEvent?.('deactivated', deactivate); });
  }
  tg?.ready?.(); tg?.expand?.();
  render();
  const ready = refresh();
  if (auth && pollIntervalMs > 0) timer = win.setInterval(() => { render(); void refresh(); }, pollIntervalMs);
  return { ready, refresh, openChat, changeView, destroy() {
    stopped = true; generation++; win.clearInterval(timer); reportsApp?.destroy(); cleanups.forEach(fn => fn());
    for (const url of previews.values()) win.URL?.revokeObjectURL(url);
  } };
}
