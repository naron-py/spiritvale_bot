"""Standalone targeted-window keyboard diagnostic for SpiritVale on Windows.

This disposable probe sends WM_KEYDOWN/WM_KEYUP only to the detected game HWND.
It does not import the production bot, change focus, move the physical mouse, alter
physical keyboard state, inject code, hook functions, or write game memory.

This method often fails with Unity games because many poll keyboard state instead of
consuming posted window messages. A focused PASS and background PASS are both needed
before considering it for production.

Examples (PowerShell, from the project root):
  .\.venv\Scripts\python.exe tools\background_window_message_test.py --inspect
  .\.venv\Scripts\python.exe tools\background_window_message_test.py
  .\.venv\Scripts\python.exe tools\background_window_message_test.py --auto
  .\.venv\Scripts\python.exe tools\background_window_message_test.py --save-results window_message_results.json
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import sys
import time
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

GAME_WINDOW_TITLE = "SpiritVale"
KEY_HOLD_SECONDS = 2.0
PREPARE_SECONDS = 5

WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
MAPVK_VK_TO_VSC = 0
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
STILL_ACTIVE = 259

VK_W = 0x57
VK_A = 0x41
VK_S = 0x53
VK_D = 0x44
VK_SPACE = 0x20
KEYS = {
    "forward": ("W", VK_W),
    "backward": ("S", VK_S),
    "left": ("A", VK_A),
    "right": ("D", VK_D),
    "space": ("SPACE", VK_SPACE),
}

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
user32.MapVirtualKeyW.argtypes = [wintypes.UINT, wintypes.UINT]
user32.MapVirtualKeyW.restype = wintypes.UINT
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
kernel32.GetExitCodeProcess.restype = wintypes.BOOL
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL


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
            print(
                f"  hwnd=0x{window.hwnd:08X} pid={window.pid} "
                f"visible={window.visible} title={window.title!r}"
            )
    return matches[0]


def scenario_name(info: WindowInfo) -> str:
    if info.minimized:
        return "minimized"
    if info.foreground:
        return "focused"
    if info.visible:
        return "background_visible"
    return "background_hidden"


def print_window(info: WindowInfo) -> None:
    print("\n[GAME WINDOW]")
    print(f"hwnd=0x{info.hwnd:08X}")
    print(f"title={info.title}")
    print(f"class={info.class_name}")
    print(f"pid={info.pid}")
    print(f"foreground={info.foreground}")
    print(f"minimized={info.minimized}")
    print(f"visible={info.visible}")
    print(f"process_running={info.process_running}")


def key_lparam(vk: int, key_up: bool, map_virtual_key: Callable[[int], int]) -> int:
    scan_code = int(map_virtual_key(vk)) & 0xFF
    value = 1 | (scan_code << 16)
    if key_up:
        value |= (1 << 30) | (1 << 31)
    return value


class WindowMessageBackend:
    """Posts bounded keyboard messages to one fixed HWND and tracks held keys."""

    name = "window_message"
    display_name = "Targeted Win32 WM_KEYDOWN/WM_KEYUP"

    def __init__(
        self,
        hwnd: int,
        post_message: Callable[[int, int, int, int], bool] | None = None,
        map_virtual_key: Callable[[int], int] | None = None,
    ) -> None:
        self.hwnd = hwnd
        self._post_message = post_message or user32.PostMessageW
        self._map_virtual_key = map_virtual_key or (
            lambda key: int(user32.MapVirtualKeyW(key, MAPVK_VK_TO_VSC))
        )
        self.held: set[int] = set()

    def _post(self, message: int, vk: int, key_up: bool) -> None:
        ctypes.set_last_error(0)
        lparam = key_lparam(vk, key_up, self._map_virtual_key)
        if not self._post_message(self.hwnd, message, vk, lparam):
            error = ctypes.get_last_error()
            if error:
                raise ctypes.WinError(error)
            raise RuntimeError(f"PostMessageW returned false for message=0x{message:04X} key=0x{vk:02X}")

    def key_down(self, vk: int) -> None:
        self._post(WM_KEYDOWN, vk, False)
        self.held.add(vk)

    def key_up(self, vk: int) -> None:
        try:
            self._post(WM_KEYUP, vk, True)
        finally:
            self.held.discard(vk)

    def hold_key(
        self,
        vk: int,
        duration: float,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        try:
            self.key_down(vk)
            sleep(duration)
        finally:
            self.key_up(vk)

    def release_all(self) -> list[Exception]:
        errors: list[Exception] = []
        for vk in list(self.held):
            try:
                self.key_up(vk)
            except Exception as exc:
                errors.append(exc)
        return errors

    def close(self) -> list[Exception]:
        return self.release_all()


class DiagnosticRunner:
    def __init__(self, hwnd: int, backend: WindowMessageBackend, duration: float) -> None:
        self.hwnd = hwnd
        self.backend = backend
        self.duration = duration
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
        return info

    def release_all(self) -> None:
        errors = self.backend.release_all()
        print("[RESET]")
        print("keys=none")
        if errors:
            print(f"[WARNING] key_release_errors={len(errors)}", file=sys.stderr)

    def send_key(self, action: str) -> WindowInfo:
        if action not in KEYS:
            raise ValueError(f"Unknown action: {action}")
        key_name, vk = KEYS[action]
        info = self.print_focus()
        if not info.process_running:
            raise RuntimeError("Game process is not running; no message sent.")
        print("\n[INPUT]")
        print(f"backend={self.backend.name}")
        print(f"game_focused={info.foreground}")
        print("action=targeted_window_key")
        print(f"target_hwnd=0x{self.hwnd:08X}")
        print(f"key={key_name}")
        print(f"virtual_key=0x{vk:02X}")
        print(f"down_message=0x{WM_KEYDOWN:04X}")
        print(f"up_message=0x{WM_KEYUP:04X}")
        print(f"duration={self.duration:.1f}")
        try:
            self.backend.hold_key(vk, self.duration)
        finally:
            self.release_all()
        return info

    def ask_result(self, info: WindowInfo, action: str) -> None:
        while True:
            answer = input("Did the character respond? [y=yes/n=no/p=partially]: ").strip().lower()
            if answer in {"y", "n", "p"}:
                break
            print("Enter y, n, or p.")
        self.results.append({
            "scenario": scenario_name(info),
            "action": action,
            "key": KEYS[action][0],
            "result": {"y": "pass", "n": "fail", "p": "partial"}[answer],
            "game_focused": info.foreground,
            "minimized": info.minimized,
            "visible": info.visible,
        })

    def run_and_record(self, action: str) -> None:
        info = self.send_key(action)
        self.ask_result(info, action)

    def print_summary(self) -> None:
        print("\n================ TEST RESULTS ================")
        print(f"Backend: {self.backend.display_name}")
        if not self.results:
            print("No observed results recorded.")
        for scenario in ("focused", "background_visible", "minimized", "background_hidden"):
            rows = [result for result in self.results if result["scenario"] == scenario]
            if rows:
                print(f"\n{scenario.replace('_', ' ').title()}:")
                for result in rows:
                    print(f"  {result['action']:<18} {str(result['result']).upper()}")
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


def prepare_scenario(runner: DiagnosticRunner, scenario: str) -> bool:
    instructions = {
        "focused": (
            "After pressing ENTER, manually switch to SpiritVale during the countdown. "
            "The script will not focus it."
        ),
        "background_visible": (
            "Leave SpiritVale restored, then keep this terminal or another application focused."
        ),
        "minimized": (
            "Manually minimize SpiritVale, then return to this terminal."
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
        "background_visible": not info.foreground and info.visible and not info.minimized,
        "minimized": not info.foreground and info.minimized,
    }[scenario]
    if not valid:
        print(f"[SKIP] Win32 state does not satisfy scenario={scenario}; no message sent.")
        return False
    print(f"[VERIFIED] scenario={scenario}")
    return True


def automatic_sequence(runner: DiagnosticRunner) -> None:
    tests = (
        ("focused", "forward"),
        ("background_visible", "forward"),
        ("minimized", "forward"),
    )
    for number, (scenario, action) in enumerate(tests, 1):
        print(f"\n========== TEST {number}: {scenario} / {action} ==========")
        if prepare_scenario(runner, scenario):
            runner.run_and_record(action)


def print_commands(duration: float) -> None:
    print(f"""
Commands:
  1 = Post W (forward) for {duration:.1f} seconds
  2 = Post S (backward) for {duration:.1f} seconds
  3 = Post A (left) for {duration:.1f} seconds
  4 = Post D (right) for {duration:.1f} seconds
  5 = Post SPACE for {duration:.1f} seconds
  a = Guided focused/background/minimized forward test
  r = Release every tracked key
  s = Show game/focus status
  h = Show commands
  q = Quit
""")


def interactive(runner: DiagnosticRunner) -> None:
    actions = {"1": "forward", "2": "backward", "3": "left", "4": "right", "5": "space"}
    print_commands(runner.duration)
    while True:
        command = input("test> ").strip().lower()
        if command == "q":
            return
        if command in actions:
            runner.run_and_record(actions[command])
        elif command == "a":
            automatic_sequence(runner)
        elif command == "r":
            runner.print_focus()
            runner.release_all()
        elif command == "s":
            print_window(runner.current())
            runner.print_focus()
        elif command == "h":
            print_commands(runner.duration)
        else:
            print("Unknown command. Enter h for help.")


def self_test() -> None:
    calls: list[tuple[object, ...]] = []
    backend = WindowMessageBackend(
        hwnd=0x1234,
        post_message=lambda hwnd, message, key, lparam: calls.append(
            (hwnd, message, key, lparam)
        ) or True,
        map_virtual_key=lambda _key: 0x11,
    )
    backend.hold_key(VK_W, 2.0, sleep=lambda seconds: calls.append(("sleep", seconds)))
    messages = [call for call in calls if isinstance(call[0], int)]
    assert [call[1] for call in messages] == [WM_KEYDOWN, WM_KEYUP]
    assert messages[0][0] == messages[1][0] == 0x1234
    assert not messages[0][3] & (1 << 31)
    assert messages[1][3] & (1 << 30)
    assert messages[1][3] & (1 << 31)
    assert not backend.held

    interrupted: list[int] = []
    backend = WindowMessageBackend(
        hwnd=1,
        post_message=lambda _hwnd, message, _key, _lparam: interrupted.append(message) or True,
        map_virtual_key=lambda _key: 1,
    )
    try:
        backend.hold_key(VK_A, 1.0, sleep=lambda _seconds: (_ for _ in ()).throw(KeyboardInterrupt()))
    except KeyboardInterrupt:
        pass
    else:
        raise AssertionError("interrupt was not propagated")
    assert interrupted == [WM_KEYDOWN, WM_KEYUP]
    assert not backend.held

    attempted: list[int] = []
    def fail_down(_hwnd, message, _key, _lparam):
        attempted.append(message)
        return message == WM_KEYUP
    backend = WindowMessageBackend(hwnd=1, post_message=fail_down, map_virtual_key=lambda _key: 1)
    try:
        backend.hold_key(VK_D, 1.0, sleep=lambda _seconds: None)
    except RuntimeError:
        pass
    else:
        raise AssertionError("failed key-down was not reported")
    assert attempted == [WM_KEYDOWN, WM_KEYUP]
    assert not backend.held

    assert scenario_name(WindowInfo(1, "x", "x", 1, True, False, True, True)) == "focused"
    assert scenario_name(WindowInfo(1, "x", "x", 1, False, False, True, True)) == "background_visible"
    assert scenario_name(WindowInfo(1, "x", "x", 1, False, True, True, True)) == "minimized"
    print("[SELF TEST] PASS")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--title", default=GAME_WINDOW_TITLE, help="partial game window title")
    parser.add_argument("--duration", type=float, default=KEY_HOLD_SECONDS, help="key hold duration in seconds")
    parser.add_argument("--inspect", action="store_true", help="inspect the window without sending messages")
    parser.add_argument("--auto", action="store_true", help="run guided focused/background/minimized tests")
    parser.add_argument("--save-results", type=Path, help="optional JSON result path")
    parser.add_argument("--self-test", action="store_true", help="run offline safety checks only")
    args = parser.parse_args()
    if not 0.05 <= args.duration <= 10.0:
        parser.error("--duration must be between 0.05 and 10.0 seconds")
    return args


def main() -> int:
    args = parse_args()
    if args.self_test:
        self_test()
        return 0

    try:
        game = find_game_window(args.title)
    except RuntimeError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 2
    print_window(game)
    print("\n[METHOD]")
    print("backend=window_message")
    print("messages=WM_KEYDOWN,WM_KEYUP")
    print("target=game_hwnd_only")
    print("focus_changes=False")
    print("physical_keyboard_state_changes=False")
    if args.inspect:
        return 0

    backend = WindowMessageBackend(game.hwnd)
    runner = DiagnosticRunner(game.hwnd, backend, args.duration)
    try:
        if args.auto:
            automatic_sequence(runner)
        else:
            print("\nBackground Window-Message Test")
            print(f"Game: {game.title}")
            print(f"HWND: 0x{game.hwnd:08X}")
            interactive(runner)
    except (KeyboardInterrupt, EOFError):
        print("\n[INTERRUPTED]")
    except (RuntimeError, OSError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 3
    finally:
        errors = backend.close()
        print("[CLOSED] all tracked keys released")
        if errors:
            print(f"[WARNING] final_key_release_errors={len(errors)}", file=sys.stderr)
        runner.print_summary()
        if args.save_results:
            runner.save(args.save_results)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
