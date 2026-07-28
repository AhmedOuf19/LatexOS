/**
 * app.js – LaTeX Studio frontend logic. No framework and no build step: this
 * file is served verbatim and runs as-is.
 *
 * Responsibilities:
 *   - Drag-and-drop + click upload (multi-file and ZIP)
 *   - Session lifecycle (persisted in localStorage so a refresh keeps your work)
 *   - File tree (keyboard-accessible) + in-browser editor (Monaco, with a
 *     <textarea> fallback if Monaco was not vendored)
 *   - Compile trigger with a live timer, PDF preview + download
 *   - Structured, filterable log rendering
 *
 * Design decisions you would otherwise have to reverse-engineer
 * -------------------------------------------------------------
 * Token on every call. Every request goes through api(), which attaches the
 *   per-instance X-Studio-Token the backend baked into this page. A *wrong*
 *   token is rejected with 403, but a *missing* one is not – curl and the test
 *   suite have to keep working – so a raw fetch() to an /api path would not fail
 *   loudly. It would just quietly drop that call out of the second layer of
 *   anti-drive-by defence. Never bypass api().
 *
 * Escape everything the server sends. Filenames, log messages and error context
 *   all originate inside an uploaded archive. An archive entry named
 *   `<img src=x onerror=...>.tex` is stored XSS against the person who uploaded
 *   it, so every server-supplied string is passed through escapeHtml() before it
 *   is interpolated into innerHTML, or is written with textContent instead.
 *
 * The editor is an abstraction, not Monaco. Monaco is vendored locally and may
 *   be absent (it is large and optional) or fail to load; the app must stay
 *   fully usable offline, so a plain <textarea> sits behind the same
 *   editorGetValue()/editorSetValue() pair. state.editorKind says which is live,
 *   and no caller outside this section needs to know.
 *
 * Naming is a contract. dom.* keys and several state.* keys mirror element ids
 *   in index.html, and tests/check_frontend_ids.py scrapes this file and fails
 *   if an id named here is missing from the HTML. Renaming either side breaks
 *   that guard – and JSON field names (session_id, detected_main, pdf_url,
 *   has_errors, duration_seconds, …) are the backend's contract, not ours.
 *
 * Load order. index.html includes this script at the end of <body> with no
 *   `defer`, which is why the element lookups below can run at module scope and
 *   why init() can be called at the bottom of the file.
 */

'use strict';

// ─── Configuration ────────────────────────────────────────────────────────────

// Same origin – FastAPI serves both the UI and the API, so every path is relative.
const API_BASE = '';

// Random per-process token the backend substituted into index.html as it served
// the page. Empty if the page was obtained some other way; api() then sends no
// token header at all and the backend falls back to its loopback bind and its
// Origin check.
const STUDIO_TOKEN =
  document.querySelector('meta[name="studio-token"]')?.content || '';

// Namespaced because every app you run on localhost shares one localStorage
// origin – an unprefixed key like "sessionId" would be a genuine collision risk.
const SESSION_STORAGE_KEY = 'latexStudio.sessionId';

// ─── State ───────────────────────────────────────────────────────────────────

/**
 * The single mutable store for everything the UI needs to redraw itself.
 *
 * There is no reactive framework here: functions read `state`, write to it, and
 * then push the change into the DOM themselves. Keeping it all in one object is
 * what lets clearSession() return the app to first-run condition in one place
 * instead of hunting for stray variables.
 */
const state = {
  sessionId: null,           // server-side workspace id; mirrored to localStorage
  files: [],                 // file records exactly as the API returned them
  detectedMain: null,        // backend's guess at the main .tex; may be null
  isUploading: false,
  isCompiling: false,
  compileTimer: null,        // setInterval handle for the live elapsed readout
  compileStart: null,
  rawLog: '',                // unparsed .log text, kept so "copy log" needs no refetch
  currentFilter: 'all',
  parsedLog: null,
  pdfUrl: null,
  currentOpenFile: null,
  currentIsText: false,      // true ONLY after a text file loaded successfully;
                             // this is what gates saving (see saveCurrentFile)
  monacoEditor: null,
  editorKind: null,          // 'monaco' | 'textarea' | null (null = not ready yet)
  // Monaco initialises asynchronously, so a file can be opened before any editor
  // exists. These hold that content until initEditor/setupTextareaEditor apply it.
  pendingEditorContent: null,
  pendingEditorLanguage: null,
  // state.fallbackEditor is attached later by setupTextareaEditor – only the
  // textarea backend has a DOM node worth keeping a handle to.
};

// ─── DOM References ───────────────────────────────────────────────────────────

/** Return the element with `id`. Shorthand – this app needs no other lookup. */
const $ = (id) => document.getElementById(id);

/**
 * Every element the app will ever touch, resolved ONCE at load time.
 *
 * Caching them here is deliberate: it gives tests/check_frontend_ids.py a single
 * block it can scrape statically and cross-check against index.html. That guard
 * exists because a missing #openNewTabBtn once threw during init() and left the
 * entire page dead rather than just breaking one button.
 *
 * Keys mirror the ids they load. The one exception is `compileTimerEl`, suffixed
 * to avoid reading like state.compileTimer, which is an interval handle.
 */
const dom = {
  dropZone: $('dropZone'), fileInput: $('fileInput'),
  uploadProgress: $('uploadProgress'), progressFill: $('progressFill'), progressText: $('progressText'),
  fileTree: $('fileTree'), fileTreeEmpty: $('fileTreeEmpty'),
  mainFileSelector: $('mainFileSelector'), mainFileSelect: $('mainFileSelect'),
  clearSessionBtn: $('clearSessionBtn'),
  compileBtn: $('compileBtn'), compileBtnText: $('compileBtnText'), compileSpinner: $('compileSpinner'),
  shellEscapeCheck: $('shellEscapeCheck'),
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

/**
 * Fetch a same-origin API path with the per-instance security token attached.
 *
 * Use this for EVERY /api call, never bare fetch(). The token proves the request
 * came from the page this backend itself served; together with the backend's
 * Origin check it is what stops a random website open in another tab from
 * driving your local compiler. `options` is passed straight through, so callers
 * can still set method, body or keepalive; only the headers are augmented.
 */
function api(path, options = {}) {
  const headers = new Headers(options.headers || {});
  if (STUDIO_TOKEN) headers.set('X-Studio-Token', STUDIO_TOKEN);
  return fetch(`${API_BASE}${path}`, { ...options, headers });
}

// ─── Small utilities ──────────────────────────────────────────────────────────

/** Return `bytes` as a short human-readable size for the file tree column. */
function formatSize(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

/**
 * Return an emoji standing in for a file type, defaulting to a generic document.
 *
 * Emoji rather than an icon font or SVG sprite: the app ships no external assets
 * and must render identically with no network at all.
 */
function fileIcon(ext) {
  const icons = {
    '.tex': '📄', '.bib': '📚', '.cls': '🎨', '.sty': '🎨',
    '.png': '🖼️', '.jpg': '🖼️', '.jpeg': '🖼️', '.svg': '🖼️', '.eps': '🖼️',
    '.pdf': '📕', '.zip': '📦', '.ttf': '🔤', '.otf': '🔤',
    '.csv': '📊', '.txt': '📝', '.bst': '📋', '.log': '🪵', '.aux': '⚙️',
  };
  return icons[ext] || '📄';
}

/**
 * Return the time since `start` (a Date.now() stamp) as e.g. "3.4s".
 *
 * One decimal is the finest useful resolution: the compile timer ticks every
 * 100 ms, and more digits would just make the readout flicker.
 */
function elapsedSince(start) {
  return ((Date.now() - start) / 1000).toFixed(1) + 's';
}

/**
 * Escape every character that could break out of HTML text or an attribute.
 *
 * Applied to EVERY server-supplied string that is interpolated into innerHTML –
 * filenames, log messages, error context. All of those originate in an uploaded
 * archive, so an entry named `<img src=x onerror=...>.tex` would be stored XSS
 * against the person who uploaded it. Quotes are escaped as well because some of
 * these values land inside attributes. Null and undefined become '' rather than
 * the strings "null"/"undefined".
 */
function escapeHtml(str) {
  return String(str ?? '')
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

// ─── Toast Notifications ──────────────────────────────────────────────────────

/**
 * Show a transient message in the bottom-right stack and remove it afterwards.
 *
 * This is the app's only channel for reporting anything that is not a compile
 * error, so `duration` is left to the caller: a "file saved" confirmation wants
 * to be gone in a second, a shell-escape security warning wants to be readable.
 */
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
    setTimeout(() => toast.remove(), 300);  // ≥ the 0.25s .toast.hiding animation
  }, duration);
}

// ─── Status Bar ───────────────────────────────────────────────────────────────

/** Refresh the footer's session id and file count from `state`. */
function updateStatusBar() {
  // Session ids are UUIDs; the first 8 characters are plenty to tell two apart
  // when supporting someone, and keep the status bar from wrapping.
  dom.sessionInfo.textContent = state.sessionId
    ? `Session: ${state.sessionId.slice(0, 8)}…` : 'No active session';
  dom.fileCount.textContent = `${state.files.length} file${state.files.length !== 1 ? 's' : ''}`;
}

// ─── LaTeX status check ───────────────────────────────────────────────────────

/**
 * Probe the backend once at startup and report LaTeX readiness in the status bar.
 *
 * The three outcomes are kept distinct on purpose because the remedy differs:
 * "LaTeX ready", "LaTeX not found" (install a distribution) and "backend
 * unreachable" (the server is not running). Telling a non-technical user to
 * install LaTeX when the real problem is a dead server sends them a long way in
 * the wrong direction.
 */
async function checkLatexStatus() {
  try {
    const res = await api('/api/status');
    if (!res.ok) throw new Error('Backend unreachable');
    const data = await res.json();
    if (data.latex_available) {
      dom.statusDot.className = 'status-dot ok';
      const availableTools = Object.entries(data.tools).filter(([, v]) => v.available).map(([k]) => k).join(', ');
      dom.statusText.textContent = `LaTeX ready (${availableTools})`;
      // Server-wide shell-escape (an env var, not the per-compile checkbox) means
      // ANY document compiled here can run programs. Say so, unprompted.
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

/**
 * Bring up the code editor, preferring Monaco and degrading to a <textarea>.
 *
 * Monaco is vendored into static/vendor and is optional – it is large, and some
 * installs simply do not have it. index.html sets window.__monacoMissing from
 * the loader script's onerror handler, because without that flag a missing
 * bundle leaves window.require undefined-or-broken and the failure surfaces much
 * later as an unexplained blank panel. Falling back to a textarea keeps the app
 * fully usable (open, edit, save, compile) with zero network and zero vendoring.
 *
 * Loading is asynchronous, so anything opened in the meantime is parked in
 * state.pendingEditorContent and picked up here.
 */
function initEditor() {
  if (window.require && !window.__monacoMissing) {
    // The AMD loader's second callback is its error callback: if the bundle is
    // present but unusable, fall back rather than leaving the panel empty.
    require(['vs/editor/editor.main'], () => {
      state.monacoEditor = monaco.editor.create(dom.monacoContainer, {
        value: state.pendingEditorContent || '',
        language: state.pendingEditorLanguage || 'latex',
        // automaticLayout: the editor lives in a flex panel that the browser
        // resizes; without it Monaco keeps the size it had at creation time.
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

/**
 * Build the no-Monaco editor: a plain textarea wired to the same save shortcut.
 *
 * Spellcheck is off because LaTeX source is mostly commands, and a document
 * underlined end to end in red is unreadable. Ctrl/Cmd+S has to be intercepted
 * by hand here (Monaco does it for us) – otherwise the browser offers to save
 * the whole page to disk, which is not what anyone pressing it in an editor
 * wants.
 */
function setupTextareaEditor() {
  const textarea = document.createElement('textarea');
  textarea.id = 'fallbackEditor';
  textarea.className = 'fallback-editor';
  textarea.spellcheck = false;
  textarea.value = state.pendingEditorContent || '';
  textarea.addEventListener('keydown', (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 's') {
      e.preventDefault();
      saveWithFeedback();
    }
  });
  dom.monacoContainer.appendChild(textarea);
  state.editorKind = 'textarea';
  state.fallbackEditor = textarea;
  state.pendingEditorContent = null;
}

/** Return the current buffer text, or '' if no editor has initialised yet. */
function editorGetValue() {
  if (state.editorKind === 'monaco' && state.monacoEditor) return state.monacoEditor.getValue();
  if (state.editorKind === 'textarea' && state.fallbackEditor) return state.fallbackEditor.value;
  return '';
}

/**
 * Replace the buffer with `text`, highlighted as `language` where supported.
 *
 * Safe to call before any editor exists – the content is stashed and applied
 * when one does. `language` is a Monaco mode id and is ignored by the textarea.
 */
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

/**
 * Save on explicit user request (Ctrl/Cmd+S) and confirm only if it worked.
 *
 * Deliberately silent on failure: saveCurrentFile() has already raised an error
 * toast, and two toasts for one keystroke reads like a bug.
 */
async function saveWithFeedback() {
  const isSaved = await saveCurrentFile();
  if (isSaved) showToast('File saved', 'success', 1500);
}

// ─── File open / save ─────────────────────────────────────────────────────────

/**
 * Write the editor buffer back to the file it came from.
 *
 * Gated on state.currentIsText rather than on which viewer is visible: a failed
 * image open once left the image panel hidden while state still pointed at the
 * binary file, and the next implicit save wrote editor text straight over it.
 * currentIsText is set only after a file's content has actually been read as
 * text, so it can never be true for a file that failed to open.
 *
 * Called implicitly before opening another file and before compiling, so it must
 * be cheap and must never throw. Returns true only if the write really
 * succeeded – callers carry on either way, but saveWithFeedback uses it to
 * decide whether to tell the user anything.
 */
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

/**
 * Load a file from the session workspace into the editor or the image viewer.
 *
 * `ext` (lower-case, with the dot) decides both which viewer is used and, for
 * text, the syntax mode. Class/style/bibliography files are highlighted as latex
 * because Monaco has no mode of their own and latex is far closer than plaintext.
 *
 * The editor and the image viewer are two panels toggled by the `hidden` class,
 * and exactly one of them must be showing when this returns.
 */
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

/**
 * Rebuild the file tree, the main-file dropdown and the session-dependent
 * buttons from a fresh listing.
 *
 * The sort order is a usability decision, not an implementation detail: the
 * detected main file first, then the other .tex files, then everything else
 * alphabetically. Someone who does not know LaTeX should see the file they are
 * about to compile at the top without scrolling or guessing.
 *
 * Called after upload and after session restore, so it must be idempotent.
 */
function renderFileTree(files, detectedMain) {
  dom.fileTreeEmpty.classList.add('hidden');
  // Remove only the rows: the empty-state node is a sibling we reveal again in
  // clearSession(), so it must survive a re-render.
  dom.fileTree.querySelectorAll('.file-item').forEach((el) => el.remove());

  const sortedFiles = [...files].sort((a, b) => {
    if (a.path === detectedMain) return -1;
    if (b.path === detectedMain) return 1;
    if (a.ext === '.tex' && b.ext !== '.tex') return -1;
    if (b.ext === '.tex' && a.ext !== '.tex') return 1;
    return a.path.localeCompare(b.path);
  });

  sortedFiles.forEach((file) => {
    const isMain = file.path === detectedMain;
    const item = document.createElement('div');
    item.className = `file-item${isMain ? ' is-main' : ''}`;
    // A row is a <div> because it lays out as a three-column grid, which a
    // <button> fights. role/tabindex/keydown hand back the button semantics a
    // keyboard or screen-reader user needs.
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

  // Open the main file for the user, but only when nothing is open yet – a
  // re-render must never yank them away from the file they are editing.
  if (detectedMain && !state.currentOpenFile) {
    const ext = detectedMain.slice(detectedMain.lastIndexOf('.')).toLowerCase();
    openFile(detectedMain, ext);
  }

  const texFiles = files.filter((f) => f.ext === '.tex');
  if (texFiles.length > 0) {
    dom.mainFileSelector.classList.remove('hidden');
    dom.mainFileSelect.innerHTML = '';
    texFiles.forEach((texFile) => {
      const opt = document.createElement('option');
      opt.value = texFile.path;       // setting .value/.text avoids HTML injection
      opt.textContent = texFile.path;
      if (texFile.path === detectedMain) opt.selected = true;
      dom.mainFileSelect.appendChild(opt);
    });
  }

  dom.clearSessionBtn.classList.remove('hidden');
  dom.compileBtn.disabled = false;
  updateStatusBar();
}

// ─── Upload ───────────────────────────────────────────────────────────────────

/**
 * Upload a FileList – multi-select, drag-and-drop, or a single ZIP – and adopt
 * the session the backend creates for it.
 *
 * The progress bar is advanced in fixed steps (10/40/80/100%) rather than
 * measured: fetch() reports no upload progress without dropping back to
 * XMLHttpRequest, and for a non-technical user a bar that moves beats a bar that
 * is accurate. Every upload starts a *new* server-side session, which is why
 * this refuses to run while another upload or a compile is in flight – a second
 * drop mid-flight would abandon the session the first one is still filling.
 */
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

    // Hold the finished bar briefly so the jump to 100% is actually seen.
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
    // A failed upload leaves any earlier session untouched, so re-enable compile
    // if one still exists instead of stranding the user with a dead button.
    dom.compileBtn.disabled = !state.sessionId;
  } finally {
    state.isUploading = false;
    updateStatusBar();
  }
}

// ─── Compile ──────────────────────────────────────────────────────────────────

/**
 * Compile the current session and render the resulting PDF and log.
 *
 * The open buffer is saved first. Without that, the user compiles the file as it
 * sits on disk rather than as it looks on their screen, and their fix appears to
 * have done nothing.
 *
 * `success` from the backend means "a PDF came out", NOT "no errors": LaTeX
 * recovers from many errors and still writes output. A run that produced a PDF
 * *and* errors is therefore announced as a warning, because silently calling it
 * a success is how people ship a document with half a chapter missing.
 */
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
    // Per-compile shell-escape opt-in (off unless the user ticked the box).
    formData.append('shell_escape', dom.shellEscapeCheck?.checked ? 'true' : 'false');

    const res = await api('/api/compile', { method: 'POST', body: formData });
    // The API answers with JSON even when it fails, but a crash or a proxy can
    // still return HTML; synthesise a result so the error path stays uniform.
    const data = await res.json().catch(() => ({ success: false, summary: `HTTP ${res.status}` }));
    const duration = elapsedSince(state.compileStart);
    clearInterval(state.compileTimer);
    dom.compileTimerEl.textContent = duration;

    if (!res.ok) throw new Error(data.detail || data.summary || `HTTP ${res.status}`);

    state.parsedLog = data.log;
    state.rawLog = data.log?.raw || '';
    renderLog(data);

    if (data.success) {
      // ?t= is a cache-buster: the PDF URL is otherwise identical between runs,
      // and the iframe would happily keep showing the previous compile.
      state.pdfUrl = `${API_BASE}/api/pdf/${state.sessionId}?t=${Date.now()}`;
      showPdf(state.pdfUrl);
      const errorCount = data.log?.errors?.length || 0;
      if (errorCount > 0) {
        // A PDF was produced, but LaTeX reported (recoverable) errors — be
        // honest that the output may be incomplete rather than "successful".
        dom.lastCompileStatus.textContent = `⚠ Compiled with ${errorCount} error(s) (${duration})`;
        showToast(`Compiled with ${errorCount} error(s) — the PDF may be incomplete. Check the log.`, 'warning', 6000);
      } else {
        dom.lastCompileStatus.textContent = `✓ Compiled in ${duration}`;
        showToast(`Compilation successful in ${duration}!`, 'success');
      }
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

/**
 * Show `url` in the preview pane and reveal the download / new-tab actions.
 *
 * Those two buttons stay hidden until there is something to act on – offering a
 * download that 404s is worse than not offering one.
 */
function showPdf(url) {
  dom.pdfCompilingState.classList.add('hidden');
  dom.pdfEmptyState.classList.add('hidden');
  dom.pdfFrame.src = url;
  dom.pdfFrame.classList.remove('hidden');
  dom.downloadBtn.classList.remove('hidden');
  dom.openNewTabBtn.classList.remove('hidden');
}

// ─── Log Rendering ────────────────────────────────────────────────────────────

/**
 * Return the log panel to its empty state.
 *
 * Runs at the start of every compile as well as from renderLog(), so a run that
 * dies mid-way can never leave the previous run's errors on screen next to the
 * new result.
 */
function clearLog() {
  dom.logContainer.querySelectorAll('.log-entry').forEach((el) => el.remove());
  dom.logEmpty.classList.remove('hidden');
  dom.logSummary.classList.add('hidden');
  dom.rawLogPre.textContent = '';
  dom.errorCount.textContent = '0';
  dom.warnCount.textContent = '0';
}

/**
 * Render a compile response into the log panel: summary, counts, entries.
 *
 * Takes the whole response rather than just `data.log` because the header line
 * needs the run-level fields (summary, duration_seconds, success) too.
 */
function renderLog(data) {
  clearLog();
  const log = data.log;
  if (!log) return;

  state.rawLog = log.raw || '';
  dom.rawLogPre.textContent = state.rawLog;

  const errors = log.errors || [], warnings = log.warnings || [], badboxes = log.badboxes || [];
  // Badboxes are deliberately excluded from the badges: they are typographic
  // nits (an over-long line), and counting them next to real errors would make
  // a perfectly fine document look broken.
  dom.errorCount.textContent = errors.length;
  dom.warnCount.textContent = warnings.length;

  dom.compileDuration.textContent = data.duration_seconds ? `${data.duration_seconds}s` : '';
  dom.logSummaryInner.textContent = data.summary || '';
  dom.logSummary.className = `log-summary ${data.success ? 'success' : 'error'}`;
  dom.logSummary.classList.remove('hidden');

  // Flatten the three server-side buckets into one list tagged with `level`, so
  // rendering and filtering have a single code path. Concatenation order is also
  // the display order: worst first, rather than by line number.
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

/**
 * Build one log row: level badge, optional file:line, message and context.
 *
 * `dataset.level` is what applyLogFilter() reads, so it must stay in sync with
 * the data-filter values on the filter buttons in index.html.
 *
 * Every field here comes from the user's own .log file, so all of it is escaped:
 * a package can emit arbitrary text into a warning, filenames included.
 */
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

/**
 * Show only the log rows matching `filter` ('all' | 'error' | 'warning').
 *
 * Rows are hidden with inline display rather than removed, so switching filters
 * costs nothing and never has to re-render. Note that badbox rows match no
 * filter but 'all' – that is intentional, see renderLog().
 */
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

/**
 * Remember the current session id so a refresh can pick it back up.
 *
 * Storage failures are swallowed: private-browsing mode and disabled storage
 * both throw here, and losing session *persistence* must not cost the user the
 * session they already have open in this tab.
 */
function persistSession() {
  try {
    if (state.sessionId) localStorage.setItem(SESSION_STORAGE_KEY, state.sessionId);
  } catch { /* storage may be disabled – not fatal */ }
}

/**
 * On load, try to re-attach to a previously-opened session so a browser refresh
 * does not lose the user's project.
 *
 * The stored id is a claim, not a fact: the server may have restarted or garbage
 * -collected the workspace since. A listing request is the cheapest way to find
 * out, and a stale id is dropped from storage silently – the user did nothing
 * wrong and there is nothing for them to act on.
 */
async function restoreSession() {
  let storedSessionId = null;
  try { storedSessionId = localStorage.getItem(SESSION_STORAGE_KEY); } catch { /* ignore */ }
  if (!storedSessionId) return;
  try {
    const res = await api(`/api/files/${storedSessionId}`);
    if (!res.ok) throw new Error('gone');
    const data = await res.json();
    state.sessionId = storedSessionId;
    state.files = data.files;
    state.detectedMain = data.detected_main;
    renderFileTree(data.files, data.detected_main);
    showToast('Restored your previous session.', 'info', 2500);
  } catch {
    try { localStorage.removeItem(SESSION_STORAGE_KEY); } catch { /* ignore */ }
  }
}

/**
 * Delete the server-side workspace and reset every panel to first-run state.
 *
 * The network call is best-effort: the user asked to start over, so the UI is
 * cleared even if the DELETE fails. Nothing leaks – the backend's session
 * garbage collector reaps abandoned workspaces on its own.
 *
 * This is the single place that knows the full set of things a session touches;
 * if you add session-dependent UI, reset it here too.
 */
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

/**
 * Save the compiled PDF to disk.
 *
 * A synthetic anchor rather than window.open: it keeps the PDF out of a new tab
 * (where the browser's own viewer would just render it again) and gives the
 * saved file a sensible name instead of the session id.
 */
function downloadPdf() {
  if (!state.sessionId) return;
  // The backend honours ?download=1 by setting Content-Disposition: attachment.
  const link = document.createElement('a');
  link.href = `${API_BASE}/api/pdf/${state.sessionId}?download=1`;
  link.download = 'output.pdf';
  document.body.appendChild(link);
  link.click();
  link.remove();
}

/**
 * Copy the raw .log to the clipboard – the fastest way to hand a failure to
 * someone who can read TeX.
 *
 * The clipboard API rejects in insecure contexts and when permission is denied,
 * so the failure is reported rather than assumed impossible.
 */
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

/**
 * Wire the drop zone: drag-and-drop, click-to-browse and keyboard activation.
 *
 * The body-level preventDefault on all four drag events is the important part.
 * A browser's default action for a dropped file is to NAVIGATE to it, so a drop
 * that lands a few pixels outside the zone would replace the app with a view of
 * the dropped file and take the whole session with it.
 */
function setupDropZone() {
  const zone = dom.dropZone;
  ['dragenter', 'dragover', 'dragleave', 'drop'].forEach((eventName) => {
    zone.addEventListener(eventName, (e) => { e.preventDefault(); e.stopPropagation(); });
    document.body.addEventListener(eventName, (e) => e.preventDefault());
  });
  zone.addEventListener('dragenter', () => zone.classList.add('drag-over'));
  zone.addEventListener('dragover', () => zone.classList.add('drag-over'));
  // dragleave also fires when the pointer crosses into a child element; without
  // the contains() test the highlight flickers as you move across the zone.
  zone.addEventListener('dragleave', (e) => { if (!zone.contains(e.relatedTarget)) zone.classList.remove('drag-over'); });
  zone.addEventListener('drop', (e) => {
    zone.classList.remove('drag-over');
    const files = e.dataTransfer?.files;
    if (files && files.length > 0) uploadFiles(files);
  });
  // The hidden <input> lives inside the zone, so its own click bubbles back
  // here; without this guard the two would trigger each other endlessly.
  zone.addEventListener('click', (e) => { if (e.target !== dom.fileInput) dom.fileInput.click(); });
  zone.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); dom.fileInput.click(); }
  });
  dom.fileInput.addEventListener('change', (e) => {
    const files = e.target.files;
    // Clearing the value matters: re-picking the SAME file fires no change event
    // otherwise, so a second attempt after a failed upload would appear dead.
    if (files && files.length > 0) { uploadFiles(files); dom.fileInput.value = ''; }
  });
}

// ─── Event Listeners ──────────────────────────────────────────────────────────

/** Wire every button, filter and lifecycle hook that is not the drop zone. */
function setupEventListeners() {
  dom.compileBtn.addEventListener('click', compileProject);
  dom.clearSessionBtn.addEventListener('click', clearSession);
  dom.downloadBtn.addEventListener('click', downloadPdf);
  dom.copyLogBtn.addEventListener('click', copyLog);
  dom.openNewTabBtn.addEventListener('click', () => { if (state.pdfUrl) window.open(state.pdfUrl, '_blank'); });
  dom.toggleLogBtn.addEventListener('click', () => dom.logSection.classList.toggle('hidden'));

  // Remember the shell-escape choice between visits. It is off unless storage
  // explicitly says '1', so a corrupt or missing value can only fail safe.
  if (dom.shellEscapeCheck) {
    try {
      dom.shellEscapeCheck.checked = localStorage.getItem('latexStudio.shellEscape') === '1';
    } catch { /* storage may be disabled */ }
    dom.shellEscapeCheck.addEventListener('change', () => {
      const isShellEscapeOn = dom.shellEscapeCheck.checked;
      try { localStorage.setItem('latexStudio.shellEscape', isShellEscapeOn ? '1' : '0'); } catch { /* ignore */ }
      // Warn on every switch-on, not just the first: this setting lets a
      // document run programs, and it persists across visits.
      if (isShellEscapeOn) {
        showToast('Shell-escape enabled. Only compile documents you trust — they can run programs on your computer.', 'warning', 7000);
      }
    });
  }
  [dom.filterAll, dom.filterErrors, dom.filterWarnings].forEach((btn) => {
    btn.addEventListener('click', () => applyLogFilter(btn.dataset.filter));
  });

  // Clean up the session on tab close, so a workspace full of someone's
  // document does not sit on disk waiting for the garbage collector.
  //
  // Two deliberate choices here:
  //   * pagehide, not unload – unload is unreliable and is ignored outright on
  //     mobile, while pagehide fires on every real teardown.
  //   * fetch with keepalive, not navigator.sendBeacon – beacon can only issue
  //     POST, and /api/cleanup is a DELETE. keepalive is what lets the request
  //     outlive the document that started it.
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

/**
 * Wire the app up and bring it to a usable state.
 *
 * Order matters: everything synchronous happens before the first `await`, so the
 * page is fully interactive while the status probe is still in flight. Session
 * restore comes last because it may auto-open a file, and the editor and its
 * listeners have to exist by then.
 */
async function init() {
  initEditor();
  setupDropZone();
  setupEventListeners();
  dom.statusDot.classList.add('pulse');  // "checking…" – removed once we know
  await checkLatexStatus();
  dom.statusDot.classList.remove('pulse');
  await restoreSession();
  updateStatusBar();
}

// Safe to run at module scope: index.html loads this script at the end of
// <body>, so every element in `dom` already exists.
init();
