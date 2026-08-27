"""Shared visual tokens for the SpiritVale desktop UI."""

BG = "#06111b"
PANEL = "#091927"
PANEL_ALT = "#0c2031"
BORDER = "#1b3a50"
TEXT = "#d7e4ee"
MUTED = "#8298a8"
CYAN = "#16d9f4"
BLUE = "#258fff"
GREEN = "#66e234"
RED = "#ff343d"
AMBER = "#ffb21a"


STYLESHEET = r"""
* {
    font-family: "Segoe UI";
    font-size: 12px;
    color: #d7e4ee;
}
QMainWindow, QWidget#root { background: #06111b; }
QFrame#topbar, QFrame#footer { background: #071522; border: 1px solid #173247; }
QFrame#sidebar { background: #07131f; border-right: 1px solid #173247; }
QFrame#panel, QFrame#card {
    background: #091927;
    border: 1px solid #1b3a50;
    border-radius: 7px;
}
QLabel#title { font-size: 19px; font-weight: 700; color: #f2f7fa; }
QLabel#section { font-size: 12px; font-weight: 700; color: #dce8ef; }
QLabel#muted { color: #8298a8; }
QLabel#cyan { color: #16d9f4; font-weight: 700; }
QLabel#green { color: #66e234; font-weight: 700; }
QLabel#red { color: #ff5960; font-weight: 700; }
QLabel#amber { color: #ffb21a; font-weight: 700; }
QLabel#pillGreen { background: #062515; border: 1px solid #0a6b32; border-radius: 4px; color: #4df478; padding: 4px 8px; font-weight: 700; }
QLabel#pillCyan { color: #16d9f4; padding: 4px 8px; font-weight: 700; }
QPushButton {
    background: #0d2940; border: 1px solid #246797; border-radius: 5px;
    padding: 7px 12px; font-weight: 600;
}
QPushButton:hover { background: #123754; border-color: #32a4ef; }
QPushButton:pressed { background: #091b2a; }
QPushButton:disabled { color: #516372; background: #0a1721; border-color: #203442; }
QPushButton#nav { background: transparent; border: none; border-radius: 3px; text-align: left; padding: 12px 13px; color: #a9bac6; }
QPushButton#nav:hover { background: #0b2740; color: white; }
QPushButton#nav:checked { background: #0d4a7b; color: white; border-left: 3px solid #31b6ff; }
QPushButton#start { background: #087c37; border-color: #18c65a; color: white; }
QPushButton#start:hover { background: #0a9844; }
QPushButton#pause { background: #a96b05; border-color: #e6a521; color: white; }
QPushButton#stop { background: #8b1d25; border-color: #dc3e48; color: white; }
QPushButton#start:disabled, QPushButton#pause:disabled, QPushButton#stop:disabled {
    color: #516372; background: #0a1721; border-color: #203442;
}
QPushButton#emergency { background: #cf1521; border: 2px solid #ff5960; color: white; font-weight: 800; }
QPushButton#emergency:hover { background: #f01e2b; }
QPushButton#danger { background: #641a21; border-color: #bd3841; }
QPushButton#save { background: #07692e; border-color: #19b951; }
QPushButton#danger:disabled, QPushButton#save:disabled {
    color: #516372; background: #0a1721; border-color: #203442;
}
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox {
    background: #07131f; border: 1px solid #24465e; border-radius: 4px;
    padding: 6px; selection-background-color: #126da5;
}
QComboBox QAbstractItemView { background: #0b1c29; selection-background-color: #145783; }
QPlainTextEdit { background: #06121d; border: 1px solid #1b3a50; color: #a9c1d0; font-family: "Consolas"; }
QGroupBox { border: 1px solid #1b3a50; border-radius: 6px; margin-top: 12px; padding-top: 12px; font-weight: 700; }
QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 4px; color: #bcd0dc; }
QCheckBox::indicator { width: 17px; height: 17px; }
QCheckBox::indicator:unchecked { background: #07131f; border: 1px solid #476172; border-radius: 3px; }
QCheckBox::indicator:checked { background: #15914a; border: 1px solid #44e278; border-radius: 3px; }
QGraphicsView { background: #06131f; border: 1px solid #1b3a50; border-radius: 4px; }
QToolTip { background: #0d2435; color: white; border: 1px solid #3479a5; padding: 5px; }
QScrollBar:vertical { background: #07131f; width: 9px; }
QScrollBar::handle:vertical { background: #24465e; min-height: 24px; border-radius: 4px; }
"""
