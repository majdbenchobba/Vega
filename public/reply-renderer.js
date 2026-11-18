import { lexer } from './vendor/marked.js';
import { decodeHTMLStrict } from './vendor/entities/decode.js';

const MAX_FORMATTED_LENGTH = 32000;
const MAX_TOKENS = 4000;
const MAX_DEPTH = 32;
const rendered = new WeakMap();
const element = (tag, children = [], attributes = {}) => ({ tag, children, attributes });
const plain = value => typeof value === 'string' ? value : '';

export function replyLink(value, { literal = false } = {}) {
  if (typeof value !== 'string' || value.length > 4096) return null;
  const decoded = (literal ? value : decodeHTMLStrict(value)).trim();
  if (/[\u0000-\u0020\u007f\u202a-\u202e\u2066-\u2069]/.test(decoded)) return null;
  try {
    const url = new URL(decoded);
    if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password || !url.hostname) return null;
    return url.href;
  } catch { return null; }
}

/** Parse Markdown into a small, inert presentation tree, never HTML output. */
export function replyPresentation(source) {
  const text = typeof source === 'string' ? source : String(source ?? '');
  const fallback = () => ({ formatted: false, nodes: [text] });
  if (text.length > MAX_FORMATTED_LENGTH) return fallback();
  let visited = 0;
  const walk = (tokens, depth = 0, literal = false) => {
    if (depth > MAX_DEPTH || !Array.isArray(tokens)) throw new RangeError('Reply formatting limit.');
    const output = [];
    for (const token of tokens) {
      if (++visited > MAX_TOKENS) throw new RangeError('Reply formatting limit.');
      const children = () => walk(token.tokens || [], depth + 1, literal);
      switch (token.type) {
        case 'space':
        case 'def':
          break;
        case 'paragraph':
          output.push(element('p', children()));
          break;
        case 'heading':
          output.push(element(`h${Math.min(6, Math.max(3, Number(token.depth) + 2 || 3))}`, children()));
          break;
        case 'blockquote':
          output.push(element('blockquote', children()));
          break;
        case 'hr':
        case 'br':
          output.push(element(token.type));
          break;
        case 'strong':
        case 'em':
        case 'del':
          output.push(element(token.type, children()));
          break;
        case 'text':
          if (token.tokens) output.push(...children());
          else {
            const value = plain(token.raw || token.text);
            output.push(literal || token.escaped ? value : decodeHTMLStrict(value));
          }
          break;
        case 'escape':
          output.push(plain(token.text));
          break;
        case 'codespan':
          output.push(element('code', [plain(token.text)]));
          break;
        case 'code':
          output.push({
            kind: 'code',
            language: /^[a-z0-9_+#.-]{1,24}$/i.test(plain(token.lang).split(/\s/)[0])
              ? token.lang.split(/\s/)[0] : 'text',
            text: plain(token.text),
          });
          break;
        case 'html':
          // HTML stays visible as source text; it never enters an HTML parser.
          output.push(token.block
            ? element('pre', [plain(token.raw || token.text)], { class: 'reply-literal' })
            : plain(token.raw || token.text));
          break;
        case 'link': {
          const href = replyLink(token.href, { literal: token.autolink === true });
          const label = walk(token.tokens || [], depth + 1, token.autolink === true);
          output.push(href ? element('a', label, {
            href, target: '_blank', rel: 'noopener noreferrer', referrerpolicy: 'no-referrer',
            title: token.title ? decodeHTMLStrict(token.title).slice(0, 300) : href,
          }) : element('span', label, { class: 'reply-unlinked' }));
          break;
        }
        case 'image': {
          // An image reference becomes a user-opened link, never an automatic
          // remote request carrying a URL taken from model/quoted content.
          const label = 'Image: ' + (decodeHTMLStrict(plain(token.text)).trim() || 'open reference');
          const href = replyLink(token.href);
          output.push(href ? element('a', [label], {
            href, target: '_blank', rel: 'noopener noreferrer', referrerpolicy: 'no-referrer',
            class: 'reply-image-reference',
          }) : element('span', [label], { class: 'reply-image-reference' }));
          break;
        }
        case 'checkbox':
          output.push(element('input', [], {
            type: 'checkbox', disabled: true, checked: token.checked === true, tabindex: '-1',
            'aria-label': token.checked ? 'Reported complete' : 'Reported pending',
          }));
          output.push(' ');
          break;
        case 'list': {
          const items = token.items.map(item => {
            if (++visited > MAX_TOKENS) throw new RangeError('Reply formatting limit.');
            return element('li', walk(item.tokens, depth + 1), {
              ...(item.task ? { class: 'reply-task-item' } : {}),
            });
          });
          const attributes = token.ordered && Number.isInteger(token.start) && token.start > 0
            ? { start: String(token.start) } : {};
          output.push(element(token.ordered ? 'ol' : 'ul', items, attributes));
          break;
        }
        case 'table': {
          const cell = (value, tag) => {
            if (++visited > MAX_TOKENS) throw new RangeError('Reply formatting limit.');
            return element(tag, walk(value.tokens, depth + 1), {
              ...(tag === 'th' ? { scope: 'col' } : {}),
              ...(['left', 'right', 'center'].includes(value.align) ? { class: `reply-align-${value.align}` } : {}),
            });
          };
          const table = element('table', [
            element('thead', [element('tr', token.header.map(value => cell(value, 'th')))]),
            element('tbody', token.rows.map(row => element('tr', row.map(value => cell(value, 'td'))))),
          ]);
          output.push(element('div', [table], {
            class: 'reply-table-wrap', role: 'region', 'aria-label': 'Table in Vega reply', tabindex: '0',
          }));
          break;
        }
        default:
          output.push(plain(token.raw || token.text));
      }
    }
    return output;
  };
  try { return { formatted: true, nodes: walk(lexer(text, { gfm: true, breaks: true })) }; }
  catch { return fallback(); }
}

function defaultClipboard(text) {
  const clipboard = globalThis.navigator?.clipboard;
  if (!clipboard?.writeText) throw new Error('Clipboard access is unavailable.');
  return clipboard.writeText(text);
}

/** Update only changed source text; polling never rebuilds an unchanged reply. */
export function renderReplyBody(container, source, {
  markdown = true, writeClipboard = defaultClipboard,
} = {}) {
  const text = typeof source === 'string' ? source : String(source ?? '');
  const previous = rendered.get(container);
  if (previous?.text === text && previous.markdown === markdown) return false;
  const presentation = markdown ? replyPresentation(text) : { formatted: false, nodes: [text] };
  const document = container.ownerDocument;
  const generation = {};
  const make = (tag, content, attributes = {}) => {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(attributes)) {
      if (key === 'checked' || key === 'disabled') node[key] = value;
      else node.setAttribute(key, value);
    }
    if (content !== undefined) node.textContent = content;
    return node;
  };
  const mount = plan => {
    if (typeof plan === 'string') return document.createTextNode(plan);
    if (plan.kind === 'code') {
      const shell = make('div', undefined, { class: 'reply-code-block' });
      const header = make('div', undefined, { class: 'reply-code-header' });
      const language = make('span', plan.language);
      const copy = make('button', 'Copy', { type: 'button', 'aria-label': 'Copy code', title: 'Copy code' });
      const status = make('span', '', { class: 'reply-copy-status', role: 'status', 'aria-live': 'polite' });
      const pre = make('pre', undefined, { tabindex: '0', 'aria-label': `${plan.language} code` });
      pre.append(make('code', plan.text));
      header.append(language, copy, status);
      shell.append(header, pre);
      copy.addEventListener('click', async () => {
        if (copy.disabled || rendered.get(container)?.generation !== generation) return;
        copy.disabled = true; status.textContent = '';
        try {
          await writeClipboard(plan.text);
          if (rendered.get(container)?.generation !== generation) return;
          copy.textContent = 'Copied'; status.textContent = 'Code copied.';
        } catch {
          if (rendered.get(container)?.generation !== generation) return;
          copy.textContent = 'Copy'; status.textContent = 'Copy unavailable. Select the code to copy it.';
        } finally {
          if (rendered.get(container)?.generation === generation) copy.disabled = false;
        }
      });
      return shell;
    }
    const node = make(plan.tag, undefined, plan.attributes);
    node.append(...plan.children.map(mount));
    return node;
  };
  const nodes = presentation.nodes.map(mount);
  container.classList.toggle('formatted-reply', presentation.formatted);
  container.replaceChildren(...nodes);
  rendered.set(container, { text, markdown, generation });
  return true;
}
