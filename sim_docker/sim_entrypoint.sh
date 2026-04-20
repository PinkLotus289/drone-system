#!/usr/bin/env bash
set -e

VEHICLE=${VEHICLE:-iris}
WORLD=${WORLD:-empty}
HEADLESS=${HEADLESS:-1}

cd /root/PX4-Autopilot

echo "🚀 Launching PX4 SITL (${VEHICLE}) with Gazebo (headless=${HEADLESS})"

export PX4_SIM_MODEL=${VEHICLE}
export HEADLESS=1

# Запуск PX4 в фоне
make px4_sitl gazebo_${VEHICLE} &

# Ожидаем и подключаем MAVLink наружу
sleep 8
echo "⚙️ Enabling MAVLink broadcast on 0.0.0.0:14550 ..."
python3 Tools/mavlink_shell.py udp:127.0.0.1:14580 <<'EOF'
mavlink stop-all
mavlink start -u 14550 -r 4000000 -p -f -t 0.0.0.0
EOF

wait
