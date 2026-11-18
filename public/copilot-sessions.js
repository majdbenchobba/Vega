const DEFAULT_MODEL = 'auto/best-free';

export class CopilotSessionsPanel {
  constructor(root, { get, post }) {
    this.root = root;
    this.get = get;
    this.post = post;
    this.projects = [];
    this.selectedProject = '';
    this.sessionId = '';
    this.destroyed = false;
    this.approvalTimer = null;
    this.approvalsLoading = false;
    this.approvalDecisions = new Set();
    this.build();
  }

  build() {
    this.root.innerHTML = `
      <header class="copilot-header">
        <div><span class="eyebrow">VEGA-CONTROLLED</span><h2>Copilot agent sessions</h2></div>
        <button type="button" data-action="refresh">Refresh</button>
      </header>
      <p class="copilot-explainer">Create and resume persistent Vega-owned sessions using the Copilot SDK through the local OmniRoute gateway. These are separate from VS Code’s existing Agents-tab conversations.</p>
      <section class="copilot-approval-queue" aria-labelledby="copilot-approval-title">
        <header><h3 id="copilot-approval-title">Pending file approvals</h3><span data-field="approval-count"></span></header>
        <p class="copilot-note">Review the complete change before approving a one-time write. Requests expire after 10 minutes.</p>
        <div data-field="approvals" class="copilot-approval-list" aria-live="polite"></div>
        <p data-field="approval-status" class="copilot-approval-status" role="status"></p>
      </section>
      <div class="copilot-layout">
        <aside class="copilot-sidebar">
          <label>Project<select data-field="project"></select></label>
          <label>Local project folder<input data-field="root" type="text" autocomplete="off" spellcheck="false" placeholder="C:\\path\\to\\project"></label>
          <button type="button" data-action="register">Register / update folder</button>
          <p class="copilot-note">Only the folder you register is available to the agent. Project folders are not discovered automatically.</p>
          <a data-action="open-vscode" hidden>Open this folder in VS Code</a>
          <hr>
          <div class="copilot-session-heading"><strong>Saved sessions</strong><button type="button" data-action="create">New</button></div>
          <div data-field="sessions" class="copilot-session-list"></div>
        </aside>
        <section class="copilot-chat">
          <form data-field="new-session" class="copilot-new-session">
            <label>Session title<input name="title" maxlength="120" value="New agent session" required></label>
            <label>OmniRoute model ID<input name="model" maxlength="200" value="${DEFAULT_MODEL}" required></label>
            <label class="copilot-first-prompt">First prompt<textarea name="prompt" rows="3" maxlength="24000" placeholder="What should this agent work on?" required></textarea></label>
            <button class="primary" type="submit">Create session and send</button>
          </form>
          <p class="copilot-note">Model availability, tool support, and any charges depend on OmniRoute, its configured provider, and your account. Vega does not switch routes automatically.</p>
          <div data-field="messages" class="copilot-messages" role="log" aria-live="polite"></div>
          <form data-field="prompt" class="copilot-prompt" hidden>
            <textarea name="text" rows="3" maxlength="24000" placeholder="Send a prompt to this agent session…" required></textarea>
            <button class="primary" type="submit">Send prompt</button>
          </form>
          <p data-field="status" role="status" class="copilot-status"></p>
        </section>
      </div>
      <p class="copilot-policy">Vega may read ordinary project files. Edits to existing or new in-folder text files wait for your review. Deletions, protected files, paths outside the folder, shell, network/MCP, deployment, and system actions stay blocked.</p>
    `;
    this.projectSelect = this.root.querySelector('[data-field="project"]');
    this.root.querySelector('[data-action="refresh"]').addEventListener('click', () => void this.show());
    this.root.querySelector('[data-action="register"]').addEventListener('click', () => void this.registerWorkspace());
    this.root.querySelector('[data-action="create"]').addEventListener('click', () => {
      this.root.querySelector('[data-field="new-session"]').hidden = false;
      this.root.querySelector('[data-field="prompt"]').hidden = true;
      this.root.querySelector('[data-field="messages"]').replaceChildren();
      this.sessionId = '';
    });
    this.projectSelect.addEventListener('change', () => {
      this.selectedProject = this.projectSelect.value;
      this.sessionId = '';
      void this.renderProject();
    });
    this.root.querySelector('[data-field="new-session"]').addEventListener('submit', event => {
      event.preventDefault();
      void this.createSession(new FormData(event.currentTarget));
    });
    this.root.querySelector('[data-field="prompt"]').addEventListener('submit', event => {
      event.preventDefault();
      void this.sendPrompt(new FormData(event.currentTarget));
    });
  }

  status(message, error = false) {
    const node = this.root.querySelector('[data-field="status"]');
    node.textContent = message;
    node.dataset.error = error ? 'true' : 'false';
  }

  async show() {
    if (this.destroyed) return;
    this.visible = true;
    this.startApprovalPolling();
    this.status('Loading registered projects…');
    try {
      const data = await this.get('/api/copilot/projects');
      this.projects = (data.projects || []).filter(project => project.alias !== 'coordinator');
      this.projectSelect.replaceChildren(...this.projects.map(project => {
        const option = document.createElement('option');
        option.value = project.alias;
        option.textContent = project.label;
        return option;
      }));
      if (this.selectedProject && this.projects.some(item => item.alias === this.selectedProject)) {
        this.projectSelect.value = this.selectedProject;
      } else {
        this.selectedProject = this.projectSelect.value;
      }
      await this.renderProject();
      if (this.currentProject()?.workspace) this.status('');
    } catch (error) {
      this.status(error.message, true);
    }
  }

  startApprovalPolling() {
    if (this.approvalTimer) return;
    void this.refreshApprovals();
    this.approvalTimer = setInterval(() => void this.refreshApprovals(), 2000);
  }

  async refreshApprovals() {
    if (this.destroyed || !this.visible || this.approvalsLoading) return;
    this.approvalsLoading = true;
    try {
      const data = await this.get('/api/copilot/approvals');
      this.renderApprovals(data.approvals || []);
      this.root.querySelector('[data-field="approval-status"]').textContent = '';
    } catch (error) {
      this.root.querySelector('[data-field="approval-status"]').textContent = error.message;
    } finally {
      this.approvalsLoading = false;
    }
  }

  renderApprovals(approvals) {
    const host = this.root.querySelector('[data-field="approvals"]');
    const count = this.root.querySelector('[data-field="approval-count"]');
    count.textContent = approvals.length ? String(approvals.length) : '';
    if (!approvals.length) {
      const empty = document.createElement('p');
      empty.className = 'copilot-approval-empty';
      empty.textContent = 'No pending file changes.';
      host.replaceChildren(empty);
      return;
    }
    const fragment = document.createDocumentFragment();
    for (const approval of approvals) {
      const item = document.createElement('article');
      item.className = 'copilot-approval-item';
      const heading = document.createElement('h4');
      heading.textContent = `${approval.project} · ${approval.title}`;
      const summary = document.createElement('p');
      summary.textContent = approval.intention;
      const files = document.createElement('ul');
      files.className = 'copilot-approval-files';
      const snapshots = document.createDocumentFragment();
      for (const file of approval.files || []) {
        const row = document.createElement('li');
        const path = document.createElement('code');
        path.textContent = file.path;
        const kind = document.createElement('span');
        kind.textContent = file.before === null ? 'New file' : 'Edit';
        row.append(path, kind);
        files.append(row);
        const snapshot = document.createElement('details');
        const label = document.createElement('summary');
        label.textContent = `Full before/after: ${file.path}`;
        const beforeLabel = document.createElement('strong');
        beforeLabel.textContent = 'Before';
        const before = document.createElement('pre');
        before.className = 'copilot-approval-snapshot';
        before.textContent = file.before === null ? 'File does not exist yet.' : file.before;
        const afterLabel = document.createElement('strong');
        afterLabel.textContent = 'After';
        const after = document.createElement('pre');
        after.className = 'copilot-approval-snapshot';
        after.textContent = file.after;
        snapshot.append(label, beforeLabel, before, afterLabel, after);
        snapshots.append(snapshot);
      }
      const review = document.createElement('details');
      const reviewLabel = document.createElement('summary');
      reviewLabel.textContent = 'Review complete diff';
      const diff = document.createElement('pre');
      diff.className = 'copilot-approval-diff';
      diff.tabIndex = 0;
      diff.textContent = approval.diff;
      review.append(reviewLabel, diff);
      const actions = document.createElement('div');
      actions.className = 'copilot-approval-actions';
      const approve = document.createElement('button');
      approve.type = 'button';
      approve.className = 'primary';
      approve.textContent = 'Approve this write';
      const reject = document.createElement('button');
      reject.type = 'button';
      reject.textContent = 'Reject';
      approve.disabled = this.approvalDecisions.has(approval.id);
      reject.disabled = approve.disabled;
      approve.addEventListener('click', () => void this.decideApproval(approval, 'approve', actions));
      reject.addEventListener('click', () => void this.decideApproval(approval, 'reject', actions));
      actions.append(approve, reject);
      item.append(heading, summary, files, snapshots, review, actions);
      fragment.append(item);
    }
    host.replaceChildren(fragment);
  }

  async decideApproval(approval, action, actions) {
    if (this.approvalDecisions.has(approval.id)) return;
    this.approvalDecisions.add(approval.id);
    for (const button of actions.querySelectorAll('button')) button.disabled = true;
    this.root.querySelector('[data-field="approval-status"]').textContent =
      action === 'approve' ? 'Checking the file and approving this one-time write…' : 'Rejecting the file change…';
    let message = '';
    try {
      const result = await this.post(`/api/copilot/approvals/${encodeURIComponent(approval.id)}/${action}`, {
        expected_revision: approval.revision, request_hash: approval.request_hash,
      });
      const state = result.approval.state;
      message = {
        approved: 'Approved for this session. This does not guarantee the edit completed.',
        rejected: 'File change rejected.',
        stale: 'The project or file changed. No write was approved.',
        expired: 'This approval expired.',
        interrupted: 'The agent session stopped. No write was approved.',
      }[state] || 'Approval decision recorded.';
    } catch (error) {
      message = error.message;
    } finally {
      this.approvalDecisions.delete(approval.id);
      await this.refreshApprovals();
      this.root.querySelector('[data-field="approval-status"]').textContent = message;
    }
  }

  currentProject() {
    return this.projects.find(project => project.alias === this.selectedProject);
  }

  async renderProject() {
    const project = this.currentProject();
    const rootInput = this.root.querySelector('[data-field="root"]');
    const openLink = this.root.querySelector('[data-action="open-vscode"]');
    rootInput.value = project?.workspace?.root || '';
    openLink.hidden = !project?.workspace?.root;
    if (project?.workspace?.root) {
      const uriPath = encodeURI(project.workspace.root.replaceAll('\\', '/'))
        .replaceAll('#', '%23').replaceAll('?', '%3F');
      openLink.href = `vscode://file/${uriPath}`;
    } else {
      openLink.removeAttribute('href');
    }
    this.sessionId = '';
    this.root.querySelector('[data-field="new-session"]').hidden = false;
    this.root.querySelector('[data-field="prompt"]').hidden = true;
    this.root.querySelector('[data-field="messages"]').replaceChildren();
    const host = this.root.querySelector('[data-field="sessions"]');
    host.replaceChildren();
    if (!project) return;
    const data = await this.get(`/api/copilot/sessions?project=${encodeURIComponent(project.alias)}`);
    for (const item of data.sessions || []) {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'copilot-session-item';
      button.dataset.sessionId = item.id;
      button.textContent = `${item.title} · ${item.model}`;
      button.title = item.last_error || item.last_message || item.title;
      button.addEventListener('click', () => void this.openSession(item.id));
      host.append(button);
    }
    if (!project.workspace) this.status('Register this project’s local folder before creating a session.');
  }

  async registerWorkspace() {
    const root = this.root.querySelector('[data-field="root"]').value.trim();
    if (!this.selectedProject || !root) {
      this.status('Choose a project and enter its absolute local folder path.', true);
      return;
    }
    this.status('Registering the selected project folder…');
    try {
      const data = await this.post('/api/copilot/workspaces', { project: this.selectedProject, root });
      this.projects = data.projects || [];
      this.selectedProject = this.projectSelect.value;
      await this.show();
      this.status('Project folder registered. Existing sessions remain bound to their original folder.');
    } catch (error) {
      this.status(error.message, true);
    }
  }

  async createSession(form) {
    const project = this.currentProject();
    if (!project?.workspace) {
      this.status('Register a local folder for this project first.', true);
      return;
    }
    const data = Object.fromEntries(form.entries());
    this.status('Creating a persistent Copilot session…');
    try {
      const result = await this.post('/api/copilot/sessions', {
        project: project.alias, title: data.title, model: data.model, prompt: data.prompt,
      });
      await this.renderProject();
      await this.openSession(result.session.id);
    } catch (error) {
      const message = error.message;
      await this.show();
      this.status(message, true);
    }
  }

  async openSession(id) {
    this.status('Resuming the saved session…');
    try {
      const result = await this.get(`/api/copilot/sessions/${encodeURIComponent(id)}`);
      this.sessionId = result.session.id;
      this.root.querySelector('[data-field="new-session"]').hidden = true;
      this.root.querySelector('[data-field="prompt"]').hidden = false;
      this.renderMessages(result.messages || []);
      this.status(result.session.last_error || '');
      const selected = this.root.querySelectorAll('.copilot-session-item');
      selected.forEach(button => button.dataset.active = String(button.dataset.sessionId === result.session.id));
    } catch (error) {
      this.status(error.message, true);
    }
  }

  renderMessages(messages) {
    const host = this.root.querySelector('[data-field="messages"]');
    const fragment = document.createDocumentFragment();
    for (const message of messages) {
      const item = document.createElement('article');
      item.className = `copilot-message copilot-message-${message.role}`;
      const heading = document.createElement('strong');
      heading.textContent = message.role === 'user' ? 'You' : 'Agent';
      const body = document.createElement('p');
      body.textContent = message.content;
      item.append(heading, body);
      fragment.append(item);
    }
    host.replaceChildren(fragment);
    host.scrollTop = host.scrollHeight;
  }

  async sendPrompt(form) {
    if (!this.sessionId) return;
    const text = String(form.get('text') || '').trim();
    if (!text) return;
    const button = this.root.querySelector('[data-field="prompt"] button');
    button.disabled = true;
    this.status('Agent is working. Any file changes will wait in the approval queue.');
    try {
      const result = await this.post(`/api/copilot/sessions/${this.sessionId}/messages`, { text });
      this.renderMessages(result.messages || []);
      this.root.querySelector('[data-field="prompt"] textarea').value = '';
      await this.show();
      await this.openSession(result.session.id);
      this.status(result.session.last_error || '');
    } catch (error) {
      this.status(error.message, true);
      const result = await this.get(`/api/copilot/sessions/${this.sessionId}`).catch(() => null);
      if (result) this.renderMessages(result.messages || []);
    } finally {
      button.disabled = false;
    }
  }

  hide() {
    this.visible = false;
    if (this.approvalTimer) clearInterval(this.approvalTimer);
    this.approvalTimer = null;
  }

  destroy() {
    this.destroyed = true;
    this.hide();
  }
}
