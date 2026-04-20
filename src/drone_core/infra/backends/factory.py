from drone_core.domain.drone_backend import DroneBackend


def create_backend(mode: str) -> DroneBackend:
    """Фабрика бэкендов по режиму системы."""
    if mode == "test":
        from drone_core.infra.backends.simulator_backend import SimulatorBackend
        return SimulatorBackend()
    elif mode == "preflight":
        raise NotImplementedError("Preflight backend ещё не реализован")
    elif mode == "full":
        raise NotImplementedError("Full backend ещё не реализован")
    else:
        raise ValueError(f"Неизвестный режим: {mode}")