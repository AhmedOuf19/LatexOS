/**
 * app.js – LaTeX Studio Frontend Logic
 *
 * Handles:
 *   - Drag-and-drop + click file upload (multi-file and ZIP)
 *   - Session management (session_id lifecycle)
 *   - File tree rendering with icons
 *   - LaTeX compilation trigger with live timer
 *   - PDF preview + download
 *   - Structured log rendering with filtering
 *   - Toast notifications
 *   - LaTeX status check on load
 */

'use strict';

// ─── Configuration ────────────────────────────────────────────────────────────
const API_BASE = '';  // Same origin (FastAPI serves both)

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
  monacoEditor: null,
};

// ─── DOM References ───────────────────────────────────────────────────────────
const $ = (id) => document.getElementById(id);

const dom = {
  dropZone:          $('dropZone'),
  fileInput:         $('fileInput'),
  uploadProgress:    $('uploadProgress'),
  progressFill:      $('progressFill'),
  progressText:      $('progressText'),
  fileTree:          $('fileTree'),
  fileTreeEmpty:     $('fileTreeEmpty'),
  mainFileSelector:  $('mainFileSelector'),
  mainFileSelect:    $('mainFileSelect'),
  clearSessionBtn:   $('clearSessionBtn'),
  compileBtn:        $('compileBtn'),
  compileBtnText:    $('compileBtnText'),
  compileSpinner:    $('compileSpinner'),
  engineSelect:      $('engineSelect'),
  statusDot:         $('statusDot'),
  statusText:        $('statusText'),
  
  // Editor & Image Viewer
  editorPanel:       $('editorPanel'),
  editorFileTitle:   $('editorFileTitle'),
  monacoContainer:   $('monacoContainer'),
  imageViewer:       $('imageViewer'),
  imageViewerImg:    $('imageViewerImg'),
  editorEmptyState:  $('editorEmptyState'),
  
  // PDF Viewer
  pdfViewport:       $('pdfViewport'),
  pdfEmptyState:     $('pdfEmptyState'),
  pdfCompilingState: $('pdfCompilingState'),
  pdfFrame:          $('pdfFrame'),
  compilingDesc:     $('compilingDesc'),
  compileTimerEl:    $('compileTimer'),
  downloadBtn:       $('downloadBtn'),
  openNewTabBtn:     $('openNewTabBtn'),
  
  // Log Section
  logSection:        $('logSection'),
  toggleLogBtn:      $('toggleLogBtn'),
  logContainer:      $('logContainer'),
  logEmpty:          $('logEmpty'),
  logSummary:        $('logSummary'),
  logSummaryInner:   $('logSummaryInner'),
  compileDuration:   $('compileDuration'),
  errorCount:        $('errorCount'),
  warnCount:         $('warnCount'),
  copyLogBtn:        $('copyLogBtn'),
  rawLogDetails:     $('rawLogDetails'),
  rawLogPre:         $('rawLogPre'),
  
  // Global & Toast
  toastContainer:    $('toastContainer'),
  sessionInfo:       $('sessionInfo'),
  fileCount:         $('fileCount'),
  lastCompileStatus: $('lastCompileStatus'),
  filterAll:         $('filterAll'),
  filterErrors:      $('filterErrors'),
  filterWarnings:    $('filterWarnings'),
};

// ─── Utility: Format file size ────────────────────────────────────────────────
function formatSize(bytes) {
  if (bytes < 1024)        return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

// ─── Utility: File type icon ──────────────────────────────────────────────────
function fileIcon(ext) {
  const icons = {
    '.tex': '📄', '.bib': '📚', '.cls': '🎨', '.sty': '🎨',
    '.png': '🖼️', '.jpg': '🖼️', '.jpeg': '🖼️', '.svg': '🖼️',
    '.eps': '🖼️', '.pdf': '📕', '.zip': '📦',
    '.ttf': '🔤', '.otf': '🔤',
    '.csv': '📊', '.txt': '📝', '.bst': '📋',
    '.log': '🪵', '.aux': '⚙️',
  };
  return icons[ext] || '📄';
}

// ─── Utility: Elapsed time ────────────────────────────────────────────────────
function elapsedSince(start) {
  return ((Date.now() - start) / 1000).toFixed(1) + 's';
}

// ─── Toast Notifications ──────────────────────────────────────────────────────
function showToast(message, type = 'info', duration = 4000) {
  const icons = { success: '✅', error: '❌', warning: '⚠️', info: 'ℹ️' };
  const toast = document.createElement('div');
  toast.className = `toast ${type}`;
  toast.innerHTML = `
    <span class="toast-icon">${icons[type] || 'ℹ️'}</span>
    <span class="toast-msg">${message}</span>
  `;
  dom.toastContainer.appendChild(toast);

  setTimeout(() => {
    toast.classList.add('hiding');
    setTimeout(() => toast.remove(), 300);
  }, duration);
}

// ─── Status Bar Update ────────────────────────────────────────────────────────
function updateStatusBar() {
  dom.sessionInfo.textContent = state.sessionId
    ? `Session: ${state.sessionId.slice(0, 8)}…`
    : 'No active session';
  dom.fileCount.textContent = `${state.files.length} file${state.files.length !== 1 ? 's' : ''}`;
}

// ─── LaTeX Status Check ───────────────────────────────────────────────────────
async function checkLatexStatus() {
  try {
    const res = await fetch(`${API_BASE}/api/status`);
    if (!res.ok) throw new Error('Backend unreachable');
    const data = await res.json();

    if (data.latex_available) {
      dom.statusDot.className = 'status-dot ok';
      const availableTools = Object.entries(data.tools)
        .filter(([, v]) => v.available)
        .map(([k]) => k)
        .join(', ');
      dom.statusText.textContent = `LaTeX ready (${availableTools})`;
    } else {
      dom.statusDot.className = 'status-dot error';
      dom.statusText.textContent = 'LaTeX not found – install MiKTeX or TeX Live';
      showToast('LaTeX (pdflatex) not found. Please install MiKTeX or TeX Live.', 'error', 8000);
    }
  } catch {
    dom.statusDot.className = 'status-dot error';
    dom.statusText.textContent = 'Backend unreachable';
    showToast('Cannot reach the backend server. Is it running?', 'error', 8000);
  }
}

// ─── Monaco Editor & File Handling ──────────────────────────────────────────────
function initMonaco() {
  if (window.require) {
    require(['vs/editor/editor.main'], function() {
      state.monacoEditor = monaco.editor.create(dom.monacoContainer, {
        value: state.pendingEditorContent || '',
        language: state.pendingEditorLanguage || 'latex',
        theme: 'vs-dark',
        automaticLayout: true,
        minimap: { enabled: false },
        wordWrap: 'on',
        fontSize: 14,
        padding: { top: 16 }
      });
      
      // Save shortcut (Ctrl+S / Cmd+S)
      state.monacoEditor.addCommand(monaco.KeyMod.CtrlCmd | monaco.KeyCode.KeyS, async () => {
        await saveCurrentFile();
        showToast('File saved', 'success', 2000);
      });
      
      state.pendingEditorContent = null;
    });
  }
}

async function saveCurrentFile() {
  if (!state.currentOpenFile || !state.monacoEditor || !state.sessionId) return;
  
  // Only save if it was a text file loaded in monaco
  if (!dom.monacoContainer.classList.contains('hidden')) {
    try {
      const content = state.monacoEditor.getValue();
      await fetch(`${API_BASE}/api/files/${state.sessionId}/${state.currentOpenFile}`, {
        method: 'PUT',
        body: content
      });
    } catch (err) {
      showToast(`Failed to save ${state.currentOpenFile}`, 'error');
      console.error(err);
    }
  }
}

async function openFile(filepath, ext) {
  if (!state.sessionId) return;
  
  // Save current file before switching
  await saveCurrentFile();
  
  state.currentOpenFile = filepath;
  dom.editorFileTitle.textContent = filepath;
  dom.editorEmptyState.classList.add('hidden');
  
  // Highlight active item in tree
  dom.fileTree.querySelectorAll('.file-item').forEach(el => {
    el.classList.toggle('active-file', el.title === filepath);
  });

  const isImage = ['.png', '.jpg', '.jpeg', '.svg', '.eps'].includes(ext);
  
  try {
    const res = await fetch(`${API_BASE}/api/files/${state.sessionId}/${filepath}`);
    if (!res.ok) throw new Error('Failed to load file');

    if (isImage) {
      const blob = await res.blob();
      dom.monacoContainer.classList.add('hidden');
      dom.imageViewer.classList.remove('hidden');
      dom.imageViewerImg.src = URL.createObjectURL(blob);
    } else {
      const text = await res.text();
      dom.imageViewer.classList.add('hidden');
      dom.monacoContainer.classList.remove('hidden');
      
      let language = 'plaintext';
      if (['.tex', '.cls', '.sty', '.bib'].includes(ext)) language = 'latex';
      else if (ext === '.json') language = 'json';
      else if (ext === '.js') language = 'javascript';
      else if (ext === '.css') language = 'css';
        
      if (state.monacoEditor) {
        monaco.editor.setModelLanguage(state.monacoEditor.getModel(), language);
        state.monacoEditor.setValue(text);
      } else {
        // Monaco hasn't finished downloading yet
        state.pendingEditorContent = text;
        state.pendingEditorLanguage = language;
      }
    }
  } catch (err) {
    showToast(`Error opening file: ${err.message}`, 'error');
  }
}

// ─── File Tree Rendering ──────────────────────────────────────────────────────
function renderFileTree(files, detectedMain) {
  dom.fileTreeEmpty.classList.add('hidden');
  dom.fileTree.querySelectorAll('.file-item').forEach(el => el.remove());

  // Sort: main file first, then by extension, then alphabetically
  const sorted = [...files].sort((a, b) => {
    if (a.path === detectedMain) return -1;
    if (b.path === detectedMain) return 1;
    if (a.ext === '.tex' && b.ext !== '.tex') return -1;
    if (b.ext === '.tex' && a.ext !== '.tex') return 1;
    return a.path.localeCompare(b.path);
  });

  sorted.forEach(file => {
    const item = document.createElement('div');
    item.className = `file-item${file.path === detectedMain ? ' is-main' : ''}`;
    item.setAttribute('role', 'listitem');
    item.title = file.path;
    item.innerHTML = `
      <span class="file-icon">${fileIcon(file.ext)}</span>
      <span class="file-name">${file.path}${file.path === detectedMain ? ' ✦' : ''}</span>
      <span class="file-size">${formatSize(file.size)}</span>
    `;
    item.addEventListener('click', () => openFile(file.path, file.ext));
    dom.fileTree.appendChild(item);
  });
  
  // Automatically open the main file if present
  if (detectedMain && !state.currentOpenFile) {
    const ext = detectedMain.substring(detectedMain.lastIndexOf('.')).toLowerCase();
    openFile(detectedMain, ext);
  }

  // Update main file selector
  const texFiles = files.filter(f => f.ext === '.tex');
  if (texFiles.length > 0) {
    dom.mainFileSelector.classList.remove('hidden');
    dom.mainFileSelect.innerHTML = texFiles
      .map(f => `<option value="${f.path}" ${f.path === detectedMain ? 'selected' : ''}>${f.path}</option>`)
      .join('');
  }

  dom.clearSessionBtn.classList.remove('hidden');
  dom.compileBtn.disabled = false;
  updateStatusBar();
}

// ─── Upload Files ─────────────────────────────────────────────────────────────
async function uploadFiles(fileList) {
  if (state.isUploading || state.isCompiling) return;
  if (!fileList || fileList.length === 0) return;

  state.isUploading = true;
  dom.uploadProgress.classList.remove('hidden');
  dom.progressFill.style.width = '10%';
  dom.progressText.textContent = 'Uploading files…';
  dom.compileBtn.disabled = true;

  const formData = new FormData();
  for (const file of fileList) {
    formData.append('files', file);
  }

  try {
    dom.progressFill.style.width = '40%';

    const res = await fetch(`${API_BASE}/api/upload`, {
      method: 'POST',
      body: formData,
    });

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

    setTimeout(() => {
      dom.uploadProgress.classList.add('hidden');
      dom.progressFill.style.width = '0%';
    }, 800);

    renderFileTree(data.files, data.detected_main);
    showToast(`${data.files.length} file(s) uploaded successfully.`, 'success');

    if (!data.detected_main) {
      showToast('No main .tex file detected. Please select one from the dropdown.', 'warning');
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

// ─── Compile Project ──────────────────────────────────────────────────────────
async function compileProject() {
  if (!state.sessionId || state.isCompiling) return;
  state.isCompiling = true;

  // Save any pending editor changes first
  await saveCurrentFile();

  // UI: Enter compiling state
  dom.compileBtn.disabled = true;
  dom.compileBtn.classList.add('compiling');
  dom.compileBtnText.textContent = 'Compiling…';
  dom.compileSpinner.classList.remove('hidden');

  dom.pdfEmptyState.classList.add('hidden');
  dom.pdfFrame.classList.add('hidden');
  dom.pdfCompilingState.classList.remove('hidden');
  dom.downloadBtn.classList.add('hidden');
  dom.openNewTabBtn.classList.add('hidden');

  // Start timer
  state.compileStart = Date.now();
  const engine = dom.engineSelect.value;
  dom.compilingDesc.textContent = `Running ${engine}…`;

  state.compileTimer = setInterval(() => {
    dom.compileTimerEl.textContent = elapsedSince(state.compileStart);
  }, 100);

  // Clear previous log
  clearLog();

  const formData = new FormData();
  formData.append('session_id', state.sessionId);
  formData.append('engine', engine);

  // Get selected main file
  const mainFile = dom.mainFileSelect.value || state.detectedMain || '';
  if (mainFile) formData.append('main_file', mainFile);

  try {
    const res = await fetch(`${API_BASE}/api/compile`, {
      method: 'POST',
      body: formData,
    });

    const data = await res.json();
    const duration = elapsedSince(state.compileStart);

    // Stop timer
    clearInterval(state.compileTimer);
    dom.compileTimerEl.textContent = duration;

    state.parsedLog = data.log;
    state.rawLog = data.log?.raw || '';

    renderLog(data);

    if (data.success) {
      // Show PDF
      state.pdfUrl = `${API_BASE}/api/pdf/${state.sessionId}?t=${Date.now()}`;
      showPdf(state.pdfUrl);
      dom.lastCompileStatus.textContent = `✓ Compiled in ${duration}`;
      showToast(`Compilation successful in ${duration}!`, 'success');
    } else {
      // Show failure state
      dom.pdfCompilingState.classList.add('hidden');
      dom.pdfEmptyState.classList.remove('hidden');
      dom.lastCompileStatus.textContent = `✗ Failed (${duration})`;

      const errorCount = data.log?.errors?.length || 0;
      showToast(
        `Compilation failed with ${errorCount} error(s). Check the log panel.`,
        'error'
      );
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

// ─── Show PDF ─────────────────────────────────────────────────────────────────
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
  dom.logContainer.querySelectorAll('.log-entry').forEach(el => el.remove());
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

  const errors   = log.errors   || [];
  const warnings = log.warnings || [];
  const badboxes = log.badboxes || [];

  // Update badge counts
  dom.errorCount.textContent = errors.length;
  dom.warnCount.textContent  = warnings.length;

  // Render summary bar
  const duration = data.duration_seconds ? `${data.duration_seconds}s` : '';
  dom.compileDuration.textContent = duration;
  dom.logSummaryInner.textContent = data.summary || '';
  dom.logSummary.className = `log-summary ${data.success ? 'success' : 'error'}`;
  dom.logSummary.classList.remove('hidden');

  const allEntries = [
    ...errors.map(e => ({...e, level: 'error'})),
    ...warnings.map(w => ({...w, level: 'warning'})),
    ...badboxes.map(b => ({...b, level: 'badbox'})),
  ];

  if (allEntries.length === 0) {
    dom.logEmpty.classList.remove('hidden');
    if (data.success) {
      dom.logEmpty.innerHTML = '<p style="color: var(--success-text);">✅ Compilation successful with no errors or warnings.</p>';
    }
    return;
  }

  dom.logEmpty.classList.add('hidden');
  allEntries.forEach(entry => {
    dom.logContainer.appendChild(createLogEntry(entry));
  });

  applyLogFilter(state.currentFilter);
}

function createLogEntry(entry) {
  const div = document.createElement('div');
  div.className = `log-entry ${entry.level}`;
  div.dataset.level = entry.level;

  const levelLabel = entry.level === 'badbox' ? 'Badbox' :
    entry.level.charAt(0).toUpperCase() + entry.level.slice(1);

  let fileLine = '';
  if (entry.file || entry.line) {
    fileLine = `<span class="log-file-line">${entry.file || ''}${entry.line ? `:${entry.line}` : ''}</span>`;
  }

  let contextHtml = '';
  if (entry.context && entry.context.trim()) {
    contextHtml = `<pre class="log-context">${escapeHtml(entry.context)}</pre>`;
  }

  div.innerHTML = `
    <div class="log-entry-header">
      <span class="log-level-badge ${entry.level}">${levelLabel}</span>
      ${fileLine}
    </div>
    <div class="log-message">${escapeHtml(entry.message)}</div>
    ${contextHtml}
  `;
  return div;
}

function escapeHtml(str) {
  return (str || '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function applyLogFilter(filter) {
  state.currentFilter = filter;

  // Update filter button states
  [dom.filterAll, dom.filterErrors, dom.filterWarnings].forEach(btn => {
    btn.classList.toggle('active', btn.dataset.filter === filter);
  });

  // Show/hide entries
  dom.logContainer.querySelectorAll('.log-entry').forEach(entry => {
    if (filter === 'all') {
      entry.style.display = '';
    } else {
      entry.style.display = entry.dataset.level === filter ? '' : 'none';
    }
  });
}

// ─── Clear Session ────────────────────────────────────────────────────────────
async function clearSession() {
  if (!state.sessionId) return;

  try {
    await fetch(`${API_BASE}/api/cleanup/${state.sessionId}`, { method: 'DELETE' });
  } catch { /* ignore */ }

  state.sessionId = null;
  state.files = [];
  state.detectedMain = null;
  state.pdfUrl = null;
  state.rawLog = '';
  state.parsedLog = null;
  state.currentOpenFile = null;
  
  if (state.monacoEditor) {
    state.monacoEditor.setValue('');
  }
  dom.editorFileTitle.textContent = 'No file selected';
  dom.monacoContainer.classList.remove('hidden');
  dom.imageViewer.classList.add('hidden');
  dom.editorEmptyState.classList.remove('hidden');

  // Reset UI
  dom.fileTree.querySelectorAll('.file-item').forEach(el => el.remove());
  dom.fileTreeEmpty.classList.remove('hidden');
  dom.mainFileSelector.classList.add('hidden');
  dom.clearSessionBtn.classList.add('hidden');
  dom.compileBtn.disabled = true;

  dom.pdfFrame.src = '';
  dom.pdfFrame.classList.add('hidden');
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

// ─── Download PDF ─────────────────────────────────────────────────────────────
function downloadPdf() {
  if (!state.sessionId) return;
  const a = document.createElement('a');
  a.href = `${API_BASE}/api/pdf/${state.sessionId}?download=1`;
  a.download = 'output.pdf';
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
}

// ─── Copy Log ─────────────────────────────────────────────────────────────────
async function copyLog() {
  if (!state.rawLog) {
    showToast('No log to copy.', 'warning');
    return;
  }
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

  // Prevent default browser behavior for drag events
  ['dragenter', 'dragover', 'dragleave', 'drop'].forEach(evt => {
    zone.addEventListener(evt, e => { e.preventDefault(); e.stopPropagation(); });
    document.body.addEventListener(evt, e => { e.preventDefault(); });
  });

  zone.addEventListener('dragenter', () => zone.classList.add('drag-over'));
  zone.addEventListener('dragover',  () => zone.classList.add('drag-over'));
  zone.addEventListener('dragleave', (e) => {
    if (!zone.contains(e.relatedTarget)) zone.classList.remove('drag-over');
  });
  zone.addEventListener('drop', (e) => {
    zone.classList.remove('drag-over');
    const files = e.dataTransfer?.files;
    if (files && files.length > 0) uploadFiles(files);
  });

  // Click to open file picker
  zone.addEventListener('click', (e) => {
    if (e.target !== dom.fileInput) dom.fileInput.click();
  });

  zone.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      dom.fileInput.click();
    }
  });

  dom.fileInput.addEventListener('change', (e) => {
    const files = e.target.files;
    if (files && files.length > 0) {
      uploadFiles(files);
      dom.fileInput.value = '';
    }
  });
}

// ─── Event Listeners ──────────────────────────────────────────────────────────
function setupEventListeners() {
  dom.compileBtn.addEventListener('click', compileProject);
  dom.clearSessionBtn.addEventListener('click', clearSession);
  dom.downloadBtn.addEventListener('click', downloadPdf);
  dom.copyLogBtn.addEventListener('click', copyLog);

  dom.openNewTabBtn.addEventListener('click', () => {
    if (state.pdfUrl) window.open(state.pdfUrl, '_blank');
  });

  dom.toggleLogBtn.addEventListener('click', () => {
    dom.logSection.classList.toggle('hidden');
  });

  // Log filters
  [dom.filterAll, dom.filterErrors, dom.filterWarnings].forEach(btn => {
    btn.addEventListener('click', () => applyLogFilter(btn.dataset.filter));
  });

  // Cleanup on page unload
  window.addEventListener('beforeunload', () => {
    if (state.sessionId) {
      navigator.sendBeacon(`${API_BASE}/api/cleanup/${state.sessionId}`);
    }
  });
}

// ─── Initialize ───────────────────────────────────────────────────────────────
async function init() {
  initMonaco();
  setupDropZone();
  setupEventListeners();

  // Set status dot to pulsing while checking
  dom.statusDot.classList.add('pulse');
  await checkLatexStatus();
  dom.statusDot.classList.remove('pulse');

  updateStatusBar();
}

// Start the app
init();
