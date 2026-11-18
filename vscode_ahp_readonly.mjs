import { randomUUID } from 'node:crypto';
import net from 'node:net';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import WebSocket from 'ws';

const ROOT_CHANNEL = 'ahp-root://';
const READ_METHODS = new Set(['initialize', 'listSessions', 'subscribe']);
const PIPE_PREFIX = '\\\\.\\pipe\\vscode-agent-host-';
const CHAT_LIMIT = 200;
const SESSION_LIMIT = 50;
const REQUEST_TIMEOUT_MS = 8000;

class AHPError extends Error {}

function validResource(value, prefix) {
  return typeof value === 'string' && value.startsWith(prefix)
    && value.length <= 2048 && !/[\u0000-\u001f]/.test(value);
}

function isValidInput(input) {
  if (!input || typeof input !== 'object' || !['list', 'read'].includes(input.operation)) return false;
  const endpoint = input.endpoint;
  if (!endpoint || typeof endpoint.path !== 'string'
      || endpoint.path.length > 512 || endpoint.path.includes('\0')
      || !endpoint.path.toLowerCase().startsWith(PIPE_PREFIX.toLowerCase())
      || typeof endpoint.token !== 'string'
      || !/^[A-Za-z0-9_-]{16,512}={0,2}$/.test(endpoint.token)) return false;
  if (input.operation === 'read') {
    return validResource(input.session_uri, 'ahp-session:/')
      && validResource(input.chat_uri, 'ahp-chat:/');
  }
  return true;
}

function connectNamedPipe(path, token) {
  return new WebSocket(`ws://localhost/?tkn=${encodeURIComponent(token)}`, {
    perMessageDeflate: false,
    createConnection: () => net.createConnection({ path }),
  });
}

export class AHPReadOnlyClient {
  constructor(socket, { timeoutMs = REQUEST_TIMEOUT_MS } = {}) {
    this.socket = socket;
    this.timeoutMs = timeoutMs;
    this.nextId = 1;
    this.pending = new Map();
    this.opened = socket.readyState === WebSocket.OPEN
      ? Promise.resolve()
      : new Promise((resolve, reject) => {
        socket.once('open', resolve);
        socket.once('error', () => reject(new AHPError('The VS Code Agent Host did not respond.')));
      });
    socket.on('message', data => this.onMessage(data));
    socket.on('error', () => this.failAll(new AHPError('The VS Code Agent Host did not respond.')));
    socket.on('close', () => this.failAll(new AHPError('The VS Code Agent Host did not respond.')));
  }

  async request(method, params) {
    if (!READ_METHODS.has(method)) throw new AHPError('The VS Code Agent Host rejected the request.');
    await this.opened;
    const id = this.nextId++;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new AHPError('The VS Code Agent Host did not respond.'));
      }, this.timeoutMs);
      this.pending.set(id, { resolve, reject, timer });
      try {
        this.socket.send(JSON.stringify({ jsonrpc: '2.0', id, method, params }));
      } catch {
        clearTimeout(timer);
        this.pending.delete(id);
        reject(new AHPError('The VS Code Agent Host did not respond.'));
      }
    });
  }

  onMessage(data) {
    let message;
    try {
      message = JSON.parse(data.toString('utf8'));
    } catch {
      this.failAll(new AHPError('The VS Code Agent Host returned invalid data.'));
      return;
    }
    const pending = this.pending.get(message.id);
    if (!pending) return;
    clearTimeout(pending.timer);
    this.pending.delete(message.id);
    if (message.error || !Object.hasOwn(message, 'result')) {
      pending.reject(new AHPError('The VS Code Agent Host rejected the request.'));
    } else {
      pending.resolve(message.result);
    }
  }

  failAll(error) {
    for (const [id, pending] of this.pending) {
      clearTimeout(pending.timer);
      pending.reject(error);
      this.pending.delete(id);
    }
  }

  close() {
    this.socket.close();
  }
}

function channelState(result) {
  const state = result?.snapshot?.state;
  if (!state || typeof state !== 'object') {
    throw new AHPError('The VS Code Agent Host returned invalid data.');
  }
  return state;
}

async function initialize(client) {
  const result = await client.request('initialize', {
    channel: ROOT_CHANNEL,
    protocolVersions: ['1.0.0', '0.10.0'],
    clientId: `vega-readonly-${randomUUID()}`,
    clientInfo: { name: 'Vega', version: '1.0.0' },
    initialSubscriptions: [ROOT_CHANNEL],
  });
  if (!['1.0.0', '0.10.0'].includes(result?.protocolVersion)) {
    throw new AHPError('The VS Code Agent Host returned invalid data.');
  }
}

async function listChats(client) {
  const result = await client.request('listSessions', { channel: ROOT_CHANNEL, limit: SESSION_LIMIT });
  if (!Array.isArray(result?.items)) {
    throw new AHPError('The VS Code Agent Host returned invalid data.');
  }
  const listedSessions = result.items.filter(item => item
    && validResource(item.resource, 'ahp-session:/'));
  const sessions = listedSessions.slice(0, SESSION_LIMIT);
  const chats = [];
  let partial = !!result.nextCursor || listedSessions.length > sessions.length;

  for (const session of sessions) {
    let state;
    try {
      state = channelState(await client.request('subscribe', { channel: session.resource }));
    } catch (error) {
      if (!(error instanceof AHPError)) throw error;
      partial = true;
      continue;
    }
    if (!Array.isArray(state.chats)) continue;
    for (const chat of state.chats) {
      if (!chat || !validResource(chat.resource, 'ahp-chat:/')) continue;
      chats.push({
        session_uri: session.resource,
        chat_uri: chat.resource,
        title: typeof chat.title === 'string' && chat.title.trim() ? chat.title.trim().slice(0, 240) : 'Untitled chat',
        status: Number.isSafeInteger(chat.status) ? chat.status : null,
        modified_at: typeof chat.modifiedAt === 'string' ? chat.modifiedAt.slice(0, 64) : null,
      });
      if (chats.length >= CHAT_LIMIT) {
        partial = true;
        break;
      }
    }
    if (chats.length >= CHAT_LIMIT) break;
  }
  return { chats, partial };
}

function plainContent(value) {
  if (typeof value === 'string') return value;
  if (value && typeof value === 'object') {
    for (const key of ['markdown', 'value', 'text']) {
      if (typeof value[key] === 'string') return value[key];
    }
  }
  return '';
}

function responseText(parts) {
  if (!Array.isArray(parts)) return '';
  return parts.map(part => {
    if (part?.kind === 'markdown' && typeof part.content === 'string') return part.content;
    if (part?.kind === 'systemNotification') return plainContent(part.content);
    if (part?.kind === 'inputRequest' && typeof part.request?.message === 'string') return part.request.message;
    if (part?.kind === 'toolCall' && typeof part.toolCall?.displayName === 'string') {
      return `[Tool: ${part.toolCall.displayName}]`;
    }
    if (part?.kind === 'error') return 'The agent reported an error.';
    return '';
  }).filter(Boolean).join('\n\n').slice(0, 12000);
}

function message(role, text, turn, suffix = '') {
  if (typeof text !== 'string' || !text.trim()) return null;
  const timestamp = Date.parse(turn?.startedAt || '');
  return {
    id: `${turn?.id || 'turn'}:${role}${suffix}`,
    role,
    text: text.slice(0, 12000),
    phase: turn?.state === 'complete' ? 'final' : 'in_progress',
    at: Number.isFinite(timestamp) ? timestamp / 1000 : 0,
  };
}

export function normalizeTranscript(state) {
  const messages = [];
  const turns = [...(Array.isArray(state?.turns) ? state.turns : [])];
  if (state?.activeTurn) turns.push(state.activeTurn);
  for (const turn of turns) {
    const user = message('user', turn?.message?.text, turn, ':user');
    if (user) messages.push(user);
    const assistant = message('assistant', responseText(turn?.responseParts), turn, ':assistant');
    if (assistant) messages.push(assistant);
  }
  return {
    messages: messages.slice(-24),
    has_more: messages.length > 24 || typeof state?.turnsNextCursor === 'string',
  };
}

async function readChat(client, sessionUri, chatUri) {
  const session = channelState(await client.request('subscribe', { channel: sessionUri }));
  if (!Array.isArray(session.chats) || !session.chats.some(chat => chat?.resource === chatUri)) {
    throw new AHPError('The VS Code Agent Host rejected the request.');
  }
  const chat = channelState(await client.request('subscribe', {
    channel: chatUri,
    view: { turns: 12 },
  }));
  return normalizeTranscript(chat);
}

export async function runOperation(input, socketFactory = connectNamedPipe) {
  if (!isValidInput(input)) throw new AHPError('The VS Code Agent Host returned invalid data.');
  const socket = socketFactory(input.endpoint.path, input.endpoint.token);
  const client = new AHPReadOnlyClient(socket);
  try {
    await initialize(client);
    return input.operation === 'list'
      ? await listChats(client)
      : await readChat(client, input.session_uri, input.chat_uri);
  } finally {
    client.close();
  }
}

async function main() {
  try {
    const raw = readFileSync(0, 'utf8');
    if (Buffer.byteLength(raw, 'utf8') > 64 * 1024) {
      throw new AHPError('The VS Code Agent Host returned invalid data.');
    }
    const input = JSON.parse(raw);
    const result = await runOperation(input);
    process.stdout.write(JSON.stringify({ result }) + '\n');
  } catch (error) {
    const allowed = new Set([
      'The VS Code Agent Host did not respond.',
      'The VS Code Agent Host rejected the request.',
      'The VS Code Agent Host returned invalid data.',
    ]);
    const message = error instanceof AHPError && allowed.has(error.message)
      ? error.message
      : 'The VS Code Agent Host did not respond.';
    process.stdout.write(JSON.stringify({ error: message }) + '\n');
    process.exitCode = 1;
  }
}

if (process.argv[1] && fileURLToPath(import.meta.url) === resolve(process.argv[1])) {
  await main();
}
