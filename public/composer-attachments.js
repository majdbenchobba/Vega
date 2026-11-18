const MiB = 1024 * 1024;
export const ATTACHMENT_LIMITS = Object.freeze({ fileBytes: 10 * MiB, count: 5, totalBytes: 25 * MiB });
const UUID = /^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/i;
const HASH = /^[a-f0-9]{64}$/i;
const IMAGE_MIMES = new Set(['image/png', 'image/jpeg', 'image/webp', 'image/gif']);
const STORAGE_KEY = 'vega-attachment-draft-v1';
const rejected = error => [400, 403, 404, 413, 415, 422].includes(error?.status);
const sizeLabel = size => size >= MiB ? `${(size / MiB).toFixed(1)} MB` : `${Math.max(1, Math.ceil(size / 1024))} KB`;

export function attachmentMetadata(value) {
  if (!value || !UUID.test(value.id) || typeof value.name !== 'string' || !value.name
    || typeof value.mime_type !== 'string' || !Number.isSafeInteger(value.size) || value.size < 0
    || value.size > ATTACHMENT_LIMITS.fileBytes || !HASH.test(value.sha256)
    || !['image', 'file', 'text', 'document'].includes(value.kind) || typeof value.state !== 'string') return null;
  // Only this attachment's local image endpoint can become an image source.
  const preview = value.kind === 'image' && IMAGE_MIMES.has(value.mime_type)
    && value.preview_url === `/api/attachments/${value.id}/content` ? value.preview_url : null;
  return { id: value.id, name: value.name, mime_type: value.mime_type, size: value.size,
    kind: value.kind, state: value.state, preview_url: preview, sha256: value.sha256.toLowerCase() };
}

export function checkAttachmentSelection(files, existing = []) {
  if (files.length + existing.length > ATTACHMENT_LIMITS.count) throw new Error('Attach up to 5 files per message.');
  if (files.some(file => !Number.isSafeInteger(file.size) || file.size < 0 || file.size > ATTACHMENT_LIMITS.fileBytes)) {
    throw new Error('Each file must be 10 MB or smaller.');
  }
  if ([...files, ...existing].reduce((total, file) => total + file.size, 0) > ATTACHMENT_LIMITS.totalBytes) {
    throw new Error('Keep the files in one message under 25 MB combined.');
  }
}

function base64(bytes) {
  let binary = '';
  for (let offset = 0; offset < bytes.length; offset += 32768) {
    binary += String.fromCharCode(...bytes.subarray(offset, offset + 32768));
  }
  return btoa(binary);
}

// This journal stores metadata and retry identities, never base64 file contents.
// A failed upload restored after reload asks for the same file and verifies its
// hash before reusing its request ID. Restoring never performs an API mutation.
export class AttachmentDraft {
  constructor({ post, enabled = () => false, storage, crypto = globalThis.crypto, onChange = () => {} }) {
    this.post = post; this.enabled = enabled; this.storage = storage; this.crypto = crypto; this.onChange = onChange;
    this.entries = []; this.locked = false;
    try {
      const saved = JSON.parse(storage?.getItem(STORAGE_KEY) || '[]');
      if (!Array.isArray(saved)) return;
      const keys = new Set();
      for (const item of saved.slice(0, ATTACHMENT_LIMITS.count)) {
        if (!item || !UUID.test(item.key) || keys.has(item.key) || typeof item.name !== 'string'
          || typeof item.mime_type !== 'string' || !Number.isSafeInteger(item.size) || item.size < 0
          || item.size > ATTACHMENT_LIMITS.fileBytes) continue;
        keys.add(item.key);
        const attachment = attachmentMetadata(item.attachment);
        const ready = attachment?.state === 'staged' && item.phase === 'ready' && !item.removeRequested;
        this.entries.push({ key: item.key, name: item.name, mime_type: item.mime_type, size: item.size,
          sha256: HASH.test(item.sha256) ? item.sha256.toLowerCase() : null, attachment,
          removeRequested: Boolean(item.removeRequested), uncertain: Boolean(item.uncertain),
          phase: ready ? 'ready' : item.removeRequested ? 'remove_error' : 'upload_error',
          error: ready ? '' : attachment ? 'Removal was not confirmed. Retry.' : 'Choose the same file to retry.',
          busy: false, file: null });
      }
      checkAttachmentSelection([], this.entries);
    } catch { this.entries = []; }
  }
  snapshot() {
    return this.entries.map(({ key, name, mime_type, size, sha256, attachment, phase, error, uncertain, removeRequested }) =>
      ({ key, name, mime_type, size, sha256, attachment, phase, error, uncertain, removeRequested }));
  }
  changed() {
    try { this.storage?.setItem(STORAGE_KEY, JSON.stringify(this.snapshot())); } catch {}
    this.onChange();
  }
  available() {
    if (this.locked) throw new Error('Retry the pending message before changing its files.');
    if (!this.enabled()) throw new Error('Attachments will be available after Vega updates.');
  }
  get ready() { return this.entries.every(item => item.phase === 'ready' && item.attachment?.state === 'staged'); }
  get ids() { return this.entries.filter(item => item.phase === 'ready').map(item => item.attachment.id); }
  async add(files) {
    this.available();
    files = Array.from(files);
    checkAttachmentSelection(files, this.entries);
    const added = files.map(file => ({
      key: this.crypto.randomUUID(), name: file.name, mime_type: file.type || 'application/octet-stream', size: file.size,
      file, sha256: null, attachment: null, phase: 'uploading', error: '', uncertain: false, removeRequested: false, busy: false,
    }));
    this.entries.push(...added); this.changed();
    await Promise.all(added.map(item => this.upload(item, item.file)));
  }
  async upload(item, file) {
    if (item.busy) return;
    const previouslyUncertain = item.uncertain;
    item.busy = true; item.phase = item.removeRequested ? 'removing' : 'uploading'; item.error = ''; this.changed();
    try {
      if (file.name !== item.name || file.size !== item.size || (file.type || 'application/octet-stream') !== item.mime_type) {
        throw new Error('Choose the original file to retry this upload.');
      }
      const bytes = new Uint8Array(await file.arrayBuffer());
      if (bytes.length !== item.size) throw new Error('The file changed. Choose the original file to retry.');
      const digest = [...new Uint8Array(await this.crypto.subtle.digest('SHA-256', bytes))]
        .map(value => value.toString(16).padStart(2, '0')).join('');
      if (item.sha256 && digest !== item.sha256) throw new Error('The file contents changed. Choose the original file to retry.');
      item.file = file; item.sha256 = digest;
      // Persist the exact identity before a request whose response can be lost.
      item.uncertain = true; this.changed();
      const data = await this.post('/api/attachments', {
        request_id: item.key, name: item.name, mime_type: item.mime_type, data_base64: base64(bytes),
      });
      const attachment = attachmentMetadata(data?.attachment);
      if (!attachment || attachment.size !== item.size || attachment.sha256 !== digest || attachment.state !== 'staged') {
        throw new Error('The upload receipt did not match this file. Retry to check it.');
      }
      item.attachment = attachment; item.uncertain = false; item.phase = 'ready'; item.error = '';
    } catch (error) {
      if (rejected(error) && !previouslyUncertain) item.uncertain = false;
      item.phase = item.removeRequested ? 'remove_error' : 'upload_error';
      item.error = error.message || 'Upload not confirmed. Retry.';
    } finally { item.busy = false; this.changed(); }
    if (item.removeRequested && item.attachment) await this.removeUploaded(item);
  }
  async retry(key, file) {
    this.available();
    const item = this.entries.find(entry => entry.key === key);
    if (!item || item.busy) return;
    if (item.attachment && item.removeRequested) return this.removeUploaded(item);
    if (!file && !item.file) return { needsFile: true };
    return this.upload(item, file || item.file);
  }
  async remove(key) {
    this.available();
    const item = this.entries.find(entry => entry.key === key);
    if (!item || item.busy) return;
    if (!item.attachment && !item.uncertain) {
      this.entries = this.entries.filter(entry => entry !== item); this.changed(); return;
    }
    item.removeRequested = true; this.changed();
    if (item.attachment) return this.removeUploaded(item);
    // Resolve an uncertain upload with its same identity before removing it.
    if (item.file) return this.upload(item, item.file);
    item.phase = 'remove_error'; item.error = 'Choose the same file to finish removing it.'; this.changed();
    return { needsFile: true };
  }
  async removeUploaded(item) {
    if (item.busy) return;
    item.busy = true; item.phase = 'removing'; item.error = ''; this.changed();
    try {
      await this.post(`/api/attachments/${item.attachment.id}/remove`, {});
      this.entries = this.entries.filter(entry => entry !== item);
    } catch (error) {
      item.phase = 'remove_error'; item.error = error.message || 'Removal not confirmed. Retry.';
    } finally { item.busy = false; this.changed(); }
  }
  committed(ids) {
    this.entries = this.entries.filter(item => !ids.includes(item.attachment?.id));
    this.changed();
  }
}

function attachmentVisual(doc, attachment, name) {
  const visual = doc.createElement(attachment?.preview_url ? 'img' : 'span');
  visual.className = attachment?.preview_url ? 'attachment-thumbnail' : 'attachment-file-icon';
  if (attachment?.preview_url) {
    visual.src = attachment.preview_url; visual.alt = name; visual.loading = 'lazy'; visual.decoding = 'async';
    visual.onerror = () => { visual.hidden = true; };
  } else {
    visual.textContent = '▤'; visual.setAttribute('aria-hidden', 'true');
  }
  return visual;
}

export function renderMessageAttachments(root, values) {
  const attachments = (Array.isArray(values) ? values : []).slice(0, ATTACHMENT_LIMITS.count)
    .map(attachmentMetadata).filter(item => item && item.state !== 'removed');
  const signature = JSON.stringify(attachments);
  if (root.dataset.signature === signature) return;
  root.dataset.signature = signature; root.replaceChildren(); root.hidden = !attachments.length;
  for (const attachment of attachments) {
    const chip = root.ownerDocument.createElement('div'); chip.className = 'message-attachment';
    const name = root.ownerDocument.createElement('span'); name.textContent = attachment.name;
    const size = root.ownerDocument.createElement('small'); size.textContent = sizeLabel(attachment.size);
    chip.append(attachmentVisual(root.ownerDocument, attachment, attachment.name), name, size);
    root.append(chip);
  }
}

export function initComposerAttachments({ composer, list, picker, addButton, status, post, enabled, onChange = () => {} }) {
  const doc = composer.ownerDocument, win = doc.defaultView;
  let retryKey = null, signature = '', storage;
  try { storage = win.sessionStorage; } catch {}
  const draft = new AttachmentDraft({ post, enabled, storage, crypto: win.crypto,
    onChange: () => { render(); onChange(); } });
  const showError = error => { status.textContent = error?.message || String(error); };
  const chooseRetry = key => {
    retryKey = key; picker.multiple = false; picker.value = '';
    status.textContent = 'Choose the same file to retry.'; picker.click();
  };
  const perform = async action => {
    try { await action(); } catch (error) { showError(error); }
  };
  const button = (label, text, key, action) => {
    const node = doc.createElement('button'); node.type = 'button'; node.textContent = text;
    node.setAttribute('aria-label', label); node.title = label; node.dataset.attachmentKey = key;
    node.onclick = () => perform(action); return node;
  };
  function render() {
    const supported = Boolean(enabled());
    addButton.disabled = draft.locked || !supported || draft.entries.length >= ATTACHMENT_LIMITS.count;
    addButton.title = supported ? 'Attach files' : 'Attachments available after Vega updates';
    const next = JSON.stringify([draft.snapshot(), draft.entries.map(item => item.busy), draft.locked, supported]);
    if (next === signature) return;
    signature = next;
    const focused = doc.activeElement?.dataset.attachmentKey;
    const focusLabel = doc.activeElement?.getAttribute('aria-label');
    list.replaceChildren(); list.hidden = !draft.entries.length;
    for (const item of draft.entries) {
      const chip = doc.createElement('div'); chip.className = 'attachment-chip'; chip.dataset.phase = item.phase;
      const copy = doc.createElement('div'); copy.className = 'attachment-copy';
      const name = doc.createElement('span'); name.textContent = item.name; name.title = item.name;
      const detail = doc.createElement('small');
      detail.textContent = item.error || ({ ready: sizeLabel(item.size), uploading: 'Uploading…', removing: 'Removing…' })[item.phase] || 'Retry needed';
      copy.append(name, detail); chip.append(attachmentVisual(doc, item.attachment, item.name), copy);
      if (item.phase.endsWith('_error')) {
        const retry = button(`Retry ${item.name}`, 'Retry', item.key, async () => {
          const result = await draft.retry(item.key);
          if (result?.needsFile) chooseRetry(item.key);
        });
        retry.disabled = draft.locked || !supported; chip.append(retry);
      }
      const remove = button(`Remove ${item.name}`, '×', item.key, async () => {
        const result = await draft.remove(item.key);
        if (result?.needsFile) chooseRetry(item.key);
      });
      remove.disabled = draft.locked || item.busy || !supported; chip.append(remove); list.append(chip);
    }
    if (focused) {
      const replacement = [...list.querySelectorAll('button')].find(node =>
        node.dataset.attachmentKey === focused && node.getAttribute('aria-label') === focusLabel && !node.disabled);
      (replacement || addButton).focus({ preventScroll: true });
    }
    if (draft.entries.some(item => item.busy)) status.textContent = 'Preparing files…';
    else if (draft.entries.some(item => item.phase.endsWith('_error'))) status.textContent = 'Some files need attention before sending.';
    else status.textContent = '';
  }
  const onPick = () => { retryKey = null; picker.multiple = true; picker.value = ''; picker.click(); };
  const onFiles = () => perform(async () => {
    const files = Array.from(picker.files || []), retry = retryKey;
    retryKey = null; picker.value = '';
    if (!files.length) return;
    if (retry) await draft.retry(retry, files[0]); else await draft.add(files);
  });
  const onPaste = event => {
    const files = Array.from(event.clipboardData?.files || []);
    if (!files.length) return; // Ordinary text paste remains native.
    event.preventDefault(); void perform(() => draft.add(files));
  };
  const hasFiles = event => Array.from(event.dataTransfer?.types || []).includes('Files');
  const onDrag = event => {
    if (!hasFiles(event)) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = enabled() && !draft.locked ? 'copy' : 'none';
    composer.classList.toggle('composer-dragging', enabled() && !draft.locked);
  };
  const onLeave = event => { if (!composer.contains(event.relatedTarget)) composer.classList.remove('composer-dragging'); };
  const onDrop = event => {
    if (!hasFiles(event)) return;
    event.preventDefault(); composer.classList.remove('composer-dragging');
    void perform(() => draft.add(Array.from(event.dataTransfer.files || [])));
  };
  const listeners = [[addButton, 'click', onPick], [picker, 'change', onFiles], [composer, 'paste', onPaste],
    [composer, 'dragenter', onDrag], [composer, 'dragover', onDrag], [composer, 'dragleave', onLeave], [composer, 'drop', onDrop]];
  for (const [node, name, fn] of listeners) node.addEventListener(name, fn);
  render();
  return { draft, render, destroy() { for (const [node, name, fn] of listeners) node.removeEventListener(name, fn); } };
}
