"""Read SpiritVale's memory to find where things actually are.

The screen can say a red dot exists; it cannot say whether that dot is a monster
or the player's own pet. Three screen-based attempts at that failed (see the
README), because identity is not in the picture. This reads it from the process.

Nothing here writes to the game -- ReadProcessMemory only.

deps: none beyond the stdlib. ctypes talks to the Win32 API directly.

usage:
  python memscan.py --survey     # how much scannable memory the game has
  python memscan.py --demo       # offline self-check, no game needed
"""
import ctypes
import struct
import sys
from ctypes import wintypes

PROCESS_NAME = "SpiritVale.exe"

# Win32 constants, from memoryapi.h
PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ = 0x0010
MEM_COMMIT = 0x1000
MEM_PRIVATE = 0x20000
PAGE_READWRITE = 0x04
PAGE_EXECUTE_READWRITE = 0x40
PAGE_WRITECOPY = 0x08
PAGE_GUARD = 0x100
READABLE = (PAGE_READWRITE, PAGE_EXECUTE_READWRITE, PAGE_WRITECOPY)

# Unity world coordinates are floats in metres. Anything outside this is not a
# position -- it filters out the vast majority of 4-byte patterns, which are
# integers, pointers or garbage that happen to decode as absurd floats.
POS_MIN, POS_MAX = -20000.0, 20000.0


class MEMORY_BASIC_INFORMATION64(ctypes.Structure):
    _fields_ = [("BaseAddress", ctypes.c_ulonglong),
                ("AllocationBase", ctypes.c_ulonglong),
                ("AllocationProtect", wintypes.DWORD),
                ("__alignment1", wintypes.DWORD),
                ("RegionSize", ctypes.c_ulonglong),
                ("State", wintypes.DWORD),
                ("Protect", wintypes.DWORD),
                ("Type", wintypes.DWORD),
                ("__alignment2", wintypes.DWORD)]


def find_pid(name=PROCESS_NAME):
    """PID of the running game, or None. Snapshot walk, no extra dependency."""
    TH32CS_SNAPPROCESS = 0x02

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                    ("th32ModuleID", wintypes.DWORD),
                    ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD),
                    ("pcPriClassBase", ctypes.c_long),
                    ("dwFlags", wintypes.DWORD),
                    ("szExeFile", ctypes.c_wchar * 260)]

    k32 = ctypes.windll.kernel32
    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap == -1:
        return None
    entry = PROCESSENTRY32W()
    entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
    try:
        ok = k32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            if entry.szExeFile.lower() == name.lower():
                return entry.th32ProcessID
            ok = k32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        k32.CloseHandle(snap)
    return None


class Mem:
    """Read-only view of another process's memory."""

    def __init__(self, pid=None):
        self.pid = pid or find_pid()
        if self.pid is None:
            raise RuntimeError(f"{PROCESS_NAME} is not running")
        self.k32 = ctypes.windll.kernel32
        self.h = self.k32.OpenProcess(
            PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, self.pid)
        if not self.h:
            raise RuntimeError(
                f"cannot open pid {self.pid} (error {ctypes.get_last_error()}) -- "
                f"run this from an elevated shell if the game is elevated")

    def close(self):
        if self.h:
            self.k32.CloseHandle(self.h)
            self.h = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def regions(self):
        """Committed, readable, private regions -- where game state lives.

        Private excludes mapped files and images: the player's coordinates are
        heap data, and skipping the rest cuts the search by a wide margin.
        """
        mbi = MEMORY_BASIC_INFORMATION64()
        addr, out = 0, []
        while self.k32.VirtualQueryEx(self.h, ctypes.c_void_p(addr),
                                      ctypes.byref(mbi), ctypes.sizeof(mbi)):
            if (mbi.State == MEM_COMMIT and mbi.Type == MEM_PRIVATE and
                    mbi.Protect in READABLE and not (mbi.Protect & PAGE_GUARD)):
                out.append((mbi.BaseAddress, mbi.RegionSize))
            addr = mbi.BaseAddress + mbi.RegionSize
            if addr >= 0x7FFFFFFFFFFF:
                break
        return out

    def read(self, addr, size):
        """Bytes at addr, or None if the region went away mid-scan."""
        buf = ctypes.create_string_buffer(size)
        got = ctypes.c_size_t()
        ok = self.k32.ReadProcessMemory(self.h, ctypes.c_void_p(addr), buf, size,
                                        ctypes.byref(got))
        return buf.raw[:got.value] if ok and got.value else None


def plausible_floats(blob, base):
    """{address: value} for 4-byte-aligned floats that could be a coordinate.

    Returns a dict so successive snapshots can be intersected by address.
    """
    out = {}
    for off in range(0, len(blob) - 3, 4):
        v = struct.unpack_from("<f", blob, off)[0]
        # NaN fails every comparison, which is what excludes it here
        if POS_MIN < v < POS_MAX and (v > 1e-4 or v < -1e-4):
            out[base + off] = v
    return out


def narrow(candidates, fresh, expect, tol):
    """Keep addresses whose value moved by `expect` (+/- tol) since last snapshot.

    expect=0 means "should not have moved", which is just as useful: standing
    still eliminates everything that drifts on its own.
    """
    kept = {}
    for addr, old in candidates.items():
        new = fresh.get(addr)
        if new is None:
            continue
        if abs((new - old) - expect) <= tol:
            kept[addr] = new
    return kept


def survey():
    """How much memory is worth scanning, and is it readable at all."""
    pid = find_pid()
    if pid is None:
        print(f"{PROCESS_NAME} is not running")
        return
    with Mem(pid) as mem:
        regions = mem.regions()
        total = sum(size for _, size in regions)
        print(f"pid {pid}: {len(regions)} private committed regions, "
              f"{total / 1e6:.0f} MB")
        sample = mem.read(regions[0][0], min(4096, regions[0][1])) if regions else None
        print(f"read check: {'ok' if sample else 'FAILED'}")
        floats = 0
        for base, size in regions[:40]:
            blob = mem.read(base, min(size, 1 << 20))
            if blob:
                floats += len(plausible_floats(blob, base))
        print(f"plausible coordinate floats in the first 40 regions: {floats:,}")


def demo():
    """Self-check for the parsing and narrowing, no game needed."""
    blob = struct.pack("<ffffff", 1.5, 1e30, float("nan"), 0.0, -250.25, 3e5)
    got = plausible_floats(blob, 0x1000)
    assert got == {0x1000: 1.5, 0x1010: -250.25}, got  # huge, NaN, zero all dropped

    # a coordinate that moved the expected amount survives; noise does not
    before = {1: 10.0, 2: 10.0, 3: 10.0}
    after = {1: 12.0, 2: 10.0, 3: 99.0}
    assert narrow(before, after, expect=2.0, tol=0.5) == {1: 12.0}
    assert narrow(before, after, expect=0.0, tol=0.5) == {2: 10.0}
    # an address that vanished between snapshots is dropped, not crashed on
    assert narrow(before, {1: 12.0}, expect=2.0, tol=0.5) == {1: 12.0}

    print("demo ok")


if __name__ == "__main__":
    if "--demo" in sys.argv:
        demo()
    elif "--survey" in sys.argv:
        survey()
    else:
        print(__doc__)
