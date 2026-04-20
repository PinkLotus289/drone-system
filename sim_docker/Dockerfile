# syntax=docker/dockerfile:1
FROM --platform=linux/amd64 px4io/px4-dev-simulation-focal:2024-05-18

WORKDIR /root

# Клонируем PX4
RUN git clone https://github.com/PX4/PX4-Autopilot.git --depth 1 --branch main
WORKDIR /root/PX4-Autopilot

# Собираем PX4 для SITL
RUN DONT_RUN=1 make px4_sitl_default

# Устанавливаем pymavlink (для доступа к mavlink_shell)
RUN apt-get update && apt-get install -y net-tools python3-pip && \
    pip3 install --no-cache-dir pymavlink

# Копируем entrypoint
COPY sim_entrypoint.sh /root/sim_entrypoint.sh
RUN chmod +x /root/sim_entrypoint.sh

EXPOSE 14540-14580/udp

ENTRYPOINT ["/root/sim_entrypoint.sh"]
