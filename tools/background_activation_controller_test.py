"""Disposable fake-activation plus ViGEm background-input probe for Windows.

This standalone diagnostic posts WM_ACTIVATEAPP/WM_ACTIVATE/WM_SETFOCUS to the
SpiritVale HWND, verifies that Windows still reports another foreground window,
and only then emits bounded virtual-Xbox input. It never imports the production
bot, calls a focus-changing API, sends keyboard/mouse input, injects code, or
writes game memory.

Run from the project root:
  .\.venv\Scripts\python.exe tools\background_activation_controller_test.py
  .\.venv\Scripts\python.exe tools\background_activation_controller_test.py --inspect
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import sys
import time
from ctypes import wintypes
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

GAME_WINDOW_TITLE = "SpiritVale"
MOVE_SECONDS = 2.0
BUTTON_SECONDS = 0.20
ACTIVATION_SETTLE_SECONDS = 0.25

WM_ACTIVATE = 0x0006
WM_SETFOCUS = 0x0007
WM_KILLFOCUS = 0x0008
WM_ACTIVATEAPP = 0x001C
WA_INACTIVE = 0
WA_ACTIVE = 1
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
STILL_ACTIVE = 259

if os.name != "nt":
    raise SystemExit("This diagnostic requires Windows.")

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
EnumWindowsProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

user32.EnumWindows.argtypes = [EnumWindowsProc, wintypes.LPARAM]
user32.EnumWindows.restype = wintypes.BOOL
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
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.PostMessageW.restype = wintypes.BOOL
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
kernel32.GetExitCodeProcess.restype = wintypes.BOOL
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL


@dataclass(frozen=True)
class WindowState:
    hwnd: int
    title: str
    class_name: str
    pid: int
    foreground_hwnd: int
    focused: bool
    minimized: bool
    visible: bool
    process_running: bool


@dataclass(frozen=True)
class TestResult:
    action: str
    observation: str
    foreground_before: int
    foreground_at_input: int
    foreground_after: int


def activation_messages() -> tuple[tuple[int, int, int], ...]:
    return (
        (WM_ACTIVATEAPP, 1, 0),
        (WM_ACTIVATE, WA_ACTIVE, 0),
        (WM_SETFOCUS, 0, 0),
    )


def deactivation_messages() -> tuple[tuple[int, int, int], ...]:
    return (
        (WM_KILLFOCUS, 0, 0),
        (WM_ACTIVATE, WA_INACTIVE, 0),
        (WM_ACTIVATEAPP, 0, 0),
    )


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


def window_state(hwnd: int) -> WindowState:
    foreground = int(user32.GetForegroundWindow() or 0)
    pid = window_pid(hwnd)
    return WindowState(
        hwnd=int(hwnd),
        title=window_text(hwnd),
        class_name=window_class(hwnd),
        pid=pid,
        foreground_hwnd=foreground,
        focused=foreground == int(hwnd),
        minimized=bool(user32.IsIconic(hwnd)),
        visible=bool(user32.IsWindowVisible(hwnd)),
        process_running=bool(user32.IsWindow(hwnd)) and process_running(pid),
    )


def enumerate_windows() -> list[WindowState]:
    handles: list[int] = []

    @EnumWindowsProc
    def callback(hwnd, _lparam):
        handles.append(int(hwnd))
        return True

    if not user32.EnumWindows(callback, 0):
        raise ctypes.WinError(ctypes.get_last_error())
    return [window_state(hwnd) for hwnd in handles if window_text(hwnd)]


def find_game_window(title_part: str) -> WindowState:
    needle = title_part.casefold().strip()
    matches = [window for window in enumerate_windows() if needle in window.title.casefold()]
    if not matches:
        raise RuntimeError(f'No top-level window title contains "{title_part}".')
    matches.sort(key=lambda window: (
        window.title.casefold() != needle,
        not window.visible,
        window.minimized,
    ))
    if len(matches) > 1:
        print(f"[WINDOW MATCHES] count={len(matches)} using=0x{matches[0].hwnd:08X}")
        for window in matches:
            print(f"  hwnd=0x{window.hwnd:08X} pid={window.pid} title={window.title!r}")
    return matches[0]


def print_state(state: WindowState, heading: str = "FOCUS") -> None:
    print(f"\n[{heading}]")
    print(f"foreground_hwnd=0x{state.foreground_hwnd:08X}")
    print(f"game_hwnd=0x{state.hwnd:08X}")
    print(f"game_focused={state.focused}")
    print(f"minimized={state.minimized}")
    print(f"visible={state.visible}")
    print(f"game_process_running={state.process_running}")


def require_background_visible(state: WindowState) -> None:
    if not state.process_running:
        raise RuntimeError("SpiritVale process is not running")
    if state.focused:
        raise RuntimeError("SpiritVale is the real foreground window; keep this terminal focused")
    if state.minimized:
        raise RuntimeError("SpiritVale is minimized; restore it without focusing it, then return here")
    if not state.visible:
        raise RuntimeError("SpiritVale window is not visible")


def post_checked(hwnd: int, message: int, wparam: int, lparam: int) -> None:
    ctypes.set_last_error(0)
    if not user32.PostMessageW(hwnd, message, wparam, lparam):
        raise ctypes.WinError(ctypes.get_last_error())


def post_sequence(
    hwnd: int,
    messages: tuple[tuple[int, int, int], ...],
    post: Callable[[int, int, int, int], None],
    label: str,
    *,
    best_effort: bool = False,
) -> None:
    names = {
        WM_ACTIVATEAPP: "WM_ACTIVATEAPP",
        WM_ACTIVATE: "WM_ACTIVATE",
        WM_SETFOCUS: "WM_SETFOCUS",
        WM_KILLFOCUS: "WM_KILLFOCUS",
    }
    first_error: Exception | None = None
    for message, wparam, lparam in messages:
        try:
            post(hwnd, message, wparam, lparam)
            print(
                f"[{label}] target=0x{hwnd:08X} message={names[message]} "
                f"wparam={wparam} lparam={lparam} posted=True"
            )
        except Exception as error:
            if not best_effort:
                raise
            if first_error is None:
                first_error = error
    if first_error is not None:
        raise first_error


class VirtualXbox:
    display_name = "Virtual Xbox 360 + targeted fake activation"

    def __init__(self) -> None:
        self._vg = None
        self._pad = None

    def connect(self) -> None:
        try:
            import vgamepad as vg
        except ImportError as error:
            raise RuntimeError("vgamepad is not installed") from error
        try:
            self._vg = vg
            self._pad = vg.VX360Gamepad()
            self.reset()
        except Exception as error:
            self._vg = None
            self._pad = None
            raise RuntimeError("Could not create the ViGEm virtual Xbox controller") from error

    def _require(self):
        if self._pad is None:
            raise RuntimeError("Controller is not connected")
        return self._pad

    def forward(self) -> None:
        pad = self._require()
        pad.left_joystick_float(x_value_float=0.0, y_value_float=1.0)
        pad.update()

    def press(self, name: str) -> None:
        if self._vg is None:
            raise RuntimeError("Controller is not connected")
        buttons = {
            "dpad_up": self._vg.XUSB_BUTTON.XUSB_GAMEPAD_DPAD_UP,
            "a": self._vg.XUSB_BUTTON.XUSB_GAMEPAD_A,
        }
        pad = self._require()
        pad.press_button(button=buttons[name])
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


def run_probe(
    hwnd: int,
    action: str,
    controller,
    *,
    state_reader: Callable[[int], WindowState] = window_state,
    post: Callable[[int, int, int, int], None] = post_checked,
    sleeper: Callable[[float], None] = time.sleep,
) -> tuple[int, int, int]:
    before = state_reader(hwnd)
    print_state(before)
    require_background_visible(before)
    activation_posted = False
    try:
        # Any partial activation sequence must receive the full neutralizing
        # sequence, even when a later PostMessage call fails.
        activation_posted = True
        post_sequence(hwnd, activation_messages(), post, "FAKE ACTIVATION")
        sleeper(ACTIVATION_SETTLE_SECONDS)

        at_input = state_reader(hwnd)
        print_state(at_input, "FOCUS AT INPUT")
        require_background_visible(at_input)
        print(
            f"\n[INPUT]\nbackend=virtual_xbox_fake_activation\n"
            f"game_focused={at_input.focused}\naction={action}\n"
            f"duration={MOVE_SECONDS if action == 'movement_forward' else BUTTON_SECONDS}"
        )
        if action == "movement_forward":
            controller.forward()
            sleeper(MOVE_SECONDS)
        elif action == "dpad_up":
            controller.press("dpad_up")
            sleeper(BUTTON_SECONDS)
        elif action == "button_a":
            controller.press("a")
            sleeper(BUTTON_SECONDS)
        else:
            raise ValueError(f"Unsupported action: {action}")
    finally:
        try:
            controller.reset()
            print("\n[RESET]\nleft_stick=(0,0)\nright_stick=(0,0)\nLT=0\nRT=0\nbuttons=none")
        finally:
            if activation_posted:
                post_sequence(
                    hwnd,
                    deactivation_messages(),
                    post,
                    "FAKE DEACTIVATION",
                    best_effort=True,
                )

    after = state_reader(hwnd)
    print_state(after, "FOCUS AFTER INPUT")
    if after.focused:
        raise RuntimeError("SpiritVale became the real foreground window; result rejected")
    return before.foreground_hwnd, at_input.foreground_hwnd, after.foreground_hwnd


def ask_observation() -> str:
    while True:
        answer = input("Did the character respond? [y/n/p]: ").strip().lower()
        if answer in {"y", "n", "p"}:
            return {"y": "pass", "n": "fail", "p": "partial"}[answer]
        print("Enter y, n, or p.")


def print_summary(results: list[TestResult]) -> None:
    print("\n================ TEST RESULTS ================")
    print("Backend: Virtual Xbox + targeted fake activation")
    print("\nBackground Visible:")
    for result in results:
        print(f"  {result.action:<20} {result.observation.upper()}")
    print("==============================================")


def self_test() -> None:
    assert activation_messages() == (
        (WM_ACTIVATEAPP, 1, 0),
        (WM_ACTIVATE, 1, 0),
        (WM_SETFOCUS, 0, 0),
    )
    assert deactivation_messages() == (
        (WM_KILLFOCUS, 0, 0),
        (WM_ACTIVATE, 0, 0),
        (WM_ACTIVATEAPP, 0, 0),
    )

    events: list[tuple] = []

    class FakeController:
        def forward(self): events.append(("forward",))
        def press(self, name): events.append(("press", name))
        def reset(self): events.append(("reset",))

    state = WindowState(10, "SpiritVale", "UnityWndClass", 20, 99, False, False, True, True)
    result = run_probe(
        10,
        "movement_forward",
        FakeController(),
        state_reader=lambda _hwnd: state,
        post=lambda hwnd, message, wparam, lparam: events.append(("post", hwnd, message, wparam, lparam)),
        sleeper=lambda _seconds: None,
    )
    assert result == (99, 99, 99)
    assert ("forward",) in events
    reset_index = max(i for i, event in enumerate(events) if event == ("reset",))
    deactivate_index = next(i for i, event in enumerate(events) if event[:3] == ("post", 10, WM_KILLFOCUS))
    assert reset_index < deactivate_index

    focused = WindowState(10, "SpiritVale", "UnityWndClass", 20, 10, True, False, True, True)
    events.clear()
    try:
        run_probe(
            10,
            "movement_forward",
            FakeController(),
            state_reader=lambda _hwnd: focused,
            post=lambda *args: events.append(args),
            sleeper=lambda _seconds: None,
        )
    except RuntimeError as error:
        assert "real foreground" in str(error)
    else:
        raise AssertionError("Focused game was not rejected")
    assert not events

    events.clear()

    def fail_during_activation(hwnd, message, wparam, lparam):
        events.append(("post", hwnd, message, wparam, lparam))
        if message == WM_ACTIVATE:
            raise RuntimeError("synthetic post failure")

    try:
        run_probe(
            10,
            "movement_forward",
            FakeController(),
            state_reader=lambda _hwnd: state,
            post=fail_during_activation,
            sleeper=lambda _seconds: None,
        )
    except RuntimeError as error:
        assert "synthetic post failure" in str(error)
    else:
        raise AssertionError("Activation post failure was not propagated")
    assert any(event[:3] == ("post", 10, WM_KILLFOCUS) for event in events)
    assert any(
        event[:5] == ("post", 10, WM_ACTIVATEAPP, 0, 0)
        for event in events
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--title", default=GAME_WINDOW_TITLE)
    parser.add_argument("--inspect", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--save-results", type=Path)
    args = parser.parse_args()

    if args.self_test:
        self_test()
        print("SELF-TEST PASS")
        return 0

    try:
        game = find_game_window(args.title)
        print_state(game, "GAME WINDOW")
        if args.inspect:
            print("No activation messages or controller input were sent.")
            return 0

        print(
            "\nKeep this terminal or another application focused. SpiritVale must be "
            "visible, not minimized, and genuinely unfocused.\n\n"
            "Commands:\n"
            "  1 = fake activation + move forward for 2 seconds\n"
            "  2 = fake activation + D-pad Up\n"
            "  3 = fake activation + button A\n"
            "  s = show actual foreground/game status\n"
            "  q = quit\n"
        )
        controller = VirtualXbox()
        controller.connect()
        results: list[TestResult] = []
        try:
            while True:
                command = input("activation-test> ").strip().lower()
                if command == "q":
                    break
                if command == "s":
                    print_state(window_state(game.hwnd))
                    continue
                actions = {"1": "movement_forward", "2": "dpad_up", "3": "button_a"}
                if command not in actions:
                    print("Enter 1, 2, 3, s, or q.")
                    continue
                action = actions[command]
                before, at_input, after = run_probe(game.hwnd, action, controller)
                results.append(TestResult(action, ask_observation(), before, at_input, after))
        finally:
            controller.reset()
            controller.close()

        print_summary(results)
        if args.save_results:
            payload = {
                "backend": "virtual_xbox_fake_activation",
                "results": [asdict(result) for result in results],
            }
            args.save_results.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            print(f"Saved: {args.save_results}")
        return 0
    except (OSError, RuntimeError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
