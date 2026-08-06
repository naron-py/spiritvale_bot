"""Press controls on the Leonardo, for Steam's generic-gamepad setup wizard.

Steam has no mapping for an unknown HID pad and runs a wizard asking you to press
each control in turn -- impossible on a board with no buttons. Nothing reaches the
game until that mapping exists.

usage:
  python pad_press.py               # interactive: type what the wizard asks for
  python pad_press.py 0             # one press: button 0 (A)
  python pad_press.py u             # one press: d-pad up
  python pad_press.py lx            # one sweep: left stick X
  python pad_press.py --port COM6 0 # explicit port instead of autodetect

buttons: 0=A 1=B 2=X 3=Y 4=LB 5=RB -- guesses, the wizard tells you what it saw.
Prefer the interactive form for a whole wizard run: it holds one serial connection
open, and opening the port can reset the board, which drops it off the bus.
"""
import sys
import time
from minimap_bot import TAP_HELP, ArduinoPad, press_repl, tap_one

if __name__ == "__main__":
    i = sys.argv.index("--port") if "--port" in sys.argv else -1
    port = sys.argv[i + 1] if i >= 0 else "auto"
    tokens = [a for n, a in enumerate(sys.argv[1:], 1)
              if not a.startswith("--") and n != i + 1]
    time.sleep(1)
    if not tokens:
        press_repl(port)
    else:
        pad = ArduinoPad(port)
        try:
            for t in tokens:
                print(f"  {t}" if tap_one(pad, t) else f"  ? {t}: expected {TAP_HELP}")
        finally:
            pad.close()
