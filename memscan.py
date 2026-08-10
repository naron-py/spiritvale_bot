"""Read SpiritVale's memory to find where things actually are.

The screen can say a red dot exists; it cannot say whether that dot is a monster
or the player's own pet. Three screen-based attempts at that failed (see the
README), because identity is not in the picture. This reads it from the process.

Nothing here writes to the game -- ReadProcessMemory only.

deps: none beyond the stdlib. ctypes talks to the Win32 API directly.

usage:
  python memscan.py --survey     # how much scannable memory the game has
  python memscan.py --track      # hunt, then rank against the minimap
  python memscan.py --hunt       # iterative walk/stand scan only
  python memscan.py --findpos    # older one-shot scan, kept for reference
  python memscan.py --check 1A2B3C4 ...   # judge addresses found in Cheat Engine
  python memscan.py --entities [addr]     # list entities near the player
  python memscan.py --pos        # live position, once POSITION_CHAIN is set
  python memscan.py --demo       # offline self-check, no game needed
"""
import ctypes
import struct
import sys

import numpy as np
from ctypes import wintypes

PROCESS_NAME = "SpiritVale.exe"

# Win32 constants, from memoryapi.h
PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ = 0x0010
MEM_COMMIT = 0x1000
MEM_PRIVATE = 0x20000
MEM_MAPPED = 0x40000
PAGE_EXECUTE_WRITECOPY = 0x80
PAGE_READWRITE = 0x04
PAGE_EXECUTE_READWRITE = 0x40
PAGE_WRITECOPY = 0x08
PAGE_GUARD = 0x100
# only the low byte is the protection; the rest are modifier flags
WRITABLE = (PAGE_READWRITE, PAGE_WRITECOPY, PAGE_EXECUTE_READWRITE,
            PAGE_EXECUTE_WRITECOPY)

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
        """Committed, writable regions -- where game state lives.

        Two things this deliberately does NOT do, both of which silently hid
        memory before. It does not compare Protect exactly: the low byte is the
        protection and the high bits are modifiers, so an exact match drops every
        page carrying WRITECOMBINE -- 270 MB of them here. And it does not insist
        on MEM_PRIVATE: MAPPED holds another 288 MB of writable memory. Images
        are still skipped, being code and statics rather than object data.
        """
        mbi = MEMORY_BASIC_INFORMATION64()
        addr, out = 0, []
        while self.k32.VirtualQueryEx(self.h, ctypes.c_void_p(addr),
                                      ctypes.byref(mbi), ctypes.sizeof(mbi)):
            if (mbi.State == MEM_COMMIT and mbi.Type in (MEM_PRIVATE, MEM_MAPPED)
                    and (mbi.Protect & 0xFF) in WRITABLE
                    and not (mbi.Protect & PAGE_GUARD)):
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


PAGE = 4096


def page_hashes(mem, regions, cap=1 << 26):
    """{region_base: uint64 array, one hash per 4 KB page}.

    A full pass reads ~9.7 GB at ~720 MB/s. Hashing it to 8 bytes per page costs
    19 MB of state, so the next pass only has to look at pages that actually
    changed. Kept as one array per region rather than a dict keyed by page:
    there are 2.4M pages, and building a dict that size in Python costs more
    than the reads do.
    """
    out = {}
    for base, size in regions:
        blob = mem.read(base, min(size, cap))
        if not blob:
            continue
        pages = len(blob) // PAGE
        if not pages:
            continue
        a = np.frombuffer(blob[:pages * PAGE], dtype=np.uint64).reshape(pages, -1)
        with np.errstate(over="ignore"):  # wrapping is the point of the mix
            out[base] = (a * np.uint64(0x9E3779B97F4A7C15)).sum(axis=1,
                                                                dtype=np.uint64)
    return out


def changed_pages(before, after):
    """[(region_base, page_index)] for pages whose hash moved. Vectorised."""
    out = []
    for base, now in after.items():
        was = before.get(base)
        if was is None or len(was) != len(now):
            continue
        for i in np.nonzero(was != now)[0]:
            out.append((base, int(i)))
    return out


def read_triple(blob, base, addr):
    """(x, y, z) at addr inside this page blob, or None if it does not fit."""
    off = addr - base
    if off < 0 or off + 12 > len(blob):
        return None
    return struct.unpack_from("<fff", blob, off)


def looks_like_place(t):
    """A world position is finite, in range, and not sitting at the origin.

    Freed-and-reused memory is mostly zeros, and a zeroed triple otherwise passes
    every movement test -- it 'moves' when the page is recycled and 'holds still'
    when it is not. That accounted for every one of the first 123k candidates.
    """
    if t is None:
        return False
    x, y, z = t
    if not all(abs(v) < POS_MAX for v in t):
        return False
    if any(v != v for v in t):  # NaN
        return False
    return abs(x) > 1e-3 and abs(z) > 1e-3


def walked_triples(before, after, base, min_move, max_move, max_dy=1.0):
    """Addresses of (x, y, z) triples that moved like a character walking.

    A Unity world position is three contiguous floats, and walking on flat ground
    has a shape nothing else does: x and z both change, together covering the
    distance travelled, while y barely moves. Vectorised -- a page holds ~1000
    candidate offsets and there are thousands of pages, so this cannot be a loop.
    """
    import numpy as np
    n = min(len(before), len(after)) // 4
    if n < 3:
        return []
    b = np.frombuffer(before[:n * 4], dtype=np.float32).astype(np.float64)
    a = np.frombuffer(after[:n * 4], dtype=np.float32).astype(np.float64)
    good = np.isfinite(b) & np.isfinite(a) & (np.abs(b) < POS_MAX) & (np.abs(a) < POS_MAX)
    d = np.where(good, a - b, np.nan)

    # index i is x, i+1 is y, i+2 is z
    dx, dy, dz = d[:n - 2], d[1:n - 1], d[2:n]
    dist = np.sqrt(dx * dx + dz * dz)
    hit = ((dist >= min_move) & (dist <= max_move) & (np.abs(dy) <= max_dy) &
           np.isfinite(dist) & np.isfinite(dy))
    return [(base + int(i) * 4, float(dist[i])) for i in np.nonzero(hit)[0]]


def still_triples(before, after, base, addrs, tol=0.05):
    """Of `addrs`, those that did NOT move -- run while the character stands still.

    This is the filter that does the real work. Plenty of floats change when the
    character walks (velocity, animation, camera), but only a position both moves
    when walking and holds perfectly still when stopped.
    """
    import numpy as np
    n = min(len(before), len(after)) // 4
    b = np.frombuffer(before[:n * 4], dtype=np.float32).astype(np.float64)
    a = np.frombuffer(after[:n * 4], dtype=np.float32).astype(np.float64)
    kept = []
    for addr in addrs:
        i = (addr - base) // 4
        if i < 0 or i + 2 >= n:
            continue
        if np.all(np.abs(a[i:i + 3] - b[i:i + 3]) <= tol):
            kept.append(addr)
    return kept


def read_pages(mem, pages):
    """{(base, index): bytes} for the given pages, skipping any that vanished."""
    out = {}
    for key in pages:
        base, i = key
        blob = mem.read(base + i * PAGE, PAGE)
        if blob:
            out[key] = blob
    return out


def find_position(walk, stand, min_move=0.5, max_move=500.0, verbose=True):
    """Addresses that behave like the player's world position.

    `walk(seconds)` must move the character; `stand(seconds)` must keep it still.
    Both are passed in so this module never imports the pad -- and so the search
    can be driven by hand if the gamepad is unavailable.

    Four steps, each one cutting the field:
      1. hash every page, walk, hash again -- only changed pages can hold it
      2. read those pages, walk again, keep triples shaped like horizontal travel
      3. read again while standing still, keep the ones that stop dead
      4. walk once more, keep the ones that move again
    Step 3 is what separates a position from the velocity and animation floats
    that also move while walking.
    """
    with Mem() as mem:
        regions = mem.regions()
        if verbose:
            print(f"pid {mem.pid}: {len(regions)} regions, "
                  f"{sum(s for _, s in regions) / 1e6:.0f} MB")

        if verbose:
            print("  1/4 hashing pages, then walking...")
        before = page_hashes(mem, regions)
        walk(2.0)
        after = page_hashes(mem, regions)
        pages = changed_pages(before, after)
        if verbose:
            print(f"      {len(before):,} pages, {len(pages):,} changed")
        if not pages:
            print("      nothing changed -- did the character actually move?")
            return []

        if verbose:
            print("  2/4 walking again, looking for horizontal travel...")
        snap1 = read_pages(mem, pages)
        walk(2.0)
        snap2 = read_pages(mem, pages)
        # keyed by page from the start: filtering a flat hit list per page is
        # O(pages x hits), which with ~100k of each does not finish
        by_page, walk1 = {}, {}
        for key, blob in snap1.items():
            if key not in snap2:
                continue
            base = key[0] + key[1] * PAGE
            found = []
            for addr, dist in walked_triples(blob, snap2[key], base,
                                             min_move, max_move):
                # both ends must look like somewhere in the world, which is what
                # rules out a page that was freed and refilled with zeros
                if (looks_like_place(read_triple(blob, base, addr)) and
                        looks_like_place(read_triple(snap2[key], base, addr))):
                    found.append(addr)
                    walk1[addr] = dist
            if found:
                by_page[key] = found
        if verbose:
            print(f"      {sum(len(v) for v in by_page.values()):,} triples "
                  f"moved like a walk, across {len(by_page):,} pages")
        if not by_page:
            return []

        if verbose:
            print("  3/4 standing still -- a position must stop dead...")
        snap3 = read_pages(mem, by_page)
        stand(2.0)
        snap4 = read_pages(mem, by_page)
        still = {}
        for key, addrs in by_page.items():
            if key in snap3 and key in snap4:
                held = still_triples(snap3[key], snap4[key],
                                     key[0] + key[1] * PAGE, addrs)
                if held:
                    still[key] = held
        if verbose:
            print(f"      {sum(len(v) for v in still.values()):,} held still")
        if not still:
            return []

        if verbose:
            print("  4/4 walking once more to confirm...")
        snap5 = read_pages(mem, still)
        walk(2.0)
        snap6 = read_pages(mem, still)
        final = []
        for key, addrs in still.items():
            if key not in snap5 or key not in snap6:
                continue
            base = key[0] + key[1] * PAGE
            moved = dict(walked_triples(snap5[key], snap6[key], base,
                                        min_move, max_move))
            for a in addrs:
                if a not in moved:
                    continue
                t5, t6 = read_triple(snap5[key], base, a), read_triple(snap6[key],
                                                                       base, a)
                if not (looks_like_place(t5) and looks_like_place(t6)):
                    continue
                # both walks lasted the same time at the same speed, so a real
                # position covers a comparable distance; recycled memory does not
                ratio = moved[a] / max(walk1.get(a, 0.0), 1e-6)
                if not 0.4 <= ratio <= 2.5:
                    continue
                # and the ground does not move under the character between walks
                if abs(t5[1] - t6[1]) > 2.0:
                    continue
                final.append((a, t6, moved[a]))
        if verbose:
            print(f"      {len(final):,} confirmed")
        return sorted(final)


def verify(mem, addrs, push, min_move=0.3, max_move=100.0, max_dy=2.0,
           balance=0.35, verbose=True):
    """Keep addresses that move opposite ways when walked opposite ways.

    `push(sx, sy, seconds)` drives the stick. With the camera fixed, a real
    position must go one way for east and the other way for west -- an anti-
    correlation nothing incidental survives. Distance alone cannot do this: it
    passes anything that merely changes while walking.
    """
    def sample():
        out = {}
        for a in addrs:
            blob = mem.read(a, 12)
            if blob:
                t = struct.unpack("<fff", blob)
                if looks_like_place(t):
                    out[a] = t
        return out

    if verbose:
        print(f"  verifying {len(addrs):,} candidates against opposite walks")
    before_e = sample()
    push(1.0, 0.0, 1.5)
    after_e = sample()
    before_w = sample()
    push(-1.0, 0.0, 1.5)
    after_w = sample()

    kept = []
    for a in addrs:
        if not all(a in s for s in (before_e, after_e, before_w, after_w)):
            continue
        ex = after_e[a][0] - before_e[a][0]
        ez = after_e[a][2] - before_e[a][2]
        wx = after_w[a][0] - before_w[a][0]
        wz = after_w[a][2] - before_w[a][2]
        east = (ex * ex + ez * ez) ** 0.5
        west = (wx * wx + wz * wz) ** 0.5
        if not (min_move <= east <= max_move and min_move <= west <= max_move):
            continue
        # opposite input, opposite travel: the dot product must be negative
        if ex * wx + ez * wz >= 0:
            continue
        # same duration at the same speed, so the two legs must be comparable.
        # Without this the list fills with values that swing wildly both ways.
        if abs(east - west) / max(east, west) > balance:
            continue
        # walking is horizontal; the ground does not rise and fall underneath
        if (abs(after_e[a][1] - before_e[a][1]) > max_dy or
                abs(after_w[a][1] - before_w[a][1]) > max_dy):
            continue
        kept.append((a, after_w[a], east, west))
    if verbose:
        print(f"  {len(kept):,} moved opposite ways, evenly, on level ground")
    return kept


# Fill these in from Cheat Engine once a pointer scan settles on a stable path.
# "GameAssembly.dll" + 0x1234AB, then offsets walked one pointer at a time; the
# last offset is added without a dereference, so it lands on the x float itself.
POSITION_CHAIN = dict(module=None, base=0x0, offsets=())

# A position address confirmed by --track. Per session: the heap is reshuffled by
# a relog, a map change and a patch, so this is a scratch value, not a constant.
PLAYER_POS_ADDR = 0x02499CB5A190


def module_base(pid, name):
    """Load address of a module in the target process, or None."""
    TH32CS_SNAPMODULE = 0x08
    TH32CS_SNAPMODULE32 = 0x10

    class MODULEENTRY32W(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("th32ModuleID", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD),
                    ("GlblcntUsage", wintypes.DWORD),
                    ("ProccntUsage", wintypes.DWORD),
                    ("modBaseAddr", ctypes.POINTER(ctypes.c_byte)),
                    ("modBaseSize", wintypes.DWORD),
                    ("hModule", wintypes.HMODULE),
                    ("szModule", ctypes.c_wchar * 256),
                    ("szExePath", ctypes.c_wchar * 260)]

    k32 = ctypes.windll.kernel32
    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPMODULE | TH32CS_SNAPMODULE32,
                                        pid)
    if snap == -1:
        return None
    entry = MODULEENTRY32W()
    entry.dwSize = ctypes.sizeof(MODULEENTRY32W)
    try:
        ok = k32.Module32FirstW(snap, ctypes.byref(entry))
        while ok:
            if entry.szModule.lower() == name.lower():
                return ctypes.cast(entry.modBaseAddr, ctypes.c_void_p).value
            ok = k32.Module32NextW(snap, ctypes.byref(entry))
    finally:
        k32.CloseHandle(snap)
    return None


def resolve(mem, start, offsets):
    """Follow a Cheat Engine pointer chain to a final address, or None.

    Every offset but the last is a dereference; the last is added plainly, which
    is what CE means by "base + offsets" -- it stops on the field, not through it.
    """
    addr = start
    for off in offsets[:-1] if offsets else []:
        blob = mem.read(addr + off, 8)
        if not blob:
            return None
        addr = struct.unpack("<Q", blob)[0]
        if not addr:
            return None
    return addr + (offsets[-1] if offsets else 0)


def read_vec3(mem, addr):
    blob = mem.read(addr, 12)
    return struct.unpack("<fff", blob) if blob else None


def player_position(mem, chain=None):
    """Live (x, y, z) from POSITION_CHAIN, or None if it is not configured yet."""
    chain = chain or POSITION_CHAIN
    if not chain.get("module"):
        return None
    base = module_base(mem.pid, chain["module"])
    if base is None:
        return None
    addr = resolve(mem, base + chain["base"], chain["offsets"])
    return read_vec3(mem, addr) if addr else None


def watch_position():
    """Print the player's position as it changes. Needs POSITION_CHAIN filled in."""
    import time
    if not POSITION_CHAIN.get("module"):
        print("POSITION_CHAIN is empty -- find the address in Cheat Engine first,\n"
              "then set module/base/offsets at the top of this file.")
        return
    with Mem() as mem:
        last = None
        for _ in range(200):
            p = player_position(mem)
            if p is None:
                print("chain did not resolve -- the game may have restarted")
                return
            if last is None or max(abs(p[i] - last[i]) for i in range(3)) > 0.01:
                print(f"  x={p[0]:9.2f}  y={p[1]:8.2f}  z={p[2]:9.2f}")
                last = p
            time.sleep(0.1)


def hunt(walk, stand, rounds=10, min_walk=0.2, still_tol=0.02,
         travelled=None, verbose=True):
    """Cheat Engine's iterative scan, automated: walk, stand, walk, stand...

    Each round is a hard filter -- a coordinate MUST change while walking and MUST
    NOT change while standing. One round of each proves little; ten in a row is
    what collapses millions of floats to a handful, and it is the part that makes
    the manual version tedious. Driving the stick from here removes that.

    still_tol is not zero on purpose. A character position keeps twitching while
    the character stands there -- server reconciliation, idle animation -- and
    demanding it hold to 1e-4 threw the real thing away along with the noise.

    Candidates live as numpy arrays keyed by page, not a Python dict of addresses:
    there are millions after round one.
    """
    with Mem() as mem:
        regions = mem.regions()
        if verbose:
            print(f"pid {mem.pid}: hashing {len(regions):,} regions...")
        h0 = page_hashes(mem, regions)
        walk(1.5)
        h1 = page_hashes(mem, regions)
        pages = changed_pages(h0, h1)
        if verbose:
            print(f"  {len(pages):,} pages changed while walking")
        if not pages:
            return {}

        cands = {}
        for key, blob in read_pages(mem, pages).items():
            n = len(blob) // 4
            a = np.frombuffer(blob[:n * 4], dtype=np.float32)
            ok = np.isfinite(a) & (np.abs(a) > 1e-3) & (np.abs(a) < POS_MAX)
            idx = np.nonzero(ok)[0]
            if len(idx):
                cands[key] = (idx.astype(np.int32), a[idx].copy())
        if verbose:
            print(f"  {sum(len(v[0]) for v in cands.values()):,} plausible floats "
                  f"to start")

        for r in range(rounds):
            moving = r % 2 == 1          # candidates were captured after a walk
            (walk if moving else stand)(1.2)
            if moving and travelled is not None and not travelled():
                # the character was blocked, so nothing was proved. Applying the
                # "must have moved" filter here would delete the real position.
                if verbose:
                    print(f"  round {r + 1:2d} walk : blocked, round skipped")
                continue
            fresh = read_pages(mem, cands)
            nxt = {}
            for key, (idx, old) in cands.items():
                blob = fresh.get(key)
                if blob is None:
                    continue
                n = len(blob) // 4
                a = np.frombuffer(blob[:n * 4], dtype=np.float32)
                fits = idx < n
                idx2, old2 = idx[fits], old[fits]
                if not len(idx2):
                    continue
                cur = a[idx2]
                d = np.abs(cur.astype(np.float64) - old2.astype(np.float64))
                keep = (d >= min_walk) if moving else (d <= still_tol)
                keep &= np.isfinite(cur)
                if keep.any():
                    nxt[key] = (idx2[keep], cur[keep].copy())
            cands = nxt
            total = sum(len(v[0]) for v in cands.values())
            if verbose:
                print(f"  round {r + 1:2d} {'walk ' if moving else 'stand'}: "
                      f"{total:,} left")
            if not total:
                break
        return cands


def correlate(mem, addrs, drive, grab_gray, shift_of, wake=lambda: None,
              legs=None, verbose=True):
    """Rank addresses by how well they track the minimap across several headings.

    The minimap is ground truth: whatever the world axes are, a real position maps
    to minimap travel by ONE rotation and scale, the same for every direction.
    Fitting that 2x2 map and measuring the residual tests all headings at once,
    which single-direction checks cannot -- a value can look right going north and
    do nothing going east, and several here do exactly that.
    """
    legs = legs or [(1.0, 0.0), (0.0, 1.0), (-1.0, 0.0), (0.0, -1.0),
                    (0.7, 0.7), (-0.7, 0.7), (0.7, -0.7), (-0.7, -0.7)]
    world = {a: [] for a in addrs}
    mini = []

    wake()                 # once, outside the measured legs
    for _ in range(2):     # throwaway: the first pushes after a pause come
        drive(0.0, 1.0, 0.8)   # out weak, and a soft leg skews every fit
        drive(0.0, -1.0, 0.8)

    for sx, sy in legs:
        g0 = grab_gray()
        before = {a: read_vec3(mem, a) for a in addrs}
        drive(sx, sy, 1.2)
        after = {a: read_vec3(mem, a) for a in addrs}
        shift = shift_of(g0, grab_gray())
        if (shift[0] ** 2 + shift[1] ** 2) ** 0.5 < 5.0:
            if verbose:
                print(f"    leg ({sx:+.1f},{sy:+.1f}) went nowhere -- dropped")
            continue        # blocked by terrain; it proves nothing either way
        mini.append(shift)
        for a in addrs:
            b, f = before[a], after[a]
            world[a].append((f[0] - b[0], f[2] - b[2]) if b and f else (0.0, 0.0))
        if verbose:
            print(f"    leg ({sx:+.1f},{sy:+.1f}) minimap "
                  f"({shift[0]:+6.1f},{shift[1]:+6.1f})")

    if len(mini) < 4:
        if verbose:
            print(f"    only {len(mini)} usable legs -- need open ground")
        return []

    M = np.array(mini)                      # minimap travel per leg
    scored = []
    for a in addrs:
        W = np.array(world[a])              # world travel per leg
        if np.abs(W).max() < 1e-3 or not np.isfinite(W).all():
            continue
        if (np.abs(W).max(axis=1) < 1e-3).any():
            continue                        # sat still on at least one heading
        A, *_ = np.linalg.lstsq(M, W, rcond=None)
        resid = float(np.abs(W - M @ A).sum() / max(np.abs(W).sum(), 1e-9))
        # a rotation-and-scale has orthogonal columns of equal length; anything
        # else is a coincidence that happens to fit these particular legs
        c0, c1 = A[:, 0], A[:, 1]
        ortho = abs(float(c0 @ c1)) / max(float(np.linalg.norm(c0) *
                                                np.linalg.norm(c1)), 1e-9)
        even = abs(float(np.linalg.norm(c0) - np.linalg.norm(c1))) / \
            max(float(np.linalg.norm(c0)), 1e-9)
        scored.append((resid + ortho + even, resid, ortho, even, a,
                       float(np.linalg.norm(c0))))
    scored.sort()
    return scored


def hunt_report(cands, limit=20):
    """Flatten hunt() output to sorted (address, value) pairs and show a few."""
    out = []
    for (base, page), (idx, vals) in cands.items():
        for i, v in zip(idx, vals):
            out.append((base + page * PAGE + int(i) * 4, float(v)))
    out.sort()
    print(f"\n{len(out):,} survivors")
    for addr, v in out[:limit]:
        print(f"  0x{addr:012X}  {v:12.3f}")
    if len(out) > limit:
        print(f"  ... and {len(out) - limit:,} more")
    return out


HITS_FILE = "memscan_hits.txt"


def track(rounds=10):
    """Hunt for the position, then rank survivors against the minimap.

    One process start to finish: addresses die on a relog, a map change and a
    patch, so finding and testing them has to happen without a gap in between.
    """
    import time
    import cv2
    import mss

    sys.path.insert(0, __file__.rsplit("\\", 1)[0])
    import minimap_bot as bot

    win = bot.find_window()
    reg = bot.minimap_region(win)
    han = cv2.createHanningWindow((reg["width"], reg["height"]), cv2.CV_32F)
    sct = mss.mss()
    pad = bot.VirtualPad()

    def grab_gray():
        img = np.array(sct.grab(reg))[:, :, :3]
        return np.float32(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY))

    def shift_of(g0, g1):
        (dx, dy), _ = cv2.phaseCorrelate(g0, g1, han)
        return dx, dy

    def drive(sx, sy, secs):
        # deliberately no wake_controller here: its up-then-down nudge lands
        # inside the measured window and cancels most of the leg's travel.
        # Waking once before the legs is enough -- the walking keeps it awake.
        t0 = time.time()
        while time.time() - t0 < secs:
            pad.stick(sx, sy, False)
            time.sleep(0.05)
        pad.stick(0.0, 0.0, False)
        time.sleep(0.25)

    def walk(secs):
        bot.wake_controller(pad)
        t0 = time.time()
        while time.time() - t0 < secs:
            a = (time.time() - t0) * 1.1
            pad.stick(float(np.cos(a)), float(np.sin(a)), False)
            time.sleep(0.05)
        pad.stick(0.0, 0.0, False)

    def stand(secs):
        pad.stick(0.0, 0.0, False)
        time.sleep(secs)

    seen = {"gray": None}

    def walk_watched(secs):
        seen["gray"] = grab_gray()
        walk(secs)

    def travelled():
        if seen["gray"] is None:
            return True
        dx, dy = shift_of(seen["gray"], grab_gray())
        return (dx * dx + dy * dy) ** 0.5 >= 8.0

    print("focus the game -- 3s")
    time.sleep(3)
    try:
        cands = hunt(walk_watched, stand, rounds=rounds, travelled=travelled)
        addrs = [base + page * PAGE + int(i) * 4
                 for (base, page), (idx, _) in cands.items() for i in idx]
        if not addrs:
            print("nothing survived the hunt")
            return
        # saved so the correlation can be retried without a fresh 3-minute hunt;
        # the addresses stay valid until the character object is rebuilt
        with open(HITS_FILE, "w") as fh:
            fh.write("\n".join(f"{a:X}" for a in addrs))
        print(f"  survivors written to {HITS_FILE}")
        print(f"\ncorrelating {len(addrs):,} survivors against the minimap")
        with Mem() as mem:
            scored = correlate(mem, addrs, drive, grab_gray, shift_of,
                               wake=lambda: bot.wake_controller(pad))
        if not scored:
            print("  none of them moved on every heading")
            return
        print(f"\n  best fits (lower is better; scale is world units per "
              f"minimap pixel):")
        print(f"  {'address':>16}{'error':>9}{'skew':>8}{'uneven':>9}{'scale':>9}"
              f"   position")
        with Mem() as mem:
            for total, resid, ortho, even, a, scale in scored[:12]:
                p = read_vec3(mem, a)
                shown = f"({p[0]:9.1f},{p[1]:7.1f},{p[2]:9.1f})" if p else "?"
                print(f"  0x{a:012X}{resid:9.3f}{ortho:8.3f}{even:9.3f}"
                      f"{scale:9.2f}   {shown}")
    finally:
        pad.close()
        sct.close()


def object_headers(mem, addr, back=0x600):
    """Candidate IL2CPP object headers before `addr`: [(base, class_ptr)].

    An object starts with a pointer to its Il2CppClass followed by a monitor
    field that is almost always zero. Several candidates match by luck, so the
    caller has to test them -- see entity_class().
    """
    blob = mem.read(addr - back, back)
    if not blob:
        return []
    out = []
    for off in range(len(blob) - 16, -1, -8):
        q0 = struct.unpack_from("<Q", blob, off)[0]
        q1 = struct.unpack_from("<Q", blob, off + 8)[0]
        if q1 or not (0x10000 < q0 < 0x7FFFFFFFFFFF):
            continue
        probe = mem.read(q0, 8)
        if not probe:
            continue
        inner = struct.unpack("<Q", probe)[0]
        if 0x10000 < inner < 0x7FFFFFFFFFFF:
            out.append((addr - back + off, q0))
    return out


def instances_of(mem, class_ptr, limit=4000):
    """Addresses of every object whose header points at `class_ptr`."""
    target = np.uint64(class_ptr)
    out = []
    for base, size in mem.regions():
        blob = mem.read(base, min(size, 1 << 24))
        if not blob:
            continue
        n = len(blob) // 8
        a = np.frombuffer(blob[:n * 8], dtype=np.uint64)
        for i in np.nonzero(a == target)[0]:
            out.append(base + int(i) * 8)
            if len(out) >= limit:
                return out
    return out


def entity_class(mem, pos_addr, verbose=True):
    """(class_ptr, offset) for the class whose instances all carry a position.

    The header nearest the position is usually the wrong one: the first match
    walking backwards had 37,591 instances, every one reading zero. What picks
    the right class is not the header pattern but the payload -- its instances
    must hold real coordinates at the same offset.
    """
    best = None
    for base, cls in object_headers(mem, pos_addr):
        off = pos_addr - base
        objs = instances_of(mem, cls, limit=1500)
        good = 0
        for o in objs:
            blob = mem.read(o + off, 12)
            if not blob:
                continue
            x, y, z = struct.unpack("<fff", blob)
            if (all(abs(v) < POS_MAX for v in (x, y, z)) and abs(y) < 3000
                    and (abs(x) > 1 or abs(z) > 1)):
                good += 1
        if verbose:
            print(f"  class 0x{cls:012X} +0x{off:<4X} {len(objs):5d} instances, "
                  f"{good:5d} with a position")
        # How many instances carry a position varies a lot by map -- 63% on one,
        # 19% on the next, because most of the class is pooled and idle. The
        # decoys sit near 3%, so the gap is still wide; do not tighten this to
        # fit whichever map you happen to be standing on.
        share = good / max(len(objs), 1)
        if good >= 15 and share > 0.10 and (best is None or good > best[2]):
            best = (cls, off, good)
    return (best[0], best[1]) if best else (None, None)


def entities(mem, class_ptr, off):
    """[(distance_from_first, address, x, y, z)] for everything with a position."""
    out = []
    for o in instances_of(mem, class_ptr):
        blob = mem.read(o + off, 12)
        if not blob:
            continue
        x, y, z = struct.unpack("<fff", blob)
        if not all(abs(v) < POS_MAX for v in (x, y, z)):
            continue
        if abs(x) < 1 and abs(z) < 1:
            continue
        out.append((o, x, y, z))
    return out


def show_entities(pos_addr=None):
    """Find the entity class from a known position address and list what is near."""
    pos_addr = pos_addr or PLAYER_POS_ADDR
    if not pos_addr:
        print("need a confirmed position address -- run --track first, then pass it")
        return
    with Mem() as mem:
        here = read_vec3(mem, pos_addr)
        if not here or not looks_like_place(here):
            print(f"0x{pos_addr:X} no longer holds a position -- re-run --track")
            return
        print(f"player at ({here[0]:.1f},{here[1]:.1f},{here[2]:.1f})")
        cls, off = entity_class(mem, pos_addr)
        if cls is None:
            print("no class looked like an entity list")
            return
        print(f"\nentity class 0x{cls:012X}, position at +0x{off:X}")
        ents = entities(mem, cls, off)
        ranked = sorted(((((x - here[0]) ** 2 + (z - here[2]) ** 2) ** 0.5, o, x, y, z)
                         for o, x, y, z in ents))
        near = [e for e in ranked if e[0] <= 34]
        print(f"{len(ents)} with positions, {len(near)} within the minimap radius\n")
        for d, o, x, y, z in near[:20]:
            print(f"  0x{o:012X}  dist {d:6.1f}  ({x:8.1f},{y:6.1f},{z:8.1f})")


def check(addrs):
    """Judge hand-found addresses: which one is the player, and where are y/z?

    Cheat Engine leaves several smooth movers, and they are not interchangeable.
    The camera tracks the character, so it moves too -- but it eases to a stop,
    while the character's own position freezes the instant the stick centres.
    That coast is the tell, so this measures it directly.
    """
    import time
    sys.path.insert(0, __file__.rsplit("\\", 1)[0])
    import minimap_bot as bot

    pad = bot.VirtualPad()
    print("focus the game -- 3s")
    time.sleep(3)

    def sample(mem):
        # read a window around each address: CE finds x, and y/z sit next to it
        return {a: read_vec3(mem, a) for a in addrs}

    try:
        with Mem() as mem:
            bot.wake_controller(pad)
            print("\nwalking east, then stopping dead:")
            start = sample(mem)
            t0 = time.time()
            while time.time() - t0 < 1.5:
                pad.stick(1.0, 0.0, False)
                time.sleep(0.05)
            moving = sample(mem)
            pad.stick(0.0, 0.0, False)      # centre the stick and read at once
            time.sleep(0.12)
            just_after = sample(mem)
            time.sleep(1.0)
            settled = sample(mem)

            print(f"  {'address':>16}{'walked':>9}{'coasted':>9}   verdict")
            for a in addrs:
                if not all(s.get(a) for s in (start, moving, just_after, settled)):
                    print(f"  0x{a:012X}   unreadable")
                    continue
                walked = max(abs(moving[a][i] - start[a][i]) for i in (0, 2))
                coast = max(abs(settled[a][i] - just_after[a][i]) for i in (0, 2))
                if walked < 0.05:
                    verdict = "did not move -- not it"
                elif coast > 0.05:
                    verdict = "coasted after stop -- camera, or smoothed"
                else:
                    verdict = "STOPS DEAD -- looks like the character"
                print(f"  0x{a:012X}{walked:9.2f}{coast:9.2f}   {verdict}")

            print("\n  current values, and the floats either side:")
            for a in addrs:
                blob = mem.read(a - 8, 32)
                if blob:
                    vals = struct.unpack("<8f", blob)
                    show = "  ".join(f"{v:9.2f}" for v in vals)
                    print(f"  0x{a:012X}  [-8..+20]  {show}")
    finally:
        pad.close()


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


def findpos():
    """Drive the character with the pad and report position candidates."""
    import time

    sys.path.insert(0, __file__.rsplit("\\", 1)[0])
    import minimap_bot as bot

    win = bot.find_window()
    pad = bot.VirtualPad()
    print(f"game window {win.width}x{win.height} -- focus it now, 3s")
    time.sleep(3)

    def walk(seconds):
        bot.wake_controller(pad)
        t0 = time.time()
        while time.time() - t0 < seconds:
            # a slow arc, so x and z both change and neither stays zero
            a = (time.time() - t0) * 1.2
            pad.stick(float(np.cos(a)), float(np.sin(a)), False)
            time.sleep(0.05)
        pad.stick(0.0, 0.0, False)

    def stand(seconds):
        pad.stick(0.0, 0.0, False)
        time.sleep(seconds)

    try:
        hits = find_position(walk, stand)
    finally:
        pad.close()

    if not hits:
        print("\nno candidates -- see which step emptied out above")
        return
    print(f"\n{len(hits):,} candidates survived the walk/stand/walk filter")

    def push(sx, sy, seconds):
        bot.wake_controller(pad)
        t0 = time.time()
        while time.time() - t0 < seconds:
            pad.stick(sx, sy, False)
            time.sleep(0.05)
        pad.stick(0.0, 0.0, False)
        time.sleep(0.3)

    pad = bot.VirtualPad()
    try:
        with Mem() as mem:
            good = verify(mem, [a for a, _, _ in hits], push)
            if not good:
                print("\nnothing tracked the character both ways")
                return
            # the best candidate is the most balanced pair of legs, not the
            # biggest -- sorting by size just surfaces the wildest values
            good.sort(key=lambda r: abs(r[2] - r[3]) / max(r[2], r[3]))
            print(f"\nbest {min(len(good), 15)} (address, position, east, west):")
            for addr, (x, y, z), e, w in good[:15]:
                print(f"  0x{addr:012X}  x={x:9.2f} y={y:8.2f} z={z:9.2f}"
                      f"   east {e:6.2f}  west {w:6.2f}")
            print("\nlive read of the top few, walking north:")
            top = [r[0] for r in good[:5]]
            first = {a: mem.read(a, 12) for a in top}
            push(0.0, 1.0, 1.5)
            for a in top:
                b0, b1 = first[a], mem.read(a, 12)
                if b0 and b1:
                    p, q = struct.unpack("<fff", b0), struct.unpack("<fff", b1)
                    d = tuple(round(q[i] - p[i], 2) for i in range(3))
                    print(f"  0x{a:012X}  {tuple(round(v, 1) for v in q)}  delta {d}")
    finally:
        pad.close()


def demo():
    """Self-check for the parsing and narrowing, no game needed."""
    import numpy as np
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

    # a walking position: x and z travel, y stays put. Laid out beside things
    # that must NOT match -- a vertical-only change, and a wild jump.
    b = struct.pack("<fffffffff", 10.0, 5.0, 20.0,   # position, walks
                                  1.0, 1.0, 1.0,      # only y will move
                                  0.0, 0.0, 0.0)      # will jump miles
    a = struct.pack("<fffffffff", 13.0, 5.02, 24.0,
                                  1.0, 9.0, 1.0,
                                  900.0, 0.0, 900.0)
    hits = walked_triples(b, a, 0x2000, min_move=1.0, max_move=50.0)
    addrs = [h[0] for h in hits]
    assert 0x2000 in addrs, hits              # 5m in x, 4m in z, y +0.02
    assert 0x200C not in addrs, hits          # vertical only
    assert 0x2018 not in addrs, hits          # 1272m in one step, a teleport
    assert abs(dict(hits)[0x2000] - 5.0) < 0.01, hits

    # standing still: the position holds, a drifting float does not
    b2 = struct.pack("<ffffff", 10.0, 5.0, 20.0, 1.0, 2.0, 3.0)
    a2 = struct.pack("<ffffff", 10.0, 5.0, 20.0, 1.4, 2.0, 3.0)
    assert still_triples(b2, a2, 0x3000, [0x3000, 0x300C]) == [0x3000]

    # page hashing must notice a single changed byte and ignore an identical page
    import hashlib  # noqa: F401  (kept out of the hash path deliberately)
    p1 = bytes(PAGE)
    p2 = bytes(PAGE - 1) + b"\x01"
    h1 = np.frombuffer(p1, dtype=np.uint64) * np.uint64(0x9E3779B97F4A7C15)
    h2 = np.frombuffer(p2, dtype=np.uint64) * np.uint64(0x9E3779B97F4A7C15)
    assert h1.sum(dtype=np.uint64) != h2.sum(dtype=np.uint64)
    was = {0x1000: np.array([1, 2, 3], dtype=np.uint64)}
    now = {0x1000: np.array([1, 9, 3], dtype=np.uint64)}
    assert changed_pages(was, now) == [(0x1000, 1)], changed_pages(was, now)
    assert changed_pages(was, was) == []
    # a region that appeared or resized between passes is skipped, not crashed on
    assert changed_pages({}, now) == []
    assert changed_pages({0x1000: np.array([1], dtype=np.uint64)}, now) == []

    # verify(): a position reverses when the stick reverses; a drifting float
    # does not. sample() runs four times -- before/after east, before/after west.
    seq = {0x10: [10.0, 15.0, 15.0, 10.0],   # walks east, walks back west
           0x20: [10.0, 15.0, 15.0, 20.0]}   # keeps drifting east regardless
    calls = {"n": 0}

    class FakeMem:
        def read(self, addr, size):
            phase = min(calls["n"] // len(seq), 3)
            calls["n"] += 1
            return struct.pack("<fff", seq[addr][phase], 3.0, 7.0)

    kept = [a for a, *_ in verify(FakeMem(), [0x10, 0x20], lambda *a: None,
                                  verbose=False)]
    assert kept == [0x10], kept

    # pointer chain: every offset but the last dereferences, the last does not
    heap = {0x1000: struct.pack("<Q", 0x2000),      # base+0x00 -> 0x2000
            0x2030: struct.pack("<Q", 0x3000)}      # 0x2000+0x30 -> 0x3000

    class ChainMem:
        pid = 0

        def read(self, addr, size):
            if size == 8:
                return heap.get(addr)
            return struct.pack("<fff", 1.5, 2.5, 3.5) if addr == 0x3018 else None

    cm = ChainMem()
    assert resolve(cm, 0x1000, (0x00, 0x30, 0x18)) == 0x3018
    assert read_vec3(cm, resolve(cm, 0x1000, (0x00, 0x30, 0x18))) == (1.5, 2.5, 3.5)
    assert resolve(cm, 0x1000, ()) == 0x1000          # no offsets is the base
    assert resolve(cm, 0x9999, (0x00, 0x10)) is None  # unreadable link gives up

    print("demo ok")


if __name__ == "__main__":
    if "--demo" in sys.argv:
        demo()
    elif "--survey" in sys.argv:
        survey()
    elif "--findpos" in sys.argv:
        findpos()
    elif "--pos" in sys.argv:
        watch_position()
    elif "--hunt" in sys.argv:
        import time as _t

        sys.path.insert(0, __file__.rsplit("\\", 1)[0])
        import minimap_bot as _bot

        _pad = _bot.VirtualPad()
        print("focus the game -- 3s")
        _t.sleep(3)

        def _walk(secs):
            _bot.wake_controller(_pad)
            _t0 = _t.time()
            while _t.time() - _t0 < secs:
                _a = (_t.time() - _t0) * 1.1
                _pad.stick(float(np.cos(_a)), float(np.sin(_a)), False)
                _t.sleep(0.05)
            _pad.stick(0.0, 0.0, False)

        def _stand(secs):
            _pad.stick(0.0, 0.0, False)
            _t.sleep(secs)

        try:
            hunt_report(hunt(_walk, _stand))
        finally:
            _pad.close()
    elif "--track" in sys.argv:
        track()
    elif "--entities" in sys.argv:
        rest = [a for a in sys.argv[sys.argv.index("--entities") + 1:]
                if not a.startswith("--")]
        show_entities(int(rest[0], 16) if rest else None)
    elif "--check" in sys.argv:
        given = [int(a, 16) for a in sys.argv[sys.argv.index("--check") + 1:]
                 if not a.startswith("--")]
        if not given:
            print("usage: python memscan.py --check <hex address> [more...]")
        else:
            check(given)
    else:
        print(__doc__)
