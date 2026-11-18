// Keys stay in this local form and the DPAPI-backed host endpoint, never in chat.
export class N8nPanel {
  constructor(root, { get, post, onRun = () => {}, pollMs = 10000 } = {}) {
    this.root = root; this.doc = root.ownerDocument; this.win = this.doc.defaultView;
    this.get = get; this.post = post; this.onRun = onRun; this.visible = false; this.busy = false;
    this.sequence = 0; this.signature = ''; this.data = null; this.disposed = false; this.loading = false;
    root.replaceChildren(); this.build();
    this.timer = pollMs > 0 ? this.win.setInterval(() => {
      if (this.visible && !this.doc.hidden && !this.busy && !this.dialog.open) void this.refresh();
    }, pollMs) : null;
  }
  el(tag, text = '', cls = '') {
    const node = this.doc.createElement(tag); node.textContent = text; if (cls) node.className = cls; return node;
  }
  button(label, action, cls = '') {
    const button = this.el('button', label, cls); button.type = 'button';
    button.addEventListener('click', () => void Promise.resolve().then(action).catch(error => this.fail(error.message)));
    return button;
  }
  fail(message) { this.error.textContent = message; if (this.dialog.open) this.reviewError.textContent = message; }
  field(label, input) { const node = this.el('label', '', 'n8n-field'); node.append(this.el('span', label), input); return node; }
  link(label) {
    const link = this.el('a', label, 'n8n-editor-link'); link.href = 'http://127.0.0.1:5678';
    link.target = '_blank'; link.rel = 'noopener noreferrer'; return link;
  }
  build() {
    this.root.classList.add('n8n-panel');
    const header = this.el('header', '', 'n8n-heading'), title = this.el('div');
    title.append(this.el('h2', 'Automations'));
    header.append(title, this.link('Open n8n ↗'));
    this.status = this.el('p', 'Checking the local service…', 'n8n-status'); this.status.setAttribute('role', 'status');
    this.error = this.el('p', '', 'n8n-error'); this.error.setAttribute('role', 'alert');
    this.notice = this.el('p', '', 'n8n-notice');
    this.connection = this.el('details', '', 'n8n-connection');
    this.connectionTitle = this.el('summary', 'Connect local n8n'); this.connection.append(this.connectionTitle);
    this.connection.append(this.el('p', 'Create an API key in n8n Settings and enter it here. Vega stores it encrypted.'));
    const form = this.el('form', '', 'n8n-connection-form');
    this.key = this.el('input'); this.key.type = 'password'; this.key.autocomplete = 'off';
    this.key.spellcheck = false; this.key.maxLength = 4096; this.key.placeholder = 'Local n8n API key';
    this.key.setAttribute('aria-label', 'Local n8n API key');
    this.connect = this.el('button', 'Connect', 'primary'); this.connect.type = 'submit';
    form.append(this.key, this.connect);
    form.addEventListener('submit', async event => {
      event.preventDefault(); if (this.busy || !this.key.value) return;
      this.busy = true; this.connect.disabled = true; this.error.textContent = '';
      try { await this.post('/api/n8n/connect', { api_key: this.key.value }); await this.refresh(); this.connection.open = false; }
      catch (error) { this.fail(error.message); }
      finally { this.key.value = ''; this.busy = false; this.connect.disabled = false; }
    });
    this.disconnect = this.button('Disconnect Vega', async () => {
      await this.post('/api/n8n/disconnect', {}); await this.refresh();
    }, 'n8n-disconnect');
    this.connection.append(form, this.disconnect);
    const design = this.el('details', '', 'n8n-design'), heading = this.el('summary', 'New workflow');
    design.append(heading, this.el('p', 'Choose a template to review. Saved workflows stay inactive.'));
    this.project = this.el('select'); this.project.setAttribute('aria-label', 'Workflow project');
    this.template = this.el('select'); this.template.setAttribute('aria-label', 'Workflow template');
    this.trigger = this.el('select'); this.trigger.setAttribute('aria-label', 'Workflow trigger');
    for (const [value, label] of [['manual', 'Manual'], ['every_10_minutes', 'Every 10 minutes · inactive until enabled in n8n']]) {
      const option = this.el('option', label); option.value = value; this.trigger.append(option);
    }
    this.name = this.el('input'); this.name.value = 'Project snapshot'; this.name.maxLength = 100;
    this.name.setAttribute('aria-label', 'Workflow name');
    const grid = this.el('div', '', 'n8n-form-grid');
    grid.append(this.field('Project', this.project), this.field('Template', this.template),
      this.field('Name', this.name), this.field('Trigger', this.trigger));
    this.preview = this.button('Preview workflow', async () => {
      if (this.busy) return; this.busy = true; this.preview.disabled = true; this.error.textContent = '';
      try {
        const result = await this.post('/api/n8n/drafts', { project: this.project.value,
          spec: { name: this.name.value, template: this.template.value, trigger: this.trigger.value } });
        await this.refresh(); this.openDraft(result.draft);
      } finally { this.busy = false; this.preview.disabled = false; }
    }, 'primary');
    const specialist = this.el('details', '', 'n8n-specialist');
    specialist.append(this.el('summary', 'Ask Vega’s agents to design it'));
    this.goal = this.el('textarea'); this.goal.rows = 3; this.goal.maxLength = 2000;
    this.goal.setAttribute('aria-label', 'Automation goal');
    this.goal.placeholder = 'For example: design a manual workflow that reads this project’s latest checklist.';
    this.designButton = this.button('Ask agents', async () => {
      if (this.busy || !this.goal.value.trim()) return;
      this.busy = true; this.designButton.disabled = true; this.error.textContent = '';
      try {
        const key = 'automation-ui:' + this.win.crypto.randomUUID();
        const result = await this.post('/api/team/runs', { request_key: key, project: this.project.value,
          mode: 'automation', goal: this.goal.value.trim() });
        this.notice.textContent = `Saved request ${result.run.id}. ${this.data?.work_paused ? 'It is queued while project work is paused.' : 'Vega will review both specialists’ results.'}`;
        await this.onRun(result.run.id);
      } finally { this.busy = false; this.designButton.disabled = false; }
    });
    specialist.append(this.goal, this.designButton);
    design.append(grid, this.preview, specialist);
    this.drafts = this.el('section', '', 'n8n-drafts');
    this.activity = this.el('section', '', 'n8n-activity');
    this.root.append(header, this.status, this.error, this.notice, this.connection, design, this.drafts, this.activity);
    this.dialog = this.el('dialog', '', 'n8n-review'); this.dialog.setAttribute('aria-label', 'Review n8n workflow');
    this.reviewTitle = this.el('h2', 'Review workflow');
    this.reviewNote = this.el('p');
    this.reviewCode = this.el('pre'); this.reviewCode.tabIndex = 0; this.reviewCode.setAttribute('aria-label', 'Exact workflow JSON');
    this.reviewCheck = this.el('input'); this.reviewCheck.type = 'checkbox';
    const approval = this.field('I reviewed this workflow. Save it inactive in my local n8n.', this.reviewCheck);
    approval.classList.add('n8n-approval');
    this.reviewError = this.el('p', '', 'n8n-error'); this.reviewError.setAttribute('role', 'alert');
    this.publishButton = this.button('Save inactive in n8n', () => this.publish(), 'primary');
    this.closeButton = this.button('Close', () => this.dialog.close());
    this.checkReceipt = this.button('Check receipt', async () => {
      if (this.busy || !this.reviewed) return;
      this.busy = true; this.checkReceipt.disabled = true;
      try {
        const result = await this.post(`/api/n8n/drafts/${this.reviewed.id}/receipt`, {});
        this.openDraft(result.draft); await this.refresh();
      } finally { this.busy = false; this.checkReceipt.disabled = false; }
    });
    const actions = this.el('div', '', 'n8n-review-actions'); actions.append(this.publishButton, this.checkReceipt, this.closeButton);
    this.reviewCheck.addEventListener('change', () => this.publishButton.disabled = !this.reviewCheck.checked || !this.reviewed?.can_publish || this.busy);
    this.dialog.addEventListener('close', () => { this.reviewed = null; this.reviewCheck.checked = false; });
    this.dialog.addEventListener('cancel', event => { if (this.busy) event.preventDefault(); });
    this.dialog.append(this.reviewTitle, this.reviewNote, this.reviewCode, approval, this.reviewError, actions);
    this.root.append(this.dialog);
  }
  async show() {
    this.visible = true;
    if (!this.projectsLoaded) {
      const data = await this.get('/api/project-tools/projects');
      for (const project of data.projects || []) {
        const option = this.el('option', project.label); option.value = project.alias; this.project.append(option);
      }
      if ((data.projects || []).some(project => project.alias === 'vega')) this.project.value = 'vega';
      this.projectsLoaded = true;
    }
    await this.refresh();
  }
  hide() { this.visible = false; this.key.value = ''; if (this.dialog.open && !this.busy) this.dialog.close(); }
  async refresh() {
    if (this.loading || this.disposed) return;
    const sequence = ++this.sequence; this.loading = true;
    try {
      const data = await this.get('/api/n8n');
      if (sequence !== this.sequence || this.disposed) return;
      this.data = data;
      this.status.textContent = data.runtime.state === 'ready'
        ? `n8n ${data.runtime.version} · running on this laptop · ${data.connected ? 'Vega connected' : 'API connection needed'}`
        : `n8n · ${data.runtime.state.replaceAll('_', ' ')}${data.runtime.error ? ' · ' + data.runtime.error : ''}`;
      this.connectionTitle.textContent = data.connected ? 'Local connection settings' : 'Connect local n8n';
      if (this.firstLoad !== false) this.connection.open = !data.connected;
      this.firstLoad = false; this.disconnect.hidden = !data.connected;
      this.connect.disabled = data.runtime.state !== 'ready'; this.preview.disabled = !this.project.value;
      if (!this.template.options.length) {
        for (const template of data.templates) { const option = this.el('option', template.label); option.value = template.id; this.template.append(option); }
      }
      const signature = JSON.stringify([data.drafts, data.activity]);
      if (signature !== this.signature) { this.signature = signature; this.renderRecords(); }
      if (this.reviewed && this.dialog.open) {
        const current = data.drafts.find(draft => draft.id === this.reviewed.id);
        if (!current || current.review_hash !== this.reviewed.review_hash || current.revision !== this.reviewed.revision || !current.can_publish) {
          this.reviewCheck.checked = false; this.publishButton.disabled = true;
          if (current) this.openDraft(current);
        }
      }
    } catch (error) {
      this.fail(error.message);
      if ([401, 403].includes(error.status)) {
        this.data = null; this.reviewed = null; this.reviewCheck.checked = false; this.publishButton.disabled = true;
        this.key.value = ''; this.drafts.replaceChildren(); this.activity.replaceChildren();
        if (this.dialog.open) this.dialog.close();
      }
    }
    finally { if (sequence === this.sequence) this.loading = false; }
  }
  renderRecords() {
    this.drafts.replaceChildren(this.el('h3', 'Drafts & receipts'));
    if (!this.data.drafts.length) this.drafts.append(this.el('p', 'No workflow drafts yet.'));
    for (const draft of this.data.drafts) {
      const card = this.el('div', '', 'n8n-record'), copy = this.el('div');
      copy.append(this.el('strong', draft.workflow.name), this.el('p', `${draft.project} · ${draft.state.replaceAll('_', ' ')}${draft.vega_reviewed ? ' · Vega reviewed' : ''}`));
      card.append(copy, this.button('Review', () => this.openDraft(draft))); this.drafts.append(card);
    }
    this.activity.replaceChildren(this.el('h3', 'n8n activity'));
    const activity = this.data.activity;
    if (!activity) { this.activity.append(this.el('p', 'Connect the API to see workflows and execution receipts.')); return; }
    if (activity.error) this.activity.append(this.el('p', activity.error, 'n8n-error'));
    if (!activity.workflows.length) this.activity.append(this.el('p', 'No workflows reported by n8n.'));
    for (const flow of activity.workflows) {
      const card = this.el('div', '', 'n8n-record'); card.append(this.el('strong', flow.name), this.el('span', flow.active ? 'Active in n8n' : 'Inactive'));
      this.activity.append(card);
    }
    const executions = activity.executions || [];
    this.activity.append(this.el('h4', 'Recent executions'));
    if (!executions.length) this.activity.append(this.el('p', 'No execution receipts reported.'));
    for (const execution of executions.slice(0, 12)) {
      const workflow = activity.workflows.find(flow => flow.id === execution.workflowId);
      this.activity.append(this.el('p', `${workflow?.name || execution.workflowId} · ${execution.status || 'Status unavailable'} · ${execution.startedAt || ''}`));
    }
  }
  openDraft(draft) {
    this.reviewed = structuredClone(draft); this.reviewCheck.checked = false;
    this.reviewTitle.textContent = draft.workflow.name;
    this.reviewNote.textContent = draft.state === 'published_inactive'
      ? `Saved inactive in n8n. Workflow receipt: ${draft.remote_id}. Activation is a separate action in n8n.`
      : draft.state === 'uncertain' ? 'Saving may have reached n8n. Check the receipt; this draft will not be posted again.'
      : `${draft.vega_reviewed ? 'Vega accepted both specialists’ work. ' : ''}This workflow only reads ${draft.project} locally. It will be saved inactive.`;
    this.reviewCode.textContent = JSON.stringify(draft.workflow, null, 2);
    this.reviewError.textContent = draft.error || (!draft.scope_valid ? 'The project scope changed. Prepare a new draft.' : !this.data?.connected ? 'Connect the local n8n API before publishing.' : '');
    this.publishButton.disabled = true; this.publishButton.hidden = draft.state !== 'draft';
    this.reviewCheck.disabled = !draft.can_publish; this.checkReceipt.hidden = draft.state !== 'uncertain';
    if (!this.dialog.open) this.dialog.showModal();
  }
  async publish() {
    if (this.busy || !this.reviewed?.can_publish || !this.reviewCheck.checked) return;
    const draft = structuredClone(this.reviewed); this.busy = true; this.publishButton.disabled = true; this.closeButton.disabled = true;
    try {
      const result = await this.post(`/api/n8n/drafts/${draft.id}/publish`, {
        approved: true, expected_revision: draft.revision, review_hash: draft.review_hash,
      });
      this.openDraft(result.draft); await this.refresh();
    } catch (error) { this.reviewCheck.checked = false; this.fail(error.message + ' Refresh the saved receipt before retrying.'); }
    finally { this.busy = false; this.closeButton.disabled = false; }
  }
  destroy() {
    this.disposed = true; this.sequence++; this.key.value = '';
    if (this.timer !== null) this.win.clearInterval(this.timer);
  }
}
