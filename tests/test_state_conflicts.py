import threading
import unittest
from unittest.mock import patch

import memscan
from minimap_bot import (LOOT_MAX_S, MEM_LOST_FRAMES, Area, MemoryEyes, WalkMap,
                         area_holds, loot_wins, should_calibrate, stale_target)


class _FightState:
    def __init__(self):
        self.ids = {}

    def monster_target_state(self, mem, unit):
        return True, False

    def network_object_id(self, mem, unit):
        return self.ids.get(unit)


class _TargetEyes(MemoryEyes):
    def __init__(self, monsters):
        self.me = 0x1000
        self.basis = ((1.0, 0.0), (0.0, 1.0))
        self.units = [("monster", unit, 0.0, 0.0, 0.0)
                      for unit in monsters]
        self.positions = {self.me: (0.0, 0.0, 1.0), **monsters}
        self.chasing = self.chasing_id = None
        self.engaged_since = self.approach = None
        self.ignored = {}
        self.mode = "chasing"
        self.misses = 0
        self.hot = self.hot_at = self.hot_loot = self.hot_loot_at = None
        self.orbit_dir, self.orbit_mark = 1, None
        self.mem = None
        self.seen_at, self.sweep_at, self.fight_ok = {}, 0, {}
        self.ms = _FightState()
        self.lock = threading.Lock()
        self.last_pos = None
        self.escapes, self.wedge_anchor, self.escape = 0, None, None
        self.area = self.walk = None
        self.goal = None
        self.routing = self.sealed = False
        self.target_name = ""
        self.generation = 0

    def _live_positions(self, addrs):
        return {unit: self.positions[unit] for unit in addrs
                if unit in self.positions}

    def _positions(self, addrs):
        return {unit: self.positions[unit] for unit in addrs
                if unit in self.positions}


class PositionFreshnessTests(unittest.TestCase):
    def test_failed_current_player_read_is_not_returned_from_history(self):
        eyes = MemoryEyes.__new__(MemoryEyes)
        eyes.me = 0x1000
        eyes.seen_at = {eyes.me: (12.0, 0.0, 34.0)}
        eyes.sweep_at = 0
        eyes._positions = lambda addrs: {}

        live = eyes._live_positions([eyes.me])

        self.assertNotIn(eyes.me, live)
        self.assertEqual(eyes.seen_at[eyes.me], (12.0, 0.0, 34.0))

    def test_position_on_world_axis_is_not_mistaken_for_zeroed_memory(self):
        import struct

        eyes = MemoryEyes.__new__(MemoryEyes)
        eyes.mem = type("Mem", (), {"read": lambda self, addr, size:
                                    struct.pack("<fff", 0.0, 1.0, 5.0)})()
        eyes.ms = type("MS", (), {"UNIT_POSITION": 0, "POS_MAX": 20000})()

        self.assertEqual(eyes._positions([0x1000]), {0x1000: (0.0, 1.0, 5.0)})

    def test_repeated_unreadable_player_never_actuates_and_tears_down(self):
        eyes = _TargetEyes({0x20000: (10.0, 0.0, 0.0)})
        eyes._live_positions = lambda addrs: {
            0x20000: eyes.positions[0x20000]
        }

        outputs = [eyes.target(float(frame))
                   for frame in range(MEM_LOST_FRAMES)]

        self.assertTrue(all(output == (None, None, None) for output in outputs))
        self.assertIsNone(eyes.me)
        self.assertIsNone(eyes.basis)


class AreaFailClosedTests(unittest.TestCase):
    def test_requested_area_holds_when_memory_construction_failed(self):
        requested = Area("pen", circle=(0.0, 0.0, 20.0))

        self.assertTrue(area_holds(None, requested))

    def test_scanner_wait_inside_area_temporarily_hands_control_to_pixels(self):
        requested = Area("pen", circle=(0.0, 0.0, 20.0))
        eyes = type("Eyes", (), {"area": requested, "mode": "no unit"})()

        self.assertFalse(area_holds(eyes, requested))
        eyes.mode = "no monster"
        self.assertFalse(area_holds(eyes, requested))

    def test_real_area_stop_state_still_holds_pixels(self):
        requested = Area("pen", circle=(0.0, 0.0, 20.0))
        eyes = type("Eyes", (), {"area": requested, "mode": "lost"})()

        self.assertTrue(area_holds(eyes, requested))


class TargetOwnershipTests(unittest.TestCase):
    def test_late_stable_id_is_latched_without_releasing_held_pointer(self):
        eyes = _TargetEyes({0x20000: (10.0, 0.0, 0.0)})
        eyes.target(1.0)
        self.assertIsNone(eyes.chasing_id)

        eyes.ms.ids[0x20000] = 77
        eyes.target(2.0)

        self.assertEqual(eyes.chasing, 0x20000)
        self.assertEqual(eyes.chasing_id, 77)

    def test_held_target_switches_when_another_is_clearly_nearer(self):
        eyes = _TargetEyes({0x2000: (60.0, 0.0, 1.0)})
        eyes.ms.ids[0x2000] = 20
        eyes.target(1.0)
        eyes.units.append(("monster", 0x3000, 0.0, 0.0, 0.0))
        eyes.positions[0x3000] = (5.0, 0.0, 1.0)
        eyes.ms.ids[0x3000] = 30

        eyes.target(1.1)

        self.assertEqual(eyes.chasing, 0x3000)

    def test_far_target_is_held_across_small_nearest_order_change(self):
        eyes = _TargetEyes({0x2000: (80.0, 0.0, 1.0),
                            0x3000: (81.0, 0.0, 1.0)})
        eyes.ms.ids.update({0x2000: 20, 0x3000: 30})
        eyes.target(1.0)
        started = eyes.engaged_since
        eyes.positions[0x2000] = (81.0, 0.0, 1.0)
        eyes.positions[0x3000] = (80.0, 0.0, 1.0)

        eyes.target(1.1)

        self.assertEqual(eyes.chasing, 0x2000)
        self.assertEqual(eyes.engaged_since, started)

    def test_ignore_follows_stable_id_across_wrapper_change(self):
        eyes = _TargetEyes({0x2000: (10.0, 0.0, 1.0)})
        eyes.ms.ids[0x2000] = 77
        eyes.target(1.0)
        eyes.target(10.0)
        eyes.units = [("monster", 0x3000, 0.0, 0.0, 0.0)]
        eyes.positions = {eyes.me: (0.0, 0.0, 1.0),
                          0x3000: (10.0, 0.0, 1.0)}
        eyes.ms.ids[0x3000] = 77

        result = eyes.target(10.1)

        self.assertEqual(result, (None, None, None))
        self.assertIsNone(eyes.chasing)

    def test_reused_pointer_with_new_stable_id_does_not_inherit_blacklist(self):
        eyes = _TargetEyes({0x20000: (10.0, 0.0, 1.0)})
        eyes.ms.ids[0x20000] = 77
        eyes._ignore_target(0x20000, 20.0, 77)

        eyes.ms.ids[0x20000] = 88

        self.assertFalse(eyes._target_ignored(0x20000, 10.0))
        result = eyes.target(10.0)
        self.assertIsNotNone(result[0])
        self.assertEqual(eyes.chasing, 0x20000)


class MovementOwnershipTests(unittest.TestCase):
    def test_guard_output_is_safe_after_direction_normalization(self):
        eyes = MemoryEyes.__new__(MemoryEyes)
        eyes.area = Area("ring", circle=(0.0, 0.0, 10.0))
        eyes.last_pos = (6.5, 0.0)
        eyes.basis = ((1.0, 0.0), (0.0, 1.0))
        eyes.mode = "chasing"
        eyes.boundary_log_at = 100.0

        sx, sy, blocked = eyes.guard_area_step(1.0, 0.0, now=1.0)
        dx, dz = sx * 3.0, sy * 3.0

        self.assertTrue(blocked)
        self.assertTrue(eyes.area.guard_step(eyes.last_pos,
                                             (6.5 + dx, dz))[0])

    def test_safe_edge_unwedge_keeps_a_legal_escape(self):
        eyes = MemoryEyes.__new__(MemoryEyes)
        eyes.area = Area("ring", circle=(0.0, 0.0, 10.0))
        eyes.last_pos = (7.0, 0.0)
        eyes.basis = ((1.0, 0.0), (0.0, 1.0))
        eyes.mode = "unwedge"
        eyes.boundary_log_at = 100.0

        sx, sy, blocked = eyes.guard_area_step(1.0, 0.0, now=1.0)

        self.assertTrue(blocked)
        self.assertGreater(abs(sx) + abs(sy), 0.1)
        self.assertTrue(eyes.area.guard_step(
            eyes.last_pos, (7.0 + sx * 3.0, sy * 3.0))[0])

    def test_redirected_loot_is_observed_against_guard_goal(self):
        class Walk:
            def __init__(self):
                self.goal = None

            def observe(self, now, px, pz, sx, sy, basis, mode, goal):
                self.goal = goal
                return None

        eyes = MemoryEyes.__new__(MemoryEyes)
        eyes.area = Area("ring", circle=(0.0, 0.0, 10.0))
        eyes.last_pos = (6.5, 0.0)
        eyes.basis = ((1.0, 0.0), (0.0, 1.0))
        eyes.mode, eyes.loot_mode = "chasing", "loot"
        eyes.goal, eyes.loot_goal = None, (10.0, 0.0)
        eyes.walk = Walk()
        eyes.boundary_log_at = 100.0

        sx, sy, _ = eyes.guard_area_step(1.0, 0.0, now=1.0)
        eyes.observe_move(1.0, sx, sy, on_loot=True)

        self.assertEqual(eyes.walk.goal, eyes.guard_goal)

    def test_exact_boundary_target_engages_without_outward_motion(self):
        eyes = _TargetEyes({0x20000: (10.0, 0.0, 0.0)})
        eyes.positions[eyes.me] = (7.0, 0.0, 0.0)
        eyes.last_pos = (7.0, 0.0)
        eyes.area = Area("ring", circle=(0.0, 0.0, 10.0))
        eyes.walk = WalkMap(path="NUL")

        sx, sy, _ = eyes.target(1.0)
        gsx, gsy, _ = eyes.guard_area_step(sx, sy, now=1.0)

        self.assertEqual(eyes.mode, "on it")
        self.assertGreater(abs(gsx) + abs(gsy), 0.1)
        self.assertTrue(eyes.area.guard_step(
            eyes.last_pos, (7.0 + gsx * 3.0, gsy * 3.0))[0])

    def test_boundary_redirect_preserves_return_semantic_mode(self):
        eyes = MemoryEyes.__new__(MemoryEyes)
        eyes.area = Area("ring", circle=(0.0, 0.0, 10.0))
        eyes.last_pos = (14.0, 0.0)
        eyes.basis = ((1.0, 0.0), (0.0, 1.0))
        eyes.goal = (7.0, 0.0)
        eyes.mode = "going back"
        eyes.boundary_log_at = 100.0

        eyes.guard_area_step(-1.0, 0.0, now=1.0)

        self.assertEqual(eyes.mode, "going back")

    def test_return_timeout_keeps_goal_inside_safety_margin(self):
        eyes = MemoryEyes.__new__(MemoryEyes)
        eyes.area = Area("ring", circle=(0.0, 0.0, 10.0))
        eyes.home_goal = (0.0, 0.0)
        eyes.returning_since = 0.0
        eyes.basis = ((1.0, 0.0), (0.0, 1.0))
        eyes.walk = None

        result = eyes._go_home(100.0, 14.0, 0.0)

        self.assertTrue(eyes.area.safe(*eyes.home_goal))
        self.assertLess(result[0], 0.0)

    def test_wrong_map_area_failure_holds_instead_of_disabling_fence(self):
        eyes = _TargetEyes({})
        eyes.area = Area("pen", circle=(0.0, 0.0, 10.0))
        eyes.returning = True
        eyes.home_goal = eyes.area.home(1000.0, 0.0)
        eyes.returning_since = 0.0

        sx, sy, _ = eyes._go_home(1.0, 1000.0, 0.0)

        self.assertIsNotNone(eyes.area)
        self.assertEqual((sx, sy), (0.0, 0.0))
        self.assertEqual(eyes.mode, "no area")

    def test_tiny_circle_home_is_still_safe(self):
        area = Area("tiny", circle=(0.0, 0.0, 2.0))

        home = area.home(4.0, 0.0)

        self.assertTrue(area.safe(*home))

    def test_legacy_mask_unsafe_start_cannot_move_farther_out(self):
        area = Area("mask", cells={(x, z) for x in range(5) for z in range(5)})

        allowed, _ = area.guard_step((1.5, 1.5), (-1.5, 1.5))

        self.assertFalse(allowed)

    def test_legacy_mask_rejects_segment_crossing_unsafe_cell(self):
        cells = set()
        for cell in ((4, 2), (3, 1)):
            for dx in (-1, 0, 1):
                for dz in (-1, 0, 1):
                    cells.add((cell[0] + dx, cell[1] + dz))
        area = Area("mask", cells=cells)
        current = (13.776271535918575, 7.283665229466127)
        proposed = (11.874246510950705, 4.963686607927563)

        allowed, _ = area.guard_step(current, proposed)

        self.assertFalse(allowed)

    def test_wander_movement_starts_wall_observation(self):
        walk = WalkMap(path="NUL")

        walk.observe(1.0, 5.0, 5.0, 1.0, 0.0,
                     ((1.0, 0.0), (0.0, 1.0)), "wander", (10.0, 5.0))

        self.assertEqual(walk.last_pos, (5.0, 5.0))

    def test_area_aware_route_never_returns_unsafe_waypoint(self):
        area = Area("pen", polygon=((0.0, 0.0), (20.0, 0.0),
                                    (20.0, 10.0), (0.0, 10.0)))
        walk = WalkMap(path="NUL")
        for z in range(-2, 8):
            walk.hits[(6, z)] = [10, 1.0]

        path = walk.route(4.0, 5.0, 16.0, 5.0, 2.0,
                          allowed=lambda x, z: area.safe(x, z))

        self.assertTrue(path is None or all(area.safe(x, z) for x, z in path))

    def test_area_notch_triggers_route_without_a_learned_wall(self):
        area = Area("u", polygon=((0, 0), (30, 0), (30, 30), (20, 30),
                                  (20, 10), (10, 10), (10, 30), (0, 30)))
        eyes = MemoryEyes.__new__(MemoryEyes)
        eyes.area = area
        eyes.walk = WalkMap(path="NUL")
        eyes.path = eyes.path_to = None
        eyes.path_at = 0.0

        waypoint = eyes.route_to(1.0, 5.0, 20.0, 25.0, 20.0)

        self.assertTrue(eyes.routing)
        self.assertNotEqual(waypoint, (25.0, 20.0))
        self.assertTrue(area.guard_step((5.0, 20.0), waypoint)[0])

    def test_route_cannot_cut_diagonally_between_two_walls(self):
        walk = WalkMap(path="NUL")
        walk.hits[(1, 0)] = [999, 1.0]
        walk.hits[(0, 1)] = [999, 1.0]

        path = walk.route(0.75, 0.75, 2.25, 2.25, 2.0)

        self.assertIsNotNone(path)
        self.assertNotEqual(path, [(2.25, 2.25)])

    def test_guarded_unwedge_keeps_lateral_escape_component(self):
        eyes = MemoryEyes.__new__(MemoryEyes)
        eyes.area = Area("ring", circle=(0.0, 0.0, 10.0))
        eyes.last_pos = (14.0, 0.0)
        eyes.basis = ((1.0, 0.0), (0.0, 1.0))
        eyes.goal = (7.0, 0.0)
        eyes.mode = "unwedge"
        eyes.boundary_log_at = 100.0

        sx, sy, blocked = eyes.guard_area_step(0.737, -0.675, now=1.0)

        self.assertTrue(blocked)
        self.assertLess(sx, 0.0)
        self.assertGreater(abs(sy), 0.3)


class ScannerRecoveryTests(unittest.TestCase):
    def test_scanner_tries_multiple_unit_seeds_to_find_local_owner(self):
        class MS:
            calls = []

            @classmethod
            def local_player(cls, mem, seed):
                cls.calls.append(seed)
                return 0x9000 if seed == 0x3000 else None

        eyes = MemoryEyes.__new__(MemoryEyes)
        eyes.ms = MS()
        found = [("monster", 0x1000, 0.0, 0.0, 0.0),
                 ("monster", 0x2000, 0.0, 0.0, 0.0),
                 ("player", 0x3000, 0.0, 0.0, 0.0)]

        self.assertEqual(eyes._find_owner(object(), found), 0x9000)
        self.assertEqual(MS.calls, [0x1000, 0x2000, 0x3000])

    def test_known_owner_can_start_calibration_without_player_class_rows(self):
        eyes = type("Eyes", (), {"me": None, "owner": 0x1000,
                                  "known_players": lambda self: []})()
        self.assertTrue(should_calibrate(eyes, now=20.0, next_cal=10.0))

    def test_optional_classes_share_one_recovery_scan_and_cache_write(self):
        class MS:
            CLASS_NAMES = {"player": "PlayerController", "loot": "LootDrop"}
            finds = []
            saved = []

            @classmethod
            def find_classes(cls, mem, wanted):
                cls.finds.append(dict(wanted))
                return {"player": 0x2000, "loot": 0x3000}

            @staticmethod
            def class_slot_rva(mem, ptr):
                return {0x2000: 0x20, 0x3000: 0x30}[ptr]

            @staticmethod
            def load_rva_cache():
                return {"monster": 0x10}

            @classmethod
            def save_rva_cache(cls, rvas):
                cls.saved.append(dict(rvas))

        eyes = MemoryEyes.__new__(MemoryEyes)
        eyes.ms, eyes.classes = MS(), {"monster": 0x1000}

        recovered = eyes._ensure_classes(
            object(), (("player", "player classification"),
                       ("loot", "loot pickup")))

        self.assertEqual(recovered, {"player", "loot"})
        self.assertEqual(MS.finds, [{"player": "PlayerController",
                                     "loot": "LootDrop"}])
        self.assertEqual(MS.saved, [{"monster": 0x10,
                                     "player": 0x20, "loot": 0x30}])

    def test_stale_generation_cannot_paint_walk_history(self):
        class Handle:
            pid = 1

            def close(self):
                pass

            def regions(self):
                return []

        calls = {"sweeps": 0, "summaries": 0}

        def world_units(mem, regions=None, classes=None):
            calls["sweeps"] += 1
            p = 10.0 * calls["sweeps"]
            return [("monster", 0x20000, p, 0.0, p)]

        class MS:
            Mem = lambda self, pid: Handle()
            local_player = lambda self, mem, unit: None
        MS.world_units = staticmethod(world_units)

        class Walk:
            def __init__(self):
                self.painted = []

            def paint(self, walkers):
                self.painted.extend(walkers)

            def save(self):
                pass

        eyes = MemoryEyes.__new__(MemoryEyes)
        eyes.ms, eyes.mem, eyes.classes = MS(), Handle(), {"monster": 1}
        eyes.scanner = eyes.stop = None
        eyes.lock = threading.Lock()
        eyes.generation, eyes.hot, eyes.hot_full_at = 0, None, 0.0
        eyes.scan_passes = 0
        eyes.hot_empty_streak, eyes.owner, eyes.chasing = 0, None, None
        eyes.last_pos, eyes.walk = None, Walk()
        eyes.available = lambda: True
        eyes._ensure_class = lambda *args: False
        eyes._ensure_classes = lambda *args: set()
        eyes._drop_stale_hot = lambda rows: None
        eyes._sweep_loot = lambda *args: None
        eyes._accept_scan_summary = lambda report: None

        def change_generation(*args, **kwargs):
            calls["summaries"] += 1
            if calls["summaries"] == 2:
                eyes.generation += 1
                eyes.stop.set()
            return {}

        with patch("minimap_bot.memory_scan_summary", side_effect=change_generation), \
                patch("minimap_bot.MEM_REFRESH_S", 0.001):
            eyes.start_scanning()
            eyes.scanner.join(0.2)

        self.assertEqual(eyes.walk.painted, [])

    def test_world_units_uses_recovered_session_classes(self):
        classes = {"monster": 0xA000, "player": 0xB000}
        instances = {0xA000: [0x2000], 0xB000: [0x3000]}
        with patch.object(memscan, "type_classes", return_value={}), \
                patch.object(memscan, "instances_of",
                             side_effect=lambda mem, cls, **kw: instances[cls]), \
                patch.object(memscan, "unit_at", return_value=True), \
                patch.object(memscan, "read_vec3", return_value=(1.0, 2.0, 3.0)), \
                patch.object(memscan, "summoned_by_players", return_value=set()), \
                patch.object(memscan, "summoner_of", return_value=None):
            rows = memscan.world_units(object(), classes=classes)

        self.assertEqual({kind for kind, *_ in rows}, {"monster", "player"})

    def test_scanner_retries_failed_heal_while_thread_is_alive(self):
        class Handle:
            pid = 1

            def close(self):
                pass

        class MS:
            Mem = lambda self, pid: Handle()

        eyes = MemoryEyes.__new__(MemoryEyes)
        eyes.ms, eyes.mem, eyes.classes = MS(), Handle(), {}
        eyes.scanner = eyes.stop = None
        eyes.lock = threading.Lock()
        calls = []

        def heal(mem=None):
            calls.append(True)
            return False

        eyes.heal = heal
        with patch("minimap_bot.MEM_REFRESH_S", 0.005), \
                patch("minimap_bot.HOT_SELF_HEAL_S", 0.02):
            eyes.start_scanning()
            threading.Event().wait(0.07)
            eyes.stop.set()
            eyes.scanner.join(0.1)

        self.assertGreaterEqual(len(calls), 2)
        self.assertLessEqual(len(calls), 5)

    def test_calibration_uses_known_owner_without_player_class_rows(self):
        eyes = MemoryEyes.__new__(MemoryEyes)
        eyes.area = None
        eyes.owner = 0x10000
        eyes.units = [("monster", 0x20000, 0.0, 0.0, 0.0)]
        eyes.known_players = lambda: []
        position = [1.0, 1.0]
        eyes._positions = lambda addrs: {
            eyes.owner: (position[0], 0.0, position[1])}

        class Pad:
            def __init__(self):
                self.calls = []

            def stick(self, sx, sy, attack):
                self.calls.append((sx, sy, attack))
                if sx or sy:
                    position[0] += sx
                    position[1] += sy

        pad = Pad()
        with patch("minimap_bot.MEM_CAL_PUSH_S", 0.01), \
                patch("minimap_bot.time.time",
                      side_effect=(0.0, 0.0, 0.02, 1.0, 1.0, 1.02)), \
                patch("minimap_bot.time.sleep", return_value=None):
            calibrated = eyes.calibrate(pad)

        self.assertTrue(calibrated)
        self.assertEqual(eyes.owner, 0x10000)
        self.assertEqual(eyes.me, 0x10000)
        self.assertIsNotNone(eyes.basis)
        self.assertEqual([(sx, sy) for sx, sy, _ in pad.calls if sx or sy],
                         [(1.0, 0.0), (0.0, 1.0)])

    def test_missing_loot_occupancy_clears_its_spawn_blacklist(self):
        eyes = _TargetEyes({})
        key = eyes._loot_key(0x90000, 10.0, 0.0, "Flax")
        eyes.classes = {"loot": 1}
        eyes.hot_loot = None
        eyes.hot_loot_full_at = 0.0
        eyes.generation = 0
        eyes.loot = {0x90000: (10.0, 0.0, 0.0, "Flax")}
        eyes.loot_ignored = {key: 100.0}
        eyes.ms.world_loot = lambda mem, cls, regions=None: []
        mem = type("Mem", (), {"regions": lambda self: []})()

        eyes._sweep_loot(mem)

        self.assertNotIn(key, eyes.loot_ignored)

    def test_short_position_read_is_a_miss_not_an_exception(self):
        class Mem:
            def read(self, address, size):
                return b"\0"

        class MS:
            UNIT_POSITION = 0
            POS_MAX = 20000

        eyes = MemoryEyes.__new__(MemoryEyes)
        eyes.mem, eyes.ms = Mem(), MS()

        self.assertEqual(eyes._positions([0x1000]), {})


class PursuitClockTests(unittest.TestCase):
    def test_return_to_zone_cannot_be_interrupted_by_loot(self):
        self.assertFalse(loot_wins("going back", 20.0, 5.0))

    def test_loot_owned_time_does_not_age_monster_pursuit(self):
        eyes = MemoryEyes.__new__(MemoryEyes)
        eyes.engaged_since, eyes.loot_since = 0.0, 0.0
        eyes.clock_at, eyes.movement_owner = 0.0, "loot"

        eyes.account_pursuit_time(100.0)

        self.assertFalse(stale_target(100.0, eyes.engaged_since))
        self.assertEqual(eyes.loot_since, 0.0)

    def test_non_pursuit_owner_suspends_both_clocks(self):
        eyes = MemoryEyes.__new__(MemoryEyes)
        eyes.engaged_since, eyes.loot_since = 5.0, 7.0
        eyes.clock_at, eyes.movement_owner = 10.0, "return"

        eyes.account_pursuit_time(20.0)

        self.assertEqual(eyes.engaged_since, 15.0)
        self.assertEqual(eyes.loot_since, 17.0)

    def test_recycled_loot_wrapper_at_new_position_gets_fresh_clock(self):
        eyes = _TargetEyes({})
        eyes.loot_target = eyes.loot_since = None
        eyes.loot_ignored = {}
        eyes.loot = {0x90000: (10.0, 0.0, 0.0, "Flax")}
        with patch("minimap_bot.LOOT_NAMES", ()):
            eyes.pick_loot(1.0)
            first_key = eyes.loot_target

            eyes.loot = {0x90000: (20.0, 0.0, 0.0, "Flax")}
            result = eyes.pick_loot(LOOT_MAX_S + 2.0)

        self.assertNotEqual(eyes.loot_target, first_key)
        self.assertEqual(eyes.loot_since, LOOT_MAX_S + 2.0)
        self.assertIsNotNone(result[0])


if __name__ == "__main__":
    unittest.main()