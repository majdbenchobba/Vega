const IDES = {
  codex: {
    label: 'Codex desktop', short: 'Codex', kind: 'codex',
    workspace: '/api/project-control/workspace',
    chats: '/api/project-control/chats',
    prompts: '/api/project-control/chat-prompts',
    pending: 'vega.codex.pending',
  },
  vscode_agents: {
    label: 'VS Code Agent Host', short: 'VS Code', kind: 'vscode_agents', readOnly: true,
    workspace: '/api/project-control/vscode-agents/workspace',
    chats: '/api/project-control/vscode-agents/chats',
    prompts: '/api/project-control/vscode-agents/chat-prompts',
    pending: 'vega.vscode-agents.pending',
  },
  antigravity: {
    label: 'Antigravity', short: 'Antigravity', kind: 'antigravity',
    workspace: '/api/project-control/antigravity/workspace',
    chats: '/api/project-control/antigravity/chats',
    prompts: '/api/project-control/antigravity/chat-prompts',
    pending: 'vega.antigravity.pending',
  },
  deepseek: {
    label: 'DeepSeek Harness', short: 'DeepSeek', kind: 'deepseek',
    workspace: '/api/project-control/deepseek/workspace',
    chats: '/api/project-control/deepseek/chats',
    prompts: '/api/project-control/deepseek/chat-prompts',
    pending: 'vega.deepseek.pending',
  },
};

export class NativeIdeSessionsPanel {
  constructor(root, { get, post }) {
    this.root = root;
    this.get = get;
    this.post = post;
    this.snapshot = null;
    this.chatId = '';
    this.currentIde = 'codex';
    this.readSequence = 0;
    this.pendingStorageError = null;
    this.pending = this.loadPending();
    this.build();
  }

  info(ide = this.currentIde) {
    return IDES[ide] || IDES.codex;
  }

  loadPending(ide = this.currentIde) {
    if (this.info(ide).readOnly) return null;
    try {
      const value = JSON.parse(sessionStorage.getItem(this.info(ide).pending) || 'null');
      if (value && /^[a-zA-Z0-9_.:-]{1,160}$/.test(value.request_key)
          && /^[a-f0-9-]{36}$/.test(value.thread_id)) {
        return { request_key: value.request_key, thread_id: value.thread_id };
      }
    } catch (error) {
      this.pendingStorageError = error.message;
    }
    return null;
  }

  clearPending(ide = this.currentIde) {
    sessionStorage.removeItem(this.info(ide).pending);
    this.pending = null;
  }

  build() {
    this.root.innerHTML = `
      <header class="copilot-header">
        <div><span class="eyebrow">NATIVE IDE CONNECTIONS</span><h2>IDE sessions</h2></div>
        <button type="button" data-action="refresh">Refresh</button>
      </header>
      <p class="copilot-explainer">These are conversations owned by the connected IDE. Vega does not change their model or permissions.</p>
      <p class="copilot-note">Available adapters: Codex desktop, VS Code Agent Host (read-only AHP), and Antigravity. DeepSeek Harness needs DEEPSEEK_API_KEY. Claude Code is not connected; Vega will not claim access to its chats until verified.</p>
      <div class="copilot-layout">
        <aside class="copilot-sidebar">
          <label>IDE<select data-field="ide" aria-label="IDE integration">
            <option value="codex">Codex desktop</option>
            <option value="vscode_agents">VS Code Agent Host (read-only)</option>
            <option value="antigravity">Antigravity</option>
            <option value="deepseek">DeepSeek Harness (headless)</option>
            <option value="claude" disabled>Claude Code - not connected</option>
          </select></label>
          <div data-field="chats" class="copilot-session-list" aria-label="IDE conversations"></div>
        </aside>
        <section class="copilot-chat">
          <div data-field="messages" class="copilot-messages" role="log" aria-live="polite"></div>
          <form data-field="prompt" class="copilot-prompt" hidden>
            <textarea name="text" rows="3" maxlength="12000" placeholder="Send a prompt to the selected conversation..." required></textarea>
            <button class="primary" type="submit">Send</button>
          </form>
          <button type="button" data-action="check-receipt" hidden>Check delivery receipt</button>
          <p data-field="status" role="status" class="copilot-status"></p>
        </section>
      </div>
      <p class="copilot-policy">Prompt sending is available only for adapters that support it. VS Code Agent Host uses a read-only AHP connection, so Vega cannot send prompts to VS Code. Other IDEs keep their own model and permission settings.</p>
    `;
    this.root.querySelector('[data-action="refresh"]').addEventListener('click', () => void this.show());
    this.root.querySelector('[data-action="check-receipt"]').addEventListener('click', () => void this.checkReceipt());
    this.root.querySelector('[data-field="ide"]').addEventListener('change', event => {
      this.chatId = '';
      this.currentIde = event.currentTarget.value;
      this.pendingStorageError = null;
      this.pending = this.loadPending();
      const info = this.info();
      this.root.querySelector('[data-field="prompt"] textarea').placeholder =
        `Send a prompt to the selected ${info.short} conversation...`;
      void this.show();
    });
    this.root.querySelector('[data-field="prompt"]').addEventListener('submit', event => {
      event.preventDefault();
      void this.send(new FormData(event.currentTarget));
    });
  }

  status(message, error = false) {
    const node = this.root.querySelector('[data-field="status"]');
    node.textContent = message;
    node.dataset.error = error ? 'true' : 'false';
  }

  async show() {
    const info = this.info();
    this.status(`Connecting to ${info.label}...`);
    try {
      const snapshot = await this.get(info.workspace);
      if (!Array.isArray(snapshot.chats)) throw new Error(`${info.label} returned an invalid conversation list.`);
      this.snapshot = snapshot;
      this.renderChats();
      const chat = this.snapshot.chats.find(item => item.id === this.chatId && item.kind === info.kind);
      if (chat) await this.openChat(chat.id);
      else this.status(snapshot.unavailable
        ? (info.kind === 'deepseek'
          ? 'DeepSeek Harness needs DEEPSEEK_API_KEY. Run configure_deepseek_key.py or set it in the environment.'
          : `Some ${info.label} sources are unavailable. Refresh after opening the app.`)
        : `Select a ${info.label} conversation.`);
      if (this.pendingStorageError) {
        this.status(`The pending delivery state could not be read: ${this.pendingStorageError}. Sending is disabled for safety.`, true);
      }
    } catch (error) {
      this.status(error.message, true);
    }
  }

  renderChats() {
    const host = this.root.querySelector('[data-field="chats"]');
    const info = this.info();
    const chats = (this.snapshot?.chats || []).filter(chat =>
      chat.host_id === 'local' && chat.kind === info.kind);
    host.replaceChildren();
    for (const chat of chats) {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'copilot-session-item';
      button.dataset.active = chat.id === this.chatId ? 'true' : 'false';
      button.textContent = `${chat.title} · ${chat.status}`;
      button.title = chat.title;
      button.addEventListener('click', () => void this.openChat(chat.id));
      host.append(button);
    }
    if (!chats.length) {
      const empty = document.createElement('p');
      empty.className = 'copilot-note';
      empty.textContent = `No local ${info.label} conversations found. Open VS Code with a chat and refresh.`;
      host.append(empty);
    }
  }

  async openChat(id) {
    const info = this.info();
    const chat = this.snapshot?.chats.find(item => item.id === id && item.host_id === 'local' && item.kind === info.kind);
    if (!chat) return;
    this.chatId = id;
    const sequence = ++this.readSequence;
    this.renderChats();
    this.status(`Loading ${info.label} conversation...`);
    try {
      const data = await this.get(`${info.chats}/${encodeURIComponent(id)}`);
      if (sequence !== this.readSequence || this.chatId !== id) return;
      if (data.chat?.id !== id || !Array.isArray(data.messages)) {
        throw new Error(`${info.label} returned a mismatched conversation.`);
      }
      const messages = this.root.querySelector('[data-field="messages"]');
      messages.replaceChildren();
      for (const message of data.messages) {
        const item = document.createElement('article');
        item.className = `copilot-message copilot-message-${message.role}`;
        const label = document.createElement('strong');
        label.textContent = message.role === 'user' ? 'You' : info.short;
        const text = document.createElement('p');
        text.textContent = message.text;
        item.append(label, text);
        messages.append(item);
      }
      messages.scrollTop = messages.scrollHeight;
      const form = this.root.querySelector('[data-field="prompt"]');
      form.hidden = !chat.can_send;
      const isUncertain = this.pending?.thread_id === id;
      const blocked = !!this.pending || !!this.pendingStorageError;
      form.querySelector('textarea').disabled = blocked;
      form.querySelector('button').disabled = blocked;
      this.root.querySelector('[data-action="check-receipt"]').hidden = !this.pending;
      this.status(this.pendingStorageError
        ? `The pending delivery state could not be read: ${this.pendingStorageError}. Sending is disabled for safety.`
        : this.pending
          ? `An earlier ${info.label} prompt${isUncertain ? ' to this conversation' : ''} has an uncertain receipt. Check it before sending another prompt.`
          : chat.can_send ? '' : `This ${info.label} conversation is read-only.`);
    } catch (error) {
      if (sequence === this.readSequence) this.status(error.message, true);
    }
  }

  async send(formData) {
    const info = this.info();
    if (info.readOnly) return;
    const chat = this.snapshot?.chats.find(item =>
      item.id === this.chatId && item.host_id === 'local' && item.kind === info.kind && item.can_send);
    const textarea = this.root.querySelector('[data-field="prompt"] textarea');
    const text = String(formData.get('text') || '').trim();
    if (!chat || !text || this.pending || this.pendingStorageError) return;
    const attempt = {
      request_key: crypto.randomUUID(),
      thread_id: chat.id,
      target_version: chat.target_version,
      text,
    };
    try {
      sessionStorage.setItem(info.pending, JSON.stringify({
        request_key: attempt.request_key, thread_id: attempt.thread_id,
      }));
    } catch (error) {
      this.status(`Could not safely save a delivery receipt key: ${error.message}`, true);
      return;
    }
    this.pending = attempt;
    this.status(`Sending to ${info.label}…`);
    this.root.querySelector('[data-field="prompt"] button').disabled = true;
    try {
      const result = await this.post(info.prompts, attempt);
      const receipt = result.receipt;
      if (receipt?.request_key !== attempt.request_key || receipt?.thread_id !== chat.id) {
        throw new Error(`${info.label} returned a mismatched delivery receipt.`);
      }
      if (receipt.state !== 'accepted') {
        this.pending = attempt;
        textarea.disabled = true;
        this.root.querySelector('[data-action="check-receipt"]').hidden = false;
        this.status('Delivery is unconfirmed. Check the conversation before trying again.', true);
      } else {
        textarea.value = '';
        this.clearPending();
        await this.openChat(chat.id);
        this.status(`${info.label} accepted the prompt. Task completion is not confirmed.`);
      }
    } catch (error) {
      textarea.disabled = true;
      this.root.querySelector('[data-action="check-receipt"]').hidden = false;
      this.status(`${error.message} Delivery may be uncertain; check the conversation before retrying.`, true);
    } finally {
      this.root.querySelector('[data-field="prompt"] button').disabled = false;
    }
  }

  async checkReceipt() {
    const attempt = this.pending;
    if (!attempt) return;
    const info = this.info();
    if (info.readOnly) return;
    this.status(`Checking ${info.label} delivery receipt...`);
    try {
      const data = await this.get(info.prompts);
      const receipt = data.receipts?.find(item => item.request_key === attempt.request_key);
      if (receipt?.thread_id === attempt.thread_id && receipt.state === 'accepted') {
        this.clearPending();
        this.root.querySelector('[data-field="prompt"] textarea').value = '';
        this.root.querySelector('[data-field="prompt"] textarea').disabled = false;
        this.root.querySelector('[data-action="check-receipt"]').hidden = true;
        await this.openChat(attempt.thread_id);
        this.status(`${info.label} accepted the prompt. Task completion is not confirmed.`);
        return;
      }
      this.status('No confirmed acceptance receipt yet. Do not resend this prompt; refresh or inspect it in the IDE.', true);
    } catch (error) {
      this.status(`${error.message} The original delivery remains uncertain.`, true);
    }
  }
}
