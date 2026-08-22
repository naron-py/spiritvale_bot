"""Standalone foreground/background virtual-controller diagnostic for Windows.

This file intentionally does not import or modify the SpiritVale bot. It sends only
ViGEm-backed Xbox controller state; it never sends keyboard or mouse input and never
changes window focus, position, visibility, or minimization.

Examples:
  python tools/background_controller_test.py --inspect
  python tools/background_controller_test.py
  python tools/background_controller_test.py --auto
  python tools/background_controller_test.py --save-results background_controller_results.json
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import platform
import struct
import sys
import time
from abc import ABC, abstractmethod
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

GAME_WINDOW_TITLE = "SpiritVale"
MOVE_SECONDS = 2.0
BUTTON_SECONDS = 0.20
PREPARE_SECONDS = 5

if os.name != "nt":
    raise SystemExit("This diagnostic requires Windows.")

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

EnumWindowsProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

user32.EnumWindows.argtypes = [EnumWindowsProc, wintypes.LPARAM]
user32.EnumWindows.restype = wintypes.BOOL
user32.EnumChildWindows.argtypes = [wintypes.HWND, EnumWindowsProc, wintypes.LPARAM]
user32.EnumChildWindows.restype = wintypes.BOOL
user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
user32.GetWindowTextLengthW.restype = ctypes.c_int
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowTextW.restype = ctypes.c_int
user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetClassNameW.restype = ctypes.c_int
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.GetForegroundWindow.argtypes = []
user32.GetForegroundWindow.restype = wintypes.HWND
user32.IsIconic.argtypes = [wintypes.HWND]
user32.IsIconic.restype = wintypes.BOOL
user32.IsWindow.argtypes = [wintypes.HWND]
user32.IsWindow.restype = wintypes.BOOL
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.IsWindowVisible.restype = wintypes.BOOL
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
kernel32.GetExitCodeProcess.restype = wintypes.BOOL
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
STILL_ACTIVE = 259


@dataclass(frozen=True)
class WindowInfo:
    hwnd: int
    title: str
    class_name: str
    pid: int
    foreground: bool
    minimized: bool
    visible: bool
    process_running: bool


class ControllerBackend(ABC):
    name = "unknown"
    display_name = "Unknown controller"

    @abstractmethod
    def connect(self) -> None: ...

    @abstractmethod
    def set_left_stick(self, x: float, y: float) -> None: ...

    @abstractmethod
    def press_button(self, button: str) -> None: ...

    @abstractmethod
    def release_button(self, button: str) -> None: ...

    @abstractmethod
    def set_trigger(self, trigger: str, value: float) -> None: ...

    @abstractmethod
    def reset(self) -> None: ...

    @abstractmethod
    def close(self) -> None: ...

    @property
    @abstractmethod
    def connected(self) -> bool: ...


class VirtualXboxBackend(ControllerBackend):
    """Xbox 360 controller state through vgamepad and the ViGEmBus driver."""

    name = "virtual_xbox"
    display_name = "Virtual Xbox 360 Controller (vgamepad/ViGEmBus)"

    def __init__(self) -> None:
        self._vg = None
        self._pad = None

    @property
    def connected(self) -> bool:
        return self._pad is not None

    def connect(self) -> None:
        try:
            import vgamepad as vg
        except ImportError as exc:
            raise RuntimeError(
                "vgamepad is not installed in this Python environment. "
                "Install the Python package only after confirming ViGEmBus is installed."
            ) from exc
        try:
            self._pad = vg.VX360Gamepad()
            self._vg = vg
            self.reset()
        except Exception as exc:
            self._pad = None
            self._vg = None
            raise RuntimeError(
                "Could not create a virtual Xbox controller. The ViGEmBus system "
                "driver may be absent or unavailable; this script never installs it."
            ) from exc

    def _require_pad(self):
        if self._pad is None:
            raise RuntimeError("Controller backend is not connected.")
        return self._pad

    def set_left_stick(self, x: float, y: float) -> None:
        pad = self._require_pad()
        pad.left_joystick_float(x_value_float=clamp(x), y_value_float=clamp(y))
        pad.update()

    def _button(self, name: str):
        if self._vg is None:
            raise RuntimeError("Controller backend is not connected.")
        buttons = {
            "a": self._vg.XUSB_BUTTON.XUSB_GAMEPAD_A,
            "b": self._vg.XUSB_BUTTON.XUSB_GAMEPAD_B,
            "x": self._vg.XUSB_BUTTON.XUSB_GAMEPAD_X,
            "y": self._vg.XUSB_BUTTON.XUSB_GAMEPAD_Y,
            "up": self._vg.XUSB_BUTTON.XUSB_GAMEPAD_DPAD_UP,
            "down": self._vg.XUSB_BUTTON.XUSB_GAMEPAD_DPAD_DOWN,
            "left": self._vg.XUSB_BUTTON.XUSB_GAMEPAD_DPAD_LEFT,
            "right": self._vg.XUSB_BUTTON.XUSB_GAMEPAD_DPAD_RIGHT,
        }
        try:
            return buttons[name.lower()]
        except KeyError as exc:
            raise ValueError(f"Unsupported button: {name}") from exc

    def press_button(self, button: str) -> None:
        pad = self._require_pad()
        pad.press_button(button=self._button(button))
        pad.update()

    def release_button(self, button: str) -> None:
        pad = self._require_pad()
        pad.release_button(button=self._button(button))
        pad.update()

    def set_trigger(self, trigger: str, value: float) -> None:
        pad = self._require_pad()
        value = clamp(value)
        if trigger.lower() == "lt":
            pad.left_trigger_float(value_float=value)
        elif trigger.lower() == "rt":
            pad.right_trigger_float(value_float=value)
        else:
            raise ValueError(f"Unsupported trigger: {trigger}")
        pad.update()

    def reset(self) -> None:
        if self._pad is not None:
            self._pad.reset()
            self._pad.update()

    def close(self) -> None:
        if self._pad is not None:
            try:
                self.reset()
            finally:
                self._pad = None
                self._vg = None


def clamp(value: float) -> float:
    return max(-1.0, min(1.0, float(value)))


def window_text(hwnd: int) -> str:
    length = user32.GetWindowTextLengthW(hwnd)
    buffer = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buffer, len(buffer))
    return buffer.value


def window_class(hwnd: int) -> str:
    buffer = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buffer, len(buffer))
    return buffer.value


def window_pid(hwnd: int) -> int:
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(pid.value)


def process_running(pid: int) -> bool:
    if not pid:
        return False
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        exit_code = wintypes.DWORD()
        return bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))) and exit_code.value == STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def describe_window(hwnd: int) -> WindowInfo:
    foreground = int(user32.GetForegroundWindow() or 0)
    pid = window_pid(hwnd)
    return WindowInfo(
        hwnd=int(hwnd),
        title=window_text(hwnd),
        class_name=window_class(hwnd),
        pid=pid,
        foreground=foreground == int(hwnd),
        minimized=bool(user32.IsIconic(hwnd)),
        visible=bool(user32.IsWindowVisible(hwnd)),
        process_running=bool(user32.IsWindow(hwnd)) and process_running(pid),
    )


def enumerate_windows() -> list[WindowInfo]:
    handles: list[int] = []

    @EnumWindowsProc
    def callback(hwnd, _lparam):
        handles.append(int(hwnd))
        return True

    if not user32.EnumWindows(callback, 0):
        raise ctypes.WinError(ctypes.get_last_error())
    return [describe_window(hwnd) for hwnd in handles if window_text(hwnd)]


def find_game_window(title_part: str) -> WindowInfo:
    needle = title_part.casefold().strip()
    matches = [w for w in enumerate_windows() if needle in w.title.casefold()]
    if not matches:
        raise RuntimeError(f'No top-level window title contains "{title_part}".')
    matches.sort(key=lambda w: (w.title.casefold() != needle, not w.visible, w.minimized))
    if len(matches) > 1:
        print(f'[WINDOW MATCHES] count={len(matches)} using=0x{matches[0].hwnd:08X}')
        for item in matches:
            print(f'  hwnd=0x{item.hwnd:08X} pid={item.pid} visible={item.visible} title={item.title!r}')
    return matches[0]


def child_windows(hwnd: int) -> list[WindowInfo]:
    handles: list[int] = []

    @EnumWindowsProc
    def callback(child, _lparam):
        handles.append(int(child))
        return True

    user32.EnumChildWindows(hwnd, callback, 0)
    return [describe_window(child) for child in handles]


def print_game_window(info: WindowInfo, include_children: bool = False) -> None:
    print("\n[GAME WINDOW]")
    print(f"hwnd=0x{info.hwnd:08X}")
    print(f"title={info.title}")
    print(f"class={info.class_name}")
    print(f"pid={info.pid}")
    print(f"foreground={info.foreground}")
    print(f"minimized={info.minimized}")
    print(f"visible={info.visible}")
    print(f"process_running={info.process_running}")
    if include_children:
        children = child_windows(info.hwnd)
        print(f"child_windows={len(children)}")
        for child in children:
            print(
                f"  hwnd=0x{child.hwnd:08X} class={child.class_name!r} "
                f"visible={child.visible} title={child.title!r}"
            )


def scenario_name(info: WindowInfo) -> str:
    if info.minimized:
        return "minimized"
    if info.foreground:
        return "focused"
    if info.visible:
        return "background_visible"
    return "background_hidden"


class DiagnosticRunner:
    def __init__(self, hwnd: int, backend: ControllerBackend) -> None:
        self.hwnd = hwnd
        self.backend = backend
        self.results: list[dict[str, object]] = []

    def current(self) -> WindowInfo:
        if not user32.IsWindow(self.hwnd):
            raise RuntimeError("The selected game window no longer exists.")
        return describe_window(self.hwnd)

    def print_focus(self) -> WindowInfo:
        info = self.current()
        foreground = int(user32.GetForegroundWindow() or 0)
        print("\n[FOCUS]")
        print(f"foreground_hwnd=0x{foreground:08X}")
        print(f"game_hwnd=0x{self.hwnd:08X}")
        print(f"game_focused={info.foreground}")
        print(f"minimized={info.minimized}")
        print(f"visible={info.visible}")
        print(f"game_process_running={info.process_running}")
        print(f"controller_connected={self.backend.connected}")
        return info

    def reset(self) -> None:
        self.backend.reset()
        print("[RESET]")
        print("left_stick=(0,0)")
        print("right_stick=(0,0)")
        print("LT=0")
        print("RT=0")
        print("buttons=none")

    def move(self, x: float, y: float, duration: float = MOVE_SECONDS) -> WindowInfo:
        info = self.print_focus()
        print("\n[INPUT]")
        print(f"backend={self.backend.name}")
        print(f"game_focused={info.foreground}")
        print("action=left_stick")
        print(f"x={x:.2f}")
        print(f"y={y:.2f}")
        print(f"duration={duration:.1f}")
        try:
            self.backend.set_left_stick(x, y)
            time.sleep(duration)
        finally:
            self.reset()
        return info

    def button(self, button: str, duration: float = BUTTON_SECONDS) -> WindowInfo:
        info = self.print_focus()
        print("\n[INPUT]")
        print(f"backend={self.backend.name}")
        print(f"game_focused={info.foreground}")
        print("action=button")
        print(f"button={button}")
        print(f"duration={duration:.2f}")
        try:
            self.backend.press_button(button)
            time.sleep(duration)
            self.backend.release_button(button)
        finally:
            self.reset()
        return info

    def trigger(self, trigger: str, duration: float = BUTTON_SECONDS) -> WindowInfo:
        info = self.print_focus()
        print("\n[INPUT]")
        print(f"backend={self.backend.name}")
        print(f"game_focused={info.foreground}")
        print("action=trigger")
        print(f"trigger={trigger.upper()}")
        print("value=1.00")
        print(f"duration={duration:.2f}")
        try:
            self.backend.set_trigger(trigger, 1.0)
            time.sleep(duration)
            self.backend.set_trigger(trigger, 0.0)
        finally:
            self.reset()
        return info

    def ask_result(self, info: WindowInfo, action: str) -> None:
        while True:
            answer = input("Did the character respond? [y=yes/n=no/p=partially]: ").strip().lower()
            if answer in {"y", "n", "p"}:
                break
            print("Enter y, n, or p.")
        verdict = {"y": "pass", "n": "fail", "p": "partial"}[answer]
        self.results.append({
            "scenario": scenario_name(info),
            "action": action,
            "result": verdict,
            "game_focused": info.foreground,
            "minimized": info.minimized,
            "visible": info.visible,
        })

    def run_and_record(self, action: str, operation: Callable[[], WindowInfo]) -> None:
        info = operation()
        self.ask_result(info, action)

    def print_summary(self) -> None:
        print("\n================ TEST RESULTS ================")
        print(f"Backend: {self.backend.display_name}")
        if not self.results:
            print("No observed results recorded.")
        else:
            order = ["focused", "background_visible", "minimized", "background_hidden"]
            for scenario in order:
                rows = [row for row in self.results if row["scenario"] == scenario]
                if not rows:
                    continue
                print(f"\n{scenario.replace('_', ' ').title()}:")
                for row in rows:
                    print(f"  {row['action']:<18} {row['result'].upper()}")
        print("==============================================")

    def save(self, path: Path) -> None:
        payload = {
            "backend": self.backend.name,
            "backend_display_name": self.backend.display_name,
            "game_hwnd": f"0x{self.hwnd:08X}",
            "results": self.results,
        }
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        print(f"[SAVED] {path.resolve()}")


def prepare_scenario(runner: DiagnosticRunner, scenario: str) -> WindowInfo | None:
    instructions = {
        "focused": (
            "After pressing ENTER, manually switch to the GAME during the countdown. "
            "The script will not focus it for you."
        ),
        "background_visible": (
            "Leave the game restored and visible or behind another application. "
            "Keep this terminal (or another application) focused."
        ),
        "minimized": (
            "Manually minimize the game, then return to this terminal. "
            "The script will not minimize or restore it."
        ),
    }
    print(f"\n[PREPARE] scenario={scenario}")
    print(instructions[scenario])
    input("Press ENTER when ready to begin preparation: ")
    if scenario == "focused":
        for remaining in range(PREPARE_SECONDS, 0, -1):
            print(f"Executing in {remaining}...", flush=True)
            time.sleep(1)
    info = runner.print_focus()
    valid = {
        "focused": info.foreground and not info.minimized,
        "background_visible": (not info.foreground) and info.visible and not info.minimized,
        "minimized": (not info.foreground) and info.minimized,
    }[scenario]
    if not valid:
        print(f"[SKIP] Win32 state does not satisfy scenario={scenario}; no input sent.")
        return None
    print(f"[VERIFIED] scenario={scenario}")
    return info


def automatic_sequence(runner: DiagnosticRunner) -> None:
    tests = [
        ("focused", "movement_forward", lambda: runner.move(0.0, 1.0)),
        ("focused", "button_a", lambda: runner.button("a")),
        ("focused", "trigger_rt", lambda: runner.trigger("rt")),
        ("background_visible", "movement_forward", lambda: runner.move(0.0, 1.0)),
        ("background_visible", "button_a", lambda: runner.button("a")),
        ("background_visible", "trigger_rt", lambda: runner.trigger("rt")),
        ("minimized", "movement_forward", lambda: runner.move(0.0, 1.0)),
    ]
    for number, (scenario, action, operation) in enumerate(tests, 1):
        print(f"\n========== TEST {number}: {scenario} / {action} ==========")
        if prepare_scenario(runner, scenario) is None:
            continue
        runner.run_and_record(action, operation)


def print_commands() -> None:
    print("""
Commands:
  1 = Move forward for 2 seconds
  2 = Move backward for 2 seconds
  3 = Move left for 2 seconds
  4 = Move right for 2 seconds
  5 = Press D-pad Up
  6 = Press D-pad Down
  7 = Press D-pad Right
  8 = Press D-pad Left
  9 = Left Trigger
  0 = Right Trigger
  a = Run guided focused/background/minimized sequence
  r = Reset controller
  s = Show status
  h = Show commands
  q = Quit
""")


def interactive(runner: DiagnosticRunner) -> None:
    operations: dict[str, tuple[str, Callable[[], WindowInfo]]] = {
        "1": ("movement_forward", lambda: runner.move(0.0, 1.0)),
        "2": ("movement_backward", lambda: runner.move(0.0, -1.0)),
        "3": ("movement_left", lambda: runner.move(-1.0, 0.0)),
        "4": ("movement_right", lambda: runner.move(1.0, 0.0)),
        "5": ("dpad_up", lambda: runner.button("up")),
        "6": ("dpad_down", lambda: runner.button("down")),
        "7": ("dpad_right", lambda: runner.button("right")),
        "8": ("dpad_left", lambda: runner.button("left")),
        "9": ("trigger_lt", lambda: runner.trigger("lt")),
        "0": ("trigger_rt", lambda: runner.trigger("rt")),
    }
    print_commands()
    while True:
        command = input("test> ").strip().lower()
        if command == "q":
            return
        if command == "r":
            runner.print_focus()
            runner.reset()
        elif command == "s":
            print_game_window(runner.current(), include_children=True)
            runner.print_focus()
        elif command == "h":
            print_commands()
        elif command == "a":
            automatic_sequence(runner)
        elif command in operations:
            action, operation = operations[command]
            runner.run_and_record(action, operation)
        else:
            print("Unknown command. Enter h for help.")


def environment_report() -> None:
    try:
        import importlib.util
        vgamepad_available = importlib.util.find_spec("vgamepad") is not None
    except Exception:
        vgamepad_available = False
    print("[ENVIRONMENT]")
    print(f"windows={platform.platform()}")
    print(f"python={platform.python_version()}")
    print(f"python_bits={struct.calcsize('P') * 8}")
    print(f"python_executable={sys.executable}")
    print(f"vgamepad_installed={vgamepad_available}")
    print("controller_backend=virtual_xbox")
    print("system_driver_install_attempted=False")


def self_test() -> None:
    assert clamp(-2) == -1.0
    assert clamp(2) == 1.0
    assert clamp(0.25) == 0.25
    focused = WindowInfo(1, "x", "x", 1, True, False, True, True)
    background = WindowInfo(1, "x", "x", 1, False, False, True, True)
    minimized = WindowInfo(1, "x", "x", 1, False, True, True, True)
    assert scenario_name(focused) == "focused"
    assert scenario_name(background) == "background_visible"
    assert scenario_name(minimized) == "minimized"
    print("[SELF TEST] PASS")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--title", default=GAME_WINDOW_TITLE, help="partial game window title")
    parser.add_argument("--inspect", action="store_true", help="inspect environment/window without creating a controller")
    parser.add_argument("--children", action="store_true", help="include child windows in inspection output")
    parser.add_argument("--auto", action="store_true", help="run the guided test sequence")
    parser.add_argument("--save-results", type=Path, help="optional JSON output path")
    parser.add_argument("--self-test", action="store_true", help="run offline pure-function checks")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.self_test:
        self_test()
        return 0

    environment_report()
    try:
        game = find_game_window(args.title)
    except RuntimeError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 2
    print_game_window(game, include_children=args.children)
    if args.inspect:
        return 0

    backend = VirtualXboxBackend()
    runner: DiagnosticRunner | None = None
    try:
        backend.connect()
        print(f"\n[CONTROLLER] backend={backend.name}")
        print(f"display_name={backend.display_name}")
        print(f"connected={backend.connected}")
        runner = DiagnosticRunner(game.hwnd, backend)
        if args.auto:
            automatic_sequence(runner)
        else:
            print("\nBackground Controller Test")
            print(f"Game: {game.title}")
            print(f"HWND: 0x{game.hwnd:08X}")
            print(f"Controller backend: {backend.display_name}")
            interactive(runner)
    except (KeyboardInterrupt, EOFError):
        print("\n[INTERRUPTED]")
    except RuntimeError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 3
    finally:
        try:
            if runner is not None:
                runner.reset()
        finally:
            backend.close()
            print("[CLOSED] controller reset and disconnected")
        if runner is not None:
            runner.print_summary()
            if args.save_results:
                runner.save(args.save_results)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
