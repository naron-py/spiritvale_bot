"""Launch the SpiritVale Farm Bot desktop UI."""

from __future__ import annotations

import argparse
import faulthandler
import logging
from pathlib import Path
import sys
import traceback

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from .config import application_root
from .main_window import MainWindow
from .runtime import DemoRuntime, ProcessRuntime


def build_parser():
    parser = argparse.ArgumentParser(description="SpiritVale Farm Bot desktop UI")
    parser.add_argument("--demo", action="store_true",
                        help="deterministic UI demo; never opens the game or controller")
    parser.add_argument("--screenshot", metavar="PATH",
                        help="demo/testing helper: save the rendered window")
    parser.add_argument("--auto-close", type=int, default=0, metavar="MS",
                        help="testing helper: close after this many milliseconds")
    return parser


def main(argv=None):
    options = build_parser().parse_args(argv)
    project_root = application_root()
    faulthandler.enable()
    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setApplicationName("SpiritVale Farm Bot")
    app.setOrganizationName("SpiritVale Bot")
    runtime = DemoRuntime() if options.demo else ProcessRuntime(project_root)
    window = MainWindow(project_root, runtime, demo_mode=options.demo)

    def exception_hook(exc_type, exc, tb):
        details = "".join(traceback.format_exception(exc_type, exc, tb))
        logging.getLogger("spiritvale.ui.fatal").critical(details)
        try:
            window.controller.emergency_stop("Unhandled UI exception")
            window.append_log("[FATAL] " + details)
        finally:
            QMessageBox.critical(window, "Farm Bot failure",
                                 "The UI failed and requested release of all inputs.\n\n"
                                 "See ui_bot/logs/ui.log for the traceback.")

    sys.excepthook = exception_hook
    window.show()

    if options.demo and options.screenshot:
        output = Path(options.screenshot).resolve()
        output.parent.mkdir(parents=True, exist_ok=True)

        def capture():
            window.grab().save(str(output))
            print(f"screenshot saved: {output}")

        QTimer.singleShot(80, window.start_bot)
        QTimer.singleShot(650, capture)
        QTimer.singleShot(760, lambda: window.emergency_stop("Screenshot complete"))
        QTimer.singleShot(max(850, options.auto_close or 850), window.close)
    elif options.auto_close:
        QTimer.singleShot(max(0, options.auto_close - 100),
                          lambda: window.emergency_stop("Automatic close"))
        QTimer.singleShot(options.auto_close, window.close)

    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
