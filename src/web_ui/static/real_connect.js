// Real Connect UI: профили + форма + Test Connection
(() => {
  const $ = (id) => document.getElementById(id);

  // ========== State ==========
  let profiles = [];
  let activeName = null;            // имя выбранного профиля или null если "новый"
  let lastTestOk = false;            // прошёл ли последний test connection — гейтит save
  let connType = "serial";
  let baud = 57600;

  // ========== Helpers ==========
  function fillForm(p) {
    $("f-name").value = p.name || "";
    $("f-display").value = p.display_name || "";
    $("f-frame").value = p.frame || "X500";
    $("f-cells").value = p.battery_cells || 4;
    $("f-vmin").value = p.battery_min_voltage ?? 14.0;
    $("f-notes").value = p.notes || "";
    setConnType(p.connection_type || "serial");
    if (p.connection_type === "serial") {
      // Если сохранённого порта нет в списке — добавим как один из вариантов.
      const sel = $("f-serial-port");
      if (p.serial_port && ![...sel.options].some(o => o.value === p.serial_port)) {
        const opt = document.createElement("option");
        opt.value = p.serial_port;
        opt.textContent = `${p.serial_port} (offline)`;
        sel.appendChild(opt);
      }
      sel.value = p.serial_port || "";
      setBaud(p.serial_baud || 57600);
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
      frame: $("f-frame").value.trim() || "X500",
      battery_cells: parseInt($("f-cells").value, 10) || 4,
      battery_min_voltage: parseFloat($("f-vmin").value) || 14.0,
      notes: $("f-notes").value.trim(),
    };
    if (connType === "serial") {
      data.serial_port = $("f-serial-port").value || null;
      data.serial_baud = baud;
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
    $("form-title").textContent = "Подключение нового борта";
    $("btn-delete-profile").classList.add("hidden");
    $("real-form").reset();
    setConnType("serial");
    setBaud(57600);
    $("f-frame").value = "X500";
    $("f-cells").value = 4;
    $("f-vmin").value = 14.0;
    $("test-results-card").classList.add("hidden");
    updateSaveButton();
    document.querySelectorAll(".profile-item").forEach(el => el.classList.remove("active"));
  }

  function setConnType(t) {
    connType = t;
    document.querySelectorAll('[data-conn]').forEach(b => {
      b.classList.toggle("active", b.dataset.conn === t);
    });
    $("block-serial").classList.toggle("hidden", t !== "serial");
    $("block-net").classList.toggle("hidden", t === "serial");
  }

  function setBaud(b) {
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
    $("btn-save").disabled = !lastTestOk;
    $("btn-save").title = lastTestOk
      ? "Сохранить профиль"
      : "Сначала пройдите Test Connection";
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
    sel.innerHTML = '<option value="">обновляю...</option>';
    try {
      const r = await fetch("/api/real/serial-ports");
      const j = await r.json();
      sel.innerHTML = "";
      if (!j.ports || j.ports.length === 0) {
        sel.innerHTML = '<option value="">— нет доступных портов —</option>';
        hint.textContent = "подключи кабель/радио и нажми ↻";
        return;
      }
      for (const p of j.ports) {
        const opt = document.createElement("option");
        opt.value = p.path;
        opt.textContent = p.hint ? `${p.label}  [${p.hint}]` : p.label;
        sel.appendChild(opt);
      }
      hint.textContent = `найдено портов: ${j.ports.length}`;
    } catch (e) {
      hint.textContent = "ошибка: " + e.message;
    }
  }

  async function deleteCurrent() {
    if (!activeName) return;
    if (!confirm(`Удалить профиль "${activeName}"?`)) return;
    const r = await fetch(`/api/real/profiles/${encodeURIComponent(activeName)}`, { method: "DELETE" });
    if (r.ok) {
      await loadProfiles();
      clearForm();
    } else {
      alert("Не удалось удалить профиль");
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
      $("form-title").textContent = `Профиль: ${saved.name}`;
      $("btn-delete-profile").classList.remove("hidden");
      // Подсветка в сайдбаре
      document.querySelectorAll(".profile-item").forEach(el => {
        el.classList.toggle("active", el.dataset.name === saved.name);
      });
    } else {
      const err = await r.json().catch(() => ({}));
      alert("Ошибка сохранения: " + (err.detail || r.statusText));
    }
  }

  async function testConnection() {
    const body = readForm();
    if (body.connection_type === "serial" && !body.serial_port) {
      alert("Выбери serial-порт");
      return;
    }
    if (body.connection_type !== "serial" && (!body.net_port)) {
      alert("Укажи host/port");
      return;
    }

    const card = $("test-results-card");
    card.classList.remove("hidden");
    $("test-meta").textContent = "тестирую... heartbeat ждём до 12с";
    $("check-grid").innerHTML = '<div class="check info"><div class="check-label">STATUS</div><div class="check-value">⏳ pending...</div></div>';
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

    $("test-meta").textContent = `${elapsed}с · gRPC :${j.grpc_port || "?"} · ${j.connection_url}`;
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
  function renderProfiles() {
    $("profile-count").textContent = String(profiles.length);
    const list = $("profile-list");
    if (profiles.length === 0) {
      list.innerHTML = '<div class="profile-empty">пусто — создайте профиль справа</div>';
      return;
    }
    list.innerHTML = "";
    for (const p of profiles) {
      const el = document.createElement("div");
      el.className = "profile-item" + (p.name === activeName ? " active" : "");
      el.dataset.name = p.name;
      const sub = p.connection_type === "serial"
        ? `serial · ${p.serial_port?.split("/").pop() || "?"} @ ${p.serial_baud}`
        : `${p.connection_type} · ${p.net_host || ""}:${p.net_port}`;
      el.innerHTML = `
        <div class="profile-name">${p.display_name || p.name}</div>
        <div class="profile-meta mono">${sub}</div>
      `;
      el.addEventListener("click", () => {
        activeName = p.name;
        lastTestOk = false;
        fillForm(p);
        $("form-title").textContent = `Профиль: ${p.name}`;
        $("btn-delete-profile").classList.remove("hidden");
        $("test-results-card").classList.add("hidden");
        updateSaveButton();
        renderProfiles();
      });
      list.appendChild(el);
    }
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
  $("f-baud-custom").addEventListener("input", (e) => {
    const v = parseInt(e.target.value, 10);
    if (!isNaN(v)) baud = v;
  });

  $("btn-reload-ports").addEventListener("click", loadSerialPorts);
  $("btn-new-profile").addEventListener("click", clearForm);
  $("btn-delete-profile").addEventListener("click", deleteCurrent);
  $("btn-test-conn").addEventListener("click", testConnection);
  $("btn-save").addEventListener("click", saveProfile);

  // Любое изменение формы инвалидирует прошлый Test Connection.
  $("real-form").addEventListener("input", () => {
    if (lastTestOk) {
      lastTestOk = false;
      updateSaveButton();
    }
  });

  // ========== Init ==========
  loadProfiles();
  loadSerialPorts();
  updateSaveButton();
})();
