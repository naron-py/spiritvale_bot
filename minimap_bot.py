"""SpiritVale minimap bot: chase nearest red dot with left stick.

deps: pip install mss opencv-python numpy pygetwindow
  virtual pad:  pip install vgamepad     (needs the ViGEmBus driver, one-time)
  real HID pad: pip install pyserial     (Leonardo running arduino_joystick_leonardo_v1.ino)

usage:
  python minimap_bot.py               # vgamepad
  python minimap_bot.py --port COM5   # Arduino Leonardo
  python minimap_bot.py --demo        # offline self-check
"""
import heapq
import json
import math
import os
import random
import struct
import sys
import threading
import time

import cv2
import numpy as np

from farming_zone import CircleZone, PolygonZone

WINDOW_TITLE = "SpiritVale"
# Square capture box centred on the player arrow. cx/cy are fractions of the
# client area, r is a fraction of client width (the chase radius).
# ponytail: this is the calibration knob -- run --snap and nudge until the cyan
# cross lands on the white arrow.
MINIMAP = dict(cx=0.927, cy=0.152, r=0.055)
SPEED = 1.0              # stick magnitude while chasing, 0..1
DEADZONE_PX = 4          # blob this close to center = arrived
# Dots this close to the character are never targeted: a monster we have arrived
# at, or a fixed red UI element at the centre. It was briefly widened to 35 to
# swallow the player's own pet as well, which does not work -- the pet wanders off
# to pick up items, so no radius covers it without blinding the bot to real
# monsters. Excluding the pet needs to know which dot it is, not where it is.
CONCEAL_PX = 20
LOST_HOLD_S = 1.0        # keep last heading this long after a dot vanishes
TARGET_TRACK_PX = 20     # same marker can move this far between 20Hz captures
TARGET_ARRIVE_PX = 28    # near-centre disappearance means arrival; keep > CONCEAL_PX
TARGET_FLICKER_FRAMES = 2  # tolerate this much point-blank occlusion, no more
STUCK_TIMEOUT_S = 3.0    # no meaningful approach this long = inaccessible target
STUCK_PROGRESS_PX = 4    # cumulative distance gain that restarts the timeout
STUCK_MIN_DIST_PX = 35   # never abandon a target already within attack approach range
TARGET_IGNORE_S = 8.0    # retry an inaccessible monster later in case it moved
ATTACK_MASH = False      # False = hold L1 down; True = tap it on the cycle below
ATTACK_PERIOD_S = 0.40   # mash cycle, ignored while ATTACK_MASH is False
ATTACK_HOLD_S = 0.15     # how long L1 stays down each mash cycle
BUFF_PERIOD_S = 60.0     # recast the buff sequence this often
BUFF_SEQUENCE = ("up", "down", "left", "right", "x", "a")
BUFF_EARLY_REFRESH_S = 5.0  # renew before the configured duration expires
BUFF_ATTACK_INTERVAL_S = 0.80  # normal attacking required between buff taps
BUFF_INITIAL_STAGGER_S = BUFF_ATTACK_INTERVAL_S
BUFF_COMBAT_DEFER_S = 2.0  # one brief deferral when a due buff meets close combat
BUFF_REPEAT_GAP_S = 0.15  # a second bounded tap confirms each cast under game lag
BUFF_TAPS = 2
SPAM_BUTTON = "y"       # button tapped on its own timer while running; None = off
SPAM_PERIOD_S = 2
SPAM_HOLD_S = 0.05
WAKE_AMP = 0.5           # stick nudge that flips the game into controller mode
WAKE_STEP_S = 0.15
WAKE_SETTLE_S = 0.5      # grace after the nudge before the first button press
BUFF_HOLD_S = 0.25       # each d-pad press
BUFF_GAP_S = 0.80        # pause between presses -- must outlast the cast animation
MIN_BLOB_AREA = 6        # px, filters compression speckle
# ponytail: the box centre is the player, so MINIMAP cx/cy is now load-bearing --
# it used to only have to contain the marker. Run --snap after any UI change.
# Red mushroom caps painted on the terrain are what the bot kept walking to.
# Size cannot separate them -- an occluded cap is a sliver the size of a dot, and
# merged dots are the size of a cap. Colour can: monster dots are drawn pure red
# (H 0, S 255), every mushroom pixel is desaturated pink (S 94-154). Anything
# under this floor is terrain art. Sample a stray blob's HSV before touching it.
RED_S_MIN = 200
WHITE_S_MAX = 65         # player dots are bright neutral white
WHITE_V_MIN = 190
PLAYER_AREA = (20, 120)  # measured 8-10px dots; rejects large white UI artwork
PLAYER_SIDE = (5, 13)
PET_NEAR_PX = 18         # pet centres measured 11.8px and 14.8px from their player
PET_RELEASE_PX = 24      # hysteresis: do not flicker at the entry threshold
PET_CONFIRM_FRAMES = 3   # a monster crossing a player for one frame stays targetable
PET_TRACK_STEP_PX = 20   # maximum marker movement between 20Hz captures
PET_FORGET_FRAMES = 40   # remember a vanished confirmed pet for about two seconds
# Reconnect after a disconnect: click Ok, pick the server, play the character.
# Every coordinate is a fraction of the client area, measured off screenshots taken
# at two different resolutions (1920x1080 and 2560x1440), so they survive a
# resolution change the same way MINIMAP does. Buttons are all centred on x.
RECONNECT = True          # False disables the whole thing, clicks included
RECONNECT_POLL_S = 2.0    # how often to look for a login screen while running
RECONNECT_SETTLE_S = 1.5  # wait after each click; these screens animate
RECONNECT_STAGE_TIMEOUT_S = 10.0  # existing short retry wait before attempt 2
RECONNECT_RETRY_MIN_S = 5.0
RECONNECT_RETRY_MAX_S = 30.0
# A screen that will not advance is either stuck or was never there. Attempt 2
# uses the short settle wait; later attempts draw a fresh bounded random delay.
# The existing ceiling then stops clicks and automation.
RECONNECT_MAX_REPEAT = 5
UI_BLUE = ((95, 90, 150), (112, 255, 255))    # the game's button blue, in HSV
# (x, y, width) fractions. Width matters: Connect and Play sit only 0.033 apart
# vertically, close enough that either matches the other on position alone -- the
# first live test read the character screen as the server screen for exactly that
# reason. Their widths are nearly 2x apart, so size is what separates them.
OK_BTN = (0.500, 0.144, 0.073)       # "Ok" on the disconnected modal
CONNECT_BTN = (0.500, 0.915, 0.081)  # "Connect" under the server table
PLAY_BTN = (0.500, 0.948, 0.147)     # "Play Character" on the character screen
# The server table reorders itself, so SEA has no fixed row -- it is found by its
# own label. The template is cut from a 1920-wide screenshot and rescaled to the
# live client width; matching a simulated 2560 screen scores 0.98, and a screen
# without the text scores 0.34. Recut it if the game restyles the list.
SEA_ICON = "sea_row.png"
SEA_REF_W = 1920
SEA_MATCH_MIN = 0.70
# Stable two-line message crop from the 1911x1073 idle-disconnect reference.
# The modal and Ok button are shared with ordinary disconnects, so only the text
# can distinguish this trigger. Search only the top-centre modal area.
IDLE_ICON = "idle_disconnect.png"
IDLE_REF_W = 1911
IDLE_MATCH_MIN = 0.72
IDLE_SEARCH = (0.38, 0.055, 0.62, 0.14)
# Dark modal body, sampled either side of the message text. Well clear of the Ok
# button, which spans x 0.464-0.536: probing right beside its edge left 0.004 of
# margin, which is no margin at all.
MODAL_DARK = ((0.44, 0.093), (0.55, 0.093))
PANEL_WHITE = (0.42, 0.60)    # white body of the server table
# Dark backdrop of the character screen. Every probe must sit clear of the
# character itself: (0.30, 0.50) was over the model, and a Weaver holding a lit
# cyan axe read 255 there, so the screen never matched and the bot sat on it
# without ever pressing Play -- measured live, the button was found at exactly
# its nominal spot the whole time. Edges only: the model, its pet and its
# weapon all live in the middle.
CHAR_BG = ((0.03, 0.50), (0.50, 0.06), (0.70, 0.85), (0.06, 0.60))
# Targeting from the game's own unit list rather than from red pixels. It knows
# what a thing IS -- monster, pet, player -- so the pet, other players' pets,
# mushroom terrain art and the minimap's rotation all stop mattering at once.
# Falls back to the screen path when the offsets go stale, which a patch does.
# The master switch. `targeting_mode()` picks per run; this only says the
# memory path may be used at all.
MEMORY_TARGETING = True
MEM_REFRESH_S = 2.0      # rediscovering units scans GBs; positions are re-read
MEM_RANGE = 70.0         # world units; roughly what the minimap used to cover
# The region caches (hot, hot_loot) narrow each sweep to the heap regions that
# held units/drops when they were built, and are only rebuilt on a relog. New
# monsters and drops spawn in fresh regions, so a bot that walks away keeps
# sweeping the old regions and sees only the pooled corpses left behind there.
# Past this radius from where a cache was built, the cache is dropped and the
# next sweep is a full pass that rebuilds it where the character is now.
HOT_RENARROW_RADIUS = 80.0
# Backstop for the movement re-narrow: a narrowed cache can go stale for
# reasons walking doesn't catch (everything in the old regions despawns). One
# un-narrowed pass every this long is a cheap safety net -- it runs in the
# background and the bot works from the cached list while it runs.
HOT_SELF_HEAL_S = 300.0
# A narrowed sweep that comes back empty this many times in a row is sweeping
# stale regions, not an empty world (the player name and loot still read), so
# the cache is dropped and the next sweep is a full pass. Recovers a stale
# cache in seconds instead of waiting out HOT_SELF_HEAL_S.
HOT_EMPTY_STREAKS = 3
MEM_CAL_PUSH_S = 0.7     # per calibration push, two of them
MEM_CAL_MIN = 0.5        # world units a push must move us to count
MEM_CAL_LEGS = 3         # pushes to fit the basis from; more resists shoving
TARGET_SWITCH = 0.7      # only swap targets for one this much nearer
# Standing on the monster, the direction to it flips every frame -- measured at a
# median of 0.4 world units, which is the bot wiggling left and right on the
# spot. Inside this, stop steering and just hit it.
MEM_ARRIVE = 2.5         # world units
# Memory hit-and-run is movement-only. Attack stays on while a valid target
# exists; this band only decides whether the left stick approaches, stops, or
# backs out.
# ponytail: these two are the calibration knobs -- the range the game actually
# swings at is unmeasured, and --fightlog is what measures it.
min_distance = 1.8            # closer than this, move directly away
resume_distance = 2.5         # keep backing out until this much space exists
assert resume_distance > min_distance
# Backward-compatible names for older tests/probes that tune the same band.
too_close_distance = min_distance
retreat_stop_distance = resume_distance
MEM_ORBIT_MIN = too_close_distance
MEM_ORBIT_MAX = retreat_stop_distance
MEM_ORBIT_SPEED = 1.0
MEM_ORBIT_FLIP_S = 1.5
MEM_ORBIT_MIN_MOVE = 0.6
# --fightlog: print distance against the target's health every frame we are on
# one. Which distances actually take health off is the only way to set the two
# constants above, and guessing them is what this exists to stop.
FIGHT_LOG = "--fightlog" in sys.argv
# A target that will not die -- already dead and still listed, unreachable, or
# simply not attackable -- otherwise parks the bot on the spot forever, swinging
# at it. Give up on one after this long and leave it alone for a while.
MEM_ENGAGE_MAX_S = 8.0
MEM_IGNORE_S = 20.0
CAL_RETRY_S = 15.0       # wait this long before trying to calibrate again
# A position read can come back empty for a frame without our unit being gone.
# Tearing the calibration down on the first miss cost a whole run: the bot went
# silent and only a double-End brought it back. Insist on a run of misses.
MEM_LOST_FRAMES = 5
# A healthy calibrated unit can still be paired with a narrowed heap sweep that
# missed newly allocated monsters after a map/session transition. Pixels seeing
# targets continuously while memory says "no monster" is direct evidence of that
# disagreement. Re-open the full heap after this long, but not on every quiet
# patch: a full sweep reads gigabytes and takes roughly 14 seconds.
MEM_PIXEL_RESCAN_S = 8.0
MEM_FULL_RESCAN_COOLDOWN_S = 60.0
# Reading every unit's position every frame cost 9 ms with 1237 of them on the
# map. Units far away cannot become the nearest one within a frame, so they are
# refreshed on a timer and only the near ones every frame.
NEAR_KEEP = 100.0        # world units; refreshed every frame inside this
# Everything outside NEAR_KEEP is refreshed a slice at a time rather than all at
# once: one big sweep put a 77 ms spike in a 50 ms frame budget, which is a
# visible stutter in the stick. Spread over this many frames it is invisible.
SWEEP_FRAMES = 20
# Whether a unit is rendered and alive barely changes between frames, and the
# pooled ones never do. Re-asking every frame walked hundreds of dead entries
# looking for the first live one; a short cache makes that a handful of reads.
LIVE_TTL_S = 0.4
# Picking loot up. Only worth doing between kills, so it runs when the monster
# path has nothing: walking off to an item mid-fight is how a bot dies.
LOOT_PICKUP = True
LOOT_RANGE = 40.0        # world units; do not cross the map for a drop
# Nearest-wins alone left items on the ground: on a busy map a monster is
# almost always the nearer of the two, so a drop a few steps away lost every
# arbitration until it despawned. Inside this radius the item wins outright --
# it is seconds of walking and the monster is still there afterwards. Melee is
# still exempt: walking out of a fight already joined is how a bot dies, and a
# drop under our feet is collected by LOOT_BUTTON regardless.
LOOT_FIRST_RANGE = 40.0  # world units; 0 turns this off and restores nearest-wins
LOOT_ARRIVE = 2.0        # world units; where LOOT_BUTTON is pressed
LOOT_BUTTON = "lt"       # left trigger picks the item up
LOOT_HOLD_S = 0.12
LOOT_TAP_GAP_S = 0.5     # between presses while standing on a drop
# A drop we cannot actually collect -- out of reach, someone else's, already
# gone -- otherwise holds the bot on the spot pressing a trigger forever.
# A drop at the edge of LOOT_RANGE is ~2.8s of walking at 14.3 units/s, but a
# wall detour, a monster in the way or a wedge escape all spend the clock too,
# and 6.0 gave up on items the bot was still walking toward. This counts only
# time actually spent going for it -- main() restarts it on any frame the item
# loses the arbitration -- so it is a real walking budget, not a wall clock.
LOOT_MAX_S = 6.0
LOOT_IGNORE_S = 30.0
# Which items to walk to, matched against the name the tooltip shows. The config
# is one case-insensitive substring per line, so "Card" takes Bee Card, Rooster
# Card and any card added later. Leave only comments/blank lines to take every
# item. `python memscan.py --loot` prints the names lying around you.
LOOT_NAMES_FILE = "loot_names.txt"


def load_loot_names(path=None):
    """Wanted item substrings from the user-editable text file."""
    path = path or os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                LOOT_NAMES_FILE)
    try:
        with open(path, encoding="utf-8") as fh:
            return tuple(line.strip() for line in fh
                         if line.strip() and not line.lstrip().startswith("#"))
    except OSError:
        # A deleted/misplaced config must fail closed, not silently change the
        # bot to collecting every item on the map.
        return None


LOOT_NAMES = load_loot_names()
# --lootlog: why a drop was or was not walked to, every frame. An item ignored
# at the character's feet has three possible causes -- not in the sweep's cache
# at all, blacklisted, or losing the arbitration -- and they look identical from
# outside the process.
LOOT_LOG = "--lootlog" in sys.argv
# The anchor/leash/patrol feature was cut as buggy: End is a plain toggle again
# and the bot roams wherever the kills lead. It is in git history if the idea is
# revisited -- the minimap scale tracking went with it, since sizing the leash
# circle in world units was the only thing it was ever for.
TOGGLE_VK = 0x23         # End, polled globally through GetAsyncKeyState
START_PAUSED = True      # launching the script must never move the character
# The bot steers by feeding a minimap delta straight to the stick, which is only
# right while the two frames agree. They do not always: the minimap rotates with
# the camera, measured at 0 degrees on one map and 90 on another, and at 90 every
# heading the bot takes is wrong -- it circles a monster instead of reaching it.
# Relogging resets the camera to north, so this checks rather than corrects.
CAMERA_CHECK = True      # False skips the startup measurement entirely
CAMERA_MAX_DEG = 20      # refuse to run past this; relog to reset the camera
CAMERA_LEG_S = 0.8       # per direction, four of them
LOOP_HZ = 20
DASHBOARD_HZ = 4          # terminal redraws; memory work remains on its 2s sweep

# Walking round walls. There is no grid to read: checked against the game's own
# global-metadata.dat, which carries every class name as plain text. A* Pathfinding
# Project is absent entirely, and every Grid/Tile name in there belongs to something
# else -- FishNet's observer spatial hash, MongoDB GridFS, Unity UI. Walkability is
# a Unity NavMesh (the game calls SnapToNavMesh / TryGetNavMeshPosition), whose
# polygons are native Detour structures in UnityPlayer.dll: undocumented, moves with
# the Unity version, and properly queryable only by calling into the process, which
# would end this bot's read-only guarantee. So the walkable area is *learned*
# instead, from two readings that cost nothing:
#   - our own position against the stick we sent: no headway means a wall ahead
#   - every other unit's position: monsters and players walk on that same navmesh,
#     so anything that moved between sweeps has just proven its ground walkable
# Cells nothing has ever touched still route as passable, or a fresh map could never
# leave the spot it started on.
PATHFIND = True
WALK_CELL = 1.5          # world units per grid cell; under MEM_ARRIVE (2.5)
# Walking is ~14.3 units/s measured, so ~0.7 units per frame at LOOP_HZ. Speed is
# the wrong test: Unity slides a character along the wall it is pushed into, so
# travel stays near full while nothing is gained. What a wall actually takes away
# is *progress along the direction we asked for*. Head-on gives 0, a 45-degree
# slide gives ~0.5 and is real headway, so this sits between them.
# ponytail: a calibration knob, not a measurement -- --walklog prints the number.
WALK_BLOCK_PROGRESS = 0.25   # world units per frame, projected onto the push
# Only frames that carried a *fresh* position count. UNIT_POSITION is
# _lastValidPosition -- server-validated, not the live transform -- and it
# repeats for several frames at a time as a matter of course. Measured at 20 Hz:
# walking by hand, 4% of frames repeated with one run of 8; with the bot driving,
# runs of 3, 4, 5, 6 and 7 inside a single 8 s window. So a repeat is the normal
# state of the feed, not evidence of anything, and counting consecutive frames
# cannot separate the two cases -- a slow feed and a wall both read as "did not
# move". At 6 that fired twice in 8 s, roughly 15 fake walls a minute, and the
# bot spent the session walking three steps and turning away from nothing.
WALK_BLOCK_FRAMES = 6    # consecutive *fresh* such frames before calling it a wall
# A repeat says nothing until it lasts far longer than the feed's own hiccups.
# The longest measured is 8 frames (0.4 s), so this is well clear of it, and it
# is the only sensor that can see a character the physics has jammed: wedged, no
# Being jammed cannot be timed off repeated reads either, and a threshold was
# tried and failed: 1.0 s was set against a measured worst stall of 8 frames,
# then a loaded run produced runs of 15, 17 and 26 frames (1.3 s) with the
# feed down at 12.4 Hz, and the bot called every one of them a jam. Any such
# limit races a feed that slows under load. What separates the two is that a
# stall *ends* with the position jumping to where the character actually
# walked, while a jammed one reads the same after it as before -- so measure
# displacement across a window and let the sample count be whatever it is.
# Walking covers ~14 units/s, so a free character clears tens of units here.
WALK_JAM_S = 2.5         # window the jam test measures displacement over
WALK_JAM_MIN = 1.5       # world units of travel in it that counts as moving
# The push test only catches a steep hit. Measured against 0.81 free walking, a
# slide sees 0.81*cos(angle into the wall)^2: 0.00 head on, 0.20 at 60 degrees --
# both caught -- but 0.41 at 45 and 0.61 at 30, which read as ordinary walking.
# Raising the limit to catch those was rejected: real walking dips that low on
# slow ground, against a monster body, and through every corner, and each would
# write a wall onto open ground. A shallow slide is also often *right* -- it is
# how a character rounds a corner. So the second test is not "am I sliding" but
# "am I getting closer", judged slowly. Free walking covers ~14 units in the
# window, so this bar is deliberately near the floor: it fires only when the bot
# is genuinely trapped, never merely slow.
WALK_STUCK_S = 1.2       # window the slow sensor judges over
WALK_STUCK_MIN = 1.5     # world units of net approach that counts as arriving
WALK_GOAL_JUMP = 5.0     # goal moved this far: restart the window, judge nothing
WALK_BLOCK_AHEAD = 2.0   # world units ahead of us the wall gets marked
WALK_BLOCK_ARC = 0.6     # radians either side of it also marked, two deep
# A router cannot free a character the physics has jammed. Wedged into a rock
# the bot pushed the same heading for minutes: it never moved, so it could only
# ever mark one cell, the route came back unchanged, and it pushed again. Back
# off sideways for a moment instead -- that is a physical answer to a physical
# problem, and the map plus the router take it from there.
WALK_ESCAPE_S = 0.6      # how long the sidestep runs
WALK_ESCAPE_TURN = 2.4   # radians off the blocked heading (~135 degrees, back)
WALK_ESCAPE_GIVEUP = 3   # escapes on one target before it is not worth chasing
# The escape counter is per-location, not per-target. A wall does not care which
# monster you are chasing, and a pack behind one wall cycles through its
# members: a per-target counter resets on every switch, so the bot unwedged and
# chased the same wall forever. The counter is anchored to where it first wedged
# and clears only once the character is this far from that point, so it counts
# unwedges at the same spot rather than across the whole session.
WALK_WEDGE_RESET = 10.0  # world units from the wedge point before the counter clears
# When the escape counter trips, ignore every monster within this of the one we
# gave up on, not just that one. A pack behind the same wall is a handful of
# units apart, and ignoring only the chased member would have the next one
# walked into the same wall a frame later. Wide enough to cover a local cluster,
# narrow enough not to blank the whole MEM_RANGE.
WALK_WEDGE_CLUSTER = 30.0
# Another player or a monster standing in the way reads exactly like a wall.
# Two sightings, a decay, and clearing any cell we later stand in are what stop
# those from rotting into the map permanently.
WALK_BLOCK_HITS = 2      # observations before a cell is really blocked
WALK_DECAY_S = 300.0     # a blocked mark this old is forgotten
WALK_REPLAN_S = 0.5      # recompute a route at most this often
WALK_WAYPOINT = 3.0      # aim this far along the route
# What a step costs the router. Ground something has walked on is cheaper than
# ground nothing has, so a route prefers proven floor -- but unknown is only
# three times the price, not forbidden, or the bot would never explore.
WALK_FLOOR_COST = 1
WALK_UNKNOWN_COST = 3
WALK_MAX_CELLS = 8000    # search expansion ceiling; past it, walk straight
WALK_PAD = 12            # cells of detour room either side of the straight line
WALK_FILE = "walkmap.json"
WALK_SAVE_S = 30.0       # the map is written from the scanner thread
# ponytail: floor grows without bound over weeks of play; at the cap it simply
# stops taking new cells. Age them out if that ever turns out to matter.
WALK_FLOOR_MAX = 200000  # cells of proven floor kept, ~2 MB of JSON
WALK_LOG = "--walklog" in sys.argv   # print what the wall sensor sees
# --targetlog: one line every time the chased monster changes. A bot that
# walks left, right, left is either following one monster that is running
# or flipping between two, and those need opposite fixes.
TARGET_LOG = "--targetlog" in sys.argv

# A named patch of ground to farm. Walked areas remain a cell mask for irregular
# roads and clearings; `--circle` records an exact centre/radius for round camps.
# The two shapes share the same targeting, return, wander and routing policy.
# Deliberately 2x WALK_CELL, and that is the hysteresis quantum: one cell of
# commit is ~4 frames of travel at the measured ~0.7 units/frame. At WALK_CELL
# it would be two frames, which is single-frame-flap territory -- the exact
# failure the leash died of.
AREA_CELL = 3.0          # world units per cell
# Must stay at least ~1.5x AREA_CELL or `core` comes out empty and the bot can
# never finish walking back in. recore() falls back rather than hang, but this
# is a real tuning trap.
AREA_BRUSH = 6.0         # painted either side of the walked line, ~12 wide
AREA_STEP = 1.5          # recorder repaints only after moving this far
AREA_SAMPLE_S = 0.2      # nobody walks AREA_STEP in less
AREA_SAFETY = 3.0        # controller aims this far inside an exact boundary
AREA_LOOKAHEAD = 3.0     # world units used to validate the next stick step
AREA_BOUNDARY_LOG_S = 2.0
AREA_WANDER_HOLD_S = 6.0 # give up on one wander point after this
AREA_WANDER_REACHED = 4.0
# A point picked closer than AREA_WANDER_REACHED counts as arrived the moment
# it is chosen, so the next frame picks another -- 20 changes of direction a
# second, which from outside is a character shaking left and right on the spot.
# Two answers, and both are needed: prefer somewhere actually worth walking to,
# and refuse to change target more often than this whatever happens, so even an
# area too small to hold a distant point cannot flap.
AREA_WANDER_MIN = 12.0   # how far away a new wander point should be
AREA_WANDER_TRIES = 12   # random picks tried before taking what we can get
AREA_WANDER_COMMIT_S = 1.5
# Further out than this is another map, not a stray step -- nothing records
# which map an area belongs to, so `--area` on the wrong one puts home
# thousands of units away. Walking "back" then is an hour of leaning into
# scenery on the wrong continent.
# ponytail: store a map id beside the cells if this ever fires by accident.
AREA_ABANDON = 150.0     # further than this from home: give up, unfence
AREA_RETURN_MAX_S = 45.0 # returning this long means it is not working
# Next to the script, not in the cwd: WALK_FILE is a bare relative name, so
# recording from one directory and running from another would silently disagree.
AREA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "areas.json")


def wake_controller(pad):
    """SpiritVale stays in keyboard mode until it sees stick motion, and button
    presses sent before that are dropped. A there-and-back nudge flips it."""
    # L1 stays down through every push that moves the character: a warp portal
    # takes a walker but not an attacker, and a map change tears down our unit,
    # the basis and the walk map. Same reason in calibrate() and camera_check().
    for sx, sy in ((0.0, WAKE_AMP), (0.0, -WAKE_AMP), (0.0, 0.0)):
        pad.stick(sx, sy, True)
        time.sleep(WAKE_STEP_S)
    # The game eats the first button press while it swaps input modes, which is
    # why the leading d-pad press went missing. Let the swap finish.
    time.sleep(WAKE_SETTLE_S)


def toggle_key_hit(get_state=None):
    """True once per physical End press, even when another window has focus.

    GetAsyncKeyState's low bit means 'pressed since the last call', so polling it
    is already edge-detected -- no key hook, no extra dependency.
    """
    if get_state is None:
        import ctypes
        get_state = ctypes.windll.user32.GetAsyncKeyState
    return bool(get_state(TOGGLE_VK) & 1)


def automation_state_request():
    """UI-only recovery hook; standalone runs never synthesize an End press."""
    return None


def controller_config_request():
    """UI-only live input configuration hook; standalone uses safe defaults."""
    return None


def hold_still(mode):
    """True when memory targeting means "stop", not merely "nothing here".

    main() reads a zero stick as handled and never falls through to the pixel
    path. Both an empty monster list and a missing unit while the background
    scanner is still landing must temporarily hand control to pixels. An active
    farming area is guarded separately by area_holds(), because pixels cannot
    enforce a world-coordinate fence.
    """
    return mode not in ("no monster", "no unit")


def area_holds(eyes, requested=None):
    """True when a fence has a real stop state, not a scanner-wait state.

    The user explicitly wants pixels to move and attack during every initial,
    relog, and recovery scan. Before calibration lands those vectors cannot be
    checked against a world-coordinate area; once basis/position exist the final
    guard_area_step() still fences them. Construction failure and honest stop
    modes continue to hold neutral rather than silently roam.
    """
    if eyes is None:
        return requested is not None
    if eyes.mode in ("no monster", "no unit"):
        return False
    return eyes.area is not None


def attack_active(now, buffing=False, blocked=False):
    """Whether the attack buttons should be down."""
    # `buffing` is retained for compatibility with old probes. Routine buffs no
    # longer own or reset attack; only an honest combat/safety block releases it.
    if blocked:
        return False
    return (now % ATTACK_PERIOD_S) < ATTACK_HOLD_S if ATTACK_MASH else True


def tap_buff(pad, key, hold):
    """Tap one ordered buff control through the active pad backend."""
    if key in ("x", "a"):
        pad.tap_button(key, hold)
    else:
        pad.tap_dpad(key, hold)


class BuffScheduler:
    """Per-buff, tick-driven press/release scheduler independent of attack."""

    def __init__(self, now=0.0, slots=None):
        self.buffs = {}
        self.order = []
        self.active = None
        self.phase = None
        self.taps = 0
        self.release_at = 0.0
        self.next_cast_at = 0.0
        self.deferred = set()
        self.last_released = False
        self.configure(slots or [
            {"id": f"buff-{index}", "name": f"Buff Slot {index}",
             "enabled": True,
             "button": (f"dpad_{key}" if key in ("up", "down", "left", "right")
                        else key), "order": index - 1}
            for index, key in enumerate(BUFF_SEQUENCE, 1)
        ], now)
        self.reset(now)

    def configure(self, slots, now, pad=None):
        """Atomically replace slot bindings while preserving matching clocks."""
        parsed = []
        for index, raw in enumerate(slots):
            slot_id = str(raw["id"])
            button = str(raw["button"]).lower()
            if not slot_id or button not in (
                    "dpad_up", "dpad_down", "dpad_left", "dpad_right",
                    "a", "b", "x", "y", "lb", "rb", "lt", "rt"):
                raise ValueError(f"invalid buff slot {slot_id!r}")
            parsed.append({
                "id": slot_id, "name": str(raw.get("name") or slot_id),
                "enabled": bool(raw.get("enabled", False)), "button": button,
                "order": int(raw.get("order", index)),
            })
        if len({slot["id"] for slot in parsed}) != len(parsed):
            raise ValueError("buff slot IDs must be unique")
        old = self.buffs
        parsed_by_id = {slot["id"]: slot for slot in parsed}
        active_slot = parsed_by_id.get(self.active)
        keep_active = bool(
            active_slot is not None and active_slot["enabled"]
            and active_slot["button"] == old[self.active]["button"])
        if self.active is not None and not keep_active and pad is not None:
            pad.release_buff(old[self.active]["button"])
        enabled_index = 0
        rebuilt = {}
        for slot in sorted(parsed, key=lambda item: item["order"]):
            previous = old.get(slot["id"])
            if previous is None:
                last_cast = None
                next_due = now + enabled_index * BUFF_INITIAL_STAGGER_S
            else:
                last_cast = previous["last_cast"]
                next_due = previous["next_due"]
            if slot["enabled"]:
                enabled_index += 1
            rebuilt[slot["id"]] = {
                **slot, "last_cast": last_cast, "next_due": next_due}
        self.buffs = rebuilt
        self.order = list(rebuilt)
        if not keep_active:
            self.active = self.phase = None
            self.taps = 0
            self.release_at = 0.0
        self.deferred.intersection_update(rebuilt)

    def reset(self, now):
        enabled_index = 0
        for slot_id in self.order:
            state = self.buffs[slot_id]
            state["last_cast"] = None
            state["next_due"] = now + enabled_index * BUFF_INITIAL_STAGGER_S
            if state["enabled"]:
                enabled_index += 1
        self.active = self.phase = None
        self.taps = 0
        self.release_at = 0.0
        self.next_cast_at = now
        self.deferred.clear()
        self.last_released = False

    def release_due(self, now, pad):
        """Advance one bounded double-tap without sleeping or global reset."""
        self.last_released = False
        if self.active is None or now < self.release_at:
            return None
        slot_id = self.active
        state = self.buffs[slot_id]
        button = state["button"]
        if self.phase == "gap":
            try:
                pad.press_buff(button)
            except Exception:
                try:
                    pad.release_buff(button)
                finally:
                    self.active = self.phase = None
                    self.taps = 0
                raise
            self.phase = "pressed"
            self.taps += 1
            self.release_at = now + BUFF_HOLD_S
            return None
        pad.release_buff(button)
        self.last_released = True
        if self.taps < BUFF_TAPS:
            self.phase = "gap"
            self.release_at = now + BUFF_REPEAT_GAP_S
            return None
        state["last_cast"] = now
        state["next_due"] = now + BUFF_PERIOD_S - BUFF_EARLY_REFRESH_S
        self.active = self.phase = None
        self.taps = 0
        self.release_at = 0.0
        self.next_cast_at = now + BUFF_ATTACK_INTERVAL_S
        self.deferred.discard(slot_id)
        label = button.removeprefix("dpad_").upper()
        print(f"\n[Buff] {label} complete; "
              f"next_due={state['next_due']:.1f}")
        return slot_id

    def cast_due(self, now, pad, attack_active, combat_priority):
        """Start at most one due tap without sleeping or changing attack state."""
        if (not attack_active or self.active is not None
                or now < self.next_cast_at):
            return None
        due = [slot_id for slot_id in self.order
               if self.buffs[slot_id]["enabled"]
               and self.buffs[slot_id]["next_due"] <= now]
        if not due:
            return None
        slot_id = min(due, key=lambda item: (
            self.buffs[item]["next_due"], self.buffs[item]["order"]))
        state = self.buffs[slot_id]
        button = state["button"]
        label = button.removeprefix("dpad_").upper()
        if combat_priority and slot_id not in self.deferred:
            self.deferred.add(slot_id)
            self.next_cast_at = now + BUFF_COMBAT_DEFER_S
            print(f"\n[Buff] {label} deferred; combat priority")
            return None
        print(f"\n[Buff] {label} due; casting one buff while "
              f"attack_active={attack_active}")
        self.active = slot_id
        self.phase = "pressed"
        self.taps = 1
        self.release_at = now + BUFF_HOLD_S
        try:
            pad.press_buff(button)
        except Exception:
            try:
                pad.release_buff(button)
            finally:
                self.active = self.phase = None
                self.taps = 0
                self.release_at = 0.0
            raise
        return slot_id


def complete_buff_tick(buffs, pad, now, attack):
    """Finish one bounded tap and immediately reassert normal attack input."""
    key = buffs.release_due(now, pad)
    if buffs.last_released and attack:
        pad.reassert_attack()
    return key


def apply_controller_config(buffs, pad, config, now):
    """Validate then hand live buff/attack bindings to the controller loop."""
    if not isinstance(config, dict):
        raise ValueError("controller configuration must be an object")
    buff_slots = config.get("buff_slots")
    attack_slots = config.get("attack_slots")
    if not isinstance(buff_slots, list) or not isinstance(attack_slots, list):
        raise ValueError("controller configuration slots must be lists")
    if len(attack_slots) != 2:
        raise ValueError("controller configuration must contain exactly two attack slots")
    allowed = {"dpad_up", "dpad_down", "dpad_left", "dpad_right",
               "a", "b", "x", "y", "lb", "rb", "lt", "rt"}
    active = []
    attack_keys = []
    slot_ids = set()
    for raw in buff_slots + attack_slots:
        if not isinstance(raw, dict) or raw.get("button") not in allowed:
            raise ValueError("controller configuration contains an invalid slot")
        slot_id, name, order = raw.get("id"), raw.get("name"), raw.get("order")
        if not isinstance(slot_id, str) or not slot_id or slot_id in slot_ids:
            raise ValueError("controller configuration slot IDs must be unique")
        if not isinstance(name, str) or not name:
            raise ValueError("controller configuration slot name is invalid")
        if type(raw.get("enabled")) is not bool:
            raise ValueError(f"{name} enabled state is invalid")
        if type(order) is not int or order < 0:
            raise ValueError(f"{name} order is invalid")
        slot_ids.add(slot_id)
        if raw.get("enabled"):
            active.append((name, raw["button"]))
    owners = {}
    for name, button in active:
        if button in owners:
            raise ValueError(f"controller button conflict: {owners[button]} and {name}")
        owners[button] = name
    for raw in attack_slots:
        if raw.get("enabled"):
            attack_keys.append(raw["button"])
    if not attack_keys:
        raise ValueError("at least one enabled attack skill is required")
    buffs.configure(buff_slots, now, pad)
    pad.configure_attack(attack_keys)


def dashboard_text(info, color=True):
    """Readable terminal dashboard with grouped state and dependency-free ANSI."""
    memory = info.get("memory") or {}
    loot = info.get("loot") or {}
    reset = "\x1b[0m"
    inner_width = 86
    label_width = 16
    value_width = inner_width - label_width - 3

    def paint(text, code):
        return f"\x1b[{code}m{text}{reset}" if color else str(text)

    def row(label, value, tone="97"):
        value = str(value or "-")
        if len(value) > value_width:
            value = value[:value_width - 1] + "…"
        return (f"│ {paint(f'{label:<{label_width}}', '2;37')} "
                f"{paint(f'{value:<{value_width}}', tone)} │")

    def rows(label, values, tone="97"):
        values = tuple(value for value in (values or ())
                       if "[filtered]" not in str(value).lower()) or ("-",)
        return [row(label if i == 0 else "", value, tone)
                for i, value in enumerate(values)]

    def section(title):
        fill = "─" * max(1, inner_width - len(title) - 3)
        return paint(f"├─ {title} {fill}┤", "1;36")

    status = info.get("status") or (
        "RUNNING" if info.get("running") else "STOPPED")
    status_tone = "1;32" if status == "RUNNING" else (
        "1;31" if status == "STOPPED" else "1;33")
    primary = info.get("bot_mode", "minimap").upper()
    source = info.get("source", "pixels").upper()
    source_tone = "1;36" if source == "MEMORY" else "1;35"
    sx, sy = info.get("stick", (0.0, 0.0))
    attack = "ON" if info.get("attack") else "OFF"
    controls = (f"stick {sx:+.2f}, {sy:+.2f}  |  attack {attack}"
                + (f"  |  button {info['action']}" if info.get("action") else ""))
    calibration = "READY" if memory.get("calibrated") else "WAITING"

    title = paint(" SPIRITVALE COMBAT BOT ", "1;97;44")
    title_pad = inner_width - len(" SPIRITVALE COMBAT BOT ")
    lines = [f"╔{'═' * (title_pad // 2)}{title}"
             f"{'═' * (title_pad - title_pad // 2)}╗", section("OVERVIEW"),
             row("Status", status, status_tone),
             row("Primary mode", primary, "1;36"),
             row("Active source", source, source_tone),
             row("Current task", info.get("state", "-"), "1;33"),
             section("COMBAT & CONTROL"),
             row("Target", info.get("target", "none"), "1;35"),
             row("Controller", controls, "1;32" if info.get("attack") else "97"),
             section("MEMORY SCANNER"),
             row("Scanner", memory.get("scanner", "off"), "1;32"),
             row("Calibration", calibration,
                 "1;32" if calibration == "READY" else "1;33"),
             row("Classes", memory.get("classes", "-"), "36"),
             row("Detected", memory.get("counts", "-")),
             section("WORLD ENTITIES"),
             row("Your player", memory.get("player", "unknown"), "1;32")]
    lines += rows("Players", memory.get("players"), "36")
    lines += rows("Pets", memory.get("pets"), "35")
    lines += rows("Monsters", memory.get("monsters"), "31")
    lines += [section("LOOT"),
              row("Summary", f"{loot.get('detected', 0)} on ground  |  "
                             f"{loot.get('wanted', 0)} wanted", "1;33")]
    lines += rows("Wanted items", loot.get("ground"), "33")
    lines += [section("NAVIGATION"),
              row("Movement", info.get("navigation", "-"), "36")]
    if info.get("warning"):
        lines += [section("ATTENTION"),
                  row("WARNING", info["warning"], "1;33")]
    command = "Press END to stop" if info.get("running") else "Press END to start"
    lines += [paint("├" + "─" * inner_width + "┤", "2;37"),
              row("Controls", command + "  |  Ctrl+C to exit", "1;97"),
              paint("╚" + "═" * inner_width + "╝", "1;36")]
    text = "\n".join(lines)
    return text + reset if color else text


def dashboard_snapshot(eyes, running, state, sx=0.0, sy=0.0, attack=False,
                       action="", on_loot=False, distance=None,
                       memory_driving=None, status=None, bot_mode=None):
    """Copy dashboard inputs; every memory value here was cached elsewhere."""
    memory = {"scanner": "off", "classes": "-", "calibrated": False,
              "counts": "-", "player": "unknown", "players": (),
              "pets": (), "monsters": ()}
    loot = {"detected": 0, "wanted": 0, "ground": ()}
    warning = ""
    recovery = ""
    if eyes is not None:
        with eyes.lock:
            report = dict(eyes.scan_summary)
            drops = list(eyes.loot.values())
            report_error = eyes.scan_error
            recovery = getattr(eyes, "recovery", "")
        if eyes.scanner and eyes.scanner.is_alive():
            scanner = ("READY - REFRESHING" if getattr(eyes, "scan_passes", 0)
                       else "SCANNING FIRST PASS")
        else:
            scanner = "NOT STARTED"
        resolved = "+".join(k for k in ("monster", "player", "pet", "loot")
                            if eyes.classes.get(k)) or "none"
        counts = report.get("counts", {})
        memory = {"scanner": scanner, "classes": resolved,
                  "calibrated": bool(eyes.me and eyes.basis),
                  "counts": (f"{counts.get('monster', 0)} monster objects | "
                             f"{counts.get('player', 0)} players | "
                             f"{counts.get('pet', 0)} pets"),
                  "player": report.get("player", "unknown"),
                  "players": tuple(report.get("players", ()))[:6],
                  "pets": tuple(report.get("pets", ()))[:6],
                  "monsters": tuple(report.get("monsters", ()))[:5]}

        name_counts = {}
        for *_, name in drops:
            name_counts[name] = name_counts.get(name, 0) + 1
        wanted = sum(n for name, n in name_counts.items() if wanted_item(name))
        ordered = sorted(((name, n) for name, n in name_counts.items()
                          if wanted_item(name)),
                         key=lambda item: (-item[1], item[0].lower()))
        ground = [f"{name} x{n} [WANTED]" for name, n in ordered[:6]]
        if len(ordered) > 6:
            ground.append(f"+{len(ordered) - 6} more types")
        loot = {"detected": len(drops), "wanted": wanted,
                "ground": tuple(ground)}
        if report_error:
            warning = f"memory report failed: {report_error}"
        elif LOOT_NAMES is None:
            warning = f"{LOOT_NAMES_FILE} missing; loot fails closed"
        elif drops and not wanted:
            warning = f"loot filter matches nothing; edit {LOOT_NAMES_FILE}"

    if memory_driving is None:
        memory_driving = bool(eyes is not None and eyes.me)
    if on_loot and eyes is not None:
        target = f"LOOT {eyes.loot_name or 'unknown'}"
    elif (eyes is not None and eyes.chasing
          and eyes.mode in ("chasing", "far", "on it")):
        # Only while actually heading to that monster. During "going back"
        # and "unwedge" the chase logic has not run this frame, so `chasing`
        # still holds the stale monster that led us out, and `distance` is the
        # home/escape distance -- printing the monster name there reads as the
        # bot heading to a target outside the area when it is walking home.
        target = f"MONSTER {eyes.target_name or 'unknown'}"
    elif memory_driving and eyes is not None:
        # Memory is driving but not on a specific monster: wandering the
        # area, walking back in, or unwedging.  Show the mode, not a
        # phantom pixel target.
        target = eyes.mode.upper()
    else:
        target = "PIXEL red marker" if running and state not in ("no monster", "no unit") else "none"
    if distance is not None:
        target += f" at {distance:.1f}"
    navigation = "-"
    if eyes is not None:
        navigation = f"{eyes.loot_mode if on_loot else eyes.mode} / "
        navigation += "route" if eyes.routing else "direct"
    if not running:
        target = "none"
        navigation = "paused"
    elif status:
        # Login handling has released every control and old targets belong to
        # the vanished gameplay session. Never display that stale frame while
        # reconnect_step() waits for the next screen.
        target = "none"
        navigation = "waiting for gameplay"
    bot_mode = bot_mode or ("memory" if eyes is not None else "minimap")
    if running and bot_mode == "memory" and not memory_driving:
        if eyes is not None and getattr(eyes, "scan_passes", 0):
            warning = recovery or warning or "memory scan ready; pixels active because memory has no target"
        else:
            warning = recovery or warning or "memory first pass pending; pixels are temporary fallback"
    return {"running": running,
            "status": status,
            "bot_mode": bot_mode,
            "source": "memory" if memory_driving else "pixels",
            "state": state.strip(), "stick": (sx, sy), "attack": attack,
            "action": action, "memory": memory, "target": target,
            "loot": loot, "navigation": navigation, "warning": warning}


class TerminalDashboard:
    def __init__(self, bot_mode=None):
        self.last = 0.0
        self.bot_mode = bot_mode
        if os.name == "nt":
            # Python does not always enable VT sequences in classic conhost.
            # Set the console flag directly; no shell window or subprocess.
            import ctypes
            kernel = ctypes.windll.kernel32
            handle = kernel.GetStdHandle(-11)       # STD_OUTPUT_HANDLE
            mode = ctypes.c_uint()
            if kernel.GetConsoleMode(handle, ctypes.byref(mode)):
                kernel.SetConsoleMode(handle, mode.value | 0x0004)

    def update(self, eyes, running, state, sx=0.0, sy=0.0, attack=False,
               action="", on_loot=False, distance=None, memory_driving=None,
               status=None, force=False):
        now = time.monotonic()
        if not force and now - self.last < 1 / DASHBOARD_HZ:
            return
        self.last = now
        info = dashboard_snapshot(eyes, running, state, sx, sy, attack,
                                  action, on_loot, distance, memory_driving,
                                  status, self.bot_mode)
        print("\x1b[2J\x1b[H" + dashboard_text(info), end="", flush=True)


def toggle_running(paused, pad, pet_filter, wake=wake_controller, area=None):
    """Toggle run state, always clearing held controls and stale target state."""
    paused = not paused
    pad.stick(0.0, 0.0, False)
    pet_filter.reset()
    if not paused and area is None:
        wake(pad)
    return paused


def heading_error(pairs):
    """Degrees between where the stick points and where the character goes.

    `pairs` is [((sx, sy), (mx, my))]: the stick vector pushed, and the terrain
    shift the minimap showed. Terrain slides opposite to travel, and image y runs
    downward while stick y runs up, so the character's own motion in stick terms
    is (-mx, +my). Averaged as unit vectors, which a plain mean of angles cannot
    do without wrapping wrongly at 180.
    """
    sx_sum = sy_sum = 0.0
    for (sx, sy), (mx, my) in pairs:
        cx, cy = -mx, my
        if (cx * cx + cy * cy) ** 0.5 < 1.0 or (sx * sx + sy * sy) ** 0.5 < 1e-6:
            continue                       # blocked, or no push: proves nothing
        dot = sx * cx + sy * cy
        cross = sx * cy - sy * cx
        ang = math.atan2(cross, dot)
        sx_sum += math.cos(ang)
        sy_sum += math.sin(ang)
    if sx_sum == 0.0 and sy_sum == 0.0:
        return None                        # nothing usable
    return math.degrees(math.atan2(sy_sum, sx_sum))


def camera_rotation(pad, sct, win, legs=((1.0, 0.0), (0.0, 1.0),
                                         (-1.0, 0.0), (0.0, -1.0))):
    """Measure heading_error live, or None if the character never moved."""
    reg = minimap_region(win)
    han = cv2.createHanningWindow((reg["width"], reg["height"]), cv2.CV_32F)

    def gray():
        img = np.array(sct.grab(reg))[:, :, :3]
        return np.float32(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY))

    pairs = []
    for sx, sy in legs:
        before = gray()
        t0 = time.time()
        while time.time() - t0 < CAMERA_LEG_S:
            pad.stick(sx, sy, True)
            time.sleep(0.05)
        pad.stick(0.0, 0.0, True)
        time.sleep(0.2)
        (mx, my), _ = cv2.phaseCorrelate(before, gray(), han)
        pairs.append(((sx, sy), (mx, my)))
    return heading_error(pairs)


def find_window():
    import pygetwindow as gw
    wins = [w for w in gw.getWindowsWithTitle(WINDOW_TITLE) if w.title.strip() == WINDOW_TITLE]
    if not wins:
        wins = gw.getWindowsWithTitle(WINDOW_TITLE)
    if not wins:
        raise RuntimeError(f"no window titled {WINDOW_TITLE!r}")
    return wins[0]


def minimap_region(win):
    r = int(win.width * MINIMAP["r"])
    x = win.left + int(win.width * MINIMAP["cx"])
    y = win.top + int(win.height * MINIMAP["cy"])
    return dict(left=x - r, top=y - r, width=2 * r, height=2 * r)


def window_region(win):
    return dict(left=win.left, top=win.top, width=win.width, height=win.height)


def find_blue_button(img, btn, tol=0.03, wtol=0.35):
    """Centre (x, y) of the blue UI button matching `btn` = (fx, fy, wfrac), or None.

    Matches on size as well as position. Looking for the button at all, rather
    than sampling a pixel, is what keeps the blue sky and the blue skill icons out
    of it; matching its width is what keeps Connect and Play Character apart.
    """
    h, w = img.shape[:2]
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, UI_BLUE[0], UI_BLUE[1])
    cnts, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    wx, wy, want = w * btn[0], h * btn[1], w * btn[2]
    for c in cnts:
        if cv2.contourArea(c) < w * h * 1e-3:  # skip small blue icons and text
            continue
        x, y, bw, bh = cv2.boundingRect(c)
        cx, cy = x + bw / 2, y + bh / 2
        if (abs(cx - wx) <= w * tol and abs(cy - wy) <= h * tol and
                abs(bw - want) <= want * wtol):
            return int(cx), int(cy)
    return None


_sea_icon = False  # False = not looked for yet, None = missing
_idle_icon = False


def sea_icon():
    """The SEA label template, or None if the file is absent."""
    global _sea_icon
    if _sea_icon is False:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), SEA_ICON)
        _sea_icon = cv2.imread(path)
        if _sea_icon is None:
            print(f"no {SEA_ICON} -- cannot pick the server row")
    return _sea_icon


def idle_icon():
    """The idle-disconnect message template, or None if the file is absent."""
    global _idle_icon
    if _idle_icon is False:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), IDLE_ICON)
        _idle_icon = cv2.imread(path)
        if _idle_icon is None:
            print(f"no {IDLE_ICON} -- idle disconnect uses generic popup handling")
    return _idle_icon


def find_idle_popup(img, template=None):
    """Whether the specific idle-disconnect message is visible in its modal."""
    t = idle_icon() if template is None else template
    if t is None:
        return False
    scale = img.shape[1] / IDLE_REF_W
    if scale != 1:
        t = cv2.resize(t, None, fx=scale, fy=scale,
                       interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_CUBIC)
    h, w = img.shape[:2]
    x0, y0, x1, y1 = (int(w * IDLE_SEARCH[0]), int(h * IDLE_SEARCH[1]),
                      int(w * IDLE_SEARCH[2]), int(h * IDLE_SEARCH[3]))
    roi = img[y0:y1, x0:x1]
    if roi.shape[0] <= t.shape[0] or roi.shape[1] <= t.shape[1]:
        return False
    gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
    needle = cv2.cvtColor(t, cv2.COLOR_BGR2GRAY)
    score = cv2.minMaxLoc(cv2.matchTemplate(
        gray, needle, cv2.TM_CCOEFF_NORMED))[1]
    return score >= IDLE_MATCH_MIN


def find_sea_row(img, template=None):
    """Centre (x, y) of the "Southeast Asia (SEA)" label, or None.

    The list is sorted by ping and reorders between sessions, so the row cannot be
    addressed by position. Clicking the label selects the row it sits in.
    """
    t = sea_icon() if template is None else template
    if t is None:
        return None
    scale = img.shape[1] / SEA_REF_W
    if scale != 1:
        t = cv2.resize(t, None, fx=scale, fy=scale,
                       interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_CUBIC)
    if img.shape[0] <= t.shape[0] or img.shape[1] <= t.shape[1]:
        return None
    res = cv2.matchTemplate(img, t, cv2.TM_CCOEFF_NORMED)
    _, score, _, loc = cv2.minMaxLoc(res)
    if score < SEA_MATCH_MIN:
        return None
    return loc[0] + t.shape[1] / 2, loc[1] + t.shape[0] / 2


def _probe(img, frac, dark):
    h, w = img.shape[:2]
    px = img[int(h * frac[1]), int(w * frac[0])]
    return int(px.max()) < 90 if dark else int(px.min()) > 200


def login_screen(img, idle_template=None):
    """Which login screen is showing, including the specific idle popup.

    Each test pairs a button with something only that screen has behind it, so
    ordinary gameplay -- blue sky above, blue skill icons below -- cannot match.
    The disconnect modal sits on top of the server table, hence the order.
    """
    if (find_blue_button(img, OK_BTN) and
            all(_probe(img, f, dark=True) for f in MODAL_DARK)):
        return ("idle disconnected" if find_idle_popup(img, idle_template)
                else "disconnected")
    if find_blue_button(img, CONNECT_BTN) and _probe(img, PANEL_WHITE, dark=False):
        return "server"
    if (find_blue_button(img, PLAY_BTN) and
            all(_probe(img, f, dark=True) for f in CHAR_BG)):
        return "character"
    return None


class ReconnectFlow:
    """Screen-confirmed reconnect episode with one click action per stage."""

    def __init__(self, stage_timeout=RECONNECT_STAGE_TIMEOUT_S,
                 max_attempts=RECONNECT_MAX_REPEAT, random_wait=None):
        self.stage_timeout = stage_timeout
        self.max_attempts = max_attempts
        self.random_wait = random_wait or (
            lambda: random.uniform(RECONNECT_RETRY_MIN_S, RECONNECT_RETRY_MAX_S))
        self.active = False
        self.failed = False
        self.stage = None
        self.attempts = 0
        self.deadline = 0.0

    @staticmethod
    def _screen_name(screen):
        if screen in ("disconnected", "idle disconnected"):
            return "disconnect popup"
        if screen == "server":
            return "server-selection screen"
        if screen == "character":
            return "character screen"
        return "loading screen"

    @staticmethod
    def _action_name(screen):
        if screen in ("disconnected", "idle disconnected"):
            return "dismiss disconnect popup"
        if screen == "server":
            return "connect to Southeast Asia"
        return "play character"

    @staticmethod
    def _failure_reason(screen):
        if screen in ("disconnected", "idle disconnected"):
            return "disconnect popup still visible"
        if screen == "server":
            return "server-selection screen still visible"
        if screen == "character":
            return "character screen still visible"
        return "valid player data not ready"

    def _arm_next(self, now, events, reason):
        next_attempt = self.attempts + 1
        if next_attempt == 2:
            delay = self.stage_timeout
        else:
            delay = float(self.random_wait())
        events.append(f"[Reconnect] Retry attempt {next_attempt} in "
                      f"{delay:.1f}s: {reason}")
        self.deadline = now + delay

    def action_completed(self, observed_at, completed_at):
        """Keep the full retry delay after blocking click/settle work."""
        if self.active:
            self.deadline += max(0.0, completed_at - observed_at)

    def _after_attempt(self, now, events, reason):
        if self.max_attempts is not None and self.attempts >= self.max_attempts:
            self.deadline = now + self.stage_timeout
        else:
            self._arm_next(now, events, reason)

    def _attempt_log(self, screen, events):
        events.extend((f"[Reconnect] Current screen: {self._screen_name(screen)}",
                       f"[Reconnect] Attempt {self.attempts}: "
                       f"{self._action_name(screen)}"))

    def cancel(self):
        """Cancel one active episode and every pending retry deadline."""
        if not self.active:
            return False
        self.active = False
        self.stage = None
        self.attempts = 0
        self.deadline = 0.0
        return True

    def _fail(self, events):
        stage = "player data" if self.stage == "player" else self.stage
        events.append(f"[Reconnect] {stage} timed out after "
                      f"{self.max_attempts} attempts; reconnect OFF for this run")
        self.active = False
        self.failed = True

    def _enter(self, screen, now, previous, events):
        self.stage = screen
        self.attempts = 1
        if previous == "idle disconnected" and screen != previous:
            events.append("[Reconnect] Idle popup dismissed")
        if screen == "idle disconnected":
            events.append("[Reconnect] Idle disconnection popup detected")
        elif screen == "server":
            events.append("[Reconnect] Server screen ready")
        self._attempt_log(screen, events)
        self._after_attempt(now, events, self._failure_reason(screen))
        return screen

    def observe(self, screen, now, player_valid=False):
        """Return (permitted action, transition logs, reset-memory-now)."""
        events = []
        if self.failed:
            return None, events, False
        if not self.active:
            if screen is None:
                return None, events, False
            self.active = True
            return self._enter(screen, now, None, events), events, False

        if self.stage == "player":
            if screen is not None:
                return self._enter(screen, now, "player", events), events, False
            if player_valid:
                self.active = False
                self.stage = None
                self.attempts = 0
                self.deadline = 0.0
                events.append("[Reconnect] Recovery successful; automation resumed")
            elif now >= self.deadline:
                reason = self._failure_reason(None)
                events.extend(("[Reconnect] Current screen: loading screen",
                               f"[Reconnect] Attempt {self.attempts} failed: {reason}"))
                if self.attempts >= self.max_attempts:
                    self._fail(events)
                else:
                    self.attempts += 1
                    events.append(f"[Reconnect] Attempt {self.attempts}: "
                                  "check valid player data")
                    self._after_attempt(now, events, reason)
            return None, events, False

        if screen is None:
            if self.stage == "character":
                self.stage = "player"
                self.attempts = 1
                events.extend(("[Reconnect] Current screen: loading screen",
                               "[Reconnect] Attempt 1: check valid player data",
                               "[Reconnect] Waiting for valid player data"))
                self._after_attempt(now, events, self._failure_reason(None))
                return None, events, True
            if now >= self.deadline:
                reason = "loading screen still visible"
                events.extend(("[Reconnect] Current screen: loading screen",
                               f"[Reconnect] Attempt {self.attempts} failed: {reason}"))
                if self.attempts >= self.max_attempts:
                    self._fail(events)
                else:
                    self.attempts += 1
                    events.append(f"[Reconnect] Attempt {self.attempts}: recheck screen")
                    self._after_attempt(now, events, reason)
            return None, events, False

        popup = ("disconnected", "idle disconnected")
        if self.stage in popup and screen in popup:
            if self.stage != "idle disconnected" and screen == "idle disconnected":
                self.stage = screen
                events.append("[Reconnect] Idle disconnection popup detected")
            screen = self.stage

        if screen != self.stage:
            previous = self.stage
            return self._enter(screen, now, previous, events), events, False

        if now < self.deadline:
            return None, events, False
        reason = self._failure_reason(screen)
        events.extend((f"[Reconnect] Current screen: {self._screen_name(screen)}",
                       f"[Reconnect] Attempt {self.attempts} failed: {reason}"))
        if self.attempts >= self.max_attempts:
            self._fail(events)
            return None, events, False
        self.attempts += 1
        events.append(f"[Reconnect] Attempt {self.attempts}: {self._action_name(screen)}")
        self._after_attempt(now, events, reason)
        return screen, events, False


def reconnect_player_valid(eyes):
    """Fresh, generation-coherent player position after a memory session reset."""
    if eyes is None:
        return True
    with eyes.lock:
        generation, owner = eyes.generation, eyes.owner
    if owner is None:
        return False
    try:
        if owner not in eyes._positions([owner]):
            return False
    except Exception:
        return False
    with eyes.lock:
        return generation == eyes.generation and owner == eyes.owner


def click_at(x, y):
    """Left click in screen coordinates. mouse_event is ancient but is 3 lines."""
    import ctypes
    u = ctypes.windll.user32
    u.SetCursorPos(int(x), int(y))
    time.sleep(0.05)
    u.mouse_event(0x0002, 0, 0, 0, 0)  # LEFTDOWN
    u.mouse_event(0x0004, 0, 0, 0, 0)  # LEFTUP


def reconnect_step(img, win, click=click_at, settle=RECONNECT_SETTLE_S,
                   sea_template=None, idle_template=None):
    """Advance the login flow by one screen. Returns what it did, or None.

    Driven by what is on screen rather than a fixed script. ReconnectFlow decides
    when the current frame is authorized to click or retry.
    """
    screen = login_screen(img, idle_template)
    if screen is None:
        return None
    h, w = img.shape[:2]

    def press(frac):
        click(win.left + w * frac[0], win.top + h * frac[1])
        time.sleep(settle)

    if screen in ("disconnected", "idle disconnected"):
        press(OK_BTN)
    elif screen == "server":
        sea = find_sea_row(img, sea_template)
        if sea is None:
            # Better to sit on this screen and say so than to click blind and
            # join whichever region happens to be sitting in that row today.
            return "server: SEA row not found"
        click(win.left + sea[0], win.top + sea[1])
        time.sleep(settle)
        press(CONNECT_BTN)
    else:
        press(PLAY_BTN)
    return screen


def find_red_dots(bgr):
    """Return [(x, y, width)] of monster dots, image coords. width = short side."""
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    # red wraps hue 0, so two bands
    mask = cv2.inRange(hsv, (0, RED_S_MIN, 90), (10, 255, 255)) | \
           cv2.inRange(hsv, (170, RED_S_MIN, 90), (180, 255, 255))
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))
    cnts, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    out = []
    for c in cnts:
        if cv2.contourArea(c) < MIN_BLOB_AREA:
            continue
        # reported for --snap only: dots merge when they cluster, so width is a
        # diagnostic, not a filter -- rejecting fat blobs loses packed monsters.
        width = min(cv2.minAreaRect(c)[1])
        m = cv2.moments(c)
        if m["m00"] == 0:
            continue
        out.append((m["m10"] / m["m00"], m["m01"] / m["m00"], width))
    return out


def find_white_players(bgr):
    """Return small bright-white player dots as (x, y), rejecting white UI art."""
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, (0, 0, WHITE_V_MIN), (180, WHITE_S_MAX, 255))
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))
    cnts, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    out = []
    for c in cnts:
        area = cv2.contourArea(c)
        _, _, w, h = cv2.boundingRect(c)
        if not (PLAYER_AREA[0] <= area <= PLAYER_AREA[1] and
                PLAYER_SIDE[0] <= w <= PLAYER_SIDE[1] and
                PLAYER_SIDE[0] <= h <= PLAYER_SIDE[1]):
            continue
        m = cv2.moments(c)
        if m["m00"]:
            out.append((m["m10"] / m["m00"], m["m01"] / m["m00"]))
    return out


class PetFilter:
    """Confirm red/white pairs over time, then return red indexes that are pets."""

    def __init__(self):
        self.states = []

    def reset(self):
        self.states = []

    def pet_indexes(self, reds, whites):
        used_red = set()
        next_states = []
        pets = set()

        # White proximity establishes identity. Once established, follow the red
        # marker itself: pets often roam well beyond the initial 18px pairing radius.
        for state in (s for s in self.states if s["confirmed"]):
            steps = state["misses"] + 1
            vx, vy = state.get("velocity", (0.0, 0.0))
            predicted = (state["red"][0] + vx * steps,
                         state["red"][1] + vy * steps)
            choices = [(((red[0] - predicted[0]) ** 2 +
                         (red[1] - predicted[1]) ** 2) ** 0.5, i, red)
                       for i, red in enumerate(reds) if i not in used_red]
            best = min(choices, default=None)
            allowance = PET_TRACK_STEP_PX * min(steps, 2)
            if best is not None and best[0] <= allowance:
                _, i, red = best
                used_red.add(i)
                velocity = ((red[0] - state["red"][0]) / steps,
                            (red[1] - state["red"][1]) / steps)
                white = min(whites,
                            key=lambda p: (p[0] - red[0]) ** 2 +
                                          (p[1] - red[1]) ** 2,
                            default=state["white"])
                next_states.append(dict(red=red, white=white,
                                        frames=state["frames"] + 1,
                                        confirmed=True, misses=0,
                                        velocity=velocity))
                pets.add(i)
            elif state["misses"] < PET_FORGET_FRAMES:
                stale = dict(state)
                stale["misses"] += 1
                next_states.append(stale)

        # Unconfirmed dots still need a nearby white player for three frames.
        pairs = []
        for i, red in enumerate(reds):
            if i in used_red:
                continue
            white = min(whites,
                        key=lambda p: (p[0] - red[0]) ** 2 + (p[1] - red[1]) ** 2,
                        default=None)
            if white is None:
                continue
            separation = ((white[0] - red[0]) ** 2 +
                          (white[1] - red[1]) ** 2) ** 0.5
            if separation <= PET_RELEASE_PX:
                pairs.append((i, red, white, separation))

        used = set()
        for i, red, white, separation in sorted(pairs, key=lambda p: p[3]):
            best = None
            for j, state in enumerate(self.states):
                if state["confirmed"] or j in used:
                    continue
                allowance = PET_TRACK_STEP_PX * (state["misses"] + 1)
                red_step = ((red[0] - state["red"][0]) ** 2 +
                            (red[1] - state["red"][1]) ** 2) ** 0.5
                white_step = ((white[0] - state["white"][0]) ** 2 +
                              (white[1] - state["white"][1]) ** 2) ** 0.5
                if red_step <= allowance and white_step <= allowance:
                    score = red_step + white_step
                    if best is None or score < best[0]:
                        best = (score, j, state)

            if best is not None:
                _, j, old = best
                used.add(j)
                frames = old["frames"] + 1
                confirmed = old["confirmed"] or frames >= PET_CONFIRM_FRAMES
                steps = old["misses"] + 1
                velocity = ((red[0] - old["red"][0]) / steps,
                            (red[1] - old["red"][1]) / steps)
            elif separation <= PET_NEAR_PX:
                frames, confirmed = 1, PET_CONFIRM_FRAMES <= 1
                velocity = (0.0, 0.0)
            else:
                continue  # release-band pairs cannot start a new pet track

            next_states.append(dict(red=red, white=white, frames=frames,
                                    confirmed=confirmed, misses=0,
                                    velocity=velocity))
            if confirmed:
                pets.add(i)

        # Survive a brief marker flicker without forgetting a confirmed pair.
        for j, state in enumerate(self.states):
            if not state["confirmed"] and j not in used and state["misses"] < 2:
                stale = dict(state)
                stale["misses"] += 1
                next_states.append(stale)
        self.states = next_states
        return pets


def nearest(dots, cx, cy):
    return min(dots, key=lambda d: (d[0] - cx) ** 2 + (d[1] - cy) ** 2, default=None)


class TargetLock:
    """Keep one red marker until it is genuinely lost instead of switching nearest.

    The bot never stands still waiting for a kill to finish. Standing on a monster
    hides its dot inside CONCEAL_PX, and there is no way to tell that from the
    monster having died: our own pet follows the character and sits in that same
    radius permanently, so any "is something still under me" test says yes forever.
    Waiting on it parked the bot for seconds after every kill. Attack is held
    continuously anyway, so walking straight to the next target keeps hitting.
    """

    def __init__(self):
        self.target_id = 0
        self.reset()

    def reset(self):
        self.current = None
        self.misses = 0
        self.concealed = False

    def pick(self, dots, cx, cy):
        self.concealed = False
        if self.current is None:
            return self._acquire(dots, cx, cy)

        dot = nearest(dots, self.current[0], self.current[1])
        if dot is not None:
            step = ((dot[0] - self.current[0]) ** 2 +
                    (dot[1] - self.current[1]) ** 2) ** 0.5
            if step <= TARGET_TRACK_PX:
                self.current = dot
                self.misses = 0
                return dot

        self.misses += 1
        # Only point-blank occlusion is worth waiting out, and that lasts a frame
        # or two. Anything longer is a pause the bot cannot justify.
        if self.misses <= TARGET_FLICKER_FRAMES:
            self.concealed = ((self.current[0] - cx) ** 2 +
                              (self.current[1] - cy) ** 2) ** 0.5 <= TARGET_ARRIVE_PX
            return None

        return self._acquire(dots, cx, cy)

    def _acquire(self, dots, cx, cy):
        self.current = nearest(dots, cx, cy)
        self.misses = 0
        self.concealed = False
        if self.current is not None:
            self.target_id += 1
        return self.current


class WalkMap:
    """Where the character can and cannot walk, learned while it walks.

    A coarse grid over world (x, z). Three states: blocked, floor, and never
    seen -- and never-seen routes as passable, so the bot explores instead of
    refusing to move on a fresh map.

    Floor comes from two sources with different authority. Standing somewhere
    proves it walkable and clears anything we believed before. *Another* unit
    standing there proves the navmesh reaches it -- monsters and players walk on
    the same surface we do -- but a cell is 1.5 units wide and a monster on the
    far side of a thin wall shares its edge, so their evidence never erases a
    wall we measured. It only makes the route prefer that ground.
    """

    def __init__(self, path=WALK_FILE, cell=WALK_CELL):
        self.path, self.cell = path, cell
        self.hits = {}            # (cx, cz) -> [sightings, last seen]
        self.floor = set()        # (cx, cz) something has walked in
        self.lock = threading.Lock()
        self.still = 0            # fresh frames in a row we asked to move and did not
        self.last_pos = None      # our position at the previous observation
        self.anchor = None        # (time, x, z) the jam window measures from
        self.mark = None          # (time, x, z, goal) the slow sensor judges from
        self.wedged = False       # the last wall came from being unable to move
        self.capped = False       # the last route ran out of budget, not of map
        self.dirty = False

    # -- the grid -----------------------------------------------------------
    def at(self, x, z):
        return (math.floor(x / self.cell), math.floor(z / self.cell))

    def centre(self, c):
        return ((c[0] + 0.5) * self.cell, (c[1] + 0.5) * self.cell)

    def free(self, x, z):
        """We are standing here, so whatever we thought before was wrong."""
        c = self.at(x, z)
        with self.lock:
            if self.hits.pop(c, None) is not None or c not in self.floor:
                self.dirty = True
            self.floor.add(c)

    def paint(self, moved):
        """Cells other units walked in. Evidence of floor, not proof of no wall.

        Everything alive stands on the same navmesh we do, and the background
        sweep already carries hundreds of positions -- so the walkable area
        fills in from other people's traffic, including where the bot has never
        been. Only units that actually moved count: a pooled monster keeps its
        last position and full health, and one parked inside scenery would paint
        floor that is not there.
        """
        with self.lock:
            if len(self.floor) >= WALK_FLOOR_MAX:
                return 0
            before = len(self.floor)
            self.floor.update(self.at(x, z) for x, z in moved)
            grew = len(self.floor) - before
            self.dirty = self.dirty or bool(grew)
        return grew

    def block(self, x, z, now):
        with self.lock:
            e = self.hits.setdefault(self.at(x, z), [0, now])
            e[0] += 1
            e[1] = now
            self.dirty = True

    def blocked(self, c, now):
        e = self.hits.get(c)
        return bool(e and e[0] >= WALK_BLOCK_HITS and now - e[1] < WALK_DECAY_S)

    def crossed(self, x0, z0, x1, z1, now):
        """Is there a known wall on the straight line to there?

        This is the per-frame question, and the answer is almost always no --
        which is what keeps the straight line as the default and the whole
        feature inert until something has actually been learned.
        """
        d = math.hypot(x1 - x0, z1 - z0)
        steps = int(d / (self.cell / 2)) + 1
        for i in range(steps + 1):
            t = i / steps
            if self.blocked(self.at(x0 + (x1 - x0) * t, z0 + (z1 - z0) * t), now):
                return True
        return False

    # -- routing ------------------------------------------------------------
    def route(self, px, pz, tx, tz, now, allowed=None, edge_allowed=None):
        """World points from here to there avoiding known walls, or None.

        Dijkstra on an 8-connected grid, a cell something has walked in costing
        `WALK_FLOOR_COST` and an unknown one `WALK_UNKNOWN_COST`. Weights rather
        than a plain BFS so the route hugs ground proven walkable; distance
        still in the cost so it does not wander -- free floor made every route
        of equal price and the answer came back as a 170-point snake.

        None means no path. `capped` then says which kind: True if the search
        ran out of budget or corridor, False if the goal is genuinely walled
        off -- which is worth giving up on the monster for.
        """
        self.capped = False
        start, goal = self.at(px, pz), self.at(tx, tz)
        if start == goal:
            return [(tx, tz)]
        # Unbounded, the search spreads through unknown space in every
        # direction: measured at 9.7 ms, which is a fifth of the frame budget.
        # A detour worth taking stays near the line, so search a corridor round
        # it. Anything needing more than that is a straight walk's problem.
        lo = (min(start[0], goal[0]) - WALK_PAD, min(start[1], goal[1]) - WALK_PAD)
        hi = (max(start[0], goal[0]) + WALK_PAD, max(start[1], goal[1]) + WALK_PAD)
        cost, prev = {start: 0}, {start: None}
        queue = [(0, start)]
        steps, walled = 0, True
        while queue:
            here, c = heapq.heappop(queue)
            if here > cost.get(c, 1 << 30):
                continue                     # a cheaper way here was found later
            steps += 1
            if steps > WALK_MAX_CELLS:
                self.capped = True
                return None
            if c == goal:
                # Popped, not pushed: a cell can be reached again more cheaply,
                # so only the pop is final.
                path, at = [(tx, tz)], prev[c]
                while at is not None:
                    path.append(self.centre(at))
                    at = prev[at]
                path.reverse()
                return path[1:]              # drop the cell we are standing in
            for dx in (-1, 0, 1):
                for dz in (-1, 0, 1):
                    n = (c[0] + dx, c[1] + dz)
                    if not (lo[0] <= n[0] <= hi[0] and lo[1] <= n[1] <= hi[1]):
                        walled = False       # the corridor stopped us, not a wall
                        continue
                    if self.blocked(n, now):
                        continue
                    if (dx and dz
                            and (self.blocked((c[0] + dx, c[1]), now)
                                 or self.blocked((c[0], c[1] + dz), now))):
                        continue
                    nx, nz = self.centre(n)
                    if allowed is not None and not allowed(nx, nz):
                        continue
                    if edge_allowed is not None:
                        cx, cz = self.centre(c)
                        if not edge_allowed(cx, cz, nx, nz):
                            continue
                    step = here + (WALK_FLOOR_COST if n in self.floor
                                   else WALK_UNKNOWN_COST)
                    if step >= cost.get(n, 1 << 30):
                        continue
                    cost[n], prev[n] = step, c
                    heapq.heappush(queue, (step, n))
        # Exhausted. If the corridor never got in the way, the goal really is
        # walled off from here and the monster is not worth walking at.
        self.capped = not walled
        return None

    def waypoint(self, px, pz, path):
        """The first point far enough along to steer at without wobbling."""
        for x, z in path:
            if math.hypot(x - px, z - pz) >= WALK_WAYPOINT:
                return x, z
        return path[-1]

    # -- learning -----------------------------------------------------------
    def observe(self, now, px, pz, sx, sy, basis, mode, goal=None):
        """Record floor under us, and a wall ahead when we are not getting there.

        Two sensors. The fast one asks whether this frame's travel went the way
        we pushed, and catches a steep hit in 0.3s. The slow one asks whether
        the last `WALK_STUCK_S` actually brought us closer to `goal`, and is the
        only thing that catches a shallow slide -- which keeps full speed and a
        healthy-looking push projection while running along the wall.

        Only while actually walking somewhere. Standing still is *correct* in
        every other mode -- the orbit holds position on purpose, a dead or lost
        unit issues no stick at all -- and marking walls from those states fills
        the map with fiction centred on wherever the bot happened to stop.
        """
        # "going back" is walking somewhere too: the bot pushes the stick toward
        # home, so a wall hit on the way back is real and the jam detector plus
        # wedge escape must run there. Without it a bot wedged on the return
        # walk leans on the wall forever -- it can neither learn the wall nor
        # fire wedge_off, and the timeout re-target keeps aiming at the same wall.
        if mode not in ("chasing", "far", "loot", "going back", "wander"):
            self.still, self.last_pos, self.mark = 0, None, None
            self.anchor = None
            return None
        self.free(px, pz)
        was, self.last_pos = self.last_pos, (px, pz)
        # No stick out means we are not trying to get anywhere, so neither
        # sensor may judge: the window has to start again when we do.
        if was is None or not basis or not (sx or sy):
            self.still, self.mark = 0, None
            self.anchor = None
            return None
        wx, wz = world_for(basis, sx, sy)
        n = math.hypot(wx, wz)
        if n < 1e-9:
            self.still, self.mark = 0, None
            return None
        ux, uz = wx / n, wz / n
        jam = self._jammed(now, px, pz, ux, uz)
        if jam:
            return jam
        # A repeat is the feed hiccuping, not a measurement: there is no travel
        # to project, and counting it as "did not move" is what stamped walls
        # across open ground. Neither sensor may judge on one -- the slow one is
        # fooled just as badly, since a stall long enough to span its window
        # leaves both of its endpoints holding the same repeated value and it
        # reads a walking character as having gained nothing. Wait for a fresh
        # position; the jam window above is what notices when none ever comes.
        if was == (px, pz):
            return None
        # Speed is not the test. Pushed into a wall the game slides the
        # character along it at nearly full pace, so travel stays high while
        # nothing is gained -- which is why measuring speed learned almost no
        # walls at all. What a wall takes away is progress along the direction
        # we asked for, and a slide takes away all of it.
        progress = (px - was[0]) * ux + (pz - was[1]) * uz
        if progress >= WALK_BLOCK_PROGRESS:
            self.still = 0
        else:
            self.still += 1
        if WALK_LOG:
            # speed against progress is the whole diagnosis: a slide keeps the
            # first and loses the second, which is what a plain speed test missed.
            print(f"\nwalklog push ({ux:5.2f},{uz:5.2f}) moved "
                  f"({px - was[0]:5.2f},{pz - was[1]:5.2f}) speed "
                  f"{math.hypot(px - was[0], pz - was[1]):5.2f} progress "
                  f"{progress:5.2f} still {self.still}")
        if self.still >= WALK_BLOCK_FRAMES:
            self.still = 0
            return self._wall(now, px, pz, ux, uz, "push")
        return self._creeping(now, px, pz, goal)

    def _jammed(self, now, px, pz, ux, uz):
        """Wall from the jam window: pushing for `WALK_JAM_S` and still here.

        Repeated position reads cannot answer this. The feed serves the same
        value for runs of 26 frames under load, so any "no fresh position for
        N seconds" test fires on ordinary hiccups -- measured, it fired on every
        one of them. Displacement over a window does not care how many samples
        arrived: a stall ends with the position jumping to where the character
        walked, and a jam ends where it started.
        """
        if self.anchor is None:
            self.anchor = (now, px, pz)
            return None
        since, ax, az = self.anchor
        if now - since < WALK_JAM_S:
            return None
        gone = math.hypot(px - ax, pz - az)
        self.anchor = (now, px, pz)
        if WALK_LOG:
            print()
            print(f"walklog jam window {now - since:4.1f}s moved {gone:6.2f}"
                  f" (need {WALK_JAM_MIN})")
        if gone >= WALK_JAM_MIN:
            return None
        self.still = 0
        return self._wall(now, px, pz, ux, uz, "push")

    def _creeping(self, now, px, pz, goal):
        """Wall from the slow sensor: a window that brought us no closer.

        A shallow slide keeps full speed and a healthy push projection while
        running along the wall, so only distance to the goal can say it is not
        working. Both distances are measured against the goal's position *now*,
        which is what makes a fleeing monster harmless: if it ran and we chased,
        we still closed on where it now is.
        """
        if goal is None:
            self.mark = None
            return None
        if self.mark is None:
            self.mark = (now, px, pz, goal)
            return None
        since, mx, mz, was_goal = self.mark
        if math.hypot(goal[0] - was_goal[0], goal[1] - was_goal[1]) > WALK_GOAL_JUMP:
            # The target changed. Judging across that writes a wall between us
            # and a monster we have only just turned towards.
            self.mark = (now, px, pz, goal)
            return None
        if now - since < WALK_STUCK_S:
            return None
        approach = (math.hypot(goal[0] - mx, goal[1] - mz)
                    - math.hypot(goal[0] - px, goal[1] - pz))
        self.mark = (now, px, pz, goal)
        if WALK_LOG:
            print(f"\nwalklog window {now - since:4.1f}s approach {approach:6.2f}"
                  f" (need {WALK_STUCK_MIN})")
        if approach >= WALK_STUCK_MIN:
            return None
        gx, gz = goal[0] - px, goal[1] - pz
        n = math.hypot(gx, gz)
        if n < 1e-9:
            return None
        return self._wall(now, px, pz, gx / n, gz / n, "creep")

    def _wall(self, now, px, pz, ux, uz, why):
        """Block an arc ahead along (ux, uz), which is a unit vector.

        One cell is too thin to describe what we are actually pressed against.
        Measured live, wedged: the bot marked the same single cell forever,
        because dodging 1.5 units at 2 units' range bends the heading by 25
        degrees, the route came back effectively unchanged, and the character
        pushed the same rock again -- and having never moved, it could only ever
        mark that one cell. So block a fan: two ranges deep, three ways wide.
        """
        hit, mine = None, self.at(px, pz)
        for reach in (WALK_BLOCK_AHEAD * 0.6, WALK_BLOCK_AHEAD):
            for turn in (-WALK_BLOCK_ARC, 0.0, WALK_BLOCK_ARC):
                c, s = math.cos(turn), math.sin(turn)
                bx = px + (ux * c - uz * s) * reach
                bz = pz + (ux * s + uz * c) * reach
                if self.at(bx, bz) == mine:
                    continue         # we are standing in it; free() clears it anyway
                self.block(bx, bz, now)
                hit = hit or self.at(bx, bz)
        self.mark = None            # that window is spent whichever sensor fired
        self.wedged = why == "push"  # only the fast sensor means we cannot move
        if WALK_LOG:
            print(f"\nwalklog WALL at {hit} ({why})")
        return hit

    def forget_walk(self):
        """Our unit changed or the bot was paused; the world did not."""
        self.still, self.last_pos, self.mark = 0, None, None
        self.anchor = None

    # -- persistence --------------------------------------------------------
    def load(self):
        try:
            with open(self.path) as f:
                blob = json.load(f)
        except (OSError, ValueError):
            return self
        # A file written at another cell size cannot be rescaled honestly, and
        # pretending otherwise puts walls where there are none.
        if blob.get("cell") != self.cell:
            return self
        self.hits = {(c[0], c[1]): [c[2], c[3]] for c in blob.get("blocked", ())}
        self.floor = {(c[0], c[1]) for c in blob.get("floor", ())}
        return self

    def save(self):
        with self.lock:
            if not self.dirty:
                return False
            rows = [[c[0], c[1], e[0], e[1]] for c, e in self.hits.items()]
            floor = [[c[0], c[1]] for c in self.floor]
            self.dirty = False
        try:
            with open(self.path, "w") as f:
                json.dump({"cell": self.cell, "blocked": rows, "floor": floor}, f)
        except OSError:
            return False
        return True


class Area:
    """A named mask, circle union, or polygon in horizontal world coordinates.

    `inside()` is exact target admission; `safe()` is the inset used for player
    movement. A return remains committed to `home_goal` after safe re-entry.
    """

    def __init__(self, name, cells=None, path=AREA_FILE, cell=AREA_CELL,
                 circle=None, circles=None, polygon=None, axes="xz"):
        self.name, self.path, self.cell = name, path, cell
        self.axes = axes
        self.cells = set(cells or ())
        self.circles = []          # (centre_x, centre_z, radius), exact world units
        self.polygon = ()          # exact horizontal world points
        self._polygon_zone = None
        if circle is not None:
            self.set_circle(*circle)
        elif circles is not None:
            for item in circles:
                self.add_circle(*item)
        elif polygon is not None:
            self.set_polygon(polygon, axes)
        self._core = set()       # cells with all eight neighbours painted
        self._core_list = []     # the same, ordered, so a pick is reproducible
        self._stale = True       # recompute those on the next question asked

    @property
    def circle(self):
        """First circle, retained for compatibility with single-circle callers."""
        return self.circles[0] if self.circles else None

    @property
    def defined(self):
        return bool(self.circles or self.polygon or self.cells)

    @property
    def runtime_supported(self):
        # The game and the bot's calibrated movement model are Unity X/Z.
        # Preserve XY metadata for honest round-trips, but never execute it as X/Z.
        return self.axes == "xz"

    def set_circle(self, x, z, radius):
        self.circles = []
        self.polygon, self._polygon_zone = (), None
        return self.add_circle(x, z, radius)

    def add_circle(self, x, z, radius):
        """Add one disc to the union; circle areas never mix with painted cells."""
        radius = float(radius)
        if not all(math.isfinite(v) for v in (x, z, radius)) or radius <= 0.0:
            raise ValueError("circle centre and radius must be finite; radius must be positive")
        self.circles.append((float(x), float(z), radius))
        self.cells.clear()
        self.polygon, self._polygon_zone = (), None
        self._stale = True
        return self

    def set_polygon(self, points, axes="xz"):
        """Replace this area with one exact polygon."""
        if axes not in ("xy", "xz"):
            raise ValueError("area axes must be xy or xz")
        zone = PolygonZone(points)
        probe = zone.nearest_safe(zone.points[0], AREA_SAFETY)
        if not zone.contains(probe, AREA_SAFETY):
            raise ValueError(f"polygon is too narrow for {AREA_SAFETY:g}-unit safety margin")
        self.axes = axes
        self.polygon = zone.points
        self._polygon_zone = zone
        self.circles = []
        self.cells.clear()
        self._stale = True
        return self

    def describe(self):
        if self.polygon:
            return f"polygon {len(self.polygon)} points on {self.axes.upper()}"
        if self.circles:
            if len(self.circles) == 1:
                x, z, radius = self.circles[0]
                return f"circle centre {x:.1f},{z:.1f}, radius {radius:.1f}"
            radii = [c[2] for c in self.circles]
            return (f"{len(self.circles)} overlapping circles, radii "
                    f"{min(radii):.1f}..{max(radii):.1f}")
        return f"{len(self.cells)} painted cells"

    @staticmethod
    def _inner_radius(circle):
        """Return radius used for safe return/wander points.

        Normal circles keep the full safety margin. A circle narrower than that
        margin has only one honest safe point: its centre.
        """
        radius = circle[2]
        return max(0.0, radius - AREA_SAFETY)

    @staticmethod
    def _radial_point(circle, x, z, radius):
        cx, cz, _ = circle
        dx, dz = x - cx, z - cz
        distance = math.hypot(dx, dz)
        if distance < 1e-9:
            return cx, cz
        scale = radius / distance
        return cx + dx * scale, cz + dz * scale

    def at(self, x, z):
        return (math.floor(x / self.cell), math.floor(z / self.cell))

    def centre(self, c):
        return ((c[0] + 0.5) * self.cell, (c[1] + 0.5) * self.cell)

    def paint(self, x, z):
        """Stamp the brush disc around one sampled position."""
        if self.circles or self.polygon:
            raise ValueError("cannot paint into an exact-shape area")
        n = int(AREA_BRUSH / self.cell) + 1
        cx, cz = self.at(x, z)
        # The cell we are standing in always counts, whatever the brush: its
        # centre can be further from us than AREA_BRUSH, and then a walk would
        # paint nothing at all.
        self.cells.add((cx, cz))
        for dx in range(-n, n + 1):
            for dz in range(-n, n + 1):
                k = (cx + dx, cz + dz)
                kx, kz = self.centre(k)
                if math.hypot(kx - x, kz - z) <= AREA_BRUSH:
                    self.cells.add(k)
        self._stale = True

    @property
    def core(self):
        """Cells it is safe to *stand* in: painted, and not on the fringe.

        Everything that picks a point to walk to -- returning, wandering --
        picks from here, so arriving always satisfies `deep()` and the bot is
        never left one step short of being back inside.

        Recomputed lazily rather than per brush stamp: the recorder paints
        several times a second and an area runs to thousands of cells, so
        rebuilding this on every stamp is the one place this could get slow.
        """
        self._recore()
        return self._core

    @property
    def core_list(self):
        self._recore()
        return self._core_list

    def _recore(self):
        if not self._stale:
            return
        self._stale = False
        self._core = {c for c in self.cells
                     if all((c[0] + dx, c[1] + dz) in self.cells
                            for dx in (-1, 0, 1) for dz in (-1, 0, 1))}
        if not self._core:
            # A recording too thin to have a middle. Better a bot with no
            # hysteresis than one that can never finish walking back in.
            self._core = set(self.cells)
        self._core_list = sorted(self._core)

    def inside(self, x, z, slack=0.0):
        """Painted here, or within `slack` world units of painted ground."""
        if self._polygon_zone is not None:
            # Target admission is exact for polygons. Safety is represented by
            # an inward margin through safe(), never by expanding the fence.
            return self._polygon_zone.contains((x, z))
        if self.circles:
            extra = max(0.0, slack)
            return any(math.hypot(x - cx, z - cz) <= radius + extra
                       for cx, cz, radius in self.circles)
        c = self.at(x, z)
        if c in self.cells:
            return True
        if slack <= 0.0:
            return False
        n = int(slack / self.cell) + 1
        for dx in range(-n, n + 1):
            for dz in range(-n, n + 1):
                k = (c[0] + dx, c[1] + dz)
                if k not in self.cells:
                    continue
                kx, kz = self.centre(k)
                if math.hypot(kx - x, kz - z) <= slack + self.cell * 0.5:
                    return True
        return False

    def safe(self, x, z, margin=AREA_SAFETY):
        """Inside with enough room for the next controller step."""
        if self._polygon_zone is not None:
            return self._polygon_zone.contains((x, z), margin)
        if self.circles:
            return any(CircleZone((cx, cz), radius).contains((x, z), margin)
                       for cx, cz, radius in self.circles)
        # A walked mask's core is its established one-cell safety margin.
        return self.deep(x, z)

    def deep(self, x, z):
        """Well inside, not merely inside. See `AREA_CELL`."""
        if self._polygon_zone is not None:
            return self._polygon_zone.contains((x, z), AREA_CELL)
        if self.circles:
            return any(math.hypot(x - c[0], z - c[1]) <= self._inner_radius(c)
                       for c in self.circles)
        return self.at(x, z) in self.core

    def home(self, x, z):
        """Nearest cell it is safe to stand in.

        Linear over the cell set, and only ever called while the character is
        outside the area, which is rare and brief.
        """
        if self._polygon_zone is not None:
            return self._polygon_zone.nearest_safe((x, z), AREA_SAFETY)
        if self.circles:
            if self.deep(x, z):
                return x, z
            choices = [self._radial_point(c, x, z, self._inner_radius(c))
                       for c in self.circles]
            return min(choices, key=lambda p: (p[0] - x) ** 2 + (p[1] - z) ** 2)
        if not self.core_list:
            return x, z
        c = min(self.core_list,
                key=lambda k: (self.centre(k)[0] - x) ** 2
                + (self.centre(k)[1] - z) ** 2)
        return self.centre(c)

    def nearest(self, x, z):
        """The centre of the nearest painted cell, whether or not it is core.

        The home point is the nearest *core* cell, in the middle of the area.
        When the bot is far from the area, the corridor to the home point is
        huge and the route caps (search budget). The nearest *painted* cell is
        on the boundary, closer, and the route to it succeeds. Once inside, we
        navigate to the core on the next frame.
        """
        if self._polygon_zone is not None:
            return self._polygon_zone.nearest_safe((x, z), 0.0)
        if self.circles:
            if self.inside(x, z):
                return x, z
            choices = [self._radial_point(c, x, z, c[2]) for c in self.circles]
            return min(choices, key=lambda p: (p[0] - x) ** 2 + (p[1] - z) ** 2)
        if not self.cells:
            return x, z
        c = min(self.cells,
                key=lambda k: (self.centre(k)[0] - x) ** 2
                + (self.centre(k)[1] - z) ** 2)
        return self.centre(c)

    def spot(self, rng):
        """Somewhere random to stand, uniformly distributed over the area."""
        if self._polygon_zone is not None:
            return self._polygon_zone.random_safe(AREA_SAFETY, rng)
        if self.circles:
            # Pick a disc proportional to its area. Overlaps get proportionally
            # more visits, but every point stays inside the exact union.
            total = sum(c[2] ** 2 for c in self.circles)
            pick = rng.random() * total
            circle = self.circles[-1]
            for candidate in self.circles:
                pick -= candidate[2] ** 2
                if pick <= 0.0:
                    circle = candidate
                    break
            cx, cz, _ = circle
            radius = self._inner_radius(circle) * math.sqrt(rng.random())
            angle = rng.random() * math.tau
            return cx + radius * math.cos(angle), cz + radius * math.sin(angle)
        return self.centre(rng.choice(self.core_list)) if self.core_list else None

    def bounds(self):
        if self._polygon_zone is not None:
            return self._polygon_zone.bounds()
        if self.circles:
            return ((min(x - radius for x, z, radius in self.circles),
                     min(z - radius for x, z, radius in self.circles)),
                    (max(x + radius for x, z, radius in self.circles),
                     max(z + radius for x, z, radius in self.circles)))
        xs = [c[0] for c in self.cells]
        zs = [c[1] for c in self.cells]
        return ((min(xs) * self.cell, min(zs) * self.cell),
                ((max(xs) + 1) * self.cell, (max(zs) + 1) * self.cell))

    def guard_step(self, current, proposed, margin=AREA_SAFETY):
        """Return (allowed, point) for one predicted horizontal movement step."""
        if self._polygon_zone is not None:
            return self._polygon_zone.guard_step(current, proposed, margin)
        if self.circles:
            zones = [CircleZone((x, z), radius) for x, z, radius in self.circles]
            intervals = sorted((interval for zone in zones
                                if (interval := zone.segment_interval(
                                    current, proposed, margin)) is not None))
            covered = 0.0
            for low, high in intervals:
                if low > covered + 1e-7:
                    break
                covered = max(covered, high)
                if covered >= 1.0 - 1e-7:
                    break
            if covered >= 1.0 - 1e-7:
                return True, proposed
            if self.safe(*current, margin) and self.safe(*proposed, margin):
                return False, current
            choices = [zone.nearest_safe(proposed, margin) for zone in zones]
            return False, min(choices,
                              key=lambda p: (p[0] - proposed[0]) ** 2
                              + (p[1] - proposed[1]) ** 2)
        # Painted masks are exact grid unions. Endpoint-only validation can cut
        # diagonally through a non-core cell, so split at every crossed grid line
        # and check one point inside each traversed cell.
        if self.safe(*current, margin) and self.safe(*proposed, margin):
            breaks = {0.0, 1.0}
            for axis in (0, 1):
                start, end = current[axis], proposed[axis]
                delta = end - start
                if abs(delta) < 1e-9:
                    continue
                low, high = sorted((start, end))
                first = math.floor(low / self.cell) + 1
                last = math.floor(high / self.cell)
                for grid in range(first, last + 1):
                    t = (grid * self.cell - start) / delta
                    if 0.0 < t < 1.0:
                        breaks.add(t)
            ordered = sorted(breaks)
            for left, right in zip(ordered, ordered[1:]):
                t = (left + right) / 2.0
                point = (current[0] + (proposed[0] - current[0]) * t,
                         current[1] + (proposed[1] - current[1]) * t)
                if not self.safe(*point, margin):
                    return False, current
            return True, proposed
        return False, self.home(*proposed)

    # -- persistence --------------------------------------------------------
    @staticmethod
    def _blob(path):
        try:
            with open(path) as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    @staticmethod
    def names(path=AREA_FILE):
        return sorted(Area._blob(path).get("areas", {}))

    def load(self):
        blob = self._blob(self.path)
        got = blob.get("areas", {}).get(self.name)
        if not got:
            return self
        if got.get("shape") == "circle":
            centre, radius = got.get("center"), got.get("radius")
            try:
                if len(centre) != 2:
                    return self
                self.axes = got.get("axes", "xz")
                self.set_circle(centre[0], centre[1], radius)
            except (TypeError, ValueError):
                pass
            return self
        if got.get("shape") == "polygon":
            try:
                self.set_polygon(got.get("points", ()), got.get("axes", "xz"))
            except (TypeError, ValueError):
                self.polygon, self._polygon_zone = (), None
            return self
        if got.get("shape") == "circles":
            self.circles = []
            self.axes = got.get("axes", "xz")
            try:
                for item in got.get("circles", ()):
                    if len(item) != 3:
                        raise ValueError
                    self.add_circle(*item)
            except (TypeError, ValueError):
                self.circles = []
            return self
        # Same rule as WalkMap: a file written at another cell size cannot be
        # rescaled honestly, and pretending otherwise fences off the wrong ground.
        if blob.get("cell") != self.cell:
            return self
        self.circles = []
        self.polygon, self._polygon_zone = (), None
        self.cells = {(c[0], c[1]) for c in got.get("cells", ())}
        self._stale = True
        return self

    def save(self):
        """Read-modify-write: saving one area must not drop the others."""
        blob = self._blob(self.path)
        if blob.get("cell") != self.cell:
            # Cell-size incompatibility invalidates only painted masks. Exact
            # world-space shapes have no grid to rescale and must survive.
            exact = {name: entry for name, entry in blob.get("areas", {}).items()
                     if isinstance(entry, dict)
                     and entry.get("shape") in ("circle", "circles", "polygon")}
            blob = {"cell": self.cell, "areas": exact}
        if self.polygon:
            entry = {"shape": "polygon", "axes": self.axes,
                     "points": [[x, z] for x, z in self.polygon]}
        elif len(self.circles) == 1:
            x, z, radius = self.circles[0]
            entry = {"shape": "circle", "axes": self.axes,
                     "center": [x, z], "radius": radius}
        elif self.circles:
            entry = {"shape": "circles", "axes": self.axes,
                     "circles": [[x, z, radius]
                                 for x, z, radius in self.circles]}
        else:
            entry = {"shape": "mask",
                     "cells": [[c[0], c[1]] for c in sorted(self.cells)]}
        blob.setdefault("areas", {})[self.name] = entry
        try:
            with open(self.path, "w") as f:
                json.dump(blob, f)
        except OSError:
            return False
        return True


class StuckWatchdog:
    """Report a locked target that has not become meaningfully closer in time."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.target_id = None
        self.best_distance = None
        self.last_progress = None

    def update(self, target_id, dot, cx, cy, now=None):
        now = time.time() if now is None else now
        distance = ((dot[0] - cx) ** 2 + (dot[1] - cy) ** 2) ** 0.5
        if target_id != self.target_id:
            self.target_id = target_id
            self.best_distance = distance
            self.last_progress = now
            return False
        if distance <= STUCK_MIN_DIST_PX:
            self.best_distance = min(self.best_distance, distance)
            self.last_progress = now
            return False
        if distance <= self.best_distance - STUCK_PROGRESS_PX:
            self.best_distance = distance
            self.last_progress = now
        return now - self.last_progress >= STUCK_TIMEOUT_S


class TargetBlacklist:
    """Temporarily follow and exclude red markers found to be inaccessible."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.tracks = []

    def block(self, dot, now=None):
        now = time.time() if now is None else now
        self.tracks.append(dict(dot=dot, until=now + TARGET_IGNORE_S))

    def filter(self, dots, now=None):
        now = time.time() if now is None else now
        tracks = [track for track in self.tracks if now < track["until"]]
        blocked = set()
        for track in tracks:
            choices = [((((dot[0] - track["dot"][0]) ** 2 +
                           (dot[1] - track["dot"][1]) ** 2) ** 0.5), i, dot)
                       for i, dot in enumerate(dots) if i not in blocked]
            best = min(choices, default=None)
            if best is not None and best[0] <= TARGET_TRACK_PX:
                _, i, dot = best
                track["dot"] = dot
                blocked.add(i)
        self.tracks = tracks
        return [dot for i, dot in enumerate(dots) if i not in blocked]


def pick_target(img, pet_filter=None, target_lock=None,
                target_blacklist=None, now=None):
    """(origin, targetable dots, chosen dot) -- the whole targeting rule, once.

    main() and --watch both call this, so the live view cannot drift out of step
    with what the bot is actually chasing.
    """
    h, w = img.shape[:2]
    # The game keeps the character pinned to the middle of its minimap, so the box
    # centre IS the player. Detecting the marker instead was fragile: it turns blue
    # in a party, and in a crowd the nearest white blob is somebody else's dot.
    cx, cy = w / 2, h / 2
    # Blobs under the player marker are never a target: we have arrived, or it is
    # our own pet, or a fixed red UI element at the centre. Keeping them to detect
    # "still fighting" was tried and reverted -- our pet never leaves that radius,
    # so it read as an endless fight and stalled the bot after every kill.
    red = find_red_dots(img)
    if pet_filter is not None:
        # Our own marker may also be white when not in a party. It is fixed at the
        # box centre and must not make a nearby real monster look like somebody's pet.
        players = [p for p in find_white_players(img)
                   if (p[0] - cx) ** 2 + (p[1] - cy) ** 2 > CONCEAL_PX ** 2]
        # One call per frame: pet_indexes advances a tracker, so calling it twice
        # would double-step every confirmation counter.
        pets = pet_filter.pet_indexes(red, players)
        red = [d for i, d in enumerate(red) if i not in pets]
    dots = [d for d in red
            if (d[0] - cx) ** 2 + (d[1] - cy) ** 2 > CONCEAL_PX ** 2]
    if target_blacklist is not None:
        dots = target_blacklist.filter(dots, now)
    chosen = target_lock.pick(dots, cx, cy) if target_lock else nearest(dots, cx, cy)
    return (cx, cy), dots, chosen


def stick_for(basis, dx, dz):
    """Stick (sx, sy) that walks toward a world offset, or None if unsolvable.

    `basis` maps a stick push to the world travel it produces, measured live:
    [[wx_for_sx, wx_for_sy], [wz_for_sx, wz_for_sy]]. Inverting it answers the
    question the bot actually has -- which way to push to go there. Doing it this
    way means the world's axes and the camera's never have to be known.
    """
    (a, b), (c, d) = basis
    det = a * d - b * c
    if abs(det) < 1e-9:
        return None                    # the two pushes were parallel: no basis
    sx = (d * dx - b * dz) / det
    sy = (-c * dx + a * dz) / det
    n = (sx * sx + sy * sy) ** 0.5
    if n < 1e-9:
        return None
    return sx / n * SPEED, sy / n * SPEED


def world_for(basis, sx, sy):
    """World travel a stick push produces -- `basis` used forwards.

    stick_for() inverts it to answer "which way do I push"; this asks the other
    question, "where was I trying to go", which is what names the cell a wall
    sits in when a push goes nowhere.
    """
    (a, b), (c, d) = basis
    return a * sx + b * sy, c * sx + d * sy


def loot_wins(mode, mdist, ldist):
    """Does the drop beat the monster this frame?

    Nearest-wins alone left items lying: on a busy map the monster is almost
    always nearer, so a drop a few steps away lost every arbitration until it
    despawned. Inside LOOT_FIRST_RANGE the item takes precedence outright.

    Three modes are never interrupted. "on it" is a fight already joined, and
    walking out of one is how a bot dies -- an item under our feet is collected
    by LOOT_BUTTON anyway, without moving. "unwedge" is the character backing
    out of something it is jammed against, and it reports a distance of zero:
    overriding that push would leave the bot stuck against the wall it is in
    the middle of escaping. "going back" is authoritative confinement; loot
    remains filtered to the zone but cannot delay the committed safe return.
    """
    if ldist is None or mode in ("on it", "unwedge", "going back"):
        return False
    if mdist is None or mode == "far":
        return True
    return ldist <= LOOT_FIRST_RANGE or ldist < mdist


def stale_target(now, engaged_since, limit=MEM_ENGAGE_MAX_S):
    """True once we have been on one target longer than it should take to die.

    Something that will not die -- already dead and still listed, unreachable,
    not attackable -- parks the bot on the spot swinging at it, which is the one
    failure mode that looks exactly like a hung bot from the outside.
    """
    return engaged_since is not None and now - engaged_since > limit


def nearest_monster(units, px, pz, reach=MEM_RANGE):
    """Closest unit that is actually a monster. Pets are simply not monsters."""
    best, best_d = None, reach
    for kind, addr, x, y, z in units:
        if kind != "monster":
            continue
        d = ((x - px) ** 2 + (z - pz) ** 2) ** 0.5
        if d < best_d:
            best, best_d = (addr, x, y, z), d
    return (best, best_d) if best else (None, None)


def pick_me(legs, floor=MEM_CAL_MIN, need=MEM_CAL_LEGS):
    """Which unit is the one answering the stick?

    `legs` is [((sx, sy), {addr: (dx, dz)})] from the calibration pushes. The obvious test -- whoever moved furthest -- is wrong on a busy
    map, where another player simply walks faster than our pushes and steals
    the identification. What is true of us and of nobody else is that our
    travel is a *linear function of the stick*: push east twice as hard, go
    twice as far; push the other way, come back. A player going about their
    own business fits that badly no matter how fast they move, so score every
    unit by how well a single 2x2 basis explains all of its legs at once and
    take the best fit.
    """
    per = {}
    for stick, moved in legs:
        for addr, d in moved.items():
            per.setdefault(addr, []).append((stick, d))
    best, best_err = None, None
    for addr, obs in per.items():
        live = [(s, d) for s, d in obs if (d[0] ** 2 + d[1] ** 2) ** 0.5 >= floor]
        if len(live) < need:
            continue                    # too little to tell motion from noise
        S = np.array([s for s, _ in live], dtype=float)
        W = np.array([d for _, d in live], dtype=float)
        if np.linalg.matrix_rank(S) < 2:
            continue
        fit, *_ = np.linalg.lstsq(S, W, rcond=None)
        # relative residual, so a unit is not rewarded for barely moving
        err = float(np.linalg.norm(S @ fit - W) / max(np.linalg.norm(W), 1e-6))
        if best_err is None or err < best_err:
            best, best_err = addr, err
    return best


def stick_vector(dx, dy):
    """Screen delta -> left-stick (x, y), y up positive.

    Direction only, magnitude SPEED. A minimap pixel is many world metres, so
    scaling tilt by pixel distance made every move a crawl the game's own
    deadzone swallowed. ponytail: drop SPEED below 1.0 only if you overshoot.
    """
    n = max((dx * dx + dy * dy) ** 0.5, 1e-6)
    scale = SPEED / n
    return float(np.clip(dx * scale, -1, 1)), float(np.clip(-dy * scale, -1, 1))


def wanted_item(name):
    """Is this item one we walk to? Empty config means all; missing means none.

    Substring, case-insensitive: "Card" collects "Bee Card", "Rooster Card" and
    every other card, which is the point -- a whole family of items is usually
    what you want and listing them one by one goes stale as the game adds more.
    The cost is that a short entry catches more than it looks like it will
    ("axe" also takes "Battle Axe"), so keep entries specific enough to mean it.
    """
    if LOOT_NAMES is None:
        return False
    if not LOOT_NAMES:
        return True
    # ("Card") is a string, not a tuple -- the missing comma is easy to write,
    # and iterating it would test the letters 'C', 'a', 'r', 'd' one at a time.
    want = (LOOT_NAMES,) if isinstance(LOOT_NAMES, str) else LOOT_NAMES
    got = name.strip().lower()
    return any(w.strip().lower() in got for w in want if w.strip())


def memory_scan_summary(ms, mem, units, owner, max_monsters=5,
                        max_players=8, max_pets=8, priority_monster=None):
    """Human-readable names from one background sweep, never from the hot path."""
    counts = {kind: sum(1 for row in units if row[0] == kind)
              for kind in ("monster", "player", "pet")}
    by_addr = {row[1]: row for row in units}
    origin = by_addr.get(owner)

    def closest(kind, limit):
        rows = [row for row in units if row[0] == kind]
        if origin:
            ox, oz = origin[2], origin[4]
            rows.sort(key=lambda row: math.hypot(row[2] - ox, row[4] - oz))
        return rows[:limit]

    player_rows = closest("player", max_players)
    pet_rows = closest("pet", max_pets)
    own_pets = ms.my_pets(mem, owner) if owner else set()
    summoner_of = getattr(ms, "summoner_of", None)
    if owner and summoner_of:
        own_pets.update(unit for kind, unit, *_ in pet_rows
                        if summoner_of(mem, unit) == owner)

    players = []
    own_name = ms.player_name(mem, owner) if owner else None
    player_rows.sort(key=lambda row: row[1] != owner)
    for kind, unit, *_ in player_rows:
        visible = bool((mem.read(unit + ms.UNIT_VISIBLE, 1) or b"\0")[0])
        if not visible and unit != owner:
            continue
        loaded_name = ms.player_name(mem, unit)
        # Rebuilt/pool copies of our PlayerController can remain visible and
        # retain our name. Character names are unique, so only the owner row is
        # useful; listing three Lepicas makes the report look like three people.
        if unit != owner and loaded_name and loaded_name == own_name:
            continue
        name = loaded_name or f"player@0x{unit:X}"
        players.append(name + (" [YOU]" if unit == owner else ""))

    pets = []
    for kind, unit, *_ in pet_rows:
        name = ms.monster_id(mem, unit) or f"pet@{unit & 0xFFFF:04X}"
        pets.append(name + (" [YOURS]" if unit in own_pets else ""))

    monster_counts = {}
    monster_order = []
    monster_names = {}
    if origin:
        ox, oz = origin[2], origin[4]
        nearby = sorted((math.hypot(x - ox, z - oz), unit)
                        for kind, unit, x, _, z in units if kind == "monster")
        if priority_monster:
            priority = next((row for row in nearby
                             if row[1] == priority_monster), None)
            if priority:
                nearby = [priority] + [row for row in nearby
                                       if row[1] != priority_monster]
        # Bounded because real_monster() takes several reads. This runs on the
        # two-second scanner thread, but an unbounded report can still starve it.
        for _, unit in nearby[:12]:
            if not ms.real_monster(mem, unit):
                continue
            name = ms.monster_id(mem, unit) or f"monster@{unit & 0xFFFF:04X}"
            monster_names[unit] = name
            if name not in monster_counts:
                monster_order.append(name)
                monster_counts[name] = 0
            monster_counts[name] += 1
            if len(monster_order) >= max_monsters:
                break
    monsters = tuple(f"{name} x{monster_counts[name]}" for name in monster_order)
    return {"counts": counts, "player": own_name or "unknown",
            "players": tuple(players), "pets": tuple(pets),
            "monsters": monsters, "monster_names": monster_names}


class MemoryEyes:
    """Targets read from the game's unit list instead of inferred from pixels.

    Discovery is slow -- finding every instance of a class means scanning GBs --
    so it happens on a timer and each frame only re-reads the positions of units
    already found. Spawns show up a refresh late, which at this range is nothing.
    """

    # Class-level so a half-built instance -- demo()'s stubs assemble their own
    # state -- routes straight instead of raising. No map means no detour.
    walk = None
    path = path_to = last_pos = goal = loot_goal = escape = None
    path_at = escape_until = 0.0
    escape_side, escapes = 1, 0
    routing = sealed = False
    # The fence, or None for the roam-anywhere bot. `returning` is a plain flag
    # and deliberately not derived from `self.mode`: mode is also the status
    # string and half a dozen branches overwrite it, which is how the deleted
    # leash lost track of the fact it was walking home.
    area = None
    returning = False
    home_goal = wander = None
    wander_until = returning_since = wander_committed = 0.0

    def __init__(self, area=None):
        import memscan
        self.ms = memscan
        self.mem = memscan.Mem()
        self.classes = memscan.type_classes(self.mem)
        self.me = None            # our own BaseUnitController
        self.area = area          # the fence, or None to roam anywhere
        self.returning = False    # walking back inside it right now
        self.home_goal = self.wander = None
        self.wander_until = self.returning_since = 0.0
        self.wander_committed = 0.0
        self.basis = None         # stick push -> world travel
        self.units = []           # cached (kind, addr, x, y, z)
        self.chasing = None       # unit held between frames, so it does not flap
        self.chasing_id = None    # FishNet ObjectId; pointer remains the read handle
        self.target_name = ""     # cached only when target changes, for dashboard
        self.approach = None      # last heading that closed on a target
        self.orbit_dir = 1        # which way round a target we circle
        self.orbit_mark = None    # (time, x, z) the orbit last made progress at
        self.spacing_state = None # APPROACH, ATTACK, RETREAT
        self.engaged_since = None # when we started on the current target
        self.ignored = {}         # unit -> time it becomes fair game again
        self.ignored_ptr_ids = {} # unit -> ObjectId when that pointer was ignored
        self.ignored_ids = {}     # FishNet ObjectId -> the same, across wrappers
        self.mode = "no unit"
        self.misses = 0           # consecutive frames our position did not read
        self.seen_at = {}         # last known position per unit
        self.sweep_at = 0         # cursor into the far units, a slice per frame
        self.fight_ok = {}        # unit -> (expiry, attackable, invisible)
        self.hot = None           # regions worth sweeping
        self.hot_at = None        # (x, z) where the character was when hot was built
        self.owner = None         # our unit, from the local connection
        self.loot = {}            # drop -> (x, y, z, name)
        self.loot_name = ""       # what we are walking to, for the status line
        self.loot_target = None   # drop held between frames
        self.loot_since = None    # when we started walking to it
        self.loot_ignored = {}    # spawn key -> time it becomes fair game again
        self.loot_mode = "no loot"
        self.clock_at = None      # last time issued movement ownership was charged
        self.movement_owner = "idle"
        self.zone_rejected = None # last outside-target count printed
        self.boundary_log_at = 0.0 # throttle final movement-guard logs
        self.hot_loot = None      # regions worth sweeping for loot
        self.hot_loot_at = None   # (x, z) where the character was when hot_loot was built
        self.hot_full_at = 0.0    # when the last full (un-narrowed) unit pass ran
        self.hot_loot_full_at = 0.0
        self.hot_empty_streak = 0  # consecutive empty narrowed sweeps
        self.scan_summary = {"counts": {}, "player": "unknown",
                             "players": (), "pets": (), "monsters": (),
                             "monster_names": {}}
        self.scan_error = ""
        self.recovery = ""
        self.fallback_since = None
        self.next_full_rescan = 0.0
        self.walk = WalkMap().load()   # learned walls; the world, not our unit
        self.path = None          # cells to the current goal, world points
        self.path_at = 0.0        # when it was planned
        self.path_to = None       # what it was planned toward
        self.last_pos = None      # our (x, z) this frame, for the walk map
        self.routing = False      # steering at a waypoint, not at the target
        self.sealed = False       # the last goal had no route to it at all
        self.goal = None          # (x, z) we are walking at, for the slow sensor
        self.loot_goal = None     # the same, for the loot path
        self.escape = None        # stick that backs us out of a wedge
        self.escape_until = 0.0
        self.escape_side = 1      # alternated, so a corner is tried both ways
        self.escapes = 0          # unwedges at the current wedge point
        self.wedge_anchor = None  # (x, z) where the current streak first wedged
        self.scanner = None
        self.stop = None
        self.scan_passes = 0     # scanner stays alive; >0 means first pass landed
        self.scan_version = 0    # monotonic publication identity across relogs
        self.scan_in_progress = False
        self.scan_started_at = 0.0
        self.last_scan_completed_at = 0.0
        self.generation = 0       # invalidates a sweep crossing a relog boundary
        self.lock = threading.Lock()

    def available(self):
        return bool(self.classes.get("monster"))

    def heal(self, mem=None):
        """Find the classes by name after a patch moved the RVAs.

        A game update moves every entry in TYPE_RVA, and that used to end
        memory targeting for the session: the bot fell back to pixels and
        chased pets until somebody re-ran Il2CppDumper by hand. The class
        names do not move, so they can be searched for instead -- a few
        minutes on the background thread, while the bot keeps working on
        pixels. The slots it finds are written to a cache file, so the next
        run is instant again and the scan is once per patch.
        """
        mem = mem or self.mem
        print("\nmemory targeting: the class offsets are stale, which means the"
              " game was patched.\n  Searching for the classes by name instead"
              " (2-4 minutes, in the background).\n  The bot keeps running on"
              " pixels until it lands.")
        found = self.ms.find_classes(mem)
        if not found:
            print("memory targeting: could not find the classes by name either. "
                  "Staying on pixels.")
            return False
        # A targeted/partial recovery must not discard valid classes that were
        # already resolved for this process.
        self.classes = dict(self.classes, **found)
        rvas = {}
        for label, ptr in found.items():
            rva = self.ms.class_slot_rva(mem, ptr)
            if rva:
                rvas[label] = rva
        if rvas:
            self.ms.save_rva_cache(rvas)
            print("memory targeting: recovered. New offsets, cached for next "
                  "time -- " + ", ".join(f"{k}=0x{v:X}" for k, v in rvas.items()))
        else:
            print("memory targeting: recovered for this session, but the slots "
                  "could not be cached; the next run will search again.")
        return True

    def close(self):
        if self.stop is not None:
            self.stop.set()
        self.mem.close()

    def account_pursuit_time(self, now, owner=None):
        """Charge elapsed time only to the pursuit that owned the issued stick."""
        if self.clock_at is not None:
            elapsed = max(0.0, now - self.clock_at)
            if self.movement_owner != "monster" and self.engaged_since is not None:
                self.engaged_since += elapsed
            if self.movement_owner != "loot" and self.loot_since is not None:
                self.loot_since += elapsed
        self.clock_at = now
        if owner is not None:
            self.movement_owner = owner

    def reset_session(self):
        """Discard every address derived from the old login session.

        Reconnecting keeps the process alive but rebuilds its heap objects. The
        class pointers survive; unit/drop addresses, calibration and narrowed
        heap regions do not. Emptying them makes target() hand control to pixels
        until the scanner completes a fresh full-heap pass.
        """
        with self.lock:
            self.generation += 1
            self.units = []
            self.loot = {}
            self.me = self.basis = self.owner = self.hot = self.hot_loot = None
            self.hot_at = self.hot_loot_at = None
            self.chasing = self.engaged_since = self.approach = None
            self.chasing_id = None
            self.target_name = ""
            self.loot_target = self.loot_since = None
            self.loot_name, self.loot_mode = "", "no loot"
            self.scan_summary = {"counts": {}, "player": "unknown",
                                 "players": (), "pets": (), "monsters": (),
                                 "monster_names": {}}
            self.scan_error = ""
            self.recovery = ""
            self.scan_passes = 0
            self.scan_in_progress = False
            self.scan_started_at = 0.0
            self.last_scan_completed_at = 0.0
            self.fallback_since = None
            self.next_full_rescan = 0.0
            self.ignored, self.ignored_ptr_ids, self.ignored_ids = {}, {}, {}
            self.loot_ignored = {}
            self.clock_at, self.movement_owner = None, "idle"
            self.seen_at, self.fight_ok = {}, {}
            self.misses = self.sweep_at = 0
            self.path = self.path_to = self.last_pos = self.goal = self.loot_goal = None
            self.path_at = self.escape_until = 0.0
            self.escape = self.orbit_mark = None
            self.escapes, self.orbit_dir = 0, 1
            self.spacing_state = None
            self.wedge_anchor = None
            # The recorded area describes the world and survives. Its active
            # walk-back/wander decision belongs to the character that did not.
            self.returning, self.home_goal, self.wander = False, None, None
            self.wander_until = self.returning_since = self.wander_committed = 0.0
            self.routing = self.sealed = False
            self.mode = "no unit"
        if self.walk:
            self.walk.forget_walk()

    def _positions(self, addrs):
        # Hot path: a few hundred of these per frame, so the read and the
        # sanity check are inline rather than three function calls deep.
        out = {}
        read, off = self.mem.read, self.ms.UNIT_POSITION
        limit = self.ms.POS_MAX
        for a in addrs:
            blob = read(a + off, 12)
            if not blob or len(blob) != 12:
                continue
            x, y, z = struct.unpack("<fff", blob)
            # NaN fails every comparison, which is what excludes it here, and a
            # zeroed triple is recycled memory rather than a place.
            if (-limit < x < limit and -limit < y < limit and -limit < z < limit
                    and (abs(x) > 1e-3 or abs(z) > 1e-3)):
                out[a] = (x, y, z)
        return out

    def _fightable(self, unit, now=None):
        """Is this monster worth walking to? Cached, short TTL to notice deaths.

        real_monster() rather than worth_fighting(): the latter is about being
        rendered and alive, which a MonsterController with no identity also
        manages, and those cannot be damaged.
        """
        now = time.time() if now is None else now
        hit = self.fight_ok.get(unit)
        if hit and hit[0] > now:
            return hit[1]
        state = getattr(self.ms, "monster_target_state", None)
        if state:
            ok, invisible = state(self.mem, unit)
        else:                       # ponytail: compatibility for tiny test stubs
            ok, invisible = self.ms.real_monster(self.mem, unit), False
        self.fight_ok[unit] = (now + LIVE_TTL_S, ok, invisible)
        return ok

    def _stable_id(self, unit):
        """Preferred held-target identity; absent on old test stubs/failed reads."""
        reader = getattr(self.ms, "network_object_id", None)
        if reader is None:
            return None
        try:
            return reader(self.mem, unit)
        except (OSError, ValueError, struct.error):
            return None

    def _target_ignored(self, unit, now, stable_id=None):
        """Blacklist by read handle and, when available, FishNet spawn identity."""
        if self.ignored.get(unit, 0.0) >= now:
            old_id = getattr(self, "ignored_ptr_ids", {}).get(unit)
            current_id = self._stable_id(unit) if stable_id is None else stable_id
            # A positive different ObjectId proves the pooled pointer now names
            # another spawn. Failed/transient reads remain conservatively ignored.
            if old_id is None or current_id is None or current_id == old_id:
                return True
        stable_id = self._stable_id(unit) if stable_id is None else stable_id
        return (stable_id is not None
                and getattr(self, "ignored_ids", {}).get(stable_id, 0.0) >= now)

    def _ignore_target(self, unit, until, stable_id=None):
        """Ignore a spawn through wrapper churn, retaining pointer fallback."""
        self.ignored[unit] = until
        stable_id = self._stable_id(unit) if stable_id is None else stable_id
        if not hasattr(self, "ignored_ptr_ids"):
            self.ignored_ptr_ids = {}
        self.ignored_ptr_ids[unit] = stable_id
        if stable_id is not None:
            if not hasattr(self, "ignored_ids"):
                self.ignored_ids = {}
            self.ignored_ids[stable_id] = until

    def _known_invisible(self, unit):
        """Whether the liveness check just identified this candidate as cloaked."""
        hit = self.fight_ok.get(unit)
        return bool(hit and len(hit) > 2 and hit[2])

    def _first_fightable(self, ranked, now=None):
        """Nearest entry that is really there. `ranked` is sorted by distance."""
        for d, u, x, y, z in ranked:
            if not self._target_ignored(u, now) and self._fightable(u, now):
                return (u, x, y, z), d
        return None, None

    def _live_positions(self, addrs):
        """Positions, reading only the units that could matter this frame.

        Reading all 1473 units every frame cost 16 ms, and most of them are the
        other side of the map: nothing a monster does in 50 ms makes it the
        nearest one from 200 units away. Near units are re-read every frame and
        the rest a slice at a time, for the same answer at a fraction of the
        syscalls.
        """
        me = self.seen_at.get(self.me)
        if not me:                       # nothing to measure distance from yet
            fresh = self._positions(addrs)
            self.seen_at.update(fresh)
            return fresh
        near, far = [], []
        for a in addrs:
            was = self.seen_at.get(a)
            if a == self.me or was is None:
                near.append(a)
            elif ((was[0] - me[0]) ** 2 + (was[2] - me[2]) ** 2) ** 0.5 < NEAR_KEEP:
                near.append(a)
            else:
                far.append(a)
        # one slice of the far ones per frame, cycling through them
        if far:
            step = max(1, -(-len(far) // SWEEP_FRAMES))
            start = self.sweep_at % len(far)
            near += far[start:start + step] or far[:step]
            self.sweep_at = start + step
        fresh = self._positions(near)
        self.seen_at.update(fresh)
        # `seen_at` is scheduling history for the far-unit slices. An address we
        # actually attempted this frame must come from this frame, especially
        # our player: returning its old coordinate makes MEM_LOST_FRAMES
        # unreachable after a relog or object rebuild.
        attempted = set(near)
        return {a: p for a, p in self.seen_at.items()
                if a not in attempted or a in fresh}

    def calibrate(self, pad):
        """Find which unit is us, and how a stick push maps to world travel.

        Our unit is the one that moves when we push, which is a fact we can
        create on demand rather than infer. It
        works mid-combat, unlike anything built on walking a clean line.
        """
        players = self.known_players()   # from the background sweep, never blocks
        if self.owner is not None and self.owner not in players:
            # local_player() resolves from any NetworkBehaviour and does not need
            # the PlayerController class slot. A partial patch recovery can know
            # our owner before player enumeration works; that is enough for the
            # two owner-only calibration legs.
            players.append(self.owner)
        if not players:
            return False

        me = self.owner

        def calibration_safe():
            if self.area is None:
                return True
            if me is None:
                return False
            position = self._positions([me]).get(me)
            if not position:
                return False
            px, pz = position[0], position[2]
            if not self.area.inside(px, pz):
                # Startup/reconnect pixels are intentionally allowed while the
                # basis is unknown. They can carry us over the recorded edge;
                # refusing every calibration push there creates a permanent
                # deadlock: no basis means we cannot route back inside, so the
                # bot remains on pixels forever. Calibrate, then target()'s
                # area return immediately takes over. A player still inside but
                # too near the edge remains protected from an outward probe.
                return True
            return self.area.safe(px, pz, AREA_SAFETY + AREA_LOOKAHEAD)

        def push(sx, sy):
            """World delta per unit over one push."""
            if not calibration_safe():
                return {}
            before = self._positions(players)
            t0 = time.time()
            while time.time() - t0 < MEM_CAL_PUSH_S:
                if not calibration_safe():
                    break
                pad.stick(sx, sy, True)
                time.sleep(0.05)
            pad.stick(0.0, 0.0, True)
            time.sleep(0.2)
            after = self._positions(players)
            return {a: (after[a][0] - before[a][0], after[a][2] - before[a][2])
                    for a in before if a in after}

        # Two jobs here, and only one of them still needs walking. WHO we are
        # comes from the local connection now (self.owner) -- a pointer walk,
        # no pushing. WHAT a push does to our position cannot be read that way:
        # it depends on the camera angle, so it has to be measured.
        #
        # Without the owner this falls back to the old way, which is why the
        # six legs and pick_me() are still here: picking the biggest mover in
        # one leg does not work, because on a busy map another player out-walks
        # us, calibration locks onto them, and every later leg is thrown away
        # as "not us" -- failing outright with a healthy character standing
        # right there.
        if (me is not None
                and not any(u == me for _, u, *_ in self.units)
                and me not in self._positions([me])):
            # Missing player-class rows are not evidence against local_player(),
            # but an unreadable owner address is evidence of a relog/rebuild.
            me = self.owner = None
        pushes = ((1.0, 0.0), (0.0, 1.0)) if me else (
            (1.0, 0.0), (0.0, 1.0), (-1.0, 0.0), (0.0, -1.0),
            (0.7, 0.7), (-0.7, 0.7))
        legs = []
        for sx, sy in pushes:
            moved = push(sx, sy)
            if moved:
                legs.append(((sx, sy), moved))
            if me and len(legs) >= 2:
                break              # two non-parallel legs is a whole basis
        if me is None:
            me = pick_me(legs)
        if me is None:
            return False

        samples = []
        for (sx, sy), moved in legs:
            if me not in moved:
                continue
            dx, dz = moved[me]
            dist = (dx * dx + dz * dz) ** 0.5
            if dist < MEM_CAL_MIN:
                continue                    # blocked that way; the wall is fine
            samples.append(((sx, sy), (dx, dz)))
            if len(samples) >= (2 if self.owner else MEM_CAL_LEGS):
                # Least squares over every leg that moved, not just two of them.
                # In a fight the character gets shoved and stunned, so a single
                # leg can be well off -- one bad push used to become the whole
                # basis, and the scale with it.
                S = np.array([s for s, _ in samples], dtype=float)
                W = np.array([w for _, w in samples], dtype=float)
                if np.linalg.matrix_rank(S) < 2:
                    continue                # all pushes along one line so far
                fit, *_ = np.linalg.lstsq(S, W, rcond=None)
                basis = ((float(fit[0][0]), float(fit[1][0])),
                         (float(fit[0][1]), float(fit[1][1])))
                if stick_for(basis, 1.0, 0.0) is None:
                    continue
                self.me, self.basis = me, basis
                return True
        return False

    def _ensure_classes(self, mem, requests):
        """Find missing optional classes in one heap pass and cache together.

        Units can be healthy while optional player/loot slots are stale.
        `find_classes` streams the same multi-gigabyte regions whatever subset
        is requested, so those names must share one pass instead of taking
        several minutes each in series.
        """
        requests = tuple((label, why) for label, why in requests
                         if not self.classes.get(label))
        if not requests:
            return set()
        wanted = {label: self.ms.CLASS_NAMES[label] for label, _ in requests}
        self.recovery = "recovering optional classes in one pass: " + "+".join(wanted)
        found = {label: ptr for label, ptr in self.ms.find_classes(mem, wanted).items()
                 if ptr}
        self.classes = dict(self.classes, **found)
        rvas = {}
        for label, ptr in found.items():
            rva = self.ms.class_slot_rva(mem, ptr)
            if rva:
                rvas[label] = rva
        if rvas:
            self.ms.save_rva_cache(dict(self.ms.load_rva_cache(), **rvas))
        for label, why in requests:
            ptr = found.get(label)
            if not ptr:
                print(f"\n{why}: no {self.ms.CLASS_NAMES[label]} class "
                      f"found; off this session")
                continue
            rva = rvas.get(label)
            print(f"\n{why}: {self.ms.CLASS_NAMES[label]} found at 0x{ptr:X}"
                  + (f", slot cached (0x{rva:X})" if rva else ""))
        self.recovery = ""
        return set(found)

    def _ensure_class(self, mem, label, why):
        """Compatibility wrapper for callers that need one optional class."""
        return label in self._ensure_classes(mem, ((label, why),))

    def _sweep_loot(self, mem, generation=None):
        """Refresh ground loot. world_loot() has already dropped the pool.

        LootDrop objects are pooled -- a fixed set, recycled, and picking an
        item up frees neither the object nor its position, the same trap the
        pooled monsters set. What separates a real drop is that it carries an
        item at all: measured on a live field, 157 of 192 slots had no name and
        the 35 that did matched what was lying there.
        """
        # Capture the region set once and use it for both the sweep and the
        # narrowing decision: a movement reset that clears hot_loot mid-sweep
        # must not turn this narrowed pass into a re-narrow to the old regions.
        regions = self.hot_loot
        # Same backstop as the unit sweep: a full pass every HOT_SELF_HEAL_S so
        # a stale loot cache self-heals even when the character never walks
        # far enough to trip the movement re-narrow.
        if regions is not None and time.time() - self.hot_loot_full_at >= HOT_SELF_HEAL_S:
            regions = None
        found = self.ms.world_loot(mem, self.classes.get("loot"),
                                   regions=regions)
        if regions is None:
            with self.lock:
                self.hot_loot_full_at = time.time()
        with self.lock:
            if generation is not None and generation != self.generation:
                return
            old_keys = {self._loot_key(d, x, z, n)
                        for d, (x, _y, z, n) in self.loot.items()}
            self.loot = {d: (x, y, z, n) for d, x, y, z, n in found}
            new_keys = {self._loot_key(d, x, z, n)
                        for d, (x, _y, z, n) in self.loot.items()}
            # Observing an occupancy disappear proves its give-up state belongs
            # to the old pooled spawn. The same wrapper/name/position may later
            # be reused for a collectible new drop.
            for key in old_keys - new_keys:
                self.loot_ignored.pop(key, None)
            # Narrow only after a full pass (regions was None when the sweep
            # ran), so an in-flight narrowed sweep cannot re-narrow to the old
            # regions after a movement reset cleared them.
            narrow = regions is None and bool(found)
        if narrow:
            spans = mem.regions()
            live = {d for d, *_rest in found}
            hot = [(b, s) for b, s in spans
                   if any(b <= d < b + s for d in live)]
            with self.lock:
                if generation is None or generation == self.generation:
                    self.hot_loot = hot
                    self.hot_loot_at = self.last_pos

    def loot_here(self):
        """Is a wanted item lying under us right now? Sets loot_name if so.

        Separate from pick_loot() because it has to work *during* a fight: the
        kill that drops the item leaves it at our feet, and waiting for the
        monster path to go quiet before pressing the trigger means walking off
        the drop first. Pressing costs nothing when there is nothing there, but
        an item at our feet is the common case, so it is worth the check.
        """
        if not (LOOT_PICKUP and self.me):
            return False
        here = self._positions([self.me]).get(self.me)
        if not here:
            return False
        px, _, pz = here
        with self.lock:
            drops = list(self.loot.values())
        for x, _, z, name in drops:
            if not wanted_item(name):
                continue
            if (x - px) ** 2 + (z - pz) ** 2 <= LOOT_ARRIVE ** 2:
                self.loot_name = name
                return True
        return False

    def loot_debug(self, now):
        """Why the loot path offered nothing, in one line.

        An item ignored at the character's feet has three causes that look
        identical from outside: it is not in the sweep's cache at all, it is
        blacklisted, or it is not wanted. Naming which one is the whole job.
        """
        here = self._positions([self.me]).get(self.me) if self.me else None
        if not here:
            return "no position"
        px, _, pz = here
        with self.lock:
            drops = list(self.loot.items())
        if not drops:
            return "cache empty -- the sweep is finding no drops at all"
        near = sorted((math.hypot(x - px, z - pz),
                       self._loot_key(d, x, z, n), n)
                      for d, (x, _, z, n) in drops)[:3]
        return f"{len(drops)} cached; nearest " + ", ".join(
            f"{n[:14]}@{dist:.1f}"
            f"{' IGNORED' if self.loot_ignored.get(key, 0) > now else ''}"
            f"{'' if wanted_item(n) else ' unwanted'}"
            for dist, key, n in near)

    @staticmethod
    def _loot_key(drop, x, z, name):
        """Identity of one pooled-slot occupancy, not the reusable wrapper."""
        return drop, name, x, z

    def pick_loot(self, now):
        """(sx, sy, distance) toward a dropped item, or (None, None, None).

        Only items in LOOT_NAMES, if that is set, and only within LOOT_RANGE.
        The held target survives between frames so the bot does not swap items
        every time two are the same distance away, and it is given up on and
        ignored after LOOT_MAX_S -- an item that cannot be collected would
        otherwise hold the bot on the spot pressing the trigger forever.
        """
        self.loot_goal = None            # same rule as target()'s goal
        if not (LOOT_PICKUP and self.me and self.basis):
            self.loot_mode = "no loot"
            return None, None, None
        here = self._positions([self.me]).get(self.me)
        if not here:
            self.loot_mode = "no loot"
            return None, None, None
        px, _, pz = here
        self.last_pos = (px, pz)
        with self.lock:
            drops = list(self.loot.items())
        ranked = sorted((((x - px) ** 2 + (z - pz) ** 2) ** 0.5,
                         self._loot_key(d, x, z, n), d, x, z, n)
                        for d, (x, _, z, n) in drops
                        if self.loot_ignored.get(self._loot_key(d, x, z, n), 0) < now
                        and wanted_item(n)
                        and (self.area is None or self.area.inside(x, z)))
        ranked = [r for r in ranked if r[0] <= LOOT_RANGE]
        if not ranked:
            self.loot_target = self.loot_since = None
            self.loot_mode = "no loot"
            return None, None, None

        held = next((r for r in ranked if r[1] == self.loot_target), None)
        pick = held or ranked[0]
        if pick[1] != self.loot_target:
            self.loot_target, self.loot_since = pick[1], now
        elif self.loot_since and now - self.loot_since > LOOT_MAX_S:
            # Same rule the monster path needs: something we cannot collect has
            # to be dropped, or it owns the bot. The clock counts only time
            # spent actually walking to it -- main() restarts it on every frame
            # the item loses the arbitration, because an item that keeps losing
            # to a nearer monster was otherwise blacklisted for LOOT_IGNORE_S
            # having never been approached at all, which reads from outside as
            # the bot ignoring a drop at its feet.
            self.loot_ignored[pick[1]] = now + LOOT_IGNORE_S
            self.loot_target = self.loot_since = None
            self.loot_mode = "loot skip"
            return None, None, None

        dist, _, drop, x, z, name = pick
        self.loot_name = name
        if dist <= LOOT_ARRIVE:
            self.loot_mode = "loot get"
            return 0.0, 0.0, dist
        gx, gz = self.route_to(now, px, pz, x, z)
        s = stick_for(self.basis, gx - px, gz - pz)
        if not s:
            self.loot_mode = "no loot"
            return None, None, None
        self.loot_goal = (gx, gz)
        self.loot_mode = "loot"
        return s[0], s[1], dist

    def start_scanning(self):
        """Rediscover units in the background, forever. Never blocks the bot.

        The first sweep has to read ~8 GB and takes about 14 seconds; narrowed
        afterwards to the regions that actually held units, it drops to about
        one. Both are far too slow for a 20 Hz loop, so this runs on its own
        thread with its own handle and the bot works from pixels until the first
        list lands. Nothing waits.
        """
        import threading
        self.stop = threading.Event()

        def sweep():
            mem = self.ms.Mem(self.mem.pid)
            class_retry = {"player": 0.0, "loot": 0.0}
            heal_at = 0.0
            saved = 0.0           # the walk map is written from here, not the
                                  # 50 ms frame, and only when it changed
            before = {}           # last sweep's positions, to spot who moved
            before_generation = None
            try:
                while not self.stop.is_set():
                    if not self.available():
                        stamp = time.time()
                        if stamp >= heal_at:
                            self.heal(mem)
                            heal_at = time.time() + HOT_SELF_HEAL_S
                        self.stop.wait(MEM_REFRESH_S)
                        continue
                    with self.lock:
                        generation, hot = self.generation, self.hot
                        # Backstop for the movement re-narrow: a narrowed cache
                        # can go stale for reasons walking does not catch -- the
                        # bot confined to a pen smaller than the re-narrow radius
                        # never walks far enough to drop it, or everything in the
                        # old regions despawns. One un-narrowed pass every
                        # HOT_SELF_HEAL_S is a cheap safety net; the bot works
                        # from the cached list while the full pass runs.
                        if hot is not None and time.time() - self.hot_full_at >= HOT_SELF_HEAL_S:
                            hot = None
                    with self.lock:
                        if generation == self.generation:
                            self.scan_in_progress = True
                            self.scan_started_at = time.time()
                    try:
                        found = self.ms.world_units(mem, regions=hot,
                                                    classes=self.classes)
                    finally:
                        with self.lock:
                            if generation == self.generation:
                                self.scan_in_progress = False
                    if hot is None:
                        with self.lock:
                            self.hot_full_at = time.time()
                        self.hot_empty_streak = 0
                    else:
                        self._drop_stale_hot(found)
                    with self.lock:
                        if generation != self.generation:
                            continue
                        self.units = found
                        self.scan_passes += 1
                        self.scan_version = getattr(self, "scan_version", 0) + 1
                        self.last_scan_completed_at = time.time()
                    if self.owner is None and found:
                        # Who we are, read instead of walked for: any unit
                        # carries the managers, so this is a pointer walk with
                        # nothing to search. Re-read whenever it is lost, since
                        # a map change rebuilds the object.
                        owner = self._find_owner(mem, found)
                        with self.lock:
                            if generation == self.generation:
                                self.owner = owner
                    with self.lock:
                        report_owner = self.owner
                        report_target = self.chasing
                    try:
                        report = memory_scan_summary(self.ms, mem, found,
                                                     report_owner,
                                                     priority_monster=report_target)
                        report_error = ""
                    except Exception as e:
                        # Diagnostics must never take targeting down. The next
                        # two-second sweep gets another chance at transient data.
                        report, report_error = None, str(e)
                    with self.lock:
                        if generation == self.generation:
                            if report is not None:
                                self._accept_scan_summary(report)
                            self.scan_error = report_error
                    stamp = time.time()
                    due = []
                    for label, why, enabled in (
                            ("player", "player classification", True),
                            ("loot", "loot pickup", LOOT_PICKUP)):
                        if (enabled and not self.classes.get(label)
                                and stamp >= class_retry[label]):
                            due.append((label, why))
                    if due:
                        self._ensure_classes(mem, due)
                        # A class scan may itself outlast the retry interval.
                        # Count from completion or one missing class immediately
                        # starts another multi-minute pass and appears endless.
                        retry_at = time.time() + HOT_SELF_HEAL_S
                        for label, _ in due:
                            class_retry[label] = retry_at
                    if LOOT_PICKUP and self.classes.get("loot"):
                        self._sweep_loot(mem, generation)
                    if PATHFIND:
                        # Everything alive walks the same navmesh we do, so a
                        # unit that moved since the last sweep has just proven
                        # its ground walkable. Free floor, hundreds of cells at
                        # a time, in places the bot has never been.
                        with self.lock:
                            if generation != self.generation:
                                continue
                            if before_generation != generation:
                                before = {}
                                before_generation = generation
                            walkers = [(x, z) for _, u, x, _, z in found
                                       if u in before and before[u] != (x, z)]
                            before = {u: (x, z) for _, u, x, _, z in found}
                            # reset_session() takes this same lock. It therefore
                            # cannot advance the generation between this final
                            # ownership check and painting movement history from
                            # an obsolete session.
                            self.walk.paint(walkers)
                        if time.time() - saved >= WALK_SAVE_S:
                            self.walk.save()
                            saved = time.time()
                    if hot is None and found:
                        # A full pass just ran (hot was None), so narrow the next
                        # sweep to where the units turned out to be, rather than
                        # paying for the whole heap every time. Keying off the
                        # *captured* hot -- not re-reading self.hot -- is what
                        # keeps an in-flight narrowed sweep from re-narrowing to
                        # the old regions after a movement reset cleared them.
                        spans = mem.regions()
                        live = {u for _, u, *_ in found}
                        hot = [(b, s) for b, s in spans
                               if any(b <= u < b + s for u in live)]
                        with self.lock:
                            if generation == self.generation:
                                self.hot = hot
                                self.hot_at = self.last_pos
                    self.stop.wait(MEM_REFRESH_S)
            finally:
                mem.close()

        def loop():
            # One failed read used to end the thread, silently and forever:
            # the unit list froze, the bot reported "no monster" from then on,
            # and the End toggle would not restart it (a dead Thread is not
            # None). Take the sweep from the top instead.
            while not self.stop.is_set():
                try:
                    sweep()
                except Exception as e:
                    print(f"\nunit sweep failed ({e}); retrying")
                    self.stop.wait(MEM_REFRESH_S)

        self.scanner = threading.Thread(target=loop, daemon=True)
        self.scanner.start()

    def _accept_scan_summary(self, report):
        """Publish a background report and fill a label that was pending."""
        self.scan_summary = report
        self.recovery = ""
        name = report.get("monster_names", {}).get(self.chasing)
        if name:
            self.target_name = name

    def note_pixel_fallback(self, now, evidence):
        """Request a full sweep when pixels repeatedly contradict memory."""
        healthy = bool(self.me and self.basis and self.mode == "no monster")
        if not evidence or not healthy:
            self.fallback_since = None
            return False
        if self.fallback_since is None:
            self.fallback_since = now
            return False
        if (now - self.fallback_since < MEM_PIXEL_RESCAN_S
                or now < self.next_full_rescan):
            return False
        with self.lock:
            self.generation += 1
            self.hot = None
            self.hot_at = None
            self.units = []
            self.scan_summary = {"counts": {}, "player": "unknown",
                                 "players": (), "pets": (), "monsters": (),
                                 "monster_names": {}}
        self.fallback_since = None
        self.next_full_rescan = now + MEM_FULL_RESCAN_COOLDOWN_S
        self.recovery = "full memory rescan: pixels see targets memory missed"
        return True

    def _why_switched(self, now, ranked):
        """Which test the previous target failed. Diagnosis only.

        A bot that walks left, right, left looks the same whether it is killing
        things quickly, losing track of one, or flapping between two -- and
        those need different fixes. Naming the reason is what tells them apart.
        """
        was = self.chasing
        if was is None:
            return "nothing held"
        entry = next((e for e in ranked if e[1] == was), None)
        if entry is None:
            if self.ignored.get(was, 0.0) >= now:
                return "held one blacklisted"
            return "held one left the unit list"
        if self.area is not None and not self.area.inside(entry[2], entry[4]):
            return "held one walked out of the area"
        if not self._fightable(was):
            return "held one died or stopped being fightable"
        if entry[0] > MEM_RANGE:
            return f"held one is {entry[0]:.0f} away, past MEM_RANGE"
        return "something clearly nearer (TARGET_SWITCH)"

    def _wander_spot(self, px, pz):
        """Somewhere in the area worth walking to -- not where we already are.

        `spot()` is uniform over the area, so on a small one most picks land
        inside AREA_WANDER_REACHED and count as arrived immediately. Try a few
        times for somewhere further off, and take whatever the last try gives
        if the area is genuinely too small to hold one.
        """
        pick = None
        for _ in range(AREA_WANDER_TRIES):
            pick = self.area.spot(random)
            if pick is None:
                return None
            if math.hypot(pick[0] - px, pick[1] - pz) >= AREA_WANDER_MIN:
                return pick
        return pick

    def _wander(self, now, px, pz):
        """Nothing left to kill inside the area, so move rather than stand.

        Monsters respawn and roam; a bot parked on one spot farms whatever
        happens to walk into it, which over an hour is close to nothing. A
        cell picked uniformly is uniform over *area* for free -- which is what
        the deleted patrol_point() needed a sqrt() to fake on a circle.
        """
        reached = (self.wander is not None
                   and math.hypot(px - self.wander[0], pz - self.wander[1])
                   < AREA_WANDER_REACHED)
        stale = now > self.wander_until
        # The commit floor is what actually stops the shaking: without it an
        # arrival re-picks on the very next frame, and a point chosen closer
        # than AREA_WANDER_REACHED has already "arrived" when it is chosen.
        settled = now < self.wander_committed
        if self.wander is None or ((reached or stale) and not settled):
            self.wander = self._wander_spot(px, pz)
            self.wander_until = now + AREA_WANDER_HOLD_S
            self.wander_committed = now + AREA_WANDER_COMMIT_S
        if self.wander is None:
            self.mode = "no monster"
            return None, None, None
        gx, gz = self.route_to(now, px, pz, *self.wander)
        s = stick_for(self.basis, gx - px, gz - pz)
        if not s:
            self.wander = None
            self.mode = "no monster"
            return None, None, None
        self.goal = (gx, gz)
        self.mode = "wander"
        return s[0], s[1], math.hypot(self.wander[0] - px, self.wander[1] - pz)

    def _go_home(self, now, px, pz):
        """Walk back inside the area, or give the fence up saying why.

        Routed, never a straight line: the leash steered raw at its anchor and
        so leaned into rock for as long as it took someone to notice. The
        distance returned is the real one -- `loot_wins()` compares it against
        the nearest drop, and a hard zero would make walking home unbeatable
        by an item lying two steps ahead of it, the same trap `unwedge` sets.
        """
        hx, hz = self.home_goal or (px, pz)
        away = math.hypot(hx - px, hz - pz)
        if away > AREA_ABANDON:
            # Another map entirely -- nothing records which map an area
            # belongs to. Never drop the fence here: the next frame would hand
            # unrestricted actuation to pixels. Hold safely and require the run
            # to be restarted with the correct named area.
            failure = (self.area.name, round(away))
            if getattr(self, "area_failed", None) != failure:
                print(chr(10) + f"area {self.area.name!r} is {away:.0f} units away"
                      " -- likely the wrong map; holding safely")
                self.area_failed = failure
            self.goal = None
            self.mode = "no area"
            return 0.0, 0.0, away
        self.area_failed = None
        if now - self.returning_since > AREA_RETURN_MAX_S:
            # The walk back is not working. Do not drop the fence: unfencing
            # lets the bot roam outside and chase monsters that are not in the
            # area, which is the one failure that looks like a bot that "walks
            # to the wrong place". Recompute the nearest inset-safe home point
            # and restart the timer; targeting the exact boundary deadlocks the
            # final movement guard at its safety margin.
            self.home_goal = self.area.home(px, pz)
            self.returning_since = now
            hx, hz = self.home_goal
            away = math.hypot(hx - px, hz - pz)
        gx, gz = self.route_to(now, px, pz, hx, hz)
        s = stick_for(self.basis, gx - px, gz - pz)
        if not s:
            self.mode = "no area"
            return None, None, None
        self.goal = (gx, gz)        # so observe_move judges the right goal
        self.mode = "going back"
        return s[0], s[1], away

    def route_to(self, now, px, pz, tx, tz):
        """Where to actually steer -- the target, or a way round a known wall.

        The straight line is the default and stays it: a route is only planned
        when the line crosses something the map says is solid. A failed plan
        returns the target too, so the worst case is exactly today's behaviour
        and MEM_ENGAGE_MAX_S gives up as it always did. Never returns nothing:
        main() reads a zero stick as handled and parks the bot.
        """
        self.routing = self.sealed = False
        allowed = None
        edge_allowed = None
        if self.area is not None and self.area.safe(px, pz):
            allowed = self.area.safe
            edge_allowed = lambda x0, z0, x1, z1: self.area.guard_step(
                (x0, z0), (x1, z1))[0]
            if not self.area.safe(tx, tz):
                # Exact target admission includes the boundary, while player
                # movement is inset. Route to the nearest safe fighting point;
                # the target distance still controls engagement/give-up.
                tx, tz = self.area.home(tx, tz)
        if not (PATHFIND and self.walk):
            return tx, tz
        area_crossed = (edge_allowed is not None
                        and not edge_allowed(px, pz, tx, tz))
        if (not area_crossed
                and not self.walk.crossed(px, pz, tx, tz, now)):
            self.path = None
            return tx, tz
        fresh = (self.path and now - self.path_at < WALK_REPLAN_S
                 and self.path_to
                 and math.hypot(tx - self.path_to[0], tz - self.path_to[1])
                 < WALK_WAYPOINT)
        if not fresh:
            self.path = self.walk.route(px, pz, tx, tz, now,
                                        allowed=allowed,
                                        edge_allowed=edge_allowed)
            self.path_at, self.path_to = now, (tx, tz)
        if not self.path:
            # No path at all, and the search was not merely out of budget: the
            # goal is walled off from here. Say so, so the caller can drop the
            # monster instead of leaning on the wall for MEM_ENGAGE_MAX_S.
            self.sealed = not self.walk.capped
            return tx, tz
        self.routing = True
        waypoint = self.walk.waypoint(px, pz, self.path)
        if edge_allowed is not None and not edge_allowed(px, pz, *waypoint):
            visible = [point for point in self.path
                       if edge_allowed(px, pz, *point)]
            waypoint = visible[-1] if visible else self.path[0]
        return waypoint

    def observe_move(self, now, sx, sy, on_loot=False):
        """Feed the walk map the stick that actually went out this frame.

        It has to be the issued one, not the one target() computed: loot can
        win the arbitration, and a wall learned against a stick we never sent
        would sit in the map at the wrong angle. `on_loot` is that arbitration's
        answer, and picks which goal the slow sensor is judging progress toward
        -- pick_loot() runs even when it loses, so its goal cannot be assumed.
        """
        if not (PATHFIND and self.walk and self.last_pos and self.basis):
            return None
        px, pz = self.last_pos
        mode = self.loot_mode if on_loot else self.mode
        goal = (getattr(self, "guard_goal", None)
                or (self.loot_goal if on_loot else self.goal))
        hit = self.walk.observe(now, px, pz, sx or 0.0, sy or 0.0, self.basis,
                                mode, goal)
        if hit:
            self.path = None            # a new wall: the old route is a lie
            if self.walk.wedged:
                self.wedge_off(now, sx or 0.0, sy or 0.0)
        return hit

    def guard_area_step(self, sx, sy, now=None):
        """Validate the final stick against the fence before controller output."""
        now = time.time() if now is None else now
        self.guard_goal = None
        if not (self.area and self.last_pos and self.basis and (sx or sy)):
            return sx, sy, False
        px, pz = self.last_pos
        dx, dz = world_for(self.basis, sx, sy)
        length = math.hypot(dx, dz)
        if length < 1e-9:
            return 0.0, 0.0, False
        proposed = (px + dx / length * AREA_LOOKAHEAD,
                    pz + dz / length * AREA_LOOKAHEAD)
        allowed, safe = self.area.guard_step((px, pz), proposed)
        if allowed:
            return sx, sy, False

        # guard_step() returns a nearby safe point, but stick_for() is
        # direction-only and normalizes it back to full magnitude. Revalidate the
        # heading that would actually go out, and rotate inward until its complete
        # lookahead segment is safe. This also gives an outward unwedge at the safe
        # edge a real lateral/back escape instead of collapsing the whole interval
        # to zero.
        ux, uz = dx / length, dz / length
        redirected = None
        endpoint = safe
        if not self.area.safe(px, pz):
            # We may discover the character already outside after a relog or a
            # target carried it over the line. No three-unit endpoint can be
            # "inside" when home is farther away than that, so fail-closed must
            # mean measurable progress toward home, not a zero stick forever.
            hx, hz = self.area.home(px, pz)
            ix, iz = hx - px, hz - pz
            inward = math.hypot(ix, iz)
            if inward > 1e-9:
                side = 1.0 if ix * dz - iz * dx >= 0.0 else -1.0
                inward_way = (ix / inward, iz / inward)
                lateral_way = (inward_way[0] - inward_way[1] * 0.7 * side,
                               inward_way[1] + inward_way[0] * 0.7 * side)
                ways = ((lateral_way, inward_way) if self.mode == "unwedge"
                        else (inward_way, lateral_way))
                for wx, wz in ways:
                    candidate = stick_for(self.basis, wx, wz)
                    if not candidate:
                        continue
                    cdx, cdz = world_for(self.basis, *candidate)
                    clen = math.hypot(cdx, cdz)
                    issued = (px + cdx / clen * AREA_LOOKAHEAD,
                              pz + cdz / clen * AREA_LOOKAHEAD)
                    if (self.area.safe(*issued)
                            or math.hypot(issued[0] - hx, issued[1] - hz) < inward):
                        redirected, endpoint = candidate, issued
                        break
        for degrees in (0, 15, -15, 30, -30, 45, -45, 60, -60,
                        75, -75, 90, -90, 105, -105, 120, -120,
                        135, -135, 150, -150, 165, -165, 180):
            if redirected is not None:
                break
            angle = math.radians(degrees)
            wx = ux * math.cos(angle) - uz * math.sin(angle)
            wz = ux * math.sin(angle) + uz * math.cos(angle)
            candidate = stick_for(self.basis, wx, wz)
            if not candidate:
                continue
            cdx, cdz = world_for(self.basis, *candidate)
            clen = math.hypot(cdx, cdz)
            if clen < 1e-9:
                continue
            issued = (px + cdx / clen * AREA_LOOKAHEAD,
                      pz + cdz / clen * AREA_LOOKAHEAD)
            if self.area.guard_step((px, pz), issued)[0]:
                redirected, endpoint = candidate, issued
                break
        if now >= getattr(self, "boundary_log_at", 0.0):
            print(f"\nzone boundary: blocked step at {proposed[0]:.1f},"
                  f"{proposed[1]:.1f}; redirecting inside")
            self.boundary_log_at = now + AREA_BOUNDARY_LOG_S
        self.goal = endpoint
        self.guard_goal = endpoint
        return (*(redirected or (0.0, 0.0)), True)

    def wedge_off(self, now, sx, sy):
        """Back out sideways from whatever we are jammed against.

        Only the fast sensor sets this off, and it means the character did not
        move at all -- which no amount of routing can fix, since a bot that
        cannot move cannot learn a second cell to route around. Turn the stick
        well off the blocked heading for a moment, alternating sides so a
        corner that defeats one way out is escaped the other.
        """
        turn = WALK_ESCAPE_TURN * self.escape_side
        c, s = math.cos(turn), math.sin(turn)
        self.escape = (sx * c - sy * s, sx * s + sy * c)
        self.escape_until = now + WALK_ESCAPE_S
        self.escape_side = -self.escape_side
        self.escapes += 1
        # Anchor the counter to where we wedged. It is set on the first
        # unwedge of a streak and left alone afterwards: the counter counts
        # unwedges at this spot, and target() clears it once we are far away.
        if getattr(self, "wedge_anchor", None) is None:
            lp = getattr(self, "last_pos", None)
            if lp:
                self.wedge_anchor = lp

    def _orbit_way(self, now, px, pz):
        """Which way round the target to go. Reverses when the circle stops
        getting anywhere -- a wall, a corner, another body in the way."""
        if self.orbit_mark is None:
            self.orbit_mark = (now, px, pz)
            return self.orbit_dir
        since, mx, mz = self.orbit_mark
        if math.hypot(px - mx, pz - mz) >= MEM_ORBIT_MIN_MOVE:
            self.orbit_mark = (now, px, pz)   # moving; keep going this way
        elif now - since >= MEM_ORBIT_FLIP_S:
            self.orbit_dir = -self.orbit_dir
            self.orbit_mark = (now, px, pz)
        return self.orbit_dir

    def _set_spacing_state(self, state, dist):
        prev = getattr(self, "spacing_state", None)
        if prev != state:
            if prev == "APPROACH" and state == "RETREAT":
                print(f"\n[Spacing] APPROACH -> RETREAT distance={dist:.2f}")
            elif prev == "RETREAT" and state == "ATTACK":
                print(f"\n[Spacing] RETREAT -> ATTACK distance={dist:.2f}")
            self.spacing_state = state

    def _area_clearance(self, x, z):
        """Approximate space left before the active area's movement boundary."""
        area = getattr(self, "area", None)
        if area is None:
            return AREA_LOOKAHEAD
        if getattr(area, "polygon", None):
            best = None
            points = area.polygon
            for a, b in zip(points, points[1:] + points[:1]):
                ax, az = a
                bx, bz = b
                dx, dz = bx - ax, bz - az
                size = dx * dx + dz * dz
                if size <= 1e-9:
                    distance = math.hypot(x - ax, z - az)
                else:
                    t = max(0.0, min(1.0, ((x - ax) * dx + (z - az) * dz) / size))
                    hx, hz = ax + dx * t, az + dz * t
                    distance = math.hypot(x - hx, z - hz)
                best = distance if best is None else min(best, distance)
            return best or 0.0
        if getattr(area, "circles", None):
            return max((radius - math.hypot(x - cx, z - cz)
                        for cx, cz, radius in area.circles), default=0.0)
        return AREA_LOOKAHEAD if area.safe(x, z) else 0.0

    def _retreat_spacing(self, px, pz, tx, tz, dist):
        """Pick a non-idle retreat heading that stays inside the active area."""
        ax, az = px - tx, pz - tz
        length = math.hypot(ax, az)
        if length < 1e-9:
            return None
        direct = stick_for(self.basis, ax, az)
        if not self.area:
            return direct
        ux, uz = ax / length, az / length
        best = None
        for degrees in (0, -30, 30, -60, 60, -90, 90):
            angle = math.radians(degrees)
            wx = ux * math.cos(angle) - uz * math.sin(angle)
            wz = ux * math.sin(angle) + uz * math.cos(angle)
            candidate = stick_for(self.basis, wx, wz)
            if not candidate:
                continue
            cdx, cdz = world_for(self.basis, *candidate)
            clen = math.hypot(cdx, cdz)
            if clen < 1e-9:
                continue
            endpoint = (px + cdx / clen * AREA_LOOKAHEAD,
                        pz + cdz / clen * AREA_LOOKAHEAD)
            if not self.area.guard_step((px, pz), endpoint)[0]:
                continue
            gain = math.hypot(endpoint[0] - tx, endpoint[1] - tz) - dist
            if gain <= 1e-6:
                continue
            clearance = self._area_clearance(*endpoint)
            score = gain * 20.0 + clearance * 0.5 - abs(degrees) * 0.02
            if best is None or score > best[0]:
                best = (score, candidate, endpoint)
        if best is None:
            hx, hz = self.area.home(px, pz)
            candidate = stick_for(self.basis, hx - px, hz - pz)
            if candidate:
                cdx, cdz = world_for(self.basis, *candidate)
                clen = math.hypot(cdx, cdz)
                if clen >= 1e-9:
                    endpoint = (px + cdx / clen * AREA_LOOKAHEAD,
                                pz + cdz / clen * AREA_LOOKAHEAD)
                    gain = math.hypot(endpoint[0] - tx, endpoint[1] - tz) - dist
                    if gain > 1e-6 and self.area.guard_step((px, pz), endpoint)[0]:
                        best = (gain * 20.0 + self._area_clearance(*endpoint) * 0.5,
                                candidate, endpoint)
        if best is None:
            return None
        _, selected, endpoint = best
        self.goal = endpoint
        return selected

    def _attack_spacing(self, now, px, pz, tx, tz, dist):
        """Keep moving around the target while attacks stay held."""
        ax, az = px - tx, pz - tz
        length = math.hypot(ax, az)
        if length < 1e-9:
            return None
        ux, uz = ax / length, az / length
        side = self._orbit_way(now, px, pz)
        tangent = (-uz * side, ux * side)
        # A small outward component turns a pure circle into kiting: the bot
        # keeps a useful gap if the monster follows instead of standing still at
        # the first attack distance.
        ways = ((tangent[0] + ux * 0.35, tangent[1] + uz * 0.35),
                (-tangent[0] + ux * 0.35, -tangent[1] + uz * 0.35),
                (ux, uz), tangent, (-tangent[0], -tangent[1]))
        best = None
        for order, (wx, wz) in enumerate(ways):
            candidate = stick_for(self.basis, wx, wz)
            if not candidate:
                continue
            cdx, cdz = world_for(self.basis, *candidate)
            clen = math.hypot(cdx, cdz)
            if clen < 1e-9:
                continue
            endpoint = (px + cdx / clen * AREA_LOOKAHEAD,
                        pz + cdz / clen * AREA_LOOKAHEAD)
            if self.area and not self.area.guard_step((px, pz), endpoint)[0]:
                continue
            end_dist = math.hypot(endpoint[0] - tx, endpoint[1] - tz)
            if end_dist < min_distance:
                continue
            clearance = self._area_clearance(*endpoint) if self.area else AREA_LOOKAHEAD
            score = -abs(end_dist - resume_distance) * 2.0 + clearance * 0.5 - order * 0.1
            if best is None or score > best[0]:
                best = (score, candidate, endpoint)
        if best is None:
            return self._retreat_spacing(px, pz, tx, tz, dist)
        _, selected, endpoint = best
        self.goal = endpoint
        return selected

    def known_players(self):
        with self.lock:
            return [u for k, u, *_ in self.units if k == "player"]

    def _find_owner(self, mem, units):
        """Resolve our player from several live NetworkBehaviour seeds."""
        for _, unit, *_ in units[:64]:
            owner = self.ms.local_player(mem, unit)
            if owner:
                return owner
        return None

    def _drop_stale_hot(self, found):
        """Drop the unit region cache once narrowed sweeps stop finding units.

        A narrowed sweep that keeps coming back empty is sweeping stale
        regions, not an empty world: the player name still reads (owner is a
        persistent pointer) and loot still lands (its own cache), so the units
        are in memory, just not in these regions. The movement re-narrow never
        fires in a small farming pen, so without this the dashboard reads
        '0 monsters, 0 players, 0 pets' until HOT_SELF_HEAL_S runs. A few
        empty passes is enough to know; the next sweep is then a full pass
        that rebuilds the cache where the units actually are.
        """
        if not found:
            self.hot_empty_streak += 1
            if self.hot_empty_streak >= HOT_EMPTY_STREAKS:
                with self.lock:
                    self.hot = self.hot_at = None
                self.hot_empty_streak = 0
                return True
        else:
            self.hot_empty_streak = 0
        return False

    def _renarrow_if_stale(self, px, pz):
        """Drop a region cache once the character has walked past the radius
        from where it was built.

        The caches narrow each sweep to the heap regions that held units/drops
        at build time, and were only rebuilt on a relog. New monsters and drops
        spawn in fresh regions, so a bot that walks away kept sweeping the old
        regions and saw only the pooled corpses left behind there -- the "5
        monster objects, 0 loot" state. Dropping the cache makes the next
        sweep a full pass that rebuilds it where the character is now. The
        anchors are read with getattr so a stub that never set them simply
        never re-narrows (the safe default).
        """
        r2 = HOT_RENARROW_RADIUS ** 2

        def far(anchor):
            if anchor is None:
                return False
            ax, az = anchor
            return (px - ax) ** 2 + (pz - az) ** 2 > r2

        with self.lock:
            if getattr(self, "hot", None) is not None and far(getattr(self, "hot_at", None)):
                self.hot = None
                self.hot_at = None
            if getattr(self, "hot_loot", None) is not None and far(getattr(self, "hot_loot_at", None)):
                self.hot_loot = None
                self.hot_loot_at = None

    def target(self, now):
        """(sx, sy, distance) toward the nearest monster, or (None, None, None).

        Positions come fresh every call; only the membership list is cached, so a
        monster that walks is chased where it is now, not where it was.
        """
        # Cleared here and set again only if a stick comes out of this call, so
        # the slow sensor is never judging progress toward a goal we gave up on.
        self.goal = None
        if not (self.me and self.basis):
            # Say so. Leaving the old mode standing made a bot with no unit at
            # all report whatever it was doing when it still had one, which read
            # as "chasing, but motionless" and sent the hunt to the wrong place.
            self.mode = "no unit"
            return None, None, None
        with self.lock:
            cached = [e for e in self.units if e[0] == "monster"]
        # Only monsters and ourselves: reading every player and pet position
        # each frame was a quarter of the work for something never targeted.
        live = self._live_positions([u for _, u, *_ in cached] + [self.me])
        here = live.get(self.me)
        if not here:
            self.misses += 1
            if self.misses < MEM_LOST_FRAMES:
                # One empty read is not a death. Do not actuate, but keep the
                # calibration until the configured run of misses is complete.
                self.mode = "lost"
                return None, None, None
        if not here:
            # Our unit was rebuilt -- map change, death or relog. Everything
            # derived from it is now meaningless: the basis was measured for a
            # unit that no longer exists. Dropping `hot` forces the next sweep
            # to search
            # the whole heap, because the new objects need not be where the old
            # ones were.
            self.me = self.basis = self.approach = None
            self.orbit_mark = None
            self.spacing_state = None
            # The owner is a pointer to the object that was just rebuilt, so it
            # is as dead as the rest. It is also what the scanner checks before
            # looking us up again, so leaving it set meant we never recovered.
            # Invalidate any narrowed sweep already in flight. Without a new
            # generation, that old sweep could publish its old-region results
            # after `hot` was cleared, narrow the scanner right back to them,
            # and leave memory recovery on pixels forever.
            with self.lock:
                self.generation += 1
                self.owner = self.hot = None
                self.hot_at = None
                self.units = []
            # Same reason as `hot`: the drops that survive a relog need not be
            # in the regions the old ones were, and a narrowed sweep that finds
            # nothing keeps finding nothing.
            self.hot_loot = None
            self.hot_loot_at = None
            with self.lock:
                self.loot = {}
            self.loot_target = self.loot_since = None
            self.chasing = self.engaged_since = None
            self.chasing_id = None
            self.ignored = {}
            self.ignored_ptr_ids = {}
            self.ignored_ids = {}
            self.seen_at, self.fight_ok = {}, {}
            # The route and the travel history belong to a unit that is gone;
            # the map and the area both describe the world and stay.
            self.path = self.last_pos = None
            self.returning, self.home_goal, self.wander = False, None, None
            if self.walk:
                self.walk.forget_walk()
            self.mode = "no unit"
            return None, None, None
        self.misses = 0
        px, _, pz = here
        self.last_pos = (px, pz)
        wedge_anchor = getattr(self, "wedge_anchor", None)
        if wedge_anchor and self.escapes:
            # The counter is per-location: it counts unwedges at the same
            # spot, not across the session. Clear it once the character is
            # this far from where it first wedged, so a wall that is actually
            # left behind stops counting against the next fight.
            ax, az = wedge_anchor
            if math.hypot(px - ax, pz - az) >= WALK_WEDGE_RESET:
                self.escapes, self.wedge_anchor = 0, None
        # The character has walked on; the region caches were built somewhere
        # else. Drop them past the radius so the next sweep is a full pass that
        # re-narrows here, instead of sweeping the old regions forever.
        self._renarrow_if_stale(px, pz)
        if self.escape and now < self.escape_until:
            # Backing out of a wedge. This overrides the target entirely: while
            # the character cannot move, nothing else it decides matters.
            self.mode = "unwedge"
            return self.escape[0], self.escape[1], 0.0
        self.escape = None
        # Confinement. Sits below the unwedge override on purpose: while the
        # character physically cannot move, nothing it decides matters -- and
        # that escape is also what stops a walk-back leaning on stone, which
        # is what the deleted leash did for minutes at a time.
        if self.area is not None:
            # Two different tests, not one with a margin. Leaving is "not
            # painted"; coming back is "painted with a whole cell to spare".
            # Evaluated before the walk-back acts, or the flag clears a frame
            # late and the bot takes one extra outward step every re-entry.
            if self.returning:
                if (self.home_goal is not None and self.area.safe(px, pz)
                        and math.hypot(px - self.home_goal[0],
                                       pz - self.home_goal[1]) <= MEM_ARRIVE):
                    self.returning, self.home_goal = False, None
            elif not self.area.safe(px, pz):
                self.returning, self.returning_since = True, now
                self.home_goal = self.area.home(px, pz)
                self.chasing = self.engaged_since = None
                self.chasing_id = None
                self.spacing_state = None
                self.target_name = ""
                print(f"\nzone: player outside safe interior; returning to "
                      f"{self.area.name!r}")
            if self.returning:
                out = self._go_home(now, px, pz)
                if out:
                    return out
        self.mode = "chasing"
        fresh = [(k, u, *live[u]) for k, u, *_ in cached if u in live]
        # Most of the list is pooled or dead objects that keep their position
        # and full health. Without this the bot parks in a pile of them,
        # swinging at each for MEM_ENGAGE_MAX_S in turn and never leaving --
        # and walking straight back if you drag the character away.
        ranked = sorted(((((x - px) ** 2 + (z - pz) ** 2) ** 0.5, u, x, y, z)
                         for k, u, x, y, z in fresh if k == "monster"
                         ),
                        key=lambda e: e[0])
        if self.area is not None:
            # Filtered *after* the sort so the held target can be looked up in
            # the full list below: searching only the filtered one meant a
            # monster stepping over the line vanished, `held` came back None,
            # and the bot took a different target -- bypassing TARGET_SWITCH,
            # which is the whole rule that stops it flapping between two.
            allowed = [e for e in ranked if self.area.inside(e[2], e[4])]
            rejected = len(ranked) - len(allowed)
            if rejected != getattr(self, "zone_rejected", None):
                if rejected:
                    print(f"\nzone: rejected {rejected} monster(s) outside "
                          f"{self.area.name!r}")
                self.zone_rejected = rejected
        else:
            allowed = ranked

        # Hold the current target rather than re-picking the nearest every
        # frame. Two monsters a similar distance away swap which is closer
        # constantly, and the bot answers by walking left, right, left, right
        # instead of going to either. It only switches for something clearly
        # nearer, or when this one is gone.
        held = next((e for e in ranked if e[1] == self.chasing
                     and (self.area is None
                          or self.area.inside(e[2], e[4]))), None)
        chasing_id = getattr(self, "chasing_id", None)
        if held and chasing_id is None:
            # Identity may have been unreadable on acquisition. Latch it as soon
            # as the same pointer produces a verified ID so later wrapper churn
            # does not reset the engagement clock.
            chasing_id = self._stable_id(held[1])
            if chasing_id is not None:
                self.chasing_id = chasing_id
        if held and chasing_id is not None:
            held_id = self._stable_id(held[1])
            if held_id is not None and held_id != chasing_id:
                # A positive mismatch proves this pooled address was reused.
                # None is only an unreadable identity this frame.
                held = None
        if held is None and chasing_id is not None:
            # The managed wrapper can move while FishNet's spawn identity stays.
            # This slow fallback runs only after the held pointer disappears.
            held = next((e for e in allowed if self._stable_id(e[1]) == chasing_id),
                        None)
            if held is not None:
                self.chasing = held[1]
        # Apply one hysteresis rule at every distance. The old split selected a
        # near held target before comparing candidates, but re-picked far targets
        # every frame -- simultaneously sticky and flappy depending on range.
        hit, dist = self._first_fightable(allowed, now)
        held_ok = (held and not self._target_ignored(held[1], now, chasing_id)
                   and self._fightable(held[1], now))
        if held_ok and (hit is None or hit[0] == held[1]
                        or dist >= held[0] * TARGET_SWITCH):
            hit, dist = (held[1], held[2], held[3], held[4]), held[0]
        if not hit:
            self.chasing = self.engaged_since = None
            self.chasing_id = None
            self.spacing_state = None
            cloaked = next((e for e in allowed
                            if e[0] <= MEM_RANGE and self._known_invisible(e[1])),
                           None)
            if cloaked:
                # Do not hand this frame to pixels: its red marker is the same
                # invisible monster memory just proved cannot be attacked.
                # A non-None zero stick is the explicit source veto; as soon as
                # the status flag clears, the short liveness TTL admits it.
                self.mode = "invisible"
                return 0.0, 0.0, cloaked[0]
            if self.area is not None:
                # Inside a fence "nothing left" is normal, not the end of the
                # map. Falling through to the pixel path here would be worse
                # than standing still: it knows nothing about the area and
                # would chase a red dot straight out of it.
                return self._wander(now, px, pz)
            self.mode = "no monster"        # nothing real left anywhere
            return None, None, None
        if dist > MEM_RANGE:
            # Nothing within melee reach. Walk to the nearest real monster
            # anywhere rather than stand: returning nothing here parks the bot,
            # because main() reads a zero stick as "handled" and never falls
            # through to the pixel path.
            self.mode = "far"
            # Fall through rather than return: a far target needs the same
            # engagement clock as a near one, or an unreachable monster across
            # a wall is walked at forever.
            # With an area set this is already confined: `ranked` only contains
            # admitted monsters, route_to() excludes unsafe cells, and the final
            # guard validates the issued segment.
        hit_id = self._stable_id(hit[0])
        if (hit[0] != self.chasing
                or (chasing_id is not None and hit_id is not None
                    and hit_id != chasing_id)):
            if TARGET_LOG:
                print(chr(10) + f"targetlog switch 0x{self.chasing or 0:X}"
                      f" -> 0x{hit[0]:X} at {dist:6.1f}"
                      f"  because {self._why_switched(now, ranked)}"
                      f"  candidates {len(allowed)}")
            # The escape counter is NOT reset here: it is per-location, and a
            # pack behind one wall cycles through its members. Resetting on
            # every switch is what let the bot unwedge and chase the same wall
            # forever. It clears on its own once we are far from the wedge.
            self.chasing, self.chasing_id, self.engaged_since = hit[0], hit_id, now
            report = getattr(self, "scan_summary", {})
            self.target_name = report.get("monster_names", {}).get(hit[0],
                                                                    "unknown")
        elif self.escapes >= WALK_ESCAPE_GIVEUP:
            # Backed out of the same spot this many times and still here.
            # Whatever is in the way, this whole cluster is not worth walking
            # at: ignoring only the chased member would have the next one in
            # the pack walked into the same wall a frame later.
            for k, u, x, y, z in fresh:
                if k == "monster" and math.hypot(x - hit[1], z - hit[3]) <= WALK_WEDGE_CLUSTER:
                    self._ignore_target(u, now + MEM_IGNORE_S)
            self.chasing = self.engaged_since = None
            self.chasing_id = None
            self.spacing_state = None
            self.escapes, self.wedge_anchor, self.mode = 0, None, "walled"
            return None, None, None
        elif stale_target(now, self.engaged_since):
            # Long enough on one target that it is not going to die: already
            # dead and still listed, unreachable, or not attackable. Parking on
            # it forever is the one failure that looks exactly like a hung bot.
            self._ignore_target(hit[0], now + MEM_IGNORE_S, hit_id)
            self.chasing = self.engaged_since = None
            self.chasing_id = None
            self.spacing_state = None
            self.mode = "gave up"
            return None, None, None
        if FIGHT_LOG and self.mem and dist <= MEM_RANGE:
            # Distance only: self.mode is still last frame's here, and the band
            # is what the distance says anyway.
            hp = self.ms.unit_health(self.mem, hit[0])
            print(f"\nfightlog {hit[0]:012X} dist {dist:5.2f} hp {hp}")
        if dist > MEM_ARRIVE:
            self._set_spacing_state("APPROACH", dist)
        boundary_standoff = False
        if (self.area is not None and self.area.inside(hit[1], hit[3])
                and not self.area.safe(hit[1], hit[3])):
            fight_x, fight_z = self.area.home(hit[1], hit[3])
            # Exact admission includes the boundary while player movement keeps
            # the inward safety margin. Once we have reached that nearest legal
            # fighting point, treat the target as joined instead of issuing the
            # same outward command until give-up. Orbit away-and-sideways so the
            # controller remains active and attack can continue without the final
            # fence having to rewrite every frame.
            boundary_standoff = (self.area.safe(px, pz)
                                  and math.hypot(px - fight_x, pz - fight_z)
                                  <= MEM_ARRIVE)
        if dist <= MEM_ARRIVE or boundary_standoff:
            # Arrived: hit-and-run owns only the left stick. Attack stays held by
            # the independent combat path while we stop or back straight out.
            self.mode = "on it"
            retreating = getattr(self, "spacing_state", None) == "RETREAT"
            if retreating:
                state = "RETREAT" if dist < resume_distance else "ATTACK"
            else:
                state = "RETREAT" if dist < min_distance else "ATTACK"
            self._set_spacing_state(state, dist)
            move = (self._attack_spacing(now, px, pz, hit[1], hit[3], dist)
                    if state == "ATTACK"
                    else self._retreat_spacing(px, pz, hit[1], hit[3], dist))
            if not move:
                return 0.0, 0.0, dist
            if self.goal is None:
                self.goal = (px + (px - hit[1]), pz + (pz - hit[3]))
            return move[0], move[1], dist
        gx, gz = self.route_to(now, px, pz, hit[1], hit[3])
        if self.sealed:
            # Walled off with no way round: walking at it is eight seconds of
            # pressing into stone before MEM_ENGAGE_MAX_S notices. There are
            # other monsters.
            self._ignore_target(hit[0], now + MEM_IGNORE_S, hit_id)
            self.chasing = self.engaged_since = None
            self.chasing_id = None
            self.spacing_state = None
            self.mode = "walled"
            return None, None, None
        s = stick_for(self.basis, gx - px, gz - pz)
        if not s:
            return None, None, None
        # What the slow sensor judges progress toward. On a route that is the
        # waypoint, not the monster: a detour round a big wall closes no
        # distance on the monster for seconds, and judging against it there
        # would write a false wall across the way round that is working.
        self.goal = (gx, gz)
        self.approach = s                # remembered for the back-off above
        return s[0], s[1], dist


class VirtualPad:
    """vgamepad backend."""

    def __init__(self):
        import vgamepad as vg
        self.pad = vg.VX360Gamepad()
        self.dpad = {"up": vg.XUSB_BUTTON.XUSB_GAMEPAD_DPAD_UP,
                     "down": vg.XUSB_BUTTON.XUSB_GAMEPAD_DPAD_DOWN,
                     "left": vg.XUSB_BUTTON.XUSB_GAMEPAD_DPAD_LEFT,
                     "right": vg.XUSB_BUTTON.XUSB_GAMEPAD_DPAD_RIGHT}
        self.face = {"a": vg.XUSB_BUTTON.XUSB_GAMEPAD_A,
                     "b": vg.XUSB_BUTTON.XUSB_GAMEPAD_B,
                     "x": vg.XUSB_BUTTON.XUSB_GAMEPAD_X,
                     "y": vg.XUSB_BUTTON.XUSB_GAMEPAD_Y}
        self.buttons = {
            **{f"dpad_{key}": value for key, value in self.dpad.items()},
            **self.face,
            "lb": vg.XUSB_BUTTON.XUSB_GAMEPAD_LEFT_SHOULDER,
            "rb": vg.XUSB_BUTTON.XUSB_GAMEPAD_RIGHT_SHOULDER,
        }
        self.attack_keys = ("lb", "rb")
        self.attack_btn = tuple(self.buttons[key] for key in self.attack_keys)
        self.attack_held = False
        self.held_triggers = set()

    def _tap(self, btn, hold):
        # The stick keeps its last value across this: left_joystick_float persists
        # between updates, so a tap never interrupts the chase.
        try:
            self.pad.press_button(btn)
            self.pad.update()
            time.sleep(hold)
        finally:
            self.pad.release_button(btn)
            self.pad.update()

    def tap_dpad(self, name, hold):
        if (getattr(self, "attack_held", False)
                and f"dpad_{name}" in self._attack_controls()):
            return
        self._tap(self.dpad[name], hold)

    def tap_button(self, name, hold=SPAM_HOLD_S):
        if (getattr(self, "attack_held", False)
                and name in self._attack_controls()):
            return
        self._tap(self.face[name], hold)

    def _button(self, key):
        if hasattr(self, "buttons"):
            return self.buttons.get(key)
        if isinstance(key, str) and key.startswith("dpad_"):
            return self.dpad[key.removeprefix("dpad_")]
        if isinstance(key, str) and key in self.face:
            return self.face[key]
        return key

    def _press_control(self, key):
        if key in ("lt", "rt"):
            trigger = self.pad.left_trigger if key == "lt" else self.pad.right_trigger
            trigger(value=255)
            self.held_triggers.add(key)
        else:
            self.pad.press_button(self._button(key))

    def _release_control(self, key):
        if key in ("lt", "rt"):
            trigger = self.pad.left_trigger if key == "lt" else self.pad.right_trigger
            trigger(value=0)
            self.held_triggers.discard(key)
        else:
            self.pad.release_button(self._button(key))

    def _attack_controls(self):
        return tuple(getattr(self, "attack_keys", getattr(self, "attack_btn", ())))

    def press_buff(self, key):
        self._press_control(key)
        self.pad.update()

    def release_buff(self, key):
        self._release_control(key)
        self.pad.update()

    def reassert_attack(self):
        for key in self._attack_controls():
            self._press_control(key)
        self.attack_held = True
        self.pad.update()

    def configure_attack(self, keys):
        keys = tuple(keys)
        was_held = bool(getattr(self, "attack_held", False))
        old_keys = self._attack_controls()
        for key in old_keys:
            if key in keys:
                continue
            self._release_control(key)
        self.attack_keys = keys
        if hasattr(self, "buttons"):
            self.attack_btn = tuple(self.buttons[key] for key in keys
                                    if key in self.buttons)
        if was_held:
            for key in keys:
                if key in old_keys:
                    continue
                self._press_control(key)
        self.attack_held = was_held
        self.pad.update()

    def tap_trigger(self, name, hold):
        # A trigger is an axis, not a button, so it cannot go through _tap. The
        # stick keeps its value across this the same way.
        if (getattr(self, "attack_held", False)
                and name in self._attack_controls()):
            return
        press = (self.pad.left_trigger if name == "lt"
                 else self.pad.right_trigger)
        press(value=255)
        self.pad.update()
        time.sleep(hold)
        press(value=0)
        self.pad.update()

    def stick(self, sx, sy, attack=False):
        if sx == 0.0 and sy == 0.0 and not attack:
            self.release_all()
            return
        self.pad.left_joystick_float(sx, sy)
        if attack:
            for key in self._attack_controls():
                self._press_control(key)
        else:
            for key in self._attack_controls():
                self._release_control(key)
        self.attack_held = bool(attack)
        self.pad.update()  # one report per frame

    def release_all(self):
        self.pad.reset()
        self.pad.update()
        self.attack_held = False
        if hasattr(self, "held_triggers"):
            self.held_triggers.clear()

    def close(self):
        self.release_all()


class ArduinoPad:
    """arduino_joystick_leonardo_v1.ino over serial. 'L<x>,<y>' -> 'OK'."""

    # ATmega32U4 boards: Arduino LLC, Arduino SA, SparkFun Pro Micro.
    VIDS = (0x2341, 0x2A03, 0x1B4F)

    @staticmethod
    def autodetect():
        from serial.tools import list_ports
        ports = list(list_ports.comports())
        for p in ports:
            if p.vid in ArduinoPad.VIDS:
                return p.device
        listing = "\n  ".join(f"{p.device}  {p.description}" for p in ports) or "(none)"
        raise RuntimeError(f"no Arduino found. ports present:\n  {listing}")

    def __init__(self, port=None, baud=115200):
        import serial
        if port in (None, "auto"):
            port = self.autodetect()
            print(f"auto-detected {port}")
        try:
            self.ser = serial.Serial(port, baud, timeout=0.5)
        except serial.SerialException as e:
            from serial.tools import list_ports
            have = ", ".join(p.device for p in list_ports.comports()) or "(none)"
            raise RuntimeError(f"{port} not usable ({e}). ports present: {have}") from None
        # Opening at 115200 does not reset a Leonardo, so waiting for its READY
        # banner just burns the whole timeout on an already-booted board. Ping
        # instead, and keep pinging: a board that IS mid-reset drops the first one.
        deadline = time.time() + 6
        while time.time() < deadline:
            self.ser.write(b"P\n")
            if self.ser.readline().strip() in (b"PONG", b"READY"):
                break
        else:
            raise RuntimeError(f"{port} opened but no PONG -- "
                               f"wrong port, or sketch not flashed")
        self.ser.reset_input_buffer()  # drop READY/PONG backlog before commands
        self.ser.timeout = 2
        self.last = None
        self.attack_keys = ("lb", "rb")
        self.attack_held = False
        self.held_triggers = set()

    ATTACK_BTNS = (4, 5)  # LB and RB in the usual XInput button order
    HAT = {"up": 0, "right": 2, "down": 4, "left": 6}  # sketch: 0..7 clockwise from N
    FACE = {"a": 0, "b": 1, "x": 2, "y": 3}  # usual XInput button order
    BUTTONS = {**FACE, "lb": 4, "rb": 5}

    def tap_dpad(self, name, hold):
        if (getattr(self, "attack_held", False)
                and f"dpad_{name}" in getattr(self, "attack_keys", ())):
            return
        try:
            self._cmd(f"V{self.HAT[name]}")
            time.sleep(hold)
        finally:
            self._cmd("V-1")  # -1 centres the hat

    def tap_button(self, n, hold=None):
        if (isinstance(n, str) and getattr(self, "attack_held", False)
                and n in getattr(self, "attack_keys", ())):
            return
        button = self.FACE.get(n, n)
        if hold is None:
            self._cmd(f"B{button}")
            return
        try:
            self._cmd(f"D{button}")
            time.sleep(hold)
        finally:
            self._cmd(f"U{button}")

    def press_buff(self, key):
        self._press_control(key)

    def release_buff(self, key):
        self._release_control(key)

    def _send_triggers(self):
        held = getattr(self, "held_triggers", set())
        self._cmd(f"T{255 if 'lt' in held else 0},{255 if 'rt' in held else 0}")

    def _press_control(self, key):
        if key.startswith("dpad_"):
            self._cmd(f"V{self.HAT[key.removeprefix('dpad_')]}")
        elif key in ("lt", "rt"):
            self.held_triggers.add(key)
            self._send_triggers()
        else:
            self._cmd(f"D{self.BUTTONS[key]}")

    def _release_control(self, key):
        if key.startswith("dpad_"):
            self._cmd("V-1")
        elif key in ("lt", "rt"):
            self.held_triggers.discard(key)
            self._send_triggers()
        else:
            self._cmd(f"U{self.BUTTONS[key]}")

    def reassert_attack(self):
        for key in getattr(self, "attack_keys", ("lb", "rb")):
            self._press_control(key)

    def configure_attack(self, keys):
        keys = tuple(keys)
        was_held = bool(getattr(self, "attack_held", False))
        old_keys = tuple(getattr(self, "attack_keys", ("lb", "rb")))
        released_hat = False
        for key in old_keys:
            if key in keys:
                continue
            self._release_control(key)
            released_hat = released_hat or key.startswith("dpad_")
        self.attack_keys = keys
        if was_held:
            for key in keys:
                if (key not in old_keys
                        or (released_hat and key.startswith("dpad_"))):
                    self._press_control(key)
        self.attack_held = was_held

    def tap_trigger(self, name, hold):
        # 'T<left>,<right>', the sketch's trigger axes. ponytail: untested on
        # the board -- the game only reads XInput, so vgamepad is the live path.
        if (getattr(self, "attack_held", False)
                and name in getattr(self, "attack_keys", ())):
            return
        try:
            self.held_triggers.add(name)
            self._send_triggers()
            time.sleep(hold)
        finally:
            self.held_triggers.discard(name)
            self._send_triggers()

    def _cmd(self, line):
        self.ser.write(f"{line}\n".encode())
        reply = self.ser.readline().strip()
        if reply != b"OK":
            print(f"\nboard replied {reply!r} to {line}")

    def stick(self, sx, sy, attack=False):
        # HID Y axis is down-positive, our sy is up-positive.
        x, y = int(sx * 32767), int(-sy * 32767)
        if (x, y, attack) == (0, 0, False):
            if self.last != (0, 0, False):
                self.release_all()
            return
        if (x, y, attack) == self.last:
            return  # ponytail: sketch is synchronous, skip no-op round trips
        if self.last is None or (x, y) != self.last[:2]:
            self._cmd(f"L{x},{y}")
        if self.last is None or attack != self.last[2]:
            for key in getattr(self, "attack_keys", ("lb", "rb")):
                (self._press_control if attack else self._release_control)(key)
        self.attack_held = bool(attack)
        self.last = (x, y, attack)

    def release_all(self):
        self._cmd("Z")
        self.last = (0, 0, False)
        self.attack_held = False
        if hasattr(self, "held_triggers"):
            self.held_triggers.clear()

    def close(self):
        try:
            self.release_all()
        finally:
            self.ser.close()


def targeting_mode(argv, area=None):
    """Which target source this run uses: "memory" or "minimap".

    Memory is the primary because it knows what a thing IS, so pets and other
    players stop being targets. Pixels remain its startup/recovery fallback.
    `--minimap` is the explicit opt-out for a run that must never use memory.

    `--area` only exists on the memory path (a recorded area is world
    coordinates, which the screen cannot give), so asking for one asks for
    memory. An explicit `--minimap` still wins: saying so out loud should never
    be silently overridden.
    """
    if not MEMORY_TARGETING or "--minimap" in argv:
        return "minimap"
    return "memory"


def should_calibrate(eyes, now, next_cal):
    """Whether memory has enough current evidence to attempt its movement basis."""
    return bool(eyes is not None and eyes.me is None and now >= next_cal
                and (eyes.owner is not None or eyes.known_players()))


def main(port=None, area=None):
    import mss

    mode = targeting_mode(sys.argv, area)
    if area and mode != "memory":
        # A recorded area is world coordinates; the screen cannot say where in
        # the world anything is, so there is nothing for the fence to test.
        print(f"--minimap was asked for, so --area {area!r} is IGNORED:"
              f" areas only exist on the memory path")
        area = None
    zone = None
    if area:
        zone = Area(area).load()
        if not zone.defined:
            # Never fall back to roaming: an unconfined run started by a typo
            # is a whole session farming the wrong side of the map.
            known = ", ".join(Area.names()) or "(none recorded yet)"
            print(f"no area named {area!r} in {AREA_FILE}")
            print(f"recorded areas: {known}")
            print(f"record one with:  python minimap_bot.py --record {area}")
            return
        if not zone.runtime_supported:
            print(f"area {area!r} uses unsupported {zone.axes.upper()} axes; "
                  "SpiritVale movement is X/Z, so confinement is OFF")
            return
        (x0, z0), (x1, z1) = zone.bounds()
        print(f"confined to area {area!r}: {zone.describe()},"
              f" x {x0:.0f}..{x1:.0f}  z {z0:.0f}..{z1:.0f}")

    win = find_window()
    pad = ArduinoPad(port) if port else VirtualPad()
    print(f"window {win.width}x{win.height} @ ({win.left},{win.top})"
          f" via {type(pad).__name__} -- End to start/stop, ctrl+c to exit")

    last = None  # (t, dist, sx, sy) of last seen dot
    had_unit = False   # so the 'unit rebuilt' notice prints once
    next_cal = 0.0     # earliest retry after a failed calibration
    eyes = None
    print(f"targeting: {mode}"
          + ("  (--memory for the unit list)" if mode == "minimap"
             else "  (--minimap for red dots only)"))
    if MEMORY_TARGETING and mode == "memory":
        try:
            eyes = MemoryEyes(zone)
            if not eyes.available():
                # Do NOT drop eyes here. This is precisely the case heal() was
                # written for, and it runs on the scanner thread -- tearing the
                # object down made that recovery unreachable and left the bot on
                # pixels (chasing pets) for the whole session.
                print("MEMORY TARGETING STALE: the class pointers did not"
                      " resolve, which means the game was patched."
                      "\n  Searching for the classes by name once the bot"
                      " starts (2-4 minutes, in the background)."
                      "\n  Until it lands: pixels, which means it will chase"
                      " pets.")
        except Exception as e:                  # game closed, no rights, no dump
            print(f"memory targeting unavailable ({e}); reading pixels instead")
            eyes = None
    pet_filter = PetFilter()
    target_lock = TargetLock()
    target_blacklist = TargetBlacklist()
    stuck_watchdog = StuckWatchdog()
    paused = START_PAUSED
    buffs = BuffScheduler()
    next_spam = 0.0   # SPAM_BUTTON goes out on its own timer
    next_loot = 0.0   # LOOT_BUTTON while standing on a drop
    next_login_check = 0.0  # a whole-window grab, so kept to RECONNECT_POLL_S
    reconnecting = RECONNECT   # switched off if a screen refuses to advance
    reconnect_flow = ReconnectFlow()
    dashboard = TerminalDashboard(mode)

    pad.stick(0.0, 0.0, False)
    dashboard.update(eyes, False, "press End to start", force=True)

    with mss.mss() as sct:
        try:
            while True:
                control_config = controller_config_request()
                if control_config is not None:
                    try:
                        apply_controller_config(
                            buffs, pad, control_config, time.time())
                        print("\n[Config] live buff and attack settings applied")
                    except ValueError as exc:
                        print(f"\n[Config] input settings rejected: {exc}")
                request = automation_state_request()
                if request == "pause":
                    if reconnect_flow.cancel():
                        print("\n[Reconnect] Pending retry cancelled: automation stopped")
                elif request == "wait" and not paused:
                    pad.stick(0.0, 0.0, False)
                    buffs.reset(time.time())
                    paused = True
                    print("\nWAITING: memory scan delayed")
                elif request == "running" and paused:
                    pad.stick(0.0, 0.0, False)
                    if zone is None:
                        wake_controller(pad)
                    buffs.reset(time.time())
                    paused = False
                    print("\nRUNNING: fresh player read received")
                if toggle_key_hit():
                    paused = toggle_running(paused, pad, pet_filter, area=zone)
                    if paused and reconnect_flow.cancel():
                        print("\n[Reconnect] Pending retry cancelled: automation stopped")
                    target_lock.reset()
                    target_blacklist.reset()
                    stuck_watchdog.reset()
                    last = None
                    buffs.reset(time.time())
                    next_spam = next_loot = 0.0
                    if eyes is not None:
                        # Same reason the pixel helpers reset here: a drop held
                        # from before the pause is stale by the time we resume.
                        eyes.loot_target = eyes.loot_since = None
                        eyes.loot_mode = "no loot"
                        # The route is stale for the same reason; the walls it
                        # avoided are not, so the map itself is left alone.
                        eyes.path = eyes.last_pos = None
                        eyes.returning, eyes.home_goal = False, None
                        eyes.wander = None
                        eyes.walk.forget_walk()
                    print(f"\n{'STOPPED' if paused else 'STARTED'} (End)")
                    if (not paused and eyes is not None and
                            (eyes.scanner is None
                             or not eyes.scanner.is_alive())):
                        # Returns at once. The first sweep is slow, so the bot
                        # runs on pixels meanwhile and upgrades itself when the
                        # unit list arrives -- nothing waits on it.
                        eyes.start_scanning()
                        print("scanning for units in the background; "
                              "reading pixels until it lands")
                    # Left commented as it came from main -- the camera check is
                    # off there, and memory targeting does not need it: the basis
                    # is measured from real travel, so a rotated camera is
                    # already accounted for.
                    # if not paused and CAMERA_CHECK:
                    #     deg = camera_rotation(pad, sct, win)
                    #     if deg is None:
                    #         print("camera check: character never moved -- "
                    #               "blocked, or the pad is not reaching the game")
                    #     elif abs(deg) > CAMERA_MAX_DEG:
                    #         # Running on would look like a broken bot: it would
                    #         # steer off at an angle and circle every target.
                    #         print(f"camera is rotated {deg:+.0f} degrees -- "
                    #               f"relog to reset it to north, then press End")
                    #         pad.stick(0.0, 0.0, False)
                    #         paused = True
                    #     else:
                    #         print(f"camera check: {deg:+.0f} degrees, good")
                if paused:
                    if eyes is not None:
                        eyes.account_pursuit_time(time.time(), "paused")
                    if reconnect_flow.failed:
                        dashboard.update(
                            eyes, False, "reconnect retry limit reached",
                            memory_driving=False, status="RECONNECT OFF")
                    else:
                        dashboard.update(eyes, False, "press End to start")
                    time.sleep(0.05)
                    continue

                if (eyes is not None and
                        (eyes.scanner is None or not eyes.scanner.is_alive())):
                    # The sweep loop catches ordinary read errors itself. This
                    # is the last guard for a thread that nevertheless exited:
                    # memory remains the configured primary and is restarted
                    # without requiring a double-End from the user.
                    eyes.start_scanning()
                    print("\nmemory scanner stopped -- restarted; pixels until it lands")

                if reconnecting and time.time() >= next_login_check:
                    login_now = time.time()
                    next_login_check = login_now + RECONNECT_POLL_S
                    full = np.array(sct.grab(window_region(win)))[:, :, :3]
                    screen = login_screen(full)
                    player_valid = bool(
                        reconnect_flow.stage == "player" and
                        reconnect_player_valid(eyes))
                    action, events, reset_memory = reconnect_flow.observe(
                        screen, login_now, player_valid)
                    for event in events:
                        print(f"\n{event}")
                    if reset_memory:
                        # Gameplay is visible again, but the process kept stale
                        # heap addresses from the vanished session. Reset once,
                        # then wait for a fresh owner and current position before
                        # ordinary actions may resume.
                        had_unit = False
                        next_cal = 0.0
                        if eyes is not None:
                            eyes.reset_session()
                    if action is not None:
                        # Drop every controller channel before touching the mouse.
                        # ReconnectFlow debounces this physical action until the
                        # screen advances or this stage's retry timeout expires.
                        pad.stick(0.0, 0.0, False)
                        did = reconnect_step(full, win)
                        reconnect_flow.action_completed(now, time.time())
                        if did == "server":
                            print("\n[Reconnect] Connecting to Southeast Asia")
                        elif did == "server: SEA row not found":
                            print("\n[Reconnect] SEA row not found; waiting to retry")
                    if reconnect_flow.failed:
                        reconnecting = False
                        paused = True
                        pad.stick(0.0, 0.0, False)

                if reconnect_flow.failed:
                    dashboard.update(
                        eyes, False, "reconnect retry limit reached",
                        memory_driving=False, status="RECONNECT OFF", force=True)
                    if eyes is not None:
                        eyes.account_pursuit_time(time.time(), "reconnect")
                    time.sleep(0.05)
                    continue

                if reconnect_flow.active:
                    # Keep normal targeting, buffs and attacks paused throughout
                    # the episode, including frames between full-window polls.
                    pad.stick(0.0, 0.0, False)
                    target_lock.reset()
                    target_blacklist.reset()
                    stuck_watchdog.reset()
                    pet_filter.reset()
                    last = None
                    buffs.reset(time.time())
                    next_spam = next_loot = 0.0
                    reconnect_state = f"waiting for {reconnect_flow.stage}"
                    dashboard.update(
                        eyes, True, reconnect_state, memory_driving=False,
                        status="RECONNECTING", force=True)
                    if eyes is not None:
                        eyes.account_pursuit_time(time.time(), "reconnect")
                    time.sleep(0.05)
                    continue

                reg = minimap_region(win)
                img = np.array(sct.grab(reg))[:, :, :3]
                h, w = img.shape[:2]
                now = time.time()
                if eyes is not None:
                    eyes.account_pursuit_time(now)
                sx = sy = None
                on_loot = False
                display_distance = None
                memory_driving = False
                if should_calibrate(eyes, now, next_cal):
                    # The sweep has landed, so the two calibration pushes can
                    # happen now. Two seconds, once, and only after the bot has
                    # already been fighting on pixels rather than before it.
                    print("\nunit list ready -- calibrating (2s)")

                    if eyes.calibrate(pad):
                        had_unit = True
                        print(f"  locked on 0x{eyes.me:012X}; targeting monsters "
                              f"by what they are, not how they look")
                    else:
                        # Usually the character was blocked or stunned and the
                        # pushes moved nothing. Retry later rather than giving up
                        # on memory targeting for the rest of the session.
                        next_cal = now + CAL_RETRY_S
                        print(f"  no unit moved when pushed -- retrying in "
                              f"{CAL_RETRY_S:g}s, pixels until then")

                if eyes is not None:
                    # The unit list knows what each thing IS, so none of the
                    # pixel machinery is needed here: no pet filter, no
                    # saturation threshold for mushrooms, no minimap rotation.
                    msx, msy, mdist = eyes.target(now)
                    # Loot gets its turn when there is no monster to fight, and
                    # also when the only monster is "far": an item two steps
                    # away is worth more than a walk across the map, and the
                    # monster is still there afterwards. Never mid-fight.
                    if (LOOT_PICKUP
                            and eyes.mode not in ("on it", "unwedge", "going back")):
                        lsx, lsy, ldist = eyes.pick_loot(now)
                        if lsx is not None and loot_wins(eyes.mode, mdist
                                                         if msx is not None
                                                         else None, ldist):
                            msx, msy, mdist = lsx, lsy, ldist
                            on_loot = True
                        elif eyes.loot_since:
                            # It lost this frame, so it is not being walked to
                            # and LOOT_MAX_S must not run against it.
                            eyes.loot_since = now
                        if LOOT_LOG:
                            if lsx is not None:
                                print(f"\nlootlog {eyes.loot_name[:14]:14} "
                                      f"dist {ldist:6.1f} mode "
                                      f"{eyes.loot_mode:9} monster "
                                      f"{eyes.mode:8} "
                                      f"{'TAKE' if on_loot else 'lost'}")
                            else:
                                # Nothing was even offered. Which of the three
                                # reasons it was is the whole question, and only
                                # the cache can answer it.
                                print(f"\nlootlog nothing offered ({eyes.loot_mode})"
                                      f" -- {eyes.loot_debug(now)}")
                    if eyes.me is None:
                        # Do not shut memory targeting down for this: it comes
                        # back on its own once the sweep finds the new unit, and
                        # closing it meant one map change dropped the bot onto
                        # pixels for the rest of the session.
                        if had_unit:
                            print("\nour unit was rebuilt (map change, death or "
                                  "relog) -- rediscovering (~15s), pixels "
                                  "until then")
                            had_unit = False
                    elif msx is None:
                        # A zero stick reads as "handled" below, so the pixel
                        # path never runs. Right for modes that genuinely mean
                        # stop; wrong for "no monster" and "no unit", which are
                        # temporary memory gaps. Leaving sx None for those lets
                        # pixels drive until the scan/calibration lands. A
                        # farming area still fails closed in area_holds() below.
                        if hold_still(eyes.mode):
                            sx = sy = 0.0
                        # Name which kind of nothing this is: "no monster" was
                        # printed for a lost unit too, hiding a dead bot behind
                        # a message that reads like a quiet patch of map.
                        state = {"no unit": "no unit  ",
                                 "lost": "lost     ",
                                 "walled": "walled   ",
                                 "gave up": "gave up  ",
                                 "no area": "NO AREA  "}.get(eyes.mode,
                                                             "no monster")
                    else:
                        sx, sy = msx, msy
                        if on_loot:
                            got = "get" if eyes.loot_mode == "loot get" else ""
                            state = f"{eyes.loot_name[:9]:9}{got:3}{mdist:5.1f}"
                        else:
                            # "~" says the bot is walking round a known wall
                            # rather than at the monster, which is the only
                            # thing routing looks like from the outside.
                            state = {"on it": "on it  ",
                                     "unwedge": "unwedge",
                                     "going back": "back in",
                                     "wander": "wander ",
                                     "far": "far    "}.get(eyes.mode,
                                                           "dist  ") + (
                                f"{mdist:6.1f}" if not eyes.routing
                                else f"{mdist:5.1f}~")
                    if sx is not None:
                        display_distance = mdist
                        memory_driving = True

                # Everything below is the pixel path, used when memory targeting
                # is off or has gone stale. It is left exactly as it was.
                if sx is None and area_holds(eyes, zone):
                    # A fence is active, so pixels must not steer: they would
                    # chase a red dot anywhere, and a walk far enough out makes
                    # target() abandon the area for the whole run. Hold still
                    # until the unit list (and its confinement) has a stick.
                    # Clear the pixel state too: coasting on a last heading is
                    # pixels steering, and `dot` must be bound for the
                    # pixel_evidence read below.
                    # Pixels are evidence that a narrowed memory scan missed
                    # targets, but never an actuator while confinement is active.
                    (_, _), _, dot = pick_target(
                        img, pet_filter, target_lock, target_blacklist, now)
                    sx = sy = 0.0
                    stuck = None
                    last = None
                    state = "in area"
                elif sx is None:
                    (cx, cy), _, dot = pick_target(
                        img, pet_filter, target_lock, target_blacklist, now)
                    stuck = (dot is not None and stuck_watchdog.update(
                        target_lock.target_id, dot, cx, cy, now))
                else:
                    dot = stuck = None

                if sx is not None:
                    pass                    # the unit list already decided
                elif stuck:
                    target_blacklist.block(dot, now)
                    target_lock.reset()
                    stuck_watchdog.reset()
                    sx = sy = 0.0
                    last = None
                    state = "stuck skip"
                    print(f"\nstuck target: ignoring for {TARGET_IGNORE_S:g}s")
                elif dot is not None:
                    dx, dy = dot[0] - cx, dot[1] - cy
                    dist = (dx * dx + dy * dy) ** 0.5
                    display_distance = dist
                    sx, sy = stick_vector(dx, dy)
                    last = (now, dist, sx, sy)
                    if dist < DEADZONE_PX:
                        sx = sy = 0.0
                        state = "centered"
                    else:
                        state = f"dist {dist:6.1f}"
                elif target_lock.concealed:
                    # The locked dot vanished near centre: stay put and attack it.
                    sx = sy = 0.0
                    state = "concealed"
                elif last and now - last[0] < LOST_HOLD_S:
                    # brief flicker/occlusion mid-chase: coast on last heading
                    _, _, sx, sy = last
                    state = "coasting"
                else:
                    sx = sy = 0.0
                    last = None
                    state = "no monster"

                pixel_evidence = bool(
                    not memory_driving and
                    (dot is not None or target_lock.current is not None
                     or last is not None))
                if (eyes is not None and
                        eyes.note_pixel_fallback(now, pixel_evidence)):
                    state = "memory rescan"
                    print("\nmemory and pixels disagree -- full memory rescan "
                          "started; pixels remain temporary fallback")

                boundary_blocked = False
                if eyes is not None:
                    # Last gate before controller output. Target, loot, routing,
                    # orbit and unwedge all share it, so no movement path can
                    # accidentally bypass the farming-zone safety margin.
                    sx, sy, boundary_blocked = eyes.guard_area_step(sx, sy, now)

                atk = attack_active(
                    now, blocked=bool(eyes is not None and memory_driving
                                 and eyes.mode in ("invisible", "going back",
                                                   "boundary", "no area"))
                    or (boundary_blocked
                        and not (eyes is not None and memory_driving
                                 and eyes.spacing_state in ("ATTACK", "RETREAT"))))
                if eyes is not None:
                    if boundary_blocked:
                        eyes.movement_owner = "boundary"
                    elif on_loot and memory_driving:
                        eyes.movement_owner = "loot"
                    elif (memory_driving
                          and eyes.mode in ("chasing", "far", "on it")):
                        eyes.movement_owner = "monster"
                    else:
                        eyes.movement_owner = eyes.mode if memory_driving else "idle"
                    # The stick that actually goes out is the one the walk map
                    # can learn a wall from -- loot may have won the arbitration
                    # above, and target()'s own vector was never sent.
                    eyes.observe_move(now, sx, sy, on_loot)
                # End a bounded press immediately before the normal controller
                # report, so attack is reasserted in this same tick if the cast
                # animation interrupted it.
                complete_buff_tick(buffs, pad, time.time(), atk)
                pad.stick(sx, sy, atk)

                key = ""
                danger_close = bool(atk and (
                    (eyes is not None and memory_driving and eyes.mode == "on it")
                    or (not memory_driving and
                        (state in ("concealed", "centered")
                         or (display_distance is not None
                             and display_distance <= STUCK_MIN_DIST_PX)))))
                cast = buffs.cast_due(now, pad, atk, danger_close)
                if cast:
                    key = cast
                elif (eyes is not None and now >= next_loot
                        and buffs.active is None
                        and (eyes.loot_mode == "loot get"
                             or eyes.loot_here())):
                    # Something is under us: LOOT_BUTTON collects it. Checked
                    # even mid-fight, because the kill drops the item at our
                    # feet and the monster path would otherwise walk off it
                    # first. Same rule as the buff and the spam button -- one
                    # press per pass, or the game drops one inside the other's
                    # animation.
                    pad.tap_trigger(LOOT_BUTTON, LOOT_HOLD_S)
                    key = LOOT_BUTTON
                    next_loot = now + LOOT_TAP_GAP_S
                elif (SPAM_BUTTON and now >= next_spam
                      and buffs.active is None):
                    # Never in the same pass as a buff press: two taps back to back
                    # land inside one another's animation and the game drops one.
                    pad.tap_button(SPAM_BUTTON, SPAM_HOLD_S)
                    key = SPAM_BUTTON
                    next_spam = now + SPAM_PERIOD_S

                dashboard.update(eyes, True, state, sx, sy, atk, key, on_loot,
                                 display_distance, memory_driving)
                time.sleep(1 / LOOP_HZ)
        except KeyboardInterrupt:
            print("\nstopped")
        finally:
            pad.close()
            print()


def demo():
    """Self-check: synthetic minimap, no game or gamepad needed."""
    global LOOT_NAMES, MEMORY_TARGETING
    import inspect
    import tempfile

    assert SPAM_BUTTON is None or SPAM_BUTTON in ArduinoPad.FACE, SPAM_BUTTON
    assert "cv2.imwrite" not in inspect.getsource(main), (
        "automatic reconnect must not save screenshots")
    assert 'status="RECONNECTING"' in inspect.getsource(main), (
        "reconnect must replace stale dashboard controls before continuing")
    reconnect_source = inspect.getsource(main)
    action_gate = reconnect_source[reconnect_source.index("if action is not None:"):
                                   reconnect_source.index(
                                       "if reconnect_flow.failed:",
                                       reconnect_source.index("if action is not None:"))]
    assert action_gate.index("pad.stick(0.0, 0.0, False)") < \
           action_gate.index("reconnect_step(full, win)"), (
               "controller input must be released before every reconnect click")
    assert reconnect_source.count("reconnect_flow = ReconnectFlow()") == 1
    paused_gate = reconnect_source[reconnect_source.index("if paused:"):
                                      reconnect_source.index(
                                          "if (eyes is not None and",
                                          reconnect_source.index("if paused:"))]
    assert "reconnect_flow.failed" in paused_gate and "RECONNECT OFF" in paused_gate, (
        "retry exhaustion must remain visible instead of advertising End recovery")
    assert "did = reconnect_step(full, win)" in reconnect_source
    assert "SEA row not found; waiting to retry" in reconnect_source
    assert 'if request == "pause":' in reconnect_source
    assert reconnect_source.count("reconnect_flow.cancel()") == 2, (
        "manual End/UI stop must cancel retries without treating scanner wait as a stop")
    assert "target_lock.current is not None" in inspect.getsource(main)
    assert "target_lock.target_id is not None" not in inspect.getsource(main), (
        "a historical target id is not current pixel evidence")

    dashboard = dashboard_text({
        "running": True, "bot_mode": "memory", "source": "memory",
        "state": "loot",
        "stick": (0.25, -1.0), "attack": False, "action": "lt",
        "memory": {"scanner": "running", "classes": "units+loot",
                   "calibrated": True, "counts": "341M 8P 3pets",
                   "player": "Lepica", "players": ("Lepica [YOU]", "unknown@D800"),
                   "pets": ("Bat [YOURS]",),
                   "monsters": ("Sun Lion x2", "Ember Wraith x1")},
        "target": "LOOT Gold Ore at 8.4",
        "loot": {"detected": 55, "wanted": 39,
                 "ground": ("Gold Ore x39 [WANTED]", "Solar Spear x2 [filtered]")},
        "navigation": "loot / direct", "warning": "",
    }, color=False)
    for expected in ("SPIRITVALE COMBAT BOT", "RUNNING", "Primary mode",
                     "MEMORY", "COMBAT & CONTROL", "MEMORY SCANNER",
                     "Lepica [YOU]", "Bat [YOURS]",
                     "Sun Lion x2", "Gold Ore x39 [WANTED]", "39 wanted"):
        assert expected in dashboard, (expected, dashboard)

    class SummaryMem:
        def read(self, addr, size):
            return b"\x01" if addr in (0x1000 + 7, 0x2000 + 7, 0x3000 + 7) else bytes(size)

    class SummaryMS:
        UNIT_VISIBLE = 7

        @staticmethod
        def my_pets(mem, owner):
            return {0x3000}

        @staticmethod
        def player_name(mem, unit):
            return {0x1000: "Lepica", 0x2000: "Rin"}.get(unit)

        @staticmethod
        def monster_id(mem, unit):
            return {0x3000: "Bat", 0x4000: "Wolf",
                    0x5000: "Slime", 0x6000: "Slime"}.get(unit)

        @staticmethod
        def real_monster(mem, unit):
            return unit in (0x5000, 0x6000)

    summary = memory_scan_summary(SummaryMS, SummaryMem(), [
        ("player", 0x1000, 0.0, 0.0, 0.0),
        ("player", 0x2000, 2.0, 0.0, 0.0),
        ("pet", 0x3000, 1.0, 0.0, 0.0),
        ("pet", 0x4000, 3.0, 0.0, 0.0),
        ("monster", 0x5000, 3.0, 0.0, 4.0),
        ("monster", 0x6000, 6.0, 0.0, 8.0),
        ("monster", 0x7000, 1.0, 0.0, 1.0),
    ], 0x1000)
    assert summary["player"] == "Lepica"
    assert summary["players"] == ("Lepica [YOU]", "Rin")
    assert summary["pets"] == ("Bat [YOURS]", "Wolf")
    assert summary["monsters"] == ("Slime x2",), summary
    assert summary["counts"] == {"monster": 3, "player": 2, "pet": 2}

    class BoundedMS(SummaryMS):
        player_reads = pet_reads = summoner_reads = monster_checks = 0

        @classmethod
        def player_name(cls, mem, unit):
            cls.player_reads += 1
            return f"Player{unit:X}"

        @classmethod
        def monster_id(cls, mem, unit):
            cls.pet_reads += 1
            return f"Unit{unit:X}"

        @classmethod
        def summoner_of(cls, mem, unit):
            cls.summoner_reads += 1
            return 0x1000 if unit == 0x3000 else 0

        @classmethod
        def real_monster(cls, mem, unit):
            cls.monster_checks += 1
            return False

    many = [("player", 0x1000, 0.0, 0.0, 0.0)]
    many += [("player", 0x2000 + i, float(i + 1), 0.0, 0.0)
             for i in range(20)]
    many += [("pet", 0x3000 + i, float(i + 1), 0.0, 1.0)
             for i in range(20)]
    many += [("monster", 0x5000 + i, float(i + 1), 0.0, 2.0)
             for i in range(20)]
    memory_scan_summary(BoundedMS, SummaryMem(), many, 0x1000,
                        max_monsters=1, max_players=2, max_pets=1)
    assert BoundedMS.player_reads <= 3, BoundedMS.player_reads
    assert BoundedMS.pet_reads <= 2, BoundedMS.pet_reads
    assert BoundedMS.summoner_reads <= 1, BoundedMS.summoner_reads
    assert BoundedMS.monster_checks == 12, BoundedMS.monster_checks

    class PriorityMS(SummaryMS):
        @staticmethod
        def real_monster(mem, unit):
            return True

        @staticmethod
        def monster_id(mem, unit):
            return "Current Boss" if unit == 0xDEAD else f"Other {unit:X}"

    priority_units = [("player", 0x1000, 0.0, 0.0, 0.0)]
    priority_units += [("monster", 0x6000 + i, float(i), 0.0, 0.0)
                       for i in range(20)]
    priority_units.append(("monster", 0xDEAD, 999.0, 0.0, 0.0))
    priority = memory_scan_summary(PriorityMS, SummaryMem(), priority_units,
                                   0x1000,
                                   max_monsters=1,
                                   priority_monster=0xDEAD)
    assert priority["monster_names"] == {0xDEAD: "Current Boss"}, priority
    refreshed = MemoryEyes.__new__(MemoryEyes)
    refreshed.chasing, refreshed.target_name = 0xDEAD, "unknown"
    refreshed.scan_summary = {}
    refreshed._accept_scan_summary(priority)
    assert refreshed.target_name == "Current Boss"

    class DashboardEyes:
        def __init__(self):
            self.lock = threading.Lock()
            self.scan_summary = summary
            self.scan_error = ""
            self.loot = {1: (1.0, 0.0, 1.0, "Gold Ore"),
                         2: (2.0, 0.0, 2.0, "Solar Spear")}
            self.scanner = None
            self.classes = {"monster": 1, "player": 2, "pet": 3, "loot": 4}
            self.me, self.basis = 0x1000, ((1.0, 0.0), (0.0, 1.0))
            self.chasing, self.target_name = 0x5000, "Slime"
            self.loot_name, self.loot_mode = "Gold Ore", "loot"
            self.mode, self.routing = "chasing", False

    saved_dashboard_names, LOOT_NAMES = LOOT_NAMES, ("Gold Ore",)
    try:
        snapshot = dashboard_snapshot(DashboardEyes(), True, "loot", 1.0, 0.0,
                                      True, "lt", True, 8.0)
        fallback = dashboard_snapshot(DashboardEyes(), True, "no monster",
                                      memory_driving=False, bot_mode="memory")
    finally:
        LOOT_NAMES = saved_dashboard_names
    assert snapshot["loot"]["detected"] == 2
    assert snapshot["loot"]["wanted"] == 1
    assert "Gold Ore x1 [WANTED]" in snapshot["loot"]["ground"]
    assert snapshot["target"] == "LOOT Gold Ore at 8.0"
    assert fallback["source"] == "pixels", fallback
    assert fallback["bot_mode"] == "memory", fallback
    assert "temporary fallback" in fallback["warning"], fallback
    reconnect_view = dashboard_snapshot(DashboardEyes(), True,
                                        "reconnect: character screen",
                                        memory_driving=False,
                                        status="RECONNECTING")
    assert reconnect_view["status"] == "RECONNECTING"
    assert reconnect_view["stick"] == (0.0, 0.0)
    assert not reconnect_view["attack"]
    assert reconnect_view["target"] == "none"
    paused_view = dashboard_snapshot(DashboardEyes(), False,
                                     "press End to start")
    assert paused_view["target"] == "none"
    assert paused_view["navigation"] == "paused"

    # Wandering the area is memory driving with no monster held. The Target
    # line must say so, not fall through to a phantom pixel target.
    wander_eyes = DashboardEyes()
    wander_eyes.chasing = None
    wander_eyes.mode = "wander"
    wander_view = dashboard_snapshot(wander_eyes, True, "wander",
                                     -0.85, -0.53, True, "", False, 10.4)
    assert wander_view["source"] == "memory", wander_view
    assert wander_view["target"] == "WANDER at 10.4", wander_view
    assert "PIXEL" not in wander_view["target"], wander_view

    # Nearest-from-scratch oscillates when two monsters exchange which is a pixel
    # closer. A lock must keep the original marker, then stop rather than coast
    # through it when that marker disappears beneath the player arrow.
    lock = TargetLock()
    left, right = (75.0, 100.0, 8.0), (127.0, 100.0, 8.0)
    assert lock.pick([left, right], 100.0, 100.0) == left
    left, right = (74.0, 100.0, 8.0), (125.0, 100.0, 8.0)
    assert nearest([left, right], 100.0, 100.0) == right
    assert lock.pick([left, right], 100.0, 100.0) == left

    # The target vanishes -- killed, or hidden under the character. Either way the
    # next one is due at once. Waiting to see whether it was really dead cannot
    # work: our own pet sits inside CONCEAL_PX forever, so every test for "is
    # something still under me" answers yes and the bot stands there.
    lock.reset()
    close, competitor = (122.0, 100.0, 8.0), (150.0, 100.0, 8.0)
    assert lock.pick([close, competitor], 100.0, 100.0) == close
    after = [lock.pick([competitor], 100.0, 100.0)
             for _ in range(TARGET_FLICKER_FRAMES + 1)]
    assert after[:-1] == [None] * TARGET_FLICKER_FRAMES, after
    assert after[-1] == competitor and not lock.concealed
    stall = len(after) / LOOP_HZ
    assert stall <= 0.2, f"post-kill stall must stay under 0.2s, got {stall}"

    # A target lost well away from the character is not an arrival: it flickered
    # or died out there, so the bot coasts rather than treating it as reached.
    lock.reset()
    far = (100.0 - TARGET_ARRIVE_PX - 15, 100.0, 8.0)
    assert lock.pick([far], 100.0, 100.0)
    assert lock.pick([], 100.0, 100.0) is None and not lock.concealed

    watchdog = StuckWatchdog()
    blocked = (60.0, 100.0, 8.0)  # 40px away and making no progress
    assert not watchdog.update(1, blocked, 100.0, 100.0, now=0.0)
    assert not watchdog.update(1, blocked, 100.0, 100.0,
                               now=STUCK_TIMEOUT_S - 0.01)
    assert watchdog.update(1, blocked, 100.0, 100.0, now=STUCK_TIMEOUT_S)

    # Four pixels of cumulative progress restarts the timeout.
    watchdog.reset()
    assert not watchdog.update(2, (50.0, 100.0, 8.0), 100.0, 100.0, now=0.0)
    assert not watchdog.update(2, (55.0, 100.0, 8.0), 100.0, 100.0, now=2.9)
    assert not watchdog.update(2, (55.0, 100.0, 8.0), 100.0, 100.0, now=5.8)
    assert watchdog.update(2, (55.0, 100.0, 8.0), 100.0, 100.0, now=5.9)
    watchdog.reset()
    assert not watchdog.update(3, (130.0, 100.0, 8.0),
                               100.0, 100.0, now=99.0)  # near targets never time out

    blacklist = TargetBlacklist()
    other = (150.0, 100.0, 8.0)
    blacklist.block(blocked, now=0.0)
    assert blacklist.filter([blocked, other], now=0.0) == [other]
    moved_blocked = (65.0, 100.0, 8.0)
    assert blacklist.filter([moved_blocked, other], now=1.0) == [other]
    assert blacklist.filter([moved_blocked, other],
                            now=TARGET_IGNORE_S + 0.01) == [moved_blocked, other]

    assert TOGGLE_VK == 0x23 and START_PAUSED
    assert BUFF_SEQUENCE == ("up", "down", "left", "right", "x", "a")
    assert attack_active(0.0, buffing=True), "routine buffs must not release attack"
    assert not attack_active(0.0, blocked=True), "do not swing at a cloaked target"
    assert attack_active(0.0, buffing=False), "attack resumes after the last buff"

    # Memory targeting. stick_for inverts a measured basis, so the world's axes
    # and the camera's orientation never have to be known -- which is the point:
    # a rotated minimap cannot mislead a heading computed this way.
    ident = ((1.0, 0.0), (0.0, 1.0))          # stick east goes world +x
    sx, sy = stick_for(ident, 3.0, 0.0)
    assert sx > 0.99 and abs(sy) < 0.01, (sx, sy)
    turned = ((0.0, -1.0), (1.0, 0.0))        # world rotated 90 degrees
    sx, sy = stick_for(turned, 0.0, 5.0)      # want +z, must push east
    assert sx > 0.99 and abs(sy) < 0.01, (sx, sy)
    assert stick_for(((1.0, 2.0), (2.0, 4.0)), 1.0, 1.0) is None  # parallel pushes
    assert abs((lambda s: (s[0] ** 2 + s[1] ** 2) ** 0.5)(
        stick_for(ident, 900.0, 900.0)) - SPEED) < 1e-6   # always full tilt

    # a pet is never a target, however close it sits
    around = [("pet", 0xA, 1.0, 0.0, 0.0), ("monster", 0xB, 9.0, 0.0, 0.0),
              ("player", 0xC, 2.0, 0.0, 0.0), ("monster", 0xD, 40.0, 0.0, 0.0)]
    hit, dist = nearest_monster(around, 0.0, 0.0)
    assert hit[0] == 0xB and abs(dist - 9.0) < 1e-6, (hit, dist)
    assert nearest_monster(around, 0.0, 0.0, reach=5.0) == (None, None)
    assert nearest_monster([("pet", 0xA, 1.0, 0.0, 0.0)], 0.0, 0.0) == (None, None)

    # Identifying our own unit among a crowd. The decoy walks further on every
    # single leg -- which is exactly how a busy map broke calibration -- but its
    # travel has nothing to do with the stick, so it must never be chosen.
    _me, _decoy, _idle = 0xAA, 0xBB, 0xCC
    _basis = ((6.0, 1.0), (-1.0, 5.0))
    _legs = []
    for _i, (_sx, _sy) in enumerate(((1.0, 0.0), (0.0, 1.0), (-1.0, 0.0),
                                     (0.0, -1.0))):
        _mine = (_sx * _basis[0][0] + _sy * _basis[1][0],
                 _sx * _basis[0][1] + _sy * _basis[1][1])
        _wander = (14.0, -9.0) if _i % 2 else (-3.0, 15.0)   # always bigger
        _legs.append(((_sx, _sy), {_me: _mine, _decoy: _wander,
                                   _idle: (0.01, 0.0)}))
    assert pick_me(_legs) == _me, hex(pick_me(_legs) or 0)
    assert pick_me([_legs[0]]) is None, "one leg cannot identify anyone"

    # A monster beyond MEM_RANGE must still be walked to. Returning nothing
    # here stands the bot still forever: main() treats a zero stick as handled
    # and never falls through to the pixel path.
    class _Fights:
        """Stands in for memscan: says which stub units are worth fighting."""
        def __init__(self, real, standing=True, hidden=(), ids=None):
            self.real, self.standing = set(real), standing
            self.hidden = set(hidden)
            self.ids = ids or {}

        def worth_fighting(self, _, unit):
            if unit == 0x1000:              # 0x1000 is us; alive unless a corpse
                return self.standing
            return unit in self.real

        def real_monster(self, mem, unit):
            return self.worth_fighting(mem, unit)

        def monster_target_state(self, mem, unit):
            return self.real_monster(mem, unit), unit in self.hidden

        def network_object_id(self, mem, unit):
            return self.ids.get(unit)

    class _Far(MemoryEyes):
        def __init__(self, at, extra=(), real=(0x2000,), hidden=(), ids=None):
            self.me, self.basis = 0x1000, [[1.0, 0.0], [0.0, 1.0]]
            self.units = [("monster", 0x2000, at, 0.0, 0.0)] + list(extra)
            self.chasing = self.engaged_since = self.approach = None
            self.ignored = {}
            self.mode, self.misses, self.hot = "chasing", 0, None
            self.orbit_dir, self.orbit_mark = 1, None
            self.spacing_state = None
            self.at, self.mem = at, None
            self.seen_at, self.sweep_at, self.fight_ok = {}, 0, {}
            self.ms = _Fights(real, hidden=hidden, ids=ids)
            self.lock = threading.Lock()

        def _positions(self, addrs):
            return {a: ((self.at, 0.0, 0.0) if a == 0x2000
                        else (self.spots.get(a, 0.0), 0.0, 0.0)
                        if hasattr(self, "spots") else (0.0, 0.0, 0.0))
                    for a in addrs}

    # Returning from the character screen creates a new heap session. Every
    # address and the measured basis from the old one must be discarded at once,
    # leaving pixels in charge until a full background sweep lands again.
    relogged = _Far(5.0)
    relogged.owner, relogged.hot = 0x1000, [(0x100000, 0x1000)]
    relogged.hot_loot = [(0x200000, 0x1000)]
    relogged.loot = {0x3000: (1.0, 0.0, 1.0, "Gem")}
    relogged.loot_target, relogged.loot_since = 0x3000, 1.0
    relogged.returning, relogged.home_goal = True, (9.0, 9.0)
    relogged.wander, relogged.wander_until = (8.0, 8.0), 99.0
    relogged.returning_since = relogged.wander_committed = 99.0
    relogged.generation = 4
    relogged.reset_session()
    assert relogged.me is None and relogged.basis is None
    assert relogged.owner is None and relogged.hot is None and relogged.hot_loot is None
    assert relogged.units == [] and relogged.loot == {}
    assert relogged.generation == 5
    assert not relogged.returning and relogged.home_goal is None
    assert relogged.wander is None and relogged.wander_until == 0.0

    # The region caches narrow each sweep to the heap regions that held
    # units/drops at build time. Built once and rebuilt only on a relog, a bot
    # that walks away keeps sweeping the old regions and sees only the pooled
    # corpses left behind there -- the "5 monster objects, 0 loot" state. Past
    # HOT_RENARROW_RADIUS from where a cache was built, target() drops it so the
    # next sweep is a full pass that re-narrows where the character is now.
    class _Renarrow(MemoryEyes):
        def __init__(self):
            self.hot = [(0x100000, 0x1000)]
            self.hot_at = (0.0, 0.0)
            self.hot_loot = [(0x200000, 0x1000)]
            self.hot_loot_at = (0.0, 0.0)
            self.lock = threading.Lock()

    rn_far = _Renarrow()
    rn_far._renarrow_if_stale(HOT_RENARROW_RADIUS + 10.0, 0.0)
    assert rn_far.hot is None and rn_far.hot_at is None
    assert rn_far.hot_loot is None and rn_far.hot_loot_at is None
    rn_near = _Renarrow()
    rn_near._renarrow_if_stale(50.0, 0.0)   # inside the radius
    assert rn_near.hot == [(0x100000, 0x1000)]
    assert rn_near.hot_loot == [(0x200000, 0x1000)]

    # A narrowed sweep that keeps coming back empty is sweeping stale regions,
    # not an empty world (the player name and loot still read). A few empty
    # passes drop the cache so the next sweep is a full pass that rebuilds it
    # where the units actually are, instead of waiting out HOT_SELF_HEAL_S.
    class _StaleHot(MemoryEyes):
        def __init__(self):
            self.hot = [(0x100000, 0x1000)]
            self.hot_at = (0.0, 0.0)
            self.hot_empty_streak = 0
            self.lock = threading.Lock()

    sh = _StaleHot()
    assert not sh._drop_stale_hot([])
    assert not sh._drop_stale_hot([])
    assert sh._drop_stale_hot([])
    assert sh.hot is None and sh.hot_at is None
    sh2 = _StaleHot()
    sh2.hot_empty_streak = HOT_EMPTY_STREAKS - 1
    assert not sh2._drop_stale_hot([("monster", 0x2000, 1.0, 0.0, 1.0)])
    assert sh2.hot == [(0x100000, 0x1000)]
    assert sh2.hot_empty_streak == 0

    far_off = _Far(MEM_RANGE * 3)              # way outside melee range
    fsx, fsy, fd = far_off.target(1.0)
    assert far_off.mode == "far", far_off.mode
    assert fsx is not None and (fsx or fsy), "must walk to it, not stand still"
    assert abs(fd - MEM_RANGE * 3) < 1.0, fd
    # FishNet ObjectId survives a managed-wrapper pointer change. Reacquiring
    # it must preserve the engagement clock rather than treating it as a switch.
    moved_wrapper = _Far(20.0, ids={0x2000: 77})
    moved_wrapper.chasing, moved_wrapper.chasing_id = 0xDEAD, 77
    moved_wrapper.engaged_since = 0.25
    moved_wrapper.target(1.0)
    assert moved_wrapper.chasing == 0x2000
    assert moved_wrapper.engaged_since == 0.25
    transient_id = _Far(20.0, ids={0x2000: 88})
    transient_id.target(1.0)
    transient_id.ms.ids.clear()          # one unreadable ObjectId frame
    transient_id.target(1.1)
    assert transient_id.chasing_id == 88
    assert transient_id.engaged_since == 1.0
    cloaked = _Far(5.0, real=(), hidden=(0x2000,))
    assert cloaked.target(1.0) == (0.0, 0.0, 5.0)
    assert cloaked.mode == "invisible", cloaked.mode
    # An identity-less MonsterController sitting in melee range must not be a
    # target. It is rendered and has health, so it passes every liveness test,
    # but it takes no damage -- and being inside MEM_ARRIVE it pins the bot to
    # "on it" with a zero stick, which is a bot that never moves and walks back
    # when you drag it away. Only a spawned monster carries a MonsterId.
    class _Nameless(_Fights):
        def real_monster(self, mem, unit):
            return self.worth_fighting(mem, unit) and unit != 0x4000

    nameless = _Far(MEM_RANGE * 3, extra=[("monster", 0x4000, 0.5, 0.0, 0.0)],
                    real=(0x2000, 0x4000))
    nameless.ms = _Nameless({0x2000, 0x4000})
    nameless.spots = {0x4000: 0.5}
    _, _, nd = nameless.target(1.0)
    assert nameless.chasing == 0x2000, "must ignore the one with no identity"
    assert nd > MEM_RANGE, nd
    assert nameless.mode == "far", nameless.mode

    # A pooled or dead monster sitting on top of us must not be a target, or
    # the bot parks in the pile and swings at it -- the exact symptom that had
    # it standing still and walking back when dragged away.
    pooled = _Far(MEM_RANGE * 3, extra=[("monster", 0x3000, 1.0, 0.0, 0.0)],
                  real=(0x2000,))
    pooled.spots = {0x3000: 1.0}
    _, _, pd = pooled.target(1.0)
    assert pooled.chasing == 0x2000, "must skip the pooled one right beside us"
    assert pd > MEM_RANGE, pd

    # --- confined to a recorded area ---
    # A circular camp uses exact world-space distance, not cell approximation.
    # The inner radius keeps the same return hysteresis as a painted mask.
    ring_area = Area("ring", path=os.devnull, circle=(10.0, -5.0, 30.0))
    assert ring_area.defined and ring_area.describe().startswith("circle centre")
    assert ring_area.inside(40.0, -5.0), "the exact boundary is inside"
    assert not ring_area.inside(40.01, -5.0), "past the exact boundary is outside"
    assert ring_area.inside(42.0, -5.0, 2.0), "target slack extends the radius"
    assert ring_area.deep(37.0, -5.0), "one AREA_CELL inside is safe home"
    assert not ring_area.deep(39.0, -5.0), "the fringe is not returned yet"
    assert ring_area.home(50.0, -5.0) == (37.0, -5.0)
    assert ring_area.nearest(50.0, -5.0) == (40.0, -5.0)
    assert all(ring_area.deep(*ring_area.spot(random)) for _ in range(100)), \
        "circle wander points must stay in the safe inner disc"

    # Several circles form one exact union. The second disc extends the target
    # area, return chooses its nearest safe edge, and wander never leaves either.
    union = Area("union", path=os.devnull,
                 circles=[(0.0, 0.0, 20.0), (35.0, 0.0, 20.0)])
    assert union.inside(-20.0, 0.0) and union.inside(55.0, 0.0)
    assert not union.inside(55.01, 0.0)
    assert union.bounds() == ((-20.0, -20.0), (55.0, 20.0))
    assert union.home(70.0, 0.0) == (52.0, 0.0)
    assert union.nearest(70.0, 0.0) == (55.0, 0.0)
    assert all(union.deep(*union.spot(random)) for _ in range(100))
    union_target = _Far(45.0)
    union_target.area = union
    union_target.target(1.0)
    assert union_target.chasing == 0x2000 and union_target.mode == "chasing", \
        "a monster in any member circle belongs to the union"
    union_outside = _Far(60.0)
    union_outside.area = union
    union_outside.target(1.0)
    assert union_outside.chasing is None and union_outside.mode == "wander"

    # Drive the real target() path too: outside monsters are rejected and a
    # character beyond the leave margin is routed back toward the inner disc.
    ring_caged = _Far(45.0)
    ring_caged.area = ring_area
    rcx, rcy, _ = ring_caged.target(1.0)
    assert ring_caged.mode == "wander" and ring_caged.chasing is None
    assert rcx is not None and (rcx or rcy), "an empty circle camp must wander"
    ring_lost = _Far(MEM_RANGE * 3)
    ring_lost.area, ring_lost.spots = ring_area, {0x1000: 50.0}
    rlx, _, _ = ring_lost.target(1.0)
    assert ring_lost.mode == "going back" and ring_lost.returning
    assert rlx < 0, "circle return must head toward its centre"

    # Circle and legacy walked-mask entries coexist in the same areas.json.
    area_tmp = os.path.join(os.environ.get("TEMP", "."), "areas_demo.json")
    try:
        os.remove(area_tmp)
    except OSError:
        pass
    saved_ring = Area("ring", path=area_tmp, circle=(10.0, -5.0, 30.0))
    assert saved_ring.save()
    saved_union = Area("union", path=area_tmp,
                       circles=[(0.0, 0.0, 20.0), (35.0, 0.0, 20.0)])
    assert saved_union.save()
    saved_mask = Area("mask", path=area_tmp)
    saved_mask.paint(0.0, 0.0)
    assert saved_mask.save()
    loaded_ring, loaded_union, loaded_mask = (Area("ring", path=area_tmp).load(),
                                              Area("union", path=area_tmp).load(),
                                              Area("mask", path=area_tmp).load())
    assert loaded_ring.circle == saved_ring.circle and loaded_ring.defined
    assert loaded_union.circles == saved_union.circles
    assert loaded_mask.cells == saved_mask.cells and loaded_mask.circle is None
    os.remove(area_tmp)

    # A strip of ground along +x, the shape a walk down a road produces.
    pen = Area("pen", path=os.devnull)
    for i in range(10):
        pen.paint(i * 2.0, 0.0)

    # A monster outside the fence is not a target at all, and with nothing left
    # inside the bot wanders rather than standing in a field for an hour.
    caged = _Far(MEM_RANGE / 2)          # monster far up +x, well outside
    caged.area = pen
    wsx, wsy, _ = caged.target(1.0)
    assert caged.mode == "wander", caged.mode
    assert wsx is not None and (wsx or wsy), "wander must move, not park the bot"
    assert caged.chasing is None, "nothing outside the fence is a target"
    assert pen.inside(*caged.wander), "wander may only aim inside the area"

    # Wandering must not re-pick every frame. A point chosen nearer than
    # AREA_WANDER_REACHED counts as arrived the moment it is chosen, so without
    # a commit floor the bot changes direction 20 times a second -- which from
    # outside is a character shaking left and right on the spot.
    small = Area("small", path=os.devnull)
    small.paint(0.0, 0.0)                 # one brush disc: everything is close
    jitter = _Far(MEM_RANGE * 3)          # the monster is far outside it
    jitter.area = small
    picks, t = [], 1.0
    for _ in range(40):                   # two seconds at 20 Hz
        jitter.target(t)
        picks.append(jitter.wander)
        t += 1 / LOOP_HZ
    changes = sum(1 for a, b in zip(picks, picks[1:]) if a != b)
    assert jitter.mode == "wander", jitter.mode
    assert changes <= 2, f"wander re-picked {changes} times in 2 s"

    # On an area big enough, it should aim somewhere worth walking to.
    roomy = Area("roomy", path=os.devnull)
    for i in range(30):
        roomy.paint(i * 3.0, 0.0)
    far_pick = _Far(MEM_RANGE * 30)       # nothing inside to distract it
    far_pick.area = roomy
    far_pick.target(1.0)
    assert math.hypot(*far_pick.wander) >= AREA_WANDER_MIN, far_pick.wander

    # Outside it: walk back, and toward the area rather than away from it.
    lost = _Far(MEM_RANGE * 3)
    lost.area, lost.spots = pen, {0x1000: 40.0}   # the strip ends well short
    lsx, _, ld = lost.target(1.0)
    assert lost.mode == "going back", lost.mode
    assert lost.returning and lsx < 0, (lsx, lost.returning)
    assert ld > 0, "a zero distance makes the walk-back unbeatable by loot"

    # The safety margin is inside the recorded boundary. A character already in
    # that fringe cancels combat and returns instead of using target slack to
    # justify another outward step.
    (_, _), (edge_x, _) = pen.bounds()
    lean = _Far(MEM_RANGE * 3)
    lean.area, lean.spots = pen, {0x1000: edge_x + 0.5}
    lean.target(1.0)
    assert lean.returning and lean.mode == "going back", lean.mode
    assert lean.chasing is None, "returning must cancel combat"
    # Genuinely out is still out.
    gone = _Far(MEM_RANGE * 3)
    gone.area, gone.spots = pen, {0x1000: edge_x + AREA_SAFETY * 4}
    gone.target(1.0)
    assert gone.returning and gone.mode == "going back", gone.mode

    # A held monster that crosses the exact boundary is dropped immediately;
    # pointer stability never grants it permission outside the zone.
    edgy = _Far(MEM_RANGE * 3, extra=[("monster", 0x5000, 0.0, 0.0, 0.0)],
                real=(0x2000, 0x5000))
    edgy.area = pen
    edgy.spots = {0x5000: edge_x + 0.5}
    edgy.chasing = 0x5000
    edgy.target(1.0)
    assert edgy.chasing != 0x5000, "a held target outside must be dropped"

    # Crossing back over the line is not being back in. The deleted leash had
    # one threshold and bounced along it forever against a monster on the edge.
    edge = _Far(MEM_RANGE * 3)
    fringe = next(pen.centre(c)[0] for c in sorted(pen.cells)
                  if c not in pen.core and c[1] == pen.at(0.0, 0.0)[1])
    edge.area, edge.returning = pen, True
    edge.home_goal, edge.spots = pen.home(fringe, 0.0), {0x1000: fringe}
    edge.target(1.0)
    assert pen.inside(fringe, 0.0) and edge.returning,         "the fringe is inside, but not yet back in"

    # Safe re-entry is not enough: finish the committed walk to home_goal or the
    # next target can reverse the stick at the line and recreate boundary flapping.
    homed = _Far(MEM_RANGE * 3)
    homed.area, homed.returning, homed.spots = pen, True, {0x1000: 10.0}
    homed.home_goal = pen.centre(pen.at(16.0, 0.0))
    homed.target(1.0)
    assert homed.returning and homed.mode == "going back", \
        "safe re-entry must finish the committed home walk"
    homed.spots = {0x1000: homed.home_goal[0]}
    homed.target(1.1)
    assert not homed.returning, "reaching home_goal must release the walk-back"

    # Another map is not a stray step -- nothing records which map an area
    # belongs to, so --area on the wrong one puts home thousands of units away.
    # Hold loudly rather than either lean into scenery or release pixels.
    elsewhere = _Far(MEM_RANGE * 3)
    elsewhere.area, elsewhere.spots = pen, {0x1000: AREA_ABANDON * 3}
    esx, _, _ = elsewhere.target(1.0)
    assert elsewhere.mode == "no area" and elsewhere.area is pen
    assert esx == 0.0, "wrong-map detection must park the bot, not steer it"
    assert hold_still("no area"), "and must not fall through to the pixel path"
    # A fence still active must hold the pixel path: pixels know nothing about
    # the area and would steer the bot anywhere, which is how it left the
    # recorded ground and never came back.
    assert area_holds(caged), "an active fence must hold the pixel path"
    assert area_holds(elsewhere), "wrong-map failure must keep holding pixels"
    assert not area_holds(None), "no eyes, no fence"

    # With no area recorded at all the whole feature is inert.
    free = _Far(MEM_RANGE * 3)
    assert free.area is None
    free.target(1.0)
    assert free.mode == "far", free.mode

    assert resume_distance > min_distance
    # Hit-and-run owns only movement. Attack remains a separate held state.
    normal = _Far((min_distance + resume_distance) / 2)
    nsx, nsy, nd = normal.target(1.0)
    assert normal.mode == "on it" and normal.spacing_state == "ATTACK"
    assert abs(nd - (min_distance + resume_distance) / 2) < 1e-6
    assert abs(nsx) + abs(nsy) > 0.1, (nsx, nsy)
    assert attack_active(1.0), "attack strafe must not release LB/RB"

    close = _Far(min_distance / 2)
    close.spacing_state = "APPROACH"
    csx, csy, _ = close.target(1.0)
    assert close.spacing_state == "RETREAT"
    assert csx < 0 and abs(csy) < 1e-6, (csx, csy)
    assert attack_active(1.0), "retreat must not release LB/RB"

    still_close = _Far((min_distance + resume_distance) / 2)
    still_close.spacing_state = "RETREAT"
    ssx, _, _ = still_close.target(1.0)
    assert still_close.spacing_state == "RETREAT" and ssx < 0
    spaced = _Far(resume_distance)
    spaced.spacing_state = "RETREAT"
    sx0, sy0, _ = spaced.target(1.0)
    assert spaced.spacing_state == "ATTACK" and abs(sx0) + abs(sy0) > 0.1

    far_fight = _Far(MEM_ARRIVE + 1.0)
    fsx, fsy, _ = far_fight.target(1.0)
    assert far_fight.spacing_state == "APPROACH" and (fsx or fsy)
    assert attack_active(1.0), "approach must not release LB/RB"
    # Loot. The rule under test is the one the pooling forces: a slot holding a
    # position is not a drop, a slot that has been seen to *change* is.
    class _Loot(MemoryEyes):
        def __init__(self, drops=()):
            self.me, self.basis = 0x1000, [[1.0, 0.0], [0.0, 1.0]]
            self.loot = {d: (x, 0.0, z, n) for d, x, z, n in drops}
            self.loot_target = self.loot_since = None
            self.loot_ignored, self.loot_mode, self.loot_name = {}, "no loot", ""
            self.hot_loot, self.mem, self.ms = None, None, None
            self.lock = threading.Lock()

        def _positions(self, addrs):
            return {a: (0.0, 0.0, 0.0) for a in addrs}   # we stand at origin

    # The tests below own this setting: it is a user config, and a self-check
    # that passes or fails depending on which items someone is farming today is
    # worse than no self-check at all.
    kept_names, LOOT_NAMES = LOOT_NAMES, ()

    # Calibration with our unit already known: it still has to measure what a
    # push does, because that depends on the camera, but it no longer has to
    # work out WHICH unit answered -- so two legs replace six.
    class _Cal(MemoryEyes):
        def __init__(self, owner):
            self.owner, self.me, self.basis = owner, None, None
            self.units = [("player", 0x1000, 0.0, 0.0, 0.0),
                          ("player", 0x2000, 5.0, 0.0, 5.0)]
            self.lock = threading.Lock()
            self.spot = {0x1000: [0.0, 0.0], 0x2000: [5.0, 5.0]}
            self.pushes = []

        def _positions(self, addrs):
            return {a: (self.spot[a][0], 0.0, self.spot[a][1]) for a in addrs
                    if a in self.spot}

    class _CalPad:
        def __init__(self, eyes):
            self.eyes = eyes

        def stick(self, sx, sy, attack=False):
            if sx or sy:
                # our unit walks with the push; the other player wanders
                self.eyes.spot[0x1000][0] += sx * 0.5
                self.eyes.spot[0x1000][1] += sy * 0.5
                self.eyes.spot[0x2000][0] += 0.9

    kept_push = MEM_CAL_PUSH_S
    globals()["MEM_CAL_PUSH_S"] = 0.01           # the clock is not under test
    try:
        cal = _Cal(0x1000)
        cal.stick_log = []
        assert cal.calibrate(_CalPad(cal)), "two legs should be a basis"
        assert cal.me == 0x1000, cal.me
        assert stick_for(cal.basis, 1.0, 0.0) is not None
        # An owner that is no longer in the unit list is a leftover from
        # before a relog, and trusting it means every leg is thrown away.
        stale = _Cal(0xDEAD)
        assert stale.calibrate(_CalPad(stale)), "stale owner must not stick"
        assert stale.me == 0x1000, stale.me

        # And without it, the old path still has to work -- that is the
        # fallback whenever the walk to our unit comes back empty.
        blind = _Cal(None)
        assert blind.calibrate(_CalPad(blind)), "six-leg fallback"
        assert blind.me == 0x1000, blind.me
    finally:
        globals()["MEM_CAL_PUSH_S"] = kept_push

    near_loot = _Loot(drops=[(0xA000, 5.0, 0.0, "Flax")])
    lsx, lsy, ld = near_loot.pick_loot(2.0)
    assert lsx is not None and abs(ld - 5.0) < 1e-6, (lsx, ld)
    assert near_loot.loot_mode == "loot", near_loot.loot_mode
    assert near_loot.loot_name == "Flax", near_loot.loot_name

    # Standing on it: stick goes still and the trigger press is what acts.
    on_it = _Loot(drops=[(0xA000, LOOT_ARRIVE / 2, 0.0, "Flax")])
    assert on_it.pick_loot(2.0)[:2] == (0.0, 0.0)
    assert on_it.loot_mode == "loot get", on_it.loot_mode

    # An item at our feet is collected without the loot path being involved at
    # all -- that is the kill-drops-at-your-feet case, mid-fight.
    underfoot = _Loot(drops=[(0xA000, LOOT_ARRIVE / 2, 0.0, "Flax")])
    assert underfoot.loot_here() and underfoot.loot_name == "Flax"
    assert not _Loot(drops=[(0xA000, LOOT_ARRIVE * 3, 0.0, "Flax")]).loot_here()

    # Who wins the frame. Nearest-wins alone left drops lying on a busy map,
    # where a monster is almost always the nearer of the two. Pinned rather than
    # read from the constant: at LOOT_FIRST_RANGE == LOOT_RANGE every drop that
    # can be offered is inside it, and the nearest-wins half is unreachable.
    global LOOT_FIRST_RANGE
    kept_first, LOOT_FIRST_RANGE = LOOT_FIRST_RANGE, 15.0
    assert loot_wins("chasing", 4.0, 14.0), "close item goes first"
    assert not loot_wins("chasing", 4.0, 20.0), "far item waits"
    assert loot_wins("chasing", 30.0, 20.0), "unless it is nearer"
    assert loot_wins("far", 80.0, 39.0), "a far monster always yields"
    assert loot_wins("no monster", None, 39.0), "nothing to fight, so loot"
    assert not loot_wins("chasing", 4.0, None), "no item, no contest"
    # Two pushes that must never be interrupted: a fight already joined, and a
    # character in the middle of backing out of a wedge (which reports 0.0, so
    # every item on the map would otherwise look nearer than the monster).
    assert not loot_wins("on it", 1.0, 1.0), "never walk out of melee"
    assert not loot_wins("unwedge", 0.0, 1.0), "never interrupt an escape"
    assert not loot_wins("going back", 20.0, 1.0), "never delay confinement"
    LOOT_FIRST_RANGE = kept_first

    # LOOT_MAX_S counts time spent walking to an item, not time the item spent
    # losing to a nearer monster -- otherwise a drop at our feet is blacklisted
    # for LOOT_IGNORE_S having never been approached, which is exactly what
    # "the bot ignores items right next to it" looks like.
    patient = _Loot(drops=[(0xA000, 5.0, 0.0, "Flax")])
    patient.pick_loot(2.0)
    for i in range(20):                     # kept losing, clock kept restarting
        patient.pick_loot(2.0 + i)
        patient.loot_since = 2.0 + i
    assert patient.pick_loot(2.0 + 20) != (None, None, None), "must still be offered"
    assert not patient.loot_ignored, patient.loot_ignored
    # And it says why when it offers nothing at all.
    assert "cache empty" in _Loot().loot_debug(1.0)
    shunned = _Loot(drops=[(0xA000, 1.0, 0.0, "Flax")])
    shunned.loot_ignored[shunned._loot_key(0xA000, 1.0, 0.0, "Flax")] = 99.0
    assert "IGNORED" in shunned.loot_debug(1.0), shunned.loot_debug(1.0)

    # Out of range is left alone: crossing the map for an item is not looting.
    away = _Loot(drops=[(0xA000, LOOT_RANGE * 2, 0.0, "Flax")])
    assert away.pick_loot(2.0) == (None, None, None)

    # An item that cannot be collected must be given up on, or it owns the bot.
    stuck_loot = _Loot(drops=[(0xA000, 5.0, 0.0, "Flax")])
    stuck_loot.pick_loot(2.0)
    assert stuck_loot.pick_loot(2.0 + LOOT_MAX_S + 1) == (None, None, None)
    assert stuck_loot.loot_mode == "loot skip", stuck_loot.loot_mode
    assert any(key[0] == 0xA000 for key in stuck_loot.loot_ignored)

    # The user-facing loot config is one substring per line. Blank lines and
    # comments are ignored so the shipped file can explain itself.
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as cfg:
        cfg.write("# wanted item families\n Gem \n\ncard\nEssence\n")
        cfg_path = cfg.name
    try:
        assert load_loot_names(cfg_path) == ("Gem", "card", "Essence")
    finally:
        os.remove(cfg_path)
    assert load_loot_names(cfg_path) is None, (
        "a missing config must disable filtered loot, not collect everything")

    # The allowlist: case-insensitive substrings, so one entry covers a family.
    LOOT_NAMES = ("card", " Flax ")
    try:
        assert wanted_item("Bee Card") and wanted_item("Rooster Card")
        assert wanted_item("flax"), "matching ignores case and padding"
        assert not wanted_item("Slingshot")
        picky = _Loot(drops=[(0xA000, 1.0, 0.0, "Slingshot"),
                             (0xB000, 9.0, 0.0, "Sprount Card")])
        _, _, pd = picky.pick_loot(2.0)
        assert picky.loot_name == "Sprount Card", picky.loot_name
        assert abs(pd - 9.0) < 1e-6, pd
        only_junk = _Loot(drops=[(0xA000, 1.0, 0.0, "Broad Sword")])
        assert only_junk.pick_loot(2.0) == (None, None, None)
        # One name without the trailing comma is a string; iterating it would
        # test single letters, and 'C' alone would match half the map.
        LOOT_NAMES = ("Card")
        assert wanted_item("Bee Card") and not wanted_item("Potions")
        # An empty entry must not turn into "everything contains ''".
        LOOT_NAMES = ("", "Card")
        assert wanted_item("Bee Card") and not wanted_item("Potions")
    finally:
        LOOT_NAMES = ()
    assert wanted_item("anything at all"), "an empty list means take everything"
    LOOT_NAMES = kept_names

    only_pooled = _Far(MEM_RANGE * 3, real=())     # nothing real at all
    assert only_pooled.target(1.0) == (None, None, None)
    assert only_pooled.mode == "no monster", only_pooled.mode
    # ...and a memory gap must not park the bot. A zero stick counts as handled
    # in main(), so both an empty monster list and a pending unit scan have to
    # reach the pixel path instead.
    assert not hold_still("no monster")
    assert not hold_still("no unit"), \
        "while memory waits for a unit scan, pixels must temporarily drive"
    assert hold_still("lost")

    # Player visibility/health is not a reliable stop signal. IsVisible can go
    # false while the character is still alive, and stopping there ends an
    # unattended grind. Monster liveness still uses worth_fighting(); only our
    # own unit must continue from its readable position.
    class _HiddenPlayer(_Far):
        def __init__(self):
            _Far.__init__(self, 5.0)
            self.ms = _Fights((0x2000,), standing=False)

    hidden = _HiddenPlayer()
    hsx, hsy, _ = hidden.target(1.0)
    assert hsx is not None and (hsx or hsy), "player visibility must not stop grinding"
    assert hidden.mode == "chasing", hidden.mode

    near = _Far(MEM_RANGE / 2)                 # inside range: ordinary chase
    near.target(1.0)
    assert near.mode == "chasing", near.mode
    assert near.approach, "the chase heading is what the back-off reverses"

    # On a target the attack buttons are independent from spacing: the stick can
    # approach, stop, or back off without changing the held LB/RB state.
    onto = _Far(MEM_ARRIVE / 2)
    onto.spacing_state = "RETREAT"
    one = onto.target(1.0)
    two = onto.target(1.0)
    assert onto.mode == "on it", onto.mode
    assert one[0] < 0 and two[0] < 0, (one, two)
    assert attack_active(1.0), "spacing must not release LB/RB"

    # A blank position read must not throw the calibration away: the bot goes
    # silent until someone notices and restarts it. Coast, then give up.
    class _Blind(MemoryEyes):
        def __init__(self):
            self.me, self.basis = 0x1000, [[1.0, 0.0], [0.0, 1.0]]
            self.units, self.chasing, self.engaged_since = [], None, None
            self.ignored = {}
            self.mode, self.misses, self.hot = "chasing", 0, [(1, 2)]
            self.generation = 7
            self.seen_at, self.sweep_at, self.fight_ok = {}, 0, {}
            self.ms, self.mem = _Fights((0x1000,)), None
            self.lock = threading.Lock()

        def _positions(self, _):
            return {}                          # every read comes back empty

    blind = _Blind()
    for i in range(MEM_LOST_FRAMES - 1):
        blind.target(1.0 + i)
        assert blind.me and blind.mode == "lost", (i, blind.mode)
    blind.target(99.0)                         # configured run complete: unit is gone
    assert blind.me is None and blind.mode == "no unit", blind.mode
    # A relog rebuilds our unit, so the pointer read from the connection is as
    # dead as the rest. Leaving it set meant the scanner never looked it up
    # again (it only reads when owner is None) and calibration kept pushing on
    # behalf of an object that no longer existed -- "no unit moved when pushed"
    # every 15s, forever, with a healthy character standing there.
    assert blind.owner is None, "the stale owner must go with the unit"
    assert blind.generation == 8, "an in-flight narrowed sweep must be invalidated"

    recovering = MemoryEyes.__new__(MemoryEyes)
    recovering.lock = threading.Lock()
    recovering.generation, recovering.hot = 3, [(1, 2)]
    recovering.units = [("monster", 1, 0.0, 0.0, 0.0)]
    recovering.fallback_since = None
    recovering.next_full_rescan = 0.0
    recovering.recovery = ""
    recovering.mode = "no monster"
    recovering.me, recovering.basis = 0x1000, ((1.0, 0.0), (0.0, 1.0))
    assert not recovering.note_pixel_fallback(100.0, True)
    assert not recovering.note_pixel_fallback(
        100.0 + MEM_PIXEL_RESCAN_S - 0.1, True)
    assert recovering.note_pixel_fallback(
        100.0 + MEM_PIXEL_RESCAN_S, True)
    assert recovering.generation == 4
    assert recovering.hot is None and not recovering.units


    # Giving up on a target that will not die. Without this the bot parks on the
    # spot swinging forever, which is indistinguishable from a hung bot.
    assert not stale_target(5.0, None)                   # nothing engaged
    assert not stale_target(5.0, 0.0, limit=8.0)         # still within its time
    assert stale_target(9.0, 0.0, limit=8.0)             # long enough, drop it
    assert MEM_IGNORE_S > MEM_ENGAGE_MAX_S, "must stay ignored longer than tried"

    # Two monsters at nearly the same distance: whichever is chosen must be kept
    # while it stays close, or the bot walks left, right, left, right as they
    # swap which is nearer. TARGET_SWITCH is how much better a rival must be.
    a_closer = [("monster", 1, 10.0, 0.0, 0.0), ("monster", 2, 10.4, 0.0, 0.0)]
    b_closer = [("monster", 1, 10.4, 0.0, 0.0), ("monster", 2, 10.0, 0.0, 0.0)]
    assert nearest_monster(a_closer, 0.0, 0.0)[0][0] == 1
    assert nearest_monster(b_closer, 0.0, 0.0)[0][0] == 2   # bare pick flaps
    held_d, rival_d = 10.4, 10.0                            # holding 1, 2 nearer
    assert not rival_d < held_d * TARGET_SWITCH, "a 4% gain must not switch"
    assert 3.0 < 10.4 * TARGET_SWITCH, "but a much nearer one still wins"

    # Camera check. Walking north scrolls the terrain down the image, and image y
    # runs downward, so an aligned camera turns a stick push of (0,1) into a
    # measured shift of (0,+8). Pushing east scrolls the terrain left: (-8,0).
    aligned = [((0.0, 1.0), (0.0, 8.0)), ((1.0, 0.0), (-8.0, 0.0)),
               ((0.0, -1.0), (0.0, -8.0)), ((-1.0, 0.0), (8.0, 0.0))]
    assert abs(heading_error(aligned)) < 1.0, heading_error(aligned)
    # rotated 90: pushing up sends the character right across the minimap, so the
    # terrain scrolls left instead of down
    turned = [((0.0, 1.0), (-8.0, 0.0)), ((1.0, 0.0), (0.0, -8.0)),
              ((0.0, -1.0), (8.0, 0.0)), ((-1.0, 0.0), (0.0, 8.0))]
    assert abs(abs(heading_error(turned)) - 90.0) < 1.0, heading_error(turned)
    # legs where nothing moved carry no information and must not drag the mean
    assert abs(heading_error(aligned + [((1.0, 0.0), (0.0, 0.0))])) < 1.0
    assert heading_error([((1.0, 0.0), (0.0, 0.0))]) is None
    polled = []
    assert toggle_key_hit(lambda vk: polled.append(vk) or 1)
    assert polled == [0x23]

    class TogglePad:
        def __init__(self):
            self.calls = []

        def stick(self, sx, sy, attack):
            self.calls.append((sx, sy, attack))

    class TogglePets:
        def __init__(self):
            self.resets = 0

        def reset(self):
            self.resets += 1

    toggle_pad, toggle_pets = TogglePad(), TogglePets()
    paused = toggle_running(True, toggle_pad, toggle_pets,
                            wake=lambda pad: pad.calls.append("wake"))
    assert not paused and toggle_pad.calls == [(0.0, 0.0, False), "wake"]
    paused = toggle_running(paused, toggle_pad, toggle_pets,
                            wake=lambda pad: pad.calls.append("wake"))
    assert paused and toggle_pad.calls[-1] == (0.0, 0.0, False)
    assert toggle_pad.calls.count("wake") == 1 and toggle_pets.resets == 2

    # The wake nudge walks the character, and walking with L1 released is how a
    # warp portal takes the bot to another map.
    warp_pad = TogglePad()
    old_sleep, time.sleep = time.sleep, lambda _s: None
    try:
        wake_controller(warp_pad)
    finally:
        time.sleep = old_sleep
    assert all(c[2] is True for c in warp_pad.calls), "wake dropped attack"

    class FakeVirtualGamepad:
        def __init__(self):
            self.calls = []
            self.held = set()

        def left_joystick_float(self, x, y):
            self.calls.append(("left", x, y))

        def press_button(self, button):
            self.held.add(button)
            self.calls.append(("down", button))

        def release_button(self, button):
            self.held.discard(button)
            self.calls.append(("up", button))

        def reset(self):
            self.held.clear()
            self.calls.append(("reset",))

        def update(self):
            self.calls.append(("update",))

    virtual = VirtualPad.__new__(VirtualPad)
    virtual.pad = FakeVirtualGamepad()
    virtual.attack_btn = ("lb", "rb")
    virtual.dpad = {key: key for key in ("up", "down", "left", "right")}
    virtual.face = {key: key for key in ("x", "a")}
    virtual.stick(0.5, -0.5, True)
    assert virtual.pad.calls == [("left", 0.5, -0.5),
                                 ("down", "lb"), ("down", "rb"),
                                 ("update",)], virtual.pad.calls
    virtual.pad.calls.clear()
    virtual.stick(0.5, -0.5, False)
    assert virtual.pad.calls == [("left", 0.5, -0.5),
                                 ("up", "lb"), ("up", "rb"),
                                 ("update",)], virtual.pad.calls
    virtual.pad.calls.clear()
    virtual.stick(0.0, 0.0, True)
    assert virtual.pad.calls == [("left", 0.0, 0.0),
                                 ("down", "lb"), ("down", "rb"),
                                 ("update",)], virtual.pad.calls
    virtual.pad.calls.clear()
    virtual.stick(0.0, 0.0, False)
    assert virtual.pad.calls == [("reset",), ("update",)], virtual.pad.calls

    virtual.pad.calls.clear()
    old_sleep, time.sleep = time.sleep, lambda seconds: virtual.pad.calls.append(
        ("sleep", seconds))
    try:
        for key in BUFF_SEQUENCE:
            tap_buff(virtual, key, BUFF_HOLD_S)
            assert not virtual.pad.held, "buff buttons must never overlap"
    finally:
        time.sleep = old_sleep
    downs = [call[1] for call in virtual.pad.calls if call[0] == "down"]
    ups = [call[1] for call in virtual.pad.calls if call[0] == "up"]
    sleeps = [call[1] for call in virtual.pad.calls if call[0] == "sleep"]
    assert downs == list(BUFF_SEQUENCE) and ups == list(BUFF_SEQUENCE)
    assert sleeps == [BUFF_HOLD_S] * len(BUFF_SEQUENCE)

    virtual.pad.calls.clear()
    time.sleep = lambda _seconds: (_ for _ in ()).throw(RuntimeError("buff failed"))
    try:
        try:
            tap_buff(virtual, "x", BUFF_HOLD_S)
        except RuntimeError:
            pass
        virtual.close()
    finally:
        time.sleep = old_sleep
    assert not virtual.pad.held and ("up", "x") in virtual.pad.calls
    assert ("reset",) in virtual.pad.calls, virtual.pad.calls

    img = np.zeros((200, 200, 3), np.uint8)
    cv2.circle(img, (150, 60), 4, (0, 0, 255), -1)   # far, up-right
    cv2.circle(img, (120, 100), 4, (0, 0, 255), -1)  # near, right
    cv2.circle(img, (40, 40), 4, (255, 0, 0), -1)    # blue, must be ignored
    # Mushroom art, measured off a real minimap: desaturated pink, and an occluded
    # cap is a dot-sized sliver -- only the colour tells these from a monster.
    cv2.circle(img, (60, 150), 20, (82, 93, 176), -1)   # whole cap
    cv2.circle(img, (30, 60), 4, (82, 93, 176), -1)     # sliver of one
    cv2.circle(img, (150, 170), 4, (0, 0, 255), -1)  # two monsters touching -> one
    cv2.circle(img, (157, 170), 4, (0, 0, 255), -1)  # fat contour, must still count

    cv2.circle(img, (105, 95), 6, (255, 255, 255), -1)  # player arrow, near centre
    cv2.circle(img, (10, 190), 9, (255, 255, 255), -1)  # other white UI, farther off

    dots = find_red_dots(img)
    assert len(dots) == 3, dots       # 2 singles + the merged pair, no mushrooms
    x, y, _ = nearest(dots, 100, 100)
    assert abs(x - 120) < 3 and abs(y - 100) < 3, (x, y)

    # pick_target: origin is the box centre, whatever colour the marker is, and a
    # dot sitting on it is never the target.
    cv2.circle(img, (104, 104), 4, (0, 0, 255), -1)    # right on the centre
    (px, py), targetable, chosen = pick_target(img)
    assert (px, py) == (100, 100), (px, py)            # 200x200 image
    assert all((d[0] - px) ** 2 + (d[1] - py) ** 2 > CONCEAL_PX ** 2
               for d in targetable), targetable
    # The dot on the centre is concealed and dropped; the next nearest is chased.
    assert abs(chosen[0] - 120) < 3 and abs(chosen[1] - 100) < 3, chosen
    assert all(d[0] != 104 for d in targetable), targetable

    # Another player's pet uses the same red dot as a monster, but stays beside
    # that player's small white dot. Confirm the pair over several frames so a
    # monster merely crossing a player is not discarded immediately.
    pet_img = np.zeros((200, 200, 3), np.uint8)
    cv2.circle(pet_img, (50, 80), 4, (255, 255, 255), -1)  # other player
    cv2.circle(pet_img, (62, 80), 4, (0, 0, 255), -1)      # their pet
    cv2.circle(pet_img, (150, 80), 4, (0, 0, 255), -1)     # real monster
    assert len(find_white_players(pet_img)) == 1
    pets = PetFilter()
    for _ in range(PET_CONFIRM_FRAMES - 1):
        _, before, chosen = pick_target(pet_img, pets)
        assert len(before) == 2 and chosen[0] < 100, (before, chosen)
    _, after, chosen = pick_target(pet_img, pets)
    assert len(after) == 1 and chosen[0] > 100, (after, chosen)

    far_pet_img = np.zeros((200, 200, 3), np.uint8)
    cv2.circle(far_pet_img, (50, 80), 4, (255, 255, 255), -1)
    cv2.circle(far_pet_img, (80, 80), 4, (0, 0, 255), -1)   # pet moved 30px away
    cv2.circle(far_pet_img, (150, 80), 4, (0, 0, 255), -1)
    _, far_after, chosen = pick_target(far_pet_img, pets)
    assert len(far_after) == 1 and chosen[0] > 100, (far_after, chosen)

    sx, sy = stick_vector(x - 100, y - 100)
    assert sx > 0.95 and abs(sy) < 0.05, (sx, sy)      # push right, full tilt
    sx, sy = stick_vector(0, -50)
    assert sy > 0.95 and abs(sx) < 0.05, (sx, sy)      # up = +y
    sx, sy = stick_vector(3, -4)                  # near target, still full
    assert abs((sx * sx + sy * sy) ** 0.5 - 1.0) < 0.01, (sx, sy)

    # Login screens. Built at an odd size on purpose: every coordinate is a
    # fraction, so the flow must work at a resolution nobody measured.
    def blue(img, btn):
        h, w = img.shape[:2]
        bw, bh = int(w * btn[2]), int(h * 0.036)   # each button at its real width
        x, y = int(w * btn[0]), int(h * btn[1])
        cv2.rectangle(img, (x - bw // 2, y - bh // 2), (x + bw // 2, y + bh // 2),
                      (232, 168, 79), -1)          # the game's button blue

    sky = np.zeros((432, 768, 3), np.uint8)
    sky[:] = (200, 150, 90)                        # bright blue sky, and
    blue(sky, PLAY_BTN)                            # blue skill icons below
    blue(sky, OK_BTN)                              # a blue thing up in the sky
    assert login_screen(sky) is None, "gameplay must never look like a login screen"

    disc = np.zeros((432, 768, 3), np.uint8)
    disc[:] = (255, 255, 255)                      # server table behind the modal
    cv2.rectangle(disc, (int(768 * 0.40), int(432 * 0.06)),
                  (int(768 * 0.60), int(432 * 0.18)), (69, 51, 49), -1)
    blue(disc, OK_BTN)
    blue(disc, CONNECT_BTN)                        # both are on screen at once
    assert login_screen(disc) == "disconnected", "the modal must be handled first"

    # The idle disconnect uses the same modal and Ok button as an ordinary
    # disconnect, so its message is the only reliable discriminator. Match that
    # stable text crop while leaving the generic reconnect trigger intact.
    idle_text = np.zeros((38, 282, 3), np.uint8)
    cv2.putText(idle_text, "idle disconnect", (4, 27), cv2.FONT_HERSHEY_SIMPLEX,
                0.7, (255, 255, 255), 2, cv2.LINE_AA)
    idle = disc.copy()
    idle_small = cv2.resize(idle_text, None, fx=768 / 1911, fy=768 / 1911,
                            interpolation=cv2.INTER_AREA)
    idle_y, idle_x = int(432 * 0.0885), int(768 * 0.425)
    idle[idle_y:idle_y + idle_small.shape[0],
         idle_x:idle_x + idle_small.shape[1]] = idle_small
    assert login_screen(idle, idle_template=idle_text) == "idle disconnected"
    assert login_screen(disc, idle_template=idle_text) == "disconnected"

    srv = np.zeros((432, 768, 3), np.uint8)
    srv[:] = (255, 255, 255)
    blue(srv, CONNECT_BTN)
    assert login_screen(srv) == "server"

    # The server list is sorted by ping and reorders between sessions, so SEA has
    # to be found by its label. Drop a fake label into an arbitrary row and check
    # the click follows it there rather than going to a fixed position.
    label = np.zeros((13, 96, 3), np.uint8)
    cv2.putText(label, "SEA", (2, 11), cv2.FONT_HERSHEY_SIMPLEX, 0.4,
                (60, 55, 55), 1, cv2.LINE_AA)
    scale = 768 / SEA_REF_W
    small = cv2.resize(label, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    row_y, row_x = 300, 250                      # third row down, nothing special
    srv[row_y:row_y + small.shape[0], row_x:row_x + small.shape[1]] = small
    found = find_sea_row(srv, label)
    assert found is not None, "SEA label must be found wherever the row sits"
    assert abs(found[1] - (row_y + small.shape[0] / 2)) < 3, found

    chars = np.zeros((432, 768, 3), np.uint8)
    chars[:] = (53, 36, 28)                        # the dark character backdrop
    cv2.circle(chars, (int(768 * 0.42), int(432 * 0.60)), 40, (250, 250, 250), -1)
    # The character stands in the middle of this screen holding whatever it
    # holds. A probe there is not a backdrop probe -- this is the frame that
    # kept the bot parked on the character screen.
    cv2.rectangle(chars, (int(768 * 0.20), int(432 * 0.25)),
                  (int(768 * 0.62), int(432 * 0.80)), (255, 255, 167), -1)
    blue(chars, PLAY_BTN)
    # Regression, found the hard way on a live disconnect: Connect and Play sit
    # 0.033 apart vertically, and the character's own bright armour can sit under
    # the white-panel probe, so this screen read as "server" and the flow stalled.
    assert login_screen(chars) == "character", "Play must not be taken for Connect"
    assert find_blue_button(chars, CONNECT_BTN) is None, "widths must separate them"

    # The clicks themselves: right buttons, right order, window offset applied.
    class FakeWin:
        left, top, width, height = 100, 50, 768, 432

    clicks = []
    def rec(x, y):
        clicks.append((x, y))

    assert reconnect_step(disc, FakeWin, rec, settle=0) == "disconnected"
    assert clicks == [(100 + 768 * OK_BTN[0], 50 + 432 * OK_BTN[1])], clicks
    clicks.clear()
    assert reconnect_step(idle, FakeWin, rec, settle=0,
                          idle_template=idle_text) == "idle disconnected"
    assert clicks == [(100 + 768 * OK_BTN[0], 50 + 432 * OK_BTN[1])], clicks
    clicks.clear()
    assert reconnect_step(srv, FakeWin, rec, settle=0, sea_template=label) == "server"
    assert clicks == [(100 + found[0], 50 + found[1]),
                      (100 + 768 * CONNECT_BTN[0], 50 + 432 * CONNECT_BTN[1])], clicks

    # No SEA row means no click at all: joining whichever region happens to sit in
    # that row today is worse than sitting on the screen and saying so.
    blank = srv.copy()
    blank[row_y:row_y + small.shape[0], row_x:row_x + small.shape[1]] = 255
    clicks.clear()
    assert reconnect_step(blank, FakeWin, rec, settle=0,
                          sea_template=label) == "server: SEA row not found"
    assert clicks == [], clicks
    clicks.clear()
    assert reconnect_step(chars, FakeWin, rec, settle=0) == "character"
    assert clicks == [(100 + 768 * PLAY_BTN[0], 50 + 432 * PLAY_BTN[1])], clicks
    clicks.clear()
    assert reconnect_step(sky, FakeWin, rec, settle=0) is None
    assert clicks == [], "gameplay must never move the mouse"

    # Reconnect orchestration permits one action per observed stage. An unchanged
    # popup or server screen is a wait, not permission to click every poll.
    flow = ReconnectFlow(stage_timeout=10.0, max_attempts=2)
    action, events, reset = flow.observe("disconnected", 0.0)
    assert action == "disconnected" and flow.active and not reset
    assert "[Reconnect] Attempt 1: dismiss disconnect popup" in events
    assert flow.observe("disconnected", 1.0)[0] is None
    assert flow.observe("server", 2.0)[0] == "server"
    assert flow.observe("character", 3.0)[0] == "character"
    assert flow.observe(None, 4.0, player_valid=False)[2]
    assert flow.observe(None, 5.0, player_valid=True)[1] == [
        "[Reconnect] Recovery successful; automation resumed"]
    assert not flow.active and not flow.failed

    idle_flow = ReconnectFlow(stage_timeout=10.0, max_attempts=2)
    action, events, reset = idle_flow.observe("idle disconnected", 0.0)
    assert action == "idle disconnected" and not reset
    assert "[Reconnect] Idle disconnection popup detected" in events
    assert idle_flow.observe("idle disconnected", 1.0)[0] is None
    assert idle_flow.observe("disconnected", 2.0)[0] is None
    assert idle_flow.observe("idle disconnected", 9.9)[0] is None
    assert idle_flow.observe("idle disconnected", 10.0)[0] == "idle disconnected"
    action, events, _ = idle_flow.observe("server", 11.0)
    assert action == "server"
    assert "[Reconnect] Idle popup dismissed" in events
    assert "[Reconnect] Server screen ready" in events
    assert idle_flow.observe("server", 12.0)[0] is None
    assert idle_flow.observe("character", 13.0)[0] == "character"
    action, events, reset = idle_flow.observe(None, 14.0, player_valid=False)
    assert action is None and reset and idle_flow.active
    assert "[Reconnect] Waiting for valid player data" in events
    assert idle_flow.observe(None, 15.0, player_valid=False) == (None, [], False)
    action, events, reset = idle_flow.observe(None, 16.0, player_valid=True)
    assert action is None and not reset and not idle_flow.active
    assert events == ["[Reconnect] Recovery successful; automation resumed"]

    class _ReconnectEyes:
        def __init__(self):
            self.lock = threading.Lock()
            self.generation, self.owner = 3, None

        def _positions(self, addrs):
            return {a: (4.0, 0.0, 5.0) for a in addrs}

    ready_eyes = _ReconnectEyes()
    assert not reconnect_player_valid(ready_eyes)
    ready_eyes.owner = 0x1000
    assert reconnect_player_valid(ready_eyes)

    # Server and loading waits share the existing bounded retry ceiling. A stage
    # retries only after its timeout and remains neutral between attempts.
    timeout_flow = ReconnectFlow(stage_timeout=5.0, max_attempts=2,
                                 random_wait=lambda: 5.0)
    assert timeout_flow.observe("server", 0.0)[0] == "server"
    assert timeout_flow.observe("server", 4.9)[0] is None
    assert timeout_flow.observe("server", 5.0)[0] == "server"
    assert timeout_flow.observe("server", 10.0)[0] is None
    assert timeout_flow.failed and not timeout_flow.active

    blank_server = ReconnectFlow(stage_timeout=5.0, max_attempts=2,
                                 random_wait=lambda: 5.0)
    assert blank_server.observe("server", 0.0)[0] == "server"
    assert blank_server.observe(None, 5.0)[0] is None
    assert blank_server.observe(None, 10.0)[0] is None
    assert blank_server.failed and not blank_server.active

    loading_flow = ReconnectFlow(stage_timeout=5.0, max_attempts=2,
                                 random_wait=lambda: 5.0)
    assert loading_flow.observe("character", 0.0)[0] == "character"
    assert loading_flow.observe(None, 1.0, player_valid=False)[2]
    assert loading_flow.observe(None, 6.0, player_valid=False)[0] is None
    assert loading_flow.active and not loading_flow.failed
    assert loading_flow.observe(None, 11.0, player_valid=False)[0] is None
    assert loading_flow.failed and not loading_flow.active

    loading_retry = ReconnectFlow(stage_timeout=5.0, max_attempts=2)
    assert loading_retry.observe("character", 0.0)[0] == "character"
    assert loading_retry.observe(None, 1.0, player_valid=False)[2]
    assert loading_retry.observe("character", 2.0, player_valid=False)[0] == "character"

    # Attempt 1 is immediate, attempt 2 follows the existing short wait, and
    # every later retry gets a freshly drawn 5..30 second delay. Seeing the same
    # popup during a pending wait must never create another action or timer.
    retry_draws = iter((7.0, 23.0))
    drawn = []
    def retry_delay():
        delay = next(retry_draws)
        drawn.append(delay)
        return delay

    retry_flow = ReconnectFlow(stage_timeout=2.0, max_attempts=4,
                               random_wait=retry_delay)
    action, events, _ = retry_flow.observe("disconnected", 0.0)
    assert action == "disconnected"
    assert "[Reconnect] Current screen: disconnect popup" in events
    assert "[Reconnect] Attempt 1: dismiss disconnect popup" in events
    assert retry_flow.observe("disconnected", 1.0)[0] is None
    action, events, _ = retry_flow.observe("disconnected", 2.0)
    assert action == "disconnected" and drawn == [7.0]
    assert "[Reconnect] Attempt 1 failed: disconnect popup still visible" in events
    assert "[Reconnect] Attempt 2: dismiss disconnect popup" in events
    assert any("[Reconnect] Retry attempt 3 in 7.0s" in e for e in events)
    assert retry_flow.observe("disconnected", 8.9)[0] is None
    action, events, _ = retry_flow.observe("disconnected", 9.0)
    assert action == "disconnected" and drawn == [7.0, 23.0]
    assert "[Reconnect] Attempt 2 failed: disconnect popup still visible" in events
    assert "[Reconnect] Attempt 3: dismiss disconnect popup" in events
    assert any("[Reconnect] Retry attempt 4 in 23.0s" in e for e in events)

    failed_clicks = []
    failed_click_flow = ReconnectFlow(stage_timeout=2.0, max_attempts=3,
                                      random_wait=lambda: 5.0)
    for retry_now in (0.0, 1.0, 2.0):
        action = failed_click_flow.observe("disconnected", retry_now)[0]
        if action is not None:
            reconnect_step(disc, FakeWin,
                           lambda x, y: failed_clicks.append((x, y)), settle=0)
    assert len(failed_clicks) == 2, failed_clicks

    completed_flow = ReconnectFlow(stage_timeout=2.0, max_attempts=4,
                                   random_wait=lambda: 5.0)
    assert completed_flow.observe("server", 0.0)[0] == "server"
    completed_flow.action_completed(0.0, 3.0)
    assert completed_flow.observe("server", 4.9)[0] is None
    assert completed_flow.observe("server", 5.0)[0] == "server"
    completed_flow.action_completed(5.0, 8.0)
    assert completed_flow.observe("server", 12.9)[0] is None
    assert completed_flow.observe("server", 13.0)[0] == "server"

    # A Connect click is verified by screen progress, not by the click call.
    # If server lag leaves the same screen up, retry it on the same schedule.
    server_draws = iter((5.0,))
    server_lag = ReconnectFlow(stage_timeout=2.0, max_attempts=3,
                               random_wait=lambda: next(server_draws))
    assert server_lag.observe("server", 0.0)[0] == "server"
    assert server_lag.observe("server", 1.0)[0] is None
    action, events, _ = server_lag.observe("server", 2.0)
    assert action == "server"
    assert "[Reconnect] Attempt 1 failed: server-selection screen still visible" in events
    assert server_lag.observe("server", 6.9)[0] is None
    assert server_lag.observe("server", 7.0)[0] == "server"

    # Loading never authorizes a click. A valid player read succeeds immediately
    # and cancels the pending randomized retry deadline.
    valid_draws = iter((30.0,))
    valid_flow = ReconnectFlow(stage_timeout=2.0, max_attempts=4,
                               random_wait=lambda: next(valid_draws))
    assert valid_flow.observe("character", 0.0)[0] == "character"
    assert valid_flow.observe(None, 1.0, player_valid=False)[2]
    action, events, _ = valid_flow.observe(None, 3.0, player_valid=False)
    assert action is None
    assert "[Reconnect] Attempt 1 failed: valid player data not ready" in events
    assert any("[Reconnect] Retry attempt 3 in 30.0s" in e for e in events)
    action, events, _ = valid_flow.observe(None, 4.0, player_valid=True)
    assert action is None and events == [
        "[Reconnect] Recovery successful; automation resumed"]
    assert not valid_flow.active and valid_flow.deadline == 0.0

    cancelled = ReconnectFlow(stage_timeout=2.0, max_attempts=4,
                              random_wait=lambda: 5.0)
    cancelled.observe("idle disconnected", 0.0)
    assert cancelled.cancel()
    assert not cancelled.active and cancelled.stage is None
    assert cancelled.attempts == 0 and cancelled.deadline == 0.0
    assert cancelled.observe(None, 99.0) == (None, [], False)
    assert RECONNECT_RETRY_MIN_S == 5.0 and RECONNECT_RETRY_MAX_S == 30.0
    assert all(RECONNECT_RETRY_MIN_S <= ReconnectFlow().random_wait()
               <= RECONNECT_RETRY_MAX_S for _ in range(100))

    # ArduinoPad wire format, no board attached
    pad = ArduinoPad.__new__(ArduinoPad)
    sent = []
    pad.ser = type("S", (), {"write": lambda _, b: sent.append(b),
                             "readline": lambda _: b"OK"})()
    pad.last = None
    pad.stick(0.0, 1.0)             # stick up -> HID Y negative
    pad.stick(0.0, 1.0)             # repeat must not re-send
    pad.stick(-1.0, 0.0, True)      # move + both attack buttons down
    pad.stick(-1.0, 0.0, False)     # stick unchanged -> only both releases
    pad.stick(0.0, 0.0, False)      # target loss -> reset every control
    pad.stick(0.0, 0.0, False)      # repeated release is safe and deduplicated
    assert sent == [b"L0,-32767\n", b"U4\n", b"U5\n",
                    b"L-32767,0\n", b"D4\n", b"D5\n",
                    b"U4\n", b"U5\n", b"Z\n"], sent

    sent.clear()
    for key in BUFF_SEQUENCE:
        tap_buff(pad, key, 0)
    assert sent == [b"V0\n", b"V-1\n", b"V4\n", b"V-1\n",
                    b"V6\n", b"V-1\n", b"V2\n", b"V-1\n",
                    b"D2\n", b"U2\n", b"D0\n", b"U0\n"], sent

    sent.clear()
    pad.tap_button("y")           # by name
    pad.tap_button(3)             # same button by index
    assert sent == [b"B3\n", b"B3\n"], sent

    # --- which target source runs -----------------------------------------
    # Memory is the default and pixels are its live fallback. --minimap is the
    # explicit opt-out for a run that must never inspect the unit list.
    assert targeting_mode([]) == "memory"
    assert targeting_mode(["minimap_bot.py"]) == "memory"
    assert targeting_mode(["--memory"]) == "memory"
    assert targeting_mode(["--minimap"]) == "minimap"
    memory_enabled = MEMORY_TARGETING
    try:
        MEMORY_TARGETING = False
        assert targeting_mode([]) == "minimap"
    finally:
        MEMORY_TARGETING = memory_enabled
    # An area is world coordinates, which the screen cannot supply, so asking
    # for one asks for the memory path.
    assert targeting_mode([], area="lunaris") == "memory"
    # ...but saying --minimap out loud is never silently overridden.
    assert targeting_mode(["--minimap"], area="lunaris") == "minimap"
    assert targeting_mode(["--memory", "--minimap"]) == "minimap"

    # --- farming areas ---------------------------------------------------
    # The brush is wide on purpose: one walk down the middle of a field should
    # cover it, so a painted point reaches well past where the character stood.
    a = Area("demo", path=os.devnull)
    a.paint(0.0, 0.0)
    assert a.inside(0.0, 0.0)
    assert a.inside(AREA_BRUSH - AREA_CELL, 0.0), "the brush is wide, not a point"
    assert not a.inside(AREA_BRUSH * 3, 0.0)
    # Target admission is exact: a monster one step over the line is rejected.
    out = AREA_BRUSH + AREA_CELL
    assert not a.inside(out, 0.0)

    # A walked line -- the shape the recorder actually produces.
    walked = Area("walk", path=os.devnull)
    for i in range(20):
        walked.paint(i * 2.0, 0.0)
    assert walked.core and walked.core <= walked.cells
    # Crossing back over the line is not being back in. Without this gap a
    # monster parked on the boundary has the bot stepping in and out forever.
    assert walked.deep(20.0, 0.0), "the middle of the strip is deep"
    fringe = next(walked.centre(c) for c in sorted(walked.cells)
                  if c not in walked.core)
    assert walked.inside(*fringe) and not walked.deep(*fringe), fringe
    # home() must land somewhere deep, or arriving never clears the walk-back.
    assert walked.deep(*walked.home(20.0, 60.0))
    # spot() may only ever offer ground it is safe to stand in.
    rng = random.Random(7)
    for _ in range(50):
        assert walked.deep(*walked.spot(rng))
    # A recording too thin to have a middle must degrade, not hang the bot
    # walking back in to somewhere that is never deep enough.
    thin = Area("thin", path=os.devnull, cell=AREA_BRUSH * 4)
    thin.paint(0.0, 0.0)
    assert thin.cells, "the cell we stand in is painted whatever the brush"
    assert thin.core, "no core means no hysteresis, not no bot"

    # Persistence: WalkMap's rules, plus one -- saving one area keeps the rest.
    tmp = os.path.join(os.environ.get("TEMP", "."), "areas_demo.json")
    walked.path = tmp
    assert walked.save()
    two = Area("second", path=tmp)
    two.paint(500.0, 500.0)
    assert two.save()
    back = Area("walk", path=tmp).load()
    assert back.cells == walked.cells, (len(back.cells), len(walked.cells))
    assert back.core == walked.core
    assert Area("second", path=tmp).load().cells, "one save must not eat the other"
    assert Area.names(tmp) == ["second", "walk"], Area.names(tmp)
    assert not Area("missing", path=tmp).load().cells
    assert not Area("walk", path=tmp, cell=AREA_CELL * 2).load().cells,         "a file at another cell size is dropped, never rescaled"
    os.remove(tmp)

    # --- the walk map ----------------------------------------------------
    # One sighting is not a wall: another player standing in a doorway would
    # otherwise seal it permanently.
    wm = WalkMap(path=os.devnull)
    wm.block(10.0, 10.0, 100.0)
    assert not wm.blocked(wm.at(10.0, 10.0), 100.0), "one sighting is not a wall"
    wm.block(10.0, 10.0, 100.0)
    assert wm.blocked(wm.at(10.0, 10.0), 100.0)
    assert not wm.blocked(wm.at(10.0, 10.0), 100.0 + WALK_DECAY_S + 1), "must decay"
    # Standing in a cell proves it walkable, whatever we believed before.
    wm.free(10.0, 10.0)
    assert not wm.blocked(wm.at(10.0, 10.0), 100.0), "standing there clears it"

    # A repeated position is the feed hiccuping, not a wall. Measured at 20 Hz,
    # runs of 3 to 26 identical reads happen while walking normally, so neither
    # counting frames nor timing the repeat can mean anything.
    ident = ((1.0, 0.0), (0.0, 1.0))      # basis: stick x -> world x
    feed = WalkMap(path=os.devnull)
    for i in range(WALK_BLOCK_FRAMES * 3):
        got = feed.observe(200.0 + i * 0.01, 5.0, 5.0, 1.0, 0.0, ident,
                           "chasing", goal=(50.0, 5.0))
        assert got is None, f"stalled feed read as a wall on frame {i}"
    # The long stall that broke the first fix: 26 frames of the same value while
    # the character was walking the whole time. It ends with the position where
    # the walking actually got to, and that must not read as a wall.
    stall = WalkMap(path=os.devnull)
    stall.observe(500.0, 5.0, 5.0, 1.0, 0.0, ident, "chasing", goal=(90.0, 5.0))
    for i in range(26):
        assert stall.observe(500.0 + i * 0.08, 5.0, 5.0, 1.0, 0.0, ident,
                             "chasing", goal=(90.0, 5.0)) is None, "stall is not a wall"
    assert stall.observe(500.0 + 26 * 0.08, 40.0, 5.0, 1.0, 0.0, ident,
                         "chasing", goal=(90.0, 5.0)) is None, "the catch-up is not a wall"

    # A wall across x = 0, from z = -9 to z = 9, with the map open beyond it.
    wall = WalkMap(path=os.devnull)
    for i in range(-6, 7):
        wall.block(0.0, i * WALK_CELL, 100.0)
        wall.block(0.0, i * WALK_CELL, 100.0)
    here, there = (-6.0, 0.0), (6.0, 0.0)
    assert wall.crossed(here[0], here[1], there[0], there[1], 100.0), "wall is on the line"
    assert not wall.crossed(-6.0, 30.0, 6.0, 30.0, 100.0), "clear line is clear"
    path = wall.route(here[0], here[1], there[0], there[1], 100.0)
    assert path, "a wall with open ends must be walkable round"
    assert not any(wall.blocked(wall.at(x, z), 100.0) for x, z in path), path
    walked = sum(math.hypot(b[0] - a[0], b[1] - a[1])
                 for a, b in zip([here] + path, path))
    assert walked > math.hypot(there[0] - here[0], there[1] - here[1]), "must go round"
    wp = wall.waypoint(here[0], here[1], path)
    assert math.hypot(wp[0] - here[0], wp[1] - here[1]) >= WALK_WAYPOINT or wp == path[-1]

    # Sealed in: no route exists, and the caller must still be handed the
    # target rather than nothing -- main() reads a zero stick as "handled".
    box = WalkMap(path=os.devnull)
    for i in range(-3, 4):
        for c in ((i, -3), (i, 3), (-3, i), (3, i)):
            box.hits[c] = [WALK_BLOCK_HITS, 100.0]
    assert box.route(0.0, 0.0, 40.0, 0.0, 100.0) is None, "sealed means no route"

    # A wall whose ends are outside the corridor is the same answer: no route,
    # walk straight. Searching further is what cost 9.7 ms of a 50 ms frame.
    long_wall = WalkMap(path=os.devnull)
    for i in range(-(WALK_PAD + 20), WALK_PAD + 21):
        long_wall.hits[(0, i)] = [WALK_BLOCK_HITS, 100.0]
    assert long_wall.route(-6.0, 0.0, 6.0, 0.0, 100.0) is None, "corridor is bounded"

    class FakeEyes(MemoryEyes):
        def __init__(self, wmap):
            self.walk, self.path, self.path_at = wmap, None, 0.0
            self.path_to, self.routing = None, False
    eyes = FakeEyes(WalkMap(path=os.devnull))
    assert eyes.route_to(100.0, -6.0, 0.0, 6.0, 0.0) == (6.0, 0.0), "clean map goes straight"
    assert not eyes.routing
    eyes.walk = wall
    assert eyes.route_to(100.0, -6.0, 0.0, 6.0, 0.0) != (6.0, 0.0), "known wall routes"
    assert eyes.routing
    eyes.walk = box
    assert eyes.route_to(100.0, 0.0, 0.0, 40.0, 0.0) == (40.0, 0.0), "no route walks straight"
    assert not eyes.routing

    # A route must be handed back for a cap or a corridor bound, and only a
    # genuine dead end may drop the monster.
    assert box.capped is False, "sealed in is not a budget problem"
    assert long_wall.capped is True, "the corridor stopped that one, not a wall"
    eyes.walk = box
    eyes.route_to(100.0, 0.0, 0.0, 40.0, 0.0)
    assert eyes.sealed, "a walled-off goal must be reported"
    eyes.walk = long_wall
    eyes.route_to(100.0, -6.0, 0.0, 6.0, 0.0)
    assert not eyes.sealed, "running out of corridor is not proof of a wall"

    # Learning a wall. The old test was travel *speed*, and it almost never
    # fired: pushed into a wall the game slides the character along it at full
    # pace. Progress along the push is what a wall actually takes away.
    ident = ((1.0, 0.0), (0.0, 1.0))
    learn = WalkMap(path=os.devnull)
    # Frames alone will not do it any more: a position that never changes is
    # what the feed looks like while hiccuping, and it hiccups for up to 8
    # frames on its own. Only a window that gains no ground means jammed.
    hit = None
    for i in range(int((WALK_JAM_S + 0.3) / 0.05)):
        # it fires once and then restarts its clock, so keep the first answer
        hit = learn.observe(100.0 + i * 0.05, 5.0, 5.0, 1.0, 0.0,
                            ident, "chasing") or hit
    assert hit, "a push that goes nowhere for long enough must mark"
    assert learn.at(5.0 + WALK_BLOCK_AHEAD, 5.0) in learn.hits, learn.hits
    assert learn.wedged, "a jam must ask for the sideways escape"

    # The regression: sliding sideways at full speed while pushing east. Travel
    # is 0.7 a frame -- the speed test saw a bot walking happily -- and progress
    # east is zero.
    slide = WalkMap(path=os.devnull)
    hit = None
    for i in range(WALK_BLOCK_FRAMES + 1):
        hit = slide.observe(100.0 + i * 0.05, 5.0, 5.0 + i * 0.7,
                            1.0, 0.0, ident, "chasing")
    assert hit, "a sideways slide must mark"
    assert slide.at(5.0 + WALK_BLOCK_AHEAD,
                    5.0 + WALK_BLOCK_FRAMES * 0.7) in slide.hits, slide.hits

    # Real headway is never a wall, whether straight on or at an angle.
    for step in ((0.7, 0.0), (0.5, 0.5)):
        run = WalkMap(path=os.devnull)
        for i in range(WALK_BLOCK_FRAMES + 3):
            assert run.observe(100.0 + i * 0.05, 5.0 + i * step[0],
                               5.0 + i * step[1], 1.0, 0.0,
                               ident, "chasing") is None, step
        assert not run.hits, step

    orbit = WalkMap(path=os.devnull)
    for i in range(WALK_BLOCK_FRAMES + 3):
        assert orbit.observe(100.0 + i * 0.05, 5.0, 5.0, 1.0, 0.0,
                             ident, "on it") is None, "the orbit stands still on purpose"
    assert not orbit.hits

    # The slow sensor. A shallow slide keeps full speed and a healthy push
    # projection -- measured 0.41 at 45 degrees against a limit of 0.25 -- so
    # only distance to the goal can say it is not working. `creep` walks a
    # window's worth of frames and returns the last thing observe() said.
    def creep(wm, steps, at, goal, push=(1.0, 0.0), seconds=WALK_STUCK_S + 0.2):
        """Walk a window's worth of frames; give back every cell marked."""
        out = []
        for i in range(steps + 1):
            got = wm.observe(100.0 + i * (seconds / steps), *at(i),
                             push[0], push[1], ident, "chasing", goal(i))
            if got:
                out.append(got)
        return out

    # Sliding north along a wall while the goal sits due east: full speed, no
    # approach at all. This is the case a 45-degree push produces and the fast
    # sensor cannot see.
    slid = WalkMap(path=os.devnull)
    marked = creep(slid, 30, lambda i: (5.0, 5.0 + i * 0.4),
                   lambda i: (40.0, 5.0), push=(0.7, 0.7))
    assert marked, "a slide that never arrives must be marked"
    # Marked toward the goal, which is east of the track we slid along.
    assert max(c[0] for c in slid.hits) > slid.at(5.0, 0.0)[0], slid.hits

    # A slide that *is* rounding the obstacle closes on the goal, and must
    # never be marked however long it runs.
    ok = WalkMap(path=os.devnull)
    assert not creep(ok, 30, lambda i: (5.0 + i * 0.4, 5.0),
                     lambda i: (40.0, 5.0))
    assert not ok.hits, ok.hits

    # A monster running away while we chase it at full speed: both distances
    # are measured against where it is now, so we are still closing on it.
    flee = WalkMap(path=os.devnull)
    assert not creep(flee, 30, lambda i: (5.0 + i * 0.4, 5.0),
                     lambda i: (40.0 + i * 0.2, 5.0))
    assert not flee.hits, flee.hits

    # Switching target restarts the window rather than judging across it --
    # otherwise the monster we have just turned towards gets a wall in front.
    # (walking at full speed throughout, or the fast sensor would fire first
    # and the slow one would never be reached)
    swap = WalkMap(path=os.devnull)
    assert not creep(swap, 30, lambda i: (5.0 + i * 0.4, 5.0),
                     lambda i: (40.0, 5.0) if i < 15 else (-40.0, 5.0))
    assert not swap.hits, swap.hits

    # Nothing may be marked before the window is up, and a mode that is not
    # walking, or a frame with no stick, throws the window away.
    early = WalkMap(path=os.devnull)
    assert not creep(early, 8, lambda i: (5.0, 5.0 + i * 0.4),
                     lambda i: (40.0, 5.0), push=(0.7, 0.7),
                     seconds=WALK_STUCK_S * 0.5)
    assert not early.hits, "the window was not up yet"
    early.observe(200.0, 5.0, 5.0, 0.0, 0.0, ident, "chasing", (40.0, 5.0))
    assert early.mark is None, "no stick means no window"
    early.mark = (0.0, 0.0, 0.0, (1.0, 1.0))
    early.observe(200.0, 5.0, 5.0, 1.0, 0.0, ident, "on it", (40.0, 5.0))
    assert early.mark is None, "not walking means no window"
    early.mark = (0.0, 0.0, 0.0, (1.0, 1.0))
    early.forget_walk()
    assert early.mark is None, "a rebuilt unit throws the window away"

    # Wedged. Measured live: pushing a rock the character never moved, so only
    # one cell could ever be learned, the route came back unchanged and the bot
    # pushed the same heading for minutes. Two answers, both needed. A fan of
    # cells, so the route cannot sidestep the obstacle by one cell...
    fan = WalkMap(path=os.devnull)
    for i in range(int((WALK_JAM_S + 0.3) / 0.05)):
        fan.observe(100.0 + i * 0.05, 5.0, 5.0, 1.0, 0.0, ident, "chasing")
    assert len(fan.hits) >= 4, fan.hits
    assert fan.wedged, "a push that moved us nowhere is a wedge"
    assert all(c != fan.at(5.0, 5.0) for c in fan.hits), "never block our own cell"
    # ...and a sideways shove, because no route can free a jammed character.
    crept = WalkMap(path=os.devnull)
    creep(crept, 30, lambda i: (5.0, 5.0 + i * 0.4),
          lambda i: (40.0, 5.0), push=(0.7, 0.7))
    assert not crept.wedged, "creeping is not a wedge -- we were moving"

    class Wedge(MemoryEyes):
        def __init__(self):
            self.walk, self.basis = WalkMap(path=os.devnull), ident
            self.last_pos, self.goal = (5.0, 5.0), (40.0, 5.0)
            self.mode, self.loot_mode = "chasing", "no loot"
            self.escape_side, self.escapes, self.escape_until = 1, 0, 0.0
            self.escape, self.path = None, None
    stuck = Wedge()
    for i in range(int((WALK_JAM_S + 0.3) / 0.05)):
        stuck.observe_move(100.0 + i * 0.05, 1.0, 0.0)
    assert stuck.escape, "a wedge must produce a way out"
    assert stuck.escape[0] < 0, ("the way out is backwards", stuck.escape)
    assert stuck.escape_until > 100.0 and stuck.escapes == 1
    # Alternating sides, so a corner that beats one way out is tried the other.
    first = stuck.escape
    stuck.wedge_off(200.0, 1.0, 0.0)
    assert (first[1] > 0) != (stuck.escape[1] > 0), (first, stuck.escape)

    # Floor painted from other units' traffic: only those that moved, and it
    # must never erase a wall we measured ourselves.
    traffic = WalkMap(path=os.devnull)
    traffic.block(0.0, 0.0, 100.0)
    traffic.block(0.0, 0.0, 100.0)
    traffic.paint([(0.0, 0.0), (20.0, 20.0)])
    assert traffic.blocked(traffic.at(0.0, 0.0), 100.0), "traffic must not clear a wall"
    assert traffic.at(20.0, 20.0) in traffic.floor
    traffic.free(0.0, 0.0)
    assert not traffic.blocked(traffic.at(0.0, 0.0), 100.0), "our own feet do clear it"

    # 0-1 BFS: with two equally short ways round, the one something has walked
    # in wins.
    pref = WalkMap(path=os.devnull)
    for i in range(-6, 7):
        pref.hits[(0, i)] = [WALK_BLOCK_HITS, 100.0]
    # The northern way round is proven ground; the southern one is unknown.
    pref.floor.update((x, z) for x in range(-8, 9) for z in range(1, 9))
    picked = pref.route(-6.0, 0.0, 6.0, 0.0, 100.0)
    assert picked, "still routable"
    assert min(z for _, z in picked) >= 0, ("must take the floor side", picked)
    assert max(z for _, z in picked) > 0, ("must go round, not through", picked)

    # Saving keeps walls and floor; a file at another cell size cannot be
    # rescaled honestly and is dropped rather than believed.
    tmp = os.path.join(os.environ.get("TEMP", "."), "walkmap_demo.json")
    wall.path = tmp
    wall.paint([(30.0, 30.0)])
    assert wall.save(), "a changed map must write"
    assert not wall.save(), "an unchanged map must not"
    back = WalkMap(path=tmp).load()
    assert back.hits == wall.hits, (len(back.hits), len(wall.hits))
    assert back.floor == wall.floor, (len(back.floor), len(wall.floor))
    assert not WalkMap(path=tmp, cell=WALK_CELL * 2).load().hits, "wrong scale is dropped"
    os.remove(tmp)

    print("demo ok")


def record_area(name):
    """Walk the character round the ground you want farmed; End saves it.

    Reads only our own position. No pad, no calibration, no minimap: the basis
    exists to turn a direction into a stick push and nothing here pushes, and
    the minimap cannot say where in the world anything is anyway.

    Do not run this while the bot itself is running -- both poll End through
    GetAsyncKeyState, whose low bit is consumed by whoever reads it first.
    """
    if not name:
        known = ", ".join(Area.names()) or "(none recorded yet)"
        print("usage: python minimap_bot.py --record <name>")
        print(f"recorded areas: {known}")
        return
    area = Area(name).load()
    if area.circles:
        print(f"area {name!r} is a circle area ({area.describe()});"
              " choose another name or replace it with --place-circles <name>"
              " <radius> --replace")
        return
    if area.cells:
        # Never silently replace ten minutes of hand-walking. Adding is what
        # you want anyway: a big field takes more than one session. Printed
        # before the slow sweep, so there is time to change your mind.
        print(f"area {name!r} already has {len(area.cells)} cells -- this ADDS"
              f" to it.  ctrl+c now to leave it alone.")
    import memscan
    print("finding your character -- the first heap sweep takes ~15 s,"
          " it is not hung", flush=True)
    mem = memscan.Mem()
    units = memscan.world_units(mem)
    me = memscan.local_player(mem, units[0][1]) if units else None
    if not me:
        print("no local player found -- is the character actually in the world?")
        return
    print(f"unit 0x{me:X} -- walk the area now."
          f"   End = save,   ctrl+c = abandon")
    last, misses = None, 0
    try:
        while True:
            blob = mem.read(me + memscan.UNIT_POSITION, 12)
            if blob:
                misses = 0
                x, _, z = struct.unpack("<fff", blob)
                # Only repaint after real travel: the position feed repeats a
                # value for many frames at a time, so counting samples would
                # say nothing about ground covered.
                if last is None or math.hypot(x - last[0], z - last[1]) >= AREA_STEP:
                    area.paint(x, z)
                    last = (x, z)
                print(f"  {len(area.cells):6} cells   at {x:8.1f},{z:8.1f}   ",
                      end=chr(13), flush=True)
            else:
                # A relog or a map change kills the pointer. Say so rather than
                # sit painting the last position for ever.
                misses += 1
                print(f"  position unreadable x{misses} (relog? map change?)  ",
                      end=chr(13), flush=True)
                if misses > MEM_LOST_FRAMES * 4:
                    print(chr(10) + "lost the character -- saving what was recorded")
                    break
            if toggle_key_hit():
                break
            time.sleep(AREA_SAMPLE_S)
    except KeyboardInterrupt:
        print(chr(10) + "abandoned -- nothing written")
        return
    if not area.cells:
        print(chr(10) + "nothing recorded")
        return
    (x0, z0), (x1, z1) = area.bounds()
    ok = area.save()
    print(chr(10) + f"{'saved' if ok else 'COULD NOT SAVE'} {name!r}:"
          f" {len(area.cells)} cells, x {x0:.0f}..{x1:.0f}  z {z0:.0f}..{z1:.0f}")
    print(f"  -> {AREA_FILE}")
    print(f"run it with:  python minimap_bot.py --area {name}")


def record_circle(name, radius, replace=False):
    """Save an exact circular farm area centred on the character's current position."""
    try:
        radius = float(radius)
    except (TypeError, ValueError):
        radius = 0.0
    if not name or not math.isfinite(radius) or radius <= 0.0:
        print("usage: python minimap_bot.py --circle <name> <radius> [--replace]")
        print("  stand at the centre; radius is in world units (try 40 to 60)")
        return
    old = Area(name).load()
    if old.defined and not replace:
        print(f"area {name!r} already exists ({old.describe()}) -- nothing changed")
        print(f"replace it explicitly with: python minimap_bot.py --circle"
              f" {name} {radius:g} --replace")
        return

    import memscan
    print("finding your character -- the first heap sweep takes ~15 s,"
          " it is not hung", flush=True)
    mem = memscan.Mem()
    units = memscan.world_units(mem)
    me = memscan.local_player(mem, units[0][1]) if units else None
    if not me:
        print("no local player found -- is the character actually in the world?")
        return
    blob = mem.read(me + memscan.UNIT_POSITION, 12)
    if not blob:
        print("your position is unreadable -- relog or enter the world, then retry")
        return
    x, _, z = struct.unpack("<fff", blob)
    area = Area(name).set_circle(x, z, radius)
    ok = area.save()
    print(f"{'saved' if ok else 'COULD NOT SAVE'} {name!r}: {area.describe()}")
    print(f"  -> {AREA_FILE}")
    print(f"run it with:  python minimap_bot.py --area {name}")


def snap(path="minimap_snap.png"):
    """Dump what the bot sees: captured region, detections circled, centre marked."""
    import mss
    win = find_window()
    with mss.mss() as sct:
        img = np.array(sct.grab(minimap_region(win)))[:, :, :3].copy()
    h, w = img.shape[:2]
    cx, cy = w // 2, h // 2  # the game pins the character here; see pick_target
    print(f"  player assumed at box centre ({cx},{cy}) -- the cross must sit on the "
          f"character marker, whatever colour it is")
    for x, y, wpx in find_red_dots(img):
        d = ((x - cx) ** 2 + (y - cy) ** 2) ** 0.5
        hid = d <= CONCEAL_PX
        cv2.circle(img, (int(x), int(y)), 8, (0, 0, 255) if hid else (0, 255, 0), 1)
        print(f"  dot at ({x:6.1f},{y:6.1f}) width {wpx:5.1f} dist {d:6.1f}"
              f"{'  REJECTED: under player marker' if hid else ''}")
    cv2.drawMarker(img, (cx, cy), (255, 255, 0), cv2.MARKER_CROSS, 14, 1)
    cv2.circle(img, (cx, cy), CONCEAL_PX, (255, 255, 0), 1)

    cv2.imwrite(path, img)
    print(f"{w}x{h} region -> {path}")


def draw_tracking(img, pet_filter=None, target_lock=None):
    """Annotate a minimap grab in place with what the bot would do. Returns a label.

    green = targetable, orange = confirmed pet, red = ignored under the arrow,
    cyan = chosen target, magenta arrow = the stick vector that would be sent.
    """
    (cx, cy), dots, dot = pick_target(img, pet_filter, target_lock)
    ipx = (int(cx), int(cy))
    for x, y, _ in find_red_dots(img):
        hidden = (x - cx) ** 2 + (y - cy) ** 2 <= CONCEAL_PX ** 2
        kept = any((x - d[0]) ** 2 + (y - d[1]) ** 2 < 1 for d in dots)
        colour = (0, 0, 255) if hidden else ((0, 255, 0) if kept else (0, 165, 255))
        cv2.circle(img, (int(x), int(y)), 7, colour, 1)
    cv2.circle(img, ipx, CONCEAL_PX, (255, 255, 0), 1)
    cv2.drawMarker(img, ipx, (255, 255, 0), cv2.MARKER_CROSS, 12, 1)
    if dot is None:
        state = "target concealed" if target_lock and target_lock.concealed else "no target"
        return f"{len(dots)} dots  {state}"
    tgt = (int(dot[0]), int(dot[1]))
    cv2.line(img, ipx, tgt, (0, 255, 255), 1)
    cv2.circle(img, tgt, 10, (0, 255, 255), 2)
    sx, sy = stick_vector(dot[0] - cx, dot[1] - cy)
    # stick vector as an arrow from the player, y flipped back to screen sense
    cv2.arrowedLine(img, ipx, (int(cx + sx * 30), int(cy - sy * 30)),
                    (255, 0, 255), 2, tipLength=0.3)
    return f"{len(dots)} dots  dist {((dot[0] - cx) ** 2 + (dot[1] - cy) ** 2) ** 0.5:.0f}"


def watch(scale=2):
    """Live view of what the vision layer sees. Read-only -- drives nothing.

    Safe to run in a second terminal while the bot works: two mss grabs of the
    same region do not collide. q or ESC quits.
    """
    import mss
    win = find_window()
    title = "spiritvale tracking -- q to quit"
    pet_filter = PetFilter()
    target_lock = TargetLock()
    print(f"watching {minimap_region(win)}  (q or ESC in the window to quit)")
    last, fps = time.time(), 0.0
    with mss.mss() as sct:
        while True:
            img = np.array(sct.grab(minimap_region(win)))[:, :, :3].copy()
            label = draw_tracking(img, pet_filter, target_lock)
            view = cv2.resize(img, None, fx=scale, fy=scale,
                              interpolation=cv2.INTER_NEAREST)
            now = time.time()
            fps = 0.9 * fps + 0.1 / max(now - last, 1e-6)
            last = now
            cv2.putText(view, f"{label}  {fps:.0f}fps", (6, 16),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.45, (255, 255, 255), 1, cv2.LINE_AA)
            cv2.imshow(title, view)
            if cv2.waitKey(max(1, int(1000 / LOOP_HZ))) in (27, ord("q")):
                break
    cv2.destroyAllWindows()


def relogin(dry=False):
    """Handle whatever login screen is showing, once. --dry looks without clicking."""
    import mss
    win = find_window()
    with mss.mss() as sct:
        img = np.array(sct.grab(window_region(win)))[:, :, :3]
    screen = login_screen(img)
    print(f"screen: {screen or 'not a login screen (gameplay, or something new)'}")
    if screen is None or dry:
        if screen and dry:
            h, w = img.shape[:2]
            if screen == "server":
                sea = find_sea_row(img)
                where = (f"SEA row at ({win.left + sea[0]:.0f},{win.top + sea[1]:.0f})"
                         if sea else "SEA row NOT FOUND -- would not click")
            else:
                f = OK_BTN if screen == "disconnected" else PLAY_BTN
                where = f"({win.left + w * f[0]:.0f},{win.top + h * f[1]:.0f})"
            print(f"  would click {where}")
        return
    print(f"handled: {reconnect_step(img, win)}")


def probe(hold=0.4, gap=2.0):
    """Press every X360 button in turn, naming each. Watch which one buffs."""
    import vgamepad as vg
    pad = vg.VX360Gamepad()
    names = [n for n in dir(vg.XUSB_BUTTON) if n.startswith("XUSB_GAMEPAD_")]
    print(f"focus the game NOW -- 3s, then {len(names)} buttons, {gap}s apart")
    time.sleep(3)
    try:
        for sx, sy in ((0.0, WAKE_AMP), (0.0, -WAKE_AMP), (0.0, 0.0)):
            pad.left_joystick_float(sx, sy)
            pad.update()
            time.sleep(WAKE_STEP_S)
        time.sleep(WAKE_SETTLE_S)
        for n in names:
            b = getattr(vg.XUSB_BUTTON, n)
            print(f"  {n.replace('XUSB_GAMEPAD_', '')}")
            pad.press_button(b)
            pad.update()
            time.sleep(hold)
            pad.release_button(b)
            pad.update()
            time.sleep(gap)
        # Triggers are axes, not buttons -- probe them too.
        for label, setter in (("LEFT_TRIGGER", pad.left_trigger_float),
                              ("RIGHT_TRIGGER", pad.right_trigger_float)):
            print(f"  {label}")
            setter(1.0)
            pad.update()
            time.sleep(hold)
            setter(0.0)
            pad.update()
            time.sleep(gap)
    finally:
        pad.reset()
        pad.update()
    print("done -- tell me which name was on screen when a buff cast")


def buff_test(port=None, hold=BUFF_HOLD_S, gap=BUFF_GAP_S):
    """Fire the buff sequence once so timing can be tuned without a 60s wait."""
    pad = ArduinoPad(port) if port else VirtualPad()
    print(f"focus the game NOW -- 3s, then {' '.join(BUFF_SEQUENCE)} "
          f"(hold {hold}s, gap {gap}s)")
    time.sleep(3)
    try:
        wake_controller(pad)
        for key in BUFF_SEQUENCE:
            print(f"  {key}")
            tap_buff(pad, key, hold)
            time.sleep(gap)
    finally:
        pad.close()
    print("done -- if nothing cast, raise hold/gap")


TAP_HELP = ("button 0-15 or a/b/x/y, d-pad u/d/l/r, stick lx/ly/rx/ry "
            "(prefix - for the other direction, e.g. -lx = left)")
_DIRS = {"u": "up", "d": "down", "l": "left", "r": "right"}


def tap_one(pad, token):
    """Fire one control named the way TAP_HELP describes it. True if understood."""
    token = token.strip().lower()
    sign = -1 if token.startswith("-") else 1
    token = token.lstrip("-+")
    if token in _DIRS:
        pad.tap_dpad(_DIRS[token], 0.2)
    elif token in ("lx", "ly", "rx", "ry"):
        # Full deflection and back. The wizard asks for one direction at a time,
        # so each axis needs both signs: -lx is left, lx is right.
        axis = "L" if token[0] == "l" else "R"
        v = 32767 * sign
        x, y = (v, 0) if token[1] == "x" else (0, v)
        pad._cmd(f"{axis}{x},{y}")
        time.sleep(0.4)
        pad._cmd(f"{axis}0,0")
    elif token in ArduinoPad.FACE:
        pad.tap_button(token)
    elif token.isdigit():
        pad.tap_button(int(token))
    else:
        return False
    return True


def press_repl(port="auto"):
    """Tap one control at a time, on demand.

    Steam has no mapping for an unknown HID pad, so it runs a setup wizard that
    asks you to press each control in turn -- and nothing reaches the game until
    that is done. Nobody can press a button on a board with no buttons, hence
    this: type what the wizard is waiting for. One connection stays open for the
    whole run, so the board never resets mid-wizard.
    """
    pad = ArduinoPad(port)
    print(f"{TAP_HELP}\nblank line quits")
    try:
        while True:
            s = input("> ").strip()
            if not s:
                break
            if not tap_one(pad, s):
                print(f"  ? expected {TAP_HELP}")
    except (EOFError, KeyboardInterrupt):
        pass
    finally:
        pad.close()
        print("\nclosed")


def stick_test(port=None, seconds=12):
    """Blind pad check: walk a circle. Character moves -> pad fine, vision is the bug."""
    pad = ArduinoPad(port) if port else VirtualPad()
    print("focus the game NOW; walking a circle for", seconds, "s")
    t0 = time.time()
    try:
        while time.time() - t0 < seconds:
            a = (time.time() - t0) * 1.2
            sx, sy = np.cos(a), np.sin(a)
            pad.stick(float(sx), float(sy), True)
            print(f"stick {sx:+.2f},{sy:+.2f}   ", end="\r")
            time.sleep(1 / LOOP_HZ)
    except KeyboardInterrupt:
        pass  # cutting the test short is normal, not a crash
    finally:
        pad.close()
        print("\ndone")


if __name__ == "__main__":
    if "--demo" in sys.argv:
        demo()
    elif "--zone" in sys.argv:
        from zone_recorder import main as zone_main
        j = sys.argv.index("--zone")
        raise SystemExit(zone_main(sys.argv[j + 1:]))
    elif "--record" in sys.argv:
        from zone_recorder import interactive_record
        raise SystemExit(0 if interactive_record() else 1)
    elif "--place-circles" in sys.argv:
        from area_editor import run_editor
        j = sys.argv.index("--place-circles")
        rest = [a for a in sys.argv[j + 1:] if not a.startswith("--")]
        run_editor(rest[0] if rest else None,
                   rest[1] if len(rest) > 1 else 50.0,
                   "--replace" in sys.argv)
    elif "--circle" in sys.argv:
        j = sys.argv.index("--circle")
        rest = [a for a in sys.argv[j + 1:] if not a.startswith("--")]
        record_circle(rest[0] if rest else None,
                      rest[1] if len(rest) > 1 else None,
                      "--replace" in sys.argv)
    elif "--snap" in sys.argv:
        snap()
    elif "--watch" in sys.argv:
        watch()
    elif "--relogin" in sys.argv:
        relogin("--dry" in sys.argv)
    elif "--probe" in sys.argv:
        probe()
    elif "--press" in sys.argv:
        i = sys.argv.index("--port") if "--port" in sys.argv else -1
        press_repl(sys.argv[i + 1] if i >= 0 else "auto")
    elif "--buff" in sys.argv:
        i = sys.argv.index("--port") if "--port" in sys.argv else -1
        j = sys.argv.index("--buff")
        rest = [a for a in sys.argv[j + 1:] if not a.startswith("--")]
        hold = float(rest[0]) if len(rest) > 0 else BUFF_HOLD_S
        gap = float(rest[1]) if len(rest) > 1 else BUFF_GAP_S
        buff_test(sys.argv[i + 1] if i >= 0 else None, hold, gap)
    elif "--test" in sys.argv:
        i = sys.argv.index("--port") if "--port" in sys.argv else -1
        stick_test(sys.argv[i + 1] if i >= 0 else None)
    else:
        i = sys.argv.index("--port") if "--port" in sys.argv else -1
        j = sys.argv.index("--area") if "--area" in sys.argv else -1
        named = [a for a in sys.argv[j + 1:] if not a.startswith("--")] if j >= 0 else []
        main(sys.argv[i + 1] if i >= 0 else None, named[0] if named else None)
