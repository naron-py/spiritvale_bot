"""Memory spacing uses its own band, never polygon arrival distance."""
import unittest
import inspect
import textwrap
from unittest.mock import patch

import minimap_bot as bot
from tests.test_state_conflicts import _TargetEyes
from tests.test_buff_scheduler import _Pad
from ui_bot.config import UiSettings


class CombatDistanceTests(unittest.TestCase):
    def test_exact_overlap_still_retreats(self):
        eyes = _TargetEyes({0x2000: (10.0, 0.0, 1.0)})
        eyes.positions[eyes.me] = (10.0, 0.0, 1.0)
        sx, sy, dist = eyes.target(1.0)
        self.assertEqual(eyes.spacing_state, "RETREAT")
        self.assertEqual(dist, 0.0)
        self.assertGreater(abs(sx) + abs(sy), 0.1)

    def test_new_target_does_not_inherit_retreat_hysteresis(self):
        eyes = _TargetEyes({0x2000: (1.0, 0.0, 1.0)})
        eyes.target(1.0)
        self.assertEqual(eyes.spacing_state, "RETREAT")
        eyes.ms.ids[0x2000] = 2
        eyes.chasing_id = 1
        eyes.positions[0x2000] = (2.0, 0.0, 1.0)
        eyes.target(1.1)
        self.assertEqual(eyes.spacing_state, "ATTACK")

    def test_real_output_keeps_both_attacks_and_polygon_segments_safe(self):
        source = inspect.getsource(bot.main)
        start = source.index("                boundary_blocked = False")
        end = source.index("                pad.stick(sx, sy, atk)", start)
        dispatch = compile(textwrap.dedent(source[start:end] +
                           "                pad.stick(sx, sy, atk)\n"),
                           "<real main output>", "exec")
        # At the right edge retreat cannot point directly away; the legal
        # sideways alternative must keep attacking without invoking a return.
        for px, tx, state in ((10, 11, "RETREAT"), (10, 12, "ATTACK"),
                              (10, 15, "APPROACH"), (16.5, 16, "RETREAT")):
            with self.subTest(px=px, tx=tx):
                eyes = _TargetEyes({0x2000: (tx, 0.0, 0.0)})
                eyes.positions[eyes.me] = (px, 0.0, 0.0)
                eyes.area = bot.Area("yard", polygon=[(0, -10), (20, -10),
                                                      (20, 10), (0, 10)], axes="xz")
                sx, sy, _ = eyes.target(1.0)
                self.assertEqual(eyes.spacing_state, state)
                pad = _Pad()
                scope = dict(vars(bot), eyes=eyes, sx=sx, sy=sy, now=1.0,
                             memory_driving=True, on_loot=False, pad=pad,
                             buffs=bot.BuffScheduler(0.0))
                exec(dispatch, scope)
                _, sx, sy, attack = pad.calls[-1]
                self.assertTrue(attack)
                self.assertTrue({"lb", "rb"}.issubset(pad.held))
                self.assertGreater(abs(sx) + abs(sy), 0.1)
                self.assertTrue(eyes.area.guard_step(
                    (px, 0), (px + sx * bot.AREA_LOOKAHEAD,
                              sy * bot.AREA_LOOKAHEAD))[0])

    def test_live_configuration_updates_only_combat_distances(self):
        config = UiSettings().control_config()
        config["combat_spacing"] = dict(min_distance=4.0, resume_distance=6.0,
                                         max_distance=8.0)
        pad = _Pad()
        pad.configure_attack = lambda keys: None
        with patch.multiple(bot, min_distance=1.8, resume_distance=2.5,
                            max_distance=3.2):
            bot.apply_controller_config(bot.BuffScheduler(0.0), pad, config, 1.0)
            self.assertEqual((bot.min_distance, bot.resume_distance,
                              bot.max_distance), (4.0, 6.0, 8.0))
            self.assertEqual(bot.MEM_ARRIVE, 2.5)

    def test_invalid_combat_config_is_rejected_before_any_input_change(self):
        for band in (None, {}, dict(min_distance=True, resume_distance=2, max_distance=3),
                     dict(min_distance=4, resume_distance=3, max_distance=8),
                     dict(min_distance=1, resume_distance=2, max_distance=float("nan")),
                     dict(min_distance=1, resume_distance=2, max_distance=10 ** 400),
                     dict(min_distance=1, resume_distance=2, max_distance=101)):
            with self.subTest(band=band):
                config = UiSettings().control_config()
                config["combat_spacing"] = band
                pad = _Pad()
                pad.configure_attack = lambda keys: pad.calls.append(keys)
                with self.assertRaisesRegex(ValueError, "combat"):
                    bot.apply_controller_config(bot.BuffScheduler(0.0), pad, config, 1.0)
                self.assertEqual(pad.calls, [])

    def test_custom_band_hysteresis_drives_real_target(self):
        eyes = _TargetEyes({0x2000: (10.0, 0.0, 1.0)})
        with patch.multiple(bot, min_distance=4.0, resume_distance=6.0,
                            max_distance=8.0, create=True):
            for index, (distance, state) in enumerate((
                    (10, "APPROACH"), (7.9, "APPROACH"), (6, "ATTACK"),
                    (8, "ATTACK"), (8.1, "APPROACH"), (5.9, "ATTACK"),
                    (3.9, "RETREAT"), (4.1, "RETREAT"), (5.9, "RETREAT"),
                    (6, "ATTACK"))):
                with self.subTest(distance=distance, state=state):
                    eyes.positions[0x2000] = (distance, 0.0, 1.0)
                    sx, sy, actual = eyes.target(1.0 + index * 0.1)
                    self.assertEqual(eyes.spacing_state, state)
                    self.assertAlmostEqual(actual, distance)
                    self.assertGreater(abs(sx) + abs(sy), 0.1)
                    if state == "APPROACH":
                        self.assertGreater(sx, 0)
                    elif state == "RETREAT":
                        self.assertLess(sx, 0)
                    else:
                        self.assertGreater(abs(sy), 0.1)
        self.assertEqual(bot.MEM_ARRIVE, 2.5)


if __name__ == "__main__":
    unittest.main()
