"""
topics.py — централизованное определение всех MQTT-топиков.
"""

# ==== Команды борту ====
def cmd(veh_id: str, action: str) -> str:
    """Топик для отправки команд борту."""
    # action: mission.upload | arm | takeoff | goto | rtl | land | winch
    return f"cmd/{veh_id}/{action}"


# ==== Телеметрия ====
def telem_pose(veh_id: str) -> str:
    return f"telem/{veh_id}/pose"

def telem_battery(veh_id: str) -> str:
    return f"telem/{veh_id}/battery"

def telem_health(veh_id: str) -> str:
    return f"telem/{veh_id}/health"


# ==== События миссий ====
def mission_events(mission_id: str) -> str:
    return f"mission/{mission_id}/events"


# ==== Состояние полезной нагрузки (лебёдка и т.п.) ====
def payload_winch_state(veh_id: str) -> str:
    return f"payload/{veh_id}/winch/state"


# ==== Шаблоны подписки (wildcards) ====
TELEM_ALL = "telem/+/+"
CMD_ALL = "cmd/+/+"
MISSION_EVENTS_ALL = "mission/+/events"
PAYLOAD_ALL = "payload/+/+/#"


# ==== Объект для удобства доступа ====
class TelemetryTopics:
    """Объединяет функции для работы с телеметрией."""
    ALL = TELEM_ALL
    pose = staticmethod(telem_pose)
    battery = staticmethod(telem_battery)
    health = staticmethod(telem_health)

