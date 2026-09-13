"""Offline scanner/cached-refresh comparison; never attaches or drives input.

Run: python -m tests.benchmark_loot_cache --baseline PATH_TO_OLD_MEMSCAN_PY
The synthetic heap measures Python/read volume, not Windows RPM or game latency.
"""
import argparse
import importlib.util
import json
from pathlib import Path
import statistics
import time

import memscan
import minimap_bot as bot
from tests.test_loot_cache import LootCacheTests, LootHeap


def measured(mem, action, repeats=25):
    samples = []
    for _ in range(repeats):
        calls, size = mem.calls, mem.bytes
        started = time.perf_counter()
        action()
        samples.append(((time.perf_counter() - started) * 1000,
                        mem.calls - calls, mem.bytes - size))
    return {"median_ms": statistics.median(s[0] for s in samples),
            "p95_ms": sorted(s[0] for s in samples)[int(0.95 * (repeats - 1))],
            "median_read_calls": statistics.median(s[1] for s in samples),
            "median_requested_bytes": statistics.median(s[2] for s in samples)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    args = parser.parse_args()
    spec = importlib.util.spec_from_file_location("baseline_memscan", args.baseline)
    baseline = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(baseline)
    mem = LootHeap()
    for drop in mem.drops[:35]:
        mem.set_item(drop, "Flax")
    expected = baseline.world_loot(mem, mem.cls)
    assert len(expected) == 35
    eyes = LootCacheTests().eyes(mem)
    eyes._sweep_loot(mem)
    assert eyes.loot == {d: (x, y, z, n) for d, x, y, z, n in expected}
    slots = dict(eyes.loot_slots)

    def full_cached_round():
        rows = {d: row for d, classes in slots.items()
                if (row := memscan.loot_slot(mem, d, *classes))}
        assert rows == eyes.loot

    report = {"fixture": "synthetic 8 MiB heap; 192 slots; 35 named",
              "baseline_source": str(args.baseline.resolve()),
              "baseline_heap_discovery": measured(mem, lambda: baseline.world_loot(mem, mem.cls)),
              "new_heap_discovery": measured(mem, lambda: memscan.world_loot(mem, mem.cls, slots={})),
              "cached_all_slots": measured(mem, full_cached_round)}
    tick = 0

    def slice_once():
        nonlocal tick
        tick += 1
        eyes._refresh_loot(now=tick * 0.051)

    report["actual_bot_slice"] = measured(mem, slice_once, repeats=120)
    mem.set_item(mem.drops[-1], "Bee Card")
    frames = 0
    while mem.drops[-1] not in eyes.loot and frames < 192:
        slice_once()
        frames += 1
    assert mem.drops[-1] in eyes.loot, "cached activation never published"
    report["activation_test"] = {
        "scheduled_refresh_calls_until_last_slot_detected": frames,
        "simulated_elapsed_ms": frames * 51,
        "heap_searches_after_activation": 0,
        "note": "Scheduled synthetic calls, not measured live spawn latency."}
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
