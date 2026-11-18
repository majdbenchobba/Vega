// Keep module-loading failures visible even when the dashboard cannot start.
const startupError = document.getElementById('error');
let startupWarning = '';
let queuedView = null;
const rememberNavigation = event => {
  const control = event.target.closest?.('[data-view]');
  if (!control) return;
  queuedView = document.querySelector('.floating-shell') ? null : control.dataset.view;
};
document.addEventListener('click', rememberNavigation, true);
function showStartupError(message) {
  startupWarning = message;
  startupError.textContent = message;
  startupError.hidden = false;
}
const startupTimer = setTimeout(() => {
  if (!document.querySelector('.floating-shell')) {
    showStartupError('Vega is taking longer to open. Reload this page if it does not finish.');
  }
}, 20000);
function releaseNavigation() {
  clearTimeout(startupTimer);
  document.removeEventListener('click', rememberNavigation, true);
  document.removeEventListener('vega:ready', releaseNavigation);
  const next = queuedView; queuedView = null;
  if (next) {
    const control = [...document.querySelectorAll('[data-view]')].find(item => item.dataset.view === next);
    control?.click();
  }
}
document.addEventListener('vega:ready', releaseNavigation);
import('./dashboard.bundle.js?worktree=20261001').catch(error => {
  // A failed fetch can remain cached during a service update. Retry only a
  // loading failure, before the dashboard has attached any controls.
  if (!(error instanceof TypeError)
      || !/fetch dynamically imported module|importing a module script failed|load module script/i.test(error.message)
      || document.querySelector('.floating-shell')) throw error;
  return import(`./dashboard.bundle.js?worktree=20261001&retry=${Date.now()}`);
}).then(() => {
  releaseNavigation();
  if (startupError.textContent === startupWarning) {
    startupError.textContent = '';
    startupError.hidden = true;
  }
}).catch(error => {
  clearTimeout(startupTimer);
  document.removeEventListener('click', rememberNavigation, true);
  document.removeEventListener('vega:ready', releaseNavigation);
  showStartupError('Vega could not open. Reload this page to try again.');
  document.getElementById('presence-state').textContent = 'Unable to open';
  console.error('Vega dashboard startup failed:', error);
});
