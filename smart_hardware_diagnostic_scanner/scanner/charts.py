"""Optional Matplotlib live charts, embedded in Tkinter (degrades gracefully)."""
from __future__ import annotations

import logging

log = logging.getLogger("shds.charts")

try:
    import matplotlib

    matplotlib.use("TkAgg")
    from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
    from matplotlib.figure import Figure

    HAS_MPL = True
except Exception:
    # ImportError (not installed) is the common case, but a broken backend
    # (e.g. TkAgg missing) raises other exceptions at import time. Either way
    # the app must degrade to the placeholder instead of crashing import.
    HAS_MPL = False
    Figure = None  # type: ignore
    FigureCanvasTkAgg = None  # type: ignore


class ChartPanel:
    """Two-line live chart (CPU + RAM). No-op placeholder when Matplotlib is absent."""

    def __init__(self, parent, bg: str = "#232333", fg: str = "#E8E8F0") -> None:
        self.parent = parent
        self.bg = bg
        self.fg = fg
        self.canvas = None
        self.ax = None
        self.fig = None
        self.line_cpu = None
        self.line_mem = None
        if HAS_MPL:
            self.fig = Figure(figsize=(5.5, 2.6), dpi=100, facecolor=bg)
            self.ax = self.fig.add_subplot(111, facecolor=bg)
            self.ax.set_ylim(0, 100)
            self.ax.set_xlabel("samples", color=fg, fontsize=8)
            self.ax.set_ylabel("%", color=fg, fontsize=8)
            self.ax.tick_params(colors=fg, labelsize=7)
            for spine in self.ax.spines.values():
                spine.set_color("#34344A")
            self.ax.grid(True, color="#34344A", linewidth=0.4, alpha=0.7)
            (self.line_cpu,) = self.ax.plot([], [], color="#00E5FF", linewidth=1.6, label="CPU %")
            (self.line_mem,) = self.ax.plot([], [], color="#B266FF", linewidth=1.6, label="RAM %")
            leg = self.ax.legend(facecolor=bg, edgecolor="#34344A", fontsize=7)
            for text in leg.get_texts():
                text.set_color(fg)
            self.fig.tight_layout(pad=0.8)
            self.canvas = FigureCanvasTkAgg(self.fig, master=parent)
            widget = self.canvas.get_tk_widget()
            widget.configure(highlightthickness=0)
            widget.pack(fill="both", expand=True)

    def update(self, cpu_hist: list, mem_hist: list) -> None:
        """Redraw from bounded histories. Safe to call without Matplotlib."""
        if not HAS_MPL or self.canvas is None or self.ax is None:
            return
        try:
            xs = list(range(len(cpu_hist)))
            self.line_cpu.set_data(xs, list(cpu_hist))
            self.line_mem.set_data(xs, list(mem_hist))
            self.ax.set_xlim(0, max(10, len(xs)))
            self.ax.set_ylim(0, 100)
            self.canvas.draw_idle()
        except Exception as exc:
            log.debug("chart update failed: %s", exc)

    def pack_widget(self) -> None:
        if self.canvas is None:
            return
        self.canvas.get_tk_widget().pack(fill="both", expand=True)

    def set_palette(self, bg: str, fg: str, border: str,
                    cpu_colour: str, mem_colour: str) -> None:
        """Re-colour the chart for a theme switch (no-op without Matplotlib)."""
        if not HAS_MPL or self.fig is None or self.ax is None:
            self.bg, self.fg = bg, fg
            return
        self.bg, self.fg = bg, fg
        try:
            self.fig.set_facecolor(bg)
            self.ax.set_facecolor(bg)
            self.ax.tick_params(colors=fg)
            for text in (self.ax.xaxis.label, self.ax.yaxis.label):
                text.set_color(fg)
            for spine in self.ax.spines.values():
                spine.set_color(border)
            for line, colour in ((self.line_cpu, cpu_colour),
                                 (self.line_mem, mem_colour)):
                if line is not None:
                    line.set_color(colour)
            self.ax.grid(True, color=border, linewidth=0.4, alpha=0.7)
            leg = self.ax.get_legend()
            if leg is not None:
                leg.set_facecolor(bg)
                leg.set_edgecolor(border)
                for text in leg.get_texts():
                    text.set_color(fg)
            self.canvas.draw_idle()
        except Exception as exc:
            log.debug("chart re-theme failed: %s", exc)
