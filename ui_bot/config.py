"""Small, validated, atomic UI settings store."""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields
import json
import os
from pathlib import Path
import shutil
from typing import Any, Mapping


def application_root() -> Path:
    """Stable application root; independent of the process working directory."""
    return Path(__file__).resolve().parent.parent


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class UiSettings:
    schema: int = 1
    mode: str = "memory"
    selected_area: str = ""
    auto_reconnect: bool = True
    follow_player: bool = True
    trail_length: int = 120
    max_entities: int = 250
    log_level: str = "INFO"
    demo_mode: bool = False

    def validated(self) -> "UiSettings":
        if self.schema != 1:
            raise ConfigError(f"unsupported settings schema {self.schema}")
        if self.mode not in ("memory", "minimap"):
            raise ConfigError("mode must be memory or minimap")
        if not isinstance(self.selected_area, str) or len(self.selected_area) > 80:
            raise ConfigError("selected area is invalid")
        if not 0 <= self.trail_length <= 5000:
            raise ConfigError("trail length must be between 0 and 5000")
        if not 10 <= self.max_entities <= 2000:
            raise ConfigError("max entities must be between 10 and 2000")
        if self.log_level not in ("DEBUG", "INFO", "WARNING", "ERROR"):
            raise ConfigError("invalid log level")
        return self

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "UiSettings":
        if not isinstance(raw, Mapping):
            raise ConfigError("settings root must be an object")
        allowed = {item.name for item in fields(cls)}
        values = {key: value for key, value in raw.items() if key in allowed}
        try:
            return cls(**values).validated()
        except TypeError as exc:
            raise ConfigError(f"invalid settings: {exc}") from exc


class AtomicConfigStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.backup = self.path.with_suffix(self.path.suffix + ".bak")
        self.temp = self.path.with_suffix(self.path.suffix + ".tmp")
        self.last_warning = ""

    def _read(self, path: Path) -> UiSettings:
        try:
            return UiSettings.from_mapping(
                json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError, ConfigError) as exc:
            raise ConfigError(f"could not load {path.name}: {exc}") from exc

    def load(self) -> UiSettings:
        self.last_warning = ""
        if not self.path.exists():
            self.last_warning = "Settings missing; using safe defaults."
            return UiSettings()
        try:
            return self._read(self.path)
        except ConfigError as primary:
            if self.backup.exists():
                try:
                    recovered = self._read(self.backup)
                    self.last_warning = f"Primary settings invalid; loaded backup ({primary})."
                    return recovered
                except ConfigError:
                    pass
            self.last_warning = f"Settings invalid; using safe defaults ({primary})."
            return UiSettings()

    def save(self, settings: UiSettings) -> None:
        settings.validated()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(asdict(settings), indent=2, sort_keys=True) + "\n"
        try:
            with self.temp.open("w", encoding="utf-8", newline="\n") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            if self.path.exists():
                shutil.copy2(self.path, self.backup)
            os.replace(self.temp, self.path)
        except OSError as exc:
            try:
                self.temp.unlink(missing_ok=True)
            except OSError:
                pass
            raise ConfigError(f"could not save settings: {exc}") from exc
