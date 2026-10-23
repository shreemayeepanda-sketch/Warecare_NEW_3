"""Diagnostic report building + JSON/TXT/CSV export with safe file handling."""
from __future__ import annotations

import csv
import html
import json
import logging
import platform
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from .diagnostics import DiagnosticReport
from .hardware import HardwareSnapshot
from .utils import format_bytes, python_info, safe_path_for_report

log = logging.getLogger("shds.reports")


def _safe_int(value: object) -> int | None:
    """Best-effort int conversion; None for anything unusable (never raises)."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def build_report_dict(snap: HardwareSnapshot, diag: DiagnosticReport,
                      thresholds: dict) -> dict:
    """Serialisable report payload."""
    ts = datetime.fromtimestamp(diag.finished_at or time.time()).isoformat(timespec="seconds")
    disks = [
        {"device": d.device, "mountpoint": d.mountpoint, "fstype": d.fstype,
         "total": d.total, "used": d.used, "free": d.free, "percent": d.percent,
         "accessible": d.accessible, "error": d.error}
        for d in snap.disks
    ]
    gpu = {
        "supported": snap.gpu.supported,
        "message": snap.gpu.message,
        "gpus": [asdict(g) for g in snap.gpu.gpus],
    }
    temps = [asdict(t) for t in snap.temps]
    return {
        "tool": "Smart Hardware Diagnostic Scanner — Dark Neon Edition",
        "author": "Devansh",
        "timestamp": ts,
        "os": snap.os_info or f"{platform.system()} {platform.release()}",
        "python": python_info(),
        "hostname": snap.hostname,
        "scan_duration_s": round(diag.duration_s, 2),
        "thresholds": dict(thresholds),
        "hardware": {
            "cpu": asdict(snap.cpu),
            "memory": asdict(snap.mem),
            "disks": disks,
            "network": asdict(snap.net),
            "temperatures": temps,
            "gpu": gpu,
        },
        "findings": [asdict(f) for f in diag.findings],
        "summary": dict(diag.summary),
        "recommendations": list(diag.recommendations),
    }


def render_txt(payload: dict) -> str:
    """Human-readable text rendering of the payload."""
    lines = [
        "SMART HARDWARE DIAGNOSTIC SCANNER — DARK NEON EDITION",
        f"Author: {payload.get('author', 'Devansh')}",
        f"Timestamp: {payload.get('timestamp', '')}",
        f"OS: {payload.get('os', '')}",
        f"Python: {payload.get('python', '')}",
        f"Host: {payload.get('hostname', '')}",
        f"Scan duration: {payload.get('scan_duration_s', 0)}s",
        "",
        "== SUMMARY ==",
    ]
    for key, val in payload.get("summary", {}).items():
        lines.append(f"  {key}: {val}")
    lines.append("")
    lines.append("== HARDWARE ==")
    hw = payload.get("hardware", {})
    cpu = hw.get("cpu", {})
    mem = hw.get("memory", {})
    cpu_pct = cpu.get("percent")
    mem_pct = mem.get("percent")
    lines.append(f"CPU: {cpu_pct if cpu_pct is not None else 'n/a'}% logical={cpu.get('logical_cores')} "
                 f"physical={cpu.get('physical_cores')} freq={cpu.get('freq_current_mhz')} MHz")
    lines.append(f"Memory: {mem_pct if mem_pct is not None else 'n/a'}% used "
                 f"({format_bytes(mem.get('used', 0))} / {format_bytes(mem.get('total', 0))})")
    for d in hw.get("disks", []):
        if d.get("accessible"):
            lines.append(f"Disk {d.get('mountpoint')}: {d.get('percent')}% used "
                         f"({format_bytes(d.get('used', 0))} / {format_bytes(d.get('total', 0))})")
        else:
            lines.append(f"Disk {d.get('mountpoint') or d.get('device')}: UNAVAILABLE ({d.get('error', '')})")
    net = hw.get("network", {})
    lines.append(f"Network: interfaces={', '.join(net.get('interfaces', [])) or 'none'}")
    gpu = hw.get("gpu", {})
    lines.append(f"GPU: {gpu.get('message', 'Unavailable')}")
    for g in gpu.get("gpus", []):
        lines.append(f"  GPU{g.get('index')} {g.get('name')}: util={g.get('utilization_pct')} "
                     f"temp={g.get('temperature_c')} mem={g.get('memory_used_mb')}/{g.get('memory_total_mb')} MB")
    lines.append("Temperatures:")
    for t in hw.get("temperatures", []):
        if t.get("status") == "available":
            lines.append(f"  {t.get('label')}: {t.get('current')} C (source: {t.get('source')})")
        else:
            lines.append(f"  {t.get('label')}: {str(t.get('status') or 'unknown').upper()} "
                         f"(source: {t.get('source')})")
    lines.append("")
    lines.append("== FINDINGS ==")
    for f in payload.get("findings", []):
        lines.append(f"[{f.get('status')}] {f.get('component')}: {f.get('message')}")
        if f.get("detail"):
            lines.append(f"    detail: {f.get('detail')}")
    lines.append("")
    lines.append("== RECOMMENDATIONS ==")
    for r in payload.get("recommendations", []):
        lines.append(f" - {r}")
    lines.append("")
    return "\n".join(lines)


def render_html(payload: dict) -> str:
    """Render a self-contained HTML report of one scan.

    Every hardware-derived value is escaped before it reaches the document,
    so a hostile device name can never inject markup.
    """
    esc = html.escape
    ts = str(payload.get("timestamp", ""))
    host = str(payload.get("hostname", ""))
    summary = payload.get("summary", {}) or {}

    cards: list[tuple[str, str]] = [
        ("Host", host),
        ("Timestamp", ts),
        ("OS", str(payload.get("os", ""))),
        ("Python", str(payload.get("python", ""))),
        ("Scan duration", f"{payload.get('scan_duration_s', '—')} s"),
    ]
    for label, key in (("PASS", "PASS"), ("WARNING", "WARNING"),
                       ("FAIL", "FAIL"), ("UNAVAILABLE", "UNAVAILABLE")):
        cards.append((label, str(summary.get(key, 0))))

    kv = "".join(
        f'<div class="kv"><div class="k">{esc(label)}</div>'
        f'<div class="v">{esc(value)}</div></div>'
        for label, value in cards)

    rows: list[str] = []
    for finding in payload.get("findings", []) or []:
        status = str(finding.get("status", ""))
        detail = str(finding.get("detail", "") or "")
        rows.append(
            "<tr>"
            f'<td><span class="b-{esc(status)}">{esc(status)}</span></td>'
            f"<td>{esc(str(finding.get('component', '')))}</td>"
            f"<td>{esc(str(finding.get('message', '')))}</td>"
            f"<td>{esc(detail)}</td>"
            "</tr>")

    recs = "".join(f"<li>{esc(str(r))}</li>"
                   for r in (payload.get("recommendations", []) or []))
    rec_html = (f"<h2>Recommendations</h2><ul>{recs}</ul>" if recs else "")

    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SHDS report {esc(ts)}</title>
<style>{_COMPARISON_CSS}</style></head>
<body>
<h1>Smart Hardware Diagnostic Scanner</h1>
<div class="sub">Scan report · {esc(ts)} · {esc(host)}</div>
<div class="grid">{kv}</div>
<h2>Findings</h2>
<table><tr><th>Status</th><th>Component</th><th>Message</th><th>Detail</th></tr>
{''.join(rows)}</table>
{rec_html}
<footer>Generated by the Smart Hardware Diagnostic Scanner. Findings describe
this application's own checks, not a vendor diagnostic.</footer>
</body></html>
"""


def save_report(payload: dict, dest: str | Path) -> Path:
    """Write JSON/TXT/CSV/HTML based on suffix. Raises with friendly message on failure."""
    path = safe_path_for_report(str(dest))
    try:
        if path.suffix.lower() == ".json":
            path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        elif path.suffix.lower() == ".txt":
            path.write_text(render_txt(payload), encoding="utf-8")
        elif path.suffix.lower() == ".csv":
            with path.open("w", newline="", encoding="utf-8") as fh:
                writer = csv.writer(fh)
                writer.writerow(["component", "status", "message", "detail"])
                for f in payload.get("findings", []):
                    writer.writerow([f.get("component"), f.get("status"),
                                     f.get("message"), f.get("detail", "")])
        elif path.suffix.lower() == ".html":
            path.write_text(render_html(payload), encoding="utf-8")
        else:
            raise ValueError("Unsupported report format.")
    except (PermissionError, OSError, ValueError) as exc:
        log.warning("report save failed: %s", exc)
        raise
    return path


_COMPARISON_CSS = """
  body { margin:0; padding:28px; background:#12121A; color:#E8E8F0;
         font:14px/1.55 "Segoe UI",system-ui,sans-serif; }
  h1 { font-size:22px; margin:0 0 4px; color:#00E5FF; }
  h2 { font-size:15px; margin:26px 0 10px; color:#00E5FF;
       border-bottom:1px solid #34344A; padding-bottom:6px; }
  .sub { color:#9A9AB0; font-size:12px; margin-bottom:18px; }
  table { border-collapse:collapse; width:100%; background:#1C1C28;
          border:1px solid #34344A; }
  th,td { text-align:left; padding:7px 10px; border-bottom:1px solid #34344A;
          font-size:13px; vertical-align:top; }
  th { background:#232333; color:#00E5FF; font-weight:600; }
  .grid { display:flex; flex-wrap:wrap; gap:10px; }
  .kv { background:#232333; border:1px solid #34344A; border-radius:8px;
        padding:10px 12px; min-width:170px; flex:1 1 170px; }
  .kv .k { color:#9A9AB0; font-size:11px; text-transform:uppercase; }
  .kv .v { font-size:16px; margin-top:3px; }
  .b-PASS { color:#39FF88; } .b-WARNING { color:#FFD60A; }
  .b-FAIL { color:#FF4D6D; } .b-UNAVAILABLE { color:#9A9AB0; }
  .b-INFO { color:#00E5FF; }
  code { background:#232333; padding:1px 5px; border-radius:4px; }
  footer { margin-top:26px; color:#9A9AB0; font-size:11px; }
"""


def render_comparison_html(comparison: dict,
                           previous_payload: dict | None,
                           current_payload: dict) -> str:
    """Render a self-contained HTML diff of two scans. Nothing is invented:
    only statuses that changed (or appeared/disappeared) are listed.
    """
    esc = html.escape
    cur_ts = str(current_payload.get("timestamp", ""))
    prev_ts = str((previous_payload or {}).get("timestamp", "") or "n/a")
    host = str(current_payload.get("hostname", ""))
    # The scanner/ package does not compute a health score, so the key is
    # usually absent. Never invent one: render "n/a" instead of a fake 100.
    cur_score = _safe_int(current_payload.get("health_score"))
    prev_score = _safe_int((previous_payload or {}).get("health_score"))

    buckets = [("regressions", "Worse", "b-FAIL"),
               ("improvements", "Better", "b-PASS"),
               ("added", "New since last scan", "b-INFO"),
               ("removed", "No longer reported", "b-UNAVAILABLE")]
    sections: list[str] = []
    total_changes = 0
    for bucket, heading, _colour in buckets:
        rows = comparison.get(bucket, []) or []
        total_changes += len(rows)
        if not rows:
            continue
        trs: list[str] = []
        for entry in rows:
            key, before, after, message = entry
            if before is None:
                before_cell = "—"
            else:
                before_status = esc(str(before))
                before_cell = f'<span class="b-{before_status}">{before_status}</span>'
            after_cell = (esc(str(after)) if after is not None else "—")
            trs.append(
                "<tr>"
                f"<td><code>{esc(str(key))}</code></td>"
                f"<td>{before_cell}</td>"
                f"<td>{after_cell}</td>"
                f"<td>{esc(str(message))}</td>"
                "</tr>")
        sections.append(
            f"<h2>{esc(heading)} ({len(rows)})</h2>"
            "<table><tr><th>Check</th><th>Before</th><th>After</th>"
            "<th>Message</th></tr>" + "".join(trs) + "</table>")
    if not sections:
        sections.append("<h2>No changes</h2><p>Every check kept the same "
                        "status in both scans.</p>")
    prev_score_html = "—" if prev_score is None else str(prev_score)
    cur_score_html = "—" if cur_score is None else str(cur_score)
    body = f"""
<h1>Scan comparison (diff)</h1>
<div class="sub">Host {esc(host)} · previous scan {esc(prev_ts)} · current scan {esc(cur_ts)}</div>
<div class="grid">
  <div class="kv"><div class="k">Health score before</div><div class="v">{esc(prev_score_html)}</div></div>
  <div class="kv"><div class="k">Health score now</div><div class="v"><span class="b-INFO">{cur_score_html}</span></div></div>
  <div class="kv"><div class="k">Changed checks</div><div class="v">{total_changes}</div></div>
</div>
{"".join(sections)}
<footer>Generated by Smart Hardware Diagnostic Scanner</footer>
"""
    return (f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SHDS scan comparison</title>
<style>{_COMPARISON_CSS}</style>
</head>
<body>
{body}
</body>
</html>
""")
