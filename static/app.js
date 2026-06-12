// ── State ───────────────────────────────────────────────────────────────────
let currentChatId = null;
let pendingFiles = [];   // [{name, media_type, data, isImage}]
let isStreaming = false;

const MAX_FILE_SIZE = 10 * 1024 * 1024; // 10 MB
const MAX_FILES = 10;

const REJECTED_TYPES = new Set([
  'application/pdf',
  'application/zip',
  'application/x-zip-compressed',
  'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
  'application/msword',
]);

// ── Init ────────────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', init);

async function init() {
  const token = localStorage.getItem('token');
  if (!token) { window.location.href = '/'; return; }

  document.getElementById('nav-username').textContent = localStorage.getItem('username') || '';

  await loadChats();

  // Route: /chats/:id
  const m = window.location.pathname.match(/^\/chats\/(.+)$/);
  if (m) {
    await loadChat(m[1]);
  } else {
    showWelcome();
  }

  // Input auto-resize
  const ta = document.getElementById('message-input');
  ta.addEventListener('input', () => {
    ta.style.height = 'auto';
    ta.style.height = Math.min(ta.scrollHeight, 160) + 'px';
  });

  // Send on Enter (not Shift+Enter)
  ta.addEventListener('keydown', e => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMessage(); }
  });

  // Paste image
  ta.addEventListener('paste', handlePaste);
  document.addEventListener('paste', handlePaste);
}

// ── Auth ────────────────────────────────────────────────────────────────────
function signOut() {
  localStorage.removeItem('token');
  localStorage.removeItem('username');
  window.location.href = '/';
}

// ── API helpers ──────────────────────────────────────────────────────────────
async function api(path, options = {}) {
  const token = localStorage.getItem('token');
  const res = await fetch(path, {
    ...options,
    headers: {
      'Content-Type': 'application/json',
      'Authorization': `Bearer ${token}`,
      ...(options.headers || {}),
    },
  });
  if (res.status === 401) { signOut(); return; }
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(err.detail || 'Request failed');
  }
  return res.json();
}

// ── Chat list ────────────────────────────────────────────────────────────────
async function loadChats() {
  try {
    const chats = await api('/api/chats');
    renderSidebar(chats);
  } catch { /* ignore */ }
}

function renderSidebar(chats) {
  const list = document.getElementById('sidebar-list');
  if (!chats.length) {
    list.innerHTML = '<div style="padding:.8rem;font-size:.8rem;color:#64748b;text-align:center">No chats yet</div>';
    return;
  }
  list.innerHTML = chats.map(c => `
    <div class="chat-item ${c.id === currentChatId ? 'active' : ''}"
         data-id="${c.id}"
         onclick="loadChat('${c.id}')">
      <div class="chat-item-title">${escHtml(c.title)}</div>
      <div class="chat-item-date">${formatDate(c.updated_at)}</div>
    </div>`).join('');
}

function updateSidebarActive() {
  document.querySelectorAll('.chat-item').forEach(el => {
    el.classList.toggle('active', el.dataset.id === currentChatId);
  });
}

// ── Load existing chat ───────────────────────────────────────────────────────
async function loadChat(chatId) {
  currentChatId = chatId;
  history.pushState(null, '', `/chats/${chatId}`);
  updateSidebarActive();

  const messagesEl = document.getElementById('messages');
  messagesEl.innerHTML = '<div class="welcome" style="height:100%"><div class="thinking"><span></span><span></span><span></span></div></div>';

  try {
    const data = await api(`/api/chats/${chatId}/messages`);
    document.title = `Aida — ${data.chat.title}`;
    renderMessages(data.messages);
  } catch (e) {
    messagesEl.innerHTML = `<div class="welcome"><p style="color:#ef4444">${escHtml(e.message)}</p></div>`;
  }
}

function renderMessages(messages) {
  const el = document.getElementById('messages');
  if (!messages.length) { showWelcome(); return; }
  el.innerHTML = messages.map(m => buildMessageHtml(m.role, m.content)).join('');
  scrollBottom();
}

// ── New chat / Welcome ───────────────────────────────────────────────────────
function newChat() {
  currentChatId = null;
  history.pushState(null, '', '/ask');
  document.title = 'Aida';
  updateSidebarActive();
  showWelcome();
}

function showWelcome() {
  document.getElementById('messages').innerHTML = `
    <div class="welcome">
      <h2>Aida</h2>
      <p>Your Python programming assistant. Ask me anything!</p>
      <div class="welcome-examples">
        <div class="example-chip" onclick="useExample(this)">How do list comprehensions work?</div>
        <div class="example-chip" onclick="useExample(this)">Explain Python decorators</div>
        <div class="example-chip" onclick="useExample(this)">What is the difference between a list and a tuple?</div>
        <div class="example-chip" onclick="useExample(this)">How do I handle exceptions in Python?</div>
        <div class="example-chip" onclick="useExample(this)">Debug my code for me</div>
        <div class="example-chip" onclick="useExample(this)">Explain recursion with an example</div>
      </div>
    </div>`;
}

function useExample(el) {
  const input = document.getElementById('message-input');
  input.value = el.textContent;
  input.focus();
  input.style.height = 'auto';
  input.style.height = Math.min(input.scrollHeight, 160) + 'px';
}

// ── File handling ────────────────────────────────────────────────────────────
async function handleFileSelect(files) {
  for (const f of Array.from(files)) {
    await validateAndAddFile(f);
  }
  document.getElementById('file-input').value = '';
}

async function handlePaste(e) {
  const items = e.clipboardData?.items;
  if (!items) return;
  for (const item of Array.from(items)) {
    if (item.type.startsWith('image/')) {
      e.preventDefault();
      const blob = item.getAsFile();
      if (blob) {
        const named = new File([blob], `pasted-image.${item.type.split('/')[1]}`, { type: item.type });
        await validateAndAddFile(named);
      }
    }
  }
}

async function validateAndAddFile(file) {
  if (pendingFiles.length >= MAX_FILES) {
    showToast(`Maximum ${MAX_FILES} files per message`);
    return;
  }
  if (file.size > MAX_FILE_SIZE) {
    showToast(`${file.name} exceeds 10 MB limit`);
    return;
  }
  if (REJECTED_TYPES.has(file.type)) {
    showToast(`${file.name}: unsupported file type (PDF, ZIP, DOCX not allowed)`);
    return;
  }

  const isImage = file.type.startsWith('image/');
  const allowedImageTypes = new Set(['image/png', 'image/jpeg', 'image/gif', 'image/webp']);
  if (isImage && !allowedImageTypes.has(file.type)) {
    showToast(`${file.name}: unsupported image format`);
    return;
  }

  const data = await readFileAsBase64(file);
  pendingFiles.push({ name: file.name, media_type: file.type, data, isImage });
  renderFilePreview();
}

function readFileAsBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result.split(',')[1]);
    reader.onerror = reject;
    reader.readAsDataURL(file);
  });
}

function renderFilePreview() {
  const el = document.getElementById('file-preview');
  el.innerHTML = pendingFiles.map((f, i) => `
    <div class="file-chip">
      <span>${f.isImage ? '🖼️' : '📄'}</span>
      <span title="${escHtml(f.name)}">${escHtml(f.name)}</span>
      <button class="file-chip-remove" onclick="removeFile(${i})" title="Remove">✕</button>
    </div>`).join('');
}

function removeFile(idx) {
  pendingFiles.splice(idx, 1);
  renderFilePreview();
}

// ── Send message ─────────────────────────────────────────────────────────────
async function sendMessage() {
  if (isStreaming) return;

  const input = document.getElementById('message-input');
  const message = input.value.trim();
  if (!message && pendingFiles.length === 0) return;

  isStreaming = true;
  input.disabled = true;
  document.getElementById('send-btn').disabled = true;

  // Snapshot files, clear state
  const filesSnapshot = [...pendingFiles];
  pendingFiles = [];
  renderFilePreview();

  // Show user message immediately
  appendUserMessage(message, filesSnapshot);

  // Reset input
  input.value = '';
  input.style.height = 'auto';

  // Create assistant placeholder
  const assistantEl = appendAssistantPlaceholder();
  let accumulated = '';

  try {
    const token = localStorage.getItem('token');
    const res = await fetch('/api/stream', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${token}` },
      body: JSON.stringify({
        chat_id: currentChatId || null,
        message,
        files: filesSnapshot.map(({ name, media_type, data }) => ({ name, media_type, data })),
      }),
    });

    if (res.status === 401) { signOut(); return; }
    if (!res.ok) {
      const err = await res.json().catch(() => ({ detail: res.statusText }));
      throw new Error(err.detail || 'Request failed');
    }

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buf = '';

    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      const lines = buf.split('\n');
      buf = lines.pop() ?? '';

      for (const line of lines) {
        if (!line.startsWith('data: ')) continue;
        let ev;
        try { ev = JSON.parse(line.slice(6)); } catch { continue; }

        switch (ev.type) {
          case 'chat_created':
            currentChatId = ev.chat_id;
            history.pushState(null, '', `/chats/${ev.chat_id}`);
            // Pre-populate sidebar with placeholder
            prependSidebarItem(ev.chat_id, 'New Chat');
            updateSidebarActive();
            break;

          case 'token':
            accumulated += ev.content;
            updateStreamingEl(assistantEl, accumulated);
            break;

          case 'title':
            updateChatTitle(currentChatId, ev.title);
            document.title = `Aida — ${ev.title}`;
            break;

          case 'done':
            finaliseAssistantEl(assistantEl, accumulated);
            // Refresh chat list for accurate ordering / titles
            loadChats();
            break;

          case 'error':
            setElError(assistantEl, ev.message);
            break;
        }
      }
    }
  } catch (e) {
    setElError(assistantEl, e.message || 'Failed to get a response');
  } finally {
    isStreaming = false;
    input.disabled = false;
    document.getElementById('send-btn').disabled = false;
    input.focus();
  }
}

// ── DOM helpers ──────────────────────────────────────────────────────────────
function appendUserMessage(text, files) {
  const messagesEl = document.getElementById('messages');

  // Replace welcome screen on first message
  if (messagesEl.querySelector('.welcome')) {
    messagesEl.innerHTML = '';
  }

  const items = [
    ...files.map(f => ({ type: f.isImage ? 'image' : 'file', filename: f.name })),
    ...(text ? [{ type: 'text', text }] : []),
  ];
  messagesEl.insertAdjacentHTML('beforeend', buildMessageHtml('user', items));
  scrollBottom();
}

function appendAssistantPlaceholder() {
  const messagesEl = document.getElementById('messages');
  messagesEl.insertAdjacentHTML('beforeend', `
    <div class="message assistant" id="msg-streaming">
      <div class="message-label">Aida</div>
      <div class="message-body">
        <div class="thinking"><span></span><span></span><span></span></div>
      </div>
    </div>`);
  scrollBottom();
  return document.getElementById('msg-streaming');
}

function updateStreamingEl(el, text) {
  el.querySelector('.message-body').innerHTML = parseMarkdown(text);
  scrollBottom();
}

function finaliseAssistantEl(el, text) {
  el.removeAttribute('id');
  el.querySelector('.message-body').innerHTML = parseMarkdown(text);
  scrollBottom();
}

function setElError(el, msg) {
  el.removeAttribute('id');
  el.querySelector('.message-body').innerHTML =
    `<span style="color:#ef4444">Error: ${escHtml(msg)}</span>`;
}

function buildMessageHtml(role, content) {
  const label = role === 'user' ? 'You' : 'Aida';
  let body;

  if (role === 'assistant') {
    body = parseMarkdown(typeof content === 'string' ? content : '');
  } else {
    // content is array of items
    const items = Array.isArray(content) ? content : [{ type: 'text', text: String(content) }];
    body = items.map(item => {
      if (item.type === 'text') return `<p>${escHtml(item.text)}</p>`;
      if (item.type === 'file') return `<span class="attach-badge">📄 ${escHtml(item.filename)}</span>`;
      if (item.type === 'image') return `<span class="attach-badge">🖼️ ${escHtml(item.filename)}</span>`;
      return '';
    }).join('');
  }

  return `<div class="message ${role}">
    <div class="message-label">${label}</div>
    <div class="message-body">${body}</div>
  </div>`;
}

function prependSidebarItem(chatId, title) {
  const list = document.getElementById('sidebar-list');
  const placeholder = list.querySelector('[style]'); // "No chats yet" node
  if (placeholder) placeholder.remove();

  const div = document.createElement('div');
  div.className = 'chat-item';
  div.dataset.id = chatId;
  div.onclick = () => loadChat(chatId);
  div.innerHTML = `<div class="chat-item-title">${escHtml(title)}</div>
    <div class="chat-item-date">Just now</div>`;
  list.prepend(div);
}

function updateChatTitle(chatId, title) {
  const el = document.querySelector(`.chat-item[data-id="${chatId}"] .chat-item-title`);
  if (el) el.textContent = title;
}

function scrollBottom() {
  const el = document.getElementById('messages');
  const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
  if (atBottom) el.scrollTop = el.scrollHeight;
}

// ── Markdown renderer ────────────────────────────────────────────────────────
function parseMarkdown(text) {
  if (!text) return '';
  let html = '';
  let i = 0;

  while (i < text.length) {
    if (text.startsWith('```', i)) {
      i += 3;
      const nlIdx = text.indexOf('\n', i);
      const lang = nlIdx !== -1 ? text.slice(i, nlIdx).trim() : '';
      const codeStart = nlIdx !== -1 ? nlIdx + 1 : i;
      const closeIdx = text.indexOf('```', codeStart);

      if (closeIdx !== -1) {
        const code = text.slice(codeStart, closeIdx);
        html += codeBlockHtml(lang, code, false);
        i = closeIdx + 3;
        // Skip immediately following newline
        if (text[i] === '\n') i++;
      } else {
        // Streaming incomplete block
        const code = text.slice(codeStart);
        html += codeBlockHtml(lang, code, true);
        i = text.length;
      }
    } else {
      const next = text.indexOf('```', i);
      const chunk = next === -1 ? text.slice(i) : text.slice(i, next);
      html += renderTextSection(chunk);
      i = next === -1 ? text.length : next;
    }
  }
  return html;
}

function codeBlockHtml(lang, code, streaming) {
  const streamCls = streaming ? ' streaming' : '';
  return `<div class="code-block${streamCls}"><div class="code-lang">${escHtml(lang) || 'code'}</div><code>${escHtml(code)}</code></div>`;
}

function renderTextSection(text) {
  if (!text.trim()) return '';
  const lines = text.split('\n');
  let out = '';
  let listType = null; // 'ul' | 'ol'

  const closeList = () => {
    if (listType) { out += `</${listType}>`; listType = null; }
  };

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];

    if (!line.trim()) {
      closeList();
      // Only insert break if there's content on either side
      if (i > 0 && i < lines.length - 1) out += '<br>';
      continue;
    }

    const h = line.match(/^(#{1,4})\s+(.*)/);
    if (h) {
      closeList();
      const lvl = Math.min(h[1].length + 2, 6);
      out += `<h${lvl}>${renderInline(h[2])}</h${lvl}>`;
      continue;
    }

    const ul = line.match(/^[-*]\s+(.*)/);
    if (ul) {
      if (listType !== 'ul') { closeList(); out += '<ul>'; listType = 'ul'; }
      out += `<li>${renderInline(ul[1])}</li>`;
      continue;
    }

    const ol = line.match(/^\d+\.\s+(.*)/);
    if (ol) {
      if (listType !== 'ol') { closeList(); out += '<ol>'; listType = 'ol'; }
      out += `<li>${renderInline(ol[1])}</li>`;
      continue;
    }

    closeList();
    out += `<p>${renderInline(line)}</p>`;
  }

  closeList();
  return out;
}

function renderInline(text) {
  let t = escHtml(text);
  // Bold
  t = t.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
  t = t.replace(/__([^_]+)__/g, '<strong>$1</strong>');
  // Italic
  t = t.replace(/\*([^*]+)\*/g, '<em>$1</em>');
  t = t.replace(/_([^_]+)_/g, '<em>$1</em>');
  // Inline code
  t = t.replace(/`([^`]+)`/g, '<code class="inline-code">$1</code>');
  return t;
}

// ── Toast ────────────────────────────────────────────────────────────────────
let _toastTimer = null;
function showToast(msg) {
  let t = document.querySelector('.error-toast');
  if (!t) {
    t = document.createElement('div');
    t.className = 'error-toast';
    document.body.appendChild(t);
  }
  t.textContent = msg;
  clearTimeout(_toastTimer);
  _toastTimer = setTimeout(() => t.remove(), 4000);
}

// ── Utilities ────────────────────────────────────────────────────────────────
function escHtml(s) {
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function formatDate(iso) {
  const d = new Date(iso);
  const now = new Date();
  const diff = now - d;
  if (diff < 60000) return 'Just now';
  if (diff < 3600000) return `${Math.floor(diff / 60000)}m ago`;
  if (diff < 86400000) return `${Math.floor(diff / 3600000)}h ago`;
  if (diff < 604800000) return `${Math.floor(diff / 86400000)}d ago`;
  return d.toLocaleDateString();
}
