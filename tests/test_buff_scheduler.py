import unittest
from unittest.mock import patch

from minimap_bot import (ArduinoPad, BUFF_ATTACK_INTERVAL_S, BUFF_EARLY_REFRESH_S,
                         BUFF_HOLD_S, BUFF_PERIOD_S, BUFF_REPEAT_GAP_S,
                         BUFF_SEQUENCE,
                         BuffScheduler, VirtualPad, apply_controller_config,
                         attack_active,
                         complete_buff_tick)


class _Pad:
    def __init__(self):
        self.held = {"lb", "rb"}
        self.calls = []

    def press_buff(self, key):
        self.held.add(key)
        self.calls.append(("buff_down", key))

    def release_buff(self, key):
        self.held.discard(key)
        self.calls.append(("buff_up", key))

    def stick(self, sx, sy, attack):
        for key in ("lb", "rb"):
            if attack:
                self.held.add(key)
            else:
                self.held.discard(key)
        self.calls.append(("stick", sx, sy, attack))

    def reassert_attack(self):
        self.held.update(("lb", "rb"))
        self.calls.append(("reassert_attack",))

    def release_all(self):
        self.held.clear()
        self.calls.append(("reset",))


class BuffSchedulerTests(unittest.TestCase):
    def test_runtime_configuration_rejects_non_boolean_enabled_before_changes(self):
        scheduler = BuffScheduler(now=0.0)
        pad = _Pad()
        pad.configure_attack = lambda keys: pad.calls.append(("configure", tuple(keys)))
        original_order = list(scheduler.order)
        config = {
            "buff_slots": [{"id": "buff-1", "name": "Buff Slot 1",
                            "enabled": "false", "button": "dpad_up", "order": 0}],
            "attack_slots": [
                {"id": "attack-1", "name": "Attack Skill 1",
                 "enabled": True, "button": "lb", "order": 0},
                {"id": "attack-2", "name": "Attack Skill 2",
                 "enabled": False, "button": "rb", "order": 1},
            ],
        }

        with self.assertRaisesRegex(ValueError, "enabled"):
            apply_controller_config(scheduler, pad, config, 10.0)

        self.assertEqual(scheduler.order, original_order)
        self.assertFalse(any(call[0] == "configure" for call in pad.calls))

    def test_runtime_configuration_rejects_extra_attack_slot_before_changes(self):
        scheduler = BuffScheduler(now=0.0)
        pad = _Pad()
        pad.configure_attack = lambda keys: pad.calls.append(("configure", tuple(keys)))
        config = {
            "buff_slots": [],
            "attack_slots": [
                {"id": "attack-1", "name": "Attack Skill 1", "enabled": True,
                 "button": "lb", "order": 0},
                {"id": "attack-2", "name": "Attack Skill 2", "enabled": True,
                 "button": "rb", "order": 1},
                {"id": "attack-3", "name": "Attack Skill 3", "enabled": True,
                 "button": "a", "order": 2},
            ],
        }

        with self.assertRaisesRegex(ValueError, "exactly two"):
            apply_controller_config(scheduler, pad, config, 10.0)

        self.assertFalse(any(call[0] == "configure" for call in pad.calls))

    def test_initial_due_times_are_staggered_and_each_buff_has_its_own_clocks(self):
        scheduler = BuffScheduler(now=100.0)

        due = [scheduler.buffs[key]["next_due"] for key in scheduler.order]
        self.assertEqual(len(set(due)), len(BUFF_SEQUENCE))
        self.assertEqual(due, sorted(due))
        self.assertTrue(all(scheduler.buffs[key]["last_cast"] is None
                            for key in scheduler.order))

    def test_simultaneous_due_buffs_cast_only_one_per_attack_window(self):
        scheduler = BuffScheduler(now=0.0)
        pad = _Pad()
        for state in scheduler.buffs.values():
            state["next_due"] = 10.0

        self.assertEqual(scheduler.cast_due(10.0, pad, True, False), "buff-1")
        self.assertIsNone(scheduler.cast_due(10.0, pad, True, False))
        self.assertEqual([call for call in pad.calls if call[0] == "buff_down"],
                         [("buff_down", "dpad_up")])

        first_release = 10.0 + BUFF_HOLD_S
        self.assertIsNone(scheduler.release_due(first_release, pad))
        scheduler.release_due(first_release + BUFF_REPEAT_GAP_S, pad)
        completed = first_release + BUFF_REPEAT_GAP_S + BUFF_HOLD_S
        self.assertEqual(scheduler.release_due(completed, pad), "buff-1")
        self.assertIsNone(scheduler.cast_due(
            completed + BUFF_ATTACK_INTERVAL_S - 0.001,
            pad, True, False))
        self.assertEqual(scheduler.cast_due(
            completed + BUFF_ATTACK_INTERVAL_S,
            pad, True, False), "buff-2")

    def test_routine_buff_tap_never_releases_attack_buttons_or_blocks(self):
        scheduler = BuffScheduler(now=0.0)
        pad = _Pad()

        with patch("minimap_bot.time.sleep", side_effect=AssertionError("blocked")):
            scheduler.cast_due(0.0, pad, attack_active=True, combat_priority=False)
            scheduler.release_due(BUFF_HOLD_S, pad)

        self.assertIn("lb", pad.held)
        self.assertIn("rb", pad.held)
        self.assertNotIn("dpad_up", pad.held)
        self.assertTrue(attack_active(BUFF_HOLD_S))

    def test_completion_allows_attack_to_be_reasserted_in_the_same_tick(self):
        scheduler = BuffScheduler(now=0.0)
        pad = _Pad()
        scheduler.cast_due(0.0, pad, True, False)
        pad.held.discard("lb")  # model a game animation interrupting attack
        pad.held.discard("rb")

        self.assertIsNone(complete_buff_tick(
            scheduler, pad, BUFF_HOLD_S, True))

        self.assertEqual(pad.calls[-2:], [("buff_up", "dpad_up"),
                                         ("reassert_attack",)])
        self.assertTrue({"lb", "rb"} <= pad.held)

    def test_danger_close_defers_once_then_casts_one_buff(self):
        scheduler = BuffScheduler(now=0.0)
        pad = _Pad()

        with patch("builtins.print") as printed:
            self.assertIsNone(scheduler.cast_due(0.0, pad, True, True))
            self.assertIsNone(scheduler.cast_due(0.1, pad, True, True))
        self.assertEqual(len(printed.call_args_list), 1)
        self.assertIn("[Buff] UP deferred; combat priority",
                      printed.call_args[0][0])

        self.assertEqual(scheduler.cast_due(
            scheduler.next_cast_at, pad, True, True), "buff-1")

    def test_safety_blocked_attack_does_not_authorize_a_buff(self):
        scheduler = BuffScheduler(now=0.0)
        pad = _Pad()

        self.assertIsNone(scheduler.cast_due(
            0.0, pad, attack_active=False, combat_priority=False))

        self.assertFalse(any(call[0] == "buff_down" for call in pad.calls))

    def test_completion_sets_per_buff_refresh_before_expiration(self):
        scheduler = BuffScheduler(now=0.0)
        pad = _Pad()
        scheduler.cast_due(0.0, pad, True, False)
        scheduler.release_due(BUFF_HOLD_S, pad)
        scheduler.release_due(BUFF_HOLD_S + BUFF_REPEAT_GAP_S, pad)
        completed = 2 * BUFF_HOLD_S + BUFF_REPEAT_GAP_S

        scheduler.release_due(completed, pad)

        self.assertEqual(scheduler.buffs["buff-1"]["last_cast"], completed)
        self.assertEqual(scheduler.buffs["buff-1"]["next_due"],
                         completed + BUFF_PERIOD_S - BUFF_EARLY_REFRESH_S)

    def test_full_neutral_still_releases_attack_and_active_buff(self):
        scheduler = BuffScheduler(now=0.0)
        pad = _Pad()
        scheduler.cast_due(0.0, pad, True, False)

        pad.release_all()  # shared Pause/Stop/Emergency/disconnect safety action
        scheduler.reset(20.0)

        self.assertFalse(pad.held)
        self.assertIsNone(scheduler.active)

    def test_disabled_buffs_are_never_pressed(self):
        scheduler = BuffScheduler(now=0.0)
        pad = _Pad()
        scheduler.configure([
            {"id": "one", "name": "One", "enabled": False,
             "button": "dpad_up", "order": 0},
            {"id": "two", "name": "Two", "enabled": True,
             "button": "x", "order": 1},
        ], now=0.0, pad=pad)

        self.assertEqual(scheduler.cast_due(0.0, pad, True, False), "two")
        self.assertEqual([call for call in pad.calls if call[0] == "buff_down"],
                         [("buff_down", "x")])

    def test_each_scheduled_buff_is_pressed_twice_without_blocking(self):
        scheduler = BuffScheduler(now=0.0)
        pad = _Pad()

        with patch("minimap_bot.time.sleep", side_effect=AssertionError("blocked")):
            scheduler.cast_due(0.0, pad, True, False)
            complete_buff_tick(scheduler, pad, BUFF_HOLD_S, True)
            complete_buff_tick(
                scheduler, pad, BUFF_HOLD_S + BUFF_REPEAT_GAP_S, True)
            completed = complete_buff_tick(
                scheduler, pad, 2 * BUFF_HOLD_S + BUFF_REPEAT_GAP_S, True)

        self.assertEqual(completed, "buff-1")
        self.assertEqual(
            [call for call in pad.calls if call[0].startswith("buff_")],
            [("buff_down", "dpad_up"), ("buff_up", "dpad_up"),
             ("buff_down", "dpad_up"), ("buff_up", "dpad_up")])
        self.assertTrue({"lb", "rb"} <= pad.held)

    def test_runtime_update_releases_active_old_binding_and_preserves_other_clocks(self):
        scheduler = BuffScheduler(now=0.0)
        pad = _Pad()
        scheduler.cast_due(0.0, pad, True, False)
        scheduler.buffs["buff-2"]["last_cast"] = 8.0
        scheduler.buffs["buff-2"]["next_due"] = 50.0

        scheduler.configure([
            {"id": "buff-2", "name": "Buff Slot 2", "enabled": True,
             "button": "y", "order": 0},
        ], now=10.0, pad=pad)

        self.assertIn(("buff_up", "dpad_up"), pad.calls)
        self.assertIsNone(scheduler.active)
        self.assertEqual(scheduler.buffs["buff-2"]["last_cast"], 8.0)
        self.assertEqual(scheduler.buffs["buff-2"]["next_due"], 50.0)

    def test_unchanged_live_config_preserves_active_double_tap_sequence(self):
        scheduler = BuffScheduler(now=0.0)
        pad = _Pad()
        scheduler.cast_due(0.0, pad, True, False)
        pad.calls.clear()
        unchanged = [
            {key: state[key] for key in
             ("id", "name", "enabled", "button", "order")}
            for state in scheduler.buffs.values()
        ]

        scheduler.configure(unchanged, now=0.01, pad=pad)

        self.assertEqual(scheduler.active, "buff-1")
        self.assertEqual(scheduler.phase, "pressed")
        self.assertEqual(scheduler.taps, 1)
        self.assertEqual(pad.calls, [])
        complete_buff_tick(scheduler, pad, BUFF_HOLD_S, True)
        complete_buff_tick(
            scheduler, pad, BUFF_HOLD_S + BUFF_REPEAT_GAP_S, True)
        complete_buff_tick(
            scheduler, pad, 2 * BUFF_HOLD_S + BUFF_REPEAT_GAP_S, True)
        self.assertEqual(
            [call for call in pad.calls if call[0].startswith("buff_")],
            [("buff_up", "dpad_up"), ("buff_down", "dpad_up"),
             ("buff_up", "dpad_up")])


class VirtualPadBuffTests(unittest.TestCase):
    def test_saving_unchanged_attack_bindings_does_not_release_them(self):
        class _Gamepad:
            def __init__(self):
                self.calls = []

            def press_button(self, button):
                self.calls.append(("down", button))

            def release_button(self, button):
                self.calls.append(("up", button))

            def update(self):
                self.calls.append(("update",))

        pad = VirtualPad.__new__(VirtualPad)
        pad.pad = _Gamepad()
        pad.buttons = {"lb": "lb", "rb": "rb"}
        pad.attack_keys = ("lb", "rb")
        pad.attack_held = True
        pad.held_triggers = set()

        pad.configure_attack(("lb", "rb"))

        self.assertFalse(any(call[0] == "up" for call in pad.pad.calls))

    def test_periodic_taps_do_not_release_custom_attack_bindings(self):
        class _Gamepad:
            def __init__(self):
                self.held = set()
                self.triggers = {"lt": 0, "rt": 0}

            def press_button(self, button):
                self.held.add(button)

            def release_button(self, button):
                self.held.discard(button)

            def left_trigger(self, value):
                self.triggers["lt"] = value

            def right_trigger(self, value):
                self.triggers["rt"] = value

            def update(self):
                pass

        pad = VirtualPad.__new__(VirtualPad)
        pad.pad = _Gamepad()
        pad.face = {"y": "y"}
        pad.buttons = {"y": "y", "lt": "lt"}
        pad.attack_keys = ("y", "lt")
        pad.attack_held = True
        pad.held_triggers = {"lt"}
        pad.pad.held.add("y")
        pad.pad.triggers["lt"] = 255

        with patch("minimap_bot.time.sleep", side_effect=AssertionError("blocked")):
            pad.tap_button("y", 0.05)
            pad.tap_trigger("lt", 0.05)

        self.assertIn("y", pad.pad.held)
        self.assertEqual(pad.pad.triggers["lt"], 255)

    def test_runtime_attack_binding_update_releases_old_and_holds_new_binding(self):
        class _Gamepad:
            def __init__(self):
                self.held = set()

            def left_joystick_float(self, x, y):
                pass

            def press_button(self, button):
                self.held.add(button)

            def release_button(self, button):
                self.held.discard(button)

            def update(self):
                pass

        pad = VirtualPad.__new__(VirtualPad)
        pad.pad = _Gamepad()
        pad.buttons = {key: key for key in (
            "dpad_up", "dpad_down", "dpad_left", "dpad_right",
            "a", "b", "x", "y", "lb", "rb")}
        pad.attack_keys = ("lb", "rb")
        pad.attack_held = False
        pad.held_triggers = set()
        pad.stick(0.0, 0.0, True)

        pad.configure_attack(("x",))

        self.assertEqual(pad.pad.held, {"x"})
        self.assertTrue(pad.attack_held)

    def test_specific_buff_release_does_not_reset_attack(self):
        class _Gamepad:
            def __init__(self):
                self.held = set()
                self.resets = 0

            def left_joystick_float(self, x, y):
                pass

            def press_button(self, button):
                self.held.add(button)

            def release_button(self, button):
                self.held.discard(button)

            def update(self):
                pass

            def reset(self):
                self.resets += 1
                self.held.clear()

        pad = VirtualPad.__new__(VirtualPad)
        pad.pad = _Gamepad()
        pad.attack_btn = ("lb", "rb")
        pad.dpad = {key: key for key in ("up", "down", "left", "right")}
        pad.face = {"x": "x", "a": "a"}
        pad.stick(0.0, 0.0, True)

        pad.press_buff("dpad_up")
        pad.release_buff("dpad_up")

        self.assertEqual(pad.pad.held, {"lb", "rb"})
        self.assertEqual(pad.pad.resets, 0)

    def test_arduino_buff_edges_never_release_attack_or_reset_controller(self):
        pad = ArduinoPad.__new__(ArduinoPad)
        sent = []
        pad._cmd = sent.append

        pad.press_buff("dpad_up")
        pad.release_buff("dpad_up")
        pad.press_buff("x")
        pad.release_buff("x")
        pad.reassert_attack()

        self.assertEqual(sent, ["V0", "V-1", "D2", "U2", "D4", "D5"])
        self.assertFalse(any(command in ("U4", "U5", "Z") for command in sent))

    def test_arduino_loot_trigger_preserves_custom_attack_trigger(self):
        pad = ArduinoPad.__new__(ArduinoPad)
        sent = []
        pad._cmd = sent.append
        pad.attack_keys = ("rt",)
        pad.attack_held = True
        pad.held_triggers = {"rt"}

        pad.tap_trigger("lt", 0.0)

        self.assertEqual(sent, ["T255,255", "T0,255"])

    def test_arduino_rebind_reasserts_retained_dpad_attack_after_hat_release(self):
        pad = ArduinoPad.__new__(ArduinoPad)
        sent = []
        pad._cmd = sent.append
        pad.attack_keys = ("dpad_up", "dpad_right")
        pad.attack_held = True
        pad.held_triggers = set()

        pad.configure_attack(("dpad_right",))

        self.assertEqual(sent, ["V-1", "V2"])


if __name__ == "__main__":
    unittest.main()
