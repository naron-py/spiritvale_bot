"""Small, validated, atomic UI settings store."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields, replace
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


SETTINGS_SCHEMA = 2
CONTROLLER_BUTTONS = (
    "dpad_up", "dpad_down", "dpad_left", "dpad_right",
    "a", "b", "x", "y", "lb", "rb", "lt", "rt",
)
BUTTON_LABELS = {
    "dpad_up": "D-Pad Up", "dpad_down": "D-Pad Down",
    "dpad_left": "D-Pad Left", "dpad_right": "D-Pad Right",
    "a": "A", "b": "B", "x": "X", "y": "Y",
    "lb": "LB", "rb": "RB", "lt": "LT", "rt": "RT",
}


@dataclass(frozen=True)
class BuffSlot:
    id: str
    name: str
    enabled: bool
    button: str
    order: int
    user_created: bool = False


@dataclass(frozen=True)
class AttackSlot:
    id: str
    name: str
    enabled: bool
    button: str
    order: int


def default_buff_slots() -> tuple[BuffSlot, ...]:
    buttons = ("dpad_up", "dpad_down", "dpad_left", "dpad_right", "x", "a")
    return tuple(BuffSlot(f"buff-{index}", f"Buff Slot {index}", True,
                          button, index - 1)
                 for index, button in enumerate(buttons, 1))


def default_attack_slots() -> tuple[AttackSlot, ...]:
    return (AttackSlot("attack-1", "Attack Skill 1", True, "lb", 0),
            AttackSlot("attack-2", "Attack Skill 2", True, "rb", 1))


def _validate_slot(slot, kind: str) -> None:
    if not isinstance(slot.id, str) or not slot.id or len(slot.id) > 80:
        raise ConfigError(f"{kind} slot ID is invalid")
    if not isinstance(slot.name, str) or not slot.name or len(slot.name) > 80:
        raise ConfigError(f"{kind} slot name is invalid")
    if type(slot.enabled) is not bool:
        raise ConfigError(f"{slot.name} enabled state is invalid")
    if slot.button not in CONTROLLER_BUTTONS:
        raise ConfigError(f"{slot.name} controller button is invalid")
    if type(slot.order) is not int or slot.order < 0:
        raise ConfigError(f"{slot.name} order is invalid")


@dataclass(frozen=True)
class UiSettings:
    schema: int = SETTINGS_SCHEMA
    mode: str = "memory"
    selected_area: str = ""
    auto_reconnect: bool = True
    follow_player: bool = True
    trail_length: int = 120
    max_entities: int = 250
    log_level: str = "INFO"
    demo_mode: bool = False
    buff_slots: tuple[BuffSlot, ...] = field(default_factory=default_buff_slots)
    attack_slots: tuple[AttackSlot, ...] = field(default_factory=default_attack_slots)

    def validated(self) -> "UiSettings":
        if self.schema != SETTINGS_SCHEMA:
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
        if not self.buff_slots:
            raise ConfigError("at least one buff slot is required")
        all_slots = tuple(self.buff_slots) + tuple(self.attack_slots)
        if len({slot.id for slot in all_slots}) != len(all_slots):
            raise ConfigError("controller slot IDs must be unique")
        for slot in self.buff_slots:
            if not isinstance(slot, BuffSlot):
                raise ConfigError("buff slots are invalid")
            _validate_slot(slot, "buff")
        for slot in self.attack_slots:
            if not isinstance(slot, AttackSlot):
                raise ConfigError("attack slots are invalid")
            _validate_slot(slot, "attack")
        if len(self.attack_slots) != 2:
            raise ConfigError("attack configuration must contain exactly two slots")
        enabled_attacks = [slot for slot in self.attack_slots if slot.enabled]
        if not enabled_attacks:
            raise ConfigError("at least one enabled attack skill is required")
        active = [slot for slot in all_slots if slot.enabled]
        owner = {}
        for slot in active:
            previous = owner.get(slot.button)
            if previous is not None:
                raise ConfigError(
                    f"controller button conflict: {previous.name} and {slot.name} "
                    f"both use {BUTTON_LABELS[slot.button]}")
            owner[slot.button] = slot
        return self

    def control_config(self) -> dict[str, list[dict[str, object]]]:
        return {"buff_slots": [asdict(slot) for slot in self.buff_slots],
                "attack_slots": [asdict(slot) for slot in self.attack_slots]}

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "UiSettings":
        settings, _warnings = cls.parse(raw)
        return settings

    @classmethod
    def parse(cls, raw: Mapping[str, Any]) -> tuple["UiSettings", list[str]]:
        if not isinstance(raw, Mapping):
            raise ConfigError("settings root must be an object")
        schema = raw.get("schema", 1)
        if schema not in (1, SETTINGS_SCHEMA):
            raise ConfigError(f"unsupported settings schema {schema}")
        allowed = {item.name for item in fields(cls)}
        values = {key: value for key, value in raw.items() if key in allowed}
        values["schema"] = SETTINGS_SCHEMA
        warnings = []

        def slots(name, slot_type, defaults):
            incoming = raw.get(name)
            if incoming is None:
                return defaults
            if not isinstance(incoming, list):
                warnings.append(f"{name} invalid; restored defaults")
                return defaults
            result = []
            by_id = {slot.id: slot for slot in defaults}
            for index, item in enumerate(incoming):
                fallback = by_id.get(item.get("id")) if isinstance(item, Mapping) else None
                if fallback is None and index < len(defaults):
                    fallback = defaults[index]
                try:
                    if not isinstance(item, Mapping):
                        raise ConfigError("slot must be an object")
                    slot = slot_type(**{
                        key: item[key] for key in item
                        if key in {entry.name for entry in fields(slot_type)}
                    })
                    _validate_slot(slot, "buff" if slot_type is BuffSlot else "attack")
                    result.append(slot)
                except (KeyError, TypeError, ConfigError) as exc:
                    label = str(item.get("name", "controller slot")) if isinstance(item, Mapping) else "controller slot"
                    warnings.append(f"{label} invalid; restored safe default ({exc})")
                    if fallback is not None:
                        result.append(fallback)
                    elif slot_type is BuffSlot:
                        result.append(BuffSlot(
                            str(item.get("id") or f"recovered-{index}"),
                            label, False, "a", index, True))
            return tuple(result) or defaults

        parsed_buffs = slots("buff_slots", BuffSlot, default_buff_slots())
        attack_defaults = default_attack_slots()
        parsed_attacks = slots("attack_slots", AttackSlot, attack_defaults)
        if len(parsed_attacks) != len(attack_defaults):
            incoming_by_id = {slot.id: slot for slot in parsed_attacks}
            parsed_attacks = tuple(incoming_by_id.get(slot.id, slot)
                                   for slot in attack_defaults)
            warnings.append(
                "extra attack slot or missing attack slot; restored exactly two defaults")
        owners = {}

        def repair_conflicts(items, defaults):
            repaired = []
            by_id = {item.id: item for item in defaults}
            for item in sorted(items, key=lambda entry: entry.order):
                replacement = item
                previous = owners.get(item.button) if item.enabled else None
                if previous is not None:
                    fallback = by_id.get(item.id)
                    if (fallback is not None and fallback.button not in owners):
                        replacement = fallback
                    else:
                        replacement = replace(item, enabled=False)
                    warnings.append(
                        f"{item.name} conflict with {previous}; restored safe default")
                if replacement.enabled:
                    owners[replacement.button] = replacement.name
                repaired.append(replacement)
            return tuple(repaired)

        # Attack owns its bindings first: loading one damaged buff must never
        # silently remove the only continuous attack input.
        values["attack_slots"] = repair_conflicts(
            parsed_attacks, attack_defaults)
        values["buff_slots"] = repair_conflicts(
            parsed_buffs, default_buff_slots())
        try:
            settings = cls(**values).validated()
        except TypeError as exc:
            raise ConfigError(f"invalid settings: {exc}") from exc
        return settings, warnings


class AtomicConfigStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.backup = self.path.with_suffix(self.path.suffix + ".bak")
        self.temp = self.path.with_suffix(self.path.suffix + ".tmp")
        self.last_warning = ""

    def _read(self, path: Path) -> tuple[UiSettings, list[str]]:
        try:
            return UiSettings.parse(
                json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError, ConfigError) as exc:
            raise ConfigError(f"could not load {path.name}: {exc}") from exc

    def load(self) -> UiSettings:
        self.last_warning = ""
        if not self.path.exists():
            self.last_warning = "Settings missing; using safe defaults."
            return UiSettings()
        try:
            settings, warnings = self._read(self.path)
            self.last_warning = "; ".join(warnings)
            return settings
        except ConfigError as primary:
            if self.backup.exists():
                try:
                    recovered, warnings = self._read(self.backup)
                    self.last_warning = f"Primary settings invalid; loaded backup ({primary})."
                    if warnings:
                        self.last_warning += " " + "; ".join(warnings)
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
                try:
                    self._read(self.path)
                except ConfigError:
                    pass  # Never replace the last valid backup with bad input.
                else:
                    shutil.copy2(self.path, self.backup)
            os.replace(self.temp, self.path)
        except OSError as exc:
            try:
                self.temp.unlink(missing_ok=True)
            except OSError:
                pass
            raise ConfigError(f"could not save settings: {exc}") from exc
