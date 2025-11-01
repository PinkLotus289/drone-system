// ========== Tabs ==========
const tabs = document.querySelectorAll('.tab-btn');
const tabSections = document.querySelectorAll('.tab');
tabs.forEach(btn => btn.addEventListener('click', () => {
  tabs.forEach(b => b.classList.remove('active'));
  tabSections.forEach(t => t.classList.remove('active'));
  btn.classList.add('active');
  document.getElementById(btn.dataset.tab).classList.add('active');
}));

// ========== Map ==========
const map = L.map('map').setView([43.07470, -89.38420], 14);
L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', { maxZoom: 19 }).addTo(map);

let base = { lat: 43.07470, lon: -89.38420 };
let baseMarker = L.marker([base.lat, base.lon]).addTo(map).bindPopup("База дронов");
fetch("/api/base")
  .then(r => r.json())
  .then(b => {
    base = b;
    if (baseMarker) map.removeLayer(baseMarker);
    baseMarker = L.marker([b.lat, b.lon]).addTo(map).bindPopup("База дронов");
    map.setView([b.lat, b.lon], 14);
  });

const droneMarkers = {};
const socket = new WebSocket(`ws://${window.location.host}/ws`);

socket.onopen = () => console.log("✅ WebSocket подключен");

socket.onmessage = (event) => {
  const msg = JSON.parse(event.data);

  // === Телеметрия ===
  if (msg.type === "telemetry_update" && msg.payload?.lat && msg.payload?.lon) {
    const id = msg.topic.split("/")[1];
    const { lat, lon } = msg.payload;
    if (!droneMarkers[id]) {
      droneMarkers[id] = L.marker([lat, lon]).addTo(map).bindPopup(`🚁 Drone ${id}`);
    } else {
      droneMarkers[id].setLatLng([lat, lon]);
    }
  }

  // === Новый активный дрон ===
  if (msg.type === "drone_active" && msg.payload) {
    const d = msg.payload;
    const id = d.id || "drone";
    if (!droneMarkers[id]) {
      droneMarkers[id] = L.marker([d.lat, d.lon]).addTo(map).bindPopup(`Drone ${id}`);
    }
  }

  // === Маршрут миссии ===
  if (msg.type === "mission_planned" && msg.payload?.waypoints) {
    const coords = msg.payload.waypoints.map(w => [w.pos?.lat ?? w.lat, w.pos?.lon ?? w.lon]);
    L.polyline(coords, { color: "blue", weight: 3 }).addTo(map);
  }
};

socket.onclose = () => console.log("❌ WebSocket закрыт");

// --------- Orders form ---------
document.getElementById("orderForm").onsubmit = async (e) => {
  e.preventDefault();
  const order = {
    from: { lat: parseFloat(val("from-lat")), lon: parseFloat(val("from-lon")) },
    to: { lat: parseFloat(val("to-lat")), lon: parseFloat(val("to-lon")) },
    weight: parseFloat(val("orderWeight")),
  };
  await fetch("/api/orders", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(order)
  });
  alert("🚀 Заказ отправлен в оркестратор!");
};

function val(id) { return document.getElementById(id).value; }

// --------- Клик по карте: выбираем точки A/B ---------
let selecting = "from";
map.on("click", (e) => {
  const { lat, lng } = e.latlng;
  if (document.getElementById("orders").classList.contains("active")) {
    if (selecting === "from") {
      setInput("from-lat", lat);
      setInput("from-lon", lng);
      L.marker([lat, lng]).addTo(map).bindPopup("A — pick-up");
      selecting = "to";
    } else {
      setInput("to-lat", lat);
      setInput("to-lon", lng);
      L.marker([lat, lng]).addTo(map).bindPopup("B — drop-off");
      selecting = "from";
    }
  }
});

function setInput(id, v) {
  document.getElementById(id).value = (+v).toFixed(6);
}

// --------- Подгружаем список дронов ---------
async function loadDrones() {
  try {
    const res = await fetch("/api/drones");
    const drones = await res.json();
    const select = document.getElementById("droneSelect");
    select.innerHTML = "";
    drones.forEach(d => {
      const opt = document.createElement("option");
      opt.value = d.id;
      opt.textContent = `${d.id} (${d.status || "IDLE"})`;
      select.appendChild(opt);
    });
  } catch (err) {
    console.error("Не удалось загрузить дронов:", err);
  }
}
loadDrones();

// --------- Автоподгон карты ---------
setInterval(() => {
  const markers = Object.values(droneMarkers);
  if (markers.length > 0) {
    const group = L.featureGroup(markers);
    map.fitBounds(group.getBounds().pad(0.3));
  }
}, 4000);

async function loadBase() {
  const res = await fetch("/api/base");
  const base = await res.json();
  if (baseMarker) map.removeLayer(baseMarker);
  baseMarker = L.marker([base.lat, base.lon]).addTo(map).bindPopup("База дронов");
  map.setView([base.lat, base.lon], 14);
}
loadBase();

