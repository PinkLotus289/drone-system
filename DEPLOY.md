# Skybite — деплой на сервер (для тестирования реальных дронов)

Гайд по запуску системы на удалённом сервере. Рекомендуемый путь — **bare-metal (systemd + Caddy)**;
Docker — как альтернатива (с оговорками по UDP, см. конец).

> **Критично:** подключение по радио использует браузерный **Web Serial**, который работает
> **только по HTTPS**. Без валидного TLS-домена радио в браузере не появится вообще. Поэтому
> reverse-proxy с сертификатом (Caddy ниже) — не опция, а требование.

---

## 0. Что понадобится
- Linux-сервер (Ubuntu 22.04+ / Debian 12, **amd64**), root/sudo.
- **Доменное имя** с A-записью на IP сервера (для TLS-сертификата).
- На стороне оператора: ноутбук с **Chrome / Edge / Brave / Opera** (Web Serial; не Safari/Firefox, не телефон).

---

## A. Bare-metal (рекомендуется)

### 1. Пакеты
```bash
sudo apt update
sudo apt install -y python3-venv python3-pip mosquitto git
# Caddy (авто-TLS):
sudo apt install -y debian-keyring debian-archive-keyring apt-transport-https curl
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo apt update && sudo apt install -y caddy
```

### 2. Код + пользователь
```bash
sudo useradd -r -m -d /opt/skybite skybite        # сервисный пользователь с HOME=/opt/skybite
sudo mkdir -p /opt/skybite && sudo chown skybite:skybite /opt/skybite
# залей проект в /opt/skybite (git clone / rsync). Нужны: src/, requirements.txt, infra/, scripts/
sudo -u skybite git clone <repo> /opt/skybite      # или rsync рабочей копии
```

### 3. venv + зависимости
```bash
cd /opt/skybite
sudo -u skybite python3 -m venv .venv
sudo -u skybite ./.venv/bin/pip install -U pip
sudo -u skybite ./.venv/bin/pip install -r requirements.txt
```

### 4. Секреты (.env)
```bash
sudo -u skybite cp .env.example .env
# сгенерировать SESSION_SECRET:
./.venv/bin/python -c "import secrets; print(secrets.token_urlsafe(48))"
sudo -u skybite nano /opt/skybite/.env   # вставь SESSION_SECRET, задай ACCESS_CODE, оставь COOKIE_SECURE=true
sudo chmod 600 /opt/skybite/.env
```

### 5. MQTT-брокер (только localhost)
```bash
sudo cp /opt/skybite/infra/mosquitto.prod.conf /etc/mosquitto/conf.d/skybite.conf
sudo systemctl enable --now mosquitto
```

### 6. Сервис приложения
```bash
sudo cp /opt/skybite/infra/skybite.service /etc/systemd/system/skybite.service
sudo systemctl daemon-reload
sudo systemctl enable --now skybite
journalctl -u skybite -f        # проверить, что поднялся без ошибок
```

### 7. Caddy (TLS + проксирование)
```bash
sudo cp /opt/skybite/infra/Caddyfile /etc/caddy/Caddyfile
sudo nano /etc/caddy/Caddyfile   # заменить skybite.example.com на свой домен
sudo systemctl restart caddy     # Caddy сам получит сертификат Let's Encrypt
```

### 8. Firewall
```bash
sudo ufw allow 22/tcp            # ssh
sudo ufw allow 80,443/tcp        # http(ACME) + https/wss
# НЕ открывать 1883 (MQTT) и 8000 (uvicorn) — они только на localhost.
# Только если используешь интернет-канал «дрон шлёт напрямую» (UDP/TCP):
#   sudo ufw allow <MAVLINK_PORT>/udp   # порт из профиля UDP/TCP
sudo ufw enable
```

### 9. Проверка
- Открой `https://твой-домен` в Chrome → должна открыться страница входа (замок TLS).
- Войди по `ACCESS_CODE`.
- На `/real` выбери **Radio** → должно быть «✓ This browser supports Web Serial».
- Подключи радио/дрон, нажми **Test connection** / **Connect by radio**.

---

## B. Docker (альтернатива)
`Dockerfile` (web-приложение) готов. Подними рядом mosquitto и Caddy.
**Оговорки:**
- **Интернет-канал (UDP/TCP «дрон шлёт на сервер»)** требует публикации MAVLink-портов из контейнера
  (`ports: "<port>:<port>/udp"`) или `network_mode: host`. На bare-metal этого нет — потому он и рекомендован.
- В контейнере `MQTT_URL=mqtt://mosquitto:1883` (имя сервиса), а сам брокер наружу не публиковать.
- Профили монтируй в volume (`/app/.drone_system`).

---

## Безопасность (важно для реального дрона)
- **Секреты обязательны:** без смены `SESSION_SECRET`/`ACCESS_CODE` любой сможет зайти и командовать бортом.
- **Радио-канал зашифрован** (wss + код доступа) — это безопасный путь по умолчанию.
- **Интернет-канал напрямую (UDP/TCP) НЕ шифрован** — открытый MAVLink-порт в интернете уязвим к инъекции
  команд. Для него используй **VPN (WireGuard)** между дроном и сервером либо ограничь источник по IP.
- Наружу открыты только 443 (и 80 для ACME). MQTT/uvicorn — localhost.

## Чего в этой поставке НЕТ (осознанно)
- **PX4/SITL на сервере** — не нужен для тестов реальных дронов. Если захочешь гонять симуляцию на сервере,
  PX4 надо собрать там отдельно (кнопка «Start simulation» иначе выдаст ошибку).
- **Postgres** — не используется (`REPO_IMPL=mem`), в compose можно не поднимать.
- **Слой безопасности полёта** (geofence, pre-arm гейты, RTL при потере линка) — для тестов *подключения*
  не требуется, но обязателен перед реальными автономными полётами.
