"""Entry point with graceful startup failure dialog (never silent close)."""
from __future__ import annotations

import logging
import sys
import traceback
from pathlib import Path


def _ensure_package_on_path() -> None:
    """Make `import scanner` work no matter where Python was launched from.

    Python puts the script's own directory on sys.path, but that breaks when
    main.py is invoked via a wrapper (py.exe launcher, shortcut with a
    different working dir, `python path/to/main.py` from E:\\Warecare, frozen
    exe edge cases, ...). The `scanner` package lives next to this file, so
    insert that directory once, up front.
    """
    base = Path(__file__).resolve().parent
    if str(base) not in sys.path:
        sys.path.insert(0, str(base))


_ensure_package_on_path()


def _log_hint() -> str:
    """Best-effort real log-file location (matches logging_config)."""
    try:
        from scanner.logging_config import get_log_dir
        return str(get_log_dir() / "scanner.log")
    except Exception:
        import platform
        system = platform.system().lower()
        if system == "windows":
            return r"%LOCALAPPDATA%\SmartHardwareDiagnosticScanner\logs\scanner.log"
        if system == "darwin":
            return ("~/Library/Application Support/"
                    "SmartHardwareDiagnosticScanner/logs/scanner.log")
        return "~/.local/share/SmartHardwareDiagnosticScanner/logs/scanner.log"


def main() -> int:
    # main.py is the GUI entry point only. Silently ignoring arguments such as
    # "--scan" would open a window that never exits and never writes a
    # report, so reject them explicitly instead.
    if len(sys.argv) > 1:
        print("main.py does not accept command-line arguments.",
              file=sys.stderr)
        print("For a headless scan use:  python shds_gui.py --scan "
              "[--json report.json] [--html report.html]",
              file=sys.stderr)
        return 2
    try:
        from scanner.app import ScannerApp
        from scanner.logging_config import configure_logging
    except Exception as exc:
        try:
            import tkinter as tk
            from tkinter import messagebox
            root = tk.Tk()
            root.withdraw()
            messagebox.showerror(
                "Startup failed",
                f"Smart Hardware Diagnostic Scanner failed to start.\n\n{exc}\n\n"
                "Install dependencies with:\n  pip install -r requirements.txt\n\n"
                f"See {_log_hint()} for details.")
            root.destroy()
        except Exception:
            print(f"Startup failed: {exc}", file=sys.stderr)
            traceback.print_exc()
        return 1
    try:
        configure_logging()
        app = ScannerApp()
        app.mainloop()
        return 0
    except Exception as exc:
        # Persist to the rotating log (stderr is None in --windowed builds).
        logging.getLogger("shds").exception("Fatal error: %s", exc)
        traceback.print_exc()
        try:
            from tkinter import messagebox
            messagebox.showerror("Fatal error", f"Unexpected error:\n{exc}")
        except Exception:
            pass
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
