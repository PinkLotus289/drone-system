from __future__ import annotations
import json
import os
import tempfile
from pathlib import Path
from typing import List, Optional

from .models import DroneProfile

# ~/.drone_system/profiles/<name>.json — отдельно от репо, как ssh-keys.
DEFAULT_DIR = Path.home() / ".drone_system" / "profiles"


class ProfileStore:
    def __init__(self, base_dir: Path = DEFAULT_DIR) -> None:
        self.base_dir = base_dir
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def _path(self, name: str) -> Path:
        # name уже валидируется DroneProfile.name; повторная защита от .. на всякий.
        if "/" in name or ".." in name:
            raise ValueError(f"Недопустимое имя профиля: {name!r}")
        return self.base_dir / f"{name}.json"

    def list(self) -> List[DroneProfile]:
        out: List[DroneProfile] = []
        for p in sorted(self.base_dir.glob("*.json")):
            try:
                out.append(DroneProfile.model_validate_json(p.read_text("utf-8")))
            except Exception as e:
                # Битый файл не должен ронять весь list — логируем и пропускаем.
                print(f"[profiles] ⚠️ skip {p.name}: {e!r}")
        return out

    def get(self, name: str) -> Optional[DroneProfile]:
        path = self._path(name)
        if not path.exists():
            return None
        return DroneProfile.model_validate_json(path.read_text("utf-8"))

    def save(self, profile: DroneProfile) -> DroneProfile:
        path = self._path(profile.name)
        # Atomic write: tmp + rename, чтобы при креше не получить полу-файл.
        with tempfile.NamedTemporaryFile(
            mode="w",
            dir=str(self.base_dir),
            prefix=f".{profile.name}.",
            suffix=".tmp",
            delete=False,
            encoding="utf-8",
        ) as f:
            f.write(profile.model_dump_json(indent=2))
            tmp_path = Path(f.name)
        os.replace(tmp_path, path)
        return profile

    def delete(self, name: str) -> bool:
        path = self._path(name)
        if not path.exists():
            return False
        path.unlink()
        return True


profile_store = ProfileStore()