// ================================================================
//  DRONE OPS · MISSION CONTROL  — Cesium 1.118
// ================================================================

// Глобальный ловец ошибок, чтобы увидеть boot-fail прямо на экране.
window.addEventListener('error', (e) => {
  const el = document.getElementById('boot-error');
  if (el && !el.textContent) {
    el.textContent = `BOOT ERROR\n${e.message}\n${e.filename}:${e.lineno}`;
    el.classList.remove('hidden');
  }
});

if (typeof Cesium === 'undefined') {
  document.getElementById('boot-error').textContent =
    'Cesium CDN не загрузился. Проверь интернет или смени CDN в index.html.';
  document.getElementById('boot-error').classList.remove('hidden');
  throw new Error('Cesium not loaded');
}

Cesium.Ion.defaultAccessToken = '';

// ---------- Viewer (Cesium 1.118 API) ----------
// OpenStreetMapImageryProvider удалён в Cesium 1.110 — используем UrlTemplateImageryProvider.
// Standard OSM tiles + CSS-фильтр (invert + hue-rotate) → dark look, без токенов и CDN-сюрпризов.
const darkTiles = new Cesium.UrlTemplateImageryProvider({
  url: 'https://tile.openstreetmap.org/{z}/{x}/{y}.png',
  credit: 'OpenStreetMap',
  maximumLevel: 19,
});
document.body.classList.add('light-map');

const viewer = new Cesium.Viewer('cesium', {
  imageryProvider: false,
  baseLayerPicker: false,
  timeline: false,
  animation: false,
  geocoder: false,
  homeButton: false,
  navigationHelpButton: false,
  sceneModePicker: false,
  fullscreenButton: false,
  infoBox: false,
  selectionIndicator: false,
  shouldAnimate: true,
});
viewer.imageryLayers.addImageryProvider(darkTiles);

viewer.scene.globe.enableLighting = false;
viewer.scene.globe.baseColor = Cesium.Color.fromCssColorString('#f3f4ee');
viewer.scene.backgroundColor = Cesium.Color.fromCssColorString('#f3f4ee');
viewer.scene.skyBox.show = false;
viewer.scene.sun.show = false;
viewer.scene.moon.show = false;
viewer.scene.skyAtmosphere.show = false;
viewer.scene.fog.enabled = false;

// Момент загрузки страницы — для UPTIME в шапке (T+ЧЧ:ММ сессии).
const SESSION_START = Date.now();

// =================================================================
// STATE
// =================================================================
const state = {
  drones: new Map(),
  missions: new Map(),
  sessionMissions: new Map(),  // ВСЕ миссии сессии (вкл. завершённые) для вкладки Missions
  trajectories: new Map(),
  entities: {
    base: null,
    drones: new Map(),
    leaderLines: new Map(),
    shadows: new Map(),
    trails: new Map(),
    waypoints: new Map(),
    missionPaths: new Map(),
  },
  base: { lat: 43.0747, lon: -89.3842 },
  // Множество ID завершённых миссий — считаем по уникальным id, а не инкрементом:
  // COMPLETED мог доставляться несколько раз (QoS/реконнект) и накручивал счётчик.
  completedMissions: new Set(),
  pickingFor: null,
  wsConnected: false,
  settings: null,             // загружается из localStorage в init
  drawings: [],               // нарисованные пользователем объекты
  drawingTool: null,          // активный tool: 'koz'|'threat'|'poi'|'route'|'measure'|null
  drawingPoints: [],          // буфер точек для текущего рисования
  drawingDragCircle: null,    // {center: [lat,lon], radiusM} для Threat tool
  // === Mission draft (in-progress new-mission form) ===
  missionPicking: null,       // 'route'|'sector' или null
  missionDraft: {             // захваченные при PICK данные
    route: null,              // массив [[lat,lon], ...] (≥2) для Patrol
    sector: null,             // массив [[lat,lon], ...] (≥3) для Sector observation
  },
};

// =================================================================
// LOCAL STORAGE PERSISTENCE
// Один ключ хранит всё: settings + drawings.
// Версия v1 — на случай миграций в будущем.
// =================================================================
const STORE_KEY = 'skybite.state.v1';

const SETTINGS_DEFAULTS = {
  // HUD · default ON
  show_grid: true,
  show_doc_frame: true,
  show_drone_trails: true,
  show_drone_shadows: true,
  // HUD · default OFF
  show_cursor_mgrs: false,
  show_compass: false,
  show_legend: false,
  show_narrow_scale: false,
  show_sandbox: false,
  // Panels · default ON
  show_taskings: true,
  show_gantt: true,
  show_ptt: false,            // PTT Comms — нерабочая панель, выключена из базового конфига
  show_event_log: true,
  show_doc_foot: true,
  // Panels · default OFF
  show_schematic: false,
  // Map / display
  map_filter: 55,             // intensity 0..100 → mapping в filter()
  compact_mode: false,
};

// Bump this when SETTINGS_DEFAULTS-семантика существенно меняется и старые
// сохранённые preferences становятся неактуальными. Старая запись без поля
// settings_version форсится на дефолт (с сохранением drawings).
const SETTINGS_VERSION = 2;

function loadStore() {
  try {
    const raw = localStorage.getItem(STORE_KEY);
    if (!raw) return { settings: { ...SETTINGS_DEFAULTS, settings_version: SETTINGS_VERSION }, drawings: [], missionDraft: null, missionForm: null, modalOpen: false };
    const obj = JSON.parse(raw);
    const savedVer = (obj.settings && obj.settings.settings_version) || 0;
    if (savedVer < SETTINGS_VERSION) {
      return {
        settings: { ...SETTINGS_DEFAULTS, settings_version: SETTINGS_VERSION },
        drawings: Array.isArray(obj.drawings) ? obj.drawings : [],
        missionDraft: null,
        missionForm: null,
        modalOpen: false,
      };
    }
    return {
      settings: { ...SETTINGS_DEFAULTS, ...(obj.settings || {}), settings_version: SETTINGS_VERSION },
      drawings: Array.isArray(obj.drawings) ? obj.drawings : [],
      missionDraft: obj.missionDraft || null,
      missionForm: obj.missionForm || null,
      modalOpen: !!obj.modalOpen,
      selectedDroneId: obj.selectedDroneId || null,
    };
  } catch (e) {
    console.warn('[settings] load failed, using defaults:', e);
    return { settings: { ...SETTINGS_DEFAULTS, settings_version: SETTINGS_VERSION }, drawings: [], missionDraft: null, missionForm: null, modalOpen: false };
  }
}
function saveStore() {
  try {
    // missionDraft и форма сохраняются вместе с settings/drawings, чтобы
    // в-процессе-создания миссия не терялась при перезагрузке страницы.
    const modalEl = document.getElementById('modal-bg');
    const modalOpen = modalEl ? !modalEl.classList.contains('hidden') : false;
    const missionForm = collectMissionForm();
    localStorage.setItem(STORE_KEY, JSON.stringify({
      settings: state.settings,
      drawings: state.drawings,
      missionDraft: state.missionDraft || null,
      missionForm,
      modalOpen,
      selectedDroneId: state.selectedDroneId || null,
    }));
  } catch (e) { console.warn('[settings] save failed:', e); }
}

// Собирает текущие значения modal-полей в plain object — для persistence
function collectMissionForm() {
  const ids = [
    'm-drone', 'm-type',
    'm-cruise-alt', 'm-takeoff-alt', 'm-takeoff-profile',
    'm-auto-rth',
    'm-notes',
    // delivery
    'from-lat', 'from-lon', 'to-lat', 'to-lon',
    // ISR
    'isr-lat', 'isr-lon', 'm-loiter-s', 'm-orbit-radius',
    // patrol
    'm-loops', 'm-loiter-wp',
    // sector
    'm-n-drones', 'm-swath-sec', 'm-sec-loops',
  ];
  const out = {};
  for (const id of ids) {
    const el = document.getElementById(id);
    if (!el) continue;
    out[id] = (el.type === 'checkbox') ? el.checked : el.value;
  }
  // seg buttons
  const prio = document.querySelector('#m-priority-seg .seg-mini-btn.active');
  if (prio) out['_priority'] = prio.dataset.priority;
  const tseg = document.querySelector('#m-type-seg .seg-mini-btn.active');
  if (tseg) out['_type'] = tseg.dataset.type;
  const psec = document.querySelector('#m-pattern-sec-seg .seg-mini-btn.active');
  if (psec) out['_pattern_sec'] = psec.dataset.pattern;
  return out;
}

// Восстанавливает поля modal'а из сохранённой формы
function restoreMissionForm(form) {
  if (!form) return;
  for (const [id, val] of Object.entries(form)) {
    if (id.startsWith('_')) continue;
    const el = document.getElementById(id);
    if (!el) continue;
    if (el.type === 'checkbox') el.checked = !!val;
    else el.value = val;
  }
  // priority seg
  if (form._priority) {
    document.querySelectorAll('#m-priority-seg .seg-mini-btn').forEach((b) => {
      b.classList.toggle('active', b.dataset.priority === form._priority);
    });
  }
  // type seg → also need to show/hide type-fields
  if (form._type) {
    document.querySelectorAll('#m-type-seg .seg-mini-btn').forEach((b) => {
      b.classList.toggle('active', b.dataset.type === form._type);
    });
    const sel = document.getElementById('m-type');
    if (sel) sel.value = form._type;
    document.querySelectorAll('.type-fields[data-type-show]').forEach((el) => {
      el.classList.toggle('hidden', el.dataset.typeShow !== form._type);
    });
  }
  // pattern segs
  if (form._pattern_sec) {
    document.querySelectorAll('#m-pattern-sec-seg .seg-mini-btn').forEach((b) => {
      b.classList.toggle('active', b.dataset.pattern === form._pattern_sec);
    });
  }
}

const COLOR = {
  IDLE: '#6b7280',
  ACTIVE: '#1d4ed8',
  FLYING: '#1d4ed8',
  TAKEOFF: '#b45309',
  MISSION: '#1d4ed8',
  IN_PROGRESS: '#1d4ed8',
  STARTED: '#1d4ed8',
  LANDING: '#b45309',
  LAND: '#b45309',
  COMPLETED: '#166534',
  ERROR: '#b91c1c',
  ABORTED: '#b91c1c',
  PLANNED: '#6b7280',
  ASSIGNED: '#6b7280',
  UPLOADED: '#6b7280',
};
const statusColor = (s) => COLOR[s] || '#6b7280';

// =================================================================
// INIT
// =================================================================
async function init() {
  // Загружаем settings + drawings + черновик миссии из localStorage.
  const s = loadStore();
  state.settings = s.settings;
  state.drawings = s.drawings;
  if (s.missionDraft) state.missionDraft = s.missionDraft;
  if (s.selectedDroneId) state.selectedDroneId = s.selectedDroneId;
  state._pendingMissionForm = s.missionForm || null;
  state._pendingModalOpen = !!s.modalOpen;
  applySettings();
  applyPopoutMode();

  try {
    const res = await fetch('/api/base');
    const b = await res.json();
    if (b && b.lat && b.lon) state.base = { lat: +b.lat, lon: +b.lon };
  } catch (e) { /* ignore */ }

  // Mission context — питает шапку (operation, sector, datalink, operator, certs).
  try {
    const r = await fetch('/api/mission_context');
    state.missionCtx = await r.json();
  } catch (e) {
    state.missionCtx = null;
  }
  applyMissionContext();

  // MODE — режим системы для шапки. test → SIMULATION (понятнее оператору).
  try {
    const mr = await fetch('/api/system/mode');
    const mj = await mr.json();
    const MODE_LABEL = { test: 'SIMULATION', preflight: 'PREFLIGHT', full: 'FULL' };
    const mraw = String(mj.mode || '').toLowerCase();
    setText('dh-mode', MODE_LABEL[mraw] || (mraw ? mraw.toUpperCase() : '—'));
  } catch (e) { setText('dh-mode', '—'); }

  // Стартовые значения шапки до первых обновлений.
  setText('kpi-objects', String((state.drawings || []).length));

  drawBase();
  flyToBase(0);

  // SVG overlay: AOR rings, KOZ, threats, scale bar
  initGeoOverlay();

  setInterval(refreshFleet, 2000);
  setInterval(refreshMissions, 1500);
  setInterval(tickClock, 1000);
  setInterval(refreshBlackBox, 3000);          // SHA-256 над event-логом для footer
  setInterval(renderPttBar, 5000);             // PTT каналы — пересчёт когда меняется флот
  setInterval(updateAirframeSchematic, 800);   // RPM-callouts «дышат» для выбранного дрона
  tickClock();
  renderPttBar();
  refreshBlackBox();
  updateAirframeSchematic();

  connectWS();
  wireUI();
  wireSettings();
  initDrawingTools();
  refreshFleet();
  restoreSessionSnapshot();   // вернуть вейпоинты и траектории после перезагрузки страницы

  // После полной инициализации: восстанавливаем form-fields и (если modal был открыт) открываем modal.
  setTimeout(() => {
    if (state._pendingMissionForm) restoreMissionForm(state._pendingMissionForm);
    if (state._pendingModalOpen) openModal();
    // Auto-save form changes на input/change events
    wireMissionFormAutosave();
  }, 100);
}

// =================================================================
// APPLY SETTINGS — навешивает body-классы для управления видимостью
// + меняет CSS-фильтр Cesium-канваса по слайдеру
// =================================================================
function applySettings() {
  const s = state.settings || SETTINGS_DEFAULTS;
  const body = document.body;

  // helper: если visible=false → добавляем "no-X" класс
  const t = (visible, cls) => body.classList.toggle('no-' + cls, !visible);
  t(s.show_cursor_mgrs, 'cursor-mgrs');
  t(s.show_compass, 'compass');
  t(s.show_legend, 'legend');
  t(s.show_grid, 'grid');
  t(s.show_narrow_scale, 'narrow-scale');
  t(s.show_doc_frame, 'doc-frame');
  t(s.show_taskings, 'taskings');
  t(s.show_gantt, 'gantt');
  t(false, 'ptt');  // PTT Comms принудительно скрыта (нерабочая), игнорируем сохранённый pref
  t(s.show_event_log, 'event-log');
  t(s.show_schematic, 'schematic');
  t(s.show_doc_foot, 'doc-foot');
  t(s.show_sandbox, 'sandbox');
  body.classList.toggle('compact', !!s.compact_mode);

  // sandbox-dock: если виден — снять hidden
  const dock = document.getElementById('sandbox-dock-bottom');
  if (dock) dock.classList.toggle('hidden', !s.show_sandbox);

  // map filter — преобразуем 0..100 в реальный CSS-filter
  // 0 = чистая картинка OSM, 100 = максимальная "бумажность"
  const v = Math.max(0, Math.min(100, Number(s.map_filter) || 0));
  const grayscale = (v / 100 * 0.85).toFixed(2);
  const saturate = (1 - v / 100 * 0.55).toFixed(2);
  const brightness = (1 + v / 100 * 0.04).toFixed(2);
  const contrast = (1 - v / 100 * 0.06).toFixed(2);
  // Используем style-element, чтобы перебить статический фильтр
  let styleEl = document.getElementById('map-filter-style');
  if (!styleEl) {
    styleEl = document.createElement('style');
    styleEl.id = 'map-filter-style';
    document.head.appendChild(styleEl);
  }
  styleEl.textContent = `.light-map .cesium-widget canvas { filter: grayscale(${grayscale}) saturate(${saturate}) brightness(${brightness}) contrast(${contrast}); }`;

  // Cesium drone trails / shadows — toggle visibility
  if (typeof viewer !== 'undefined' && viewer && state.entities) {
    state.entities.trails.forEach((e) => { if (e) e.show = !!s.show_drone_trails; });
    state.entities.shadows.forEach((e) => { if (e) e.show = !!s.show_drone_shadows; });
  }

  // Sync strip toggle .active state с settings
  document.querySelectorAll('.dh-toggle[data-vis]').forEach((btn) => {
    const k = btn.dataset.vis;
    btn.classList.toggle('active', !!s[k]);
  });
  // Sync slider value label
  const slVal = document.getElementById('dh-filter-val');
  if (slVal) slVal.textContent = s.map_filter;
  const slEl = document.querySelector('.dh-slider[data-vis-slider="map_filter"]');
  if (slEl && +slEl.value !== +s.map_filter) slEl.value = s.map_filter;
}

// =================================================================
// POP-OUT MODE — querystring ?popout=event-log|taskings|schematic
// Скрывает основной UI, оставляет только нужную секцию.
// =================================================================
function applyPopoutMode() {
  const params = new URLSearchParams(location.search);
  const p = params.get('popout');
  if (!p) return;
  const allowed = ['event-log', 'taskings', 'schematic'];
  if (!allowed.includes(p)) return;
  document.body.classList.add('popout-mode', 'popout-' + p);
  document.title = `Skybite · ${p.toUpperCase()} · pop-out`;

  // Force-снять "no-X" классы для нужной панели —
  // иначе если в settings галка выключена, popout будет пуст.
  const ensure = (cls) => document.body.classList.remove('no-' + cls);
  if (p === 'event-log') ensure('event-log');
  if (p === 'taskings') { ensure('taskings'); ensure('gantt'); }
  if (p === 'schematic') ensure('schematic');
}

// =================================================================
// WIRE SETTINGS — drawer open/close, checkbox handlers, slider, buttons
// =================================================================
function wireSettings() {
  const drawer = document.getElementById('settings-drawer');
  const btnOpen = document.getElementById('btn-settings');
  const btnClose = document.getElementById('sd-close');

  // ── Strip: group popover open/close ───────────────────────────
  document.querySelectorAll('[data-toggle-group]').forEach((btn) => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      const group = btn.closest('.dh-group');
      if (!group) return;
      const wasOpen = group.classList.contains('open');
      // close all other groups
      document.querySelectorAll('.dh-group.open').forEach((g) => g.classList.remove('open'));
      if (!wasOpen) group.classList.add('open');
    });
  });
  // Close popovers on outside click
  document.addEventListener('click', (e) => {
    if (!e.target.closest('.dh-group')) {
      document.querySelectorAll('.dh-group.open').forEach((g) => g.classList.remove('open'));
    }
  });
  // Esc closes any open popover
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
      document.querySelectorAll('.dh-group.open').forEach((g) => g.classList.remove('open'));
    }
  });

  // ── Strip: toggle-кнопки для visibility ───────────────────────
  document.querySelectorAll('.dh-toggle[data-vis]').forEach((btn) => {
    btn.classList.toggle('active', !!state.settings[btn.dataset.vis]);
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      const k = btn.dataset.vis;
      state.settings[k] = !state.settings[k];
      saveStore();
      applySettings();
    });
  });

  // ── Strip: pop-out buttons ─────────────────────────────────────
  document.querySelectorAll('.dh-toggle[data-popout]').forEach((btn) => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      const panel = btn.dataset.popout;
      const url = `/sim?popout=${encodeURIComponent(panel)}`;
      const w = panel === 'event-log' ? 720 : 760;
      const h = 760;
      window.open(url, `skybite-popout-${panel}`, `width=${w},height=${h},popup`);
      // Закрываем popover после клика
      const g = btn.closest('.dh-group');
      if (g) g.classList.remove('open');
    });
  });

  // ── Strip: map filter slider ───────────────────────────────────
  const sl = document.querySelector('.dh-slider[data-vis-slider="map_filter"]');
  const slVal = document.getElementById('dh-filter-val');
  if (sl) {
    sl.value = state.settings.map_filter;
    if (slVal) slVal.textContent = state.settings.map_filter;
    sl.addEventListener('input', () => {
      state.settings.map_filter = +sl.value;
      if (slVal) slVal.textContent = sl.value;
      applySettings();
    });
    sl.addEventListener('change', saveStore);
    // Не пропускать клик к outside-handler'у
    sl.addEventListener('click', (e) => e.stopPropagation());
    sl.addEventListener('mousedown', (e) => e.stopPropagation());
  }

  // ── Strip: Zoom-to-fit ──────────────────────────────────────────
  const zoomFleetStrip = document.getElementById('dh-zoom-fleet');
  if (zoomFleetStrip) zoomFleetStrip.addEventListener('click', (e) => {
    e.stopPropagation();
    zoomToFleet();
    const g = zoomFleetStrip.closest('.dh-group');
    if (g) g.classList.remove('open');
  });

  // ── Drawer (минимальный — drawings list + Export/Reset) ────────
  if (!drawer || !btnOpen) return;

  btnOpen.addEventListener('click', () => {
    drawer.classList.toggle('hidden');
    btnOpen.classList.toggle('active', !drawer.classList.contains('hidden'));
  });
  if (btnClose) btnClose.addEventListener('click', () => {
    drawer.classList.add('hidden');
    btnOpen.classList.remove('active');
  });

  const zoomBtn = document.getElementById('sd-zoom-fleet');
  if (zoomBtn) zoomBtn.addEventListener('click', zoomToFleet);

  // Export drawings + settings as JSON download
  const exportBtn = document.getElementById('sd-export');
  if (exportBtn) exportBtn.addEventListener('click', exportStore);

  // Reset to defaults (strip toggles + drawings)
  const resetBtn = document.getElementById('sd-reset');
  if (resetBtn) resetBtn.addEventListener('click', () => {
    if (!confirm('Reset all UI toggles to defaults and clear drawings?')) return;
    state.settings = { ...SETTINGS_DEFAULTS, settings_version: SETTINGS_VERSION };
    state.drawings = [];
    saveStore();
    applySettings();   // also syncs strip .active classes
    if (typeof drawingsRender === 'function') drawingsRender();
    if (typeof renderDrawingsList === 'function') renderDrawingsList();
  });

  // Clear drawings only
  const clearBtn = document.getElementById('sd-clear-drawings');
  if (clearBtn) clearBtn.addEventListener('click', () => {
    if (!state.drawings.length) return;
    if (!confirm(`Delete all ${state.drawings.length} drawn object(s)?`)) return;
    state.drawings = [];
    saveStore();
    if (typeof drawingsRender === 'function') drawingsRender();
    if (typeof renderDrawingsList === 'function') renderDrawingsList();
  });

  // Render drawings list (initially)
  if (typeof renderDrawingsList === 'function') renderDrawingsList();
}

function zoomToFleet() {
  const drones = [...state.drones.values()].filter((d) => d.lat != null && d.lon != null);
  if (drones.length === 0) {
    flyToBase();
    return;
  }
  const lats = drones.map((d) => d.lat);
  const lons = drones.map((d) => d.lon);
  const minLat = Math.min(...lats, state.base.lat);
  const maxLat = Math.max(...lats, state.base.lat);
  const minLon = Math.min(...lons, state.base.lon);
  const maxLon = Math.max(...lons, state.base.lon);
  const rect = Cesium.Rectangle.fromDegrees(minLon, minLat, maxLon, maxLat);
  viewer.camera.flyTo({ destination: rect, duration: 1.0 });
}

function exportStore() {
  const data = JSON.stringify({ settings: state.settings, drawings: state.drawings }, null, 2);
  const blob = new Blob([data], { type: 'application/json' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  const stamp = new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19);
  a.download = `sentinel-state-${stamp}.json`;
  document.body.appendChild(a);
  a.click();
  setTimeout(() => { URL.revokeObjectURL(url); a.remove(); }, 100);
}

// =================================================================
// DRAWING TOOLS
// 5 инструментов: KOZ (polygon) / Threat (drag-radius) / POI (click) /
//                 Route (polyline) / Measure (polyline без сохранения).
// Состояние ввода в state.drawingTool/Points/DragCircle.
// Сохранённые объекты в state.drawings (persists в localStorage).
// =================================================================

function initDrawingTools() {
  // Кнопки toolbar (теперь внутри DRAW popover'а)
  document.querySelectorAll('[data-draw-tool]').forEach((btn) => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      const tool = btn.dataset.drawTool;
      if (state.drawingTool === tool) {
        cancelDrawing();
      } else {
        startDrawing(tool);
      }
      // Закрываем popover после выбора tool'а — пользователь будет рисовать на карте
      const g = btn.closest('.dh-group');
      if (g) g.classList.remove('open');
    });
  });

  // Cesium handler для рисования
  const drawHandler = new Cesium.ScreenSpaceEventHandler(viewer.canvas);

  // LEFT_CLICK — добавить точку. Threat: 1й клик = центр, 2й клик = граница
  drawHandler.setInputAction((click) => {
    if (!state.drawingTool) return;
    const lla = canvasToLla(click.position);
    if (!lla) return;
    const t = state.drawingTool;
    if (t === 'koz' || t === 'route' || t === 'measure') {
      state.drawingPoints.push([lla.lat, lla.lon]);
      drawingsRenderPreview();
      updateMeasureReadout();
    } else if (t === 'poi') {
      // POI = single click → попап для свойств
      state.drawingPoints = [[lla.lat, lla.lon]];
      openPropertyPopup('poi');
    } else if (t === 'threat') {
      if (!state.drawingDragCircle) {
        // 1й клик — ставим центр, ждём 2го клика для радиуса
        state.drawingDragCircle = { center: [lla.lat, lla.lon], radiusM: 0 };
        showDrawHint('threat-2');
      } else {
        // 2й клик — граница
        const c = state.drawingDragCircle.center;
        state.drawingDragCircle.radiusM = haversineMeters(c[0], c[1], lla.lat, lla.lon);
        if (state.drawingDragCircle.radiusM >= 10) openPropertyPopup('threat');
        else { state.drawingDragCircle = null; showDrawHint('threat'); }
      }
      drawingsRenderPreview();
    }
  }, Cesium.ScreenSpaceEventType.LEFT_CLICK);

  // MOUSE_MOVE — для threat показываем preview-радиус "за курсором" между 1-м и 2-м кликом
  drawHandler.setInputAction((evt) => {
    if (state.drawingTool !== 'threat' || !state.drawingDragCircle) return;
    const lla = canvasToLla(evt.endPosition);
    if (!lla) return;
    const c = state.drawingDragCircle.center;
    state.drawingDragCircle.radiusM = haversineMeters(c[0], c[1], lla.lat, lla.lon);
    drawingsRenderPreview();
  }, Cesium.ScreenSpaceEventType.MOUSE_MOVE);

  // DOUBLE_CLICK — финиш polyline/polygon (для KOZ/Route)
  drawHandler.setInputAction(() => {
    if (!state.drawingTool) return;
    const t = state.drawingTool;
    if (t === 'koz' && state.drawingPoints.length >= 3) {
      openPropertyPopup('koz');
    } else if (t === 'route' && state.drawingPoints.length >= 2) {
      openPropertyPopup('route');
    } else if (t === 'measure') {
      cancelDrawing();
    }
  }, Cesium.ScreenSpaceEventType.LEFT_DOUBLE_CLICK);

  // ESC = отмена, Enter = финиш (для draw tools и mission pick)
  document.addEventListener('keydown', (e) => {
    // Mission picking — отдельный режим (route/area/sector)
    if (state.missionPicking) {
      if (e.key === 'Escape') { cancelMissionPick(); return; }
      if (e.key === 'Enter') { commitMissionPick(); return; }
    }
    if (!state.drawingTool) return;
    if (e.key === 'Escape') { cancelDrawing(); }
    else if (e.key === 'Enter') {
      const t = state.drawingTool;
      if (t === 'koz' && state.drawingPoints.length >= 3) openPropertyPopup('koz');
      else if (t === 'route' && state.drawingPoints.length >= 2) openPropertyPopup('route');
      else if (t === 'measure') cancelDrawing();
    }
  });

  // === Mission picking handlers (LEFT_CLICK + DBL_CLICK для route/area/sector) ===
  const missionPickHandler = new Cesium.ScreenSpaceEventHandler(viewer.canvas);
  missionPickHandler.setInputAction((click) => {
    if (!state.missionPicking) return;
    const lla = canvasToLla(click.position);
    if (!lla) return;
    state.drawingPoints.push([lla.lat, lla.lon]);
    drawingsRenderPreview();
    updateMissionPickHint();
  }, Cesium.ScreenSpaceEventType.LEFT_CLICK);

  missionPickHandler.setInputAction(() => {
    if (!state.missionPicking) return;
    commitMissionPick();
  }, Cesium.ScreenSpaceEventType.LEFT_DOUBLE_CLICK);

  // Wire popup save/cancel/close
  const popup = document.getElementById('draw-popup');
  document.getElementById('dp-close')?.addEventListener('click', closePropertyPopup);
  document.getElementById('dp-cancel')?.addEventListener('click', closePropertyPopup);
  document.getElementById('dp-save')?.addEventListener('click', commitDrawing);

  // Первый рендер (если в localStorage уже что-то есть)
  drawingsRender();
}

function startDrawing(tool) {
  state.drawingTool = tool;
  state.drawingPoints = [];
  state.drawingDragCircle = null;
  document.querySelectorAll('[data-draw-tool]').forEach((b) => {
    b.classList.toggle('active', b.dataset.drawTool === tool);
  });
  updateDrawGroupLabel();
  showDrawHint(tool);
  viewer.canvas.style.cursor = 'crosshair';
}

function cancelDrawing() {
  state.drawingTool = null;
  state.drawingPoints = [];
  state.drawingDragCircle = null;
  document.querySelectorAll('[data-draw-tool]').forEach((b) => b.classList.remove('active'));
  updateDrawGroupLabel();
  hideDrawHint();
  viewer.canvas.style.cursor = '';
  drawingsRenderPreview();
}

// Показывает в strip-кнопке DRAW: активный tool или просто "DRAW"
function updateDrawGroupLabel() {
  const label = document.getElementById('dh-draw-label');
  if (!label) return;
  const t = state.drawingTool;
  if (t) {
    label.innerHTML = `DRAW · ${t.toUpperCase()} <span class="dh-chev">▾</span>`;
    label.classList.add('has-active');
  } else {
    label.innerHTML = `DRAW <span class="dh-chev">▾</span>`;
    label.classList.remove('has-active');
  }
}

function showDrawHint(tool) {
  const hint = document.getElementById('draw-hint');
  if (!hint) return;
  let text = '';
  if (tool === 'koz') text = `<b>KOZ</b> · click to add vertex · <span class="kbd-inv">DBL-click</span> or <span class="kbd-inv">Enter</span> to finish · <span class="kbd-inv">Esc</span> to cancel`;
  else if (tool === 'threat') text = `<b>Threat</b> · click center · <span class="kbd-inv">Esc</span> to cancel`;
  else if (tool === 'threat-2') text = `<b>Threat</b> · click again to set radius · <span class="kbd-inv">Esc</span> to cancel`;
  else if (tool === 'poi') text = `<b>POI</b> · single-click to drop marker · <span class="kbd-inv">Esc</span> to cancel`;
  else if (tool === 'route') text = `<b>Route</b> · click points, <span class="kbd-inv">DBL-click</span>/<span class="kbd-inv">Enter</span> to finish · <span class="kbd-inv">Esc</span> to cancel`;
  else if (tool === 'measure') text = `<b>Measure</b> · click points · <span class="kbd-inv">Esc</span>/<span class="kbd-inv">DBL-click</span> to clear<span class="measure-out" id="measure-out"></span>`;
  hint.innerHTML = text;
  hint.classList.remove('hidden');
}
function hideDrawHint() {
  const hint = document.getElementById('draw-hint');
  if (hint) hint.classList.add('hidden');
}

function updateMeasureReadout() {
  if (state.drawingTool !== 'measure') return;
  const out = document.getElementById('measure-out');
  if (!out) return;
  const pts = state.drawingPoints;
  if (pts.length < 2) { out.textContent = ''; return; }
  let total = 0;
  for (let i = 1; i < pts.length; i++) {
    total += haversineMeters(pts[i-1][0], pts[i-1][1], pts[i][0], pts[i][1]);
  }
  out.textContent = total >= 1000 ? `${(total/1000).toFixed(2)} km` : `${Math.round(total)} m`;
}

function canvasToLla(canvasPos) {
  const ray = viewer.camera.getPickRay(canvasPos);
  const cart = ray && viewer.scene.globe.pick(ray, viewer.scene);
  if (!cart) return null;
  const c = Cesium.Cartographic.fromCartesian(cart);
  return { lat: Cesium.Math.toDegrees(c.latitude), lon: Cesium.Math.toDegrees(c.longitude) };
}

// =================================================================
// PROPERTY POPUP — заполнение/сабмит формы
// =================================================================
function openPropertyPopup(kind) {
  state.popupKind = kind;
  const pop = document.getElementById('draw-popup');
  if (!pop) return;
  document.getElementById('dp-title').textContent = `Save ${kind.toUpperCase()}`;

  // Показываем только нужные поля
  const showRow = (id, on) => {
    const el = document.getElementById(id);
    if (el) el.style.display = on ? '' : 'none';
  };
  showRow('dp-classification-row', kind === 'koz');
  showRow('dp-type-row', kind === 'threat');
  showRow('dp-confidence-row', kind === 'threat');
  showRow('dp-poi-type-row', kind === 'poi');

  // Пресет имени
  document.getElementById('dp-name').value = ({
    koz: 'KOZ-' + (state.drawings.filter((d) => d.kind === 'koz').length + 1),
    threat: 'Threat-' + (state.drawings.filter((d) => d.kind === 'threat').length + 1),
    poi: 'POI-' + (state.drawings.filter((d) => d.kind === 'poi').length + 1),
    route: 'Route-' + (state.drawings.filter((d) => d.kind === 'route').length + 1),
  })[kind] || '';

  pop.classList.remove('hidden');
  document.getElementById('dp-name').focus();
}

function closePropertyPopup() {
  const pop = document.getElementById('draw-popup');
  if (pop) pop.classList.add('hidden');
  cancelDrawing();
}

function commitDrawing() {
  const kind = state.popupKind;
  if (!kind) return;
  const name = document.getElementById('dp-name').value.trim() || kind;
  const obj = { id: 'd-' + Date.now().toString(36), kind, name, hidden: false };

  if (kind === 'koz') {
    obj.polygon = [...state.drawingPoints];
    obj.classification = document.getElementById('dp-classification').value.trim();
  } else if (kind === 'threat') {
    obj.center = state.drawingDragCircle.center;
    obj.radius_m = state.drawingDragCircle.radiusM;
    obj.type = document.getElementById('dp-type').value;
    obj.confidence = +document.getElementById('dp-confidence').value || 0.7;
  } else if (kind === 'poi') {
    obj.point = state.drawingPoints[0];
    obj.poi_type = document.getElementById('dp-poi-type').value;
  } else if (kind === 'route') {
    obj.polyline = [...state.drawingPoints];
  }

  state.drawings.push(obj);
  saveStore();

  document.getElementById('draw-popup').classList.add('hidden');
  cancelDrawing();
  drawingsRender();
  renderDrawingsList();
  logEvent('draw', `${kind.toUpperCase()} "${name}" saved`, 'success');
}

// =================================================================
// DRAWINGS RENDER (заменяет stub)
// =================================================================
// Сэмплирует географическую окружность (центр+радиус в метрах) в точки экрана.
// Так threat-зона ложится на карту как настоящий круг на земле и при наклоне
// камеры проецируется в корректный эллипс — а не рисуется кривым SVG-кругом.
function _geoCircleCanvasPts(latC, lonC, radiusM, n = 64) {
  const mPerLat = 111111;
  const mPerLon = 111111 * Math.cos(latC * Math.PI / 180);
  const out = [];
  for (let i = 0; i < n; i++) {
    const a = (i / n) * 2 * Math.PI;
    const la = latC + (radiusM * Math.cos(a)) / mPerLat;
    const lo = lonC + (radiusM * Math.sin(a)) / mPerLon;
    const p = llaToCanvas(la, lo);
    if (p) out.push(p);
  }
  return out;
}

function drawingsRender() {
  const layer = document.getElementById('ovr-drawings');
  if (!layer) return;
  let inner = '';
  for (const d of state.drawings) {
    if (d.hidden) continue;
    if (d.kind === 'koz' && Array.isArray(d.polygon)) {
      const pts = d.polygon.map(([la, lo]) => llaToCanvas(la, lo)).filter(Boolean);
      if (pts.length >= 3) {
        const ptStr = pts.map((p) => `${p.x.toFixed(1)},${p.y.toFixed(1)}`).join(' ');
        inner += `<polygon class="draw-koz" points="${ptStr}"/>`;
        const lab = pts[0];
        inner += `<text class="draw-koz-label" x="${(lab.x + 6).toFixed(1)}" y="${(lab.y + 12).toFixed(1)}">${esc(d.name)}</text>`;
        if (d.classification) {
          inner += `<text class="draw-koz-label" style="font-size:9px;font-weight:500" x="${(lab.x + 6).toFixed(1)}" y="${(lab.y + 24).toFixed(1)}">${esc(d.classification)}</text>`;
        }
      }
    } else if (d.kind === 'threat' && Array.isArray(d.center)) {
      const c = llaToCanvas(d.center[0], d.center[1]);
      const cpts = _geoCircleCanvasPts(d.center[0], d.center[1], d.radius_m);
      if (c && cpts.length >= 8) {
        const ptStr = cpts.map((p) => `${p.x.toFixed(1)},${p.y.toFixed(1)}`).join(' ');
        inner += `<polygon class="draw-threat" points="${ptStr}"/>`;
        inner += `<line class="draw-threat-x" x1="${(c.x-7).toFixed(1)}" y1="${(c.y-7).toFixed(1)}" x2="${(c.x+7).toFixed(1)}" y2="${(c.y+7).toFixed(1)}"/>`;
        inner += `<line class="draw-threat-x" x1="${(c.x-7).toFixed(1)}" y1="${(c.y+7).toFixed(1)}" x2="${(c.x+7).toFixed(1)}" y2="${(c.y-7).toFixed(1)}"/>`;
        const topY = Math.min(...cpts.map((p) => p.y));
        inner += `<text class="draw-threat-label" x="${c.x.toFixed(1)}" y="${(topY - 4).toFixed(1)}" text-anchor="middle">${esc(d.name)} · ${esc(d.type || '')}</text>`;
      }
    } else if (d.kind === 'poi' && Array.isArray(d.point)) {
      const p = llaToCanvas(d.point[0], d.point[1]);
      if (p) {
        inner += `<circle class="draw-poi" cx="${p.x.toFixed(1)}" cy="${p.y.toFixed(1)}" r="5"/>`;
        inner += `<text class="draw-poi-label" x="${(p.x + 9).toFixed(1)}" y="${(p.y + 4).toFixed(1)}">${esc(d.name)}</text>`;
      }
    } else if (d.kind === 'route' && Array.isArray(d.polyline)) {
      const pts = d.polyline.map(([la, lo]) => llaToCanvas(la, lo)).filter(Boolean);
      if (pts.length >= 2) {
        const ptStr = pts.map((p) => `${p.x.toFixed(1)},${p.y.toFixed(1)}`).join(' ');
        inner += `<polyline class="draw-route" points="${ptStr}"/>`;
        for (const p of pts) {
          inner += `<circle class="draw-route-vertex" cx="${p.x.toFixed(1)}" cy="${p.y.toFixed(1)}" r="3"/>`;
        }
        const lab = pts[0];
        inner += `<text class="draw-route-label" x="${(lab.x + 8).toFixed(1)}" y="${(lab.y - 6).toFixed(1)}">${esc(d.name)}</text>`;
      }
    }
  }
  layer.innerHTML = inner;
  setText('kpi-objects', String((state.drawings || []).length));  // шапка · Objects
}

// Превью текущего рисуемого объекта (до commit)
function drawingsRenderPreview() {
  const layer = document.getElementById('ovr-drawing-preview');
  if (!layer) return;
  let inner = '';
  const t = state.drawingTool;
  const mt = state.missionPicking;          // mission-pick режим
  const pts = state.drawingPoints;

  // Mission-pick превью: route (polyline) / area / sector (polygon при ≥3)
  if (mt && pts.length > 0) {
    const screen = pts.map(([la, lo]) => llaToCanvas(la, lo)).filter(Boolean);
    if (screen.length >= 1) {
      const ptStr = screen.map((p) => `${p.x.toFixed(1)},${p.y.toFixed(1)}`).join(' ');
      if (screen.length >= 2) {
        const isPoly = (mt === 'sector') && screen.length >= 3;
        if (isPoly) {
          inner += `<polygon class="draw-preview-line" points="${ptStr}" style="fill:rgba(99,102,241,0.08)"/>`;
        } else {
          inner += `<polyline class="draw-preview-line" points="${ptStr}"/>`;
        }
      }
      for (const p of screen) {
        inner += `<circle class="draw-preview-vertex" cx="${p.x.toFixed(1)}" cy="${p.y.toFixed(1)}" r="3.5"/>`;
      }
    }
    layer.innerHTML = inner;
    return;
  }

  if ((t === 'koz' || t === 'route' || t === 'measure') && pts.length > 0) {
    const screen = pts.map(([la, lo]) => llaToCanvas(la, lo)).filter(Boolean);
    if (screen.length >= 1) {
      const ptStr = screen.map((p) => `${p.x.toFixed(1)},${p.y.toFixed(1)}`).join(' ');
      if (screen.length >= 2) {
        if (t === 'koz') {
          // closed polygon preview only when ≥3 points
          if (screen.length >= 3) {
            inner += `<polygon class="draw-preview-line" points="${ptStr}" style="fill:rgba(99,102,241,0.06)"/>`;
          } else {
            inner += `<polyline class="draw-preview-line" points="${ptStr}"/>`;
          }
        } else {
          inner += `<polyline class="draw-preview-line" points="${ptStr}"/>`;
        }
      }
      for (const p of screen) {
        inner += `<circle class="draw-preview-vertex" cx="${p.x.toFixed(1)}" cy="${p.y.toFixed(1)}" r="3"/>`;
      }
    }
  } else if (t === 'threat' && state.drawingDragCircle && state.drawingDragCircle.radiusM > 0) {
    const c = state.drawingDragCircle.center;
    const cp = llaToCanvas(c[0], c[1]);
    const cpts = _geoCircleCanvasPts(c[0], c[1], state.drawingDragCircle.radiusM);
    if (cp && cpts.length >= 8) {
      const ptStr = cpts.map((p) => `${p.x.toFixed(1)},${p.y.toFixed(1)}`).join(' ');
      inner += `<polygon class="draw-preview-circle" points="${ptStr}"/>`;
      inner += `<circle class="draw-preview-vertex" cx="${cp.x.toFixed(1)}" cy="${cp.y.toFixed(1)}" r="3"/>`;
    }
  }
  layer.innerHTML = inner;
}

function renderDrawingsList() {
  const list = document.getElementById('sd-drawings-list');
  const cnt = document.getElementById('sd-drawings-count');
  if (cnt) cnt.textContent = state.drawings.length;
  if (!list) return;
  if (state.drawings.length === 0) {
    list.innerHTML = '<div class="sd-empty">— no objects drawn yet —</div>';
    return;
  }
  list.innerHTML = '';
  state.drawings.forEach((d, i) => {
    const row = document.createElement('div');
    row.className = 'sd-drawing-item';
    const visIcon = d.hidden ? '◯' : '◉';
    row.innerHTML = `
      <span class="sd-drawing-kind">${esc(d.kind)}</span>
      <span class="sd-drawing-name">${esc(d.name || '—')}</span>
      <button class="sd-drawing-vis" data-idx="${i}" title="Toggle visibility">${visIcon}</button>
      <button class="sd-drawing-del" data-idx="${i}" title="Delete">×</button>
    `;
    list.appendChild(row);
  });
  list.querySelectorAll('.sd-drawing-vis').forEach((btn) => {
    btn.addEventListener('click', () => {
      const i = +btn.dataset.idx;
      if (state.drawings[i]) {
        state.drawings[i].hidden = !state.drawings[i].hidden;
        saveStore();
        drawingsRender();
        renderDrawingsList();
      }
    });
  });
  list.querySelectorAll('.sd-drawing-del').forEach((btn) => {
    btn.addEventListener('click', () => {
      const i = +btn.dataset.idx;
      state.drawings.splice(i, 1);
      saveStore();
      drawingsRender();
      renderDrawingsList();
    });
  });
}

// =================================================================
// GEO OVERLAY (SVG поверх Cesium): AOR rings вокруг базы,
// KOZ polygons и threat circles из mission_context, scale bar.
// Всё перерисовывается на каждом camera move.
// =================================================================
function initGeoOverlay() {
  // Подписка на изменения камеры — Cesium вызывает changed.raiseEvent на каждый move
  viewer.scene.postRender.addEventListener(geoOverlayUpdate);
  // Первая отрисовка
  geoOverlayUpdate();
  // Cursor MGRS readout
  initCursorReadout();
}

// =================================================================
// CURSOR MGRS READOUT
// При движении мыши над Cesium-канвасом — overlay-плашка с координатами
// =================================================================
function initCursorReadout() {
  const overlay = document.getElementById('hud-cursor');
  if (!overlay) return;

  const handler = new Cesium.ScreenSpaceEventHandler(viewer.canvas);
  handler.setInputAction((movement) => {
    const ray = viewer.camera.getPickRay(movement.endPosition);
    const cart = ray && viewer.scene.globe.pick(ray, viewer.scene);
    if (!cart) {
      overlay.classList.add('hidden');
      return;
    }
    const carto = Cesium.Cartographic.fromCartesian(cart);
    const lat = Cesium.Math.toDegrees(carto.latitude);
    const lon = Cesium.Math.toDegrees(carto.longitude);
    const elev = carto.height || 0;

    overlay.classList.remove('hidden');

    // MGRS — через vendored mgrs.js (UMD global window.mgrs.forward)
    let mgrsStr = '—';
    try {
      if (window.mgrs && typeof window.mgrs.forward === 'function') {
        mgrsStr = window.mgrs.forward([lon, lat], 5);  // 5 = 1m precision
      }
    } catch (e) { /* ignore */ }

    setText('cur-mgrs', mgrsStr);
    setText('cur-lat', lat.toFixed(6) + '°');
    setText('cur-lon', lon.toFixed(6) + '°');
    setText('cur-elev', `${Math.round(elev)} m AMSL`);

    // RNG/BRG до selectedDroneId или базы
    const sel = state.selectedDroneId ? state.drones.get(state.selectedDroneId) : null;
    const target = sel && sel.lat != null ? sel : { lat: state.base.lat, lon: state.base.lon, _isBase: true };
    const dist = haversineMeters(lat, lon, target.lat, target.lon);
    const brg = bearingDeg(lat, lon, target.lat, target.lon);
    setText('cur-rng', dist >= 1000 ? `${(dist / 1000).toFixed(2)} km` : `${Math.round(dist)} m`);
    setText('cur-brg', `${String(Math.round(brg)).padStart(3, '0')}°`);
  }, Cesium.ScreenSpaceEventType.MOUSE_MOVE);

  // При выходе мыши за пределы canvas — скрываем
  viewer.canvas.addEventListener('mouseleave', () => overlay.classList.add('hidden'));
  viewer.canvas.addEventListener('mouseenter', () => overlay.classList.remove('hidden'));
}

function haversineMeters(lat1, lon1, lat2, lon2) {
  const R = 6371000;
  const toRad = (d) => d * Math.PI / 180;
  const dLat = toRad(lat2 - lat1);
  const dLon = toRad(lon2 - lon1);
  const a = Math.sin(dLat / 2) ** 2 +
            Math.cos(toRad(lat1)) * Math.cos(toRad(lat2)) * Math.sin(dLon / 2) ** 2;
  return 2 * R * Math.asin(Math.sqrt(a));
}

function bearingDeg(lat1, lon1, lat2, lon2) {
  const toRad = (d) => d * Math.PI / 180;
  const toDeg = (r) => r * 180 / Math.PI;
  const dLon = toRad(lon2 - lon1);
  const y = Math.sin(dLon) * Math.cos(toRad(lat2));
  const x = Math.cos(toRad(lat1)) * Math.sin(toRad(lat2)) -
            Math.sin(toRad(lat1)) * Math.cos(toRad(lat2)) * Math.cos(dLon);
  return ((toDeg(Math.atan2(y, x)) + 360) % 360);
}

function llaToCanvas(lat, lon, alt = 0) {
  const cart = Cesium.Cartesian3.fromDegrees(lon, lat, alt);
  const canvas = viewer.scene.cartesianToCanvasCoordinates(cart);
  if (!canvas) return null;
  // Если точка за горизонтом — Cesium может вернуть значения вне viewport.
  return { x: canvas.x, y: canvas.y };
}

function geoOverlayUpdate() {
  // Статичные слои AOR/KOZ/threats удалены — теперь это рисуется через Drawing Tools.
  // Здесь оставлены только N-arrow (фиксированный) и scale bar (пересчёт по zoom).
  const baseLat = state.base.lat;
  const baseLon = state.base.lon;

  // Drawing tools layer — рендерим сохранённые объекты пользователя (если есть) +
  // preview активного рисуемого объекта (точки/линии следуют за камерой).
  if (typeof drawingsRender === 'function') drawingsRender();
  if (typeof drawingsRenderPreview === 'function') drawingsRenderPreview();

  // Scale bar — пиксели на каждые 500m
  const scaleG = document.getElementById('ovr-scale');
  if (scaleG) {
    const baseCanvas = llaToCanvas(baseLat, baseLon);
    if (baseCanvas) {
      const px500 = (() => {
        const e = llaToCanvas(baseLat + (500 / 111111), baseLon);
        return e ? Math.hypot(e.x - baseCanvas.x, e.y - baseCanvas.y) : 0;
      })();
      const w = Math.max(40, Math.min(220, px500));
      const wrap = viewer.canvas.height - 40;
      scaleG.setAttribute('transform', `translate(60, ${wrap})`);
      scaleG.innerHTML = `
        <rect class="scale-tick" x="0" y="0" width="${w.toFixed(1)}" height="3"/>
        <rect class="scale-tick alt" x="${w.toFixed(1)}" y="0" width="${w.toFixed(1)}" height="3"/>
        <text class="scale-text" x="0" y="-4">0</text>
        <text class="scale-text" x="${w.toFixed(1)}" y="-4">500m</text>
        <text class="scale-text" x="${(2*w).toFixed(1)}" y="-4">1km</text>
      `;
    }
  }
}

// =================================================================
// MISSION CONTEXT (doc-header + classification banners)
// =================================================================
function applyMissionContext() {
  const ctx = state.missionCtx;
  if (!ctx) return;
  const opr = ctx.operator || {};

  // Шапка (MODE/UPTIME/Active/Objects/Link/Fleet/Missions) заполняется живыми
  // данными в tickClock/refreshMissions/drawingsRender/setLink — здесь больше
  // декоративных полей из конфига нет. Остаётся только футер.

  // Footer: оператор + cert pills из конфига
  setText('df-oper', `${opr.name || '—'} · ${opr.clearance || '—'}`);
  const certWrap = document.getElementById('df-cert-pills');
  if (certWrap) {
    // первый span с классом df-l оставляем — это label "Cert"
    certWrap.innerHTML = '<span class="df-l">Cert</span>';
    (ctx.certifications || []).forEach((c) => {
      const pill = document.createElement('span');
      pill.className = 'cert-pill';
      pill.textContent = c;
      certWrap.appendChild(pill);
    });
  }
}

// =================================================================
// PTT COMMS BAR (UI-stub) — заполняется по флоту + базе
// =================================================================
function renderPttBar() {
  const bar = document.getElementById('ptt-bar');
  if (!bar) return;
  const drones = [...state.drones.values()];
  const channels = [
    { id: 'FLEET-NET', label: 'FLEET-NET', state: 'ok' },
    ...drones.slice(0, 6).map((d) => ({
      id: d.id,
      label: (d.name || d.id).toUpperCase(),
      state: pttStateForStatus(d.status),
    })),
    { id: 'BASE', label: 'BASE', state: 'ok' },
  ];
  bar.innerHTML = '';
  channels.forEach((ch) => {
    const btn = document.createElement('button');
    btn.className = `ptt ptt-${ch.state}`;
    btn.dataset.channel = ch.id;
    btn.title = `Push-to-talk · ${ch.label}`;
    btn.textContent = ch.label;
    btn.onclick = () => logEvent('ptt', `keyed channel: ${ch.label}`, '');
    bar.appendChild(btn);
  });
  setText('ptt-count', `${channels.length} ch`);
}

function pttStateForStatus(s) {
  if (!s) return 'ok';
  if (s === 'ERROR' || s === 'ABORTED') return 'crit';
  if (s === 'TAKEOFF' || s === 'LAND' || s === 'LANDING') return 'warn';
  return 'ok';
}

// =================================================================
// FOOTER: BLACK BOX size + SHA-256 чексумма event-лога
// =================================================================
async function refreshBlackBox() {
  const log = document.getElementById('event-log');
  if (!log) return;
  const text = log.textContent || '';
  const bytes = new Blob([text]).size;
  // KB или MB красиво
  let sz;
  if (bytes < 1024) sz = `${bytes} B`;
  else if (bytes < 1024 * 1024) sz = `${(bytes / 1024).toFixed(1)} KB`;
  else sz = `${(bytes / 1024 / 1024).toFixed(2)} MB`;
  setText('df-blackbox', `REC · ${sz}`);

  // SHA-256 первых 8 hex символов — достаточно для «выглядит-серьёзно»
  try {
    const hash = await sha256Hex(text);
    setText('df-chksum', `SHA-256 0x${hash.slice(0, 8).toUpperCase()}…${hash.slice(-4).toUpperCase()}`);
  } catch (e) {
    setText('df-chksum', 'SHA-256 0x—');
  }
}

async function sha256Hex(s) {
  const buf = new TextEncoder().encode(s);
  const hashBuf = await crypto.subtle.digest('SHA-256', buf);
  return Array.from(new Uint8Array(hashBuf)).map((b) => b.toString(16).padStart(2, '0')).join('');
}

// =================================================================
// AIRFRAME SCHEMATIC (FIG. 2)
// Stub-RPM пока telemetry pipeline не расширим: FLYING → 5800-6300 разброс,
// IDLE → 0. Когда придёт реальный actuator_output_status — заменим в шаге 6.
// =================================================================
function updateAirframeSchematic() {
  const id = state.selectedDroneId;
  const target = document.getElementById('schem-target');
  if (!id) {
    if (target) target.textContent = '— select UAS —';
    for (let i = 1; i <= 4; i++) {
      setText(`schem-m${i}-rpm`, '— RPM');
      const st = document.getElementById(`schem-m${i}-status`);
      if (st) { st.textContent = '▲ —'; st.setAttribute('fill', 'var(--ink-3)'); }
    }
    setText('schem-payload', '— kg');
    return;
  }
  const d = state.drones.get(id);
  if (!d) return;
  if (target) target.textContent = (d.name || id).toUpperCase();

  const flying = ['FLYING', 'MISSION', 'IN_PROGRESS', 'STARTED', 'TAKEOFF', 'LAND', 'LANDING'].includes(d.status);

  // Если PX4 шлёт реальный actuator_output_status — используем motors[0:4] (PWM us 1000..2000).
  // Иначе stub: 5800-6300 RPM при FLYING, 0 при IDLE.
  const realMotors = Array.isArray(d.motors) && d.motors.length >= 4;

  for (let i = 1; i <= 4; i++) {
    let rpm, status, color;
    if (realMotors) {
      const pwm = Number(d.motors[i - 1]) || 0;
      // PWM 1000..2000 → RPM 0..7500 (грубая линеаризация; в SITL PWM ≈ 1100 idle, 1450 hover)
      rpm = Math.max(0, Math.round((pwm - 1000) * 7.5));
      if (rpm > 4000) { status = '▲ NORMAL'; color = 'var(--accent)'; }
      else if (rpm > 1000) { status = '▲ LOW'; color = 'var(--warning)'; }
      else { status = '◯ IDLE'; color = 'var(--ink-3)'; }
    } else if (flying) {
      const base = 6000 + Math.round(80 * Math.sin(Date.now() / 1000 + i));
      rpm = base + (i - 2) * 30;
      status = '▲ NORMAL';
      color = 'var(--accent)';
    } else if (d.status === 'ERROR' || d.status === 'ABORTED') {
      rpm = 0; status = '✗ FAULT'; color = 'var(--error)';
    } else {
      rpm = 0; status = '◯ IDLE'; color = 'var(--ink-3)';
    }
    setText(`schem-m${i}-rpm`, `${rpm.toLocaleString()} RPM`);
    const st = document.getElementById(`schem-m${i}-status`);
    if (st) { st.textContent = status; st.setAttribute('fill', color); }
  }
  setText('schem-payload', 'n/a');  // payload removed — не симулируется
}

function setText(id, txt) {
  const el = document.getElementById(id);
  if (el) el.textContent = txt;
}

// =================================================================
// BASE
// =================================================================
function drawBase() {
  const { lat, lon } = state.base;
  state.entities.base = viewer.entities.add({
    name: 'BASE',
    position: Cesium.Cartesian3.fromDegrees(lon, lat, 0),
    ellipse: {
      semiMinorAxis: 40.0,
      semiMajorAxis: 40.0,
      material: Cesium.Color.fromCssColorString('#1d4ed8').withAlpha(0.12),
      outline: true,
      outlineColor: Cesium.Color.fromCssColorString('#1d4ed8'),
      outlineWidth: 2,
      heightReference: Cesium.HeightReference.CLAMP_TO_GROUND,
    },
    cylinder: {
      length: 10.0,
      topRadius: 0,
      bottomRadius: 14.0,
      material: Cesium.Color.fromCssColorString('#1d4ed8').withAlpha(0.28),
      outline: true,
      outlineColor: Cesium.Color.fromCssColorString('#1d4ed8'),
    },
    label: {
      text: '◼ BASE',
      font: '700 11px "IBM Plex Mono", "JetBrains Mono", monospace',
      fillColor: Cesium.Color.fromCssColorString('#0a0e14'),
      outlineColor: Cesium.Color.WHITE,
      outlineWidth: 3,
      style: Cesium.LabelStyle.FILL_AND_OUTLINE,
      verticalOrigin: Cesium.VerticalOrigin.BOTTOM,
      pixelOffset: new Cesium.Cartesian2(0, -14),
      heightReference: Cesium.HeightReference.CLAMP_TO_GROUND,
      disableDepthTestDistance: Number.POSITIVE_INFINITY,
    },
  });
}

function flyToBase(duration = 1.4) {
  const { lat, lon } = state.base;
  viewer.camera.flyTo({
    destination: Cesium.Cartesian3.fromDegrees(lon, lat - 0.006, 1800),
    orientation: {
      heading: 0,
      pitch: Cesium.Math.toRadians(-50),
      roll: 0,
    },
    duration,
  });
}

// =================================================================
// WEBSOCKET
// =================================================================
function connectWS() {
  const ws = new WebSocket(`ws://${location.host}/ws`);
  ws.onopen = () => {
    state.wsConnected = true;
    setLink('ONLINE', 'ok');
    logEvent('sys', 'websocket connected', 'success');
  };
  ws.onclose = () => {
    state.wsConnected = false;
    setLink('OFFLINE', 'err');
    logEvent('sys', 'websocket closed, retrying…', 'error');
    setTimeout(connectWS, 2000);
  };
  ws.onerror = () => setLink('ERROR', 'err');
  ws.onmessage = (ev) => {
    try {
      handleMsg(JSON.parse(ev.data));
    } catch (e) { console.error(e); }
  };
}

function handleMsg(msg) {
  if (msg.type === 'telemetry_update') {
    const parts = String(msg.topic).split('/');
    const id = parts[1];
    const stream = parts[2] || 'pose';
    const p = msg.payload || {};
    let upd = { last_telem: Date.now() };

    if (stream === 'pose') {
      if (p.lat == null || p.lon == null) return;
      upd.lat = p.lat;
      upd.lon = p.lon;
      upd.alt = p.alt;
    } else if (stream === 'battery') {
      upd.battery_v = p.voltage_v;
      upd.battery_pct = p.remaining_pct;
    } else if (stream === 'attitude') {
      upd.pitch = p.pitch;
      upd.roll = p.roll;
      upd.yaw = p.yaw;
    } else if (stream === 'gps') {
      upd.sat_count = p.satellites;
      upd.fix_type = p.fix_type;
    } else if (stream === 'velocity') {
      upd.gs = p.gs;
      upd.vz = p.vz;
    } else if (stream === 'actuators') {
      upd.motors = p.motors;
      upd.motors_active = p.active;
    } else {
      return;
    }

    upsertDrone(id, upd);
    if (stream === 'pose') renderDrone(id);
    return;
  }

  if (msg.type === 'drone_active' && msg.payload?.id) {
    upsertDrone(msg.payload.id, msg.payload);
    renderDrone(msg.payload.id);
    return;
  }

  if (msg.type === 'mission_planned' && msg.payload?.waypoints) {
    const mid = msg.topic.split('/')[1];
    const pl = msg.payload;
    drawWaypoints(mid, pl.waypoints, pl.mission_type, pl.takeoff_profile);
    updateSessionMission(mid, {
      mission_type: pl.mission_type || 'delivery',
      notes: pl.notes || null,
      priority: pl.priority || 'normal',
      wp_count: pl.waypoints.length,
      status: 'PLANNED',
    });
    logEvent('orch', `mission ${short(mid)} planned (${pl.waypoints.length} wps)`);
    return;
  }

  if (msg.type === 'mission_assigned' && msg.payload) {
    const mid = msg.topic.split('/')[1];
    updateSessionMission(mid, { vehicle_id: msg.payload.vehicle_id, status: 'ASSIGNED' });
    logEvent(msg.payload.vehicle_id || 'orch', `${short(mid)} assigned`);
    return;
  }

  if (msg.type === 'mission_progress' && msg.payload) {
    const { vehicle_id, current, total } = msg.payload;
    const mid = msg.topic.split('/')[1];
    updateSessionMission(mid, { vehicle_id, current, total, status: 'IN_PROGRESS' });
    logEvent(vehicle_id, `wp ${current}/${total}`);
    return;
  }

  if (msg.type === 'mission_status' && msg.payload) {
    const { status, vehicle_id } = msg.payload;
    const mid = msg.topic.split('/')[1];
    updateSessionMission(mid, { status, vehicle_id });
    const lvl = status === 'COMPLETED' ? 'success'
             : (String(status).includes('FAILED') || status === 'ABORTED') ? 'error'
             : '';
    logEvent(vehicle_id || 'orch', `${short(mid)} → ${status}`, lvl);
    if (status === 'COMPLETED') {
      state.completedMissions.add(mid);  // идемпотентно: дубли COMPLETED не накручивают
      clearWaypoints(mid);
      clearTrail(vehicle_id);
      updateKpis();
    }
    return;
  }
}

// =================================================================
// DRONES
// =================================================================
function upsertDrone(id, upd) {
  const prev = state.drones.get(id) || { id, name: id, status: 'IDLE' };
  const next = { ...prev, ...upd };
  if (prev.lat != null && (prev.lat !== next.lat || prev.lon !== next.lon)) {
    const dy = next.lat - prev.lat;
    const dx = (next.lon - prev.lon) * Math.cos(next.lat * Math.PI / 180);
    if (dx * dx + dy * dy > 1e-12) {
      next.heading = Math.atan2(dx, dy) * 180 / Math.PI;
    }
  }
  state.drones.set(id, next);
  if (next.lat != null && next.lon != null) {
    pushTrail(id, next.lon, next.lat, next.alt);
  }
  updateCard(id);
  updateKpis();
}

function pushTrail(id, lon, lat, alt) {
  if (!state.trajectories.has(id)) state.trajectories.set(id, []);
  const arr = state.trajectories.get(id);
  const n = arr.length;
  if (n > 0) {
    const [plon, plat, palt] = arr[n - 1];
    if (Math.abs(plon - lon) < 1e-7 && Math.abs(plat - lat) < 1e-7 && Math.abs((palt || 0) - (alt || 0)) < 0.3) return;
  }
  arr.push([lon, lat, Math.max(0, alt || 0)]);
  if (arr.length > 600) arr.shift();
  renderTrail(id);
}

function renderDrone(id) {
  const d = state.drones.get(id);
  if (!d || d.lat == null || d.lon == null) return;
  const alt = Math.max(0.1, d.alt || 0);
  const pos = Cesium.Cartesian3.fromDegrees(d.lon, d.lat, alt);
  const color = Cesium.Color.fromCssColorString(statusColor(d.status));

  let ent = state.entities.drones.get(id);
  if (!ent) {
    ent = viewer.entities.add({
      position: pos,
      point: {
        pixelSize: 16,
        color,
        outlineColor: Cesium.Color.WHITE,
        outlineWidth: 2,
        disableDepthTestDistance: Number.POSITIVE_INFINITY,
      },
      label: {
        text: d.name || id,
        font: '700 11px "IBM Plex Mono", "JetBrains Mono", monospace',
        fillColor: Cesium.Color.fromCssColorString('#0a0e14'),
        outlineColor: Cesium.Color.WHITE,
        outlineWidth: 3,
        style: Cesium.LabelStyle.FILL_AND_OUTLINE,
        verticalOrigin: Cesium.VerticalOrigin.BOTTOM,
        pixelOffset: new Cesium.Cartesian2(0, -20),
        disableDepthTestDistance: Number.POSITIVE_INFINITY,
      },
    });
    state.entities.drones.set(id, ent);
  } else {
    ent.position = pos;
    ent.point.color = color;
  }

  let leader = state.entities.leaderLines.get(id);
  if (!leader) {
    leader = viewer.entities.add({
      polyline: {
        positions: new Cesium.CallbackProperty(() => {
          const dd = state.drones.get(id);
          if (!dd || dd.lat == null) return [];
          const aN = Math.max(0.1, dd.alt || 0);
          return [
            Cesium.Cartesian3.fromDegrees(dd.lon, dd.lat, aN),
            Cesium.Cartesian3.fromDegrees(dd.lon, dd.lat, 0),
          ];
        }, false),
        width: 1.5,
        material: new Cesium.PolylineDashMaterialProperty({
          color: color.withAlpha(0.6),
          dashLength: 8.0,
        }),
      },
    });
    state.entities.leaderLines.set(id, leader);
  } else {
    leader.polyline.material = new Cesium.PolylineDashMaterialProperty({
      color: color.withAlpha(0.6),
      dashLength: 8.0,
    });
  }

  let shadow = state.entities.shadows.get(id);
  if (!shadow) {
    shadow = viewer.entities.add({
      position: new Cesium.CallbackProperty(() => {
        const dd = state.drones.get(id);
        return Cesium.Cartesian3.fromDegrees(dd.lon, dd.lat, 0);
      }, false),
      ellipse: {
        semiMinorAxis: 4.5,
        semiMajorAxis: 4.5,
        material: color.withAlpha(0.35),
        heightReference: Cesium.HeightReference.CLAMP_TO_GROUND,
      },
    });
    state.entities.shadows.set(id, shadow);
  } else {
    shadow.ellipse.material = color.withAlpha(0.35);
  }
}

function renderTrail(id) {
  const pts = state.trajectories.get(id);
  if (!pts || pts.length < 2) return;
  const flat = [];
  pts.forEach(([lon, lat, alt]) => flat.push(lon, lat, alt));
  const positions = Cesium.Cartesian3.fromDegreesArrayHeights(flat);
  const d = state.drones.get(id);
  const color = Cesium.Color.fromCssColorString(statusColor(d?.status || 'FLYING')).withAlpha(0.65);

  let trail = state.entities.trails.get(id);
  if (!trail) {
    trail = viewer.entities.add({
      polyline: { positions, width: 3, material: color },
    });
    state.entities.trails.set(id, trail);
  } else {
    trail.polyline.positions = positions;
    trail.polyline.material = color;
  }
}

function clearTrail(id) {
  if (!id) return;
  const t = state.entities.trails.get(id);
  if (t) { viewer.entities.remove(t); state.entities.trails.delete(id); }
  state.trajectories.delete(id);
}

// =================================================================
// WAYPOINTS
// =================================================================
// Базовый цвет NAV/ORBIT-точек и трека — слегка различается по типу миссии.
// TAKEOFF/LAND всегда имеют свой цвет (оранжевый/зелёный) для читаемости.
const MISSION_TYPE_COLOR = {
  delivery: '#1d4ed8',  // синий
  isr:      '#7c3aed',  // фиолетовый
  patrol:   '#0d9488',  // бирюзовый
  sector:   '#ea580c',  // оранжевый
};

function drawWaypoints(mid, wps, missionType, takeoffProfile) {
  clearWaypoints(mid);
  const arr = [];
  const pathPts = [];
  const typeCss = MISSION_TYPE_COLOR[String(missionType || '').toLowerCase()] || '#1d4ed8';

  // Lead-in: пунктир от базы (точка старта дрона) до первой точки маршрута,
  // с учётом профиля взлёта. vertical → набор вверх над базой, потом к WP
  // (L-образно); inclined → диагональ от базы к WP. Цвет — янтарный (launch leg).
  const firstNav = wps.find((w) => {
    const k = String((w.kind || 'NAV')).toUpperCase();
    return k !== 'TAKEOFF' && k !== 'LAND';
  });
  if (firstNav && state.base && state.base.lat != null) {
    const fp = firstNav.pos || firstNav;
    const cruiseAlt = Math.max(2, fp.alt || 1);
    const b = state.base;
    const profile = String(takeoffProfile || 'vertical').toLowerCase();
    const leadPts = (profile === 'inclined')
      ? [b.lon, b.lat, 1, fp.lon, fp.lat, cruiseAlt]                       // диагональ
      : [b.lon, b.lat, 1, b.lon, b.lat, cruiseAlt, fp.lon, fp.lat, cruiseAlt]; // вверх → к WP
    const leadEnt = viewer.entities.add({
      polyline: {
        positions: Cesium.Cartesian3.fromDegreesArrayHeights(leadPts),
        width: 1.5,
        material: new Cesium.PolylineDashMaterialProperty({
          color: Cesium.Color.fromCssColorString('#b45309').withAlpha(0.7),
          dashLength: 10.0,
        }),
      },
    });
    arr.push(leadEnt);
  }

  wps.forEach((wp, idx) => {
    const p = wp.pos || wp;
    const lat = p.lat, lon = p.lon;
    const alt = Math.max(1, p.alt || 1);
    const kind = String(wp.kind || 'NAV').toUpperCase();
    const css = kind === 'TAKEOFF' ? '#b45309'
              : kind === 'LAND' ? '#166534'
              : typeCss;
    const color = Cesium.Color.fromCssColorString(css);

    const ent = viewer.entities.add({
      position: Cesium.Cartesian3.fromDegrees(lon, lat, alt / 2),
      cylinder: {
        length: Math.max(alt, 2),
        topRadius: 4.0,
        bottomRadius: 4.0,
        material: color.withAlpha(0.32),
        outline: true,
        outlineColor: color,
      },
      label: {
        text: kind === 'TAKEOFF' ? '▲ TAKEOFF'
             : kind === 'LAND' ? '▼ LAND'
             : kind === 'ORBIT' ? `◯ ORBIT`
             : `WP${idx}`,
        font: '700 10px "IBM Plex Mono", "JetBrains Mono", monospace',
        fillColor: color,
        outlineColor: Cesium.Color.WHITE,
        outlineWidth: 3,
        style: Cesium.LabelStyle.FILL_AND_OUTLINE,
        verticalOrigin: Cesium.VerticalOrigin.BOTTOM,
        pixelOffset: new Cesium.Cartesian2(0, -alt * 0.5 - 8),
        disableDepthTestDistance: Number.POSITIVE_INFINITY,
      },
    });
    arr.push(ent);
    pathPts.push(lon, lat, alt);
  });
  state.entities.waypoints.set(mid, arr);

  if (pathPts.length >= 6) {
    const pathEnt = viewer.entities.add({
      polyline: {
        positions: Cesium.Cartesian3.fromDegreesArrayHeights(pathPts),
        width: 1.5,
        material: new Cesium.PolylineDashMaterialProperty({
          color: Cesium.Color.fromCssColorString(typeCss).withAlpha(0.6),
          dashLength: 16.0,
        }),
      },
    });
    state.entities.missionPaths.set(mid, pathEnt);
  }
}

function clearWaypoints(mid) {
  const arr = state.entities.waypoints.get(mid) || [];
  arr.forEach((e) => viewer.entities.remove(e));
  state.entities.waypoints.delete(mid);
  const p = state.entities.missionPaths.get(mid);
  if (p) viewer.entities.remove(p);
  state.entities.missionPaths.delete(mid);
}

// =================================================================
// CARDS / MISSIONS / KPI
// =================================================================
function updateCard(id) {
  const d = state.drones.get(id);
  if (!d) return;
  const container = document.getElementById('drone-cards');
  let row = document.getElementById(`drone-card-${id}`);

  if (!row) {
    // === Structure creation, 1 раз ===
    row = document.createElement('div');
    row.className = 'drone-card';
    row.id = `drone-card-${id}`;
    row.title = 'Click to select · DETAIL button or double-click opens detail';
    row.innerHTML = `
      <div class="drone-card-head">
        <span class="drone-name"></span>
        <div class="drone-card-actions">
          <button class="drone-detail-btn" type="button" title="Open UAS detail in new tab">↗ DETAIL</button>
          <span class="chip"></span>
        </div>
      </div>
      <div class="metrics metrics-6">
        <div class="metric"><span class="metric-label">ALT</span><span class="metric-value" data-k="alt">—</span></div>
        <div class="metric"><span class="metric-label">GS</span><span class="metric-value" data-k="gs">—</span></div>
        <div class="metric"><span class="metric-label">HDG</span><span class="metric-value" data-k="hdg">—</span></div>
        <div class="metric"><span class="metric-label">PIT</span><span class="metric-value" data-k="pit">—</span></div>
        <div class="metric"><span class="metric-label">ROL</span><span class="metric-value" data-k="rol">—</span></div>
        <div class="metric"><span class="metric-label">SV</span><span class="metric-value" data-k="sv">—</span></div>
      </div>
      <div class="bars">
        <div class="bar-row"><span class="bar-l">BATT</span><div class="bar-track"><div class="bar-fill" data-bar="batt" style="width:0%"></div></div><span class="bar-v" data-bv="batt">—</span></div>
        <div class="bar-row"><span class="bar-l">LINK</span><div class="bar-track"><div class="bar-fill" data-bar="link" style="width:0%"></div></div><span class="bar-v" data-bv="link">—</span></div>
      </div>
      <div class="progress-wrap" data-mission-wrap style="display:none">
        <div class="progress-bar"><span data-mission-bar style="width:0%"></span></div>
        <span class="mono" data-mission-count>0/0</span>
      </div>
    `;

    // Detail button — clearly separate handler with stopPropagation
    row.querySelector('.drone-detail-btn').addEventListener('click', (e) => {
      e.stopPropagation();
      window.open(`/sim/drone/${encodeURIComponent(id)}`, '_blank', 'noopener');
    });
    // Click on card → select
    row.addEventListener('click', () => {
      document.querySelectorAll('.drone-card').forEach((c) => c.classList.remove('selected'));
      row.classList.add('selected');
      state.selectedDroneId = id;
      saveStore();
      flyToDrone(id);
      updateAirframeSchematic();
    });
    // Dblclick → open detail
    row.addEventListener('dblclick', (e) => {
      e.stopPropagation();
      window.open(`/sim/drone/${encodeURIComponent(id)}`, '_blank', 'noopener');
    });

    container.appendChild(row);
  }

  // === Только обновление значений (innerHTML не перезаписывается) ===
  if (state.selectedDroneId === id) row.classList.add('selected');

  // Name + status chip
  row.querySelector('.drone-name').textContent = d.name || id;
  const chip = row.querySelector('.chip');
  const status = d.status || 'IDLE';
  if (chip.textContent !== status) {
    chip.textContent = status;
    chip.className = `chip ${status}`;
  }

  // Telemetry values
  const flying = ['FLYING', 'MISSION', 'IN_PROGRESS', 'STARTED'].includes(status);
  const hdg = (d.yaw != null) ? d.yaw : d.heading;
  const setVal = (k, v) => {
    const el = row.querySelector(`[data-k="${k}"]`);
    if (el && el.textContent !== v) el.textContent = v;
  };
  setVal('alt', `${fmt(d.alt, 1)}m`);
  setVal('gs', `${fmt(d.gs, 1)}m/s`);
  setVal('hdg', hdg != null ? String(Math.round(((hdg % 360) + 360) % 360)).padStart(3, '0') + '°' : '—°');
  setVal('pit', `${fmt(d.pitch, 1)}°`);
  setVal('rol', `${fmt(d.roll, 1)}°`);
  setVal('sv', d.sat_count != null ? String(d.sat_count) : '—');

  // Bars
  const ageS = d.last_telem ? (Date.now() - d.last_telem) / 1000 : 999;
  const linkPct = Math.max(0, Math.min(100, Math.round(100 - (Math.max(0, ageS - 1.5) / 4.5) * 100)));
  const linkClass = linkPct > 70 ? '' : (linkPct > 30 ? 'amber' : 'red');
  const batt = d.battery_pct != null ? Math.round(d.battery_pct) : null;
  const battClass = batt == null ? '' : (batt > 40 ? '' : (batt > 20 ? 'amber' : 'red'));
  const pyld = flying ? 2.4 : 0;

  const setBar = (k, pct, cls, label) => {
    const fill = row.querySelector(`[data-bar="${k}"]`);
    const text = row.querySelector(`[data-bv="${k}"]`);
    if (fill) {
      const w = `${Math.max(0, Math.min(100, pct))}%`;
      if (fill.style.width !== w) fill.style.width = w;
      const newCls = `bar-fill ${cls}`.trim();
      if (fill.className !== newCls) fill.className = newCls;
    }
    if (text && text.textContent !== label) text.textContent = label;
  };
  setBar('batt', batt != null ? batt : 0, battClass, batt != null ? `${batt}%` : '—');
  setBar('link', linkPct, linkClass, `${linkPct}%`);
  setBar('pyld', (pyld / 5) * 100, 'amber', `${pyld.toFixed(1)}kg`);

  // Mission progress
  const mission = [...state.missions.values()].find(
    (m) => m.vehicle_id === id && m.status !== 'COMPLETED' && m.status !== 'ABORTED'
  );
  const wrap = row.querySelector('[data-mission-wrap]');
  if (mission) {
    const cur = mission.progress_current ?? 0;
    const tot = mission.progress_total ?? 0;
    const pct = tot > 0 ? Math.round(100 * cur / tot) : 0;
    wrap.style.display = '';
    wrap.querySelector('[data-mission-bar]').style.width = `${pct}%`;
    wrap.querySelector('[data-mission-count]').textContent = `${cur}/${tot}`;
  } else {
    wrap.style.display = 'none';
  }

  document.getElementById('fleet-count').textContent = state.drones.size;
}

function flyToDrone(id) {
  const d = state.drones.get(id);
  if (!d || d.lat == null) return;
  viewer.camera.flyTo({
    destination: Cesium.Cartesian3.fromDegrees(d.lon, d.lat - 0.002, Math.max(400, (d.alt || 0) + 350)),
    orientation: { heading: 0, pitch: Cesium.Math.toRadians(-40), roll: 0 },
    duration: 1.2,
  });
}

function updateKpis() {
  const drones = [...state.drones.values()];
  const total = drones.length;
  const avail = drones.filter((d) => d.status === 'IDLE' || d.status === 'ACTIVE').length;
  const flying = drones.filter((d) => d.status === 'FLYING').length;
  document.getElementById('kpi-active').textContent = `${avail}/${total}`;
  document.getElementById('kpi-flight').textContent = String(flying);
  document.getElementById('kpi-missions').textContent = String(state.completedMissions.size);
}

function setLink(text, level) {
  const box = document.getElementById('kpi-link-box');
  box.classList.remove('error');
  if (level === 'err') box.classList.add('error');
  // Показываем РЕАЛЬНЫЙ статус WS-линка (ONLINE/OFFLINE/ERROR), без подмены из конфига.
  document.getElementById('kpi-link').textContent = text;
}

async function refreshFleet() {
  try {
    const res = await fetch('/api/fleet');
    const data = await res.json();
    (data.fleet || []).forEach((d) => {
      upsertDrone(d.id, d);
      renderDrone(d.id);
    });
    updateKpis();
  } catch { /* silent */ }
}

// Восстановление карты после перезагрузки страницы: вейпоинты активных миссий
// и траектории дронов берём из бэкенд-снапшота (как drawings — но с сервера).
async function restoreSessionSnapshot() {
  try {
    const r = await fetch('/api/session/snapshot');
    const snap = await r.json();
    (snap.missions || []).forEach((m) => {
      if (m.mission_id && Array.isArray(m.waypoints) && m.waypoints.length) {
        drawWaypoints(m.mission_id, m.waypoints, m.mission_type, m.takeoff_profile);
      }
    });
    const trails = snap.trails || {};
    Object.keys(trails).forEach((id) => {
      const pts = trails[id];
      if (Array.isArray(pts) && pts.length >= 2) {
        state.trajectories.set(id, pts.map((p) => [p[0], p[1], p[2] || 0]));
        renderTrail(id);
      }
    });
    // синхронизируем видимость трейлов с настройкой
    state.entities.trails.forEach((e) => { if (e) e.show = !!(state.settings && state.settings.show_drone_trails); });
  } catch (e) { /* ignore */ }
}

async function refreshMissions() {
  try {
    const res = await fetch('/api/active_missions');
    const data = await res.json();
    const ms = data.missions || [];
    state.missions = new Map(ms.map((m) => [m.mission_id, m]));
    renderMissionsList();
    document.getElementById('missions-count').textContent = ms.length;
    setText('kpi-active-missions', String(ms.length));  // шапка · Active
    state.drones.forEach((_, id) => updateCard(id));
  } catch { /* silent */ }
}

// Tab.3 — Active Missions: одна строка на миссию, прогресс ИНЛАЙНОМ рядом с задачей.
// Колонки: ID · UAS · Type (реальный тип миссии) · Status · Progress (бар + %).
function renderMissionsList() {
  const tbody = document.getElementById('taskings-tbody');
  const missions = [...state.missions.values()];

  if (missions.length === 0) {
    tbody.innerHTML = '<tr class="tt-empty"><td colspan="5">— no active missions —</td></tr>';
    return;
  }

  tbody.innerHTML = '';
  missions.forEach((m) => {
    const cur = m.progress_current ?? 0;
    const tot = m.progress_total ?? 0;
    const done = m.status === 'COMPLETED';
    const pct = done ? 100 : (tot > 0 ? Math.round(100 * cur / tot) : 0);
    const mtype = (m.mission_type || 'delivery');
    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td class="mono tt-id">${short(m.mission_id)}</td>
      <td class="mono tt-uas">${esc(m.vehicle_id || '—')}</td>
      <td class="tt-type"><span class="ms-type-dot" style="background:${MISSION_TYPE_COLOR[mtype] || '#1d4ed8'}"></span>${esc(mtype)}</td>
      <td class="tt-status"><span class="chip ${m.status}">${shortStatus(m.status)}</span></td>
      <td class="tt-prog">
        <div class="tt-prog-track">
          <div class="tt-prog-bar gantt-${done ? 'COMPLETED' : (m.status || 'IDLE')}" style="width:${pct}%"></div>
          <span class="tt-prog-label mono">${tot > 0 ? pct + '%' : '—'}</span>
        </div>
      </td>
    `;
    tbody.appendChild(tr);
  });
}

// Сократить длинные status'ы для таблицы — IN_PROGRESS → IN-PRG и т.п.
function shortStatus(s) {
  if (!s) return '—';
  return ({
    IN_PROGRESS: 'IN-PRG',
    UPLOAD_FAILED: 'UP-FAIL',
    START_FAILED: 'ST-FAIL',
    COMPLETED: 'DONE',
    ASSIGNED: 'ASGN',
    UPLOADED: 'UPLD',
    PLANNED: 'PLAN',
  })[s] || s;
}

// =================================================================
// SESSION MISSIONS LOG (вкладка MISSIONS)
// =================================================================
// Накапливает ВСЕ миссии сессии (включая завершённые, которые исчезают из
// активного списка). Открывается кнопкой MISSIONS, по клику на строку —
// детальное окно с пометками оператора.
function updateSessionMission(mid, fields) {
  if (!mid) return;
  const now = Date.now();
  const prev = state.sessionMissions.get(mid) || {
    mission_id: mid, first_seen: now, mission_type: 'delivery',
    status: 'PLANNED', current: 0, total: 0, notes: null, vehicle_id: null,
  };
  // notes/wp_count пишем только если пришли непустыми (не затираем поздними событиями)
  const merged = { ...prev };
  for (const [k, v] of Object.entries(fields)) {
    if (v === null || v === undefined) continue;
    merged[k] = v;
  }
  merged.updated = now;
  state.sessionMissions.set(mid, merged);
  // если открыт лог — перерисуем
  const bg = document.getElementById('missions-modal-bg');
  if (bg && !bg.classList.contains('hidden')) renderMissionsLog();
}

function _missionPct(m) {
  if (m.status === 'COMPLETED') return 100;
  return m.total > 0 ? Math.round(100 * (m.current || 0) / m.total) : 0;
}

function openMissionsLog() {
  renderMissionsLog();
  document.getElementById('missions-modal-bg')?.classList.remove('hidden');
}
function closeMissionsLog() {
  document.getElementById('missions-modal-bg')?.classList.add('hidden');
}

function renderMissionsLog() {
  const tbody = document.getElementById('missions-log-tbody');
  const cnt = document.getElementById('missions-log-count');
  if (!tbody) return;
  const items = [...state.sessionMissions.values()];
  if (cnt) cnt.textContent = items.length;
  if (items.length === 0) {
    tbody.innerHTML = '<tr class="ms-empty"><td colspan="6">— no missions this session —</td></tr>';
    return;
  }
  // активные сверху, завершённые/отменённые вниз; внутри — по времени (новые выше)
  const rank = (s) => (s === 'COMPLETED' || String(s).includes('FAILED') || s === 'ABORTED') ? 1 : 0;
  items.sort((a, b) => rank(a.status) - rank(b.status) || (b.first_seen - a.first_seen));
  tbody.innerHTML = '';
  items.forEach((m) => {
    const pct = _missionPct(m);
    const tr = document.createElement('tr');
    tr.className = 'ms-row';
    tr.innerHTML = `
      <td class="mono ms-id">${short(m.mission_id)}</td>
      <td class="ms-type"><span class="ms-type-dot" style="background:${MISSION_TYPE_COLOR[m.mission_type] || '#1d4ed8'}"></span>${esc(m.mission_type)}</td>
      <td class="mono">${esc(m.vehicle_id || '—')}</td>
      <td><span class="chip ${m.status}">${shortStatus(m.status)}</span></td>
      <td class="mono">${pct}%</td>
      <td class="ms-notes-cell">${m.notes ? '📝' : '—'}</td>
    `;
    tr.addEventListener('click', () => openMissionDetail(m.mission_id));
    tbody.appendChild(tr);
  });
}

function openMissionDetail(mid) {
  const m = state.sessionMissions.get(mid);
  if (!m) return;
  document.getElementById('md-id').textContent = short(mid);
  const body = document.getElementById('mission-detail-body');
  const pct = _missionPct(m);
  const seen = new Date(m.first_seen);
  const pad = (n) => String(n).padStart(2, '0');
  const tstr = `${pad(seen.getHours())}:${pad(seen.getMinutes())}:${pad(seen.getSeconds())}`;
  const row = (l, v) => `<div class="md-row"><span class="md-l">${l}</span><span class="md-v mono">${v}</span></div>`;
  body.innerHTML = `
    ${row('Mission ID', esc(m.mission_id))}
    ${row('Type', `<span class="ms-type-dot" style="background:${MISSION_TYPE_COLOR[m.mission_type] || '#1d4ed8'}"></span>${esc(m.mission_type)}`)}
    ${row('Priority', esc(m.priority || 'normal'))}
    ${row('Vehicle', esc(m.vehicle_id || '—'))}
    ${row('Status', `<span class="chip ${m.status}">${shortStatus(m.status)}</span>`)}
    ${row('Progress', `${m.current || 0}/${m.total || 0} · ${pct}%`)}
    ${row('Waypoints', m.wp_count != null ? m.wp_count : '—')}
    ${row('Started', tstr)}
    <div class="md-notes-block">
      <div class="md-l">Operator notes</div>
      <div class="md-notes">${m.notes ? esc(m.notes) : '<span class="md-empty">— none —</span>'}</div>
    </div>
  `;
  document.getElementById('mission-detail-bg')?.classList.remove('hidden');
}
function closeMissionDetail() {
  document.getElementById('mission-detail-bg')?.classList.add('hidden');
}

// =================================================================
// CLOCK + COMPASS
// =================================================================
function tickClock() {
  const d = new Date();
  const pad = (n) => String(n).padStart(2, '0');
  document.getElementById('clock-time').textContent =
    `${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}:${pad(d.getUTCSeconds())}`;

  // DTG (Date-Time Group) — military format: DDHHMMZ MMM YY
  // e.g. 091823Z MAY 26
  const dtgEl = document.getElementById('dh-dtg');
  if (dtgEl) {
    const months = ['JAN','FEB','MAR','APR','MAY','JUN','JUL','AUG','SEP','OCT','NOV','DEC'];
    const dtg = `${pad(d.getUTCDate())}${pad(d.getUTCHours())}${pad(d.getUTCMinutes())}Z ${months[d.getUTCMonth()]} ${String(d.getUTCFullYear()).slice(-2)}`;
    dtgEl.textContent = dtg;
  }

  // UPTIME — время с момента загрузки страницы (сессия), T+ЧЧ:ММ
  const upS = Math.floor((Date.now() - SESSION_START) / 1000);
  const uh = Math.floor(upS / 3600);
  const um = Math.floor((upS % 3600) / 60);
  setText('dh-uptime', uh > 0 ? `T+${uh}:${pad(um)}` : `T+${pad(um)}:${pad(upS % 60)}`);

  const h = Cesium.Math.toDegrees(viewer.camera.heading);
  const norm = Math.round(((h % 360) + 360) % 360);
  document.getElementById('hud-heading').textContent = String(norm).padStart(3, '0') + '°';
}

// =================================================================
// EVENT LOG
// =================================================================
function logEvent(veh, msg, level = '') {
  const log = document.getElementById('event-log');
  const d = new Date();
  const pad = (n) => String(n).padStart(2, '0');
  const ts = `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
  const row = document.createElement('div');
  row.className = `event-row ${level || ''}`;
  row.innerHTML = `
    <span class="event-ts">${ts}</span>
    <span class="event-veh">${esc(veh)}</span>
    <span class="event-msg">${esc(msg)}</span>
  `;
  log.insertBefore(row, log.firstChild);
  while (log.children.length > 250) log.removeChild(log.lastChild);
}

// =================================================================
// MODAL / PICK
// =================================================================
function openModal() {
  // защита: одновременно рисование и pick-coords не может быть активно
  if (state.drawingTool) cancelDrawing();
  refreshDroneSelect();
  refreshModalHint();
  refreshMissionDraftDisplay();
  refreshMissionEstimate();
  document.getElementById('modal-bg').classList.remove('hidden');
}

// Заполняет <select id="m-drone"> текущим флотом — auto-assign + список
function refreshDroneSelect() {
  const sel = document.getElementById('m-drone');
  if (!sel) return;
  const prev = sel.value;
  const drones = [...state.drones.values()];
  let html = '<option value="">Auto-assign · pick closest available</option>';
  drones.forEach((d) => {
    const status = d.status || 'IDLE';
    const free = ['IDLE', 'ACTIVE'].includes(status);
    const label = `${(d.name || d.id).toUpperCase()} · ${status}${free ? '' : ' (busy)'}`;
    html += `<option value="${esc(d.id)}"${free ? '' : ' disabled'}>${esc(label)}</option>`;
  });
  sel.innerHTML = html;
  if (prev && [...sel.options].some((o) => o.value === prev)) sel.value = prev;
}

function refreshModalHint() {
  const hint = document.getElementById('modal-foot-hint');
  if (!hint) return;
  const droneEl = document.getElementById('m-drone');
  const priority = document.querySelector('#m-priority-seg .seg-mini-btn.active')?.dataset.priority || 'normal';
  const typeBtn = document.querySelector('#m-type-seg .seg-mini-btn.active');
  const type = typeBtn?.textContent.trim() || 'Delivery';
  const isSector = typeBtn?.dataset.type === 'sector';
  const drone = isSector
    ? `${+document.getElementById('m-n-drones')?.value || 3} drones (auto)`
    : (droneEl?.value
        ? (droneEl.options[droneEl.selectedIndex].text.split(' · ')[0])
        : 'Auto-assign');
  hint.textContent = `${drone} · ${priority.charAt(0).toUpperCase() + priority.slice(1)} · ${type}`;
}
function closeModal() {
  document.getElementById('modal-bg').classList.add('hidden');
  cancelPick();
  saveStore();   // persist modal-closed state
}
function startPick(t) {
  state.pickingFor = t;
  document.getElementById('pick-from').classList.toggle('active', t === 'from');
  document.getElementById('pick-to').classList.toggle('active', t === 'to');
  const label = t === 'from' ? 'PICKUP' : t === 'to' ? 'DROP' : t === 'isr' ? 'ISR TARGET' : t.toUpperCase();
  document.getElementById('pick-mode-label').textContent = label;
  document.getElementById('modal-bg').classList.add('hidden');
  document.getElementById('pick-banner').classList.remove('hidden');
}
function cancelPick() {
  state.pickingFor = null;
  document.getElementById('pick-from').classList.remove('active');
  document.getElementById('pick-to').classList.remove('active');
  document.getElementById('pick-banner').classList.add('hidden');
}

// =================================================================
// WIRING
// =================================================================
function wireUI() {
  document.getElementById('fab-order').onclick = openModal;
  document.getElementById('modal-close').onclick = closeModal;
  document.getElementById('modal-cancel').onclick = closeModal;
  document.getElementById('modal-submit').onclick = submitOrder;

  // Missions log (session)
  document.getElementById('btn-missions')?.addEventListener('click', openMissionsLog);
  document.getElementById('missions-close')?.addEventListener('click', closeMissionsLog);
  document.getElementById('missions-modal-bg')?.addEventListener('click', (e) => {
    if (e.target.id === 'missions-modal-bg') closeMissionsLog();
  });
  document.getElementById('mission-detail-close')?.addEventListener('click', closeMissionDetail);
  document.getElementById('mission-detail-bg')?.addEventListener('click', (e) => {
    if (e.target.id === 'mission-detail-bg') closeMissionDetail();
  });

  // Priority seg-mini buttons
  document.querySelectorAll('#m-priority-seg .seg-mini-btn').forEach((b) => {
    b.addEventListener('click', () => {
      if (b.disabled) return;  // приоритеты кроме Normal временно отключены
      document.querySelectorAll('#m-priority-seg .seg-mini-btn').forEach((x) => x.classList.remove('active'));
      b.classList.add('active');
      refreshModalHint();
    });
  });

  // Mission type seg-mini buttons (Delivery/ISR/Patrol/Sector)
  document.querySelectorAll('#m-type-seg .seg-mini-btn').forEach((b) => {
    b.addEventListener('click', () => {
      if (b.disabled) return;  // Sector observation временно отключён
      document.querySelectorAll('#m-type-seg .seg-mini-btn').forEach((x) => x.classList.remove('active'));
      b.classList.add('active');
      const type = b.dataset.type;
      // Update legacy select
      const sel = document.getElementById('m-type');
      if (sel) sel.value = type;
      // Show appropriate type-fields, hide others
      document.querySelectorAll('.type-fields[data-type-show]').forEach((el) => {
        el.classList.toggle('hidden', el.dataset.typeShow !== type);
      });
      refreshModalHint();
      refreshMissionEstimate();
    });
  });

  // Pattern seg buttons (sector only — Search больше не существует)
  document.querySelectorAll('#m-pattern-sec-seg .seg-mini-btn').forEach((b) => {
    b.addEventListener('click', () => {
      const parent = b.closest('.seg-mini');
      parent.querySelectorAll('.seg-mini-btn').forEach((x) => x.classList.remove('active'));
      b.classList.add('active');
      refreshMissionEstimate();
    });
  });

  // Inputs that affect mission estimate (auto-recompute coverage/wp count)
  ['m-swath-sec', 'm-n-drones', 'm-sec-loops', 'm-loops', 'm-loiter-wp'].forEach((id) => {
    const el = document.getElementById(id);
    if (el) el.addEventListener('input', refreshMissionEstimate);
  });

  // Pick-route / pick-sector buttons (Search PICK AREA удалён)
  document.getElementById('pick-route-btn')?.addEventListener('click', () => beginPickFromModal('route'));
  document.getElementById('pick-sector-btn')?.addEventListener('click', () => beginPickFromModal('sector'));
  document.getElementById('clear-route-btn')?.addEventListener('click', () => {
    state.missionDraft.route = null;
    refreshMissionDraftDisplay();
  });
  document.getElementById('clear-sector-btn')?.addEventListener('click', () => {
    state.missionDraft.sector = null;
    refreshMissionDraftDisplay();
    refreshMissionEstimate();
  });

  // ISR's own "PICK" button writes into isr-lat/isr-lon (similar to pick-from/pick-to)
  document.querySelector('[data-pick-into="isr"]')?.addEventListener('click', () => startPick('isr'));

  // Live hint update
  ['m-drone'].forEach((id) => {
    const el = document.getElementById(id);
    if (el) el.addEventListener('change', refreshModalHint);
  });

  document.getElementById('pick-from').onclick = () => startPick('from');
  document.getElementById('pick-to').onclick = () => startPick('to');

  document.getElementById('btn-perspective').onclick = () => flyToBase();
  document.getElementById('btn-top-down').onclick = () => {
    viewer.camera.flyTo({
      destination: Cesium.Cartesian3.fromDegrees(state.base.lon, state.base.lat, 3000),
      orientation: { heading: 0, pitch: Cesium.Math.toRadians(-90), roll: 0 },
      duration: 1.2,
    });
  };
  document.getElementById('btn-home').onclick = () => flyToBase();

  document.getElementById('btn-clear-events').onclick = () => {
    document.getElementById('event-log').innerHTML = '';
  };

  document.querySelectorAll('.sandbox-btn').forEach((b) => {
    b.onclick = () => logEvent('sandbox', `action: ${b.dataset.action} · stub`, 'warn');
  });

  // map click → fill input (single-coord picks: from / to / isr)
  const handler = new Cesium.ScreenSpaceEventHandler(viewer.canvas);
  handler.setInputAction((click) => {
    if (!state.pickingFor) return;
    const ray = viewer.camera.getPickRay(click.position);
    const cart = ray && viewer.scene.globe.pick(ray, viewer.scene);
    if (!cart) return;
    const c = Cesium.Cartographic.fromCartesian(cart);
    const lat = Cesium.Math.toDegrees(c.latitude);
    const lon = Cesium.Math.toDegrees(c.longitude);
    if (state.pickingFor === 'from') {
      document.getElementById('from-lat').value = lat.toFixed(6);
      document.getElementById('from-lon').value = lon.toFixed(6);
    } else if (state.pickingFor === 'to') {
      document.getElementById('to-lat').value = lat.toFixed(6);
      document.getElementById('to-lon').value = lon.toFixed(6);
    } else if (state.pickingFor === 'isr') {
      document.getElementById('isr-lat').value = lat.toFixed(6);
      document.getElementById('isr-lon').value = lon.toFixed(6);
    }
    cancelPick();
    openModal();
  }, Cesium.ScreenSpaceEventType.LEFT_CLICK);

  document.addEventListener('keydown', (e) => {
    if (e.key !== 'Escape') return;
    if (state.missionPicking) { cancelMissionPick(); return; }
    if (state.pickingFor) { cancelPick(); openModal(); return; }
    const mdBg = document.getElementById('mission-detail-bg');
    if (mdBg && !mdBg.classList.contains('hidden')) { closeMissionDetail(); return; }
    const msBg = document.getElementById('missions-modal-bg');
    if (msBg && !msBg.classList.contains('hidden')) { closeMissionsLog(); return; }
    if (!document.getElementById('modal-bg').classList.contains('hidden')) closeModal();
  });
}

async function submitOrder() {
  // Общие поля
  const droneId = document.getElementById('m-drone')?.value || '';
  const priority = document.querySelector('#m-priority-seg .seg-mini-btn.active')?.dataset.priority || 'normal';
  const type = document.getElementById('m-type')?.value || 'delivery';
  // Sector observation временно отключён.
  if (type === 'sector') {
    logEvent('orch', 'Sector observation is temporarily unavailable', 'error');
    return;
  }
  const cruiseAlt = +document.getElementById('m-cruise-alt')?.value || 60;
  // Скорость авто: единый безопасный оптимум применяется ко всем горизонтальным
  // участкам (см. AUTO_CRUISE_SPEED). Пользователь её не задаёт.
  const cruiseSpeed = AUTO_CRUISE_SPEED;
  const takeoffAlt = +document.getElementById('m-takeoff-alt')?.value || 15;
  const takeoffProfile = document.getElementById('m-takeoff-profile')?.value || 'vertical';
  const autoRth = !!document.getElementById('m-auto-rth')?.checked;
  const notes = (document.getElementById('m-notes')?.value || '').trim();

  const base = state.base;  // {lat, lon}

  // Билдим N orders в зависимости от типа
  const orders = [];

  if (type === 'delivery') {
    const fLat = +document.getElementById('from-lat').value;
    const fLon = +document.getElementById('from-lon').value;
    const tLat = +document.getElementById('to-lat').value;
    const tLon = +document.getElementById('to-lon').value;
    if (!fLat || !fLon || !tLat || !tLon) {
      logEvent('orch', 'Delivery: missing PICKUP/DROP coordinates', 'error');
      return;
    }
    const wps = buildDeliveryWaypoints({
      pickup: { lat: fLat, lon: fLon },
      drop:   { lat: tLat, lon: tLon },
      base, cruiseAlt, cruiseSpeed, autoRth,
    });
    orders.push({
      from: { lat: fLat, lon: fLon, alt: cruiseAlt },
      to:   { lat: tLat, lon: tLon, alt: cruiseAlt },
      weight: 0,
      drone_id: droneId || null,
      mission_type: 'delivery',
      waypoints: wps,
    });

  } else if (type === 'isr') {
    const lat = +document.getElementById('isr-lat').value;
    const lon = +document.getElementById('isr-lon').value;
    if (!lat || !lon) {
      logEvent('orch', 'ISR: missing TARGET coordinate', 'error');
      return;
    }
    const loiterS = +document.getElementById('m-loiter-s').value || 60;
    const orbitR = +document.getElementById('m-orbit-radius').value || 0;
    const wps = buildISRWaypoints({
      target: { lat, lon },
      loiter_s: loiterS,
      orbit_radius: orbitR,
      base, cruiseAlt, cruiseSpeed, autoRth,
    });
    orders.push({
      from: { lat, lon, alt: cruiseAlt },
      to:   { lat, lon, alt: cruiseAlt },
      weight: 0,
      drone_id: droneId || null,
      mission_type: 'isr',
      waypoints: wps,
      orbit_radius_m: orbitR,
    });

  } else if (type === 'patrol') {
    const r = state.missionDraft.route;
    if (!r || r.length < 2) {
      logEvent('orch', 'Patrol: route required (≥ 2 waypoints)', 'error');
      return;
    }
    const loops = Math.max(1, Math.min(20, +document.getElementById('m-loops').value || 1));
    const loiterWp = +document.getElementById('m-loiter-wp').value || 0;
    const wps = buildPatrolWaypoints({
      route: r,
      loops,
      loiter_wp: loiterWp,
      base, cruiseAlt, cruiseSpeed, autoRth,
    });
    orders.push({
      from: { lat: r[0][0], lon: r[0][1], alt: cruiseAlt },
      to:   { lat: r[r.length-1][0], lon: r[r.length-1][1], alt: cruiseAlt },
      weight: 0,
      drone_id: droneId || null,
      mission_type: 'patrol',
      waypoints: wps,
      loop_count: loops,
    });

  } else if (type === 'sector') {
    const s = state.missionDraft.sector;
    if (!s || s.length < 3) {
      logEvent('orch', 'Sector: area required (≥ 3 vertices)', 'error');
      return;
    }
    const n = Math.max(2, Math.min(8, +document.getElementById('m-n-drones').value || 3));
    const swath = +document.getElementById('m-swath-sec').value || 80;
    const loops = Math.max(1, Math.min(20, +document.getElementById('m-sec-loops').value || 1));
    const pattern = document.querySelector('#m-pattern-sec-seg .seg-mini-btn.active')?.dataset.pattern || 'lawnmower';

    // Берём N свободных дронов — СТРОГО IDLE
    const free = [...state.drones.values()].filter((d) => (d.status || 'IDLE') === 'IDLE');
    if (free.length < n) {
      const total = state.drones.size;
      logEvent('orch', `Sector: need ${n} IDLE drones, only ${free.length} of ${total} are idle`, 'error');
      return;
    }
    const picked = free.slice(0, n);
    const splits = splitSectorForDrones(s, n);
    for (let i = 0; i < n; i++) {
      const sub = splits[i];
      const wps = buildSectorWaypoints({
        subArea: sub, pattern, swath, loops,
        base, cruiseAlt, cruiseSpeed, autoRth,
      });
      if (!wps.length) continue;
      orders.push({
        from: { lat: wps[0].lat, lon: wps[0].lon, alt: cruiseAlt },
        to:   { lat: wps[wps.length-1].lat, lon: wps[wps.length-1].lon, alt: cruiseAlt },
        weight: 0,
        drone_id: picked[i].id,
        mission_type: 'sector',
        waypoints: wps,
        pattern,
        sector_polygon: s,
        sector_index: i,
        sector_total: n,
        loop_count: loops,
      });
    }
  }

  if (!orders.length) {
    logEvent('orch', 'no orders to dispatch', 'error');
    return;
  }

  // Шлём все orders на /api/orders
  let success = 0;
  for (const o of orders) {
    try {
      const res = await fetch('/api/orders', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          from: o.from,
          to: o.to,
          weight: o.weight,
          drone_id: o.drone_id,
          priority,
          mission_type: o.mission_type,
          cruise_alt_m: cruiseAlt,
          cruise_speed_mps: cruiseSpeed,
          takeoff_alt_m: takeoffAlt,
          takeoff_profile: takeoffProfile,
          auto_rth: autoRth,
          notes: notes || null,
          waypoints: o.waypoints || null,
          loop_count: o.loop_count || null,
          orbit_radius_m: o.orbit_radius_m || null,
          pattern: o.pattern || null,
          sector_polygon: o.sector_polygon || null,
          sector_index: o.sector_index ?? null,
          sector_total: o.sector_total ?? null,
        }),
      });
      const data = await res.json();
      if (data.order_id) success++;
    } catch (e) {
      logEvent('orch', `dispatch failed: ${e.message}`, 'error');
    }
  }

  const tail = orders.length > 1 ? ` (${success}/${orders.length} drones)` : '';
  logEvent('orch', `${type.toUpperCase()} dispatched · ${priority}${tail}`, 'success');

  // Reset draft
  state.missionDraft = { route: null, sector: null };
  saveStore();
  closeModal();
}

// =================================================================
// UTIL
// =================================================================
function fmt(v, d) { return (v == null || isNaN(v)) ? '—' : (+v).toFixed(d); }
function short(s) { return !s ? '—' : String(s).slice(0, 12); }
function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}

// Подписка на input/change events во всём modal'е — автосохранение state.
function wireMissionFormAutosave() {
  const modal = document.getElementById('modal-bg');
  if (!modal) return;
  let saveTimer = null;
  const triggerSave = () => {
    if (saveTimer) clearTimeout(saveTimer);
    saveTimer = setTimeout(() => { saveStore(); saveTimer = null; }, 400);
  };
  modal.addEventListener('input', triggerSave);
  modal.addEventListener('change', triggerSave);
  // Также при клике на seg-mini-btn (priority/type/pattern)
  modal.querySelectorAll('.seg-mini-btn').forEach((b) => {
    b.addEventListener('click', triggerSave);
  });
}

// =================================================================
// MISSION WAYPOINT BUILDERS — фазированные waypoints под каждый тип
// =================================================================
// Все builder'ы возвращают массив waypoints с полями:
//   { lat, lon, alt, kind?, speed_m_s, loiter_s? }
// Bridge: kind=NAV (default) - fly_through; kind=LAND - приземление.
//
// Takeoff handled bridge'ом отдельно (action.takeoff(takeoff_alt)) ДО старта миссии.
// PX4 затем navigate'ит к первому WP миссии, плавно набирая высоту от takeoff_alt до alt_first_wp.

// Авто-скорость: единый безопасный оптимум для горизонтального полёта (м/с).
// Применяется ко ВСЕМ waypoints одинаково. Важно: в MAVSDK MissionItem.speed_m_s
// = «скорость ПОСЛЕ этой точки», поэтому если задавать на спусках 2 м/с, эта
// скорость наследуется следующим крейсерским участком (баг «2 м/с на круизе»).
// Поэтому скорость теперь ЕДИНАЯ; вертикальные спуски (точки с тем же lat/lon)
// ограничиваются вертикальными лимитами PX4, а не горизонтальной скоростью.
const AUTO_CRUISE_SPEED = 10.0;

const DEFAULTS = {
  pickup_alt: 10,         // высота захвата/сброса груза (м)
  hover_pickup_s: 3,      // hover на pickup/drop (сек)
  approach_alt: 10,       // высота approach над базой перед LAND (м)
  orbit_speed: 5.0,       // скорость по орбите ISR (m/s)
};

// Генератор waypoints для RTH (return to base + approach + LAND).
// Скорость единая (cruiseSpeed) на всех точках — спуски вертикальные, их темп
// задаёт автопилот, а горизонтальная скорость не наследуется в кривом виде.
function _rthLand(base, cruiseAlt, cruiseSpeed) {
  return [
    { lat: base.lat, lon: base.lon, alt: cruiseAlt, speed_m_s: cruiseSpeed },
    { lat: base.lat, lon: base.lon, alt: DEFAULTS.approach_alt, speed_m_s: cruiseSpeed },
    { lat: base.lat, lon: base.lon, alt: 0, kind: 'LAND', speed_m_s: cruiseSpeed },
  ];
}

// LAND на месте — если auto_rth=false, миссия завершается там, где остановилась
function _landHere(lastPos) {
  return [{ lat: lastPos.lat, lon: lastPos.lon, alt: 0, kind: 'LAND', speed_m_s: AUTO_CRUISE_SPEED }];
}

// DELIVERY: transit → descend → hover → climb → transit → descend → hover → climb → RTH+LAND
function buildDeliveryWaypoints({ pickup, drop, base, cruiseAlt, cruiseSpeed, autoRth }) {
  const wps = [];
  // Phase: transit to pickup (climbs from takeoff_alt to cruiseAlt during this leg)
  wps.push({ lat: pickup.lat, lon: pickup.lon, alt: cruiseAlt, speed_m_s: cruiseSpeed });
  // Phase: descend + hover at pickup (cargo grab simulation)
  wps.push({ lat: pickup.lat, lon: pickup.lon, alt: DEFAULTS.pickup_alt, speed_m_s: cruiseSpeed, loiter_s: DEFAULTS.hover_pickup_s });
  // Phase: climb back to cruise
  wps.push({ lat: pickup.lat, lon: pickup.lon, alt: cruiseAlt, speed_m_s: cruiseSpeed });
  // Phase: transit to drop
  wps.push({ lat: drop.lat, lon: drop.lon, alt: cruiseAlt, speed_m_s: cruiseSpeed });
  // Phase: descend + hover at drop
  wps.push({ lat: drop.lat, lon: drop.lon, alt: DEFAULTS.pickup_alt, speed_m_s: cruiseSpeed, loiter_s: DEFAULTS.hover_pickup_s });
  // Phase: climb back
  wps.push({ lat: drop.lat, lon: drop.lon, alt: cruiseAlt, speed_m_s: cruiseSpeed });
  // Phase: RTH or land here
  if (autoRth) wps.push(..._rthLand(base, cruiseAlt, cruiseSpeed));
  else         wps.push(..._landHere(drop));
  return wps;
}

// ISR: transit → surveillance (orbit ИЛИ hover на месте) → RTH+LAND.
//
// Орбита и зависание исполняются НАТИВНО в bridge (do_orbit / goto+hover),
// а НЕ генерацией многоугольника из вейпоинтов. Поэтому здесь мы шлём ОДНУ
// семантическую точку наблюдения:
//   - orbit_radius>0 → kind:'ORBIT' с orbit_radius_m и loiter_s (длительность);
//   - orbit_radius=0 → обычный NAV с loiter_s (зависание на месте).
// Bridge сам держит высоту — mission-loiter PX4 для мультикоптера сломан.
function buildISRWaypoints({ target, loiter_s, orbit_radius, base, cruiseAlt, cruiseSpeed, autoRth }) {
  const wps = [];
  // Transit to target
  wps.push({ lat: target.lat, lon: target.lon, alt: cruiseAlt, speed_m_s: cruiseSpeed });
  // Surveillance
  if (orbit_radius > 0) {
    wps.push({
      lat: target.lat, lon: target.lon, alt: cruiseAlt,
      kind: 'ORBIT', orbit_radius_m: orbit_radius,
      speed_m_s: DEFAULTS.orbit_speed, loiter_s,
    });
  } else {
    // Зависание над точкой (hover) на cruiseAlt.
    wps.push({ lat: target.lat, lon: target.lon, alt: cruiseAlt, speed_m_s: cruiseSpeed, loiter_s });
  }
  // RTH / land
  if (autoRth) wps.push(..._rthLand(base, cruiseAlt, cruiseSpeed));
  else         wps.push(..._landHere(target));
  return wps;
}

// PATROL: проход по маршруту вейпоинтов × loops, с зависанием (hover) на
// каждой точке заданное время → RTH+LAND.
//
// Зависание исполняется НАТИВНО в bridge (goto точки + hover на месте) —
// просто помечаем точку loiter_s>0. Никаких микро-кружков: дрон реально
// стоит на месте. PX4 mission-loiter для мультикоптера сломан (садит дрон),
// поэтому patrol идёт через нативный executor (см. NATIVE_MISSION_TYPES).
function buildPatrolWaypoints({ route, loops, loiter_wp, base, cruiseAlt, cruiseSpeed, autoRth }) {
  const wps = [];
  for (let l = 0; l < loops; l++) {
    for (const [lat, lon] of route) {
      wps.push({
        lat, lon, alt: cruiseAlt, speed_m_s: cruiseSpeed,
        // loiter_wp>0 → дрон зависает на точке loiter_wp секунд; иначе пролёт
        loiter_s: loiter_wp > 0 ? loiter_wp : 0,
      });
    }
  }
  if (autoRth) wps.push(..._rthLand(base, cruiseAlt, cruiseSpeed));
  else {
    const last = route[route.length - 1];
    wps.push(..._landHere({ lat: last[0], lon: last[1] }));
  }
  return wps;
}

// SECTOR: для подзоны i-го дрона — pattern × loops → RTH+LAND
function buildSectorWaypoints({ subArea, pattern, swath, loops, base, cruiseAlt, cruiseSpeed, autoRth }) {
  const path = generatePattern(subArea, pattern, swath);
  if (!path.length) return [];
  const wps = [];
  for (let l = 0; l < loops; l++) {
    for (const [lat, lon] of path) {
      wps.push({ lat, lon, alt: cruiseAlt, speed_m_s: cruiseSpeed });
    }
  }
  if (autoRth) wps.push(..._rthLand(base, cruiseAlt, cruiseSpeed));
  else {
    const last = path[path.length - 1];
    wps.push(..._landHere({ lat: last[0], lon: last[1] }));
  }
  return wps;
}

// =================================================================
// MISSION PICKING (route / area / sector) — collect points/polygon from map
// =================================================================
function beginPickFromModal(kind) {
  // kind: 'route' | 'sector'
  state.missionPicking = kind;
  state.drawingPoints = [];
  document.getElementById('modal-bg').classList.add('hidden');
  showMissionPickHint(kind);
  viewer.canvas.style.cursor = 'crosshair';
  drawingsRenderPreview();
}

function cancelMissionPick() {
  state.missionPicking = null;
  state.drawingPoints = [];
  hideMissionPickHint();
  viewer.canvas.style.cursor = '';
  drawingsRenderPreview();
  // Reopen modal
  document.getElementById('modal-bg').classList.remove('hidden');
}

function commitMissionPick() {
  const kind = state.missionPicking;
  const pts = [...state.drawingPoints];
  state.missionPicking = null;
  state.drawingPoints = [];
  hideMissionPickHint();
  viewer.canvas.style.cursor = '';
  drawingsRenderPreview();

  // Минимальные требования: route ≥ 2 точек, sector ≥ 3
  if (kind === 'route' && pts.length >= 2) {
    state.missionDraft.route = pts;
  } else if (kind === 'sector' && pts.length >= 3) {
    state.missionDraft.sector = pts;
  }
  refreshMissionDraftDisplay();
  refreshMissionEstimate();
  document.getElementById('modal-bg').classList.remove('hidden');
  saveStore();
}

function showMissionPickHint(kind) {
  const hint = document.getElementById('mission-pick-hint');
  if (!hint) return;
  let text = '';
  if (kind === 'route') text = `<b>PICK ROUTE</b> · click ≥ 2 waypoints · <span class="kbd-inv">DBL-click</span>/<span class="kbd-inv">Enter</span> finish · <span class="kbd-inv">Esc</span> cancel`;
  else if (kind === 'sector') text = `<b>PICK SECTOR</b> · click ≥ 3 polygon vertices · <span class="kbd-inv">DBL-click</span>/<span class="kbd-inv">Enter</span> finish · <span class="kbd-inv">Esc</span> cancel`;
  hint.innerHTML = text + ` <span class="measure-out" id="mpick-count">0 points</span>`;
  hint.classList.remove('hidden');
}
function updateMissionPickHint() {
  const n = state.drawingPoints.length;
  const out = document.getElementById('mpick-count');
  if (out) out.textContent = `${n} point${n === 1 ? '' : 's'}`;
}
function hideMissionPickHint() {
  const hint = document.getElementById('mission-pick-hint');
  if (hint) hint.classList.add('hidden');
}

// Текст в полях captured route / sector
function refreshMissionDraftDisplay() {
  const r = state.missionDraft.route;
  const s = state.missionDraft.sector;

  const rDisp = document.getElementById('m-route-display');
  if (rDisp) {
    if (r && r.length) {
      const len = polylineLengthM(r);
      rDisp.textContent = `${r.length} WPs · ${(len/1000).toFixed(2)} km`;
      rDisp.classList.add('has-data');
    } else { rDisp.textContent = '— no waypoints —'; rDisp.classList.remove('has-data'); }
  }
  const sDisp = document.getElementById('m-sector-display');
  if (sDisp) {
    if (s && s.length) {
      const km2 = polygonAreaKm2(s);
      sDisp.textContent = `${s.length} vertices · ${km2.toFixed(2)} km²`;
      sDisp.classList.add('has-data');
    } else { sDisp.textContent = '— no sector —'; sDisp.classList.remove('has-data'); }
  }
}

// Estimate: для sector — сколько waypoints и сколько покрытия
function refreshMissionEstimate() {
  const type = document.getElementById('m-type')?.value;
  // Sector
  if (type === 'sector' && state.missionDraft.sector) {
    const swath = +document.getElementById('m-swath-sec')?.value || 80;
    const n = Math.max(2, Math.min(8, +document.getElementById('m-n-drones')?.value || 3));
    const pattern = document.querySelector('#m-pattern-sec-seg .seg-mini-btn.active')?.dataset.pattern || 'lawnmower';
    const splits = splitSectorForDrones(state.missionDraft.sector, n);
    let totalWps = 0;
    splits.forEach((sub) => {
      const wps = generatePattern(sub, pattern, swath);
      totalWps += wps.length;
    });
    const km2 = polygonAreaKm2(state.missionDraft.sector);
    const out = document.getElementById('m-est-coverage');
    if (out) out.textContent = `${km2.toFixed(2)} km² · ~${totalWps} wps total`;
  }
}

// =================================================================
// GEOMETRY HELPERS
// =================================================================
function polylineLengthM(pts) {
  let s = 0;
  for (let i = 1; i < pts.length; i++) {
    s += haversineMeters(pts[i-1][0], pts[i-1][1], pts[i][0], pts[i][1]);
  }
  return s;
}

// Площадь полигона в км² (формула геодезическая, приближенная для малых площадей)
function polygonAreaKm2(pts) {
  if (!pts || pts.length < 3) return 0;
  const R = 6371000;  // м
  let area = 0;
  for (let i = 0; i < pts.length; i++) {
    const [lat1, lon1] = pts[i];
    const [lat2, lon2] = pts[(i + 1) % pts.length];
    area += (lon2 - lon1) * Math.PI / 180
          * (2 + Math.sin(lat1 * Math.PI / 180) + Math.sin(lat2 * Math.PI / 180));
  }
  area = Math.abs(area * R * R / 2);
  return area / 1e6;
}

// Bounding box: {minLat, maxLat, minLon, maxLon}
function bbox(pts) {
  let minLat = Infinity, maxLat = -Infinity, minLon = Infinity, maxLon = -Infinity;
  for (const [lat, lon] of pts) {
    if (lat < minLat) minLat = lat;
    if (lat > maxLat) maxLat = lat;
    if (lon < minLon) minLon = lon;
    if (lon > maxLon) maxLon = lon;
  }
  return { minLat, maxLat, minLon, maxLon };
}

// =================================================================
// PATTERN GENERATORS — area polygon + swath_m + altitude → waypoints
// Возвращает массив [[lat, lon], ...] (alt задаётся отдельно из cruise-alt).
// =================================================================
function generatePattern(area, pattern, swath_m) {
  if (!area || area.length < 3) return [];
  if (pattern === 'lawnmower') return patternLawnmower(area, swath_m);
  if (pattern === 'spiral') return patternSpiral(area, swath_m);
  if (pattern === 'box') return patternBox(area);
  return [];
}

// Lawnmower — параллельные «строчки» по bbox, чередуя направление
function patternLawnmower(area, swath_m) {
  const b = bbox(area);
  const midLat = (b.minLat + b.maxLat) / 2;
  const mPerDegLat = 111111;
  const mPerDegLon = 111111 * Math.cos(midLat * Math.PI / 180);
  const stepLat = swath_m / mPerDegLat;

  const wps = [];
  let leftToRight = true;
  for (let lat = b.minLat + stepLat / 2; lat <= b.maxLat; lat += stepLat) {
    if (leftToRight) {
      wps.push([lat, b.minLon]);
      wps.push([lat, b.maxLon]);
    } else {
      wps.push([lat, b.maxLon]);
      wps.push([lat, b.minLon]);
    }
    leftToRight = !leftToRight;
  }
  return wps;
}

// Spiral — концентрические прямоугольники, сжимающиеся внутрь
function patternSpiral(area, swath_m) {
  const b = bbox(area);
  const midLat = (b.minLat + b.maxLat) / 2;
  const mPerDegLat = 111111;
  const mPerDegLon = 111111 * Math.cos(midLat * Math.PI / 180);
  const stepLat = swath_m / mPerDegLat;
  const stepLon = swath_m / mPerDegLon;

  const wps = [];
  let l = b.minLat, r = b.maxLat, w = b.minLon, e = b.maxLon;
  while (r - l > stepLat && e - w > stepLon) {
    // обход по периметру: SW → NW → NE → SE → SW (inset)
    wps.push([l, w]);
    wps.push([r, w]);
    wps.push([r, e]);
    wps.push([l, e]);
    wps.push([l, w]);
    l += stepLat; r -= stepLat;
    w += stepLon; e -= stepLon;
  }
  return wps;
}

// Box — периметр полигона
function patternBox(area) {
  // Просто вершины + замыкание
  return [...area, area[0]];
}

// =================================================================
// MULTI-DRONE SPLIT (sector observation)
// Делим bbox сектора на N вертикальных полос; каждый дрон получает свою.
// =================================================================
function splitSectorForDrones(sector, n) {
  const b = bbox(sector);
  const stripW = (b.maxLon - b.minLon) / n;
  const sub = [];
  for (let i = 0; i < n; i++) {
    const lonL = b.minLon + i * stripW;
    const lonR = lonL + stripW;
    sub.push([
      [b.minLat, lonL],
      [b.minLat, lonR],
      [b.maxLat, lonR],
      [b.maxLat, lonL],
    ]);
  }
  return sub;
}

// =================================================================
// START
// =================================================================
init().catch((e) => {
  const el = document.getElementById('boot-error');
  el.textContent = `INIT ERROR\n${e.message}`;
  el.classList.remove('hidden');
});
