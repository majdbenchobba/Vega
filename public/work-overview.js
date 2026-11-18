// Read-only navigation. Graph nodes never apply changes or dispatch instructions.
import { renderReplyBody } from './reply-renderer.js';
import { projectMark } from './miniapp/visuals.js';
import { MapMotion } from './map-motion.js';

const NS = 'http://www.w3.org/2000/svg';
const LABELS = {
  idle: 'Idle', working: 'In progress', paused: 'Paused', blocked: 'Blocked',
  queued: 'Queued', planning: 'Planning', reading: 'Reading sources', delegating: 'Specialists working',
  preparing: 'Preparing a draft', reviewing: 'Vega reviewing', verifying: 'Checking',
  awaiting_approval: 'Your review', applying: 'Applying approved change', completed: 'Result ready',
  failed: 'Needs attention', interrupted: 'Interrupted', cancelled: 'Cancelled', expired: 'Expired',
  needs_revision: 'Revisions needed', review_blocked: 'Review blocked', handled: 'Routed',
  routing: 'Routing', stale_target: 'Destination changed', awaiting_input: 'Needs details',
  draft: 'Workflow draft', published_inactive: 'Saved inactive', uncertain: 'Receipt uncertain',
};
const COLUMNS = [
  ['queued', 'Queued'], ['working', 'In progress'], ['vega', 'Vega review'],
  ['owner', 'Your review'], ['results', 'Results & receipts'], ['attention', 'Needs attention'],
];
const short = (value, limit = 100) => {
  const text = String(value || '').replace(/\s+/g, ' ').trim();
  return text.length > limit ? text.slice(0, limit - 1) + '…' : text;
};
const stateLabel = state => LABELS[state] || short(String(state || 'Not observed').replaceAll('_', ' '));
const chatSource = project => project.codex_activity?.available ? project.codex_activity : project.chat_activity || project.codex_activity;
const activitySignature = value => value && { ...value, checked_at: null };
const recordedAt = value => value ? new Date(typeof value === 'number' ? value * 1000 : value).toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' }) : 'Not recorded';
export function catalogGroups(projects) {
  const groups = [
    { key: 'connected', label: 'Connected chats', projects: [] },
    { key: 'unchecked', label: 'Connection needs checking', projects: [] },
    { key: 'unconnected', label: 'Unconnected chats', projects: [] },
  ];
  for (const project of projects) {
    const source = project.connection?.source?.state;
    const chat = chatSource(project);
    const key = source === 'missing' ? 'unconnected'
      : chat?.available || source === 'found' ? 'connected' : 'unchecked';
    groups.find(group => group.key === key).projects.push(project);
  }
  return groups.filter(group => group.projects.length);
}
export function projectState(project) {
  if (project.connection?.state === 'missing') return 'Chat not connected';
  if (project.connection?.state === 'history_missing') return 'Chat history unavailable';
  const chat = chatSource(project);
  if (chat?.observed_at) return chat.label || (chat.available ? stateLabel(chat.state) : 'Chat update unavailable');
  if (!project.observed_at) return 'Not checked yet';
  if (project.freshness?.state === 'stale') return 'Older update';
  return Object.hasOwn(LABELS, project.status) ? LABELS[project.status] : 'Update saved';
}
const stamp = value => value ? new Date(typeof value === 'number' ? value * 1000 : value).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : 'Not observed';
export function workColumn(item) {
  if (item.kind === 'chat') return item.chat.available && item.state === 'working' ? 'working'
    : ['error', 'interrupted'].includes(item.state) ? 'attention' : 'results';
  if (item.state === 'queued') return 'queued';
  if (['planning', 'reading', 'delegating', 'preparing', 'applying', 'routing', 'publishing'].includes(item.state)) return 'working';
  if (['reviewing', 'verifying'].includes(item.state)) return 'vega';
  if (['awaiting_approval', 'draft'].includes(item.state)) return 'owner';
  if (['completed', 'handled', 'published_inactive', 'cancelled'].includes(item.state)) return 'results';
  return 'attention';
}
export function boardItems(data) {
  return [
    ...(data.projects || []).filter(project => (!data.selected_project || project.alias === data.selected_project)
      && project.connection?.source?.state !== 'missing' && chatSource(project)?.observed_at).map(project => {
      const chat = chatSource(project);
      return { id: `chat:${project.alias}`, project: project.alias, label: project.label, title: project.label,
        kind: 'chat', state: chat.state, status: chat.label, chat, updated_at: chat.observed_at };
    }),
    ...(data.runs || []).map(run => ({ ...run, kind: 'run', title: short(run.goal), status: run.activity?.label || stateLabel(run.state) })),
    ...(data.requests || []).map(request => ({ ...request, kind: 'request', title: short(request.instruction) || 'Project instruction',
      status: request.state === 'handled' ? 'Routed · receipt saved' : stateLabel(request.state) })),
    ...(data.automation?.drafts || []).map(draft => ({ ...draft, kind: 'automation', title: 'n8n project workflow', status: stateLabel(draft.state) })),
  ];
}
export function stepProgress(run) {
  const progress = run.progress;
  if (!progress || !Number.isInteger(progress.total) || progress.total !== 4
      || !Number.isInteger(progress.completed) || progress.completed < 0 || progress.completed > 4) return null;
  return { percent: progress.completed * 25, text: `${progress.completed}/4 review steps` };
}

// A vertical trunk with paired project branches. Expanding a branch reserves
// its actual height, so later projects never overlap the revealed work.
export function verticalTreeLayout(branches) {
  const nodes = [], edges = [], trunk = 490;
  const addBranch = (branch, x, y, parent = null, level = 0) => {
    const box = { ...branch, x, y, width: 264, height: 112, parent, level };
    nodes.push(box);
    let bottom = y + box.height;
    for (const child of branch.children || []) {
      const childY = bottom + 44, childX = x + 28;
      edges.push({ from: branch.id, to: child.id, kind: 'child', bus: x - 16 });
      bottom = addBranch(child, childX, childY, branch.id, level + 1);
    }
    return bottom;
  };
  let y = 208;
  for (let index = 0; index < branches.length; index += 2) {
    let bottom = y;
    for (const [offset, x] of [[0, 100], [1, 640]]) {
      const branch = branches[index + offset];
      if (!branch) continue;
      edges.push({ from: 'vega', to: branch.id, kind: 'trunk', trunk });
      bottom = Math.max(bottom, addBranch(branch, x, y, 'vega'));
    }
    y = bottom + 76;
  }
  return { nodes, edges, trunk, width: Math.max(980, ...nodes.map(n => n.x + n.width + 80)), height: Math.max(420, y) };
}

export class WorkOverview {
  constructor(root, { get, post, onRun = () => {}, onRequests = () => {}, onAutomations = () => {}, pollMs = 5000 } = {}) {
    this.root = root; this.doc = root.ownerDocument; this.win = this.doc.defaultView;
    this.get = get; this.post = post; this.onRun = onRun; this.onRequests = onRequests; this.onAutomations = onAutomations;
    this.visible = false; this.project = ''; this.mode = 'map'; this.data = null; this.signature = '';
    this.sequence = 0; this.disposed = false; this.loading = false;
    this.expandedProjects = new Set(); this.expandedRuns = new Set(); this.projectSnapshots = new Map();
    this.inspected = null; this.inspectorSignature = ''; this.selectedNodeId = 'vega';
    this.pan = { x: 20, y: 25, scale: 1 }; this.needsFit = true;
    this.motion = new MapMotion(this.win); this.expanded = false;
    try {
      const saved = this.win.localStorage.getItem('vega.work.view.v3');
      if (['list', 'map', 'board'].includes(saved)) this.mode = saved;
    } catch {}
    root.replaceChildren(); this.build();
    this.timer = pollMs > 0 ? this.win.setInterval(() => {
      if (this.visible && !this.doc.hidden && !this.loading) void this.refresh();
    }, pollMs) : null;
    this.resize = () => this.resizeViewport();
    this.win.addEventListener('resize', this.resize);
  }
  el(tag, text = '', cls = '') {
    const node = this.doc.createElement(tag); node.textContent = text;
    if (cls) node.className = cls;
    return node;
  }
  reply(text, cls = 'wo-chat-text') {
    const node = this.el('div', '', cls);
    renderReplyBody(node, text);
    return node;
  }
  button(text, action, cls = '') {
    const item = this.el('button', text, cls); item.type = 'button';
    item.addEventListener('click', () => void Promise.resolve(action()).catch(error => { this.error.textContent = error.message; }));
    return item;
  }
  icon(path) {
    const svg = this.doc.createElementNS(NS, 'svg');
    for (const [key, value] of Object.entries({ viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor',
      'stroke-width': '1.7', 'stroke-linecap': 'round', 'stroke-linejoin': 'round', 'aria-hidden': 'true' })) svg.setAttribute(key, value);
    const shape = this.doc.createElementNS(NS, 'path'); shape.setAttribute('d', path); svg.append(shape); return svg;
  }
  build() {
    this.root.classList.add('work-overview');
    const heading = this.el('header', '', 'wo-heading'), title = this.el('div');
    title.append(this.el('h2', 'Projects'));
    this.updated = this.el('span', 'Loading…', 'wo-updated'); this.updated.setAttribute('role', 'status');
    heading.append(title, this.updated);
    const toolbar = this.el('div', '', 'wo-toolbar');
    this.switcher = this.el('div', '', 'wo-switch'); this.switcher.setAttribute('role', 'group'); this.switcher.setAttribute('aria-label', 'Work view');
    this.listButton = this.button('Overview', () => this.setMode('list'));
    this.mapButton = this.button('Map', () => this.setMode('map'));
    this.mapButton.prepend(this.icon('M3 9h5v6H3ZM16 3h5v6h-5ZM16 15h5v6h-5ZM8 12h4M12 6h4M12 18h4M12 6v12'));
    this.boardButton = this.button('Board', () => this.setMode('board'));
    this.boardButton.prepend(this.icon('M3 4h7v16H3ZM14 4h7v10h-7'));
    this.switcher.append(this.mapButton, this.listButton, this.boardButton);
    const filter = this.el('label', '', 'wo-filter'); filter.append(this.el('span', 'Project'));
    this.projectSelect = this.el('select'); this.projectSelect.setAttribute('aria-label', 'Show project');
    this.projectSelect.addEventListener('change', () => this.selectProject(this.projectSelect.value));
    filter.append(this.projectSelect);
    const automation = this.button('Automations', () => this.openAutomations(), 'wo-automation-link');
    automation.prepend(this.icon('M9 7H6a5 5 0 1 0 0 10h3l6-10h3a5 5 0 1 1 0 10h-3L9 7Z'));
    this.refreshButton = this.button('↻', () => this.refresh(), 'wo-refresh');
    this.refreshButton.title = 'Refresh workspace'; this.refreshButton.setAttribute('aria-label', 'Refresh workspace');
    toolbar.append(this.switcher, filter, automation, this.refreshButton);
    this.pause = this.el('p', 'Automatic work is paused.', 'wo-pause'); this.pause.hidden = true;
    this.error = this.el('p', '', 'wo-error'); this.error.setAttribute('role', 'alert');
    this.stats = this.el('div', '', 'wo-stats');
    this.summary = this.el('section', '', 'wo-project-summary'); this.summary.hidden = true;
    this.content = this.el('div', '', 'wo-content');
    this.list = this.el('section', '', 'wo-list'); this.list.setAttribute('aria-label', 'Project overview');
    this.map = this.el('div', '', 'wo-map'); this.map.setAttribute('aria-label', 'Project and agent map');
    this.map.tabIndex = 0;
    this.canvas = this.el('div', '', 'wo-canvas');
    this.edges = this.doc.createElementNS(NS, 'svg'); this.edges.classList.add('wo-edges'); this.edges.setAttribute('aria-hidden', 'true');
    this.nodes = this.el('div', '', 'wo-nodes'); this.canvas.append(this.edges, this.nodes);
    const zoom = this.el('div', '', 'wo-zoom'); zoom.setAttribute('aria-label', 'Map zoom');
    const zoomOut = this.button('−', () => this.zoomBy(1 / 1.2, undefined, undefined, true)); zoomOut.setAttribute('aria-label', 'Zoom out');
    const zoomIn = this.button('+', () => this.zoomBy(1.2, undefined, undefined, true)); zoomIn.setAttribute('aria-label', 'Zoom in');
    this.zoomLabel = this.button('100%', () => this.zoomBy(1 / this.pan.scale, undefined, undefined, true), 'wo-zoom-value');
    this.zoomLabel.setAttribute('aria-label', 'Reset zoom to 100 percent');
    this.expandButton = this.button('⛶ Expand', () => this.setExpanded(!this.expanded));
    this.expandButton.setAttribute('aria-label', 'Expand map'); this.expandButton.setAttribute('aria-expanded', 'false');
    zoom.append(zoomOut, this.zoomLabel, zoomIn, this.button('Fit all', () => this.fit()),
      this.button('Fit width', () => this.fitWidth()), this.button('Top', () => this.home()), this.expandButton);
    this.inspector = this.el('aside', '', 'wo-inspector'); this.inspector.hidden = true;
    this.inspector.setAttribute('aria-label', 'Work details'); this.inspector.setAttribute('role', 'region');
    this.map.append(this.canvas, zoom, this.el('span', 'Drag or scroll to move · Ctrl + scroll to zoom', 'wo-map-hint'), this.inspector);
    this.board = this.el('div', '', 'wo-board'); this.board.setAttribute('aria-label', 'Work by status');
    this.content.append(this.list, this.map, this.board);
    this.footer = this.el('p', '', 'wo-footer');
    this.root.append(heading, toolbar, this.pause, this.error, this.stats, this.summary, this.content, this.footer);
    const pointers = new Map();
    let dragged = false, suppressClick = false;
    let gesture = null;
    const beginGesture = () => {
      const points = [...pointers.values()];
      if (points.length === 2) {
        const [a, b] = points;
        gesture = { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2, distance: Math.hypot(a.x - b.x, a.y - b.y) || 1, pan: { ...this.pan } };
      } else if (points.length === 1) gesture = { ...points[0], pan: { ...this.pan } };
      else gesture = null;
    };
    this.map.addEventListener('pointerdown', event => {
      if (event.button !== 0 || event.target.closest('input, select, .wo-inspector, .wo-zoom')) return;
      this.motion.cancel(); dragged = false; suppressClick = false;
      const rect = this.map.getBoundingClientRect();
      pointers.set(event.pointerId, { x: event.clientX - rect.left, y: event.clientY - rect.top });
      beginGesture(); this.needsFit = false;
      event.target.setPointerCapture(event.pointerId);
    });
    this.map.addEventListener('pointermove', event => {
      if (!pointers.has(event.pointerId) || !gesture) return;
      const rect = this.map.getBoundingClientRect();
      pointers.set(event.pointerId, { x: event.clientX - rect.left, y: event.clientY - rect.top });
      const points = [...pointers.values()];
      if (points.length === 1 && !dragged && Math.hypot(points[0].x - gesture.x, points[0].y - gesture.y) < 5) return;
      dragged = true; suppressClick = true; this.map.classList.add('wo-dragging');
      if (points.length === 2 && gesture.distance) {
        const [a, b] = points, scale = Math.min(2.4, Math.max(.08, gesture.pan.scale * Math.hypot(a.x - b.x, a.y - b.y) / gesture.distance));
        this.pan = { scale, x: (a.x + b.x) / 2 - (gesture.x - gesture.pan.x) * scale / gesture.pan.scale,
          y: (a.y + b.y) / 2 - (gesture.y - gesture.pan.y) * scale / gesture.pan.scale };
      } else if (points.length === 1) {
        this.pan.x = gesture.pan.x + points[0].x - gesture.x; this.pan.y = gesture.pan.y + points[0].y - gesture.y;
      }
      this.transform();
    });
    const end = event => { pointers.delete(event.pointerId); beginGesture(); if (!pointers.size) this.map.classList.remove('wo-dragging'); };
    this.map.addEventListener('pointerup', end); this.map.addEventListener('pointercancel', end); this.map.addEventListener('lostpointercapture', end);
    this.map.addEventListener('click', event => {
      if (suppressClick && event.detail !== 0) { event.preventDefault(); event.stopImmediatePropagation(); }
      suppressClick = false;
    }, true);
    this.map.addEventListener('wheel', event => {
      if (event.target.closest('.wo-inspector')) return;
      event.preventDefault(); this.needsFit = false; this.motion.cancel();
      const unit = event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? this.map.clientHeight : 1;
      if (event.ctrlKey || event.metaKey) {
        const rect = this.map.getBoundingClientRect();
        this.zoomBy(Math.exp(-event.deltaY * unit * .002), event.clientX - rect.left, event.clientY - rect.top);
      } else {
        this.pan.x -= (event.shiftKey ? event.deltaY : event.deltaX) * unit;
        if (!event.shiftKey) this.pan.y -= event.deltaY * unit;
        this.transform();
      }
    }, { passive: false });
    this.map.addEventListener('keydown', event => {
      if (event.key === 'Escape') {
        if (!this.inspector.hidden) this.closeInspector();
        else this.setExpanded(false);
        return;
      }
      if (event.target.closest('.wo-inspector')) return;
      this.motion.cancel();
      const nodeId = event.target.dataset.nodeId;
      if (nodeId && ['ArrowDown', 'ArrowUp', 'ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) {
        event.preventDefault(); this.navigateTree(nodeId, event.key); return;
      }
      if (event.target !== this.map && !nodeId) return;
      const delta = { ArrowLeft: [40, 0], ArrowRight: [-40, 0], ArrowUp: [0, 40], ArrowDown: [0, -40] }[event.key];
      if (delta) { event.preventDefault(); this.pan.x += delta[0]; this.pan.y += delta[1]; this.transform(); }
      else if (event.key === '+' || event.key === '=') { event.preventDefault(); this.zoomBy(1.25); }
      else if (event.key === '-') { event.preventDefault(); this.zoomBy(.8); }
      else if (event.key.toLowerCase() === 'f') { event.preventDefault(); this.fit(); }
      else if (event.key === '0') { event.preventDefault(); this.home(); }
    });
    this.setMode(this.mode);
  }
  async show() { this.visible = true; await this.refresh(); }
  hide() { this.motion?.cancel(); this.setExpanded(false); this.visible = false; if (this.automationDialog?.open) this.automationDialog.close(); }
  setMode(mode) {
    this.motion?.cancel();
    if (mode !== 'map') this.setExpanded(false);
    this.mode = ['map', 'board'].includes(mode) ? mode : 'list';
    this.root.dataset.mode = this.mode;
    const workspace = this.root.closest('.workspace');
    if (workspace) workspace.dataset.workMode = this.mode;
    this.list.hidden = this.mode !== 'list';
    this.map.hidden = this.mode !== 'map'; this.board.hidden = this.mode !== 'board';
    this.listButton.setAttribute('aria-pressed', String(this.mode === 'list'));
    this.mapButton.setAttribute('aria-pressed', String(this.mode === 'map'));
    this.boardButton.setAttribute('aria-pressed', String(this.mode === 'board'));
    this.summary.hidden = true;
    this.stats.hidden = this.mode !== 'board';
    this.footer.hidden = this.mode !== 'board';
    try { this.win.localStorage.setItem('vega.work.view.v3', this.mode); } catch {}
    if (this.mode === 'map' && this.data) this.win.requestAnimationFrame(() => { if (this.needsFit) this.home(); });
  }
  async selectProject(project, { expand = true } = {}) {
    this.project = project; this.signature = '';
    if (project) {
      if (expand) this.expandedProjects.add(project);
      else this.expandedProjects.delete(project);
    }
    this.inspected = project && expand ? { kind: 'project', id: project } : null;
    this.inspectorSignature = '';
    this.selectedNodeId = project ? `project:${project}` : 'vega';
    this.sequence++; this.loading = false;
    await this.refresh();
  }
  async refresh() {
    if (this.disposed || this.loading) return;
    const sequence = ++this.sequence; this.loading = true; this.refreshButton.disabled = true;
    try {
      const project = this.project;
      const projects = [...new Set([project, ...this.expandedProjects])], snapshots = new Map();
      let cursor = 0;
      // Keep every open branch current without sending an unbounded burst.
      await Promise.all(Array.from({ length: Math.min(3, projects.length) }, async () => {
        while (cursor < projects.length && sequence === this.sequence && !this.disposed) {
          const alias = projects[cursor++];
          const snapshot = await this.get('/api/work-overview' + (alias ? '/projects/' + encodeURIComponent(alias) : ''));
          if (snapshot.version !== 1 || !Array.isArray(snapshot.projects) || !Array.isArray(snapshot.runs)) {
            throw new Error('The workspace returned an incomplete snapshot.');
          }
          snapshots.set(alias, snapshot);
        }
      }));
      if (this.disposed || sequence !== this.sequence) return;
      const data = snapshots.get(project);
      this.projectSnapshots = snapshots;
      this.data = data; this.error.textContent = '';
      this.updated.textContent = `Snapshot ${stamp(data.snapshot_at)}`;
      const signature = JSON.stringify([data.projects.map(({ alias, label, status, observed_at, checklist, freshness, connection, codex_activity, chat_activity }) =>
        ({ alias, label, status, observed_at, checklist, freshness, connection,
          codex_activity: activitySignature(codex_activity), chat_activity: activitySignature(chat_activity) })),
        projects.map(alias => { const snapshot = snapshots.get(alias); return [alias, snapshot.runs, snapshot.requests, snapshot.automation?.drafts]; }),
        data.work_paused, data.automation?.runtime?.state, data.automation?.connected,
        [...this.expandedProjects], [...this.expandedRuns]]);
      if (signature !== this.signature) { this.signature = signature; this.render(); }
    } catch (error) {
      if (sequence !== this.sequence || this.disposed) return;
      this.error.textContent = 'Workspace refresh failed. ' + error.message;
      this.updated.textContent = this.data ? `Last snapshot ${stamp(this.data.snapshot_at)}` : 'Unavailable';
      if ([401, 403].includes(error.status)) {
        this.data = null; this.projectSnapshots.clear(); this.expandedProjects.clear(); this.expandedRuns.clear();
        this.geometry?.clear(); this.treeNodes = [];
        this.nodes.replaceChildren(); this.edges.replaceChildren(); this.board.replaceChildren();
        this.list.replaceChildren(); this.summary.replaceChildren(); this.closeInspector(false);
      }
    } finally {
      if (sequence === this.sequence) { this.loading = false; this.refreshButton.disabled = false; }
    }
  }
  render() {
    const data = this.data, items = boardItems(data);
    this.pause.hidden = !data.work_paused;
    const groups = catalogGroups(data.projects);
    const projectsSignature = JSON.stringify(groups.map(g => [g.key, g.projects.map(p => [p.alias, p.label])]));
    if (projectsSignature !== this.projectsSignature) {
      const all = this.el('option', `All projects · ${data.projects.length}`); all.value = '';
      this.projectSelect.replaceChildren(all);
      for (const group of groups) {
        const options = this.el('optgroup'); options.label = `${group.label} · ${group.projects.length}`;
        for (const project of group.projects) {
          const option = this.el('option', project.label); option.value = project.alias; options.append(option);
        }
        this.projectSelect.append(options);
      }
      this.projectsSignature = projectsSignature;
    }
    this.projectSelect.value = this.project;
    this.stats.replaceChildren();
    for (const [value, label] of [
      [data.projects.length, 'Projects'],
      [items.filter(item => ['working', 'vega'].includes(workColumn(item))).length, 'Working'],
      [items.filter(item => workColumn(item) === 'owner').length, 'Your review'],
      [`${data.agents?.length || 0} / ${data.skills?.length || 0}`, 'Agents / skills'],
    ]) {
      const stat = this.el('div', '', 'wo-stat'); stat.append(this.el('strong', String(value)), this.el('span', label)); this.stats.append(stat);
    }
    this.summary.hidden = true;
    this.renderList(items); this.renderInspector(); this.renderMap(); this.renderBoard(items);
    this.footer.textContent = 'Chat activity updates automatically · saved records keep their dates.';
  }
  renderList(items) {
    const unconnectedOpen = this.list.querySelector('.wo-unconnected')?.open || false;
    const scrollTop = this.content?.scrollTop;
    this.list.replaceChildren();
    const projects = this.data.projects.filter(project => !this.project || project.alias === this.project);
    if (this.project) this.list.append(this.button('← All projects', () => this.selectProject(''), 'wo-back'));
    const groups = catalogGroups(projects), containers = new Map();
    if (!this.project) {
      const counts = groups.map(group => `${group.projects.length} ${group.key === 'unchecked' ? 'need checking' : group.key}`);
      this.list.append(this.el('p', counts.join(' · '), 'wo-catalog-summary'));
      for (const group of groups) {
        const section = this.el(group.key === 'unconnected' ? 'details' : 'section', '', `wo-${group.key}`);
        if (group.key === 'unconnected') {
          section.open = unconnectedOpen;
          section.append(this.el('summary', `${group.label} · ${group.projects.length}`));
          section.append(this.el('p', 'Saved notes · chat connection needed.', 'wo-catalog-summary'));
        } else section.append(this.el('h3', group.label, 'wo-catalog-heading'));
        this.list.append(section);
        for (const project of group.projects) containers.set(project.alias, section);
      }
    }
    for (const project of projects) {
      const row = this.el('article', '', 'wo-project-row');
      const open = this.button(project.label, () => this.selectProject(project.alias), 'wo-project-name');
      const status = this.el('span', projectState(project), 'wo-project-state');
      const checklist = project.checklist || {};
      const next = [...(checklist.blockers || []), ...(checklist.working || []), ...(checklist.next || [])][0];
      row.append(open, status);
      const chat = chatSource(project);
      if (chat?.observed_at) row.append(this.el('p', `Chat activity ${recordedAt(chat.observed_at)}`, 'wo-project-next'));
      if (this.project) {
        if (chat?.latest_update?.text) row.append(this.reply(chat.latest_update.text, 'wo-project-detail'));
        if (project.freshness?.evidence_at) {
          row.append(this.el('p', `Project notes ${recordedAt(project.freshness.evidence_at)}`, 'wo-project-next'));
        }
        if (next) row.append(this.el('p', typeof next === 'string' ? next : next.text || next.label || '', 'wo-project-next'));
      }
      (containers.get(project.alias) || this.list).append(row);
    }
    if (this.project) {
      const related = items.filter(item => item.project === this.project && item.kind !== 'chat');
      this.list.append(this.el('h3', 'Recent work'));
      if (!related.length) this.list.append(this.el('p', 'No recent work for this project.', 'wo-empty'));
      for (const item of related) {
        const action = item.kind === 'run' ? () => this.onRun(item.id) : item.kind === 'automation' ? () => this.openAutomations() : this.onRequests;
        const row = this.button('', action, 'wo-work-row');
        row.append(this.el('strong', item.title), this.el('span', item.status));
        this.list.append(row);
      }
    }
    if (this.content && scrollTop !== undefined) this.content.scrollTop = scrollTop;
  }
  treeBranches() {
    return this.data.projects.map(project => {
      const branch = { id: `project:${project.alias}`, kind: 'project', title: project.label,
        detail: projectState(project), state: project.freshness?.state === 'stale' ? 'stale' : project.status,
        record: project, children: [], expandable: true, expanded: this.expandedProjects.has(project.alias) };
      if (!branch.expanded) return branch;
      const snapshot = this.projectSnapshots.get(project.alias) || this.data;
      const chat = chatSource(project);
      if (chat?.observed_at) branch.children.push({ id: `chat:${project.alias}`, kind: 'chat',
        title: 'Chat activity', detail: `${chat.label} · ${stamp(chat.observed_at)}`,
        state: chat.state, record: project });
      const runs = snapshot.runs.filter(run => run.project === project.alias);
      for (const run of runs) {
        const work = { id: `run:${run.id}`, kind: 'run', title: run.goal,
          detail: run.activity?.label || stateLabel(run.state), state: run.state, record: run,
          expandable: !!run.agents?.length, expanded: this.expandedRuns.has(run.id), children: [] };
        if (work.expanded) {
          work.children = (run.agents || []).map(agent => ({ id: `agent:${run.id}:${agent.id}`,
            kind: 'agent', title: agent.label || agent.role?.replaceAll('_', ' '), detail: stateLabel(agent.state),
            state: agent.state, record: agent, run }));
          if (work.children.length) work.children.push({ id: `review:${run.id}`, kind: 'review', title: 'Vega review',
            detail: run.review?.model_reviewed ? stateLabel(run.review.status === 'accepted' ? 'completed' : run.review.status)
              : run.state === 'reviewing' ? 'Review in progress' : 'Not yet reviewed',
            state: run.review?.status, record: run });
          if (run.state === 'awaiting_approval' || run.activity?.implementation_verified) work.children.push({
            id: `owner:${run.id}`, kind: 'owner', title: run.activity?.implementation_verified ? 'Applied & verified' : 'Your review',
            detail: run.activity?.implementation_verified ? 'Matching file hash recorded' : 'Exact change awaiting approval',
            state: run.state, record: run });
        }
        branch.children.push(work);
      }
      if (!runs.length && !chat?.observed_at) branch.children.push({ id: `empty:${project.alias}`, kind: 'empty', title: 'No agent runs yet',
        detail: 'Saved project notes are in the detail popup.', record: project });
      const requests = (snapshot.requests || []).filter(item => item.project === project.alias);
      if (requests.length) branch.children.push({ id: `requests:${project.alias}`, kind: 'request', title: 'Routing receipts',
        detail: `${requests.length} saved target receipts`, record: requests });
      const drafts = (snapshot.automation?.drafts || []).filter(item => item.project === project.alias);
      if (drafts.length) branch.children.push({ id: `automation:${project.alias}`, kind: 'automation', title: 'Automations',
        detail: `${drafts.length} saved workflow${drafts.length === 1 ? '' : 's'}`, record: drafts });
      return branch;
    });
  }
  renderMap() {
    this.motion?.cancel();
    const focused = this.doc.activeElement?.dataset.nodeId;
    const previous = this.geometry;
    const layout = verticalTreeLayout(this.treeBranches());
    const root = { id: 'vega', x: layout.trunk - 125, y: 32, width: 250, height: 112, kind: 'vega', title: 'Vega',
      detail: this.data.work_paused ? 'Automatic work paused' : 'Projects, agents & reviews', record: this.data };
    this.treeNodes = [root, ...layout.nodes]; this.geometry = new Map(this.treeNodes.map(item => [item.id, item]));
    this.nodes.replaceChildren(); this.edges.replaceChildren();
    if (!this.geometry.has(this.selectedNodeId)) this.selectedNodeId = 'vega';
    const stroke = (path, selected = false, from = '', to = '') => {
      const line = this.doc.createElementNS(NS, 'path'); line.setAttribute('d', path);
      line.dataset.from = from; line.dataset.to = to;
      if (selected) line.setAttribute('class', 'wo-edge-selected');
      this.edges.append(line);
    };
    const projects = layout.nodes.filter(item => item.kind === 'project');
    if (projects.length) stroke(`M${layout.trunk},144 V${Math.max(...projects.map(item => item.y + item.height / 2))}`);
    for (const edge of layout.edges) {
      const a = this.geometry.get(edge.from), b = this.geometry.get(edge.to);
      if (edge.kind === 'trunk') {
        const y = b.y + b.height / 2, x = b.x < layout.trunk ? b.x + b.width : b.x;
        stroke(`M${layout.trunk},${y - 44} C${layout.trunk},${y} ${layout.trunk},${y} ${x},${y}`, b.expanded, a.id, b.id);
      } else {
        const x1 = a.x + a.width / 2, y1 = a.y + a.height, y2 = b.y + b.height / 2;
        stroke(`M${x1},${y1} V${y1 + 12} H${edge.bus} V${y2 - 14} Q${edge.bus},${y2} ${edge.bus + 14},${y2} H${b.x}`, false, a.id, b.id);
      }
    }
    for (const item of this.treeNodes) {
      const button = this.button('', () => this.activateNode(item.id), 'wo-node');
      button.dataset.nodeId = item.id; button.dataset.kind = item.kind; button.dataset.state = item.state || 'idle';
      button.dataset.selected = String(item.id === this.selectedNodeId);
      button.tabIndex = item.id === this.selectedNodeId ? 0 : -1;
      button.style.left = item.x + 'px'; button.style.top = item.y + 'px'; button.style.width = item.width + 'px'; button.style.height = item.height + 'px';
      const symbols = { vega: '◉', project: '◇', chat: '◉', run: '◇', agent: '◉', review: '✓', automation: '∞', request: '↗', owner: '◎', empty: '·' };
      const top = this.el('div', '', 'wo-node-top');
      top.append(['project', 'vega'].includes(item.kind) ? projectMark(this.doc, item.kind === 'vega' ? { alias: 'vega' } : item.record)
        : this.el('span', symbols[item.kind], 'wo-node-symbol'),
      this.el('span', item.kind === 'vega' ? 'Work tree' : item.kind === 'empty' ? 'Work' : item.kind));
      if (item.expandable) {
        button.setAttribute('aria-expanded', String(!!item.expanded));
        top.append(this.el('span', item.expanded ? '−' : '+', 'wo-branch-toggle'));
      }
      button.setAttribute('aria-label', item.title || item.kind);
      button.title = item.expandable ? `${item.expanded ? 'Collapse' : 'Expand'} ${item.title}` : `Details: ${item.title}`;
      button.append(top, this.el('strong', short(item.title, 90)), this.el('span', short(item.detail, 65), 'wo-node-detail'));
      button.addEventListener('focus', () => {
        this.selectedNodeId = item.id; this.updateNodeFocus(); this.highlight(item.id);
      });
      button.addEventListener('pointerenter', () => this.highlight(item.id));
      button.addEventListener('pointerleave', () => this.highlight(this.inspected ? this.selectedNodeId : null));
      const old = previous?.get(item.id);
      if (!old) button.classList.add('wo-node-enter');
      else if ((old.x !== item.x || old.y !== item.y) && !this.win.matchMedia?.('(prefers-reduced-motion: reduce)').matches)
        button.animate?.([{ transform: `translate(${old.x - item.x}px,${old.y - item.y}px)` }, { transform: 'translate(0,0)' }],
          { duration: 190, easing: 'ease-out' });
      this.nodes.append(button);
    }
    this.canvasWidth = layout.width; this.canvasHeight = layout.height;
    this.canvas.style.width = layout.width + 'px'; this.canvas.style.height = layout.height + 'px';
    this.edges.setAttribute('width', String(layout.width)); this.edges.setAttribute('height', String(layout.height));
    // Expanding or collapsing content must not clamp or recenter the camera.
    this.transform({ clamp: false });
    this.highlight(this.inspected ? this.selectedNodeId : null);
    if (focused && this.geometry.has(focused)) {
      this.restoringFocus = true;
      try { this.nodeButton(focused)?.focus({ preventScroll: true }); }
      finally { this.restoringFocus = false; }
    }
    if (this.visible && this.mode === 'map') this.win.requestAnimationFrame(() => {
      if (this.needsFit) this.home();
    });
  }
  nodeButton(id) { return [...this.nodes.children].find(node => node.dataset.nodeId === id); }
  highlight(id) {
    const related = new Set(id ? [id] : []);
    // Trace the complete ancestor path, plus this branch's immediate children.
    let current = this.geometry?.get(id);
    while (current?.parent) { related.add(current.parent); current = this.geometry.get(current.parent); }
    for (const node of this.treeNodes || []) if (node.parent === id) related.add(node.id);
    this.map.classList?.toggle('wo-tracing', !!id);
    for (const node of this.nodes.children) node.classList.toggle?.('wo-unrelated', !!id && !related.has(node.dataset.nodeId));
    for (const edge of this.edges.children) edge.classList.toggle?.('wo-edge-selected',
      !!id && (!edge.dataset.to || related.has(edge.dataset.from) && related.has(edge.dataset.to)));
  }
  setExpanded(value) {
    if (this.expanded === value || !this.root) return;
    this.motion?.cancel(); this.expanded = value; this.root.classList.toggle('wo-expanded', value);
    this.expandButton.textContent = value ? '↙ Collapse' : '⛶ Expand';
    this.expandButton.setAttribute('aria-label', value ? 'Collapse map' : 'Expand map');
    this.expandButton.setAttribute('aria-expanded', String(value));
    this.resizeViewport();
  }
  updateNodeFocus() {
    for (const node of this.nodes.children) {
      node.tabIndex = node.dataset.nodeId === this.selectedNodeId ? 0 : -1;
      node.dataset.selected = String(node.dataset.nodeId === this.selectedNodeId);
    }
  }
  focusNode(id) {
    const item = this.geometry.get(id); if (!item) return;
    this.needsFit = false; this.selectedNodeId = id; this.updateNodeFocus();
    this.nodeButton(id)?.focus({ preventScroll: true });
    // Only explicit arrow-key navigation reveals an off-screen node.
    this.ensureNodeVisible(id);
  }
  ensureNodeVisible(id) {
    const item = this.geometry.get(id); if (!item) return;
    const x = this.pan.x + item.x * this.pan.scale, y = this.pan.y + item.y * this.pan.scale;
    const right = x + item.width * this.pan.scale, bottom = y + item.height * this.pan.scale;
    if (x < 70) this.pan.x += 70 - x;
    else if (right > this.map.clientWidth - 18) this.pan.x -= right - this.map.clientWidth + 18;
    if (y < 120) this.pan.y += 120 - y;
    else if (bottom > this.map.clientHeight - 65) this.pan.y -= bottom - this.map.clientHeight + 65;
    this.transform({ clamp: false });
  }
  navigateTree(id, key) {
    const index = this.treeNodes.findIndex(node => node.id === id), item = this.treeNodes[index];
    if (!item) return;
    if (key === 'ArrowRight' && item.expandable && !item.expanded) { void this.activateNode(id); return; }
    if (key === 'ArrowLeft') {
      if (item.expanded) { void this.activateNode(id); return; }
      if (item.parent) this.focusNode(item.parent);
      return;
    }
    const next = key === 'Home' ? this.treeNodes[0] : key === 'End' ? this.treeNodes.at(-1)
      : this.treeNodes[Math.max(0, Math.min(this.treeNodes.length - 1, index + (key === 'ArrowUp' ? -1 : 1)))];
    this.focusNode(next.id);
  }
  async activateNode(id) {
    const item = this.geometry.get(id); if (!item) return;
    this.selectedNodeId = id;
    if (item.kind === 'project') {
      await this.selectProject(item.record.alias, { expand: !this.expandedProjects.has(item.record.alias) });
      return;
    }
    if (item.kind === 'run') {
      if (this.expandedRuns.has(item.record.id)) this.expandedRuns.delete(item.record.id);
      else this.expandedRuns.add(item.record.id);
    }
    this.inspected = { kind: item.kind, id, record: item.record, run: item.run,
      project: item.record?.project || item.run?.project || (Array.isArray(item.record) ? item.record[0]?.project : null) };
    this.inspectorSignature = ''; this.renderInspector(); this.renderMap();
  }
  closeInspector(restore = true) {
    this.inspected = null; this.inspectorSignature = ''; this.inspector.hidden = true; this.inspector.replaceChildren();
    if (restore) this.nodeButton(this.selectedNodeId)?.focus({ preventScroll: true });
  }
  renderInspector() {
    if (!this.inspected || !this.data) { this.inspector.hidden = true; return; }
    const selection = this.inspected;
    const snapshot = this.projectSnapshots.get(selection.project) || this.data;
    let record = selection.record;
    if (selection.kind === 'project') record = this.data.projects.find(p => p.alias === selection.id);
    else if (selection.kind === 'chat') record = this.data.projects.find(p => p.alias === record?.alias);
    else if (['run', 'review', 'owner'].includes(selection.kind)) record = snapshot.runs.find(run => run.id === record?.id);
    else if (selection.kind === 'agent') record = snapshot.runs.find(run => run.id === selection.run?.id)?.agents?.find(a => a.id === record?.id);
    else if (selection.kind === 'request') record = (snapshot.requests || []).filter(r => r.project === selection.project);
    else if (selection.kind === 'automation') record = (snapshot.automation?.drafts || []).filter(d => d.project === selection.project);
    else if (selection.kind === 'vega') record = this.data;
    if (!record) { this.closeInspector(false); return; }
    const signature = JSON.stringify([selection.kind, record], (key, value) => key === 'checked_at' ? null : value);
    if (signature === this.inspectorSignature) return;
    const hadFocus = this.inspector.contains(this.doc.activeElement);
    // Polling must not remove a focused control or a disclosure the owner is reading.
    if (hadFocus && this.inspectorSignature) return;
    this.inspectorSignature = signature; this.inspector.hidden = false; this.inspector.replaceChildren();
    const header = this.el('header', '', 'wo-inspector-heading');
    const title = ['project', 'chat'].includes(selection.kind) ? record.label : selection.kind === 'vega' ? 'Vega work tree'
      : selection.kind === 'agent' ? record.label || record.role : selection.kind === 'request' ? 'Routing receipts'
      : selection.kind === 'automation' ? 'Automations' : selection.kind === 'review' ? 'Vega review'
      : selection.kind === 'owner' ? 'Your review' : selection.kind === 'empty' ? 'Project work' : 'Agent work';
    const close = this.button('×', () => this.closeInspector(), 'wo-inspector-close'); close.setAttribute('aria-label', 'Close work details');
    header.append(this.el('h3', title), close); this.inspector.append(header);
    const body = this.el('div', '', 'wo-inspector-body'); this.inspector.append(body);
    const paragraph = (text, cls = '') => { if (text) body.append(this.el('p', String(text), cls)); };
    const recorded = value => value ? new Date(typeof value === 'number' ? value * 1000 : value).toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' }) : 'Not recorded';
    if (selection.kind === 'project' || selection.kind === 'chat') {
      paragraph(projectState(record), 'wo-detail-state');
      if (chatSource(record)?.thread_id) {
        const activity = chatSource(record);
        if (activity.latest_update?.text) body.append(this.reply(activity.latest_update.text));
        if (activity.latest_update?.at) paragraph(`Public update ${recorded(activity.latest_update.at)}`, 'wo-detail-meta');
        paragraph(`Session update ${recorded(activity.observed_at)}`, 'wo-detail-meta');
        if (activity.source_error) paragraph(activity.source_error);
      }
      if (selection.kind === 'chat') return;
      if (record.freshness?.evidence_at) paragraph(`Project notes recorded ${recorded(record.freshness.evidence_at)}`, 'wo-detail-meta');
      const connection = record.connection;
      if (connection) {
        const group = this.el('details'); group.append(this.el('summary', 'Chat connection'));
        const source = connection.source, destination = connection.destination;
        if (connection.can_route === false) group.append(this.el('p', 'Chat not found. Link a current chat to send requests.'));
        else if (source?.state === 'missing') group.append(this.el('p', 'History unavailable. Requests use the registered project owner.'));
        else group.append(this.el('p', source?.title ? `History: ${source.title}` : 'This chat has not been checked yet.'));
        if (connection.via_project) group.append(this.el('p', `Requests: ${destination?.title || record.routing_owner}`));
        if (source?.checked_at) group.append(this.el('p', `History checked ${recorded(source.checked_at)}`, 'wo-detail-meta'));
        if (connection.via_project && destination?.checked_at) group.append(this.el('p', `Destination checked ${recorded(destination.checked_at)}`, 'wo-detail-meta'));
        if (connection.state === 'check_due') group.append(this.el('p', 'Connection check is over a day old.'));
        body.append(group);
      }
      if (!Object.hasOwn(LABELS, record.status)) paragraph(record.status);
      for (const [key, label] of [['blockers', 'Blockers'], ['working', 'In progress'], ['next', 'Next'], ['done', 'Completed notes'], ['suggestions', 'Suggestions']]) {
        const values = record.checklist?.[key] || []; if (!values.length) continue;
        const group = this.el('details'); group.append(this.el('summary', `${label} · ${values.length}`));
        const list = this.el('ul');
        for (const value of values) list.append(this.el('li', typeof value === 'string' ? value : value.text || value.label || ''));
        group.append(list); body.append(group);
      }
      if (!Object.values(record.checklist || {}).some(value => Array.isArray(value) && value.length)) paragraph('No checklist items saved.');
      paragraph('Its work branch is expanded on the map.', 'wo-detail-meta');
    } else if (selection.kind === 'vega') {
      paragraph(record.work_paused ? 'Automatic work is paused.' : 'Automatic work is available.');
      paragraph(`${record.projects.length} projects · ${record.agents?.length || 0} specialist roles`);
      paragraph('Choose a project to expand its branch. Select a work item for its status and review.');
      const help = this.el('details'); help.append(this.el('summary', 'Map controls'), this.el('p', 'Drag or scroll to pan. Ctrl + scroll, pinch, or + / − to zoom. Tab focuses the tree; arrow keys move between nodes. Enter expands a branch. Escape closes details. F fits the tree; 0 returns to the top.')); body.append(help);
    } else if (selection.kind === 'request') {
      for (const receipt of record) {
        const card = this.el('details'); card.append(this.el('summary', `${stateLabel(receipt.state)} · ${short(receipt.instruction, 60)}`));
        card.append(this.el('p', receipt.instruction), this.el('p', `Recorded ${recorded(receipt.created_at)}`, 'wo-detail-meta'));
        const text = receipt.route_receipt?.receipt; if (text) card.append(this.el('p', typeof text === 'string' ? text : JSON.stringify(text)));
        body.append(card);
      }
      body.append(this.button('Review routing receipts', this.onRequests, 'wo-detail-action'));
    } else if (selection.kind === 'automation') {
      for (const draft of record) paragraph(stateLabel(draft.state));
      body.append(this.button('Workflow controls', () => this.openAutomations(), 'wo-detail-action'));
    } else if (selection.kind === 'agent') {
      paragraph(stateLabel(record.state), 'wo-detail-state'); paragraph(`${record.model_requests || 0} model requests`, 'wo-detail-meta'); paragraph(record.error);
      body.append(this.button('Open work review', () => this.onRun(selection.run.id), 'wo-detail-action'));
    } else if (selection.kind === 'empty') {
      paragraph('No recent agent runs for this project. The project notes and routing receipts are separate from agent work.');
    } else {
      paragraph(record.goal); paragraph(record.activity?.label || stateLabel(record.state), 'wo-detail-state');
      paragraph(`Updated ${recorded(record.updated_at)}`, 'wo-detail-meta');
      const progress = stepProgress(record); if (progress) paragraph(progress.text);
      if (selection.kind === 'review') paragraph(record.review?.summary || 'No review summary recorded.');
      paragraph(record.error);
      body.append(this.button(record.state === 'awaiting_approval' ? 'Review exact change' : 'Open work review', () => this.onRun(record.id), 'wo-detail-action'));
    }
  }
  async openAutomations() {
    if (!this.post) return this.onAutomations();
    if (!this.automationDialog) {
      const dialog = this.el('dialog', '', 'wo-automation-dialog'); dialog.setAttribute('aria-label', 'Workflow controls');
      const close = this.button('Close', () => dialog.close(), 'wo-overlay-close');
      const content = this.el('div'); dialog.append(close, content); this.root.append(dialog);
      this.automationDialog = dialog;
      const { N8nPanel } = await import('./n8n-panel.js');
      if (this.disposed) return;
      this.automationPanel = new N8nPanel(content, { get: this.get, post: this.post, onRun: this.onRun });
      dialog.addEventListener('close', () => this.automationPanel?.hide());
    }
    if (!this.automationPanel || this.disposed || !this.visible) return;
    if (!this.automationDialog.open) this.automationDialog.showModal();
    await this.automationPanel.show();
  }
  renderBoard(items) {
    const scroll = this.board.scrollTop;
    const focusedChat = this.doc.activeElement?.closest?.('.wo-chat-card')?.dataset.project;
    this.expandedChatCards ||= new Set();
    this.board.replaceChildren();
    for (const [key, label] of COLUMNS) {
      const column = this.el('section', '', 'wo-column'); column.dataset.column = key;
      const chosen = items.filter(item => workColumn(item) === key), heading = this.el('h3', label);
      heading.append(this.el('span', String(chosen.length), 'wo-column-count')); column.append(heading);
      if (!chosen.length) column.append(this.el('p', 'No items', 'wo-empty'));
      for (const item of chosen) {
        if (item.kind === 'chat') {
          const card = this.el('details', '', 'wo-card wo-chat-card'); card.dataset.project = item.project;
          card.open = this.expandedChatCards.has(item.project);
          const summary = this.el('summary');
          summary.append(this.el('span', 'Chat activity', 'wo-card-kind'), this.el('strong', item.title),
            this.el('span', item.status, 'wo-card-status'), this.el('small', recordedAt(item.updated_at)));
          card.append(summary);
          if (item.chat.latest_update?.text) card.append(
            this.el('small', `Update ${recordedAt(item.chat.latest_update.at)}`),
            this.reply(item.chat.latest_update.text));
          else card.append(this.el('p', 'No public message recorded for this turn yet.'));
          if (!item.chat.available) card.append(this.el('p', 'Chat unavailable · showing the last saved update.'));
          card.addEventListener('toggle', () => {
            if (card.isConnected) card.open ? this.expandedChatCards.add(item.project) : this.expandedChatCards.delete(item.project);
          });
          column.append(card);
          continue;
        }
        const card = this.button('', () => item.kind === 'run' ? this.onRun(item.id) : item.kind === 'automation' ? this.openAutomations() : this.onRequests(), 'wo-card');
        card.dataset.kind = item.kind;
        const type = item.kind === 'run' ? item.mode === 'automation' ? 'SAVED AGENT WORKFLOW' : 'SAVED AGENT RUN' : item.kind === 'request' ? 'PROJECT ROUTING' : 'N8N WORKFLOW';
        card.append(this.el('span', type, 'wo-card-kind'), this.el('strong', item.title), this.el('small', item.label || item.project), this.el('span', item.status, 'wo-card-status'));
        if (item.created_at) card.append(this.el('small', `Recorded ${recordedAt(item.created_at)}`));
        const progress = stepProgress(item);
        if (progress) {
          const meter = this.el('progress'); meter.max = 100; meter.value = progress.percent; meter.setAttribute('aria-label', progress.text);
          card.append(meter, this.el('small', `${progress.percent}% · ${progress.text}`));
        }
        column.append(card);
      }
      this.board.append(column);
    }
    this.board.scrollTop = scroll;
    if (focusedChat) [...this.board.querySelectorAll('.wo-chat-card')].find(card => card.dataset.project === focusedChat)?.querySelector('summary')?.focus({ preventScroll: true });
  }
  transform({ clamp = true } = {}) {
    if (this.map.clientWidth && this.canvasWidth) {
      this.viewportSize = { width: this.map.clientWidth, height: this.map.clientHeight };
      if (clamp) {
        this.pan.x = Math.max(120 - this.canvasWidth * this.pan.scale, Math.min(this.map.clientWidth - 120, this.pan.x));
        this.pan.y = Math.max(160 - this.canvasHeight * this.pan.scale, Math.min(this.map.clientHeight - 160, this.pan.y));
      }
    }
    this.canvas.style.transform = `translate(${this.pan.x}px,${this.pan.y}px) scale(${this.pan.scale})`;
    this.zoomLabel.textContent = Math.round(this.pan.scale * 100) + '%';
  }
  resizeViewport() {
    this.motion?.cancel();
    if (!this.visible || this.mode !== 'map') return;
    if (this.needsFit) { this.home(); return; }
    if (this.viewportSize) {
      this.pan.x += (this.map.clientWidth - this.viewportSize.width) / 2;
      this.pan.y += (this.map.clientHeight - this.viewportSize.height) / 2;
    }
    this.transform();
  }
  home() {
    if (this.mode !== 'map' || !this.map.clientWidth || !this.canvasWidth) return;
    const width = this.map.clientWidth;
    const scale = width < 600 ? .85 : Math.min(1, (width - 150) / this.canvasWidth);
    this.transition({ scale, x: width / 2 + 28 - 490 * scale, y: 145 });
  }
  fitWidth() {
    if (!this.canvasWidth || this.mode !== 'map') return;
    const scale = Math.min(1, Math.max(.08, (this.map.clientWidth - 140) / this.canvasWidth));
    this.transition({ scale, x: 70, y: 130 });
  }
  fit() {
    if (this.mode !== 'map' || this.map.clientWidth < 1 || !this.canvasWidth) return;
    const width = this.map.clientWidth, height = this.map.clientHeight;
    const scale = Math.min(1, Math.max(.08, Math.min((width - 140) / this.canvasWidth, (height - 190) / this.canvasHeight)));
    this.transition({ scale, x: 45 + (width - this.canvasWidth * scale) / 2,
      y: 130 + (height - 190 - this.canvasHeight * scale) / 2 });
  }
  transition(next) {
    this.needsFit = false;
    if (this.motion) this.motion.move({ ...this.pan }, next, frame => { this.pan = frame; this.transform({ clamp: false }); });
    else { this.pan = next; this.transform({ clamp: false }); }
  }
  zoomBy(factor, x = this.map.clientWidth / 2, y = this.map.clientHeight / 2, animated = false) {
    this.motion?.cancel();
    const before = this.pan.scale, after = Math.min(2.4, Math.max(.08, before * factor));
    const next = { x: x - (x - this.pan.x) * after / before, y: y - (y - this.pan.y) * after / before, scale: after };
    if (animated) this.transition(next);
    else { this.pan = next; this.needsFit = false; this.transform(); }
  }
  destroy() {
    this.disposed = true; this.sequence++; this.visible = false;
    this.motion?.cancel(); this.setExpanded(false);
    if (this.timer !== null) this.win.clearInterval(this.timer);
    this.win.removeEventListener('resize', this.resize);
    if (this.automationDialog?.open) this.automationDialog.close();
    this.automationPanel?.destroy();
  }
}
