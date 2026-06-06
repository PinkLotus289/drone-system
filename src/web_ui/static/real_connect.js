// Real Connect UI: профили + форма + Test Connection
(() => {
  const $ = (id) => document.getElementById(id);

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
    // Test connection доступен для всех каналов (для radio — через реле, см. testRadio).
    const testBtn = $("btn-test-conn");
    if (testBtn) testBtn.classList.remove("hidden");
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
      el.innerHTML = "✓ This browser supports Web Serial — radio connection available.";
    } else {
      el.className = "radio-support bad";
      el.innerHTML = "⚠ This browser does not support Web Serial. Open the page in <b>Chrome</b>, <b>Edge</b>, Brave or Opera on a desktop (not on a phone).";
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
    // Для radio сохранять можно без серверного теста (проверка = сам коннект).
    const gateOk = (connType === "radio") || lastTestOk;
    $("btn-save").disabled = !gateOk;
    $("btn-save").title = gateOk ? "Save profile" : "Run a successful Test Connection first";
    const ready = document.getElementById("ready-state");
    if (ready) {
      if (connType === "radio") {
        ready.textContent = "radio · save then connect";
        ready.style.color = "var(--ink-3)";
      } else {
        ready.textContent = lastTestOk ? "✓ validated · ready to save" : "awaiting test";
        ready.style.color = lastTestOk ? "var(--green)" : "var(--ink-3)";
      }
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
        hint.textContent = "connect cable/radio and press ↻";
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
      alert("Save failed: " + (err.detail || r.statusText));
    }
  }

  async function testConnection() {
    const body = readForm();
    if (body.connection_type === "serial" && !body.serial_port) {
      alert("Select a serial port");
      return;
    }
    if (body.connection_type !== "serial" && (!body.net_port)) {
      alert("Set host/port");
      return;
    }

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

    add("HEARTBEAT", j.heartbeat ? "✓ OK" : "✗ no", j.heartbeat ? "ok" : "bad");

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
      const cls = v >= 14.5 ? "ok" : v >= 13.8 ? "warn" : "bad";
      add("BATTERY", `${v?.toFixed?.(2) ?? "?"} V · ${(pct*100).toFixed(0)}%`, cls);
    } else if (j.battery_error) {
      add("BATTERY", "n/a", "warn");
    }

    if (j.health) {
      const h = j.health;
      add("CALIB GYRO", h.gyro_ok ? "✓" : "✗", h.gyro_ok ? "ok" : "bad");
      add("CALIB ACCEL", h.accel_ok ? "✓" : "✗", h.accel_ok ? "ok" : "bad");
      add("CALIB MAG", h.mag_ok ? "✓" : "✗", h.mag_ok ? "ok" : "bad");
      add("GLOBAL POS", h.global_pos_ok ? "✓" : "✗", h.global_pos_ok ? "ok" : "warn");
      add("HOME POS", h.home_ok ? "✓" : "✗", h.home_ok ? "ok" : "warn");
      add("ARMABLE", h.armable ? "✓ READY" : "✗ NOT READY", h.armable ? "ok" : "bad");
    }

    if (j.flight_mode) {
      add("FLIGHT MODE", j.flight_mode.replace(/^.*\./, ""), "info");
    }
  }

  // ========== Render profile list ==========
  async function loadConnections() {
    try {
      const r = await fetch("/api/real/connections");
      if (r.status === 401) { location.href = "/login"; return; }
      const j = await r.json();
      connections = j.connections || {};
      radioStatus = j.radio || {};
      await loadFleet();
      refreshLive();   // только обновляем статус/телеметрию в существующих карточках
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
  function setRadioStatus(text, kind) {
    const s = $("radio-status"); const dot = $("radio-dot");
    if (s) s.textContent = text;
    if (dot) dot.className = "radio-dot" + (kind ? " " + kind : "");
  }
  function resetRadioButtons() {
    $("btn-radio-connect")?.classList.remove("hidden");
    $("btn-radio-disconnect")?.classList.add("hidden");
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
      "linked": ["● on air", "on"],
      "reconnecting": ["reconnecting…", "warn"],
      "stopped": ["idle", ""],
    };
    try {
      await radioLink.start({
        name, baud: baudVal || radioBaud, force: true,
        onStatus: (phase, detail) => {
          if (phase === "warn") { setRadioStatus("⚠ " + detail, "warn"); return; }
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
    if (radioLink) { try { await radioLink.stop(); } catch { /* ignore */ } }
    radioActiveName = null;
    resetRadioButtons();
    setRadioStatus("idle", "");
    loadConnections();
  }

  async function onRadioConnectClick() {
    const data = readForm();
    if (!data.name) { alert("Set a profile name first (§B · Airframe)"); $("f-name").focus(); return; }
    await saveProfile();           // profile must exist (relay key = name)
    await startRadio(data.name, radioBaud);
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
    const data = readForm();
    if (!data.name) { alert("Set a profile name first (§B · Airframe)"); $("f-name").focus(); return; }
    if (!radioLink) { alert("Web Serial is not supported in this browser — open it in Chrome or Edge"); return; }
    await saveProfile();

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
          else if (phase === "linked") setRadioStatus("● link up (testing)", "warn");
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
    activeName = p.name;
    lastTestOk = false;
    fillForm(p);
    $("form-title").textContent = `Profile: ${p.name}`;
    $("btn-delete-profile").classList.remove("hidden");
    $("test-results-card").classList.add("hidden");
    updateSaveButton();
    // подсветить активную карточку без полной перерисовки
    document.querySelectorAll("#profile-list .pcard").forEach((el) =>
      el.classList.toggle("active", el.dataset.name === p.name));
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
        sub = `📡 radio · ${p.serial_baud || 57600} baud · browser`;
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
          <button class="pcard-btn" data-btn type="button">Connect</button>
        </div>
        <div class="pcard-live mono" data-live hidden></div>
      `;
      el.addEventListener("click", () => selectProfile(p));
      el.querySelector("[data-btn]").addEventListener("click", (e) => {
        e.stopPropagation();
        const connected = !!(connections[p.name] && connections[p.name].connected);
        if (p.connection_type === "radio") {
          // Radio коннектится через браузер (Web Serial), не серверным POST.
          if (connected || radioActiveName === p.name) stopRadio();
          else startRadio(p.name, p.serial_baud || 57600);
        } else if (connected) {
          disconnectProfile(p.name);
        } else {
          connectProfile(p.name);
        }
      });
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
      const btn = el.querySelector("[data-btn]");
      const live = el.querySelector("[data-live]");
      if (dot) dot.className = "pcard-dot" + (connected ? " on" : "");
      if (btn) { btn.textContent = connected ? "Disconnect" : "Connect"; btn.className = "pcard-btn" + (connected ? " is-disc" : ""); }
      if (!live) return;
      if (!connected) { live.hidden = true; live.textContent = ""; return; }
      live.hidden = false;
      const d = fleet["veh_" + name];
      if (!d) { live.innerHTML = '<span class="pcard-wait">link up · waiting for telemetry…</span>'; return; }
      const alt = d.alt != null ? Number(d.alt).toFixed(1) + "m" : "—";
      const batt = d.battery_pct != null ? Math.round(d.battery_pct) + "%" : "—";
      const sats = d.sat_count != null ? d.sat_count + "🛰" : "—";
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
  $("btn-test-conn").addEventListener("click", () => (connType === "radio" ? testRadio() : testConnection()));
  $("btn-save").addEventListener("click", saveProfile);

  // Radio: выбор baud + connect/disconnect/auto-detect
  document.querySelectorAll('#radio-baud-seg [data-rbaud]').forEach(btn =>
    btn.addEventListener("click", () => setRadioBaud(btn.dataset.rbaud)));
  $("btn-radio-connect")?.addEventListener("click", onRadioConnectClick);
  $("btn-radio-disconnect")?.addEventListener("click", stopRadio);
  $("btn-radio-autodetect")?.addEventListener("click", onRadioAutodetect);
  // Если оператор уходит со страницы — корректно гасим радио-линк.
  window.addEventListener("beforeunload", () => { if (radioActiveName && radioLink) radioLink.stop(); });

  // Любое изменение формы инвалидирует прошлый Test Connection.
  $("real-form").addEventListener("input", () => {
    if (lastTestOk) {
      lastTestOk = false;
      updateSaveButton();
    }
  });

  // ========== Init ==========
  setConnType("radio");   // дефолтный канал — Radio (Serial-форма убрана)
  loadProfiles();
  loadConnections();
  setInterval(loadConnections, 2000);   // живой статус + телеметрия подключённых бортов
  updateSaveButton();
})();
