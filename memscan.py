"""Read SpiritVale's memory to find where things actually are.

The screen can say a red dot exists; it cannot say whether that dot is a monster
or the player's own pet. Three screen-based attempts at that failed (see the
README), because identity is not in the picture. This reads it from the process.

Nothing here writes to the game -- ReadProcessMemory only.

SOLVED, and not by scanning: Il2CppDumper on GameAssembly.dll plus
global-metadata.dat gives the class and field layout outright. A pet is a
MonsterController exactly like a monster is -- the difference is that its
SummoningComponent._Summoner is set. See the offsets below. What follows is the
history of getting there, kept because it says which roads are closed.

READ THIS BEFORE EXTENDING THE SCANNING. The player position is found reliably
(--track, fit 0.002-0.03 on a quiet map). The entity list is not, and scanning
harder will not get it: 226,000 Vec3s sit within a 400-unit square around the
character, one every half metre, so any target position matches something --
both signs of the fitted transform matched ~30 of 30 monster dots with a median
gap of 0.4 units. Killing a monster narrows it to a few dozen freed addresses,
but those are its death effects, one position repeated across a churning pool.

The game answers this itself. global-metadata.dat next to the exe carries the
IL2CPP class and field names, and already contains IsMonster, IsMonsterNotSummon,
MonsterId, PetId, OwnerId and EntityType -- IsMonsterNotSummon being exactly the
monster-versus-pet distinction all of this set out to recover. Il2CppDumper turns
that file plus GameAssembly.dll into class definitions with byte offsets, which
replaces every heuristic here with a field read. Do that before writing more
scanning code.

Separately, and useful without any of this: the minimap frame is rotated relative
to the stick frame, and the rotation is per map (0 degrees on one, 90 on another).
minimap_bot feeds minimap deltas straight to the stick, so on a rotated map every
heading it takes is wrong. correlate() measures that rotation in about 15 seconds.

deps: none beyond the stdlib. ctypes talks to the Win32 API directly.

usage:
  python memscan.py --survey     # how much scannable memory the game has
  python memscan.py --track      # hunt, then rank against the minimap
  python memscan.py --hunt       # iterative walk/stand scan only
  python memscan.py --findpos    # older one-shot scan, kept for reference
  python memscan.py --check 1A2B3C4 ...   # judge addresses found in Cheat Engine
  python memscan.py --units [addr]        # monsters vs pets vs you
  python memscan.py --ids                 # MonsterId counts; summons are singletons
  python memscan.py --entities [addr]     # older, superseded by --units
  python memscan.py --pos        # live position, once POSITION_CHAIN is set
  python memscan.py --demo       # offline self-check, no game needed
"""
import ctypes
import json
import os
import struct
import sys

import numpy as np
from ctypes import wintypes

PROCESS_NAME = "SpiritVale.exe"

# Field offsets from Il2CppDumper (GameAssembly.dll + global-metadata.dat,
# metadata version 31). These are what the whole scanning detour was trying to
# reconstruct, and they answer it outright.
#
# Every creature is a BaseUnitController. It has exactly two subclasses,
# MonsterController and PlayerController -- so a pet is a MonsterController too,
# which is why no amount of looking at red pixels could ever separate them, and
# why the game itself carries a helper called IsMonsterNotSummon.
#
# What separates them is the summoner. Each unit owns a SummoningComponent, and
# that component's _Summoner points at whoever summoned the unit: null for a real
# monster, set for a pet. The player's own pets are also listed directly in their
# own component's ActiveSummons.
UNIT_POSITION = 0x190        # BaseUnitController._lastValidPosition, Vector3
UNIT_SUMMONING = 0x148       # BaseUnitController.Summoning -> SummoningComponent
SUMMONING_SUMMONER = 0x140   # SummoningComponent._Summoner -> BaseUnitController
SUMMONING_ACTIVE = 0x118     # SummoningComponent.ActiveSummons -> List<Monster>
MONSTER_ID = 0x218           # MonsterController.MonsterId, string
MONSTER_SPAWNER = 0x288      # MonsterController.Spawner
# Summoned units are MonsterControllers with a MonsterId like any other, and
# nothing structural separates them: measured on a live map, all 38 targetable
# monsters had a null Spawner *and* a null summoner, so neither field can be the
# test. What does separate them is the id itself -- real spawns come in numbers
# (32 Zombie Goblin Soldier, 18 Zombie Goblin Minion, 17 Monster Bat) and a
# summon is a singleton. Deny by name; add to the set when one turns up.
MONSTER_DENY = {"skeleton mage", "seraphim arbiter", "skeleton", "abomination", "wraith king"}
# Pets carry the game's own naming, e.g. 'Pet_Earth', and they reach the target
# list for the same reason: their summoner field reads null, so the pet test
# built on it never fires for them.
MONSTER_DENY_PREFIX = ("pet_",)
# The unit list holds every monster the client knows about, and most of them are
# not there to be fought: pooled or despawned objects keep their last position
# and get their health reset to full, so they look exactly like a healthy
# monster standing still. Measured on a live map: 516 monster entries, 468 with
# a position that had not changed in 2 seconds, and the bot swinging at one of
# them 1.3 units away. Two fields tell them apart -- a pooled object is not
# rendered, and a corpse has no health. Filtering on both took 161 entries
# within 98 units down to 20, against 13 red dots on the minimap.
UNIT_VISIBLE = 0x18D         # BaseUnitController.IsVisible, bool
UNIT_HEALTH = 0x128          # BaseUnitController.Health -> HealthComponent
HEALTH_CURRENT = 0x138       # HealthComponent._health, int

# Where each class's Il2CppClass pointer is kept, as an offset into
# GameAssembly.dll. From Il2CppDumper's script.json, the *_TypeInfo entries.
# These move with every patch -- re-dump and update them. Everything else in this
# file is a search; these three lines are what make it a lookup instead.
TYPE_RVA = dict(monster=0x5D08E50, player=0x5C60880, summoning=0x5CC1C70)
# What each of those slots is supposed to contain. An Il2CppClass carries its own
# name, so the RVAs can be checked rather than trusted -- and rediscovered from
# these names when a patch moves them, which is what makes an update survivable
# without a re-dump. See find_classes().
CLASS_NAMES = dict(monster="MonsterController", player="PlayerController",
                   summoning="SummoningComponent")
CLASS_NAME_OFF = 0x10        # Il2CppClass.name, char*
RVA_CACHE = "il2cpp_rva.json"  # rediscovered slots, so it is slow only once
# Updated for the build of 2026-08-11 15:59. The previous values were
# 0x5D6F750 / 0x5D973D8 / 0x5DF95F0 and a patch moved every one of them, which
# is what "memory targeting unavailable" means in practice. The field offsets
# above did NOT move across that patch -- only these three lines needed redoing.

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
PAGE_NOACCESS = 0x01
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
        self._bufs = {}          # size -> reusable read buffer
        self._got = ctypes.c_size_t()

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

    def readable_regions(self):
        """Every committed readable region, images included.

        Wider than regions(): class metadata and the name strings behind it are
        read-only, so a writable-only sweep cannot see them. Only used by the
        one-off class rediscovery, never per frame.
        """
        mbi = MEMORY_BASIC_INFORMATION64()
        addr, out = 0, []
        while self.k32.VirtualQueryEx(self.h, ctypes.c_void_p(addr),
                                      ctypes.byref(mbi), ctypes.sizeof(mbi)):
            if (mbi.State == MEM_COMMIT and (mbi.Protect & 0xFF) != PAGE_NOACCESS
                    and not (mbi.Protect & PAGE_GUARD)):
                out.append((mbi.BaseAddress, mbi.RegionSize))
            nxt = mbi.BaseAddress + mbi.RegionSize
            if nxt <= addr or nxt >= 0x7FFFFFFFFFFF:
                break
            addr = nxt
        return out

    def read(self, addr, size):
        """Bytes at addr, or None if the region went away mid-scan."""
        # One buffer per size, reused: the bot reads a few hundred 12-byte
        # positions per frame and allocating a fresh ctypes buffer for each was
        # measurable next to the syscall itself.
        buf = self._bufs.get(size)
        if buf is None:
            buf = self._bufs[size] = ctypes.create_string_buffer(size)
        got = self._got
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
         travelled=None, stayed=None, verbose=True):
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
            if not moving and stayed is not None and not stayed():
                # a monster shoved the character, so it did not stand still and
                # the "must not have moved" filter would delete the real position
                if verbose:
                    print(f"  round {r + 1:2d} stand: pushed around, round skipped")
                continue
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
              legs=None, secs=1.2, verbose=True):
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
        drive(sx, sy, secs)
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
        # A normalised direction vector fits the minimap perfectly -- it IS the
        # heading -- and swamped the rankings until this went in. A real map
        # position sits hundreds of units from the origin and converts at a
        # sane number of world units per pixel.
        here = read_vec3(mem, a)
        if not here or not looks_like_place(here):
            continue
        if max(abs(here[0]), abs(here[2])) < 10.0 or abs(here[1]) > 5000:
            continue
        W = np.array(world[a])              # world travel per leg
        if np.abs(W).max() < 1e-3 or not np.isfinite(W).all():
            continue
        # A majority of headings, not all of them: monsters interrupt the walk,
        # and demanding every leg move threw the real position away on a busy map.
        moved_legs = int((np.abs(W).max(axis=1) >= 1e-3).sum())
        if moved_legs < max(4, int(0.7 * len(M))):
            continue
        A, *_ = np.linalg.lstsq(M, W, rcond=None)
        resid = float(np.abs(W - M @ A).sum() / max(np.abs(W).sum(), 1e-9))
        # a rotation-and-scale has orthogonal columns of equal length; anything
        # else is a coincidence that happens to fit these particular legs
        c0, c1 = A[:, 0], A[:, 1]
        ortho = abs(float(c0 @ c1)) / max(float(np.linalg.norm(c0) *
                                                np.linalg.norm(c1)), 1e-9)
        even = abs(float(np.linalg.norm(c0) - np.linalg.norm(c1))) / \
            max(float(np.linalg.norm(c0)), 1e-9)
        scale = float(np.linalg.norm(c0))
        if not 0.05 <= scale <= 3.0:
            continue        # a plausible minimap pixel is a fraction of a metre
        scored.append((resid + ortho + even, resid, ortho, even, a, scale))
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


def track(rounds=10, secs=0.7):
    # Short legs on purpose. The map can be small enough that a couple of seconds
    # of walking ends at a wall, and a leg that stops early travels an unknown
    # fraction of what was asked for, which is worse for the fit than a short one.
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
                               wake=lambda: bot.wake_controller(pad), secs=secs)
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


def instances_of(mem, class_ptr, limit=4000, regions=None):
    """Addresses of every object whose header points at `class_ptr`.

    `regions` narrows the search to bases already known to hold instances. A full
    sweep reads ~8 GB and takes about 14 seconds; the units live in a handful of
    heap regions, so repeating the sweep is almost all waste.
    """
    target = np.uint64(class_ptr)
    out = []
    for base, size in (regions if regions is not None else mem.regions()):
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


SUMMONING_CONTROLLER = 0x100   # SummoningComponent.Controller, points back
LIST_ITEMS = 0x10              # IL2CPP List<T>: _items array pointer
LIST_SIZE = 0x18               # IL2CPP List<T>: _size
ARRAY_DATA = 0x20              # IL2CPP array: first element


def read_ptr(mem, addr):
    blob = mem.read(addr, 8)
    if not blob:
        return 0
    p = struct.unpack("<Q", blob)[0]
    return p if 0x10000 < p < 0x7FFFFFFFFFFF else 0


def cs_string(mem, ptr, cap=128):
    """IL2CPP System.String: length at +0x10, UTF-16 chars right after it."""
    if not ptr:
        return None
    blob = mem.read(ptr + 0x10, 4)
    if not blob:
        return None
    n = struct.unpack("<i", blob)[0]
    if not 0 < n < cap:
        return None
    chars = mem.read(ptr + 0x14, n * 2)
    return chars.decode("utf-16-le", "replace") if chars else None


def monster_id(mem, unit):
    """MonsterController.MonsterId, e.g. 'Zombie Goblin Soldier', or None."""
    return cs_string(mem, read_ptr(mem, unit + MONSTER_ID))


def unit_health(mem, unit):
    """Current health, or None. Damage landing is the only proof of a hit."""
    health = read_ptr(mem, unit + UNIT_HEALTH)
    if not health:
        return None
    blob = mem.read(health + HEALTH_CURRENT, 4)
    return struct.unpack("<i", blob)[0] if blob else None


def worth_fighting(mem, unit):
    """Is this unit actually there to be fought?

    Rendered and with health left. Pooled and despawned monsters keep their
    last position and a full health bar, so distance and health alone cannot
    tell them from a live one standing still -- being drawn is what separates
    them.
    """
    blob = mem.read(unit + UNIT_VISIBLE, 1)
    if not blob or not blob[0]:
        return False
    health = read_ptr(mem, unit + UNIT_HEALTH)
    if not health:
        return False
    blob = mem.read(health + HEALTH_CURRENT, 4)
    return bool(blob) and struct.unpack("<i", blob)[0] > 0


def real_monster(mem, unit):
    """Is this a spawned monster that can actually be fought?

    worth_fighting() is not enough on its own. Measured on a live map: 232
    monster objects were rendered and had health, but only 32 carried a
    MonsterId -- against 26 red dots on the minimap. The other 200 are
    MonsterController objects with no identity yet; they take no damage, and
    because they sit within melee range the bot stood on one swinging until the
    give-up timer fired, then started on the next of them. From the outside that
    is a bot that will not move and walks back if you drag it away.

    The id is also the only thing that separates another player's summon from a
    spawned monster -- see MONSTER_DENY.
    """
    if not worth_fighting(mem, unit):
        return False
    name = monster_id(mem, unit)
    if not name:
        return False
    name = name.strip().lower()
    return name not in MONSTER_DENY and not name.startswith(MONSTER_DENY_PREFIX)


def unit_at(mem, obj):
    """True if `obj` really is a BaseUnitController.

    Checked by going out to its SummoningComponent and back: the component's
    Controller field points at its owner, so obj -> Summoning -> Controller == obj
    is a round trip that random memory does not survive.
    """
    comp = read_ptr(mem, obj + UNIT_SUMMONING)
    return bool(comp) and read_ptr(mem, comp + SUMMONING_CONTROLLER) == obj


def list_items(mem, lst, cap=64):
    """Elements of an IL2CPP List<T> of references."""
    if not lst:
        return []
    arr = read_ptr(mem, lst + LIST_ITEMS)
    size = mem.read(lst + LIST_SIZE, 4)
    if not arr or not size:
        return []
    n = min(struct.unpack("<i", size)[0], cap)
    out = []
    for i in range(max(n, 0)):
        p = read_ptr(mem, arr + ARRAY_DATA + i * 8)
        if p:
            out.append(p)
    return out


def summoner_of(mem, unit):
    """The unit that summoned this one, or 0 if it was not summoned."""
    comp = read_ptr(mem, unit + UNIT_SUMMONING)
    return read_ptr(mem, comp + SUMMONING_SUMMONER) if comp else 0


def units_like(mem, sample, limit=4000):
    """Every object sharing `sample`'s class that passes the round-trip check."""
    cls = read_ptr(mem, sample)
    if not cls:
        return []
    return [o for o in instances_of(mem, cls, limit) if unit_at(mem, o)]


def looks_like_class(mem, ptr):
    """Rough check that `ptr` is an Il2CppClass and not an uninitialised slot.

    A class is heap-allocated below module space and starts with a pointer to its
    Il2CppImage. Slots read as garbage before their class is initialised -- at a
    login or loading screen, one read as 0x1E0000 -- and scanning for a garbage
    value matches unrelated memory and invents units out of it.
    """
    if not (0x10000 < ptr < 0x7FF000000000):
        return False
    first = read_ptr(mem, ptr)
    return bool(first) and bool(mem.read(first, 8))


def class_name(mem, ptr):
    """The name an Il2CppClass carries, or None if `ptr` is not one."""
    if not looks_like_class(mem, ptr):
        return None
    name_ptr = read_ptr(mem, ptr + CLASS_NAME_OFF)
    if not name_ptr:
        return None
    blob = mem.read(name_ptr, 64)
    if not blob or b"\0" not in blob:
        return None
    text = blob.split(b"\0", 1)[0]
    try:
        return text.decode("ascii")
    except UnicodeDecodeError:
        return None


def rva_cache_path():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), RVA_CACHE)


def load_rva_cache():
    """Slots found by a previous rediscovery, or {}."""
    try:
        with open(rva_cache_path()) as fh:
            got = json.load(fh)
        return {k: int(v) for k, v in got.items() if isinstance(v, int)}
    except (OSError, ValueError, AttributeError):
        return {}


def save_rva_cache(rvas):
    try:
        with open(rva_cache_path(), "w") as fh:
            json.dump(rvas, fh, indent=1)
    except OSError:
        pass                              # a cache that cannot be written is fine


def find_classes(mem, wanted=None, progress=None):
    """Locate Il2CppClass pointers by class NAME, with no dump involved.

    This is the answer to a game patch. The RVAs in TYPE_RVA are positions in
    GameAssembly.dll and every one of them moved the last time the game
    updated; the class names did not, because they are the game's own source
    identifiers. A class stores its name at CLASS_NAME_OFF, so the name can be
    found in memory and then the object pointing at it is the class.

    Takes 2-4 minutes over ~11 GB, so it is a fallback rather than the normal
    path -- and its results are cached as fresh RVAs, making it once per patch.
    """
    wanted = wanted or CLASS_NAMES
    chunk = 1 << 24

    def chunks(regions):
        """Stream the given regions a piece at a time.

        Collecting them into a list first meant holding all 11 GB in this
        process at once, which swapped and never finished. Streaming stays flat
        in memory whatever the game is doing.
        """
        for base, size in regions:
            off = 0
            while off < size:
                blob = mem.read(base + off, min(chunk, size - off))
                if blob:
                    yield base + off, blob
                off += chunk

    # The two passes want different memory, which is most of the speed here.
    # Class names are metadata: mapped read-only, about 500 MB. The class
    # objects are heap: writable, about 11 GB. Searching each pass over
    # everything took 214 s; over the half that can hold what it is looking
    # for, well under half that.
    writable = mem.regions()
    seen = set(writable)
    read_only = [r for r in mem.readable_regions() if r not in seen]

    # pass one: where each class name string sits
    at = {}
    needles = {label: name.encode() + bytes(1) for label, name in wanted.items()}
    scanned = 0
    for where in (read_only, writable):      # cheap half first
        for base, blob in chunks(where):
            scanned += len(blob)
            for label, needle in needles.items():
                start = blob.find(needle)
                while start >= 0:
                    at[base + start] = label
                    start = blob.find(needle, start + 1)
        if len(at) >= len(wanted):
            break                            # every name found; no need to go on
    if progress:
        progress(f"read {scanned >> 20} MB, found {len(at)} name strings")
    if not at:
        return {}

    # pass two: objects whose name field points at one of those strings
    targets = np.array(sorted(at), dtype=np.uint64)
    found = {}
    for base, blob in chunks(writable):
        n = len(blob) // 8
        if not n:
            continue
        words = np.frombuffer(blob[:n * 8], dtype=np.uint64)
        for i in np.nonzero(np.isin(words, targets))[0]:
            off = int(i) * 8
            if off < CLASS_NAME_OFF:
                continue
            cand = base + off - CLASS_NAME_OFF
            label = at.get(int(words[i]))
            if label and class_name(mem, cand) == wanted[label]:
                found.setdefault(label, []).append(cand)

    # The name also appears in reflection data, which looks close enough to a
    # class to pass. The real one is the one objects are actually built from, so
    # let instance count decide: measured 683 against 0 for the impostors.
    out = {}
    for label, cands in found.items():
        if len(cands) == 1:
            out[label] = cands[0]
            continue
        best, most = None, 0
        for ptr in cands:
            n = len(list(instances_of(mem, ptr, limit=200)))
            if n > most:
                best, most = ptr, n
        if best:
            out[label] = best
    return out


def class_slot_rva(mem, ptr, module="GameAssembly.dll", span=0x8000000):
    """The offset into `module` of a slot holding `ptr`, or None.

    Turns a rediscovered class back into the cheap lookup the bot normally
    uses, so the minute-long scan happens once per patch instead of per run.
    """
    base = module_base(mem.pid, module)
    if base is None:
        return None
    needle = struct.pack("<Q", ptr)
    off = 0
    while off < span:
        blob = mem.read(base + off, min(1 << 24, span - off))
        if blob:
            hit = blob.find(needle)
            while hit >= 0:
                if (base + off + hit) % 8 == 0:
                    return off + hit
                hit = blob.find(needle, hit + 1)
        off += 1 << 24
    return None


def type_classes(mem, rvas=None):
    """{'monster': class_ptr, ...} resolved through GameAssembly.dll at runtime.

    The RVAs are positions in the module; ASLR moves the module, so each is read
    from wherever it landed. Every slot is checked against the name the class
    should have -- a patch moves these and the old address then points at
    something else entirely, which used to surface as invented units rather than
    as "the offsets are stale".
    """
    base = module_base(mem.pid, "GameAssembly.dll")
    if base is None:
        return {}
    out = {}
    for name, rva in dict(TYPE_RVA, **(rvas or load_rva_cache())).items():
        ptr = read_ptr(mem, base + rva)
        if class_name(mem, ptr) == CLASS_NAMES.get(name):
            out[name] = ptr
    return out


def unit_regions(mem, cls=None):
    """The regions that actually hold units, for narrowing later sweeps."""
    cls = cls or type_classes(mem)
    hot = set()
    for name in ("monster", "player"):
        if not cls.get(name):
            continue
        for obj in instances_of(mem, cls[name], limit=8000):
            hot.add(obj & ~0xFFFFFF)          # 16 MB granularity
    return [(b, s) for b, s in mem.regions()
            if any(b <= h + 0xFFFFFF and b + s > h for h in hot)]


def world_units(mem, regions=None):
    """[(kind, unit, x, y, z)] for every unit the client is tracking.

    kind is 'monster', 'pet' or 'player'. No searching and no heuristics: the
    class pointers come from the dump, and a unit is a pet exactly when its
    SummoningComponent names a summoner.
    """
    cls = type_classes(mem)
    if not cls.get("monster"):
        return []
    out = []
    for name, kind in (("monster", None), ("player", "player")):
        if not cls.get(name):
            continue        # that class has not been initialised yet
        for obj in instances_of(mem, cls[name], limit=8000, regions=regions):
            if not unit_at(mem, obj):
                continue
            pos = read_vec3(mem, obj + UNIT_POSITION)
            if not pos or not looks_like_place(pos):
                continue
            k = kind or ("pet" if summoner_of(mem, obj) else "monster")
            out.append((k, obj, pos[0], pos[1], pos[2]))
    return out


def my_pets(mem, me):
    """The player's own summons, straight off their SummoningComponent."""
    comp = read_ptr(mem, me + UNIT_SUMMONING)
    return set(list_items(mem, read_ptr(mem, comp + SUMMONING_ACTIVE))) if comp \
        else set()


def classify(mem, player_pos_addr):
    """[(kind, unit, x, y, z)] for every unit sharing a class with a known one.

    kind is 'you', 'your pet', 'pet' or 'monster'. Nothing here is a guess: a
    summoned unit has a summoner and a monster does not, and the player's own
    pets are the ones their SummoningComponent lists.
    """
    me = player_pos_addr - UNIT_POSITION
    if not unit_at(mem, me):
        return None, []
    my_comp = read_ptr(mem, me + UNIT_SUMMONING)
    mine = set(list_items(mem, read_ptr(mem, my_comp + SUMMONING_ACTIVE)))

    seen = {me: "you"}
    for pet in mine:
        seen[pet] = "your pet"
    # Pets are MonsterControllers, so one of ours names the monster class; the
    # player's own class only ever finds other players.
    for sample in list(mine) + [me]:
        for u in units_like(mem, sample):
            seen.setdefault(u, None)

    out = []
    for u, kind in seen.items():
        if kind is None:
            kind = "pet" if summoner_of(mem, u) else "monster"
        p = read_vec3(mem, u + UNIT_POSITION)
        if p:
            out.append((kind, u, p[0], p[1], p[2]))
    return me, out


def show_ids():
    """Count the MonsterIds on the map, so a summon can be told from a spawn.

    A spawned monster comes in numbers and somebody's summon is a singleton;
    reading the ids is how MONSTER_DENY gets filled in, rather than by guessing
    which of the units on the screen is a pet.
    """
    with Mem() as mem:
        names = {}
        for kind, u, *_ in world_units(mem):
            if kind != "monster" or not worth_fighting(mem, u):
                continue
            name = monster_id(mem, u)
            if name:
                names[name] = names.get(name, 0) + 1
        if not names:
            print("no identified monsters -- see --units")
            return
        print(f"{sum(names.values())} monsters, {len(names)} distinct ids")
        for name, n in sorted(names.items(), key=lambda kv: -kv[1]):
            low = name.strip().lower()
            deny = "" if (low not in MONSTER_DENY
                          and not low.startswith(MONSTER_DENY_PREFIX)) else "  DENIED"
            print(f"  {n:4d}  {name!r}{deny}")


def show_units(pos_addr=None):
    """List every unit, split into monsters, pets and players.

    Needs no position hunt: the classes are looked up in the module. A position
    address is only used, when given, to say which unit is you and which pets
    are yours.
    """
    with Mem() as mem:
        units = world_units(mem)
        if not units:
            print("no units -- are the TYPE_RVA offsets still right after the "
                  "patch? re-run Il2CppDumper and update them")
            return
        me = pos_addr - UNIT_POSITION if pos_addr else None
        if me is not None and not unit_at(mem, me):
            print(f"0x{pos_addr:X} is not inside a unit; listing without 'you'")
            me = None
        mine = my_pets(mem, me) if me else set()

        counts = {}
        for k, *_ in units:
            counts[k] = counts.get(k, 0) + 1
        print(", ".join(f"{v} {k}" for k, v in sorted(counts.items())))
        if me is None:
            for k, u, x, y, z in units[:20]:
                print(f"  {k:8} 0x{u:012X}  ({x:8.1f},{y:6.1f},{z:8.1f})")
            return

        p = read_vec3(mem, me + UNIT_POSITION)
        px, py, pz = p
        print(f"you: 0x{me:012X} at ({px:.1f},{py:.1f},{pz:.1f}), "
              f"{len(mine)} pet(s) of your own\n")
        rows = sorted((((x - px) ** 2 + (z - pz) ** 2) ** 0.5, k, u, x, y, z)
                      for k, u, x, y, z in units if u != me)
        for d, k, u, x, y, z in rows[:25]:
            label = "YOUR PET" if u in mine else k
            print(f"  {label:8} 0x{u:012X} dist {d:6.1f}  "
                  f"({x:8.1f},{y:6.1f},{z:8.1f})")


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


    # unit classification, against a fake heap laid out like the real one
    heap = {}

    def put(addr, val):
        heap[addr] = struct.pack("<Q", val)

    # above read_ptr's validity floor, or the round trip reads as invalid
    ME, PET, MON, OTHER = 0x110000, 0x120000, 0x130000, 0x140000
    COMPS = {ME: 0x111000, PET: 0x121000, MON: 0x131000, OTHER: 0x141000}
    for unit, comp in COMPS.items():
        put(unit + UNIT_SUMMONING, comp)
        put(comp + SUMMONING_CONTROLLER, unit)       # the round trip
        put(unit, 0xC1A55)                           # same class for all
    put(COMPS[PET] + SUMMONING_SUMMONER, ME)        # our pet: we summoned it
    put(COMPS[OTHER] + SUMMONING_SUMMONER, 0x900000)  # someone else's pet
    put(COMPS[MON] + SUMMONING_SUMMONER, 0)         # a monster has no summoner
    put(COMPS[ME] + SUMMONING_ACTIVE, 0x150000)     # our ActiveSummons list
    put(0x150000 + LIST_ITEMS, 0x160000)
    heap[0x150000 + LIST_SIZE] = struct.pack("<i", 1)
    put(0x160000 + ARRAY_DATA, PET)

    class UnitMem:
        pid = 0

        def read(self, addr, size):
            if size == 12:
                base = {ME: 1.0, PET: 2.0, MON: 3.0, OTHER: 4.0}
                for u, v in base.items():
                    if addr == u + UNIT_POSITION:
                        return struct.pack("<fff", v, 10.0, v)
                return None
            return heap.get(addr, struct.pack("<Q", 0))[:size]

        def regions(self):
            return []

    um = UnitMem()
    # Another player's summon is a MonsterController with an ordinary MonsterId
    # and no summoner, so only the name tells it from a spawn. Chasing one means
    # following its owner around the map instead of farming.
    class NameMem:
        pid = 0
        HEALTH, TEXT = 0x200000, 0x300000

        def __init__(self, name):
            self.name = name

        def read(self, addr, size):
            if addr == 0x10000 + UNIT_VISIBLE:
                return b"\x01"
            if addr == 0x10000 + UNIT_HEALTH:
                return struct.pack("<Q", self.HEALTH)
            if addr == self.HEALTH + HEALTH_CURRENT:
                return struct.pack("<i", 1000)
            if addr == 0x10000 + MONSTER_ID:
                return struct.pack("<Q", self.TEXT if self.name else 0)
            if addr == self.TEXT + 0x10:
                return struct.pack("<i", len(self.name))
            if addr == self.TEXT + 0x14:
                return self.name.encode("utf-16-le")
            return bytes(size)

    spawned = NameMem("Zombie Goblin Soldier")
    assert monster_id(spawned, 0x10000) == "Zombie Goblin Soldier"
    assert real_monster(spawned, 0x10000)
    for denied in ("Skeleton Mage", "skeleton mage", " Skeleton Mage "):
        assert not real_monster(NameMem(denied), 0x10000), denied
    assert not real_monster(NameMem(""), 0x10000), "no id, cannot be damaged"
    assert not real_monster(NameMem("Pet_Earth"), 0x10000), "a pet is not a target"

    assert unit_at(um, ME) and unit_at(um, MON)
    assert not unit_at(um, 0x999000)                # no round trip, not a unit
    assert list_items(um, 0x150000) == [PET]
    assert summoner_of(um, PET) == ME and summoner_of(um, MON) == 0
    me_obj, rows = classify(um, ME + UNIT_POSITION)
    kinds = {u: k for k, u, *_ in rows}
    assert me_obj == ME and kinds[ME] == "you", kinds
    assert kinds[PET] == "your pet", kinds
    assert kinds.get(MON) in (None, "monster"), kinds

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
    elif "--units" in sys.argv:
        rest = [a for a in sys.argv[sys.argv.index("--units") + 1:]
                if not a.startswith("--")]
        show_units(int(rest[0], 16) if rest else None)
    elif "--ids" in sys.argv:
        show_ids()
    elif "--check" in sys.argv:
        given = [int(a, 16) for a in sys.argv[sys.argv.index("--check") + 1:]
                 if not a.startswith("--")]
        if not given:
            print("usage: python memscan.py --check <hex address> [more...]")
        else:
            check(given)
    else:
        print(__doc__)
