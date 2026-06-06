// Launcher: 3-button mode selection. Управляет sim-supervisor'ом, ведёт на /real.
(() => {
  const $ = (id) => document.getElementById(id);
  const msg = $("launcher-msg");

  function setMsg(text, cls = "") {
    msg.className = cls;
    msg.textContent = text;
  }

  function setSimStatus(state, errorText) {
    const dot = document.querySelector("#status-sim .status-dot");
    const txt = document.querySelector("#status-sim .status-text");
    dot.dataset.state = state;
    txt.textContent = state;
    const startBtn = $("btn-sim-start");
    const openBtn = $("btn-sim-open");
    const stopBtn = $("btn-sim-stop");
    if (state === "running") {
      startBtn.classList.add("hidden");
      openBtn.classList.remove("hidden");
      stopBtn.classList.remove("hidden");
    } else if (state === "starting" || state === "stopping") {
      startBtn.disabled = true;
      startBtn.textContent = state === "starting" ? "Starting…" : "Stopping…";
      openBtn.classList.add("hidden");
      stopBtn.classList.add("hidden");
    } else {
      startBtn.classList.remove("hidden");
      startBtn.disabled = false;
      startBtn.textContent = "▶ Start";
      openBtn.classList.add("hidden");
      stopBtn.classList.add("hidden");
    }
    if (state === "error") setMsg(errorText || "Simulation error", "err");

    // Пока симуляция поднята/поднимается — прячем выбор кол-ва дронов и
    // оценку памяти (они актуальны только до старта).
    const live = (state === "running" || state === "starting");
    const cfg = $("sim-config");
    const aux = document.querySelector("#status-sim .status-aux");
    if (cfg) cfg.classList.toggle("hidden", live);
    if (aux) aux.classList.toggle("hidden", live);
  }

  async function refreshStatus() {
    try {
      const r = await fetch("/api/launcher/sim/status");
      const j = await r.json();
      setSimStatus(j.state, j.error);
    } catch (e) {
      setMsg("Cannot fetch status: " + e.message, "err");
    }
  }

  // Stepper для num-drones (clamped 1..8) — также апдейтит hint с оценкой ресурсов.
  const numInput = $("sim-num-drones");
  const numHint = $("num-hint");
  const numMinus = $("num-minus");
  const numPlus = $("num-plus");

  function clampN() {
    let n = parseInt(numInput.value, 10);
    if (isNaN(n)) n = 2;
    n = Math.max(1, Math.min(10, n));
    numInput.value = String(n);
    return n;
  }
  function refreshNumUi() {
    const n = clampN();
    numMinus.disabled = n <= 1;
    numPlus.disabled = n >= 10;
    const ramMb = n * 250;
    const bootEst = Math.ceil(15 + n * 4);
    numHint.textContent = `~${ramMb} MB · boot ~${bootEst} s`;
  }
  numMinus.addEventListener("click", () => { numInput.value = clampN() - 1; refreshNumUi(); });
  numPlus.addEventListener("click",  () => { numInput.value = clampN() + 1; refreshNumUi(); });
  numInput.addEventListener("input", refreshNumUi);
  refreshNumUi();

  $("btn-sim-start").addEventListener("click", async () => {
    const n = clampN();
    setMsg(`Booting ${n} PX4 instance${n === 1 ? "" : "s"} — about ${15 + n * 4} s (parallel boot + ready-check)…`, "warn");
    setSimStatus("starting");
    try {
      const r = await fetch("/api/launcher/sim/start", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ num_drones: n }),
      });
      const j = await r.json();
      if (j.ok) {
        setMsg(`Simulation started · ${j.num_drones || n} airframes · opening UI…`, "ok");
        setSimStatus("running");
        setTimeout(() => { window.location.href = "/sim"; }, 800);
      } else {
        setMsg("Start failed: " + (j.error || "unknown"), "err");
        setSimStatus(j.state || "error", j.error);
      }
    } catch (e) {
      setMsg("Network/server: " + e.message, "err");
      setSimStatus("error", e.message);
    }
  });

  $("btn-sim-open").addEventListener("click", () => {
    window.location.href = "/sim";
  });

  $("btn-sim-stop").addEventListener("click", async () => {
    setMsg("Stopping simulation…", "warn");
    setSimStatus("stopping");
    try {
      const r = await fetch("/api/launcher/sim/stop", { method: "POST" });
      const j = await r.json();
      if (j.ok) {
        setMsg("Simulation stopped.", "ok");
        setSimStatus("stopped");
      } else {
        setMsg("Stop failed.", "err");
      }
    } catch (e) {
      setMsg("Network/server: " + e.message, "err");
    }
  });

  $("btn-real-open").addEventListener("click", () => {
    window.location.href = "/real";
  });

  // Local launch (если профилей нет — открываем setup, бэкенд решит).
  const localBtn = $("btn-local-launch");
  if (localBtn) localBtn.addEventListener("click", () => {
    window.location.href = "/local_launch";
  });

  // Card fleet — locked, но клик ведёт в roadmap-сообщение.
  const fleetCard = $("card-fleet");
  if (fleetCard) fleetCard.addEventListener("click", () => {
    setMsg("Fleet operations · in development · target 0.6.0", "warn");
  });

  // ─── Часы, session-аптайм и индикатор System в шапке ────────────
  function pad(n) { return String(n).padStart(2, "0"); }
  // Аптайм = время с момента входа под профилем (iat из сессионного токена).
  // После logout/выброса оператор логинится заново → новый iat → счётчик с нуля.
  let loginAt = null;            // ms, момент входа в систему
  function fmtUptime(s) {
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
    return h > 0 ? `${h}:${pad(m)}:${pad(sec)}` : `${pad(m)}:${pad(sec)}`;
  }
  function tickClock() {
    const d = new Date();
    const el = $("clock-time");
    if (el) el.textContent = `${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}:${pad(d.getUTCSeconds())}`;
    const up = $("hero-uptime");
    if (up && loginAt !== null) {
      const s = Math.max(0, Math.floor((Date.now() - loginAt) / 1000));
      up.textContent = fmtUptime(s);
    }
  }
  setInterval(tickClock, 1000); tickClock();

  // ─── Оператор + момент входа (для session-аптайма) ──────────────
  async function refreshMe() {
    try {
      const r = await fetch("/api/me", { cache: "no-store" });
      if (!r.ok) throw new Error("unauthorized");
      const j = await r.json();
      const op = $("lobby-op");
      if (op) op.textContent = j.operator || "—";
      if (typeof j.iat === "number") { loginAt = j.iat * 1000; tickClock(); }
    } catch (e) { /* не залогинен — middleware и так редиректнет */ }
  }
  refreshMe();

  // Logout / смена профиля: гасим сессию и уходим на экран входа.
  const logoutBtn = $("btn-lobby-logout");
  if (logoutBtn) logoutBtn.addEventListener("click", async () => {
    logoutBtn.disabled = true;
    try { await fetch("/api/logout", { method: "POST" }); } catch (e) { /* ignore */ }
    window.location.href = "/login";
  });

  // ─── Health: реальный признак «система жива» ────────────────────
  function setHealth(ok) {
    const el = $("ts-health");
    if (!el) return;
    el.textContent = ok ? "Healthy" : "Offline";
    el.className = "ts-v " + (ok ? "green" : "amber");
  }
  async function refreshHealth() {
    try {
      const r = await fetch("/api/launcher/health", { cache: "no-store" });
      if (!r.ok) throw new Error("bad status");
      await r.json();
      setHealth(true);
    } catch (e) {
      setHealth(false);
    }
  }
  refreshHealth();
  setInterval(refreshHealth, 5000);

  // ─── Профили реальных дронов в стат-ячейках + карточке Real ────
  async function refreshProfiles() {
    try {
      const r = await fetch("/api/real/profiles");
      const j = await r.json();
      const list = j.profiles || [];
      const tsP = $("ts-profiles");
      const cnt = $("real-profile-count");
      const last = $("real-last-used");
      const lastConn = $("real-last-conn");
      if (tsP) tsP.innerHTML = `${list.length} <small>· ${list.length ? '1 active' : 'none yet'}</small>`;
      if (cnt) cnt.textContent = String(list.length);
      if (list.length > 0 && last) {
        const p = list[0];
        last.textContent = p.name || p.display_name || "—";
        if (lastConn) {
          if (p.connection_type === "serial") {
            lastConn.textContent = `${p.serial_port || "?"} · ${p.serial_baud || "?"}`;
          } else {
            lastConn.textContent = `${p.connection_type || "?"} · ${p.net_host || ""}:${p.net_port || "?"}`;
          }
        }
      } else if (last) {
        last.textContent = "—";
        if (lastConn) lastConn.textContent = "no profile yet";
      }
    } catch (e) { /* silent */ }
  }
  refreshProfiles();

  // ─── Пуллинг статуса каждые 3 с ────────────────────────────────
  refreshStatus();
  setInterval(refreshStatus, 3000);
  setInterval(refreshProfiles, 8000);
})();