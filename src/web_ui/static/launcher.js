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
      startBtn.textContent = state === "starting" ? "запуск..." : "остановка...";
      openBtn.classList.add("hidden");
      stopBtn.classList.add("hidden");
    } else {
      startBtn.classList.remove("hidden");
      startBtn.disabled = false;
      startBtn.textContent = "▶️ Запустить";
      openBtn.classList.add("hidden");
      stopBtn.classList.add("hidden");
    }
    if (state === "error") setMsg(errorText || "ошибка sim", "err");
  }

  async function refreshStatus() {
    try {
      const r = await fetch("/api/launcher/sim/status");
      const j = await r.json();
      setSimStatus(j.state, j.error);
    } catch (e) {
      setMsg("не могу получить статус: " + e.message, "err");
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
    const bootEst = Math.ceil(15 + n * 4);  // грубая оценка boot-time
    numHint.textContent = `~${ramMb}MB · boot ~${bootEst}с`;
  }
  numMinus.addEventListener("click", () => { numInput.value = clampN() - 1; refreshNumUi(); });
  numPlus.addEventListener("click",  () => { numInput.value = clampN() + 1; refreshNumUi(); });
  numInput.addEventListener("input", refreshNumUi);
  refreshNumUi();

  $("btn-sim-start").addEventListener("click", async () => {
    const n = clampN();
    setMsg(`Поднимаю ${n} PX4-инстанс${n === 1 ? "" : n < 5 ? "а" : "ов"} и компоненты — займёт ~${15 + n * 4}с (parallel boot + ready-check)`, "warn");
    setSimStatus("starting");
    try {
      const r = await fetch("/api/launcher/sim/start", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ num_drones: n }),
      });
      const j = await r.json();
      if (j.ok) {
        setMsg(`Симуляция запущена (${j.num_drones || n} дронов). Открываю UI...`, "ok");
        setSimStatus("running");
        setTimeout(() => { window.location.href = "/sim"; }, 800);
      } else {
        setMsg("Ошибка запуска: " + (j.error || "unknown"), "err");
        setSimStatus(j.state || "error", j.error);
      }
    } catch (e) {
      setMsg("Сеть/сервер: " + e.message, "err");
      setSimStatus("error", e.message);
    }
  });

  $("btn-sim-open").addEventListener("click", () => {
    window.location.href = "/sim";
  });

  $("btn-sim-stop").addEventListener("click", async () => {
    setMsg("Останавливаю sim...", "warn");
    setSimStatus("stopping");
    try {
      const r = await fetch("/api/launcher/sim/stop", { method: "POST" });
      const j = await r.json();
      if (j.ok) {
        setMsg("Симуляция остановлена.", "ok");
        setSimStatus("stopped");
      } else {
        setMsg("Ошибка остановки", "err");
      }
    } catch (e) {
      setMsg("Сеть/сервер: " + e.message, "err");
    }
  });

  $("btn-real-open").addEventListener("click", () => {
    window.location.href = "/real";
  });

  // Часы в шапке.
  function tickClock() {
    const d = new Date();
    const hh = String(d.getUTCHours()).padStart(2, "0");
    const mm = String(d.getUTCMinutes()).padStart(2, "0");
    const ss = String(d.getUTCSeconds()).padStart(2, "0");
    const el = document.getElementById("clock-time");
    if (el) el.textContent = `${hh}:${mm}:${ss}`;
  }
  setInterval(tickClock, 1000); tickClock();

  // Пуллинг статуса каждые 3с пока кто-то на странице.
  refreshStatus();
  setInterval(refreshStatus, 3000);
})();