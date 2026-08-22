"""Guarded, reversible SpiritVale runInBackground patch.

This standalone utility changes one verified byte in Unity's serialized
PlayerSettings. It never imports or modifies the production bot.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

TARGET = Path(
    r"D:\Games\Steam\steamapps\common\SpiritVale\SpiritVale_Data\globalgamemanagers"
)
EXPECTED_ORIGINAL_HASH = (
    "ae8bccf6d59b9d160ac869c9151f0788c534115dc5f8d66e03ff2e9fecef94bf"
)
FIELD_OFFSET = 1662
ORIGINAL_BYTE = 0
PATCHED_BYTE = 1


@dataclass(frozen=True)
class PatchResult:
    original_hash: str
    patched_hash: str
    backup: Path
    changed_offsets: tuple[int, ...]


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def changed_offsets(before: bytes, after: bytes) -> tuple[int, ...]:
    if len(before) != len(after):
        raise RuntimeError("Refusing a size-changing operation")
    return tuple(index for index, pair in enumerate(zip(before, after)) if pair[0] != pair[1])


def _write_atomically(target: Path, data: bytes) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.hermes-", dir=target.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        shutil.copystat(target, temporary)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def _require_game_closed(process_running: Callable[[], bool]) -> None:
    if process_running():
        raise RuntimeError("SpiritVale.exe is running; close the game before continuing")


def enable(
    target: Path,
    backup: Path,
    *,
    offset: int,
    expected_hash: str,
    process_running: Callable[[], bool],
) -> PatchResult:
    _require_game_closed(process_running)
    original = target.read_bytes()
    original_hash = sha256(original)
    if original_hash != expected_hash:
        raise RuntimeError(
            f"Original hash mismatch: expected {expected_hash}, got {original_hash}"
        )
    if not 0 <= offset < len(original):
        raise RuntimeError(f"Field offset {offset} is outside the file")
    if original[offset] != ORIGINAL_BYTE:
        raise RuntimeError(
            f"Expected byte {ORIGINAL_BYTE} at offset {offset}, got {original[offset]}"
        )
    if backup.exists():
        raise RuntimeError(f"Backup already exists; refusing to overwrite: {backup}")

    backup.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(target, backup)
    if sha256(backup.read_bytes()) != original_hash:
        backup.unlink(missing_ok=True)
        raise RuntimeError("Backup verification failed")

    patched = bytearray(original)
    patched[offset] = PATCHED_BYTE
    patched_bytes = bytes(patched)
    differences = changed_offsets(original, patched_bytes)
    if differences != (offset,):
        raise RuntimeError(f"Expected exactly one changed byte, got {differences}")

    try:
        _write_atomically(target, patched_bytes)
        written = target.read_bytes()
        if written != patched_bytes:
            raise RuntimeError("Patched file verification failed")
    except Exception:
        _write_atomically(target, original)
        if target.read_bytes() != original:
            raise RuntimeError(
                f"Patch failed and automatic recovery failed; restore from {backup}"
            )
        raise

    return PatchResult(
        original_hash=original_hash,
        patched_hash=sha256(patched_bytes),
        backup=backup,
        changed_offsets=differences,
    )


def restore(
    target: Path,
    backup: Path,
    *,
    offset: int,
    expected_original_hash: str,
    process_running: Callable[[], bool],
) -> None:
    _require_game_closed(process_running)
    original = backup.read_bytes()
    if sha256(original) != expected_original_hash:
        raise RuntimeError("Backup hash does not match the verified original")
    if not 0 <= offset < len(original) or original[offset] != ORIGINAL_BYTE:
        raise RuntimeError("Backup does not contain the expected original field")

    expected_patched = bytearray(original)
    expected_patched[offset] = PATCHED_BYTE
    current = target.read_bytes()
    if current != bytes(expected_patched):
        raise RuntimeError(
            "Current game file is not the exact expected patched image; refusing restore"
        )

    _write_atomically(target, original)
    if target.read_bytes() != original:
        raise RuntimeError("Restore verification failed")


def spiritvale_running() -> bool:
    completed = subprocess.run(
        ["tasklist.exe", "/FI", "IMAGENAME eq SpiritVale.exe", "/FO", "CSV", "/NH"],
        capture_output=True,
        check=False,
        text=True,
    )
    return '"SpiritVale.exe"' in completed.stdout


def default_backup(target: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return target.with_name(f"{target.name}.pre-run-in-background-{stamp}.bak")


def write_rollback_cmd(path: Path, backup: Path) -> None:
    script = Path(__file__).resolve()
    python = Path(sys.executable).resolve()
    content = (
        "@echo off\r\n"
        f'"{python}" "{script}" --restore "{backup.resolve()}"\r\n'
        "set \"rc=%ERRORLEVEL%\"\r\n"
        "echo.\r\n"
        "if not \"%rc%\"==\"0\" echo Restore failed. Nothing was overwritten unless verification passed.\r\n"
        "pause\r\n"
        "exit /b %rc%\r\n"
    )
    path.write_text(content, encoding="utf-8", newline="")


def inspect_target() -> None:
    data = TARGET.read_bytes()
    state = "unknown"
    if sha256(data) == EXPECTED_ORIGINAL_HASH and data[FIELD_OFFSET] == ORIGINAL_BYTE:
        state = "verified_original"
    elif data[FIELD_OFFSET] == PATCHED_BYTE:
        candidate = bytearray(data)
        candidate[FIELD_OFFSET] = ORIGINAL_BYTE
        if sha256(bytes(candidate)) == EXPECTED_ORIGINAL_HASH:
            state = "verified_patched"
    print(f"target={TARGET}")
    print(f"size={len(data)}")
    print(f"sha256={sha256(data)}")
    print(f"field_offset={FIELD_OFFSET}")
    print(f"field_byte={data[FIELD_OFFSET]}")
    print(f"state={state}")
    print(f"game_running={spiritvale_running()}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--enable", action="store_true")
    action.add_argument("--restore", type=Path, metavar="BACKUP")
    action.add_argument("--inspect", action="store_true")
    parser.add_argument("--backup", type=Path)
    parser.add_argument("--rollback-cmd", type=Path)
    args = parser.parse_args()

    try:
        if args.inspect:
            inspect_target()
            return 0
        if args.restore:
            restore(
                TARGET,
                args.restore,
                offset=FIELD_OFFSET,
                expected_original_hash=EXPECTED_ORIGINAL_HASH,
                process_running=spiritvale_running,
            )
            print("RESTORE PASS")
            print(f"target={TARGET}")
            print(f"sha256={EXPECTED_ORIGINAL_HASH}")
            return 0

        backup = args.backup or default_backup(TARGET)
        result = enable(
            TARGET,
            backup,
            offset=FIELD_OFFSET,
            expected_hash=EXPECTED_ORIGINAL_HASH,
            process_running=spiritvale_running,
        )
        rollback_cmd = args.rollback_cmd or backup.with_suffix(backup.suffix + ".restore.cmd")
        write_rollback_cmd(rollback_cmd, backup)
        print("ENABLE PASS")
        print(f"target={TARGET}")
        print(f"backup={backup}")
        print(f"rollback={rollback_cmd}")
        print(f"original_sha256={result.original_hash}")
        print(f"patched_sha256={result.patched_hash}")
        print(f"changed_offsets={result.changed_offsets}")
        return 0
    except (OSError, RuntimeError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
