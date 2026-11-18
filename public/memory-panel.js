// Standalone memory UI. All host requests go through the supplied helpers.
// Imported values are text, including their titles, roles and provenance.
const PAGE_SIZE = 50;
const DEFAULT_UPLOAD_LIMIT = 20 * 1024 * 1024;
let panelSequence = 0;

const text = value => {
  if (value == null) return '';
  if (typeof value === 'object') {
    try { return JSON.stringify(value, null, 2); } catch { return String(value); }
  }
  return String(value);
};
const list = value => Array.isArray(value) ? value : [];
const count = value => Number.isFinite(Number(value)) ? Math.max(0, Math.floor(Number(value))) : 0;
const scope = value => value == null ? '' : String(value);
export function memoryEditorProject(memory, currentProject = '') {
  // An existing Personal note has an intentional empty scope.
  return scope(memory == null ? currentProject : memory.project);
}
const humanize = value => text(value).replace(/[_-]+/g, ' ').replace(/^\w/, character => character.toUpperCase());
const SOURCE_LABELS = new Map(Object.entries({
  manual: 'Manual',
  automatic: 'Automatic',
  reviewed: 'Reviewed suggestion',
  imported_reference: 'Imported reference',
  'imported-reference': 'Imported reference',
  imported: 'Imported reference',
}));
const sourceKind = value => SOURCE_LABELS.get(value) || humanize(value) || 'Source not reported';

export class MemoryPanel {
  constructor({ root, get, post, getProject = () => '', onEdit, onAdd, onSettings } = {}) {
    if (!root || typeof root.append !== 'function' || !root.ownerDocument) {
      throw new TypeError('MemoryPanel needs a root element.');
    }
    if (typeof get !== 'function' || typeof post !== 'function' || typeof getProject !== 'function') {
      throw new TypeError('MemoryPanel needs get, post and getProject functions.');
    }
    this.root = root;
    this.document = root.ownerDocument;
    this.window = this.document.defaultView;
    this.get = get;
    this.post = post;
    this.getProject = getProject;
    this.onEdit = onEdit;
    this.onAdd = onAdd;
    this.onSettings = onSettings;
    this.prefix = `vega-memory-${++panelSequence}`;
    this.state = null;
    this.fresh = false;
    this.loading = false;
    this.busy = false;
    this.destroyed = false;
    this.loadVersion = 0;
    this.searchTimer = null;
    this.fileReader = null;
    this.file = null;
    this.preview = null;
    this.selected = new Set();
    this.page = 0;
    this.previewButtons = new Map();
    this.objectURLs = new Set();
    this.targetDraft = scope(getProject());
    this._build();
    this._controls();
  }

  _node(tag, content, className) {
    const element = this.document.createElement(tag);
    if (content !== undefined) element.textContent = text(content);
    if (className) element.className = className;
    return element;
  }

  _button(label, handler, className = '', focusKey) {
    const button = this._node('button', label, `mp-button ${className}`.trim());
    button.type = 'button';
    if (focusKey) button.dataset.memoryFocus = focusKey;
    button.addEventListener('click', () => { void handler(); });
    return button;
  }

  _field(label, control, name, hint) {
    const field = this._node('div', undefined, 'mp-field');
    control.id = `${this.prefix}-${name}`;
    control.name = name;
    const labelNode = this._node('label', label);
    labelNode.htmlFor = control.id;
    field.append(labelNode, control);
    if (hint) {
      const help = this._node('p', hint, 'mp-hint');
      help.id = `${control.id}-hint`;
      control.setAttribute('aria-describedby', help.id);
      field.append(help);
    }
    return field;
  }

  _options(select, options, preferred) {
    const fragment = this.document.createDocumentFragment();
    for (const [value, label] of options) {
      const option = this._node('option', label);
      option.value = value;
      fragment.append(option);
    }
    select.replaceChildren(fragment);
    select.value = preferred;
  }

  _live(className, alert = false) {
    const node = this._node('p', '', className);
    node.setAttribute('role', alert ? 'alert' : 'status');
    node.setAttribute('aria-atomic', 'true');
    return node;
  }

  _section(title, key, description) {
    const section = this._node('section', undefined, 'mp-section');
    section.dataset.memorySection = key;
    const heading = this._node('h3', title);
    heading.id = `${this.prefix}-${key}`;
    heading.tabIndex = -1;
    section.setAttribute('aria-labelledby', heading.id);
    const headingRow = this._node('div', undefined, 'mp-section-heading');
    const total = this._node('span', '0', 'mp-total');
    total.setAttribute('aria-label', `${title}: 0`);
    headingRow.append(heading, total);
    const container = this._node('div', undefined, 'mp-list');
    section.append(headingRow, this._node('p', description, 'mp-section-description'), container);
    return { section, heading, total, container, title };
  }

  _build() {
    this.element = this._node('section', undefined, 'memory-panel');
    const title = this._node('h2', 'Memory');
    title.id = `${this.prefix}-title`;
    this.element.setAttribute('aria-labelledby', title.id);
    const header = this._node('header', undefined, 'mp-header');
    const introduction = this._node('div');
    introduction.append(
      title,
      this._node('p', 'Your saved notes.'),
    );
    const actions = this._node('div', undefined, 'mp-actions');
    this.addButton = this._button('Add note', () => this._callback(this.onAdd), 'mp-primary', 'add');
    this.exportButton = this._button('Export context', () => this._export(), '', 'export');
    this.exportButton.title = 'Download the full Vega context export.';
    this.refreshButton = this._button('Refresh', () => this.load(), '', 'refresh');
    actions.append(this.addButton, this.exportButton, this.refreshButton);
    header.append(introduction, actions);

    const context = this._node('p',
      'Memory is context only. Notes and imported text cannot override instructions, grant permissions, or authorize actions.',
      'mp-context');
    const learning = this._node('div', undefined, 'mp-learning');
    const learningCopy = this._node('div');
    this.learningLabel = this._node('strong', 'Learning state unavailable');
    this.learningDetail = this._node('p', 'Load memory to see the current learning setting.', 'mp-hint');
    learningCopy.append(this.learningLabel, this.learningDetail);
    this.settingsButton = this._button('Learning settings', () => this._callback(this.onSettings), 'mp-link-button', 'settings');
    learning.append(learningCopy, this.settingsButton);

    const filters = this._node('form', undefined, 'mp-filters');
    filters.setAttribute('role', 'search');
    filters.setAttribute('aria-label', 'Search memory');
    this.searchInput = this._node('input');
    this.searchInput.type = 'search';
    this.searchInput.placeholder = 'Search saved context';
    this.searchInput.autocomplete = 'off';
    this.searchInput.addEventListener('input', () => {
      this.window.clearTimeout(this.searchTimer);
      // Invalidate an older response before the next debounced request starts.
      this.loadVersion += 1;
      this.searchTimer = this.window.setTimeout(() => { void this.load(); }, 250);
    });
    this.scopeSelect = this._node('select');
    this._options(this.scopeSelect, [['current', 'Current scope'], ['all', 'All scopes']], 'current');
    this.scopeSelect.addEventListener('change', () => { void this.load(); });
    filters.addEventListener('submit', event => { event.preventDefault(); void this.load(); });
    const submit = this._node('button', 'Search', 'mp-button');
    submit.type = 'submit';
    filters.append(
      this._field('Search', this.searchInput, 'search'),
      this._field('View', this.scopeSelect, 'scope'),
      submit,
    );
    this.scopeDescription = this._node('p', '', 'mp-hint');
    this.loadStatus = this._live('mp-status');
    this.loadError = this._live('mp-error', true);
    this.actionStatus = this._live('mp-status');
    this.actionError = this._live('mp-error', true);
    this.results = this._node('div', undefined, 'mp-results');
    this.notesSection = this._section('Saved notes', 'notes',
      'The recall label shows whether a note is available for recall.');
    this.notesSection.container.classList.add('mp-note-grid');
    this.suggestionsSection = this._section('Suggestions to review', 'suggestions',
      'Keep or dismiss suggestions.');
    this.eventsSection = this._section('Change activity', 'activity',
      'Review and undo changes.');
    this._scrollable(this.eventsSection.container, 'mp-history-list', 'Recent memory changes');
    this.results.append(this.notesSection.section, this.suggestionsSection.section, this.eventsSection.section);
    this._empty(this.notesSection.container, 'Load memory to see saved notes.');
    this._empty(this.suggestionsSection.container, 'Load memory to see suggestions.');
    this._empty(this.eventsSection.container, 'Load memory to see recent changes.');
    const memoryHelp = this._node('details', undefined, 'mp-disclosure mp-help');
    memoryHelp.append(this._node('summary', 'How memory works'), context);
    this.element.append(header, learning, filters, this.scopeDescription,
      this.loadStatus, this.loadError, this.actionStatus, this.actionError, this.results);
    this._buildImporter();
    this.importsSection = this._section('Import history', 'imports',
      'Review or undo imported batches.');
    this._scrollable(this.importsSection.container, 'mp-history-list', 'Earlier file imports');
    this._empty(this.importsSection.container, 'Load memory to see earlier imports.');
    this.element.append(this.importsSection.section);
    const disclosures = new Map();
    for (const [label, section, heading] of [
      ['Recent changes and undo', this.eventsSection.section, this.eventsSection.heading],
      ['Import notes or history', this.importSection, this.importHeading],
      ['Previous imports', this.importsSection.section, this.importsSection.heading],
    ]) {
      const details = this._node('details', undefined, 'mp-disclosure');
      details.append(this._node('summary', label));
      section.parentNode.insertBefore(details, section);
      details.append(section);
      disclosures.set(heading, details);
    }
    this.element.append(memoryHelp);
    const navigation = this._node('nav', undefined, 'mp-section-nav');
    navigation.setAttribute('aria-label', 'Memory sections');
    for (const [label, heading] of [
      ['Saved notes', this.notesSection.heading],
      ['Suggestions', this.suggestionsSection.heading],
      ['Activity', this.eventsSection.heading],
      ['Import a file', this.importHeading],
      ['Import history', this.importsSection.heading],
    ]) {
      const link = this._node('a', label);
      link.href = `#${heading.id}`;
      link.addEventListener('click', event => {
        event.preventDefault();
        if (disclosures.has(heading)) disclosures.get(heading).open = true;
        heading.focus();
        heading.scrollIntoView({ block: 'start' });
      });
      navigation.append(link);
    }
    this.element.insertBefore(navigation, this.results);
    this.root.append(this.element);
  }

  _scrollable(element, className, label) {
    element.classList.add(className);
    element.tabIndex = 0;
    element.setAttribute('aria-label', label);
    element.setAttribute('role', 'region');
  }

  _buildImporter() {
    this.importSection = this._node('section', undefined, 'mp-section mp-import');
    const title = this._node('h3', 'Review a file import');
    this.importHeading = title;
    title.tabIndex = -1;
    title.id = `${this.prefix}-import`;
    this.importSection.setAttribute('aria-labelledby', title.id);
    this.importSection.append(title, this._node('p',
      'Import a file you own. Processing stays on this PC.',
      'mp-section-description'));
    const formatsHelp = this._node('p',
      'Supported files: ChatGPT JSON or ZIP containing conversations.json; Codex JSONL or thread JSON; Claude JSON or ZIP; Vega context export; notes JSON, TXT or Markdown.',
      'mp-hint');
    this.importForm = this._node('form', undefined, 'mp-import-form');
    this.importForm.addEventListener('submit', event => { event.preventDefault(); void this._previewFile(); });
    this.importFields = this._node('fieldset');
    this.importFields.append(this._node('legend', 'File and destination'));
    const grid = this._node('div', undefined, 'mp-form-grid');
    this.fileInput = this._node('input');
    this.fileInput.type = 'file';
    this.fileInput.accept = '.json,.jsonl,.zip,.txt,.md,.markdown';
    this.fileInput.addEventListener('change', () => {
      this.file = this.fileInput.files?.[0] || null;
      if (!this.sourceInput.value.trim() && this.file) this.sourceInput.value = this.file.name.slice(0, 120);
      this.importError.textContent = '';
      this.importStatus.textContent = this.file ? 'File chosen. Create a preview to review its records.' : '';
      this._fileHint();
      this._controls();
    });
    this.providerSelect = this._node('select');
    this._options(this.providerSelect, [['', 'Load memory to see formats']], '');
    this.providerSelect.addEventListener('change', () => this._controls());
    this.sourceInput = this._node('input');
    this.sourceInput.type = 'text';
    this.sourceInput.maxLength = 120;
    this.sourceInput.placeholder = 'For example, work notes from June';
    this.targetSelect = this._node('select');
    this._projectOptions();
    this.targetSelect.addEventListener('change', () => {
      this.targetDraft = this.targetSelect.value;
      this._draftHint();
    });
    this.modeSelect = this._node('select');
    this._options(this.modeSelect, [
      ['suggestions', 'Useful memory candidates'],
      ['references', 'Visible reference text'],
    ], 'suggestions');
    grid.append(
      this._field('File from your device', this.fileInput, 'file'),
      this._field('File format', this.providerSelect, 'provider'),
      this._field('Source label', this.sourceInput, 'source-label', 'A name to keep with the imported records.'),
      this._field('Target scope in Vega', this.targetSelect, 'target',
        'This is where selected records will be saved. Source scopes stay in their provenance.'),
      this._field('Review style', this.modeSelect, 'mode',
        'Both styles require a preview and your selection before anything is added.'),
    );
    this.importFields.append(grid);
    this.fileHint = this._node('p', '', 'mp-hint');
    this.draftHint = this._node('p', '', 'mp-hint');
    this.previewButton = this._node('button', 'Preview file', 'mp-button mp-primary');
    this.previewButton.type = 'submit';
    this.previewButton.dataset.memoryFocus = 'preview-file';
    this.importForm.append(this.importFields, this.fileHint, this.draftHint, this.previewButton);
    this.importStatus = this._live('mp-status');
    this.importError = this._live('mp-error', true);
    this.previewRegion = this._node('div', undefined, 'mp-preview');
    this.previewRegion.hidden = true;
    this.previewHeading = this._node('h4', 'Review records');
    this.previewHeading.tabIndex = -1;
    this.previewHeading.id = `${this.prefix}-preview`;
    this.previewRegion.setAttribute('aria-labelledby', this.previewHeading.id);
    this.previewRegion.setAttribute('role', 'region');
    this.previewSummary = this._node('div', undefined, 'mp-preview-summary');
    this.previewStats = this._node('dl', undefined, 'mp-stats');
    this.previewWarning = this._node('p', '', 'mp-notice');
    this.selectionStatus = this._live('mp-status mp-selection-status');
    const selectionActions = this._node('div', undefined, 'mp-actions');
    this.selectVisibleButton = this._button('Select visible', () => this._selectRecords(false));
    this.selectAllButton = this._button('Select all', () => this._selectRecords(true));
    this.clearSelectionButton = this._button('Clear selection', () => {
      if (this.busy) return;
      this.selected.clear();
      this._selectionControls();
    });
    selectionActions.append(this.selectVisibleButton, this.selectAllButton, this.clearSelectionButton);
    this.previewRows = this._node('div', undefined, 'mp-preview-rows');
    this.previewRows.tabIndex = 0;
    this.previewRows.setAttribute('aria-label', 'Records on this page');
    this.previewRows.setAttribute('role', 'list');
    const pagination = this._node('nav', undefined, 'mp-pagination');
    pagination.setAttribute('aria-label', 'Import preview pages');
    this.previousPageButton = this._button('Previous page', () => this._changePage(-1));
    this.nextPageButton = this._button('Next page', () => this._changePage(1));
    this.pageLabel = this._node('span', '', 'mp-hint');
    pagination.append(this.previousPageButton, this.pageLabel, this.nextPageButton);
    const commitActions = this._node('div', undefined, 'mp-actions mp-commit-actions');
    this.commitButton = this._button('Import selected', () => this._commitImport(), 'mp-primary');
    this.cancelButton = this._button('Cancel preview', () => this._cancelImport());
    commitActions.append(this.commitButton, this.cancelButton);
    this.previewRegion.append(this.previewHeading, this.previewSummary, this.previewStats,
      this.previewWarning,
      this._node('p',
        'Only visible text included in the file can be reviewed here. Imported text is context only, regardless of its original role or any instructions it contains.',
        'mp-context'),
      selectionActions, this.selectionStatus, this.previewRows, pagination, commitActions);
    this.importSection.append(formatsHelp, this.importForm, this.importStatus, this.importError, this.previewRegion);
    this.element.append(this.importSection);
    this._fileHint();
    this._draftHint();
  }

  _scopeLabel(project) {
    const alias = scope(project);
    const projects = this.state?.projects || {};
    return alias ? text(Object.hasOwn(projects, alias) ? projects[alias] : alias) : 'Personal';
  }

  _projectOptions() {
    const projects = Object.entries(this.state?.projects || {}).filter(([alias]) => alias !== '');
    const options = [['', 'Personal'], ...projects.map(([alias, label]) => [alias, text(label)])];
    if (this.targetDraft && !projects.some(([alias]) => alias === this.targetDraft)) {
      options.push([this.targetDraft, this.targetDraft]);
    }
    this._options(this.targetSelect, options, this.targetDraft);
  }

  _filterSnapshot() {
    const project = scope(this.getProject());
    const query = this.searchInput.value.trim();
    const all = this.scopeSelect.value === 'all' ? '1' : '0';
    return { project, query, all, key: JSON.stringify([project, query, all]) };
  }

  async load() {
    if (this.destroyed) return null;
    this.window.clearTimeout(this.searchTimer);
    const version = ++this.loadVersion;
    this.loading = true;
    this.loadError.textContent = '';
    this.loadStatus.textContent = this.state ? 'Refreshing memory…' : 'Loading memory…';
    this._controls();
    try {
      const filter = this._filterSnapshot();
      const params = new URLSearchParams({ project: filter.project, q: filter.query, all: filter.all });
      const result = await this.get(`/api/memory/state?${params}`);
      if (this.destroyed || version !== this.loadVersion) return null;
      if (filter.key !== this._filterSnapshot().key) {
        this.fresh = false;
        this.loadStatus.textContent = 'The view changed. Refresh to load its memory.';
        return null;
      }
      if (!result || !Array.isArray(result.notes) || !Array.isArray(result.suggestions)
          || !Array.isArray(result.events) || !Array.isArray(result.imports)) {
        throw new Error('The memory response is incomplete. Refresh to try again.');
      }
      this.state = result;
      this.fresh = true;
      this._renderState(filter);
      this.loadStatus.textContent = `${result.notes.length} saved notes · ${result.suggestions.length} suggestions`;
      return result;
    } catch (error) {
      if (!this.destroyed && version === this.loadVersion) {
        this.fresh = false;
        this.loadStatus.textContent = '';
        this.loadError.textContent = this._errorMessage(error, 'Memory could not be loaded.')
          + (this.state ? ' The previous results remain visible. Refresh to try again.' : ' Use Refresh to try again.');
      }
      return null;
    } finally {
      if (!this.destroyed && version === this.loadVersion) {
        this.loading = false;
        this._controls();
      }
    }
  }

  _renderState(filter) {
    const focused = this._captureFocus();
    const learning = this.state.learning || {};
    if (typeof learning.enabled !== 'boolean') {
      this.learningLabel.textContent = 'Learning state unavailable';
      this.learningDetail.textContent = 'Open learning settings to review the current configuration.';
    } else if (!learning.enabled) {
      this.learningLabel.textContent = 'Learning is off';
      this.learningDetail.textContent = 'Saved notes remain available.';
    } else {
      this.learningLabel.textContent = 'Learning is on';
      this.learningDetail.textContent = learning.review_required === true
        ? 'Review required before saving.'
        : learning.review_required === false
          ? 'Eligible details save automatically · undo in Recent changes.'
          : 'The review setting was not reported. Open learning settings to check it.';
    }
    this.scopeDescription.textContent = filter.all === '1'
      ? 'All scopes.'
      : `Current scope: ${this._scopeLabel(filter.project)}. Personal context may also be included by Vega.`;
    this.scopeSelect.options[0].textContent = `Current scope — ${this._scopeLabel(filter.project)}`;
    this._renderNotes();
    this._renderSuggestions();
    this._renderEvents();
    this._renderImports();
    const formats = list(this.state.formats).filter(item => item && typeof item.id === 'string' && item.id);
    const preferred = this.providerSelect.value;
    const options = formats.map(item => [item.id, text(item.label || item.id)]);
    if (!options.length) options.push(['', 'No import formats available']);
    // Preserve a chosen format even if a later response no longer offers it.
    if (preferred && !options.some(([id]) => id === preferred)) {
      options.push([preferred, `${preferred} (currently unavailable)`]);
    }
    this._options(this.providerSelect, options, preferred || options[0][0]);
    this._projectOptions();
    this._fileHint();
    this._draftHint();
    // The file input, source label, mode, preview rows and selection are untouched.
    this._restoreFocus(focused);
  }

  _captureFocus() {
    const active = this.document.activeElement;
    if (!active || !this.element.contains(active)) return null;
    const section = active.closest('[data-memory-section]');
    return section ? { key: active.dataset.memoryFocus, section: section.dataset.memorySection } : null;
  }

  _restoreFocus(saved) {
    if (!saved) return;
    const match = [...this.element.querySelectorAll('[data-memory-focus]')]
      .find(element => element.dataset.memoryFocus === saved.key && !element.disabled
        && element.dataset.unavailable !== 'true');
    const section = [this.notesSection, this.suggestionsSection, this.eventsSection, this.importsSection]
      .find(item => item.section.dataset.memorySection === saved.section);
    (match || section?.heading)?.focus({ preventScroll: true });
  }

  _empty(container, message) {
    container.replaceChildren(this._node('p', message, 'mp-empty'));
  }

  _setCount(section, value) {
    section.total.textContent = String(value);
    section.total.setAttribute('aria-label', `${section.title}: ${value}`);
  }

  _badge(label, className = '') {
    return this._node('span', label, `mp-badge ${className}`.trim());
  }

  _body(value, threshold = 650) {
    const body = text(value);
    const container = this._node('div', undefined, 'mp-body');
    if (body.length <= threshold) {
      container.append(this._node('p', body || 'No text supplied.', 'mp-text'));
    } else {
      container.append(this._node('p', `${body.slice(0, threshold)}…`, 'mp-text'));
      const details = this._node('details', undefined, 'mp-details');
      details.append(this._node('summary', 'Read full text'), this._node('p', body, 'mp-text'));
      container.append(details);
    }
    return container;
  }

  _metadata(entries, className = 'mp-metadata') {
    const dl = this._node('dl', undefined, className);
    for (const [label, value] of entries) {
      const pair = this._node('div');
      pair.append(this._node('dt', label), this._node('dd', value));
      dl.append(pair);
    }
    return dl;
  }

  _provenance(provenance) {
    const container = this._node('div', undefined, 'mp-provenance');
    if (!provenance || typeof provenance !== 'object') {
      container.append(this._node('p', `Source: ${text(provenance) || 'Not reported'}`, 'mp-hint'));
      return container;
    }
    container.append(this._node('p', `Source: ${text(provenance.label || provenance.provider || 'Not reported')}`, 'mp-hint'));
    const entries = Object.entries(provenance).filter(([key]) => key !== 'label' && key !== 'authority');
    if (entries.length) {
      const details = this._node('details', undefined, 'mp-details');
      details.append(this._node('summary', 'Source details'), this._metadata(entries.map(([key, value]) => [
        key === 'original_scope' ? 'Source scope (from file)' : humanize(key),
        value == null || value === '' ? 'Not specified' : text(value),
      ])));
      container.append(details);
    }
    return container;
  }

  _recordHeader(record) {
    const badges = this._node('div', undefined, 'mp-badges');
    badges.append(this._badge(sourceKind(record.source_kind)), this._badge(this._scopeLabel(record.project)));
    return badges;
  }

  _renderNotes() {
    const notes = this.state.notes;
    this._setCount(this.notesSection, notes.length);
    this.notesSection.container.replaceChildren();
    for (const note of notes) {
      const article = this._node('article', undefined, 'mp-card');
      const badges = this._recordHeader(note);
      badges.append(this._badge(note.recall_allowed === true ? 'Available for recall'
        : note.recall_allowed === false ? 'Recall off' : 'Recall not reported', 'mp-badge-subtle'));
      article.append(badges, this._node('h4', note.title || 'Untitled note'), this._body(note.body), this._provenance(note.provenance));
      const footer = this._node('div', undefined, 'mp-card-footer');
      footer.append(this._node('span', `Revision ${text(note.revision ?? '—')}`, 'mp-hint'));
      const edit = this._button('Review / edit', () => this._callback(this.onEdit, note), '', `note:${note.id}`);
      edit.dataset.memoryMutation = 'edit';
      edit.disabled = typeof this.onEdit !== 'function';
      if (edit.disabled) edit.title = 'The manual note editor is not connected.';
      footer.append(edit);
      article.append(footer);
      this.notesSection.container.append(article);
    }
    if (!notes.length) this._empty(this.notesSection.container, 'No matching notes.');
  }

  _renderSuggestions() {
    const suggestions = this.state.suggestions;
    this._setCount(this.suggestionsSection, suggestions.length);
    this.suggestionsSection.container.replaceChildren();
    for (const suggestion of suggestions) {
      const article = this._node('article', undefined, 'mp-card');
      const badges = this._node('div', undefined, 'mp-badges');
      badges.append(this._badge('Awaiting review', 'mp-badge-review'), this._badge(this._scopeLabel(suggestion.project)));
      const actions = this._node('div', undefined, 'mp-actions');
      for (const [label, accept] of [['Keep', true], ['Dismiss', false]]) {
        const button = this._button(label, () => this._review(suggestion, accept),
          accept ? 'mp-primary' : '', `suggestion:${suggestion.id}:${accept}`);
        button.dataset.memoryMutation = 'review';
        button.setAttribute('aria-label', `${label}: ${text(suggestion.title || 'Untitled suggestion')}`);
        actions.append(button);
      }
      article.append(badges, this._node('h4', suggestion.title || 'Untitled suggestion'),
        this._body(suggestion.body), this._provenance(suggestion.provenance),
        this._node('p', `Review reason: ${text(suggestion.review_reason) || 'Confirm this detail is useful and correctly scoped.'}`, 'mp-hint'),
        actions);
      this.suggestionsSection.container.append(article);
    }
    if (!suggestions.length) this._empty(this.suggestionsSection.container, 'No suggestions waiting for review in this view.');
  }

  _date(value) {
    if (value == null || value === '') return 'Time not reported';
    const date = new Date(typeof value === 'number' && value < 1e12 ? value * 1000 : value);
    return Number.isNaN(date.valueOf()) ? text(value) : date.toLocaleString();
  }

  _renderEvents() {
    const events = this.state.events;
    this._setCount(this.eventsSection, events.length);
    this.eventsSection.container.replaceChildren();
    for (const event of events) {
      const row = this._node('article', undefined, 'mp-activity-row');
      const copy = this._node('div');
      const badges = this._recordHeader(event);
      if (event.undone) badges.append(this._badge('Undone', 'mp-badge-subtle'));
      copy.append(badges, this._node('h4', event.title || 'Memory change'),
        this._node('p', `${humanize(event.action) || 'Changed'} · Revision ${text(event.revision ?? '—')} · ${this._date(event.created_at)}`, 'mp-hint'));
      const undo = this._button(event.undone ? 'Undone' : 'Undo', () => this._undoEvent(event), '', `event:${event.id}`);
      undo.dataset.memoryMutation = 'undo';
      undo.dataset.unavailable = event.undone ? 'true' : 'false';
      undo.setAttribute('aria-label', `${event.undone ? 'Already undone' : 'Undo change'}: ${text(event.title || 'Memory change')}`);
      row.append(copy, undo);
      this.eventsSection.container.append(row);
    }
    if (!events.length) this._empty(this.eventsSection.container, 'No change activity in this view.');
  }

  _renderImports() {
    const imports = this.state.imports;
    this._setCount(this.importsSection, imports.length);
    this.importsSection.container.replaceChildren();
    for (const item of imports) {
      const metadata = item.metadata || {};
      const result = item.result || {};
      const row = this._node('article', undefined, 'mp-activity-row');
      const copy = this._node('div');
      const badges = this._node('div', undefined, 'mp-badges');
      badges.append(this._badge('Imported reference'), this._badge(`Target: ${this._scopeLabel(metadata.project)}`));
      if (item.undone) badges.append(this._badge('Undone', 'mp-badge-subtle'));
      copy.append(badges, this._node('h4', item.source_label || metadata.filename || 'File import'),
        this._node('p', `${count(result.added)} added · ${count(result.duplicates)} duplicates · ${this._date(item.created_at)}`, 'mp-hint'));
      const details = this._node('details', undefined, 'mp-details');
      details.append(this._node('summary', 'Import details'), this._metadata([
        ['File', metadata.filename || 'Not reported'],
        ['Source format', metadata.provider || 'Not reported'],
        ['Source label', item.source_label || 'Not reported'],
        ['Target scope in Vega', this._scopeLabel(metadata.project)],
        ['Batch id', item.id],
        ['Changes', list(result.event_ids).length],
      ]));
      if (metadata.stats && typeof metadata.stats === 'object') {
        details.append(this._metadata(this._statEntries(metadata.stats)));
      }
      copy.append(details);
      const undo = this._button(item.undone ? 'Undone' : 'Undo import', () => this._undoImport(item), '', `batch:${item.id}`);
      undo.dataset.memoryMutation = 'undo-import';
      undo.dataset.unavailable = item.undone ? 'true' : 'false';
      undo.setAttribute('aria-label', `${item.undone ? 'Already undone' : 'Undo import'}: ${text(item.source_label || metadata.filename || 'File import')}`);
      row.append(copy, undo);
      this.importsSection.container.append(row);
    }
    if (!imports.length) this._empty(this.importsSection.container, 'No file imports yet. Reviewed imports will appear here.');
  }

  _uploadLimit() {
    const limit = Number(this.state?.max_upload_bytes);
    return Number.isFinite(limit) && limit > 0 ? limit : DEFAULT_UPLOAD_LIMIT;
  }

  _fileHint() {
    const limit = (this._uploadLimit() / 1024 / 1024).toLocaleString(undefined, { maximumFractionDigits: 1 });
    const chosen = this.file ? ` Chosen: ${this.file.name} (${this.file.size.toLocaleString()} bytes).` : '';
    this.fileHint.textContent = `Maximum file size: ${limit} MB.${chosen}`;
  }

  _draftHint() {
    this.draftHint.textContent = this.preview
      ? `This preview is fixed to ${this._scopeLabel(this.preview.project)}. Cancel it to change the file, review style, or target.`
      : `Import target: ${this._scopeLabel(this.targetDraft)}. Changing the view above does not change this destination.`;
  }

  _errorMessage(error, fallback) {
    return text(error?.message || (typeof error === 'string' ? error : '')) || fallback;
  }

  async _run(message, work, { importer = false, refresh = false } = {}) {
    if (this.busy || this.destroyed) return null;
    this.busy = true;
    const status = importer ? this.importStatus : this.actionStatus;
    const errorNode = importer ? this.importError : this.actionError;
    status.textContent = message;
    errorNode.textContent = '';
    this._controls();
    try {
      return await work(status);
    } catch (error) {
      if (!this.destroyed) {
        status.textContent = '';
        errorNode.textContent = this._errorMessage(error, 'The request could not be completed.');
        errorNode.scrollIntoView({ block: 'nearest' });
      }
      return null;
    } finally {
      if (!this.destroyed) {
        // A failed write may still have reached the host. Reload without replaying it.
        if (refresh) {
          this.fresh = false;
          await this.load();
        }
        if (!this.destroyed) {
          this.busy = false;
          this._controls();
        }
      }
    }
  }

  async _callback(callback, value) {
    if (typeof callback !== 'function' || this.busy || this.destroyed) return;
    this.actionError.textContent = '';
    try { await callback(value); }
    catch (error) { if (!this.destroyed) this.actionError.textContent = this._errorMessage(error, 'The requested view could not be opened.'); }
  }

  _review(suggestion, accept) {
    if (!this.fresh || this.loading) return;
    return this._run(accept ? 'Keeping suggestion…' : 'Dismissing suggestion…', async status => {
      await this.post('/api/memory/candidate/review', { id: suggestion.id, accept });
      if (!this.destroyed) status.textContent = accept ? 'Suggestion kept.' : 'Suggestion dismissed.';
    }, { refresh: true });
  }

  _undoEvent(event) {
    if (!this.fresh || this.loading || event.undone) return;
    return this._run('Undoing change…', async status => {
      await this.post('/api/memory/undo', { event_id: event.id });
      if (!this.destroyed) status.textContent = 'Change undone.';
    }, { refresh: true });
  }

  _undoImport(item) {
    if (!this.fresh || this.loading || item.undone) return;
    return this._run('Undoing import…', async status => {
      await this.post('/api/memory/import/undo', { batch_id: item.id });
      if (!this.destroyed) status.textContent = 'Import undone.';
    }, { refresh: true });
  }

  _readFile(file) {
    return new Promise((resolve, reject) => {
      const reader = new this.window.FileReader();
      this.fileReader = reader;
      const clear = () => { if (this.fileReader === reader) this.fileReader = null; };
      reader.onload = () => {
        clear();
        const encoded = text(reader.result);
        const separator = encoded.indexOf(',');
        if (separator < 0) reject(new Error('The file could not be read. Choose it again.'));
        else resolve(encoded.slice(separator + 1));
      };
      reader.onerror = () => { clear(); reject(new Error('The file could not be read. Choose it again.')); };
      reader.onabort = () => { clear(); reject(new Error('File reading was cancelled.')); };
      reader.readAsDataURL(file);
    });
  }

  _previewFile() {
    if (this.preview || !this.fresh || this.loading) return;
    return this._run('Reading file and preparing a preview…', async status => {
      const file = this.file;
      if (!file) throw new Error('Choose a file before creating a preview.');
      if (!/\.(json|jsonl|zip|txt|md|markdown)$/i.test(file.name)) {
        throw new Error('Choose a JSON, JSONL, ZIP, TXT or Markdown file.');
      }
      if (!file.size) throw new Error('This file is empty. Choose a file with visible text.');
      if (file.size > this._uploadLimit()) throw new Error('This file exceeds the size limit shown above.');
      if (!list(this.state?.formats).some(format => format?.id === this.providerSelect.value)) {
        throw new Error('Choose one of the available file formats.');
      }
      const payload = {
        filename: file.name,
        provider: this.providerSelect.value,
        project: this.targetSelect.value || null,
        source_label: this.sourceInput.value.trim() || file.name.slice(0, 120),
        mode: this.modeSelect.value,
      };
      payload.data_base64 = await this._readFile(file);
      if (this.destroyed) return;
      const result = await this.post('/api/memory/import/preview', payload);
      if (this.destroyed) return;
      if (typeof result?.preview_id !== 'string' || !result.preview_id) {
        throw new Error('Vega did not return a usable preview. No records were selected or committed.');
      }
      const ids = new Set();
      let valid = Array.isArray(result.records);
      for (const record of list(result.records)) {
        if (!record || typeof record.id !== 'string' || !record.id || ids.has(record.id)) {
          valid = false;
          break;
        }
        ids.add(record.id);
      }
      // Keep a known preview id cancellable even if its record list is invalid.
      this.preview = valid ? result : { ...result, records: [] };
      this.selected = new Set();
      this.page = 0;
      this._renderPreview();
      this.previewHeading.focus();
      if (!valid) throw new Error('This preview has missing or repeated record ids. Cancel this preview, then try again.');
      status.textContent = `${result.records.length} records ready to review. Nothing is selected yet.`;
    }, { importer: true });
  }

  _statEntries(stats = {}) {
    return [
      ['Visible records', count(stats.visible_records)],
      ['Non-visible records excluded', count(stats.non_visible)],
      ['Sensitive content filtered', count(stats.sensitive)],
      ['Invalid records', count(stats.invalid_records)],
      ['Duplicates detected', count(stats.duplicates)],
      ['Ambiguous branches', count(stats.ambiguous_branches)],
      ['Incidental content', count(stats.incidental)],
      ['Record limit reached', stats.limit_reached ? 'Yes' : 'No'],
    ];
  }

  _renderPreview() {
    const preview = this.preview;
    this.previewRegion.hidden = !preview;
    if (!preview) return;
    this.previewSummary.replaceChildren(this._metadata([
      ['Source file', preview.filename],
      ['Source format', preview.provider],
      ['Source label', preview.source_label],
      ['Source scopes', 'Shown on each record exactly as supplied by the file.'],
      ['Target scope in Vega', this._scopeLabel(preview.project)],
      ['Archive member', preview.archive_member || 'Not an archive member'],
    ]));
    this.previewStats.replaceChildren(...this._metadata(this._statEntries(preview.stats)).children);
    this.previewWarning.textContent = preview.stats?.limit_reached
      ? 'The record limit was reached. This preview covers only part of the file. Review a smaller export for the remaining content.'
      : '';
    this._draftHint();
    this._renderPreviewRows();
  }

  _visibleRecords() {
    return this.preview ? this.preview.records.slice(this.page * PAGE_SIZE, (this.page + 1) * PAGE_SIZE) : [];
  }

  _renderPreviewRows() {
    this.previewRows.replaceChildren();
    this.previewButtons.clear();
    for (const [index, record] of this._visibleRecords().entries()) {
      const row = this._node('article', undefined, 'mp-preview-row');
      row.setAttribute('role', 'listitem');
      const copy = this._node('div', undefined, 'mp-preview-copy');
      const number = this.page * PAGE_SIZE + index + 1;
      const title = text(record.title || `Record ${number}`);
      const badges = this._node('div', undefined, 'mp-badges');
      badges.append(this._badge(`Record ${number}`), this._badge('Context only', 'mp-badge-subtle'));
      copy.append(badges, this._node('h5', title), this._body(record.text, 450),
        this._metadata([
          ['Source format', record.provider || this.preview.provider],
          ['Original role', record.role || 'Not specified'],
          ['Source record id', record.source_id == null || record.source_id === '' ? 'Not specified' : record.source_id],
          ['Source scope (from file)', record.original_scope == null || record.original_scope === ''
            ? 'Not specified in file' : text(record.original_scope)],
          ['Target scope in Vega', this._scopeLabel(this.preview.project)],
        ]));
      const button = this._button('Select', () => {
        if (this.busy || !this.preview) return;
        if (this.selected.has(record.id)) this.selected.delete(record.id);
        else this.selected.add(record.id);
        this._selectionControls();
      }, 'mp-select-record');
      button.setAttribute('aria-label', `Include record ${number}: ${title}`);
      button.setAttribute('aria-pressed', 'false');
      this.previewButtons.set(record.id, { button, row });
      row.append(copy, button);
      this.previewRows.append(row);
    }
    if (!this.preview?.records.length) {
      this._empty(this.previewRows, 'No records are available to import. Review the file format and the counts above, then cancel to choose another file.');
    }
    this._selectionControls();
    this.previewRows.scrollTop = 0;
  }

  _selectRecords(all) {
    if (!this.preview || this.busy) return;
    for (const record of all ? this.preview.records : this._visibleRecords()) this.selected.add(record.id);
    this._selectionControls();
  }

  _changePage(direction) {
    if (!this.preview || this.busy) return;
    const pages = Math.max(1, Math.ceil(this.preview.records.length / PAGE_SIZE));
    this.page = Math.max(0, Math.min(pages - 1, this.page + direction));
    this._renderPreviewRows();
    this.previewHeading.focus({ preventScroll: true });
  }

  _selectionControls() {
    const total = this.preview?.records.length || 0;
    const pages = Math.max(1, Math.ceil(total / PAGE_SIZE));
    const first = total ? this.page * PAGE_SIZE + 1 : 0;
    const last = Math.min(total, (this.page + 1) * PAGE_SIZE);
    this.selectionStatus.textContent = `Showing ${first}–${last} of ${total} records. ${this.selected.size} selected across all pages.`;
    this.pageLabel.textContent = `Page ${this.page + 1} of ${pages}`;
    for (const [id, { button, row }] of this.previewButtons) {
      const selected = this.selected.has(id);
      button.textContent = selected ? 'Selected' : 'Select';
      button.setAttribute('aria-pressed', String(selected));
      button.disabled = this.busy;
      row.dataset.selected = String(selected);
    }
    this.selectVisibleButton.textContent = `Select visible (${this._visibleRecords().length})`;
    this.selectAllButton.textContent = `Select all (${total})`;
    this.selectVisibleButton.disabled = this.busy || !total || this._visibleRecords().every(record => this.selected.has(record.id));
    this.selectAllButton.disabled = this.busy || !total || this.selected.size === total;
    this.clearSelectionButton.disabled = this.busy || !this.selected.size;
    this.previousPageButton.disabled = this.busy || this.page === 0;
    this.nextPageButton.disabled = this.busy || this.page >= pages - 1;
    this.commitButton.textContent = `Import selected (${this.selected.size})`;
    this.commitButton.disabled = this.busy || !this.preview || !this.selected.size;
    this.cancelButton.disabled = this.busy || !this.preview;
  }

  _resetPreview() {
    this.preview = null;
    this.selected.clear();
    this.page = 0;
    this.previewButtons.clear();
    this.previewRows.replaceChildren();
    this.previewRegion.hidden = true;
    this._draftHint();
    this.importHeading.focus();
  }

  _commitImport() {
    if (!this.preview || !this.selected.size) return;
    const previewId = this.preview.preview_id;
    // Only ids explicitly selected by the owner can reach commit.
    const selectedIds = this.preview.records.filter(record => this.selected.has(record.id)).map(record => record.id);
    if (!selectedIds.length) return;
    return this._run('Importing selected records…', async status => {
      const result = await this.post('/api/memory/import/commit', { preview_id: previewId, selected_ids: selectedIds });
      if (this.destroyed) return;
      if (typeof result?.batch_id !== 'string' || !result.batch_id) {
        throw new Error('The host did not confirm the import batch. Review import history before retrying. Your selection is still available.');
      }
      this._resetPreview();
      status.textContent = `Import complete: ${count(result?.added)} added, ${count(result?.duplicates)} duplicates. See import history to undo this batch.`;
    }, { importer: true, refresh: true });
  }

  _cancelImport() {
    if (!this.preview) return;
    const previewId = this.preview.preview_id;
    return this._run('Cancelling preview…', async status => {
      await this.post('/api/memory/import/cancel', { preview_id: previewId });
      if (this.destroyed) return;
      this._resetPreview();
      status.textContent = 'Preview cancelled. Your file and form draft are still available.';
    }, { importer: true });
  }

  _export() {
    return this._run('Preparing context export…', async status => {
      const result = await this.get('/api/export');
      if (this.destroyed) return;
      const blob = new this.window.Blob([JSON.stringify(result, null, 2)], { type: 'application/json;charset=utf-8' });
      const url = this.window.URL.createObjectURL(blob);
      this.objectURLs.add(url);
      const anchor = this._node('a', 'Download Vega context');
      anchor.href = url;
      anchor.download = `vega-context-${new Date().toISOString().slice(0, 10)}.json`;
      anchor.hidden = true;
      this.element.append(anchor);
      anchor.click();
      anchor.remove();
      this.window.setTimeout(() => {
        this.window.URL.revokeObjectURL(url);
        this.objectURLs.delete(url);
      }, 60000);
      status.textContent = 'Context export download requested.';
    });
  }

  _controls() {
    const stale = !this.fresh || this.loading;
    this.refreshButton.disabled = this.busy || this.loading;
    this.exportButton.disabled = this.busy;
    this.addButton.disabled = this.busy || typeof this.onAdd !== 'function';
    this.settingsButton.disabled = this.busy || typeof this.onSettings !== 'function';
    if (typeof this.onAdd !== 'function') this.addButton.title = 'The manual note editor is not connected.';
    if (typeof this.onSettings !== 'function') this.settingsButton.title = 'Learning settings navigation is not connected.';
    this.results.setAttribute('aria-busy', String(this.loading));
    for (const button of this.element.querySelectorAll('[data-memory-mutation]')) {
      button.disabled = this.busy || stale || button.dataset.unavailable === 'true'
        || (button.dataset.memoryMutation === 'edit' && typeof this.onEdit !== 'function');
    }
    this.importFields.disabled = this.busy || Boolean(this.preview);
    const available = list(this.state?.formats).some(format => format?.id === this.providerSelect.value);
    this.previewButton.disabled = this.busy || stale || Boolean(this.preview) || !this.file || !available;
    this.previewButton.hidden = Boolean(this.preview);
    this._selectionControls();
  }

  destroy() {
    if (this.destroyed) return;
    this.destroyed = true;
    this.loadVersion += 1;
    this.window.clearTimeout(this.searchTimer);
    this.fileReader?.abort();
    for (const url of this.objectURLs) this.window.URL.revokeObjectURL(url);
    this.objectURLs.clear();
    this.file = null;
    this.preview = null;
    this.selected.clear();
    this.element.remove();
  }
}
