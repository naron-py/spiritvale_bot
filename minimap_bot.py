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
import struct
import sys
import threading
import time

import cv2
import numpy as np

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
BUFF_SEQUENCE = ("up", "left", "down", "right")
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
# A screen that will not advance is either stuck or was never there. Measured
# from a live freeze: login_screen() read "disconnected" during ordinary play and
# the bot clicked (0.500, 0.144) into the world every poll for the rest of the
# session, dropping the stick each time -- a bot that stands still forever. A
# real flow walks disconnected -> server -> character within a poll or two, so
# repeating the same screen this many times means stop clicking, not click again.
RECONNECT_MAX_REPEAT = 5
RECONNECT_DUMP_MAX = 5    # frames written when it fires; the only evidence there is
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
MEMORY_TARGETING = True
MEM_REFRESH_S = 2.0      # rediscovering units scans GBs; positions are re-read
MEM_RANGE = 70.0         # world units; roughly what the minimap used to cover
MEM_CAL_PUSH_S = 0.7     # per calibration push, two of them
MEM_CAL_MIN = 0.5        # world units a push must move us to count
MEM_CAL_LEGS = 3         # pushes to fit the basis from; more resists shoving
TARGET_SWITCH = 0.7      # only swap targets for one this much nearer
# Standing on the monster, the direction to it flips every frame -- measured at a
# median of 0.4 world units, which is the bot wiggling left and right on the
# spot. Inside this, stop steering and just hit it.
MEM_ARRIVE = 2.5         # world units
# Arriving used to mean a stick of exactly zero, and the damage stopped a couple
# of seconds later with the target still standing: measured 46474 -> 37502 and
# then frozen for dozens of frames at an unchanging 2.02 units, attack still
# held. The game sits in keyboard mode until it sees stick motion, and with a
# dead stick it goes back there, so the held attack stops landing. So the stick
# has to keep moving on a target -- but the push that replaced the zero only
# cancelled itself out frame to frame, which left the character pressed against
# the monster, and that close the game gives no attack at all: it needs room to
# swing. Circle the target instead. One motion pays for both lessons.
# ponytail: MIN/MAX are the calibration knob -- the range the game actually
# swings at is unmeasured, and --fightlog is what measures it.
MEM_ORBIT_MIN = 1.8       # closer than this, push away from the target
MEM_ORBIT_MAX = 2.5       # further than this, push back in
MEM_ORBIT_SPEED = 0.7     # stick magnitude while circling
# A wall or a corner stops the circle dead, and the radius cannot say so -- the
# radius is the thing the orbit holds constant. Our own position not changing
# is the honest test: when it does not, go round the other way.
MEM_ORBIT_FLIP_S = 1.5
MEM_ORBIT_MIN_MOVE = 0.6  # world units of our own travel that counts as progress
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
LOOT_FIRST_RANGE = 15.0  # world units; 0 turns this off and restores nearest-wins
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
LOOT_MAX_S = 15.0
LOOT_IGNORE_S = 30.0
# Which items to walk to, matched against the name the tooltip shows. Empty
# means every item the bot can see. Each entry is a case-insensitive substring,
# so ("Card",) takes Bee Card, Rooster Card and any card added later; a full
# name still works, and a short entry catches everything containing it.
# `python memscan.py --loot` prints the names lying around you, which is the
# list to write this from.
LOOT_NAMES = ("Grape", "Card", "Essence", "Gem" )          # e.g. ("Flax", "Slingshot", "Pioneer Relic")
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
WALK_BLOCK_FRAMES = 3    # consecutive such frames before calling it a wall
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
WALK_MAX_CELLS = 4000    # search expansion ceiling; past it, walk straight
WALK_PAD = 12            # cells of detour room either side of the straight line
WALK_FILE = "walkmap.json"
WALK_SAVE_S = 30.0       # the map is written from the scanner thread
# ponytail: floor grows without bound over weeks of play; at the cap it simply
# stops taking new cells. Age them out if that ever turns out to matter.
WALK_FLOOR_MAX = 200000  # cells of proven floor kept, ~2 MB of JSON
WALK_LOG = "--walklog" in sys.argv   # print what the wall sensor sees


def wake_controller(pad):
    """SpiritVale stays in keyboard mode until it sees stick motion, and button
    presses sent before that are dropped. A there-and-back nudge flips it."""
    for sx, sy in ((0.0, WAKE_AMP), (0.0, -WAKE_AMP), (0.0, 0.0)):
        pad.stick(sx, sy, False)
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


def hold_still(mode):
    """True when memory targeting means "stop", not merely "nothing here".

    main() reads a zero stick as handled and never falls through to the pixel
    path, so this is the difference between a bot that waits out a death and a
    bot that stands in a field forever because the unit list came back empty.
    """
    return mode != "no monster"


def toggle_running(paused, pad, pet_filter, wake=wake_controller):
    """Toggle run state, always clearing held controls and stale target state."""
    paused = not paused
    pad.stick(0.0, 0.0, False)
    pet_filter.reset()
    if not paused:
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
            pad.stick(sx, sy, False)
            time.sleep(0.05)
        pad.stick(0.0, 0.0, False)
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


def sea_icon():
    """The SEA label template, or None if the file is absent."""
    global _sea_icon
    if _sea_icon is False:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), SEA_ICON)
        _sea_icon = cv2.imread(path)
        if _sea_icon is None:
            print(f"no {SEA_ICON} -- cannot pick the server row")
    return _sea_icon


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


def login_screen(img):
    """Which login screen is showing: 'disconnected', 'server', 'character', None.

    Each test pairs a button with something only that screen has behind it, so
    ordinary gameplay -- blue sky above, blue skill icons below -- cannot match.
    The disconnect modal sits on top of the server table, hence the order.
    """
    if (find_blue_button(img, OK_BTN) and
            all(_probe(img, f, dark=True) for f in MODAL_DARK)):
        return "disconnected"
    if find_blue_button(img, CONNECT_BTN) and _probe(img, PANEL_WHITE, dark=False):
        return "server"
    if (find_blue_button(img, PLAY_BTN) and
            all(_probe(img, f, dark=True) for f in CHAR_BG)):
        return "character"
    return None


def click_at(x, y):
    """Left click in screen coordinates. mouse_event is ancient but is 3 lines."""
    import ctypes
    u = ctypes.windll.user32
    u.SetCursorPos(int(x), int(y))
    time.sleep(0.05)
    u.mouse_event(0x0002, 0, 0, 0, 0)  # LEFTDOWN
    u.mouse_event(0x0004, 0, 0, 0, 0)  # LEFTUP


def reconnect_step(img, win, click=click_at, settle=RECONNECT_SETTLE_S,
                   sea_template=None):
    """Advance the login flow by one screen. Returns what it did, or None.

    Driven by what is on screen rather than a fixed script, so a slow server or a
    missed click just means the same screen is handled again next poll.
    """
    screen = login_screen(img)
    if screen is None:
        return None
    h, w = img.shape[:2]

    def press(frac):
        click(win.left + w * frac[0], win.top + h * frac[1])
        time.sleep(settle)

    if screen == "disconnected":
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
        self.still = 0            # frames in a row we asked to move and did not
        self.last_pos = None      # our position at the previous observation
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
    def route(self, px, pz, tx, tz, now):
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
        if mode not in ("chasing", "far", "loot"):
            self.still, self.last_pos, self.mark = 0, None, None
            return None
        self.free(px, pz)
        was, self.last_pos = self.last_pos, (px, pz)
        # No stick out means we are not trying to get anywhere, so neither
        # sensor may judge: the window has to start again when we do.
        if was is None or not basis or not (sx or sy):
            self.still, self.mark = 0, None
            return None
        wx, wz = world_for(basis, sx, sy)
        n = math.hypot(wx, wz)
        if n < 1e-9:
            self.still, self.mark = 0, None
            return None
        ux, uz = wx / n, wz / n
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

    Two modes are never interrupted. "on it" is a fight already joined, and
    walking out of one is how a bot dies -- an item under our feet is collected
    by LOOT_BUTTON anyway, without moving. "unwedge" is the character backing
    out of something it is jammed against, and it reports a distance of zero:
    overriding that push would leave the bot stuck against the wall it is in
    the middle of escaping.
    """
    if ldist is None or mode in ("on it", "unwedge"):
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
    """Is this item one we walk to? Empty LOOT_NAMES means all of them.

    Substring, case-insensitive: "Card" collects "Bee Card", "Rooster Card" and
    every other card, which is the point -- a whole family of items is usually
    what you want and listing them one by one goes stale as the game adds more.
    The cost is that a short entry catches more than it looks like it will
    ("axe" also takes "Battle Axe"), so keep entries specific enough to mean it.
    """
    if not LOOT_NAMES:
        return True
    # ("Card") is a string, not a tuple -- the missing comma is easy to write,
    # and iterating it would test the letters 'C', 'a', 'r', 'd' one at a time.
    want = (LOOT_NAMES,) if isinstance(LOOT_NAMES, str) else LOOT_NAMES
    got = name.strip().lower()
    return any(w.strip().lower() in got for w in want if w.strip())


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

    def __init__(self):
        import memscan
        self.ms = memscan
        self.mem = memscan.Mem()
        self.classes = memscan.type_classes(self.mem)
        self.me = None            # our own BaseUnitController
        self.basis = None         # stick push -> world travel
        self.units = []           # cached (kind, addr, x, y, z)
        self.chasing = None       # unit held between frames, so it does not flap
        self.approach = None      # last heading that closed on a target
        self.orbit_dir = 1        # which way round a target we circle
        self.orbit_mark = None    # (time, x, z) the orbit last made progress at
        self.engaged_since = None # when we started on the current target
        self.ignored = {}         # unit -> time it becomes fair game again
        self.mode = "no unit"
        self.misses = 0           # consecutive frames our position did not read
        self.seen_at = {}         # last known position per unit
        self.sweep_at = 0         # cursor into the far units, a slice per frame
        self.fight_ok = {}        # unit -> (expiry, is it worth fighting)
        self.hot = None           # regions worth sweeping
        self.owner = None         # our unit, from the local connection
        self.loot = {}            # drop -> (x, y, z, name)
        self.loot_name = ""       # what we are walking to, for the status line
        self.loot_target = None   # drop held between frames
        self.loot_since = None    # when we started walking to it
        self.loot_ignored = {}    # drop -> time it becomes fair game again
        self.loot_mode = "no loot"
        self.hot_loot = None      # regions worth sweeping for loot
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
        self.escapes = 0          # on the current target
        self.scanner = None
        self.stop = None
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
        self.classes = found
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

    def _positions(self, addrs):
        # Hot path: a few hundred of these per frame, so the read and the
        # sanity check are inline rather than three function calls deep.
        out = {}
        read, off = self.mem.read, self.ms.UNIT_POSITION
        limit = self.ms.POS_MAX
        for a in addrs:
            blob = read(a + off, 12)
            if not blob:
                continue
            x, y, z = struct.unpack("<fff", blob)
            # NaN fails every comparison, which is what excludes it here, and a
            # zeroed triple is recycled memory rather than a place.
            if -limit < x < limit and -limit < y < limit and -limit < z < limit                     and (x > 1e-3 or x < -1e-3) and (z > 1e-3 or z < -1e-3):
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
        ok = self.ms.real_monster(self.mem, unit)
        self.fight_ok[unit] = (now + LIVE_TTL_S, ok)
        return ok

    def _first_fightable(self, ranked):
        """Nearest entry that is really there. `ranked` is sorted by distance."""
        for d, u, x, y, z in ranked:
            if self._fightable(u):
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
            self.seen_at = self._positions(addrs)
            return self.seen_at
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
        self.seen_at.update(self._positions(near))
        return self.seen_at

    def calibrate(self, pad):
        """Find which unit is us, and how a stick push maps to world travel.

        Our unit is the one that moves when we push, which is a fact we can
        create on demand rather than infer. It
        works mid-combat, unlike anything built on walking a clean line.
        """
        players = self.known_players()   # from the background sweep, never blocks
        if not players:
            return False

        def push(sx, sy):
            """World delta per unit over one push."""
            before = self._positions(players)
            t0 = time.time()
            while time.time() - t0 < MEM_CAL_PUSH_S:
                pad.stick(sx, sy, False)
                time.sleep(0.05)
            pad.stick(0.0, 0.0, False)
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
        me = self.owner
        if me is not None and not any(u == me for _, u, *_ in self.units):
            me = self.owner = None       # from before a relog; look again
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

    def _ensure_class(self, mem, label, why):
        """Find one class by name once, and cache its slot as an RVA.

        Separate from heal() on purpose: the units can be perfectly healthy
        while a class the bot never needed before has no cached slot at all,
        and heal() only fires when the *monster* class is missing. Searching
        for the single name we lack costs a fraction of a full heal, and it
        happens once per patch.
        """
        found = self.ms.find_classes(mem, {label: self.ms.CLASS_NAMES[label]})
        if not found.get(label):
            print(f"\n{why}: no {self.ms.CLASS_NAMES[label]} class "
                  f"found; off this session")
            return False
        self.classes = dict(self.classes, **found)
        rva = self.ms.class_slot_rva(mem, found[label])
        if rva:
            self.ms.save_rva_cache(dict(self.ms.load_rva_cache(), **{label: rva}))
        print(f"\n{why}: {self.ms.CLASS_NAMES[label]} found at "
              f"0x{found[label]:X}"
              + (f", slot cached (0x{rva:X})" if rva else ""))
        return True

    def _sweep_loot(self, mem):
        """Refresh ground loot. world_loot() has already dropped the pool.

        LootDrop objects are pooled -- a fixed set, recycled, and picking an
        item up frees neither the object nor its position, the same trap the
        pooled monsters set. What separates a real drop is that it carries an
        item at all: measured on a live field, 157 of 192 slots had no name and
        the 35 that did matched what was lying there.
        """
        found = self.ms.world_loot(mem, self.classes.get("loot"),
                                   regions=self.hot_loot)
        with self.lock:
            self.loot = {d: (x, y, z, n) for d, x, y, z, n in found}
        if self.hot_loot is None and found:
            spans = mem.regions()
            live = {d for d, *_rest in found}
            self.hot_loot = [(b, s) for b, s in spans
                             if any(b <= d < b + s for d in live)]

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
        near = sorted((math.hypot(x - px, z - pz), d, n)
                      for d, (x, _, z, n) in drops)[:3]
        return f"{len(drops)} cached; nearest " + ", ".join(
            f"{n[:14]}@{dist:.1f}"
            f"{' IGNORED' if self.loot_ignored.get(d, 0) > now else ''}"
            f"{'' if wanted_item(n) else ' unwanted'}"
            for dist, d, n in near)

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
        ranked = sorted((((x - px) ** 2 + (z - pz) ** 2) ** 0.5, d, x, z, n)
                        for d, (x, _, z, n) in drops
                        if self.loot_ignored.get(d, 0) < now and wanted_item(n))
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

        dist, drop, x, z, name = pick
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
            looked = True         # loot class not looked up yet; once only
            saved = 0.0           # the walk map is written from here, not the
                                  # 50 ms frame, and only when it changed
            before = {}           # last sweep's positions, to spot who moved
            try:
                if not self.available():
                    self.heal(mem)
                while not self.stop.is_set():
                    if not self.available():
                        self.stop.wait(MEM_REFRESH_S)
                        continue
                    found = self.ms.world_units(mem, regions=self.hot)
                    with self.lock:
                        self.units = found
                    if self.owner is None and found:
                        # Who we are, read instead of walked for: any unit
                        # carries the managers, so this is a pointer walk with
                        # nothing to search. Re-read whenever it is lost, since
                        # a map change rebuilds the object.
                        self.owner = self.ms.local_player(mem, found[0][1])
                    if LOOT_PICKUP and not self.classes.get("loot") and looked:
                        looked = self._ensure_class(mem, "loot", "loot pickup")
                    if LOOT_PICKUP and self.classes.get("loot"):
                        self._sweep_loot(mem)
                    if PATHFIND:
                        # Everything alive walks the same navmesh we do, so a
                        # unit that moved since the last sweep has just proven
                        # its ground walkable. Free floor, hundreds of cells at
                        # a time, in places the bot has never been.
                        walkers = [(x, z) for _, u, x, _, z in found
                                   if u in before and before[u] != (x, z)]
                        before = {u: (x, z) for _, u, x, _, z in found}
                        self.walk.paint(walkers)
                        if time.time() - saved >= WALK_SAVE_S:
                            self.walk.save()
                            saved = time.time()
                    if self.hot is None and found:
                        # Narrow the next sweep to where the units turned out to
                        # be, rather than paying for the whole heap every time.
                        spans = mem.regions()
                        live = {u for _, u, *_ in found}
                        self.hot = [(b, s) for b, s in spans
                                    if any(b <= u < b + s for u in live)]
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

    def route_to(self, now, px, pz, tx, tz):
        """Where to actually steer -- the target, or a way round a known wall.

        The straight line is the default and stays it: a route is only planned
        when the line crosses something the map says is solid. A failed plan
        returns the target too, so the worst case is exactly today's behaviour
        and MEM_ENGAGE_MAX_S gives up as it always did. Never returns nothing:
        main() reads a zero stick as handled and parks the bot.
        """
        self.routing = self.sealed = False
        if not (PATHFIND and self.walk):
            return tx, tz
        if not self.walk.crossed(px, pz, tx, tz, now):
            self.path = None
            return tx, tz
        fresh = (self.path and now - self.path_at < WALK_REPLAN_S
                 and self.path_to
                 and math.hypot(tx - self.path_to[0], tz - self.path_to[1])
                 < WALK_WAYPOINT)
        if not fresh:
            self.path = self.walk.route(px, pz, tx, tz, now)
            self.path_at, self.path_to = now, (tx, tz)
        if not self.path:
            # No path at all, and the search was not merely out of budget: the
            # goal is walled off from here. Say so, so the caller can drop the
            # monster instead of leaning on the wall for MEM_ENGAGE_MAX_S.
            self.sealed = not self.walk.capped
            return tx, tz
        self.routing = True
        return self.walk.waypoint(px, pz, self.path)

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
        hit = self.walk.observe(now, px, pz, sx or 0.0, sy or 0.0, self.basis,
                                mode, self.loot_goal if on_loot else self.goal)
        if hit:
            self.path = None            # a new wall: the old route is a lie
            if self.walk.wedged:
                self.wedge_off(now, sx or 0.0, sy or 0.0)
        return hit

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

    def known_players(self):
        with self.lock:
            return [u for k, u, *_ in self.units if k == "player"]

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
        if not here and self.misses < MEM_LOST_FRAMES:
            # One empty read is not a death. Coast on the last known state.
            self.misses += 1
            self.mode = "lost"
            return None, None, None
        if not here:
            # Our unit was rebuilt -- map change, death or relog. Everything
            # derived from it is now meaningless: the basis was measured for a
            # unit that no longer exists. Dropping `hot` forces the next sweep
            # to search
            # the whole heap, because the new objects need not be where the old
            # ones were.
            self.me = self.basis = self.hot = self.approach = None
            self.orbit_mark = None
            # The owner is a pointer to the object that was just rebuilt, so it
            # is as dead as the rest. It is also what the scanner checks before
            # looking us up again, so leaving it set meant we never recovered.
            self.owner = None
            # Same reason as `hot`: the drops that survive a relog need not be
            # in the regions the old ones were, and a narrowed sweep that finds
            # nothing keeps finding nothing.
            self.hot_loot = None
            with self.lock:
                self.loot = {}
            self.loot_target = self.loot_since = None
            self.chasing = self.engaged_since = None
            self.ignored = {}
            self.seen_at, self.fight_ok = {}, {}
            # The route and the travel history belong to a unit that is gone;
            # the map itself describes the world and stays.
            self.path = self.last_pos = None
            if self.walk:
                self.walk.forget_walk()
            self.mode = "no unit"
            with self.lock:
                self.units = []
            return None, None, None
        self.misses = 0
        px, _, pz = here
        self.last_pos = (px, pz)
        if not self.ms.worth_fighting(self.mem, self.me):
            # We are dead (or not rendered). Swinging at things from a corpse
            # looks exactly like a bot that cannot kill anything -- it cost a
            # whole debugging session once, with the conclusion that melee
            # range was wrong when the character was simply lying down.
            self.mode = "dead"
            return None, None, None
        if self.escape and now < self.escape_until:
            # Backing out of a wedge. This overrides the target entirely: while
            # the character cannot move, nothing else it decides matters.
            self.mode = "unwedge"
            return self.escape[0], self.escape[1], 0.0
        self.escape = None
        self.mode = "chasing"
        fresh = [(k, u, *live[u]) for k, u, *_ in cached if u in live]
        # Most of the list is pooled or dead objects that keep their position
        # and full health. Without this the bot parks in a pile of them,
        # swinging at each for MEM_ENGAGE_MAX_S in turn and never leaving --
        # and walking straight back if you drag the character away.
        ranked = sorted(((((x - px) ** 2 + (z - pz) ** 2) ** 0.5, u, x, y, z)
                         for k, u, x, y, z in fresh if k == "monster"
                         and self.ignored.get(u, 0.0) < now),
                        key=lambda e: e[0])

        # Hold the current target rather than re-picking the nearest every
        # frame. Two monsters a similar distance away swap which is closer
        # constantly, and the bot answers by walking left, right, left, right
        # instead of going to either. It only switches for something clearly
        # nearer, or when this one is gone.
        held = next((e for e in ranked if e[1] == self.chasing), None)
        if held and held[0] <= MEM_RANGE and self._fightable(held[1]):
            hit, dist = (held[1], held[2], held[3], held[4]), held[0]
        else:
            # Checked in distance order and stopped at the first one that is
            # really there, so the liveness reads cost a handful per frame
            # rather than one per monster on the map.
            hit, dist = self._first_fightable(ranked)
            if hit and held and held[0] <= MEM_RANGE                     and dist > held[0] * TARGET_SWITCH and self._fightable(held[1]):
                hit, dist = (held[1], held[2], held[3], held[4]), held[0]
        if not hit:
            self.chasing = self.engaged_since = None
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
        if hit[0] != self.chasing:
            self.chasing, self.engaged_since, self.escapes = hit[0], now, 0
        elif self.escapes >= WALK_ESCAPE_GIVEUP:
            # Backed out of the same approach this many times and still here.
            # Whatever is in the way, this monster is not the one to fight.
            self.ignored[hit[0]] = now + MEM_IGNORE_S
            self.chasing = self.engaged_since = None
            self.escapes, self.mode = 0, "walled"
            return None, None, None
        elif stale_target(now, self.engaged_since):
            # Long enough on one target that it is not going to die: already
            # dead and still listed, unreachable, or not attackable. Parking on
            # it forever is the one failure that looks exactly like a hung bot.
            self.ignored[hit[0]] = now + MEM_IGNORE_S
            self.chasing = self.engaged_since = None
            self.mode = "gave up"
            return None, None, None
        if FIGHT_LOG and self.mem and dist <= MEM_RANGE:
            # Distance only: self.mode is still last frame's here, and the band
            # is what the distance says anyway.
            hp = self.ms.unit_health(self.mem, hit[0])
            print(f"\nfightlog {hit[0]:012X} dist {dist:5.2f} hp {hp}")
        if dist <= MEM_ARRIVE:
            # Arrived: circle it rather than stand on it. Standing on the
            # monster is no attack, and a dead stick is no attack either.
            self.mode = "on it"
            radial = (stick_for(self.basis, hit[1] - px, hit[3] - pz)
                      if dist >= MEM_ORBIT_MIN else self.approach)
            # Point blank the direction to the target flips every frame,
            # measured at 0.4 units, so the last good heading stands in.
            if not radial:
                return 0.0, 0.0, dist
            rx, ry = radial
            way = self._orbit_way(now, px, pz)
            ox, oy = -ry * way, rx * way
            pull = (-1.0 if dist < MEM_ORBIT_MIN else
                    1.0 if dist > MEM_ORBIT_MAX else 0.0)
            ox, oy = ox + rx * pull, oy + ry * pull
            n = math.hypot(ox, oy) or 1.0
            return ox / n * MEM_ORBIT_SPEED, oy / n * MEM_ORBIT_SPEED, dist
        gx, gz = self.route_to(now, px, pz, hit[1], hit[3])
        if self.sealed:
            # Walled off with no way round: walking at it is eight seconds of
            # pressing into stone before MEM_ENGAGE_MAX_S notices. There are
            # other monsters.
            self.ignored[hit[0]] = now + MEM_IGNORE_S
            self.chasing = self.engaged_since = None
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
        self.attack_btn = vg.XUSB_BUTTON.XUSB_GAMEPAD_LEFT_SHOULDER  # L1 / LB
        self.dpad = {"up": vg.XUSB_BUTTON.XUSB_GAMEPAD_DPAD_UP,
                     "down": vg.XUSB_BUTTON.XUSB_GAMEPAD_DPAD_DOWN,
                     "left": vg.XUSB_BUTTON.XUSB_GAMEPAD_DPAD_LEFT,
                     "right": vg.XUSB_BUTTON.XUSB_GAMEPAD_DPAD_RIGHT}
        self.face = {"a": vg.XUSB_BUTTON.XUSB_GAMEPAD_A,
                     "b": vg.XUSB_BUTTON.XUSB_GAMEPAD_B,
                     "x": vg.XUSB_BUTTON.XUSB_GAMEPAD_X,
                     "y": vg.XUSB_BUTTON.XUSB_GAMEPAD_Y}

    def _tap(self, btn, hold):
        # The stick keeps its last value across this: left_joystick_float persists
        # between updates, so a tap never interrupts the chase.
        self.pad.press_button(btn)
        self.pad.update()
        time.sleep(hold)
        self.pad.release_button(btn)
        self.pad.update()

    def tap_dpad(self, name, hold):
        self._tap(self.dpad[name], hold)

    def tap_button(self, name, hold=SPAM_HOLD_S):
        self._tap(self.face[name], hold)

    def tap_trigger(self, name, hold):
        # A trigger is an axis, not a button, so it cannot go through _tap. The
        # stick keeps its value across this the same way.
        press = (self.pad.left_trigger if name == "lt"
                 else self.pad.right_trigger)
        press(value=255)
        self.pad.update()
        time.sleep(hold)
        press(value=0)
        self.pad.update()

    def stick(self, sx, sy, attack=False):
        self.pad.left_joystick_float(sx, sy)
        if attack:
            self.pad.press_button(self.attack_btn)
        else:
            self.pad.release_button(self.attack_btn)
        self.pad.update()  # one report per frame

    def close(self):
        self.stick(0.0, 0.0, False)


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

    ATTACK_BTN = 4  # LB in the usual XInput button order
    HAT = {"up": 0, "right": 2, "down": 4, "left": 6}  # sketch: 0..7 clockwise from N
    FACE = {"a": 0, "b": 1, "x": 2, "y": 3}  # usual XInput button order

    def tap_dpad(self, name, hold):
        self._cmd(f"V{self.HAT[name]}")
        time.sleep(hold)
        self._cmd("V-1")  # -1 centres the hat

    def tap_button(self, n, hold=None):
        # hold is ignored: the sketch's own B command is press, 50ms, release.
        self._cmd(f"B{self.FACE.get(n, n)}")

    def tap_trigger(self, name, hold):
        # 'T<left>,<right>', the sketch's trigger axes. ponytail: untested on
        # the board -- the game only reads XInput, so vgamepad is the live path.
        lo, hi = (255, 0) if name == "lt" else (0, 255)
        self._cmd(f"T{lo},{hi}")
        time.sleep(hold)
        self._cmd("T0,0")

    def _cmd(self, line):
        self.ser.write(f"{line}\n".encode())
        reply = self.ser.readline().strip()
        if reply != b"OK":
            print(f"\nboard replied {reply!r} to {line}")

    def stick(self, sx, sy, attack=False):
        # HID Y axis is down-positive, our sy is up-positive.
        x, y = int(sx * 32767), int(-sy * 32767)
        if (x, y, attack) == self.last:
            return  # ponytail: sketch is synchronous, skip no-op round trips
        if self.last is None or (x, y) != self.last[:2]:
            self._cmd(f"L{x},{y}")
        if self.last is None or attack != self.last[2]:
            self._cmd(f"{'D' if attack else 'U'}{self.ATTACK_BTN}")
        self.last = (x, y, attack)

    def close(self):
        self.ser.write(b"Z\n")
        self.ser.readline()
        self.ser.close()


def main(port=None):
    import mss

    win = find_window()
    pad = ArduinoPad(port) if port else VirtualPad()
    print(f"window {win.width}x{win.height} @ ({win.left},{win.top})"
          f" via {type(pad).__name__} -- End to start/stop, ctrl+c to exit")

    last = None  # (t, dist, sx, sy) of last seen dot
    had_unit = False   # so the 'unit rebuilt' notice prints once
    next_cal = 0.0     # earliest retry after a failed calibration
    eyes = None
    if MEMORY_TARGETING:
        try:
            eyes = MemoryEyes()
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
    next_buff = 0.0   # 0 = cast once at startup, then every BUFF_PERIOD_S
    buff_queue = []   # d-pad presses left in the current cast
    next_press = 0.0  # earliest time for the next one
    next_spam = 0.0   # SPAM_BUTTON goes out on its own timer
    next_loot = 0.0   # LOOT_BUTTON while standing on a drop
    next_login_check = 0.0  # a whole-window grab, so kept to RECONNECT_POLL_S
    reconnecting = RECONNECT   # switched off if a screen refuses to advance
    same_screen = (None, 0)    # what reconnect_step did last, and how many in a row
    dumps = 0                  # frames written, capped by RECONNECT_DUMP_MAX

    pad.stick(0.0, 0.0, False)
    print("STOPPED -- press End to start")

    with mss.mss() as sct:
        try:
            while True:
                if toggle_key_hit():
                    paused = toggle_running(paused, pad, pet_filter)
                    target_lock.reset()
                    target_blacklist.reset()
                    stuck_watchdog.reset()
                    last = None
                    buff_queue = []
                    next_buff = next_press = next_spam = next_loot = 0.0
                    if eyes is not None:
                        # Same reason the pixel helpers reset here: a drop held
                        # from before the pause is stale by the time we resume.
                        eyes.loot_target = eyes.loot_since = None
                        eyes.loot_mode = "no loot"
                        # The route is stale for the same reason; the walls it
                        # avoided are not, so the map itself is left alone.
                        eyes.path = eyes.last_pos = None
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
                    time.sleep(0.05)
                    continue

                if reconnecting and time.time() >= next_login_check:
                    next_login_check = time.time() + RECONNECT_POLL_S
                    full = np.array(sct.grab(window_region(win)))[:, :, :3]
                    if not login_screen(full):
                        same_screen = (None, 0)
                    else:
                        # Drop the stick and attack before touching the mouse: the
                        # character is gone, and a held button carries into the
                        # next session.
                        pad.stick(0.0, 0.0, False)
                        did = reconnect_step(full, win)
                        print(f"\nreconnect: handled the {did} screen")
                        if dumps < RECONNECT_DUMP_MAX:
                            # Which blue blob matched is the one thing the log
                            # cannot say, and a false positive can only be
                            # guessed at without it. Capped, so a real
                            # reconnect does not paper the folder.
                            cv2.imwrite(f"reconnect_{did}_{dumps}.png", full)
                            dumps += 1
                        seen, n = same_screen
                        same_screen = (did, n + 1 if did == seen else 1)
                        if same_screen[1] >= RECONNECT_MAX_REPEAT:
                            # Either the click misses or the screen was never
                            # there. Both end the same way -- pinned in this
                            # branch, clicking into the game every poll with
                            # the stick dropped, which is worse than no
                            # reconnect at all.
                            reconnecting = False
                            print(f"\nreconnect: the {did} screen did not "
                                  f"advance in {RECONNECT_MAX_REPEAT} tries -- "
                                  f"reconnect OFF for this run; see "
                                  f"reconnect_*.png. Restart to re-arm.")
                        target_lock.reset()
                        target_blacklist.reset()
                        stuck_watchdog.reset()
                        pet_filter.reset()
                        last = None
                        buff_queue = []
                        next_buff = next_press = next_spam = next_loot = 0.0
                        continue

                if not buff_queue and time.time() >= next_buff:
                    buff_queue = list(BUFF_SEQUENCE)
                    next_buff = time.time() + BUFF_PERIOD_S
                    print(f"\nbuffing: {' '.join(BUFF_SEQUENCE)}")

                reg = minimap_region(win)
                img = np.array(sct.grab(reg))[:, :, :3]
                h, w = img.shape[:2]
                now = time.time()
                sx = sy = None
                if (eyes is not None and eyes.me is None
                        and now >= next_cal and eyes.known_players()):
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
                    on_loot = False
                    if LOOT_PICKUP and eyes.mode not in ("on it", "unwedge"):
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
                        # path never runs. Right for the modes that mean stop
                        # (a corpse swings at nothing, a rebuilt unit has no
                        # basis) -- wrong for "no monster", which is the unit
                        # list saying it has nothing, not the screen. Leaving
                        # sx None there is what walks the bot to a red dot
                        # instead of standing still until it is restarted.
                        if hold_still(eyes.mode):
                            sx = sy = 0.0
                        # Name which kind of nothing this is: "no monster" was
                        # printed for a lost unit too, hiding a dead bot behind
                        # a message that reads like a quiet patch of map.
                        state = {"no unit": "no unit  ",
                                 "lost": "lost     ",
                                 "dead": "DEAD     ",
                                 "walled": "walled   ",
                                 "gave up": "gave up  "}.get(eyes.mode,
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
                                     "far": "far    "}.get(eyes.mode,
                                                           "dist  ") + (
                                f"{mdist:6.1f}" if not eyes.routing
                                else f"{mdist:5.1f}~")

                # Everything below is the pixel path, used when memory targeting
                # is off or has gone stale. It is left exactly as it was.
                if sx is None:
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

                # L1 held down continuously. Set ATTACK_MASH to mash it instead.
                atk = (now % ATTACK_PERIOD_S) < ATTACK_HOLD_S if ATTACK_MASH else True
                if eyes is not None:
                    # The stick that actually goes out is the one the walk map
                    # can learn a wall from -- loot may have won the arbitration
                    # above, and target()'s own vector was never sent.
                    eyes.observe_move(now, sx, sy, on_loot)
                pad.stick(sx, sy, atk)

                # One d-pad press per pass, spaced by BUFF_GAP_S. The stick and L1
                # keep their last value across a tap, so the buff casts mid-chase
                # instead of parking the bot for a whole sequence.
                key = ""
                if buff_queue and now >= next_press:
                    key = buff_queue.pop(0)
                    pad.tap_dpad(key, BUFF_HOLD_S)
                    next_press = now + BUFF_GAP_S
                elif (eyes is not None and now >= next_loot
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
                elif SPAM_BUTTON and now >= next_spam:
                    # Never in the same pass as a buff press: two taps back to back
                    # land inside one another's animation and the game drops one.
                    pad.tap_button(SPAM_BUTTON, SPAM_HOLD_S)
                    key = SPAM_BUTTON
                    next_spam = now + SPAM_PERIOD_S

                how = "  memory" if (eyes and eyes.me) else "  pixels"
                print(f"{state:12} stick {sx:+.2f},{sy:+.2f} "
                      f"atk {'#' if atk else '.'} {key:5}{how}   ",
                      end="\r")
                time.sleep(1 / LOOP_HZ)
        except KeyboardInterrupt:
            print("\nstopped")
        finally:
            pad.close()


def demo():
    """Self-check: synthetic minimap, no game or gamepad needed."""
    assert SPAM_BUTTON is None or SPAM_BUTTON in ArduinoPad.FACE, SPAM_BUTTON

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
        def __init__(self, real, standing=True):
            self.real, self.standing = set(real), standing

        def worth_fighting(self, _, unit):
            if unit == 0x1000:              # 0x1000 is us; alive unless a corpse
                return self.standing
            return unit in self.real

        def real_monster(self, mem, unit):
            return self.worth_fighting(mem, unit)

    class _Far(MemoryEyes):
        def __init__(self, at, extra=(), real=(0x2000,)):
            self.me, self.basis = 0x1000, [[1.0, 0.0], [0.0, 1.0]]
            self.units = [("monster", 0x2000, at, 0.0, 0.0)] + list(extra)
            self.chasing = self.engaged_since = self.approach = None
            self.ignored = {}
            self.mode, self.misses, self.hot = "chasing", 0, None
            self.orbit_dir, self.orbit_mark = 1, None
            self.at, self.mem = at, None
            self.seen_at, self.sweep_at, self.fight_ok = {}, 0, {}
            self.ms = _Fights(real)
            self.lock = threading.Lock()

        def _positions(self, addrs):
            return {a: ((self.at, 0.0, 0.0) if a == 0x2000
                        else (self.spots.get(a, 0.0), 0.0, 0.0)
                        if hasattr(self, "spots") else (0.0, 0.0, 0.0))
                    for a in addrs}

    far_off = _Far(MEM_RANGE * 3)              # way outside melee range
    fsx, fsy, fd = far_off.target(1.0)
    assert far_off.mode == "far", far_off.mode
    assert fsx is not None and (fsx or fsy), "must walk to it, not stand still"
    assert abs(fd - MEM_RANGE * 3) < 1.0, fd
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

    # In melee the bot circles the target instead of standing on it: standing
    # on a monster is a character with no room to swing, and the game gives no
    # attack for it. The stick must also never go still (see MEM_ORBIT_MIN).
    band = (MEM_ORBIT_MIN + MEM_ORBIT_MAX) / 2
    ring = _Far(band)
    rsx, rsy, rd = ring.target(1.0)
    assert ring.mode == "on it", ring.mode
    assert abs(rd - band) < 1e-6, rd
    assert abs(math.hypot(rsx, rsy) - MEM_ORBIT_SPEED) < 1e-6, (rsx, rsy)
    # Target sits on +x from us, so in the band the push is pure tangent.
    assert abs(rsx) < 1e-6 and abs(rsy) > 0, (rsx, rsy)

    # Too close: the push has to carry us away from it, or we stay jammed in.
    close = _Far(MEM_ORBIT_MIN / 2)
    close.approach = (1.0, 0.0)          # last heading that closed on it
    csx, _, _ = close.target(1.0)
    assert csx < 0, f"must back off when inside MEM_ORBIT_MIN, got {csx}"

    # Drifted out but still in melee: pull back in rather than let it slide.
    # Defaults put MEM_ORBIT_MAX at MEM_ARRIVE, leaving no room to drift, so
    # this narrows the band the way tuning it down would.
    kept_max = MEM_ORBIT_MAX
    try:
        globals()["MEM_ORBIT_MAX"] = MEM_ARRIVE / 2
        wide = _Far(MEM_ARRIVE * 0.75)
        wsx, _, _ = wide.target(1.0)
        assert wsx > 0, f"must close back to the band, got {wsx}"
    finally:
        globals()["MEM_ORBIT_MAX"] = kept_max

    # Blocked: our own position is what says the circle is getting nowhere --
    # the radius cannot, it is the thing being held constant. _Far parks us at
    # the origin, which is exactly a bot pressed against a wall.
    stuck = _Far(band)
    stuck.target(1.0)
    assert stuck.orbit_dir == 1, stuck.orbit_dir
    stuck.target(1.0 + MEM_ORBIT_FLIP_S / 2)
    assert stuck.orbit_dir == 1, "must not reverse before MEM_ORBIT_FLIP_S"
    stuck.target(1.0 + MEM_ORBIT_FLIP_S + 0.1)
    assert stuck.orbit_dir == -1, "blocked circle must turn round"
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
    global LOOT_NAMES
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
    # where a monster is almost always the nearer of the two.
    assert loot_wins("chasing", 4.0, LOOT_FIRST_RANGE - 1), "close item goes first"
    assert not loot_wins("chasing", 4.0, LOOT_FIRST_RANGE + 5), "far item waits"
    assert loot_wins("chasing", 30.0, LOOT_FIRST_RANGE + 5), "unless it is nearer"
    assert loot_wins("far", 80.0, 39.0), "a far monster always yields"
    assert loot_wins("no monster", None, 39.0), "nothing to fight, so loot"
    assert not loot_wins("chasing", 4.0, None), "no item, no contest"
    # Two pushes that must never be interrupted: a fight already joined, and a
    # character in the middle of backing out of a wedge (which reports 0.0, so
    # every item on the map would otherwise look nearer than the monster).
    assert not loot_wins("on it", 1.0, 1.0), "never walk out of melee"
    assert not loot_wins("unwedge", 0.0, 1.0), "never interrupt an escape"

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
    assert 0xA000 not in patient.loot_ignored, patient.loot_ignored
    # And it says why when it offers nothing at all.
    assert "cache empty" in _Loot().loot_debug(1.0)
    shunned = _Loot(drops=[(0xA000, 1.0, 0.0, "Flax")])
    shunned.loot_ignored[0xA000] = 99.0
    assert "IGNORED" in shunned.loot_debug(1.0), shunned.loot_debug(1.0)

    # Out of range is left alone: crossing the map for an item is not looting.
    away = _Loot(drops=[(0xA000, LOOT_RANGE * 2, 0.0, "Flax")])
    assert away.pick_loot(2.0) == (None, None, None)

    # An item that cannot be collected must be given up on, or it owns the bot.
    stuck_loot = _Loot(drops=[(0xA000, 5.0, 0.0, "Flax")])
    stuck_loot.pick_loot(2.0)
    assert stuck_loot.pick_loot(2.0 + LOOT_MAX_S + 1) == (None, None, None)
    assert stuck_loot.loot_mode == "loot skip", stuck_loot.loot_mode
    assert 0xA000 in stuck_loot.loot_ignored

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
    # ...and an empty unit list must not park the bot. A zero stick counts as
    # handled in main(), so "no monster" has to reach the pixel path instead:
    # this is the branch that stood still for a whole session once.
    assert not hold_still("no monster")
    assert hold_still("dead") and hold_still("lost") and hold_still("no unit")

    # A dead character must be reported as such, not left swinging: a corpse
    # that keeps attacking reads as "melee range is wrong" and sends the next
    # debugging session somewhere it should not go.
    class _Corpse(_Far):
        def __init__(self):
            _Far.__init__(self, 5.0)
            self.ms = _Fights((), standing=False)   # we are the dead one

    corpse = _Corpse()
    assert corpse.target(1.0) == (None, None, None)
    assert corpse.mode == "dead", corpse.mode

    near = _Far(MEM_RANGE / 2)                 # inside range: ordinary chase
    near.target(1.0)
    assert near.mode == "chasing", near.mode
    assert near.approach, "the chase heading is what the back-off reverses"

    # On a target the stick must never be exactly zero: the game falls back to
    # keyboard mode and the held attack quietly stops landing, measured as a
    # target frozen at 37502 hp for dozens of frames while the bot reported
    # "on it". The orbit is what keeps it alive, so it must not cancel itself
    # out either -- that was the old holding push, and it is how the character
    # ended up jammed against the monster with no room to swing.
    onto = _Far(MEM_ARRIVE / 2)
    onto.approach = (0.6, -0.8)
    one = onto.target(1.0)
    two = onto.target(1.0)
    assert onto.mode == "on it", onto.mode
    assert one[0] or one[1], "a dead stick loses the attack"
    assert abs(one[0] + two[0]) > 1e-9 or abs(one[1] + two[1]) > 1e-9, (one, two)
    # Inside MEM_ORBIT_MIN it has to open the distance, not hold it.
    assert one[0] * 0.6 + one[1] * -0.8 < 0, one

    # A blank position read must not throw the calibration away: the bot goes
    # silent until someone notices and restarts it. Coast, then give up.
    class _Blind(MemoryEyes):
        def __init__(self):
            self.me, self.basis = 0x1000, [[1.0, 0.0], [0.0, 1.0]]
            self.units, self.chasing, self.engaged_since = [], None, None
            self.ignored = {}
            self.mode, self.misses, self.hot = "chasing", 0, None
            self.seen_at, self.sweep_at, self.fight_ok = {}, 0, {}
            self.ms, self.mem = _Fights((0x1000,)), None
            self.lock = threading.Lock()

        def _positions(self, _):
            return {}                          # every read comes back empty

    blind = _Blind()
    for i in range(MEM_LOST_FRAMES):
        blind.target(1.0 + i)
        assert blind.me and blind.mode == "lost", (i, blind.mode)
    blind.target(99.0)                         # one miss too many: unit is gone
    assert blind.me is None and blind.mode == "no unit", blind.mode
    # A relog rebuilds our unit, so the pointer read from the connection is as
    # dead as the rest. Leaving it set meant the scanner never looked it up
    # again (it only reads when owner is None) and calibration kept pushing on
    # behalf of an object that no longer existed -- "no unit moved when pushed"
    # every 15s, forever, with a healthy character standing there.
    assert blind.owner is None, "the stale owner must go with the unit"


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

    # ArduinoPad wire format, no board attached
    pad = ArduinoPad.__new__(ArduinoPad)
    sent = []
    pad.ser = type("S", (), {"write": lambda _, b: sent.append(b),
                             "readline": lambda _: b"OK"})()
    pad.last = None
    pad.stick(0.0, 1.0)             # stick up -> HID Y negative
    pad.stick(0.0, 1.0)             # repeat must not re-send
    pad.stick(-1.0, 0.0, True)      # move + attack down
    pad.stick(-1.0, 0.0, False)     # stick unchanged -> only the release
    assert sent == [b"L0,-32767\n", b"U4\n",
                    b"L-32767,0\n", b"D4\n",
                    b"U4\n"], sent

    sent.clear()
    for key in BUFF_SEQUENCE:
        pad.tap_dpad(key, 0)
    assert sent == [b"V0\n", b"V-1\n", b"V6\n", b"V-1\n",
                    b"V4\n", b"V-1\n", b"V2\n", b"V-1\n"], sent

    sent.clear()
    pad.tap_button("y")           # by name
    pad.tap_button(3)             # same button by index
    assert sent == [b"B3\n", b"B3\n"], sent

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
    for i in range(WALK_BLOCK_FRAMES + 1):
        hit = learn.observe(100.0 + i * 0.05, 5.0, 5.0, 1.0, 0.0, ident, "chasing")
    assert hit, "a push that goes nowhere must mark"
    assert learn.at(5.0 + WALK_BLOCK_AHEAD, 5.0) in learn.hits, learn.hits

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
    for i in range(WALK_BLOCK_FRAMES + 1):
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
    for i in range(WALK_BLOCK_FRAMES + 1):
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
            pad.tap_dpad(key, hold)
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
            pad.stick(float(sx), float(sy), False)
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
        main(sys.argv[i + 1] if i >= 0 else None)
