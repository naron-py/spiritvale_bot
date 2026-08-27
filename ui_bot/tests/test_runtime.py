import unittest

from ui_bot.model import BotSnapshot, BotState, SnapshotError
from ui_bot.runtime import DemoEngine, encode_command, parse_protocol_line
from ui_bot.runtime_child import EVENT_PREFIX, SNAPSHOT_PREFIX


class RuntimeProtocolTests(unittest.TestCase):
    def test_command_encoding_is_newline_delimited_json(self):
        self.assertEqual(encode_command("pause"), b'{"command":"pause"}\n')
        with self.assertRaises(ValueError):
            encode_command("arbitrary")

    def test_protocol_parser_separates_snapshot_event_and_terminal_output(self):
        snapshot = (SNAPSHOT_PREFIX +
                    '{"sequence":1,"timestamp":1,"state":"RUNNING"}')
        kind, value = parse_protocol_line(snapshot)
        self.assertEqual(kind, "snapshot")
        self.assertIsInstance(value, BotSnapshot)
        self.assertEqual(value.state, BotState.RUNNING)

        kind, value = parse_protocol_line(
            EVENT_PREFIX + '{"level":"INFO","message":"ready"}')
        self.assertEqual((kind, value["message"]), ("event", "ready"))
        self.assertEqual(parse_protocol_line("window found"),
                         ("log", "window found"))

    def test_malformed_protocol_snapshot_is_rejected(self):
        with self.assertRaises(SnapshotError):
            parse_protocol_line(SNAPSHOT_PREFIX + "{broken")


class DemoEngineTests(unittest.TestCase):
    def test_demo_is_deterministic_and_has_realistic_world_states(self):
        one = DemoEngine(seed=42)
        two = DemoEngine(seed=42)

        a = [one.next_snapshot(True) for _ in range(4)]
        b = [two.next_snapshot(True) for _ in range(4)]

        self.assertEqual(a, b)
        self.assertTrue(all(item.state == BotState.RUNNING for item in a))
        self.assertTrue(all(item.connected and item.memory_active for item in a))
        self.assertGreaterEqual(len(a[-1].entities), 5)
        self.assertIsNotNone(a[-1].target)
        self.assertGreaterEqual(len(a[-1].zone.points), 4)

    def test_paused_demo_is_neutral_and_does_not_advance_player(self):
        engine = DemoEngine(seed=1)
        running = engine.next_snapshot(True)
        paused = engine.next_snapshot(False)
        paused_again = engine.next_snapshot(False)

        self.assertEqual(paused.state, BotState.PAUSED)
        self.assertEqual(paused.player, paused_again.player)
        self.assertNotEqual(running.status, "input active while paused")


if __name__ == "__main__":
    unittest.main()
