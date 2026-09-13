"""Shared visual tokens for the SpiritVale desktop UI."""

BG = "#0d141c"
PANEL = "#121d27"
PANEL_ALT = "#17232d"
BORDER = "#293744"
TEXT = "#d8e0e8"
MUTED = "#8292a0"
CYAN = "#61b5ff"
BLUE = "#4aa7f2"
GREEN = "#6ddd9a"
RED = "#ff343d"
AMBER = "#ffb21a"


STYLESHEET = r"""
* {
    font-family: "Segoe UI";
    font-size: 12px;
    color: #d8e0e8;
}
QMainWindow, QWidget#root { background: #0d141c; }
QFrame#topbar { background: #101922; border-bottom: 1px solid #293440; }
QFrame#sidebar { background: #0f171f; border-right: 1px solid #293440; }
QFrame#panel, QFrame#card {
    background: #121d27;
    border: 1px solid #293744;
    border-radius: 8px;
}
QLabel#title { font-size: 18px; font-weight: 650; color: #eef3f7; }
QLabel#section { font-size: 12px; font-weight: 700; color: #dce8ef; }
QLabel#muted { color: #8292a0; }
QLabel#modeReason { color: #8292a0; background: #0f171f; border-bottom: 1px solid #293440; padding: 4px 18px; }
QLabel#navSection { color: #657686; font-size: 10px; font-weight: 700; padding: 4px 14px; }
QLabel#summaryRow { border-bottom: 1px solid #25333e; padding: 8px 0; }
QLabel#cyan { color: #61b5ff; font-weight: 700; }
QLabel#green { color: #6ddd9a; font-weight: 700; }
QLabel#red { color: #ff5960; font-weight: 700; }
QLabel#amber { color: #ffb21a; font-weight: 700; }
QLabel#pillGreen { background: #12241b; border: 1px solid #286342; border-radius: 5px; color: #6ddd9a; padding: 5px 9px; font-weight: 700; }
QLabel#pillCyan { color: #9cabba; padding: 5px 8px; font-weight: 600; }
QPushButton {
    background: #1a2631; border: 1px solid #394a58; border-radius: 6px;
    padding: 7px 12px; font-weight: 600;
}
QPushButton:hover { background: #223341; border-color: #557083; }
QPushButton:pressed { background: #121c25; }
QPushButton:disabled { color: #516372; background: #0a1721; border-color: #203442; }
QPushButton#nav { background: transparent; border: none; border-radius: 6px; text-align: left; padding: 12px 14px; color: #9cabba; }
QPushButton#nav:hover { background: #172531; color: white; }
QPushButton#nav:checked { background: #1a2a38; color: white; border-left: 3px solid #4aa7f2; }
QPushButton#start { background: #167647; border-color: #2a9b65; color: white; }
QPushButton#start:hover { background: #1b8b56; }
QPushButton#pause { background: #1a2631; border-color: #52616e; color: white; }
QPushButton#stop { background: #1a2631; border-color: #714047; color: #ffb8bd; }
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
    background: #0f171f; border: 1px solid #354653; border-radius: 5px;
    padding: 6px; selection-background-color: #126da5;
}
QComboBox QAbstractItemView { background: #0b1c29; selection-background-color: #145783; }
QPlainTextEdit { background: #101820; border: 1px solid #293744; color: #a9bbc8; font-family: "Consolas"; }
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
