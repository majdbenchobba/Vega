import { projectMark } from '/mini/visuals.js';
import { impacts, nodeImpact, overviewGraph, scopedGraph, recoveryTone, formattedDate, observedStatus, machineHealth, hostingStatus, observationOld } from './infrastructure-model.js';
import { MapCamera, mapLayout, horizontalEdgePoints } from './infrastructure-camera.js';

const PROVIDERS = { local: 'Laptop', aws: 'AWS', cloudflare: 'Cloudflare', other: 'External', project: 'Project' };
const ICONS = {
  project: 'M3 6h7l2 2h9v12H3ZM3 6V3h7l2 3',
  runtime: 'M4 3h16v7H4ZM4 14h16v7H4ZM7 6h.01M7 17h.01M11 6h6M11 17h6',
  service: 'M7 16a5 5 0 0 1 0-10 7 7 0 0 1 13 3 4 4 0 0 1-1 8H7Z',
  storage: 'M4 5c0-4 16-4 16 0s-16 4-16 0Zm0 0v14c0 4 16 4 16 0V5M4 12c0 4 16 4 16 0',
};
function el(doc, tag, cls, text) {
  const n = doc.createElement(tag); if (cls) n.className = cls; if (text !== undefined) n.textContent = text; return n;
}
function svg(doc, tag, attrs = {}) {
  const n = doc.createElementNS('http://www.w3.org/2000/svg', tag);
  for (const [key, value] of Object.entries(attrs)) n.setAttribute(key, String(value));
  return n;
}
function symbol(doc, kind, provider) {
  const wrap = el(doc, 'span', 'infra-symbol'); wrap.dataset.provider = provider;
  const image = svg(doc, 'svg', { viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor', 'stroke-width': 1.5, 'aria-hidden': true });
  image.append(svg(doc, 'path', { d: ICONS[kind] || ICONS.service })); wrap.append(image); return wrap;
}
function badge(doc, text, tone = '') { return el(doc, 'span', 'infra-badge ' + tone, text); }

export class InfrastructurePanel {
  constructor(root, { get, post }) {
    this.root = root; this.doc = root.ownerDocument; this.get = get; this.post = post;
    this.group = 'all'; this.tab = 'map'; this.scenario = 'normal'; this.query = ''; this.selected = null;
    this.mapViews = new Map(); this.expanded = false; this.orientation = 'horizontal';
    this.active = false; this.loading = false; this.timer = null; this.revision = 0;
    this.root.classList.add('infrastructure');
    this.build();
    this.reposition = () => this.positionDrawer();
    this.doc.defaultView.addEventListener('resize', this.reposition);
    this.doc.defaultView.addEventListener('scroll', this.reposition, { capture: true, passive: true });
  }
  node(tag, cls, text) { return el(this.doc, tag, cls, text); }
  button(label, action, cls = '') {
    const n = this.node('button', cls, label); n.type = 'button'; n.onclick = action; return n;
  }
  build() {
    const header = this.node('header', 'infra-header');
    const title = this.node('div'); title.append(this.node('span', 'infra-eyebrow', 'YOUR SYSTEMS'), this.node('h1', '', 'Infrastructure'));
    this.freshness = this.node('p', 'infra-muted', 'Loading observed state…'); title.append(this.freshness);
    const actions = this.node('div', 'infra-actions');
    this.refreshButton = this.button('↻ Refresh view', () => void this.load());
    this.cloudButton = this.button('Check cloud', () => void this.refreshCloud(), 'infra-primary');
    this.cloudButton.title = 'Read AWS and Cloudflare metadata using existing access';
    actions.append(this.refreshButton, this.cloudButton); header.append(title, actions);
    this.metrics = this.node('div', 'infra-metrics');
    const controls = this.node('div', 'infra-controls');
    this.tabs = this.node('div', 'infra-tabs'); this.tabs.setAttribute('role', 'tablist'); this.tabs.setAttribute('aria-label', 'Infrastructure views');
    for (const [key, label] of [['map', 'Map'], ['recovery', 'Recovery'], ['inventory', 'Inventory']]) {
      const b = this.button(label, () => { this.setExpanded(false); this.tab = key; this.closeDetail(false); this.renderContent(); });
      b.dataset.tab = key; b.setAttribute('role', 'tab'); this.tabs.append(b);
    }
    this.project = this.doc.createElement('select'); this.project.setAttribute('aria-label', 'Project');
    this.project.onchange = () => { this.group = this.project.value; this.closeDetail(false); this.renderContent(); };
    this.search = this.doc.createElement('input'); this.search.type = 'search'; this.search.placeholder = 'Find a service or project';
    this.search.setAttribute('aria-label', 'Find a service or project');
    this.search.oninput = () => { this.query = this.search.value.toLowerCase().trim(); this.renderContent(); };
    controls.append(this.tabs, this.project, this.search);
    const sim = this.node('div', 'infra-simulation');
    const simLabel = this.node('label', '', 'What if…');
    this.scenarioSelect = this.doc.createElement('select'); this.scenarioSelect.setAttribute('aria-label', 'Outage scenario');
    simLabel.append(this.scenarioSelect); sim.append(simLabel, this.node('span', 'infra-muted', 'Simulation only'));
    this.scenarioSelect.onchange = () => { this.scenario = this.scenarioSelect.value; this.renderContent(); if (this.selected) this.showDetail(this.selected, false); };
    this.scenarioSummary = this.node('div', 'infra-scenario-summary'); this.scenarioSummary.setAttribute('role', 'status'); sim.append(this.scenarioSummary);
    this.error = this.node('div', 'infra-error'); this.error.setAttribute('role', 'alert'); this.error.hidden = true;
    this.body = this.node('div', 'infra-body');
    this.content = this.node('div', 'infra-content'); this.content.setAttribute('role', 'tabpanel');
    this.drawer = this.node('aside', 'infra-detail'); this.drawer.setAttribute('aria-label', 'Infrastructure details'); this.drawer.hidden = true;
    this.body.append(this.content, this.drawer);
    this.footer = this.node('footer', 'infra-footer');
    this.footer.append(this.node('span', '', 'Observed inventory · reviewed relationships · unknowns stay visible'));
    this.scopeButton = this.button('Evidence & scope', () => this.showScope()); this.footer.append(this.scopeButton);
    this.root.replaceChildren(header, this.metrics, controls, sim, this.error, this.body, this.footer);
    this.root.addEventListener('keydown', event => {
      if (event.key !== 'Escape') return;
      if (!this.drawer.hidden) this.closeDetail();
      else this.setExpanded(false);
    });
  }
  async show() { this.active = true; await this.load(); }
  hide() { this.camera?.motion.cancel(); this.setExpanded(false); this.active = false; clearTimeout(this.timer); this.timer = null; }
  destroy() {
    this.hide(); this.revision++;
    this.camera?.destroy();
    this.doc.defaultView.removeEventListener('resize', this.reposition);
    this.doc.defaultView.removeEventListener('scroll', this.reposition, true);
  }
  message(value) { this.error.textContent = value || ''; this.error.hidden = !value; }
  async load() {
    if (this.loading || !this.active) return;
    const revision = this.revision;
    this.loading = true; this.refreshButton.disabled = true;
    try {
      const data = await this.get('/api/infrastructure');
      if (revision !== this.revision || !this.active) return;
      const drawerScroll = this.drawer.scrollTop;
      this.data = data; this.message(data.refresh.error); this.renderHeader(); this.renderContent();
      if (this.selected) { this.showDetail(this.selected, false); this.drawer.scrollTop = drawerScroll; }
      this.cloudButton.disabled = data.refresh.active; this.cloudButton.textContent = data.refresh.active ? 'Checking cloud…' : 'Check cloud';
      if (data.refresh.active) this.timer = setTimeout(() => { this.timer = null; void this.load(); }, 5000);
    } catch (error) { if (this.active) this.message(error.message || 'Inventory unavailable.'); }
    finally { this.loading = false; this.refreshButton.disabled = false; }
  }
  async refreshCloud() {
    this.cloudButton.disabled = true; this.message('');
    try { const result = await this.post('/api/infrastructure/refresh', {}); this.message(result.message); await this.load(); }
    catch (error) { this.message(error.message); }
    finally { if (!this.data?.refresh.active) this.cloudButton.disabled = false; }
  }
  renderHeader() {
    const data = this.data, scopes = data.cloud.scopes, w = data.workspace;
    const times = scopes.map(s => s.ok ? s.checked_at : s.last_success?.checked_at).filter(Boolean).sort();
    const r2Scope = scopes.find(s => s.service === 'r2'), r2 = r2Scope?.ok ? r2Scope : r2Scope?.last_success;
    this.freshness.textContent = `Cloud observations: ${formattedDate(times.at(-1))} · ${scopes.filter(s => !s.ok).length} reads unavailable · relationships: ${formattedDate(data.audited_at)}`;
    this.metrics.replaceChildren();
    const counts = [
      [w.available ? w.projects : '?', 'Codex projects', w.available ? `${w.chats} chats · ${w.independent} independent` : 'Live inventory unavailable'],
      [data.machines.length ? data.machines.filter(n => n.observed_state === 'running').length : '?',
        data.machines.some(n => observationOld(n)) ? 'AWS last known running' : 'AWS machines running',
        `${data.machines.length} found · EC2 + Lightsail`],
      [r2?.rows.length ?? '?', 'R2 buckets', r2Scope?.ok ? 'Presence checked separately from contents' : 'Last observation · latest read unavailable'],
      [data.backups.filter(b => recoveryTone(b) !== 'good').length, 'Recovery gaps', `${data.backups.length} scoped recovery records`],
    ];
    for (const [number, title, detail] of counts) {
      const item = this.node('div', 'infra-metric'); item.append(this.node('strong', '', String(number)),
        this.node('span', '', title), this.node('small', '', detail)); this.metrics.append(item);
    }
    this.project.replaceChildren(new Option('All projects', 'all'));
    for (const group of data.groups) this.project.append(new Option(group.label + (group.current === false ? ' · saved map' : ''), group.id));
    if (![...this.project.options].some(o => o.value === this.group)) this.group = 'all';
    this.project.value = this.group;
    this.scenarioSelect.replaceChildren();
    for (const [value, label] of [['normal', 'Normal operation'], ['aws', 'AWS suspended / unavailable'], ['laptop', 'Laptop shut down'], ['cloudflare', 'Cloudflare unavailable']])
      this.scenarioSelect.append(new Option(label, value));
    for (const machine of data.machines) this.scenarioSelect.append(new Option('Stop ' + machine.label, 'server:' + machine.id));
    if (![...this.scenarioSelect.options].some(o => o.value === this.scenario)) this.scenario = 'normal';
    this.scenarioSelect.value = this.scenario;
  }
  renderContent() {
    if (!this.data) return;
    this.camera?.destroy(); this.camera = null;
    this.state = impacts(this.data, this.scenario);
    for (const b of this.tabs.children) b.setAttribute('aria-selected', String(b.dataset.tab === this.tab));
    const projectNodes = this.data.nodes.filter(n => n.kind === 'project' && (this.group === 'all' || n.group === this.group));
    const stopped = projectNodes.filter(n => this.state.get(n.id)?.severity === 2).length;
    const partial = projectNodes.filter(n => this.state.get(n.id)?.severity === 1).length;
    this.scenarioSummary.textContent = this.scenario === 'normal' ? 'Select a node to explore.' : `${stopped} unavailable · ${partial} partly affected · mapped dependencies only`;
    this.root.dataset.scenario = this.scenario === 'normal' ? 'normal' : 'outage';
    this.content.replaceChildren();
    if (this.tab === 'map') this.renderMap();
    if (this.tab === 'recovery') this.renderRecovery();
    if (this.tab === 'inventory') this.renderInventory();
  }
  match(item) { return !this.query || [item.label, item.id, item.summary, item.provider, item.bucket, ...(item.children || [])].join(' ').toLowerCase().includes(this.query); }
  mark(item) {
    return item.kind === 'project' ? projectMark(this.doc, { id: item.project_id, alias: item.alias })
      : symbol(this.doc, item.kind, item.provider);
  }
  status(item) {
    const impact = nodeImpact(item, this.state);
    if (item.planned) return ['Planned', 'neutral'];
    if (this.scenario !== 'normal') {
      if (impact.severity === 2) return ['Would stop', 'bad'];
      if (impact.severity === 1) return ['Partly affected', 'warn'];
      if (impact.backup) return ['Backup unavailable', 'warn'];
      return [item.unmapped ? 'Not mapped' : 'No mapped impact', 'neutral'];
    }
    const observed = observedStatus(item) || hostingStatus(item, this.data);
    if (observed) return observed;
    return [item.unmapped ? 'Not mapped' : item.kind === 'project' ? 'Mapped' : 'Documented', 'neutral'];
  }
  renderMap() {
    const graph = overviewGraph(this.data, this.group); this.graph = graph;
    const visible = graph.nodes.filter(n => this.match(n));
    const bar = this.node('div', 'infra-map-bar');
    const legend = this.node('div', 'infra-legend');
    legend.append(this.node('span', 'infra-line-key', 'Dependency →'), this.node('span', 'infra-line-key backup', 'Recovery copy'), this.node('span', 'infra-line-key planned', 'Planned'));
    const zoom = this.node('div', 'infra-zoom');
    const less = this.button('−', () => this.camera?.zoomButton(this.camera.camera.z / 1.2));
    less.setAttribute('aria-label', 'Zoom out');
    const more = this.button('+', () => this.camera?.zoomButton(this.camera.camera.z * 1.2));
    more.setAttribute('aria-label', 'Zoom in');
    const value = this.button('100%', () => this.camera?.zoomButton(1), 'infra-zoom-value');
    value.setAttribute('aria-label', 'Reset zoom to 100%');
    this.expandButton = this.button(this.expanded ? '↙ Collapse' : '⛶ Expand', () => this.setExpanded(!this.expanded));
    this.expandButton.setAttribute('aria-label', this.expanded ? 'Collapse map' : 'Expand map');
    this.expandButton.setAttribute('aria-expanded', String(this.expanded));
    const orientation = this.node('select'); orientation.setAttribute('aria-label', 'Map layout');
    for (const [key, label] of [['horizontal', 'Horizontal'], ['vertical', 'Vertical']]) {
      const option = this.node('option', '', label); option.value = key; orientation.append(option);
    }
    orientation.value = this.orientation;
    orientation.onchange = () => { this.orientation = orientation.value; this.renderContent(); };
    zoom.append(orientation, less, value, more, this.button('Fit all', () => this.camera?.fit()),
      this.button(this.orientation === 'horizontal' ? 'Fit height' : 'Fit width',
        () => this.camera?.fit(this.orientation === 'horizontal' ? 'height' : false)), this.expandButton);
    bar.append(legend, zoom); this.content.append(bar);
    if (!visible.length) { this.content.append(this.node('p', 'infra-empty', 'No matching nodes.')); return; }
    const scroll = this.node('div', 'infra-canvas-scroll'); scroll.tabIndex = 0;
    scroll.setAttribute('role', 'region'); scroll.setAttribute('aria-label', 'Dependency map');
    scroll.setAttribute('aria-description', 'Drag to move. Control and scroll, or pinch, to zoom. Arrow keys move; Home fits all.');
    const canvas = this.node('div', 'infra-canvas');
    const columns = ['project', 'runtime', 'service', 'storage'], titles = ['PROJECTS', 'WHERE IT RUNS', 'CONNECTED SERVICES', 'DATA & RECOVERY'];
    const layout = mapLayout(visible, this.orientation), { positions: position, width, height, cardWidth, cardHeight, sections } = layout;
    const stages = this.node('nav', 'infra-stage-nav'); stages.setAttribute('aria-label', 'Map sections');
    columns.forEach((kind, column) => {
      const heading = this.node('div', 'infra-column-heading', titles[column]);
      heading.style.left = sections[column].x + 'px'; heading.style.top = (sections[column].y + 15) + 'px'; canvas.append(heading);
      const section = this.node('div', 'infra-section-area');
      Object.assign(section.style, { left: (sections[column].x - 14) + 'px', top: (sections[column].y - 4) + 'px',
        width: (sections[column].width + 28) + 'px', height: sections[column].height + 'px' });
      canvas.prepend(section);
      stages.append(this.button(['Projects', 'Hosts', 'Services', 'Recovery'][column], () => this.camera?.focusSection(sections[column])));
    });
    this.content.append(stages);
    canvas.style.width = width + 'px'; canvas.style.height = height + 'px';
    const paths = svg(this.doc, 'svg', { width, height, class: 'infra-connections', 'aria-hidden': true });
    const definitions = svg(this.doc, 'defs');
    const marker = svg(this.doc, 'marker', { id: 'infra-arrow', viewBox: '0 0 10 10', refX: 8, refY: 5, markerWidth: 5, markerHeight: 5, orient: 'auto-start-reverse' });
    marker.append(svg(this.doc, 'path', { d: 'M0 0L10 5L0 10Z', fill: 'context-stroke' })); definitions.append(marker); paths.append(definitions);
    for (const [edgeIndex, e] of graph.edges.entries()) {
      const a = position.get(e.source), b = position.get(e.target); if (!a || !b) continue;
      const forward = b.x > a.x, ax = a.x + (forward ? cardWidth : 0), bx = b.x + (forward ? 0 : cardWidth);
      const mid = forward ? (ax + bx) / 2 : Math.max(a.x, b.x) + cardWidth + 40;
      const active = this.selected === e.source || this.selected === e.target;
      const d = this.orientation === 'horizontal'
        ? horizontalEdgePoints(a, b, cardWidth, cardHeight, edgeIndex, position.values()).map((p, i) => `${i ? 'L' : 'M'}${p.x},${p.y}`).join(' ')
        : `M${ax},${a.y + cardHeight / 2}C${mid},${a.y + cardHeight / 2} ${mid},${b.y + cardHeight / 2} ${bx},${b.y + cardHeight / 2}`;
      const path = svg(this.doc, 'path', { d,
        class: `infra-edge ${e.effect} ${e.planned ? 'planned' : ''} ${active ? 'selected' : ''}`, 'data-source': e.source,
        'data-target': e.target, 'marker-end': 'url(#infra-arrow)' });
      path.append(svg(this.doc, 'title')); path.firstChild.textContent = e.label; paths.append(path);
    }
    canvas.append(paths);
    for (const item of visible) {
      const p = position.get(item.id), b = this.button('', () => this.showDetail(item.id), 'infra-node');
      b.dataset.id = item.id; b.dataset.provider = item.provider;
      b.style.left = p.x + 'px'; b.style.top = p.y + 'px';
      b.classList.toggle('selected', item.id === this.selected);
      b.onpointerenter = () => this.highlight(item.id);
      b.onpointerleave = () => this.highlight(this.selected);
      b.onfocus = () => this.highlight(item.id);
      b.onblur = () => this.highlight(this.selected);
      const [status, tone] = this.status(item);
      const copy = this.node('span', 'infra-node-copy'); copy.append(this.node('strong', '', item.label), this.node('small', '', item.summary));
      const states = this.node('span', 'infra-node-states'); states.append(badge(this.doc, status, tone));
      const health = this.scenario === 'normal' ? machineHealth(item) : null;
      if (health) states.append(badge(this.doc, ...health));
      b.append(this.mark(item), copy, states);
      if (item.observed_at) {
        const time = this.node('small', 'infra-node-observed', formattedDate(item.observed_at));
        time.title = 'Last observed server state'; b.append(time);
      }
      b.setAttribute('aria-label', [item.label, item.summary, status, health?.[0], item.observed_at ? 'Observed ' + formattedDate(item.observed_at) : null].filter(Boolean).join(' — '));
      canvas.append(b);
    }
    scroll.append(canvas); this.content.append(scroll);
    const hint = this.node('div', 'infra-map-hint', 'Drag to move · Ctrl + scroll or pinch to zoom');
    this.content.append(hint);
    const viewKey = this.orientation + '|' + this.group + '|' + this.query;
    this.camera = new MapCamera(scroll, canvas, layout, {
      saved: this.mapViews.get(viewKey),
      onChange: state => { this.mapViews.set(viewKey, state); value.textContent = Math.round(state.z * 100) + '%'; },
    });
    this.highlight(this.selected);
  }
  setExpanded(value) {
    if (this.expanded === value) return;
    this.expanded = value; this.root.classList.toggle('infra-expanded', value);
    if (this.expandButton) {
      this.expandButton.textContent = value ? '↙ Collapse' : '⛶ Expand';
      this.expandButton.setAttribute('aria-label', value ? 'Collapse map' : 'Expand map');
      this.expandButton.setAttribute('aria-expanded', String(value));
      this.expandButton.focus({ preventScroll: true });
    }
    this.positionDrawer();
  }
  highlight(id) {
    const related = new Set(id ? [id, ...(this.graph?.nodes.filter(n => n.children?.includes(id)).map(n => n.id) || [])] : []);
    const neighbors = new Set(related);
    for (const path of this.content.querySelectorAll('.infra-edge')) {
      const active = related.has(path.dataset.source) || related.has(path.dataset.target);
      path.classList.toggle('selected', active);
      if (active) { neighbors.add(path.dataset.source); neighbors.add(path.dataset.target); }
    }
    this.content.classList.toggle('infra-tracing', !!id);
    for (const node of this.content.querySelectorAll('.infra-node')) node.classList.toggle('unrelated', !!id && !neighbors.has(node.dataset.id));
  }
  scopedBackups() {
    const ids = new Set(this.data.nodes.filter(n => this.group === 'all' || n.group === this.group).map(n => n.id));
    return this.data.backups.filter(b => ids.has(b.id) && this.match(b));
  }
  renderRecovery() {
    const intro = this.node('div', 'infra-section-intro');
    intro.append(this.node('h2', '', 'Recovery coverage'), this.node('p', 'infra-muted', 'A copy, a restore and a running replacement are different checkpoints.'));
    this.content.append(intro);
    const wrapper = this.node('div', 'infra-table-scroll'), table = this.node('table', 'infra-table');
    const head = this.node('thead'), row = this.node('tr');
    for (const title of ['Project', 'Offsite copy', 'Restore evidence', 'Full failover', 'Schedule', 'Captured']) row.append(this.node('th', '', title));
    head.append(row); table.append(head); const body = this.node('tbody');
    for (const item of this.scopedBackups()) {
      const r = this.node('tr'), name = this.node('td'); name.append(this.button(item.label + ' ↗', () => this.showDetail(item.id), 'infra-text-button'));
      const copy = this.node('td'); copy.append(badge(this.doc, item.copy, recoveryTone(item)));
      if (item.source_changed) copy.append(this.node('small', 'infra-warn', 'Source updated · review'));
      if (item.bucket && item.presence === 'unknown') copy.append(this.node('small', 'infra-warn', 'Destination: ' + (item.last_presence !== 'unknown' ? 'last known ' + item.last_presence : 'not verified')));
      for (const cell of [name, copy, this.node('td', '', item.restore), this.node('td', 'infra-muted', item.failover), this.node('td', '', item.cadence), this.node('td', 'infra-muted', formattedDate(item.captured_at))]) r.append(cell);
      body.append(r);
    }
    table.append(body); wrapper.append(table); this.content.append(wrapper);
    if (!body.children.length) this.content.append(this.node('p', 'infra-empty', 'No recovery records match this filter.'));
    const priorities = this.node('div', 'infra-priorities');
    for (const [n, title, detail] of [
      ['01', 'Independent runtime', 'LeadForge / PV: copies exist; replacement hosting and cutover remain untested.'],
      ['02', 'Close coverage gaps', 'Verify missing destinations and later writes before relying on old receipts.'],
      ['03', 'Replace Vega’s AWS path', 'AI, transcription and voice need an implemented alternative. R2 backups remain paused.'],
    ]) { const card = this.node('article'); card.append(this.node('span', 'infra-eyebrow', n), this.node('h3', '', title), this.node('p', 'infra-muted', detail)); priorities.append(card); }
    this.content.append(priorities);
  }
  renderInventory() {
    const nodeIds = new Set(scopedGraph(this.data, this.group).nodes.map(n => n.id));
    const list = this.node('div', 'infra-server-grid');
    for (const item of this.data.machines.filter(n => (this.group === 'all' || nodeIds.has(n.id)) && this.match(n))) {
      const card = this.button('', () => this.showDetail(item.id), 'infra-server-card');
      const [status, tone] = this.status(item);
      card.append(symbol(this.doc, 'runtime', 'aws'), this.node('h3', '', item.label), badge(this.doc, status, tone),
        this.node('p', 'infra-muted', item.summary), this.node('small', '', item.id)); list.append(card);
    }
    this.content.append(this.node('h2', 'infra-section-heading', 'Machines'), list);
    const scopes = this.node('div', 'infra-scope-list');
    for (const scope of this.data.cloud.scopes.filter(s => !['ec2', 'lightsail', 'snapshots', 'volumes', 'instance-status'].includes(s.service))) {
      const observed = scope.ok ? scope : scope.last_success;
      const rows = (observed?.rows || []).filter(row => !this.query || JSON.stringify(row).toLowerCase().includes(this.query));
      if (this.query && !rows.length) continue;
      const details = this.node('details', 'infra-provider-inventory'), summary = this.node('summary');
      summary.append(this.node('strong', '', `${scope.provider === 'aws' ? 'AWS' : 'Cloudflare'} · ${scope.service.toUpperCase()}`),
        badge(this.doc, scope.ok ? `${scope.rows.length} found${scope.complete === false ? ' · partial list' : ''}` : 'Latest read unavailable', scope.ok ? 'neutral' : 'warn'));
      details.append(summary, this.node('p', 'infra-muted', scope.ok ? formattedDate(scope.checked_at)
        : `${scope.error || 'Read unavailable'} · last observation: ${formattedDate(observed?.checked_at)}${observed?.complete === false ? ' · partial list' : ''}`));
      if (this.group !== 'all') details.append(this.node('p', 'infra-muted', 'Account inventory · project ownership is not inferred from names.'));
      for (const row of rows) {
        const line = this.node('div', 'infra-inventory-row'); line.append(this.node('strong', '', row.name));
        if (row.state || row.deployment_state) line.append(badge(this.doc, row.state || row.deployment_state));
        if (row.domains?.length) line.append(this.node('small', 'infra-muted', row.domains.join(' · ')));
        details.append(line);
      }
      scopes.append(details);
    }
    this.content.append(this.node('h2', 'infra-section-heading', 'Cloud inventory'), scopes);
    const unavailable = this.data.cloud.scopes.filter(s => !s.ok);
    if (unavailable.length) this.content.append(this.node('p', 'infra-muted', 'Reads unavailable: ' + unavailable.map(s => `${s.service} ${s.region || ''}`).join(' · ')));
    this.content.append(this.button('All observation scopes', () => this.showScope(), 'infra-text-button'));
  }
  openDrawer(title) {
    this.drawer.hidden = false; this.body.classList.add('has-detail');
    this.drawer.replaceChildren();
    const header = this.node('div', 'infra-detail-header');
    header.append(this.node('h2', '', title), this.button('×', () => this.closeDetail(), 'infra-close'));
    header.lastChild.setAttribute('aria-label', 'Close details'); this.drawer.append(header); this.drawer.scrollTop = 0;
    this.positionDrawer();
  }
  positionDrawer() {
    if (this.drawer.hidden) return;
    const win = this.doc.defaultView, bounds = this.body.getBoundingClientRect();
    this.drawer.style.setProperty('--infra-detail-top', Math.max(16, Math.min(bounds.top, win.innerHeight - 360)) + 'px');
    this.drawer.style.setProperty('--infra-detail-right', Math.max(12, this.doc.documentElement.clientWidth - bounds.right) + 'px');
  }
  closeDetail(focus = true) {
    const id = this.selected; this.selected = null; this.drawer.hidden = true; this.body.classList.remove('has-detail');
    for (const n of this.content.querySelectorAll('.infra-node.selected')) n.classList.remove('selected');
    for (const n of this.content.querySelectorAll('.infra-edge.selected')) n.classList.remove('selected');
    this.highlight(null);
    if (focus) [...this.content.querySelectorAll('[data-id]')].find(n => n.dataset.id === id)?.focus();
  }
  fact(label, value) {
    const row = this.node('div', 'infra-fact'); row.append(this.node('dt', '', label), this.node('dd', '', String(value))); return row;
  }
  showDetail(id, focus = true) {
    const item = this.data.nodes.find(n => n.id === id) || overviewGraph(this.data).nodes.find(n => n.id === id);
    if (!item) { this.closeDetail(false); return; }
    this.selected = id; this.openDrawer(item.label);
    const [status, tone] = this.status(item);
    this.drawer.append(badge(this.doc, PROVIDERS[item.provider]), badge(this.doc, status, tone), this.node('p', 'infra-muted', item.summary));
    const health = this.scenario === 'normal' ? machineHealth(item) : null;
    if (health) this.drawer.append(badge(this.doc, ...health));
    if (item.health?.checked_at) {
      this.drawer.append(this.node('p', 'infra-muted', 'Server checks · ' + formattedDate(item.health.checked_at)));
      const facts = this.node('dl', 'infra-facts');
      for (const [key, label] of [['system', 'System'], ['instance', 'Instance'], ['ebs', 'Attached disks']])
        if (item.health[key]) facts.append(this.fact(label, item.health[key]));
      if (facts.children.length) this.drawer.append(facts);
    }
    if (item.aggregate) {
      if (item.group) this.drawer.append(this.button('Explore project →', () => {
        this.group = item.group; this.project.value = item.group; this.closeDetail(false); this.query = ''; this.search.value = ''; this.renderContent();
      }, 'infra-primary'));
      const heading = this.node('h3', '', item.kind === 'project' ? 'Products' : 'Included resources'); this.drawer.append(heading);
      for (const childId of item.children) {
        const child = this.data.nodes.find(n => n.id === childId);
        this.drawer.append(this.button(child.label + ' →', () => this.showDetail(childId), 'infra-related'));
      }
    }
    const impact = nodeImpact(item, this.state);
    if (this.scenario !== 'normal' && impact.causes.length) {
      this.drawer.append(this.node('h3', '', 'Scenario impact'));
      for (const cause of impact.causes) this.drawer.append(this.node('p', 'infra-impact-line', cause));
    }
    if (item.facts?.length) {
      const facts = this.node('dl', 'infra-facts');
      for (const entry of item.facts) facts.append(this.fact(entry.label, entry.value));
      this.drawer.append(facts);
    }
    if (item.observed_at) this.drawer.append(this.node('p', 'infra-muted', 'Observed ' + formattedDate(item.observed_at)));
    const own = new Set(item.children || [id]), dependencies = this.data.edges.filter(e => own.has(e.source)), consumers = this.data.edges.filter(e => own.has(e.target));
    for (const [title, edges, key] of [['Uses', dependencies, 'target'], ['Used by', consumers, 'source']]) {
      if (!edges.length) continue; this.drawer.append(this.node('h3', '', title)); const seen = new Set();
      for (const e of edges) {
        const target = this.data.nodes.find(n => n.id === e[key]);
        if (!target || seen.has(e[key])) continue; seen.add(e[key]);
        const b = this.button('', () => this.showDetail(target.id), 'infra-related');
        b.append(this.node('strong', '', target.label), this.node('small', '', e.label + (e.planned ? ' · planned' : e.effect === 'backup' ? ' · copy' : ''))); this.drawer.append(b);
      }
    }
    for (const backup of this.data.backups.filter(b => own.has(b.id))) {
      const card = this.node('section', 'infra-backup-detail'); card.append(this.node('h3', '', 'Recovery · ' + backup.label), badge(this.doc, backup.copy, recoveryTone(backup)));
      const facts = this.node('dl', 'infra-facts');
      for (const [label, value] of [['Destination', backup.bucket || 'Local files only'], ['Restore', backup.restore], ['Full failover', backup.failover], ['Schedule', backup.cadence], ['Snapshot', formattedDate(backup.captured_at)]]) facts.append(this.fact(label, value));
      card.append(facts, this.node('h4', '', 'Covered'));
      if (backup.bucket && backup.presence === 'unknown') card.append(this.node('p', 'infra-warn', 'Latest destination read unavailable. Last known: ' + backup.last_presence + '.'));
      for (const covered of backup.coverage) card.append(this.node('p', 'infra-coverage', '✓ ' + covered));
      card.append(this.node('h4', '', 'Remaining'));
      for (const gap of backup.gaps) card.append(this.node('p', 'infra-gap', '· ' + gap));
      if (backup.source_changed) card.append(this.node('p', 'infra-warn', 'Source changed after review. Recheck before relying on this receipt.'));
      this.drawer.append(card);
    }
    this.evidence(item.sources || []);
    for (const n of this.content.querySelectorAll('.infra-node')) n.classList.toggle('selected', n.dataset.id === id);
    this.highlight(id);
    if (focus) this.drawer.querySelector('button')?.focus({ preventScroll: true });
  }
  evidence(ids) {
    if (!ids.length) return;
    const details = this.node('details', 'infra-evidence'); details.append(this.node('summary', '', `Evidence · ${ids.length}`));
    for (const id of ids) {
      const source = this.data.sources[id]; if (!source) continue;
      const card = this.node('article'); card.append(this.node('strong', '', source.title), this.node('small', 'infra-muted', source.reported_at),
        this.node('p', '', source.summary), this.node('small', 'infra-muted', source.file));
      if (source.changed || !source.available) card.append(badge(this.doc, source.changed ? 'Source updated since review' : 'Source unavailable', 'warn'));
      details.append(card);
    }
    this.drawer.append(details);
  }
  showScope() {
    if (!this.data) return;
    this.selected = null; this.openDrawer('Evidence & scope');
    for (const [label, value] of [
      ['Live local view', 'Current Codex project membership; refresh view to update.'],
      ['Cloud view', 'Timestamped read-only AWS and Cloudflare metadata. Check cloud to update.'],
      ['Relationships', 'Reviewed source/configuration and saved recovery receipts; not a live application health test.'],
      ['Backup coverage', 'Named snapshots only. No automatic claim for later writes or a complete disk.'],
      ['Not covered', 'Other AWS accounts, every managed service, third-party billing and end-to-end failover.'],
      ['Changes', 'This page does not stop servers, copy data, resume backups or change DNS.'],
    ]) this.drawer.append(this.fact(label, value));
    this.drawer.append(this.node('h3', '', 'Provider guidance'));
    for (const [label, href] of [
      ['AWS · stopping and starting EC2', 'https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/Stop_Start.html'],
      ['AWS · account suspension', 'https://repost.aws/knowledge-center/reactivate-suspended-account'],
      ['Cloudflare · Workers AI catalogue', 'https://developers.cloudflare.com/workers-ai/models/'],
    ]) {
      const link = this.node('a', 'infra-source-link', label + ' ↗'); link.href = href;
      link.target = '_blank'; link.rel = 'noopener noreferrer'; this.drawer.append(link);
    }
    this.drawer.append(this.node('h3', '', 'Cloud observations'));
    for (const error of this.data.cloud.errors || []) this.drawer.append(this.node('p', 'infra-warn', error.provider + ' · ' + error.error));
    for (const scope of this.data.cloud.scopes) {
      const row = this.node('div', 'infra-observation'); row.append(this.node('strong', '', `${scope.service.toUpperCase()} ${scope.region || ''}`),
        badge(this.doc, scope.ok ? `${scope.rows.length} found${scope.complete === false ? ' · partial' : ''}` : 'Read unavailable', scope.ok ? 'neutral' : 'warn'),
        this.node('small', 'infra-muted', formattedDate(scope.checked_at))); this.drawer.append(row);
    }
    this.evidence(Object.keys(this.data.sources)); this.drawer.querySelector('button').focus({ preventScroll: true });
  }
}
