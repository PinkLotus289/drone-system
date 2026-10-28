# drone-system.net — техническая документация системы управления полётами дронов

Документ описывает архитектуру, компоненты и внутренние протоколы системы на текущем
этапе. Предназначен для инженеров, сопровождающих и развивающих проект.

> **Статус проекта.** Система находится в активной разработке. Описанные решения
> отражают текущую реализацию и могут изменяться по мере развития проекта; ряд
> компонентов будет дорабатываться и расширяться.
>
> **Контекст.** drone-system.net — отдельный сервис дронной доставки, разрабатываемый
> для **SkyBite** (доставка еды). В тексте под «системой» понимается именно
> drone-system.net. Исторически часть системных идентификаторов (сервис `skybite`,
> каталог `/opt/skybite`, cookie `skybite_session`) сохраняет прежнее имя — они
> приведены как есть, поскольку являются реальными именами в коде и инфраструктуре.

---

## 1. Обзор

drone-system.net — платформа управления флотом БПЛА для дронной доставки. Оператор
работает через веб-интерфейс (3D-карта, телеметрия, постановка заданий). На текущем
этапе система поддерживает два режима источника бортов:

- **Симуляция** — виртуальные дроны на PX4 SITL (физическая модель SIH), запускаются на
  сервере;
- **Реальные дроны** — подключение по радиоканалу (через браузер оператора) или напрямую
  по интернету (UDP/TCP).

Связь между компонентами построена на шине **MQTT**. Автопилот — **PX4**, связь с
автопилотом — через **MAVSDK**.

### Технологический стек

| Слой | Технология |
|---|---|
| Веб-бэкенд | Python 3.12, FastAPI, Uvicorn (ASGI) |
| Реальное время (UI) | WebSocket |
| Шина сообщений | MQTT (Mosquitto), клиент paho-mqtt |
| Автопилот (симуляция) | PX4 SITL, модель SIH `sihsim_quadx` |
| Связь с автопилотом | MAVSDK (gRPC + MAVLink) |
| Фронтенд | HTML/JS, CesiumJS (3D-карта) |
| Хранилище | In-memory (по умолчанию) либо PostgreSQL (SQLModel) |
| Reverse-proxy / TLS | Caddy (в продакшене) |

---

## 2. Топология процессов

Система — это набор взаимодействующих процессов, общающихся через MQTT (localhost:1883):

```
┌──────────────────────────── Веб-приложение (uvicorn) ────────────────────────────┐
│ FastAPI: web_ui.main:app                                                          │
│  • HTTP API + статика + WebSocket /ws                                             │
│  • MqttBus (ui-bus): слушает fleet/active, telem/+/+, mission/+/*                 │
│  • Супервизоры: SimSupervisor, RealSupervisor, RadioRelayManager                 │
│  • ControlAuthority (арбитраж управления)                                         │
└───────────────────────────────────────────────────────────────────────────────────┘
        │ порождает (subprocess)                         ▲ MQTT
        ▼                                                │
┌───────────────────────────┐   ┌──────────────────────┴───────────────────────────┐
│ PX4 SITL (instance 0..N)  │   │ Воркеры (порождаются при старте симуляции):       │
│ модель SIH sihsim_quadx   │   │  • telemetry_ingest — приём телеметрии в репозиторий│
└───────────┬───────────────┘   │  • orchestrator     — обработка заказов → миссии   │
            │ MAVLink UDP        └────────────────────────────────────────────────────┘
            ▼
┌───────────────────────────┐
│ mavsdk_bridge (на дрон)   │  MAVSDK ↔ PX4, перевод команд MQTT ↔ MAVLink,
│ System(gRPC :50151+id)    │  публикация телеметрии в MQTT
└───────────────────────────┘
            │
        Mosquitto (MQTT, 127.0.0.1:1883)
```

Каждый дрон обслуживается **отдельным процессом** `mavsdk_bridge` со своим экземпляром
`mavsdk_server` (отдельный gRPC-порт) — это изолирует борта друг от друга.

---

## 3. Конфигурация (переменные окружения)

Настройки читаются через `pydantic-settings` (`src/drone_core/config/settings.py`).
Реальные переменные окружения имеют приоритет; в проде подаются через
`EnvironmentFile=/opt/skybite/.env`.

| Переменная | По умолчанию | Назначение |
|---|---|---|
| `ENV` | `dev` | Окружение (`dev`/`prod`) |
| `SYSTEM_MODE` | `test` | `test` (PX4 SITL) / `preflight` / `full` |
| `MQTT_URL` | `mqtt://127.0.0.1:1883` | Адрес MQTT-брокера |
| `REPO_IMPL` | `mem` | Репозитории: `mem` (in-memory) или `pg` (PostgreSQL) |
| `DB_URL` | `postgresql+asyncpg://…` | Строка подключения к БД (для `pg`) |
| `ACCESS_CODE` | `skybite` | Код входа оператора |
| `SESSION_SECRET` | `dev-secret-…` | Ключ HMAC-подписи сессионной cookie |
| `SESSION_TTL_HOURS` | `12` | Время жизни сессии |
| `COOKIE_SECURE` | `False` | Флаг `Secure` для cookie (`True` за HTTPS) |
| `CONTROL_TTL_SEC` | `25.0` | TTL аренды управления (арбитраж) |
| `SLA_MAX_TOTAL_MIN` / `SLA_WAIT_*` | 20 / 60 / 60 | SLA-таймауты (мин/сек) |

---

## 4. Веб-слой (`src/web_ui/`)

### 4.1 Точка входа — `main.py`

- Создаётся `FastAPI(title="Drone System Dashboard")`, монтируется `/static`,
  подключаются роутеры `launcher_router` и `real_router`.
- Синглтоны: `bus = MqttBus(...)` (client_id `ui-bus`), `control = ControlAuthority(ttl=CONTROL_TTL_SEC)`.
- **HTTP-middleware `_auth_guard`** проверяет сессионную cookie на каждом запросе.
  Публичные пути: `/login`, `/api/login`, `/static/*`, `/favicon`. Для API без
  авторизации — `401`, для страниц — `302` на `/login`.
- На старте подписывается на MQTT и поддерживает состояние в `app.state`:
  `active_drones`, `active_missions` (терминальные чистятся через 20с),
  `session_missions` (полный журнал сессии), `drone_trails` (буфер траекторий, до 600 точек).

### 4.2 Аутентификация — `auth.py`

- Cookie `skybite_session` = `base64(payload).HMAC-SHA256`, где
  `payload = {op: позывной, sid: id_сессии, iat, exp}`.
- `make_session_token(operator, secret, ttl_hours)` — выпуск; `verify_token` —
  проверка подписи и срока; `operator_from_cookies` — извлечение из cookie.
- `new_sid()` = `secrets.token_hex(8)`. Логаут добавляет `sid` в `revoked_sids`
  (инвалидация до истечения TTL).

### 4.3 Арбитраж управления — `control.py`

`ControlAuthority` гарантирует, что бортами командует только один оператор
(в рамках scope, обычно `"system"`):

- `acquire(scope, operator, sid)` — захват свободного scope;
- `heartbeat(scope, sid)` — продление аренды на `+ttl`;
- `release` / `takeover` — отпустить / принудительно перехватить;
- `is_holder(scope, sid)` — проверка прав (используется как гейт на командах).

Аренда живёт `CONTROL_TTL_SEC` секунд и требует периодического heartbeat.

### 4.4 Ключевые HTTP-эндпоинты

**Аутентификация и сессия**
- `GET /login` — страница входа;
- `POST /api/login` — проверка `ACCESS_CODE`, установка cookie;
- `POST /api/logout` — инвалидация сессии + release управления;
- `GET /api/me` — текущий оператор.

**Арбитраж управления**
- `GET /api/control`, `POST /api/control/acquire|heartbeat|release|takeover`.

**Команды и заказы** (требуют control authority, scope `system`)
- `POST /api/drone/{veh_id}/command` — ручные команды
  (`arm`, `disarm`, `takeoff`, `land`, `rtl`, `goto`, `hold`); публикует в `cmd/{veh_id}/...`;
- `POST /api/orders` — создание заказа, публикация в `orders/new`.

**Чтение состояния**
- `GET /api/drones`, `/api/fleet`, `/api/free_drones` — флот;
- `GET /api/missions`, `/api/active_missions` — миссии;
- `GET /api/session/snapshot` — восстановление состояния UI после перезагрузки страницы;
- `GET /api/base`, `/api/mission_context`, `/api/settings`, `/api/system/mode`.

**Страницы**
- `GET /` (лобби), `/sim`, `/sim/drone/{id}`, `/real`, `/testing`, `/local_launch`.

### 4.5 WebSocket `/ws`

Авторизуется по той же cookie. После подключения клиент регистрируется в списке
получателей; все MQTT-сообщения (телеметрия, статусы миссий) ретранслируются
подключённым клиентам через `asyncio`.

### 4.6 Управление симуляцией — `launcher.py`

- `GET /api/launcher/health` — liveness + uptime;
- `GET /api/launcher/sim/status` — состояние симуляции;
- `POST /api/launcher/sim/start` — старт N дронов (1..10), предварительно сбрасывает
  состояние UI;
- `POST /api/launcher/sim/stop` — остановка + очистка.

### 4.7 Реальные дроны — `real.py`

- CRUD профилей: `GET/POST /api/real/profiles`, `GET/DELETE /api/real/profiles/{name}`;
- `GET /api/real/serial-ports` — авто-обнаружение USB-портов (macOS `/dev/cu.usbserial*`
  и др., Linux `/dev/ttyUSB*`, `/dev/ttyACM*`);
- `POST /api/real/test-connection` — пробное MAVSDK-подключение (heartbeat, версия
  прошивки, health-флаги, GPS, батарея, режим);
- `POST /api/real/connect/{name}` / `POST /api/real/disconnect/{name}` — поднять/снять мост;
- `WS /api/real/radio/agent?name=...` — канал браузерного радио-агента (см. §8).

Построение адреса MAVSDK по типу профиля: `serial://port:baud`, `udp://host:port`,
`tcp://host:port`. Для `radio` адрес выдаёт реле (`tcp://127.0.0.1:<port>`).

### 4.8 Фронтенд (`static/`, `tabs/`)

- `app.js` — основной UI симулятора: CesiumJS 3D-карта, глобальное состояние
  (дроны, миссии, траектории, арбитраж), WebSocket-клиент `/ws`;
- `radio_link.js` — класс работы с Web Serial API (браузерный радио-агент);
- `launcher.js` — управление стартом/остановом симуляции;
- `real_connect.js` — мастер профилей и подключения реальных бортов;
- страницы: `login.html`, `launcher.html`, `index.html`, `real_connect.html`,
  `drone_detail.html`, `local_launch.html`, `testing.html`.

> WebSocket-адреса формируются по протоколу страницы (`wss://` на HTTPS) — и для
> телеметрии (`app.js`), и для радио (`radio_link.js`).

---

## 5. Шина сообщений (MQTT)

`MqttBus` (`infra/messaging/mqtt_bus.py`) — обёртка над paho-mqtt с поддержкой sync- и
async-обработчиков (async исполняются в выделенном event-loop через
`run_coroutine_threadsafe`). QoS по умолчанию — **1** (гарантированная доставка),
`retain=False`. Сообщение приходит как `Message(topic, payload, qos, retain, ts)`.

### Карта топиков

**Команды борту** (`cmd/{veh_id}/...`)
- `cmd/{veh_id}/arm`, `.../takeoff`, `.../land`, `.../rtl`, `.../goto`
- `cmd/{veh_id}/mission.upload`, `.../mission.start`
- `cmd/{veh_id}/winch` (лебёдка)

**Телеметрия** (`telem/{veh_id}/...`) — публикует `mavsdk_bridge`
- `pose` (lat/lon/alt), `attitude` (pitch/roll/yaw), `velocity` (gs/vz),
  `battery` (voltage/percent), `gps` (sats/fix), `actuators` (PWM моторов)

**Миссии** (`mission/{mission_id}/...`)
- `planned`, `assigned`, `status` (UPLOADED/COMPLETED/ABORTED/…), `progress` (current/total)

**Состояние флота и заказы**
- `fleet/active` — периодический снапшот борта `{id, name, status, lat, lon, alt, soc, ts}`
- `orders/new` — новый заказ от UI к оркестратору

---

## 6. Доменная модель (`drone_core/domain/models.py`)

**Перечисления**
- `VehicleStatus`: `IDLE`, `BUSY`, `FLYING`, `OFFLINE`, `ERROR`
- `MissionStatus`: `CREATED`, `PLANNED`, `ASSIGNED`, `UPLOADED`, `IN_PROGRESS`,
  `COMPLETED`, `ABORTED`
- `Waypoint.kind`: `TAKEOFF`, `NAV`, `LAND`, `RTL`, `ORBIT`

**Сущности**
- `LLA{lat, lon, alt}` — точка в пространстве.
- `Vehicle{id, name, status, pos, soc, mode, last_ts}` — состояние борта.
- `Waypoint{pos, kind, hold_s, speed_m_s, acceptance_radius_m, orbit_radius_m}` —
  точка маршрута (в т.ч. радиус орбиты для ISR).
- `Mission{id="mis_…", pickup, dropoff, payload_kg, priority, vehicle_id, status,
  mission_type, takeoff_profile, waypoints[], created_at}`.
- `Order{id="ord_…", base, addr1, addr2, payload_kg, priority}` — заказ уровня UI.

---

## 7. Ядро: воркеры (`drone_core/workers/`)

### 7.1 Orchestrator — обработка заказов

Подписан на `orders/new`, `mission/+/status`, `fleet/active`. Конвейер обработки заказа:

1. **Планирование** — кастомные waypoints из заказа либо `plan_order()`; сохранение
   `Mission`, публикация `mission/{id}/planned`.
2. **Назначение борта** — `_select_vehicle()` выбирает свободный дрон (статус `IDLE`,
   SoC > 40%, сортировка по заряду), резервирует его; статус → `ASSIGNED`.
3. **Загрузка маршрута** — публикует `cmd/{veh}/mission.upload`, ждёт подтверждения
   через `mission/{id}/status` (таймаут 30с); статус → `UPLOADED`.
4. **Старт** — `cmd/{veh}/arm` + `cmd/{veh}/mission.start`; статус → `IN_PROGRESS`.

Освобождает борт при `COMPLETED`/`ABORTED`/`*_FAILED`.

### 7.2 Planner — построение маршрута

`plan_order(order)` строит маршрут `база → addr1 → addr2 → база`:
`TAKEOFF → NAV → NAV → NAV(approach 10м) → LAND`. Дистанции — по формуле гаверсинуса,
ETA = расстояние / cruise + 60с. На промежуточных точках `hold_s=0` (пролёт без
зависания — чтобы PX4 не сработал landing-detector).

### 7.3 Telemetry Ingest — приём телеметрии

Подписан на `telem/+/+` и `fleet/active`. Кеширует последнюю телеметрию
(`LAST_TELEM[veh][type]`) и поддерживает репозиторий флота (`add`/`update` Vehicle).

---

## 8. Симуляция (`src/simulator/`)

### 8.1 Запуск PX4 — `px4_launcher.py`

- Сборка проверяется/выполняется: `make px4_sitl CMAKE_CXX_STANDARD=17`;
  бинарь — `build/px4_sitl_default/bin/px4`.
- Каждый инстанс: `bin/px4 -i N -d rootfs_N -s etc/init.d-posix/rcS`,
  `cwd = build_dir`. Перед запуском `rootfs_N` очищается (иначе остаётся миссия из
  прошлого запуска).
- Окружение: `PX4_SIM_MODEL=sihsim_quadx`, `PX4_HOME_LAT/LON` (на дрон).
- Готовность определяется по строке `Ready for takeoff` в stdout (таймаут 40с).

**Модель SIH (Simulation-In-Hardware)** — это встроенный в PX4 физический симулятор
(не Gazebo): лёгкий, не требует GPU. Параметры рамы/физики донастроены в **форке
PX4-Autopilot проекта** (отдельные коммиты поверх upstream); поведение приближено к
реальному квадрокоптеру X-конфигурации.

### 8.2 Карта портов (на инстанс `i`)

| Канал | Порт | Назначение |
|---|---|---|
| GCS link | `18570+i` | Ground Control (broadcast) |
| Offboard local | `14580+i` | — |
| Offboard remote / `mavlink_out` | `14540+i` | MAVSDK слушает (`udp://:14540+i`) |
| gRPC `mavsdk_server` (симуляция) | `50151+i` | На дрон |
| gRPC (тест-подключение) | `50191+` | Пул для `test-connection` |
| gRPC (реальные борта) | `50230+` | Пул для real-мостов |
| TCP радио-реле | `51000+` | Пул реле (см. §9) |

### 8.3 Мост — `mavsdk_bridge.py`

Центральный компонент. На каждый дрон: `System(port=50151+id)` подключается к
`udp://:{mavlink_out}`. Работает в двух режимах: SITL (по `config.yaml` + `DRONE_ID`)
и реальный борт (`REAL_BRIDGE=1` + `REAL_CONN_URL`/`REAL_GRPC_PORT`).

**Настройка параметров PX4 при подключении** (важно для устойчивости SITL):
- отключение RC и failsafe: `COM_RC_IN_MODE=4`, `NAV_RCL_ACT=0`, `NAV_DLL_ACT=0`,
  `COM_RCL_EXCEPT=7`;
- circuit-breakers (USB/питание/safety switch): `CBRK_USB_CHK`, `CBRK_SUPPLY_CHK`,
  `CBRK_IO_SAFETY`;
- симуляционные сенсоры: `SENS_EN_BAROSIM=1`, `SENS_EN_MAGSIM=1`, `SENS_EN_GPSSIM=1`;
- поведение арминга/посадки: `COM_DISARM_PRFLT=-1`, `COM_DISARM_LAND=-1` (SIH даёт
  ложные «landed»), `MIS_TAKEOFF_ALT=5.0`, `MPC_TKO_SPEED=1.5`.

**Команды** (подписка `cmd/{name}/#`, где `name=veh_{id}`): `arm`, `takeoff`, `land`,
`rtl`, `goto(lat,lon,alt)`, `mission.upload`, `mission.start`.

**Взлёт** — детерминированный: `set_takeoff_altitude` → `takeoff()` → ожидание отрыва →
набор высоты по профилю (`vertical` — вертикально над точкой; `inclined` — по диагонали
к первой точке маршрута).

**Телеметрия** — публикуется в MQTT с разной частотой: `pose` 4 Гц, `attitude`/`velocity`
2.5 Гц, `actuators` 2 Гц, `battery`/`gps` 1 Гц, `fleet/active` 1 Гц. Статус борта в
`fleet/active`: `FLYING` если в воздухе и выше 1 м, иначе `IDLE`.

**Завершение миссии** — определяется по «взлетал и сел»: форсируется 100% прогресс,
disarm (т.к. `COM_DISARM_LAND=-1` держит моторы), публикуется `mission/{id}/status =
COMPLETED`, борт возвращается в idle.

### 8.4 Конфигурация дронов — `config.yaml`

Содержит блок `mqtt`, блок `simulator` (`num_drones`, home по умолчанию Madison, WI:
43.0747, −89.3842) и список из 10 заготовленных дронов (сетка ~4×3 с разнесёнными
home-позициями). Поля дрона: `id`, `name`, `px4_path`, `mavlink_out`, `instance`,
`home_lat/lon`. Лаунчер берёт первые N дронов.

### 8.5 Жизненный цикл (SimSupervisor)

Старт: читает `config.yaml` → `start_px4_instances()` → по одному `mavsdk_bridge` на
дрон (env `DRONE_ID`) → воркеры `telemetry_ingest` и `orchestrator`. Все дочерние
процессы стартуют в своей process-group (`start_new_session=True`).

Стоп: двухфазно — `SIGTERM` отслеживаемым детям + grace ~2.5с → `pkill` по паттернам
(`bin/px4`, `mavsdk_server`, `simulator.mavsdk_bridge`, воркеры). Это исключает
процессы-сироты.

### 8.6 Семантика режимов полёта

- **Delivery / Sector** — обычная PX4-миссия (загрузка waypoints + `start_mission`).
- **ISR** — нативный `do_orbit` над точкой (радиус/скорость из waypoint).
- **Patrol** — `goto_location` + зависание (`hold_s`) на контрольных точках.

ISR и Patrol исполняются нативным executor'ом моста (`NATIVE_MISSION_TYPES = {"isr",
"patrol"}`), а не как обычная PX4-миссия.

---

## 9. Подключение реальных дронов

### 9.1 Радиоканал (через браузер оператора)

Наземное USB-радио (SiK) физически у оператора, сервер удалённый. Поэтому байты MAVLink
идут через браузер:

```
[дрон] ~RF~ [USB SiK] —Web Serial→ [браузер] —WSS→ [radio_relay] —localhost TCP→ [mavsdk_bridge] —MQTT→ UI
```

- `radio_relay.py`: на каждое подключение поднимается локальный TCP-сервер
  (`127.0.0.1:51000+`), к которому цепляется обычный `mavsdk_bridge` по
  `tcp://127.0.0.1:<port>`. Реле перекачивает байты в обе стороны.
- Браузерный агент (`radio_link.js`, Web Serial API) подключается по
  `WS /api/real/radio/agent?name=...` и стримит сырые MAVLink-байты.
- `RadioRelayManager` управляет реле (создание, привязка агента, teardown), ведёт
  статистику (`bytes_to_drone`, `bytes_from_drone`, `last_rx`).

> Требование: страница по HTTPS (Web Serial работает только в secure context).

### 9.2 Интернет-канал (дрон → сервер напрямую)

Профиль типа `udp`/`tcp`: `RealSupervisor` поднимает `mavsdk_bridge` с
`REAL_BRIDGE=1`, `REAL_CONN_URL=udp://:port` (или `tcp://host:port`), выделяя gRPC-порт
из пула 50230+. Дрон (или компаньон-компьютер) шлёт MAVLink на публичный адрес сервера.

> Канал не шифрован — для реальной эксплуатации рекомендуется WireGuard или ограничение
> по IP источника, плюс открытие соответствующего UDP/TCP-порта в firewall.

---

## 10. Хранилище данных

Выбор реализации — переменная `REPO_IMPL`:
- `mem` (по умолчанию) — `FleetMem` / `MissionsMem`, всё в памяти процесса;
- `pg` — `FleetPg` / `MissionsPg` на PostgreSQL (SQLModel; таблицы `VehicleRow`,
  `MissionRow`, `WaypointRow`).

Профили реальных дронов хранятся отдельно — JSON-файлы в `~/.drone_system/profiles/`
(`ProfileStore`, атомарная запись через tmp+rename).

---

## 11. Развёртывание

На текущем этапе система развёрнута на выделенном сервере (Ubuntu 24.04) в рабочей
тестовой конфигурации. Краткая топология:

```
Интернет → Caddy (443/80, TLS Let's Encrypt) → uvicorn (127.0.0.1:8000)
                                                  ├─ Mosquitto (127.0.0.1:1883)
                                                  └─ PX4 SITL + mavsdk_bridge (по запросу)
```

- systemd-сервисы: `skybite` (веб-приложение), `caddy` (reverse-proxy + TLS),
  `mosquitto` (MQTT) — все с автозапуском.
- Наружу открыты только `22/80/443` (ufw); `8000` и `1883` доступны лишь на localhost.
- `COOKIE_SECURE=true`, секреты (`ACCESS_CODE`, `SESSION_SECRET`) — в
  `/opt/skybite/.env`.
- Подробности процесса развёртывания — в `DEPLOY.md`.

---

## 12. Замечания и зоны внимания

- **Семантика waypoints формируется на стороне UI** — оркестратор транслирует кастомные
  waypoints без валидации семантики режимов.
- **Жёсткие константы в `SimulatorBackend`** (`speed_m_s=5.0`, `acceptance_radius_m=5.0`)
  не берутся из waypoint при PX4-миссии (нативный executor моста — учитывает).
- **`FleetPg.list_free()`** фильтрует по статусу `"FREE"`, которого нет в `VehicleStatus`
  (актуально только при `REPO_IMPL=pg`; по умолчанию используется `mem`).
- **Контур безопасности полёта** (геозоны, pre-arm гейты, RTL при потере линка) для
  тестов подключения не требуется, но обязателен перед реальными автономными полётами.
- Режимы `preflight` и `full` (`SYSTEM_MODE`) — заглушки (`NotImplementedError`).

Перечисленное отражает текущее состояние; по мере развития проекта эти места
планируется дорабатывать.

---

## 13. Направления дальнейшего развития

Проект находится в ранней стадии, и приведённый ниже список — ориентир, а не
обязательства. Среди возможных направлений:

- контур безопасности полёта (геозоны, pre-arm гейты, RTL при потере связи);
- реализация режимов `preflight` и `full` (работа с реальным флотом помимо симуляции);
- зрелое хранилище (PostgreSQL) и история миссий вместо in-memory по умолчанию;
- защищённый интернет-канал к бортам (VPN/WireGuard, ограничение по источнику);
- интеграция с бизнес-логикой заказов SkyBite (доставка еды) и сценарии эксплуатации;
- расширение телеметрии, отчётности и пользовательского интерфейса оператора.

---

*Документ отражает состояние системы на текущем этапе разработки и будет обновляться по
мере развития проекта. Для бизнес-описания см. `DEPLOYMENT_REPORT.md`, для инструкции по
развёртыванию — `DEPLOY.md`.*
