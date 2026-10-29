// ============================================================
//  Nova AI Assistant — script.js  (v4 — Premium UI)
//  API_BASE_URL is set in config.js and loaded before this file.
// ============================================================

// ── DOM refs ──────────────────────────────────────────────────
const chatBox       = document.getElementById('chatBox');
const userInput     = document.getElementById('userInput');
const sendBtn       = document.getElementById('sendBtn');
const voiceBtn      = document.getElementById('voiceBtn');
const statusBar     = document.getElementById('statusBar');
const statusLabel   = document.getElementById('statusLabel');
const mobileStatusDot = document.getElementById('mobileStatusDot');
const emptyState    = document.getElementById('emptyState');
const confirmModal  = document.getElementById('confirmModal');
const confirmIcon   = document.getElementById('confirmIcon');
const confirmTitle  = document.getElementById('confirmTitle');
const confirmBody   = document.getElementById('confirmBody');
const confirmDetail = document.getElementById('confirmDetail');
const confirmYesBtn = document.getElementById('confirmYesBtn');
const confirmNoBtn  = document.getElementById('confirmNoBtn');

// ── State ──────────────────────────────────────────────────────
let isWaitingForBot = false;
let isListening     = false;
let recognition     = null;
let thinkingEl      = null;

// ── Conversation state ────────────────────────────────────────
// currentConvId: UUID of the active conversation, or null for a fresh chat.
// Set from /chat response.conversation_id after every send.
// Reset to null by newChat().
let currentConvId = null;

// ── Image / vision state ──────────────────────────────────────
// selectedFile is now managed in the FILE / VISION HELPERS section below.
// This stub is kept as a comment so old readers understand the migration.
// selectedImageFile is a legacy alias provided via Object.defineProperty there.

// ── Auth helpers ──────────────────────────────────────────────

/**
 * Return the logged-in user's first name, or null if not available.
 * Extracts the first space-delimited word from full_name.
 * Single source of truth — used by sendMessage, voice path, and welcome.
 */
function _getUserFirstName() {
  // 1. Use the saved display name (set by the name prompt)
  if (typeof getDisplayName === 'function') {
    const dn = getDisplayName();
    if (dn) return dn.trim().split(/\s+/)[0];
  }
  // 2. Fall back to full_name from session (only if it's not an email)
  const _u = (typeof getUser === 'function') ? getUser() : null;
  if (!_u) return null;
  if (_u.full_name && !_u.full_name.includes('@') && _u.full_name.trim()) {
    return _u.full_name.trim().split(/\s+/)[0];
  }
  return null;
}

// ============================================================
//  FILE / VISION HELPERS
//  Handles images (→ /vision), PDFs and TXT files (→ /upload).
//  The existing image vision flow is fully preserved.
// ============================================================

// ── Limits & allowed types ────────────────────────────────────
const MAX_IMAGE_BYTES   = 4 * 1024 * 1024;  // 4 MB — matches /vision backend limit
const MAX_DOC_BYTES     = 2 * 1024 * 1024;  // 2 MB — matches /upload backend limit
const ALLOWED_IMG_TYPES = new Set(['image/jpeg', 'image/jpg', 'image/png', 'image/webp']);
const ALLOWED_DOC_TYPES = new Set(['application/pdf', 'text/plain']);

// ── State ─────────────────────────────────────────────────────
// selectedFile: { file: File, kind: 'image'|'pdf'|'txt' }  or  null
let selectedFile = null;

// Keep the old name as an alias so any legacy call sites still work.
Object.defineProperty(window, 'selectedImageFile', {
  get() { return selectedFile?.kind === 'image' ? selectedFile.file : null; },
  set(v) { selectedFile = v ? { file: v, kind: 'image' } : null; },
  configurable: true,
});

// ── Open the OS file picker ───────────────────────────────────
/** Open the OS file picker (replaces triggerImagePicker). */
function triggerFilePicker() {
  const input = document.getElementById('fileInput');
  if (input) { input.value = ''; input.click(); }
}

/** Legacy alias used by any inline onclick="triggerImagePicker()" in the HTML. */
function triggerImagePicker() { triggerFilePicker(); }

/**
 * Called when the user selects a file.
 * Validates type and size client-side, then shows the preview strip.
 */
function handleFileSelected(event) {
  const file = event.target.files[0];
  if (!file) return;

  const mime = file.type.toLowerCase();

  if (ALLOWED_IMG_TYPES.has(mime)) {
    // ── Image ──────────────────────────────────────────────────
    if (file.size > MAX_IMAGE_BYTES) {
      const mb = (file.size / (1024 * 1024)).toFixed(1);
      addMessage(`Image too large (${mb} MB). Maximum allowed size is 4 MB.`, 'error');
      return;
    }
    selectedFile = { file, kind: 'image' };
    _showFilePreview(file, 'image');

  } else if (ALLOWED_DOC_TYPES.has(mime)) {
    // ── PDF / TXT ──────────────────────────────────────────────
    if (file.size > MAX_DOC_BYTES) {
      const mb = (file.size / (1024 * 1024)).toFixed(1);
      addMessage(`File too large (${mb} MB). Maximum allowed size for documents is 2 MB.`, 'error');
      return;
    }
    const kind = mime === 'application/pdf' ? 'pdf' : 'txt';
    selectedFile = { file, kind };
    _showFilePreview(file, kind);

  } else {
    addMessage(
      `Unsupported file type (${mime || 'unknown'}). Please choose a JPG/PNG/WEBP image, a PDF, or a TXT file.`,
      'error'
    );
  }
}

/** Legacy handler alias referenced by the old onchange="handleImageSelected(event)". */
function handleImageSelected(event) { handleFileSelected(event); }

// ── Preview strip ─────────────────────────────────────────────

/**
 * Show the file preview strip above the input bar.
 * kind: 'image' | 'pdf' | 'txt'
 */
function _showFilePreview(file, kind) {
  const bar       = document.getElementById('filePreviewBar');
  const thumb     = document.getElementById('filePreviewThumb');
  const docIcon   = document.getElementById('filePreviewDocIcon');
  const label     = document.getElementById('filePreviewLabel');
  const attachBtn = document.getElementById('attachBtn');
  if (!bar) return;

  if (kind === 'image') {
    // Show thumbnail, hide doc icon
    if (thumb) {
      const objectUrl = URL.createObjectURL(file);
      thumb.src = objectUrl;
      thumb.onload = () => URL.revokeObjectURL(objectUrl);
      thumb.hidden = false;
    }
    if (docIcon) docIcon.hidden = true;
  } else {
    // Show doc icon, hide thumbnail
    if (thumb) { thumb.src = ''; thumb.hidden = true; }
    if (docIcon) docIcon.hidden = false;
  }

  const kb = (file.size / 1024).toFixed(0);
  const typeTag = kind === 'pdf' ? 'PDF' : kind === 'txt' ? 'TXT' : 'Image';
  if (label) label.textContent = `${file.name}  (${typeTag} · ${kb} KB)`;

  bar.hidden = false;
  if (attachBtn) attachBtn.classList.add('has-image');
}

/** Remove the selected file and hide the preview strip. */
function removeSelectedFile() {
  selectedFile = null;

  const bar       = document.getElementById('filePreviewBar');
  const thumb     = document.getElementById('filePreviewThumb');
  const docIcon   = document.getElementById('filePreviewDocIcon');
  const input     = document.getElementById('fileInput');
  const attachBtn = document.getElementById('attachBtn');

  if (bar)       bar.hidden   = true;
  if (thumb)     { thumb.src  = ''; thumb.hidden = true; }
  if (docIcon)   docIcon.hidden = true;
  if (input)     input.value  = '';
  if (attachBtn) attachBtn.classList.remove('has-image');
}

/** Legacy remove function referenced by older code. */
function removeSelectedImage() { removeSelectedFile(); }

// ── Chat bubble helpers ───────────────────────────────────────

/**
 * Add an image bubble to the chat showing the user's uploaded image.
 * Identical to the original — preserved for the vision flow.
 */
function addImageMessage(file) {
  hideEmptyState();

  const wrapper = document.createElement('div');
  wrapper.className = 'message user-message';

  const bubble = document.createElement('div');
  bubble.className = 'bubble image-bubble';

  const img = document.createElement('img');
  img.className = 'chat-image-thumb';
  img.alt = 'Uploaded image';

  const objectUrl = URL.createObjectURL(file);
  img.src = objectUrl;
  img.onload = () => URL.revokeObjectURL(objectUrl);

  bubble.appendChild(img);
  wrapper.appendChild(bubble);
  chatBox.appendChild(wrapper);
  scrollToBottom();
  return wrapper;
}

/**
 * Add a document bubble showing the file name and type badge.
 */
function addDocMessage(file, kind) {
  hideEmptyState();

  const wrapper = document.createElement('div');
  wrapper.className = 'message user-message';

  const bubble = document.createElement('div');
  bubble.className = 'bubble doc-bubble';

  const icon = document.createElement('span');
  icon.className   = 'doc-bubble-icon';
  icon.textContent = kind === 'pdf' ? '📄' : '📝';
  icon.setAttribute('aria-hidden', 'true');

  const info = document.createElement('div');
  info.className = 'doc-bubble-info';

  const name = document.createElement('span');
  name.className   = 'doc-bubble-name';
  name.textContent = file.name;

  const meta = document.createElement('span');
  meta.className   = 'doc-bubble-meta';
  meta.textContent = `${kind.toUpperCase()} · ${(file.size / 1024).toFixed(0)} KB`;

  info.appendChild(name);
  info.appendChild(meta);
  bubble.appendChild(icon);
  bubble.appendChild(info);
  wrapper.appendChild(bubble);
  chatBox.appendChild(wrapper);
  scrollToBottom();
  return wrapper;
}

// ── Send helpers ──────────────────────────────────────────────

/**
 * POST an image (+ optional text prompt) to /vision.
 * This is the original vision flow — unchanged.
 */
async function sendVision(promptText) {
  const sf = selectedFile;
  if (!sf || sf.kind !== 'image') return;
  const file = sf.file;

  addImageMessage(file);
  if (promptText) addMessage(promptText, 'user');

  removeSelectedFile();
  lockInput();
  showThinking();
  closeSidebar();

  const formData = new FormData();
  formData.append('image', file);
  formData.append('prompt', promptText || 'What is in this image?');

  const firstName = _getUserFirstName();
  if (firstName) formData.append('user_name', firstName);

  const headers = {};
  const authHeader = (typeof getAuthHeader === 'function') ? getAuthHeader() : null;
  if (authHeader) headers['Authorization'] = authHeader;

  try {
    const res = await fetch(`${window.API_BASE_URL}/vision`, {
      method:  'POST',
      headers,
      body:    formData,
      signal:  AbortSignal.timeout(40_000),
    });

    hideThinking();
    const data = await res.json();

    if (!res.ok) {
      const errMsg = data?.response || `Server error (${res.status}). Thodi der baad try karo.`;
      addMessage(errMsg, 'error');
      setStatus('offline', `Error (${res.status})`);
      unlockInput();
      return;
    }

    setStatus('online', 'Nova Online');
    const reply = data?.response || 'Image dekh li, lekin koi response nahi mila.';
    addMessage(reply, 'bot');
    speak(reply);

  } catch (err) {
    hideThinking();
    if (err.name === 'AbortError' || err.name === 'TimeoutError') {
      addMessage('Vision request timeout ho gayi. Image chhoti karke dobara try karo.', 'error');
    } else {
      addMessage('Vision request fail hua. Backend check karo.', 'error');
    }
    setStatus('offline', 'Server offline');
  }

  unlockInput();
}

/**
 * POST a PDF or TXT file (+ optional text prompt) to /upload.
 */
async function sendDocument(promptText) {
  const sf = selectedFile;
  if (!sf || sf.kind === 'image') return;
  const { file, kind } = sf;

  addDocMessage(file, kind);
  if (promptText) addMessage(promptText, 'user');

  removeSelectedFile();
  lockInput();
  showThinking();
  closeSidebar();

  const formData = new FormData();
  formData.append('file', file);
  formData.append('prompt', promptText || '');

  const firstName = _getUserFirstName();
  if (firstName) formData.append('user_name', firstName);
  if (currentConvId) formData.append('conversation_id', currentConvId);

  // Forward AI prefs so document answers respect language / style settings
  const _docPrefs = (typeof getPrefs === 'function') ? getPrefs() : {};
  if (_docPrefs.language)       formData.append('language',       _docPrefs.language);
  if (_docPrefs.response_style) formData.append('response_style', _docPrefs.response_style);

  const headers = {};
  const authHeader = (typeof getAuthHeader === 'function') ? getAuthHeader() : null;
  if (authHeader) headers['Authorization'] = authHeader;

  try {
    const res = await fetch(`${window.API_BASE_URL}/upload`, {
      method:  'POST',
      headers,
      body:    formData,
      signal:  AbortSignal.timeout(60_000),
    });

    hideThinking();
    const data = await res.json();

    if (!res.ok) {
      // Map common HTTP status codes to friendly messages
      const errMsg = data?.response
        || (res.status === 413 ? 'File too large. Maximum size is 2 MB for documents.' : null)
        || (res.status === 422 ? 'Could not read the file. Try a different one.' : null)
        || (res.status === 400 ? (data?.detail || 'Invalid file.') : null)
        || `Server error (${res.status}). Thodi der baad try karo.`;
      addMessage(errMsg, 'error');
      setStatus('offline', `Error (${res.status})`);
      unlockInput();
      return;
    }

    // Track conversation id (same as /chat)
    if (data.conversation_id) {
      currentConvId = data.conversation_id;
      loadConversations();
    }

    setStatus('online', 'Nova Online');
    await completeThinking();
    showAgentStepsPill(data.agent_steps);
    await handleServerResponse(data);

  } catch (err) {
    hideThinking();
    if (err.name === 'AbortError' || err.name === 'TimeoutError') {
      addMessage('Document upload timeout ho gayi. File chhoti karke dobara try karo.', 'error');
    } else {
      addMessage('Document upload fail hua. Backend check karo.', 'error');
    }
    setStatus('offline', 'Server offline');
  }

  unlockInput();
}


// ============================================================
//  AUTH INIT
//  Runs immediately on page load.
//  1. Redirects to login.html if no valid session exists.
//     Attempts a silent token refresh before redirecting.
//  2. Populates the sidebar profile area from the stored session.
//  3. Personalises the empty-state heading with the user's first name.
//  4. Fetches a personalised welcome message from Nova via /chat/welcome.
//
//  Name is now collected at signup — no post-login name prompt needed.
// ============================================================
(async function initAuth() {
  // ── 1. Auth guard with silent token refresh ──────────────────
  if (!isLoggedIn()) {
    const session         = (typeof getSession      === 'function') ? getSession()     : null;
    const hasRefreshToken = session && session.refresh_token;

    if (hasRefreshToken && typeof refreshSession === 'function') {
      const refreshed = await refreshSession();
      if (!refreshed) {
        if (typeof clearSession === 'function') clearSession();
        window.location.href = 'login.html';
        return;
      }
      // Refreshed — fall through to populate UI
    } else {
      if (typeof clearSession === 'function') clearSession();
      window.location.href = 'login.html';
      return;
    }
  }

  // ── 2. Populate sidebar profile area ────────────────────────
  const user = (typeof getUser === 'function') ? getUser() : null;

  const nameEl   = document.getElementById('profileName');
  const avatarEl = document.getElementById('profileAvatar');

  if (user && nameEl && avatarEl) {
    const dn          = (typeof getDisplayName === 'function') ? getDisplayName() : null;
    const displayName = dn
      || (user.full_name && !user.full_name.includes('@') ? user.full_name : null)
      || user.email
      || 'User';

    nameEl.textContent = displayName;

    if (user.avatar_url) {
      const img      = document.createElement('img');
      img.src        = user.avatar_url;
      img.alt        = displayName;
      img.style.cssText = 'width:100%;height:100%;object-fit:cover;border-radius:50%;';
      img.onerror    = () => { avatarEl.textContent = displayName.charAt(0).toUpperCase(); };
      avatarEl.innerHTML = '';
      avatarEl.appendChild(img);
    } else {
      avatarEl.textContent = displayName.charAt(0).toUpperCase();
    }
  } else if (nameEl) {
    nameEl.textContent = 'Guest';
  }

  // ── 3. Personalise empty-state heading ───────────────────────
  const titleEl   = document.getElementById('emptyTitle');
  const firstName = _getUserFirstName();
  if (titleEl && firstName) {
    titleEl.textContent = `Hey, ${firstName} 👋`;
  }

  // ── 4. Welcome message (non-blocking) ────────────────────────
  // Small delay so the UI finishes painting before the fetch fires.
  setTimeout(_fetchWelcomeMessage, 600);
})();

// ============================================================
//  WELCOME MESSAGE — fetched from /chat/welcome on page load
// ============================================================

/**
 * POST to /chat/welcome with the user's first name.
 * On success, displays Nova's greeting as the first bot message.
 * On failure, silently skips — the empty state is still visible.
 */
async function _fetchWelcomeMessage() {
  // Don't fire if a real chat is already in progress
  if (isWaitingForBot) return;

  const firstName = _getUserFirstName();
  const headers   = { 'Content-Type': 'application/json' };

  const authHeader = (typeof getAuthHeader === 'function') ? getAuthHeader() : null;
  if (authHeader) headers['Authorization'] = authHeader;

  try {
    const res = await fetch(`${window.API_BASE_URL}/chat/welcome`, {
      method:  'POST',
      headers,
      body:    JSON.stringify({ user_name: firstName || undefined }),
      signal:  AbortSignal.timeout(12_000),
    });

    if (!res.ok) {
      // Silently skip — a missing welcome message is non-fatal
      console.warn('[nova] Welcome message fetch failed:', res.status);
      return;
    }

    const data = await res.json();

    // Only display if we got a proper message back and chat is still empty
    if (data?.type === 'message' && data?.response) {
      // If the user already started typing or sent a message, skip
      if (isWaitingForBot || document.querySelector('.message')) return;
      addMessage(data.response, 'bot');
      speak(data.response);
    }
  } catch (err) {
    // Non-fatal: timeout, network error, etc. — just skip the welcome
    console.warn('[nova] Welcome message error (non-fatal):', err?.message || err);
  }
};

// ============================================================
//  EMPTY STATE
// ============================================================

/** Hide the empty state welcome screen once a message exists. */
function hideEmptyState() {
  if (emptyState && emptyState.parentNode) {
    emptyState.style.opacity = '0';
    emptyState.style.transition = 'opacity 0.25s ease';
    setTimeout(() => {
      if (emptyState.parentNode) emptyState.remove();
    }, 250);
  }
}

/** Called when a suggestion chip is clicked. */
function useSuggestion(btn) {
  const text = btn.querySelector('.chip-text')?.textContent?.trim();
  if (!text) return;
  userInput.value = text;
  userInput.focus();
  // Auto-send
  sendMessage();
}

// ============================================================
//  SIDEBAR (mobile toggle)
// ============================================================
function toggleSidebar() {
  const sidebar  = document.getElementById('sidebar');
  const overlay  = document.getElementById('sidebarOverlay');
  const toggle   = document.getElementById('sidebarToggle');
  const isOpen   = sidebar.classList.contains('open');

  if (isOpen) {
    closeSidebar();
  } else {
    sidebar.classList.add('open');
    overlay.classList.add('visible');
    toggle.setAttribute('aria-expanded', 'true');
  }
}

function closeSidebar() {
  const sidebar = document.getElementById('sidebar');
  const overlay = document.getElementById('sidebarOverlay');
  const toggle  = document.getElementById('sidebarToggle');
  sidebar.classList.remove('open');
  overlay.classList.remove('visible');
  toggle.setAttribute('aria-expanded', 'false');
}

// ============================================================
//  TEXTAREA AUTO-RESIZE
// ============================================================
function autoResizeTextarea(el) {
  el.style.height = 'auto';
  el.style.height = Math.min(el.scrollHeight, 160) + 'px';
}

/** Handle Enter (send) vs Shift+Enter (newline) */
function handleInputKeydown(e) {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    sendMessage();
  }
}

// ============================================================
//  CONNECTION STATUS
// ============================================================
async function checkConnection() {
  setStatus('checking', 'Connecting…');
  try {
    const res = await fetch(`${window.API_BASE_URL}/`, {
      method: 'GET',
      signal: AbortSignal.timeout(5000),
    });
    if (res.ok) {
      setStatus('online', 'Nova Online');
    } else {
      setStatus('offline', `Server error (${res.status})`);
    }
  } catch {
    setStatus('offline', 'Server offline');
  }
}

function setStatus(state, label) {
  // Sidebar status bar
  statusBar.className     = `sidebar-status ${state}`;
  statusLabel.textContent = label;

  // Mobile dot
  if (mobileStatusDot) {
    mobileStatusDot.className = `mobile-status-dot ${state}`;
  }
}

checkConnection();
setInterval(checkConnection, 30_000);

// ============================================================
//  CHAT BUBBLE HELPERS
// ============================================================

/**
 * Add a text message bubble.
 * type: 'user' | 'bot' | 'error'
 *
 * Bot bubbles are rendered through _renderMarkdown() which converts
 * a safe subset of markdown to HTML. User and error bubbles use
 * .textContent to ensure no HTML injection.
 */
function addMessage(text, type) {
  hideEmptyState();

  const wrapper = document.createElement('div');
  wrapper.className = `message ${type}-message`;

  if (type === 'bot' || type === 'error') {
    const avatar = document.createElement('div');
    avatar.className = 'bot-avatar';
    avatar.setAttribute('aria-hidden', 'true');
    avatar.textContent = 'N';
    wrapper.appendChild(avatar);
  }

  const bubble = document.createElement('div');
  bubble.className = 'bubble';

  if (type === 'bot') {
    // Safe markdown render — only for Nova's own output
    bubble.innerHTML = _renderMarkdown(text);
    bubble.classList.add('bubble--md');
  } else {
    // User input and errors: plain text, never HTML
    bubble.textContent = text;
  }

  if (type === 'error') bubble.setAttribute('role', 'alert');
  wrapper.appendChild(bubble);

  chatBox.appendChild(wrapper);
  scrollToBottom();
  return wrapper;
}

/**
 * Convert a safe subset of markdown to HTML for bot bubbles.
 *
 * Supported syntax (in render order):
 *   ## Heading 2        → <h2>
 *   ### Heading 3       → <h3>
 *   **bold**            → <strong>
 *   *italic*            → <em>
 *   `inline code`       → <code>
 *   - bullet / * bullet → <ul><li>
 *   1. numbered item    → <ol><li>
 *   blank line          → paragraph break
 *
 * Security:
 *   - All raw text segments are HTML-escaped before insertion.
 *   - No arbitrary HTML is ever passed through.
 *   - No <script>, <img>, <a> or other tags are generated from user/LLM content.
 *   - Inline patterns (bold/italic/code) are applied after escaping so they
 *     can only produce the specific safe tags listed above.
 *
 * @param {string} text  Raw text from the LLM
 * @returns {string}     Safe HTML string
 */
function _renderMarkdown(text) {
  if (!text) return '';

  // Split into lines for block-level processing
  const lines = text.split('\n');
  const out   = [];
  let inUl = false;
  let inOl = false;

  function closeList() {
    if (inUl) { out.push('</ul>'); inUl = false; }
    if (inOl) { out.push('</ol>'); inOl = false; }
  }

  for (let i = 0; i < lines.length; i++) {
    const raw = lines[i];

    // ── Heading 2: ## text ───────────────────────────────
    if (/^##\s+/.test(raw)) {
      closeList();
      const content = _mdInline(_esc(raw.replace(/^##\s+/, '')));
      out.push(`<h2 class="md-h2">${content}</h2>`);
      continue;
    }

    // ── Heading 3: ### text ──────────────────────────────
    if (/^###\s+/.test(raw)) {
      closeList();
      const content = _mdInline(_esc(raw.replace(/^###\s+/, '')));
      out.push(`<h3 class="md-h3">${content}</h3>`);
      continue;
    }

    // ── Unordered list: - item or * item ─────────────────
    if (/^[-*]\s+/.test(raw)) {
      closeList();  // close OL if switching from numbered
      if (!inUl) { out.push('<ul class="md-ul">'); inUl = true; }
      const content = _mdInline(_esc(raw.replace(/^[-*]\s+/, '')));
      out.push(`<li>${content}</li>`);
      continue;
    }

    // ── Ordered list: 1. item ────────────────────────────
    if (/^\d+\.\s+/.test(raw)) {
      closeList();  // close UL if switching
      if (!inOl) { out.push('<ol class="md-ol">'); inOl = true; }
      const content = _mdInline(_esc(raw.replace(/^\d+\.\s+/, '')));
      out.push(`<li>${content}</li>`);
      continue;
    }

    // ── Blank line — close any open list ─────────────────
    if (raw.trim() === '') {
      closeList();
      // Only emit a paragraph break if there's content above and below
      const prevHasContent = out.length > 0;
      const nextHasContent = lines.slice(i + 1).some(l => l.trim() !== '');
      if (prevHasContent && nextHasContent) {
        out.push('<div class="md-gap"></div>');
      }
      continue;
    }

    // ── Plain paragraph line ─────────────────────────────
    closeList();
    out.push(`<p class="md-p">${_mdInline(_esc(raw))}</p>`);
  }

  closeList();
  return out.join('\n');
}

/**
 * Escape HTML special characters in a raw string.
 * Applied to every text segment BEFORE inline markdown processing
 * so that LLM output can never inject HTML.
 */
function _esc(str) {
  return str
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

/**
 * Apply inline markdown patterns to an already-escaped string.
 * Only generates the specific safe tags: <code>, <strong>, <em>.
 * Patterns are applied in order: code first (to prevent bold/italic
 * inside backticks), then bold, then italic.
 */
function _mdInline(escaped) {
  // `code` — must be first so **bold inside code** is not parsed
  escaped = escaped.replace(/`([^`]+)`/g, '<code class="md-code">$1</code>');
  // **bold**
  escaped = escaped.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
  // *italic* (single asterisk, not already consumed by bold)
  escaped = escaped.replace(/\*([^*]+)\*/g, '<em>$1</em>');
  return escaped;
}

/**
 * Add a rich tool-result card.
 * @param {string} iconEmoji
 * @param {string} headerLabel
 * @param {string|Node} bodyContent
 */
function addToolCard(iconEmoji, headerLabel, bodyContent) {
  hideEmptyState();

  const wrapper = document.createElement('div');
  wrapper.className = 'tool-message';

  // Nova avatar
  const avatar = document.createElement('div');
  avatar.className = 'bot-avatar';
  avatar.setAttribute('aria-hidden', 'true');
  avatar.textContent = 'N';
  wrapper.appendChild(avatar);

  const card = document.createElement('div');
  card.className = 'tool-card';

  const header = document.createElement('div');
  header.className = 'tool-header';
  header.textContent = `${iconEmoji} ${headerLabel}`;
  card.appendChild(header);

  const body = document.createElement('div');
  body.className = 'tool-body';
  if (typeof bodyContent === 'string') {
    body.textContent = bodyContent;
  } else {
    body.appendChild(bodyContent);
  }
  card.appendChild(body);

  wrapper.appendChild(card);
  chatBox.appendChild(wrapper);
  scrollToBottom();
  return card; // return the card element (used to update loading cards)
}

function scrollToBottom() {
  chatBox.scrollTop = chatBox.scrollHeight;
}

// ============================================================
//  AGENT STATUS INDICATOR
//
//  Single source of truth for all in-flight and post-flight
//  status feedback.  Replaces the old showThinking() bubble
//  and showToolState() spinner with one controlled element.
//
//  Lifecycle:
//    showThinking()          → "Thinking…" + dots  (on request start)
//    updateThinkingLabel(s)  → swap label text in-place
//    completeThinking()      → "Completed ✓", then auto-removes in 900 ms
//    hideThinking()          → remove immediately (errors / cancels)
//
//    showAgentStepsPill(steps) → compact read-only summary pill
//                                shown between bubble removal and reply
//    _removeAgentStepsPill() → fade-out then remove
//
//  The old showToolState / hideToolState names are kept as no-ops
//  so existing executors don't throw.  The pill now takes that role.
// ============================================================

// ── Elapsed-time label progression ───────────────────────────
// Shown while waiting so the user knows the agent is still active.
// Labels are plain English (not Hinglish) so they read cleanly at a glance.
const _THINKING_LABELS = [
  'Thinking',        //  0 s
  'Thinking',        //  3 s  (no-op: hold initial label)
  'Working on it',   //  6 s
  'One moment',      // 10 s
  'Almost there',    // 15 s
];
const _THINKING_INTERVALS = [0, 3000, 6000, 10000, 15000]; // ms from start

let _thinkingTimers  = [];  // setTimeout handles
let _thinkingStartMs = 0;   // Date.now() when showThinking() was called

function showThinking() {
  _clearThinkingTimers();
  _thinkingStartMs = Date.now();

  // Build the bubble element (matches existing .thinking-message structure)
  thinkingEl = document.createElement('div');
  thinkingEl.className = 'thinking-message';

  const avatar = document.createElement('div');
  avatar.className = 'bot-avatar';
  avatar.setAttribute('aria-hidden', 'true');
  avatar.textContent = 'N';
  thinkingEl.appendChild(avatar);

  const bubble = document.createElement('div');
  bubble.className = 'bubble';
  bubble.innerHTML =
    '<span class="thinking-label">Thinking</span>' +
    '<div class="thinking-dots" aria-hidden="true">' +
    '<span></span><span></span><span></span>' +
    '</div>';
  thinkingEl.appendChild(bubble);

  chatBox.appendChild(thinkingEl);
  scrollToBottom();

  // Schedule label progressions
  _THINKING_INTERVALS.forEach((ms, i) => {
    if (ms === 0) return;  // first label already set above
    const t = setTimeout(() => updateThinkingLabel(_THINKING_LABELS[i]), ms);
    _thinkingTimers.push(t);
  });
}

/** Swap the label text with a brief opacity cross-fade. */
function updateThinkingLabel(text) {
  if (!thinkingEl) return;
  const label = thinkingEl.querySelector('.thinking-label');
  if (!label || label.textContent === text) return;
  label.style.opacity = '0';
  setTimeout(() => {
    if (label) {
      label.textContent = text;
      label.style.opacity = '1';
    }
  }, 120);
}

/**
 * Transition bubble to "Completed ✓" then self-remove after 900 ms.
 * Call this just before rendering the actual reply.
 * Returns a Promise that resolves when the element has been removed.
 */
function completeThinking() {
  return new Promise(resolve => {
    _clearThinkingTimers();
    if (!thinkingEl) { resolve(); return; }

    const el = thinkingEl;
    thinkingEl = null;

    const label = el.querySelector('.thinking-label');
    if (label) {
      label.style.opacity = '0';
      setTimeout(() => {
        if (label) { label.textContent = 'Completed ✓'; label.style.opacity = '1'; }
      }, 100);
    }
    el.classList.add('is-done');

    setTimeout(() => {
      if (el.parentNode) el.remove();
      resolve();
    }, 900);
  });
}

/** Remove the thinking bubble immediately (errors, cancels). */
function hideThinking() {
  _clearThinkingTimers();
  if (thinkingEl) { thinkingEl.remove(); thinkingEl = null; }
}

function _clearThinkingTimers() {
  _thinkingTimers.forEach(clearTimeout);
  _thinkingTimers = [];
}

// ── Agent-steps summary pill ──────────────────────────────────

/**
 * Map an agent_steps array (from the backend response) to a
 * human-readable one-line summary shown in the pill.
 *
 * Examples:
 *   [{action:"final_reply"}]                         → null  (plain chat, show nothing)
 *   [{action:"tool_call",tool:"GET_WEATHER"}, …]     → "☁ Checked weather"
 *   [{action:"tool_call",tool:"PLAY_YOUTUBE"}, …]    → "▶ Searched YouTube"
 *   [{action:"tool_call",tool:"SAVE_MEMORY"}, …]     → "🧠 Saved to memory"
 *   [{action:"tool_call",tool:"RECALL_MEMORY"}, …]   → "🧠 Recalled memory"
 *   [{action:"tool_call",tool:"DELETE_MEMORY"}, …]   → "🗑 Deleted memory"
 *   [{action:"tool_call",tool:"OPEN_WEBSITE"}, …]    → "🌐 Opened website"
 *   [{action:"tool_call",tool:"CALL_CONTACT"}, …]    → "📞 Initiated call"
 *   [{action:"tool_call",tool:"SEND_SMS"}, …]        → "💬 Sent message"
 *   [{action:"timeout"}]                             → "⚠ Timed out"
 *   [{action:"error"}]                               → "⚠ Error"
 *   multiple tool steps                              → "⚙ Used 2 tools"
 */
const _TOOL_PILL_META = {
  GET_WEATHER:      { icon: '☁',  label: 'Checked weather' },
  PLAY_YOUTUBE:     { icon: '▶',  label: 'Searched YouTube' },
  SEARCH_YOUTUBE:   { icon: '▶',  label: 'Searched YouTube' },
  OPEN_WEBSITE:     { icon: '🌐', label: 'Opened website' },
  SAVE_MEMORY:      { icon: '🧠', label: 'Saved to memory' },
  RECALL_MEMORY:    { icon: '🧠', label: 'Recalled memory' },
  DELETE_MEMORY:    { icon: '🗑', label: 'Deleted memory' },
  WHATSAPP_MESSAGE: { icon: '💬', label: 'Opened WhatsApp' },
  PHONE_CALL:       { icon: '📞', label: 'Triggered call' },
  SEND_EMAIL:       { icon: '📧', label: 'Opened email draft' },
  CALL_CONTACT:     { icon: '📞', label: 'Initiated call' },
  SEND_SMS:         { icon: '💬', label: 'Sent message' },
  SEND_MESSAGE:     { icon: '💬', label: 'Sent message' },
  SET_ALARM:        { icon: '⏰', label: 'Set alarm' },
  GET_CONTACTS:     { icon: '👤', label: 'Searched contacts' },
};

function _summariseAgentSteps(steps) {
  if (!Array.isArray(steps) || steps.length === 0) return null;

  // Special states
  if (steps.some(s => s.action === 'timeout')) return { icon: '⚠', label: 'Timed out' };
  if (steps.some(s => s.action === 'error'))   return { icon: '⚠', label: 'Error' };

  // Collect tool calls
  const toolSteps = steps.filter(s => s.action === 'tool_call' && s.tool);
  if (toolSteps.length === 0) return null;   // pure chat — no pill needed

  if (toolSteps.length === 1) {
    return _TOOL_PILL_META[toolSteps[0].tool] || { icon: '⚙', label: 'Used tool' };
  }

  // Multiple different tools
  const unique = [...new Set(toolSteps.map(s => s.tool))];
  if (unique.length === 1) {
    // same tool called twice (e.g. two weather lookups)
    return _TOOL_PILL_META[unique[0]] || { icon: '⚙', label: 'Used tool' };
  }
  return { icon: '⚙', label: `Used ${unique.length} tools` };
}

let _agentPillEl = null;

/**
 * Render a compact read-only pill summarising what the agent did.
 * Auto-removes after `durationMs` milliseconds (default 2 s).
 * Only shown when `steps` contains at least one tool_call.
 */
function showAgentStepsPill(steps, durationMs = 2000) {
  _removeAgentStepsPill();                // clear any leftover

  const summary = _summariseAgentSteps(steps);
  if (!summary) return;                   // plain chat — no pill

  const pill = document.createElement('div');
  pill.className = 'agent-status-pill';
  pill.setAttribute('aria-live', 'polite');

  pill.innerHTML =
    `<div class="agent-status-pill-inner">` +
    `<span class="agent-status-pill-icon" aria-hidden="true">${summary.icon}</span>` +
    `<span>${escHtml(summary.label)}</span>` +
    `</div>`;

  _agentPillEl = pill;
  chatBox.appendChild(pill);
  scrollToBottom();

  // Auto-remove with a fade
  setTimeout(() => _removeAgentStepsPill(), durationMs);
}

function _removeAgentStepsPill() {
  if (!_agentPillEl) return;
  const el = _agentPillEl;
  _agentPillEl = null;
  el.classList.add('is-fading');
  setTimeout(() => { if (el.parentNode) el.remove(); }, 280);
}

// ── Backward-compat stubs (called by executors; no-op now) ────
// showToolState / hideToolState are kept so the executor functions
// don't throw.  The pill shown by showAgentStepsPill() takes their role.
function showToolState(_label) { /* no-op — pill handles this now */ }
function hideToolState()       { /* no-op */ }

// ── Input lock / unlock ───────────────────────────────────────
function lockInput() {
  isWaitingForBot     = true;
  sendBtn.disabled    = true;
  userInput.disabled  = true;
}

function unlockInput() {
  isWaitingForBot     = false;
  sendBtn.disabled    = false;
  userInput.disabled  = false;
  userInput.focus();
}

// ============================================================
//  SEND MESSAGE  →  parse response  →  dispatch
// ============================================================
async function sendMessage() {
  const message = userInput.value.trim();

  // If a file is loaded, route to the right upload path regardless of whether text was typed
  if (selectedFile) {
    userInput.value = '';
    userInput.style.height = 'auto';
    if (selectedFile.kind === 'image') {
      await sendVision(message);
    } else {
      await sendDocument(message);
    }
    return;
  }

  if (!message || isWaitingForBot) return;

  addMessage(message, 'user');
  userInput.value = '';
  // Reset textarea height
  userInput.style.height = 'auto';
  lockInput();
  showThinking();

  // Close sidebar on mobile when sending
  closeSidebar();

  const endpoint = `${window.API_BASE_URL}/chat`;

  try {
    // Build headers — attach token if logged in
    const headers = { 'Content-Type': 'application/json' };
    const authHeader = (typeof getAuthHeader === 'function') ? getAuthHeader() : null;
    if (authHeader) headers['Authorization'] = authHeader;

    // Extract first name via shared helper — keeps extraction logic in one place.
    // Only the display name is sent — no email, no ID, no token.
    const _firstName = _getUserFirstName();

    // Read AI prefs — sent as optional fields; backend ignores missing/unknown values.
    const _prefs = (typeof getPrefs === 'function') ? getPrefs() : {};

    const res = await fetch(endpoint, {
      method: 'POST',
      headers,
      body: JSON.stringify({
        message,
        user_name:       _firstName || undefined,
        conversation_id: currentConvId || undefined,
        language:        _prefs.language       || undefined,
        response_style:  _prefs.response_style || undefined,
      }),
    });

    // Response received — play the "Completed ✓" flash then remove the bubble.
    // On errors we skip the animation and just hide immediately.
    if (!res.ok) {
      hideThinking();
      // 401: session expired or invalid → redirect to login
      if (res.status === 401) {
        if (typeof clearSession === 'function') clearSession();
        window.location.href = 'login.html';
        return;
      }
      console.error(`Backend HTTP ${res.status}`);
      addMessage(`Server error (${res.status}). Thodi der baad try karo.`, 'error');
      setStatus('offline', `Server error (${res.status})`);
      unlockInput();
      return;
    }

    const data = await res.json();

    // "Completed ✓" flash — awaited so it plays before the reply renders.
    await completeThinking();

    // Show a compact pill summarising what the agent did (tool calls only).
    // Plain chat replies produce no pill. Pill auto-removes after 2 s.
    showAgentStepsPill(data.agent_steps);

    // Capture / update the conversation_id returned by the backend.
    // We always update (not just on the first message) so that if the
    // backend ever re-assigns the id we stay in sync.
    const isNewConv = !currentConvId;
    if (data.conversation_id) {
      currentConvId = data.conversation_id;
      // Refresh the sidebar: always on the very first message of a new conv
      // (so it appears immediately), and on subsequent messages so the
      // title and updated_at timestamp stay current.
      loadConversations();
    }

    setStatus('online', 'Nova Online');
    await handleServerResponse(data);

  } catch (err) {
    hideThinking();
    console.error('Fetch error:', err);
    if (err.name === 'AbortError' || err.name === 'TimeoutError') {
      addMessage('Request timeout ho gayi. Server slow hai ya band hai.', 'error');
    } else {
      addMessage('Backend se connect nahi ho pa raha. Server check karo.', 'error');
    }
    setStatus('offline', 'Server offline');
  }

  unlockInput();
}

// ============================================================
//  RESPONSE DISPATCHER
// ============================================================
async function handleServerResponse(data) {
  if (!data || typeof data !== 'object') {
    addMessage('Unexpected response from server.', 'error');
    return;
  }

  const type = data.type;

  // ── Plain chat message ───────────────────────────────────
  if (type === 'message' || !type) {
    const reply = data.response || 'Sorry, koi reply nahi mili.';
    addMessage(reply, 'bot');
    speak(reply);
    return;
  }

  // ── Tool call ────────────────────────────────────────────
  if (type === 'tool_call') {
    const { intent, parameters, requires_confirmation, response: finalText, tool_result: toolResult } = data;

    if (!intent || !parameters) {
      addMessage('Tool call ka response incomplete tha.', 'error');
      return;
    }

    // Confirmation gate: show modal only for high-risk tools.
    // Low-risk tools (YouTube, weather, alarm, etc.) run directly.
    // We check both HIGH_RISK_TOOLS (frontend safety net) AND the backend
    // requires_confirmation flag — either can trigger the modal.
    const needsConfirm = HIGH_RISK_TOOLS.has(intent) || requires_confirmation;

    if (needsConfirm) {
      const confirmed = await showConfirmation(intent, parameters);
      if (!confirmed) {
        addMessage('Theek hai, action cancel kar diya. 👍', 'bot');
        return;
      }
    }

    // `response` is the agent loop's final natural-language reply;
    // `tool_result` (for GET_WEATHER) is data already fetched on the backend.
    await executeTool(intent, parameters, { response: finalText, tool_result: toolResult });
    return;
  }

  // ── Android tool call ────────────────────────────────────
  if (type === 'android_tool_call') {
    addMessage(
      data.response ||
      'Android tool dispatched. Nova Companion app check karo.',
      'bot'
    );
    return;
  }

  // ── Unknown type ─────────────────────────────────────────
  addMessage(data.response || 'Unexpected response type from server.', 'bot');
}

// ============================================================
//  CONFIRMATION MODAL
// ============================================================
// ── Risk classification (mirrors backend ToolRiskLevel) ──────
// HIGH = requires_confirmation always shown (destructive / real-world impact)
// LOW  = runs directly, no modal
//
// This is the single source of truth on the frontend. It mirrors
// requires_confirmation from the backend response — we double-check
// it here so a misconfigured backend can never silently skip a modal
// for a known-dangerous tool.
const HIGH_RISK_TOOLS = new Set([
  'CALL_CONTACT',
  'SEND_SMS',
  'SEND_MESSAGE',  // alias for SEND_SMS
  'DELETE_MEMORY',
  'OPEN_WEBSITE',  // medium — opens external URLs
  'WHATSAPP_MESSAGE', // medium — opens wa.me link
  'PHONE_CALL',       // medium — opens tel: link
  'SEND_EMAIL',       // medium — opens mailto: link
  '_DELETE_CONVERSATION',  // internal — sidebar delete button
]);

const TOOL_CONFIRM_META = {
  // ── Low-risk (no modal) ──────────────────────────────────
  SEARCH_YOUTUBE: null,   // runs directly
  PLAY_YOUTUBE:   null,   // runs directly
  GET_WEATHER:    null,   // runs directly
  SET_ALARM:      null,   // runs directly
  GET_CONTACTS:   null,   // runs directly
  SAVE_MEMORY:    null,   // runs directly — low risk, no confirmation
  RECALL_MEMORY:  null,   // runs directly — read-only

  // ── Medium / High-risk (modal required) ──────────────────
  OPEN_WEBSITE: {
    icon:      '🌐',
    title:     'Website kholni hai?',
    body:      'Nova yeh website aapke liye open karna chahti hai:',
    getDetail: (p) => p.url,
  },
  WHATSAPP_MESSAGE: {
    icon:      '💬',
    title:     'WhatsApp kholna hai?',
    body:      'Nova WhatsApp mein yeh message pre-fill karega. Aapko khud Send karna hoga:',
    getDetail: (p) => {
      const to  = p.phone_number ? `To: +${p.phone_number}` : '';
      const msg = p.message ? `\n"${String(p.message).slice(0, 120)}${p.message.length > 120 ? '…' : ''}"` : '';
      return (to + msg).trim() || p.whatsapp_url || '(no details)';
    },
  },
  PHONE_CALL: {
    icon:      '📞',
    title:     'Call karna hai?',
    body:      'Nova yeh number dial karne ke liye browser ko prompt karega:',
    getDetail: (p) => p.phone_number || '(unknown number)',
  },
  SEND_EMAIL: {
    icon:      '📧',
    title:     'Email kholni hai?',
    body:      'Nova aapki email app mein yeh email pre-fill karega. Aapko khud Send karna hoga:',
    getDetail: (p) => {
      const lines = [`To: ${p.to}`];
      if (p.subject) lines.push(`Subject: ${p.subject}`);
      if (p.body)    lines.push(`"${String(p.body).slice(0, 100)}${p.body.length > 100 ? '…' : ''}"`);
      return lines.join('\n');
    },
  },
  CALL_CONTACT: {
    icon:      '📞',
    title:     'Call karna hai?',
    body:      'Nova yeh contact ke liye dialer open karega:',
    getDetail: (p) => p.contact_name || p.phone_number || '(unknown)',
  },
  SEND_SMS: {
    icon:      '💬',
    title:     'Message bhejna hai?',
    body:      'Nova SMS app mein yeh message pre-fill karega:',
    getDetail: (p) => `To: ${p.phone_number}\n"${p.message_body}"`,
  },
  SEND_MESSAGE: {
    icon:      '💬',
    title:     'Message bhejna hai?',
    body:      'Nova SMS app mein yeh message pre-fill karega:',
    getDetail: (p) => `To: ${p.phone_number}\n"${p.message_body}"`,
  },
  DELETE_MEMORY: {
    icon:      '🗑️',
    title:     'Memory delete karna hai?',
    body:      'Yeh memory permanently delete ho jayegi.',
    getDetail: (p) => {
      if (!p.key || p.key === '__all__') return 'Saari memories delete ho jayengi. Yeh undo nahi ho sakta.';
      return `"${p.key}" memory delete ho jayegi.`;
    },
  },

  // Internal pseudo-intent used only by the sidebar delete button.
  // Never sent to the backend — only consumed by deleteConversationUI().
  _DELETE_CONVERSATION: {
    icon:      '🗑️',
    title:     'Chat delete karna hai?',
    body:      'Is conversation ki saari messages permanently delete ho jayengi.',
    getDetail: () => 'Yeh action undo nahi ho sakta.',
  },
};

/**
 * Show the confirmation modal for a high-risk tool.
 * Returns a Promise<boolean> — true if confirmed, false if cancelled.
 *
 * Should only be called for tools in HIGH_RISK_TOOLS. Low-risk tools
 * must NOT reach this function.
 */
function showConfirmation(intent, parameters) {
  return new Promise((resolve) => {
    const meta = TOOL_CONFIRM_META[intent] || {
      icon:      '⚠️',
      title:     'Action confirm karein',
      body:      'Kya aap yeh action karna chahte hain?',
      getDetail: () => intent,
    };

    confirmIcon.textContent   = meta.icon;
    confirmTitle.textContent  = meta.title;
    confirmBody.textContent   = meta.body;
    // getDetail may return multi-line strings (e.g. SMS body) — show as-is
    confirmDetail.textContent = meta.getDetail(parameters);

    confirmModal.showModal();

    function onYes()    { cleanup(); resolve(true); }
    function onNo()     { cleanup(); resolve(false); }
    function onCancel(e){ e.preventDefault(); cleanup(); resolve(false); }

    function cleanup() {
      confirmYesBtn.removeEventListener('click', onYes);
      confirmNoBtn.removeEventListener('click',  onNo);
      confirmModal.removeEventListener('cancel', onCancel);
      confirmModal.close();
    }

    confirmYesBtn.addEventListener('click', onYes,    { once: true });
    confirmNoBtn.addEventListener('click',  onNo,     { once: true });
    confirmModal.addEventListener('cancel', onCancel, { once: true });
  });
}

// ============================================================
//  TOOL EXECUTORS — all original logic preserved
// ============================================================
async function executeTool(intent, parameters, extra = {}) {
  switch (intent) {
    case 'SEARCH_YOUTUBE': return executePlayYoutube(parameters, extra);
    case 'PLAY_YOUTUBE':  return executePlayYoutube(parameters, extra);
    case 'OPEN_WEBSITE':  return executeOpenWebsite(parameters, extra);
    case 'GET_WEATHER':   return executeGetWeather(parameters, extra);
    case 'SAVE_MEMORY':   return executeSaveMemory(parameters, extra);
    case 'RECALL_MEMORY': return executeRecallMemory(parameters, extra);
    case 'DELETE_MEMORY': return executeDeleteMemory(parameters, extra);
    case 'WHATSAPP_MESSAGE': return executeWhatsappMessage(parameters, extra);
    case 'PHONE_CALL':       return executePhoneCall(parameters, extra);
    case 'SEND_EMAIL':       return executeSendEmail(parameters, extra);
    case 'SEND_SMS':
    case 'SEND_MESSAGE':  return executeSendMessage(parameters, extra);
    default:
      addMessage(`Unknown tool: "${intent}". Yeh feature abhi available nahi.`, 'error');
  }
}

// ── PLAY_YOUTUBE ─────────────────────────────────────────────
function executePlayYoutube(params, extra = {}) {
  showToolState('Searching YouTube…');

  const url = params.youtube_url ||
    `https://www.youtube.com/results?search_query=${encodeURIComponent(params.query)}`;

  const opened = window.open(url, '_blank', 'noopener,noreferrer');

  hideToolState();

  // Final natural-language reply from the agent loop (if present)
  if (extra.response) addMessage(extra.response, 'bot');

  const frag = document.createDocumentFragment();
  const line = document.createElement('div');
  line.className = 'tool-link-line';

  if (opened) {
    line.innerHTML = `YouTube search page khul gayi. Agar nahi khula: <a href="${escHtml(url)}" target="_blank" rel="noopener noreferrer">yahan click karo</a>`;
  } else {
    line.innerHTML = `Browser ne popup block kiya. <a href="${escHtml(url)}" target="_blank" rel="noopener noreferrer">Yahan click karo YouTube kholne ke liye</a>`;
  }
  frag.appendChild(line);
  addToolCard('▶️', 'YouTube Search', frag);
  speak(extra.response || `YouTube par "${params.query}" search kiya. Tab mein dekho.`);
}

// ── OPEN_WEBSITE ─────────────────────────────────────────────
function executeOpenWebsite(params, extra = {}) {
  showToolState('Opening website…');

  const url    = params.url;
  const opened = window.open(url, '_blank', 'noopener,noreferrer');

  hideToolState();

  // Final natural-language reply from the agent loop (if present)
  if (extra.response) addMessage(extra.response, 'bot');

  const frag = document.createDocumentFragment();
  const line = document.createElement('div');
  line.className = 'tool-link-line';

  if (opened) {
    line.innerHTML = `Yeh website ek naye tab mein khul gayi: <a href="${escHtml(url)}" target="_blank" rel="noopener noreferrer">${escHtml(url)}</a>`;
  } else {
    line.innerHTML = `Popup block ho gaya. <a href="${escHtml(url)}" target="_blank" rel="noopener noreferrer">Manually yahan click karo</a>`;
  }
  frag.appendChild(line);
  addToolCard('🌐', opened ? 'Website Opened' : 'Website', frag);
  speak(extra.response || (opened ? 'Website nayi tab mein khul gayi.' : 'Browser ne popup block kiya. Link par click karo.'));
}

// ── GET_WEATHER ──────────────────────────────────────────────
async function executeGetWeather(params, extra = {}) {
  const city = params.city;

  // If the agent loop already fetched the weather, render it directly
  // (no second request to /weather).
  if (extra.tool_result && extra.tool_result.ok) {
    if (extra.response) addMessage(extra.response, 'bot');
    _renderWeatherCard(extra.tool_result);
    speak(extra.response || `${extra.tool_result.city} mein abhi ${extra.tool_result.temp_c} degree Celsius hai. ${extra.tool_result.description}.`);
    return;
  }

  // Fallback: fetch from /weather (preserved for non-agent callers)
  const loadCard = addToolCard('🌤', 'Weather', `${city} ka mausam fetch ho raha hai…`);

  try {
    const weatherHeaders = {};
    const authHeader = (typeof getAuthHeader === 'function') ? getAuthHeader() : null;
    if (authHeader) weatherHeaders['Authorization'] = authHeader;

    const res = await fetch(
      `${window.API_BASE_URL}/weather?city=${encodeURIComponent(city)}`,
      { headers: weatherHeaders, signal: AbortSignal.timeout(10_000) }
    );

    // Remove the loading card — addToolCard returns the inner .tool-card element
    const loadWrapper = loadCard.closest('.tool-message');
    if (loadWrapper) loadWrapper.remove();

    if (!res.ok) {
      addMessage(`Weather fetch nahi hua (HTTP ${res.status}). Dobara try karo.`, 'error');
      return;
    }

    const w = await res.json();

    if (!w.ok) {
      addToolCard('⚠️', 'Weather Error', w.error || 'Weather data nahi mila.');
      speak(w.error || 'Weather data nahi mila.');
      return;
    }

    _renderWeatherCard(w);
    speak(`${w.city} mein abhi ${w.temp_c} degree Celsius hai. ${w.description}.`);

  } catch (err) {
    const loadWrapper = loadCard.closest?.('.tool-message');
    if (loadWrapper) loadWrapper.remove();

    if (err.name === 'AbortError' || err.name === 'TimeoutError') {
      addMessage('Weather API response timeout ho gayi. Dobara try karo.', 'error');
    } else {
      addMessage('Weather fetch karte waqt error aaya. Server check karo.', 'error');
    }
  }
}

/**
 * Render weather data (from /weather or the agent loop's tool_result) as a
 * rich grid card.
 */
function _renderWeatherCard(w) {
  const grid = document.createElement('div');
  grid.className = 'weather-grid';

  const rows = [
    ['📍 City',        w.city],
    ['🌡 Temperature', `${w.temp_c}°C / ${w.temp_f}°F`],
    ['🤔 Feels Like',  `${w.feels_like_c}°C`],
    ['💧 Humidity',    `${w.humidity}%`],
    ['💨 Wind',        `${w.wind_kmph} km/h`],
    ['👁 Visibility',  `${w.visibility_km} km`],
    ['☁️ Condition',   w.description],
  ];

  rows.forEach(([label, value]) => {
    const lEl = document.createElement('span');
    lEl.className   = 'wl';
    lEl.textContent = label;
    const vEl = document.createElement('span');
    vEl.className   = 'wv';
    vEl.textContent = value;
    grid.appendChild(lEl);
    grid.appendChild(vEl);
  });

  addToolCard('🌤', `Weather — ${w.city}`, grid);
}

// ── SAVE_MEMORY ───────────────────────────────────────────────
// The agent loop executes SAVE_MEMORY on the backend — the result
// arrives here in extra.tool_result (ok + message). We just render it.
function executeSaveMemory(_params, extra = {}) {
  const result = extra.tool_result || {};
  const nlReply = extra.response || result.message || '';

  if (nlReply) addMessage(nlReply, 'bot');

  if (result.ok && result.data) {
    const { key, value } = result.data;
    addToolCard(
      '🧠',
      'Memory Saved',
      `"${key}" = "${String(value).slice(0, 80)}"`,
    );
  } else if (result.ok === false) {
    addToolCard('⚠️', 'Memory Error', result.message || 'Memory save nahi ho saka.');
  }
}

// ── RECALL_MEMORY ─────────────────────────────────────────────
function executeRecallMemory(_params, extra = {}) {
  const result = extra.tool_result || {};
  const nlReply = extra.response || '';

  if (nlReply) addMessage(nlReply, 'bot');

  if (!result.ok) {
    if (result.error) addMessage(result.error, 'error');
    return;
  }

  const memories = result.memories || [];
  if (memories.length === 0) {
    addToolCard('🧠', 'Memory', 'Koi yaad nahi hai abhi.');
    return;
  }

  const list = document.createElement('div');
  list.style.cssText = 'display:flex;flex-direction:column;gap:6px;';
  memories.forEach(({ key, value }) => {
    const row = document.createElement('div');
    row.style.cssText = 'display:flex;gap:8px;align-items:baseline;';
    const k = document.createElement('span');
    k.style.cssText = 'color:var(--accent-2);font-size:0.8rem;flex-shrink:0;min-width:80px;word-break:break-all;';
    k.textContent = key;
    const v = document.createElement('span');
    v.style.cssText = 'color:var(--text-primary);font-size:0.88rem;';
    v.textContent = value;
    row.appendChild(k);
    row.appendChild(v);
    list.appendChild(row);
  });
  addToolCard('🧠', `Memories (${memories.length})`, list);
}

// ── DELETE_MEMORY ─────────────────────────────────────────────
// Clears one memory key or all memories on the backend; optionally
// also clears the conversation (when key === '__all__').
// Must only be called AFTER the confirmation modal has been accepted.
async function executeDeleteMemory(params, extra = {}) {
  const key = params.key || '__all__';

  // Show the agent's natural-language reply first if present
  if (extra.response) addMessage(extra.response, 'bot');

  // If the backend already executed the deletion (tool_result present), render result
  if (extra.tool_result) {
    const r = extra.tool_result;
    if (r.ok) {
      addToolCard('🗑️', 'Memory Deleted', r.message || 'Memory delete ho gayi.');
    } else {
      addToolCard('⚠️', 'Memory Error', r.message || 'Memory delete nahi ho saka.');
    }
    return;
  }

  // Fallback: backend didn't return a tool_result (shouldn't happen, but be safe)
  addToolCard('🗑️', 'Memory', 'Memory delete ki request bheji gayi.');

  // Additionally clear the local conversation view when wiping all
  if (key === '__all__' && currentConvId) {
    const headers = {};
    const authHeader = (typeof getAuthHeader === 'function') ? getAuthHeader() : null;
    if (authHeader) headers['Authorization'] = authHeader;
    try {
      await fetch(
        `${window.API_BASE_URL}/conversations/${encodeURIComponent(currentConvId)}`,
        { method: 'DELETE', headers, signal: AbortSignal.timeout(8000) }
      );
    } catch (err) {
      console.warn('[nova] DELETE_MEMORY conv cleanup error:', err?.message || err);
    }
    newChat();
    loadConversations();
  }
}

// ── WHATSAPP_MESSAGE ──────────────────────────────────────────
/**
 * Renders a rich card with target number, message preview, and an
 * "Open WhatsApp" button. The link is NOT auto-opened — the user
 * clicks the button. Nova CANNOT send the message automatically.
 */
function executeWhatsappMessage(params, extra = {}) {
  const phone = (params.phone_number || '').replace(/\D/g, '');
  const msg   = (params.message      || '').trim();
  const url   = params.whatsapp_url ||
    `https://wa.me/${phone}${msg ? '?text=' + encodeURIComponent(msg) : ''}`;

  // Natural-language reply from the agent loop (e.g. "Yeh number par WhatsApp...")
  if (extra.response) addMessage(extra.response, 'bot');

  // ── Build card body ───────────────────────────────────────
  const body = document.createElement('div');
  body.className = 'comm-card-body';

  // Target row
  const targetRow = document.createElement('div');
  targetRow.className = 'comm-card-row';
  targetRow.innerHTML =
    `<span class="comm-card-label">To</span>` +
    `<span class="comm-card-value">+${escHtml(phone)}</span>`;
  body.appendChild(targetRow);

  // Message preview row (if present)
  if (msg) {
    const msgRow = document.createElement('div');
    msgRow.className = 'comm-card-row';
    const preview = msg.length > 120 ? msg.slice(0, 120) + '…' : msg;
    msgRow.innerHTML =
      `<span class="comm-card-label">Message</span>` +
      `<span class="comm-card-value comm-card-preview">${escHtml(preview)}</span>`;
    body.appendChild(msgRow);
  }

  // Note
  const note = document.createElement('p');
  note.className = 'comm-card-note';
  note.textContent = 'Browser WhatsApp / app kholega. Final Send aapko karna hoga.';
  body.appendChild(note);

  // Button
  const btn = document.createElement('a');
  btn.href      = url;
  btn.target    = '_blank';
  btn.rel       = 'noopener noreferrer';
  btn.className = 'comm-card-btn comm-card-btn--whatsapp';
  btn.setAttribute('role', 'button');
  btn.innerHTML =
    `<svg width="15" height="15" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">` +
    `<path d="M17.472 14.382c-.297-.149-1.758-.867-2.03-.967-.273-.099-.471-.148-.67.15` +
    `-.197.297-.767.966-.94 1.164-.173.199-.347.223-.644.075-.297-.15-1.255-.463-2.39-1.475` +
    `-.883-.788-1.48-1.761-1.653-2.059-.173-.297-.018-.458.13-.606.134-.133.298-.347.446-.52` +
    `.149-.174.198-.298.298-.497.099-.198.05-.371-.025-.52-.075-.149-.669-1.612-.916-2.207` +
    `-.242-.579-.487-.5-.669-.51-.173-.008-.371-.01-.57-.01-.198 0-.52.074-.792.372` +
    `-.272.297-1.04 1.016-1.04 2.479 0 1.462 1.065 2.875 1.213 3.074` +
    `.149.198 2.096 3.2 5.077 4.487.709.306 1.262.489 1.694.625` +
    `.712.227 1.36.195 1.871.118.571-.085 1.758-.719 2.006-1.413` +
    `.248-.694.248-1.289.173-1.413-.074-.124-.272-.198-.57-.347z"/>` +
    `<path d="M12 0C5.373 0 0 5.373 0 12c0 2.136.558 4.135 1.535 5.874L0 24l6.283-1.506` +
    `A11.94 11.94 0 0 0 12 24c6.627 0 12-5.373 12-12S18.627 0 12 0zm0 21.818` +
    `a9.818 9.818 0 0 1-5.006-1.371l-.358-.214-3.724.892.924-3.638-.234-.374` +
    `A9.818 9.818 0 1 1 12 21.818z"/>` +
    `</svg>` +
    `Open WhatsApp`;
  body.appendChild(btn);

  addToolCard('💬', 'WhatsApp Message', body);
  speak(extra.response || `WhatsApp card ready hai. Button click karke message bhejna hoga.`);
}

// ── PHONE_CALL ────────────────────────────────────────────────
/**
 * Renders a rich card with the target number and a "Call" button.
 * tel: is triggered by the button click, not automatically.
 * Nova CANNOT place the call — the browser/OS prompts the user.
 */
function executePhoneCall(params, extra = {}) {
  const phone = (params.phone_number || '').trim();
  const url   = params.tel_url || `tel:${phone}`;

  if (extra.response) addMessage(extra.response, 'bot');

  const body = document.createElement('div');
  body.className = 'comm-card-body';

  // Target row
  const targetRow = document.createElement('div');
  targetRow.className = 'comm-card-row';
  targetRow.innerHTML =
    `<span class="comm-card-label">Number</span>` +
    `<span class="comm-card-value">${escHtml(phone)}</span>`;
  body.appendChild(targetRow);

  // Note
  const note = document.createElement('p');
  note.className = 'comm-card-note';
  note.textContent = 'Browser call prompt kholega. Confirm karna aapko hoga.';
  body.appendChild(note);

  // Button — anchor with tel: href so browser handles the dial prompt
  const btn = document.createElement('a');
  btn.href      = url;
  btn.className = 'comm-card-btn comm-card-btn--call';
  btn.setAttribute('role', 'button');
  btn.innerHTML =
    `<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" ` +
    `stroke-width="2.2" aria-hidden="true">` +
    `<path d="M22 16.92v3a2 2 0 0 1-2.18 2 19.79 19.79 0 0 1-8.63-3.07` +
    `A19.5 19.5 0 0 1 4.69 12 19.79 19.79 0 0 1 1.63 3.41 2 2 0 0 1 3.6 1h3a2 2 0 0 1 2 1.72` +
    `c.127.96.361 1.903.7 2.81a2 2 0 0 1-.45 2.11L7.91 8.6A16 16 0 0 0 15.4 16.09l.96-.95` +
    `a2 2 0 0 1 2.11-.45c.907.339 1.85.573 2.81.7A2 2 0 0 1 22 16.92z"/>` +
    `</svg>` +
    `Call ${escHtml(phone)}`;
  body.appendChild(btn);

  addToolCard('📞', 'Phone Call', body);
  speak(extra.response || `Call card ready hai. Button click karke call karna hoga.`);
}

// ── SEND_EMAIL ────────────────────────────────────────────────
/**
 * Renders a rich card with recipient, subject preview, body preview,
 * and an "Open Email" button. mailto: is triggered by the button.
 * Nova CANNOT send the email — the user must click Send in their client.
 */
function executeSendEmail(params, extra = {}) {
  const to      = (params.to      || '').trim();
  const subject = (params.subject || '').trim();
  const body    = (params.body    || '').trim();

  const url = params.mailto_url || (() => {
    const parts = [];
    if (subject) parts.push(`subject=${encodeURIComponent(subject)}`);
    if (body)    parts.push(`body=${encodeURIComponent(body)}`);
    return `mailto:${encodeURIComponent(to)}${parts.length ? '?' + parts.join('&') : ''}`;
  })();

  if (extra.response) addMessage(extra.response, 'bot');

  const cardBody = document.createElement('div');
  cardBody.className = 'comm-card-body';

  // To row
  const toRow = document.createElement('div');
  toRow.className = 'comm-card-row';
  toRow.innerHTML =
    `<span class="comm-card-label">To</span>` +
    `<span class="comm-card-value">${escHtml(to)}</span>`;
  cardBody.appendChild(toRow);

  // Subject row (if present)
  if (subject) {
    const subRow = document.createElement('div');
    subRow.className = 'comm-card-row';
    const preview = subject.length > 80 ? subject.slice(0, 80) + '…' : subject;
    subRow.innerHTML =
      `<span class="comm-card-label">Subject</span>` +
      `<span class="comm-card-value">${escHtml(preview)}</span>`;
    cardBody.appendChild(subRow);
  }

  // Body preview row (if present)
  if (body) {
    const bodyRow = document.createElement('div');
    bodyRow.className = 'comm-card-row';
    const preview = body.length > 120 ? body.slice(0, 120) + '…' : body;
    bodyRow.innerHTML =
      `<span class="comm-card-label">Body</span>` +
      `<span class="comm-card-value comm-card-preview">${escHtml(preview)}</span>`;
    cardBody.appendChild(bodyRow);
  }

  // Note
  const note = document.createElement('p');
  note.className = 'comm-card-note';
  note.textContent = 'Email app khulega draft ke saath. Final Send aapko karna hoga.';
  cardBody.appendChild(note);

  // Button
  const btn = document.createElement('a');
  btn.href      = url;
  btn.className = 'comm-card-btn comm-card-btn--email';
  btn.setAttribute('role', 'button');
  btn.innerHTML =
    `<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" ` +
    `stroke-width="2.2" aria-hidden="true">` +
    `<path d="M4 4h16c1.1 0 2 .9 2 2v12c0 1.1-.9 2-2 2H4c-1.1 0-2-.9-2-2V6c0-1.1.9-2 2-2z"/>` +
    `<polyline points="22,6 12,13 2,6"/>` +
    `</svg>` +
    `Open Email`;
  cardBody.appendChild(btn);

  addToolCard('📧', 'Email Draft', cardBody);
  speak(extra.response || `Email draft card ready hai. Button click karke email app kholna hoga.`);
}

// ── SEND_MESSAGE / SEND_SMS ───────────────────────────────────
// Android-only: Nova cannot send SMS directly from the browser.
// This is reached only when the backend returns it as type=tool_call
// (i.e. device is offline and it was mis-routed).  If the companion
// app is online the backend returns type=android_tool_call which
// goes through the existing Android path, not here.
function executeSendMessage(params, extra = {}) {
  if (extra.response) addMessage(extra.response, 'bot');

  const body = params.message_body || '';
  const to   = params.phone_number || '';
  const detail = to
    ? `To: ${to}${body ? '\n"' + body.slice(0, 80) + (body.length > 80 ? '…' : '') + '"' : ''}`
    : 'Nova Companion app se SMS bhejna hoga.';

  addToolCard(
    '💬',
    'Message (Nova Companion Required)',
    `SMS bhejna ke liye phone par Nova Companion app open karo.\n\n${detail}`,
  );
}

// ── XSS-safe HTML escaping ────────────────────────────────────
function escHtml(str) {
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

// ============================================================
//  SPEECH SYNTHESIS
// ============================================================
function speak(text) {
  if (!('speechSynthesis' in window)) return;
  // Respect auto_speak preference — skip TTS if disabled
  if (typeof getPref === 'function' && !getPref('auto_speak')) return;
  speechSynthesis.cancel();

  const utterance   = new SpeechSynthesisUtterance(text);
  utterance.rate    = 0.92;
  utterance.pitch   = 1;
  utterance.volume  = 1;

  function trySpeak() {
    const voices = speechSynthesis.getVoices();
    if (voices.length === 0) {
      speechSynthesis.addEventListener('voiceschanged', trySpeak, { once: true });
      return;
    }
    const hiVoice   = voices.find(v => v.lang === 'hi-IN');
    const enInVoice = voices.find(v => v.lang === 'en-IN');
    const enVoice   = voices.find(v => v.lang.startsWith('en'));
    utterance.voice = hiVoice || enInVoice || enVoice || null;
    utterance.lang  = utterance.voice?.lang || 'en-IN';
    speechSynthesis.speak(utterance);
  }
  trySpeak();
}

// ============================================================
//  SPEECH RECOGNITION  (hi-IN / Hinglish, interimResults)
// ============================================================
const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
const hasSpeechAPI = Boolean(SpeechRecognition);

function toggleVoiceInput() {
  if (!hasSpeechAPI) {
    addMessage('Tumhara browser voice input support nahi karta. Chrome ya Edge use karo.', 'error');
    return;
  }
  // Respect voice_enabled preference
  if (typeof getPref === 'function' && !getPref('voice_enabled')) {
    addMessage('Voice input disabled hai. Settings mein enable karo.', 'error');
    return;
  }
  if (isListening) { stopListening(); return; }
  startListening();
}

function startListening() {
  recognition = new SpeechRecognition();
  recognition.lang            = 'hi-IN';
  recognition.interimResults  = true;
  recognition.maxAlternatives = 1;
  recognition.continuous      = false;

  recognition.onstart = () => {
    isListening = true;
    voiceBtn.classList.add('listening');
    voiceBtn.setAttribute('aria-pressed', 'true');
    voiceBtn.setAttribute('aria-label', 'Stop voice input');
    userInput.placeholder = 'Bol raha / rahi hun… 🎤';
  };

  recognition.onresult = (event) => {
    let transcript = '';
    for (let i = event.resultIndex; i < event.results.length; i++) {
      transcript += event.results[i][0].transcript;
    }
    userInput.value = transcript;
    autoResizeTextarea(userInput);

    if (event.results[event.results.length - 1].isFinal) {
      userInput.value = transcript.trim();
      stopListening();
      if (transcript.trim()) sendMessage();
    }
  };

  recognition.onerror = (event) => {
    stopListening();
    const micErrors = {
      'not-allowed':   '🚫 Mic permission deny ki gayi. Browser settings mein mic allow karo.',
      'no-speech':     'Koi awaaz nahi suni. Dobara try karo.',
      'audio-capture': 'Mic nahi mila. System settings check karo.',
      'network':       'Voice recognition ke liye internet chahiye.',
      'aborted':       null,
    };
    const msg = micErrors[event.error];
    if (msg) addMessage(msg, 'error');
    else if (!(event.error in micErrors)) {
      addMessage(`Voice error: ${event.error}. Dobara try karo.`, 'error');
    }
  };

  recognition.onend = () => { stopListening(); };

  try {
    recognition.start();
  } catch {
    stopListening();
    addMessage('Mic start nahi ho saka. Dobara try karo.', 'error');
  }
}

function stopListening() {
  isListening = false;
  if (recognition) {
    try { recognition.stop(); } catch { /* ignore */ }
    recognition = null;
  }
  voiceBtn.classList.remove('listening');
  voiceBtn.setAttribute('aria-pressed', 'false');
  voiceBtn.setAttribute('aria-label', 'Start voice input');
  userInput.placeholder = 'Message Nova…';
}

// ============================================================
//  NEW CHAT / CLEAR CHAT
// ============================================================

/**
 * Start a fresh conversation.
 * - Resets currentConvId so the next message creates a new DB conversation.
 * - Clears all messages from the UI.
 * - Deselects any active conversation in the sidebar.
 */
function newChat() {
  if ('speechSynthesis' in window) speechSynthesis.cancel();
  if (isListening) stopListening();

  // Reset conversation state
  currentConvId = null;

  // Deselect sidebar items
  document.querySelectorAll('.conv-item.active').forEach(el => el.classList.remove('active'));

  // Remove all messages from chat area
  [...chatBox.children].forEach(child => {
    if (child.id !== 'emptyState') child.remove();
  });

  // Re-insert empty state if it was removed
  if (!document.getElementById('emptyState')) {
    const es = createEmptyState();
    chatBox.appendChild(es);
  }

  userInput.focus();
}

/** Alias kept for any other callers (e.g. suggestion chips) */
function clearChat() { newChat(); }

/** Rebuild the empty state element (used by clearChat) */
function createEmptyState() {
  // Get first name for the welcome message
  const firstName = _getUserFirstName();
  const greeting = firstName ? `Hey, ${firstName} 👋` : `Hey, I'm Nova 👋`;

  const el = document.createElement('div');
  el.className = 'empty-state';
  el.id = 'emptyState';
  el.setAttribute('aria-label', 'Welcome screen');
  el.innerHTML = `
    <div class="empty-robot" aria-hidden="true">
      <lottie-player
        src="AI Robot.json"
        background="transparent"
        speed="1"
        loop
        autoplay>
      </lottie-player>
    </div>
    <h1 class="empty-title">${greeting}</h1>
    <p class="empty-subtitle">Real-time AI assistant · Hinglish · Voice · Tools</p>
    <div class="suggestions-grid" role="list" aria-label="Suggested prompts">
      <button class="suggestion-chip" role="listitem" onclick="useSuggestion(this)">
        <span class="chip-icon">☀️</span>
        <span class="chip-text">Delhi ka weather kya hai?</span>
      </button>
      <button class="suggestion-chip" role="listitem" onclick="useSuggestion(this)">
        <span class="chip-icon">▶️</span>
        <span class="chip-text">Play Arijit Singh songs on YouTube</span>
      </button>
      <button class="suggestion-chip" role="listitem" onclick="useSuggestion(this)">
        <span class="chip-icon">🌐</span>
        <span class="chip-text">Open GitHub for me</span>
      </button>
      <button class="suggestion-chip" role="listitem" onclick="useSuggestion(this)">
        <span class="chip-icon">💬</span>
        <span class="chip-text">Tum kaun ho? Apne baare mein batao.</span>
      </button>
    </div>
  `;
  return el;
}


// ============================================================
//  CONVERSATION HISTORY  (Phase 5)
//  Sidebar list: load, render, select, delete
// ============================================================

/**
 * Fetch the user's conversation list and render it in the sidebar.
 * Called: on page load (after auth), after sending the first message
 * in a new conversation, after deleting a conversation.
 * Silent on failure — history is non-critical.
 */
async function loadConversations() {
  const user = (typeof getUser === 'function') ? getUser() : null;
  if (!user) return;  // anonymous — no history

  const headers = {};
  const authHeader = (typeof getAuthHeader === 'function') ? getAuthHeader() : null;
  if (!authHeader) return;  // can't call authenticated route without a token
  headers['Authorization'] = authHeader;

  try {
    const res = await fetch(`${window.API_BASE_URL}/conversations`, {
      method:  'GET',
      headers,
      signal:  AbortSignal.timeout(8000),
    });

    if (!res.ok) {
      // 401 = token expired; others = backend issue. Either way, skip silently.
      console.warn('[nova] loadConversations failed:', res.status);
      return;
    }

    const data = await res.json();
    renderConversationList(data.conversations || []);
  } catch (err) {
    // Network error, timeout — non-fatal
    console.warn('[nova] loadConversations error:', err?.message || err);
  }
}

/**
 * Render conversation items into the #convList sidebar nav,
 * grouped under date section headers: Today / Yesterday / This Week /
 * This Month / Earlier.
 *
 * Preserves the active selection if the currently active conv is still in
 * the list.
 *
 * @param {Array<{id, title, updated_at}>} convs  — newest-first from backend
 */
function renderConversationList(convs) {
  const list = document.getElementById('convList');
  if (!list) return;

  if (!convs.length) {
    list.innerHTML =
      `<div class="conv-list-empty" aria-label="No conversations yet">` +
        `<span class="conv-list-empty-icon" aria-hidden="true">💬</span>` +
        `<span class="conv-list-empty-title">No chats yet</span>` +
        `<span class="conv-list-empty-sub">Start a new conversation above</span>` +
      `</div>`;
    return;
  }

  // ── Assign each conversation to a labelled bucket ────────────────────────
  const now   = new Date();
  const today = _startOfDay(now);
  const yesterday = new Date(today - 86400000);
  const weekAgo   = new Date(today - 6 * 86400000);   // 6 full days back → "This Week"
  const monthAgo  = new Date(today);
  monthAgo.setDate(1);                                  // first of current month → "This Month"

  // Ordered bucket definitions — first match wins
  const BUCKETS = [
    { key: 'today',     label: 'Today' },
    { key: 'yesterday', label: 'Yesterday' },
    { key: 'week',      label: 'This Week' },
    { key: 'month',     label: 'This Month' },
    { key: 'earlier',   label: 'Earlier' },
  ];

  function _bucket(isoStr) {
    const d = _startOfDay(new Date(isoStr));
    if (d >= today)     return 'today';
    if (d >= yesterday) return 'yesterday';
    if (d >= weekAgo)   return 'week';
    if (d >= monthAgo)  return 'month';
    return 'earlier';
  }

  // Group conversations, preserving order within each bucket
  const groups = {};
  BUCKETS.forEach(b => { groups[b.key] = []; });
  convs.forEach(conv => {
    const b = conv.updated_at ? _bucket(conv.updated_at) : 'earlier';
    groups[b].push(conv);
  });

  // ── Build DOM ─────────────────────────────────────────────────────────────
  const frag = document.createDocumentFragment();

  BUCKETS.forEach(({ key, label }) => {
    const items = groups[key];
    if (!items.length) return;

    // Section header
    const header = document.createElement('div');
    header.className = 'conv-list-group-header';
    header.textContent = label;
    header.setAttribute('aria-hidden', 'true');
    frag.appendChild(header);

    // Conversation buttons
    items.forEach(conv => {
      const item = _buildConvItem(conv);
      frag.appendChild(item);
    });
  });

  list.innerHTML = '';
  list.appendChild(frag);
}

/** Return midnight of the given Date in local time. */
function _startOfDay(d) {
  return new Date(d.getFullYear(), d.getMonth(), d.getDate());
}

/**
 * Build a single conversation button element.
 * Extracted so renderConversationList stays readable.
 */
function _buildConvItem(conv) {
  const item = document.createElement('button');
  item.className  = 'conv-item';
  item.dataset.id = conv.id;
  if (conv.id === currentConvId) item.classList.add('active');

  item.innerHTML = `
    <span class="conv-item-text">
      <span class="conv-item-title">${escHtml(conv.title || 'Nova Chat')}</span>
    </span>
    <span
      class="conv-item-delete"
      role="button"
      aria-label="Delete conversation"
      title="Delete"
      data-id="${escHtml(conv.id)}"
    >
      <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" aria-hidden="true">
        <polyline points="3 6 5 6 21 6"/>
        <path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/>
        <path d="M10 11v6M14 11v6"/>
      </svg>
    </span>
  `;

  item.addEventListener('click', (e) => {
    if (e.target.closest('.conv-item-delete')) return;
    selectConversation(conv.id, conv.title || 'Nova Chat');
  });

  item.querySelector('.conv-item-delete').addEventListener('click', (e) => {
    e.stopPropagation();
    deleteConversationUI(conv.id);
  });

  return item;
}

/**
 * Format a UTC ISO timestamp for sidebar display.
 * Returns "Today", "Yesterday", or "DD MMM" for older dates.
 */
function _formatConvDate(isoStr) {
  if (!isoStr) return '';
  try {
    const d     = new Date(isoStr);
    const now   = new Date();
    const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
    const conv  = new Date(d.getFullYear(), d.getMonth(), d.getDate());
    const diffMs = today - conv;
    if (diffMs === 0)           return 'Today';
    if (diffMs === 86400000)    return 'Yesterday';
    return d.toLocaleDateString(undefined, { day: 'numeric', month: 'short' });
  } catch { return ''; }
}

/**
 * Select a conversation: load its messages into the chat area.
 * Updates sidebar active state and currentConvId.
 */
async function selectConversation(convId, title) {
  if (convId === currentConvId) {
    closeSidebar();
    return;  // already viewing this conv
  }

  currentConvId = convId;

  // Update active state in sidebar
  document.querySelectorAll('.conv-item').forEach(el => {
    el.classList.toggle('active', el.dataset.id === convId);
  });

  closeSidebar();
  await loadConversationMessages(convId);
}

/**
 * Fetch messages for a conversation and render them in the chat area.
 * Shows a loading skeleton while fetching.
 */
async function loadConversationMessages(convId) {
  // Clear current chat (but don't reset currentConvId)
  if ('speechSynthesis' in window) speechSynthesis.cancel();
  [...chatBox.children].forEach(child => {
    if (child.id !== 'emptyState') child.remove();
  });

  // Remove empty state
  const es = document.getElementById('emptyState');
  if (es) es.remove();

  // Show a subtle loading indicator
  const loadingEl = document.createElement('div');
  loadingEl.className = 'conv-loading';
  loadingEl.setAttribute('aria-live', 'polite');
  loadingEl.innerHTML = '<span class="conv-loading-text">Loading conversation…</span>';
  chatBox.appendChild(loadingEl);
  scrollToBottom();

  const headers = {};
  const authHeader = (typeof getAuthHeader === 'function') ? getAuthHeader() : null;
  if (authHeader) headers['Authorization'] = authHeader;

  try {
    const res = await fetch(
      `${window.API_BASE_URL}/conversations/${encodeURIComponent(convId)}/messages`,
      { method: 'GET', headers, signal: AbortSignal.timeout(10000) }
    );

    loadingEl.remove();

    if (!res.ok) {
      if (res.status === 403) {
        addMessage('Yeh conversation access nahi ho saka.', 'error');
      } else {
        addMessage(`Conversation load nahi hua (${res.status}). Dobara try karo.`, 'error');
      }
      return;
    }

    const data = await res.json();
    const messages = data.messages || [];

    if (!messages.length) {
      // Empty conversation — show empty state equivalent
      addMessage('Is conversation mein abhi koi messages nahi hain.', 'bot');
      return;
    }

    // Render all messages without speaking them
    messages.forEach(msg => {
      if (msg.role === 'user') {
        addMessage(msg.content, 'user');
      } else if (msg.role === 'bot') {
        // Tool calls were stored as "[Tool: INTENT]" — show as a tool note
        if (msg.content.startsWith('[Tool:') && msg.tool_intent) {
          addMessage(msg.content, 'bot');
        } else {
          addMessage(msg.content, 'bot');
        }
      }
    });

    scrollToBottom();

  } catch (err) {
    loadingEl.remove();
    if (err.name === 'AbortError' || err.name === 'TimeoutError') {
      addMessage('Conversation load timeout. Dobara try karo.', 'error');
    } else {
      addMessage('Conversation load nahi hua. Backend check karo.', 'error');
    }
  }
}

/**
 * Delete a conversation using the styled confirmation modal, then refresh
 * the sidebar.  Uses showConfirmation() (the same <dialog> modal as tool
 * confirmations) instead of window.confirm() for consistent UI.
 */
async function deleteConversationUI(convId) {
  const confirmed = await showConfirmation('_DELETE_CONVERSATION', { _convId: convId });
  if (!confirmed) return;

  const headers = {};
  const authHeader = (typeof getAuthHeader === 'function') ? getAuthHeader() : null;
  if (authHeader) headers['Authorization'] = authHeader;

  try {
    await fetch(
      `${window.API_BASE_URL}/conversations/${encodeURIComponent(convId)}`,
      { method: 'DELETE', headers, signal: AbortSignal.timeout(8000) }
    );
  } catch (err) {
    console.warn('[nova] deleteConversation error:', err?.message || err);
  }

  // If we just deleted the active conversation, start fresh
  if (convId === currentConvId) {
    newChat();
  }

  // Refresh sidebar list
  loadConversations();
}

// ── Load conversation list on page load ──────────────────────
// Fires after auth init and welcome message with a short delay.
// Only runs when a user is logged in.
setTimeout(loadConversations, 800);



// ============================================================
//  PHASE 6 — Profile Menu + Appearance
//  Covers:
//    - applyTheme / toggleTheme (localStorage + data-theme)
//    - Profile menu open/close (trigger, outside click, Escape)
//    - Profile modal open/close + data population
//    - Reactive theme row in profile modal
//    - Logout wiring
// ============================================================

// ── 1. THEME MANAGEMENT ──────────────────────────────────────

const THEME_KEY = 'nova_theme'; // localStorage key

/**
 * Apply a theme: 'dark' | 'light'
 * - Sets data-theme on <html>
 * - Updates the toggle pill, label, and icon in the menu
 * - Persists to localStorage
 * - Reactively updates the profile modal's Theme row if open
 */
function applyTheme(theme) {
  document.documentElement.setAttribute('data-theme', theme);
  localStorage.setItem(THEME_KEY, theme);

  const pill  = document.getElementById('themePill');
  const label = document.getElementById('themeLabel');
  const icon  = document.getElementById('themeIcon');

  const isLight = theme === 'light';

  // Toggle pill visual state
  if (pill) pill.classList.toggle('on', isLight);

  // Label text
  if (label) label.textContent = isLight ? 'Dark mode' : 'Light mode';

  // Icon: sun (light) or moon (dark)
  if (icon) {
    icon.innerHTML = isLight
      // Moon icon — switch to dark
      ? `<path d="M21 12.79A9 9 0 1 1 11.21 3a7 7 0 0 0 9.79 9.79z"/>`
      // Sun icon — switch to light
      : `<circle cx="12" cy="12" r="5"/>
         <line x1="12" y1="1"  x2="12" y2="3"/>
         <line x1="12" y1="21" x2="12" y2="23"/>
         <line x1="4.22" y1="4.22"  x2="5.64" y2="5.64"/>
         <line x1="18.36" y1="18.36" x2="19.78" y2="19.78"/>
         <line x1="1" y1="12" x2="3" y2="12"/>
         <line x1="21" y1="12" x2="23" y2="12"/>
         <line x1="4.22" y1="19.78" x2="5.64" y2="18.36"/>
         <line x1="18.36" y1="5.64" x2="19.78" y2="4.22"/>`;
  }

  // Reactively update the profile modal's Theme row
  _updateProfileModalThemeRow(theme);
}

/** Toggle between dark and light. */
function toggleTheme() {
  const current = document.documentElement.getAttribute('data-theme') || 'dark';
  applyTheme(current === 'dark' ? 'light' : 'dark');
}

/** Read saved theme from localStorage and apply on page load. */
function initTheme() {
  const saved = localStorage.getItem(THEME_KEY) || 'dark';
  applyTheme(saved);
}

// Initialise immediately
initTheme();


// ── 2. PROFILE MENU OPEN / CLOSE ─────────────────────────────

let _menuOpen = false;

/**
 * Toggle the profile popup menu.
 * Keeps ARIA attributes and the hidden attribute in sync.
 */
function toggleProfileMenu() {
  _menuOpen ? closeProfileMenu() : openProfileMenu();
}

function openProfileMenu() {
  const trigger = document.getElementById('profileTrigger');
  const menu    = document.getElementById('profileMenu');
  if (!trigger || !menu) return;

  _menuOpen = true;
  menu.removeAttribute('hidden');
  trigger.setAttribute('aria-expanded', 'true');

  // Populate the menu header with live user data each time it opens
  _populateMenuHeader();
}

function closeProfileMenu() {
  const trigger = document.getElementById('profileTrigger');
  const menu    = document.getElementById('profileMenu');
  if (!trigger || !menu) return;

  _menuOpen = false;
  menu.setAttribute('hidden', '');
  trigger.setAttribute('aria-expanded', 'false');
}

/** Fill the mini header inside the popup with current user data. */
function _populateMenuHeader() {
  const user = (typeof getUser === 'function') ? getUser() : null;

  const menuAvatar = document.getElementById('menuAvatar');
  const menuName   = document.getElementById('menuName');
  const menuEmail  = document.getElementById('menuEmail');

  if (!menuAvatar || !menuName || !menuEmail) return;

  if (user) {
    const displayName = user.full_name || user.email || 'User';
    menuName.textContent  = displayName;
    menuEmail.textContent = user.email || '';

    // Avatar: image or initial
    if (user.avatar_url) {
      const img  = document.createElement('img');
      img.src    = user.avatar_url;
      img.alt    = displayName;
      img.style.cssText = 'width:100%;height:100%;object-fit:cover;border-radius:50%;';
      img.onerror = () => { menuAvatar.textContent = displayName.charAt(0).toUpperCase(); };
      menuAvatar.innerHTML = '';
      menuAvatar.appendChild(img);
    } else {
      menuAvatar.textContent = displayName.charAt(0).toUpperCase();
    }
  } else {
    menuName.textContent   = 'Guest';
    menuEmail.textContent  = '';
    menuAvatar.textContent = 'G';
  }
}

// Wire up the trigger button
(function _attachMenuTrigger() {
  const trigger = document.getElementById('profileTrigger');
  if (!trigger) return;
  trigger.addEventListener('click', (e) => {
    e.stopPropagation();
    toggleProfileMenu();
  });
})();

// Close on outside click
document.addEventListener('click', (e) => {
  if (!_menuOpen) return;
  const menu    = document.getElementById('profileMenu');
  const trigger = document.getElementById('profileTrigger');
  if (!menu) return;

  // If the click is outside both the menu and the trigger, close
  if (!menu.contains(e.target) && !trigger.contains(e.target)) {
    closeProfileMenu();
  }
});

// Close on Escape
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && _menuOpen) {
    closeProfileMenu();
    document.getElementById('profileTrigger')?.focus();
  }
});


// ── 3. THEME TOGGLE — wire up menu item ──────────────────────
(function _attachThemeToggle() {
  const btn = document.getElementById('menuItemTheme');
  if (!btn) return;
  btn.addEventListener('click', (e) => {
    e.stopPropagation();    // don't close the menu
    toggleTheme();
    // Briefly prevent default close so the user sees the pill animate
    // Menu stays open so user can see the change — they close it manually
  });
})();


// ── 4. PROFILE MODAL ─────────────────────────────────────────

const profileModal        = document.getElementById('profileModal');
const profileModalClose   = document.getElementById('profileModalClose');
const profileModalLogout  = document.getElementById('profileModalLogout');

/** Populate and open the profile modal. */
function openProfileModal() {
  if (!profileModal) return;

  // Close the menu first
  closeProfileMenu();

  // Populate with current user data
  const user = (typeof getUser === 'function') ? getUser() : null;

  const avatarEl = document.getElementById('profileModalAvatar');
  const nameEl   = document.getElementById('profileModalTitle');
  const emailEl  = document.getElementById('profileModalEmail');

  if (user) {
    const displayName = user.full_name || user.email || 'User';

    if (nameEl)  nameEl.textContent  = displayName;
    if (emailEl) emailEl.textContent = user.email || '';

    // Large avatar
    if (avatarEl) {
      if (user.avatar_url) {
        const img  = document.createElement('img');
        img.src    = user.avatar_url;
        img.alt    = displayName;
        img.onerror = () => { avatarEl.textContent = displayName.charAt(0).toUpperCase(); };
        avatarEl.innerHTML = '';
        avatarEl.appendChild(img);
      } else {
        avatarEl.textContent = displayName.charAt(0).toUpperCase();
      }
    }
  } else {
    if (nameEl)   nameEl.textContent  = 'Guest';
    if (emailEl)  emailEl.textContent = '';
    if (avatarEl) avatarEl.textContent = 'G';
  }

  // Populate Theme row to reflect current theme
  const currentTheme = document.documentElement.getAttribute('data-theme') || 'dark';
  _updateProfileModalThemeRow(currentTheme);

  profileModal.showModal();
}

/** Update the Theme info row in the profile modal. */
function _updateProfileModalThemeRow(theme) {
  const themeRow = document.getElementById('profileModalTheme');
  if (!themeRow) return;
  themeRow.textContent = theme === 'light' ? 'Light' : 'Dark';
}

/** Close the profile modal. */
function closeProfileModal() {
  if (profileModal && profileModal.open) profileModal.close();
}

// Close button
if (profileModalClose) {
  profileModalClose.addEventListener('click', closeProfileModal);
}

// Clicking the backdrop (::backdrop) closes the modal
if (profileModal) {
  profileModal.addEventListener('click', (e) => {
    // A click directly on the <dialog> element (not its content) = backdrop click
    if (e.target === profileModal) closeProfileModal();
  });

  // Escape key is handled natively by <dialog>, but let's keep modal in sync
  profileModal.addEventListener('cancel', (e) => {
    // Default behaviour already closes the dialog; no extra action needed
  });
}


// ── 5. MENU ITEMS WIRING ─────────────────────────────────────

// "Profile" → open profile modal
document.getElementById('menuItemProfile')?.addEventListener('click', () => {
  openProfileModal();
});

// "Sign out" in popup menu → logout
document.getElementById('menuItemLogout')?.addEventListener('click', () => {
  closeProfileMenu();
  if (typeof logout === 'function') logout();
});

// "Sign out" in profile modal → logout
document.getElementById('profileModalLogout')?.addEventListener('click', () => {
  closeProfileModal();
  if (typeof logout === 'function') logout();
});


// ── 5. MENU ITEMS WIRING ─────────────────────────────────────

// "Profile" → open profile modal
(function _attachMenuProfile() {
  const btn = document.getElementById('menuItemProfile');
  if (!btn) return;
  btn.addEventListener('click', () => openProfileModal());
})();

// "Sign out" in popup menu → logout
(function _attachMenuLogout() {
  const btn = document.getElementById('menuItemLogout');
  if (!btn) return;
  btn.addEventListener('click', () => {
    closeProfileMenu();
    if (typeof logout === 'function') logout();
  });
})();

// "Sign out" in profile modal → logout
if (profileModalLogout) {
  profileModalLogout.addEventListener('click', () => {
    closeProfileModal();
    if (typeof logout === 'function') logout();
  });
}

// ── 5. MENU ITEMS WIRING ─────────────────────────────────────

// "Profile" → open profile modal
document.getElementById('menuItemProfile')?.addEventListener('click', () => {
  openProfileModal();
});

// "Sign out" in popup menu → logout
document.getElementById('menuItemLogout')?.addEventListener('click', () => {
  closeProfileMenu();
  if (typeof logout === 'function') logout();
});

// "Sign out" in profile modal → logout
document.getElementById('profileModalLogout')?.addEventListener('click', () => {
  closeProfileModal();
  if (typeof logout === 'function') logout();
});


// ============================================================
//  SETTINGS PANEL
//  Manages the <dialog id="settingsModal"> — Profile section
//  and AI Settings section (language, voice, auto-speak, style).
//
//  State is read/written via prefs.js (localStorage, per-user).
//  All controls reflect current prefs on open; Save persists them.
// ============================================================

const settingsModal    = document.getElementById('settingsModal');
const settingsCloseBtn = document.getElementById('settingsCloseBtn');
const settingsSaveBtn  = document.getElementById('settingsSaveBtn');

// ── Open / close ──────────────────────────────────────────────

function openSettings() {
  if (!settingsModal) return;
  closeProfileMenu();
  _populateSettings();
  settingsModal.showModal();
}

function closeSettings() {
  if (settingsModal && settingsModal.open) settingsModal.close();
}

// Close button
settingsCloseBtn?.addEventListener('click', closeSettings);

// Backdrop click closes
settingsModal?.addEventListener('click', (e) => {
  if (e.target === settingsModal) closeSettings();
});

// Escape handled natively by <dialog>

// ── Populate controls from prefs ──────────────────────────────

function _populateSettings() {
  // ── Profile section ───────────────────────────────────────
  const user        = (typeof getUser === 'function') ? getUser() : null;
  const avatarEl    = document.getElementById('settingsAvatar');
  const nameEl      = document.getElementById('settingsName');
  const emailEl     = document.getElementById('settingsEmail');

  if (user) {
    const dn = (typeof getDisplayName === 'function') ? getDisplayName() : null;
    const displayName = dn || user.full_name || user.email || 'User';

    if (nameEl)  nameEl.textContent  = displayName;
    if (emailEl) emailEl.textContent = user.email || '';

    if (avatarEl) {
      if (user.avatar_url) {
        // Use img if avatar_url is set
        const img = document.createElement('img');
        img.src = user.avatar_url;
        img.alt = displayName;
        img.onerror = () => { avatarEl.textContent = displayName.charAt(0).toUpperCase(); };
        avatarEl.innerHTML = '';
        avatarEl.appendChild(img);
      } else {
        avatarEl.textContent = displayName.charAt(0).toUpperCase();
      }
    }
  } else {
    if (nameEl)   nameEl.textContent   = 'Guest';
    if (emailEl)  emailEl.textContent  = '';
    if (avatarEl) avatarEl.textContent = 'G';
  }

  // ── AI Settings section ───────────────────────────────────
  const prefs = (typeof getPrefs === 'function') ? getPrefs() : {
    language: 'hinglish', voice_enabled: true, auto_speak: true, response_style: 'normal',
  };

  // Segmented controls — mark active button
  document.querySelectorAll('.seg-btn').forEach(btn => {
    const prefKey = btn.dataset.pref;
    const prefVal = btn.dataset.val;
    btn.classList.toggle('active', prefs[prefKey] === prefVal);
  });

  // Toggle buttons — set aria-checked
  _setToggle('toggleVoiceEnabled', prefs.voice_enabled);
  _setToggle('toggleAutoSpeak',    prefs.auto_speak);

  // Reset save button to idle state
  _setSaveState('idle');
}

// ── Helpers ───────────────────────────────────────────────────

function _setToggle(id, isOn) {
  const btn = document.getElementById(id);
  if (!btn) return;
  btn.setAttribute('aria-checked', isOn ? 'true' : 'false');
}

function _setSaveState(state) {
  const btn     = document.getElementById('settingsSaveBtn');
  const spinner = document.getElementById('settingsSaveSpinner');
  const txt     = document.getElementById('settingsSaveTxt');
  if (!btn) return;

  if (state === 'saving') {
    btn.disabled = true;
    spinner?.classList.add('visible');
    if (txt) txt.textContent = 'Saving…';
  } else if (state === 'saved') {
    btn.disabled = false;
    spinner?.classList.remove('visible');
    if (txt) txt.textContent = '✓ Saved!';
    setTimeout(() => { if (txt) txt.textContent = 'Save preferences'; }, 1600);
  } else {
    btn.disabled = false;
    spinner?.classList.remove('visible');
    if (txt) txt.textContent = 'Save preferences';
  }
}

// ── Segmented button interaction ──────────────────────────────

document.querySelectorAll('.seg-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    const prefKey = btn.dataset.pref;
    // Deactivate siblings in the same group, activate clicked
    document.querySelectorAll(`.seg-btn[data-pref="${prefKey}"]`).forEach(b => {
      b.classList.toggle('active', b === btn);
    });
  });
});

// ── Toggle switch interaction ──────────────────────────────────

document.querySelectorAll('.settings-toggle-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    const current = btn.getAttribute('aria-checked') === 'true';
    btn.setAttribute('aria-checked', current ? 'false' : 'true');
  });
});

// ── Save button ───────────────────────────────────────────────

settingsSaveBtn?.addEventListener('click', () => {
  _setSaveState('saving');

  // Read segmented values
  const language = document.querySelector('.seg-btn[data-pref="language"].active')?.dataset.val
    || 'hinglish';
  const responseStyle = document.querySelector('.seg-btn[data-pref="response_style"].active')?.dataset.val
    || 'normal';

  // Read toggles
  const voiceEnabled = document.getElementById('toggleVoiceEnabled')?.getAttribute('aria-checked') === 'true';
  const autoSpeak    = document.getElementById('toggleAutoSpeak')?.getAttribute('aria-checked') === 'true';

  // Persist via prefs.js
  if (typeof savePrefs === 'function') {
    savePrefs({
      language,
      voice_enabled:  voiceEnabled,
      auto_speak:     autoSpeak,
      response_style: responseStyle,
    });
  }

  // Apply voice btn visibility immediately
  _applyVoicePref(voiceEnabled);

  // Brief saving animation then close
  setTimeout(() => {
    _setSaveState('saved');
    setTimeout(closeSettings, 800);
  }, 400);
});

/**
 * Show or dim the voice button based on the voice_enabled pref.
 * The button stays in the DOM — we just add an .pref-disabled class
 * that dims it and prevents the click from activating voice.
 * The actual gate is in toggleVoiceInput() which checks getPref().
 */
function _applyVoicePref(enabled) {
  const btn = document.getElementById('voiceBtn');
  if (!btn) return;
  btn.classList.toggle('pref-disabled', !enabled);
  btn.title = enabled ? 'Voice input' : 'Voice input (disabled in Settings)';
}

/** Apply all prefs to the live UI — called once on page load. */
function _applyAllPrefs() {
  if (typeof getPref !== 'function') return;
  _applyVoicePref(getPref('voice_enabled'));
}

// Run on load (after prefs.js has initialised)
_applyAllPrefs();

// ── Wire "Settings" menu item ──────────────────────────────────

document.getElementById('menuItemSettings')?.addEventListener('click', () => {
  openSettings();
});

// ── Settings logout button ────────────────────────────────────

document.getElementById('settingsLogoutBtn')?.addEventListener('click', () => {
  closeSettings();
  if (typeof logout === 'function') logout();
});


// ============================================================
//  PRIVACY SECTION — Settings panel
//
//  All mutating actions require a valid auth token.
//  user_id is NEVER sent from the frontend — the backend reads
//  it from the verified JWT.  We only send the token.
//
//  Three subsections:
//    1. Memories   — load list, show/hide, clear all
//    2. Conversations — clear all with confirmation
//    3. Delete account — two-step: show panel + type "DELETE"
// ============================================================

// ── Auth header helper (reuse existing getAuthHeader from auth.js) ──
function _privacyHeaders() {
  const h = { 'Content-Type': 'application/json' };
  const auth = (typeof getAuthHeader === 'function') ? getAuthHeader() : null;
  if (auth) h['Authorization'] = auth;
  return h;
}

// ── Load and render memories when settings panel opens ───────

let _privacyMemoriesLoaded = false;

async function privacyLoadMemories() {
  _privacyMemoriesLoaded = false;
  const descEl   = document.getElementById('privacyMemoriesDesc');
  const showBtn  = document.getElementById('privacyToggleMemoriesBtn');
  const clearBtn = document.getElementById('privacyClearMemoriesBtn');
  if (!descEl) return;

  // Must be logged in
  if (typeof isLoggedIn === 'function' && !isLoggedIn()) {
    descEl.textContent = 'Log in to view memories.';
    return;
  }

  descEl.textContent = 'Loading…';

  try {
    const res = await fetch(`${window.API_BASE_URL}/privacy/memories`, {
      method: 'GET',
      headers: _privacyHeaders(),
      signal: AbortSignal.timeout(10_000),
    });

    if (!res.ok) {
      descEl.textContent = `Error loading memories (${res.status}).`;
      return;
    }

    const data = await res.json();
    const count = data.count || 0;
    _privacyMemoriesLoaded = true;

    if (count === 0) {
      descEl.textContent = 'No memories stored.';
      if (showBtn)  showBtn.hidden  = true;
      if (clearBtn) clearBtn.hidden = true;
      _renderMemoryList([]);
    } else {
      descEl.textContent = `${count} item${count === 1 ? '' : 's'} stored`;
      if (showBtn)  showBtn.hidden  = false;
      if (clearBtn) clearBtn.hidden = false;
      _renderMemoryList(data.memories || []);
    }
  } catch (err) {
    descEl.textContent = 'Could not load memories.';
  }
}

function _renderMemoryList(memories) {
  const listEl = document.getElementById('privacyMemoryList');
  if (!listEl) return;

  listEl.innerHTML = '';

  if (!memories.length) {
    const empty = document.createElement('div');
    empty.className   = 'privacy-memory-empty';
    empty.textContent = 'No memories yet.';
    listEl.appendChild(empty);
    return;
  }

  memories.forEach(m => {
    const row = document.createElement('div');
    row.className = 'privacy-memory-item';

    const keyEl = document.createElement('span');
    keyEl.className   = 'privacy-memory-key';
    keyEl.textContent = m.key;
    keyEl.title       = m.key;

    const valEl = document.createElement('span');
    valEl.className   = 'privacy-memory-val';
    // Truncate long values visually; full value in title for hover
    valEl.textContent = m.value.length > 80 ? m.value.slice(0, 77) + '…' : m.value;
    valEl.title       = m.value;

    row.appendChild(keyEl);
    row.appendChild(valEl);
    listEl.appendChild(row);
  });
}

// ── Show / hide memory list ───────────────────────────────────

let _memoriesVisible = false;

function privacyToggleMemories() {
  _memoriesVisible = !_memoriesVisible;
  const listEl  = document.getElementById('privacyMemoryList');
  const showBtn = document.getElementById('privacyToggleMemoriesBtn');
  if (listEl)  listEl.hidden   = !_memoriesVisible;
  if (showBtn) showBtn.textContent = _memoriesVisible ? 'Hide' : 'Show';
}

document.getElementById('privacyToggleMemoriesBtn')
  ?.addEventListener('click', privacyToggleMemories);

// ── Clear memories ────────────────────────────────────────────

document.getElementById('privacyClearMemoriesBtn')?.addEventListener('click', async () => {
  const confirmed = await showConfirmation('_PRIVACY_CLEAR_MEMORIES', {});
  if (!confirmed) return;

  const btn = document.getElementById('privacyClearMemoriesBtn');
  if (btn) { btn.disabled = true; btn.textContent = 'Clearing…'; }

  try {
    const res = await fetch(`${window.API_BASE_URL}/privacy/memories`, {
      method: 'DELETE',
      headers: _privacyHeaders(),
      signal: AbortSignal.timeout(10_000),
    });

    const data = await res.json().catch(() => ({}));

    if (res.ok && data.ok) {
      // Re-render empty state
      const descEl = document.getElementById('privacyMemoriesDesc');
      if (descEl) descEl.textContent = 'No memories stored.';
      document.getElementById('privacyToggleMemoriesBtn')?.setAttribute('hidden', '');
      if (btn) btn.hidden = true;
      _renderMemoryList([]);
      const listEl = document.getElementById('privacyMemoryList');
      if (listEl) listEl.hidden = true;
      _memoriesVisible = false;
      // Notify chat UI
      addMessage('✓ All memories cleared.', 'bot');
    } else {
      addMessage(`Memory clear failed: ${data.message || res.status}`, 'error');
    }
  } catch (err) {
    addMessage('Memory clear failed. Try again.', 'error');
  } finally {
    if (btn) { btn.disabled = false; btn.innerHTML = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" aria-hidden="true"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/></svg> Clear all memories'; }
  }
});

// ── Clear conversations ───────────────────────────────────────

document.getElementById('privacyClearConvsBtn')?.addEventListener('click', async () => {
  const confirmed = await showConfirmation('_PRIVACY_CLEAR_CONVS', {});
  if (!confirmed) return;

  const btn = document.getElementById('privacyClearConvsBtn');
  if (btn) { btn.disabled = true; btn.textContent = 'Clearing…'; }

  try {
    const res = await fetch(`${window.API_BASE_URL}/privacy/conversations`, {
      method: 'DELETE',
      headers: _privacyHeaders(),
      signal: AbortSignal.timeout(15_000),
    });

    const data = await res.json().catch(() => ({}));

    if (res.ok && data.ok) {
      // Reset sidebar and chat
      newChat();
      loadConversations();
      addMessage(`✓ All conversations cleared (${data.deleted_count || 0} removed).`, 'bot');
      closeSettings();
    } else {
      addMessage(`Conversations clear failed: ${data.message || res.status}`, 'error');
    }
  } catch (err) {
    addMessage('Conversations clear failed. Try again.', 'error');
  } finally {
    if (btn) { btn.disabled = false; btn.innerHTML = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" aria-hidden="true"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/></svg> Clear all conversations'; }
  }
});

// ── Delete account — two-step ─────────────────────────────────

// Step 1: reveal the confirmation panel
document.getElementById('privacyShowDeleteBtn')?.addEventListener('click', () => {
  const confirmEl = document.getElementById('privacyDeleteConfirm');
  const showBtn   = document.getElementById('privacyShowDeleteBtn');
  if (!confirmEl) return;
  confirmEl.hidden = false;
  showBtn.hidden   = true;
  // Focus the input for keyboard users
  setTimeout(() => document.getElementById('privacyDeleteInput')?.focus(), 50);
});

// Cancel
document.getElementById('privacyCancelDeleteBtn')?.addEventListener('click', () => {
  _resetDeleteAccountUI();
});

function _resetDeleteAccountUI() {
  const confirmEl = document.getElementById('privacyDeleteConfirm');
  const showBtn   = document.getElementById('privacyShowDeleteBtn');
  const input     = document.getElementById('privacyDeleteInput');
  const hintEl    = document.getElementById('privacyDeleteHint');
  const confirmBtn = document.getElementById('privacyConfirmDeleteBtn');

  if (confirmEl) confirmEl.hidden = true;
  if (showBtn)   showBtn.hidden   = false;
  if (input)     { input.value = ''; input.classList.remove('valid'); }
  if (hintEl)    { hintEl.textContent = ''; hintEl.className = 'privacy-delete-hint'; }
  if (confirmBtn) confirmBtn.disabled = true;
}

// Enable confirm button only when user typed exactly "DELETE"
document.getElementById('privacyDeleteInput')?.addEventListener('input', (e) => {
  const val        = e.target.value;
  const confirmBtn = document.getElementById('privacyConfirmDeleteBtn');
  const hintEl     = document.getElementById('privacyDeleteHint');
  const isExact    = val === 'DELETE';

  e.target.classList.toggle('valid', isExact);
  if (confirmBtn) confirmBtn.disabled = !isExact;

  if (!val) {
    hintEl.textContent = '';
    hintEl.className   = 'privacy-delete-hint';
  } else if (isExact) {
    hintEl.textContent = 'Ready. Click the button below to permanently delete.';
    hintEl.className   = 'privacy-delete-hint ready';
  } else {
    hintEl.textContent = `Type exactly: DELETE (${val.length}/6)`;
    hintEl.className   = 'privacy-delete-hint error';
  }
});

// Step 2: confirm deletion
document.getElementById('privacyConfirmDeleteBtn')?.addEventListener('click', async () => {
  const input = document.getElementById('privacyDeleteInput');
  if (!input || input.value !== 'DELETE') return;

  const confirmBtn = document.getElementById('privacyConfirmDeleteBtn');
  const spinner    = document.getElementById('privacyDeleteSpinner');
  const btnTxt     = document.getElementById('privacyDeleteBtnTxt');

  if (confirmBtn) confirmBtn.disabled = true;
  if (spinner)    spinner.classList.add('visible');
  if (btnTxt)     btnTxt.textContent  = 'Deleting…';

  try {
    const res = await fetch(`${window.API_BASE_URL}/privacy/account`, {
      method: 'DELETE',
      headers: _privacyHeaders(),
      signal: AbortSignal.timeout(20_000),
    });

    const data = await res.json().catch(() => ({}));

    if (res.ok && data.ok) {
      // Clear all local state
      if (typeof clearSession === 'function')    clearSession();
      localStorage.removeItem('nova_display_name');
      localStorage.removeItem('nova_name_skipped');
      // Clear prefs for all keys matching nova_prefs_*
      Object.keys(localStorage)
        .filter(k => k.startsWith('nova_prefs_'))
        .forEach(k => localStorage.removeItem(k));

      // Redirect to login
      window.location.href = 'login.html';
    } else {
      if (spinner) spinner.classList.remove('visible');
      if (btnTxt)  btnTxt.textContent = 'Delete account permanently';
      if (confirmBtn) confirmBtn.disabled = false;
      addMessage(`Account deletion failed: ${data.message || res.status}`, 'error');
      closeSettings();
    }
  } catch (err) {
    if (spinner) spinner.classList.remove('visible');
    if (btnTxt)  btnTxt.textContent = 'Delete account permanently';
    if (confirmBtn) confirmBtn.disabled = false;
    addMessage('Account deletion failed. Try again.', 'error');
  }
});

// ── Confirmation modal entries for privacy actions ───────────
// Reuse the existing showConfirmation() / TOOL_CONFIRM_META mechanism.
// These pseudo-intents are never sent to the backend — they only drive
// the confirmation modal UI.

TOOL_CONFIRM_META['_PRIVACY_CLEAR_MEMORIES'] = {
  icon:      '🧠',
  title:     'Clear all memories?',
  body:      'Nova will forget everything you have asked her to remember. This cannot be undone.',
  getDetail: () => 'All stored memories will be permanently deleted.',
};

TOOL_CONFIRM_META['_PRIVACY_CLEAR_CONVS'] = {
  icon:      '💬',
  title:     'Clear all conversations?',
  body:      'All chat history will be permanently deleted from the server.',
  getDetail: () => 'This cannot be undone. Your current chat will also be reset.',
};

// ── Re-load memories every time the settings panel opens ──────
// Patch openSettings to call privacyLoadMemories.
const _origOpenSettings = openSettings;
window.openSettings = function() {
  _origOpenSettings();
  _resetDeleteAccountUI();
  _memoriesVisible = false;
  const listEl = document.getElementById('privacyMemoryList');
  if (listEl) listEl.hidden = true;
  const showBtn = document.getElementById('privacyToggleMemoriesBtn');
  if (showBtn) showBtn.textContent = 'Show';
  privacyLoadMemories();
};


// ============================================================
//  CONNECTED DEVICES — Settings panel (Section 4)
//
//  Fetches real data from GET /device/devices (JWT-authenticated).
//  Shows honest Online / Offline / Not connected states.
//  No faking — if nothing is registered the empty state is shown.
// ============================================================

/**
 * Fetch device list and render it in the Connected Devices section.
 * Called every time the Settings panel opens.
 */
async function devicesLoad() {
  const loadingEl  = document.getElementById('deviceLoading');
  const emptyEl    = document.getElementById('deviceEmptyState');
  const listEl     = document.getElementById('deviceList');
  const errorEl    = document.getElementById('deviceError');

  // Reset to loading state
  if (loadingEl) loadingEl.hidden = false;
  if (emptyEl)   emptyEl.hidden   = true;
  if (listEl)    listEl.hidden    = true;
  if (errorEl)   errorEl.hidden   = true;
  if (listEl)    listEl.innerHTML = '';

  // Must be logged in to call this endpoint
  const authHeader = (typeof getAuthHeader === 'function') ? getAuthHeader() : null;
  if (!authHeader) {
    if (loadingEl) loadingEl.hidden = true;
    if (emptyEl)   emptyEl.hidden   = false;
    return;
  }

  try {
    const res = await fetch(`${window.API_BASE_URL}/device/devices`, {
      method:  'GET',
      headers: { 'Authorization': authHeader },
      signal:  AbortSignal.timeout(8_000),
    });

    if (loadingEl) loadingEl.hidden = true;

    if (!res.ok) {
      // 401 / 503 = auth or config issue — show empty state, not an error
      if (res.status === 401 || res.status === 503) {
        if (emptyEl) emptyEl.hidden = false;
        return;
      }
      throw new Error(`HTTP ${res.status}`);
    }

    const data = await res.json();
    const devices = data.devices || [];

    if (devices.length === 0) {
      if (emptyEl) emptyEl.hidden = false;
      return;
    }

    // Render device cards
    devices.forEach(device => {
      const card = _buildDeviceCard(device);
      listEl.appendChild(card);
    });
    if (listEl) listEl.hidden = false;

  } catch (err) {
    if (loadingEl) loadingEl.hidden = true;
    // Network error or timeout — show empty state with a soft note
    if (emptyEl) emptyEl.hidden = false;
    if (err.name !== 'AbortError' && err.name !== 'TimeoutError') {
      // Only show an explicit error for unexpected failures
      if (errorEl) {
        errorEl.textContent = 'Could not load device info. Check connection.';
        errorEl.hidden = false;
      }
    }
  }
}

/**
 * Build a single device <li> card element from the API response object.
 * @param {Object} device  — {device_name, online, last_seen, registered_at, ...}
 * @returns {HTMLLIElement}
 */
function _buildDeviceCard(device) {
  const isOnline = Boolean(device.online);

  const li = document.createElement('li');
  li.className = isOnline ? 'device-card device-card--online' : 'device-card';

  // Status dot
  const dot = document.createElement('span');
  dot.className = isOnline
    ? 'device-status-dot device-status-dot--online'
    : 'device-status-dot device-status-dot--offline';
  dot.setAttribute('aria-hidden', 'true');

  // Info block
  const info = document.createElement('div');
  info.className = 'device-info';

  const nameEl = document.createElement('span');
  nameEl.className   = 'device-name';
  nameEl.textContent = device.device_name || 'Android Device';

  const metaEl = document.createElement('span');
  metaEl.className   = 'device-meta';
  metaEl.textContent = _deviceMetaLine(device);

  info.appendChild(nameEl);
  info.appendChild(metaEl);

  // Status badge
  const badge = document.createElement('span');
  badge.className   = isOnline ? 'device-badge device-badge--online' : 'device-badge device-badge--offline';
  badge.textContent = isOnline ? 'Online' : 'Offline';

  li.appendChild(dot);
  li.appendChild(info);
  li.appendChild(badge);
  return li;
}

/**
 * Build the secondary meta line for a device card.
 * Shows last-seen relative time when offline, or "Active now" when online.
 */
function _deviceMetaLine(device) {
  if (device.online) {
    return 'Active now';
  }
  if (device.last_seen) {
    const ago = _relativeTime(device.last_seen);
    return ago ? `Last seen ${ago}` : 'Last seen: unknown';
  }
  if (device.registered_at) {
    const ago = _relativeTime(device.registered_at);
    return ago ? `Registered ${ago}` : 'Registered';
  }
  return 'Never connected';
}

/**
 * Convert a Unix timestamp (seconds) to a human-readable relative string.
 * e.g. "2 minutes ago", "3 hours ago", "just now"
 */
function _relativeTime(unixSeconds) {
  if (!unixSeconds) return null;
  const diffMs   = Date.now() - unixSeconds * 1000;
  const diffSecs = Math.floor(diffMs / 1000);
  if (diffSecs < 0)     return 'just now';
  if (diffSecs < 60)    return 'just now';
  const diffMin  = Math.floor(diffSecs / 60);
  if (diffMin < 60)     return `${diffMin} minute${diffMin === 1 ? '' : 's'} ago`;
  const diffHr   = Math.floor(diffMin / 60);
  if (diffHr < 24)      return `${diffHr} hour${diffHr === 1 ? '' : 's'} ago`;
  const diffDay  = Math.floor(diffHr / 24);
  if (diffDay < 30)     return `${diffDay} day${diffDay === 1 ? '' : 's'} ago`;
  const diffMon  = Math.floor(diffDay / 30);
  return `${diffMon} month${diffMon === 1 ? '' : 's'} ago`;
}

// ── Hook into the existing openSettings patch ─────────────────
// The previous patch (Privacy section) already overwrites window.openSettings.
// We extend it by wrapping it once more so devicesLoad() is also called.
(function _patchOpenSettingsForDevices() {
  const _prev = window.openSettings;
  window.openSettings = function () {
    _prev();
    devicesLoad();
  };
})();
