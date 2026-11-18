import { InfrastructurePanel } from './infrastructure.js';
async function api(path, body) {
  const headers = { Accept: 'application/json' };
  if (body !== undefined) {
    const session = await fetch('/api/session', { cache: 'no-store' });
    if (!session.ok) throw new Error('Open Vega on this laptop.');
    headers['X-Vega-CSRF'] = (await session.json()).csrf;
    headers['Content-Type'] = 'application/json';
  }
  const response = await fetch(path, { method: body === undefined ? 'GET' : 'POST', headers, cache: 'no-store',
    ...(body === undefined ? {} : { body: JSON.stringify(body) }), signal: AbortSignal.timeout(30000) });
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || 'The inventory is unavailable.');
  return result;
}
const panel = new InfrastructurePanel(document.getElementById('infrastructure-workspace'), { get: api, post: api });
void panel.show();
window.addEventListener('pagehide', () => panel.destroy());
