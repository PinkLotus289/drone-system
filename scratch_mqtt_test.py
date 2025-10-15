# save as scratch_mqtt_test.py и запусти: python scratch_mqtt_test.py
import time
from src.drone_core.utils.logging import setup
from src.drone_core.infra.messaging.mqtt_bus import MqttBus
from src.drone_core.infra.messaging import topics as T
import sys, os
sys.path.append(os.path.join(os.path.dirname(__file__), "src"))

setup("DEBUG")

bus = MqttBus("mqtt://localhost:1883")
bus.start()

def on_pose(msg):
    print("<< telem pose:", msg.topic, msg.payload)

bus.subscribe(T.TELEM_ALL, on_pose)

# опубликуем тестовые сообщения
for i in range(3):
    bus.publish(T.telem_pose("veh_001"), {"lat": 52.0+i*0.001, "lon": 21.0, "alt": 30+i})
    time.sleep(0.5)

time.sleep(1)
bus.stop()
