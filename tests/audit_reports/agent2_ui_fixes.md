# Agent 2 — UI & CustomTkinter audit of `ewarecare.py`

**Target:** `E:\Warecare\ewarecare.py` (6,823-line standalone build), audited read-only at
`master`. **Deliverable 2 of 2** (Deliverable 1: `tests/test_standalone_ui.py`, 18 tests).

**Scope reviewed:** vector icon renderer (lines 4159–4437), theme module (lines 110–560),
`ScannerApp` `_build_style` / `_build_layout` (lines 4951–5783), the theme-toggle path
(`_toggle_theme` / `apply_theme` / `_retheme_plain_widgets` / `_retheme_text_tags`,
lines 5803–5910), runtime sections and `on_closing`.

**Standing context for Agent 4:** the standalone was verified semantically identical to
the read-only `smart_hardware_diagnostic_scanner/scanner/` package (AST + normalized
text diff — only logger renames like `log_app`/`log_charts` and `_first_line`→
`_diag_first_line`), and the flattened icons section (4159–4437) is **byte-identical**
to `scanner/icons.py`. Every defect below therefore reproduces in the original package;
fixes should ideally be mirrored there. None of these are seeded mutations — all were
found by code review and confirmed with runtime probes. All findings reproduced
empirically with post-toggle probes on the live app (single Tk root, `update()` pumps).

**Icons summary (clean):** every painter stays inside the 24×24 design grid;
`get_icon`'s validation (unknown name → `KeyError`), clamping (`max(8, int(size))`),
cache (`_BITMAP_CACHE` keyed `(name, size, color)`) and fresh-`CTkImage` return are all
correct. Covered green by 6 delivered tests (`test_get_icon_*`, `test_every_painter_*`).

---

## 1. `tk.LabelFrame` group boxes never re-theme — wrong class name in the swap filter
**Severity: CRITICAL** (user-visible: half of the Reports & Tools tab and the Add-rule
box stay in the other palette after every toggle)

**Location: `ewarecare.py` lines 5806–5807**

```python
    _THEME_SWAP_CLASSES = ("Frame", "Label", "Text", "Canvas",
                           "LabelFrame", "Toplevel")
```

**Root cause:** `_retheme_plain_widgets` (5893) skips any widget whose `winfo_class()`
is not in `_THEME_SWAP_CLASSES`. Tk's class name for `tk.LabelFrame` is **`"Labelframe"`**
(lowercase f), and `ttk.Labelframe` reports `"TLabeframe"` — the listed `"LabelFrame"`
never matches, so the group boxes keep construction-time colours forever. Affected
boxes (constructed at 5732 and 5753 for Processes/Reports; "Add rule" on Automation;
"Thresholds" and "Safe temp-file cleanup" on Reports & Tools — 5695/5710 region):
after toggling dark→light, `Add rule` box probes `bg = '#1C1C28'` (dark BG_PANEL)
where light wants `#FFFFFF`, and `fg = '#00E5FF'` (dark NEON_CYAN).

**Fix (validated by runtime patch simulation: the boxes re-theme in both directions):**

```diff
     _THEME_SWAP_CLASSES = ("Frame", "Label", "Text", "Canvas",
-                           "LabelFrame", "Toplevel")
+                           "Labelframe", "Toplevel")
```

`OLD:` (exact match, lines 5806–5807)
```
    _THEME_SWAP_CLASSES = ("Frame", "Label", "Text", "Canvas",
                           "LabelFrame", "Toplevel")
```
`NEW:`
```
    _THEME_SWAP_CLASSES = ("Frame", "Label", "Text", "Canvas",
                           "Labelframe", "Toplevel")
```

---

## 2. Text carets do not follow the palette (`insertbackground`)
**Severity: MEDIUM** (in light mode the diagnostics caret stays bright `#00E5FF` on
near-white — visible but ugly; the other two lists keep the black system caret that is
near-invisible on the dark `#0E0E14` output background)

**Three sub-sites:**

**2a — the re-theme walk never visits `insertbackground` (lines 5895–5896):**
```python
            for option in ("bg", "foreground", "highlightbackground"):
```
So even `diag_text` (constructed at 5562–5564 with `insertbackground=T.NEON_CYAN`,
lines 5562–5564) keeps the dark accent `#00E5FF` after toggling to light (probe-confirmed).

**2b/2c — `clean_list` (lines 5724–5726) and `bigfiles_list` (lines 5748–5750) are
constructed without any caret colour:**
```python
        self.clean_list = tk.Text(clean_box, height=8, bg=T.BG_OUTPUT,
                                  fg=T.FG_PRIMARY, font=T.FONT_MONO, wrap="word",
                                  state="disabled")
```
Their `insertbackground` reports the system default (black) in **both** modes —
invisible-on-dark in the current default dark UI.

**Fix (all three blocks required; validated by runtime patch simulation):**

`OLD:` (lines 5895–5896)
```
            for option in ("bg", "foreground", "highlightbackground"):
```
`NEW:`
```
            for option in ("bg", "foreground", "highlightbackground",
                           "insertbackground"):
```

`OLD:` (lines 5724–5726)
```
        self.clean_list = tk.Text(clean_box, height=8, bg=T.BG_OUTPUT,
                                  fg=T.FG_PRIMARY, font=T.FONT_MONO, wrap="word",
                                  state="disabled")
```
`NEW:`
```
        self.clean_list = tk.Text(clean_box, height=8, bg=T.BG_OUTPUT,
                                  fg=T.FG_PRIMARY, font=T.FONT_MONO, wrap="word",
                                  insertbackground=T.NEON_CYAN,
                                  state="disabled")
```

`OLD:` (lines 5748–5750)
```
        self.bigfiles_list = tk.Text(big_box, height=10, bg=T.BG_OUTPUT,
                                     fg=T.FG_PRIMARY, font=T.FONT_MONO,
                                     wrap="word", state="disabled")
```
`NEW:`
```
        self.bigfiles_list = tk.Text(big_box, height=10, bg=T.BG_OUTPUT,
                                     fg=T.FG_PRIMARY, font=T.FONT_MONO,
                                     insertbackground=T.NEON_CYAN,
                                     wrap="word", state="disabled")
```

Adding a caret colour that is already a palette value is safe for the value-swap:
`swap.get()` only rewrites recognised colours, and `"insertbackground"` then re-themes
in both directions like `bg`/`foreground` already do (probe-verified).

---

## 3. `ttk.Combobox` ignores the palette (stays clam default `#dcdad5` in both modes)
**Severity: MEDIUM** (two controls sit on the dark sidebar/Reports page and read as
light-grey islands in dark mode; light mode is also off-palette)

**Location: `_build_style`, lines 4951–4981 — it configures `TSpinbox` (4979–4980) and
`TEntry` (4981) but never `TCombobox`.** Comboboxes: sidebar refresh interval
(line 5008) and report scheduling day (line 5768). Probe-confirmed:
`style.lookup("TCombobox", "background") == '#dcdad5'` in dark **and** light.
The drop-down popdown list is a classic `tk.Listbox`, which ttk styles do not reach;
the tk option database is the idiomatic hook.

**Fix (place after the `TEntry` line inside `_build_style`; validated: the style follows
the live palette after every toggle):**

`OLD:` (lines 4981–4983)
```
        style.configure("TEntry", fieldbackground=T.ENTRY_BG, foreground=T.FG_PRIMARY)

    def _build_layout(self) -> None:
```
`NEW:`
```
        style.configure("TEntry", fieldbackground=T.ENTRY_BG, foreground=T.FG_PRIMARY)
        style.configure("TCombobox", fieldbackground=T.ENTRY_BG,
                        background=T.BG_CARD, foreground=T.FG_PRIMARY,
                        arrowcolor=T.FG_PRIMARY)
        # The combobox popdown is a classic tk.Listbox below the style system.
        self.option_add("*TCombobox*Listbox.background", T.BG_OUTPUT)
        self.option_add("*TCombobox*Listbox.foreground", T.FG_PRIMARY)

    def _build_layout(self) -> None:
```

---

## 4. `ttk.Radiobutton` / `ttk.Scrollbar` are unstyled (native grey on the dark UI)
**Severity: LOW** (cosmetic)

- `ttk.Radiobutton` ×2 on the Processes page, lines 5229–5232, parented in a
  `ttk.Frame` (TFrame → `background=T.BG_MAIN`); unconfigured TRadiobutton keeps the
  clam light-grey background in dark mode.
- `ttk.Scrollbar` ×4, lines 5241, 5267, 5322, 5565 — clam's default grey trough/bar
  clashes with the palette on every text/tree column.

**Optional fix** (`_build_style`, independent anchor so it also applies if fix 3 was
already applied or skipped):

`OLD:` (lines 4977–4979)
```
        style.configure("Treeview.Heading", background=T.BG_SIDEBAR,
                        foreground=T.NEON_CYAN, font=T.FONT_SMALL)
        style.configure("TSpinbox", fieldbackground=T.ENTRY_BG, background=T.ENTRY_BG,
```
`NEW:`
```
        style.configure("Treeview.Heading", background=T.BG_SIDEBAR,
                        foreground=T.NEON_CYAN, font=T.FONT_SMALL)
        style.configure("TRadiobutton", background=T.BG_MAIN,
                        foreground=T.FG_PRIMARY)
        style.configure("TScrollbar", background=T.BG_PANEL,
                        troughcolor=T.BG_OUTPUT, bordercolor=T.BORDER)
        style.configure("TSpinbox", fieldbackground=T.ENTRY_BG, background=T.ENTRY_BG,
```
(Scrollbar colours: trough aligns with the Text widgets the four scrollbars mostly
adjoin; review values if the page contexts argue otherwise. Tests deliberately do not
anchor this cosmetically.)

---

## 5. HiDPI icon crispness — cached bitmap lacks widget-scaling headroom
**Severity: MEDIUM — DEFERRED (report-only, no edit block)**

**Evidence:** `_SUPERSAMPLE = 4` (line 4190) already renders each painter at 4×
resolution (4424–4429), but the cached bitmap is downscaled to **exactly
`(size, size)`** (4421, 4430) and handed to `CTkImage(size=(size, size))` (4432).
customtkinter then displays icons at `round(size × widget_scaling)`; on the audit
machine `widget_scaling == 1.25`, so every 20 px nav icon is drawn at 25 px from a
20 px bitmap (lossy Lanczos upscale — probe-confirmed: a 20 px `get_icon` bitmap is
displayed as a 25 px PhotoImage). Strokes also soften at 22 px → 28 px.

**Why deferred, not fixed here:**
1. The original package's tests pin the current contract: `scanner/tests/test_app.py`
   lines 597–604 assert `cget("light_image")` size `(size, size)` and cache-object
   identity, lines 661–669 pin `_BITMAP_CACHE` — passing widget-scaled sizes breaks
   the passing 481-test suite, which Agent 4 must keep green.
2. The icons section is byte-identical to the read-only `scanner/icons.py`; the
   standalone must not drift from the package. This is an upstream change in **both**
   (package first, standalone re-flattened after).
3. The visible result is cosmetic (soft anti-aliased strokes, still legible at 100%
   scaling — completely invisible in single-scale DPI environments).

**Suggested upstream shape (for discussion, NOT machine-applicable):** cache the
bitmap at `ceil(size × widget_scaling)` and let `CTkImage(size=(size, size))` keep
the logical size; `get_icon` is widget-free today, so this needs either an explicit
scaling parameter (with the scaling factor folded into the cache key) or per-root
call-site scaling — a deliberate API decision, not a patch-line edit.

---

## 6. `apply_theme()` with the already-active mode inverts every plain widget
**Severity: MEDIUM** (any future caller — or user script — that calls `apply_theme`
with the current mode flips the whole plain-widget UI until the next real toggle)

**Location: `apply_theme`, lines 5812–5844** — after normalising the mode (5820) it
immediately runs the full pipeline (5821–5823):

```python
        mode = "light" if str(mode).lower() == "light" else "dark"
        T.set_mode(mode)
        self._build_style()
```

**Root cause:** `_retheme_plain_widgets` walks every plain widget and swaps colours
through an unconditional, symmetric first-wins value map (5868–5874: each palette
value ↔ its counterpart) — correct **only** when the mode actually changed. Called
with the already-active mode, it pairs each dark value with its light counterpart:
probe-confirmed, a card label in a dark UI reads `bg == '#FFFFFF'` while the palette
wants `#1C1C28`. Because the map is symmetric, a *second* same-mode call flips back —
the bug is periodic, confusing, and poisons any test/code that assumes idempotence.
`_toggle_theme` (5809–5810) is unaffected (it always alternates).

**Fix (validated empirically: both real toggle directions still re-theme; same-mode
requests return early with status feedback; all 7 `apply_theme` call sites in
`scanner/tests/test_app.py` checked):**

`OLD:` (lines 5820–5822)
```
        mode = "light" if str(mode).lower() == "light" else "dark"
        T.set_mode(mode)
        self._build_style()
```
`NEW:`
```
        mode = "light" if str(mode).lower() == "light" else "dark"
        if T.get_mode() == mode:
            self.set_status(f"Theme: {mode}.")   # same mode = pure no-op
            return
        T.set_mode(mode)
        self._build_style()
```

---

## Test run (delivered state, pre-fix)

`python -m pytest tests/test_standalone_ui.py -q` from `E:\Warecare` → **5 failed,
14 passed in 26.24 s** (tail):

```
FAILED tests/test_standalone_ui.py::test_automation_labelframe_follows_theme
FAILED tests/test_standalone_ui.py::test_reports_labelframes_follow_theme - A...
FAILED tests/test_standalone_ui.py::test_text_widget_caret_follows_theme - As...
FAILED tests/test_standalone_ui.py::test_combobox_uses_theme_palette - Assert...
FAILED tests/test_standalone_ui.py::test_apply_theme_same_mode_is_a_no_op - A...
5 failed, 14 passed in 26.24s
```

Sample anchor evidence from the failures:
- Bug 1 (Reports): "Thresholds' box keeps the dark background in light mode" —
  `bg '#1C1C28'` ≠ light `#FFFFFF`.
- Bug 2: diagnostics caret in light mode is `#00E5FF`, expected `#0077B8`.
- Bug 3: `lookup("TCombobox","background")` = `#dcdad5` while light `BG_CARD` is `#FFFFFF`.
- Bug 6: card label `bg` = `#FFFFFF` while dark `BG_CARD` = `#1C1C28`.

**Pre-fix RED → bug mapping**

| failing test | bug |
|---|---|
| `test_automation_labelframe_follows_theme` | 1 (CRITICAL) |
| `test_reports_labelframes_follow_theme` | 1 (CRITICAL) |
| `test_text_widget_caret_follows_theme` | 2 (MEDIUM, sub-sites 2a+2b+2c) |
| `test_combobox_uses_theme_palette` | 3 (MEDIUM) |
| `test_apply_theme_same_mode_is_a_no_op` (runs **last**, by design) | 6 (MEDIUM) |

Everything else (icons, palette flip, CTk factory re-theme walk, plain-widget/coverage
gate, text tags, charts, leak census, after-loop survival) passes **pre-fix** — those
paths are already correct.

**Ordering note (why the Bug 6 test is last):** pre-fix it fails mid-body and leaves
the symmetric swap in the inverted half of its period-2 cycle (a single later mode call
can only flip between the two inconsistent halves — verified). Running it last confines
the inversion to its own failure; the delivered run above shows all other tests green.

**Post-fix expectation:** with fixes 1–3 + 6 applied, all 18 tests should pass
(fix 4/5 are not test-anchored). Fixes 1, 3, 6 and 2's three blocks were each
validated by runtime patch simulation against the delivered assertions.

**Harness compliance:** one Tk root (the module-scoped `ScannerApp`), `update()` pumps
only, `messagebox` patched for the constructor path, no dialogs/network/temp-files,
runtime ~26 s (< 90 s budget).

## Unverifiable / notes for Agent 4

- The `*TCombobox*Listbox.*` option-database lines (fix 3) cannot be exercised by the
  delivered tests: the popdown `Listbox` only materialises while the dropdown is open,
  which is excluded by the no-interaction constraint. It is the standard tk idiom;
  spot-check visually after applying, or assert via `app.option_get(...)` if allowed.
- Fix 6's `Theme: {mode}.` status feedback was validated via runtime simulation only;
  the suite does not ossify status text because `_apply_snapshot` legitimately
  overwrites the status bar moments later ("Updated hh:mm:ss — host …").
- Bug 5 numbers are environment-dependent (widget scaling); an accepted upstream fix
  must derive scaling per root/call site rather than hard-coding 1.25.
- Icon source of truth: `get_icon`/`clear_cache` region 4404–4437 + `_PAINTERS`
  (4159–4437) are byte-identical to `scanner/icons.py`; coordinate any edit with the
  package and re-verify byte-identity after Agent 4's pass.

---

## Post-audit resolution (Bug 5 — HiDPI — applied)

Deferred no longer; applied in a follow-up commit with the package updated in the
same change so the two icon copies stay byte-identical:

- `scanner/icons.py` and the flattened `ewarecare.py` copy both gain
  `_HI_DPI_SCALE = 1.25`; the cached bitmap is downscaled to
  `round(size * _HI_DPI_SCALE)` while `CTkImage(size=(size, size))` keeps the
  logical size. Tk therefore renders the strokes 1:1 at the Windows-default 125%
  scaling instead of losslessly upscaling a 1x bitmap (on unscaled 100% displays
  the slightly larger bitmap is gently downscaled — still crisp). Chosen over the
  "derive widget scaling per call site" shape noted above because `get_icon` is
  widget-free and the cache key remains `(name, size, color)`.
- Contract tests updated for the new contract: the pinned bitmap-size assertion in
  `scanner/tests/test_app.py::test_bitmaps_are_squares_with_transparent_corners`
  (now `round(20 * _HI_DPI_SCALE)` = (25, 25)), the bitmap-size pins and bbox
  bounds in `tests/test_standalone_ui.py`, and the two monitor-hub colour-sampling
  tests (now sample the bitmap centre, since `getpixel((20, 20))` assumed the
  bitmap equalled the logical size).
- Verification after the change: focused run (`test_app.py` + standalone UI) and
  the **full suite: 545 passed, 36 subtests**; sub-window checks re-ran clean and a
  live launch of `python ewarecare.py` stayed windowed/planned-alive 12 s with
  byte-empty stdout/stderr. The two icon sections were re-verified in sync
  (identical `_HI_DPI_SCALE` reference counts and key lines in both copies).
