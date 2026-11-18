// Dedicated work page never imports voice or opens the microphone.
import { WorkOverview } from './work-overview.js';
import { N8nPanel } from './n8n-panel.js';
import { initWorkActivity, localTeamApi } from './team-panel.js';
import { CopilotSessionsPanel } from './copilot-sessions.js';
import { NativeIdeSessionsPanel } from './native-ide-sessions.js';
async function api(path, body) {
  const headers = { Accept: 'application/json' };
  if (body !== undefined) {
    const session = await fetch('/api/session', { cache: 'no-store' });
    if (!session.ok) throw new Error('The private Vega session is unavailable.');
    headers['X-Vega-CSRF'] = (await session.json()).csrf; headers['Content-Type'] = 'application/json';
  }
  const response = await fetch(path, { method: body === undefined ? 'GET' : 'POST', headers, cache: 'no-store',
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    signal: AbortSignal.timeout(body === undefined ? 30000 : path.startsWith('/api/copilot/sessions') ? 660000 : 180000) });
  const data = await response.json();
  if (!response.ok) { const error = new Error(data.error || 'The local request failed.'); error.status = response.status; throw error; }
  return data;
}
const activity = initWorkActivity({ root: document.getElementById('work-activity'), api: localTeamApi() });
const n8n = new N8nPanel(document.getElementById('automations'), { get: api, post: api, onRun: id => activity.openRun(id) });
const map = new WorkOverview(document.getElementById('work-map'), {
  get: api, post: api, onRun: id => activity.openRun(id), onRequests: () => activity.open({ tab: 'requests' }),
  onAutomations: () => showAutomations(),
});
const copilot = new CopilotSessionsPanel(document.getElementById('copilot-sessions'), { get: api, post: api });
const nativeIde = new NativeIdeSessionsPanel(document.getElementById('native-ide-sessions'), { get: api, post: api });
function showAutomations() {
  copilot.hide();
  map.hide(); document.getElementById('work-map').hidden = true;
  document.getElementById('automations').hidden = false; document.getElementById('back-to-work').hidden = false;
  void n8n.show();
}
document.getElementById('back-to-work').onclick = () => {
  n8n.hide(); document.getElementById('automations').hidden = true; document.getElementById('back-to-work').hidden = true;
  document.getElementById('work-map').hidden = false; void map.show();
};
function showAgentSessions(panel) {
  if (panel !== copilot) copilot.hide();
  map.hide(); document.getElementById('work-map').hidden = true;
  document.getElementById('automations').hidden = true;
  document.getElementById('copilot-sessions').hidden = panel !== copilot;
  document.getElementById('native-ide-sessions').hidden = panel !== nativeIde;
  document.getElementById('show-agent-sessions').setAttribute('aria-pressed', String(panel === copilot));
  document.getElementById('show-native-agent-sessions').setAttribute('aria-pressed', String(panel === nativeIde));
  document.getElementById('back-to-agents').hidden = false;
  void panel.show();
}
document.getElementById('show-agent-sessions').onclick = () => showAgentSessions(copilot);
document.getElementById('show-native-agent-sessions').onclick = () => showAgentSessions(nativeIde);
document.getElementById('back-to-agents').onclick = () => {
  copilot.hide();
  document.getElementById('copilot-sessions').hidden = true;
  document.getElementById('native-ide-sessions').hidden = true;
  document.getElementById('show-agent-sessions').setAttribute('aria-pressed', 'false');
  document.getElementById('show-native-agent-sessions').setAttribute('aria-pressed', 'false');
  document.getElementById('back-to-agents').hidden = true;
  document.getElementById('work-map').hidden = false; void map.show();
};
void map.show();
window.addEventListener('pagehide', () => { map.destroy(); n8n.destroy(); activity.destroy(); copilot.destroy(); });
