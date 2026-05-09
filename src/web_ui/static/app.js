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

// =================================================================
// STATE
// =================================================================
const state = {
  drones: new Map(),
  missions: new Map(),
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
  completedToday: 0,
  pickingFor: null,
  mode: 'ops',
  wsConnected: false,
};

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
  try {
    const res = await fetch('/api/base');
    const b = await res.json();
    if (b && b.lat && b.lon) state.base = { lat: +b.lat, lon: +b.lon };
  } catch (e) { /* ignore */ }

  drawBase();
  flyToBase(0);

  setInterval(refreshFleet, 2000);
  setInterval(refreshMissions, 1500);
  setInterval(tickClock, 1000);
  tickClock();

  connectWS();
  wireUI();
  refreshFleet();
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
  if (msg.type === 'telemetry_update' && msg.payload?.lat != null && msg.payload?.lon != null) {
    const id = msg.topic.split('/')[1];
    upsertDrone(id, {
      lat: msg.payload.lat,
      lon: msg.payload.lon,
      alt: msg.payload.alt,
    });
    renderDrone(id);
    return;
  }

  if (msg.type === 'drone_active' && msg.payload?.id) {
    upsertDrone(msg.payload.id, msg.payload);
    renderDrone(msg.payload.id);
    return;
  }

  if (msg.type === 'mission_planned' && msg.payload?.waypoints) {
    const mid = msg.topic.split('/')[1];
    drawWaypoints(mid, msg.payload.waypoints);
    logEvent('orch', `mission ${short(mid)} planned (${msg.payload.waypoints.length} wps)`);
    return;
  }

  if (msg.type === 'mission_assigned' && msg.payload) {
    const mid = msg.topic.split('/')[1];
    logEvent(msg.payload.vehicle_id || 'orch', `${short(mid)} assigned`);
    return;
  }

  if (msg.type === 'mission_progress' && msg.payload) {
    const { vehicle_id, current, total } = msg.payload;
    logEvent(vehicle_id, `wp ${current}/${total}`);
    return;
  }

  if (msg.type === 'mission_status' && msg.payload) {
    const { status, vehicle_id } = msg.payload;
    const mid = msg.topic.split('/')[1];
    const lvl = status === 'COMPLETED' ? 'success'
             : (String(status).includes('FAILED') || status === 'ABORTED') ? 'error'
             : '';
    logEvent(vehicle_id || 'orch', `${short(mid)} → ${status}`, lvl);
    if (status === 'COMPLETED') {
      state.completedToday++;
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
function drawWaypoints(mid, wps) {
  clearWaypoints(mid);
  const arr = [];
  const pathPts = [];

  wps.forEach((wp, idx) => {
    const p = wp.pos || wp;
    const lat = p.lat, lon = p.lon;
    const alt = Math.max(1, p.alt || 1);
    const kind = String(wp.kind || 'NAV').toUpperCase();
    const css = kind === 'TAKEOFF' ? '#b45309'
              : kind === 'LAND' ? '#166534'
              : '#1d4ed8';
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
          color: Cesium.Color.fromCssColorString('#1d4ed8').withAlpha(0.6),
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
    row = document.createElement('div');
    row.className = 'drone-card';
    row.id = `drone-card-${id}`;
    row.addEventListener('click', () => {
      document.querySelectorAll('.drone-card').forEach((c) => c.classList.remove('selected'));
      row.classList.add('selected');
      flyToDrone(id);
    });
    container.appendChild(row);
  }
  const mission = [...state.missions.values()].find(
    (m) => m.vehicle_id === id && m.status !== 'COMPLETED' && m.status !== 'ABORTED'
  );
  const cur = mission?.progress_current ?? 0;
  const tot = mission?.progress_total ?? 0;
  const pct = tot > 0 ? Math.round(100 * cur / tot) : 0;

  row.innerHTML = `
    <div class="drone-card-head">
      <span class="drone-name">${esc(d.name || id)}</span>
      <span class="chip ${d.status || 'IDLE'}">${d.status || 'IDLE'}</span>
    </div>
    <div class="metrics">
      <div class="metric"><span class="metric-label">ALT</span><span class="metric-value">${fmt(d.alt, 1)}m</span></div>
      <div class="metric"><span class="metric-label">LAT</span><span class="metric-value">${fmt(d.lat, 5)}</span></div>
      <div class="metric"><span class="metric-label">LON</span><span class="metric-value">${fmt(d.lon, 5)}</span></div>
    </div>
    ${mission ? `
      <div class="progress-wrap">
        <div class="progress-bar"><span style="width:${pct}%"></span></div>
        <span class="mono">${cur}/${tot}</span>
      </div>` : `<div class="no-mission">no active mission</div>`}
  `;
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
  document.getElementById('kpi-missions').textContent = String(state.completedToday);
}

function setLink(text, level) {
  const box = document.getElementById('kpi-link-box');
  box.classList.remove('error');
  if (level === 'err') box.classList.add('error');
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

async function refreshMissions() {
  try {
    const res = await fetch('/api/active_missions');
    const data = await res.json();
    const ms = data.missions || [];
    state.missions = new Map(ms.map((m) => [m.mission_id, m]));
    renderMissionsList();
    document.getElementById('missions-count').textContent = ms.length;
    state.drones.forEach((_, id) => updateCard(id));
  } catch { /* silent */ }
}

function renderMissionsList() {
  const c = document.getElementById('missions-list');
  if (state.missions.size === 0) {
    c.innerHTML = `<div style="padding:14px;text-align:center;color:var(--text-mute);font-size:11px;letter-spacing:0.1em;">— NO ACTIVE MISSIONS —</div>`;
    return;
  }
  c.innerHTML = '';
  [...state.missions.values()].forEach((m) => {
    const cur = m.progress_current ?? 0;
    const tot = m.progress_total ?? 0;
    const pct = tot > 0 ? Math.round(100 * cur / tot) : 0;
    const row = document.createElement('div');
    row.className = 'mission-row';
    row.innerHTML = `
      <div class="mission-head">
        <span class="mission-id">${short(m.mission_id)}</span>
        <span class="mission-veh">${esc(m.vehicle_id || '—')}</span>
      </div>
      <div class="mission-foot">
        <span class="chip ${m.status}">${m.status}</span>
        <span class="mono">${cur}/${tot}</span>
      </div>
      <div class="progress-bar"><span style="width:${pct}%"></span></div>
    `;
    c.appendChild(row);
  });
}

// =================================================================
// CLOCK + COMPASS
// =================================================================
function tickClock() {
  const d = new Date();
  const pad = (n) => String(n).padStart(2, '0');
  document.getElementById('clock-time').textContent =
    `${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}:${pad(d.getUTCSeconds())}`;
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
function openModal() { document.getElementById('modal-bg').classList.remove('hidden'); }
function closeModal() {
  document.getElementById('modal-bg').classList.add('hidden');
  cancelPick();
}
function startPick(t) {
  state.pickingFor = t;
  document.getElementById('pick-from').classList.toggle('active', t === 'from');
  document.getElementById('pick-to').classList.toggle('active', t === 'to');
  document.getElementById('pick-mode-label').textContent = t === 'from' ? 'PICKUP' : 'DROP';
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

  document.querySelectorAll('.mode-btn').forEach((b) => {
    b.onclick = () => {
      document.querySelectorAll('.mode-btn').forEach((x) => x.classList.remove('active'));
      b.classList.add('active');
      state.mode = b.dataset.mode;
      const dock = document.getElementById('sandbox-dock');
      if (state.mode === 'sandbox') {
        dock.classList.remove('hidden');
        logEvent('sys', 'SANDBOX mode enabled', 'warn');
      } else {
        dock.classList.add('hidden');
        logEvent('sys', 'OPS mode enabled', 'success');
      }
    };
  });

  document.querySelectorAll('.sandbox-btn').forEach((b) => {
    b.onclick = () => logEvent('sandbox', `action: ${b.dataset.action} · stub`, 'warn');
  });

  // map click → fill input
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
    } else {
      document.getElementById('to-lat').value = lat.toFixed(6);
      document.getElementById('to-lon').value = lon.toFixed(6);
    }
    cancelPick();
    openModal();
  }, Cesium.ScreenSpaceEventType.LEFT_CLICK);

  document.addEventListener('keydown', (e) => {
    if (e.key !== 'Escape') return;
    if (state.pickingFor) { cancelPick(); openModal(); }
    else if (!document.getElementById('modal-bg').classList.contains('hidden')) closeModal();
  });
}

async function submitOrder() {
  const fLat = +document.getElementById('from-lat').value;
  const fLon = +document.getElementById('from-lon').value;
  const tLat = +document.getElementById('to-lat').value;
  const tLon = +document.getElementById('to-lon').value;
  const weight = +document.getElementById('weight').value || 2;

  if (!fLat || !fLon || !tLat || !tLon) {
    logEvent('orch', 'order: missing coordinates', 'error');
    return;
  }
  try {
    const res = await fetch('/api/orders', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        from: { lat: fLat, lon: fLon, alt: 60 },
        to: { lat: tLat, lon: tLon, alt: 60 },
        weight,
      }),
    });
    const data = await res.json();
    logEvent('orch', `order ${short(data.order_id)} dispatched`, 'success');
    closeModal();
  } catch (e) {
    logEvent('orch', `dispatch failed: ${e}`, 'error');
  }
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

// =================================================================
// START
// =================================================================
init().catch((e) => {
  const el = document.getElementById('boot-error');
  el.textContent = `INIT ERROR\n${e.message}`;
  el.classList.remove('hidden');
});
