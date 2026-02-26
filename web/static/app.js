/* ──────────────────────────────────────────────────────────────────────────
   Porpulsion — UI
   Talks to the porpulsion proxy API which forwards to Ollama.
   ────────────────────────────────────────────────────────────────────────── */

// ── State ─────────────────────────────────────────────────────────────────
let chatHistory  = []; // [{role, content}] — active conversation
let activeReader = null;
let currentModel = '';
let activeConvId = null;

// ── Conversation persistence ───────────────────────────────────────────────
const CONV_KEY = 'porpulsion_conversations';

function loadAllConversations() {
  try { return JSON.parse(localStorage.getItem(CONV_KEY) || '[]'); }
  catch { return []; }
}

function saveAllConversations(convs) {
  try {
    localStorage.setItem(CONV_KEY, JSON.stringify(convs));
  } catch {
    if (convs.length > 1) {
      convs.sort((a, b) => a.updatedAt - b.updatedAt);
      convs.shift();
      try { localStorage.setItem(CONV_KEY, JSON.stringify(convs)); } catch {}
    }
  }
}

function saveActiveConversation() {
  if (!activeConvId || chatHistory.length === 0) return;
  const convs = loadAllConversations();
  const idx   = convs.findIndex(c => c.id === activeConvId);
  if (idx === -1) return;
  convs[idx].messages  = chatHistory.slice();
  convs[idx].updatedAt = Date.now();
  convs[idx].model     = currentModel;
  saveAllConversations(convs);
}

function createNewConversation() {
  const id = (crypto.randomUUID ? crypto.randomUUID()
    : Date.now().toString(36) + Math.random().toString(36).slice(2));
  const conv = {
    id, title: 'New conversation',
    createdAt: Date.now(), updatedAt: Date.now(),
    model: currentModel, messages: [],
  };
  const convs = loadAllConversations();
  convs.unshift(conv);
  saveAllConversations(convs);
  activeConvId = id;
  return conv;
}

function loadConversation(id) {
  if (id === activeConvId) return;
  saveActiveConversation();
  const convs = loadAllConversations();
  const conv  = convs.find(c => c.id === id);
  if (!conv) return;
  activeConvId = id;
  chatHistory  = conv.messages.slice();
  renderChatHistory();
  document.getElementById('chatMeta').textContent = '';
  renderSidebar();
}

function deleteConversation(id) {
  const convs = loadAllConversations().filter(c => c.id !== id);
  saveAllConversations(convs);
  if (activeConvId === id) {
    activeConvId = null;
    chatHistory  = [];
    if (convs.length > 0) {
      loadConversation(convs[0].id);
    } else {
      renderChatHistory();
    }
  }
  renderSidebar();
}

function renderSidebar() {
  const list  = document.getElementById('convList');
  if (!list) return;
  const convs = loadAllConversations();
  if (convs.length === 0) {
    list.innerHTML = `<p class="conv-empty">No conversations yet.<br>Send a message to get started.</p>`;
    return;
  }
  list.innerHTML = convs.map(c => {
    const active = c.id === activeConvId;
    return `<div class="conv-item${active ? ' active' : ''}" onclick="loadConversation('${esc(c.id)}')">
      <div class="conv-item-body">
        <div class="conv-title">${esc(c.title)}</div>
        <div class="conv-date">${formatConvDate(c.updatedAt)}</div>
      </div>
      <button class="conv-delete" title="Delete" onclick="event.stopPropagation();deleteConversation('${esc(c.id)}')">
        <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor"
          stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
          <line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/>
        </svg>
      </button>
    </div>`;
  }).join('');
}

function formatConvDate(ts) {
  const d = new Date(ts), now = new Date();
  if (d.toDateString() === now.toDateString()) {
    return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  }
  return d.toLocaleDateString([], { month: 'short', day: 'numeric' });
}

// ── Sidebar toggle (hamburger button) ────────────────────────────────────
function toggleSidebar() {
  const sidebar  = document.getElementById('convSidebar');
  const hamBtn   = document.getElementById('hamBtn');
  const overlay  = document.getElementById('sidebarOverlay');
  const open     = sidebar.classList.toggle('open');
  hamBtn.classList.toggle('active', open);
  // On mobile the overlay dims the chat area and catches tap-to-close.
  overlay.classList.toggle('show', open);
}

// ── Settings page ─────────────────────────────────────────────────────────
function openSettings() {
  document.getElementById('settingsPage').classList.add('open');
}

function closeSettings() {
  document.getElementById('settingsPage').classList.remove('open');
}

// ── Custom model picker ────────────────────────────────────────────────────
let _modelPickerOpen = false;

function toggleModelPicker() {
  _modelPickerOpen ? closeModelPicker() : openModelPicker();
}

function openModelPicker() {
  _modelPickerOpen = true;
  document.getElementById('modelPicker').classList.add('open');
  document.getElementById('modelDropdown').classList.add('open');
}

function closeModelPicker() {
  _modelPickerOpen = false;
  document.getElementById('modelPicker').classList.remove('open');
  document.getElementById('modelDropdown').classList.remove('open');
}

// Close picker on click outside
document.addEventListener('click', e => {
  if (_modelPickerOpen &&
      !e.target.closest('#modelPicker') &&
      !e.target.closest('#modelDropdown')) {
    closeModelPicker();
  }
});

function selectModel(name) {
  currentModel = name;
  document.getElementById('modelPickerLabel').textContent = name || 'Select model';
  // Update active state in list
  document.querySelectorAll('.model-dropdown-item').forEach(el => {
    el.classList.toggle('active', el.dataset.model === name);
  });
  closeModelPicker();
}

function renderModelDropdown(models) {
  const list = document.getElementById('modelDropdownList');
  if (!models || models.length === 0) {
    list.innerHTML = `<div class="model-dropdown-empty">No models installed</div>`;
    return;
  }
  list.innerHTML = models.map(m =>
    `<div class="model-dropdown-item${m.name === currentModel ? ' active' : ''}"
      data-model="${esc(m.name)}"
      onclick="selectModel('${esc(m.name)}')">
      ${esc(m.name)}
    </div>`
  ).join('');
}

// ── Boot ──────────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', async () => {
  initMarkdown();
  // Sidebar is open by default (HTML has class="conv-sidebar open")
  // Load the most recent conversation if one exists.
  const existing = loadAllConversations();
  if (existing.length > 0) {
    activeConvId = existing[0].id;
    chatHistory  = existing[0].messages.slice();
    renderChatHistory();
  }
  renderSidebar();
  await Promise.all([loadModels(), loadInfo(), loadFeatures(), startMetricsStream()]);
});

// ── Model list ────────────────────────────────────────────────────────────
async function loadModels() {
  try {
    const resp = await fetch('/api/models');
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const models = await resp.json();

    if (!models || models.length === 0) {
      document.getElementById('modelPickerLabel').textContent = 'No models installed';
      renderModelDropdown([]);
      renderModelList([]);
      setEmptyState(true);
      return;
    }
    setEmptyState(false);

    // Restore previously selected model if still available.
    if (!currentModel || !models.some(m => m.name === currentModel)) {
      currentModel = models[0].name;
    }
    document.getElementById('modelPickerLabel').textContent = currentModel;
    renderModelDropdown(models);
    renderModelList(models);
    setStatus(true);
  } catch {
    setStatus(false);
    document.getElementById('modelPickerLabel').textContent = 'Cannot reach Ollama';
    renderModelDropdown([]);
  }
}

function setEmptyState(empty) {
  const history   = document.getElementById('chatHistory');
  const inputWrap = document.querySelector('.chat-input-wrap');
  const existing  = document.getElementById('emptyState');
  if (existing) existing.remove();

  if (empty) {
    const el = document.createElement('div');
    el.id = 'emptyState';
    el.className = 'empty-state';
    el.innerHTML = `
      <svg width="40" height="40" viewBox="0 0 24 24" fill="none" stroke="currentColor"
        stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" style="color:var(--text3)">
        <rect x="2" y="3" width="20" height="14" rx="2"/>
        <line x1="8" y1="21" x2="16" y2="21"/><line x1="12" y1="17" x2="12" y2="21"/>
      </svg>
      <p class="empty-title">No models installed</p>
      <p class="empty-sub">Search for a model in Settings to get started.</p>
      <button class="btn-send" style="margin-top:6px" onclick="openSettings()">
        Browse models
      </button>`;
    history.parentNode.insertBefore(el, history);
    history.style.display = 'none';
    inputWrap.style.opacity = '0.4';
    inputWrap.style.pointerEvents = 'none';
  } else {
    history.style.display = '';
    inputWrap.style.opacity = '';
    inputWrap.style.pointerEvents = '';
  }
}

function renderModelList(models) {
  const container = document.getElementById('modelList');
  if (!models || models.length === 0) {
    container.innerHTML = '<p style="font-size:0.78rem;color:var(--text3)">No local models.</p>';
    return;
  }
  container.innerHTML = models.map(m => {
    const sizeGB = m.size ? (m.size / 1e9).toFixed(1) + ' GB' : '';
    const quant  = m.details?.quantization_level || '';
    const meta   = [quant, sizeGB].filter(Boolean).join(' · ');
    return `<div class="model-item">
      <div>
        <div class="model-item-name">${esc(m.name)}</div>
        ${meta ? `<div class="model-item-meta">${esc(meta)}</div>` : ''}
      </div>
      <button class="btn-delete" title="Delete model"
        onclick="deleteModel('${esc(m.name)}')">
        <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor"
          stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <polyline points="3 6 5 6 21 6"/>
          <path d="M19 6l-1 14H6L5 6"/>
          <path d="M10 11v6M14 11v6"/>
          <path d="M9 6V4h6v2"/>
        </svg>
      </button>
    </div>`;
  }).join('');
}

// ── Model search ───────────────────────────────────────────────────────────
const KNOWN_MODELS = [
  // Tiny / edge (< 1B)
  { name: 'smollm2:135m',      desc: 'SmolLM2 135M — smallest useful LLM, ~270 MB' },
  { name: 'smollm2:360m',      desc: 'SmolLM2 360M — runs on anything' },
  { name: 'qwen2.5:0.5b',      desc: 'Qwen 2.5 500M — very fast, low memory' },
  { name: 'qwen2.5:1.5b',      desc: 'Qwen 2.5 1.5B — good for simple tasks' },
  { name: 'tinyllama',         desc: 'TinyLlama 1.1B — popular lightweight baseline' },
  // Small (1–4B)
  { name: 'llama3.2:1b',       desc: 'Meta Llama 3.2 1B — ultra lightweight' },
  { name: 'llama3.2',          desc: 'Meta Llama 3.2 3B — fast general purpose' },
  { name: 'smollm2',           desc: 'SmolLM2 1.7B — tiny, fast' },
  { name: 'moondream',         desc: 'Moondream 2 1.8B — small vision model' },
  { name: 'deepseek-r1:1.5b',  desc: 'DeepSeek R1 1.5B — reasoning in tiny form' },
  { name: 'phi4-mini',         desc: 'Microsoft Phi-4 Mini 3.8B — efficient reasoning' },
  { name: 'phi3.5',            desc: 'Microsoft Phi-3.5 Mini 3.8B' },
  { name: 'gemma3',            desc: 'Google Gemma 3 4B — lightweight multimodal' },
  { name: 'qwen2.5:3b',        desc: 'Qwen 2.5 3B — solid small model' },
  // Medium (5–9B)
  { name: 'llama3.1',          desc: 'Meta Llama 3.1 8B — strong instruction following' },
  { name: 'mistral',           desc: 'Mistral 7B — fast and capable' },
  { name: 'qwen2.5',           desc: 'Alibaba Qwen 2.5 7B — multilingual' },
  { name: 'qwen2.5-coder',     desc: 'Qwen 2.5 Coder 7B — code generation' },
  { name: 'deepseek-r1',       desc: 'DeepSeek R1 7B — strong reasoning' },
  { name: 'codellama',         desc: 'Meta Code Llama 7B — code generation' },
  { name: 'llava',             desc: 'LLaVA 7B — vision + language' },
  // Large (10–20B)
  { name: 'gemma3:12b',        desc: 'Google Gemma 3 12B' },
  { name: 'mistral-nemo',      desc: 'Mistral Nemo 12B — multilingual' },
  { name: 'phi4',              desc: 'Microsoft Phi-4 14B — strong reasoning' },
  { name: 'qwen2.5:14b',       desc: 'Alibaba Qwen 2.5 14B' },
  { name: 'deepseek-r1:14b',   desc: 'DeepSeek R1 14B' },
  // XL (20B+)
  { name: 'gemma3:27b',        desc: 'Google Gemma 3 27B — high quality' },
  { name: 'llama3.1:70b',      desc: 'Meta Llama 3.1 70B — high quality, large' },
  { name: 'qwen2.5:72b',       desc: 'Alibaba Qwen 2.5 72B — top quality' },
  { name: 'deepseek-r1:70b',   desc: 'DeepSeek R1 70B — frontier reasoning' },
  // Embeddings
  { name: 'nomic-embed-text',  desc: 'Nomic Embed Text — text embeddings' },
  { name: 'mxbai-embed-large', desc: 'MxBai Embed Large — high quality embeddings' },
];

let searchDebounce = null;

function onSearchInput() {
  const q     = document.getElementById('modelSearch').value.trim();
  const clear = document.getElementById('searchClear');
  clear.style.display = q ? '' : 'none';
  clearTimeout(searchDebounce);
  if (!q) { hideSearchResults(); return; }
  searchDebounce = setTimeout(() => renderSearchResults(q), 120);
}

function onSearchKeydown(e) {
  if (e.key !== 'Enter') return;
  const q = document.getElementById('modelSearch').value.trim();
  if (q) pullModel(q);
}

function clearSearch() {
  document.getElementById('modelSearch').value = '';
  document.getElementById('searchClear').style.display = 'none';
  hideSearchResults();
}

function hideSearchResults() {
  document.getElementById('searchResults').style.display = 'none';
}

function renderSearchResults(query) {
  const q       = query.toLowerCase();
  const results = document.getElementById('searchResults');

  const matches = KNOWN_MODELS.filter(m =>
    m.name.includes(q) || m.desc.toLowerCase().includes(q)
  );
  if (query && !KNOWN_MODELS.some(m => m.name === query)) {
    matches.unshift({ name: query, desc: 'Pull by exact name' });
  }

  if (matches.length === 0) {
    results.innerHTML = `<div class="search-no-results">No matches — press Enter to pull "${esc(query)}"</div>`;
  } else {
    results.innerHTML = matches.map(m =>
      `<div class="search-result-item" onclick="pullModel('${esc(m.name)}')">
        <div class="search-result-left">
          <span class="search-result-name">${esc(m.name)}</span>
          <span class="search-result-desc">${esc(m.desc)}</span>
        </div>
        <button class="btn-pull-small" onclick="event.stopPropagation();pullModel('${esc(m.name)}')">Pull</button>
      </div>`
    ).join('');
  }
  results.style.display = '';
}

// ── Pull model ─────────────────────────────────────────────────────────────
async function pullModel(name) {
  if (!name) {
    name = document.getElementById('modelSearch').value.trim();
  }
  if (!name) return;

  const progress = document.getElementById('pullProgress');
  hideSearchResults();
  clearSearch();
  document.querySelectorAll('.btn-pull-small').forEach(b => b.disabled = true);
  progress.textContent = `Pulling ${name}…`;

  try {
    const resp = await fetch('/api/pull', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name }),
    });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);

    const reader  = resp.body.getReader();
    const decoder = new TextDecoder();
    let   buffer  = '';

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split('\n');
      buffer = lines.pop();
      for (const line of lines) {
        if (!line.startsWith('data: ')) continue;
        try {
          const ev = JSON.parse(line.slice(6));
          if (ev.error) { progress.textContent = 'Error: ' + ev.error; return; }
          if (ev.total && ev.completed) {
            const pct = Math.round((ev.completed / ev.total) * 100);
            progress.textContent = `${ev.status} — ${pct}%`;
          } else {
            progress.textContent = ev.status || '';
          }
        } catch {}
      }
    }

    progress.textContent = `✓ ${name} ready`;
    setTimeout(() => { progress.textContent = ''; }, 3000);
    await loadModels();
  } catch (err) {
    progress.textContent = 'Error: ' + err.message;
  } finally {
    document.querySelectorAll('.btn-pull-small').forEach(b => b.disabled = false);
  }
}

// ── Delete model ───────────────────────────────────────────────────────────
async function deleteModel(name) {
  if (!confirm(`Delete model "${name}"?`)) return;
  try {
    const resp = await fetch(`/api/models/${encodeURIComponent(name)}`, { method: 'DELETE' });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    if (currentModel === name) currentModel = '';
    await loadModels();
  } catch (err) {
    alert('Delete failed: ' + err.message);
  }
}

// ── Performance features ───────────────────────────────────────────────────

// IDs that belong to the Memory Footprint section; everything else → CPU Performance.
const MEMORY_FEATURE_IDS = new Set(['lean_context', 'low_vram', 'aggressive_quant']);

async function loadFeatures() {
  try {
    const resp = await fetch('/api/features');
    if (!resp.ok) return;
    const list = await resp.json();
    renderFeatures(list);
    if (list.some(f => f.id === 'quant_advisor' && f.enabled)) {
      loadQuantAdvice();
    }
  } catch {}
}

function featureRow(f) {
  return `
  <div class="feature-row" id="feature-row-${esc(f.id)}">
    <div class="feature-info">
      <span class="feature-name">${esc(f.name)}</span>
      <span class="feature-desc">${esc(f.description)}</span>
    </div>
    <label class="toggle-switch" title="${f.enabled ? 'Enabled' : 'Disabled'}">
      <input type="checkbox" ${f.enabled ? 'checked' : ''}
        onchange="toggleFeature('${esc(f.id)}', this.checked)" />
      <span class="toggle-track"></span>
    </label>
  </div>`;
}

function renderFeatures(list) {
  const cpu    = document.getElementById('featureListCpu');
  const memory = document.getElementById('featureListMemory');
  const empty  = '<p style="color:var(--text3);font-size:0.78rem">No flags in this group.</p>';

  if (!list || list.length === 0) {
    cpu.innerHTML = memory.innerHTML = empty;
    return;
  }

  const cpuFlags    = list.filter(f => !MEMORY_FEATURE_IDS.has(f.id));
  const memoryFlags = list.filter(f =>  MEMORY_FEATURE_IDS.has(f.id));

  cpu.innerHTML    = cpuFlags.length    ? cpuFlags.map(featureRow).join('')    : empty;
  memory.innerHTML = memoryFlags.length ? memoryFlags.map(featureRow).join('') : empty;
}

async function toggleFeature(id, enabled) {
  try {
    const resp = await fetch('/api/features', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ feature: id, enabled }),
    });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const updated = await resp.json();
    renderFeatures(updated);
    if (id === 'quant_advisor') {
      if (enabled) loadQuantAdvice();
      else document.getElementById('quantAdvicePanel').style.display = 'none';
    }
  } catch (err) {
    alert('Could not toggle feature: ' + err.message);
  }
}

async function loadQuantAdvice() {
  const panel = document.getElementById('quantAdvicePanel');
  panel.style.display = '';
  panel.innerHTML = '<span style="font-size:0.78rem;color:var(--text3)">Analysing RAM…</span>';
  try {
    const resp = await fetch('/api/quant-advice');
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const data = await resp.json();
    const rec  = data.recommended;
    const ram  = data.ram_gb ? data.ram_gb.toFixed(1) : '?';
    panel.innerHTML = `
      <div class="quant-advice">
        <div class="quant-advice-header">
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor"
            stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/>
          </svg>
          Quant recommendation — ${ram} GB RAM
        </div>
        <div class="quant-badge">${esc(rec?.label || '—')}</div>
        <p class="quant-desc">${esc(rec?.description || '')}</p>
        <p class="quant-hint">Models pulled without a tag will use <strong>${esc(rec?.tag || '—')}</strong>.</p>
      </div>`;
  } catch {
    panel.innerHTML = '<span style="font-size:0.78rem;color:var(--text3)">Could not load advice.</span>';
  }
}

// ── Server info ────────────────────────────────────────────────────────────
async function loadInfo() {
  try {
    const resp = await fetch('/api/info');
    const info = await resp.json();
    renderInfo(info);
  } catch {
    const p = document.getElementById('infoPanel');
    if (p) p.innerHTML = '<p style="color:var(--text3);font-size:0.84rem">Could not load server info.</p>';
  }
}

function renderInfo(info) {
  const cpuInfo = info.cpu || {};
  const featureKeys   = ['has_avx','has_avx2','has_f16c','has_fma','has_avx512','has_amx','has_neon','has_sve'];
  const featureLabels = { has_avx:'AVX', has_avx2:'AVX2', has_f16c:'F16C', has_fma:'FMA',
                          has_avx512:'AVX-512', has_amx:'AMX', has_neon:'NEON', has_sve:'SVE' };
  const badges = featureKeys.map(k =>
    `<span class="badge ${cpuInfo[k] ? 'on' : ''}">${featureLabels[k]}</span>`
  ).join('');

  const ramGB  = info.ram_gb != null ? info.ram_gb.toFixed(1) + ' GB' : '—';
  const cores  = [
    cpuInfo.logical_cores ? `${cpuInfo.logical_cores} logical` : null,
    cpuInfo.p_cores       ? `${cpuInfo.p_cores}P` : null,
    cpuInfo.e_cores       ? `${cpuInfo.e_cores}E` : null,
  ].filter(Boolean).join(' · ') || '—';

  const cards = [
    ['Version',    info.version          || '—'],
    ['Ollama',     info.ollama_version   || '—'],
    ['Uptime',     info.uptime_seconds != null ? formatUptime(info.uptime_seconds) : '—'],
    ['RAM',        ramGB],
    ['CPU',        cpuInfo.model         || '—'],
    ['Cores',      cores],
    ['L3 Cache',   cpuInfo.l3_cache_mb   ? `${cpuInfo.l3_cache_mb} MB` : '—'],
    ['SIMD',       badges],
  ];

  const panel = document.getElementById('infoPanel');
  if (panel) {
    panel.className = 'info-grid';
    panel.innerHTML = cards.map(([label, val]) =>
      `<div class="info-card">
         <span class="info-card-label">${label}</span>
         <span class="info-card-value">${val}</span>
       </div>`
    ).join('');
  }
}

// ── Metrics stream — with reconnect on page reload ─────────────────────────
// The NetworkError on reload happens because the SSE connection is abruptly
// closed. We catch it and reconnect after a short delay.
let _metricsRetry = null;

async function startMetricsStream() {
  clearTimeout(_metricsRetry);
  try {
    const resp = await fetch('/api/metrics', { headers: { Accept: 'text/event-stream' } });
    if (!resp.ok) { scheduleMetricsReconnect(); return; }
    const reader  = resp.body.getReader();
    const decoder = new TextDecoder();
    let   buffer  = '';
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split('\n');
      buffer = lines.pop();
      for (const line of lines) {
        if (!line.startsWith('data: ')) continue;
        try {
          const snap = JSON.parse(line.slice(6));
          updateTPS(snap.tokens_per_second || 0);
        } catch {}
      }
    }
  } catch {
    // Swallow — expected on tab close / page reload
  }
  // Stream ended (server closed or navigated away) — reconnect.
  scheduleMetricsReconnect();
}

function scheduleMetricsReconnect() {
  clearTimeout(_metricsRetry);
  _metricsRetry = setTimeout(startMetricsStream, 3000);
}

function updateTPS(tps) {
  const badge = document.getElementById('tpsBadge');
  if (tps > 0) {
    badge.textContent = tps.toFixed(1) + ' tok/s';
    badge.classList.add('active');
  } else {
    badge.textContent = '— tok/s';
    badge.classList.remove('active');
  }
}

// ── Status dot ─────────��───────────────────────────────────────────────────
function setStatus(ok) {
  const dot = document.getElementById('statusDot');
  dot.classList.toggle('ok', ok);
  dot.classList.toggle('err', !ok);
  dot.title = ok ? 'Ollama connected' : 'Ollama unreachable';
}

// ── Config helpers ─────────────────────────────────────────────────────────
function getConfig() {
  return {
    systemPrompt: document.getElementById('systemPrompt').value.trim(),
    temperature:  parseFloat(document.getElementById('temperature').value),
    topP:         parseFloat(document.getElementById('topP').value),
    topK:         parseInt(document.getElementById('topK').value, 10),
    maxTokens:    parseInt(document.getElementById('maxTokens').value, 10) || 2048,
    ctxSize:      parseInt(document.getElementById('ctxSize').value, 10)   || 4096,
  };
}

function resetConfig() {
  document.getElementById('systemPrompt').value = '';
  document.getElementById('temperature').value  = '0.7';
  document.getElementById('topP').value         = '0.9';
  document.getElementById('topK').value         = '40';
  document.getElementById('maxTokens').value    = '2048';
  document.getElementById('ctxSize').value      = '4096';
  document.getElementById('tempVal').textContent = '0.70';
  document.getElementById('topPVal').textContent = '0.90';
  document.getElementById('topKVal').textContent = '40';
}

// ── Chat ───────────────────────────────────────────────────────────────────
function newChat() {
  saveActiveConversation();
  activeConvId = null;
  chatHistory  = [];
  renderChatHistory();
  document.getElementById('chatMeta').textContent = '';
  renderSidebar();
}

function chatKeydown(e) {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendChat(); }
}

// handleActionBtn dispatches to send or stop depending on current state.
function handleActionBtn() {
  const btn = document.getElementById('actionBtn');
  if (btn.classList.contains('stopping')) stopChat();
  else sendChat();
}

async function sendChat() {
  const input     = document.getElementById('chatInput');
  const actionBtn = document.getElementById('actionBtn');
  const meta      = document.getElementById('chatMeta');
  const text      = input.value.trim();
  if (!text || !currentModel) return;

  input.value = '';
  actionBtn.classList.add('stopping');
  actionBtn.title = 'Stop';
  meta.textContent = '';

  const cfg = getConfig();

  const messages = [];
  if (cfg.systemPrompt) messages.push({ role: 'system', content: cfg.systemPrompt });
  chatHistory.forEach(m => { if (m.role !== '_system') messages.push(m); });
  messages.push({ role: 'user', content: text });

  if (!activeConvId) createNewConversation();

  chatHistory.push({ role: 'user', content: text });
  appendMessageBubble(chatHistory.length - 1);

  // Set title from first user message.
  const convs = loadAllConversations();
  const ci = convs.findIndex(c => c.id === activeConvId);
  if (ci !== -1 && convs[ci].title === 'New conversation') {
    convs[ci].title = text.length > 60 ? text.slice(0, 60) + '…' : text;
    saveAllConversations(convs);
    renderSidebar();
  }

  const start = Date.now();
  let   tokenCount = 0;

  try {
    const resp = await fetch('/v1/chat/completions', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        model:       currentModel,
        messages,
        stream:      true,
        max_tokens:  cfg.maxTokens,
        temperature: cfg.temperature,
        top_p:       cfg.topP,
        top_k:       cfg.topK,
      }),
    });

    if (!resp.ok) throw new Error(`HTTP ${resp.status}: ${await resp.text()}`);

    chatHistory.push({ role: 'assistant', content: '' });
    const idx    = chatHistory.length - 1;
    const bubble = appendMessageBubble(idx, true);

    activeReader  = resp.body.getReader();
    const decoder = new TextDecoder();
    let   sseBuffer = '';

    while (true) {
      const { done, value } = await activeReader.read();
      if (done) break;
      sseBuffer += decoder.decode(value, { stream: true });
      const lines = sseBuffer.split('\n');
      sseBuffer = lines.pop();
      for (const line of lines) {
        if (!line.startsWith('data: ')) continue;
        const raw = line.slice(6).trim();
        if (raw === '[DONE]') continue;
        try {
          const chunk = JSON.parse(raw);
          if (chunk.error) throw new Error(chunk.error);
          const delta = chunk.choices?.[0]?.delta?.content || '';
          if (delta) {
            chatHistory[idx].content += delta;
            tokenCount++;
            updateStreamingBubble(bubble, chatHistory[idx].content);
          }
        } catch (parseErr) {
          if (parseErr.message?.startsWith('Error:')) throw parseErr;
        }
      }
    }

    finaliseAssistantBubble(bubble, chatHistory[idx].content);

    const elapsed = ((Date.now() - start) / 1000).toFixed(1);
    const tps     = tokenCount > 0 ? (tokenCount / ((Date.now() - start) / 1000)).toFixed(1) : '—';
    meta.textContent = `${elapsed}s · ${tokenCount} tokens · ${tps} tok/s`;

  } catch (err) {
    if (err.name !== 'AbortError') {
      chatHistory.push({ role: 'error', content: `Error: ${err.message}` });
      appendMessageBubble(chatHistory.length - 1);
    }
  } finally {
    activeReader = null;
    const ab = document.getElementById('actionBtn');
    ab.classList.remove('stopping');
    ab.title = 'Send';
    saveActiveConversation();
    renderSidebar();
  }
}

function stopChat() {
  if (activeReader) { activeReader.cancel(); activeReader = null; }
  const ab = document.getElementById('actionBtn');
  ab.classList.remove('stopping');
  ab.title = 'Send';
  document.getElementById('chatMeta').textContent = 'Stopped.';
  const streaming = document.querySelector('.msg.assistant.streaming');
  if (streaming) {
    const idx = parseInt(streaming.dataset.idx, 10);
    if (!isNaN(idx)) finaliseAssistantBubble(streaming, chatHistory[idx]?.content || '');
  }
  saveActiveConversation();
  renderSidebar();
}

function appendMessageBubble(idx, streaming = false) {
  const msg       = chatHistory[idx];
  const container = document.getElementById('chatHistory');
  const el        = document.createElement('div');
  el.dataset.idx  = idx;

  if (msg.role === 'user') {
    el.className = 'msg user';
    const bubble = document.createElement('div');
    bubble.className = 'msg-bubble';
    bubble.innerHTML = renderMarkdown(msg.content);
    el.appendChild(bubble);
  } else if (msg.role === 'error') {
    el.className   = 'msg error';
    el.textContent = msg.content;
  } else if (streaming) {
    el.className = 'msg assistant streaming';
    el.innerHTML = `<div class="msg-assistant-inner"><div class="streaming-text"></div></div>`;
  } else {
    el.className = 'msg assistant';
    el.innerHTML = `<div class="msg-assistant-inner">${renderMarkdown(msg.content)}</div>`;
  }

  container.appendChild(el);
  el.scrollIntoView({ block: 'end', behavior: 'instant' });
  return el;
}

function updateStreamingBubble(el, content) {
  const textNode = el.querySelector('.streaming-text');
  if (textNode) textNode.textContent = content;
  const container = document.getElementById('chatHistory');
  const nearBottom = container.scrollHeight - container.scrollTop - container.clientHeight < 120;
  if (nearBottom) container.scrollTop = container.scrollHeight;
}

function finaliseAssistantBubble(el, content) {
  el.classList.remove('streaming');
  el.innerHTML = `<div class="msg-assistant-inner">${renderMarkdown(content)}</div>`;
  el.scrollIntoView({ block: 'end', behavior: 'instant' });
}

function renderChatHistory() {
  const container = document.getElementById('chatHistory');
  container.innerHTML = '';
  chatHistory.forEach((_, i) => appendMessageBubble(i));
}

// ── Utilities ─────────────────────────────────────────────────────────────
function escapeHtml(s) {
  return s
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');
}

function esc(s) { return escapeHtml(String(s ?? '')); }

function formatUptime(secs) {
  if (secs < 60)   return `${Math.floor(secs)}s`;
  if (secs < 3600) return `${Math.floor(secs/60)}m ${Math.floor(secs%60)}s`;
  return `${Math.floor(secs/3600)}h ${Math.floor((secs%3600)/60)}m`;
}

// ── Markdown rendering ─────────────────────────────────────────────────────
function initMarkdown() {
  if (typeof marked === 'undefined') return;

  marked.use({
    gfm: true,
    breaks: false,
    extensions: [],
    renderer: {
      code(text, lang) {
        const l = (lang || 'text').toLowerCase().split(/[\s{]/)[0];
        const highlighted = (typeof hljs !== 'undefined' && hljs.getLanguage(l))
          ? hljs.highlight(text, { language: l }).value
          : escapeHtml(text);
        return `<div class="code-block">
  <div class="code-block-header">
    <span class="code-lang">${escapeHtml(l)}</span>
    <button class="btn-copy-code" onclick="copyCode(this)">
      <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor"
        stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <rect x="9" y="9" width="13" height="13" rx="2"/>
        <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>
      </svg>Copy
    </button>
  </div>
  <pre><code class="hljs">${highlighted}</code></pre>
</div>`;
      }
    }
  });
}

function renderMarkdown(text) {
  if (typeof marked === 'undefined') {
    return `<pre style="white-space:pre-wrap;word-break:break-word">${escapeHtml(text)}</pre>`;
  }
  try {
    // Normalise line endings.
    let normalised = text.replace(/\r\n/g, '\n').replace(/\r/g, '\n');

    // Promote single newlines between plain-text lines into paragraph breaks.
    // Only applies when neither the preceding nor the following line looks like
    // a markdown block element (heading, list, fence, blockquote, blank).
    // This prevents models that output prose with single \n from getting
    // everything jammed onto one line by marked's default paragraph logic.
    const mdBlock = /^(\s*(#{1,6} |[-*+] |[0-9]+\. |>|```|~~~|\s*$))/;
    normalised = normalised.replace(/([^\n])\n([^\n])/g, (_, before, after) => {
      // If the line after looks like a markdown block, keep as-is.
      if (mdBlock.test(after)) return before + '\n' + after;
      // Otherwise double the newline so marked creates a new paragraph.
      return before + '\n\n' + after;
    });

    const html = marked.parse(normalised);
    return html || `<pre style="white-space:pre-wrap">${escapeHtml(text)}</pre>`;
  } catch(e) {
    console.error('marked.parse error:', e);
    return `<pre style="white-space:pre-wrap;word-break:break-word">${escapeHtml(text)}</pre>`;
  }
}

function copyCode(btn) {
  const code = btn.closest('.code-block')?.querySelector('code')?.textContent ?? '';
  navigator.clipboard.writeText(code).then(() => {
    btn.classList.add('copied');
    btn.innerHTML = `<svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor"
      stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
      <polyline points="20 6 9 17 4 12"/></svg> Copied!`;
    showToast('Copied to clipboard');
    setTimeout(() => {
      btn.classList.remove('copied');
      btn.innerHTML = `<svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor"
        stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <rect x="9" y="9" width="13" height="13" rx="2"/>
        <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>
      </svg> Copy`;
    }, 2000);
  });
}

let _toastTimer = null;
function showToast(msg) {
  let toast = document.getElementById('toast');
  if (!toast) {
    toast = document.createElement('div');
    toast.id = 'toast'; toast.className = 'toast';
    document.body.appendChild(toast);
  }
  toast.textContent = msg;
  toast.classList.add('show');
  clearTimeout(_toastTimer);
  _toastTimer = setTimeout(() => toast.classList.remove('show'), 1800);
}
