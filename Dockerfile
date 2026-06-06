# Контейнер web-приложения Skybite (только UI + реле + мосты; PX4 SITL НЕ входит).
# Образ amd64 — mavsdk тянет бинарь mavsdk_server под платформу; на ARM проверяй наличие колёс.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/src \
    HOME=/app

WORKDIR /app

# Системные зависимости mavsdk_server (gRPC-бинарь) — минимум.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates libatomic1 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src ./src

# Профили бортов: ~/.drone_system → вынеси в volume, чтобы переживали рестарт.
VOLUME ["/app/.drone_system"]

EXPOSE 8000
# За reverse-proxy (Caddy) внутри сети контейнеров; TLS терминирует прокси.
CMD ["uvicorn", "web_ui.main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--forwarded-allow-ips=*"]
