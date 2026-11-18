// Local project files. Transport and dashboard navigation belong to the caller.
// Importing this module does not touch the DOM, start timers, or make requests.
const API = '/api/project-tools';
const TERMINAL = new Set(['applied', 'cancelled', 'stale', 'failed', 'interrupted', 'uncertain', 'expired']);
const STATE_LABELS = {
  preparing: 'Preparing', awaiting_approval: 'Awaiting approval', applied: 'Applied',
  cancelled: 'Cancelled', stale: 'Stale', failed: 'Failed', interrupted: 'Interrupted',
  uncertain: 'Uncertain', expired: 'Expired',
};
const STATE_DETAILS = {
  preparing: 'Preparing a change for review.',
  awaiting_approval: 'Review the complete diff before choosing Apply reviewed change.',
  applied: 'Applied · receipt below.',
  cancelled: 'This proposal was cancelled.',
  stale: 'The file or workspace changed. Reload the file before preparing a new review.',
  failed: 'The operation failed. Check the receipt for details.',
  interrupted: 'The operation was interrupted. Refresh the proposal to check its receipt.',
  uncertain: 'The outcome is uncertain. Refresh the proposal to check its receipt.',
  expired: 'This review expired. Reload the file before preparing a new proposal.',
};
let panelSequence = 0;
const string = value => value == null ? '' : typeof value === 'object' ? JSON.stringify(value, null, 2) : String(value);
const array = value => Array.isArray(value) ? value : [];
const label = state => STATE_LABELS[state] || string(state) || 'Unknown';
const errorText = error => error?.message ? string(error.message) : 'The local request could not be completed.';
const hasValue = value => value !== undefined && value !== null && value !== '';
const registeredFolder = project => typeof project?.alias === 'string' && project.alias.length > 0 &&
  project.workspace?.can_read === true && hasValue(project.thread_id) && hasValue(project.workspace.revision);
const timestamp = value => {
  const milliseconds = typeof value === 'number' ? value * 1000
    : typeof value === 'string' && /^\d{4}-\d{2}-\d{2}T/.test(value) ? Date.parse(value) : NaN;
  return Number.isFinite(milliseconds) && Math.abs(milliseconds) <= 8.64e15 ? milliseconds : NaN;
};
const dateLabel = value => {
  const milliseconds = timestamp(value);
  return Number.isFinite(milliseconds)
    ? new Date(milliseconds).toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' }) : 'Not reported';
};
const quantity = (count, singular, plural = `${singular}s`) => `${count} ${Number(count) === 1 ? singular : plural}`;
const unifiedDiff = value => typeof value === 'string' &&
  /^--- [^\r\n]*\r?\n\+\+\+ [^\r\n]*\r?\n@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@/m.test(value);
const bindingKey = project => JSON.stringify([
  project?.alias, project?.thread_id, project?.workspace?.revision,
  project?.workspace?.root, project?.workspace?.can_read, project?.workspace?.can_write,
]);
const bytesLabel = bytes => Number.isFinite(bytes) ? `${bytes.toLocaleString()} bytes` : 'Size not reported';

function newState(alias) {
  return {
    alias, project: null, ready: false, loading: false, unavailable: '',
    path: '', entries: [], inspected: false, browsing: false, browseVersion: 0, browseError: '', truncated: false,
    query: '', searched: false, searching: false, searchVersion: 0, matches: [], searchSummary: '', searchError: '',
    selectedPath: null, file: null, draft: '', instruction: '', fileVersion: 0, reading: false, fileError: '',
    draftVersion: 0, instructionVersion: 0, needsReload: false, searchLine: null,
    proposals: new Map(), proposalVersion: 0, proposalsLoading: false, proposalError: '',
    activeId: null, reviewOrigin: null, review: null, historySignature: '', reviewSignature: '',
    receipt: null, status: '', error: '',
  };
}

export class ProjectToolsPanel {
  constructor(container, { get, post } = {}) {
    if (!container?.ownerDocument || typeof container.append !== 'function') {
      throw new TypeError('ProjectToolsPanel needs a container element.');
    }
    if (typeof get !== 'function' || typeof post !== 'function') {
      throw new TypeError('ProjectToolsPanel needs get and post functions.');
    }
    this.container = container;
    this.document = container.ownerDocument;
    this.timers = this.document.defaultView || globalThis;
    this.get = get;
    this.post = post;
    this.prefix = `vega-project-tools-${++panelSequence}`;
    this.visible = false;
    this.viewVersion = 0;
    this.pollTimer = null;
    this.mutation = null;
    // Keep identities and unresolved submissions across hide/show and project changes.
    this.attempts = new Map();
    this.requestSequence = 0;
    this.projects = [];
    this.state = newState('');
    this._build();
    this._render();
    this.element.hidden = true;
  }

  _node(tag, content, className) {
    const node = this.document.createElement(tag);
    if (content !== undefined) node.textContent = string(content);
    if (className) node.className = className;
    return node;
  }

  _button(text, action, handler, primary = false) {
    const button = this._node('button', text, `pt-button${primary ? ' pt-primary' : ''}`);
    button.type = 'button';
    button.dataset.action = action;
    button.addEventListener('click', event => {
      event.preventDefault();
      if (!button.disabled && this.visible) return handler();
    });
    return button;
  }

  _field(text, control, name, help) {
    const field = this._node('div', undefined, 'pt-field');
    control.id = `${this.prefix}-${name}`;
    control.name = name;
    const caption = this._node('label', text);
    caption.htmlFor = control.id;
    field.append(caption, control);
    if (help) {
      const hint = this._node('p', help, 'pt-hint');
      hint.id = `${control.id}-help`;
      control.setAttribute('aria-describedby', hint.id);
      field.append(hint);
    }
    return field;
  }

  _section(title, name, className = '') {
    const section = this._node('section', undefined, `pt-card ${className}`.trim());
    const heading = this._node('h3', title);
    heading.id = `${this.prefix}-${name}`;
    section.setAttribute('aria-labelledby', heading.id);
    section.append(heading);
    return { section, heading };
  }

  _live(className, alert = false) {
    const node = this._node('p', '', className);
    node.setAttribute('role', alert ? 'alert' : 'status');
    node.setAttribute('aria-live', alert ? 'assertive' : 'polite');
    node.setAttribute('aria-atomic', 'true');
    return node;
  }

  _code(name) {
    const pre = this._node('pre', '', 'pt-code');
    pre.tabIndex = 0;
    pre.setAttribute('role', 'region');
    pre.setAttribute('aria-label', name);
    return pre;
  }

  _build() {
    this.element = this._node('section', undefined, 'project-tools-panel');
    const title = this._node('h2', 'Project files');
    title.id = `${this.prefix}-title`;
    this.element.setAttribute('aria-labelledby', title.id);
    const header = this._node('header', undefined, 'pt-header');
    const introduction = this._node('div');
    introduction.append(title, this._node('p', 'Browse files and review changes.'));
    this.refreshButton = this._button('Refresh project', 'refresh-project', () => this.show(this.state.alias));
    this.projectSelect = this._node('select');
    this.projectSelect.addEventListener('change', () => {
      if (!this.projectSelect.disabled && this.visible) return this.show(this.projectSelect.value);
    });
    const projectControls = this._node('div', undefined, 'pt-project-controls');
    projectControls.append(this._field('Project folder', this.projectSelect, 'project'), this.refreshButton);
    header.append(introduction, projectControls);
    this._setProjects([]);
    this.context = this._node('div', undefined, 'pt-context');
    this.projectLabel = this._node('strong');
    this.folderLabel = this._node('code');
    this.accessLabel = this._node('span', '', 'pt-badge');
    this.context.append(this.projectLabel, this.folderLabel, this.accessLabel);
    this.status = this._live('pt-status');
    this.error = this._live('pt-error', true);
    this.unavailable = this._node('div', undefined, 'pt-unavailable');
    this.unavailableText = this._node('p');
    const projectsLink = this._node('a', 'Project requests and receipts');
    projectsLink.href = '/projects';
    this.unavailable.append(this.unavailableText, projectsLink);

    this.workspace = this._node('div', undefined, 'pt-workspace');
    const { section: browser } = this._section('Files and folders', 'browser', 'pt-browser');
    this.browser = browser;
    this.breadcrumb = this._node('nav', undefined, 'pt-breadcrumb');
    this.breadcrumb.setAttribute('aria-label', 'Folder path');
    this.folderStatus = this._live('pt-hint');
    this.folderError = this._live('pt-error', true);
    this.entries = this._node('ul', undefined, 'pt-file-list');
    this.entries.setAttribute('aria-label', 'Folder contents');
    this.refreshFolderButton = this._button('Refresh folder', 'refresh-folder', () => this._inspect(this.state.path));
    browser.append(this.breadcrumb, this.refreshFolderButton, this.folderStatus, this.folderError, this.entries);

    const search = this._node('form', undefined, 'pt-search');
    search.setAttribute('role', 'search');
    search.setAttribute('aria-label', 'Search project files');
    this.searchInput = this._node('input');
    this.searchInput.type = 'search';
    this.searchInput.autocomplete = 'off';
    this.searchInput.spellcheck = false;
    this.searchInput.addEventListener('input', () => {
      const state = this.state;
      state.query = this.searchInput.value;
      state.searchVersion += 1;
      state.searching = false;
      state.searched = false;
      state.matches = [];
      state.searchSummary = '';
      state.searchError = '';
      this._renderSearch();
      this._controls();
    });
    this.searchButton = this._node('button', 'Search', 'pt-button');
    this.searchButton.type = 'submit';
    this.searchButton.dataset.action = 'search';
    search.addEventListener('submit', event => { event.preventDefault(); return this._search(); });
    search.append(this._field('Find literal text', this.searchInput, 'search',
      'Exact-text search.'), this.searchButton);
    this.searchStatus = this._live('pt-hint');
    this.searchError = this._live('pt-error', true);
    this.matches = this._node('ul', undefined, 'pt-search-results');
    this.matches.setAttribute('aria-label', 'Literal search results');
    browser.append(search, this.searchStatus, this.searchError, this.matches);
    this.fileReceipt = this._node('details', undefined, 'pt-file-receipt');
    this.fileReceipt.append(this._node('summary', 'Latest file operation receipt'));
    this.fileReceiptBody = this._node('dl', undefined, 'pt-receipt');
    this.fileReceipt.append(this.fileReceiptBody);
    browser.append(this.fileReceipt);

    const { section: editor, heading: fileHeading } = this._section('Selected file', 'file', 'pt-editor');
    this.editor = editor;
    this.fileHeading = fileHeading;
    fileHeading.tabIndex = -1;
    this.fileMeta = this._node('p', '', 'pt-hint');
    this.fileStatus = this._live('pt-hint');
    this.fileError = this._live('pt-error', true);
    this.reloadButton = this._button('Reload file', 'reload-file', () => this._openFile(this.state.selectedPath, null, true));
    this.reloadHint = this._node('p', '', 'pt-notice');
    this.fileText = this._code('Saved file contents, read only');
    this.fileText.dataset.content = 'file';
    this.writeTools = this._node('div', undefined, 'pt-write-tools');
    this.editInput = this._node('textarea');
    this.editInput.rows = 12;
    this.editInput.spellcheck = false;
    this.editInput.autocomplete = 'off';
    this.editInput.addEventListener('input', () => {
      this.state.draft = this.editInput.value;
      this.state.draftVersion += 1;
      this._controls();
    });
    this.reviewButton = this._button('Review changes', 'propose', () => this._prepare('propose'), true);
    const draftField = this._field('Edit draft', this.editInput, 'draft',
      'Opening or reloading a file clears this draft.');
    const draftActions = this._node('div', undefined, 'pt-actions');
    draftActions.append(this.reviewButton);
    this.instructionInput = this._node('textarea');
    this.instructionInput.rows = 3;
    this.instructionInput.addEventListener('input', () => {
      this.state.instruction = this.instructionInput.value;
      this.state.instructionVersion += 1;
      this._controls();
    });
    this.suggestButton = this._button('Ask Vega to prepare', 'suggest', () => this._prepare('suggest'));
    const instructionField = this._field('Change instruction for the selected file', this.instructionInput, 'instruction',
      'Vega prepares a diff from this snapshot for review.');
    this.writeHint = this._node('p', '', 'pt-hint');
    this.writeTools.append(draftField, draftActions, instructionField, this.suggestButton, this.writeHint);
    editor.append(this.fileMeta, this.fileStatus, this.fileError, this.reloadButton, this.reloadHint, this.fileText, this.writeTools);
    this.workspace.append(browser, editor);

    const { section: review, heading: reviewHeading } = this._section('Review a change', 'review', 'pt-review');
    this.reviewSection = review;
    this.reviewHeading = reviewHeading;
    reviewHeading.tabIndex = -1;
    this.reviewEmpty = this._node('p', 'Choose a proposal or prepare a change.', 'pt-hint');
    this.reviewBody = this._node('div');
    this.reviewPath = this._node('code', '', 'pt-path');
    this.reviewLabel = this._node('p');
    this.reviewState = this._node('span', '', 'pt-badge');
    this.reviewDetail = this._live('pt-status');
    this.reviewSummary = this._node('p');
    this.reviewDates = this._node('p', '', 'pt-hint');
    this.reviewWarning = this._live('pt-notice');
    this.diff = this._code('Unified diff to review');
    this.diff.dataset.content = 'diff';
    this.diffEmpty = this._node('p', '', 'pt-hint');
    this.hashes = this._node('dl', undefined, 'pt-receipt');
    const reviewActions = this._node('div', undefined, 'pt-actions');
    this.applyButton = this._button('Apply reviewed change', 'apply', () => this._act('apply'), true);
    this.cancelButton = this._button('Cancel', 'cancel', () => this._act('cancel'));
    this.undoButton = this._button('Undo as a new review', 'undo', () => this._act('undo'));
    this.refreshProposalButton = this._button('Refresh proposal', 'refresh-proposal', () => this._refreshProposal(this.state.activeId));
    reviewActions.append(this.applyButton, this.cancelButton, this.undoButton, this.refreshProposalButton);
    this.proposalReceipt = this._node('dl', undefined, 'pt-receipt');
    this.receiptHeading = this._node('h4', 'Operation receipt');
    this.receiptDetail = this._node('p', '', 'pt-receipt-detail');
    this.receiptEmpty = this._node('p', '', 'pt-hint');
    this.reviewMetadata = this._node('details', undefined, 'pt-review-metadata');
    this.reviewMetadata.append(this._node('summary', 'Hashes and receipt details'),
      this.reviewDates, this.hashes, this.proposalReceipt);
    this.reviewBody.append(this.reviewPath, this.reviewLabel, this.reviewState, this.reviewDetail,
      this.reviewSummary, this.reviewWarning, this.diff, this.diffEmpty,
      reviewActions, this.receiptHeading, this.receiptDetail, this.receiptEmpty, this.reviewMetadata);
    review.append(this.reviewEmpty, this.reviewBody);
    const { section: history } = this._section('Proposals and receipts', 'history', 'pt-history');
    this.historySection = history;
    this.refreshHistoryButton = this._button('Refresh proposals', 'refresh-proposals', () => this._refreshProposals());
    this.historyStatus = this._live('pt-hint');
    this.historyError = this._live('pt-error', true);
    this.history = this._node('ul', undefined, 'pt-proposal-list');
    this.history.setAttribute('aria-label', 'Project proposals');
    history.append(this.refreshHistoryButton, this.historyStatus, this.historyError, this.history);
    this.element.append(header, this.context, this.status, this.error, this.unavailable, this.workspace, review, history);
    this.container.append(this.element);
  }

  _context() { return { state: this.state, view: this.viewVersion }; }
  _current(context) { return this.visible && this.state === context.state && this.viewVersion === context.view; }
  _binding() {
    const { alias, project } = this.state;
    return { project: alias, thread_id: project.thread_id, workspace_revision: project.workspace.revision };
  }
  _canRead() { return this.visible && this.state.ready && !this.state.loading; }
  _canWrite() {
    return this._canRead() && this.state.project.workspace.can_write === true &&
      this.state.file?.writable === true && !this.state.reading && !this.state.needsReload;
  }

  _setProjects(projects) {
    this.projects = projects;
    const placeholder = this._node('option', projects.length ? 'Choose a project' : 'No registered folders available');
    placeholder.value = '';
    placeholder.disabled = true;
    const options = projects.map(project => {
      const option = this._node('option', project.label || project.alias);
      option.value = project.alias;
      return option;
    });
    this.projectSelect.replaceChildren(placeholder, ...options);
    this.projectSelect.value = projects.some(project => project.alias === this.state.alias) ? this.state.alias : '';
  }

  async show(projectAlias = this.state.alias) {
    this._stopPolling();
    this.viewVersion += 1;
    this.visible = true;
    this.element.hidden = false;
    const alias = string(projectAlias);
    if (alias !== this.state.alias) this.state = newState(alias);
    const state = this.state, context = this._context();
    state.loading = true;
    state.ready = false;
    state.browsing = state.searching = state.reading = state.proposalsLoading = false;
    state.status = 'Checking the project folder…';
    state.error = '';
    this._render();
    try {
      const result = await this.get(`${API}/projects`);
      if (!this._current(context)) return;
      const registered = result?.available === true ? array(result.projects).filter(registeredFolder) : [];
      this._setProjects(registered);
      const project = array(result?.projects).find(item => item?.alias === alias);
      const available = result?.available === true && registeredFolder(project);
      if (!available) {
        this.state = newState(alias);
        this.state.project = project || null;
        this.state.unavailable = result?.available !== true
          ? 'Project folders are currently unavailable.'
          : registered.length
            ? `${alias ? 'Folder unavailable for this project. ' : ''}Choose a project folder above to browse its files.`
            : 'No registered project folders are available.';
        this._render();
        return;
      }
      if (state.project && bindingKey(state.project) !== bindingKey(project)) {
        Object.assign(state, newState(alias));
        state.status = 'The workspace registration changed. The previous file and draft were cleared.';
      } else {
        state.status = 'Project folder ready.';
      }
      state.project = project;
      state.ready = true;
      state.loading = false;
      state.unavailable = '';
      this._render();
      const loads = [this._inspect(state.path), this._refreshProposals()];
      if (state.selectedPath !== null && !state.file) loads.push(this._openFile(state.selectedPath));
      if (state.searched && state.query.length) loads.push(this._search());
      await Promise.allSettled(loads);
    } catch (error) {
      if (!this._current(context)) return;
      this._setProjects([]);
      state.error = `Could not check the project folder. ${errorText(error)}`;
      state.status = 'Refresh project to reconnect. Your existing draft is kept in this view.';
      if (!state.project?.workspace) state.unavailable = 'Folder unavailable until the local host can confirm its registration.';
    } finally {
      if (this._current(context)) {
        state.loading = false;
        this._render();
        this._schedulePoll();
      }
    }
  }

  hide() {
    this.visible = false;
    this.element.hidden = true;
    this.viewVersion += 1;
    this._stopPolling();
    this._controls();
  }

  async _inspect(path) {
    if (!this._canRead()) return;
    const context = this._context(), state = this.state, version = ++state.browseVersion;
    state.path = string(path);
    state.browsing = true;
    state.browseError = '';
    state.entries = [];
    this._renderBrowser();
    this._controls();
    try {
      const result = await this.post(`${API}/inspect`, { ...this._binding(), path: state.path });
      if (!this._current(context) || version !== state.browseVersion) return;
      if (typeof result?.path !== 'string' || !Array.isArray(result.entries)) throw new Error('The folder response was incomplete.');
      state.path = result.path;
      state.entries = result.entries.filter(entry => ['file', 'directory'].includes(entry?.kind) && typeof entry.path === 'string');
      state.truncated = result.truncated === true;
      state.inspected = true;
      state.receipt = result.receipt || null;
    } catch (error) {
      if (this._current(context) && version === state.browseVersion) state.browseError = errorText(error);
    } finally {
      if (this._current(context) && version === state.browseVersion) {
        state.browsing = false;
        this._renderBrowser();
        this._renderFileReceipt();
        this._controls();
      }
    }
  }

  async _search() {
    if (!this._canRead() || !this.state.query.length || this.state.searching) return;
    const context = this._context(), state = this.state, version = ++state.searchVersion;
    const query = state.query;
    state.searching = true;
    state.searched = true;
    state.searchError = '';
    state.matches = [];
    state.searchSummary = 'Searching project files…';
    this._renderSearch();
    this._controls();
    try {
      const result = await this.post(`${API}/search`, { ...this._binding(), path: '', query });
      if (!this._current(context) || version !== state.searchVersion) return;
      if (!Array.isArray(result?.matches)) throw new Error('The search response was incomplete.');
      state.matches = result.matches.filter(match => typeof match?.path === 'string');
      state.searchSummary = `${quantity(state.matches.length, 'match', 'matches')} · ${quantity(result.files_scanned, 'file')} scanned.${result.truncated ? ' Results were truncated; narrow your search to see more.' : ''}`;
      state.receipt = result.receipt || null;
    } catch (error) {
      if (this._current(context) && version === state.searchVersion) {
        state.searchError = errorText(error);
        state.searchSummary = 'Search could not be completed.';
      }
    } finally {
      if (this._current(context) && version === state.searchVersion) {
        state.searching = false;
        this._renderSearch();
        this._renderFileReceipt();
        this._controls();
      }
    }
  }

  async _openFile(path, line = null, reload = false) {
    if (!this._canRead() || path === null) return;
    const state = this.state;
    if (!reload && state.selectedPath === path && (state.reading || state.file)) {
      this.fileHeading.focus();
      return;
    }
    const context = this._context(), version = ++state.fileVersion;
    state.selectedPath = string(path);
    state.file = null;
    state.draft = state.instruction = '';
    state.draftVersion += 1;
    state.instructionVersion += 1;
    state.reading = true;
    state.needsReload = false;
    state.fileError = '';
    state.searchLine = line;
    state.activeId = state.reviewOrigin = state.review = null;
    state.reviewSignature = '';
    state.error = '';
    state.status = this.mutation ? 'An earlier change request is pending. Check proposals for its result.' : '';
    this._renderMessages();
    this._renderFile();
    this._renderReview();
    this._renderHistory();
    this._controls();
    try {
      const result = await this.post(`${API}/read`, { ...this._binding(), path: state.selectedPath });
      if (!this._current(context) || version !== state.fileVersion) return;
      const file = result?.file;
      if (file?.path !== state.selectedPath || typeof file.text !== 'string' || !hasValue(file.sha256)) {
        throw new Error('The file response did not match the selected file.');
      }
      state.file = { ...file };
      state.draft = file.text;
      state.receipt = result.receipt || null;
      this.fileHeading.focus();
    } catch (error) {
      if (this._current(context) && version === state.fileVersion) state.fileError = errorText(error);
    } finally {
      if (this._current(context) && version === state.fileVersion) {
        state.reading = false;
        this._renderFile();
        this._renderFileReceipt();
        this._controls();
      }
    }
  }

  _proposalValid(proposal, project = this.state.alias) {
    return proposal && typeof proposal.id === 'string' && proposal.id.length > 0 &&
      proposal.project === project && typeof proposal.path === 'string' &&
      hasValue(proposal.revision) && typeof proposal.state === 'string';
  }

  _merge(proposal) {
    if (!this._proposalValid(proposal)) return false;
    const old = this.state.proposals.get(proposal.id);
    if (old && Number.isFinite(Number(old.revision)) && Number.isFinite(Number(proposal.revision)) &&
        Number(old.revision) > Number(proposal.revision)) return false;
    this.state.proposals.set(proposal.id, { ...proposal });
    if ((!old || old.revision !== proposal.revision || old.state !== proposal.state) &&
        proposal.path === this.state.file?.path && proposal.before_sha256 === this.state.file.sha256 &&
        ['applied', 'stale', 'uncertain', 'interrupted'].includes(proposal.state)) {
      this.state.needsReload = true;
    }
    for (const attempt of this.attempts.values()) {
      if (attempt.uncertain && ['apply', 'cancel'].includes(attempt.kind) &&
          attempt.sourceId === proposal.id && attempt.project === proposal.project &&
          (attempt.revision !== proposal.revision || TERMINAL.has(proposal.state))) {
        attempt.uncertain = false;
        attempt.proposalId = proposal.id;
        this.state.error = '';
        this.state.status = `Refreshed proposal: ${label(proposal.state).toLowerCase()}. Check its operation receipt.`;
        this._renderMessages();
      }
    }
    return true;
  }

  async _refreshProposals() {
    if (!this._canRead()) return;
    const context = this._context(), state = this.state, version = ++state.proposalVersion;
    this._stopPolling();
    state.proposalsLoading = true;
    state.proposalError = '';
    this._renderHistory();
    this._controls();
    try {
      const result = await this.get(`${API}/proposals?project=${encodeURIComponent(state.alias)}`);
      if (!this._current(context) || version !== state.proposalVersion) return;
      if (!Array.isArray(result?.proposals)) throw new Error('The proposals response was incomplete.');
      for (const proposal of result.proposals) this._merge(proposal);
    } catch (error) {
      if (this._current(context) && version === state.proposalVersion) state.proposalError = errorText(error);
    } finally {
      if (this._current(context) && version === state.proposalVersion) {
        state.proposalsLoading = false;
        this._renderReview();
        this._renderHistory();
        this._controls();
        this._schedulePoll();
      }
    }
  }

  async _refreshProposal(id) {
    if (!id || !this._canRead() || this.mutation) return;
    const context = this._context(), state = this.state, version = ++state.proposalVersion;
    this._stopPolling();
    state.proposalsLoading = true;
    state.proposalError = '';
    this._controls();
    try {
      const result = await this.get(`${API}/proposals/${encodeURIComponent(id)}`);
      if (!this._current(context) || version !== state.proposalVersion) return;
      if (!this._proposalValid(result?.proposal) || result.proposal.id !== id) throw new Error('The proposal response did not match this review.');
      this._merge(result.proposal);
    } catch (error) {
      if (this._current(context) && version === state.proposalVersion) state.proposalError = errorText(error);
    } finally {
      if (this._current(context) && version === state.proposalVersion) {
        state.proposalsLoading = false;
        this._renderReview();
        this._renderHistory();
        this._controls();
        this._schedulePoll();
      }
    }
  }

  _selectProposal(id, origin = null) {
    if (!this._canRead() || !this.state.proposals.has(id)) return;
    this.state.activeId = id;
    this.state.reviewOrigin = origin;
    this._renderReview();
    this._renderHistory();
    this._controls();
    this.reviewHeading.focus();
  }

  _requestKey() {
    const crypto = this.document.defaultView?.crypto || globalThis.crypto;
    return typeof crypto?.randomUUID === 'function' ? crypto.randomUUID()
      : `${this.prefix}-${Date.now()}-${++this.requestSequence}`;
  }

  _prepareSignature(kind) {
    const state = this.state;
    return JSON.stringify([kind, this._binding(), state.file?.path, state.file?.sha256,
      kind === 'propose' ? state.draft : state.instruction]);
  }

  _actionSignature(kind, proposal) {
    return JSON.stringify([kind, proposal.project, proposal.id, proposal.revision, proposal.review_hash]);
  }

  async _prepare(kind) {
    if (!this._canWrite() || this.mutation) return;
    const state = this.state;
    if (kind === 'propose' ? state.draft === state.file.text : !state.instruction.trim()) return;
    const signature = this._prepareSignature(kind);
    const previous = this.attempts.get(signature);
    const existing = previous?.proposalId && state.proposals.get(previous.proposalId);
    if (previous && (!existing || !TERMINAL.has(existing.state))) {
      if (existing) this._selectProposal(existing.id);
      return this._recover(previous);
    }
    const payload = {
      ...this._binding(), path: state.file.path, expected_sha256: state.file.sha256,
      [kind === 'propose' ? 'text' : 'instruction']: kind === 'propose' ? state.draft : state.instruction,
      request_key: this._requestKey(),
    };
    const origin = {
      kind, fileVersion: state.fileVersion,
      contentVersion: kind === 'propose' ? state.draftVersion : state.instructionVersion,
    };
    return this._mutate(kind, `${API}/${kind}`, payload, signature, origin);
  }

  _reviewChanged() {
    const origin = this.state.reviewOrigin;
    return origin && (origin.fileVersion !== this.state.fileVersion ||
      origin.contentVersion !== (origin.kind === 'propose' ? this.state.draftVersion : this.state.instructionVersion));
  }

  _canApply(proposal) {
    const review = this.state.review;
    const expires = timestamp(proposal?.expires_at);
    return this._canRead() && this.state.project.workspace.can_write === true &&
      proposal?.state === 'awaiting_approval' && proposal.can_apply === true &&
      unifiedDiff(proposal.diff) &&
      hasValue(proposal.before_sha256) && hasValue(proposal.after_sha256) && hasValue(proposal.review_hash) &&
      (!Number.isFinite(expires) || expires > Date.now()) &&
      review?.id === proposal.id && review.revision === proposal.revision &&
      review.review_hash === proposal.review_hash && review.diff === proposal.diff &&
      !this._reviewChanged();
  }

  _blockedProposal(proposal) {
    return [...this.attempts.values()].some(attempt => attempt.uncertain && attempt.sourceId === proposal?.id &&
      attempt.project === this.state.alias && attempt.revision === proposal?.revision);
  }

  async _act(kind) {
    const state = this.state, proposal = state.proposals.get(state.activeId);
    if (!this._canRead() || this.mutation || state.proposalsLoading || !proposal || this._blockedProposal(proposal)) return;
    if (kind === 'apply' && !this._canApply(proposal)) return;
    if (kind === 'cancel' && !(proposal.can_cancel === true && ['preparing', 'awaiting_approval'].includes(proposal.state))) return;
    if (kind === 'undo' && !(proposal.state === 'applied' && proposal.can_undo === true && state.project.workspace.can_write === true)) return;
    const signature = this._actionSignature(kind, proposal);
    const previous = this.attempts.get(signature);
    if (previous) return this._recover(previous);
    const payload = { expected_revision: proposal.revision };
    if (kind === 'apply') payload.review_hash = state.review.review_hash;
    if (kind === 'undo') payload.request_key = this._requestKey();
    return this._mutate(kind, `${API}/proposals/${encodeURIComponent(proposal.id)}/${kind}`, payload, signature, null, proposal);
  }

  async _mutate(kind, url, payload, signature, origin, source = null) {
    if (this.mutation) return;
    const context = this._context(), state = this.state;
    const fileVersion = state.fileVersion;
    const selectionCurrent = () => fileVersion === state.fileVersion && (origin
      ? origin.contentVersion === (kind === 'propose' ? state.draftVersion : state.instructionVersion)
      : state.activeId === source.id);
    const attempt = {
      kind, project: state.alias, path: payload.path ?? source.path, requestKey: payload.request_key,
      sourceId: source?.id, revision: source?.revision, proposalId: null, uncertain: true,
    };
    this.attempts.set(signature, attempt);
    this.mutation = attempt;
    state.proposalVersion += 1;
    state.proposalsLoading = false;
    this._stopPolling();
    state.error = '';
    state.status = ({ propose: 'Preparing your draft for review…', suggest: 'Asking Vega to prepare this file change…',
      apply: 'Applying the reviewed change…', cancel: 'Cancelling the proposal…', undo: 'Preparing an undo proposal for review…' })[kind];
    this._renderMessages();
    this._controls();
    let recover = false, selectionChanged = false;
    try {
      const result = await this.post(url, payload);
      if (!this._current(context)) return;
      selectionChanged = !selectionCurrent();
      if (selectionChanged) { recover = true; return; }
      const proposal = result?.proposal;
      if (!this._proposalValid(proposal) || proposal.path !== attempt.path ||
          (source && kind !== 'undo' && proposal.id !== source.id) ||
          (kind === 'undo' && (proposal.id === source.id || proposal.state !== 'awaiting_approval')) ||
          (origin && proposal.before_sha256 !== payload.expected_sha256)) {
        throw new Error('The response did not match the submitted change.');
      }
      attempt.proposalId = proposal.id;
      attempt.uncertain = false;
      this._merge(proposal);
      this._selectProposal(proposal.id, origin);
      state.status = kind === 'undo'
        ? 'Undo prepared as a new proposal. Review the inverse diff before applying it.'
        : `Proposal ${label(proposal.state).toLowerCase()}. Check its diff and operation receipt.`;
    } catch (error) {
      if (!this._current(context)) return;
      selectionChanged = !selectionCurrent();
      if (selectionChanged) { recover = true; return; }
      state.error = `The request result is unconfirmed. ${errorText(error)}`;
      state.status = 'Checking proposals and receipts. This request will not be sent again automatically.';
      recover = true;
    } finally {
      if (this.mutation === attempt) this.mutation = null;
      if (this._current(context)) {
        this._renderMessages();
        this._renderReview();
        this._renderHistory();
        this._controls();
        if (recover) {
          // Reads recover an uncertain POST. Never replay apply, cancel, or generation here.
          if (selectionChanged) await this._refreshProposals();
          else await this._recover(attempt);
        }
        this._schedulePoll();
      } else if (this.visible) {
        this._controls();
        // A fresh read may discover work completed while the panel was hidden.
        if (this.state.alias === attempt.project && this._canRead()) await this._refreshProposals();
        this._schedulePoll();
      }
    }
  }

  async _recover(attempt) {
    if (!this._canRead() || attempt.project !== this.state.alias || this.mutation) return;
    this.state.status = attempt.uncertain
      ? 'The request may have completed. Refresh proposals and inspect the receipt before preparing another change.'
      : 'Refreshing the existing proposal.';
    this._renderMessages();
    const id = attempt.proposalId || (attempt.kind !== 'undo' && attempt.sourceId);
    if (id) await this._refreshProposal(id);
    else await this._refreshProposals();
  }

  _stopPolling() {
    if (this.pollTimer !== null) this.timers.clearTimeout(this.pollTimer);
    this.pollTimer = null;
  }

  _schedulePoll() {
    this._stopPolling();
    if (!this._canRead() || this.mutation || this.state.proposalsLoading ||
        ![...this.state.proposals.values()].some(proposal => proposal.state === 'preparing')) return;
    this.pollTimer = this.timers.setTimeout(() => this._poll(), 1500);
  }

  async _poll() {
    this.pollTimer = null;
    if (!this._canRead() || this.mutation || this.state.proposalsLoading) return;
    const context = this._context(), state = this.state, version = ++state.proposalVersion;
    const preparing = [...state.proposals.values()].filter(proposal => proposal.state === 'preparing');
    state.proposalsLoading = true;
    this._controls();
    const results = await Promise.allSettled(preparing.map(proposal =>
      Promise.resolve().then(() => this.get(`${API}/proposals/${encodeURIComponent(proposal.id)}`))));
    if (!this._current(context) || version !== state.proposalVersion) return;
    state.proposalsLoading = false;
    state.proposalError = '';
    for (let index = 0; index < results.length; index += 1) {
      const result = results[index];
      if (result.status === 'fulfilled' && this._proposalValid(result.value?.proposal) &&
          result.value.proposal.id === preparing[index].id) {
        this._merge(result.value.proposal);
      } else {
        state.proposalError = 'Could not refresh the preparation. Refresh the proposal to check its receipt.';
      }
    }
    this._renderReview();
    this._renderHistory();
    this._controls();
    this._schedulePoll();
  }

  _renderMessages() {
    this.status.textContent = this.state.status;
    this.error.textContent = this.state.error;
  }

  _render() {
    const state = this.state, mapped = Boolean(state.project?.workspace?.can_read && !state.unavailable);
    this.projectLabel.textContent = state.project?.label || state.alias || 'No project selected';
    this.folderLabel.textContent = mapped ? state.project.workspace.root : '';
    this.accessLabel.textContent = mapped ? (state.project.workspace.can_write ? 'Review before writing' : 'Read only') : 'Folder unavailable';
    this.unavailable.hidden = !state.unavailable;
    this.unavailableText.textContent = state.unavailable;
    this.workspace.hidden = !mapped;
    this.reviewSection.hidden = !mapped;
    this.historySection.hidden = !mapped;
    this.searchInput.value = state.query;
    this._renderMessages();
    this._renderBrowser();
    this._renderSearch();
    this._renderFile();
    this._renderFileReceipt();
    this._renderReview();
    this._renderHistory();
    this._controls();
  }

  _renderBrowser() {
    const state = this.state, version = state.browseVersion;
    const parts = state.path.replace(/\\/g, '/').split('/').filter(Boolean);
    const crumbs = this._node('ol');
    for (let index = 0; index <= parts.length; index += 1) {
      const path = parts.slice(0, index).join('/');
      const button = this._button(index ? parts[index - 1] : 'Project folder', 'folder', () => {
        if (state === this.state && version === state.browseVersion) return this._inspect(path);
      });
      if (index === parts.length) {
        button.setAttribute('aria-current', 'page');
        button.disabled = true;
      } else button.disabled = !this._canRead();
      const item = this._node('li');
      item.append(button);
      crumbs.append(item);
    }
    this.breadcrumb.replaceChildren(crumbs);
    const rows = [];
    for (const entry of state.entries) {
      const item = this._node('li');
      const button = this._button('', entry.kind === 'directory' ? 'open-folder' : 'open-file', () => {
        if (state !== this.state || version !== state.browseVersion) return;
        return entry.kind === 'directory' ? this._inspect(entry.path) : this._openFile(entry.path);
      });
      button.disabled = !this._canRead();
      button.setAttribute('aria-label', `Open ${entry.kind === 'directory' ? 'folder' : 'file'} ${string(entry.name)}`);
      button.append(this._node('span', entry.kind === 'directory' ? 'Folder' : 'File', 'pt-kind'),
        this._node('span', entry.name, 'pt-entry-name'),
        this._node('span', entry.kind === 'file' ? bytesLabel(entry.size) : 'Browse', 'pt-entry-meta'));
      item.append(button);
      rows.push(item);
    }
    this.entries.replaceChildren(...rows);
    this.browser.setAttribute('aria-busy', String(state.browsing));
    this.folderStatus.textContent = state.browsing ? 'Loading folder…'
      : state.inspected ? `${state.entries.length ? quantity(state.entries.length, 'entry', 'entries') : 'This folder is empty.'}${state.truncated ? ' Listing truncated; search for a file or open a subfolder.' : ''}`
        : 'Open the project folder to list files.';
    this.folderError.textContent = state.browseError;
  }

  _renderSearch() {
    const state = this.state, version = state.searchVersion;
    this.searchStatus.textContent = state.searchSummary;
    this.searchError.textContent = state.searchError;
    this.matches.setAttribute('aria-busy', String(state.searching));
    const rows = state.matches.map(match => {
      const row = this._node('li');
      const button = this._button('', 'open-match', () => {
        if (state === this.state && version === state.searchVersion) return this._openFile(match.path, match.line);
      });
      button.disabled = !this._canRead();
      button.append(this._node('code', `${match.path}:${string(match.line)}`, 'pt-path'),
        this._node('span', match.text, 'pt-match-text'));
      row.append(button);
      return row;
    });
    this.matches.replaceChildren(...rows);
  }

  _renderFile() {
    const state = this.state, file = state.file;
    this.fileMeta.textContent = state.selectedPath === null ? 'Choose a file from the folder or search results.'
      : `${state.selectedPath}${file ? ` · ${bytesLabel(file.bytes)}` : ''}${state.searchLine ? ` · Search match at line ${state.searchLine}` : ''}`;
    this.fileStatus.textContent = state.reading ? 'Reading the selected file…' : file ? 'Saved file snapshot · read only' : '';
    this.fileError.textContent = state.fileError;
    this.fileText.textContent = file?.text ?? '';
    this.fileText.hidden = !file;
    this.writeTools.hidden = !file;
    if (this.editInput.value !== state.draft) this.editInput.value = state.draft;
    if (this.instructionInput.value !== state.instruction) this.instructionInput.value = state.instruction;
    this.editor.setAttribute('aria-busy', String(state.reading));
  }

  _details(container, pairs) {
    const children = [];
    for (const [term, value] of pairs) {
      children.push(this._node('dt', term), this._node('dd', hasValue(value) ? value : 'Not reported'));
    }
    container.replaceChildren(...children);
  }

  _receipt(container, receipt, includeDetail = true) {
    const pairs = [
      ['Receipt', receipt?.id], ['Operation', receipt?.operation], ['State', receipt?.state],
    ];
    if (includeDetail) pairs.push(['Detail', receipt?.detail]);
    pairs.push(['Finished', dateLabel(receipt?.finished_at)]);
    this._details(container, pairs);
  }

  _renderFileReceipt() {
    this.fileReceipt.hidden = !this.state.receipt;
    this._receipt(this.fileReceiptBody, this.state.receipt);
  }

  _renderReview() {
    const state = this.state, proposal = state.proposals.get(state.activeId);
    this.reviewEmpty.hidden = Boolean(proposal);
    this.reviewBody.hidden = !proposal;
    if (!proposal) {
      state.review = null;
      state.reviewSignature = '';
      for (const node of [this.reviewPath, this.reviewLabel, this.reviewState, this.reviewDetail,
        this.reviewSummary, this.reviewDates, this.reviewWarning, this.diff, this.diffEmpty,
        this.hashes, this.proposalReceipt, this.receiptDetail, this.receiptEmpty]) node.textContent = '';
      this.reviewMetadata.open = false;
      return;
    }
    const signature = JSON.stringify(proposal);
    if (signature === state.reviewSignature) return;
    state.reviewSignature = signature;
    this.reviewPath.textContent = proposal.path;
    this.reviewLabel.textContent = proposal.label || proposal.id;
    this.reviewState.textContent = label(proposal.state);
    this.reviewState.dataset.state = proposal.state;
    this.reviewDetail.textContent = STATE_DETAILS[proposal.state] || 'Refresh this proposal to check its current state.';
    this.reviewSummary.textContent = string(proposal.summary);
    this.reviewDates.textContent = `Revision ${string(proposal.revision)} · Created ${dateLabel(proposal.created_at)} · Expires ${dateLabel(proposal.expires_at)}`;
    this.diff.textContent = typeof proposal.diff === 'string' ? proposal.diff : '';
    this.diff.hidden = !this.diff.textContent;
    this.diffEmpty.hidden = Boolean(this.diff.textContent);
    this.diffEmpty.textContent = proposal.state === 'preparing' ? 'Waiting for a concrete diff…' : 'No diff is available. This proposal cannot be applied without a concrete review.';
    this._details(this.hashes, [['Before SHA-256', proposal.before_sha256], ['After SHA-256', proposal.after_sha256]]);
    this._receipt(this.proposalReceipt, proposal.receipt, false);
    this.receiptDetail.textContent = string(proposal.receipt?.detail);
    this.receiptDetail.hidden = !hasValue(proposal.receipt?.detail);
    this.proposalReceipt.hidden = !proposal.receipt;
    this.receiptEmpty.hidden = Boolean(proposal.receipt);
    this.receiptEmpty.textContent = proposal.receipt ? '' : 'No operation receipt has been reported yet.';
    state.review = Object.freeze({
      id: proposal.id, revision: proposal.revision, review_hash: proposal.review_hash, diff: proposal.diff,
    });
  }

  _renderHistory() {
    const state = this.state, proposals = [...state.proposals.values()]
      .sort((left, right) => (timestamp(right.created_at) || 0) - (timestamp(left.created_at) || 0));
    this.historyStatus.textContent = state.proposalsLoading ? 'Refreshing proposals…'
      : proposals.length ? `${quantity(proposals.length, 'proposal')}. Select one to review its diff and receipt.` : 'No proposals for this project yet.';
    this.historyError.textContent = state.proposalError;
    this.historySection.setAttribute('aria-busy', String(state.proposalsLoading));
    const signature = JSON.stringify([this._canRead(), state.activeId,
      proposals.map(proposal => [proposal.id, proposal.revision, proposal.state, proposal.summary])]);
    if (signature === state.historySignature) return;
    state.historySignature = signature;
    const focusedId = this.document.activeElement?.dataset?.proposalId;
    let restore = null;
    const rows = proposals.map(proposal => {
      const row = this._node('li');
      const button = this._button('', 'open-proposal', () => {
        if (state !== this.state) return;
        this._selectProposal(proposal.id);
        return this._refreshProposal(proposal.id);
      });
      button.dataset.proposalId = proposal.id;
      button.disabled = !this._canRead();
      if (state.activeId === proposal.id) button.setAttribute('aria-current', 'true');
      button.append(this._node('span', proposal.path, 'pt-path'), this._node('span', label(proposal.state), 'pt-badge'),
        this._node('span', proposal.summary || proposal.label || proposal.id, 'pt-hint'));
      if (focusedId === proposal.id) restore = button;
      row.append(button);
      return row;
    });
    this.history.replaceChildren(...rows);
    if (restore) restore.focus();
  }

  _controls() {
    const state = this.state, proposal = state.proposals.get(state.activeId);
    const read = this._canRead(), write = this._canWrite(), busy = Boolean(this.mutation);
    this.element.setAttribute('aria-busy', String(state.loading || busy));
    this.refreshButton.disabled = !this.visible || state.loading;
    this.projectSelect.disabled = !this.visible || state.loading || busy || !this.projects.length;
    this.refreshFolderButton.disabled = !read || state.browsing;
    this.searchInput.disabled = !read;
    this.searchButton.disabled = !read || !state.query.length || state.searching;
    this.reloadButton.hidden = state.selectedPath === null;
    this.reloadButton.disabled = !read || state.reading;
    this.editInput.readOnly = !write;
    this.instructionInput.disabled = !write;
    this.reviewButton.disabled = !write || busy || state.draft === state.file?.text;
    this.suggestButton.disabled = !write || busy || !state.instruction.trim();
    this.writeHint.textContent = !state.project?.workspace?.can_write ? 'This project folder is read only.'
      : state.file && !state.file.writable ? 'This file is read only. Choose a writable text file to prepare a change.'
        : state.needsReload ? 'Reload this file before preparing another change.'
          : 'Every file write requires its own reviewed proposal.';
    this.reloadHint.hidden = !state.needsReload;
    this.reloadHint.textContent = state.needsReload ? 'This saved snapshot may be out of date. Reload file clears the draft and reads the current contents.' : '';
    const blocked = !read || busy || state.proposalsLoading || this._blockedProposal(proposal);
    this.applyButton.disabled = blocked || !this._canApply(proposal) ||
      Boolean(proposal && this.attempts.has(this._actionSignature('apply', proposal)));
    this.cancelButton.hidden = !proposal?.can_cancel || TERMINAL.has(proposal?.state);
    this.cancelButton.disabled = blocked || !['preparing', 'awaiting_approval'].includes(proposal?.state) ||
      Boolean(proposal && this.attempts.has(this._actionSignature('cancel', proposal)));
    this.undoButton.hidden = proposal?.state !== 'applied' || proposal?.can_undo !== true;
    this.undoButton.disabled = blocked || !state.project?.workspace?.can_write ||
      Boolean(proposal && this.attempts.has(this._actionSignature('undo', proposal)));
    this.refreshProposalButton.disabled = !read || busy || state.proposalsLoading || !proposal;
    this.refreshHistoryButton.disabled = !read || busy || state.proposalsLoading;
    this.reviewWarning.textContent = this._reviewChanged()
      ? 'The draft or instruction changed after this proposal was prepared. Prepare a new review for your current edits.'
      : this._blockedProposal(proposal) ? 'The last request is unconfirmed. Refresh this proposal to check the receipt; it will not be applied again automatically.'
        : proposal?.review_run && proposal?.state === 'awaiting_approval'
          ? proposal.vega_review_state === 'accepted'
            ? 'Vega accepted the specialists’ draft. Open this run in Vega work to review and apply its exact diff.'
            : 'Vega must review the specialists’ draft before it can be approved. Follow the run in Vega work.'
        : proposal?.state === 'awaiting_approval' && !unifiedDiff(proposal.diff)
          ? 'A concrete unified diff is required before applying. Reload the file and prepare a review with visible changes.'
          : proposal?.state === 'awaiting_approval' && timestamp(proposal.expires_at) <= Date.now()
            ? 'This review has expired. Refresh the proposal to check its receipt.'
        : '';
  }
}
