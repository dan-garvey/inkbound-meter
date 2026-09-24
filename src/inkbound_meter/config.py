from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path


def data_directory() -> Path:
    root = os.environ.get("LOCALAPPDATA")
    return Path(root) / "InkboundMeter" if root else Path.home() / ".local/share/inkbound-meter"


def default_log() -> Path:
    return Path(os.environ.get("USERPROFILE", str(Path.home()))) / (
        "AppData/LocalLow/Shiny Shoe/Inkbound/logfile.log"
    )


@dataclass
class Settings:
    x: int | None = None
    y: int | None = None
    width: int = 360
    opacity: float = 0.50
    scope: str = "encounter"
    metric: str = "damage"
    interaction_key: str = "F8"
    visibility_key: str = "F9"
    party_enabled: bool = True
    party_x: int | None = None
    party_y: int | None = None

    @classmethod
    def load(cls, path: Path) -> Settings:
        try:
            values = json.loads(path.read_text(encoding="utf-8"))
            result = cls(
                **{key: value for key, value in values.items() if key in cls.__dataclass_fields__}
            )
            result.width = max(300, min(700, int(result.width)))
            result.opacity = max(0.35, min(1.0, float(result.opacity)))
            if result.scope not in ("encounter", "run"):
                result.scope = "encounter"
            if result.metric not in ("damage", "taken", "shielding", "healing", "pressure"):
                result.metric = "damage"
            for key in ("interaction_key", "visibility_key"):
                if not isinstance(getattr(result, key), str):
                    setattr(result, key, getattr(cls(), key))
            if not isinstance(result.party_enabled, bool):
                result.party_enabled = True
            for key in ("x", "y", "party_x", "party_y"):
                value = getattr(result, key)
                if value is not None:
                    setattr(result, key, int(value))
            return result
        except (OSError, ValueError, TypeError, AttributeError):
            return cls()

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(asdict(self), indent=2) + "\n", encoding="utf-8")
        temporary.replace(path)
