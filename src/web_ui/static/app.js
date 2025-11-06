// ========== Tabs ==========
const tabs = document.querySelectorAll(".tab-btn");
const tabSections = document.querySelectorAll(".tab");
tabs.forEach((btn) =>
  btn.addEventListener("click", () => {
    tabs.forEach((b) => b.classList.remove("active"));
    tabSections.forEach((t) => t.classList.remove("active"));
    btn.classList.add("active");
    document.getElementById(btn.dataset.tab).classList.add("active");
  })
);

// ========== Map ==========
const map = L.map("map").setView([43.0747, -89.3842], 14);
L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", { maxZoom: 19 }).addTo(map);

// Держим ссылки
let baseMarker = null;
const droneMarkers = {};          // { [droneId]: L.Marker }
const missionPolylines = {};      // { [droneId]: L.Polyline }

// --- база ---
async function loadBase() {
  try {
    const res = await fetch("/api/base");
    const b = await res.json();
    if (baseMarker) map.removeLayer(baseMarker);
    baseMarker = L.marker([b.lat, b.lon]).addTo(map).bindPopup("База дронов");
    document.getElementById("base-lat").value = b.lat.toFixed(6);
    document.getElementById("base-lon").value = b.lon.toFixed(6);
    // центрируем только один раз при первом рендере
    if (!window.__baseCentered) {
      map.setView([b.lat, b.lon], 14);
      window.__baseCentered = true;
    }
  } catch (e) {
    console.error("Не удалось загрузить базу:", e);
  }
}
document.getElementById("reload-base").addEventListener("click", loadBase);
loadBase();

// ========== WebSocket ==========
const socket = new WebSocket(`ws://${window.location.host}/ws`);
socket.onopen = () => console.log("✅ WebSocket подключен");
socket.onclose = () => console.log("❌ WebSocket закрыт");

socket.onmessage = (event) => {
  const msg = JSON.parse(event.data);

  // ---- телеметрия ----
  if (msg.type === "telemetry_update" && msg.payload?.lat && msg.payload?.lon) {
    const id = msg.topic.split("/")[1]; // telem/<id>/...
    const { lat, lon, alt } = msg.payload;
    upsertDroneMarker(id, lat, lon);
    // обновим Fleet-таблицу «на лету»
    scheduleFleetRefreshSoon();
  }

  // ---- появление активного дрона ----
  if (msg.type === "drone_active" && msg.payload) {
    const d = msg.payload;
    if (d.lat && d.lon) upsertDroneMarker(d.id || "drone", d.lat, d.lon);
    scheduleFleetRefreshSoon();
  }

  // ---- план миссии (линия маршрута) ----
  if (msg.type === "mission_planned" && msg.payload?.waypoints) {
    const id = msg.topic.split("/")[1]; // mission/<id>/planned
    const coords = msg.payload.waypoints.map((w) => [
      w.pos?.lat ?? w.lat,
      w.pos?.lon ?? w.lon,
    ]);
    drawMissionPolyline(id, coords);
  }
};

function upsertDroneMarker(id, lat, lon) {
  if (!droneMarkers[id]) {
    droneMarkers[id] = L.marker([lat, lon]).addTo(map).bindPopup(`🚁 Drone ${id}`);
  } else {
    droneMarkers[id].setLatLng([lat, lon]);
  }
}

function drawMissionPolyline(droneId, coords) {
  // удалим предыдущую линию, если есть, чтобы не плодить
  if (missionPolylines[droneId]) {
    map.removeLayer(missionPolylines[droneId]);
  }
  const pl = L.polyline(coords, { weight: 3 }); // цвет по умолчанию из темы
  pl.addTo(map).bindPopup(`📦 Маршрут дрона ${droneId}`);
  missionPolylines[droneId] = pl;
}

// Мягкое авто-центрирование по всем маркерам раз в 5 c (без дёрганья)
setInterval(() => {
  const markers = Object.values(droneMarkers);
  if (markers.length > 0) {
    const group = L.featureGroup(markers);
    const bounds = group.getBounds().pad(0.25);
    // не дёргаем камеру, если маркеры +/- в кадре
    if (!map.getBounds().contains(bounds)) {
      map.fitBounds(bounds);
    }
  }
}, 5000);

// ========== Orders ==========
function val(id) { return document.getElementById(id).value; }

document.getElementById("orderForm").onsubmit = async (e) => {
  e.preventDefault();
  const order = {
    from: { lat: parseFloat(val("from-lat")), lon: parseFloat(val("from-lon")) },
    to:   { lat: parseFloat(val("to-lat")),   lon: parseFloat(val("to-lon")) },
    weight: parseFloat(val("orderWeight")),
    // опционально: выбранный дрон
    drone_id: document.getElementById("droneSelect").value || undefined,
  };
  await fetch("/api/orders", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(order),
  });
  alert("🚀 Заказ отправлен!");
};

// Выбор точек A/B кликом по карте (не плодим лишние маркеры)
let selecting = "from";
let pickMarker, dropMarker;
map.on("click", (e) => {
  if (!document.getElementById("orders").classList.contains("active")) return;
  const { lat, lng } = e.latlng;
  if (selecting === "from") {
    setInput("from-lat", lat);
    setInput("from-lon", lng);
    if (pickMarker) map.removeLayer(pickMarker);
    pickMarker = L.marker([lat, lng]).addTo(map).bindPopup("A — pick-up");
    selecting = "to";
  } else {
    setInput("to-lat", lat);
    setInput("to-lon", lng);
    if (dropMarker) map.removeLayer(dropMarker);
    dropMarker = L.marker([lat, lng]).addTo(map).bindPopup("B — drop-off");
    selecting = "from";
  }
});
function setInput(id, v) { document.getElementById(id).value = (+v).toFixed(6); }

// ========== Fleet ==========
async function loadFleet() {
  try {
    const res = await fetch("/api/fleet"); // если нет — /api/drones с адаптацией ниже
    let data = await res.json();
    const list = Array.isArray(data) ? data : (data.fleet || data.drones || []);
    const tbody = document.querySelector("#fleet-table tbody");
    tbody.innerHTML = "";
    list.forEach((d) => {
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td>${d.name || d.id}</td>
        <td>${d.status || ""}</td>
        <td>${fmtCoord(d.lat)}, ${fmtCoord(d.lon)}</td>
        <td>${d.alt != null ? (+d.alt).toFixed(1) : ""}</td>
      `;
      tbody.appendChild(tr);
    });
  } catch (e) {
    console.error("Не удалось загрузить флот:", e);
  }
}
function fmtCoord(x){ return x!=null ? (+x).toFixed(6) : ""; }
loadFleet();
let fleetRefreshTimer = null;
function scheduleFleetRefreshSoon(){
  clearTimeout(fleetRefreshTimer);
  fleetRefreshTimer = setTimeout(loadFleet, 300);
}

// Свободные дроны в выпадающем списке Orders
async function loadFreeDrones() {
  try {
    const res = await fetch("/api/drones");
    const data = await res.json();
    const list = Array.isArray(data) ? data : (data.drones || []);
    const free = list.filter(d => !["BUSY","IN_MISSION","ERROR"].includes((d.status||"").toUpperCase()));
    const select = document.getElementById("droneSelect");
    select.innerHTML = "";
    free.forEach((d) => {
      const opt = document.createElement("option");
      opt.value = d.id;
      opt.textContent = `${d.name || d.id} (${d.status || "IDLE"})`;
      select.appendChild(opt);
    });
  } catch (e) {
    console.error("Не удалось загрузить свободных дронов:", e);
  }
}
loadFreeDrones();
// периодически актуализируем
setInterval(loadFreeDrones, 4000);
