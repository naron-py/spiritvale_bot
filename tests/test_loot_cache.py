"""Loot cache regressions on a byte-addressed heap; no game or pad needed."""
import struct
import threading
import unittest
from unittest.mock import patch

import memscan
import minimap_bot as bot


class LootHeap:
    def __init__(self, count=192):
        self.base = 0x100000
        self.data = bytearray(8 * 1024 * 1024)
        self.cls = self.base + 0x700000
        self.go_cls = self.cls + 0x100
        self.calls = self.bytes = 0
        self.failed = set()
        self.on_read = None
        self.ptr(self.go_cls, self.cls + 0x200)
        self.ptr(self.go_cls + memscan.CLASS_NAME_OFF, self.cls + 0x300)
        self.put(self.cls + 0x300, b"GameObject\0")
        self.drops = [self.base + i * 0x1000 for i in range(count)]
        for drop in self.drops:
            self.ptr(drop, self.cls)
            self.ptr(drop + memscan.LOOT_SYNC, drop + 0x200)
            self.ptr(drop + memscan.LOOT_GO, drop + 0x400)
            self.ptr(drop + 0x400, self.go_cls)
            self.ptr(drop + 0x400 + memscan.GO_NATIVE, drop + 0x500)
            self.ptr(drop + 0x500 + memscan.LOOT_POS_CHAIN[0], drop + 0x600)
            self.ptr(drop + 0x600 + memscan.LOOT_POS_CHAIN[1], drop + 0x700)
            self.set_item(drop, "")

    def put(self, address, data):
        offset = address - self.base
        self.data[offset:offset + len(data)] = data

    def ptr(self, address, value):
        self.put(address, struct.pack("<Q", value))

    def set_item(self, drop, name, position=(10.0, 0.0, 20.0)):
        sync = drop + 0x200
        self.ptr(sync + memscan.LOOT_NAME, drop + 0x800 if name else 0)
        self.ptr(sync + memscan.LOOT_KEY, 0)
        self.put(drop + 0x810, struct.pack("<i", len(name)))
        self.put(drop + 0x814, name.encode("utf-16-le"))
        self.put(drop + 0x700 + memscan.LOOT_POS_CHAIN[-1],
                 struct.pack("<fff", *position))

    def read(self, address, size):
        self.calls += 1
        self.bytes += size
        if self.on_read:
            self.on_read(address, size)
        if address in self.failed:
            return b"\0"  # deliberate short read, not authoritative absence
        offset = address - self.base
        if offset < 0 or offset + size > len(self.data):
            return None
        return bytes(self.data[offset:offset + size])

    def regions(self):
        return [(self.base, len(self.data))]


class LootCacheTests(unittest.TestCase):
    def eyes(self, mem):
        eyes = bot.MemoryEyes.__new__(bot.MemoryEyes)
        eyes.ms, eyes.mem = memscan, mem
        eyes.classes = {"loot": mem.cls}
        eyes.lock = threading.Lock()
        eyes.generation = 0
        eyes.loot = {}
        eyes.loot_ignored = {}
        eyes.loot_target = None
        eyes.hot_loot = None
        eyes.hot_loot_full_at = 0.0
        eyes.last_pos = (10.0, 20.0)
        return eyes

    def test_discovery_retains_empty_slots_but_publishes_only_named_loot(self):
        mem = LootHeap(3)
        mem.set_item(mem.drops[1], "Flax")
        slots = {}
        rows = memscan.world_loot(mem, mem.cls, slots=slots)
        self.assertEqual(set(slots), set(mem.drops))
        self.assertEqual(rows, [(mem.drops[1], 10.0, 0.0, 20.0, "Flax")])
        self.assertEqual(set(slots.values()), {(mem.cls, mem.go_cls)})

    def test_cached_slot_observes_activation_movement_and_empty_without_scan(self):
        mem = LootHeap(1)
        drop = mem.drops[0]
        with patch.object(memscan, "instances_of", side_effect=AssertionError("heap scan")):
            self.assertEqual(memscan.loot_slot(mem, drop, mem.cls), ())
            mem.set_item(drop, "Flax")
            self.assertEqual(memscan.loot_slot(mem, drop, mem.cls),
                             (10.0, 0.0, 20.0, "Flax"))
            mem.set_item(drop, "Bee Card", (25.0, 0.0, 30.0))
            self.assertEqual(memscan.loot_slot(mem, drop, mem.cls),
                             (25.0, 0.0, 30.0, "Bee Card"))
            mem.set_item(drop, "")
            self.assertEqual(memscan.loot_slot(mem, drop, mem.cls), ())


    def test_bot_refreshes_a_pooled_activation_between_background_sweeps(self):
        mem = LootHeap(3)
        eyes = self.eyes(mem)
        eyes._sweep_loot(mem)
        self.assertEqual(eyes.loot, {})
        mem.set_item(mem.drops[0], "Flax")
        with patch.object(memscan, "world_loot", side_effect=AssertionError("heap scan")):
            eyes._refresh_loot(now=1.0)
        self.assertEqual(eyes.loot[mem.drops[0]], (10.0, 0.0, 20.0, "Flax"))


    def test_real_loot_consumers_refresh_before_using_cached_rows(self):
        for consumer in ("loot_here", "pick_loot"):
            with self.subTest(consumer=consumer):
                mem = LootHeap(1)
                eyes = self.eyes(mem)
                eyes.me = 0x90000
                eyes.basis = ((1.0, 0.0), (0.0, 1.0))
                eyes._positions = lambda addrs: {eyes.me: (10.0, 0.0, 20.0)}
                eyes._sweep_loot(mem)
                mem.set_item(mem.drops[0], "Flax")
                with patch.object(bot, "LOOT_NAMES", ()):
                    result = (eyes.loot_here() if consumer == "loot_here"
                              else eyes.pick_loot(1.0))
                self.assertTrue(result if consumer == "loot_here"
                                else result[0] is not None)
                self.assertEqual(eyes.loot_name, "Flax")

    def test_slow_sweep_cannot_overwrite_a_newer_cached_slot_read(self):
        mem = LootHeap(1)
        drop = mem.drops[0]
        mem.set_item(drop, "Flax")
        eyes = self.eyes(mem)
        eyes._sweep_loot(mem)
        original = memscan.world_loot

        def slow_scan(*args, **kwargs):
            rows = original(*args, **kwargs)
            mem.set_item(drop, "")
            eyes._refresh_loot(now=1.0)
            return rows

        with patch.object(memscan, "world_loot", side_effect=slow_scan):
            eyes._sweep_loot(mem)
        self.assertNotIn(drop, eyes.loot)

    def test_sweep_without_explicit_generation_rejects_session_change(self):
        mem = LootHeap(1)
        eyes = self.eyes(mem)
        eyes.walk = None
        original = memscan.world_loot

        def relog(*args, **kwargs):
            rows = original(*args, **kwargs)
            eyes.reset_session()
            return rows

        with patch.object(memscan, "world_loot", side_effect=relog):
            eyes._sweep_loot(mem)
        self.assertEqual(eyes.loot_slots, ())
        self.assertEqual(eyes.hot_loot_full_at, 0.0)

    def test_empty_pool_still_narrows_future_discovery(self):
        mem = LootHeap(1)
        eyes = self.eyes(mem)
        eyes._sweep_loot(mem)
        self.assertEqual(eyes.hot_loot, mem.regions())


    def test_refresh_reuses_verified_static_class_metadata(self):
        mem = LootHeap(1)
        eyes = self.eyes(mem)
        eyes._sweep_loot(mem)
        mem.set_item(mem.drops[0], "Flax")
        with patch.object(memscan, "class_name", side_effect=AssertionError("static name read")):
            eyes._refresh_loot(now=1.0)
        self.assertIn(mem.drops[0], eyes.loot)


    def test_short_reads_and_wrong_classes_suppress_only_the_bad_slot(self):
        mem = LootHeap(2)
        eyes = self.eyes(mem)
        bad, good = mem.drops
        for drop in mem.drops:
            mem.set_item(drop, "Flax")
        eyes._sweep_loot(mem)
        key = eyes._loot_key(bad, 10.0, 20.0, "Flax")
        eyes.loot_ignored[key] = 100.0
        for index, address in enumerate((bad, bad + memscan.LOOT_SYNC,
                                        bad + 0x200 + memscan.LOOT_NAME,
                                        bad + 0x810, bad + 0x814,
                                        bad + 0x700 + memscan.LOOT_POS_CHAIN[-1])):
            mem.failed = {address}
            eyes._refresh_loot(now=1.0 + index)
            self.assertNotIn(bad, eyes.loot)
            self.assertIn(good, eyes.loot)
            self.assertIn(key, eyes.loot_ignored)
        mem.failed.clear()
        mem.ptr(bad, mem.go_cls)
        eyes._refresh_loot(now=10.0)
        self.assertNotIn(bad, eyes.loot)
        mem.ptr(bad, mem.cls)
        mem.ptr(bad + 0x400, mem.cls)
        eyes._refresh_loot(now=11.0)
        self.assertNotIn(bad, eyes.loot)
        self.assertIn(good, eyes.loot)
        self.assertEqual(len(eyes.loot_slots), 2)

    def test_confirmed_empty_retires_blacklist_even_after_an_unknown_read(self):
        mem = LootHeap(1)
        drop = mem.drops[0]
        mem.set_item(drop, "Flax")
        eyes = self.eyes(mem)
        eyes._sweep_loot(mem)
        key = eyes._loot_key(drop, 10.0, 20.0, "Flax")
        eyes.loot_ignored[key] = 100.0
        mem.failed.add(drop)
        eyes._refresh_loot(now=1.0)
        self.assertIn(key, eyes.loot_ignored)
        mem.failed.clear()
        mem.set_item(drop, "")
        eyes._refresh_loot(now=2.0)
        self.assertNotIn(key, eyes.loot_ignored)
        mem.set_item(drop, "Flax")
        eyes._refresh_loot(now=3.0)
        self.assertIn(drop, eyes.loot)

    def test_name_pointer_reuse_during_read_is_rejected(self):
        mem = LootHeap(1)
        drop = mem.drops[0]
        mem.set_item(drop, "Flax")

        def reuse(address, size):
            if address == drop + 0x700 + memscan.LOOT_POS_CHAIN[-1]:
                mem.set_item(drop, "Bee Card")

        mem.on_read = reuse
        self.assertIsNone(memscan.loot_slot(mem, drop, mem.cls, mem.go_cls))

    def test_refresh_session_race_cannot_republish_old_slots(self):
        mem = LootHeap(1)
        eyes = self.eyes(mem)
        eyes.walk = None
        eyes._sweep_loot(mem)
        mem.set_item(mem.drops[0], "Flax")

        def reset(address, size):
            mem.on_read = None
            eyes.reset_session()

        mem.on_read = reset
        eyes._refresh_loot(now=1.0)
        self.assertEqual(eyes.loot, {})
        self.assertEqual(eyes.loot_slots, ())
        self.assertEqual(eyes.loot_read_at, {})

    def test_frame_slice_has_a_count_cap_fair_cursor_and_shared_throttle(self):
        mem = LootHeap(70)
        eyes = self.eyes(mem)
        eyes._sweep_loot(mem)
        for drop in mem.drops:
            mem.set_item(drop, "Flax")
        with patch.object(bot, "LOOT_REFRESH_BUDGET_S", 10.0):
            eyes._refresh_loot(now=1.0)
            self.assertEqual(len(eyes.loot), bot.LOOT_REFRESH_MAX)
            calls = mem.calls
            eyes._refresh_loot(now=1.01)
            self.assertEqual(mem.calls, calls)
            eyes._refresh_loot(now=1.1)
            eyes._refresh_loot(now=1.2)
        self.assertEqual(set(eyes.loot), set(mem.drops))

    def test_time_budget_yields_after_one_slow_slot(self):
        mem = LootHeap(3)
        eyes = self.eyes(mem)
        eyes._sweep_loot(mem)
        with patch.object(bot.time, "perf_counter", side_effect=(0.0, 0.002)):
            eyes._refresh_loot(now=1.0)
        self.assertEqual(eyes.loot_cursor, 1)

    def test_disabled_or_obsolete_cache_performs_no_reads(self):
        mem = LootHeap(1)
        eyes = self.eyes(mem)
        eyes._sweep_loot(mem)
        before = mem.calls
        with patch.object(bot, "LOOT_PICKUP", False):
            eyes._refresh_loot(now=1.0)
        eyes.generation += 1
        eyes._refresh_loot(now=2.0)
        self.assertEqual(mem.calls, before)


    def test_nonnull_empty_string_is_confirmed_empty_not_a_read_failure(self):
        mem = LootHeap(1)
        drop = mem.drops[0]
        mem.ptr(drop + 0x200 + memscan.LOOT_NAME, drop + 0x800)
        self.assertEqual(memscan.loot_slot(mem, drop, mem.cls, mem.go_cls), ())


if __name__ == "__main__":
    unittest.main()
