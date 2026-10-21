# Smart Hardware Diagnostic Scanner — Dark Neon Edition

**Author:** Devansh

A brand-new, fully functional Python desktop application for Windows 11 that monitors
CPU, RAM, GPU, disks, network, processes and sensors, runs a modular diagnostic scan,
shows live charts, and exports JSON / TXT / CSV reports — all in a dark-neon Tkinter UI.

## 1. Features

- Dashboard cards for CPU, RAM, GPU, Disk, Network with progress bars + status dots.
- **Per-core CPU bars** drawn live inside the CPU card.
- **Disk I/O throughput** (read/write per second) shown on the Disk card.
- **Battery card data**: level, AC/battery state and time remaining, with its own
  PASS / WARNING / FAIL rule; desktops honestly report UNAVAILABLE.
- **Uptime** and battery state in the status bar.
- Live monitoring (start/stop) with configurable refresh interval (0.5 / 1 / 2 / 5 s).
- **Live threshold alerts**: an in-app banner always, plus a native desktop toast
  when `plyer` is installed (repeat alerts for the same component are limited by a
  2-minute cooldown). Can be switched off in the sidebar.
- Live CPU + RAM chart when Matplotlib is installed (graceful fallback otherwise).
- **System Health score (0–100)** on the Dashboard: combines CPU (30%), RAM (25%),
  worst disk (25%) and OS-level findings (20%). Unavailable metrics are left out
  and the remaining weights renormalise, so a missing sensor never pretends the
  machine is broken; the OS share is excluded until a diagnostic scan has run.
- **Cross-platform OS error detection** (read-only): the Windows Update service,
  Defender real-time protection and the System Restore policy (Windows); `dmesg`
  hardware errors, inode exhaustion (`df -i`) and failed systemd services
  (Linux); recent unified-log errors and stale/missing Time Machine backups
  (macOS). Every check uses explicit argv, `shell=False` and an 8-second timeout,
  and missing/permission-denied commands degrade to UNAVAILABLE.
- **Error Explainer + "How to fix"**: FAIL/WARNING findings get a plain-English
  explanation and a safe, user-initiated action — opening the relevant settings
  page (e.g. `ms-settings:windowsupdate`, Storage Sense) or copying a read-only
  command (`df -i`, `systemctl --failed`, `dmesg --level=err,warn`). The app
  never applies a fix by itself.
- **Compare with last scan**: every scan is diffed against the previous one, listing
  regressions / improvements / new / resolved checks in the output.
- **Save diff HTML**: a self-contained HTML document comparing the last two scans
  (`scanner/reports.py` builds the same diff for the package version).
- **Start-up viewer (read-only)**: Windows Run/RunOnce registry keys + user
  Startup folder, Linux `~/.config/autostart` + `/etc/xdg/autostart`, macOS
  `~/Library/LaunchAgents` + `/Library/LaunchAgents`. Nothing is ever disabled.
- **Connections page**: one snapshot of INET sockets with the owning process
  where visible (read-only; macOS may require privileges and says so).
- **SMART disk health** (`smartctl` from smartmontools, optional): per-device
  PASS / FAIL / UNAVAILABLE verdicts from the documented exit-code bitfield.
- **Largest-files finder (read-only)**: pick any folder, get the top 25 files
  with a time budget; nothing is deleted.
- **Scheduled weekly scan**: registers the headless CLI in Windows Task
  Scheduler (`schtasks`, explicit argv, no shell) or on a marked cron line;
  removal touches only the SHDS entries.
- **Update check** (manual, metadata-only): compares `App version` against a
  GitHub releases URL — never downloads or installs anything.
- **Dark / light theme**: live toggle in the sidebar; the choice is persisted
  for the single-file app in `config.json`.
- **Scan history + trends** in a local SQLite database, with a History page that
  charts health score, CPU and RAM across scans and lists every stored scan.
- Modular diagnostic engine: PASS / WARNING / FAIL / UNAVAILABLE with documented,
  configurable thresholds.
- GPU detection via `nvidia-smi`, `rocm-smi` (AMD) or `intel_gpu_top` (Intel) —
  8 s timeout, `shell=False`, never fabricated.
- Process list (PID, name, CPU %, Mem MB), sorted by CPU or memory, read-only.
- **Process details window** (double-click a row): executable path, command line,
  user, threads, children, network connections and open files — read-only, and
  permission failures are shown per field rather than crashing.
- Reports: JSON + readable TXT + CSV + **self-contained HTML** (inline CSS + an
  embedded health-trend chart) export to a user-selected destination.
- **Thresholds persist** between runs in `config.json` (window size is remembered
  too), validated before being applied and sanitised on load.
- **Keyboard shortcuts**: F5 refresh, Ctrl+R scan, Ctrl+H history, Ctrl+S save JSON,
  Ctrl+D dashboard.
- **Headless CLI** (`--scan`) for Task Scheduler / CI, with JSON/TXT/CSV/HTML output
  and meaningful exit codes.
- Rotating logs (`scanner.log`, 512 KB × 3) in a per-user writable location
  (`%LOCALAPPDATA%\SmartHardwareDiagnosticScanner\logs` on Windows,
  `~/.local/share/…` on Linux, `~/Library/Application Support/…` on macOS);
  the temp directory is used as a fallback and logging never blocks startup.
- Safe temp-file cleanup: optional, preview-first, top-level temp dir only,
  allow-listed suffixes (`.tmp .log .bak .cache .dmp .old`), containment-checked,
  locked files skipped, confirmation required.
- Thread-safe design: background workers + queue, Tkinter updated on main thread only.

## 2. Supported operating systems

- Primary: **Windows 11** (Windows 10 also works).
- Linux / macOS: fully supported. Launchers per OS:
  - Windows: double-click `Open Smart Hardware Scanner.bat`
  - Linux / macOS: `chmod +x shds.sh && ./shds.sh`
    (arguments are forwarded, e.g. `./shds.sh --scan --quiet --json out.json`)
- Data locations adapt to each OS automatically:
  - Windows: `%LOCALAPPDATA%\SmartHardwareDiagnosticScanner`
  - macOS: `~/Library/Application Support/SmartHardwareDiagnosticScanner`
  - Linux: `~/.local/share/SmartHardwareDiagnosticScanner`
  (overridable on Linux/macOS via `XDG_DATA_HOME`)
- UI/monospace fonts are chosen per OS (Segoe UI/Consolas, Helvetica Neue/Menlo,
  DejaVu Sans/DejaVu Sans Mono); Tk falls back to its default if one is absent.
- OS-level checks are read-only and platform-specific:
  - **Windows:** `wuauserv` (Windows Update), Defender real-time protection,
    and the documented `DisableSR` System Restore policy value.
  - **Linux:** `dmesg` hardware/segfault lines, inode usage from `df -i -P`,
    and failed units from `systemctl --failed`.
  - **macOS:** recent unified-log errors (`log show … --last 1h`) and the age of
    the latest Time Machine backup (`tmutil latestbackup`).
- What degrades per platform (honestly reported, never invented):
  - temperature sensors: unsupported on Windows/macOS stock Python
  - GPU metrics: NVIDIA via `nvidia-smi`, AMD via `rocm-smi`, Intel via
    `intel_gpu_top` — whichever the system actually provides
  - battery: unavailable on desktops/VMs on any OS

## 3. Python requirements

- Python **3.11+** (CI covers 3.11–3.14; brand-new interpreters need recent wheels).
- Tkinter included with standard Windows Python from python.org.
- `psutil` strongly recommended (the app degrades to UNAVAILABLE metrics without
  it); `matplotlib` optional for charts; `plyer` optional for native desktop
  toasts (the in-app banner always works).

## 4. Virtual environment (Windows PowerShell)

```powershell
cd smart_hardware_diagnostic_scanner
py -3 -m venv .venv   # any 3.11+ works, e.g. py -3.14
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
```

If script execution is blocked:

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

## 5. Install dependencies

```powershell
pip install -r requirements.txt
# optional: live charts (the app degrades gracefully without it)
pip install -r requirements-optional.txt
# development: tests + Windows EXE build
pip install -r requirements-dev.txt
```

## 6. Run the application

```powershell
python main.py
```

> **Which entry point?** `main.py` runs the modular package build (`scanner/`).
> `shds_gui.py` is the fuller single-file build; the feature list above (scan
> history, HTML reports, battery, per-core bars, process details) refers to it,
> and it also provides the `--scan` CLI. Both share the same diagnostics engine
> and degrade gracefully when `psutil` / `matplotlib` are missing.
>
> **Single-file-only features** (present in `shds_gui.py`, not in the
> `scanner/` package build): scan history / History tab / trend HTML,
> `config.json` persistence of thresholds, window geometry and theme, the
> battery card, native `plyer` toasts, per-core bars and the process-details
> window.
>
> `main.py` takes no command-line arguments. For a headless run use
> `python shds_gui.py --scan …`.

**Everything is also available as a single self-contained file**
(`shds_gui.py`) — double-click `Open Smart Hardware Scanner.bat`, or:

```powershell
python shds_gui.py
```

No Node.js, web server, cloud account, or internet connection required.

### Headless / scheduled scans

`shds_gui.py --scan` runs one scan without opening a window, which makes it easy to
schedule in Task Scheduler or call from CI:

```powershell
python shds_gui.py --scan --json report.json --html report.html
python shds_gui.py --scan --quiet --no-gpu --no-history --csv report.csv
```

| Flag | Effect |
|---|---|
| `--scan` | headless single scan (no GUI) |
| `--json/--txt/--csv/--html PATH` | write one or more report formats |
| `--no-gpu` | skip the `nvidia-smi` query (faster) |
| `--no-history` | do not add the scan to the local history database |
| `--thresholds PATH` | load thresholds from a specific config JSON |
| `--quiet` | print only the summary (health score + counts), not the per-finding list |

Exit codes: `0` = no FAIL findings, `1` = at least one FAIL, `2` = the scan or a
report could not run. That makes it usable as a scheduled health check, e.g.
"alert me when exit code is not 0".

## 7. Run tests

```powershell
python -m pytest -q
# or without pytest:
python -m unittest discover -s tests -v
```

Tests mock hardware/subprocess — they never touch your real disks and never
delete real files.

## 8. Hardware sensor limitations (honest)

- **CPU temperature:** reported only if `psutil.sensors_temperatures()` exposes it
  (rare on Windows laptops). Otherwise UNAVAILABLE — never invented.
- **RAM temperature:** standard `psutil` cannot read it; always reported
  UNSUPPORTED unless a real vendor SDK provides it.
- **GPU:** NVIDIA via `nvidia-smi`, AMD via `rocm-smi`, Intel via `intel_gpu_top`
  (`scanner/gpu.py` tries them in that order). No vendor tool present →
  UNAVAILABLE; Intel metrics need a complete `intel_gpu_top` sample.
- **Disks:** inaccessible/permission-denied partitions shown as UNAVAILABLE.
- **Network:** interface counters + up/down rates from successive samples.
  This is **not** an internet speed test.
- **Disk I/O:** aggregate throughput from successive samples; the first sample
  after start has no rate (reported as `n/a`), never a guess.
- **Battery:** read via `psutil.sensors_battery()`. Desktops report UNSUPPORTED /
  UNAVAILABLE rather than an invented level, and "time remaining" is omitted when
  the OS does not provide it.
- **System Health score:** a documented 0–100 summary combining live metrics and
  OS-level findings (see Features), not a vendor diagnostic. It summarises this
  app's own checks only; UNAVAILABLE checks are never counted as failures.

## 9. Generate reports

1. Click **Run diagnostic scan** (Dashboard or Diagnostics tab).
2. Open **Reports & Tools** → **Save JSON / TXT / CSV / HTML**.
3. Pick a destination in the file dialog.

Reports contain timestamp, OS/Python, uptime, the health score, available
hardware (including battery and disk I/O), findings, warnings, unavailable
metrics, recommendations, thresholds, and scan duration. The HTML report is
fully self-contained — inline CSS plus an embedded trend chart when Matplotlib
is available — so it can be e-mailed or opened from anywhere without extra
files. The official `SHDS.exe` build excludes Matplotlib, so that build ships
the chart-less variant.

**Scan history** lives in a local SQLite database (History tab). It stores one
row per scan (timestamp, score, CPU/RAM/disk, FAIL and WARNING counts), shows a
trend chart, and can be cleared at any time. **Save trend HTML** exports it.

## 10. Troubleshooting

| Symptom | Fix |
|---|---|
| `psutil not installed` warning | `pip install psutil` |
| Charts show install hint | `pip install matplotlib` |
| `nvidia-smi not found` | Install NVIDIA drivers; AMD (`rocm-smi`)/Intel (`intel_gpu_top`) are tried next |
| `smartctl not found` | Optional: install smartmontools for disk-health checks |
| Connections empty on macOS | The socket table needs elevated privileges there; the app says so instead of showing a blank list |
| Empty temps | Normal on many Windows laptops — reported as unsupported |
| App shows error dialog at startup | `pip install -r requirements.txt`, check the log file (see Features for its per-user location) |
| UI frozen | Should not happen (workers + queue); kill and report the log file (per-user location, see Features) |

## 11. Build a Windows executable (PyInstaller)

```powershell
pip install pyinstaller   # or (Windows only): pip install -r requirements-dev.txt
python -m PyInstaller --noconfirm --clean shds.spec
```

- Output: `dist\SHDS.exe` (onefile, console kept so the CLI still works).
- Alternative (older, folder build of the package entry point):

```powershell
pyinstaller --noconfirm --clean --windowed --name SHDS-DarkNeon `
  --collect-submodules scanner `
  --hidden-import psutil `
  main.py
```

- The `shds.spec` build intentionally **excludes matplotlib/plyer** — the charts
  area then shows its documented placeholder, keeping the binary lean, and the
  HTML report ships without the embedded trend chart. To ship charts, copy the
  spec, **remove `matplotlib` from `excludes`**, and add
  `--collect-data matplotlib --collect-submodules matplotlib` to the copy
  (an `excludes` entry wins over the collect flags in the same spec).
- External tools (`nvidia-smi`, `rocm-smi`, `intel_gpu_top`, `smartctl`,
  `schtasks`, `crontab`) are **never bundled**; target machines need them for
  those features, otherwise the app degrades honestly.
- CI: `.github/workflows/ci.yml` (at the repository root) runs the test suite
  on Linux/Windows/macOS (Python 3.11–3.14, with `xvfb` on Linux for the GUI
  tests) and builds + smoke-tests `SHDS.exe` as an artifact.

## 12. Project structure

```text
smart_hardware_diagnostic_scanner/
  main.py                     # package-based entry point
  shds_gui.py                 # single-file app (GUI + CLI + all modules)
  shds.spec                   # PyInstaller onefile spec (SHDS.exe)
  .github/workflows/ci.yml    # multi-OS test matrix + Windows EXE build
  Open Smart Hardware Scanner.bat   # one-tap launcher (pythonw, no console)
  shds.sh                     # Linux/macOS launcher (python3/python fallback)
  requirements.txt
  requirements-optional.txt
  requirements-dev.txt
  README.md
  scanner/
    __init__.py
    app.py
    theme.py                  # dark/light palettes + per-OS fonts
    diagnostics.py
    explainer.py              # plain-English explanations + safe fixes
    hardware.py
    gpu.py                    # nvidia-smi / rocm-smi / intel_gpu_top chain
    monitoring.py
    charts.py
    reports.py                # build + render + comparison-HTML diff
    smart.py                  # SMART health via smartctl (optional)
    netview.py                # per-process connections (read-only)
    startup.py                # OS startup entries (read-only)
    filesutil.py              # largest-files finder (read-only)
    updater.py                # GitHub release check (metadata only)
    scheduler.py              # pure schtasks/cron builders
    logging_config.py
    utils.py
  tests/
    __init__.py
    test_app.py
    test_diagnostics.py
    test_explainer.py
    test_hardware.py
    test_reports.py
    test_gpu.py
    test_logging_config.py
    test_theme.py
    test_smart.py
    test_netview.py
    test_startup.py
    test_filesutil.py
    test_updater.py
    test_scheduler.py
    test_features.py          # headless tests for the single-file helpers
    test_shds_gui.py          # tests for the new single-file features
    test_regressions.py       # regression tests pinning fixed bugs
```

Runtime data (created on first run, per user):

```text
%LOCALAPPDATA%\SmartHardwareDiagnosticScanner\
  config.json    # thresholds, alert toggle, window geometry, theme
  history.db     # SQLite scan history (History tab)
  logs\scanner.log
```

## 13. Optional integrations (user-confirmed only)

- **Scheduled weekly scan** — "Reports & Tools" page, day + HH:MM. Windows uses
  `schtasks /Create /F /TN "SHDS weekly scan" /SC WEEKLY …` (explicit argv,
  `shell=False`); Linux/macOS rewrite the user's crontab, keeping every other
  job and replacing only the line tagged with the SHDS marker. The scheduled
  command runs the documented headless flags: `--scan --quiet --no-history`.
  Both "Register" and "Remove" ask for confirmation and log the exact command.
- **SMART disk health** — optional `smartctl`. The verdict mapping is
  documented in `scanner/smart.py` (and mirrored in `shds_gui.py`): smartctl's
  exit-code bit reads bit 3 (value 8) as FAIL, exit 0 as PASS, everything else
  as UNAVAILABLE — an unreadable disk is never reported as failed.
- **Update check** — clicking "⇩ Check for updates" fetches one small JSON
  response from `scanner/updater.py`'s `DEFAULT_RELEASES_URL`. That URL is a
  **placeholder** (`https://api.github.com/repos/your-github-user/…`): point it
  at your real repository by setting `"update_url"` in `config.json` once this
  project is pushed to GitHub. The check is manual, metadata-only, and never
  downloads or installs anything.
- **Startup entries / Connections / Largest files** — read-only viewers; the
  app never disables a startup entry, never signals a process from these
  pages, and never deletes a file it merely listed.

