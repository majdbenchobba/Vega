// Bounded Antigravity UI calls through its local Chrome DevTools endpoint.
// Read-only calls never start the IDE; a send is only made by an explicit owner request.
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';

const MAX_TEXT = 12000;
const MAX_RESPONSE = 512 * 1024;
const ID = /^[a-f0-9-]{36}$/;

function dataDirectory() {
  return path.join(process.env.APPDATA || path.join(os.homedir(), 'AppData', 'Roaming'), 'Antigravity');
}

async function cdpPort() {
  if (process.env.ANTIGRAVITY_CDP_PORT) {
    const value = Number(process.env.ANTIGRAVITY_CDP_PORT);
    if (Number.isInteger(value) && value > 0 && value < 65536) return value;
    throw new Error('The configured Antigravity DevTools port is invalid.');
  }
  let text;
  try {
    text = await fs.readFile(path.join(dataDirectory(), 'DevToolsActivePort'), 'utf8');
  } catch {
    throw new Error('Open Antigravity on this laptop to reconnect.');
  }
  const value = Number(text.split(/\r?\n/, 1)[0]);
  if (!Number.isInteger(value) || value <= 0 || value >= 65536) {
    throw new Error('Antigravity did not expose a valid local DevTools port.');
  }
  return value;
}

async function target(port) {
  let response;
  try {
    response = await fetch(`http://127.0.0.1:${port}/json/list`, { signal: AbortSignal.timeout(8000) });
  } catch {
    throw new Error('Open Antigravity on this laptop to reconnect.');
  }
  if (!response.ok) throw new Error('Antigravity did not return a local page.');
  const targets = await response.json();
  const page = Array.isArray(targets)
    ? targets.find(item => item?.type === 'page'
        && typeof item?.webSocketDebuggerUrl === 'string'
        && item.webSocketDebuggerUrl.startsWith('ws://127.0.0.1:')
        && typeof item?.url === 'string'
        && /^https?:\/\/127\.0\.0\.1:/.test(item.url)
        && String(item?.title || '').includes('Antigravity'))
    : null;
  if (!page) throw new Error('The Antigravity window is not available.');
  return page.webSocketDebuggerUrl;
}

class CDP {
  constructor(url) {
    this.url = url;
    this.socket = null;
    this.next = 0;
    this.pending = new Map();
  }

  async connect() {
    this.socket = new WebSocket(this.url);
    await new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error('Antigravity did not answer.')), 8000);
      const onOpen = () => { clearTimeout(timer); resolve(); };
      const onError = () => { clearTimeout(timer); reject(new Error('Antigravity did not answer.')); };
      this.socket.addEventListener('open', onOpen, { once: true });
      this.socket.addEventListener('error', onError, { once: true });
    });
    this.socket.onmessage = event => {
      let message;
      try { message = JSON.parse(event.data); } catch { return; }
      if (!message.id || !this.pending.has(message.id)) return;
      const pending = this.pending.get(message.id);
      this.pending.delete(message.id);
      if (message.error) pending.reject(new Error('Antigravity rejected the request.'));
      else pending.resolve(message.result);
    };
    this.socket.onerror = () => this.close();
    this.socket.onclose = () => this.close();
  }

  call(method, params) {
    const id = ++this.next;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this.socket.send(JSON.stringify({ id, method, params }));
    });
  }

  async evaluate(expression) {
    const result = await this.call('Runtime.evaluate', {
      expression, returnByValue: true, awaitPromise: true,
    });
    if (result?.exceptionDetails) throw new Error('Antigravity did not return a readable result.');
    return result?.result?.value;
  }

  async key(modifiers, key, code, windowsVirtualKeyCode) {
    await this.call('Input.dispatchKeyEvent', {
      type: 'keyDown', modifiers, key, code, windowsVirtualKeyCode,
    });
    await this.call('Input.dispatchKeyEvent', {
      type: 'keyUp', modifiers, key, code, windowsVirtualKeyCode,
    });
  }

  async clearEditor() {
    await this.key(2, 'a', 'KeyA', 65);
    await this.key(0, 'Backspace', 'Backspace', 8);
  }

  close() {
    for (const item of this.pending.values()) item.reject(new Error('Antigravity closed the connection.'));
    this.pending.clear();
    this.socket?.close();
  }
}

const SNAPSHOT = `(() => {
  const nodes = Array.from(document.querySelectorAll('[data-testid="project-header"],[data-testid="conversation-row-sidebar"]'));
  let project = null;
  const projects = [];
  const chats = [];
  for (const node of nodes) {
    if (node.getAttribute('data-testid') === 'project-header') {
      const title = (node.innerText || '').trim();
      if (!title) continue;
      project = { id: title, title };
      projects.push(project);
      continue;
    }
    const id = node.getAttribute('data-cascade-id') || '';
    if (!/^[a-f0-9-]{36}$/.test(id)) continue;
    const anchor = node.querySelector('a[href^="/c/"]');
    const title = (anchor?.getAttribute('aria-label') || node.querySelector('span')?.innerText || '').trim();
    if (!title) continue;
    chats.push({
      id, title, project: project ? project.title : null,
      selected: node.getAttribute('data-selected') === 'true',
      pinned: node.getAttribute('data-pinned') === 'true',
      href: anchor ? anchor.getAttribute('href') : null,
    });
  }
  return { projects, chats };
})()`;

function openExpression(chatId) {
  if (!ID.test(chatId)) throw new Error('Invalid Antigravity conversation.');
  return `(() => {
    const row = document.querySelector('[data-cascade-id="${chatId}"]');
    if (!row) return { ok: false, error: 'This conversation is no longer visible in Antigravity.' };
    const anchor = row.querySelector('a[href^="/c/"]');
    if (!anchor) return { ok: false, error: 'This conversation has no open link.' };
    anchor.click();
    return { ok: true };
  })()`;
}

async function snapshot(client) {
  const value = await client.evaluate(SNAPSHOT);
  if (!value || !Array.isArray(value.projects) || !Array.isArray(value.chats)) {
    throw new Error('Antigravity did not return a readable conversation list.');
  }
  return value;
}

async function open(client, chatId) {
  const result = await client.evaluate(openExpression(chatId));
  if (!result?.ok) throw new Error(result?.error || 'The Antigravity conversation could not be opened.');
  await new Promise(resolve => setTimeout(resolve, 600));
}

async function send(client, chatId, text) {
  if (typeof text !== 'string' || !text.trim() || text.length > MAX_TEXT) {
    throw new Error('Enter a prompt of up to 12,000 characters.');
  }
  await open(client, chatId);
  const selected = await client.evaluate(`Array.from(document.querySelectorAll('[data-cascade-id]'))
    .some(row => row.getAttribute('data-cascade-id') === ${JSON.stringify(chatId)} && row.getAttribute('data-selected') === 'true')`);
  if (!selected) throw new Error('Antigravity did not keep the selected conversation open.');
  const editor = await client.evaluate(`(() => {
    const box = document.querySelector('[aria-label="Message input"]');
    if (!box) return null;
    box.focus();
    return { tag: box.tagName, editable: box.getAttribute('contenteditable') };
  })()`);
  if (!editor || editor.editable !== 'true') throw new Error('The Antigravity message box is unavailable.');
  await client.clearEditor();
  await client.call('Input.insertText', { text });
  await new Promise(resolve => setTimeout(resolve, 250));
  const state = await client.evaluate(`JSON.stringify({
    text: document.querySelector('[aria-label="Message input"]')?.innerText || '',
    disabled: document.querySelector('[data-testid="send-button"]')?.disabled,
  })`);
  const current = JSON.parse(state || '{}');
  if (!current.text || current.text.trim() !== text.trim() || current.disabled) {
    throw new Error('Antigravity did not accept the prompt text. Nothing was sent.');
  }
  const clicked = await client.evaluate(`(() => {
    const send = document.querySelector('[data-testid="send-button"]');
    if (!send) return false;
    send.click();
    return true;
  })()`);
  if (!clicked) throw new Error('The Antigravity send control is unavailable.');
  return { accepted: true, chat_id: chatId };
}

async function run(input) {
  if (!input || typeof input !== 'object' || !['snapshot', 'open', 'send'].includes(input.operation)) {
    throw new Error('Invalid Antigravity request.');
  }
  const port = await cdpPort();
  const url = await target(port);
  const client = new CDP(url);
  await client.connect();
  try {
    if (input.operation === 'snapshot') return await snapshot(client);
    if (!ID.test(input.chatId || '')) throw new Error('Invalid Antigravity conversation.');
    if (input.operation === 'open') {
      await open(client, input.chatId);
      return { opened: true, chat_id: input.chatId };
    }
    return await send(client, input.chatId, input.text);
  } finally {
    client.close();
  }
}

if (process.argv[1]?.endsWith('antigravity-cdp.mjs')) {
  const timer = setTimeout(() => process.exit(2), 20000);
  try {
    let raw = '';
    for await (const chunk of process.stdin) {
      raw += chunk;
      if (raw.length > MAX_RESPONSE) throw new Error('Request too large.');
    }
    console.log(JSON.stringify({ result: await run(JSON.parse(raw)) }));
  } catch (error) {
    console.log(JSON.stringify({ error: String(error?.message || 'Antigravity request failed.').slice(0, 500) }));
    process.exitCode = 1;
  } finally {
    clearTimeout(timer);
  }
}
