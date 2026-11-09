"""PPK2 capture worker for the HIL run app.

The worker owns the PPK2 serial port and runs in its own process, so the
100 kS/s stream never waits on tkinter or on the STM32 UART thread.
Host timestamps come from time.perf_counter(). On Windows that is the
system-wide QueryPerformanceCounter, so they compare directly with the
timestamps taken in the GUI process.

Files written while recording, all in the run directory:
  current_1ms_HH.f32     float32 LE, mean current (uA) of every 100 samples
                         (1 ms), one file per hour of PPK2 samples
  summary_1s.csv         one row per 100,000 samples (1 s of PPK2 time)
  ppk_anchors.csv        PPK2 sample index -> host time, one row per window
  d7_pulses.csv          every D7 pulse: sample index, width, host time
  ppk_loss.csv           sample gaps found by the PPK2 counter or a reconnect
  snippets/d7_NNNNN.npz  full-rate current and D0-D7 around a D7 pulse
  ppk_raw/raw_HHhMMmSSs.npz  every sample, Current(uA) and D0-D7, one file per
                         minute, bzip2 inside (format in ppk_raw.py)
  ppk_raw/raw_HHhMMmSSs.csv  the same rows as CSV text, when raw CSV is on
  ppk_meta.json          totals, written when recording stops
"""

from __future__ import annotations

import csv
import json
import multiprocessing as mp
import queue
import time
import traceback
from collections import deque
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np

from ppk_raw import RawWriter

try:
    import serial
    from ppk2_api.ppk2_api import PPK2_API
    PPK2_IMPORT_ERROR = ""
except ImportError as _exc:  # reported to the GUI by the worker
    serial = None  # type: ignore[assignment]
    PPK2_API = object  # type: ignore[assignment,misc]
    PPK2_IMPORT_ERROR = str(_exc)

SAMPLE_RATE_HZ = 100_000
SAMPLE_PERIOD_S = 1.0 / SAMPLE_RATE_HZ
D7_MASK = 1 << 7
SERIAL_RX_BUFFER_BYTES = 1 << 22    # 4 MB, about 10 s of PPK2 data
PROCESS_MIN_BYTES = 4_000           # convert at least 1000 samples at a time
PROCESS_MAX_WAIT_S = 0.010
LIVE_PERIOD_S = 0.25
GUI_BLOCK = 1_000                   # 10 ms blocks for the live plot
BLOCK_1MS = 100
BLOCK_1S = SAMPLE_RATE_HZ
BLOCKS_1MS_PER_FILE = 3_600_000     # one current_1ms file per hour of samples
ANCHOR_WINDOW = 1_000_000           # 10 s of samples per anchor window
ANCHOR_MIN_READS = 20
ANCHOR_RECENT_READS = 50
ANCHOR_JUMP_S = 0.002               # a sustained step above this splits a window
COUNTER_PROBE = 20_000
D7_NOISE_EDGES = 400                # more edges than this in one chunk = noise
STALL_WARN_S = 3.0
STALL_RECONNECT_S = 10.0
RECONNECT_PERIOD_S = 2.0

EMPTY = np.empty(0, dtype=np.float64)


# ── Device ─────────────────────────────────────────────────────────────────

class Ppk2Device(PPK2_API):  # type: ignore[misc,valid-type]
    """ppk2-api with a metadata read that tolerates stray binary bytes."""

    def _read_metadata(self):
        text = ""
        for _ in range(10):
            read = self.ser.read(self.ser.in_waiting)
            time.sleep(0.1)
            if read:
                text += read.decode("utf-8", errors="ignore")
            if "END" in text:
                return text
        return None


def open_device(port: str, vdd_mv: int, dut_on: bool) -> "Ppk2Device":
    """Open the PPK2, load its calibration, set source mode and start streaming."""
    ppk = Ppk2Device(port, timeout=1, write_timeout=1)
    try:
        if hasattr(ppk.ser, "set_buffer_size"):
            # pyserial defaults to 4 KB on Windows, 10 ms of data at 400 kB/s.
            ppk.ser.set_buffer_size(rx_size=SERIAL_RX_BUFFER_BYTES)
        # A previous session that never reached stop_measuring() leaves the PPK2
        # streaming, and those samples break the metadata read.
        ppk.stop_measuring()
        time.sleep(0.2)
        ppk.ser.reset_input_buffer()
        ok = ppk.get_modifiers()
        if not ok:
            time.sleep(0.3)
            ppk.ser.reset_input_buffer()
            ok = ppk.get_modifiers()
        if not ok:
            raise RuntimeError("PPK2 did not return its calibration data")
        ppk.set_source_voltage(vdd_mv)
        ppk.use_source_meter()
        ppk.toggle_DUT_power("ON" if dut_on else "OFF")
        time.sleep(0.1)
        ppk.ser.reset_input_buffer()
        ppk.start_measuring()
    except Exception:
        try:
            ppk.ser.close()
        except Exception:
            pass
        raise
    return ppk


# ── Stream helpers ─────────────────────────────────────────────────────────

class AnchorTracker:
    """Maps PPK2 sample index to host time without trusting n * 10 us for 24 h.

    Every serial read gives a pair (host time, samples received so far). Its
    offset t - n * 10 us is the true offset plus the USB delay, and the delay is
    never negative, so the smallest offset in a window is the best estimate.
    Windows are 10 s long and close early when the offset steps up for good,
    which is what lost samples look like.
    """

    def __init__(self) -> None:
        self.n_start = 0
        self.min_off = float("inf")
        self.reads = 0
        self.t_first = 0.0
        self.t_last = 0.0
        self.last_n = 0
        self.recent: deque = deque(maxlen=ANCHOR_RECENT_READS)  # (offset, n, t)
        self.last_closed: Optional[Tuple] = None

    def add(self, t: float, n: int) -> Optional[Tuple]:
        off = t - n * SAMPLE_PERIOD_S
        self.recent.append((off, n, t))
        closed = None

        if (self.reads >= ANCHOR_MIN_READS
                and len(self.recent) == self.recent.maxlen
                and min(r[0] for r in self.recent) > self.min_off + ANCHOR_JUMP_S):
            # Sustained step: end the window where the step began and seed the
            # next one with the reads after it.
            first_off, first_n, first_t = self.recent[0]
            closed = self._closed_row(n_end=first_n)
            self.n_start = first_n
            self.min_off = min(r[0] for r in self.recent)
            self.reads = len(self.recent)
            self.t_first = first_t
        elif self.reads > 0 and n - self.n_start >= ANCHOR_WINDOW:
            closed = self._closed_row(n_end=n)
            self.n_start = n
            self.min_off = off
            self.reads = 1
            self.t_first = t
        else:
            if self.reads == 0:
                self.t_first = t
            self.min_off = min(self.min_off, off)
            self.reads += 1

        self.t_last = t
        self.last_n = n
        if closed is not None:
            self.last_closed = closed
        return closed

    def force_close(self) -> Optional[Tuple]:
        """Close the open window, e.g. at a reconnect or when recording stops."""
        if self.reads == 0:
            return None
        closed = self._closed_row(n_end=self.last_n)
        self.last_closed = closed
        self.n_start = self.last_n
        self.min_off = float("inf")
        self.reads = 0
        self.recent.clear()
        return closed

    def open_row(self) -> Optional[Tuple]:
        return self._closed_row(n_end=self.last_n) if self.reads else None

    def _closed_row(self, n_end: int) -> Tuple:
        return (self.n_start, n_end, self.min_off, self.reads, self.t_first, self.t_last)

    def offset(self) -> Optional[float]:
        if self.reads >= ANCHOR_MIN_READS or (self.reads and self.last_closed is None):
            return self.min_off
        if self.last_closed is not None:
            return self.last_closed[2]
        return None


class CounterCheck:
    """Finds lost samples with the 6-bit counter in bits 18-23 of each sample.

    The bit layout follows the nRF Connect Power Profiler. ppk2-api ignores
    these bits, so the check first probes whether they count up by one per
    sample and switches itself off if they do not.
    """

    def __init__(self) -> None:
        self.state = "probing"
        self.prev: Optional[int] = None
        self._ok = 0
        self._total = 0

    def feed(self, cnt: np.ndarray) -> List[Tuple[int, int]]:
        """Return (index in chunk, lost samples mod 64) for every gap."""
        if cnt.size == 0 or self.state == "off":
            return []
        if self.prev is None:
            d = np.diff(cnt) & 63
            base = 1
        else:
            d = np.diff(cnt, prepend=np.int16(self.prev)) & 63
            base = 0
        self.prev = int(cnt[-1])
        if self.state == "probing":
            self._ok += int(np.count_nonzero(d == 1))
            self._total += int(d.size)
            if self._total >= COUNTER_PROBE:
                self.state = "on" if self._ok >= 0.98 * self._total else "off"
            return []
        bad = np.flatnonzero(d != 1)
        return [(int(i) + base, int((int(d[i]) - 1) & 63)) for i in bad]


class PulseTracker:
    """Rising edges and pulse widths on one digital channel, across chunks."""

    def __init__(self) -> None:
        # A channel already high at the first sample is not a new pulse.
        self.prev_high = True
        self.rise_n: Optional[int] = None

    def feed(self, n0: int, high: np.ndarray):
        """Return (rises, pulses, noise_edges); pulses are (n_rise, width)."""
        h = high.astype(np.int8)
        d = np.diff(h, prepend=np.int8(1 if self.prev_high else 0))
        rises = np.flatnonzero(d == 1)
        falls = np.flatnonzero(d == -1)
        self.prev_high = bool(high[-1])
        if rises.size + falls.size > D7_NOISE_EDGES:
            self.rise_n = None
            return [], [], int(rises.size + falls.size)

        events = sorted([(int(i), 1) for i in rises] + [(int(i), -1) for i in falls])
        new_rises: List[int] = []
        pulses: List[Tuple[int, int]] = []
        for i, kind in events:
            n = n0 + i
            if kind == 1:
                self.rise_n = n
                new_rises.append(n)
            elif self.rise_n is not None:
                pulses.append((self.rise_n, n - self.rise_n))
                self.rise_n = None
        return new_rises, pulses, 0


class HistoryRing:
    """The last few seconds of full-rate data, for the snippets around D7."""

    def __init__(self, capacity: int) -> None:
        self.cap = capacity
        self.cur = np.zeros(capacity, dtype=np.float32)
        self.bits = np.zeros(capacity, dtype=np.uint8)
        self.end_n = 0
        self.filled = 0

    def write(self, n0: int, cur: np.ndarray, bits: np.ndarray) -> None:
        k = cur.size
        if k >= self.cap:
            cur, bits = cur[-self.cap:], bits[-self.cap:]
            n0 += k - self.cap
            k = self.cap
        pos = n0 % self.cap
        first = min(k, self.cap - pos)
        self.cur[pos:pos + first] = cur[:first]
        self.bits[pos:pos + first] = bits[:first]
        if k > first:
            self.cur[:k - first] = cur[first:]
            self.bits[:k - first] = bits[first:]
        self.end_n = n0 + k
        self.filled = min(self.cap, self.filled + k)

    def read(self, a: int, b: int) -> Tuple[int, np.ndarray, np.ndarray]:
        a = max(a, self.end_n - self.filled)
        b = min(b, self.end_n)
        idx = np.arange(a, b) % self.cap
        return a, self.cur[idx], self.bits[idx]


class BlockReducer:
    """Mean and max of consecutive fixed-size blocks, across chunk boundaries."""

    def __init__(self, size: int, n_start: int = 0) -> None:
        self.size = size
        self._rem = EMPTY
        self._rem_n0 = n_start

    def feed(self, n0: int, x: np.ndarray) -> Tuple[int, np.ndarray, np.ndarray]:
        if self._rem.size == 0:
            self._rem_n0 = n0
            buf = x
        else:
            buf = np.concatenate((self._rem, x))
        first = self._rem_n0
        nb = buf.size // self.size
        if nb == 0:
            self._rem = buf.copy()
            return first, EMPTY, EMPTY
        blocks = buf[:nb * self.size].reshape(nb, self.size)
        self._rem = buf[nb * self.size:].copy()
        self._rem_n0 = first + nb * self.size
        return first, blocks.mean(axis=1), blocks.max(axis=1)


class SecondStats:
    """Sum, min and max of every 100,000 samples without copying them."""

    def __init__(self, n_start: int) -> None:
        self.sec = 0
        self.n_start = n_start
        self._reset()

    def _reset(self) -> None:
        self.count = 0
        self.sum = 0.0
        self.min = float("inf")
        self.max = float("-inf")

    def feed(self, x: np.ndarray) -> List[list]:
        rows = []
        i = 0
        while i < x.size:
            take = min(BLOCK_1S - self.count, x.size - i)
            seg = x[i:i + take]
            self.sum += float(seg.sum())
            self.min = min(self.min, float(seg.min()))
            self.max = max(self.max, float(seg.max()))
            self.count += take
            i += take
            if self.count == BLOCK_1S:
                rows.append(self.row())
                self.sec += 1
                self.n_start += BLOCK_1S
                self._reset()
        return rows

    def row(self) -> list:
        mean = self.sum / self.count if self.count else 0.0
        return [self.sec, self.n_start, self.count, mean, self.min, self.max,
                self.sum * SAMPLE_PERIOD_S]


# ── Recording ──────────────────────────────────────────────────────────────

def _fmt(v: float) -> str:
    return f"{v:.4f}"


class Recorder:
    """Writes one run's PPK2 files. Everything is flushed every few seconds,
    so a crash late in a 24 h run still leaves the hours before it."""

    def __init__(self, run_dir: Path, rec_start_n: int, raw_csv: bool = False,
                 warn=None, on_file=None) -> None:
        self.dir = Path(run_dir)
        self.snip_dir = self.dir / "snippets"
        self.snip_dir.mkdir(parents=True, exist_ok=True)
        self.rec_start_n = rec_start_n
        self.raw = RawWriter(self.dir, rec_start_n, csv_on=raw_csv,
                             warn=warn or (lambda _text: None), on_file=on_file)
        self._ms = BlockReducer(BLOCK_1MS, rec_start_n)
        self._ms_blocks = 0
        self._ms_file = None
        self._ms_file_idx = -1
        self.ms_files: List[str] = []
        self._sec = SecondStats(rec_start_n)
        self.new_seconds: List[Tuple[int, float]] = []
        self._files = []
        self._w_sum = self._csv("summary_1s.csv", [
            "sec", "n_start", "samples", "mean_uA", "min_uA", "max_uA", "charge_uC"])
        self._w_anc = self._csv("ppk_anchors.csv", [
            "n_start", "n_end", "offset_s", "reads", "t_first", "t_last"])
        self._w_pul = self._csv("d7_pulses.csv", [
            "rise_no", "n", "n_rel", "width_samples", "glitch", "t_host_est", "snippet"])
        self._w_loss = self._csv("ppk_loss.csv", [
            "n", "n_rel", "lost_samples", "source", "t_host"])
        self.total_sum = 0.0
        self.total_n = 0
        self.pulses = 0
        self.glitches = 0
        self.snippets = 0
        self.losses = 0
        self.lost = 0
        self._last_flush = time.perf_counter()

    def _csv(self, name: str, header: List[str]):
        f = open(self.dir / name, "w", newline="", encoding="utf-8")
        w = csv.writer(f)
        w.writerow(header)
        self._files.append(f)
        return w

    def feed(self, n0: int, cur: np.ndarray, bits: np.ndarray) -> None:
        self.raw.feed(cur, bits)
        first, means, _ = self._ms.feed(n0, cur)
        if means.size:
            self._write_ms(means)
        for row in self._sec.feed(cur):
            self._write_second(row)
        self.total_sum += float(cur.sum())
        self.total_n += int(cur.size)
        now = time.perf_counter()
        if now - self._last_flush >= 5.0:
            self.flush()
            self._last_flush = now

    def _write_ms(self, means: np.ndarray) -> None:
        data = means.astype("<f4")
        i = 0
        while i < data.size:
            idx = self._ms_blocks // BLOCKS_1MS_PER_FILE
            if idx != self._ms_file_idx:
                if self._ms_file is not None:
                    self._ms_file.close()
                name = f"current_1ms_{idx:02d}.f32"
                self._ms_file = open(self.dir / name, "ab")
                self._ms_file_idx = idx
                self.ms_files.append(name)
            space = (idx + 1) * BLOCKS_1MS_PER_FILE - self._ms_blocks
            take = min(space, data.size - i)
            self._ms_file.write(data[i:i + take].tobytes())
            self._ms_blocks += take
            i += take

    def _write_second(self, row: list) -> None:
        sec, n_start, count, mean, mn, mx, charge = row
        self._w_sum.writerow([sec, n_start, count, _fmt(mean), _fmt(mn), _fmt(mx), _fmt(charge)])
        self.new_seconds.append((sec, mean))

    def write_anchor(self, row: Tuple) -> None:
        n_start, n_end, off, reads, t_first, t_last = row
        self._w_anc.writerow([n_start, n_end, f"{off:.9f}", reads, f"{t_first:.6f}", f"{t_last:.6f}"])

    def write_pulse(self, rise_no: int, n: int, width: Optional[int], glitch: bool,
                    t_est: Optional[float], snippet: str) -> None:
        self.pulses += 0 if glitch else 1
        self.glitches += 1 if glitch else 0
        self._w_pul.writerow([rise_no, n, n - self.rec_start_n,
                              "" if width is None else width, int(glitch),
                              "" if t_est is None else f"{t_est:.6f}", snippet])

    def write_loss(self, n: int, lost: int, source: str, t: float) -> None:
        self.losses += 1
        self.lost += lost
        self._w_loss.writerow([n, n - self.rec_start_n, lost, source, f"{t:.6f}"])

    def take_seconds(self) -> List[Tuple[int, float]]:
        out, self.new_seconds = self.new_seconds, []
        return out

    def flush(self) -> None:
        for f in self._files:
            f.flush()
        if self._ms_file is not None:
            self._ms_file.flush()

    def close(self, n_end: int, extra: dict) -> dict:
        raw = self.raw.close()
        if self._sec.count:
            self._write_second(self._sec.row())  # partial last second
        if self._ms_file is not None:
            self._ms_file.close()
            self._ms_file = None
        for f in self._files:
            f.close()
        avg = self.total_sum / self.total_n if self.total_n else 0.0
        charge_uc = self.total_sum * SAMPLE_PERIOD_S
        meta = {
            "rec_start_n": self.rec_start_n,
            "rec_end_n": n_end,
            "samples": self.total_n,
            "duration_s_by_samples": self.total_n * SAMPLE_PERIOD_S,
            "avg_uA": avg,
            "charge_uC": charge_uc,
            "charge_mAh": charge_uc / 3.6e6,
            "sample_period_s": SAMPLE_PERIOD_S,
            "current_1ms": {"files": self.ms_files, "blocks": self._ms_blocks,
                            "samples_per_block": BLOCK_1MS,
                            "blocks_per_file": BLOCKS_1MS_PER_FILE,
                            "dtype": "float32 little-endian, uA"},
            "pulses": self.pulses,
            "glitches": self.glitches,
            "snippets": self.snippets,
            "loss_events": self.losses,
            "lost_samples_counter": self.lost,
            "raw": raw,
        }
        meta.update(extra)
        with open(self.dir / "ppk_meta.json", "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2)
        return meta


# ── Worker ─────────────────────────────────────────────────────────────────

class CaptureWorker:
    def __init__(self, evt_q, opts: dict) -> None:
        self.evt_q = evt_q
        self.pre_n = int(float(opts.get("snippet_pre_s", 1.0)) * SAMPLE_RATE_HZ)
        self.post_n = int(float(opts.get("snippet_post_s", 1.0)) * SAMPLE_RATE_HZ)
        self.max_snippets = int(opts.get("max_snippets", 1000))
        self.min_pulse = int(opts.get("min_pulse_samples", 20))
        self.ppk: Optional[Ppk2Device] = None
        self.port = ""
        self.vdd_mv = 3300
        self.dut_on = False
        self.measuring = False
        self.reconnecting = False
        self.next_reconnect = 0.0
        self.rec: Optional[Recorder] = None
        self.modifiers: dict = {}
        self._reset_stream()

    # -- plumbing --

    def _send(self, kind: str, data=None) -> None:
        self.evt_q.put((kind, data))

    def _log(self, text: str) -> None:
        self._send("log", text)

    def _reset_stream(self) -> None:
        self.pending = b""
        self.bytes_total = 0
        self.n_proc = 0
        self.anchors = AnchorTracker()
        self.counter = CounterCheck()
        self.pulses = PulseTracker()
        self.hist = HistoryRing(self.pre_n + self.post_n + 2 * SAMPLE_RATE_HZ)
        self.live = BlockReducer(GUI_BLOCK)
        self.live_n0: Optional[int] = None
        self.live_mean: List[np.ndarray] = []
        self.live_max: List[np.ndarray] = []
        self.rate_hist: deque = deque()
        now = time.perf_counter()
        self.last_proc = now
        self.last_live = 0.0
        self.t_last_data = now
        self.stall_warned = False
        self.loss_events = 0
        self.lost_samples = 0
        self.rise_count = 0
        self.rise_numbers: Dict[int, int] = {}
        self.pending_snips: Dict[int, int] = {}  # n_rise -> rise_no
        self.noise_warned_at = 0.0
        self.last_1s_uA: Optional[float] = None
        self.size_mismatch_warned = False
        self._reset_logic_stats()
        self.prev_bits_byte: Optional[int] = None

    def _reset_logic_stats(self) -> None:
        """Per-channel activity of D0-D7 since the last live message."""
        self.logic_edges = np.zeros(8, dtype=np.int64)
        self.logic_or = 0
        self.logic_and = 0xFF
        self.logic_samples = 0

    # -- main loop --

    def run(self, cmd_q) -> None:
        if PPK2_IMPORT_ERROR:
            self._send("error", f"ppk2-api / pyserial not available: {PPK2_IMPORT_ERROR}")
        parent = mp.parent_process()
        next_parent_check = 0.0
        while True:
            try:
                while True:
                    if not self._handle(cmd_q.get_nowait()):
                        return
            except queue.Empty:
                pass

            now = time.perf_counter()
            if now >= next_parent_check:
                next_parent_check = now + 1.0
                if parent is not None and not parent.is_alive():
                    self._log("GUI process is gone, closing the PPK2")
                    return

            if self.reconnecting:
                self._try_reconnect(now)
                time.sleep(0.05)
                continue
            if not self.measuring:
                time.sleep(0.02)
                continue

            try:
                waiting = self.ppk.ser.in_waiting
                data = self.ppk.ser.read(waiting) if waiting else b""
            except Exception as exc:  # serial.SerialException, OSError
                self._link_lost(f"{exc}")
                continue

            now = time.perf_counter()
            if data:
                self.pending += data
                self.bytes_total += len(data)
                self.t_last_data = now
                self.stall_warned = False
                closed = self.anchors.add(now, self.bytes_total // 4)
                if closed is not None and self.rec is not None:
                    self.rec.write_anchor(closed)
            elif now - self.t_last_data > STALL_RECONNECT_S:
                self._link_lost(f"no data for {now - self.t_last_data:.0f} s")
                continue
            elif now - self.t_last_data > STALL_WARN_S and not self.stall_warned:
                self.stall_warned = True
                self._send("warn", f"PPK2 stream silent for {now - self.t_last_data:.1f} s")

            if len(self.pending) >= PROCESS_MIN_BYTES or (
                    self.pending and now - self.last_proc >= PROCESS_MAX_WAIT_S):
                self._process()
                self.last_proc = now
            if now - self.last_live >= LIVE_PERIOD_S:
                self._send_live(now)
                self.last_live = now
            # About 400 bytes arrive per ms; reading more often only adds overhead.
            time.sleep(0.001)

    def shutdown(self) -> None:
        try:
            self._disconnect(send=False)
        except Exception:
            pass

    # -- commands --

    def _handle(self, cmd) -> bool:
        kind = cmd[0]
        try:
            if kind == "connect":
                self._connect(cmd[1], int(cmd[2]))
            elif kind == "dut":
                self._set_dut(bool(cmd[1]))
            elif kind == "rec_start":
                self._rec_start(cmd[1])
            elif kind == "rec_stop":
                self._rec_stop()
            elif kind == "disconnect":
                self._disconnect()
            elif kind == "quit":
                self._disconnect(send=False)
                return False
        except Exception as exc:
            self._send("error", f"{kind}: {exc}")
        return True

    def _connect(self, port: str, vdd_mv: int) -> None:
        if self.ppk is not None or self.reconnecting:
            self._disconnect(send=False)
        self.port, self.vdd_mv = port, vdd_mv
        self.ppk = open_device(port, vdd_mv, dut_on=False)
        self.dut_on = False
        self.modifiers = _jsonable_modifiers(self.ppk.modifiers)
        self._reset_stream()
        self.measuring = True
        self._send("connected", {"port": port, "vdd_mv": vdd_mv, "t": time.perf_counter()})
        self._send("dut", {"on": False, "t": time.perf_counter()})

    def _set_dut(self, on: bool) -> None:
        if self.ppk is None:
            raise RuntimeError("PPK2 is not connected")
        self.ppk.toggle_DUT_power("ON" if on else "OFF")
        self.dut_on = on
        self._send("dut", {"on": on, "t": time.perf_counter()})

    def _rec_start(self, params: dict) -> None:
        if not self.measuring:
            raise RuntimeError("PPK2 is not measuring")
        if self.rec is not None:
            self._rec_stop()
        self._process()
        self.rec = Recorder(Path(params["run_dir"]), self.n_proc,
                            raw_csv=bool(params.get("raw_csv", False)),
                            warn=lambda text: self._send("warn", text),
                            on_file=lambda info: self._send("raw_file", info))
        self.pending_snips.clear()
        self._send("rec_started", {"rec_start_n": self.n_proc, "t": time.perf_counter(),
                                   "offset": self.anchors.offset(),
                                   "counter": self.counter.state})

    def _rec_stop(self) -> None:
        if self.rec is None:
            self._send("rec_stopped", None)
            return
        if self.measuring:
            self._process()
        # Snippets still waiting for their post-trigger part are saved short.
        for n_rise, rise_no in list(self.pending_snips.items()):
            self._save_snippet(n_rise, rise_no)
        self.pending_snips.clear()
        open_row = self.anchors.open_row()
        if open_row is not None:
            self.rec.write_anchor(open_row)
        meta = self.rec.close(self.n_proc, {
            "port": self.port,
            "vdd_mv": self.vdd_mv,
            "counter_check": self.counter.state,
            "modifiers": self.modifiers,
        })
        self.rec = None
        self._send("rec_stopped", meta)

    def _disconnect(self, send: bool = True) -> None:
        if self.rec is not None:
            self._rec_stop()
        if self.ppk is not None:
            try:
                self.ppk.stop_measuring()
                self.ppk.toggle_DUT_power("OFF")
                time.sleep(0.1)
            except Exception:
                pass
            try:
                self.ppk.ser.close()
            except Exception:
                pass
        self.ppk = None
        self.measuring = False
        self.reconnecting = False
        self.dut_on = False
        if send:
            self._send("disconnected", None)

    # -- link loss --

    def _link_lost(self, reason: str) -> None:
        self._send("warn", f"PPK2 link lost ({reason}), reconnecting every "
                           f"{RECONNECT_PERIOD_S:.0f} s")
        try:
            # Bytes already received are valid; converting them keeps the
            # sample count equal to what the anchors have seen.
            self._process()
        except Exception:
            pass
        if self.rec is not None:
            self.rec.flush()
        try:
            self.ppk.ser.close()
        except Exception:
            pass
        self.ppk = None
        self.measuring = False
        self.reconnecting = True
        self.next_reconnect = time.perf_counter() + RECONNECT_PERIOD_S
        self._send("link", {"state": "reconnecting"})

    def _try_reconnect(self, now: float) -> None:
        if now < self.next_reconnect:
            return
        self.next_reconnect = now + RECONNECT_PERIOD_S
        try:
            self.ppk = open_device(self.port, self.vdd_mv, dut_on=self.dut_on)
        except Exception:
            self.ppk = None
            return
        now = time.perf_counter()
        gap_s = now - self.t_last_data
        # Sample numbering continues; the anchors see the gap as an offset step.
        self.pending = b""
        self.bytes_total = self.n_proc * 4
        closed = self.anchors.force_close()
        if closed is not None and self.rec is not None:
            self.rec.write_anchor(closed)
        self._loss(self.n_proc, int(gap_s * SAMPLE_RATE_HZ), "reconnect", now)
        self.counter = CounterCheck()
        self.pulses.rise_n = None
        self.t_last_data = now
        self.reconnecting = False
        self.measuring = True
        self._send("warn", f"PPK2 reconnected after {gap_s:.1f} s, DUT power "
                           f"{'ON' if self.dut_on else 'OFF'} restored")
        self._send("link", {"state": "ok"})

    def _loss(self, n: int, lost: int, source: str, t: float) -> None:
        self.loss_events += 1
        self.lost_samples += lost
        if self.rec is not None:
            self.rec.write_loss(n, lost, source, t)
        self._send("loss", {"n": n, "lost": lost, "source": source, "t": t})

    # -- data path --

    def _process(self) -> None:
        nbytes = len(self.pending) & ~3
        if nbytes == 0:
            return
        buf = self.pending[:nbytes]
        self.pending = self.pending[nbytes:]
        raw = np.frombuffer(buf, dtype="<u4")
        samples, _ = self.ppk.get_samples(buf)
        cur = np.asarray(samples, dtype=np.float64)
        if cur.size != raw.size:
            # ppk2-api drops a sample it cannot convert; keep indices aligned.
            if not self.size_mismatch_warned:
                self.size_mismatch_warned = True
                self._send("warn", f"PPK2 conversion returned {cur.size} of {raw.size} samples")
            fill = cur[-1] if cur.size else 0.0
            cur = np.concatenate((cur, np.full(raw.size - cur.size, fill)))[:raw.size]
        bits = (raw >> 24).astype(np.uint8)
        n0 = self.n_proc
        self.n_proc += raw.size
        t_now = time.perf_counter()

        for i, lost in self.counter.feed(((raw >> 18) & 0x3F).astype(np.int16)):
            self._loss(n0 + i, lost, "counter", t_now)

        self.hist.write(n0, cur, bits)
        self._logic_stats(bits)

        rises, pulses, noise = self.pulses.feed(n0, (bits & D7_MASK) != 0)
        if noise:
            self.rise_numbers.clear()
        if noise and t_now - self.noise_warned_at > 10.0:
            self.noise_warned_at = t_now
            self._send("warn", f"D7 is noisy ({noise} edges in {raw.size / 100:.0f} ms), "
                               "check the logic port VCC and GND")
        for n_rise in rises:
            self._on_rise(n_rise)
        for n_rise, width in pulses:
            self._on_pulse(n_rise, width)
        self._flush_snippets()

        first, means, maxs = self.live.feed(n0, cur)
        if means.size:
            if self.live_n0 is None:
                self.live_n0 = first
            self.live_mean.append(means.astype(np.float32))
            self.live_max.append(maxs.astype(np.float32))

        if self.rec is not None:
            self.rec.feed(n0, cur, bits)

    def _logic_stats(self, bits: np.ndarray) -> None:
        """Edges and levels on every logic channel, to show in the GUI which
        of D0-D7 actually sees a signal (wiring check)."""
        if bits.size == 0:
            return
        seq = bits if self.prev_bits_byte is None else np.concatenate(
            (np.array([self.prev_bits_byte], dtype=np.uint8), bits))
        changed = np.bitwise_xor(seq[1:], seq[:-1])
        if changed.any():
            self.logic_edges += np.unpackbits(changed[changed != 0][:, None], axis=1,
                                              bitorder="little").sum(axis=0, dtype=np.int64)
        self.logic_or |= int(np.bitwise_or.reduce(bits))
        self.logic_and &= int(np.bitwise_and.reduce(bits))
        self.logic_samples += int(bits.size)
        self.prev_bits_byte = int(bits[-1])

    def _on_rise(self, n_rise: int) -> None:
        self.rise_count += 1
        self.rise_numbers[n_rise] = self.rise_count
        if (self.rec is not None
                and self.rec.snippets + len(self.pending_snips) < self.max_snippets):
            self.pending_snips[n_rise] = self.rise_count

    def _on_pulse(self, n_rise: int, width: int) -> None:
        rise_no = self.rise_numbers.pop(n_rise, 0)
        glitch = width < self.min_pulse
        snippet = ""
        if n_rise in self.pending_snips:
            if glitch:
                del self.pending_snips[n_rise]
            else:
                snippet = f"snippets/d7_{rise_no:05d}.npz"
        off = self.anchors.offset()
        t_est = None if off is None else n_rise * SAMPLE_PERIOD_S + off
        if self.rec is not None:
            self.rec.write_pulse(rise_no, n_rise, width, glitch, t_est, snippet)
        self._send("pulse", {
            "rise_no": rise_no, "n": n_rise,
            "n_rel": None if self.rec is None else n_rise - self.rec.rec_start_n,
            "width": width, "glitch": glitch, "t_est": t_est,
            "snippet": snippet, "recording": self.rec is not None,
        })

    def _flush_snippets(self) -> None:
        for n_rise, rise_no in list(self.pending_snips.items()):
            if self.n_proc >= n_rise + self.post_n:
                self._save_snippet(n_rise, rise_no)
                del self.pending_snips[n_rise]

    def _save_snippet(self, n_rise: int, rise_no: int) -> None:
        if self.rec is None:
            return
        a, cur, bits = self.hist.read(n_rise - self.pre_n, n_rise + self.post_n)
        np.savez(self.rec.snip_dir / f"d7_{rise_no:05d}.npz",
                 current_uA=cur, bits=bits, n_start=np.int64(a),
                 n_rise=np.int64(n_rise), n_rel_start=np.int64(a - self.rec.rec_start_n),
                 sample_period_s=np.float64(SAMPLE_PERIOD_S))
        self.rec.snippets += 1

    def _send_live(self, now: float) -> None:
        received = self.bytes_total // 4
        self.rate_hist.append((now, received))
        while len(self.rate_hist) > 2 and now - self.rate_hist[0][0] > 5.0:
            self.rate_hist.popleft()
        t0, n0 = self.rate_hist[0]
        rate = (received - n0) / (now - t0) if now > t0 else 0.0

        msg: dict = {
            "t": now, "n": self.n_proc, "offset": self.anchors.offset(), "rate": rate,
            "counter": self.counter.state, "losses": self.loss_events,
            "lost": self.lost_samples, "rises": self.rise_count, "dut": self.dut_on,
            "g_n0": self.live_n0, "g_block": GUI_BLOCK,
            "g_mean": (np.concatenate(self.live_mean) if self.live_mean
                       else np.empty(0, np.float32)),
            "g_max": (np.concatenate(self.live_max) if self.live_max
                      else np.empty(0, np.float32)),
            "rec": None,
            "logic": {"edges": self.logic_edges.tolist(), "or": self.logic_or,
                      "and": self.logic_and, "samples": self.logic_samples},
        }
        self._reset_logic_stats()
        self.live_n0 = None
        self.live_mean, self.live_max = [], []
        if self.rec is not None:
            r = self.rec
            msg["rec"] = {
                "samples": r.total_n,
                "avg_uA": r.total_sum / r.total_n if r.total_n else 0.0,
                "charge_uC": r.total_sum * SAMPLE_PERIOD_S,
                "seconds": r.take_seconds(),
                "snippets": r.snippets,
                "losses": r.losses,
                "raw": r.raw.stats(),
            }
        self._send("live", msg)


def _jsonable_modifiers(mod: dict) -> dict:
    out = {}
    for k, v in mod.items():
        if isinstance(v, dict):
            out[k] = {kk: (float(vv) if isinstance(vv, (int, float)) else str(vv))
                      for kk, vv in v.items()}
        else:
            out[k] = None if v is None else str(v).strip()
    return out


def worker_main(cmd_q, evt_q, opts: dict) -> None:
    worker = CaptureWorker(evt_q, opts)
    try:
        worker.run(cmd_q)
    except Exception:
        evt_q.put(("fatal", traceback.format_exc()))
    finally:
        worker.shutdown()
        evt_q.put(("exited", None))


# ── GUI-side handle ────────────────────────────────────────────────────────

class Ppk2Client:
    """Starts the capture process and passes commands and events."""

    def __init__(self, opts: dict, target=None) -> None:
        ctx = mp.get_context("spawn")
        self.cmd_q = ctx.Queue()
        self.evt_q = ctx.Queue()
        self.proc = ctx.Process(target=target or worker_main,
                                args=(self.cmd_q, self.evt_q, opts),
                                name="ppk2-capture", daemon=True)
        self.proc.start()

    def send(self, *cmd) -> None:
        self.cmd_q.put(cmd)

    def poll(self, limit: int = 500) -> list:
        out = []
        for _ in range(limit):
            try:
                out.append(self.evt_q.get_nowait())
            except queue.Empty:
                break
        return out

    def close(self, timeout: float = 5.0) -> None:
        if self.proc.is_alive():
            self.send("quit")
            self.proc.join(timeout)
        if self.proc.is_alive():
            self.proc.terminate()


def list_ppk2_ports() -> List[str]:
    try:
        return list(PPK2_API.list_devices())
    except Exception:
        return []
