// Pure dependency calculations. Outage controls never call a provider API.
export function impacts(data, scenario = 'normal') {
  const state = new Map(data.nodes.map(node => [node.id, { severity: 0, causes: [], backup: false }]));
  for (const node of data.nodes) {
    if (node.planned) continue;
    const hit = scenario === 'aws' && node.provider === 'aws'
      || scenario === 'cloudflare' && node.provider === 'cloudflare'
      || scenario === 'laptop' && node.id === 'laptop'
      || scenario.startsWith('server:') && node.id === scenario.slice(7);
    if (hit) state.set(node.id, { severity: 2, causes: ['Unavailable in this scenario'], backup: false });
  }
  for (let pass = 0; pass < data.nodes.length; pass++) {
    let changed = false;
    for (const edge of data.edges) {
      if (edge.planned) continue;
      const source = state.get(edge.source), target = state.get(edge.target);
      if (!source || !target || !target.severity) continue;
      if (edge.effect === 'backup') { source.backup = true; continue; }
      const severity = edge.effect === 'stop' && target.severity === 2 ? 2 : 1;
      if (severity > source.severity) { source.severity = severity; changed = true; }
      if (!source.causes.includes(edge.label)) source.causes.push(edge.label);
    }
    if (!changed) break;
  }
  return state;
}

export function scopedGraph(data, group = 'all') {
  if (group === 'all') return { nodes: data.nodes, edges: data.edges };
  const ids = new Set(data.nodes.filter(n => n.group === group).map(n => n.id));
  for (let pass = 0; pass < data.nodes.length; pass++) {
    let changed = false;
    for (const e of data.edges) if (ids.has(e.source) && !ids.has(e.target)) { ids.add(e.target); changed = true; }
    if (!changed) break;
  }
  return { nodes: data.nodes.filter(n => ids.has(n.id)), edges: data.edges.filter(e => ids.has(e.source) && ids.has(e.target)) };
}

export function overviewGraph(data, group = 'all') {
  const graph = scopedGraph(data, group);
  if (group !== 'all') return graph;
  const aliases = new Map(), projects = [];
  for (const g of data.groups) {
    const children = graph.nodes.filter(n => n.kind === 'project' && n.group === g.id);
    if (!children.length) continue;
    children.forEach(n => aliases.set(n.id, 'group:' + g.id));
    projects.push({ id: 'group:' + g.id, label: g.label, kind: 'project', provider: 'project', group: g.id,
      project_id: g.project_id, alias: g.id, children: children.map(n => n.id),
      summary: `${children.length} ${children.length === 1 ? 'product' : 'products'} · ${g.chats ?? '?'} ${g.chats === 1 ? 'chat' : 'chats'}`,
      facts: [], sources: [...new Set(children.flatMap(n => n.sources))], aggregate: true });
  }
  const clusters = [
    ['aws-services', 'AWS AI & services', 'service', 'aws', graph.nodes.filter(n => n.kind === 'service' && n.provider === 'aws')],
    ['cf-network', 'Cloudflare network', 'service', 'cloudflare', graph.nodes.filter(n => ['cf-edge', 'cf-tunnel'].includes(n.id))],
  ];
  for (const [id, label, kind, provider, children] of clusters) {
    if (!children.length) continue;
    children.forEach(n => aliases.set(n.id, id));
    projects.push({ id, label, kind, provider, children: children.map(n => n.id), aggregate: true,
      summary: `${children.length} ${kind === 'runtime' ? 'machines' : 'services'} · explore details`,
      facts: [], sources: [...new Set(children.flatMap(n => n.sources))] });
  }
  const edges = new Map();
  for (const e of graph.edges) {
    const source = aliases.get(e.source) ?? e.source, target = aliases.get(e.target) ?? e.target;
    const key = `${source}|${target}|${e.effect}|${e.planned}`;
    if (source !== target) edges.set(key, { ...e, source, target });
  }
  return { nodes: [...projects, ...graph.nodes.filter(n => !aliases.has(n.id))], edges: [...edges.values()] };
}

export function observationOld(item, now = Date.now()) {
  const at = typeof item.observed_at === 'number' ? item.observed_at * 1000 : Date.parse(item.observed_at);
  return !!item.observed_stale || !Number.isFinite(at) || now - at > 15 * 60 * 1000;
}

export function observedStatus(item, now = Date.now()) {
  if (!item.observed_state) return null;
  const state = item.observed_state, old = observationOld(item, now);
  const label = state === 'present' ? 'Bucket present' : state.charAt(0).toUpperCase() + state.slice(1);
  const tone = old ? 'warn' : ['running', 'connected', 'present'].includes(state) ? 'good'
    : ['error', 'failed', 'impaired', 'absent'].includes(state) ? 'bad'
      : ['stopped', 'stopping', 'pending', 'unknown'].includes(state) ? 'warn' : 'neutral';
  return [old && state !== 'unknown' ? 'Last known: ' + label.toLowerCase() : label, tone];
}

export function machineHealth(item, now = Date.now()) {
  if (!item.machine || !item.id.startsWith('ec2:')) return null;
  const health = item.health;
  if (!health?.checked_at) return ['Checks not read', 'neutral'];
  const old = observationOld({ observed_at: health.checked_at, observed_stale: health.stale }, now);
  const checks = [health.system, health.instance, health.ebs].filter(Boolean);
  if (checks.includes('impaired')) return [old ? 'Last check failed' : 'Check failed', old ? 'warn' : 'bad'];
  if (old) return ['Checks outdated', 'warn'];
  if (health.available === false) return ['Checks unavailable', 'warn'];
  if (checks.includes('initializing')) return ['Checks starting', 'neutral'];
  if (item.observed_state !== 'running') return null;
  if (health.system === 'ok' && health.instance === 'ok' && (!health.ebs || health.ebs === 'ok')) return ['Checks passed', 'good'];
  return ['Checks unknown', 'neutral'];
}

// Only actual hosting paths count. Backups and planned hosts are not running
// replicas, and an EC2 state is never promoted to an application-health claim.
export function hostingStatus(item, data, now = Date.now()) {
  if (item.kind !== 'project') return null;
  const ids = new Set(item.children || [item.id]);
  for (let pass = 0; pass < data.nodes.length; pass++) {
    let changed = false;
    for (const e of data.edges) if (ids.has(e.source) && e.effect === 'stop' && !e.planned && !ids.has(e.target)) {
      ids.add(e.target); changed = true;
    }
    if (!changed) break;
  }
  const hosts = data.nodes.filter(n => ids.has(n.id) && n.kind === 'runtime' && !n.planned && (n.machine || n.observed_state));
  if (!hosts.length) return null;
  const old = hosts.some(n => observationOld(n, now));
  const failed = hosts.filter(n => machineHealth(n, now)?.[1] === 'bad').length;
  const stopped = hosts.filter(n => ['stopped', 'stopping', 'terminated', 'shutting-down'].includes(n.observed_state)).length;
  const running = hosts.filter(n => ['running', 'connected'].includes(n.observed_state)).length;
  const parts = [];
  if (running) parts.push(`${running} ${running === 1 ? 'host' : 'hosts'} running`);
  if (stopped) parts.push(`${stopped} stopped`);
  if (failed) parts.push(`${failed} check failed`);
  if (hosts.length > running + stopped) parts.push(`${hosts.length - running - stopped} unknown`);
  return [(old ? 'Last known · ' : '') + parts.join(' · '), old || stopped ? 'warn' : failed ? 'bad' : 'neutral'];
}

export function nodeImpact(node, state) {
  if (!node.aggregate) return state.get(node.id) ?? { severity: 0, causes: [] };
  const children = node.children.map(id => state.get(id)).filter(Boolean);
  // An umbrella is only wholly unavailable when all its mapped products are.
  return { severity: children.every(v => v.severity === 2) ? 2 : children.some(v => v.severity) ? 1 : 0,
    causes: [...new Set(children.flatMap(v => v.causes))], backup: children.some(v => v.backup) };
}

export function recoveryTone(item) {
  if (item.presence === 'absent') return 'bad';
  if (item.source_changed || item.presence === 'unknown' || !item.copy.startsWith('Verified')) return 'warn';
  return 'good';
}

export function formattedDate(value) {
  const date = new Date(typeof value === 'number' ? value * 1000 : value);
  if (typeof value === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(value) && Number.isFinite(date.valueOf()))
    return date.toLocaleDateString('en-GB', { timeZone: 'UTC', day: '2-digit', month: 'short', year: 'numeric' });
  return value && Number.isFinite(date.valueOf())
    ? date.toLocaleString('en-GB', { timeZone: 'Africa/Tunis', day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit' }) + ' Tunis'
    : 'Not verified';
}
