"""Offline execution of real main-loop source; no window or input devices."""
import inspect
import textwrap
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

import minimap_bot as bot
import cv2
import numpy as np
from ui_bot.runtime_child import CommandGate


def loop_slice(start, end, env):
    source = inspect.getsource(bot.main)
    source = source[source.index(start):source.index(end)]
    source = textwrap.dedent('                ' + source)
    scope = dict(vars(bot), **env)
    scope['gameplay_reached'] = False
    exec('for _frame in range(1):\n' + textwrap.indent(source, '    ')
         + '\n    gameplay_reached = True', scope)
    return scope


def environment(flow=None):
    return dict(reconnecting=True, next_login_check=0, now=-1000,
                time=SimpleNamespace(time=lambda: 100., sleep=lambda _: None),
                sct=Mock(), np=SimpleNamespace(array=lambda _: np.zeros((2, 2, 3), dtype=np.uint8)),
                window_region=lambda _: {}, win=Mock(), login_screen=lambda _: 'server',
                reconnect_step=Mock(return_value='server'), reconnect_flow=flow or bot.ReconnectFlow(),
                eyes=None, pad=Mock(), dashboard=Mock(), paused=False, memory_wait=False,
                target_lock=Mock(), target_blacklist=Mock(), stuck_watchdog=Mock(),
                pet_filter=Mock(), buffs=Mock(), last=None, next_spam=0, next_loot=0)


class ReconnectLoopTests(unittest.TestCase):
    def test_red_minimap_target_is_positive_evidence_not_blank_or_center(self):
        image = np.zeros((1080, 1920, 3), dtype=np.uint8)
        cx, cy = int(1920 * bot.MINIMAP['cx']), int(1080 * bot.MINIMAP['cy'])
        self.assertFalse(bot.reconnect_pixel_ready(image))
        cv2.circle(image, (cx, cy), 4, (0, 0, 255), -1)
        self.assertFalse(bot.reconnect_pixel_ready(image))
        cv2.circle(image, (cx - 45, cy), 4, (0, 0, 255), -1)
        self.assertTrue(bot.reconnect_pixel_ready(image))
        from unittest.mock import patch
        for screen in ('disconnected', 'idle disconnected', 'server', 'character'):
            with patch.object(bot, 'login_screen', return_value=screen):
                self.assertFalse(bot.reconnect_pixel_ready(image))

    def test_red_target_resumes_existing_pixel_loop_during_memory_wait(self):
        flow = bot.ReconnectFlow()
        flow.observe('character', 0)
        flow.observe(None, 1)
        env = environment(flow)
        env.update(paused=True, memory_wait=True, login_screen=lambda _: None,
                   reconnect_pixel_ready=lambda _: True)
        result = loop_slice('if reconnecting and time.time()', 'reg = minimap_region(win)', env)
        self.assertTrue(result['gameplay_reached'])
        self.assertFalse(result['paused'])
        self.assertFalse(result['memory_wait'])
        self.assertFalse(flow.active)
        self.assertEqual(flow.deadline, 0)
        env['reconnect_step'].assert_not_called()

    def test_loading_without_memory_or_red_target_stays_neutral(self):
        flow = bot.ReconnectFlow()
        flow.observe('character', 0)
        flow.observe(None, 1)
        env = environment(flow)
        env.update(login_screen=lambda _: None, reconnect_pixel_ready=lambda _: False)
        result = loop_slice('if reconnecting and time.time()', 'reg = minimap_region(win)', env)
        self.assertTrue(flow.active)
        self.assertFalse(result['gameplay_reached'])
        env['pad'].stick.assert_called_with(0., 0., False)

    def test_manual_pause_never_checks_or_resumes_pixel_fallback(self):
        flow = bot.ReconnectFlow()
        flow.observe('character', 0)
        flow.observe(None, 1)
        evidence = Mock(return_value=True)
        env = environment(flow)
        env.update(paused=True, memory_wait=False, reconnect_pixel_ready=evidence)
        result = loop_slice('if paused and not memory_wait:', 'reg = minimap_region(win)', env)
        self.assertTrue(result['paused'])
        self.assertFalse(result['gameplay_reached'])
        evidence.assert_not_called()
        env['reconnect_step'].assert_not_called()

    def test_pixel_evidence_cannot_skip_visible_login_or_reset_twice(self):
        flow = bot.ReconnectFlow()
        flow.observe('idle disconnected', 0)
        action, _, reset = flow.observe('idle disconnected', 1, pixel_ready=True)
        self.assertIsNone(action)
        self.assertFalse(reset)
        self.assertTrue(flow.active)
        _, _, reset = flow.observe(None, 2, pixel_ready=True)
        self.assertTrue(reset)
        self.assertTrue(flow.active)
        action, events, reset = flow.observe(None, 3, pixel_ready=True)
        self.assertFalse(reset)
        self.assertFalse(flow.active)
        self.assertTrue(any('Pixel fallback' in event for event in events))
        self.assertEqual(flow.observe(None, 100, pixel_ready=True), (None, [], False))

    def test_main_completion_uses_login_observation_not_gameplay_clock(self):
        env = environment()
        result = loop_slice('if reconnecting and time.time()', 'reg = minimap_region(win)', env)
        self.assertEqual(result['reconnect_flow'].deadline, 100 + bot.RECONNECT_STAGE_TIMEOUT_S)
        env['reconnect_step'].assert_called_once()

    def test_skipped_character_resets_before_fresh_owner_validation(self):
        for screen in ('disconnected', 'idle disconnected', 'server'):
            with self.subTest(screen=screen):
                flow = bot.ReconnectFlow(random_wait=lambda: 15)
                flow.observe(screen, 0)
                eyes = SimpleNamespace(lock=threading.Lock(), generation=1, owner=123,
                                       scanner=SimpleNamespace(is_alive=lambda: True))
                reads = []
                eyes._positions = lambda owners: reads.append(owners) or {123: (2, 0, 3)}
                def reset():
                    eyes.generation += 1
                    eyes.owner = None
                eyes.reset_session = Mock(side_effect=reset)
                eyes.account_pursuit_time = Mock()
                env = environment(flow)
                env.update(eyes=eyes, login_screen=lambda _: None)
                result = loop_slice('if reconnecting and time.time()', 'reg = minimap_region(win)', env)
                eyes.reset_session.assert_called_once()
                self.assertEqual(reads, [])
                self.assertTrue(flow.active)
                self.assertFalse(result['gameplay_reached'])
                result['next_login_check'] = 0
                result = loop_slice('if reconnecting and time.time()', 'reg = minimap_region(win)', result)
                self.assertTrue(flow.active)
                eyes.reset_session.assert_called_once()
                eyes.owner = 123
                result['next_login_check'] = 0
                result = loop_slice('if reconnecting and time.time()', 'reg = minimap_region(win)', result)
                self.assertFalse(flow.active)
                self.assertEqual(reads, [[123]])
                self.assertTrue(result['gameplay_reached'])

    def test_skipped_character_without_memory_fails_bounded(self):
        flow = bot.ReconnectFlow(random_wait=lambda: 15)
        flow.observe('server', 0)
        env = environment(flow)
        env['login_screen'] = lambda _: None
        for now in range(100, 1000, 100):
            env.update(time=SimpleNamespace(time=lambda: now, sleep=lambda _: None),
                       next_login_check=0)
            env = loop_slice('if reconnecting and time.time()', 'reg = minimap_region(win)', env)
            self.assertFalse(env['gameplay_reached'])
            if flow.failed:
                break
        self.assertTrue(flow.failed)
        self.assertEqual(flow.attempts, 5)
        env['reconnect_step'].assert_not_called()

    def test_initial_unknown_is_not_recovery_and_character_pixel_still_works(self):
        flow = bot.ReconnectFlow()
        self.assertEqual(flow.observe(None, 0), (None, [], False))
        self.assertFalse(flow.active)
        flow.observe('character', 1)
        self.assertTrue(flow.observe(None, 2)[2])
        flow.observe(None, 3, bot.reconnect_player_valid(None))
        self.assertFalse(flow.active)

    def run_control(self, env, gate):
        env.update(automation_state_request=gate.poll_internal,
                   toggle_key_hit=gate.poll_toggle, zone=None)
        return loop_slice('request = automation_state_request()', 'reg = minimap_region(win)', env)

    def test_memory_wait_polls_reconnect_but_never_gameplay(self):
        gate = CommandGate()
        gate.observe(True)
        gate.submit('memory_wait')
        env = environment()
        env['dashboard'].update.side_effect = lambda eyes, running, *a, **k: gate.observe(running)
        result = self.run_control(env, gate)
        env['reconnect_step'].assert_called_once()
        self.assertTrue(result['paused'])
        self.assertTrue(result['memory_wait'])
        self.assertFalse(gate._observed)
        gate.submit('pause')
        result = self.run_control(result, gate)
        self.assertTrue(result['paused'])
        self.assertFalse(result['memory_wait'])
        self.assertFalse(result['reconnect_flow'].active)
        gate.submit('memory_recovered')
        self.assertIsNone(gate.poll_internal())
        self.assertFalse(gate.poll_toggle())

    def test_end_during_wait_stops_intent_even_without_readiness(self):
        gate = CommandGate()
        gate.observe(True)
        gate.submit('memory_wait')
        gate.poll_internal()
        self.assertTrue(gate.allow_hotkey_toggle())
        env = environment()
        env.update(paused=True, memory_wait=True, zone=None,
                   automation_state_request=gate.poll_internal,
                   toggle_key_hit=lambda: True,
                   toggle_running=lambda paused, *a, **k: not paused)
        env['reconnect_flow'].observe('server', 0)
        result = loop_slice('request = automation_state_request()', 'reg = minimap_region(win)', env)
        self.assertTrue(result['paused'])
        self.assertFalse(result['memory_wait'])
        self.assertFalse(result['reconnect_flow'].active)
        env['reconnect_step'].assert_not_called()
        gate.observe(not result['paused'])
        self.assertFalse(gate.poll_toggle())
        gate.submit('memory_recovered')
        self.assertIsNone(gate.poll_internal())

    def test_wait_without_login_stays_before_gameplay(self):
        gate = CommandGate()
        gate.observe(True)
        gate.submit('memory_wait')
        env = environment()
        env['login_screen'] = lambda _: None
        result = self.run_control(env, gate)
        # The real continue skips a marker inserted at the gameplay boundary.
        self.assertTrue(result['paused'])
        self.assertTrue(result['memory_wait'])
        self.assertFalse(result['gameplay_reached'])
        env['dashboard'].update.assert_called()
        self.assertFalse(result['reconnect_flow'].active)

    def test_explicit_resume_during_wait_keeps_reconnect_active(self):
        gate = CommandGate()
        gate.observe(True)
        gate.submit('memory_wait')
        env = self.run_control(environment(), gate)
        gate.submit('resume')
        env['wake_controller'] = Mock()
        env['toggle_running'] = lambda paused, *a, **k: not paused
        result = self.run_control(env, gate)
        self.assertFalse(result['paused'])
        self.assertFalse(result['memory_wait'])
        self.assertTrue(result['reconnect_flow'].active)
        self.assertFalse(result['gameplay_reached'])
        env['wake_controller'].assert_not_called()


if __name__ == '__main__':
    unittest.main()
