# MEGA PROMPT — Smart Hardware Diagnostic Scanner (for DeepSeek R1)
# Copy everything below the line into DeepSeek R1. Attach nothing else.
# Recommended settings: Temperature 0.2, Top-P 0.9, Max tokens 32000, Think mode ON.
# Expected output: ~13 source files + 6 test files + 3 requirements + README, complete and runnable.

---

You are DeepSeek R1, an expert senior Python engineer specializing in Windows desktop apps, Tkinter, psutil, secure subprocess handling, and threaded GUI design.

GOAL:
Generate the COMPLETE project "Smart Hardware Diagnostic Scanner — Dark Neon Edition" by Devansh, from scratch, file by file, with zero truncation. Every file must be complete, runnable, and mutually consistent. All 64 unit tests must pass with `python -m unittest discover -s tests -v`.

TECH STACK & CONSTRAINTS:
- Python 3.11+ (must run on 3.14). Only stdlib + psutil (core) + matplotlib (optional) + pytest (dev).
- Windows 11 primary. Linux/macOS must launch with graceful UNAVAILABLE degradation.
- Tkinter/ttk only for GUI. No web, no cloud, no Node.
- NEVER invent sensor data. Missing sensors/drivers = UNAVAILABLE/UNSUPPORTED with honest message.
- NEVER delete/terminate/modify anything except: user-confirmed temp-file cleanup (allow-listed) and report export.
- subprocess MUST use shell=False + explicit argv list + timeout=8s.
- All Tkinter widget updates on MAIN thread only. Background work via daemon threads + queue.Queue, polled with .after().
- Logging must NEVER block startup. RotatingFileHandler 512KB x3.
- No network speed test. Network tab = interface counters + up/down rates from successive samples.
- No RAM temperature via psutil. Always report UNSUPPORTED.
- GPU = NVIDIA only via nvidia-smi. AMD/Intel = UNAVAILABLE by design.

EXACT PROJECT TREE TO GENERATE:
```
smart_hardware_diagnostic_scanner/
  main.py
  requirements.txt
  requirements-optional.txt
  requirements-dev.txt
  README.md
  scanner/
    __init__.py
    theme.py
    utils.py
    logging_config.py
    hardware.py
    gpu.py
    diagnostics.py
    monitoring.py
    charts.py
    reports.py
    app.py
  tests/
    __init__.py
    test_diagnostics.py
    test_hardware.py
    test_gpu.py
    test_reports.py
    test_logging_config.py
    test_app.py
```

FILE 1 — scanner/__init__.py:
`__version__ = "1.0.0"`, `__author__ = "Devansh"`.

FILE 2 — scanner/theme.py (exact constants, valid Tk hex only):
BG_MAIN="#12121A", BG_PANEL="#1C1C28", BG_CARD="#232333", BG_SIDEBAR="#16161F", BG_OUTPUT="#0E0E14", BG_STATUSBAR="#16161F", FG_PRIMARY="#E8E8F0", FG_MUTED="#9A9AB0", FG_DIM="#6B6B85", NEON_CYAN="#00E5FF", NEON_PURPLE="#B266FF", NEON_GREEN="#39FF88", NEON_YELLOW="#FFD60A", NEON_RED="#FF4D6D", NEON_ORANGE="#FF9F1C", BORDER="#34344A", ENTRY_BG="#101018", PROGRESS_BG="#2A2A3D", FONT_FAMILY="Segoe UI", FONT_TITLE=(FAMILY,16,"bold"), FONT_HEADING=(FAMILY,11,"bold"), FONT_BODY=(FAMILY,10), FONT_SMALL=(FAMILY,9), FONT_MONO=("Consolas",9), STATUS_COLOURS={PASS:NEON_GREEN,WARNING:NEON_YELLOW,FAIL:NEON_RED,UNAVAILABLE:FG_MUTED,INFO:NEON_CYAN}, CARD_MIN_WIDTH=136, CARD_HEIGHT=150.

FILE 3 — scanner/utils.py (exact signatures):
- format_bytes(num: float)->str: float(num), "Unavailable" on TypeError/ValueError/negative/nan/inf; B/KB/MB/GB/TB/PB, 1024 divisor, "0 B" for 0, "{int} B" if idx==0 else "{value:.2f} unit". Must guard math.isfinite.
- pct(used,total)->float|None: None if not total or total<=0 or NaN; else round(used/total*100,1).
- is_windows()->bool, python_info()->str (sys.version single-line), os_info()->str, which(cmd)->str|None via shutil.which (no shell, try/except->None).
- safe_path_for_report(path_str, allowed_dir=None)->Path: ValueError on empty/directories/bad suffix (only .json/.txt/.csv); if allowed_dir, resolve().relative_to check; mkdir parents if needed.

FILE 4 — scanner/logging_config.py:
- APP_NAME="SmartHardwareDiagnosticScanner".
- _user_data_root()->Path: win32 -> %LOCALAPPDATA%/APP_NAME else %APPDATA%/APP_NAME else ~/AppData/Local/APP_NAME; darwin -> ~/Library/Application Support/APP_NAME; else $XDG_DATA_HOME/APP_NAME else ~/.local/share/APP_NAME.
- get_log_dir()->Path, NEVER raises: try primary=_user_data_root()/"logs", fallbacks=(primary, tempdir/APP_NAME/logs, cwd/logs); for each: mkdir(parents,exist_ok), write+delete ".write_test", return on success; on total failure return primary.
- configure_logging(level=INFO)->Logger named "shds", IDEMPOTENT (if handlers: return). RotatingFileHandler(scanner.log, maxBytes=512_000, backupCount=3, utf-8) + StreamHandler(WARNING). Formatter "%(asctime)s [%(levelname)s] %(name)s: %(message)s". propagate=False. File failure -> console-only + warning, never raise.

FILE 5 — scanner/gpu.py:
- NVIDIA_SMI_TIMEOUT=8. Dataclass GpuInfo(index,name,utilization_pct,temperature_c,memory_used_mb,memory_total_mb,fan_pct,power_w,available,source="nvidia-smi",error) and GpuStatus(supported,tool_found,gpus,message="Unavailable").
- _to_float(raw)->float|None: strip lower, remove w/%/c, None on "",n/a,unknown,[n/a],none.
- query_nvidia_gpus(timeout=8)->GpuStatus, NEVER raises: which("nvidia-smi") else message "nvidia-smi not found..."; argv=[exe,"--query-gpu=index,name,utilization.gpu,temperature.gpu,memory.used,memory.total,fan.speed,power.draw","--format=csv,noheader,nounits"], subprocess.run(shell=False,capture, text, timeout). Handle FileNotFoundError, TimeoutExpired, OSError, returncode!=0 (truncate stderr 300), empty stdout. Parse CSV lines, pad to 8 cols, idx=int(float(parts[0])) fallback len(gpus). supported=True, message "Found N NVIDIA GPU(s).".

FILE 6 — scanner/hardware.py:
- HAS_PSUTIL try import. Dataclasses: CpuSnapshot(available,percent,per_core,physical_cores,logical_cores,freq_current/min/max_mhz,load_avg,error), MemSnapshot(available,total,available_bytes,used,percent,error), DiskEntry(device,mountpoint,fstype,total,used,free,percent,accessible,error), NetSnapshot(available,interfaces,bytes_sent,bytes_recv,up_bps,down_bps,error), TempReading(label,current,source,status), ProcEntry(pid,name,cpu,mem_mb), HardwareSnapshot(cpu,mem,disks,net,temps,gpu,hostname,os_info,timestamp). Module _prev_net dict.
- _rate_bps(current,previous,dt)->float|None: None on None/dt<=0/current<previous (log counter reset); else (cur-prev)*8/dt.
- collect_cpu(): psutil.cpu_percent(interval=None) + percpu, cpu_count logical/physical, cpu_freq, getloadavg guarded by hasattr; available=True; on fail error+warning. No-psutil -> error "psutil not installed".
- collect_memory(): virtual_memory total/available/used/percent.
- collect_disks()->list: disk_partitions(all=False), dedupe by (mountpoint or device).lower(), disk_usage per part, PermissionError/FileNotFoundError/OSError -> accessible=False + error, never raise.
- collect_network(): net_if_addrs sorted keys, net_io_counters sent/recv, monotonic dt, per-counter _rate_bps (independent reset handling), first sample None rates, update _prev_net.
- collect_temps(): [] if no psutil -> [unsupported]; sensors_temperatures() else [unsupported]; per chip:item label f"{chip}:{label}", float(current) guarded, status available/unavailable; ALWAYS append RAM UNSUPPORTED "no reliable sensor interface".
- collect_processes(limit=12,sort_by="cpu"): process_iter(["pid","name","cpu_percent","memory_info"]), skip NoSuchProcess/AccessDenied/ZombieProcess, name[:60], mem rss/1MiB rounded 1dp, sort by mem_mb if sort_by=="mem" else cpu desc, return [:max(1,limit)].
- collect_snapshot(include_gpu=True): hostname via socket, os_info f"{system} {release} ({version})", timestamp time.time(), each section independent; if include_gpu: query_nvidia_gpus() try/except -> GpuStatus(message=f"GPU query failed: {exc}").

FILE 7 — scanner/diagnostics.py:
- DEFAULT_THRESHOLDS exact: cpu_warn 85, cpu_fail 97, mem_warn 85, mem_fail 96, disk_warn 85, disk_fail 95, temp_warn 85, temp_fail 100, gpu_temp_warn 83, gpu_temp_fail 95.
- Finding(component,status,message,detail), DiagnosticReport(started,finished,findings,summary,recommendations) + duration_s property max(0,fin-start).
- _status_for(value,warn,fail): None->UNAVAILABLE, >=fail->FAIL, >=warn->WARNING else PASS.
- diagnose_cpu: UNAVAILABLE if not available/percent None; else cores f"{P}P/{L}L", PASS "healthy", WARNING "high load...", FAIL "extreme load... (no damage claimed)". NEVER use word "damaged".
- diagnose_memory: format_bytes used/total, WARNING "memory pressure...", FAIL "critically low...".
- diagnose_disks: [] -> UNAVAILABLE; per disk inaccessible->UNAVAILABLE else _status_for disk thresholds.
- diagnose_gpu: not supported/empty -> UNAVAILABLE; per gpu temp available -> _status_for gpu_temp, elif util -> PASS "temp unavailable", else UNAVAILABLE.
- diagnose_network(snap,th): not available->UNAVAILABLE; no interfaces->WARNING; else PASS "N interface(s): ...".
- diagnose_temps: filter status==available and current not None, none->UNAVAILABLE "never invented"; else first 8 with _status_for temp thresholds f"{label}: {cur:.1f}C (source: ...)".
- build_recommendations(findings): CPU high, Memory high, Storage WARNING/FAIL, Sensors WARNING/FAIL, GPU UNAVAILABLE driver hint, Sensors UNAVAILABLE honesty note, else "All checks look healthy..."; append "N check(s) unavailable..." if any UNAVAILABLE. NEVER suggest delete system/format/registry/disable service.
- run_diagnostics(snap,thresholds=None): merge defaults+override, time start, all diagnose_*, summary dict PASS/WARNING/FAIL/UNAVAILABLE counts, build recommendations.

FILE 8 — scanner/monitoring.py:
- MAX_HISTORY=120. Sample(t,cpu,mem,gpu=None,up_bps,down_bps). MonitorWorker(interval_s=1.0,include_gpu=False): interval max(0.5,float), samples Queue, history list, _stop Event, _thread, _lock, running bool. start()->bool False if running else spawn daemon "shds-monitor". stop(timeout=3): set stop, join outside lock, running=False. set_interval clamps 0.5. _loop: while not stop: collect_snapshot, Sample(t=time.time(),cpu,mem,gpu first util,up/down), put sample+snap, append history trimmed, try/except log warning; wait interval-elapsed via _stop.wait. poll()->list drain get_nowait. clear_history().

FILE 9 — scanner/charts.py:
- try: matplotlib.use("TkAgg"), FigureCanvasTkAgg, Figure, HAS_MPL=True; except Exception: HAS_MPL=False, Figure=None, FigureCanvasTkAgg=None (must catch Exception not just ImportError).
- ChartPanel(parent,bg="#232333",fg="#E8E8F0"): if HAS_MPL create Figure(5.5,2.6,dpi100,facecolor bg), ax facecolor bg, ylim 0-100, xlabel samples, ylabel %, tick colors, spines #34344A, grid, line_cpu #00E5FF + line_mem #B266FF width 1.6, legend, tight_layout pad 0.8, FigureCanvasTkAgg pack fill both expand. update(cpu_hist,mem_hist): no-op if no MPL; else set_data with INDEPENDENT x ranges (range(len(cpu)) vs range(len(mem))), set_xlim 0,max(10,len), ylim 0-100, draw_idle, try/except debug.

FILE 10 — scanner/reports.py:
- build_report_dict(snap,diag,thresholds)->dict: ts datetime.fromtimestamp(finished or now).isoformat(sec), tool "Smart Hardware Diagnostic Scanner — Dark Neon Edition", author Devansh, timestamp, os snap.os_info, python python_info(), hostname, scan_duration round(duration,2), thresholds dict, hardware {cpu:asdict, memory:asdict, disks list, network:asdict, temperatures:asdict list, gpu {supported,message,gpus asdict}}, findings asdict, summary dict, recommendations list. Must be json.dumps-able.
- render_txt(payload)->str sections: header, Author/Timestamp/OS/Python/Host/Scan duration, == SUMMARY ==, == HARDWARE == (CPU %, Memory used/total via format_bytes, per accessible disk else UNAVAILABLE, Network interfaces, GPU message + per-GPU util/temp/mem, Temperatures available vs STATUS), == FINDINGS == [status] comp: msg + detail, == RECOMMENDATIONS == " - ...".
- save_report(payload,dest)->Path via safe_path_for_report: .json write_text json indent2, .txt render_txt, .csv writer header component,status,message,detail + rows; else ValueError. Catch PermissionError/OSError/ValueError log+reraise.

FILE 11 — scanner/app.py (largest, ~820 lines, Tkinter dark neon, ALL updates on main thread):
- Imports, log=configure_logging(), NAV_ITEMS=["Dashboard","Processes","Diagnostics","Reports & Tools"], SAFE_TEMP_SUFFIXES={.tmp,.log,.bak,.cache,.dmp,.old}.
- _status_of_pct(value,warn,fail), approved_temp_root()->Path(tempdir).resolve(), preview_temp_candidates(limit=200)->(list[Path],str): iterdir top-level only, skip symlink/non-file, suffix allow-list, resolve().relative_to(root) containment, except OSError/ValueError skip.
- class ScannerApp(tk.Tk): title "Smart Hardware Diagnostic Scanner — Dark Neon Edition  |  by Devansh", geometry 1180x760 minsize 960x620 bg BG_MAIN. thresholds=dict(DEFAULT), monitor=MonitorWorker(1.0,include_gpu=False), latest_snap/diag/payload None, cpu_hist/mem_hist [], refresh_interval DoubleVar 1.0, proc_sort StringVar "cpu", monitor_running False, _bg_queue Queue, _scan_active False. _build_style (clam, Card/Side/Title/Head/Accent/Ghost/Treeview/Progressbar/Entry/Spinbox), _build_layout (sidebar 210px brand "◈ SHDS", nav buttons, interval Combobox 0.5/1/2/5 readonly, monitor toggle; main col header + head_status "● idle" + pages_frame with 4 pages place-rel + status bar Ready/Last scan). show_page lift + pressed state. _card helper (CARD_MIN_WIDTH x CARD_HEIGHT, title dot val bar detail). _build_dashboard: cards_row grid 5 equal-weight cols (CPU/RAM/GPU/Disk/Network), <Configure>->_on_cards_resize (ignore width<CARD_MIN_WIDTH, target=max(60,col-8), wrap sync). Refresh + Run-scan buttons, chart_box ChartPanel if HAS_MPL else hint label. _build_processes: radio cpu/mem + Refresh, Treeview pid/name/cpu/mem + scrollbar. _build_diagnostics: Run scan + Clear (lambda chain), indeterminate scan_bar, Text + scrollbar + STATUS_COLOURS tags. _build_reports: Save JSON/TXT/CSV buttons, thresholds LabelFrame grid entries + Apply, cleanup LabelFrame approved path label + Preview/Delete buttons + clean_list Text + _previewed list.
- Data flows: refresh_once (skip if scan active, thread _collect_job collect_snapshot include_gpu True -> ("snapshot") / ("error")); run_scan (guard active, scan_bar.start(12), thread _scan_job snapshot+run_diagnostics+build_report -> ("scan") + ("scan_done")); _poll_bg_queue every 250ms: snapshot->_apply_snapshot, scan->_apply_snapshot+_apply_diag, scan_done->inactive+bar stop+idle, error->inactive+log FAIL, procs->_render_procs, monitor_items noop. ALL Tk config inside these main-thread methods; wrap TclError.
- _apply_snapshot: CPU card (val/bar/dot/status warn/fail, detail cores+MHz), RAM (used/total), GPU first (util/temp name[:22]), Disk worst accessible percent (mountpoint + N volumes), Network (N ifaces, down/up Mbps or first 3 names). set_status Updated HH:MM:SS host, then refresh_processes().
- _apply_diag: store diag/payload, scan_var Last scan timestamp (duration .1f), log each [status] comp: msg + detail, Recommendations, Summary PASS/WARNING/FAIL/UNAVAILABLE, set_status complete, show_page Diagnostics.
- Monitoring: _apply_interval, toggle_monitoring start/stop + head_status live/idle, _poll_monitor (poll Worker, split Snapshot/Sample, append non-None cpu/mem trimmed 120, chart.update, _apply_snapshot last snap, after interval ms).
- Processes: refresh_processes reads proc_sort.get() on MAIN (fallback "cpu" on TclError), thread _proc_job(sort_by) collect_processes(14) -> ("procs"); _render_procs delete+insert (pid,name,cpu .1f,mem .1f) TclError-safe. DELETE separate _drain_procs; single _poll_bg_queue owns queue.
- Reports/tools: export_report(fmt) needs latest_payload else info; asksaveasfilename defaultextension + filetypes + initialfile shds-report-%Y%m%d-%H%M%S; save_report try ValueError/PermissionError/OSError -> showerror; success info+PASS log. THRESHOLD_PAIRS=[(cpu_warn,cpu_fail),(mem_warn,mem_fail),(disk_warn,disk_fail),(temp_warn,temp_fail),(gpu_temp_warn,gpu_temp_fail)]. validate_thresholds(values) static: each 0..200 else "'k' must be number 0-200"; each pair warn>=fail -> "'w' (w) must be less than 'f' (f)". apply_thresholds: parse float all first (error dialog on fail), validate, only then self.thresholds=values. preview_cleanup: preview_temp_candidates -> clean_list + log. clean_previewed: needs preview else info; askyesno confirm; per file resolve+relative_to+suffix+is_file, stat size, unlink, count removed/skipped/freed format_bytes; log PASS/WARNING + info + re-preview.
- on_closing: monitor.stop guarded, _scan_active=False, destroy TclError-safe. after() reschedules wrapped in try TclError + winfo_exists guard to avoid teardown "invalid command name" errors.

FILE 12 — main.py (never silent close):
- _ensure_package_on_path: Path(__file__).parent insert 0 sys.path if missing.
- _log_hint()->str: from scanner.logging_config import get_log_dir -> str(dir/scanner.log) except -> r"%LOCALAPPDATA%\SmartHardwareDiagnosticScanner\logs\scanner.log".
- main()->int: try import ScannerApp+configure_logging except -> Tk withdraw + showerror Startup failed + exc + pip install -r requirements.txt + _log_hint(), destroy, return 1 (fallback stderr+traceback). try configure_logging, app=ScannerApp, mainloop, return 0 except -> logger shds exception Fatal, traceback, messagebox Fatal, return 1. if __name__=="__main__": raise SystemExit(main()).

REQUIREMENTS (3 files, split!):
- requirements.txt: ONLY "psutil>=5.9" + comment core runtime.
- requirements-optional.txt: "matplotlib>=3.7" + wheel-lag comment.
- requirements-dev.txt: "pytest>=7.0" + "pyinstaller>=6.0; sys_platform=='win32'".

README.md (sections 1-12): Title+author Devansh, Features (cards, monitoring, charts, thresholds, nvidia-smi 8s shell=False, processes read-only, JSON/TXT/CSV, rotating logs per-user %LOCALAPPDATA% + linux/mac paths + fallback, temp cleanup suffixes, thread-safe), OS support, Python 3.11+ tested 3.14, venv `py -3 -m venv .venv` (NOT pinned 3.11), Install (3 pip lines), Run `python main.py`, Tests pytest + unittest, Sensor honesty, Reports steps, Troubleshooting table (log per-user location), PyInstaller `--collect-data matplotlib --collect-submodules matplotlib` (NO duplicate flag), Project structure listing ALL 11 scanner files + ALL 6 test files.

TESTS (must all pass, mock hardware, never touch real disks):
- test_diagnostics: cpu PASS/WARNING/FAIL + UNAVAILABLE, memory thresholds, no "damag" word on high, custom thresholds, disk inaccessible/WARNING/FAIL/empty, gpu no-tool, network no-iface WARNING, temps unsupported/never-invented, full run summary counts + duration>=0 + recs, recs ban delete system/disable service/format/registry.
- test_hardware: format_bytes 0/1024/8GB/neg/junk, pct 50/0/neg, no-psutil paths, cpu mock, mem mock, disk invalid + dedupe, network counter reset per-counter (sent reset->up None down>0, recv reset->down None up>0), _rate_bps reset/equal/grow/bad-dt, temps empty, process AccessDenied skip, snapshot gpu-fail never raises.
- test_gpu: missing tool, timeout, nonzero exit, empty output, valid parse + shell False + argv list assert, N/A->None, OSError.
- test_reports: json roundtrip, txt sections FINDINGS/RECOMMENDATIONS/SUMMARY/HARDWARE, save json/txt/csv in tempdir + header check, invalid suffix ValueError, matplotlib absent import, no-psutil snapshot builds UNAVAILABLE.
- test_logging_config: win LOCALAPPDATA, win APPDATA fallback, linux XDG/home, mac Application Support, unwritable->temp fallback, never raises, configure returns shds non-propagate idempotent, file-error->console-only.
- test_app: validate_thresholds static (mixed bad rejected, equal/greater rejected, less accepted, out-of-range, pairs independent), process sort plain string to worker + failure logged + TclError fallback cpu (real Tk, mocked psutil/mpl/messagebox), dashboard 5 cards fit at 1180/960/700 + tiny-width ignored.

REASONING ORDER:
1. Plan all file interfaces first for consistency.
2. Generate theme→utils→logging→gpu→hardware→diagnostics→monitoring→charts→reports→app→main→requirements→tests→README.
3. Self-check cross-file: log path code==README, requirements split==HAS_MPL/HAS_PSUTIL fallbacks, thresholds README==diagnostics, suffixes app==README, tests import names exist.

OUTPUT FORMAT:
For each file: `## FILE: path` then complete fenced code block. No truncation, no "...rest...". Minimal comments. End with run instructions + "64 tests expected OK".

Begin generation now.
