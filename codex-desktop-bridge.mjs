// Bounded calls to the installed desktop bridge. No Codex process is started.
import net from 'node:net';
import fs from 'node:fs/promises';
import { randomUUID } from 'node:crypto';

const MAX = 8 * 1024 * 1024;
export class DesktopPipe {
  constructor(path) { this.path = path; this.pending = new Map(); this.buffer = Buffer.alloc(0); this.next = 0; }
  async connect() {
    this.socket = net.createConnection(this.path);
    await new Promise((resolve, reject) => {
      this.socket.once('connect', resolve); this.socket.once('error', reject);
    });
    this.socket.on('error', () => this.close());
    this.socket.on('close', () => this.close());
    this.socket.on('data', chunk => {
      this.buffer = Buffer.concat([this.buffer, chunk]);
      while (this.buffer.length >= 4) {
        const size = this.buffer.readUInt32LE(0);
        if (size > MAX) return this.close();
        if (this.buffer.length < size + 4) return;
        let value;
        try { value = JSON.parse(this.buffer.subarray(4, size + 4).toString('utf8')); }
        catch { return this.close(); }
        this.buffer = this.buffer.subarray(size + 4);
        const pending = this.pending.get(value.id);
        if (!pending) continue;
        this.pending.delete(value.id);
        if (value.error) pending.reject(new Error('Codex did not accept the bridge request.'));
        else pending.resolve(value.result);
      }
    });
  }
  rpc(method, params) {
    const id = ++this.next;
    const body = Buffer.from(JSON.stringify({ jsonrpc: '2.0', id, method, params }));
    if (body.length > MAX) throw new Error('Request too large.');
    const header = Buffer.alloc(4); header.writeUInt32LE(body.length);
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this.socket.write(Buffer.concat([header, body]));
    });
  }
  close() {
    for (const item of this.pending.values()) item.reject(new Error('Desktop connection closed.'));
    this.pending.clear();
    this.socket?.destroy();
  }
}

export async function run(input) {
  if (!['snapshot', 'read', 'send'].includes(input.operation)
      || !/^[a-f0-9-]{36}$/.test(input.callerThreadId)) throw new Error('Invalid desktop request.');
  // The saved pipe is a hint, not an authority grant. Rediscover after an app restart.
  let paths = [...new Set([process.env.CODEX_APP_TOOLS_PIPE_PATH, input.pipe].filter(Boolean))];
  if (process.platform === 'win32') {
    const names = await fs.readdir('\\\\.\\pipe\\');
    const appTools = names.filter(name => /^codex-app-tools-[a-f0-9-]{36}$/.test(name))
      .map(name => '\\\\.\\pipe\\' + name);
    const browserTools = names.filter(name => /^codex-browser-use-[a-f0-9-]{36}$/.test(name))
      .map(name => '\\\\.\\pipe\\' + name);
    paths.push(...appTools, ...browserTools);
  }
  paths = [...new Set(paths)].filter(path =>
    /^\\\\\.\\pipe\\codex-(?:app-tools|browser-use)-[a-f0-9-]{36}$/.test(path));
  let client;
  for (const path of paths) {
    const candidate = new DesktopPipe(path);
    try { await candidate.connect(); client = candidate; break; }
    catch { candidate.close(); }
  }
  if (!client) throw new Error('Open Codex on your laptop to reconnect.');
  try {
    const catalog = await client.rpc('tools/list', { threadStartKind: 'all' });
    async function call(name, args) {
      const tool = catalog.tools?.find(t => t.name === name && t.namespace === 'codex_app');
      if (!tool) throw new Error('This desktop version does not support the requested chat action.');
      const result = await client.rpc('tools/call', {
        callerSource: 'codex', hostId: 'local', namespace: 'codex_app',
        threadId: input.callerThreadId, turnId: 'vega-miniapp-' + randomUUID(),
        callId: 'vega-miniapp-' + randomUUID(), tool: name, arguments: args,
      });
      if (!result?.success) throw new Error('Codex could not complete the requested chat action.');
      const texts = result.contentItems?.filter(item => item.type === 'inputText').map(item => item.text) ?? [];
      for (const text of texts) {
        try { const value = JSON.parse(text); if (value && typeof value === 'object') return value; } catch {}
      }
      throw new Error('Codex returned an unreadable receipt.');
    }
    if (input.operation === 'snapshot') {
      const [projects, chats] = await Promise.all([
        call('list_projects', {}), call('list_threads', { limit: 50 }),
      ]);
      return { projects, chats };
    }
    if (!/^[a-f0-9-]{36}$/.test(input.threadId)) throw new Error('Invalid chat.');
    if (input.operation === 'read') {
      return await call('read_thread', { threadId: input.threadId, hostId: 'local',
        turnLimit: 3, includeOutputs: false, maxOutputCharsPerItem: 2400 });
    }
    if (typeof input.text !== 'string' || !input.text.trim() || input.text.length > 24000) {
      throw new Error('Enter a prompt of up to 12,000 characters.');
    }
    // Only an explicit paired-owner POST reaches this branch. Preserve the
    // selected chat's model, permissions and ordinary desktop queue semantics.
    return await call('send_message_to_thread', {
      threadId: input.threadId, hostId: 'local', prompt: input.text,
    });
  } finally { client.close(); }
}

if (process.argv[1]?.endsWith('codex-desktop-bridge.mjs')) {
  const timer = setTimeout(() => process.exit(2), 18000);
  try {
    let raw = '';
    for await (const chunk of process.stdin) {
      raw += chunk;
      if (raw.length > 128000) throw new Error('Request too large.');
    }
    console.log(JSON.stringify({ result: await run(JSON.parse(raw)) }));
  } catch (error) {
    console.log(JSON.stringify({ error: error.message }));
    process.exitCode = 1;
  } finally { clearTimeout(timer); }
}
