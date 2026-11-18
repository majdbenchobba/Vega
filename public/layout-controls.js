// Layout only: dashboard.js continues to own views, drafts, context and voice.
const STORAGE_KEY = 'vega.layout.sidebarCollapsed';
const instances = new WeakMap();
const SVG_NS = 'http://www.w3.org/2000/svg';

// 24px current-color outline icons with consistent 1.7px strokes.
const icons = {
  conversation: [['path', { d: 'M5 5h14v11H9l-4 3Z' }]],
  briefing: [
    ['rect', { x: 4, y: 4, width: 16, height: 16, rx: 2 }],
    ['path', { d: 'M8 9h8M8 13h5M8 17h3' }],
  ],
  planning: [
    ['rect', { x: 3, y: 5, width: 18, height: 16, rx: 2 }],
    ['path', { d: 'M8 3v4M16 3v4M3 10h18M7 14h3M14 14h3M7 18h3' }],
  ],
  memory: [
    ['circle', { cx: 6, cy: 6, r: 2 }],
    ['circle', { cx: 18, cy: 8, r: 2 }],
    ['circle', { cx: 9, cy: 18, r: 2 }],
    ['path', { d: 'm7.8 7 8.3 1M7 7.8l1.3 8.3M16.8 9.7l-6.5 6.7' }],
  ],
  settings: [
    ['circle', { cx: 12, cy: 12, r: 3 }],
    ['path', { d: 'M19.4 15a1.7 1.7 0 0 0 .3 1.9l.1.1-2.8 2.8-.1-.1a1.7 1.7 0 0 0-1.9-.3 1.7 1.7 0 0 0-1 1.6v.2h-4v-.2a1.7 1.7 0 0 0-1-1.6 1.7 1.7 0 0 0-1.9.3l-.1.1L4.2 17l.1-.1a1.7 1.7 0 0 0 .3-1.9A1.7 1.7 0 0 0 3 14H3v-4h.2a1.7 1.7 0 0 0 1.6-1 1.7 1.7 0 0 0-.3-1.9L4.2 7 7 4.2l.1.1a1.7 1.7 0 0 0 1.9.3 1.7 1.7 0 0 0 1-1.6V3h4v.2a1.7 1.7 0 0 0 1 1.6 1.7 1.7 0 0 0 1.9-.3l.1-.1L19.8 7l-.1.1a1.7 1.7 0 0 0-.3 1.9 1.7 1.7 0 0 0 1.6 1h.2v4H21a1.7 1.7 0 0 0-1.6 1Z' }],
  ],
  tools: [
    ['path', { d: 'M3 7h7l2 2h9v11H3ZM3 7V4h7l2 3M8 13l-2 2 2 2M16 13l2 2-2 2' }],
  ],
  microphone: [
    ['rect', { x: 9, y: 3, width: 6, height: 11, rx: 3 }],
    ['path', { d: 'M6 11a6 6 0 0 0 12 0M12 17v4M9 21h6' }],
  ],
  pause: [['path', { d: 'M9 6v12M15 6v12' }]],
  stop: [['rect', { x: 7, y: 7, width: 10, height: 10, rx: 1.5 }]],
  compact: [
    ['path', { d: 'M7 5H5v4M17 5h2v4M7 19H5v-4M17 19h2v-4' }],
    ['rect', { x: 8, y: 8, width: 8, height: 8, rx: 1.5 }],
  ],
  sidebar: [
    ['rect', { x: 3, y: 4, width: 18, height: 16, rx: 2 }],
    ['path', { d: 'M9 4v16' }],
    ['path', { class: 'layout-toggle-arrow', d: 'm16 9-3 3 3 3' }],
  ],
};

function createIcon(document, name) {
  const svg = document.createElementNS(SVG_NS, 'svg');
  const attributes = {
    class: 'layout-control-icon', viewBox: '0 0 24 24', fill: 'none',
    stroke: 'currentColor', 'stroke-width': '1.7', 'stroke-linecap': 'round',
    'stroke-linejoin': 'round', 'aria-hidden': 'true', focusable: 'false',
  };
  for (const [name, value] of Object.entries(attributes)) svg.setAttribute(name, value);
  for (const [tag, attributes] of icons[name] || icons.conversation) {
    const shape = document.createElementNS(SVG_NS, tag);
    for (const [name, value] of Object.entries(attributes)) shape.setAttribute(name, String(value));
    svg.append(shape);
  }
  return svg;
}

function decorateButton(button, icon) {
  const label = button.ownerDocument.createElement('span');
  label.className = 'layout-control-label';
  const name = button.getAttribute('aria-label') || button.textContent.trim();
  // Move only the button's existing contents; the button and its listeners stay.
  label.append(...button.childNodes);
  button.append(createIcon(button.ownerDocument, icon), label);
  button.classList.add('layout-icon-control');
  if (!button.hasAttribute('aria-label')) button.setAttribute('aria-label', name);
  if (!button.hasAttribute('title')) button.setAttribute('title', name);
  return name;
}

function initialCollapsed(window) {
  try {
    const saved = window.localStorage.getItem(STORAGE_KEY);
    if (saved === 'true' || saved === 'false') return saved === 'true';
  } catch { /* Storage can be unavailable; the control still works in memory. */ }
  return Boolean(window.matchMedia?.('(max-width: 720px)').matches);
}

/**
 * Call after the dashboard DOM exists. Repeated calls return the same controller.
 * Returns null when the expected shell is absent; importing performs no DOM work.
 * The optional controller exposes { collapsed, setCollapsed(boolean) }.
 */
export function initializeLayoutControls() {
  if (typeof document === 'undefined') return null;
  const workspace = document.querySelector('.workspace');
  if (!workspace) return null;
  if (instances.has(workspace)) return instances.get(workspace);

  const sidebar = workspace.querySelector('.sidebar');
  const topbar = workspace.querySelector('.topbar');
  const title = workspace.querySelector('#view-title');
  const scope = workspace.querySelector('#project-scope');
  const window = document.defaultView;
  if (!sidebar || !topbar || !title || !scope || !window) return null;

  const viewNames = new Map();
  for (const button of sidebar.querySelectorAll('button[data-view]')) {
    viewNames.set(button.dataset.view, decorateButton(button, button.dataset.view));
    button.classList.add('layout-view-button');
  }
  for (const panel of workspace.querySelectorAll('[data-panel]')) {
    const label = viewNames.get(panel.dataset.panel);
    if (label && !panel.hasAttribute('aria-label') && !panel.hasAttribute('aria-labelledby')) {
      panel.setAttribute('aria-label', label);
    }
  }

  // Keep the live heading in the accessibility tree and under dashboard control.
  const headingGroup = title.parentElement;
  if (headingGroup !== topbar && headingGroup.parentElement === topbar && !headingGroup.contains(scope)) {
    headingGroup.classList.add('layout-sr-only');
  } else {
    title.classList.add('layout-sr-only');
  }
  topbar.querySelector('.eyebrow')?.setAttribute('aria-hidden', 'true');
  title.setAttribute('aria-live', 'polite');
  title.setAttribute('aria-atomic', 'true');
  if (!scope.hasAttribute('aria-label')) scope.setAttribute('aria-label', 'Current project context');
  if (!scope.hasAttribute('title')) scope.setAttribute('title', 'Choose project context');

  for (const [id, icon] of [['voice-start', 'microphone'], ['voice-interrupt', 'stop'], ['voice-pause', 'pause'], ['compact', 'compact']]) {
    const button = sidebar.querySelector(`#${id}`);
    if (button) decorateButton(button, icon);
  }
  sidebar.querySelector('#vega-title')?.classList.add('layout-sr-only');

  const tools = document.createElement('div');
  tools.className = 'layout-sidebar-tools';
  const toggle = document.createElement('button');
  toggle.type = 'button';
  toggle.className = 'layout-sidebar-toggle layout-icon-control';
  if (!sidebar.id) sidebar.id = 'vega-layout-sidebar';
  toggle.setAttribute('aria-controls', sidebar.id);
  const toggleLabel = document.createElement('span');
  toggleLabel.className = 'layout-control-label';
  const toggleIcon = createIcon(document, 'sidebar');
  const toggleArrow = toggleIcon.querySelector('.layout-toggle-arrow');
  toggle.append(toggleIcon, toggleLabel);
  tools.append(toggle);
  sidebar.prepend(tools);

  let collapsed = initialCollapsed(window);
  function applyCollapsed() {
    workspace.dataset.sidebarCollapsed = String(collapsed);
    const label = collapsed ? 'Expand sidebar' : 'Collapse sidebar';
    toggleLabel.textContent = label;
    toggle.setAttribute('aria-label', label);
    toggle.setAttribute('title', label);
    toggle.setAttribute('aria-expanded', String(!collapsed));
    toggleArrow.setAttribute('d', collapsed ? 'm13 9 3 3-3 3' : 'm16 9-3 3 3 3');
  }
  function setCollapsed(value) {
    collapsed = Boolean(value);
    applyCollapsed();
    try {
      window.localStorage.setItem(STORAGE_KEY, String(collapsed));
    } catch { /* No preference or network fallback: this is visual state only. */ }
  }
  toggle.addEventListener('click', () => setCollapsed(!collapsed));
  applyCollapsed();
  workspace.classList.add('layout-refined');

  const presenceState = sidebar.querySelector('#presence-state');
  const connection = sidebar.querySelector('#connection');
  const compact = sidebar.querySelector('#compact');
  function syncHints() {
    if (presenceState) presenceState.parentElement.title = presenceState.textContent.trim();
    if (connection) connection.parentElement.title = connection.textContent.trim();
    if (compact) compact.setAttribute('aria-pressed', String(document.body.classList.contains('compact-mode')));
  }
  syncHints();
  // Observe only status text and the existing compact class, never chat content.
  // Native title hints remain visible outside the scrolling rail's clip area.
  const hints = new window.MutationObserver(syncHints);
  for (const status of [presenceState, connection]) {
    if (status) hints.observe(status, { childList: true, characterData: true, subtree: true });
  }
  if (compact) hints.observe(document.body, { attributes: true, attributeFilter: ['class'] });

  const controller = Object.freeze({
    get collapsed() { return collapsed; },
    setCollapsed,
  });
  instances.set(workspace, controller);
  return controller;
}
