// ---------- Tabs ----------
const tabs = document.querySelectorAll('.tab-btn');
const tabSections = document.querySelectorAll('.tab');
tabs.forEach(btn => btn.addEventListener('click', () => {
  tabs.forEach(b => b.classList.remove('active'));
  tabSections.forEach(t => t.classList.remove('active'));
  btn.classList.add('active');
  document.getElementById(btn.dataset.tab).classList.add('active');
}));

// ---------- Map ----------
const map = L.map('map').setView([43.07470, -89.38420], 13); // Madison center
L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',{maxZoom:19}).addTo(map);

let base = {lat: 43.07470, lon: -89.38420};
let baseMarker = L.marker([base.lat, base.lon]).addTo(map).bindPopup("База дронов");
let drones = {};
let currentRoute = null;

// load settings (base + drone list)
async function loadSettings() {
  const s = await (await fetch("/api/settings")).json();
  if (s.base) {
    base = s.base;
    if (baseMarker) map.removeLayer(baseMarker);
    baseMarker = L.marker([base.lat, base.lon]).addTo(map).bindPopup("База дронов");
    map.setView([base.lat, base.lon], 13);
  }
}
loadSettings();

// click to set A/B on Orders tab
let selecting = 'from';
map.on('click', (e) => {
  const {lat, lng} = e.latlng;
  if (document.getElementById('orders').classList.contains('active')) {
    if (selecting === 'from') {
      setInput('from-lat', lat); setInput('from-lon', lng);
      L.marker([lat, lng]).addTo(map).bindPopup('A — pick-up');
      selecting = 'to';
    } else {
      setInput('to-lat', lat); setInput('to-lon', lng);
      L.marker([lat, lng]).addTo(map).bindPopup('B — drop-off');
      selecting = 'from';
      drawABRoute(); // рисуем после выбора B
    }
  } else if (document.getElementById('settings').classList.contains('active')) {
    setInput('base-lat', lat); setInput('base-lon', lng);
    if (baseMarker) map.removeLayer(baseMarker);
    baseMarker = L.marker([lat, lng]).addTo(map).bindPopup("База дронов");
  }
});

function setInput(id, v){ document.getElementById(id).value = (+v).toFixed(6); }

// -------- Orders form --------
document.getElementById('orderForm').onsubmit = async (e) => {
  e.preventDefault();
  const order = {
    id: `order_${Date.now()}`,
    from: { lat: parseFloat(val('from-lat')), lon: parseFloat(val('from-lon')) },
    to:   { lat: parseFloat(val('to-lat')),   lon: parseFloat(val('to-lon')) },
    drone: document.getElementById('droneSelect').value,
    weight: parseFloat(val('orderWeight'))
  };
  // рисуем итоговый маршрут База→A→B→База
  drawFullRoute(order);
  await fetch("/api/orders",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(order)});
  const li = document.createElement("li");
  li.textContent = `Заказ ${order.id}: Base→A→B→Base`;
  document.getElementById("orders-list").appendChild(li);
};

function val(id){ return document.getElementById(id).value; }

function drawABRoute(){
  if (currentRoute) { map.removeLayer(currentRoute); currentRoute=null; }
  const A=[parseFloat(val('from-lat')), parseFloat(val('from-lon'))];
  const B=[parseFloat(val('to-lat')), parseFloat(val('to-lon'))];
  currentRoute = L.polyline([A,B],{color:'blue'}).addTo(map);
}

function drawFullRoute(order){
  if (currentRoute) { map.removeLayer(currentRoute); currentRoute=null; }
  const pts = [
    [base.lat, base.lon],
    [order.from.lat, order.from.lon],
    [order.to.lat, order.to.lon],
    [base.lat, base.lon]
  ];
  currentRoute = L.polyline(pts,{color:'blue'}).addTo(map);
  map.fitBounds(currentRoute.getBounds(), {padding:[20,20]});
}

// -------- Settings form (writes to config.yaml) --------
document.getElementById('settingsForm').onsubmit = async (e)=>{
  e.preventDefault();
  const body = {
    base: { lat: parseFloat(val('base-lat')||base.lat), lon: parseFloat(val('base-lon')||base.lon) },
    drone_count: parseInt(val('drone-count')||1),
    mavsdk_port_start: 14540
  };
  const res = await fetch("/api/settings",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
  const js = await res.json();
  // перезагружаем список дронов
  await loadDrones();
  // визуально обновим базу
  base = body.base;
  if (baseMarker) map.removeLayer(baseMarker);
  baseMarker = L.marker([base.lat, base.lon]).addTo(map).bindPopup("База дронов");
  alert("Настройки сохранены. Перезапусти PX4 Launcher и MAVSDK Bridge, чтобы число дронов применилось.");
};

// -------- Drones list / select --------
async function loadDrones(){
  const res = await fetch("/api/drones"); const js = await res.json();
  const sel = document.getElementById('droneSelect'); sel.innerHTML="";
  js.drones.forEach(d=>{
    const opt=document.createElement("option"); opt.value=d.id; opt.textContent=d.id; sel.appendChild(opt);
  });
}
loadDrones();

// -------- Telemetry WS (many drones) --------
const ws = new WebSocket(`ws://${location.host}/ws/telemetry`);
ws.onmessage = (ev)=>{
  try{
    const t = JSON.parse(ev.data);
    const id = t.id || "sim_drone_1";
    if (!drones[id]) {
      drones[id] = L.marker([t.lat, t.lon]).addTo(map).bindPopup(id);
    } else {
      drones[id].setLatLng([t.lat, t.lon]);
    }
  }catch(e){ console.error(e); }
};
