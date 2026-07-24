/**
 * app.js – LaTeX Studio frontend logic.
 *
 * Responsibilities:
 *   - Drag-and-drop + click upload (multi-file and ZIP)
 *   - Session lifecycle (persisted in localStorage so a refresh keeps your work)
 *   - File tree (keyboard-accessible) + in-browser editor (Monaco, with a
 *     <textarea> fallback if Monaco was not vendored)
 *   - Compile trigger with a live timer, PDF preview + download
 *   - Structured, filterable log rendering
 *
 * Security: every API call carries the per-instance token the backend injected
 * into the page, and all server-supplied strings are HTML-escaped before they
 * are placed in the DOM.
 */

'use strict';

// ─── Configuration ────────────────────────────────────────────────────────────
const API_BASE = '';  // same origin – FastAPI serves both the UI and the API
const STUDIO_TOKEN =
  document.querySelector('meta[name="studio-token"]')?.content || '';
const SESSION_STORAGE_KEY = 'latexStudio.sessionId';

// ─── State ───────────────────────────────────────────────────────────────────
const state = {
  sessionId: null,
  files: [],
  detectedMain: null,
  isUploading: false,
  isCompiling: false,
  compileTimer: null,
  compileStart: null,
  rawLog: '',
  currentFilter: 'all',
  parsedLog: null,
  pdfUrl: null,
  currentOpenFile: null,
  currentIsText: false,      // true only after a text file loads successfully
  monacoEditor: null,
  editorKind: null,          // 'monaco' | 'textarea' | null
  pendingEditorContent: null,
  pendingEditorLanguage: null,
};

// ─── DOM References ───────────────────────────────────────────────────────────
const $ = (id) => document.getElementById(id);
const dom = {
  dropZone: $('dropZone'), fileInput: $('fileInput'),
  uploadProgress: $('uploadProgress'), progressFill: $('progressFill'), progressText: $('progressText'),
  fileTree: $('fileTree'), fileTreeEmpty: $('fileTreeEmpty'),
  mainFileSelector: $('mainFileSelector'), mainFileSelect: $('mainFileSelect'),
  clearSessionBtn: $('clearSessionBtn'),
  compileBtn: $('compileBtn'), compileBtnText: $('compileBtnText'), compileSpinner: $('compileSpinner'),
  engineSelect: $('engineSelect'), statusDot: $('statusDot'), statusText: $('statusText'),
  editorPanel: $('editorPanel'), editorFileTitle: $('editorFileTitle'),
  monacoContainer: $('monacoContainer'), imageViewer: $('imageViewer'), imageViewerImg: $('imageViewerImg'),
  editorEmptyState: $('editorEmptyState'),
  pdfViewport: $('pdfViewport'), pdfEmptyState: $('pdfEmptyState'), pdfCompilingState: $('pdfCompilingState'),
  pdfFrame: $('pdfFrame'), compilingDesc: $('compilingDesc'), compileTimerEl: $('compileTimer'),
  downloadBtn: $('downloadBtn'), openNewTabBtn: $('openNewTabBtn'),
  logSection: $('logSection'), toggleLogBtn: $('toggleLogBtn'),
  logContainer: $('logContainer'), logEmpty: $('logEmpty'),
  logSummary: $('logSummary'), logSummaryInner: $('logSummaryInner'), compileDuration: $('compileDuration'),
  errorCount: $('errorCount'), warnCount: $('warnCount'),
  copyLogBtn: $('copyLogBtn'), rawLogDetails: $('rawLogDetails'), rawLogPre: $('rawLogPre'),
  toastContainer: $('toastContainer'), sessionInfo: $('sessionInfo'),
  fileCount: $('fileCount'), lastCompileStatus: $('lastCompileStatus'),
  filterAll: $('filterAll'), filterErrors: $('filterErrors'), filterWarnings: $('filterWarnings'),
};

// ─── API helper (adds the security token to every request) ────────────────────
function api(path, options = {}) {
  const headers = new Headers(options.headers || {});
  if (STUDIO_TOKEN) headers.set('X-Studio-Token', STUDIO_TOKEN);
  return fetch(`${API_BASE}${path}`, { ...options, headers });
}

// ─── Small utilities ──────────────────────────────────────────────────────────
function formatSize(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function fileIcon(ext) {
  const icons = {
    '.tex': '📄', '.bib': '📚', '.cls': '🎨', '.sty': '🎨',
    '.png': '🖼️', '.jpg': '🖼️', '.jpeg': '🖼️', '.svg': '🖼️', '.eps': '🖼️',
    '.pdf': '📕', '.zip': '📦', '.ttf': '🔤', '.otf': '🔤',
    '.csv': '📊', '.txt': '📝', '.bst': '📋', '.log': '🪵', '.aux': '⚙️',
  };
  return icons[ext] || '📄';
}

function elapsedSince(start) {
  return ((Date.now() - start) / 1000).toFixed(1) + 's';
}

/** Escape every character that could break out of HTML text or an attribute. */
function escapeHtml(str) {
  return String(str ?? '')
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

// ─── Toast Notifications ──────────────────────────────────────────────────────
function showToast(message, type = 'info', duration = 4000) {
  const icons = { success: '✅', error: '❌', warning: '⚠️', info: 'ℹ️' };
  const toast = document.createElement('div');
  toast.className = `toast ${type}`;
  // textContent for the message so a hostile filename cannot inject markup.
  const icon = document.createElement('span');
  icon.className = 'toast-icon';
  icon.textContent = icons[type] || 'ℹ️';
  const msg = document.createElement('span');
  msg.className = 'toast-msg';
  msg.textContent = message;
  toast.append(icon, msg);
  dom.toastContainer.appendChild(toast);
  setTimeout(() => {
    toast.classList.add('hiding');
    setTimeout(() => toast.remove(), 300);
  }, duration);
}

// ─── Status Bar ───────────────────────────────────────────────────────────────
function updateStatusBar() {
  dom.sessionInfo.textContent = state.sessionId
    ? `Session: ${state.sessionId.slice(0, 8)}…` : 'No active session';
  dom.fileCount.textContent = `${state.files.length} file${state.files.length !== 1 ? 's' : ''}`;
}

// ─── LaTeX status check ───────────────────────────────────────────────────────
async function checkLatexStatus() {
  try {
    const res = await api('/api/status');
    if (!res.ok) throw new Error('Backend unreachable');
    const data = await res.json();
    if (data.latex_available) {
      dom.statusDot.className = 'status-dot ok';
      const tools = Object.entries(data.tools).filter(([, v]) => v.available).map(([k]) => k).join(', ');
      dom.statusText.textContent = `LaTeX ready (${tools})`;
      if (data.shell_escape_enabled) {
        showToast('Shell-escape is ENABLED. Only compile documents you trust.', 'warning', 8000);
      }
    } else {
      dom.statusDot.className = 'status-dot error';
      dom.statusText.textContent = 'LaTeX not found – install a distribution';
      showToast('pdflatex not found. Run install first, or install a LaTeX distribution.', 'error', 8000);
    }
  } catch {
    dom.statusDot.className = 'status-dot error';
    dom.statusText.textContent = 'Backend unreachable';
    showToast('Cannot reach the backend server. Is it running?', 'error', 8000);
  }
}

// ─── Editor abstraction (Monaco, or a textarea fallback) ──────────────────────
function initEditor() {
  // Use Monaco when its loader is present; otherwise fall back to a textarea so
  // the app still works fully offline even if Monaco was not vendored.
  if (window.require && !window.__monacoMissing) {
    require(['vs/editor/editor.main'], () => {
      state.monacoEditor = monaco.editor.create(dom.monacoContainer, {
        value: state.pendingEditorContent || '',
        language: state.pendingEditorLanguage || 'latex',
        theme: 'vs-dark', automaticLayout: true, minimap: { enabled: false },
        wordWrap: 'on', fontSize: 14, padding: { top: 16 },
      });
      state.editorKind = 'monaco';
      state.monacoEditor.addCommand(monaco.KeyMod.CtrlCmd | monaco.KeyCode.KeyS, saveWithFeedback);
      state.pendingEditorContent = null;
    }, () => setupTextareaEditor());
  } else {
    setupTextareaEditor();
  }
}

function setupTextareaEditor() {
  const ta = document.createElement('textarea');
  ta.id = 'fallbackEditor';
  ta.className = 'fallback-editor';
  ta.spellcheck = false;
  ta.value = state.pendingEditorContent || '';
  ta.addEventListener('keydown', (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 's') {
      e.preventDefault();
      saveWithFeedback();
    }
  });
  dom.monacoContainer.appendChild(ta);
  state.editorKind = 'textarea';
  state.fallbackEditor = ta;
  state.pendingEditorContent = null;
}

function editorGetValue() {
  if (state.editorKind === 'monaco' && state.monacoEditor) return state.monacoEditor.getValue();
  if (state.editorKind === 'textarea' && state.fallbackEditor) return state.fallbackEditor.value;
  return '';
}

function editorSetValue(text, language) {
  if (state.editorKind === 'monaco' && state.monacoEditor) {
    monaco.editor.setModelLanguage(state.monacoEditor.getModel(), language || 'plaintext');
    state.monacoEditor.setValue(text);
  } else if (state.editorKind === 'textarea' && state.fallbackEditor) {
    state.fallbackEditor.value = text;
  } else {
    // Editor not ready yet – stash and apply once it initialises.
    state.pendingEditorContent = text;
    state.pendingEditorLanguage = language;
  }
}

async function saveWithFeedback() {
  const ok = await saveCurrentFile();
  if (ok) showToast('File saved', 'success', 1500);
}

// ─── File open / save ─────────────────────────────────────────────────────────
/** Save the current buffer. Returns true only if the write actually succeeded. */
async function saveCurrentFile() {
  if (!state.currentOpenFile || !state.sessionId || !state.currentIsText) return false;
  try {
    const res = await api(`/api/files/${state.sessionId}/${state.currentOpenFile}`, {
      method: 'PUT', body: editorGetValue(),
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return true;
  } catch (err) {
    showToast(`Failed to save ${state.currentOpenFile}: ${err.message}`, 'error');
    return false;
  }
}

async function openFile(filepath, ext) {
  if (!state.sessionId) return;
  await saveCurrentFile();  // persist the previous buffer first

  const isImage = ['.png', '.jpg', '.jpeg', '.svg', '.eps', '.gif', '.bmp'].includes(ext);
  try {
    const res = await api(`/api/files/${state.sessionId}/${filepath}`);
    if (!res.ok) throw new Error('Failed to load file');

    // Only commit the "current file" AFTER a successful load, so a failed open
    // can never leave us pointing the editor at the wrong file.
    state.currentOpenFile = filepath;
    dom.editorFileTitle.textContent = filepath;
    dom.editorEmptyState.classList.add('hidden');
    dom.fileTree.querySelectorAll('.file-item').forEach((el) => {
      el.classList.toggle('active-file', el.dataset.path === filepath);
    });

    if (isImage) {
      state.currentIsText = false;
      const blob = await res.blob();
      dom.monacoContainer.classList.add('hidden');
      dom.imageViewer.classList.remove('hidden');
      dom.imageViewerImg.src = URL.createObjectURL(blob);
    } else {
      state.currentIsText = true;
      const text = await res.text();
      dom.imageViewer.classList.add('hidden');
      dom.monacoContainer.classList.remove('hidden');
      let language = 'plaintext';
      if (['.tex', '.cls', '.sty', '.bib', '.bst'].includes(ext)) language = 'latex';
      else if (ext === '.json') language = 'json';
      else if (ext === '.js') language = 'javascript';
      else if (ext === '.css') language = 'css';
      editorSetValue(text, language);
    }
  } catch (err) {
    showToast(`Error opening file: ${err.message}`, 'error');
  }
}

// ─── File Tree ────────────────────────────────────────────────────────────────
function renderFileTree(files, detectedMain) {
  dom.fileTreeEmpty.classList.add('hidden');
  dom.fileTree.querySelectorAll('.file-item').forEach((el) => el.remove());

  const sorted = [...files].sort((a, b) => {
    if (a.path === detectedMain) return -1;
    if (b.path === detectedMain) return 1;
    if (a.ext === '.tex' && b.ext !== '.tex') return -1;
    if (b.ext === '.tex' && a.ext !== '.tex') return 1;
    return a.path.localeCompare(b.path);
  });

  sorted.forEach((file) => {
    const isMain = file.path === detectedMain;
    const item = document.createElement('div');
    item.className = `file-item${isMain ? ' is-main' : ''}`;
    item.setAttribute('role', 'button');
    item.setAttribute('tabindex', '0');
    item.setAttribute('aria-label', `Open ${file.path}`);
    item.dataset.path = file.path;
    item.title = file.path;
    // Build with escaped values so a crafted filename cannot inject markup.
    item.innerHTML =
      `<span class="file-icon">${fileIcon(file.ext)}</span>` +
      `<span class="file-name">${escapeHtml(file.path)}${isMain ? ' ✦' : ''}</span>` +
      `<span class="file-size">${formatSize(file.size)}</span>`;
    item.addEventListener('click', () => openFile(file.path, file.ext));
    item.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); openFile(file.path, file.ext); }
    });
    dom.fileTree.appendChild(item);
  });

  if (detectedMain && !state.currentOpenFile) {
    const ext = detectedMain.slice(detectedMain.lastIndexOf('.')).toLowerCase();
    openFile(detectedMain, ext);
  }

  const texFiles = files.filter((f) => f.ext === '.tex');
  if (texFiles.length > 0) {
    dom.mainFileSelector.classList.remove('hidden');
    dom.mainFileSelect.innerHTML = '';
    texFiles.forEach((f) => {
      const opt = document.createElement('option');
      opt.value = f.path;             // setting .value/.text avoids HTML injection
      opt.textContent = f.path;
      if (f.path === detectedMain) opt.selected = true;
      dom.mainFileSelect.appendChild(opt);
    });
  }

  dom.clearSessionBtn.classList.remove('hidden');
  dom.compileBtn.disabled = false;
  updateStatusBar();
}

// ─── Upload ───────────────────────────────────────────────────────────────────
async function uploadFiles(fileList) {
  if (state.isUploading || state.isCompiling) return;
  if (!fileList || fileList.length === 0) return;

  state.isUploading = true;
  dom.uploadProgress.classList.remove('hidden');
  dom.progressFill.style.width = '10%';
  dom.progressText.textContent = 'Uploading files…';
  dom.compileBtn.disabled = true;

  const formData = new FormData();
  for (const file of fileList) formData.append('files', file);

  try {
    dom.progressFill.style.width = '40%';
    const res = await api('/api/upload', { method: 'POST', body: formData });
    dom.progressFill.style.width = '80%';
    if (!res.ok) {
      const err = await res.json().catch(() => ({ detail: 'Upload failed' }));
      throw new Error(err.detail || `HTTP ${res.status}`);
    }
    const data = await res.json();
    dom.progressFill.style.width = '100%';
    dom.progressText.textContent = data.message;

    state.sessionId = data.session_id;
    state.files = data.files;
    state.detectedMain = data.detected_main;
    persistSession();

    setTimeout(() => { dom.uploadProgress.classList.add('hidden'); dom.progressFill.style.width = '0%'; }, 800);

    renderFileTree(data.files, data.detected_main);
    showToast(`${data.files.length} file(s) uploaded successfully.`, 'success');
    if (!data.detected_main) {
      showToast('No main .tex file detected. Pick one from the dropdown.', 'warning');
    }
  } catch (err) {
    dom.progressFill.style.width = '0%';
    dom.uploadProgress.classList.add('hidden');
    showToast(`Upload failed: ${err.message}`, 'error');
    dom.compileBtn.disabled = !state.sessionId;
  } finally {
    state.isUploading = false;
    updateStatusBar();
  }
}

// ─── Compile ──────────────────────────────────────────────────────────────────
async function compileProject() {
  if (!state.sessionId || state.isCompiling) return;

  // Everything is inside try/finally so isCompiling and the button always reset,
  // even if a DOM step throws before the network request.
  state.isCompiling = true;
  try {
    await saveCurrentFile();

    dom.compileBtn.disabled = true;
    dom.compileBtn.classList.add('compiling');
    dom.compileBtnText.textContent = 'Compiling…';
    dom.compileSpinner.classList.remove('hidden');
    dom.pdfEmptyState.classList.add('hidden');
    dom.pdfFrame.classList.add('hidden');
    dom.pdfCompilingState.classList.remove('hidden');
    dom.downloadBtn.classList.add('hidden');
    dom.openNewTabBtn.classList.add('hidden');

    state.compileStart = Date.now();
    const engine = dom.engineSelect.value;
    dom.compilingDesc.textContent = `Running ${engine}…`;
    state.compileTimer = setInterval(() => {
      dom.compileTimerEl.textContent = elapsedSince(state.compileStart);
    }, 100);
    clearLog();

    const formData = new FormData();
    formData.append('session_id', state.sessionId);
    formData.append('engine', engine);
    const mainFile = dom.mainFileSelect.value || state.detectedMain || '';
    if (mainFile) formData.append('main_file', mainFile);

    const res = await api('/api/compile', { method: 'POST', body: formData });
    const data = await res.json().catch(() => ({ success: false, summary: `HTTP ${res.status}` }));
    const duration = elapsedSince(state.compileStart);
    clearInterval(state.compileTimer);
    dom.compileTimerEl.textContent = duration;

    if (!res.ok) throw new Error(data.detail || data.summary || `HTTP ${res.status}`);

    state.parsedLog = data.log;
    state.rawLog = data.log?.raw || '';
    renderLog(data);

    if (data.success) {
      state.pdfUrl = `${API_BASE}/api/pdf/${state.sessionId}?t=${Date.now()}`;
      showPdf(state.pdfUrl);
      dom.lastCompileStatus.textContent = `✓ Compiled in ${duration}`;
      showToast(`Compilation successful in ${duration}!`, 'success');
    } else {
      dom.pdfCompilingState.classList.add('hidden');
      dom.pdfEmptyState.classList.remove('hidden');
      dom.lastCompileStatus.textContent = `✗ Failed (${duration})`;
      const errorCount = data.log?.errors?.length || 0;
      showToast(`Compilation failed with ${errorCount} error(s). Check the log panel.`, 'error');
    }
  } catch (err) {
    clearInterval(state.compileTimer);
    dom.pdfCompilingState.classList.add('hidden');
    dom.pdfEmptyState.classList.remove('hidden');
    dom.lastCompileStatus.textContent = '✗ Error';
    showToast(`Compilation error: ${err.message}`, 'error');
  } finally {
    state.isCompiling = false;
    dom.compileBtn.disabled = false;
    dom.compileBtn.classList.remove('compiling');
    dom.compileBtnText.textContent = 'Compile PDF';
    dom.compileSpinner.classList.add('hidden');
  }
}

function showPdf(url) {
  dom.pdfCompilingState.classList.add('hidden');
  dom.pdfEmptyState.classList.add('hidden');
  dom.pdfFrame.src = url;
  dom.pdfFrame.classList.remove('hidden');
  dom.downloadBtn.classList.remove('hidden');
  dom.openNewTabBtn.classList.remove('hidden');
}

// ─── Log Rendering ────────────────────────────────────────────────────────────
function clearLog() {
  dom.logContainer.querySelectorAll('.log-entry').forEach((el) => el.remove());
  dom.logEmpty.classList.remove('hidden');
  dom.logSummary.classList.add('hidden');
  dom.rawLogPre.textContent = '';
  dom.errorCount.textContent = '0';
  dom.warnCount.textContent = '0';
}

function renderLog(data) {
  clearLog();
  const log = data.log;
  if (!log) return;

  state.rawLog = log.raw || '';
  dom.rawLogPre.textContent = state.rawLog;

  const errors = log.errors || [], warnings = log.warnings || [], badboxes = log.badboxes || [];
  dom.errorCount.textContent = errors.length;
  dom.warnCount.textContent = warnings.length;

  dom.compileDuration.textContent = data.duration_seconds ? `${data.duration_seconds}s` : '';
  dom.logSummaryInner.textContent = data.summary || '';
  dom.logSummary.className = `log-summary ${data.success ? 'success' : 'error'}`;
  dom.logSummary.classList.remove('hidden');

  const allEntries = [
    ...errors.map((e) => ({ ...e, level: 'error' })),
    ...warnings.map((w) => ({ ...w, level: 'warning' })),
    ...badboxes.map((b) => ({ ...b, level: 'badbox' })),
  ];
  if (allEntries.length === 0) {
    dom.logEmpty.classList.remove('hidden');
    if (data.success) {
      dom.logEmpty.innerHTML = '<p style="color: var(--success-text);">✅ Compilation successful with no errors or warnings.</p>';
    }
    return;
  }
  dom.logEmpty.classList.add('hidden');
  allEntries.forEach((entry) => dom.logContainer.appendChild(createLogEntry(entry)));
  applyLogFilter(state.currentFilter);
}

function createLogEntry(entry) {
  const div = document.createElement('div');
  div.className = `log-entry ${entry.level}`;
  div.dataset.level = entry.level;
  const levelLabel = entry.level === 'badbox' ? 'Badbox'
    : entry.level.charAt(0).toUpperCase() + entry.level.slice(1);
  let fileLine = '';
  if (entry.file || entry.line) {
    fileLine = `<span class="log-file-line">${escapeHtml(entry.file || '')}${entry.line ? `:${entry.line}` : ''}</span>`;
  }
  let contextHtml = '';
  if (entry.context && entry.context.trim()) {
    contextHtml = `<pre class="log-context">${escapeHtml(entry.context)}</pre>`;
  }
  div.innerHTML =
    `<div class="log-entry-header"><span class="log-level-badge ${entry.level}">${levelLabel}</span>${fileLine}</div>` +
    `<div class="log-message">${escapeHtml(entry.message)}</div>${contextHtml}`;
  return div;
}

function applyLogFilter(filter) {
  state.currentFilter = filter;
  [dom.filterAll, dom.filterErrors, dom.filterWarnings].forEach((btn) => {
    btn.classList.toggle('active', btn.dataset.filter === filter);
  });
  dom.logContainer.querySelectorAll('.log-entry').forEach((entry) => {
    entry.style.display = (filter === 'all' || entry.dataset.level === filter) ? '' : 'none';
  });
}

// ─── Session persistence & clearing ───────────────────────────────────────────
function persistSession() {
  try {
    if (state.sessionId) localStorage.setItem(SESSION_STORAGE_KEY, state.sessionId);
  } catch { /* storage may be disabled – not fatal */ }
}

/** On load, try to re-attach to a previously-opened session so a browser
 *  refresh does not lose the user's project. */
async function restoreSession() {
  let stored = null;
  try { stored = localStorage.getItem(SESSION_STORAGE_KEY); } catch { /* ignore */ }
  if (!stored) return;
  try {
    const res = await api(`/api/files/${stored}`);
    if (!res.ok) throw new Error('gone');
    const data = await res.json();
    state.sessionId = stored;
    state.files = data.files;
    state.detectedMain = data.detected_main;
    renderFileTree(data.files, data.detected_main);
    showToast('Restored your previous session.', 'info', 2500);
  } catch {
    try { localStorage.removeItem(SESSION_STORAGE_KEY); } catch { /* ignore */ }
  }
}

async function clearSession() {
  if (!state.sessionId) return;
  try { await api(`/api/cleanup/${state.sessionId}`, { method: 'DELETE' }); } catch { /* ignore */ }
  try { localStorage.removeItem(SESSION_STORAGE_KEY); } catch { /* ignore */ }

  state.sessionId = null; state.files = []; state.detectedMain = null;
  state.pdfUrl = null; state.rawLog = ''; state.parsedLog = null;
  state.currentOpenFile = null; state.currentIsText = false;

  editorSetValue('', 'latex');
  dom.editorFileTitle.textContent = 'No file selected';
  dom.monacoContainer.classList.remove('hidden');
  dom.imageViewer.classList.add('hidden');
  dom.editorEmptyState.classList.remove('hidden');

  dom.fileTree.querySelectorAll('.file-item').forEach((el) => el.remove());
  dom.fileTreeEmpty.classList.remove('hidden');
  dom.mainFileSelector.classList.add('hidden');
  dom.clearSessionBtn.classList.add('hidden');
  dom.compileBtn.disabled = true;

  dom.pdfFrame.src = ''; dom.pdfFrame.classList.add('hidden');
  dom.pdfEmptyState.classList.remove('hidden');
  dom.pdfCompilingState.classList.add('hidden');
  dom.downloadBtn.classList.add('hidden');
  dom.openNewTabBtn.classList.add('hidden');

  clearLog();
  dom.logEmpty.innerHTML = '<p>No log yet. Compile your project to see output here.</p>';
  dom.lastCompileStatus.textContent = 'Not compiled';
  updateStatusBar();
  showToast('Project cleared. Ready for a new upload.', 'info');
}

// ─── Download / Copy ──────────────────────────────────────────────────────────
function downloadPdf() {
  if (!state.sessionId) return;
  // The backend honours ?download=1 by setting Content-Disposition: attachment.
  const a = document.createElement('a');
  a.href = `${API_BASE}/api/pdf/${state.sessionId}?download=1`;
  a.download = 'output.pdf';
  document.body.appendChild(a);
  a.click();
  a.remove();
}

async function copyLog() {
  if (!state.rawLog) { showToast('No log to copy.', 'warning'); return; }
  try {
    await navigator.clipboard.writeText(state.rawLog);
    showToast('Log copied to clipboard.', 'success', 2000);
  } catch {
    showToast('Could not copy to clipboard.', 'error');
  }
}

// ─── Drag and Drop ────────────────────────────────────────────────────────────
function setupDropZone() {
  const zone = dom.dropZone;
  ['dragenter', 'dragover', 'dragleave', 'drop'].forEach((evt) => {
    zone.addEventListener(evt, (e) => { e.preventDefault(); e.stopPropagation(); });
    document.body.addEventListener(evt, (e) => e.preventDefault());
  });
  zone.addEventListener('dragenter', () => zone.classList.add('drag-over'));
  zone.addEventListener('dragover', () => zone.classList.add('drag-over'));
  zone.addEventListener('dragleave', (e) => { if (!zone.contains(e.relatedTarget)) zone.classList.remove('drag-over'); });
  zone.addEventListener('drop', (e) => {
    zone.classList.remove('drag-over');
    const files = e.dataTransfer?.files;
    if (files && files.length > 0) uploadFiles(files);
  });
  zone.addEventListener('click', (e) => { if (e.target !== dom.fileInput) dom.fileInput.click(); });
  zone.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); dom.fileInput.click(); }
  });
  dom.fileInput.addEventListener('change', (e) => {
    const files = e.target.files;
    if (files && files.length > 0) { uploadFiles(files); dom.fileInput.value = ''; }
  });
}

// ─── Event Listeners ──────────────────────────────────────────────────────────
function setupEventListeners() {
  dom.compileBtn.addEventListener('click', compileProject);
  dom.clearSessionBtn.addEventListener('click', clearSession);
  dom.downloadBtn.addEventListener('click', downloadPdf);
  dom.copyLogBtn.addEventListener('click', copyLog);
  dom.openNewTabBtn.addEventListener('click', () => { if (state.pdfUrl) window.open(state.pdfUrl, '_blank'); });
  dom.toggleLogBtn.addEventListener('click', () => dom.logSection.classList.toggle('hidden'));
  [dom.filterAll, dom.filterErrors, dom.filterWarnings].forEach((btn) => {
    btn.addEventListener('click', () => applyLogFilter(btn.dataset.filter));
  });

  // Clean up the session on tab close. fetch(keepalive) is used because
  // navigator.sendBeacon can only issue POST, but /api/cleanup is DELETE.
  window.addEventListener('pagehide', () => {
    if (state.sessionId) {
      try {
        api(`/api/cleanup/${state.sessionId}`, { method: 'DELETE', keepalive: true });
        localStorage.removeItem(SESSION_STORAGE_KEY);
      } catch { /* best effort */ }
    }
  });
}

// ─── Initialize ───────────────────────────────────────────────────────────────
async function init() {
  initEditor();
  setupDropZone();
  setupEventListeners();
  dom.statusDot.classList.add('pulse');
  await checkLatexStatus();
  dom.statusDot.classList.remove('pulse');
  await restoreSession();
  updateStatusBar();
}

init();
