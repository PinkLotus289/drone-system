// Real Connect UI: профили + форма + Test Connection
(() => {
  const $ = (id) => document.getElementById(id);
  const demoMode = new URLSearchParams(location.search).get("demo") === "1";
  let demoTrace = [];
  let demoEpoch = 0;

  // ========== State ==========
  let profiles = [];
  let activeName = null;            // имя выбранного профиля или null если "новый"
  let lastTestOk = false;            // прошёл ли последний test connection — гейтит save
  let connType = "radio";          // дефолтный канал (Serial убран — бесполезен на удалённом сервере)
  let baud = 57600;
  let radioBaud = 57600;            // скорость для radio-канала (USB-радио в браузере)
  let connections = {};             // {name: {connected, url}} — активные мосты к бортам
  let radioStatus = {};             // {name: {agent_connected, ...}} — статус radio-реле
  let fleet = {};                   // {veh_id: {lat,lon,alt,status,battery_pct,sat_count,...}} — live телеметрия

  // Один активный radio-link на вкладку (Web Serial — один порт за раз).
  const radioLink = (typeof RadioLink !== "undefined") ? new RadioLink() : null;
  let radioActiveName = null;       // имя профиля, который сейчас связан по радио

  // ========== Validation ==========
  const NAME_RE = /^[a-zA-Z0-9_\-]{1,40}$/;

  function setFieldError(input, msg) {
    if (!input) return;
    const host = input.closest(".field") || input.parentElement;
    let el = host.querySelector(".field-err");
    if (!el) {
      el = document.createElement("div");
      el.className = "field-err";
      host.appendChild(el);
    }
    el.textContent = msg || "";
    el.style.display = msg ? "block" : "none";
    input.classList.toggle("invalid", !!msg);
  }

  function clearErrors() {
    document.querySelectorAll(".field-err").forEach(e => { e.textContent = ""; e.style.display = "none"; });
    document.querySelectorAll(".invalid").forEach(e => e.classList.remove("invalid"));
  }

  // Полная проверка формы перед test/connect/save. Показывает ошибки прямо под
  // полями (без серверных 422/500) и фокусирует первое проблемное поле.
  function validateForm() {
    clearErrors();
    let firstBad = null;
    const fail = (input, msg) => { setFieldError(input, msg); firstBad = firstBad || input; };

    const name = $("f-name").value.trim();
    if (!name) fail($("f-name"), "Enter a profile name");
    else if (!NAME_RE.test(name)) fail($("f-name"), "Letters, digits, “_” and “-” only (max 40 chars)");

    if (connType === "udp" || connType === "tcp") {
      const host = $("f-net-host").value.trim();
      const port = parseInt($("f-net-port").value, 10);
      if (connType === "tcp" && !host) fail($("f-net-host"), "Enter host (drone IP)");
      if (isNaN(port) || port < 1 || port > 65535) fail($("f-net-port"), "Port must be 1–65535");
    }

    if (firstBad) { firstBad.focus(); return false; }
    return true;
  }

  // ========== Helpers ==========
  function fillForm(p) {
    $("f-name").value = p.name || "";
    $("f-display").value = p.display_name || "";
    $("f-notes").value = p.notes || "";
    setConnType(p.connection_type || "serial");
    if (p.connection_type === "serial") {
      // Serial-форма убрана; legacy serial-профиль остаётся подключаемым с карточки,
      // но в форме показываем только baud (sel может отсутствовать).
      const sel = $("f-serial-port");
      if (sel) {
        if (p.serial_port && ![...sel.options].some(o => o.value === p.serial_port)) {
          const opt = document.createElement("option");
          opt.value = p.serial_port;
          opt.textContent = `${p.serial_port} (offline)`;
          sel.appendChild(opt);
        }
        sel.value = p.serial_port || "";
      }
      setBaud(p.serial_baud || 57600);
    } else if (p.connection_type === "radio") {
      setRadioBaud(p.serial_baud || 57600);
    } else {
      $("f-net-host").value = p.net_host || "";
      $("f-net-port").value = p.net_port || "";
    }
  }

  function readForm() {
    const name = $("f-name").value.trim();
    const data = {
      name,
      display_name: $("f-display").value.trim(),
      connection_type: connType,
      notes: $("f-notes").value.trim(),
      // frame / battery_* опущены — бэкенд подставит дефолты (DroneProfile).
    };
    if (connType === "serial") {
      const sp = $("f-serial-port");          // legacy: Serial-форма убрана
      data.serial_port = sp ? (sp.value || null) : null;
      data.serial_baud = baud;
    } else if (connType === "radio") {
      // radio: порт выбирается в браузере, на сервере хранится только скорость.
      data.serial_baud = radioBaud;
    } else {
      data.net_host = $("f-net-host").value.trim() || null;
      const p = parseInt($("f-net-port").value, 10);
      data.net_port = isNaN(p) ? null : p;
    }
    return data;
  }

  function clearForm() {
    activeName = null;
    lastTestOk = false;
    $("form-title").textContent = "New connection";
    $("btn-delete-profile").classList.add("hidden");
    $("real-form").reset();
    setConnType("radio");
    setBaud(57600);
    setRadioBaud(57600);
    $("test-results-card").classList.add("hidden");
    syncRadioUi(null);
    updateSaveButton();
    document.querySelectorAll("#profile-list .pcard").forEach(el => el.classList.remove("active"));
  }

  function setConnType(t) {
    connType = t;
    document.querySelectorAll('[data-conn]').forEach(b => {
      b.classList.toggle("active", b.dataset.conn === t);
    });
    const bs = $("block-serial");                 // Serial-блок удалён из формы; guard на случай legacy
    if (bs) bs.classList.toggle("hidden", t !== "serial");
    $("block-net").classList.toggle("hidden", t !== "udp" && t !== "tcp");
    const br = $("block-radio");
    if (br) br.classList.toggle("hidden", t !== "radio");
    // Шаг 3 — блоки действий: радио (Connect by radio) vs сеть (Test connection).
    const ar = $("act-radio");
    if (ar) ar.classList.toggle("hidden", t !== "radio");
    const an = $("act-net");
    if (an) an.classList.toggle("hidden", t === "radio");
    if (t === "radio") renderRadioSupport();
    updateSaveButton();
  }

  function setRadioBaud(b) {
    radioBaud = parseInt(b, 10) || 57600;
    document.querySelectorAll('#radio-baud-seg [data-rbaud]').forEach(btn =>
      btn.classList.toggle("active", String(btn.dataset.rbaud) === String(radioBaud)));
  }

  // Показать поддержку Web Serial и заблокировать кнопки, если браузер не тот.
  function renderRadioSupport() {
    const el = $("radio-support");
    const connectBtn = $("btn-radio-connect");
    const autoBtn = $("btn-radio-autodetect");
    if (!el) return;
    const ok = radioLink && RadioLink.isSupported();
    if (ok) {
      el.className = "radio-support ok";
      el.innerHTML = "This browser supports Web Serial — radio connection available.";
    } else {
      el.className = "radio-support bad";
      el.innerHTML = "This browser does not support Web Serial. Open the page in <b>Chrome</b>, <b>Edge</b>, Brave or Opera on a desktop (not on a phone).";
    }
    if (connectBtn) connectBtn.disabled = !ok;
    if (autoBtn) autoBtn.style.display = ok ? "" : "none";
  }

  function setBaud(b) {
    if (!$("baud-seg")) { baud = parseInt(b, 10) || 57600; return; }  // Serial-блок убран
    const known = ["57600", "115200", "921600"];
    document.querySelectorAll('#baud-seg [data-baud]').forEach(btn => btn.classList.remove("active"));
    if (known.includes(String(b))) {
      const btn = document.querySelector(`#baud-seg [data-baud="${b}"]`);
      if (btn) btn.classList.add("active");
      $("f-baud-custom").classList.add("hidden");
      baud = parseInt(b, 10);
    } else {
      document.querySelector('#baud-seg [data-baud="custom"]').classList.add("active");
      const inp = $("f-baud-custom");
      inp.classList.remove("hidden");
      inp.value = b;
      baud = parseInt(b, 10) || 57600;
    }
  }

  function updateSaveButton() {
    // Save доступен всегда; корректность ввода проверяет validateForm при клике.
    const saveBtn = $("btn-save");
    if (saveBtn) { saveBtn.disabled = false; saveBtn.title = "Save profile"; }
    // ready-state — только для сетевых каналов (в act-net); для radio скрыт.
    const ready = document.getElementById("ready-state");
    if (ready && connType !== "radio") {
      ready.textContent = lastTestOk ? "validated · ready" : "awaiting test";
      ready.style.color = lastTestOk ? "var(--green)" : "var(--ink-3)";
    }
  }

  // ========== API calls ==========
  async function loadProfiles() {
    try {
      const r = await fetch("/api/real/profiles");
      const j = await r.json();
      profiles = j.profiles || [];
      renderProfiles();
    } catch (e) {
      console.error(e);
    }
  }

  async function loadSerialPorts() {
    const sel = $("f-serial-port");
    const hint = $("port-hint");
    if (!sel) return;                 // Serial-форма убрана — нечего заполнять
    sel.innerHTML = '<option value="">refreshing…</option>';
    try {
      const r = await fetch("/api/real/serial-ports");
      const j = await r.json();
      sel.innerHTML = "";
      if (!j.ports || j.ports.length === 0) {
        sel.innerHTML = '<option value="">— no ports detected —</option>';
        hint.textContent = "connect cable/radio and reload";
        return;
      }
      for (const p of j.ports) {
        const opt = document.createElement("option");
        opt.value = p.path;
        opt.textContent = p.hint ? `${p.label}  [${p.hint}]` : p.label;
        sel.appendChild(opt);
      }
      hint.textContent = `found ${j.ports.length} port(s)`;
    } catch (e) {
      hint.textContent = "error: " + e.message;
    }
  }

  async function deleteCurrent() {
    if (!activeName) return;
    if (!confirm(`Delete profile "${activeName}"?`)) return;
    const r = await fetch(`/api/real/profiles/${encodeURIComponent(activeName)}`, { method: "DELETE" });
    if (r.ok) {
      await loadProfiles();
      clearForm();
    } else {
      alert("Failed to delete profile");
    }
  }

  async function saveProfile() {
    if (!validateForm()) return;
    const body = readForm();
    const r = await fetch("/api/real/profiles", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (r.ok) {
      const saved = await r.json();
      activeName = saved.name;
      await loadProfiles();
      $("form-title").textContent = `Profile · ${saved.name}`;
      $("btn-delete-profile").classList.remove("hidden");
      document.querySelectorAll("#profile-list .pcard").forEach(el => {
        el.classList.toggle("active", el.dataset.name === saved.name);
      });
    } else {
      const err = await r.json().catch(() => ({}));
      // В HTTP/2 r.statusText всегда пустой — показываем код статуса и detail
      // (для 422 — текст валидации, для 500 — фолбэк).
      const reason = err.detail || r.statusText || "server error";
      alert(`Save failed (${r.status}): ${reason}`);
    }
  }

  async function testConnection() {
    if (!validateForm()) return;
    const body = readForm();

    const card = $("test-results-card");
    card.classList.remove("hidden");
    $("test-meta").textContent = "running… waiting for heartbeat up to 12 s";
    $("check-grid").innerHTML = '<div class="check info"><div class="check-label">Status</div><div class="check-value">⏳ pending…</div></div>';
    $("raw-result").textContent = "";

    const startedAt = Date.now();
    const r = await fetch("/api/real/test-connection", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        connection_type: body.connection_type,
        serial_port: body.serial_port,
        serial_baud: body.serial_baud,
        net_host: body.net_host,
        net_port: body.net_port,
        timeout_s: 12,
      }),
    });
    const j = await r.json();
    const elapsed = ((Date.now() - startedAt) / 1000).toFixed(1);

    $("test-meta").textContent = `${elapsed} s · gRPC :${j.grpc_port || "?"} · ${j.connection_url}`;
    renderChecks(j);
    $("raw-result").textContent = JSON.stringify(j, null, 2);

    lastTestOk = !!j.ok;
    updateSaveButton();
  }

  function renderChecks(j) {
    const grid = $("check-grid");
    grid.innerHTML = "";

    function add(label, value, cls = "info") {
      const el = document.createElement("div");
      el.className = `check ${cls}`;
      el.innerHTML = `<div class="check-label">${label}</div><div class="check-value">${value}</div>`;
      grid.appendChild(el);
    }

    add("HEARTBEAT", j.heartbeat ? "OK" : "none", j.heartbeat ? "ok" : "bad");

    if (j.error) {
      add("ERROR", j.error, "bad");
      if (j.message) add("MESSAGE", j.message, "bad");
      return;
    }

    if (j.firmware) {
      const fw = j.firmware;
      add("FIRMWARE", `${fw.flight_sw_major}.${fw.flight_sw_minor}.${fw.flight_sw_patch}`, "info");
    }

    if (j.gps) {
      const fix = j.gps.fix_type || "?";
      const sats = j.gps.satellites ?? 0;
      const cls = (fix.includes("3D") || fix.includes("FIX_TYPE_3D")) && sats >= 6 ? "ok"
                : sats > 0 ? "warn" : "bad";
      add("GPS FIX", `${fix} · ${sats} sat`, cls);
    } else if (j.gps_error) {
      add("GPS", "n/a", "warn");
    }

    if (j.battery) {
      const v = j.battery.voltage_v;
      const pct = j.battery.remaining_percent;
      const percent = Number.isFinite(pct) && pct >= 0 && pct <= 100 ? pct : null;
      add("BATTERY", `${v?.toFixed?.(2) ?? "?"} V · ${percent == null ? "charge unknown" : percent.toFixed(0) + "%"}`, percent != null && percent >= 30 ? "ok" : "bad");
    } else if (j.battery_error) {
      add("BATTERY", "n/a", "warn");
    }

    if (j.health) {
      const h = j.health;
      add("CALIB GYRO", h.gyro_ok ? "OK" : "FAIL", h.gyro_ok ? "ok" : "bad");
      add("CALIB ACCEL", h.accel_ok ? "OK" : "FAIL", h.accel_ok ? "ok" : "bad");
      add("CALIB MAG", h.mag_ok ? "OK" : "FAIL", h.mag_ok ? "ok" : "bad");
      add("GLOBAL POS", h.global_pos_ok ? "OK" : "no", h.global_pos_ok ? "ok" : "warn");
      add("HOME POS", h.home_ok ? "OK" : "no", h.home_ok ? "ok" : "warn");
      add("ARMABLE", h.armable ? "READY" : "NOT READY", h.armable ? "ok" : "bad");
    }

    if (j.flight_mode) {
      add("FLIGHT MODE", j.flight_mode.replace(/^.*\./, ""), "info");
    }
  }

  // ========== Render profile list ==========
  async function loadConnections() {
    if (demoMode) return;
    try {
      const r = await fetch("/api/real/connections");
      if (r.status === 401) { location.href = "/login"; return; }
      const j = await r.json();
      connections = j.connections || {};
      radioStatus = j.radio || {};
      await loadFleet();
      refreshLive();
      const name = radioActiveName || activeName;
      if (name && connections[name]?.bridge_running) startSupervision(name);
    } catch { /* silent */ }
  }

  async function loadFleet() {
    try {
      const r = await fetch("/api/fleet");
      const j = await r.json();
      fleet = {};
      for (const d of (j.fleet || [])) fleet[d.id] = d;
    } catch { /* silent */ }
  }

  async function connectProfile(name) {
    const r = await fetch(`/api/real/connect/${encodeURIComponent(name)}`, { method: "POST" });
    const j = await r.json().catch(() => ({}));
    if (!j.ok) alert("Connect failed: " + (j.error || j.detail || r.status));
    loadConnections();
  }
  async function disconnectProfile(name) {
    await fetch(`/api/real/disconnect/${encodeURIComponent(name)}`, { method: "POST" }).catch(() => {});
    loadConnections();
  }

  // ========== Radio (Web Serial в браузере) ==========
  // Последний статус активного радио-линка (он один на вкладку, поэтому всегда
  // относится к radioActiveName). Нужен, чтобы восстановить статус при возврате
  // к подключённому профилю после просмотра другого.
  let radioLastStatus = { text: "idle", kind: "" };

  function setRadioStatus(text, kind) {
    radioLastStatus = { text, kind: kind || "" };
    const s = $("radio-status"); const dot = $("radio-dot");
    if (s) s.textContent = text;
    if (dot) dot.className = "radio-dot" + (kind ? " " + kind : "");
  }
  function resetRadioButtons() {
    $("btn-radio-connect")?.classList.remove("hidden");
    $("btn-radio-disconnect")?.classList.add("hidden");
  }

  // Привести радио-блок §3 (статус + кнопки Connect/Disconnect) в соответствие
  // ВЫБРАННОМУ профилю. Радио-линк один на вкладку, поэтому для всех профилей,
  // кроме активного, показываем "idle" + Connect, не трогая реальный статус линка.
  function syncRadioUi(name) {
    const s = $("radio-status"); const dot = $("radio-dot");
    if (radioActiveName && radioActiveName === name) {
      $("btn-radio-connect")?.classList.add("hidden");
      $("btn-radio-disconnect")?.classList.remove("hidden");
      if (s) s.textContent = radioLastStatus.text;
      if (dot) dot.className = "radio-dot" + (radioLastStatus.kind ? " " + radioLastStatus.kind : "");
    } else {
      resetRadioButtons();
      if (s) s.textContent = "idle";
      if (dot) dot.className = "radio-dot";
    }
  }

  async function startRadio(name, baudVal) {
    if (!radioLink) { alert("Web Serial is not supported in this browser — open it in Chrome or Edge"); return; }
    if (radioActiveName && radioActiveName !== name) await stopRadio();  // one port at a time
    radioActiveName = name;
    $("btn-radio-connect")?.classList.add("hidden");
    $("btn-radio-disconnect")?.classList.remove("hidden");
    const phaseMap = {
      "requesting-port": ["select USB device…", "warn"],
      "port-open": ["port open", "warn"],
      "server-open": ["linking to server…", "warn"],
      "connecting-server": ["starting bridge to drone…", "warn"],
      // Транспорт поднят, но дрон ещё не подтверждён (нет MAVLink-кадров).
      "link-up": ["link up · waiting for drone…", "warn"],
      // Реально пошёл MAVLink-поток от борта — вот это настоящий on air.
      "on-air": ["on air", "on"],
      // За таймаут не пришло ни одного MAVLink-кадра.
      "no-data": ["no telemetry — wrong device/baud or drone offline", "bad"],
      "reconnecting": ["reconnecting…", "warn"],
      "stopped": ["idle", ""],
    };
    try {
      await radioLink.start({
        name, baud: baudVal || radioBaud, force: true,
        onStatus: (phase, detail) => {
          // «Check it» активна только на реальном on air; иначе — гасим Шаг 4.
          if (phase === "on-air") setCheckAvail(true);
          else if (phase === "no-data" || phase === "stopped") setCheckAvail(false);
          if (phase === "warn") { setRadioStatus(detail, "warn"); return; }
          if (phase === "error") { setRadioStatus("error: " + detail, "bad"); return; }
          const m = phaseMap[phase];
          setRadioStatus(m ? (m[0] + (detail ? " " + detail : "")) : phase, m ? m[1] : "");
        },
        onError: () => { radioActiveName = null; resetRadioButtons(); },
      });
    } catch (e) {
      setRadioStatus("error: " + e.message, "bad");
      radioActiveName = null; resetRadioButtons();
      return;
    }
    loadConnections();
  }

  async function stopRadio() {
    if (supervisedName) {
      await runPreflight();
      if (!pfSnapshot?.can_disarm || pfSnapshot.armed !== false || riskyState()) {
        commandNotice("Disconnect blocked: confirm landed and disarmed first. LAND remains available.", true);
        return;
      }
    }
    if (radioLink) { try { await radioLink.stop(); } catch { /* ignore */ } }
    radioActiveName = null;
    resetRadioButtons();
    setCheckAvail(false);   // погасить «Check it» + Шаг 4
    setRadioStatus("idle", "");
    loadConnections();
  }

  async function onRadioConnectClick() {
    if (!validateForm()) return;
    const data = readForm();
    // Профиль НЕ сохраняем — подключение работает с временным профилем на сервере
    // (имя = только ключ реле). Сохранение — отдельным действием по кнопке Save.
    await startRadio(data.name, radioBaud);
  }

  // ===== Real-flight supervision: state comes only from bridge telemetry =====
  let pfTimer = null;
  let supervisedName = null;
  let pfSnapshot = null;
  let pfPollBusy = false;
  let pendingCommand = null;
  let requestedFlight = "test_hop";
  let keepaliveBusy = false;
  let landRequested = false;
  let lastGoodPoll = 0;
  const setMeta = (t) => { if ($("pf-meta")) $("pf-meta").textContent = t; };
  const supervised = () => supervisedName || radioActiveName || activeName;
  const reportCurrent = () => lastGoodPoll > 0 && Date.now() - lastGoodPoll < 2000;
  const riskyState = () => pendingCommand || (supervisedName && !reportCurrent()) || (pfSnapshot && (pfSnapshot.armed !== false || pfSnapshot.in_air !== false || ["landing", "fault"].includes(pfSnapshot.phase)));

  function commandNotice(text, bad = false) {
    $("pf-command").textContent = text;
    $("pf-command").classList.toggle("bad", bad);
  }
  function disableFlightControls() {
    ["btn-arm", "btn-disarm", "btn-motor-test", "btn-test-hop", "btn-test-takeoff", "btn-hold", "btn-test-return"].forEach((id) => { if ($(id)) $(id).disabled = true; });
    document.querySelectorAll("[data-step]").forEach((b) => b.disabled = true);
  }
  function setCheckAvail(on) {
    if ($("btn-check")) $("btn-check").disabled = !on;
    // Never hide LAND or stop polling because a transport status turned red.
    if (!on) disableFlightControls();
  }
  function hideStep4() {
    if (riskyState()) return;
    $("step4-wstep")?.classList.add("hidden");
    if (pfTimer) clearInterval(pfTimer);
    pfTimer = null; supervisedName = null; pfSnapshot = null;
    disableFlightControls();
  }
  async function onCheckClick() {
    const name = radioActiveName || activeName;
    if (!name) return;
    startSupervision(name);
    $("step4-wstep")?.scrollIntoView({ behavior: "smooth", block: "start" });
  }
  function startSupervision(name) {
    if (supervisedName && supervisedName !== name && riskyState()) return;
    if (supervisedName !== name) { pfSnapshot = null; pendingCommand = null; landRequested = false; }
    supervisedName = name;
    $("step4-wstep").classList.remove("hidden");
    if (!pfTimer) pfTimer = setInterval(runPreflight, 750);
    runPreflight();
  }
  function renderCheckGrid(id, list) {
    const grid = $(id);
    grid.replaceChildren();
    (list || []).forEach((c) => {
      const el = document.createElement("div");
      el.className = "check " + (c.status || "info");
      const label = document.createElement("div"), value = document.createElement("div");
      label.className = "check-label"; value.className = "check-value";
      label.textContent = c.label; value.textContent = c.value;
      el.append(label, value); grid.append(el);
    });
  }
  async function runPreflight() {
    const name = supervisedName;
    const epoch = demoEpoch;
    if (!name || pfPollBusy) return;
    pfPollBusy = true;
    try {
      const r = await fetch(demoMode ? "/api/demo/flight/state" : `/api/real/preflight/${encodeURIComponent(name)}`, { cache: "no-store", signal: AbortSignal.timeout(2500) });
      const j = await r.json();
      if (name !== supervisedName || epoch !== demoEpoch) return;
      if (!r.ok || !j.bridge_id) throw new Error(j.message || j.error || "Waiting for real-airframe telemetry");
      if (demoMode && j.demo !== true) throw new Error("Demo report expected; controls blocked");
      if (pfSnapshot?.bridge_id && pfSnapshot.bridge_id !== j.bridge_id) {
        commandNotice("Bridge restarted. Previous commands will not be retried.", true);
        pendingCommand = null;
      }
      pfSnapshot = j;
      if (demoMode) renderDemo(j);
      lastGoodPoll = Date.now();
      disableFlightControls();
      setMeta(`report age ${j.report_age_s ?? "?"} s · ${name}`);
      renderCheckGrid("pf-grid", j.checks);
      renderCheckGrid("flight-check-grid", j.flight_checks);
      const verdict = $("pf-verdict");
      verdict.className = "pf-verdict " + (j.ok ? "ok" : "bad");
      verdict.textContent = j.ok ? "PASSED · continuously ready" : "BLOCKED · " + (j.reasons || []).join("; ");
      $("pf-state").textContent = `${j.phase || "unknown"} · armed: ${j.armed == null ? "unknown" : j.armed ? "YES" : "NO"} · airborne: ${j.in_air == null ? "unknown" : j.in_air ? "YES" : "NO"} · altitude: ${Number.isFinite(j.alt) ? j.alt.toFixed(2) + " m" : "unknown"} · ${j.mode || "mode unknown"}`;
      if (j.flight_active) $("pf-state").textContent += ` · ${j.flight_remaining_s ?? "?"} s remaining`;
      $("pf-px4").textContent = (j.status_text || []).map((m) => m.type + ": " + m.text).join("\n") || "No messages received";
      if (pendingCommand) {
        const result = (j.commands || []).find((c) => c.id === pendingCommand.id);
        if (result) {
          const terminal = ["completed", "rejected", "failed", "cancelled"].includes(result.status);
          commandNotice(`${result.action}: ${result.status}${result.reason ? " · " + result.reason : ""}`, ["rejected", "failed"].includes(result.status));
          if (terminal) pendingCommand = null;
        } else if (Date.now() - pendingCommand.since > 5000) {
          commandNotice("No command acknowledgement. State is unknown; do not retry flight actions. LAND remains available.", true);
        }
      }
      const blocked = !!pendingCommand || j.operation_busy || landRequested;
      $("btn-arm").disabled = blocked || !j.can_arm;
      $("btn-disarm").disabled = blocked || !j.can_disarm || j.armed !== true;
      $("btn-motor-test").disabled = blocked || !j.can_arm;
      $("btn-test-hop").disabled = blocked || !j.flight_ready || j.armed !== true || j.in_air !== false;
      $("btn-test-takeoff").disabled = $("btn-test-hop").disabled;
      $("arm-hint").textContent = j.in_air ? "Airborne · use flight controls or LAND" : j.armed ? "Armed · start a test or disarm on the ground" : j.can_arm ? "10 seconds of confirmed readiness · ready" : j.operation_reason || "Resolve the reported blockers";
      const canMove = !blocked && j.can_move;
      document.querySelectorAll("[data-step]").forEach((b) => b.disabled = !canMove);
      $("btn-hold").disabled = !canMove; $("btn-test-return").disabled = !canMove;
      $("land-hint").textContent = j.operation_reason || (demoMode ? "DEMO · cancels the virtual test and lands the training model." : "LAND needs a working link. Keep RC backup ready.");
      if (j.phase === "landed" && j.armed === false && j.in_air === false) landRequested = false;
    } catch (e) {
      if (name === supervisedName) {
        lastGoodPoll = 0;
        disableFlightControls();
        // Keep the last confirmed state visible, labelled unknown. Never
        // convert a failed poll into a landed/disarmed state.
        $("pf-verdict").className = "pf-verdict bad";
        $("pf-verdict").textContent = "STATE UNKNOWN · " + e.message;
        renderCheckGrid("pf-grid", [{label: "TELEMETRY", value: "Unavailable — previous readiness is no longer valid", status: "bad"}]);
        renderCheckGrid("flight-check-grid", []);
        if (!$("pf-state").textContent.startsWith("LAST CONFIRMED")) $("pf-state").textContent = "LAST CONFIRMED · " + $("pf-state").textContent;
        $("arm-hint").textContent = "Telemetry unavailable; actions blocked";
        setMeta("telemetry unavailable · LAND / RC backup");
      }
    } finally { pfPollBusy = false; }
  }

  async function droneCommand(action, extra = {}) {
    const name = supervised();
    if (!name) return;
    if (action === "land") { landRequested = true; closeModals(); disableFlightControls(); }
    const send = () => fetch(demoMode ? "/api/demo/flight/command" : `/api/drone/${encodeURIComponent("veh_" + name)}/command`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action, ...extra }), signal: AbortSignal.timeout(2500),
    });
    try {
      if (!demoMode && action !== "land") {
        const lease = await fetch("/api/control/acquire", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ scope: "system" }), signal: AbortSignal.timeout(2500) });
        if (!lease.ok) throw new Error("Another operator holds control. LAND remains available.");
      }
      const r = await send();
      const j = await r.json();
      if (!r.ok || !j.ok) throw new Error(j.message || j.error || j.detail || `HTTP ${r.status}`);
      // HTTP acceptance only means queued. Completion comes from the bridge.
      if (action !== "test_keepalive") {
        pendingCommand = { id: j.command_id, action, since: Date.now() };
        commandNotice(`${action}: queued · awaiting bridge acknowledgement`);
        disableFlightControls();
      }
      return j;
    } catch (e) {
      if (action !== "test_keepalive") commandNotice(`${action}: ${e.message}${action === "land" ? " · landing NOT confirmed; use RC backup if needed" : ""}`, true);
      return null;
    }
  }
  async function doArm() { await droneCommand("arm", { safety_confirmed: true }); }
  async function doMotorTest() { await droneCommand("motor_test", { safety_confirmed: true }); }
  async function doTestHop() { await droneCommand(requestedFlight, { safety_confirmed: true }); }
  async function doDisarm() { await droneCommand("disarm"); }

  function renderDemo(j) {
    $("demo-status").textContent = j.link_connected ? "Virtual telemetry connected · " + j.scenario : "Virtual link lost · last reported position shown. Restore Healthy systems to reconnect.";
    if (j.position && j.link_connected) {
      const x = 160 + j.position.east_m * 22, y = 105 - j.position.north_m * 22;
      $("demo-drone").setAttribute("transform", `translate(${x} ${y})`);
      if (!demoTrace.length || Math.hypot(x-demoTrace.at(-1)[0], y-demoTrace.at(-1)[1]) > .2) demoTrace.push([x, y]);
      demoTrace = demoTrace.slice(-240);
      $("demo-trail").setAttribute("d", demoTrace.map((p, i) => `${i ? "L" : "M"}${p[0]} ${p[1]}`).join(" "));
      $("demo-position-label").textContent = `N ${j.position.north_m.toFixed(2)} m · E ${j.position.east_m.toFixed(2)} m · height ${(j.alt ?? 0).toFixed(2)} m`;
    }
  }

  async function startDemo(reset = false) {
    demoEpoch++;
    disableFlightControls();
    $("btn-demo-reset").disabled = true;
    if (pfTimer) clearInterval(pfTimer);
    pfTimer = null;
    try {
      if (reset) {
        const stopped = await fetch("/api/demo/flight", { method: "DELETE", signal: AbortSignal.timeout(2500) });
        if (!stopped.ok) throw new Error("Could not reset demo");
      }
      const r = await fetch("/api/demo/flight/start", { method: "POST", signal: AbortSignal.timeout(2500) });
      const j = await r.json();
      if (!r.ok || !j.demo) throw new Error(j.detail || "Could not start demo");
      supervisedName = null; pfSnapshot = null; pendingCommand = null; landRequested = false;
      demoTrace = []; $("demo-trail").setAttribute("d", "");
      $("demo-scenario").value = j.scenario;
      commandNotice(reset ? "Demo reset. The virtual board is disarmed at the start position." : "Demo connected. Wait for 10 seconds of continuous readiness.");
      startSupervision("walkthrough");
    } catch (e) { $("demo-status").textContent = e.message; }
    finally { $("btn-demo-reset").disabled = false; }
  }

  // This is separate from polling: successful web requests alone must never
  // count as an operator keepalive inside the bridge. No command auto-retry.
  setInterval(async () => {
    if (keepaliveBusy || !supervisedName || !pfSnapshot || !reportCurrent()) return;
    if (!pendingCommand && pfSnapshot.armed !== true && !pfSnapshot.flight_active) return;
    keepaliveBusy = true;
    try { await droneCommand("test_keepalive"); } finally { keepaliveBusy = false; }
  }, 1000);

  // ---- modals ----
  function openModal(id) {
    const m = $(id);
    if (!m) return;
    m.querySelectorAll('input[type=checkbox]').forEach((c) => (c.checked = false));
    m.querySelectorAll('.pf-modal-foot .btn-primary').forEach((b) => { if (b.id.startsWith("confirm-")) b.disabled = true; });
    m.classList.remove("hidden");
  }
  function closeModals() { document.querySelectorAll(".pf-overlay").forEach((o) => o.classList.add("hidden")); }
  function wireModalGate(modalId, confirmId) {
    const modal = $(modalId), btn = $(confirmId);
    if (!modal || !btn) return;
    const boxes = [...modal.querySelectorAll('input[type=checkbox]')];
    const upd = () => { btn.disabled = !boxes.every((b) => b.checked); };
    boxes.forEach((b) => b.addEventListener("change", upd));
  }
  function setGuideLang(l) {
    document.querySelectorAll('.pf-lang button').forEach((b) => b.classList.toggle("on", b.dataset.lang === l));
    $("guide-en")?.classList.toggle("hidden", l !== "en");
    $("guide-ru")?.classList.toggle("hidden", l !== "ru");
  }

  async function onRadioAutodetect() {
    if (!radioLink) return;
    setRadioStatus("detecting baud…", "warn");
    try {
      const res = await RadioLink.detectBaud((b) => setRadioStatus("trying " + b + "…", "warn"));
      if (res && res.baud) { setRadioBaud(res.baud); setRadioStatus("detected: " + res.baud + " baud", "on"); }
      else setRadioStatus("no MAVLink detected — set baud manually", "bad");
    } catch (e) { setRadioStatus("failed: " + e.message, "bad"); }
  }

  // Read-only проверка радио-линка: поднимаем serial+WS (без полного моста),
  // сервер читает heartbeat/health через реле, показываем в §C.
  async function testRadio() {
    if (!validateForm()) return;
    const data = readForm();
    if (!radioLink) { alert("Web Serial is not supported in this browser — open it in Chrome or Edge"); return; }

    const card = $("test-results-card");
    card.classList.remove("hidden");
    $("test-meta").textContent = "radio · opening USB + bridging…";
    $("check-grid").innerHTML = '<div class="check info"><div class="check-label">Status</div><div class="check-value">⏳ link up…</div></div>';
    $("raw-result").textContent = "";

    // 1) поднять линк только до реле (без mavsdk_bridge)
    try {
      await radioLink.start({
        name: data.name, baud: radioBaud, force: true, spawnBridge: false,
        onStatus: (phase, detail) => {
          if (phase === "error") setRadioStatus("error: " + detail, "bad");
          else if (phase === "linked") setRadioStatus("link up (testing)", "warn");
          else setRadioStatus(phase + (detail ? " " + detail : ""), "warn");
        },
      });
    } catch (e) {
      $("test-meta").textContent = "radio · could not open USB";
      renderChecks({ ok: false, error: "serial", message: e.message, heartbeat: false });
      setRadioStatus("idle", "");
      return;
    }

    // 2) серверная read-only проверка через реле
    $("test-meta").textContent = "radio · reading heartbeat up to 12 s…";
    const startedAt = Date.now();
    let j;
    try {
      const r = await fetch(`/api/real/radio/test/${encodeURIComponent(data.name)}`, { method: "POST" });
      j = await r.json();
    } catch (e) {
      j = { ok: false, error: "request_failed", message: e.message, heartbeat: false };
    }
    const elapsed = ((Date.now() - startedAt) / 1000).toFixed(1);
    $("test-meta").textContent = `${elapsed} s · radio relay${j.grpc_port ? " gRPC :" + j.grpc_port : ""}`;
    renderChecks(j);
    $("raw-result").textContent = JSON.stringify(j, null, 2);

    // 3) тест завершён — отцепляем линк (освобождаем порт и реле)
    await radioLink.stop();
    setRadioStatus("idle", "");
  }

  function selectProfile(p) {
    if (supervisedName && p.name !== supervisedName && riskyState()) {
      commandNotice("Keep the active drone selected until landing and disarm are confirmed.", true);
      return;
    }
    activeName = p.name;
    lastTestOk = false;
    fillForm(p);
    $("form-title").textContent = `Profile: ${p.name}`;
    $("btn-delete-profile").classList.remove("hidden");
    $("test-results-card").classList.add("hidden");
    syncRadioUi(p.name);   // статус/кнопки радио — для ЭТОГО профиля, не прошлого
    updateSaveButton();
    // подсветить активную карточку без полной перерисовки
    document.querySelectorAll("#profile-list .pcard").forEach((el) =>
      el.classList.toggle("active", el.dataset.name === p.name));
  }

  // Сброс формы под новый профиль (кнопка New profile).
  function newProfile() {
    if (supervisedName && riskyState()) { commandNotice("Land and disarm before switching profiles.", true); return; }
    activeName = null;
    lastTestOk = false;
    $("f-name").value = "";
    $("f-display").value = "";
    $("f-notes").value = "";
    $("f-net-host").value = "";
    $("f-net-port").value = "";
    clearErrors();
    setConnType("radio");
    setRadioBaud(57600);
    $("form-title").textContent = "New connection";
    $("btn-delete-profile").classList.add("hidden");
    $("test-results-card").classList.add("hidden");
    document.querySelectorAll("#profile-list .pcard").forEach((el) => el.classList.remove("active"));
    syncRadioUi(null);   // новый профиль ещё ни к чему не подключён
    updateSaveButton();
    $("f-name").focus();
  }

  // Полная (ре)сборка списка — только при изменении НАБОРА профилей / выборе.
  function renderProfiles() {
    $("profile-count").textContent = String(profiles.length);
    const list = $("profile-list");
    if (profiles.length === 0) {
      list.innerHTML = '<div class="profile-empty">— no saved connections yet —</div>';
      return;
    }
    list.innerHTML = "";
    for (const p of profiles) {
      let sub;
      if (p.connection_type === "serial") {
        sub = `serial · ${p.serial_port?.split("/").pop() || "?"} @ ${p.serial_baud}`;
      } else if (p.connection_type === "radio") {
        sub = `radio · ${p.serial_baud || 57600} baud · browser`;
      } else {
        sub = `${p.connection_type.toUpperCase()} · ${p.net_host || "0.0.0.0"}:${p.net_port}`;
      }
      const el = document.createElement("div");
      el.className = "pcard" + (p.name === activeName ? " active" : "");
      el.dataset.name = p.name;
      el.innerHTML = `
        <div class="pcard-main">
          <span class="pcard-dot" data-dot></span>
          <div class="pcard-info">
            <div class="pcard-name">${p.display_name || p.name}</div>
            <div class="pcard-meta mono">${sub}</div>
          </div>
        </div>
        <div class="pcard-live mono" data-live hidden></div>
      `;
      // Клик по карточке = открыть профиль (загрузить параметры в поля формы).
      el.addEventListener("click", () => selectProfile(p));
      list.appendChild(el);
    }
    refreshLive();
  }

  // Лёгкое обновление статуса/телеметрии БЕЗ перестроения DOM — нет мерцания.
  function refreshLive() {
    document.querySelectorAll("#profile-list .pcard").forEach((el) => {
      const name = el.dataset.name;
      const connected = !!(connections[name] && connections[name].connected);
      el.classList.toggle("connected", connected);
      const dot = el.querySelector("[data-dot]");
      const live = el.querySelector("[data-live]");
      if (dot) dot.className = "pcard-dot" + (connected ? " on" : "");
      // Кнопка карточки всегда "Open" (загрузка профиля в поля) — больше не
      // превращаем её в Connect/Disconnect.
      if (!live) return;
      if (!connected) { live.hidden = true; live.textContent = ""; return; }
      live.hidden = false;
      const d = fleet["veh_" + name];
      if (!d) { live.innerHTML = '<span class="pcard-wait">link up · waiting for telemetry…</span>'; return; }
      const alt = d.alt != null ? Number(d.alt).toFixed(1) + "m" : "—";
      const batt = d.battery_pct != null ? Math.round(d.battery_pct) + "%" : "—";
      const sats = d.sat_count != null ? d.sat_count + " sat" : "—";
      const pos = d.lat != null ? `${Number(d.lat).toFixed(5)}, ${Number(d.lon).toFixed(5)}` : "—";
      live.innerHTML = `<b>${d.status || "LINK"}</b> · ALT ${alt} · BAT ${batt} · GPS ${sats}<br><span class="pcard-pos">${pos}</span>`;
    });
  }

  // ========== Wiring ==========
  document.querySelectorAll('[data-conn]').forEach(btn => {
    btn.addEventListener("click", () => setConnType(btn.dataset.conn));
  });
  document.querySelectorAll('#baud-seg [data-baud]').forEach(btn => {
    btn.addEventListener("click", () => {
      if (btn.dataset.baud === "custom") {
        document.querySelectorAll('#baud-seg [data-baud]').forEach(b => b.classList.remove("active"));
        btn.classList.add("active");
        $("f-baud-custom").classList.remove("hidden");
        $("f-baud-custom").focus();
      } else {
        setBaud(btn.dataset.baud);
      }
    });
  });
  $("f-baud-custom")?.addEventListener("input", (e) => {
    const v = parseInt(e.target.value, 10);
    if (!isNaN(v)) baud = v;
  });

  $("btn-reload-ports")?.addEventListener("click", loadSerialPorts);
  $("btn-delete-profile").addEventListener("click", deleteCurrent);
  $("btn-new-profile")?.addEventListener("click", newProfile);
  $("btn-test-conn").addEventListener("click", () => (connType === "radio" ? testRadio() : testConnection()));
  $("btn-save").addEventListener("click", saveProfile);

  // Radio: выбор baud + connect/disconnect/auto-detect
  document.querySelectorAll('#radio-baud-seg [data-rbaud]').forEach(btn =>
    btn.addEventListener("click", () => setRadioBaud(btn.dataset.rbaud)));
  $("btn-radio-connect")?.addEventListener("click", onRadioConnectClick);
  $("btn-radio-disconnect")?.addEventListener("click", stopRadio);

  // Pre-flight & arm (Step 4)
  $("btn-check")?.addEventListener("click", onCheckClick);
  $("btn-guide")?.addEventListener("click", () => openModal("modal-guide"));

  // open confirmation modals
  $("btn-arm")?.addEventListener("click", () => openModal("modal-arm"));
  $("btn-motor-test")?.addEventListener("click", () => openModal("modal-motor"));
  $("btn-test-hop")?.addEventListener("click", () => { requestedFlight = "test_hop"; openModal("modal-hop"); });
  $("btn-test-takeoff")?.addEventListener("click", () => { requestedFlight = "test_takeoff"; openModal("modal-hop"); });
  document.querySelectorAll("[data-step]").forEach((b) => b.addEventListener("click", () => droneCommand("test_step", { direction: b.dataset.step })));
  $("btn-test-return")?.addEventListener("click", () => droneCommand("test_return"));
  $("btn-hold")?.addEventListener("click", () => droneCommand("hold"));
  $("btn-net-connect")?.addEventListener("click", async () => {
    if (!validateForm()) return;
    await saveProfile();
    if (activeName) { await connectProfile(activeName); startSupervision(activeName); }
  });


  // confirm actions
  $("confirm-arm")?.addEventListener("click", () => { closeModals(); doArm(false); });
  $("confirm-motor")?.addEventListener("click", () => { closeModals(); doMotorTest(); });
  $("confirm-hop")?.addEventListener("click", () => { closeModals(); doTestHop(); });

  // in-flight + disarm
  $("btn-land")?.addEventListener("click", () => droneCommand("land"));
  $("btn-rtl")?.addEventListener("click", () => droneCommand("rtl"));
  $("btn-disarm")?.addEventListener("click", doDisarm);

  // modal plumbing: checkbox gating, close buttons, lang toggle, click-outside
  wireModalGate("modal-arm", "confirm-arm");
  wireModalGate("modal-motor", "confirm-motor");
  wireModalGate("modal-hop", "confirm-hop");
  document.querySelectorAll("[data-close]").forEach((b) => b.addEventListener("click", closeModals));
  document.querySelectorAll(".pf-lang button").forEach((b) => b.addEventListener("click", () => setGuideLang(b.dataset.lang)));
  document.querySelectorAll(".pf-overlay").forEach((o) => o.addEventListener("click", (e) => { if (e.target === o) closeModals(); }));

  // Живая валидация: поле имени принимает только разрешённые символы, а ошибки
  // под полями гаснут по мере исправления (серверных ошибок ввода больше нет).
  $("f-name")?.addEventListener("input", (e) => {
    const cleaned = e.target.value.replace(/[^a-zA-Z0-9_\-]/g, "").slice(0, 40);
    if (cleaned !== e.target.value) e.target.value = cleaned;
    setFieldError($("f-name"), "");
  });
  $("f-net-host")?.addEventListener("input", () => setFieldError($("f-net-host"), ""));
  $("f-net-port")?.addEventListener("input", () => setFieldError($("f-net-port"), ""));
  // Если оператор уходит со страницы — корректно гасим радио-линк.
  window.addEventListener("beforeunload", () => {
    if (demoMode) return; // never send a walkthrough command to a hardware endpoint
    if (supervisedName && riskyState()) {
      fetch(`/api/drone/${encodeURIComponent("veh_" + supervisedName)}/command`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ action: "land" }), keepalive: true }).catch(() => {});
    }
    // Do not intentionally tear down the radio ahead of a LAND request.
    // Closing the tab still loses Web Serial; PX4 loss failsafes are required.
  });

  // Любое изменение формы инвалидирует прошлый Test Connection.
  $("real-form").addEventListener("input", () => {
    if (lastTestOk) {
      lastTestOk = false;
      updateSaveButton();
    }
  });

  // ========== Init ==========
  if (demoMode) {
    document.body.classList.add("demo-page");
    document.title = "Skybite · Virtual board demo";
    document.querySelector(".brand-name").textContent = "Virtual board · DEMO";
    $("demo-panel").classList.remove("hidden");
    $("demo-entry").classList.add("hidden");
    $("quick-launch-link").classList.add("hidden");
    $("form-title").textContent = "Virtual board walkthrough";
    $("step4-wstep").querySelector(".wstep-num").textContent = "1";
    document.querySelector(".w-sub").textContent = "Same controls and safety checks, with an isolated training model.";
    [...document.querySelectorAll("#real-form > .wstep")].slice(0, 3).forEach((el) => el.classList.add("hidden"));
    $("btn-delete-profile").classList.add("hidden");
    $("btn-land").textContent = "DEMO LAND · cancel virtual test";
    $("board-messages-label").textContent = "Virtual board events";
    $("flight-safeguards-label").textContent = "Training safeguards · simulated values";
    $("flight-safeguards-hint").textContent = "These generated values let you rehearse the checks. No parameters are read from or written to a real autopilot.";
    $("btn-demo-reset").addEventListener("click", () => startDemo(true));
    $("demo-exit").addEventListener("click", () => { fetch("/api/demo/flight", { method: "DELETE", keepalive: true }).catch(() => {}); });
    $("demo-scenario").addEventListener("change", async () => {
      try {
        const r = await fetch("/api/demo/flight/scenario", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({scenario: $("demo-scenario").value}), signal: AbortSignal.timeout(2500)});
        if (!r.ok) throw new Error("Scenario change failed; reset the demo");
        await runPreflight();
      } catch (e) { $("demo-status").textContent = e.message; }
    });
    startDemo();
    return;
  }
  setConnType("radio");   // дефолтный канал — Radio (Serial-форма убрана)
  loadProfiles();
  loadConnections();
  setInterval(loadConnections, 2000);   // живой статус + телеметрия подключённых бортов
  updateSaveButton();
})();
