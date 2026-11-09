"""VIMU x PPK2 HIL run app.

One START RUN button streams an NPZ recording to the STM32 VIMU and records
the PPK2 on the same host clock, so every D7 pulse (model FALL verdict on the
ESP32) can be mapped back to the NPZ sample that was playing.

    py vimu_ppk2_run_app.py
    py vimu_ppk2_run_app.py --stm32-port COM27 --ppk2-port COM17 --npz <file.npz>

Run sequence (see README.md):
  ARM      STM32 STOP + CLEAR, ring prefilled with 10 s of NPZ data
  REC      PPK2 starts writing the run files
  PRE-ROLL baseline current before any data plays (default 1 s)
  GO       STM32 START; the run clock starts here (t_run = 0)
  RUN      auto refill; every UPDATE poll logs the played sample count
  STOP     at the end of the NPZ range (+ tail) or by the button
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
import traceback
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Optional

import numpy as np

import analyze_run
import ppk2_capture as pc
import ppk_raw
import run_sync as rs
from run_sync import vimu

APP_DIR = Path(__file__).resolve().parent
RATE = rs.RATE_HZ
AUTOSAVE_MIN_S = 60  # shortest auto-save interval; each snapshot re-reads the run files

# Every PPK2 sample is saved as ppk_raw/raw_HHhMMmSSs.npz, one file per minute
# (about 0.2 GB per hour). With RAW_CSV = 1 each file also gets a CSV twin with
# the same rows, Timestamp(ms),Current(uA),D0-D7, to check the NPZ against
# (about 11 GB per hour). Set it to 0 once the NPZ is trusted. --raw-csv 0/1
# overrides it for one start of the app.
RAW_CSV = 1

FILES_ROWS_MAX = 2000   # rows kept in the saved-files list (24 h = 1440 raw files)
FILL_ROWS_MAX = 300     # rows kept in the refill list (one refill every few seconds)

C_APP, C_BG, C_HEADER = vimu.C_APP, vimu.C_BG, vimu.C_HEADER
C_PANEL, C_PANEL_2, C_PANEL_3 = vimu.C_PANEL, vimu.C_PANEL_2, vimu.C_PANEL_3
C_FIELD, C_BORDER, C_BORDER_2 = vimu.C_FIELD, vimu.C_BORDER, vimu.C_BORDER_2
C_TEXT, C_SUB, C_DIM, C_FAINT = vimu.C_TEXT, vimu.C_SUB, vimu.C_DIM, vimu.C_FAINT
C_GREEN, C_YELLOW, C_ORANGE, C_RED = vimu.C_GREEN, vimu.C_YELLOW, vimu.C_ORANGE, vimu.C_RED
C_BLUE, C_PURPLE, C_CYAN = vimu.C_BLUE, vimu.C_PURPLE, vimu.C_CYAN
FONT_UI, FONT_UI_SM, FONT_UI_XS = vimu.FONT_UI, vimu.FONT_UI_SM, vimu.FONT_UI_XS
FONT_UI_BOLD, FONT_TITLE = vimu.FONT_UI_BOLD, vimu.FONT_TITLE
FONT_MONO, FONT_MONO_SM = vimu.FONT_MONO, vimu.FONT_MONO_SM
blend = vimu._blend

ACTIVE_PHASES = ("ARMING", "REC_WAIT", "PREROLL", "RUNNING", "TAIL")
VERDICT_COLORS = {"FP": C_RED, "TP": C_GREEN, "DUP": C_CYAN, "WARMUP": C_DIM,
                  "PREROLL": C_DIM, "GLITCH": C_YELLOW, "IDLE": C_FAINT}


def keep_awake(on: bool) -> None:
    """Stop Windows from sleeping on idle while a run is in progress.
    Lid close, a manual sleep and Windows Update restarts are not covered."""
    try:
        import ctypes
        ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
        ctypes.windll.kernel32.SetThreadExecutionState(
            ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if on else 0))
    except Exception:
        pass


def normalize_port(text: str) -> str:
    """'19', 'com19', ' COM19 ' -> 'COM19'. Anything else is passed through."""
    t = text.strip().replace(" ", "")
    if t.isdigit():
        return f"COM{t}"
    if t[:3].upper() == "COM":
        return "COM" + t[3:]
    return t


class HmsInput(tk.Frame):
    """Hours : minutes : seconds as three spin boxes. Each box takes typed
    digits, the arrows, the Up/Down keys or the mouse wheel."""

    def __init__(self, parent: tk.Widget, hours_max: int = 99) -> None:
        super().__init__(parent, bg=C_PANEL)
        self.hours_max = hours_max
        self._boxes = []
        check = (self.register(lambda p: p == "" or (p.isdigit() and len(p) <= 2)), "%P")
        for i, top in enumerate((hours_max, 59, 59)):
            if i:
                tk.Label(self, text=":", bg=C_PANEL, fg=C_SUB, font=FONT_MONO).pack(side="left")
            var = tk.StringVar(value="00")
            box = tk.Spinbox(
                self, from_=0, to=top, wrap=True, width=3, textvariable=var, format="%02.0f",
                justify="center", font=FONT_MONO, bg=C_FIELD, fg=C_TEXT, insertbackground=C_TEXT,
                buttonbackground=C_PANEL_2, relief="flat", highlightthickness=1,
                highlightbackground=C_BORDER, highlightcolor=C_BLUE, disabledbackground=C_PANEL_2,
                disabledforeground=C_DIM, validate="key", validatecommand=check)
            box.bind("<FocusOut>", lambda _e, k=i: self._clamp(k))
            box.bind("<Return>", lambda _e, k=i: self._clamp(k))
            box.bind("<MouseWheel>", lambda e, b=box: self._wheel(b, e))
            box.pack(side="left")
            self._boxes.append((box, var, top))

    @staticmethod
    def _wheel(box: tk.Spinbox, event) -> str:
        if str(box.cget("state")) == "normal":
            box.invoke("buttonup" if event.delta > 0 else "buttondown")
        return "break"

    def _clamp(self, k: int) -> int:
        _box, var, top = self._boxes[k]
        text = var.get().strip()
        value = min(int(text), top) if text.isdigit() else 0
        var.set(f"{value:02d}")
        return value

    def seconds(self) -> int:
        h, m, s = (self._clamp(k) for k in range(3))
        return h * 3600 + m * 60 + s

    def peek_seconds(self) -> int:
        """Current value without re-writing the boxes (safe while typing)."""
        total = 0
        for (_box, var, top), mult in zip(self._boxes, (3600, 60, 1)):
            text = var.get().strip()
            total += (min(int(text), top) if text.isdigit() else 0) * mult
        return total

    def set_seconds(self, seconds: float) -> None:
        total = max(0, int(round(seconds)))
        h = min(total // 3600, self.hours_max)
        values = (h, (total % 3600) // 60, total % 60)
        for (_box, var, _top), v in zip(self._boxes, values):
            var.set(f"{v:02d}")

    def set_state(self, state: str) -> None:
        for box, _var, _top in self._boxes:
            box.config(state=state)


def fmt_current(ua: Optional[float]) -> str:
    if ua is None or not math.isfinite(ua):
        return "--"
    a = abs(ua)
    if a >= 1000:
        return f"{ua / 1000:.3f} mA"
    if a >= 1:
        return f"{ua:.2f} µA"
    return f"{ua * 1000:.0f} nA"


def raw_stat(raw: dict) -> tuple:
    """Text and colour of the 'Raw 100 kS/s' line in the PPK2 stats."""
    if not raw["npz_on"]:
        return f"stopped: {raw['error'] or 'disk almost full'}", C_RED
    text = f"{raw['files']} files, {raw['npz_bytes'] / 1e9:.2f} GB"
    if raw["csv_on"]:
        text += f" · CSV {raw['csv_bytes'] / 1e9:.1f} GB"
    elif raw["csv_bytes"]:
        text += f" · CSV stopped at {raw['csv_bytes'] / 1e9:.1f} GB"
    else:
        text += " · CSV off"
    if raw["backlog_s"] > 10:
        return f"{text} · {raw['backlog_s']:.0f} s behind", C_YELLOW
    return text, C_TEXT


def logic_states(window) -> str:
    """D0..D7 over the recent live messages: T = toggled, H = always high,
    L = always low, ? = no data."""
    window = [w for w in window if w and w.get("samples")]
    if not window:
        return "????????"
    edges = np.sum([w["edges"] for w in window], axis=0)
    ever_high = 0
    always_high = 0xFF
    for w in window:
        ever_high |= w["or"]
        always_high &= w["and"]
    out = []
    for ch in range(8):
        if edges[ch] > 0:
            out.append("T")
        elif always_high >> ch & 1:
            out.append("H")
        else:
            out.append("L" if not ever_high >> ch & 1 else "T")
    return "".join(out)


def fmt_bytes(n: float) -> str:
    if n >= 1e9:
        return f"{n / 1e9:.2f} GB"
    if n >= 1e6:
        return f"{n / 1e6:.1f} MB"
    return f"{n / 1e3:.0f} kB"


# ── Ring gauge ─────────────────────────────────────────────────────────────

class RingBar(tk.Canvas):
    """Fill of the STM32 ring as a horizontal bar. The dashed line is the
    refill threshold: below it the app sends the STM32 new samples."""

    def __init__(self, parent: tk.Widget, capacity: int, threshold: int) -> None:
        super().__init__(parent, bg=C_PANEL, highlightthickness=0, height=30)
        self.capacity = capacity
        self.threshold = threshold
        self.ring: Optional[int] = None
        self.free: Optional[int] = None
        self.bind("<Configure>", lambda _e: self.redraw())

    def set(self, ring: Optional[int], free: Optional[int]) -> None:
        if (ring, free) != (self.ring, self.free):
            self.ring, self.free = ring, free
            self.redraw()

    def redraw(self) -> None:
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        if w < 60:
            return
        x0, x1, y0, y1 = 1, w - 1, 3, h - 3
        self.create_rectangle(x0, y0, x1, y1, fill=C_FIELD, outline=C_BORDER)
        if self.ring is not None and self.ring > 0:
            frac = min(self.ring / self.capacity, 1.0)
            color = C_YELLOW if self.ring < self.threshold else C_GREEN
            self.create_rectangle(x0, y0, x0 + (x1 - x0) * frac, y1, outline="",
                                  fill=blend(color, C_FIELD, 0.55))
        xt = x0 + (x1 - x0) * self.threshold / self.capacity
        self.create_line(xt, y0 - 2, xt, y1 + 2, fill=C_SUB, dash=(2, 2))
        if self.ring is None:
            text, color = "no ring count from the STM32", C_DIM
        else:
            text = f"{self.ring} / {self.capacity} samples  ·  {self.ring / RATE:.1f} s"
            if self.free is not None:
                text += f"  ·  free {self.free}"
            color = C_RED if self.ring == 0 else C_TEXT
        self.create_text(x0 + 8, h / 2, text=text, anchor="w", fill=color, font=FONT_MONO_SM)


# ── Run state ──────────────────────────────────────────────────────────────

@dataclass
class RunState:
    run_dir: Path
    start_idx: int
    end_idx: int
    session: dict
    events_file: object = None
    events_writer: object = None
    log_file: object = None
    t_go: Optional[float] = None
    rec_requested: bool = False
    rec_started: bool = False
    stm_stopped: bool = False
    ppk_stopped: bool = False
    status: str = ""
    discard: bool = False
    detected: set = field(default_factory=set)
    counts: dict = field(default_factory=dict)
    loss_logged_at: float = 0.0
    last_ckpt: float = 0.0          # t_run of the last auto-save snapshot
    ckpt_count: int = 0
    last_ckpt_label: str = ""

    @property
    def length(self) -> int:
        return self.end_idx - self.start_idx


# ── App ────────────────────────────────────────────────────────────────────

class RunApp:
    def __init__(self, root: tk.Tk, args: argparse.Namespace) -> None:
        self.root = root
        self.args = args
        self.runs_dir = Path(args.runs_dir)
        self.sq: queue.Queue = queue.Queue()
        self.lq: queue.Queue = queue.Queue()
        self.bgq: queue.Queue = queue.Queue()

        self.stm: Optional[rs.RunMonitorThread] = None
        self.stm_connected = False
        self.stm_status: Optional[vimu.StatusPayload] = None
        self._stm_user_disconnect = False
        self._stm_resume: Optional[dict] = None

        self.ppk = pc.Ppk2Client({
            "snippet_pre_s": args.snippet_pre, "snippet_post_s": args.snippet_post,
            "max_snippets": args.max_snippets,
            "min_pulse_samples": max(1, int(args.min_pulse_us / 10)),
        })
        self.ppk_connected = False
        self.ppk_link = "ok"
        self.ppk_mv = args.voltage
        self.dut_on = False
        self.dut_on_at: Optional[float] = None
        self.ppk_live: dict = {}
        self.recent_blocks: deque = deque(maxlen=100)   # 10 ms means -> 1 s average
        self.logic_window: deque = deque(maxlen=40)     # 10 s of live messages
        self.rec_stats: Optional[dict] = None
        self._idle_pulse_logged_at = 0.0

        self.npz: Optional[rs.NpzRunData] = None
        self.npz_loading = False
        self.phase = "IDLE"
        self.run: Optional[RunState] = None
        self.last_run_dir: Optional[Path] = None
        self.clock = rs.PlayClock()
        self.last_sync: Optional[rs.SyncPoint] = None
        self._quit_pending = False
        self._last_ui = 0.0
        self._last_disk = 0.0
        self.ring_stats: dict = {}
        self._reset_ring_stats()
        self._last_pulse_text = "--"
        self._lock_state: Optional[str] = None
        self._ckpt_proc: Optional[subprocess.Popen] = None
        self._ckpt_run: Optional[RunState] = None
        self._ckpt_label = ""

        self._build_ui()
        self._refresh_ports()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        if args.npz:
            self._load_npz(args.npz)
        self._tick()

    # ── UI construction ────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        self.root.title("VIMU × PPK2 HIL Run")
        self.root.configure(bg=C_APP)
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        small = sw < 1480 or sh < 1000
        if small:
            # Laptop screens: fill the screen instead of opening partly off it.
            self.root.geometry(f"{min(1440, sw - 20)}x{min(920, sh - 80)}+0+0")
            try:
                self.root.state("zoomed")
            except tk.TclError:
                pass
        else:
            self.root.geometry("1440x920")
        self.root.minsize(1150, 600)

        header = tk.Frame(self.root, bg=C_HEADER)
        header.pack(fill="x")
        tk.Label(header, text="VIMU × PPK2  ·  HIL Run", bg=C_HEADER, fg=C_TEXT,
                 font=FONT_TITLE).pack(side="left", padx=16, pady=10)
        self.phase_var = tk.StringVar(value="IDLE")
        self.phase_lbl = tk.Label(header, textvariable=self.phase_var, bg=C_HEADER,
                                  fg=C_DIM, font=("Segoe UI Semibold", 13))
        self.phase_lbl.pack(side="right", padx=16)

        body = tk.Frame(self.root, bg=C_APP)
        body.pack(fill="both", expand=True, padx=10, pady=(6, 0))

        self._build_connections(body)
        self._build_runbar(body)

        # Saved files / ring buffer / stats above, D7 table/log below, with a
        # draggable divider.
        panes = tk.PanedWindow(body, orient="vertical", bg=C_BORDER, bd=0, sashwidth=7,
                               sashrelief="flat", showhandle=False, opaqueresize=True,
                               sashcursor="sb_v_double_arrow")
        self._panes = panes
        panes.pack(fill="both", expand=True, pady=(6, 0))
        middle = tk.Frame(panes, bg=C_APP)
        middle.columnconfigure(0, weight=1)
        middle.rowconfigure(0, weight=1)
        self._build_files_ring(middle)
        self._build_stats(middle)
        bottom = tk.Frame(panes, bg=C_APP)
        self._build_table(bottom)
        self._build_log(bottom)
        panes.add(middle, minsize=160, stretch="always")
        panes.add(bottom, minsize=90, height=170 if small else 230, stretch="never")

        status = tk.Frame(self.root, bg=C_HEADER)
        status.pack(fill="x", side="bottom")
        self.status_var = tk.StringVar(value=f"Runs folder: {self.runs_dir}")
        tk.Label(status, textvariable=self.status_var, bg=C_HEADER, fg=C_DIM,
                 font=FONT_UI_XS, anchor="w").pack(side="left", padx=10, pady=3)

    def _card(self, parent: tk.Widget, title: str) -> tk.Frame:
        outer = tk.Frame(parent, bg=C_PANEL, highlightthickness=1, highlightbackground=C_BORDER)
        tk.Label(outer, text=title, bg=C_PANEL, fg=C_SUB, font=FONT_UI_XS,
                 anchor="w").pack(fill="x", padx=10, pady=(6, 2))
        inner = tk.Frame(outer, bg=C_PANEL)
        inner.pack(fill="both", expand=True, padx=10, pady=(0, 8))
        return outer, inner

    def _btn(self, parent: tk.Widget, text: str, cmd, color: str, **kw) -> tk.Button:
        return tk.Button(parent, text=text, command=cmd,
                         bg=blend(color, C_PANEL, 0.78), fg=color,
                         activebackground=blend(color, C_PANEL, 0.6), activeforeground=C_TEXT,
                         disabledforeground=C_FAINT, relief="flat", bd=0, padx=12, pady=4,
                         font=FONT_UI_BOLD, cursor="hand2", highlightthickness=1,
                         highlightbackground=blend(color, C_PANEL, 0.5), **kw)

    def _entry(self, parent: tk.Widget, var: tk.StringVar, width: int) -> tk.Entry:
        return tk.Entry(parent, textvariable=var, width=width, bg=C_FIELD, fg=C_TEXT,
                        insertbackground=C_TEXT, relief="flat", font=FONT_MONO,
                        highlightthickness=1, highlightbackground=C_BORDER,
                        disabledbackground=C_PANEL_2, disabledforeground=C_DIM)

    def _label(self, parent, text="", fg=C_SUB, font=FONT_UI_SM, **kw) -> tk.Label:
        return tk.Label(parent, text=text, bg=C_PANEL, fg=fg, font=font, **kw)

    def _build_connections(self, parent: tk.Widget) -> None:
        row = tk.Frame(parent, bg=C_APP)
        row.pack(fill="x")
        for i in range(3):
            row.columnconfigure(i, weight=1, uniform="conn")

        # STM32
        card, inner = self._card(row, "STM32 VIMU  (USART1)")
        card.grid(row=0, column=0, sticky="nsew", padx=(0, 5))
        self.stm_port_var = tk.StringVar(value=self.args.stm32_port)
        # Editable: pick from the list or type e.g. "19" / "COM19".
        self.stm_port_cb = ttk.Combobox(inner, textvariable=self.stm_port_var, width=10,
                                        style="Vimu.TCombobox", postcommand=self._refresh_ports)
        self.stm_port_cb.grid(row=0, column=0, padx=(0, 6))
        self._btn(inner, "↻", self._refresh_ports, C_SUB).grid(row=0, column=1, padx=(0, 6))
        self.stm_btn = self._btn(inner, "Connect", self._stm_toggle, C_BLUE, width=10)
        self.stm_btn.grid(row=0, column=2)
        self.stm_info_var = tk.StringVar(value="disconnected")
        self._label(inner, textvariable=self.stm_info_var, fg=C_DIM, font=FONT_MONO_SM,
                    anchor="w").grid(row=1, column=0, columnspan=4, sticky="w", pady=(6, 0))

        # PPK2
        card, inner = self._card(row, "PPK2  (source meter, D7 = model FALL)")
        card.grid(row=0, column=1, sticky="nsew", padx=5)
        self.ppk_port_var = tk.StringVar(value=self.args.ppk2_port or "")
        self.ppk_port_cb = ttk.Combobox(inner, textvariable=self.ppk_port_var, width=10,
                                        style="Vimu.TCombobox", postcommand=self._refresh_ports)
        self.ppk_port_cb.grid(row=0, column=0, padx=(0, 6))
        self.mv_var = tk.StringVar(value=str(self.args.voltage))
        self.mv_entry = self._entry(inner, self.mv_var, 6)
        self.mv_entry.grid(row=0, column=1)
        self._label(inner, "mV").grid(row=0, column=2, padx=(2, 8))
        self.ppk_btn = self._btn(inner, "Connect", self._ppk_toggle, C_BLUE, width=10)
        self.ppk_btn.grid(row=0, column=3, padx=(0, 6))
        self.dut_btn = self._btn(inner, "DUT OFF", self._dut_toggle, C_DIM, width=9)
        self.dut_btn.grid(row=0, column=4)
        self.ppk_info_var = tk.StringVar(value="disconnected")
        self._label(inner, textvariable=self.ppk_info_var, fg=C_DIM, font=FONT_MONO_SM,
                    anchor="w").grid(row=1, column=0, columnspan=5, sticky="w", pady=(6, 0))

        # NPZ
        card, inner = self._card(row, "NPZ recording")
        card.grid(row=0, column=2, sticky="nsew", padx=(5, 0))
        self.npz_btn = self._btn(inner, "Load NPZ…", self._pick_npz, C_PURPLE)
        self.npz_btn.grid(row=0, column=0, rowspan=2, padx=(0, 14), sticky="w")
        self._label(inner, "Start").grid(row=0, column=1, sticky="w")
        self.start_in = HmsInput(inner)
        self.start_in.grid(row=0, column=2, sticky="w", padx=(8, 0), pady=(0, 3))
        self._label(inner, "Duration").grid(row=1, column=1, sticky="w")
        self.dur_in = HmsInput(inner)
        self.dur_in.set_seconds(24 * 3600)
        self.dur_in.grid(row=1, column=2, sticky="w", padx=(8, 0))
        self._label(inner, "h : m : s", fg=C_DIM, font=FONT_UI_XS).grid(
            row=0, column=3, rowspan=2, sticky="w", padx=(8, 0))
        self.npz_info_var = tk.StringVar(value="no file loaded")
        self._label(inner, textvariable=self.npz_info_var, fg=C_DIM, font=FONT_MONO_SM,
                    anchor="w").grid(row=2, column=0, columnspan=4, sticky="w", pady=(6, 0))

    def _build_runbar(self, parent: tk.Widget) -> None:
        bar = tk.Frame(parent, bg=C_PANEL, highlightthickness=1, highlightbackground=C_BORDER)
        bar.pack(fill="x", pady=(6, 0))
        self.run_btn = tk.Button(bar, text="▶  START RUN", command=self._start_or_stop,
                                 font=("Segoe UI Semibold", 13), relief="flat", bd=0,
                                 padx=26, pady=8, cursor="hand2", width=16)
        self.run_btn.pack(side="left", padx=10, pady=8)
        self.ready_var = tk.StringVar()
        tk.Label(bar, textvariable=self.ready_var, bg=C_PANEL, fg=C_SUB,
                 font=FONT_MONO, anchor="w").pack(side="left", padx=10)

        # Auto-save: every interval, analyse the data so far into summary.*
        # and checkpoints/<time>_summary.*, without interrupting the run.
        auto = tk.Frame(bar, bg=C_PANEL)
        auto.pack(side="left", padx=(18, 0))
        autosave_s = getattr(self.args, "autosave_s", 0)
        self.autosave_var = tk.BooleanVar(value=autosave_s > 0)
        tk.Checkbutton(auto, text="Auto-save every", variable=self.autosave_var,
                       bg=C_PANEL, fg=C_SUB, activebackground=C_PANEL, activeforeground=C_TEXT,
                       selectcolor=C_FIELD, font=FONT_UI_SM, bd=0, highlightthickness=0,
                       cursor="hand2").pack(side="left")
        self.autosave_in = HmsInput(auto)
        self.autosave_in.set_seconds(autosave_s or 3600)
        self.autosave_in.pack(side="left", padx=(6, 0))
        self._btn(bar, "Open run folder", self._open_folder, C_SUB).pack(side="right", padx=10)

    def _build_files_ring(self, parent: tk.Widget) -> None:
        """Two columns where the current plot used to be: the files saved so
        far in this run, and the STM32 ring buffer."""
        area = tk.Frame(parent, bg=C_APP)
        area.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        area.columnconfigure(0, weight=3, minsize=360)
        area.columnconfigure(1, weight=2, minsize=320)
        area.rowconfigure(0, weight=1)
        self._build_files(area)
        self._build_ring(area)

    def _table(self, parent: tk.Widget, cols: list, stretch: str, height: int) -> ttk.Treeview:
        """A Treeview with its scrollbar, packed to fill the rest of parent."""
        holder = tk.Frame(parent, bg=C_PANEL)
        holder.pack(fill="both", expand=True, padx=(8, 6), pady=(0, 8))
        table = ttk.Treeview(holder, columns=[c[0] for c in cols], show="headings",
                             style="Run.Treeview", height=height)
        for key, title, width in cols:
            table.heading(key, text=title, anchor="w")
            table.column(key, width=width, minwidth=40, anchor="w", stretch=key == stretch)
        sb = ttk.Scrollbar(holder, orient="vertical", command=table.yview)
        table.configure(yscrollcommand=sb.set)
        table.pack(side="left", fill="both", expand=True)
        sb.pack(side="left", fill="y")
        return table

    def _build_files(self, parent: tk.Widget) -> None:
        frame = tk.Frame(parent, bg=C_PANEL, highlightthickness=1, highlightbackground=C_BORDER)
        frame.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        self.files_title_var = tk.StringVar(value="Saved files")
        tk.Label(frame, textvariable=self.files_title_var, bg=C_PANEL, fg=C_SUB, font=FONT_UI_XS,
                 anchor="w").pack(fill="x", padx=10, pady=(6, 2))
        self.files_table = self._table(frame, [
            ("saved", "Saved at", 72), ("file", "File", 190), ("data", "Data (PPK2 time)", 150),
            ("npz", "NPZ", 66), ("csv", "CSV", 70)], stretch="file", height=6)
        for tag, color in (("raw", C_TEXT), ("ckpt", C_CYAN), ("final", C_GREEN)):
            self.files_table.tag_configure(tag, foreground=color)

    def _build_ring(self, parent: tk.Widget) -> None:
        frame = tk.Frame(parent, bg=C_PANEL, highlightthickness=1, highlightbackground=C_BORDER)
        frame.grid(row=0, column=1, sticky="nsew")
        cap, thr = vimu.RING_CAPACITY, self.args.refill_threshold * RATE
        tk.Label(frame, text=f"VIMU ring buffer  ·  {cap} samples = {cap / RATE:g} s  ·  "
                             f"refill below {self.args.refill_threshold} s (dashed)",
                 bg=C_PANEL, fg=C_SUB, font=FONT_UI_XS, anchor="w").pack(fill="x", padx=10, pady=(6, 2))
        self.ring_bar = RingBar(frame, cap, thr)
        self.ring_bar.pack(fill="x", padx=10, pady=(0, 4))
        grid = tk.Frame(frame, bg=C_PANEL)
        grid.pack(fill="x", padx=10, pady=(0, 6))
        self.ring_vars: dict = {}
        self.ring_lbls: dict = {}
        for r, (key, label) in enumerate((("state", "State"), ("flags", "Flags"),
                                          ("sent", "NPZ samples"), ("fills", "Refills"),
                                          ("min", "Lowest ring"), ("under", "Underruns"))):
            tk.Label(grid, text=label, bg=C_PANEL, fg=C_DIM, font=FONT_UI_SM,
                     anchor="w").grid(row=r, column=0, sticky="w", padx=(0, 10))
            var = tk.StringVar(value="--")
            lbl = tk.Label(grid, textvariable=var, bg=C_PANEL, fg=C_DIM, font=FONT_MONO_SM, anchor="w")
            lbl.grid(row=r, column=1, sticky="w")
            self.ring_vars[key] = var
            self.ring_lbls[key] = lbl
        tk.Label(frame, text="Refills (newest first)", bg=C_PANEL, fg=C_SUB, font=FONT_UI_XS,
                 anchor="w").pack(fill="x", padx=10, pady=(0, 2))
        self.fill_table = self._table(frame, [
            ("t", "Time", 72), ("kind", "Kind", 62), ("before", "Ring before", 96),
            ("sent", "Sent", 70), ("cursor", "NPZ cursor", 90)], stretch="cursor", height=4)
        for tag, color in (("refill", C_TEXT), ("prefill", C_CYAN), ("short", C_YELLOW)):
            self.fill_table.tag_configure(tag, foreground=color)

    def _build_stats(self, parent: tk.Widget) -> None:
        # Scrollable: on a short screen the panel is cut, so its content sits
        # in a canvas with a scrollbar that only shows when needed.
        outer = tk.Frame(parent, bg=C_PANEL, highlightthickness=1, highlightbackground=C_BORDER,
                         width=400)
        outer.grid(row=0, column=1, sticky="nsew")
        outer.grid_propagate(False)
        outer.rowconfigure(0, weight=1)
        outer.columnconfigure(0, weight=1)
        canvas = tk.Canvas(outer, bg=C_PANEL, highlightthickness=0, bd=0, yscrollincrement=20)
        canvas.grid(row=0, column=0, sticky="nsew")
        bar = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=bar.set)
        frame = tk.Frame(canvas, bg=C_PANEL)
        win = canvas.create_window(0, 0, window=frame, anchor="nw")

        def update_region(_event=None) -> None:
            canvas.configure(scrollregion=(0, 0, frame.winfo_reqwidth(), frame.winfo_reqheight()))
            if frame.winfo_reqheight() > canvas.winfo_height() + 1:
                bar.grid(row=0, column=1, sticky="ns")
            else:
                bar.grid_remove()
                canvas.yview_moveto(0)

        def on_canvas(event) -> None:
            canvas.itemconfigure(win, width=event.width)
            update_region()

        frame.bind("<Configure>", update_region)
        canvas.bind("<Configure>", on_canvas)
        self._stats_canvas = canvas
        self._stats_bar = bar
        self.root.bind_all("<MouseWheel>", self._on_wheel, add="+")

        self.stat_vars: dict = {}
        self.stat_lbls: dict = {}
        sections = [
            ("RUN", [("elapsed", "Elapsed"), ("scenario", "NPZ time"), ("progress", "Progress"),
                     ("eta", "Remaining"), ("activity", "Activity"), ("autosave", "Auto-save")]),
            ("SYNC", [("stm", "STM32"), ("played", "Played"), ("drift", "Drift vs 50 Hz"),
                      ("rtt", "UPDATE RTT")]),
            ("PPK2", [("live", "Live (1 s)"), ("avg", "Run average"), ("charge", "Charge"),
                      ("rate", "Stream"), ("loss", "Sample loss"), ("raw", "Raw 100 kS/s")]),
            ("D7", [("pulses", "Pulses"), ("fp", "False alarms"), ("tp", "Falls hit"),
                    ("last", "Last pulse")]),
        ]
        r = 0
        for title, items in sections:
            tk.Label(frame, text=title, bg=C_PANEL, fg=C_BLUE, font=FONT_UI_XS,
                     anchor="w").grid(row=r, column=0, columnspan=2, sticky="w",
                                      padx=12, pady=(6 if r else 4, 0))
            r += 1
            for key, label in items:
                tk.Label(frame, text=label, bg=C_PANEL, fg=C_DIM, font=FONT_UI_SM,
                         anchor="w").grid(row=r, column=0, sticky="w", padx=(16, 8))
                var = tk.StringVar(value="--")
                lbl = tk.Label(frame, textvariable=var, bg=C_PANEL, fg=C_TEXT, font=FONT_MONO,
                               anchor="w")
                lbl.grid(row=r, column=1, sticky="w")
                self.stat_vars[key] = var
                self.stat_lbls[key] = lbl
                r += 1
        frame.columnconfigure(1, weight=1)

    def _on_wheel(self, event) -> None:
        """Mouse wheel anywhere over the stats panel scrolls it. The h:m:s
        boxes handle their own wheel and stop it before it reaches here."""
        if not self._stats_bar.winfo_ismapped():
            return
        try:
            w = self.root.winfo_containing(event.x_root, event.y_root)
        except (KeyError, tk.TclError):  # e.g. over an open combobox list
            return
        while w is not None:
            if w is self._stats_canvas:
                self._stats_canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")
                return
            w = w.master

    def _build_table(self, parent: tk.Widget) -> None:
        frame = tk.Frame(parent, bg=C_PANEL, highlightthickness=1, highlightbackground=C_BORDER)
        frame.pack(side="left", fill="both", expand=True, padx=(0, 6))
        tk.Label(frame, text="D7 pulses (live mapping; final values in d7_events.csv)",
                 bg=C_PANEL, fg=C_SUB, font=FONT_UI_XS, anchor="w").pack(fill="x", padx=10, pady=(6, 2))
        cols = [("no", "#", 50), ("run", "Run time", 90), ("scen", "NPZ time", 90),
                ("idx", "NPZ idx", 80), ("act", "Activity", 220), ("verdict", "Verdict", 70),
                ("w", "Width", 60)]
        self.table = ttk.Treeview(frame, columns=[c[0] for c in cols], show="headings",
                                  style="Run.Treeview", height=7)
        for key, title, width in cols:
            self.table.heading(key, text=title)
            self.table.column(key, width=width, anchor="w", stretch=key == "act")
        for verdict, color in VERDICT_COLORS.items():
            self.table.tag_configure(verdict, foreground=color)
        sb = ttk.Scrollbar(frame, orient="vertical", command=self.table.yview)
        self.table.configure(yscrollcommand=sb.set)
        self.table.pack(side="left", fill="both", expand=True, padx=(8, 0), pady=(0, 8))
        sb.pack(side="left", fill="y", pady=(0, 8), padx=(0, 6))

    def _build_log(self, parent: tk.Widget) -> None:
        frame = tk.Frame(parent, bg=C_PANEL, highlightthickness=1, highlightbackground=C_BORDER)
        frame.pack(side="left", fill="both", expand=True)
        tk.Label(frame, text="Log", bg=C_PANEL, fg=C_SUB, font=FONT_UI_XS,
                 anchor="w").pack(fill="x", padx=10, pady=(6, 2))
        self.log = tk.Text(frame, bg=C_FIELD, fg=C_SUB, font=FONT_MONO_SM, relief="flat",
                           wrap="none", height=10, state="disabled", insertbackground=C_TEXT)
        self.log.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        for tag, color in (("warn", C_YELLOW), ("error", C_RED), ("run", C_CYAN),
                           ("ppk", C_PURPLE), ("time", C_DIM), ("ok", C_GREEN)):
            self.log.tag_configure(tag, foreground=color)

    # ── Logging ────────────────────────────────────────────────────────────

    def _log(self, msg: str, tag: str = "", screen: bool = True) -> None:
        if not screen:
            if self.run is not None and self.run.log_file is not None:
                try:
                    self.run.log_file.write(f"{datetime.now().isoformat(timespec='milliseconds')}  {msg}\n")
                except Exception:
                    pass
            return
        if not tag:
            up = msg[:12].upper()
            if "[ERROR" in up:
                tag = "error"
            elif "[WARN" in up:
                tag = "warn"
            elif "[RUN" in up or "[APP" in up or "[STM32" in up:
                tag = "run"
            elif "[PPK2" in up:
                tag = "ppk"
        stamp = time.strftime("%H:%M:%S")
        self.log.config(state="normal")
        self.log.insert("end", stamp + "  ", "time")
        self.log.insert("end", msg + "\n", tag)
        lines = int(self.log.index("end-1c").split(".")[0])
        if lines > 1000:
            self.log.delete("1.0", f"{lines - 1000}.0")
        self.log.see("end")
        self.log.config(state="disabled")
        if self.run is not None and self.run.log_file is not None:
            try:
                self.run.log_file.write(f"{datetime.now().isoformat(timespec='milliseconds')}  {msg}\n")
                self.run.log_file.flush()
            except Exception:
                pass

    def _event(self, source: str, kind: str, detail: str = "", t: Optional[float] = None,
               log: bool = True) -> None:
        t = time.perf_counter() if t is None else t
        if log:
            tag = "warn" if kind in ("warn", "ring_underrun", "playback_stall", "played_jump",
                                     "stm32_not_running", "uart_lost", "sample_loss",
                                     "link_lost") else ("error" if kind == "error" else "")
            self._log(f"[{source.upper()}] {kind}: {detail}" if detail else f"[{source.upper()}] {kind}", tag)
        run = self.run
        if run is not None and run.events_writer is not None:
            t_run = f"{t - run.t_go:.3f}" if run.t_go is not None else ""
            run.events_writer.writerow([f"{t:.6f}", t_run, datetime.now().isoformat(timespec="seconds"),
                                        source, kind, detail])
            run.events_file.flush()

    # ── Ports and connections ──────────────────────────────────────────────

    def _refresh_ports(self) -> None:
        try:
            from serial.tools import list_ports
            ports = sorted(p.device for p in list_ports.comports())
        except Exception:
            ports = []
        ppk_ports = pc.list_ppk2_ports()
        self.stm_port_cb["values"] = [p for p in ports if p not in ppk_ports] + ppk_ports
        self.ppk_port_cb["values"] = ppk_ports + [p for p in ports if p not in ppk_ports]
        if not self.ppk_port_var.get() and ppk_ports:
            self.ppk_port_var.set(ppk_ports[-1])

    def _stm_toggle(self) -> None:
        if self.stm is not None:
            if self.phase in ACTIVE_PHASES:
                messagebox.showwarning("Run in progress", "Stop the run before disconnecting.")
                return
            self._stm_user_disconnect = True
            self.stm.request_stop()
            self.stm_btn.config(text="Connect", state="disabled")
            return
        self._stm_start_thread()

    def _stm_start_thread(self) -> None:
        port = normalize_port(self.stm_port_var.get())
        if not port:
            return
        self.stm_port_var.set(port)
        self.stm = rs.RunMonitorThread(port, self.args.baud, self.args.poll, self.sq, self.lq,
                                       refill_threshold_s=self.args.refill_threshold)
        if self.npz is not None:
            self.stm.set_data_source(self.npz.samples, self.npz.fall_ids)
        self.stm.start()
        self.stm_btn.config(text="Disconnect")
        self.stm_info_var.set(f"connecting to {port}…")

    def _on_stm_lost(self) -> None:
        thread = self.stm
        self.stm = None
        self.stm_connected = False
        self.stm_btn.config(text="Connect", state="normal")
        if self._stm_user_disconnect:
            self._stm_user_disconnect = False
            self.stm_info_var.set("disconnected")
            return
        if self.run is not None and self.phase in ("RUNNING", "TAIL"):
            if self._stm_resume is None and thread is not None:
                self._stm_resume = thread.export_run_state()
                self._event("app", "uart_lost", "STM32 UART dropped, retrying every 3 s")
            self.stm_info_var.set("link lost, reconnecting…")
            self.root.after(3000, self._stm_reconnect_for_run)
        elif self.run is not None and self.phase in ("ARMING", "REC_WAIT", "PREROLL"):
            self._event("app", "error", "STM32 UART dropped before GO")
            self._stop_run("error")
        else:
            self.stm_info_var.set("disconnected")

    def _stm_reconnect_for_run(self) -> None:
        if self._stm_resume is None or self.stm is not None or self.phase not in ("RUNNING", "TAIL"):
            return
        self._stm_start_thread()

    def _ppk_toggle(self) -> None:
        if self.ppk_connected or self.ppk_link == "reconnecting":
            if self.phase in ACTIVE_PHASES:
                messagebox.showwarning("Run in progress", "Stop the run before disconnecting.")
                return
            self.ppk.send("disconnect")
            return
        try:
            mv = int(self.mv_var.get())
        except ValueError:
            messagebox.showerror("PPK2", "Voltage must be a whole number of mV")
            return
        port = normalize_port(self.ppk_port_var.get())
        if not port:
            messagebox.showerror("PPK2", "Select or type the PPK2 port (COM17 here)")
            return
        self.ppk_port_var.set(port)
        self.ppk_info_var.set(f"connecting to {port}…")
        self.ppk_mv = mv
        self.ppk.send("connect", port, mv)

    def _dut_toggle(self) -> None:
        if not self.ppk_connected:
            return
        if self.dut_on and self.phase in ACTIVE_PHASES:
            if not messagebox.askyesno("DUT power", "A run is in progress. Turn the ESP32 off anyway?"):
                return
        self.ppk.send("dut", not self.dut_on)

    # ── NPZ ────────────────────────────────────────────────────────────────

    def _pick_npz(self) -> None:
        path = filedialog.askopenfilename(
            title="Load NPZ recording", initialdir=str(rs.TOOL_DIR),
            filetypes=[("NumPy archive", "*.npz"), ("All files", "*.*")])
        if path:
            self._load_npz(path)

    def _load_npz(self, path: str) -> None:
        if self.npz_loading:
            return
        self.npz_loading = True
        self.npz_info_var.set("loading…")

        def work():
            try:
                self.bgq.put(("npz", rs.load_npz_run_data(path)))
            except Exception as exc:
                self.bgq.put(("npz_error", f"{path}: {exc}"))

        threading.Thread(target=work, daemon=True, name="npz-load").start()

    def _on_npz_loaded(self, data: rs.NpzRunData) -> None:
        self.npz_loading = False
        self.npz = data
        hours = data.n / RATE / 3600
        name = Path(data.path).name
        self.npz_info_var.set(f"{name}  ·  {data.n:,} samples  ·  {hours:.2f} h  ·  "
                              f"{len(data.episodes)} falls")
        self.start_in.set_seconds(0)
        self.dur_in.set_seconds(data.n / RATE)
        if self.stm is not None:
            self.stm.set_data_source(data.samples, data.fall_ids)
        self._log(f"[APP] NPZ loaded: {name}, {data.n:,} samples at {RATE} Hz ({hours:.2f} h), "
                  f"{len(data.episodes)} labelled falls")
        if not data.episodes:
            self._log("[APP] This file has no labelled falls: every D7 pulse will count as a "
                      "false alarm", "warn")

    # ── Run control ────────────────────────────────────────────────────────

    def _ready_items(self):
        return [("STM32", self.stm_connected), ("PPK2", self.ppk_connected),
                ("DUT", self.dut_on), ("NPZ", self.npz is not None)]

    def _start_or_stop(self) -> None:
        if self.phase in ACTIVE_PHASES:
            choice = self._ask_stop_choice(
                "Stop run", "Stop the run now?\n\n"
                "Save: keep the files and analyse them.\n"
                f"Discard: delete this run's folder ({self.run.run_dir.name}).")
            if choice == "cancel" or (choice == "discard" and not self._confirm_discard()):
                return
            self._stop_run("stopped", discard=choice == "discard")
        elif self.phase in ("IDLE", "DONE"):
            self._start_run()

    def _ask_stop_choice(self, title: str, message: str) -> str:
        """Modal dialog: returns 'save', 'discard' or 'cancel'."""
        dlg = tk.Toplevel(self.root)
        dlg.title(title)
        dlg.configure(bg=C_PANEL)
        dlg.transient(self.root)
        dlg.resizable(False, False)
        result = {"choice": "cancel"}

        def choose(value: str) -> None:
            result["choice"] = value
            dlg.destroy()

        tk.Label(dlg, text=message, bg=C_PANEL, fg=C_TEXT, font=FONT_UI, justify="left",
                 wraplength=460).pack(padx=22, pady=(18, 14), anchor="w")
        row = tk.Frame(dlg, bg=C_PANEL)
        row.pack(fill="x", padx=16, pady=(0, 16))
        self._btn(row, "Stop & save", lambda: choose("save"), C_GREEN).pack(side="left", padx=4)
        self._btn(row, "Stop & discard", lambda: choose("discard"), C_RED).pack(side="left", padx=4)
        self._btn(row, "Keep running", lambda: choose("cancel"), C_SUB).pack(side="right", padx=4)
        dlg.protocol("WM_DELETE_WINDOW", lambda: choose("cancel"))
        dlg.bind("<Escape>", lambda _e: choose("cancel"))
        dlg.update_idletasks()
        x = self.root.winfo_rootx() + (self.root.winfo_width() - dlg.winfo_width()) // 2
        y = self.root.winfo_rooty() + (self.root.winfo_height() - dlg.winfo_height()) // 3
        dlg.geometry(f"+{max(x, 0)}+{max(y, 0)}")
        dlg.grab_set()
        dlg.focus_set()
        self.root.wait_window(dlg)
        return result["choice"]

    def _confirm_discard(self) -> bool:
        """Second question before throwing away more than a few minutes of data."""
        run = self.run
        if run is None or run.t_go is None:
            return True
        elapsed = time.perf_counter() - run.t_go
        if elapsed < 300:
            return True
        return messagebox.askyesno(
            "Discard run", f"Delete {rs.fmt_hms(elapsed)} of recorded data permanently?",
            icon="warning", default="no")

    def _start_run(self) -> None:
        missing = [name for name, ok in self._ready_items() if not ok]
        if missing:
            messagebox.showwarning("Not ready", "Missing: " + ", ".join(missing))
            return
        npz = self.npz
        start_s = self.start_in.seconds()
        dur_s = self.dur_in.seconds()
        start_idx = int(round(start_s * RATE))
        end_idx = min(npz.n, start_idx + int(round(dur_s * RATE)))
        if start_idx >= npz.n or end_idx - start_idx < 10 * RATE:
            messagebox.showerror("Run range", "The run must cover at least 10 s inside the file.")
            return
        if self.dut_on_at is not None and time.perf_counter() - self.dut_on_at < self.args.boot_wait:
            if not messagebox.askyesno(
                    "ESP32 just powered", "DUT power was switched on less than "
                    f"{self.args.boot_wait:g} s ago. The ESP32 may still be booting and "
                    "re-initialising the sensor. Start anyway?"):
                return

        self.runs_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        run_dir = self.runs_dir / stamp
        k = 1
        while run_dir.exists():
            run_dir = self.runs_dir / f"{stamp}_{k}"
            k += 1
        run_dir.mkdir(parents=True)

        a = self.args
        session = {
            "app": "vimu_ppk2_run_app", "format": 1,
            "created": datetime.now().isoformat(timespec="seconds"),
            "status": "arming",
            "npz": {"path": npz.path, "samples": npz.n, "stride": npz.stride,
                    "falls": len(npz.episodes)},
            "run": {"start_idx": start_idx, "end_idx": end_idx,
                    "start": rs.fmt_hms(start_idx / RATE),
                    "duration_s": (end_idx - start_idx) / RATE, "rate_hz": RATE},
            "options": {"preroll_s": a.preroll, "tail_s": a.tail,
                        "match_tolerance_s": a.tolerance, "warmup_s": a.warmup,
                        "refill_threshold_s": a.refill_threshold, "poll_s": a.poll,
                        "snippet_pre_s": a.snippet_pre, "snippet_post_s": a.snippet_post,
                        "max_snippets": a.max_snippets, "min_pulse_us": a.min_pulse_us,
                        "autosave_s": self._autosave_interval() if self.autosave_var.get() else 0,
                        "raw_csv": int(a.raw_csv), "raw_file_s": ppk_raw.FILE_S},
            "stm32": {"port": self.stm_port_var.get(), "baud": a.baud},
            "ppk2": {"port": self.ppk_port_var.get(), "vdd_mv": self.ppk_mv},
            "time_base": "time.perf_counter() seconds on this PC; t_run = t_host - t_go",
        }
        run = RunState(run_dir=run_dir, start_idx=start_idx, end_idx=end_idx, session=session)
        run.counts = {k: 0 for k in ("FP", "TP", "DUP", "WARMUP", "PREROLL", "GLITCH")}
        run.events_file = open(run_dir / "run_events.csv", "w", newline="", encoding="utf-8")
        run.events_writer = csv.writer(run.events_file)
        run.events_writer.writerow(["t_host", "t_run_s", "wall", "source", "kind", "detail"])
        run.log_file = open(run_dir / "app_log.txt", "w", encoding="utf-8")
        self.run = run
        self.last_run_dir = run_dir
        self._write_session()
        self._log_raw_estimate(run_dir, (end_idx - start_idx) / RATE / 3600)

        self.clock = rs.PlayClock()
        self.last_sync = None
        self.rec_stats = None
        self._reset_ring_stats()
        for table in (self.table, self.files_table, self.fill_table):
            table.delete(*table.get_children())
        self._refresh_files_title()
        self._last_pulse_text = "--"
        self.status_var.set(f"Run folder: {run_dir}")

        keep_awake(True)
        self._set_phase("ARMING")
        self._event("app", "arm", f"NPZ {start_idx}..{end_idx} "
                                  f"({rs.fmt_hms(start_idx / RATE)} + {rs.fmt_hms((end_idx - start_idx) / RATE)})")
        self.stm.push_cmd(("run_arm", {"start": start_idx, "end": end_idx,
                                       "sync_csv": str(run_dir / "sync_log.csv")}))
        self.root.after(60000, lambda r=run: self._arm_watchdog(r))

    def _log_raw_estimate(self, run_dir: Path, hours: float) -> None:
        """Disk needed by the raw files of this run, and whether the CSV fits."""
        per_hour = 3600 * pc.SAMPLE_RATE_HZ / 1e9
        npz_gb = hours * per_hour * ppk_raw.NPZ_BYTES_PER_SAMPLE
        free_gb = shutil.disk_usage(run_dir).free / 1e9
        if not self.args.raw_csv:
            self._log(f"[APP] Raw 100 kS/s: NPZ about {npz_gb:.1f} GB in ppk_raw/, CSV off; "
                      f"{free_gb:.0f} GB free")
            return
        csv_h = per_hour * ppk_raw.CSV_BYTES_PER_ROW
        self._log(f"[APP] Raw 100 kS/s: NPZ about {npz_gb:.1f} GB in ppk_raw/, CSV twin ON, "
                  f"about {hours * csv_h:.0f} GB ({csv_h:.0f} GB/h); {free_gb:.0f} GB free")
        room_h = (free_gb - ppk_raw.CSV_MIN_FREE_GB) / csv_h
        if room_h < hours:
            self._log(f"[WARN] The raw CSV does not fit: it stops by itself after about "
                      f"{max(room_h, 0.0):.1f} h, when {ppk_raw.CSV_MIN_FREE_GB:g} GB is left. "
                      "The NPZ files continue. Set RAW_CSV = 0 for long runs.", "warn")

    def _arm_watchdog(self, run: RunState) -> None:
        if self.run is run and self.phase in ("ARMING", "REC_WAIT", "PREROLL"):
            self._event("app", "error", f"run did not reach GO within 60 s (stuck in {self.phase})")
            self._stop_run("error")

    def _go(self) -> None:
        if self.phase == "PREROLL" and self.stm is not None:
            self.stm.push_cmd(("run_go", None))

    def _stop_run(self, status: str, discard: bool = False) -> None:
        run = self.run
        if run is None or self.phase not in ACTIVE_PHASES:
            return
        run.status = status
        run.discard = discard
        self._set_phase("STOPPING")
        self._event("app", "stop", f"{status}, files {'discarded' if discard else 'kept'}")
        self._stm_resume = None
        if self.stm is not None and self.stm_connected:
            self.stm.push_cmd(("run_stop", None))
        else:
            run.stm_stopped = True
        if run.rec_requested:
            self.ppk.send("rec_stop")
        else:
            run.ppk_stopped = True
        self.root.after(15000, lambda r=run: self._force_finalize(r))
        self._maybe_finalize()

    def _maybe_finalize(self) -> None:
        run = self.run
        if run is not None and self.phase == "STOPPING" and run.stm_stopped and run.ppk_stopped:
            self._finalize()

    def _force_finalize(self, run: RunState) -> None:
        if self.run is run and self.phase == "STOPPING":
            self._log("[WARN] Stop not confirmed by every device after 15 s, finishing anyway", "warn")
            self._finalize()

    def _finalize(self) -> None:
        run = self.run
        self._stop_checkpoint()
        if run.discard:
            for f in (run.events_file, run.log_file):
                try:
                    f.close()
                except Exception:
                    pass
            run.events_writer = None
            run.log_file = None
            self._discard_run_dir(run, attempt=0)
            return
        s = run.session
        s["status"] = run.status
        s["t_stop"] = time.perf_counter()
        s["wall_stop"] = datetime.now().isoformat(timespec="milliseconds")
        s["live_counts"] = run.counts
        self._write_session()
        self._event("app", "finished", f"status={run.status}, analysing…")
        run.events_file.close()
        run.events_writer = None
        self._set_phase("ANALYZING")
        npz = self.npz

        def work():
            try:
                summary = analyze_run.analyze(run.run_dir, npz=npz, log=lambda _s: None)
                self.bgq.put(("analysis", (run, summary)))
            except Exception:
                self.bgq.put(("analysis_error", (run, traceback.format_exc())))

        threading.Thread(target=work, daemon=True, name="analysis").start()

    def _discard_run_dir(self, run: RunState, attempt: int) -> None:
        """Delete a discarded run. Windows refuses while a file is still open
        (e.g. the PPK2 process closing late), so retry for a few seconds."""
        try:
            shutil.rmtree(run.run_dir)
        except FileNotFoundError:
            pass
        except OSError as exc:
            if attempt < 10:
                self.root.after(1000, lambda: self._discard_run_dir(run, attempt + 1))
                return
            self._log(f"[WARN] Could not delete {run.run_dir}: {exc}", "warn")
        else:
            self._log(f"[APP] Run discarded, deleted {run.run_dir.name}", "warn")
            self.files_table.delete(*self.files_table.get_children())
        if self.run is run:
            self.run = None
        if self.last_run_dir == run.run_dir:
            self.last_run_dir = None
        self.status_var.set(f"Runs folder: {self.runs_dir}")
        keep_awake(False)
        self._set_phase("IDLE")
        if self._quit_pending:
            self._quit()

    def _on_analysis(self, run: RunState, summary: Optional[dict], error: str = "") -> None:
        if error:
            self._log(f"[ERROR] Analysis failed:\n{error}", "error")
        else:
            for line in analyze_run.format_summary(summary).splitlines():
                self._log(line, "ok" if line and not line.startswith(" ") else "")
            self._log(f"[APP] Results in {run.run_dir}", "ok")
            self._add_file_row(time.time(), "summary.*, d7_events.csv", "whole run", tag="final")
        try:
            run.log_file.close()
        except Exception:
            pass
        run.log_file = None
        keep_awake(False)
        self._set_phase("DONE")
        if self._quit_pending:
            self._quit()

    def _write_session(self) -> None:
        run = self.run
        if run is None:
            return
        tmp = run.run_dir / "session.json.tmp"
        tmp.write_text(json.dumps(run.session, indent=2), encoding="utf-8")
        os.replace(tmp, run.run_dir / "session.json")

    def _set_phase(self, phase: str) -> None:
        self.phase = phase
        colors = {"IDLE": C_DIM, "ARMING": C_YELLOW, "REC_WAIT": C_YELLOW, "PREROLL": C_YELLOW,
                  "RUNNING": C_GREEN, "TAIL": C_GREEN, "STOPPING": C_ORANGE,
                  "ANALYZING": C_PURPLE, "DONE": C_BLUE}
        names = {"REC_WAIT": "ARMING", "PREROLL": "PRE-ROLL", "TAIL": "RUNNING (tail)"}
        self.phase_var.set(names.get(phase, phase))
        self.phase_lbl.config(fg=colors.get(phase, C_DIM))
        self._update_controls()

    def _update_controls(self) -> None:
        active = self.phase in ACTIVE_PHASES
        busy = self.phase in ("STOPPING", "ANALYZING")
        if active:
            self.run_btn.config(text="■  STOP RUN", state="normal", bg=blend(C_RED, C_PANEL, 0.7),
                                fg=C_TEXT, activebackground=C_RED)
        elif busy:
            self.run_btn.config(text="…  " + self.phase.title(), state="disabled",
                                bg=C_PANEL_2, disabledforeground=C_DIM)
        else:
            ready = all(ok for _, ok in self._ready_items())
            self.run_btn.config(text="▶  START RUN", state="normal" if ready else "disabled",
                                bg=blend(C_GREEN, C_PANEL, 0.35 if ready else 0.85),
                                fg=C_BG if ready else C_DIM, activebackground=C_GREEN,
                                disabledforeground=C_DIM)
        locked = "disabled" if (active or busy) else "normal"
        # Only on a change: re-configuring a box every 0.5 s would disturb typing.
        if locked != self._lock_state:
            self._lock_state = locked
            for w in (self.npz_btn, self.mv_entry, self.stm_port_cb, self.ppk_port_cb):
                w.config(state=locked)
            self.start_in.set_state(locked)
            self.dur_in.set_state(locked)
        self.ready_var.set("   ".join(f"{'✓' if ok else '✗'} {name}"
                                      for name, ok in self._ready_items()))

    # ── Event pump ─────────────────────────────────────────────────────────

    def _tick(self) -> None:
        try:
            self._drain()
            now = time.perf_counter()
            if now - self._last_ui >= 0.5:
                self._last_ui = now
                self._autosave_tick(now)
                self._refresh_stats(now)
                self._refresh_ring()
                self._update_controls()
            if now - self._last_disk >= 10.0:
                self._last_disk = now
                self._refresh_files_title()
        except Exception:
            self._log("[ERROR] GUI tick: " + traceback.format_exc().strip().splitlines()[-1], "error")
        finally:
            self.root.after(100, self._tick)

    def _drain(self) -> None:
        for _ in range(500):
            try:
                msg = self.lq.get_nowait()
            except queue.Empty:
                break
            # Routine refill lines come every few seconds for 24 h: file only.
            routine = msg.startswith("[AUTO] ring=") or msg.startswith("[DATA] Done")
            self._log(msg, screen=not routine)
        for _ in range(500):
            try:
                self._on_stm_item(self.sq.get_nowait())
            except queue.Empty:
                break
        for kind, data in self.ppk.poll():
            self._on_ppk_event(kind, data)
        while True:
            try:
                kind, data = self.bgq.get_nowait()
            except queue.Empty:
                break
            if kind == "npz":
                self._on_npz_loaded(data)
            elif kind == "npz_error":
                self.npz_loading = False
                self.npz_info_var.set("load failed")
                self._log(f"[ERROR] NPZ load failed: {data}", "error")
            elif kind == "analysis":
                self._on_analysis(*data)
            elif kind == "analysis_error":
                self._on_analysis(data[0], None, data[1])

    def _on_stm_item(self, item) -> None:
        if isinstance(item, vimu.StatusPayload):
            self.stm_status = item
            return
        if item == "connected":
            self.stm_connected = True
            if self._stm_resume is not None and self.stm is not None:
                self.stm.push_cmd(("run_resume", self._stm_resume))
                self._stm_resume = None
            return
        if item == "error":
            return
        if item == "disconnected":
            self._on_stm_lost()
            return
        if not isinstance(item, tuple):
            return
        kind, data = item
        run = self.run
        if kind == "sync":
            self.clock.add(data.t, data.played)
            self.last_sync = data
            # The ring empties for good at the end of the data; that is not a low.
            if run is not None and data.cursor < run.end_idx:
                low = self.ring_stats["min"]
                self.ring_stats["min"] = data.ring if low is None else min(low, data.ring)
        elif kind == "ring_fill":
            self._on_ring_fill(data)
        elif kind == "run_event":
            t, ev_kind, detail = data
            if ev_kind == "ring_underrun":
                self.ring_stats["underruns"] += 1
            self._event("stm32", ev_kind, detail, t=t, log=False)
        elif kind == "run_armed" and run is not None and self.phase == "ARMING":
            self._event("app", "armed", f"prefill {data['prefill']} samples; {data['status']}")
            self._set_phase("REC_WAIT")
            run.rec_requested = True
            self.ppk.send("rec_start", {"run_dir": str(run.run_dir),
                                        "raw_csv": bool(self.args.raw_csv)})
        elif kind == "run_go" and run is not None and self.phase == "PREROLL":
            run.t_go = data["t_go"]
            self.clock.add(run.t_go, 0)
            wall = datetime.now() - timedelta(seconds=time.perf_counter() - run.t_go)
            run.session.update({"status": "running", "t_go": run.t_go,
                                "wall_go": wall.isoformat(timespec="milliseconds"),
                                "go_rtt_ms": data["rtt_ms"]})
            self._write_session()
            self._set_phase("RUNNING")
            self._event("app", "go", f"STM32 START acknowledged in {data['rtt_ms']:.1f} ms, "
                                     "t_run = 0")
        elif kind == "run_complete" and self.phase == "RUNNING":
            self._set_phase("TAIL")
            self._event("app", "complete", f"all samples played, stopping in {self.args.tail:g} s")
            self.root.after(int(self.args.tail * 1000), lambda: self._stop_run("completed"))
        elif kind == "run_stopped" and run is not None:
            run.stm_stopped = True
            self._maybe_finalize()
        elif kind == "run_error":
            self._event("app", "error", str(data))
            if self.phase in ("ARMING", "REC_WAIT", "PREROLL"):
                self._stop_run("error")

    def _on_ppk_event(self, kind: str, data) -> None:
        run = self.run
        active = run is not None and self.phase in ACTIVE_PHASES + ("STOPPING",)
        if kind == "live":
            self._on_live(data)
        elif kind == "pulse":
            self._on_pulse(data)
        elif kind == "raw_file":
            self._on_raw_file(data)
        elif kind == "log":
            self._log(f"[PPK2] {data}")
        elif kind == "warn":
            if active:
                self._event("ppk2", "warn", str(data))
            else:
                self._log(f"[WARN] PPK2: {data}", "warn")
        elif kind in ("error", "fatal"):
            if active:
                self._event("ppk2", "error", str(data))
            else:
                self._log(f"[ERROR] PPK2: {data}", "error")
            if not self.ppk_connected:
                self.ppk_info_var.set("error, see log")
            if self.phase == "REC_WAIT" and str(data).startswith("rec_start"):
                self._stop_run("error")
        elif kind == "connected":
            self.ppk_connected = True
            self.ppk_link = "ok"
            self.ppk_btn.config(text="Disconnect")
            self._log(f"[PPK2] connected on {data['port']}, source {data['vdd_mv']} mV, "
                      "measuring (DUT power OFF)")
        elif kind == "disconnected":
            self.ppk_connected = False
            self.dut_on = False
            self.dut_on_at = None
            self.ppk_btn.config(text="Connect")
            self.ppk_info_var.set("disconnected")
        elif kind == "dut":
            self.dut_on = data["on"]
            self.dut_on_at = data["t"] if data["on"] else None
            self.dut_btn.config(text="DUT ON" if self.dut_on else "DUT OFF",
                                fg=C_GREEN if self.dut_on else C_DIM)
            if active:
                self._event("ppk2", "dut_power", "ON" if self.dut_on else "OFF", t=data["t"])
            else:
                self._log(f"[PPK2] DUT power {'ON' if self.dut_on else 'OFF'}")
        elif kind == "link":
            self.ppk_link = data["state"]
            if active:
                self._event("ppk2", "link_lost" if data["state"] == "reconnecting" else "link_ok")
        elif kind == "loss":
            if active and run is not None:
                now = time.perf_counter()
                detail = f"{data['lost']} samples ({data['source']}) at n={data['n']}"
                # every gap is in ppk_loss.csv; keep the event log readable
                self._event("ppk2", "sample_loss", detail, t=data["t"],
                            log=now - run.loss_logged_at > 10)
                if now - run.loss_logged_at > 10:
                    run.loss_logged_at = now
        elif kind == "rec_started" and run is not None:
            run.rec_started = True
            run.session["rec"] = data
            self._write_session()
            if self.phase == "REC_WAIT":
                self._set_phase("PREROLL")
                self._event("app", "recording", f"PPK2 recording from sample {data['rec_start_n']}, "
                                                f"GO in {self.args.preroll:g} s")
                self.root.after(int(self.args.preroll * 1000), self._go)
        elif kind == "rec_stopped" and run is not None and self.phase == "STOPPING":
            run.session["ppk_meta"] = data
            run.ppk_stopped = True
            self._maybe_finalize()
        elif kind == "exited":
            self.ppk_connected = False
            self._log("[WARN] PPK2 process exited", "warn")

    def _on_live(self, d: dict) -> None:
        self.ppk_live = d
        if d["g_mean"].size:
            self.recent_blocks.extend(d["g_mean"].tolist())
        if d["rec"] is not None and self.run is not None:
            self.rec_stats = d["rec"]
        self.logic_window.append(d.get("logic"))
        if self.ppk_connected:
            if self.ppk_link == "reconnecting":
                self.ppk_info_var.set("link lost, reconnecting…")
            else:
                self.ppk_info_var.set(f"{d['rate'] / 1000:.1f} kS/s  ·  DUT {'ON' if d['dut'] else 'OFF'}"
                                      f"  ·  D7 pulses {d['rises']}"
                                      f"  ·  D0-D7 {logic_states(self.logic_window)}")

    # ── Auto-save snapshots ────────────────────────────────────────────────

    def _autosave_interval(self) -> int:
        return max(AUTOSAVE_MIN_S, self.autosave_in.peek_seconds())

    def _autosave_tick(self, now: float) -> None:
        self._poll_checkpoint()
        run = self.run
        if (not self.autosave_var.get() or run is None or run.t_go is None
                or self.phase not in ("RUNNING", "TAIL") or self._ckpt_proc is not None):
            return
        t_run = now - run.t_go
        interval = self._autosave_interval()
        due = run.last_ckpt + interval
        if t_run >= due:
            # Stay on the schedule (1h, 2h, ...) unless it fell a whole interval
            # behind, e.g. auto-save switched on in the middle of a run.
            self._start_checkpoint(run, due if t_run - due < interval else t_run)

    def _start_checkpoint(self, run: RunState, at: float) -> None:
        """Analyse the run so far in a separate process: the analysis re-reads
        hours of CSV, which must not hold the GIL the UART thread needs."""
        sec = int(round(at))
        label = f"{sec // 3600:02d}h{sec % 3600 // 60:02d}m{sec % 60:02d}s"
        run.last_ckpt = at
        t_run = time.perf_counter() - run.t_go
        run.session.setdefault("checkpoints", []).append(
            {"label": label, "t_run_s": round(t_run, 1),
             "wall": datetime.now().isoformat(timespec="seconds")})
        self._write_session()
        cmd = [sys.executable, str(APP_DIR / "analyze_run.py"), str(run.run_dir),
               "--checkpoint", label]
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        try:
            self._ckpt_proc = subprocess.Popen(cmd, cwd=str(APP_DIR), stdout=subprocess.DEVNULL,
                                               stderr=subprocess.PIPE, creationflags=flags)
        except OSError as exc:
            self._event("app", "checkpoint_failed", f"{label}: {exc}")
            return
        self._ckpt_run = run
        self._ckpt_label = label
        self._event("app", "checkpoint", f"{label}: saving the data so far")

    def _poll_checkpoint(self) -> None:
        proc = self._ckpt_proc
        if proc is None or proc.poll() is None:
            return
        err = proc.stderr.read().decode(errors="replace").strip() if proc.stderr else ""
        self._ckpt_proc = None
        run, label = self._ckpt_run, self._ckpt_label
        if proc.returncode != 0:
            self._event("app", "checkpoint_failed",
                        f"{label}: exit {proc.returncode} {err.splitlines()[-1] if err else ''}")
            return
        run.ckpt_count += 1
        run.last_ckpt_label = label
        if run is self.run:
            self._add_file_row(time.time(), f"checkpoints/{label}_summary.*",
                               f"run to {label}", tag="ckpt")
        detail = f"{label} saved"
        try:
            s = json.loads((run.run_dir / "summary.json").read_text(encoding="utf-8"))
            v = s["d7"]["verdicts"]
            avg = s.get("current", {}).get("avg_uA")
            detail += (f": FP {v.get('FP', 0)}, TP {v.get('TP', 0)}"
                       + (f", average {fmt_current(avg)}" if avg is not None else ""))
        except (OSError, ValueError, KeyError):
            pass
        self._event("app", "checkpoint_saved", detail)

    def _refresh_autosave_stat(self, now: float) -> None:
        if not self.autosave_var.get():
            self._set_stat("autosave", "off", C_DIM)
            return
        if self._ckpt_proc is not None:
            self._set_stat("autosave", f"saving {self._ckpt_label}…", C_YELLOW)
            return
        run = self.run
        if run is not None and run.t_go is not None and self.phase in ("RUNNING", "TAIL"):
            left = run.last_ckpt + self._autosave_interval() - (now - run.t_go)
            self._set_stat("autosave", f"next in {rs.fmt_hms(max(left, 0.0))}  "
                                       f"({run.ckpt_count} saved)", C_CYAN)
        else:
            self._set_stat("autosave", f"every {rs.fmt_hms(self._autosave_interval())}", C_SUB)

    def _stop_checkpoint(self) -> None:
        """A snapshot still running when the run ends is superseded by the
        final analysis (or by deleting the folder), so stop it."""
        proc = self._ckpt_proc
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        self._ckpt_proc = None

    def _on_pulse(self, d: dict) -> None:
        run = self.run
        t = d["t_est"]
        if run is None or not d["recording"] or self.phase not in ACTIVE_PHASES + ("STOPPING",):
            now = time.perf_counter()
            if now - self._idle_pulse_logged_at > 10:
                self._idle_pulse_logged_at = now
                self._log(f"[PPK2] D7 pulse outside a run (width {d['width'] / 100:.2f} ms)")
            return

        idx = None
        lab = {"category": "", "phase": "", "task": ""}
        if d["glitch"]:
            verdict = "GLITCH"
        elif run.t_go is None or t is None:
            verdict = "PREROLL"
        else:
            played = self.clock.played_at(t)
            if played is None or played < 0:
                verdict = "PREROLL"
            else:
                idx = run.start_idx + min(int(math.floor(played)), run.length - 1)
                lab = self.npz.labels_at(idx)
                verdict, _fid = rs.classify_pulse(idx, run.start_idx, self.npz.episodes,
                                                  int(self.args.tolerance * RATE),
                                                  int(self.args.warmup * RATE), run.detected)
        run.counts[verdict] = run.counts.get(verdict, 0) + 1

        t_run = rs.fmt_hms(t - run.t_go, 1) if (t is not None and run.t_go is not None) else "--"
        activity = " · ".join(x for x in (lab["category"], lab["phase"], lab["task"]) if x)
        self.table.insert("", 0, values=(
            d["rise_no"], t_run, rs.fmt_hms(idx / RATE, 1) if idx is not None else "--",
            idx if idx is not None else "--", activity or "--", verdict,
            f"{d['width'] / 100:.2f} ms"), tags=(verdict,))
        children = self.table.get_children()
        if len(children) > 500:
            self.table.delete(*children[500:])
        self._last_pulse_text = f"#{d['rise_no']} {verdict} @ {t_run}"

    # ── Stats panel ────────────────────────────────────────────────────────

    def _set_stat(self, key: str, text: str, color: str = C_TEXT) -> None:
        self.stat_vars[key].set(text)
        self.stat_lbls[key].config(fg=color)

    def _refresh_stats(self, now: float) -> None:
        run = self.run
        st = self.stm_status
        if self.stm_connected and st is not None:
            ring = f"{st.ring_samples}/{vimu.RING_CAPACITY}" if st.ring_samples is not None else st.level_name
            self.stm_info_var.set(f"{st.state_name}  ·  ring {ring}  ·  {', '.join(st.flag_names)}")
        elif self.stm_connected:
            self.stm_info_var.set("connected")

        running = run is not None and run.t_go is not None
        played = 0.0
        if running:
            t_end = now if self.phase in ACTIVE_PHASES else run.session.get("t_stop", now)
            played = self.clock.played_at(t_end) or 0.0
            played = min(max(played, 0.0), run.length)
            idx = run.start_idx + min(int(played), run.length - 1)
            self._set_stat("elapsed", rs.fmt_hms(t_end - run.t_go))
            self._set_stat("scenario", f"{rs.fmt_hms(idx / RATE)} / {rs.fmt_hms(run.end_idx / RATE)}")
            self._set_stat("progress", f"{played / run.length * 100:.2f} %  ({int(played):,} smp)")
            self._set_stat("eta", rs.fmt_hms((run.length - played) / RATE))
            lab = self.npz.labels_at(idx)
            self._set_stat("activity", " · ".join(x for x in (lab["category"], lab["phase"],
                                                              lab["task"]) if x) or "--", C_CYAN)
        else:
            for key in ("elapsed", "scenario", "progress", "eta", "activity"):
                self._set_stat(key, "--", C_DIM)
        self._refresh_autosave_stat(now)

        if st is not None:
            self._set_stat("stm", f"{st.state_name}, ring {st.ring_samples}",
                           C_GREEN if st.state == vimu.VIMU_STATE_RUNNING else C_YELLOW)
        else:
            self._set_stat("stm", "--", C_DIM)
        sp = self.last_sync
        if running and sp is not None:
            drift = sp.played - (sp.t - run.t_go) * RATE
            self._set_stat("played", f"{sp.played:,}")
            # A steady clock offset (the STM32 here runs about -900 ppm) is
            # harmless: the mapping uses the played count. Only a rate far off
            # 50 Hz is worth a colour; stalls and jumps are logged as events.
            elapsed = sp.t - run.t_go
            if elapsed > 30:
                ppm = (sp.played / (elapsed * RATE) - 1) * 1e6
                self._set_stat("drift", f"{drift:+.0f} smp ({ppm:+.0f} ppm)",
                               C_TEXT if abs(ppm) < 2000 else C_YELLOW)
            else:
                self._set_stat("drift", f"{drift:+.0f} samples")
            self._set_stat("rtt", f"{sp.rtt_ms:.1f} ms  ({len(self.clock):,} points)")
        else:
            for key in ("played", "drift", "rtt"):
                self._set_stat(key, "--", C_DIM)

        live = self.ppk_live
        if self.ppk_connected and live:
            if self.recent_blocks:
                self._set_stat("live", fmt_current(float(np.mean(self.recent_blocks))), C_CYAN)
            rate = live["rate"]
            self._set_stat("rate", f"{rate / 1000:.1f} kS/s", C_GREEN if rate >= 99_000 else C_RED)
            rec_live = live.get("rec")
            if rec_live is not None and self.phase in ACTIVE_PHASES:
                n_run = rec_live.get("losses", 0)
                self._set_stat("loss", f"{n_run} in run ({live['losses']} since connect)",
                               C_TEXT if n_run == 0 else C_YELLOW)
            else:
                self._set_stat("loss", f"{live['losses']} since connect (counter {live['counter']})",
                               C_TEXT if live["losses"] == 0 else C_YELLOW)
        else:
            for key in ("live", "rate", "loss"):
                self._set_stat(key, "--", C_DIM)
        rec = self.rec_stats
        if run is not None and rec is not None:
            self._set_stat("avg", fmt_current(rec["avg_uA"]))
            self._set_stat("charge", f"{rec['charge_uC'] / 3.6e6:.4f} mAh")
        else:
            self._set_stat("avg", "--", C_DIM)
            self._set_stat("charge", "--", C_DIM)
        raw = rec.get("raw") if run is not None and rec is not None else None
        if raw is not None:
            self._set_stat("raw", *raw_stat(raw))
        else:
            self._set_stat("raw", "--", C_DIM)

        if run is not None:
            c = run.counts
            total = sum(c.values())
            other = c.get("WARMUP", 0) + c.get("PREROLL", 0) + c.get("GLITCH", 0)
            hours = played / RATE / 3600
            fp = c.get("FP", 0)
            fp_rate = f"  ({fp / hours:.1f}/h)" if hours > 0.05 else ""
            falls = [ep for ep in self.npz.episodes if run.start_idx <= ep.start_sample < run.end_idx]
            self._set_stat("pulses", f"{total}  (other {other})")
            self._set_stat("fp", f"{fp}{fp_rate}", C_RED if fp else C_TEXT)
            self._set_stat("tp", f"{len(run.detected)} / {len(falls)}", C_GREEN if run.detected else C_TEXT)
            self._set_stat("last", self._last_pulse_text)
        else:
            for key in ("pulses", "fp", "tp", "last"):
                self._set_stat(key, "--", C_DIM)

    # ── Saved files and ring buffer ────────────────────────────────────────

    def _reset_ring_stats(self) -> None:
        self.ring_stats = {"fills": 0, "walls": deque(maxlen=50), "min": None, "underruns": 0}

    def _add_file_row(self, wall: float, name: str, data: str, npz: str = "",
                      csv_size: str = "", tag: str = "raw") -> None:
        self.files_table.insert("", 0, values=(
            datetime.fromtimestamp(wall).strftime("%H:%M:%S"), name, data, npz, csv_size),
            tags=(tag,))
        children = self.files_table.get_children()
        if len(children) > FILES_ROWS_MAX:
            self.files_table.delete(*children[FILES_ROWS_MAX:])

    def _on_raw_file(self, d: dict) -> None:
        """One raw minute saved by the PPK2 process: ppk_raw/raw_<time>.npz (+ .csv)."""
        a = d["t0_ms"] / 1000
        b = a + d["rows"] / pc.SAMPLE_RATE_HZ
        self._add_file_row(d["t_wall"], f"{ppk_raw.RAW_DIR}/{d['name']}.npz",
                           f"{rs.fmt_hms(a)} – {rs.fmt_hms(b)}", fmt_bytes(d["npz_bytes"]),
                           fmt_bytes(d["csv_bytes"]) if d["csv_bytes"] else "--")

    def _refresh_files_title(self) -> None:
        try:
            free = f"{shutil.disk_usage(self.runs_dir.resolve().anchor).free / 1e9:.0f} GB free"
        except OSError:
            free = "free space unknown"
        where = self.last_run_dir.name if self.last_run_dir is not None else "no run yet"
        self.files_title_var.set(f"Saved files  ·  {where}  ·  {free}")

    def _on_ring_fill(self, d: dict) -> None:
        """One prefill or refill of the STM32 ring by the UART thread."""
        if d["kind"] == "refill":
            self.ring_stats["fills"] += 1
            self.ring_stats["walls"].append(d["wall"])
        before = d["ring_before"]
        short = d["sent"] < d["requested"]
        self.fill_table.insert("", 0, values=(
            datetime.fromtimestamp(d["wall"]).strftime("%H:%M:%S"), d["kind"],
            "--" if before is None else f"{before}  ({before / RATE:.1f} s)",
            f"+{d['sent']}" + (f"/{d['requested']}" if short else ""),
            f"{d['cursor']:,}"), tags=("short" if short else d["kind"],))
        children = self.fill_table.get_children()
        if len(children) > FILL_ROWS_MAX:
            self.fill_table.delete(*children[FILL_ROWS_MAX:])

    def _set_ring(self, key: str, text: str, color: str = C_TEXT) -> None:
        self.ring_vars[key].set(text)
        self.ring_lbls[key].config(fg=color)

    def _refresh_ring(self) -> None:
        st = self.stm_status if self.stm_connected else None
        self.ring_bar.set(None if st is None else st.ring_samples,
                          None if st is None else st.free_samples)
        if st is None:
            self._set_ring("state", "connected" if self.stm_connected else "disconnected", C_DIM)
            self._set_ring("flags", "--", C_DIM)
        else:
            running = st.state == vimu.VIMU_STATE_RUNNING
            self._set_ring("state", f"{st.state_name}  ·  level {st.level_name}",
                           C_GREEN if running else C_YELLOW)
            self._set_ring("flags", ", ".join(st.flag_names),
                           C_RED if running and st.ring_empty else C_TEXT)

        run, sp, info = self.run, self.last_sync, self.ring_stats
        if run is not None and run.t_go is not None and sp is not None:
            self._set_ring("sent", f"{sp.cursor - run.start_idx:,} sent  ·  {sp.played:,} played")
        else:
            self._set_ring("sent", "--", C_DIM)
        walls = info["walls"]
        if info["fills"]:
            every = (f"  ·  every {(walls[-1] - walls[0]) / (len(walls) - 1):.1f} s"
                     if len(walls) > 1 else "")
            self._set_ring("fills", f"{info['fills']}{every}")
        else:
            self._set_ring("fills", "--" if run is None else "0", C_DIM)
        low = info["min"]
        if low is None:
            self._set_ring("min", "--", C_DIM)
        else:
            self._set_ring("min", f"{low}  ({low / RATE:.1f} s)",
                           C_RED if low == 0 else (C_YELLOW if low < 3 * RATE else C_TEXT))
        if run is None:
            self._set_ring("under", "--", C_DIM)
        else:
            n = info["underruns"]
            self._set_ring("under", str(n), C_RED if n else C_TEXT)

    # ── Misc ───────────────────────────────────────────────────────────────

    def _open_folder(self) -> None:
        target = self.last_run_dir or self.runs_dir
        target.mkdir(parents=True, exist_ok=True)
        os.startfile(str(target))

    def _on_close(self) -> None:
        if self.phase in ACTIVE_PHASES:
            choice = self._ask_stop_choice(
                "Quit", "A run is in progress. Stop it and quit?\n\n"
                "Save: keep the files, analyse them, then quit.\n"
                f"Discard: delete this run's folder ({self.run.run_dir.name}), then quit.")
            if choice == "cancel" or (choice == "discard" and not self._confirm_discard()):
                return
            self._quit_pending = True
            self._stop_run("stopped", discard=choice == "discard")
            return
        if self.phase in ("STOPPING", "ANALYZING"):
            if messagebox.askyesno("Quit", "The run is still being saved. Quit when it is done?"):
                self._quit_pending = True
            return
        self._quit()

    def _quit(self) -> None:
        self._stop_checkpoint()
        if self.stm is not None:
            self._stm_user_disconnect = True
            self.stm.request_stop()
        self.ppk.close(timeout=5)
        self.root.destroy()


# ── Entry point ────────────────────────────────────────────────────────────

def _style(root: tk.Tk) -> None:
    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass
    root.option_add("*TCombobox*Listbox.background", C_FIELD)
    root.option_add("*TCombobox*Listbox.foreground", C_TEXT)
    root.option_add("*TCombobox*Listbox.selectBackground", C_PANEL_3)
    root.option_add("*TCombobox*Listbox.selectForeground", C_TEXT)
    style.configure("Vimu.TCombobox", fieldbackground=C_FIELD, background=C_PANEL_2,
                    foreground=C_TEXT, selectbackground=C_PANEL_3, selectforeground=C_TEXT,
                    bordercolor=C_BORDER, arrowcolor=C_SUB, lightcolor=C_BORDER,
                    darkcolor=C_BORDER, padding=(6, 3), insertcolor=C_TEXT)
    style.map("Vimu.TCombobox", fieldbackground=[("readonly", C_FIELD), ("disabled", C_PANEL_2)],
              foreground=[("readonly", C_TEXT), ("disabled", C_DIM)])
    style.configure("Run.Treeview", background=C_FIELD, fieldbackground=C_FIELD,
                    foreground=C_TEXT, rowheight=21, borderwidth=0, font=FONT_MONO_SM)
    style.configure("Run.Treeview.Heading", background=C_PANEL_2, foreground=C_SUB,
                    font=FONT_UI_XS, relief="flat", borderwidth=0)
    style.map("Run.Treeview", background=[("selected", C_PANEL_3)])
    style.configure("Vertical.TScrollbar", background=C_PANEL_2, troughcolor=C_FIELD,
                    bordercolor=C_BORDER, arrowcolor=C_SUB)


def main() -> None:
    ap = argparse.ArgumentParser(description="VIMU x PPK2 HIL run app")
    ap.add_argument("--stm32-port", default=vimu.DEFAULT_SERIAL_PORT)
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--poll", type=float, default=0.5, help="STM32 UPDATE poll period (s)")
    ap.add_argument("--ppk2-port", default="", help="PPK2 data port, e.g. COM17")
    ap.add_argument("--voltage", type=int, default=3300, help="PPK2 source voltage (mV)")
    ap.add_argument("--npz", default="", help="NPZ file to load at start")
    ap.add_argument("--preroll", type=float, default=1.0, help="PPK2 baseline before GO (s)")
    ap.add_argument("--tail", type=float, default=5.0, help="keep recording after the last sample (s)")
    ap.add_argument("--tolerance", type=float, default=3.0, help="TP window after a fall ends (s)")
    ap.add_argument("--warmup", type=float, default=4.0, help="seconds after GO that are not scored")
    ap.add_argument("--boot-wait", type=float, default=5.0, help="warn if DUT was powered more recently (s)")
    ap.add_argument("--refill-threshold", type=int, default=7, help="refill the ring below this (s)")
    ap.add_argument("--snippet-pre", type=float, default=1.0, help="full-rate data before a D7 pulse (s)")
    ap.add_argument("--snippet-post", type=float, default=1.0, help="full-rate data after a D7 pulse (s)")
    ap.add_argument("--max-snippets", type=int, default=1000, help="snippet files per run")
    ap.add_argument("--min-pulse-us", type=float, default=200, help="shorter D7 pulses are glitches")
    ap.add_argument("--raw-csv", type=int, choices=(0, 1), default=RAW_CSV,
                    help=f"1 = CSV twin of every raw NPZ file, 0 = NPZ only (default {RAW_CSV}, "
                         "the RAW_CSV line at the top of this file)")
    ap.add_argument("--runs-dir", default=str(APP_DIR / "runs"))
    ap.add_argument("--autosave", default="",
                    help="turn auto-save on with this interval, e.g. 1:00:00 (h:m:s)")
    args = ap.parse_args()
    try:
        args.autosave_s = int(rs.parse_hms(args.autosave)) if args.autosave else 0
    except ValueError:
        ap.error(f"--autosave: '{args.autosave}' is not h:m:s")

    root = tk.Tk()
    _style(root)
    RunApp(root, args)
    root.mainloop()


if __name__ == "__main__":
    main()
