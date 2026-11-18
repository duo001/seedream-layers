/* seedream-layers 前端：原生 JS，无构建。 */

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));

const STORAGE = {
  baseUrl: 'seedream.baseUrl',
  key: 'seedream.key',
  model: 'seedream.model',
};

/* 旧版本（img2psd）的键名，用于自动迁移，避免用户重填 Key */
const LEGACY_STORAGE = {
  baseUrl: 'img2psd.baseUrl',
  key: 'img2psd.key',
  model: 'img2psd.model',
};

const FALLBACK = {
  baseUrl: 'https://ark.cn-beijing.volces.com/api/plan/v3',
  model: 'doubao-seedream-5-0-pro',
};

const state = {
  mode: 'upload',
  file: null,
  previewUrl: null,
  jobId: null,
  job: null,
  canvas: { w: 0, h: 0 },
  base: null,
  layers: [],
  selected: null,
  zoom: 1,
  busy: false,
  controller: null,
  timerId: null,
  startedAt: 0,
  config: { baseUrl: FALLBACK.baseUrl, key: '', model: FALLBACK.model },
  defaults: { ...FALLBACK },
  models: [],
};

const el = {
  mask: $('#mask'),
  configDrawer: $('#configDrawer'),
  historyDrawer: $('#historyDrawer'),
  modelChip: $('#modelChip'),
  board: $('#board'),
  sizer: $('#sizer'),
  canvasArea: $('#canvasArea'),
  emptyState: $('#emptyState'),
  toolbar: $('#toolbar'),
  layerPanel: $('#layerPanel'),
  layerList: $('#layerList'),
  layerCount: $('#layerCount'),
  jobBadge: $('#jobBadge'),
  zoomLabel: $('#zoomLabel'),
  inspector: $('#inspector'),
  insName: $('#insName'),
  insGeo: $('#insGeo'),
  opacityRange: $('#opacityRange'),
  opacityValue: $('#opacityValue'),
  runBtn: $('#runBtn'),
  runLabel: $('#runLabel'),
  runSpinner: $('#runSpinner'),
  runTimer: $('#runTimer'),
  cancelBtn: $('#cancelBtn'),
  fileInput: $('#fileInput'),
  dropzone: $('#dropzone'),
  dzEmpty: $('#dzEmpty'),
  dzPreview: $('#dzPreview'),
  previewImg: $('#previewImg'),
  previewName: $('#previewName'),
  previewMeta: $('#previewMeta'),
  promptInput: $('#promptInput'),
  layerPrompt: $('#layerPrompt'),
  sizeSelect: $('#sizeSelect'),
  watermark: $('#watermark'),
  maxImages: $('#maxImages'),
  toast: $('#toast'),
};

/* ---------------- 基础工具 ---------------- */

let toastTimer = null;
function toast(message, type = 'error') {
  el.toast.textContent = message;
  el.toast.className = 'toast' + (type === 'success' ? ' success' : '');
  el.toast.hidden = false;
  if (toastTimer) clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.toast.hidden = true; }, 4200);
}

function clamp(value, min, max) {
  return Math.min(max, Math.max(min, value));
}

function fileUrl(relative) {
  if (!state.jobId || !relative) return '';
  const segments = String(relative).split('/').filter(Boolean).map(encodeURIComponent);
  return `/files/${encodeURIComponent(state.jobId)}/${segments.join('/')}`;
}

function messageOf(payload, status) {
  if (!payload) return `HTTP ${status}`;
  const detail = payload.detail ?? payload.error ?? payload;
  if (typeof detail === 'string') return detail;
  if (Array.isArray(detail)) {
    return detail.map((item) => item.msg || JSON.stringify(item)).join('；');
  }
  if (typeof detail === 'object') {
    if (detail.message) {
      const upstream = detail.upstream_status ? `（上游 HTTP ${detail.upstream_status}）` : '';
      return detail.message + upstream;
    }
    return JSON.stringify(detail).slice(0, 400);
  }
  return String(detail);
}

class ApiError extends Error {
  constructor(message, payload) {
    super(message);
    this.payload = payload;
  }
}

async function request(url, options = {}) {
  const response = await fetch(url, {
    ...options,
    signal: state.controller ? state.controller.signal : undefined,
  });
  const text = await response.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = null; }
  if (!response.ok) {
    throw new ApiError(messageOf(data, response.status) || `请求失败（HTTP ${response.status}）`, data);
  }
  return data;
}

function loadImage(src) {
  return new Promise((resolve, reject) => {
    const image = new Image();
    image.onload = () => resolve(image);
    image.onerror = () => reject(new Error('图片加载失败'));
    image.src = src;
  });
}

function downloadBlob(blob, filename) {
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 2000);
}

/* ---------------- 配置 ---------------- */

function loadConfig() {
  const baseUrl = readSetting('baseUrl') || state.defaults.baseUrl;
  const key = readSetting('key') || '';
  const model = readSetting('model') || state.defaults.model;
  state.config = { baseUrl, key, model };
  syncConfigUI();
}

/* 读配置：新键名优先，读到旧键就顺手迁移过去 */
function readSetting(name) {
  const current = localStorage.getItem(STORAGE[name]);
  if (current !== null) return current;
  const legacy = localStorage.getItem(LEGACY_STORAGE[name]);
  if (legacy === null) return null;
  localStorage.setItem(STORAGE[name], legacy);
  localStorage.removeItem(LEGACY_STORAGE[name]);
  return legacy;
}

function syncConfigUI() {
  $('#cfgBaseUrl').value = state.config.baseUrl || state.defaults.baseUrl;
  $('#cfgKey').value = state.config.key || '';
  $('#cfgModel').value = state.config.model || state.defaults.model;
  el.modelChip.textContent = state.config.key
    ? state.config.model
    : `${state.config.model} · 未配置 Key`;
  el.modelChip.title = state.config.baseUrl;
}

function saveConfig() {
  const baseUrl = $('#cfgBaseUrl').value.trim() || state.defaults.baseUrl;
  const key = $('#cfgKey').value.trim();
  const model = $('#cfgModel').value.trim() || state.defaults.model;
  state.config = { baseUrl, key, model };
  localStorage.setItem(STORAGE.baseUrl, baseUrl);
  localStorage.setItem(STORAGE.key, key);
  localStorage.setItem(STORAGE.model, model);
  Object.values(LEGACY_STORAGE).forEach((name) => localStorage.removeItem(name));
  syncConfigUI();
  closeDrawers();
  toast('配置已保存到本地浏览器', 'success');
}

/* ---------------- 抽屉 ---------------- */

function openDrawer(drawer) {
  closeDrawers();
  el.mask.hidden = false;
  drawer.hidden = false;
}

function closeDrawers() {
  el.mask.hidden = true;
  el.configDrawer.hidden = true;
  el.historyDrawer.hidden = true;
}

/* ---------------- 初始化 ---------------- */

async function boot() {
  bindEvents();
  try {
    const health = await fetch('/api/health').then((response) => response.json());
    if (health?.defaults) state.defaults = { ...state.defaults, ...health.defaults };
    if (Array.isArray(health?.models)) {
      state.models = health.models;
      $('#modelOptions').innerHTML = health.models
        .map((model) => `<option value="${model}"></option>`)
        .join('');
    }
  } catch {
    toast('无法连接本地后端，请确认服务已启动');
  }
  loadConfig();
}

function bindEvents() {
  $$('.mode-tab').forEach((tab) => {
    tab.addEventListener('click', () => {
      state.mode = tab.dataset.mode;
      $$('.mode-tab').forEach((item) => item.classList.toggle('active', item === tab));
      $('#panelUpload').hidden = state.mode !== 'upload';
      $('#panelText').hidden = state.mode !== 'text';
      el.runLabel.textContent = state.mode === 'upload' ? '开始拆分' : '生成并拆分';
    });
  });

  el.dropzone.addEventListener('dragover', (event) => {
    event.preventDefault();
    el.dropzone.classList.add('dragover');
  });
  el.dropzone.addEventListener('dragleave', () => el.dropzone.classList.remove('dragover'));
  el.dropzone.addEventListener('drop', (event) => {
    event.preventDefault();
    el.dropzone.classList.remove('dragover');
    const file = event.dataTransfer?.files?.[0];
    if (file) setFile(file);
  });
  el.fileInput.addEventListener('change', () => {
    const file = el.fileInput.files?.[0];
    if (file) setFile(file);
    el.fileInput.value = '';
  });

  document.addEventListener('paste', (event) => {
    const items = event.clipboardData?.items || [];
    for (const item of items) {
      if (item.kind === 'file' && item.type.startsWith('image/')) {
        const file = item.getAsFile();
        if (file) {
          setFile(file);
          event.preventDefault();
        }
        break;
      }
    }
  });

  el.runBtn.addEventListener('click', run);
  el.cancelBtn.addEventListener('click', () => state.controller?.abort());

  $('#configBtn').addEventListener('click', () => openDrawer(el.configDrawer));
  $('#historyBtn').addEventListener('click', () => {
    openDrawer(el.historyDrawer);
    loadHistory();
  });
  el.mask.addEventListener('click', closeDrawers);
  // 事件委托：抽屉里的关闭按钮即使被重建也有效
  document.addEventListener('click', (event) => {
    if (event.target.closest('[data-close]')) closeDrawers();
  });
  $('#saveConfig').addEventListener('click', saveConfig);
  $('#resetConfig').addEventListener('click', () => {
    state.config = { ...state.defaults, key: state.config.key };
    localStorage.setItem(STORAGE.baseUrl, state.defaults.baseUrl);
    localStorage.setItem(STORAGE.model, state.defaults.model);
    syncConfigUI();
    toast('已恢复默认 Base URL 与 Model ID', 'success');
  });
  $('#clearKey').addEventListener('click', () => {
    localStorage.removeItem(STORAGE.key);
    state.config.key = '';
    $('#cfgKey').value = '';
    syncConfigUI();
    toast('已清除本地 Key', 'success');
  });

  el.toolbar.addEventListener('click', (event) => {
    const action = event.target.closest('[data-act]')?.dataset.act;
    if (!action) return;
    if (action === 'zoom-in') applyZoom(state.zoom * 1.2);
    if (action === 'zoom-out') applyZoom(state.zoom / 1.2);
    if (action === 'fit') fitZoom();
    if (action === 'toggle-all') toggleAllLayers();
    if (action === 'reset') resetAllLayers();
    if (action === 'export-png') exportComposite();
    if (action === 'export-zip') exportZip();
  });

  el.canvasArea.addEventListener('wheel', (event) => {
    if (!(event.ctrlKey || event.metaKey)) return;
    if (!state.jobId) return;
    event.preventDefault();
    applyZoom(state.zoom * (event.deltaY < 0 ? 1.1 : 1 / 1.1));
  }, { passive: false });

  el.opacityRange.addEventListener('input', () => {
    const layer = state.selected;
    if (!layer) return;
    layer.opacity = Number(el.opacityRange.value) / 100;
    if (layer.el) layer.el.style.opacity = String(layer.opacity);
    el.opacityValue.textContent = `${el.opacityRange.value}%`;
  });

  $('#resetLayer').addEventListener('click', () => {
    if (state.selected) resetLayer(state.selected);
  });

  document.addEventListener('keydown', (event) => {
    const tag = (event.target?.tagName || '').toLowerCase();
    const typing = ['input', 'textarea', 'select'].includes(tag) || event.target?.isContentEditable;

    if (event.key === 'Escape') {
      if (!el.configDrawer.hidden || !el.historyDrawer.hidden) {
        closeDrawers();
      } else if (typing) {
        event.target.blur();
      } else if (state.selected) {
        selectLayer(null);
      }
      return;
    }
    if (typing) return;

    const layer = state.selected;
    if (!layer) return;

    if (event.key === 'Delete' || event.key === 'Backspace') {
      event.preventDefault();
      removeLayer(layer);
      return;
    }
    const step = event.shiftKey ? 10 : 1;
    const moves = {
      ArrowLeft: [-step, 0],
      ArrowRight: [step, 0],
      ArrowUp: [0, -step],
      ArrowDown: [0, step],
    };
    if (moves[event.key]) {
      event.preventDefault();
      layer.x += moves[event.key][0];
      layer.y += moves[event.key][1];
      positionLayer(layer);
      updateInspector();
    }
  });

  window.addEventListener('resize', () => {
    if (state.jobId) fitZoom();
  });
}

/* ---------------- 上传与预检 ---------------- */

async function setFile(file) {
  try {
    if (!/^image\/(png|jpeg)$/.test(file.type)) {
      throw new Error('只支持 PNG / JPEG');
    }
    if (file.size > 30 * 1024 * 1024) {
      throw new Error(`图片 ${(file.size / 1024 / 1024).toFixed(1)}MB，超过 30MB`);
    }
    const bitmap = await createImageBitmap(file);
    const pixels = bitmap.width * bitmap.height;
    if (pixels < 512 * 512) throw new Error(`总像素 ${bitmap.width}×${bitmap.height} 过小，要求 ≥ 512×512`);
    if (pixels > 6000 * 6000) throw new Error(`总像素 ${bitmap.width}×${bitmap.height} 过大，要求 ≤ 6000×6000`);
    const ratio = bitmap.width / bitmap.height;
    if (ratio < 1 / 16 || ratio > 16) throw new Error('宽高比需在 1/16 ~ 16 之间');

    state.file = file;
    if (state.previewUrl) URL.revokeObjectURL(state.previewUrl);
    state.previewUrl = URL.createObjectURL(file);
    el.previewImg.src = state.previewUrl;
    el.previewName.textContent = file.name || '粘贴的图片';
    el.previewMeta.textContent = `${bitmap.width}×${bitmap.height} · ${(file.size / 1024).toFixed(0)}KB`;
    el.dzEmpty.hidden = true;
    el.dzPreview.hidden = false;
    bitmap.close?.();
  } catch (error) {
    toast(error.message || '图片预检失败');
  }
}

/* ---------------- 发起请求 ---------------- */

function setBusy(busy) {
  el.runBtn.disabled = busy;
  el.runSpinner.hidden = !busy;
  el.cancelBtn.hidden = !busy;
  el.runTimer.hidden = !busy;
  el.runLabel.textContent = busy
    ? '处理中…'
    : state.mode === 'upload' ? '开始拆分' : '生成并拆分';
}

function startTimer() {
  state.startedAt = Date.now();
  el.runTimer.textContent = '0s';
  state.timerId = setInterval(() => {
    const seconds = Math.floor((Date.now() - state.startedAt) / 1000);
    el.runTimer.textContent = seconds < 60
      ? `${seconds}s`
      : `${Math.floor(seconds / 60)}m${String(seconds % 60).padStart(2, '0')}s`;
  }, 500);
}

function stopTimer() {
  if (state.timerId) clearInterval(state.timerId);
  state.timerId = null;
}

async function run() {
  if (state.busy) return;
  if (!state.config.key) {
    openDrawer(el.configDrawer);
    toast('请先在「配置」里填写 Agent Plan 专属 API Key');
    return;
  }

  state.busy = true;
  state.controller = new AbortController();
  setBusy(true);
  startTimer();

  try {
    if (state.mode === 'upload') {
      await runDecompose();
    } else {
      await runGenerate();
    }
  } catch (error) {
    if (error.name === 'AbortError') {
      toast('已取消本地请求（方舟侧任务可能仍在执行）');
    } else {
      toast(error.message || '请求失败');
      if (error.payload) console.warn('[seedream-layers] 上游响应', error.payload);
    }
  } finally {
    state.busy = false;
    state.controller = null;
    stopTimer();
    setBusy(false);
  }
}

function commonForm() {
  const maxImages = Number(el.maxImages.value) || 0;
  const fields = {
    baseUrl: state.config.baseUrl,
    baseKey: state.config.key,
    model: state.config.model,
    size: el.sizeSelect.value,
    watermark: el.watermark.checked ? 'true' : 'false',
  };
  if (maxImages > 0) fields.maxImages = String(maxImages);
  return fields;
}

async function runDecompose() {
  if (!state.file) {
    toast('请先上传一张图片');
    return;
  }
  const form = new FormData();
  form.append('image', state.file, state.file.name || 'upload.png');
  Object.entries(commonForm()).forEach(([key, value]) => form.append(key, value));
  const layerPrompt = el.layerPrompt.value.trim();
  if (layerPrompt) form.append('prompt', layerPrompt);

  const job = await request('/api/decompose', { method: 'POST', body: form });
  applyJob(job);
  announceJob(job);
}

async function runGenerate() {
  const prompt = el.promptInput.value.trim();
  if (!prompt) {
    toast('请先填写生成提示词');
    return;
  }
  const body = {
    baseUrl: state.config.baseUrl,
    baseKey: state.config.key,
    model: state.config.model,
    prompt,
    layerPrompt: el.layerPrompt.value.trim(),
    size: el.sizeSelect.value,
    watermark: el.watermark.checked,
    maxImages: Number(el.maxImages.value) || 0,
  };
  const job = await request('/api/generate', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  applyJob(job);
  announceJob(job);
}

function announceJob(job) {
  const layers = job.layers?.length || 0;
  const tokens = job.usage?.output_tokens ? ` · ${job.usage.output_tokens} tokens` : '';
  toast(`拆分完成：${layers} 个图层 + 1 张底图 · ${state.canvas.w}×${state.canvas.h}${tokens}`, 'success');
}

/* ---------------- 渲染 ---------------- */

function applyJob(job) {
  if (!job || !job.canvas || !job.canvas.w) throw new Error('返回结果缺少画布尺寸');

  state.job = job;
  state.jobId = job.job_id;
  state.canvas = { w: job.canvas.w, h: job.canvas.h };
  state.base = { file: job.base.file, visible: true, el: null, size: job.base.size };
  state.layers = (job.layers || []).map((layer, index) => {
    const bbox = layer.bbox?.absolute || null;
    const width = bbox ? bbox[2] - bbox[0] : state.canvas.w;
    const height = bbox ? bbox[3] - bbox[1] : state.canvas.h;
    const item = {
      id: layer.id || `l${index + 1}`,
      name: layer.name || `图层 ${index + 1}`,
      description: layer.description || '',
      alignedFile: layer.aligned_file,
      file: layer.file,
      size: layer.size || '',
      x: bbox ? bbox[0] : 0,
      y: bbox ? bbox[1] : 0,
      w: Math.max(1, width),
      h: Math.max(1, height),
      opacity: 1,
      visible: true,
      order: layer.z_index || index + 1,
      el: null,
    };
    item.origin = { x: item.x, y: item.y, w: item.w, h: item.h };
    return item;
  }).sort((a, b) => a.order - b.order);

  normalizeOrders();
  state.selected = null;

  el.emptyState.hidden = true;
  el.sizer.hidden = false;
  el.toolbar.hidden = false;
  el.layerPanel.hidden = false;
  el.inspector.hidden = true;
  el.jobBadge.textContent = `${state.jobId} · ${state.canvas.w}×${state.canvas.h}`;

  renderBoard();
  renderLayerList();
  fitZoom();
}

function renderBoard() {
  el.board.innerHTML = '';
  el.board.style.width = `${state.canvas.w}px`;
  el.board.style.height = `${state.canvas.h}px`;

  const baseImg = document.createElement('img');
  baseImg.className = 'base-img';
  baseImg.src = fileUrl(state.base.file);
  baseImg.draggable = false;
  baseImg.alt = '';
  el.board.appendChild(baseImg);
  state.base.el = baseImg;
  applyBaseVisibility();

  state.layers.forEach((layer) => {
    const box = createLayerElement(layer);
    el.board.appendChild(box);
  });
}

function createLayerElement(layer) {
  const box = document.createElement('div');
  box.className = 'layer-box';

  const img = document.createElement('img');
  img.src = fileUrl(layer.file);
  img.alt = layer.name;
  img.draggable = false;
  box.appendChild(img);

  const handle = document.createElement('div');
  handle.className = 'handle';
  box.appendChild(handle);

  box.addEventListener('pointerdown', (event) => onLayerPointerDown(event, layer));
  handle.addEventListener('pointerdown', (event) => onResizePointerDown(event, layer));
  box.addEventListener('dblclick', (event) => {
    event.stopPropagation();
    resetLayer(layer);
  });

  // 先挂到 layer 上，positionLayer 依赖 layer.el 才能写入几何样式
  layer.el = box;
  positionLayer(layer);
  box.style.opacity = String(layer.opacity);
  box.style.zIndex = String(layer.order);
  box.classList.toggle('hidden', !layer.visible);
  return box;
}

function positionLayer(layer) {
  if (!layer.el) return;
  layer.el.style.left = `${layer.x}px`;
  layer.el.style.top = `${layer.y}px`;
  layer.el.style.width = `${layer.w}px`;
  layer.el.style.height = `${layer.h}px`;
}

function onLayerPointerDown(event, layer) {
  if (event.button !== 0) return;
  if (event.target.classList.contains('handle')) return;
  event.stopPropagation();
  selectLayer(layer);

  const element = layer.el;
  const pointerId = event.pointerId;
  const startClientX = event.clientX;
  const startClientY = event.clientY;
  const startX = layer.x;
  const startY = layer.y;

  element.setPointerCapture(pointerId);

  const move = (moveEvent) => {
    if (moveEvent.pointerId !== pointerId) return;
    layer.x = Math.round(startX + (moveEvent.clientX - startClientX) / state.zoom);
    layer.y = Math.round(startY + (moveEvent.clientY - startClientY) / state.zoom);
    positionLayer(layer);
    updateInspector();
  };
  const finish = () => {
    element.removeEventListener('pointermove', move);
    element.removeEventListener('pointerup', finish);
    element.removeEventListener('pointercancel', finish);
    if (element.hasPointerCapture(pointerId)) element.releasePointerCapture(pointerId);
  };
  element.addEventListener('pointermove', move);
  element.addEventListener('pointerup', finish);
  element.addEventListener('pointercancel', finish);
}

function onResizePointerDown(event, layer) {
  if (event.button !== 0) return;
  event.stopPropagation();
  event.preventDefault();
  selectLayer(layer);

  const element = layer.el;
  const pointerId = event.pointerId;
  const startClientX = event.clientX;
  const startClientY = event.clientY;
  const startW = layer.w;
  const startH = layer.h;
  const aspect = startW / Math.max(1, startH);

  element.setPointerCapture(pointerId);

  const move = (moveEvent) => {
    if (moveEvent.pointerId !== pointerId) return;
    const dx = (moveEvent.clientX - startClientX) / state.zoom;
    const dy = (moveEvent.clientY - startClientY) / state.zoom;
    const delta = Math.max(dx, dy);
    const nextWidth = Math.max(24, startW + delta);
    layer.w = Math.round(nextWidth);
    layer.h = Math.round(nextWidth / aspect);
    positionLayer(layer);
    updateInspector();
  };
  const finish = () => {
    element.removeEventListener('pointermove', move);
    element.removeEventListener('pointerup', finish);
    element.removeEventListener('pointercancel', finish);
    if (element.hasPointerCapture(pointerId)) element.releasePointerCapture(pointerId);
  };
  element.addEventListener('pointermove', move);
  element.addEventListener('pointerup', finish);
  element.addEventListener('pointercancel', finish);
}

function applyZoom(next) {
  if (typeof next === 'number' && Number.isFinite(next)) {
    state.zoom = clamp(next, 0.05, 8);
  }
  el.board.style.transform = `scale(${state.zoom})`;
  el.sizer.style.width = `${state.canvas.w * state.zoom}px`;
  el.sizer.style.height = `${state.canvas.h * state.zoom}px`;
  el.zoomLabel.textContent = `${Math.round(state.zoom * 100)}%`;
}

function fitZoom() {
  if (!state.canvas.w) return;
  const width = el.canvasArea.clientWidth - 56;
  const height = el.canvasArea.clientHeight - 56;
  const zoom = Math.min(width / state.canvas.w, height / state.canvas.h, 1);
  applyZoom(Math.max(0.03, zoom));
}

/* ---------------- 图层列表 ---------------- */

function renderLayerList() {
  el.layerList.innerHTML = '';

  if (state.base) {
    el.layerList.appendChild(createBaseItem());
  }

  [...state.layers].sort((a, b) => b.order - a.order).forEach((layer) => {
    el.layerList.appendChild(createLayerItem(layer));
  });

  el.layerCount.textContent = `${state.layers.length + 1} 层`;
}

function createBaseItem() {
  const item = document.createElement('div');
  item.className = 'layer-item';
  item.dataset.base = '1';

  const thumb = document.createElement('img');
  thumb.className = 'layer-thumb';
  thumb.src = fileUrl(state.base.file);
  thumb.alt = '背景';

  const info = document.createElement('div');
  info.className = 'layer-info';
  info.innerHTML = `
    <span class="layer-name">背景</span>
    <span class="layer-desc">底图</span>
    <span class="layer-meta">z_index 0 · ${state.base.size || ''}</span>
  `;

  const actions = document.createElement('div');
  actions.className = 'layer-actions';
  const eye = document.createElement('button');
  eye.textContent = state.base.visible ? '◉' : '◌';
  eye.title = '显示 / 隐藏';
  item.classList.toggle('dimmed', !state.base.visible);
  eye.addEventListener('click', (event) => {
    event.stopPropagation();
    state.base.visible = !state.base.visible;
    applyBaseVisibility();
    eye.textContent = state.base.visible ? '◉' : '◌';
    item.classList.toggle('dimmed', !state.base.visible);
  });
  actions.appendChild(eye);

  item.append(thumb, info, actions);
  return item;
}

function createLayerItem(layer) {
  const item = document.createElement('div');
  item.className = 'layer-item' + (state.selected === layer ? ' active' : '');
  item.classList.toggle('dimmed', !layer.visible);

  const thumb = document.createElement('img');
  thumb.className = 'layer-thumb';
  thumb.src = fileUrl(layer.file);
  thumb.alt = layer.name;

  const info = document.createElement('div');
  info.className = 'layer-info';

  const name = document.createElement('input');
  name.className = 'layer-name';
  name.value = layer.name;
  name.spellcheck = false;
  name.addEventListener('click', (event) => event.stopPropagation());
  name.addEventListener('pointerdown', (event) => event.stopPropagation());
  name.addEventListener('change', () => {
    layer.name = name.value.trim() || `图层 ${layer.order}`;
  });

  const desc = document.createElement('span');
  desc.className = 'layer-desc';
  desc.textContent = layer.description || '—';
  desc.title = layer.description || '';

  const meta = document.createElement('span');
  meta.className = 'layer-meta';
  meta.textContent = `z ${layer.order} · ${layer.size}`;

  info.append(name, desc, meta);

  const actions = document.createElement('div');
  actions.className = 'layer-actions';

  const eye = document.createElement('button');
  eye.textContent = layer.visible ? '◉' : '◌';
  eye.title = '显示 / 隐藏';
  eye.addEventListener('click', (event) => {
    event.stopPropagation();
    layer.visible = !layer.visible;
    layer.el?.classList.toggle('hidden', !layer.visible);
    eye.textContent = layer.visible ? '◉' : '◌';
    item.classList.toggle('dimmed', !layer.visible);
  });

  const up = document.createElement('button');
  up.textContent = '↑';
  up.title = '上移一层';
  up.addEventListener('click', (event) => {
    event.stopPropagation();
    moveLayer(layer, 1);
  });

  const down = document.createElement('button');
  down.textContent = '↓';
  down.title = '下移一层';
  down.addEventListener('click', (event) => {
    event.stopPropagation();
    moveLayer(layer, -1);
  });

  const remove = document.createElement('button');
  remove.textContent = '×';
  remove.title = '移除该图层（仅本次会话）';
  remove.addEventListener('click', (event) => {
    event.stopPropagation();
    removeLayer(layer);
  });

  actions.append(eye, up, down, remove);

  item.append(thumb, info, actions);
  item.addEventListener('click', () => selectLayer(layer));
  return item;
}

function applyBaseVisibility() {
  if (!state.base?.el) return;
  state.base.el.style.display = state.base.visible ? '' : 'none';
}

function selectLayer(layer) {
  state.selected = layer;

  $$('.layer-box', el.board).forEach((box) => box.classList.remove('selected'));
  $$('.layer-item', el.layerList).forEach((item) => item.classList.remove('active'));

  if (layer) {
    layer.el?.classList.add('selected');
    const index = [...state.layers].sort((a, b) => b.order - a.order).indexOf(layer);
    const item = $$('.layer-item', el.layerList).filter((node) => !node.dataset.base)[index];
    item?.classList.add('active');
    item?.scrollIntoView({ block: 'nearest' });
    el.inspector.hidden = false;
    updateInspector();
  } else {
    el.inspector.hidden = true;
  }
}

function updateInspector() {
  const layer = state.selected;
  if (!layer) return;
  el.insName.textContent = layer.name;
  el.insGeo.textContent = `x ${layer.x} · y ${layer.y} · ${layer.w}×${layer.h}`;
  el.opacityRange.value = String(Math.round(layer.opacity * 100));
  el.opacityValue.textContent = `${Math.round(layer.opacity * 100)}%`;
}

function resetLayer(layer) {
  if (!layer?.origin) return;
  layer.x = layer.origin.x;
  layer.y = layer.origin.y;
  layer.w = layer.origin.w;
  layer.h = layer.origin.h;
  positionLayer(layer);
  updateInspector();
}

function resetAllLayers() {
  state.layers.forEach(resetLayer);
  toast('已还原所有图层到模型原始位置', 'success');
}

function toggleAllLayers() {
  const anyVisible = state.base.visible || state.layers.some((layer) => layer.visible);
  state.base.visible = !anyVisible;
  state.layers.forEach((layer) => {
    layer.visible = !anyVisible;
    layer.el?.classList.toggle('hidden', !layer.visible);
  });
  applyBaseVisibility();
  renderLayerList();
}

function moveLayer(layer, direction) {
  const sorted = [...state.layers].sort((a, b) => a.order - b.order);
  const index = sorted.indexOf(layer);
  const target = index + direction;
  if (target < 0 || target >= sorted.length) return;
  [sorted[index], sorted[target]] = [sorted[target], sorted[index]];
  sorted.forEach((item, order) => {
    item.order = order + 1;
    if (item.el) item.el.style.zIndex = String(item.order);
  });
  state.layers = sorted;
  renderLayerList();
  selectLayer(layer);
}

function removeLayer(layer) {
  const index = state.layers.indexOf(layer);
  if (index === -1) return;
  layer.el?.remove();
  state.layers.splice(index, 1);
  if (state.selected === layer) {
    state.selected = null;
    el.inspector.hidden = true;
  }
  normalizeOrders();
  renderLayerList();
  if (state.layers.length === 0) toast('所有图层已移除，可重新拆分');
}

function normalizeOrders() {
  state.layers.sort((a, b) => a.order - b.order);
  state.layers.forEach((layer, index) => {
    layer.order = index + 1;
    if (layer.el) layer.el.style.zIndex = String(layer.order);
  });
}

/* ---------------- 导出 ---------------- */

async function exportComposite() {
  if (!state.jobId) return;
  try {
    toast('正在合成 PNG…', 'success');
    const width = state.canvas.w;
    const height = state.canvas.h;
    const canvas = document.createElement('canvas');
    canvas.width = width;
    canvas.height = height;
    const context = canvas.getContext('2d');

    if (state.base.visible) {
      const baseImage = await loadImage(fileUrl(state.base.file));
      context.drawImage(baseImage, 0, 0, width, height);
    }

    const sorted = [...state.layers].sort((a, b) => a.order - b.order);
    for (const layer of sorted) {
      if (!layer.visible) continue;
      const image = await loadImage(fileUrl(layer.file));
      context.globalAlpha = layer.opacity;
      context.drawImage(image, layer.x, layer.y, layer.w, layer.h);
    }
    context.globalAlpha = 1;

    const blob = await new Promise((resolve) => canvas.toBlob(resolve, 'image/png'));
    if (!blob) throw new Error('合成失败');
    downloadBlob(blob, `seedream-layers-${state.jobId}.png`);
    toast('合成图已下载', 'success');
  } catch (error) {
    toast(`合成失败：${error.message}`);
  }
}

async function exportZip() {
  if (!state.jobId) return;
  try {
    toast('正在打包分层图片…', 'success');
    const response = await fetch(`/api/export/zip?job=${encodeURIComponent(state.jobId)}`);
    if (!response.ok) {
      const text = await response.text();
      let payload = null;
      try { payload = JSON.parse(text); } catch { payload = text; }
      throw new Error(messageOf(payload, response.status));
    }
    const blob = await response.blob();
    downloadBlob(blob, `seedream-layers-${state.jobId}.zip`);
    toast('分层包已下载（含 composite.png / layers/ / source/ / layers.json / viewer.html）', 'success');
  } catch (error) {
    toast(`打包失败：${error.message}`);
  }
}

/* ---------------- 历史任务 ---------------- */

async function loadHistory() {
  const container = $('#historyList');
  container.innerHTML = '<p class="tip">加载中…</p>';
  try {
    const data = await request('/api/jobs');
    const jobs = data?.jobs || [];
    if (!jobs.length) {
      container.innerHTML = '<p class="tip">还没有历史任务。拆一张图试试。</p>';
      return;
    }
    container.innerHTML = '';
    jobs.forEach((job) => {
      const item = document.createElement('div');
      item.className = 'history-item';

      const thumb = document.createElement('img');
      thumb.alt = '';
      if (job.thumb) {
        const segments = String(job.thumb).split('/').filter(Boolean).map(encodeURIComponent);
        thumb.src = `/files/${encodeURIComponent(job.job_id)}/${segments.join('/')}`;
      }

      const meta = document.createElement('div');
      meta.className = 'history-meta';
      const date = job.created_at ? job.created_at.replace('T', ' ').slice(0, 19) : '';
      meta.innerHTML = `
        <b>${job.job_id}</b>
        <small>${date} · ${job.layer_count} 层 · ${job.canvas?.w || '?'}×${job.canvas?.h || '?'}</small>
        <small>${job.model || ''}</small>
      `;

      item.append(thumb, meta);
      item.addEventListener('click', async () => {
        try {
          const result = await request(`/api/jobs/${encodeURIComponent(job.job_id)}`);
          applyJob(result);
          closeDrawers();
          toast(`已载入任务 ${job.job_id}`, 'success');
        } catch (error) {
          toast(error.message);
        }
      });
      container.appendChild(item);
    });
  } catch (error) {
    container.innerHTML = `<p class="tip">加载失败：${error.message}</p>`;
  }
}

boot();
