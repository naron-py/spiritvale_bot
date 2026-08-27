# UI automation recovery audit

This audit covers every path found in the UI process supervisor, immutable snapshot
adapter, controller state machine, and terminal automation loop that can intentionally
or accidentally stop issuing useful inputs.

## Lifecycle invariants

- `STOPPED`, `PAUSED`, and `EMERGENCY_STOP` are user-owned states. Automatic recovery
  never overrides Stop, Pause, window close, or the emergency latch.
- A transient runtime failure preserves `desired_running`, releases inputs, and enters
  `RECOVERING`.
- Memory recovery keeps requesting current-generation/current-session snapshots. Three
  consecutive finite, fresh, valid player snapshots are required before `resume`.
- Positive snapshot sequences must increase; duplicate/out-of-order frames cannot
  validate recovery. Confirmation must use a sequence later than the actual resume
  request.
- A replacement worker may restart its sequence at one; generation/session filtering,
  not the old sequence floor, rejects stale callbacks.
- Recovery restarts the exact owned `QProcess`; replacement starts only from its
  `finished` callback. No fixed replacement delay is used.
- Retry delay is bounded: controller recovery requests a restart after 20 seconds and
  backs repeated restarts off up to 30 seconds. The total controller recovery window is
  300 seconds by default. Process discovery uses the existing 1–10 second capped
  backoff. All values are constructor-configurable.
- Successful RUNNING confirmation clears validation streaks, restart counters, action
  history, and progress baselines.

## UI/controller transitions

| Trigger | Prior defect / no-action state | Classification | Current behavior |
|---|---|---|---|
| User Start | duplicate worker risk | User action | Idempotent; resumes owned paused worker or starts one worker. |
| User Pause | `PAUSED` | User terminal state | Clears intent before transport; if the worker is absent/unwritable, requests an exact non-auto-resuming replacement. |
| User Stop / End | `STOPPED` | User terminal state | Overrides recovery/switching immediately; transport failure retires the worker without restoring run intent. |
| Emergency Stop | latched `SAFE_STOP` | User terminal state | Kills/releases and remains latched until explicit reset. |
| Window close / forced close | stopped process | User terminal state | Emergency cleanup; no automatic resurrection after UI exit. |
| Mode/area revision replacement | `SWITCHING_MODE` | Bounded transition | Start/Pause disabled, Stop available; exact old process exits before one replacement; a hung exit is terminated. |
| Crash during mode replacement | false successful switch | Transient | Failure enters bounded discovery/recovery while preserving pending mode/area settings. |
| One invalid/stale/nonfinite Memory player frame | permanent `PAUSED`, child printed `STOPPED (End)` | Transient | Immediate pause/release, `RECOVERING`, scans continue, 3 valid frames, automatic resume. |
| Invalid frame during validation | could accidentally count through noise | Transient | Resets valid streak and resume request; continues recovery. |
| Duplicate/out-of-order sequence | could falsely validate or confirm recovery | Stale data | Rejected before counters or state mutate. |
| Old PID/session/generation snapshot | could overwrite current state | Stale data | Rejected; cannot count toward recovery. |
| Game/process disappears while run was desired | controller changed to `IDLE`/`SAFE_STOP` | Transient within window | Preserves intent, enters `RECOVERING`, process discovery reconnects with capped backoff. |
| Worker crashes or exits unexpectedly | `SAFE_STOP` despite runtime reconnect | Transient within window | `RECOVERING`; replacement snapshots revalidate and resume. |
| Worker remains alive but publishes no snapshots | unbounded silent wait | Transient/hung worker | 8-second configurable heartbeat retires exact generation and reconnects. |
| Malformed protocol snapshot | emergency latch | Transient | Reject frame, attempt neutralization, remain armed for good snapshots/heartbeat restart. |
| Controller command transport failure | emergency latch | Transient unless release cannot be restored | Enter recovery; heartbeat/reconnect remains armed. Total-window exhaustion latches safely. |
| Progress watchdog: target distance/player position do not improve | silent RUNNING with no useful action | Transient | After configurable 45 seconds, classify reason, pause/release, reset terminal state through validated recovery. |
| Progress watchdog: no candidates and patrol does not move | silent idle/wander | Transient | Same validated recovery; reason identifies no-target/patrol stall. |
| Progress watchdog: nonzero movement command but no player movement | stuck/input failure | Transient | Same recovery; terminal stuck logic gets a reset, then worker restart escalation. |
| Recovery remains invalid for 20 seconds | indefinite recovery | Transient escalation | Exact-worker restart; repeated attempts use capped backoff. |
| Recovery remains invalid for 300 seconds | indefinite recovery | Unrecoverable for current run | Emergency release and latched `SAFE_STOP` with state, trigger, last player/sequence, zone, counts, actions, and abandonment reason. |
| Invalid polygon/settings/offset policy failures | unsafe to execute | Unrecoverable until corrected | Latched safe stop; user correction/reset required. |

## Child adapter paths

`ui_bot/runtime_child.py` continues to publish snapshots while terminal automation is
paused. Its local safety gate still queues pause on invalid Memory readiness so input
neutralization does not depend on UI rendering latency. The parent now owns recovery
intent and sends resume only after validation. Pixel mode does not depend on player
memory and therefore does not pause for a missing player read.

Window discovery failure, controller/backend construction failure, or an unexpected
exception exits the child nonzero. `ProcessRuntime` classifies that as unexpected,
reconnects with bounded backoff, and `AppController` preserves run intent. A child that
hangs instead of exiting is covered by the snapshot heartbeat.

## Terminal automation branches

These are operational self-healing modes, not UI pause transitions:

| Condition | Existing terminal behavior preserved |
|---|---|
| Memory scanner starting, empty unit list, or `no monster` | Temporary Pixel fallback; scanner continues. |
| Scanner thread exception/death | Sweep retries; dead thread is recreated. |
| Temporary local-player read miss | `MEM_LOST_FRAMES` tolerance; no cached coordinate actuation. UI recovery adds immediate outer neutralization when readiness is lost. |
| Calibration unavailable/fails | Delayed recalibration and Pixel fallback. |
| No eligible monster in selected area | Committed area wander; progress watchdog detects a wander that produces no movement. |
| All ranked monsters outside area | Rejection is current-scan data; wander/return continues. Zone and player geometry remain enforced. |
| Held target disappears/dies/pointer is reused/leaves zone | Clear or reject held target and choose from next fresh candidates. |
| Invisible/unattackable monster | Neutral handled mode until invisibility clears; liveness TTL rechecks. |
| Monster/loot engagement timeout | Temporary stable-identity blacklist; choose another candidate. |
| Route search budget exhausted | Straight-walk fallback; never zero-stick merely because budget was capped. |
| Route proves sealed target | Blacklist target as `walled`; choose another. |
| Fast wedge | Wall fan plus alternating sideways/back escape. |
| Slow no-progress route | Mark wall, replan, and eventually blacklist/choose another. |
| Player outside selected area | Routed committed return with final segment guard. |
| Player farther than `AREA_ABANDON` from area | Intentional fail-closed wrong-map hold. UI progress recovery cannot disable the fence; after the total recovery window it reports and safely stops. |
| Reconnect screen transient | Existing bounded screen matching/reconnect flow. Repeated false-positive screen detection disables that click path; process/progress supervisors remain escalation. |
| Input/backend method raises | Exception escapes child, exact worker is restarted and revalidated. |

## Zone rejection count

`zone: rejected N` is recomputed from the current `ranked` terminal monster set on each
target pass. It is not cumulative. The UI `entities=N` number is a separately capped,
deduplicated publication intended for rendering; it need not equal terminal ranking.
Changes such as 195 to 200 are expected when the sweep, liveness TTL, positions, and
classifications change.

The UI forwards the first count, a material change (at least five and ten percent),
or the latest changed count after a 15-second interval. Small per-scan churn is
suppressed; terminal behavior and non-zone output are unchanged.

## Permanent stops

Only these paths intentionally have no automatic exit:

1. Explicit Pause or Stop.
2. Explicit Emergency Stop until Reset E-Stop.
3. UI close/forced shutdown.
4. Configuration or structural-memory failures that cannot be executed safely.
5. Recovery-window exhaustion after bounded resume and exact-worker restart attempts.

The fifth path logs the state before failure, trigger, last valid player position and
sequence, selected zone, current entity/candidate counts, actions attempted, elapsed
window, and exact abandonment reason.
