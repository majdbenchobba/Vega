import { init, initWorkspace } from '/mini/app.js';

async function api(path, { method = 'GET', body } = {}) {
  if (!path.startsWith('/mini/api/')) throw new Error('Unsupported project request.');
  const target = path.startsWith('/mini/api/team')
    ? path.replace('/mini/api/team', '/api/team')
    : path.replace('/mini/api/', '/api/project-control/');
  const headers = { 'Content-Type': 'application/json' };
  if (method !== 'GET') {
    const session = await fetch('/api/session', { cache: 'no-store' });
    if (!session.ok) throw new Error('The local Vega session is unavailable.');
    headers['X-Vega-CSRF'] = (await session.json()).csrf;
  }
  const response = await fetch(target, {
    method, headers, cache: 'no-store',
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    signal: AbortSignal.timeout(path === '/mini/api/chat-attachments' ? 60000 : 20000),
  });
  const result = await response.json();
  if (!response.ok) {
    const error = new Error(result.error || 'The project request could not be completed.');
    error.status = response.status;
    throw error;
  }
  return result;
}

initWorkspace({ root: document.getElementById('miniapp'), api, localMode: true,
  mountReports: root => init({ root, api, localMode: true, pollIntervalMs: 0 }) });
const infrastructureLink = document.createElement('a');
infrastructureLink.href = '/infrastructure';
infrastructureLink.textContent = 'Infrastructure ↗';
infrastructureLink.target = '_blank'; infrastructureLink.rel = 'noopener';
infrastructureLink.style.cssText = 'padding:8px;border:1px solid #454451;border-radius:10px;color:#dedde9;font:11px system-ui;text-decoration:none';
document.querySelector('.workspace-header').append(infrastructureLink);
