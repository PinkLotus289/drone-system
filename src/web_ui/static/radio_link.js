// =====================================================================
// Skybite Radio Link — браузерный «связной» для наземного USB-радио.
//
// Читает MAVLink-байты из serial-порта (Web Serial API) и гонит их на сервер
// по WebSocket; обратно — команды с сервера пишет в порт. Сервер релеит поток
// в локальный MAVSDK-мост (см. radio_relay.py). Работает в Chromium-браузерах
// (Chrome/Edge/Brave/Opera) на десктопе; требует https и клик пользователя для
// доступа к порту.
//
// API:  const link = new RadioLink();
//       RadioLink.isSupported()                  → bool
//       await link.start({ name, baud, force, onStatus, onError })
//       await link.stop()
//       await RadioLink.detectBaud(onProgress)   → {port, baud} | null
// =====================================================================
(function () {
  const MAVLINK_MAGIC = new Set([0xfd, 0xfe]); // v2 (0xFD), v1 (0xFE)

  function wsUrl(name) {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    return `${proto}//${location.host}/api/real/radio/agent?name=${encodeURIComponent(name)}`;
  }

  class RadioLink {
    constructor() {
      this._port = null;
      this._ws = null;
      this._reader = null;
      this._writer = null;
      this._active = false;
      this._name = null;
      this._onStatus = () => {};
      this._onError = () => {};
      this._reconnectTimer = null;
      this._noDataTimer = null;
      this._sawMavlink = false;
      this._magicCount = 0;
    }

    // Порог «реально пошёл MAVLink» и время ожидания первых данных от борта.
    static _MAV_RX_THRESHOLD = 6;
    static _NO_DATA_MS = 8000;

    static isSupported() {
      return typeof navigator !== "undefined" && "serial" in navigator;
    }

    // Порты, к которым доступ уже выдан ранее (для авто-переподключения).
    static async knownPorts() {
      if (!RadioLink.isSupported()) return [];
      try { return await navigator.serial.getPorts(); } catch { return []; }
    }

    _status(phase, detail) { try { this._onStatus(phase, detail || ""); } catch {} }

    // ---- выбор порта -------------------------------------------------
    async _choosePort(force) {
      // Без принуждения — пробуем уже авторизованный порт (тихое переподключение).
      if (!force) {
        const known = await RadioLink.knownPorts();
        if (known.length === 1) return known[0];
        if (known.length > 1) return known[0];
      }
      // Иначе показываем системный пикер браузера (нужен жест пользователя).
      this._status("requesting-port");
      return await navigator.serial.requestPort(); // фильтров нет — показываем все устройства
    }

    // ---- старт -------------------------------------------------------
    // spawnBridge=false — только поднять serial+WS (для Test connection): сервер
    // сам подцепит MAVSDK к реле, полноценный mavsdk_bridge не нужен.
    async start({ name, baud = 57600, force = false, spawnBridge = true, onStatus, onError } = {}) {
      if (!RadioLink.isSupported()) {
        throw new Error("Web Serial is not supported in this browser — open it in Chrome or Edge");
      }
      if (this._active) await this.stop();
      this._name = name;
      this._baud = baud;
      this._onStatus = onStatus || (() => {});
      this._onError = onError || (() => {});
      this._active = true;

      // 1) порт
      const port = await this._choosePort(force);
      this._port = port;
      await port.open({ baudRate: baud, bufferSize: 4096 });
      this._status("port-open", `${baud} baud`);

      // 2) WS до сервера (+ ensure relay) и старт насосов
      await this._openWs();

      // 3) поднимаем серверный мост к борту (MAVSDK ← relay tcp).
      //    Для Test connection (spawnBridge=false) мост не нужен — сервер сам
      //    подключит read-only MAVSDK к реле.
      if (spawnBridge) {
        this._status("connecting-server");
        try {
          const r = await fetch(`/api/real/connect/${encodeURIComponent(name)}`, { method: "POST" });
          const j = await r.json().catch(() => ({}));
          if (!j.ok && j.error !== "already_connected") {
            this._status("warn", `bridge: ${j.error || r.status}`);
          }
        } catch (e) {
          this._status("warn", `bridge: ${e.message}`);
        }
      }

      // насос serial → ws (читает порт до stop())
      // Транспорт поднят, но это ещё НЕ значит, что на том конце дрон: статус
      // "on air" выставляется только когда реально пошли MAVLink-кадры (см.
      // _pumpSerialToWs). До этого — "link-up" (ждём борт).
      this._sawMavlink = false;
      this._magicCount = 0;
      this._armNoDataWatchdog();
      this._pumpSerialToWs();
      this._status("link-up");
      return true;
    }

    // Если за _NO_DATA_MS не пришло ни одного MAVLink-кадра — предупреждаем:
    // скорее всего выбран не тот порт/скорость или дрон не на связи.
    _armNoDataWatchdog() {
      if (this._noDataTimer) clearTimeout(this._noDataTimer);
      this._noDataTimer = setTimeout(() => {
        if (this._active && !this._sawMavlink) {
          this._status("no-data");
        }
      }, RadioLink._NO_DATA_MS);
    }

    async _openWs() {
      return new Promise((resolve, reject) => {
        let settled = false;
        const ws = new WebSocket(wsUrl(this._name));
        ws.binaryType = "arraybuffer";
        this._ws = ws;
        ws.onopen = () => { settled = true; this._status("server-open"); resolve(); };
        ws.onmessage = (ev) => { this._onWsBytes(ev.data); };
        ws.onerror = () => { if (!settled) { settled = true; reject(new Error("WS error")); } };
        ws.onclose = () => {
          if (!settled) { settled = true; reject(new Error("WS closed before open")); return; }
          if (this._active) this._scheduleReconnect();
        };
      });
    }

    _scheduleReconnect() {
      if (this._reconnectTimer || !this._active) return;
      this._status("reconnecting");
      this._reconnectTimer = setTimeout(async () => {
        this._reconnectTimer = null;
        if (!this._active) return;
        try { await this._openWs(); this._status(this._sawMavlink ? "on-air" : "link-up"); }
        catch { this._scheduleReconnect(); }
      }, 2000);
    }

    // ---- насосы байтов ----------------------------------------------
    async _pumpSerialToWs() {
      try {
        this._reader = this._port.readable.getReader();
        while (this._active) {
          const { value, done } = await this._reader.read();
          if (done) break;
          if (value && value.length) {
            if (!this._sawMavlink) this._sniffMavlink(value);
            if (this._ws && this._ws.readyState === WebSocket.OPEN) {
              this._ws.send(value); // Uint8Array → бинарный фрейм
            }
          }
        }
      } catch (e) {
        if (this._active) this._fail(`serial read: ${e.message}`);
      } finally {
        try { this._reader && this._reader.releaseLock(); } catch {}
        this._reader = null;
      }
    }

    // Ищем MAVLink-magic в входящем потоке. Несколько magic-байтов подряд по
    // времени — надёжный признак, что на том конце реально дрон, а не случайный
    // serial-/Bluetooth-девайс (который молчит или шлёт не-MAVLink).
    _sniffMavlink(chunk) {
      for (const b of chunk) {
        if (MAVLINK_MAGIC.has(b)) this._magicCount++;
      }
      if (this._magicCount >= RadioLink._MAV_RX_THRESHOLD) {
        this._sawMavlink = true;
        if (this._noDataTimer) { clearTimeout(this._noDataTimer); this._noDataTimer = null; }
        this._status("on-air");
      }
    }

    async _onWsBytes(arrbuf) {
      if (!this._active || !this._port) return;
      try {
        const bytes = new Uint8Array(arrbuf);
        if (!this._writer) this._writer = this._port.writable.getWriter();
        await this._writer.write(bytes);
      } catch (e) {
        if (this._active) this._fail(`serial write: ${e.message}`);
      }
    }

    _fail(msg) {
      this._status("error", msg);
      try { this._onError(new Error(msg)); } catch {}
      this.stop();
    }

    // ---- стоп --------------------------------------------------------
    async stop() {
      const wasActive = this._active;
      this._active = false;
      if (this._reconnectTimer) { clearTimeout(this._reconnectTimer); this._reconnectTimer = null; }
      if (this._noDataTimer) { clearTimeout(this._noDataTimer); this._noDataTimer = null; }
      this._sawMavlink = false;
      this._magicCount = 0;

      try { this._reader && (await this._reader.cancel()); } catch {}
      try { this._reader && this._reader.releaseLock(); } catch {}
      this._reader = null;
      try { this._writer && this._writer.releaseLock(); } catch {}
      this._writer = null;
      try { this._ws && this._ws.close(); } catch {}
      this._ws = null;
      try { this._port && (await this._port.close()); } catch {}

      // гасим серверный мост
      if (wasActive && this._name) {
        try { await fetch(`/api/real/disconnect/${encodeURIComponent(this._name)}`, { method: "POST" }); } catch {}
      }
      this._port = null;
      this._status("stopped");
    }

    // ---- авто-определение скорости ----------------------------------
    // Открывает выбранный порт на каждой скорости-кандидате и слушает ~1.6с:
    // если ловим несколько MAVLink-magic-байтов — скорость угадана.
    static async detectBaud(onProgress) {
      if (!RadioLink.isSupported()) throw new Error("Web Serial is not supported");
      const port = await navigator.serial.requestPort();
      const candidates = [57600, 115200, 921600, 38400, 230400];
      for (const baud of candidates) {
        if (onProgress) try { onProgress(baud); } catch {}
        try {
          await port.open({ baudRate: baud, bufferSize: 4096 });
        } catch { continue; }
        let magic = 0;
        const reader = port.readable.getReader();
        const deadline = Date.now() + 1600;
        try {
          while (Date.now() < deadline) {
            const to = new Promise((res) => setTimeout(() => res({ value: null, done: false }), 400));
            const { value, done } = await Promise.race([reader.read(), to]);
            if (done) break;
            if (value) for (const b of value) if (MAVLINK_MAGIC.has(b)) magic++;
            if (magic >= 3) break;
          }
        } catch {}
        try { await reader.cancel(); } catch {}
        try { reader.releaseLock(); } catch {}
        try { await port.close(); } catch {}
        if (magic >= 3) return { port, baud };
      }
      return null;
    }
  }

  window.RadioLink = RadioLink;
})();
