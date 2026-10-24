"""Dark Neon desktop GUI (CustomTkinter). All widget updates run on the main thread."""
from __future__ import annotations

import json
import os
import platform
import queue
import subprocess
import sys
import tempfile
import threading
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import customtkinter as ctk

from . import theme as T
from . import icons
from .charts import HAS_MPL, ChartPanel
try:
    from . import crashlog as _crashlog
except ImportError:  # scanner/crashlog.py is optional; the UI degrades
    _crashlog = None
from . import ctk_widgets as ctkw
from .actions import ACTION_LABELS, METRIC_LABELS, ActionRule, RuleEngine
from .diagnostics import (DEFAULT_THRESHOLDS, DiagnosticReport, compare_reports,
                          run_diagnostics, system_health_score)
from .explainer import Explanation, explain_crash, explain_finding
from .filesutil import largest_files
from .gpu import GpuStatus
from .hardware import HAS_PSUTIL, HardwareSnapshot, collect_processes, collect_snapshot
from .logging_config import configure_logging
from .monitoring import MonitorWorker, Sample
from .netview import collect_connections
from .reports import build_report_dict, render_comparison_html, save_report
from .scheduler import (SCHTASKS_TASK_NAME, VALID_DAYS, build_cron_line,
                        build_schtasks_argv, merge_crontab,
                        strip_scheduled_entries, validate_time)
from .smart import SmartStatus, query_smart_devices
from .startup import collect_startup_entries
from .updater import UpdateCheck, check_for_update
from .utils import format_bytes, safe_path_for_report

log = configure_logging()

# "Crash Log" is a new page and icons.py's NAV_ICONS predates it; register
# its sidebar icon mapping here (runtime only — icons.py is not modified).
# A crash history reads as a document, so it reuses the existing "reports"
# painter. This keeps NAV_ICONS and NAV_ITEMS in lockstep, which show_page
# and the icon-coverage invariant both rely on.
icons.NAV_ICONS.setdefault("Crash Log", "reports")

NAV_ITEMS = ["Dashboard", "Processes", "Connections", "Startup", "Automation",
             "Diagnostics", "Reports & Tools", "Crash Log"]

#: Display size (px) of the sidebar's vector icons.
ICON_SIZE = 20

SAFE_TEMP_SUFFIXES = {".tmp", ".log", ".bak", ".cache", ".dmp", ".old"}


def _status_of_pct(value: float | None, warn: float, fail: float) -> str:
    if value is None:
        return "UNAVAILABLE"
    if value >= fail:
        return "FAIL"
    if value >= warn:
        return "WARNING"
    return "PASS"


def approved_temp_root() -> Path:
    return Path(tempfile.gettempdir()).resolve()


def preview_temp_candidates(limit: int = 200) -> tuple[list[Path], str]:
    """List safe top-level temp files (non-recursive, allow-listed suffixes only)."""
    root = approved_temp_root()
    try:
        entries = list(root.iterdir())
    except (PermissionError, OSError) as exc:
        return [], f"Cannot read temp dir: {exc}"
    out: list[Path] = []
    for p in entries:
        if len(out) >= limit:
            break
        try:
            if p.is_symlink() or not p.is_file():
                continue
            if p.suffix.lower() not in SAFE_TEMP_SUFFIXES:
                continue
            # validate containment
            p.resolve().relative_to(root)
            out.append(p)
        except (OSError, ValueError):
            continue
    return out, f"Scanned {root} (top level only, safe suffixes)."


# ---------------------------------------------------------------------------
# Crash log (Crash Log page). Data helpers are module-level and pure: they
# read the Windows event log through scanner.crashlog and the
# crashes/crash_*.json records written next to pre-crash snapshots. All of
# them degrade to empty results plus honest notes instead of raising.
# ---------------------------------------------------------------------------
CRASH_ROW_LIMIT = 5        # application-crash rows shown (newest first)
SHUTDOWN_ROW_LIMIT = 3     # unexpected-shutdown rows merged with those
CRASH_ROWS_CAP = 50        # defensive bound after merging saved files

#: The spec'd degrade row, shown when the event log cannot be read at all.
#: The status note (the page's muted line) still carries the precise reason.
CRASH_UNAVAILABLE_ROW = "Event log unavailable (run as administrator)"


def _crash_dirs() -> list[Path]:
    """Directories holding crash_*.json records, most specific first.

    scanner.crashlog.default_crashes_dir() is authoritative when present;
    the documented fallback is <repo root>/crashes next to the scanner
    package.
    """
    candidates: list[Path] = []
    if _crashlog is not None and hasattr(_crashlog, "default_crashes_dir"):
        try:
            candidates.append(Path(_crashlog.default_crashes_dir()))
        except Exception:  # noqa: BLE001 - degrade, never crash the GUI
            log.debug("default_crashes_dir() failed", exc_info=True)
    candidates.append(Path(__file__).resolve().parents[2] / "crashes")
    out: list[Path] = []
    for directory in candidates:
        if directory not in out:
            out.append(directory)
    return out


def _crash_file_records() -> list[tuple[dict, dict | None]]:
    """(crash, snapshot) pairs from saved crashes/crash_*.json files.

    Reader-only, matching the background snapshotter's documented shape
    {"saved_at", "crash", "snapshot"}. Malformed or unreadable files are
    skipped — nothing is invented for them.
    """
    out: list[tuple[dict, dict | None]] = []
    for directory in _crash_dirs():
        try:
            paths = sorted(directory.glob("crash_*.json"))
        except OSError:
            continue
        for path in paths:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(data, dict):
                continue
            crash = data.get("crash")
            if not isinstance(crash, dict) or not crash:
                continue
            snapshot = data.get("snapshot")
            out.append((crash,
                        snapshot if isinstance(snapshot, dict) else None))
    return out


def _crash_row_identity(crash: dict) -> tuple:
    """Stable identity for de-duplicating event-log vs saved-file rows."""
    return (str(crash.get("event_id", "")), str(crash.get("timestamp", "")),
            str(crash.get("app", "")))


def _crash_code_display(crash: dict) -> str:
    """Error Code column text; '—' when the record names no code."""
    raw = crash.get("exception_code")
    if raw in (None, ""):
        return "—"
    if isinstance(raw, int) and not isinstance(raw, bool):
        return f"0x{raw & 0xFFFFFFFF:08X}"
    text = str(raw).strip()
    return text.upper() if text else "—"


def _crash_row(crash: dict, snapshot: dict | None = None,
               shutdown: bool = False) -> dict:
    """Uniform row dict: a crash entry (or one with a stored snapshot)."""
    return {"status_row": "", "crash": crash, "snapshot": snapshot,
            "shutdown": shutdown}


def _crash_status_row(text: str) -> dict:
    return {"status_row": text, "crash": None, "snapshot": None,
            "shutdown": False}


def _crash_sample_line(snapshot: dict | None) -> str:
    """One-line summary of the pre-crash hardware snapshot, or "".

    Reads exactly the documented field names ("cpu", "ram_percent",
    "gpu_temp", "captured_at"); values the snapshot does not carry are
    omitted, never guessed ("No data" semantics).
    """
    if not isinstance(snapshot, dict):
        return ""
    parts: list[str] = []
    cpu = snapshot.get("cpu")
    if isinstance(cpu, (int, float)) and not isinstance(cpu, bool):
        parts.append(f"CPU {float(cpu):.0f}%")
    ram = snapshot.get("ram_percent")
    if isinstance(ram, (int, float)) and not isinstance(ram, bool):
        parts.append(f"RAM {float(ram):.0f}%")
    gpu = snapshot.get("gpu_temp")
    if isinstance(gpu, (int, float)) and not isinstance(gpu, bool):
        parts.append(f"GPU {float(gpu):.0f} °C")
    captured = str(snapshot.get("captured_at") or "").strip()
    if captured:
        parts.append(f"captured {captured}")
    return " · ".join(parts)


def _collect_crash_rows() -> tuple[list[dict], str]:
    """Worker-side collector: crash rows (newest first) plus a status note.

    Rows are the uniform dicts of `_crash_row`; the optional final row is
    the honest UNAVAILABLE entry for an unreadable event log — never a
    fabricated crash. Saved crash_*.json records are merged, de-duplicated
    against the event-log rows, and keep the pre-crash snapshot they were
    written with. Runs off-thread: only plain data moves through the queue.
    """
    rows: list[dict] = []
    status_parts: list[str] = []
    event_unavailable = ""
    if _crashlog is None:
        event_unavailable = "crashlog module missing"
    else:
        try:
            ok, reason = _crashlog.availability()
        except Exception as exc:  # noqa: BLE001 - degrade, never crash GUI
            ok, reason = False, str(exc)
        if ok:
            crashes: list[dict] = []
            shutdowns: list[dict] = []
            try:
                crashes = list(
                    _crashlog.collect_recent_crashes(CRASH_ROW_LIMIT))
            except Exception as exc:  # noqa: BLE001
                log.warning("crash collect failed: %s", exc)
                status_parts.append(f"Crash collection failed: {exc}")
            try:
                shutdowns = list(_crashlog.collect_unexpected_shutdowns(
                    SHUTDOWN_ROW_LIMIT))
            except Exception as exc:  # noqa: BLE001
                log.warning("shutdown collect failed: %s", exc)
                status_parts.append(f"Shutdown collection failed: {exc}")
            rows.extend(_crash_row(crash) for crash in crashes)
            rows.extend(_crash_row(crash, shutdown=True)
                        for crash in shutdowns)
            status_parts.append(f"{len(crashes)} recent crash(es), "
                                f"{len(shutdowns)} unexpected shutdown(s)")
        else:
            event_unavailable = str(reason or "").strip()

    # Merge saved crash_*.json records not already listed (event log wins),
    # attaching each record's stored pre-crash snapshot to its row.
    try:
        files = _crash_file_records()
    except Exception as exc:  # noqa: BLE001 - degrade, never crash GUI
        log.warning("crash file scan failed: %s", exc)
        files = []
    seen = {_crash_row_identity(row["crash"]) for row in rows}
    merged = 0
    for crash, snapshot in files:
        identity = _crash_row_identity(crash)
        if identity in seen:
            for row in rows:  # upgrade a known row with its stored snapshot
                if (row["crash"] is not None
                        and _crash_row_identity(row["crash"]) == identity
                        and row["snapshot"] is None
                        and snapshot is not None):
                    row["snapshot"] = snapshot
            continue
        seen.add(identity)
        merged += 1
        rows.append(_crash_row(crash, snapshot))
    if files:
        dropped = len(files) - merged
        status_parts.append(
            f"{merged} saved file record(s) merged"
            + (f", {dropped} already listed" if dropped else ""))
    if event_unavailable:
        status_parts.insert(0, f"Event log unavailable: {event_unavailable}.")

    def _ts_key(row: dict) -> tuple[bool, str, str]:
        crash = row["crash"] or {}
        timestamp = str(crash.get("timestamp", ""))
        return (bool(timestamp), timestamp, str(crash.get("app", "")))

    try:
        rows.sort(key=_ts_key, reverse=True)  # newest first; undated last
    except Exception:  # noqa: BLE001 - sorting must never break the page
        log.debug("crash row sort failed", exc_info=True)
    del rows[CRASH_ROWS_CAP:]  # merged saved files stay bounded
    note = "; ".join(part for part in status_parts if part)
    if event_unavailable:
        rows.append(_crash_status_row(CRASH_UNAVAILABLE_ROW))
    return rows, note or "No crash records available."


class ScannerApp(ctk.CTk):
    """Main application window."""

    def __init__(self) -> None:
        super().__init__()
        self.title("Smart Hardware Diagnostic Scanner — Dark Neon Edition  |  by Devansh")
        self.geometry("1180x760")
        self.minsize(960, 620)
        self.configure(bg=T.BG_MAIN)

        self.thresholds: dict = dict(DEFAULT_THRESHOLDS)
        self.monitor = MonitorWorker(interval_s=1.0, include_gpu=False)
        self.latest_snap: HardwareSnapshot | None = None
        self.latest_diag: DiagnosticReport | None = None
        self.previous_diag: DiagnosticReport | None = None
        self.previous_payload: dict | None = None
        self.last_comparison: dict | None = None
        self.latest_payload: dict | None = None
        self.cpu_hist: list[float] = []
        self.mem_hist: list[float] = []
        self.refresh_interval = tk.DoubleVar(value=1.0)
        self.proc_sort = tk.StringVar(value="cpu")
        self.monitor_running = False

        self._bg_queue: queue.Queue = queue.Queue()
        self._scan_active = False
        self._refresh_active = False
        self._procs_active = False
        self._crashes_active = False
        self._crashlog_seen = False
        self._crash_rows: dict[str, dict] = {}
        self._crash_explanation: Explanation | None = None
        self._monitor_polling = False
        self._after_ids: set[str] = set()

        # Auto-Action Rules ("The Kill Switch"). The Master Switch is OFF by
        # default — nothing automatic happens until the user flips it here.
        self.rules_enabled = tk.BooleanVar(value=False)
        self.actions_engine = RuleEngine(
            on_log=self._automation_log, self_pid=os.getpid(),
            clean_action=self._automation_clean_temp)

        self._build_style()
        self._build_layout()
        self.show_page("Dashboard")
        self._log("Application started. Optional deps: "
                  f"psutil={'yes' if HAS_PSUTIL else 'NO — metrics unavailable'} / "
                  f"matplotlib={'yes' if HAS_MPL else 'NO — charts disabled'}.", "INFO")
        if not HAS_PSUTIL:
            messagebox.showwarning(
                "Optional dependency missing",
                "psutil is not installed.\nThe app will launch, but live metrics "
                "are unavailable.\nInstall with: pip install psutil")
        self._reschedule(250, self._poll_bg_queue)
        self._reschedule(300, self.refresh_once)
        self.protocol("WM_DELETE_WINDOW", self.on_closing)

    # ---------- styling / layout ----------
    def _build_style(self) -> None:
        """Style the widgets that stay ttk (labels, frames, trees, comboboxes).

        Buttons, entries and progress bars are CustomTkinter widgets and take
        their colours from the palette via `T.ctk_*_kwargs()` instead, so they
        are not configured here.
        """
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("TFrame", background=T.BG_MAIN)
        style.configure("Card.TFrame", background=T.BG_CARD, relief="flat")
        style.configure("Side.TFrame", background=T.BG_SIDEBAR)
        style.configure("TLabel", background=T.BG_MAIN, foreground=T.FG_PRIMARY,
                        font=T.FONT_BODY)
        style.configure("Card.TLabel", background=T.BG_CARD, foreground=T.FG_PRIMARY)
        style.configure("Muted.TLabel", background=T.BG_CARD, foreground=T.FG_MUTED,
                        font=T.FONT_SMALL)
        style.configure("Title.TLabel", background=T.BG_MAIN, foreground=T.FG_PRIMARY,
                        font=T.FONT_TITLE)
        style.configure("Head.TLabel", background=T.BG_MAIN, foreground=T.NEON_CYAN,
                        font=T.FONT_HEADING)
        style.configure("Treeview", background=T.BG_PANEL, fieldbackground=T.BG_PANEL,
                        foreground=T.FG_PRIMARY, rowheight=24, font=T.FONT_BODY)
        style.configure("Treeview.Heading", background=T.BG_SIDEBAR,
                        foreground=T.NEON_CYAN, font=T.FONT_SMALL)
        style.configure("TSpinbox", fieldbackground=T.ENTRY_BG, background=T.ENTRY_BG,
                        foreground=T.FG_PRIMARY)
        style.configure("TEntry", fieldbackground=T.ENTRY_BG, foreground=T.FG_PRIMARY)

    def _build_layout(self) -> None:
        # Sidebar (240 px): brand, icon navigation, live controls.
        self.sidebar = tk.Frame(self, bg=T.BG_SIDEBAR, width=240)
        self.sidebar.pack(side="left", fill="y")
        self.sidebar.pack_propagate(False)
        brand = tk.Label(self.sidebar, text="◈  SHDS", bg=T.BG_SIDEBAR,
                         fg=T.NEON_CYAN, font=(T.FONT_FAMILY, 17, "bold"))
        brand.pack(pady=(18, 2), padx=16, anchor="w")
        sub = tk.Label(self.sidebar, text="Hardware Diagnostic Scanner\nby Devansh",
                       bg=T.BG_SIDEBAR, fg=T.FG_MUTED, font=T.FONT_SMALL,
                       justify="left")
        sub.pack(padx=16, anchor="w", pady=(0, 14))
        self.nav_buttons: dict[str, ctk.CTkButton] = {}
        for item in NAV_ITEMS:
            btn = T.make_side_button(self.sidebar, T.BG_SIDEBAR,
                                     text=f" {item}",
                                     image=self._nav_icon(item, active=False),
                                     command=lambda n=item: self.show_page(n))
            btn.pack(fill="x", padx=10, pady=3)
            self.nav_buttons[item] = btn
        tk.Frame(self.sidebar, bg=T.BORDER, height=1).pack(
            fill="x", padx=16, pady=(14, 0))
        tk.Label(self.sidebar, text="Refresh interval (s):", bg=T.BG_SIDEBAR,
                 fg=T.FG_MUTED, font=T.FONT_SMALL).pack(padx=16, pady=(12, 2),
                                                        anchor="w")
        intervals = ttk.Combobox(self.sidebar, textvariable=self.refresh_interval,
                                 values=["0.5", "1.0", "2.0", "5.0"], width=8,
                                 state="readonly")
        intervals.pack(padx=16, anchor="w")
        intervals.bind("<<ComboboxSelected>>", lambda _e: self._apply_interval())
        self.mon_btn = T.make_button(self.sidebar, "accent", T.BG_SIDEBAR,
                                     text="▶  Start monitoring",
                                     command=self.toggle_monitoring)
        self.mon_btn.pack(fill="x", padx=16, pady=(16, 6))
        self.theme_btn = T.make_button(
            self.sidebar, "ghost", T.BG_SIDEBAR,
            text="◐  Switch to light" if T.get_mode() == "dark" else "◐  Switch to dark",
            command=self._toggle_theme)
        self.theme_btn.pack(fill="x", padx=16)
        self._update_btn = T.make_button(
            self.sidebar, "ghost", T.BG_SIDEBAR,
            text="⇩  Check for updates",
            command=self.check_for_updates)
        self._update_btn.pack(fill="x", padx=16, pady=(6, 0))

        # Main column
        main_col = ttk.Frame(self)
        main_col.pack(side="left", fill="both", expand=True)
        header = ttk.Frame(main_col)
        header.pack(fill="x", padx=18, pady=(14, 6))
        self.page_title = ttk.Label(header, text="Hardware Dashboard",
                                    style="Title.TLabel")
        self.page_title.pack(side="left")
        self.head_status = tk.Label(header, text="● idle", bg=T.BG_MAIN,
                                    fg=T.FG_MUTED, font=T.FONT_SMALL)
        self.head_status.pack(side="right")

        self.pages_frame = ttk.Frame(main_col)
        self.pages_frame.pack(fill="both", expand=True, padx=18, pady=6)
        self.pages: dict[str, ttk.Frame] = {}
        for name in NAV_ITEMS:
            frame = ttk.Frame(self.pages_frame)
            frame.place(relx=0, rely=0, relwidth=1, relheight=1)
            self.pages[name] = frame
        self._build_dashboard(self.pages["Dashboard"])
        self._build_processes(self.pages["Processes"])
        self._build_connections(self.pages["Connections"])
        self._build_startup(self.pages["Startup"])
        self._build_automation(self.pages["Automation"])
        self._build_diagnostics(self.pages["Diagnostics"])
        self._build_reports(self.pages["Reports & Tools"])
        self._build_crashlog(self.pages["Crash Log"])

        # Status bar
        self.status_var = tk.StringVar(value="Ready.")
        self.scan_var = tk.StringVar(value="Last scan: never")
        bar = tk.Frame(self, bg=T.BG_STATUSBAR, height=28)
        # Pack the bottom strip before the expanding main column: pack grants
        # parcels in order, so a later "side=bottom" slave otherwise takes its
        # requested width out of the left-hand region, starving the cards.
        bar.pack(side="bottom", fill="x", before=main_col)
        tk.Label(bar, textvariable=self.status_var, bg=T.BG_STATUSBAR,
                 fg=T.FG_MUTED, font=T.FONT_SMALL).pack(side="left", padx=12)
        tk.Label(bar, textvariable=self.scan_var, bg=T.BG_STATUSBAR,
                 fg=T.NEON_PURPLE, font=T.FONT_SMALL).pack(side="right", padx=12)

    # ---------- pages ----------
    def _nav_icon(self, item: str, active: bool):
        """Vector icon for one sidebar entry (cyan when active, muted else)."""
        return icons.get_icon(icons.NAV_ICONS[item], ICON_SIZE,
                              T.NEON_CYAN if active else T.FG_MUTED)

    def _card(self, parent, title: str, icon_name: str, accent_key: str):
        """Build one rounded dashboard card.

        Layout: vector icon top-left with the title beside it, the live
        value in large bold text below, a progress bar and a detail line.
        Returns (card, value_label, bar, detail_label, status_dot, icon_label);
        `self.cards[title]` keeps the (value, bar, detail, dot) tuple the
        snapshot renderer updates.
        """
        accent = getattr(T, accent_key)
        card = T.make_card(parent, T.BG_MAIN)
        card.configure(width=T.CARD_MIN_WIDTH, height=T.CARD_HEIGHT)
        card.grid_propagate(False)
        card.columnconfigure(0, weight=1)
        card.rowconfigure(3, weight=1)
        top = tk.Frame(card, bg=T.BG_CARD)
        top.grid(row=0, column=0, sticky="ew", padx=12, pady=(10, 0))
        icon = ctk.CTkLabel(top, image=icons.get_icon(icon_name, 22, accent),
                            text="", bg_color=T.BG_CARD, width=26)
        icon.pack(side="left")
        tk.Label(top, text=title, bg=T.BG_CARD, fg=accent,
                 font=T.FONT_HEADING).pack(side="left", padx=(8, 0))
        dot = tk.Label(top, text="●", bg=T.BG_CARD, fg=T.FG_DIM, font=T.FONT_BODY)
        dot.pack(side="right")
        val = tk.Label(card, text="—", bg=T.BG_CARD, fg=T.FG_PRIMARY,
                       font=(T.FONT_FAMILY, 16, "bold"), anchor="w")
        val.grid(row=1, column=0, sticky="ew", padx=12, pady=(4, 0))
        bar = ctkw.make_progress(card, T.BG_CARD, mode="determinate", maximum=100)
        bar.grid(row=2, column=0, sticky="ew", padx=12, pady=(6, 0))
        detail = tk.Label(card, text="", bg=T.BG_CARD, fg=T.FG_MUTED,
                          font=T.FONT_SMALL, wraplength=T.CARD_MIN_WIDTH - 36,
                          justify="left", anchor="nw")
        detail.grid(row=3, column=0, sticky="ew", padx=12, pady=(4, 8))
        return card, val, bar, detail, dot, icon

    def _build_dashboard(self, page) -> None:
        # "System Health": one combined 0-100 score across CPU, RAM, disk and
        # the OS-level checks, presented as a wide hero card above the five
        # metric cards (whose single-row grid layout is covered by tests).
        health = T.make_card(page, T.BG_MAIN, height=92)
        health.pack(fill="x", pady=(0, 8))
        health.grid_propagate(False)
        health.columnconfigure(2, weight=1)
        health.rowconfigure(0, weight=1)
        self.health_icon = ctk.CTkLabel(
            health, image=icons.get_icon("security", 28, T.NEON_CYAN),
            text="", bg_color=T.BG_CARD, width=34)
        self.health_icon.grid(row=0, column=0, rowspan=2, padx=(14, 6),
                              pady=10, sticky="ns")
        left = tk.Frame(health, bg=T.BG_CARD)
        left.grid(row=0, column=1, sticky="nsw", padx=(0, 10), pady=8)
        tk.Label(left, text="SYSTEM HEALTH", bg=T.BG_CARD, fg=T.FG_MUTED,
                 font=T.FONT_SMALL).pack(anchor="w")
        score_row = tk.Frame(left, bg=T.BG_CARD)
        score_row.pack(anchor="w")
        self.health_var = tk.StringVar(value="—")
        self.health_label = tk.Label(score_row, textvariable=self.health_var,
                                     bg=T.BG_CARD, fg=T.FG_DIM,
                                     font=(T.FONT_FAMILY, 24, "bold"))
        self.health_label.pack(side="left")
        tk.Label(score_row, text="/ 100", bg=T.BG_CARD, fg=T.FG_MUTED,
                 font=T.FONT_SMALL).pack(side="left", anchor="s",
                                         padx=(4, 0), pady=(0, 3))
        right = tk.Frame(health, bg=T.BG_CARD)
        right.grid(row=0, column=2, sticky="ew", padx=(0, 14), pady=8)
        right.columnconfigure(0, weight=1)
        self.health_bar = ctkw.make_progress(right, T.BG_CARD, height=10)
        self.health_bar.grid(row=0, column=0, sticky="ew", pady=(6, 2))
        self.health_bar.set(0)
        self.health_detail = tk.Label(
            right, text="Waiting for the first metrics sample…", bg=T.BG_CARD,
            fg=T.FG_MUTED, font=T.FONT_SMALL, justify="left", anchor="w",
            wraplength=420)
        self.health_detail.grid(row=1, column=0, sticky="ew")

        # Cards sit in a single-row grid whose column widths are recomputed
        # from the row's actual pixel width on every resize, so all five
        # cards stay visible at any window size (default 1180 and min 960,
        # and narrower as well). This replaces the fixed-width pack layout
        # that overflowed the window.
        self.cards_row = ttk.Frame(page)
        self.cards_row.pack(fill="x", pady=(0, 8))
        # (title, icon name, palette accent key) per card.
        self._card_titles = [("CPU", "cpu", "NEON_CYAN"),
                             ("RAM", "ram", "NEON_PURPLE"),
                             ("GPU", "gpu", "NEON_GREEN"),
                             ("Disk", "disk", "AMBER"),
                             ("Network", "network", "NEON_YELLOW")]
        self.cards: dict[str, tuple] = {}
        self._card_icons: dict[str, tuple] = {}
        self._card_slots: list[tuple[object, int]] = []
        for col, (title, icon_name, accent_key) in enumerate(self._card_titles):
            card, val, bar, detail, dot, icon = self._card(
                self.cards_row, title, icon_name, accent_key)
            card.grid(row=0, column=col, sticky="nsew", padx=(0, 8))
            self.cards_row.columnconfigure(col, weight=1, minsize=0)
            self.cards[title] = (val, bar, detail, dot)
            self._card_slots.append((card, col))
            self._card_icons[title] = (icon, icon_name, accent_key)
        # Rows must not grow vertically: cards keep CARD_HEIGHT, the chart
        # below absorbs the rest.
        self.cards_row.rowconfigure(0, weight=0)
        self.cards_row.bind("<Configure>", self._on_cards_resize)
        self._last_card_width = 0

        btn_row = ttk.Frame(page)
        btn_row.pack(fill="x", pady=(0, 8))
        T.make_button(btn_row, "ghost", T.BG_MAIN, text="⟳  Refresh now",
                      command=self.refresh_once).pack(side="left", padx=(0, 8))
        T.make_button(btn_row, "accent", T.BG_MAIN, text="♥  Run diagnostic scan",
                      command=self.run_scan).pack(side="left")
        ttk.Label(btn_row, text="Charts: CPU + RAM (live)" if HAS_MPL else
                  "Charts unavailable — matplotlib not installed.",
                  font=T.FONT_SMALL, foreground=T.FG_MUTED,
                  background=T.BG_MAIN).pack(side="right")
        chart_box = tk.Frame(page, bg=T.BG_CARD, highlightbackground=T.BORDER,
                             highlightthickness=1)
        chart_box.pack(fill="both", expand=True, pady=(0, 4))
        if HAS_MPL:
            self.chart = ChartPanel(chart_box, bg=T.BG_CARD, fg=T.FG_PRIMARY)
        else:
            tk.Label(chart_box, text="Install matplotlib for live charts:\n"
                     "pip install matplotlib", bg=T.BG_CARD, fg=T.FG_MUTED,
                     font=T.FONT_BODY, justify="center").pack(expand=True, pady=30)
            self.chart = None

    def _on_cards_resize(self, event) -> None:
        # The grid distributes the row width across the five equal-weight
        # columns; sync each card's requested width to its actual column width
        # (minus the inter-card padding) so details wrap correctly and cards
        # never force the row wider than the window.
        row_width = event.width
        n = len(self._card_slots)
        if n == 0 or row_width < T.CARD_MIN_WIDTH:
            return
        col_width = row_width // n
        target = max(60, col_width - 8)  # minus padx=(0, 8)
        if abs(target - self._last_card_width) < 1:
            return
        self._last_card_width = target
        wrap = max(48, target - 36)
        for card, _col in self._card_slots:
            try:
                card.configure(width=target)
            except tk.TclError:
                pass
            for child in card.winfo_children():
                if isinstance(child, tk.Label):
                    try:
                        child.configure(wraplength=wrap)
                    except tk.TclError:
                        pass

    def _build_processes(self, page) -> None:
        ttk.Label(page, text="Top processes (read-only — nothing is terminated)",
                  style="Head.TLabel").pack(anchor="w", pady=(0, 6))
        row = ttk.Frame(page)
        row.pack(fill="x", pady=(0, 6))
        ttk.Radiobutton(row, text="Sort by CPU", variable=self.proc_sort,
                        value="cpu").pack(side="left", padx=(0, 10))
        ttk.Radiobutton(row, text="Sort by memory", variable=self.proc_sort,
                        value="mem").pack(side="left")
        ctkw.make_button(row, "ghost", T.BG_MAIN, text="Refresh",
                         command=self.refresh_processes).pack(side="right")
        cols = ("pid", "name", "cpu", "mem")
        self.proc_tree = ttk.Treeview(page, columns=cols, show="headings", height=16)
        for c, w, label in [("pid", 90, "PID"), ("name", 420, "Name"),
                            ("cpu", 110, "CPU %"), ("mem", 130, "Mem MB")]:
            self.proc_tree.heading(c, text=label)
            self.proc_tree.column(c, width=w, anchor="w" if c == "name" else "center")
        vsb = ttk.Scrollbar(page, orient="vertical", command=self.proc_tree.yview)
        self.proc_tree.configure(yscrollcommand=vsb.set)
        self.proc_tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="left", fill="y")

    def _build_connections(self, page) -> None:
        ttk.Label(page, text="Active network connections (read-only)",
                  style="Head.TLabel").pack(anchor="w", pady=(0, 6))
        ttk.Label(page, text="A snapshot of INET connections with the owning "
                  "process where visible. Nothing is listed silently as blank.",
                  style="Muted.TLabel").pack(anchor="w", pady=(0, 6))
        row = ttk.Frame(page)
        row.pack(fill="x", pady=(0, 6))
        self.conn_note = tk.StringVar(value="Not loaded yet.")
        ttk.Label(row, textvariable=self.conn_note,
                  style="Muted.TLabel").pack(side="left", padx=(0, 8))
        ctkw.make_button(row, "ghost", T.BG_MAIN, text="Refresh",
                         command=self.refresh_connections).pack(side="right")
        cols = ("kind", "local", "remote", "status", "pid", "process")
        self.conn_tree = ttk.Treeview(page, columns=cols, show="headings", height=16)
        for c, w, label in [("kind", 70, "Type"), ("local", 220, "Local"),
                            ("remote", 220, "Remote"), ("status", 130, "Status"),
                            ("pid", 80, "PID"), ("process", 240, "Process")]:
            self.conn_tree.heading(c, text=label)
            self.conn_tree.column(c, width=w, anchor="w" if c in ("process", "status")
                                  else "center")
        vsb = ttk.Scrollbar(page, orient="vertical", command=self.conn_tree.yview)
        self.conn_tree.configure(yscrollcommand=vsb.set)
        self.conn_tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="left", fill="y")

    def refresh_connections(self) -> None:
        """Collect connections off-thread; rows land via the background queue."""
        threading.Thread(target=self._connections_job, name="shds-conn",
                         daemon=True).start()
        self.set_status("Collecting connections…")

    def _connections_job(self) -> None:
        try:
            rows, note = collect_connections(limit=200)
        except Exception as exc:  # noqa: BLE001 - degrade, never crash GUI
            log.warning("connection collect failed: %s", exc)
            self._bg_queue.put(("connections", ([], f"Connection query failed: {exc}")))
            return
        self._bg_queue.put(("connections", (rows, note)))

    def _render_connections(self, data) -> None:
        rows, note = data
        try:
            self.conn_tree.delete(*self.conn_tree.get_children())
            for row in rows:
                self.conn_tree.insert("", "end", values=(
                    row.kind, row.local, row.remote, row.status,
                    row.pid if row.pid else "—", row.process or "—"))
            self.conn_note.set(note)
        except tk.TclError:
            pass
        self.set_status(f"Connections: {note}")

    def _build_startup(self, page) -> None:
        ttk.Label(page, text="Startup entries (read-only — nothing is disabled)",
                  style="Head.TLabel").pack(anchor="w", pady=(0, 6))
        ttk.Label(page, text="Programs registered to start with your session: "
                  "Windows Run keys + Startup folder; Linux autostart files; "
                  "macOS LaunchAgents. Sorting is informational only.",
                  style="Muted.TLabel").pack(anchor="w", pady=(0, 6))
        row = ttk.Frame(page)
        row.pack(fill="x", pady=(0, 6))
        self.startup_note = tk.StringVar(value="Not loaded yet.")
        ttk.Label(row, textvariable=self.startup_note,
                  style="Muted.TLabel").pack(side="left", padx=(0, 8))
        ctkw.make_button(row, "ghost", T.BG_MAIN, text="Refresh",
                         command=self.refresh_startup).pack(side="right")
        cols = ("name", "kind", "source", "command", "note")
        self.startup_tree = ttk.Treeview(page, columns=cols, show="headings",
                                         height=16)
        for c, w, label in [("name", 180, "Name"), ("kind", 120, "Type"),
                            ("source", 260, "Source"), ("command", 320, "Command"),
                            ("note", 110, "Note")]:
            self.startup_tree.heading(c, text=label)
            self.startup_tree.column(c, width=w, anchor="w")
        vsb = ttk.Scrollbar(page, orient="vertical",
                            command=self.startup_tree.yview)
        self.startup_tree.configure(yscrollcommand=vsb.set)
        self.startup_tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="left", fill="y")

    def refresh_startup(self) -> None:
        threading.Thread(target=self._startup_job, name="shds-startup",
                         daemon=True).start()
        self.set_status("Collecting startup entries…")

    def _startup_job(self) -> None:
        try:
            entries, note = collect_startup_entries()
        except Exception as exc:  # noqa: BLE001 - degrade, never crash GUI
            log.warning("startup collect failed: %s", exc)
            self._bg_queue.put(("startup", ([], f"Startup enumeration failed: {exc}")))
            return
        self._bg_queue.put(("startup", (entries, note)))

    def _render_startup(self, data) -> None:
        entries, note = data
        try:
            self.startup_tree.delete(*self.startup_tree.get_children())
            for entry in entries:
                self.startup_tree.insert("", "end", values=(
                    entry.name or "—", entry.kind, entry.source,
                    entry.command or "—", entry.note))
            self.startup_note.set(note)
        except tk.TclError:
            pass
        self.set_status(f"Startup: {note}")

    # ---------- crash log (event-log history + saved snapshot files) ----------
    def _build_crashlog(self, page) -> None:
        ttk.Label(page, text="Crash log (read-only — nothing is deleted or "
                  "changed)", style="Head.TLabel").pack(anchor="w", pady=(0, 6))
        ttk.Label(page, text="Recent application crashes and unexpected "
                  "shutdowns, newest first. Select a row for a plain-English "
                  "explanation — and for the hardware snapshot taken seconds "
                  "before the crash when the background recorder saved one.",
                  style="Muted.TLabel", wraplength=800,
                  justify="left").pack(anchor="w", pady=(0, 6))
        row = ttk.Frame(page)
        row.pack(fill="x", pady=(0, 6))
        self.crash_note = tk.StringVar(value="Not loaded yet.")
        ttk.Label(row, textvariable=self.crash_note,
                  style="Muted.TLabel").pack(side="left", padx=(0, 8))
        ctkw.make_button(row, "ghost", T.BG_MAIN, text="Refresh",
                         command=self.refresh_crashlog).pack(side="right")
        # Normal container nesting (the tree's MASTER is tree_box, no in_=
        # re-pack): re-packing a page child into a later sub-frame left the
        # Treeview mapped but never painted on Tk 9; a plain nested pack
        # renders it normally and keeps the intended stacked layout.
        tree_box = ttk.Frame(page)
        tree_box.pack(fill="x", pady=(0, 6))
        cols = ("timestamp", "app", "errcode")
        self.crash_tree = ttk.Treeview(tree_box, columns=cols, show="headings",
                                       height=7)
        for c, w, label in [("timestamp", 165, "Timestamp"),
                            ("app", 420, "App"),
                            ("errcode", 140, "Error Code")]:
            self.crash_tree.heading(c, text=label)
            self.crash_tree.column(c, width=w, anchor="w")
        self.crash_tree.tag_configure("unavailable", foreground=T.FG_MUTED)
        vsb = ttk.Scrollbar(tree_box, orient="vertical",
                            command=self.crash_tree.yview)
        self.crash_tree.configure(yscrollcommand=vsb.set)
        self.crash_tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="left", fill="y")
        self.crash_tree.bind("<<TreeviewSelect>>", self._on_crash_select)

        # Detail: plain-English explanation for the selected crash plus the
        # exact pre-crash hardware sample, when one was recorded beside it.
        detail_box = tk.LabelFrame(page, text="Selected crash", bg=T.BG_PANEL,
                                   fg=T.NEON_CYAN, font=T.FONT_SMALL,
                                   padx=10, pady=8)
        detail_box.pack(fill="both", expand=True, pady=(8, 0))
        self.crash_detail = tk.Text(detail_box, height=6, bg=T.BG_OUTPUT,
                                    fg=T.FG_PRIMARY, font=T.FONT_MONO,
                                    wrap="word", state="disabled")
        dsb = ttk.Scrollbar(detail_box, orient="vertical",
                            command=self.crash_detail.yview)
        self.crash_detail.configure(yscrollcommand=dsb.set)
        self.crash_detail.pack(side="left", fill="both", expand=True, padx=2,
                               pady=2)
        dsb.pack(side="left", fill="y")
        crow = tk.Frame(detail_box, bg=T.BG_PANEL)
        crow.pack(fill="x")
        self.crash_snapshot_label = tk.Label(
            crow, text="", bg=T.BG_PANEL, fg=T.FG_MUTED, font=T.FONT_SMALL,
            anchor="w", justify="left", wraplength=720)
        self.crash_snapshot_label.pack(side="left", fill="x", expand=True)
        ctkw.make_button(crow, "ghost", T.BG_PANEL, text="How to fix",
                         width=96,
                         command=self._show_crash_fix).pack(side="right",
                                                            padx=(8, 0))
        self._set_crash_detail("Select a crash row to see a plain-English "
                               "explanation.")

    def refresh_crashlog(self) -> None:
        """Collect crash history off-thread; rows land via the bg queue."""
        if self._crashes_active:
            return
        self._crashes_active = True
        try:
            threading.Thread(target=self._crash_job, name="shds-crashlog",
                             daemon=True).start()
        except (RuntimeError, OSError) as exc:
            log.warning("crash thread failed to start: %s", exc)
            self._crashes_active = False
            self.set_status("Crash log unavailable: background thread could "
                            "not start.")
            return
        self.set_status("Collecting crash history…")

    def _crash_job(self) -> None:
        """Worker: plain data only — no Tkinter access from here."""
        try:
            rows, note = _collect_crash_rows()
        except Exception as exc:  # noqa: BLE001 - degrade, never crash GUI
            log.warning("crash history failed: %s", exc)
            rows, note = [], f"Crash history failed: {exc}"
        finally:
            self._crashes_active = False
        self._bg_queue.put(("crashes", (rows, note)))

    def _render_crashlog(self, data) -> None:
        rows, note = data
        try:
            self.crash_tree.delete(*self.crash_tree.get_children())
        except tk.TclError:
            return
        self._crash_rows = {}
        self._crash_explanation = None
        for entry in rows:
            if entry["status_row"]:
                # UNAVAILABLE row, never a fabricated crash — muted colour.
                iid = self.crash_tree.insert(
                    "", "end", values=("—", entry["status_row"], "—"),
                    tags=("unavailable",))
                self._crash_rows[iid] = entry
                continue
            crash = entry["crash"]
            if entry["shutdown"]:
                app = "Windows (unexpected shutdown)"
                code = "—"
            else:
                app = str(crash.get("app") or "").strip() or "—"
                code = _crash_code_display(crash)
            ts = str(crash.get("timestamp") or "").strip() or "—"
            iid = self.crash_tree.insert("", "end", values=(ts, app, code))
            self._crash_rows[iid] = entry
        self.crash_note.set(note or "No rows.")
        self._set_crash_detail("Select a crash row to see a plain-English "
                               "explanation.")
        self.crash_snapshot_label.configure(text="", fg=T.FG_MUTED)
        self.set_status(f"Crash log: {note or 'no rows'}")

    def _on_crash_select(self, _event=None) -> None:
        """Populate the explanation panel from the explainer (main thread)."""
        try:
            selection = self.crash_tree.selection()
        except tk.TclError:
            return
        if not selection:
            return
        entry = self._crash_rows.get(selection[0])
        if entry is None:
            return
        if entry["status_row"]:
            self._crash_explanation = Explanation(
                "Event log unavailable",
                "The Windows event log could not be read, so this row marks "
                "it unavailable instead of showing a guess. Saved "
                "crashes/crash_*.json records are still listed.",
                "Restart the scanner with administrator rights ('Run as "
                "administrator') so the event log can be read, then press "
                "Refresh. Nothing was changed in the meantime.", "info")
            self._set_crash_detail(
                f"{entry['status_row']}\n"
                f"{self._crash_explanation.explanation}")
            self.crash_snapshot_label.configure(text="", fg=T.FG_MUTED)
            return
        crash = entry["crash"]
        explanation = explain_crash(crash, entry["snapshot"])
        self._crash_explanation = explanation
        parts: list[str] = []
        if explanation is not None:
            parts.append(explanation.explanation)
        message = str(crash.get("message") or "").strip()
        if message and (explanation is None
                        or message not in explanation.explanation):
            parts.append(f"Recorded at {crash.get('timestamp', 'unknown time')}:"
                         f"\n{message}")
        self._set_crash_detail("\n\n".join(p for p in parts if p.strip())
                               if parts
                               else "No explanation available for this "
                                    "record.")
        sample = _crash_sample_line(entry["snapshot"])
        if sample:
            self.crash_snapshot_label.configure(
                text=f"Hardware at crash: {sample}", fg=T.FG_PRIMARY)
        else:
            self.crash_snapshot_label.configure(
                text="No hardware snapshot stored for this crash.",
                fg=T.FG_MUTED)

    def _show_crash_fix(self) -> None:
        """Show the composed, generic advice for the selected crash."""
        explanation = self._crash_explanation
        if explanation is None:
            messagebox.showinfo("How to fix", "Select a crash row first.")
            return
        self._log(f"Crash fix guidance — {explanation.title}: "
                  f"{explanation.explanation}", "INFO")
        self._log(f"    {explanation.fix}", "INFO")
        messagebox.showinfo(explanation.title,
                            f"{explanation.explanation}\n\n"
                            f"{explanation.fix}")

    def _set_crash_detail(self, text: str) -> None:
        try:
            self.crash_detail.configure(state="normal")
            self.crash_detail.delete("1.0", "end")
            self.crash_detail.insert("end", text)
            self.crash_detail.configure(state="disabled")
        except tk.TclError:
            pass

    # ---------- automation (Auto-Action Rules / Kill Switch) ----------
    def _build_automation(self, page) -> None:
        ttk.Label(page, text="Auto-Action Rules (The Kill Switch)",
                  style="Head.TLabel").pack(anchor="w", pady=(0, 4))
        ttk.Label(page, text=(
            "⚠️ Very powerful: when a rule's threshold stays exceeded for the "
            "whole duration, its action runs automatically. Kill rules "
            "terminate the single worst eligible process (critical system "
            "processes are never touched). Rules are only evaluated while "
            "monitoring is running, and only while the Master Switch is on."),
            style="Muted.TLabel", wraplength=680, justify="left").pack(
            anchor="w", pady=(0, 8))

        # Master Switch
        master_row = ttk.Frame(page)
        master_row.pack(fill="x", pady=(0, 8))
        self.rules_master = ctkw.make_switch(
            master_row, T.BG_MAIN, text="Enable Auto-Action Rules",
            variable=self.rules_enabled, command=self._toggle_rules_master)
        self.rules_master.pack(side="left")

        # Rule builder
        add_box = tk.LabelFrame(page, text="Add rule", bg=T.BG_PANEL,
                                fg=T.NEON_CYAN, font=T.FONT_SMALL,
                                padx=10, pady=8)
        add_box.pack(fill="x", pady=(8, 8))
        self.rule_metric_var = tk.StringVar(value=METRIC_LABELS["cpu"])
        self.rule_threshold_var = tk.StringVar(value="95")
        self.rule_duration_var = tk.StringVar(value="120")
        self.rule_action_var = tk.StringVar(value=ACTION_LABELS["kill_process"])

        fields = [
            ("Metric (watched)", ctkw.make_menu(
                add_box, T.BG_PANEL, values=list(METRIC_LABELS.values()),
                variable=self.rule_metric_var, width=90)),
            ("Threshold (%)", ctkw.make_entry(add_box, T.BG_PANEL,
                                              textvariable=self.rule_threshold_var,
                                              width=10)),
            ("Duration (s)", ctkw.make_entry(add_box, T.BG_PANEL,
                                             textvariable=self.rule_duration_var,
                                             width=10)),
            ("Action (when fired)", ctkw.make_menu(
                add_box, T.BG_PANEL, values=list(ACTION_LABELS.values()),
                variable=self.rule_action_var, width=170)),
        ]
        brow = ttk.Frame(add_box)
        brow.pack(fill="x")
        for label_text, widget in fields:
            col = ttk.Frame(brow)
            col.pack(side="left", padx=(0, 10))
            tk.Label(col, text=label_text, bg=T.BG_PANEL, fg=T.FG_MUTED,
                     font=T.FONT_SMALL).pack(anchor="w")
            widget.pack(anchor="w", pady=(2, 0))
        ctkw.make_button(add_box, "accent", T.BG_PANEL, text="Add Rule",
                         command=self._add_rule).pack(anchor="w", pady=(8, 0))

        # Active rules list
        ttk.Label(page, text="Active rules", style="Head.TLabel").pack(
            anchor="w", pady=(8, 2))
        cols = ("metric", "threshold", "duration", "action", "fired")
        self.rules_tree = ttk.Treeview(page, columns=cols, show="headings",
                                       height=7)
        for c, w, label in [("metric", 90, "Metric"), ("threshold", 110, "Threshold"),
                            ("duration", 110, "Duration"), ("action", 170, "Action"),
                            ("fired", 80, "Fired")]:
            self.rules_tree.heading(c, text=label)
            self.rules_tree.column(c, width=w, anchor="w", stretch=False)
        self.rules_tree.column("duration", stretch=True)
        self.rules_tree.pack(fill="x", pady=(0, 6))
        rrow = ttk.Frame(page)
        rrow.pack(fill="x")
        ctkw.make_button(rrow, "ghost", T.BG_MAIN, text="Remove Selected",
                         command=self._remove_selected_rule).pack(side="left",
                                                                  padx=(0, 8))
        ttk.Label(rrow, text="A rule fires only when its metric stays over the "
                  "threshold for the whole duration; the timer resets when it "
                  "drops back below.", style="Muted.TLabel").pack(side="left")

    def _refresh_rules_tree(self) -> None:
        """Render the engine's rule list (values used by the Remove button)."""
        try:
            self.rules_tree.delete(*self.rules_tree.get_children())
            for i, rule in enumerate(self.actions_engine.rules):
                self.rules_tree.insert(
                    "", "end", iid=str(i),
                    values=(METRIC_LABELS[rule.metric],
                            f">{rule.threshold:g}%",
                            f"{rule.duration_s:g}s",
                            ACTION_LABELS[rule.action_type],
                            rule.times_fired),
                    tags=(f"{rule.metric}|{rule.threshold:g}|"
                          f"{rule.duration_s:g}|{rule.action_type}",))
        except (AttributeError, tk.TclError):
            pass

    def _add_rule(self) -> None:
        try:
            threshold = float(self.rule_threshold_var.get())
            duration = float(self.rule_duration_var.get())
        except (ValueError, tk.TclError):
            messagebox.showerror("Invalid rule",
                                 "Threshold and duration must be numbers.")
            return
        if not 0 < threshold <= 100:
            messagebox.showerror("Invalid rule",
                                 "Threshold must be a percentage between 0 and 100.")
            return
        if not 5 <= duration <= 86400:
            messagebox.showerror("Invalid rule",
                                 "Duration must be between 5 and 86400 seconds.")
            return
        reverse_metric = {label: key for key, label in METRIC_LABELS.items()}
        reverse_action = {label: key for key, label in ACTION_LABELS.items()}
        rule = ActionRule(reverse_metric[self.rule_metric_var.get()],
                          threshold, duration,
                          reverse_action[self.rule_action_var.get()])
        self.actions_engine.rules.append(rule)
        self._refresh_rules_tree()
        armed = "armed" if self.rules_enabled.get() else "NOT armed (Master Switch off)"
        self._log(f"Auto-action rule added: {rule.description()} — {armed}.", "INFO")
        self.set_status(f"Rule added ({rule.description()}).")

    def _remove_selected_rule(self) -> None:
        selection = self.rules_tree.selection()
        if not selection:
            messagebox.showinfo("Remove rule", "Select a rule row first.")
            return
        try:
            index = int(selection[0])
            rule = self.actions_engine.rules.pop(index)
        except (ValueError, IndexError, tk.TclError):
            self.set_status("Rule list changed — try again.")
            return
        self._refresh_rules_tree()
        self._log(f"Auto-action rule removed: {rule.description()}", "INFO")

    def _toggle_rules_master(self) -> None:
        try:
            enabled = bool(int(self.rules_master.get()))
        except (ValueError, tk.TclError, AttributeError):
            enabled = False
        self.actions_engine.enabled = enabled
        if enabled:
            for rule in self.actions_engine.rules:
                rule.breach_s = 0.0
            self._log("Master Switch ENABLED — auto-action rules are armed. "
                      "They run only while monitoring is active.", "WARNING")
        else:
            self._log("Master Switch disabled — auto-action rules are inert.", "INFO")

    def _automation_log(self, message: str, tag: str = "AUTO") -> None:
        """Thread-safe funnel for auto-action events (called from workers)."""
        self._bg_queue.put(("auto_log", (message, tag)))

    def _automation_clean_temp(self) -> str:
        """Safe temp cleanup for the clean_temp rule. Runs on a worker thread.

        Same policy as the Reports & Tools cleanup: only the OS temp root,
        only top-level files, only allow-listed suffixes, containment checked.
        No interactive prompts — automation implies prior consent.
        """
        files, note = preview_temp_candidates(limit=200)
        root = approved_temp_root()
        removed = skipped = 0
        freed = 0
        for p in files:
            try:
                rp = p.resolve()
                rp.relative_to(root)  # containment check
                if rp.suffix.lower() not in SAFE_TEMP_SUFFIXES or not rp.is_file():
                    skipped += 1
                    continue
                size = rp.stat().st_size
                rp.unlink()
                freed += size
                removed += 1
            except (OSError, ValueError):
                skipped += 1
        return (f"removed {removed}, skipped {skipped}, "
                f"freed {format_bytes(freed)}. {note}")

    def _build_diagnostics(self, page) -> None:
        ttk.Label(page, text="Diagnostic scan output", style="Head.TLabel").pack(anchor="w")
        row = ttk.Frame(page)
        row.pack(fill="x", pady=6)
        ctkw.make_button(row, "accent", T.BG_MAIN, text="♥  Run diagnostic scan",
                         command=self.run_scan).pack(side="left", padx=(0, 8))
        ctkw.make_button(row, "ghost", T.BG_MAIN, text="Save diff HTML",
                         command=self.export_diff_html).pack(side="left", padx=(0, 8))
        ctkw.make_button(row, "ghost", T.BG_MAIN, text="♥  Check SMART (smartctl)",
                         command=self.check_smart).pack(side="left", padx=(0, 8))
        ctkw.make_button(row, "ghost", T.BG_MAIN, text="Clear",
                         command=self._clear_diagnostics_view).pack(side="left")
        self.scan_bar = ctkw.make_progress(row, T.BG_MAIN,
                                           mode="indeterminate", width=220)
        self.scan_bar.pack(side="right")
        # "How to fix" rows for FAIL/WARNING findings; rebuilt after every scan.
        self.fix_frame = tk.Frame(page, bg=T.BG_MAIN)
        self.fix_frame.pack(fill="x", pady=(0, 6))
        tk.Label(self.fix_frame,
                 text="FAIL and WARNING findings will offer a 'How to fix' "
                      "button after a scan.",
                 bg=T.BG_MAIN, fg=T.FG_MUTED, font=T.FONT_SMALL,
                 anchor="w").pack(fill="x")
        out_box = tk.Frame(page, bg=T.BG_OUTPUT, highlightbackground=T.BORDER,
                           highlightthickness=1)
        out_box.pack(fill="both", expand=True)
        self.diag_text = tk.Text(out_box, bg=T.BG_OUTPUT, fg=T.OUTPUT_FG,
                                 insertbackground=T.NEON_CYAN, font=T.FONT_MONO,
                                 wrap="word", state="disabled")
        sb = ttk.Scrollbar(out_box, orient="vertical", command=self.diag_text.yview)
        self.diag_text.configure(yscrollcommand=sb.set)
        self.diag_text.pack(side="left", fill="both", expand=True, padx=2, pady=2)
        sb.pack(side="left", fill="y")
        for tag, colour in T.STATUS_COLOURS.items():
            self.diag_text.tag_configure(tag, foreground=colour)

    def _clear_diagnostics_view(self) -> None:
        try:
            self.diag_text.configure(state="normal")
            self.diag_text.delete("1.0", "end")
            self.diag_text.configure(state="disabled")
        except tk.TclError:
            pass
        for child in self.fix_frame.winfo_children():
            child.destroy()

    def _render_fix_buttons(self, findings) -> None:
        """Rebuild the FAIL/WARNING rows with one 'How to fix' button each."""
        frame = getattr(self, "fix_frame", None)
        if frame is None:
            return
        for child in frame.winfo_children():
            child.destroy()
        actionable = [f for f in findings if f.status in ("FAIL", "WARNING")]
        # FAIL rows first: they are the ones that need attention now.
        actionable.sort(key=lambda f: 0 if f.status == "FAIL" else 1)
        if not actionable:
            tk.Label(frame, text="No FAIL or WARNING findings — nothing to fix.",
                     bg=T.BG_MAIN, fg=T.FG_MUTED, font=T.FONT_SMALL,
                     anchor="w").pack(fill="x")
            return
        header_colour = (T.STATUS_COLOURS.get("FAIL", T.NEON_RED)
                         if any(f.status == "FAIL" for f in actionable)
                         else T.STATUS_COLOURS.get("WARNING", T.NEON_YELLOW))
        tk.Label(
            frame,
            text="FAIL and WARNING findings need attention. 'How to fix' "
                 "explains the problem and opens the right settings or copies a "
                 "safe command — nothing is changed for you.",
            bg=T.BG_MAIN, fg=header_colour, font=T.FONT_SMALL, anchor="w",
            justify="left").pack(fill="x", pady=(0, 2))
        for finding in actionable:
            row = tk.Frame(frame, bg=T.BG_MAIN)
            row.pack(fill="x", pady=1)
            tk.Label(row,
                     text=f"[{finding.status}] {finding.component}: "
                          f"{finding.message}",
                     bg=T.BG_MAIN,
                     fg=T.STATUS_COLOURS.get(finding.status, T.FG_PRIMARY),
                     font=T.FONT_SMALL, anchor="w", justify="left").pack(
                         side="left", fill="x", expand=True)
            ctkw.make_button(row, "ghost", T.BG_MAIN, text="How to fix",
                             width=96,
                             command=lambda f=finding: self._show_fix(f)).pack(
                                 side="right", padx=(8, 0))

    def _copy_to_clipboard(self, text: str) -> bool:
        try:
            self.clipboard_clear()
            self.clipboard_append(text)
            self.update_idletasks()
            return True
        except tk.TclError:
            return False

    def _open_settings_uri(self, uri: str) -> bool:
        """Open an OS settings page; never a shell, never a state change."""
        system = platform.system()
        try:
            if system == "Windows" and hasattr(os, "startfile"):
                os.startfile(uri)  # noqa: S606 - user-initiated settings page
            else:
                argv = (["open", uri] if system == "Darwin"
                        else ["xdg-open", uri])
                proc = subprocess.run(argv, shell=False, check=False,
                                      capture_output=True, timeout=8)
                if proc.returncode != 0:
                    raise OSError(
                        f"{argv[0]} exited with code {proc.returncode}")
            return True
        except (OSError, subprocess.SubprocessError) as exc:
            log.warning("could not open settings URI %s: %s", uri, exc)
            self._copy_to_clipboard(uri)
            return False

    def _show_fix(self, finding) -> None:
        """Explain one FAIL/WARNING finding and perform its safe fix."""
        explanation = explain_finding(finding)
        if explanation is None:
            self.set_status("No fix guidance needed for this finding.")
            return
        self._log(f"Fix guidance — {explanation.title}: "
                  f"{explanation.explanation}", "INFO")
        self._log(f"    {explanation.fix}", "INFO")
        if explanation.kind == "uri" and explanation.target:
            if self._open_settings_uri(explanation.target):
                self.set_status(f"Opened settings: {explanation.title}")
                messagebox.showinfo(
                    explanation.title,
                    f"{explanation.explanation}\n\n{explanation.fix}")
            else:
                self.set_status("Settings could not be opened — the address "
                                "was copied to the clipboard instead.")
            return
        if explanation.kind == "command" and explanation.target:
            copied = self._copy_to_clipboard(explanation.target)
            self.set_status(
                "Safe command copied to the clipboard — run it yourself if you "
                "choose." if copied else "Could not copy the command.")
            intro = ("Copied to your clipboard (nothing was run):"
                     if copied else "Run this yourself if you choose:")
            messagebox.showinfo(
                explanation.title,
                f"{explanation.explanation}\n\n{intro}\n\n"
                f"{explanation.target}\n\n{explanation.fix}")
            return
        messagebox.showinfo(explanation.title,
                            f"{explanation.explanation}\n\n{explanation.fix}")

    def _build_reports(self, page) -> None:
        ttk.Label(page, text="Reports, thresholds & safe tools", style="Head.TLabel").pack(anchor="w")
        row = ttk.Frame(page)
        row.pack(fill="x", pady=8)
        ctkw.make_button(row, "ghost", T.BG_MAIN, text="Save JSON",
                         command=lambda: self.export_report("json")).pack(side="left", padx=(0, 8))
        ctkw.make_button(row, "ghost", T.BG_MAIN, text="Save TXT",
                         command=lambda: self.export_report("txt")).pack(side="left", padx=(0, 8))
        ctkw.make_button(row, "ghost", T.BG_MAIN, text="Save CSV",
                         command=lambda: self.export_report("csv")).pack(side="left")
        th_box = tk.LabelFrame(page, text="Thresholds (% / °C) — documented, configurable",
                               bg=T.BG_PANEL, fg=T.NEON_CYAN, font=T.FONT_SMALL,
                               padx=10, pady=8)
        th_box.pack(fill="x", pady=8)
        self.th_vars: dict[str, tk.StringVar] = {}
        for i, (key, val) in enumerate(self.thresholds.items()):
            tk.Label(th_box, text=key, bg=T.BG_PANEL, fg=T.FG_MUTED,
                     font=T.FONT_SMALL).grid(row=i // 4, column=(i % 4) * 2,
                                             sticky="w", padx=4, pady=2)
            var = tk.StringVar(value=str(val))
            ent = ctkw.make_entry(th_box, T.BG_PANEL, textvariable=var, width=8)
            ent.grid(row=i // 4, column=(i % 4) * 2 + 1, padx=4, pady=2, sticky="w")
            self.th_vars[key] = var
        ctkw.make_button(th_box, "ghost", T.BG_PANEL, text="Apply thresholds",
                         command=self.apply_thresholds).grid(row=3, column=6, padx=8)
        clean_box = tk.LabelFrame(page, text="Safe temp-file cleanup (optional, preview first)",
                                  bg=T.BG_PANEL, fg=T.NEON_CYAN, font=T.FONT_SMALL,
                                  padx=10, pady=8)
        clean_box.pack(fill="both", expand=True)
        ttk.Label(clean_box, text=f"Approved location only: {approved_temp_root()} "
                  "(top-level files, safe suffixes, never recursive).",
                  font=T.FONT_SMALL, foreground=T.FG_MUTED,
                  background=T.BG_PANEL).pack(anchor="w")
        brow = ttk.Frame(clean_box)
        brow.pack(fill="x", pady=6)
        ctkw.make_button(brow, "ghost", T.BG_PANEL, text="Preview files",
                         command=self.preview_cleanup).pack(side="left", padx=(0, 8))
        ctkw.make_button(brow, "ghost", T.BG_PANEL, text="Delete previewed files",
                         command=self.clean_previewed).pack(side="left")
        self.clean_list = tk.Text(clean_box, height=8, bg=T.BG_OUTPUT,
                                  fg=T.FG_PRIMARY, font=T.FONT_MONO, wrap="word",
                                  state="disabled")
        self.clean_list.pack(fill="both", expand=True)
        self._previewed: list[Path] = []

        tools_row = ttk.Frame(page)
        tools_row.pack(fill="both", expand=True, pady=(8, 0))
        big_box = tk.LabelFrame(
            tools_row, text="Largest files (read-only — nothing is deleted)",
            bg=T.BG_PANEL, fg=T.NEON_CYAN, font=T.FONT_SMALL, padx=10, pady=8)
        big_box.pack(side="left", fill="both", expand=True, padx=(0, 8))
        self.bigfiles_var = tk.StringVar(value="")
        brow = ttk.Frame(big_box)
        brow.pack(fill="x", pady=(0, 6))
        tk.Label(brow, textvariable=self.bigfiles_var, bg=T.BG_PANEL,
                 fg=T.FG_MUTED, font=T.FONT_SMALL).pack(side="left", padx=(0, 8))
        ctkw.make_button(brow, "ghost", T.BG_PANEL, text="Choose folder…",
                         command=self.choose_big_files_folder).pack(side="right",
                                                                    padx=(8, 0))
        self.bigfiles_btn = ctkw.make_button(brow, "ghost", T.BG_PANEL,
                                             text="Find largest files",
                                             command=self.find_largest_files)
        self.bigfiles_btn.pack(side="right")
        self.bigfiles_list = tk.Text(big_box, height=10, bg=T.BG_OUTPUT,
                                     fg=T.FG_PRIMARY, font=T.FONT_MONO,
                                     wrap="word", state="disabled")
        self.bigfiles_list.pack(fill="both", expand=True)

        sched_box = tk.LabelFrame(
            tools_row, text="Scheduled weekly scan (runs headless, your machine)",
            bg=T.BG_PANEL, fg=T.NEON_CYAN, font=T.FONT_SMALL, padx=10, pady=8)
        sched_box.pack(side="left", fill="both", expand=True)
        ttk.Label(sched_box, text="Registers this scanner in Windows Task "
                  "Scheduler (schtasks) or on a cron line (Linux/macOS).\nThe "
                  "scan runs with the documented flags: --scan --quiet "
                  "--no-history (writes nothing).",
                  font=T.FONT_SMALL, foreground=T.FG_MUTED,
                  background=T.BG_PANEL).pack(anchor="w", pady=(0, 6))
        srow = ttk.Frame(sched_box)
        srow.pack(fill="x", pady=(0, 6))
        tk.Label(srow, text="Day:", bg=T.BG_PANEL, fg=T.FG_MUTED,
                 font=T.FONT_SMALL).pack(side="left")
        self.schedule_day_var = tk.StringVar(value="MON")
        ttk.Combobox(srow, textvariable=self.schedule_day_var,
                     values=list(VALID_DAYS), width=6,
                     state="readonly").pack(side="left", padx=(4, 12))
        tk.Label(srow, text="Time (HH:MM):", bg=T.BG_PANEL, fg=T.FG_MUTED,
                 font=T.FONT_SMALL).pack(side="left")
        self.schedule_time_var = tk.StringVar(value="09:00")
        ctkw.make_entry(srow, T.BG_PANEL, textvariable=self.schedule_time_var,
                        width=7).pack(side="left", padx=(4, 12))
        ctkw.make_button(srow, "ghost", T.BG_PANEL, text="Register weekly scan",
                         command=self.register_schedule).pack(side="left",
                                                              padx=(0, 8))
        ctkw.make_button(srow, "ghost", T.BG_PANEL, text="Remove schedule",
                         command=self.remove_schedule).pack(side="left")

    # ---------- navigation / status ----------
    def show_page(self, name: str) -> None:
        self.pages[name].lift()
        self.current_page = name
        self.page_title.configure(text=name)
        # Crash Log populates once when it first opens; afterwards only its
        # Refresh button re-collects (the other tabs behave the same — no
        # event-log work just for switching pages).
        if name == "Crash Log" and not self._crashlog_seen:
            self._crashlog_seen = True
            self.refresh_crashlog()
        # CustomTkinter buttons have no ttk "pressed" state: the active nav
        # item is highlighted by its colours and icon instead (solid
        # background + neon-cyan icon and label).
        for key, btn in self.nav_buttons.items():
            active = key == name
            btn.configure(
                fg_color=T.BTN_SIDE_ACTIVE_BG if active else T.BTN_SIDE_BG,
                text_color=T.BTN_SIDE_ACTIVE_FG if active else T.BTN_SIDE_FG,
                hover_color=T.BTN_SIDE_ACTIVE_BG if active else T.BTN_SIDE_HOVER,
                bg_color=T.BG_SIDEBAR,
                image=self._nav_icon(key, active))
        self.set_status(f"Viewing {name}.")

    def set_status(self, msg: str) -> None:
        self.status_var.set(msg)

    # ---------- theme (dark default, live light toggle) ----------
    # Widget roles whose colours both palettes define; the swap map pairs each
    # value with its counterpart via the shared palette key.
    _THEME_SWAP_CLASSES = ("Frame", "Label", "Text", "Canvas",
                           "LabelFrame", "Toplevel")

    def _toggle_theme(self) -> None:
        self.apply_theme("dark" if T.get_mode() == "light" else "light")

    def apply_theme(self, mode: str) -> None:
        """Switch palette live: ttk styles rebuild, plain widgets walk.

        ttk widgets pick the new colours up from the rebuilt styles; plain
        tk widgets keep construction-time colours, so every descendant is
        re-configured through the palette swap map. Dynamic colours (status
        dots, output tags, chart) are re-rendered from the new palette.
        """
        mode = "light" if str(mode).lower() == "light" else "dark"
        T.set_mode(mode)
        self._build_style()
        self._retheme_plain_widgets()
        self._retheme_text_tags()
        # Navigation highlight reads the freshly swapped palette.
        try:
            self.show_page(getattr(self, "current_page", "Dashboard"))
        except (KeyError, tk.TclError):
            pass
        if self.chart is not None:
            self.chart.set_palette(T.BG_CARD, T.FG_PRIMARY, T.BORDER,
                                   T.NEON_CYAN, T.NEON_PURPLE)
        # Vector icons carry colours baked into their bitmaps: redraw them
        # from the freshly swapped palette.
        self._refresh_card_icons()
        self.theme_btn.configure(
            text="◐  Switch to dark" if mode == "light" else "◐  Switch to light")
        self.set_status(f"Theme: {mode}.")
        if self.latest_snap is not None:
            try:
                self._apply_snapshot(self.latest_snap)  # re-render dot colours
            except Exception:  # noqa: BLE001 - re-render must never break toggling
                log.debug("snapshot re-render after theme change failed",
                          exc_info=True)

    def _refresh_card_icons(self) -> None:
        """Re-render the dashboard's vector icons after a palette swap.

        Icons are colour-bearing bitmaps, so a live theme toggle must draw
        them again from the newly active palette (the nav buttons get the
        same treatment inside `show_page`).
        """
        try:
            for icon_label, icon_name, accent_key in self._card_icons.values():
                icon_label.configure(
                    image=icons.get_icon(icon_name, 22, getattr(T, accent_key)),
                    bg_color=T.BG_CARD)
            health_icon = getattr(self, "health_icon", None)
            if health_icon is not None:
                health_icon.configure(
                    image=icons.get_icon("security", 28, T.NEON_CYAN),
                    bg_color=T.BG_CARD)
        except (tk.TclError, KeyError, AttributeError) as exc:
            log.debug("card icon re-render skipped: %s", exc)

    def _retheme_plain_widgets(self) -> None:
        """Re-colour every plain (non-ttk) widget via the palette swap map."""
        dark, light = T.palettes()
        swap: dict[str, str] = {}
        for key in T.PALETTE_KEYS:
            # First-wins: two palette keys may share a colour, but must agree
            # on their counterpart (see the note in theme.retheme_widget).
            swap.setdefault(dark[key], light[key])
            swap.setdefault(light[key], dark[key])

        def _descendants(widget) -> "tuple[tk.Misc, ...]":
            for child in widget.winfo_children():
                yield child
                yield from _descendants(child)

        for widget in _descendants(self):
            # CustomTkinter widgets know their role from construction
            # (theme.make_* factories) and re-colour straight from the palette.
            # Un-tagged CTk children (internal widgets, plain CTkLabel images)
            # are skipped as well: their colours are owned by the parent CTk
            # widget's redraw, and re-colouring them here would corrupt it.
            if T.retheme_widget(widget) or isinstance(widget, ctk.CTkBaseClass):
                continue
            try:
                cls = widget.winfo_class()
            except tk.TclError:
                continue
            if cls not in self._THEME_SWAP_CLASSES:
                continue  # ttk widgets follow the rebuilt styles
            for option in ("bg", "foreground", "highlightbackground"):
                try:
                    current = str(widget.cget(option))
                    new_value = swap.get(current)
                    if new_value:
                        widget.configure({option: new_value})
                except Exception:  # noqa: BLE001 - option not supported here, skip
                    pass

    def _retheme_text_tags(self) -> None:
        try:
            for tag, colour in T.STATUS_COLOURS.items():
                self.diag_text.tag_configure(tag, foreground=colour)
        except (tk.TclError, AttributeError):
            pass
        try:
            # The Crash Log's UNAVAILABLE status row also follows the palette.
            self.crash_tree.tag_configure("unavailable",
                                          foreground=T.FG_MUTED)
        except (tk.TclError, AttributeError):
            pass

    def _log(self, msg: str, tag: str = "INFO") -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        line = f"[{stamp}] {msg}\n"
        try:
            self.diag_text.configure(state="normal")
            self.diag_text.insert("end", line, tag if tag in T.STATUS_COLOURS else None)
            self.diag_text.see("end")
            self.diag_text.configure(state="disabled")
        except (tk.TclError, AttributeError):
            pass
        if tag == "FAIL":
            log.error(msg)
        elif tag == "WARNING":
            log.warning(msg)
        else:
            log.info(msg)

    # ---------- data flows (thread-safe) ----------
    def refresh_once(self) -> None:
        if self._scan_active or self._refresh_active:
            return
        self._refresh_active = True
        self.set_status("Refreshing metrics…")
        try:
            threading.Thread(target=self._collect_job, name="shds-refresh",
                             daemon=True).start()
        except (RuntimeError, OSError) as exc:  # can't start new thread / shutdown
            log.warning("refresh thread failed to start: %s", exc)
            self._refresh_active = False
            self.set_status("Refresh unavailable: background thread could not start.")
            return

    def _collect_job(self) -> None:
        try:
            snap = collect_snapshot(include_gpu=True)
            item = ("snapshot", snap)
        except Exception as exc:  # never crash GUI
            log.exception("collect failed")
            item = ("refresh_error", f"Refresh failed: {exc}")
        finally:
            self._refresh_active = False
        self._bg_queue.put(item)

    def run_scan(self) -> None:
        if self._scan_active:
            messagebox.showinfo("Scan running", "A diagnostic scan is already running.")
            return
        self._scan_active = True
        try:
            self.scan_bar.start(12)
        except tk.TclError:
            pass
        self.head_status.configure(text="● scanning…", fg=T.NEON_YELLOW)
        self.set_status("Running diagnostic scan…")
        self._log("Starting diagnostic scan…", "INFO")
        try:
            threading.Thread(target=self._scan_job, name="shds-scan", daemon=True).start()
        except (RuntimeError, OSError) as exc:
            log.warning("scan thread failed to start: %s", exc)
            self._scan_active = False
            try:
                self.scan_bar.stop()
            except tk.TclError:
                pass
            self.head_status.configure(text="● idle", fg=T.FG_MUTED)
            self.set_status("Scan unavailable: background thread could not start.")

    def _scan_job(self) -> None:
        try:
            snap = collect_snapshot(include_gpu=True)
            diag = run_diagnostics(snap, self.thresholds)
            payload = build_report_dict(snap, diag, self.thresholds)
            self._bg_queue.put(("scan", (snap, diag, payload)))
        except Exception as exc:
            log.exception("scan failed")
            self._bg_queue.put(("error", f"Scan failed: {exc}"))
        finally:
            self._bg_queue.put(("scan_done", None))

    def _reschedule(self, ms: int, fn) -> None:
        """Schedule `fn`, remembering the id so shutdown can cancel it."""
        try:
            if not self.winfo_exists():
                return
        except tk.TclError:
            return
        holder: dict[str, str] = {}

        def _run() -> None:
            self._after_ids.discard(holder.get("id", ""))
            fn()

        try:
            holder["id"] = self.after(ms, _run)
            self._after_ids.add(holder["id"])
        except tk.TclError:
            pass

    def _poll_bg_queue(self) -> None:
        try:
            while True:
                try:
                    kind, data = self._bg_queue.get_nowait()
                except queue.Empty:
                    break
                # Each item is handled in isolation: a handler that raises must
                # not abort the rest of the drain, otherwise the items queued
                # behind it (e.g. the "scan_done" that clears the scan state)
                # are delayed by a full poll interval.
                try:
                    if kind == "snapshot":
                        self._apply_snapshot(data)
                    elif kind == "scan":
                        snap, diag, payload = data
                        self._apply_snapshot(snap)
                        self._apply_diag(diag, payload)
                    elif kind == "scan_done":
                        self._scan_active = False
                        try:
                            self.scan_bar.stop()
                        except tk.TclError:
                            pass
                        self.head_status.configure(
                            text="● live" if self.monitor_running else "● idle",
                            fg=T.NEON_GREEN if self.monitor_running else T.FG_MUTED)
                    elif kind == "error":
                        self._scan_active = False
                        try:
                            self.scan_bar.stop()
                        except tk.TclError:
                            pass
                        self._log(str(data), "FAIL")
                        self.set_status(str(data))
                    elif kind == "refresh_error":
                        # A failed manual refresh must not disturb an in-flight scan.
                        self._log(str(data), "FAIL")
                        self.set_status(str(data))
                    elif kind == "procs":
                        self._render_procs(data)
                    elif kind == "connections":
                        self._render_connections(data)
                    elif kind == "startup":
                        self._render_startup(data)
                    elif kind == "crashes":
                        self._render_crashlog(data)
                    elif kind == "smart":
                        self._render_smart(data)
                    elif kind == "bigfiles":
                        self._render_bigfiles(data)
                    elif kind == "update":
                        self._render_update_check(data)
                    elif kind == "schedule":
                        success, message = data
                        self._log(("Registered: " if success else "Failed: ")
                                  + message, "PASS" if success else "FAIL")
                        self.set_status(message)
                    elif kind == "auto_log":
                        # Auto-action events arrive from worker threads with
                        # no tkinter access; log them here on the main thread.
                        message, tag = data
                        self._log(message, tag)
                        if tag in ("FAIL", "WARNING"):
                            self.set_status(message)
                except Exception:  # noqa: BLE001
                    # One poisoned item must not kill the loop.
                    log.exception("background queue handler failed; polling continues")
        finally:
            # Runs even when a handler failed, so the loop always reschedules.
            self._reschedule(250, self._poll_bg_queue)

    def _apply_snapshot(self, snap: HardwareSnapshot) -> None:
        self.latest_snap = snap
        val_cpu, bar_cpu, det_cpu, dot_cpu = self.cards["CPU"]
        if snap.cpu.available and snap.cpu.percent is not None:
            val_cpu.configure(text=f"{snap.cpu.percent:.1f} %")
            bar_cpu.configure(value=snap.cpu.percent)
            st = _status_of_pct(snap.cpu.percent, self.thresholds["cpu_warn"],
                                self.thresholds["cpu_fail"])
            dot_cpu.configure(fg=T.STATUS_COLOURS.get(st, T.FG_DIM))
            det_cpu.configure(text=f"{snap.cpu.logical_cores or '?'} logical cores · "
                              f"{snap.cpu.freq_current_mhz or '—'} MHz")
        else:
            val_cpu.configure(text="Unavailable")
            bar_cpu.configure(value=0)
            dot_cpu.configure(fg=T.FG_DIM)
            det_cpu.configure(text=snap.cpu.error or "psutil unavailable")
        val_m, bar_m, det_m, dot_m = self.cards["RAM"]
        if snap.mem.available and snap.mem.percent is not None:
            val_m.configure(text=f"{snap.mem.percent:.1f} %")
            bar_m.configure(value=snap.mem.percent)
            st = _status_of_pct(snap.mem.percent, self.thresholds["mem_warn"],
                                self.thresholds["mem_fail"])
            dot_m.configure(fg=T.STATUS_COLOURS.get(st, T.FG_DIM))
            det_m.configure(text=f"{format_bytes(snap.mem.used)} / {format_bytes(snap.mem.total)}")
        else:
            val_m.configure(text="Unavailable")
            bar_m.configure(value=0)
            dot_m.configure(fg=T.FG_DIM)
            det_m.configure(text="psutil unavailable")
        val_g, bar_g, det_g, dot_g = self.cards["GPU"]
        g: GpuStatus = snap.gpu
        if g.supported and g.gpus:
            first = g.gpus[0]
            if first.utilization_pct is not None:
                val_g.configure(text=f"{first.utilization_pct:.0f} %")
                bar_g.configure(value=first.utilization_pct)
            else:
                val_g.configure(text="No util")
                bar_g.configure(value=0)
            # Colour the dot by the same thresholds the diagnostics use, so the
            # dashboard cannot show a green "healthy" dot for a GPU that the
            # scan rates as FAIL (e.g. overheating).
            if first.temperature_c is not None:
                gpu_st = _status_of_pct(first.temperature_c,
                                        self.thresholds.get("gpu_temp_warn", 83.0),
                                        self.thresholds.get("gpu_temp_fail", 95.0))
            elif first.utilization_pct is not None:
                # No temperature reported: utilisation alone is not a failure
                # condition, so the dot stays neutral-green (PASS).
                gpu_st = "PASS"
            else:
                gpu_st = "UNAVAILABLE"
            dot_g.configure(fg=T.STATUS_COLOURS.get(gpu_st, T.FG_DIM))
            temp = f"{first.temperature_c:.0f}C" if first.temperature_c is not None else "temp n/a"
            det_g.configure(text=f"{first.name[:22]} · {temp}")
        else:
            val_g.configure(text="Unavailable")
            bar_g.configure(value=0)
            dot_g.configure(fg=T.FG_DIM)
            det_g.configure(text=(g.message[:42] if g.message else "unsupported"))
        val_d, bar_d, det_d, dot_d = self.cards["Disk"]
        acc = [d for d in snap.disks if d.accessible and d.percent is not None]
        if acc:
            worst = max(acc, key=lambda d: d.percent or 0)
            assert worst.percent is not None
            val_d.configure(text=f"{worst.percent:.0f} %")
            bar_d.configure(value=worst.percent)
            st = _status_of_pct(worst.percent, self.thresholds["disk_warn"],
                                self.thresholds["disk_fail"])
            dot_d.configure(fg=T.STATUS_COLOURS.get(st, T.FG_DIM))
            det_d.configure(text=f"{worst.mountpoint} · {len(acc)} volume(s)")
        else:
            val_d.configure(text="Unavailable")
            bar_d.configure(value=0)
            dot_d.configure(fg=T.FG_DIM)
            det_d.configure(text="no accessible volumes")
        val_n, bar_n, det_n, dot_n = self.cards["Network"]
        if snap.net.available:
            val_n.configure(text=f"{len(snap.net.interfaces)} ifaces")
            bar_n.configure(value=0)
            dot_n.configure(fg=T.NEON_GREEN)
            if snap.net.down_bps is not None and snap.net.up_bps is not None:
                det_n.configure(text=f"↓ {snap.net.down_bps/1e6:.2f} Mbps · "
                                f"↑ {snap.net.up_bps/1e6:.2f} Mbps")
            else:
                det_n.configure(text=", ".join(snap.net.interfaces[:3]) or "no traffic yet")
        else:
            val_n.configure(text="Unavailable")
            bar_n.configure(value=0)
            dot_n.configure(fg=T.FG_DIM)
            det_n.configure(text="counters unavailable")
        self.set_status(f"Updated {datetime.now().strftime('%H:%M:%S')} — host {snap.hostname}")
        self._update_system_health()
        self.refresh_processes()

    def _update_system_health(self) -> None:
        """Recompute the dashboard System Health score (never raises)."""
        score_var = getattr(self, "health_var", None)
        if score_var is None:
            return
        findings = (self.latest_diag.findings
                    if self.latest_diag is not None else None)
        try:
            health = system_health_score(self.latest_snap, findings)
        except Exception:  # noqa: BLE001 - cosmetic; never break a refresh
            log.debug("system health score failed", exc_info=True)
            return
        total = health.get("total")
        if total is None:
            score_var.set("—")
            self.health_label.configure(fg=T.FG_DIM)
            self.health_bar.set(0)
            self.health_detail.configure(
                text="Waiting for the first metrics sample…")
            return
        score_var.set(str(total))
        if total >= 80:
            colour = T.STATUS_COLOURS.get("PASS", T.NEON_GREEN)
        elif total >= 60:
            colour = T.STATUS_COLOURS.get("WARNING", T.NEON_YELLOW)
        else:
            colour = T.STATUS_COLOURS.get("FAIL", T.NEON_RED)
        self.health_label.configure(fg=colour)
        self.health_bar.configure(progress_color=colour)
        self.health_bar.set(total)
        parts = health.get("components", {})
        bits = [f"{label} {parts[key]:.0f}"
                for key, label in (("cpu", "CPU"), ("mem", "RAM"),
                                   ("disk", "Disk")) if key in parts]
        if "os" in parts:
            bits.append(f"OS {health.get('os_fail', 0)} fail / "
                        f"{health.get('os_warning', 0)} warn")
            note = ""
        else:
            note = " · run a scan for OS checks"
        self.health_detail.configure(text=" · ".join(bits) + note)

    def _apply_diag(self, diag: DiagnosticReport, payload: dict) -> None:
        # Compare against the previous scan *before* overwriting it.
        had_previous = self.previous_diag is not None
        self.last_comparison = compare_reports(self.previous_diag, diag)
        self.previous_diag = diag
        self.previous_payload = self.latest_payload
        self.latest_diag = diag
        self.latest_payload = payload
        self.scan_var.set(f"Last scan: {payload.get('timestamp', '')} "
                          f"({diag.duration_s:.1f}s)")
        for f in diag.findings:
            self._log(f"[{f.status}] {f.component}: {f.message}", f.status)
            if f.detail:
                self._log(f"    detail: {f.detail}", "INFO")
        if had_previous:
            self._log_comparison(self.last_comparison)
        else:
            # The very first scan has nothing to compare against: saying
            # "no changes since last scan" would be a false claim.
            self._log("— First scan: no previous results to compare against —", "INFO")
        self._log("— Recommendations —", "INFO")
        for r in diag.recommendations:
            self._log(f" • {r}", "INFO")
        s = diag.summary
        self._log(f"Summary: PASS={s.get('PASS',0)} WARNING={s.get('WARNING',0)} "
                  f"FAIL={s.get('FAIL',0)} UNAVAILABLE={s.get('UNAVAILABLE',0)}", "INFO")
        self._render_fix_buttons(diag.findings)
        self._update_system_health()
        self.set_status("Scan complete.")
        self.show_page("Diagnostics")

    def _log_comparison(self, comparison: dict | None) -> None:
        """Print the regression/improvement diff from the previous scan."""
        if not comparison:
            return
        regressions = comparison.get("regressions", [])
        improvements = comparison.get("improvements", [])
        added = comparison.get("added", [])
        removed = comparison.get("removed", [])
        if not (regressions or improvements or added or removed):
            self._log("— Changes since last scan: none (same statuses) —", "INFO")
            return
        self._log("— Changes since last scan —", "INFO")
        for key, before, after, _msg in regressions:
            self._log(f" • WORSE   {key}: {before} → {after}", "WARNING")
        for key, before, after, _msg in improvements:
            self._log(f" • BETTER  {key}: {before} → {after}", "PASS")
        for key, _before, after, _msg in added:
            self._log(f" • NEW     {key}: {after}", "INFO")
        for key, before, _after, _msg in removed:
            self._log(f" • GONE    {key}: was {before}", "INFO")

    # ---------- diff HTML export ----------
    def export_diff_html(self) -> None:
        """Write a self-contained HTML diff of the last two scans."""
        if self.latest_payload is None:
            messagebox.showinfo("Nothing to diff", "Run a diagnostic scan first.")
            return
        if self.last_comparison is None or self.previous_payload is None:
            # `previous_payload` is the honest "was there really a scan before
            # this one?" marker: `previous_diag` is already set after the
            # first scan, so checking it would let a one-scan diff through.
            messagebox.showinfo(
                "Nothing to diff",
                "A diff needs two scans — run another scan, then try again.")
            return
        dest = filedialog.asksaveasfilename(
            defaultextension=".html", filetypes=[("HTML", "*.html")],
            initialfile=f"shds-diff-{datetime.now().strftime('%Y%m%d-%H%M%S')}.html")
        if not dest:
            return
        try:
            path = safe_path_for_report(dest)
            path.write_text(render_comparison_html(
                self.last_comparison, self.previous_payload,
                self.latest_payload), encoding="utf-8")
        except (ValueError, PermissionError, OSError) as exc:
            messagebox.showerror("Export failed", str(exc))
            return
        self.set_status(f"Diff report saved: {path}")
        self._log(f"Diff report saved: {path}", "PASS")
        messagebox.showinfo("Diff saved", f"Saved diff report to:\n{path}")

    # ---------- SMART disk health ----------
    def check_smart(self) -> None:
        """Query smartctl (optional tool) off-thread; results are logged."""
        self.set_status("Querying SMART health (smartctl)…")
        self._log("Checking SMART disk health (requires the optional "
                  "smartmontools tool; admin rights may be needed)…", "INFO")
        threading.Thread(target=self._smart_job, name="shds-smart",
                         daemon=True).start()

    def _smart_job(self) -> None:
        try:
            status = query_smart_devices()
        except Exception as exc:  # noqa: BLE001 - never crash the GUI
            log.warning("smart query failed: %s", exc)
            status = SmartStatus(message=f"SMART query failed: {exc}")
        self._bg_queue.put(("smart", status))

    def _render_smart(self, status: SmartStatus) -> None:
        self._log(f"[SMART] {status.message}", "INFO")
        for device in status.devices:
            tag = {"PASS": "PASS", "FAIL": "FAIL"}.get(device.status, "INFO")
            line = f"[SMART] {device.device}: {device.status}"
            if device.model:
                line += f" ({device.model})"
            self._log(line, tag)
            if device.detail:
                self._log(f"    detail: {device.detail}", "INFO")
        if not status.tool_found:
            self._log(status.message, "WARNING")
        self.set_status(f"SMART: {status.message}")

    # ---------- largest files (read-only) ----------
    def choose_big_files_folder(self) -> None:
        chosen = filedialog.askdirectory(title="Choose a folder to analyse")
        if chosen:
            self.bigfiles_var.set(chosen)

    def find_largest_files(self) -> None:
        folder = str(self.bigfiles_var.get()).strip()
        if not folder:
            messagebox.showinfo("No folder", "Choose a folder first.")
            return
        ctkw.apply_button_state(self.bigfiles_btn, "disabled")
        threading.Thread(target=self._bigfiles_job, args=(folder,),
                         name="shds-bigfiles", daemon=True).start()
        self.set_status("Scanning for largest files…")

    def _bigfiles_job(self, folder: str) -> None:
        try:
            files, note = largest_files(folder, limit=25, time_budget_s=20.0)
        except Exception as exc:  # noqa: BLE001 - degrade, never crash GUI
            log.warning("largest-files scan failed: %s", exc)
            self._bg_queue.put(("bigfiles", ([], f"Scan failed: {exc}", folder)))
            return
        self._bg_queue.put(("bigfiles", (files, note, folder)))

    def _render_bigfiles(self, data) -> None:
        files, note, folder = data
        ctkw.apply_button_state(self.bigfiles_btn, "normal")
        try:
            self.bigfiles_list.configure(state="normal")
            self.bigfiles_list.delete("1.0", "end")
            self.bigfiles_list.insert("end", f"Folder: {folder}\n{note}\n\n"
                                      if note else "")
            if not files:
                self.bigfiles_list.insert("end", "No files found.\n")
            for item in files:
                self.bigfiles_list.insert(
                    "end", f"{format_bytes(item.size_bytes):>12}  {item.path}\n")
            self.bigfiles_list.configure(state="disabled")
        except tk.TclError:
            pass
        self.set_status(f"Largest files: {note}")

    # ---------- scheduled scans ----------
    def register_schedule(self) -> None:
        """Confirm, then register a weekly headless scan (schtasks/cron)."""
        day = str(self.schedule_day_var.get()).strip().upper()[:3]
        when = str(self.schedule_time_var.get()).strip()
        python_exe = sys.executable or "python"
        script = self._package_entry_script()
        if day not in VALID_DAYS:
            messagebox.showerror("Invalid day",
                                 f"Choose one of: {', '.join(VALID_DAYS)}.")
            return
        if not validate_time(when):
            messagebox.showerror("Invalid time",
                                 "Time must be HH:MM (24-hour), e.g. 09:30.")
            return
        if platform.system().lower() == "windows":
            try:
                argv = build_schtasks_argv(python_exe, script, day, when)
            except ValueError as exc:
                messagebox.showerror("Invalid schedule", str(exc))
                return
            shown = subprocess.list2cmdline(argv)
            summary = (f"Create Windows scheduled task \"{SCHTASKS_TASK_NAME}\" "
                       f"({day} {when}):\n{shown}")
        else:
            try:
                line = build_cron_line(python_exe, script, day, when)
            except ValueError as exc:
                messagebox.showerror("Invalid schedule", str(exc))
                return
            summary = (f"Add this line to your crontab (runs on {day}, {when}):\n"
                       f"\n    {line}\n\nOther cron jobs are preserved; "
                       "re-registering replaces the previous SHDS entry.")
        if not messagebox.askyesno("Confirm scheduled scan", summary
                                   + "\n\nRegister it now?"):
            return
        threading.Thread(target=self._schedule_job, args=(day, when),
                         name="shds-schedule", daemon=True).start()

    def _package_entry_script(self) -> str:
        """Runnable entry point for the scheduled scan: <project>/main.py."""
        package_dir = Path(__file__).resolve().parent.parent
        return str(package_dir / "main.py")

    def _schedule_job(self, day: str, when: str) -> None:
        """Runs off-thread; communicates the outcome via the background queue."""
        python_exe = sys.executable
        script = self._package_entry_script()
        is_windows = platform.system().lower() == "windows"
        try:
            if is_windows:
                argv = build_schtasks_argv(python_exe, script, day, when)
                proc = subprocess.run(argv, shell=False, capture_output=True,
                                      text=True, timeout=8)
                if proc.returncode == 0:
                    self._bg_queue.put(("schedule", (True,
                        f"Windows task registered ({day} {when}).")))
                else:
                    err = (proc.stderr or proc.stdout or "").strip()[:200]
                    self._bg_queue.put(("schedule", (False,
                        f"schtasks failed (exit {proc.returncode}): {err}")))
            else:
                try:
                    proc = subprocess.run(["crontab", "-l"], shell=False,
                                          capture_output=True, text=True,
                                          timeout=8)
                except FileNotFoundError:
                    self._bg_queue.put(("schedule", (False,
                        "crontab command not found on this system.")))
                    return
                if proc.returncode == 0:
                    current = proc.stdout
                elif "no crontab" in (proc.stdout + proc.stderr).lower():
                    current = None  # normal for first-time users
                else:
                    err = (proc.stderr or "").strip()[:200]
                    self._bg_queue.put(("schedule", (False,
                        f"Cannot read crontab: {err or 'access denied'}")))
                    return
                crontab_text = merge_crontab(current)
                crontab_text = (crontab_text.rstrip("\n") + "\n"
                                if crontab_text else "")
                crontab_text += (build_cron_line(python_exe, script, day, when)
                                 + "\n")
                proc = subprocess.run(["crontab", "-"], shell=False,
                                      input=crontab_text, capture_output=True,
                                      text=True, timeout=8)
                if proc.returncode == 0:
                    self._bg_queue.put(("schedule", (True,
                        f"Cron entry registered ({day} {when}).")))
                else:
                    err = (proc.stderr or "").strip()[:200]
                    self._bg_queue.put(("schedule", (False,
                        f"crontab update failed (exit {proc.returncode}): {err}")))
        except subprocess.TimeoutExpired:
            self._bg_queue.put(("schedule", (False,
                "Scheduler command timed out after 8s.")))
        except (OSError, ValueError) as exc:
            self._bg_queue.put(("schedule", (False, f"Scheduler error: {exc}")))

    def remove_schedule(self) -> None:
        """Confirm, then remove the SHDS scheduled scan (never other jobs)."""
        is_windows = platform.system().lower() == "windows"
        if is_windows:
            summary = (f"Delete the Windows scheduled task "
                       f"\"{SCHTASKS_TASK_NAME}\"?\n\n"
                       "No other scheduled tasks are touched.")
        else:
            summary = ("Remove the marked SHDS line from your crontab?\n\n"
                       "Only lines tagged with the SHDS marker are removed;\n"
                       "every other cron job is preserved.")
        if not messagebox.askyesno("Remove scheduled scan", summary):
            return
        threading.Thread(target=self._remove_schedule_job,
                         name="shds-unschedule", daemon=True).start()

    def _remove_schedule_job(self) -> None:
        try:
            if platform.system().lower() == "windows":
                argv = ["schtasks", "/Delete", "/TN", SCHTASKS_TASK_NAME, "/F"]
                proc = subprocess.run(argv, shell=False, capture_output=True,
                                      text=True, timeout=8)
                if proc.returncode == 0:
                    self._bg_queue.put(("schedule", (True,
                        "Windows scheduled task removed.")))
                else:
                    err = (proc.stderr or proc.stdout or "").strip()[:200]
                    self._bg_queue.put(("schedule", (False,
                        f"schtasks delete failed (exit {proc.returncode}): {err}"
                        " (task may not exist)")))
            else:
                proc = subprocess.run(["crontab", "-l"], shell=False,
                                      capture_output=True, text=True, timeout=8)
                if proc.returncode != 0:
                    self._bg_queue.put(("schedule", (False,
                        "No crontab exists — nothing to remove.")))
                    return
                new_text, removed = strip_scheduled_entries(proc.stdout)
                if removed == 0:
                    self._bg_queue.put(("schedule", (True,
                        "No SHDS cron line found — nothing removed.")))
                    return
                proc = subprocess.run(["crontab", "-"], shell=False,
                                      input=new_text, capture_output=True,
                                      text=True, timeout=8)
                if proc.returncode == 0:
                    self._bg_queue.put(("schedule", (True,
                        f"Removed {removed} SHDS cron line(s).")))
                else:
                    err = (proc.stderr or "").strip()[:200]
                    self._bg_queue.put(("schedule", (False,
                        f"crontab update failed (exit {proc.returncode}): {err}")))
        except subprocess.TimeoutExpired:
            self._bg_queue.put(("schedule", (False,
                "Scheduler command timed out after 8s.")))
        except (OSError, ValueError) as exc:
            self._bg_queue.put(("schedule", (False, f"Scheduler error: {exc}")))

    # ---------- version check ----------
    def check_for_updates(self) -> None:
        """Manual, metadata-only version check (never downloads anything)."""
        ctkw.apply_button_state(self._update_btn, "disabled")
        self.set_status("Checking for updates…")
        threading.Thread(target=self._update_job, name="shds-update",
                         daemon=True).start()

    def _update_job(self) -> None:
        try:
            result = check_for_update()
        except Exception as exc:  # noqa: BLE001 - degrade, never crash GUI
            log.warning("update check failed: %s", exc)
            result = UpdateCheck(detail=f"Update check failed: {exc}")
        self._bg_queue.put(("update", result))

    def _render_update_check(self, result: UpdateCheck) -> None:
        ctkw.apply_button_state(self._update_btn, "normal")
        if result.status == "update_available":
            self._log(f"Update available: version {result.latest} "
                      f"(you have {result.current}). Download it manually from "
                      "the project's GitHub releases — nothing is auto-installed.",
                      "WARNING")
        elif result.status == "up_to_date":
            self._log(f"Update check: {result.detail}", "PASS")
        else:
            self._log(f"Update check unavailable: {result.detail}", "WARNING")
        self.set_status(f"Update check: {result.status.replace('_', ' ')}.")

    # ---------- monitoring ----------
    def _apply_interval(self) -> None:
        try:
            self.monitor.set_interval(float(self.refresh_interval.get()))
        except (tk.TclError, ValueError):
            pass

    def _poll_interval_ms(self) -> int:
        try:
            return int(max(0.5, float(self.refresh_interval.get())) * 1000)
        except (tk.TclError, ValueError):
            return 1000

    def toggle_monitoring(self) -> None:
        if self.monitor_running:
            self.monitor.stop()
            self.monitor_running = False
            self.mon_btn.configure(text="▶  Start monitoring")
            self.head_status.configure(text="● idle", fg=T.FG_MUTED)
            self.set_status("Monitoring stopped.")
            self._log("Live monitoring stopped.", "INFO")
            return
        self._apply_interval()
        if self.monitor.start():
            self.monitor_running = True
            self.mon_btn.configure(text="■  Stop monitoring")
            self.head_status.configure(text="● live", fg=T.NEON_GREEN)
            self.set_status("Live monitoring running…")
            self._log("Live monitoring started.", "PASS")
            if not self._monitor_polling:
                self._monitor_polling = True
                self._reschedule(500, self._poll_monitor)
        else:
            messagebox.showinfo("Monitoring", "Monitoring is already running.")

    def _poll_monitor(self) -> None:
        if not self.monitor_running:
            self._monitor_polling = False
            return
        try:
            items = self.monitor.poll()
            snaps = [i for i in items if isinstance(i, HardwareSnapshot)]
            samples = [i for i in items if isinstance(i, Sample)]
            for s in samples:
                if s.cpu is not None:
                    self.cpu_hist.append(s.cpu)
                if s.mem is not None:
                    self.mem_hist.append(s.mem)
                for hist in (self.cpu_hist, self.mem_hist):
                    if len(hist) > 120:
                        del hist[: len(hist) - 120]
            if self.chart is not None:
                self.chart.update(self.cpu_hist, self.mem_hist)
            if snaps:
                snap = snaps[-1]
                # Monitor snapshots are collected with include_gpu=False, so
                # their GPU status is a default "unsupported" stub. Applying it
                # verbatim would wipe the real GPU reading from the card on
                # every single tick. Keep the last known GPU data instead.
                if (not snap.gpu.supported and not snap.gpu.gpus
                        and self.latest_snap is not None
                        and self.latest_snap.gpu.supported):
                    snap.gpu = self.latest_snap.gpu
                self._apply_snapshot(snap)  # main thread — safe
                # Auto-Action Rules: evaluate on every monitoring tick. The
                # checks are cheap and main-thread safe; actual actions are
                # dispatched to worker threads inside the engine.
                try:
                    self.actions_engine.tick(snap, self.monitor.interval_s)
                except Exception:  # noqa: BLE001 - rules must not break monitoring
                    log.exception("auto-action tick failed; monitoring continues")
        except Exception:  # noqa: BLE001 - one bad tick must not kill the loop
            # Without this, an exception during _apply_snapshot silently
            # stopped live monitoring while the header kept saying "live".
            log.exception("monitor poll failed; polling continues")
        self._reschedule(self._poll_interval_ms(), self._poll_monitor)

    # ---------- processes ----------
    def refresh_processes(self) -> None:
        # Read the Tkinter variable here on the main thread; the worker only
        # receives a plain string and never touches Tkinter.
        # `_apply_snapshot` calls this on every monitoring tick, so guard
        # against overlapping collectors (a slow process_iter must not pile up
        # daemon threads).
        if self._procs_active:
            return
        try:
            sort_by = str(self.proc_sort.get())
        except tk.TclError:
            sort_by = "cpu"
        self._procs_active = True
        try:
            threading.Thread(target=self._proc_job, args=(sort_by,),
                             name="shds-procs", daemon=True).start()
        except (RuntimeError, OSError) as exc:
            log.warning("process thread failed to start: %s", exc)
            self._procs_active = False

    def _proc_job(self, sort_by: str) -> None:
        """Worker: collect top processes. Takes plain values only — no Tkinter."""
        try:
            rows = collect_processes(limit=14, sort_by=sort_by)
            self._bg_queue.put(("procs", rows))
        except Exception as exc:
            log.warning("process refresh failed: %s", exc)
        finally:
            self._procs_active = False

    def _render_procs(self, rows) -> None:
        try:
            self.proc_tree.delete(*self.proc_tree.get_children())
            for r in rows:
                self.proc_tree.insert("", "end",
                                      values=(r.pid, r.name, f"{r.cpu:.1f}", f"{r.mem_mb:.1f}"))
        except tk.TclError:
            pass

    # ---------- reports / tools ----------
    def export_report(self, fmt: str) -> None:
        if not self.latest_payload:
            messagebox.showinfo("No report yet",
                                "Run a diagnostic scan first, then export.")
            return
        ext = {"json": ".json", "txt": ".txt", "csv": ".csv"}[fmt]
        types = {"json": [("JSON", "*.json")], "txt": [("Text", "*.txt")],
                 "csv": [("CSV", "*.csv")]}[fmt]
        dest = filedialog.asksaveasfilename(
            defaultextension=ext, filetypes=types,
            initialfile=f"shds-report-{datetime.now().strftime('%Y%m%d-%H%M%S')}{ext}")
        if not dest:
            return
        try:
            path = save_report(self.latest_payload, dest)
        except (ValueError, PermissionError, OSError) as exc:
            messagebox.showerror("Export failed", str(exc))
            return
        messagebox.showinfo("Report saved", f"Saved report to:\n{path}")
        self.set_status(f"Report saved: {path}")
        self._log(f"Report saved: {path}", "PASS")

    # Pairs of (warning key, failure key) used to validate threshold ordering.
    THRESHOLD_PAIRS = [("cpu_warn", "cpu_fail"), ("mem_warn", "mem_fail"),
                       ("disk_warn", "disk_fail"), ("temp_warn", "temp_fail"),
                       ("gpu_temp_warn", "gpu_temp_fail")]

    @staticmethod
    def validate_thresholds(values: dict[str, float]) -> str | None:
        """Validate candidate thresholds without side effects.

        Returns an error message, or None when the values are consistent.
        Every entered value is checked before any of them is applied.
        """
        for key, val in values.items():
            if not 0 <= val <= 200:
                return f"'{key}' must be a number between 0 and 200."
        for warn_key, fail_key in ScannerApp.THRESHOLD_PAIRS:
            warn, fail = values.get(warn_key), values.get(fail_key)
            if warn is None or fail is None:
                continue
            if warn >= fail:
                return (f"'{warn_key}' ({warn:g}) must be less than "
                        f"'{fail_key}' ({fail:g}).")
        return None

    def apply_thresholds(self) -> None:
        # Parse everything first; only mutate self.thresholds when all values
        # are valid, so the active thresholds never end up half-applied.
        values: dict[str, float] = {}
        for key, var in self.th_vars.items():
            try:
                values[key] = float(var.get())
            except (ValueError, tk.TclError):
                messagebox.showerror("Invalid threshold", f"'{key}' must be a number.")
                return
        error = self.validate_thresholds(values)
        if error is not None:
            messagebox.showerror("Invalid threshold", error)
            self.set_status("Thresholds not applied — invalid values.")
            self._log(f"Thresholds rejected: {error}", "WARNING")
            return
        self.thresholds = values
        self._log(f"Thresholds updated: {self.thresholds}", "INFO")
        self.set_status("Thresholds applied.")

    def preview_cleanup(self) -> None:
        files, note = preview_temp_candidates()
        self._previewed = files
        try:
            self.clean_list.configure(state="normal")
            self.clean_list.delete("1.0", "end")
            self.clean_list.insert("end", note + "\n")
            if not files:
                self.clean_list.insert("end", "Nothing safe to delete.\n")
            for p in files:
                try:
                    size = p.stat().st_size
                except OSError:
                    size = 0
                self.clean_list.insert("end", f"{p}  ({format_bytes(size)})\n")
            self.clean_list.configure(state="disabled")
        except tk.TclError:
            pass
        self._log(f"Cleanup preview: {len(files)} file(s). {note}", "INFO")

    def clean_previewed(self) -> None:
        if not self._previewed:
            messagebox.showinfo("Cleanup", "Preview first — nothing selected.")
            return
        if not messagebox.askyesno(
                "Confirm deletion",
                f"Delete {len(self._previewed)} file(s) inside\n{approved_temp_root()}\n"
                "only? Locked files are skipped. This cannot be undone."):
            return
        root = approved_temp_root()
        removed, skipped, freed = 0, 0, 0
        for p in list(self._previewed):
            try:
                rp = p.resolve()
                rp.relative_to(root)  # containment check
                if rp.suffix.lower() not in SAFE_TEMP_SUFFIXES or not rp.is_file():
                    skipped += 1
                    continue
                size = rp.stat().st_size
                rp.unlink()
                freed += size
                removed += 1
            except (OSError, ValueError):
                skipped += 1
        self._previewed = []
        msg = (f"Cleanup done: removed {removed}, skipped {skipped}, "
               f"freed {format_bytes(freed)}.")
        self._log(msg, "PASS" if removed else "WARNING")
        messagebox.showinfo("Cleanup", msg)
        self.preview_cleanup()

    # ---------- shutdown ----------
    def on_closing(self) -> None:
        try:
            if self.monitor_running:
                self.monitor.request_stop()
        except Exception:
            pass
        self._scan_active = False
        # Cancel pending callbacks first: a callback that fires after destroy()
        # prints Tcl's "invalid command name" and is never useful.
        for after_id in list(self._after_ids):
            try:
                self.after_cancel(after_id)
            except tk.TclError:
                pass
        self._after_ids.clear()
        try:
            self.destroy()
        except tk.TclError:
            pass
