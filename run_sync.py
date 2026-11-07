"""STM32 run control and the time maps shared by the GUI and analyze_run.py.

Every timestamp in a run is time.perf_counter() on this PC. Two maps tie the
devices to it:
  PlayClock  host time <-> samples the STM32 has played. When the STM32
             answers UPDATE it has moved cursor - ring_samples samples from
             its ring into the MMA8451 FIFO, which is the moment a real sensor
             would have produced them.
  AnchorMap  PPK2 sample index -> host time, from ppk2_capture's anchors.
"""

from __future__ import annotations

import bisect
import csv
import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Set, Tuple

import numpy as np

TOOL_DIR = Path(__file__).resolve().parent.parent / "Tool_send_data_fall_numpy"
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

import vimu_uart_e2e_test as vimu  # noqa: E402

RATE_HZ = vimu.SAMPLE_RATE_HZ          # 50 Hz playback
PPK2_PERIOD_S = 1e-5                   # 100 kS/s


# ── Time formatting ────────────────────────────────────────────────────────

def parse_hms(text: str) -> float:
    """'hh:mm:ss', 'mm:ss' or plain seconds -> seconds."""
    text = text.strip()
    if not text:
        raise ValueError("empty time")
    parts = text.split(":")
    if len(parts) > 3:
        raise ValueError(f"bad time '{text}'")
    total = 0.0
    for part in parts:
        total = total * 60 + float(part)
    if total < 0:
        raise ValueError("negative time")
    return total


def fmt_hms(seconds: Optional[float], decimals: int = 0) -> str:
    if seconds is None:
        return "--:--:--"
    sign = "-" if seconds < 0 else ""
    s = abs(seconds)
    h = int(s // 3600)
    m = int((s % 3600) // 60)
    sec = s - h * 3600 - m * 60
    if decimals:
        return f"{sign}{h:02d}:{m:02d}:{sec:0{3 + decimals}.{decimals}f}"
    return f"{sign}{h:02d}:{m:02d}:{int(sec):02d}"


# ── NPZ data with labels ───────────────────────────────────────────────────

@dataclass
class NpzRunData:
    path: str
    samples: np.ndarray            # (N, 3) int16 raw16 at 50 Hz
    fall_ids: np.ndarray           # (N,) uint16, 0 = no fall
    episodes: list                 # vimu.FallEpisode, indices at 50 Hz
    stride: int                    # source samples per 50 Hz sample
    category: Optional[np.ndarray]
    category_names: List[str]
    task: Optional[np.ndarray]
    manifest: list
    manifest_starts: List[int]     # source-rate start_idx of each entry

    @property
    def n(self) -> int:
        return len(self.samples)

    def labels_at(self, idx: int) -> dict:
        idx = min(max(int(idx), 0), self.n - 1)
        out = {"category": "", "task": "", "phase": "", "episode_id": "",
               "fall_id": int(self.fall_ids[idx])}
        if self.category is not None:
            c = int(self.category[idx])
            out["category"] = self.category_names[c] if c < len(self.category_names) else str(c)
        entry = self._manifest_entry(idx)
        if entry is not None:
            out["phase"] = str(entry.get("phase") or "")
            out["task"] = str(entry.get("task") or "")
            out["episode_id"] = str(entry.get("episode_id") or "")
        elif self.task is not None:
            out["task"] = str(int(self.task[idx]))
        return out

    def _manifest_entry(self, idx: int) -> Optional[dict]:
        if not self.manifest:
            return None
        src = idx * self.stride
        j = bisect.bisect_right(self.manifest_starts, src) - 1
        if j >= 0 and src < int(self.manifest[j].get("end_idx", 0)):
            return self.manifest[j]
        return None


def load_npz_run_data(path: str) -> NpzRunData:
    samples, fall_ids, episodes = vimu.load_npz_accel_samples(path)
    n = len(samples)
    stride = 1
    category = None
    names: List[str] = []
    task = None
    manifest: list = []
    with np.load(path, allow_pickle=True) as d:
        src_rate = RATE_HZ
        if "metadata_json" in d.files:
            try:
                src_rate = int(json.loads(str(d["metadata_json"])).get("sample_rate_hz", RATE_HZ))
            except (ValueError, TypeError):
                pass
        stride = max(1, round(src_rate / RATE_HZ))  # same rule as the vimu loader
        if "y_category" in d.files:
            category = np.asarray(d["y_category"])[::stride][:n].copy()
        if "category_names" in d.files:
            names = [str(c) for c in d["category_names"]]
        if "y_task" in d.files:
            task = np.asarray(d["y_task"])[::stride][:n].copy()
        if "manifest_json" in d.files:
            try:
                manifest = json.loads(str(d["manifest_json"]))
            except (ValueError, TypeError):
                manifest = []
    manifest = sorted(manifest, key=lambda e: int(e.get("start_idx", 0)))
    return NpzRunData(
        path=str(path), samples=samples, fall_ids=fall_ids, episodes=episodes,
        stride=stride, category=category, category_names=names, task=task,
        manifest=manifest, manifest_starts=[int(e.get("start_idx", 0)) for e in manifest],
    )


def classify_pulse(npz_idx: int, run_start: int, episodes: list, tol_samples: int,
                   warmup_samples: int, detected: Set[int]) -> Tuple[str, int]:
    """TP for the first pulse inside [fall start, fall end + tolerance], DUP for
    later ones, FP outside every fall, WARMUP while the model window still
    holds data from before the run."""
    if npz_idx - run_start < warmup_samples:
        return "WARMUP", 0
    for ep in episodes:
        if ep.start_sample <= npz_idx < ep.end_sample + tol_samples:
            if ep.fall_id in detected:
                return "DUP", ep.fall_id
            detected.add(ep.fall_id)
            return "TP", ep.fall_id
    return "FP", 0


# ── Time maps ──────────────────────────────────────────────────────────────

class PlayClock:
    """Host time <-> samples played by the STM32, interpolated between polls."""

    def __init__(self) -> None:
        self._t: List[float] = []
        self._p: List[float] = []
        self._arr: Optional[Tuple[np.ndarray, np.ndarray]] = None

    def __len__(self) -> int:
        return len(self._t)

    def add(self, t: float, played: float) -> None:
        if self._t and t <= self._t[-1]:
            return
        self._t.append(float(t))
        self._p.append(float(played))
        self._arr = None

    def last(self) -> Optional[Tuple[float, float]]:
        return (self._t[-1], self._p[-1]) if self._t else None

    def _arrays(self) -> Tuple[np.ndarray, np.ndarray]:
        if self._arr is None:
            self._arr = (np.asarray(self._t), np.asarray(self._p))
        return self._arr

    def played_at(self, t: float) -> Optional[float]:
        if not self._t:
            return None
        # Live lookups are almost always in the newest segment; answer those
        # without rebuilding the arrays (24 h = 170k points).
        if t >= self._t[-1]:
            return self._p[-1] + (t - self._t[-1]) * RATE_HZ
        if len(self._t) >= 2 and t >= self._t[-2]:
            t0, t1, p0, p1 = self._t[-2], self._t[-1], self._p[-2], self._p[-1]
            return p0 + (p1 - p0) * (t - t0) / (t1 - t0)
        ts, ps = self._arrays()
        if t <= ts[0]:
            return ps[0] - (ts[0] - t) * RATE_HZ
        if t >= ts[-1]:
            return ps[-1] + (t - ts[-1]) * RATE_HZ
        return float(np.interp(t, ts, ps))

    def time_of(self, played: float) -> Optional[float]:
        """First host time at which `played` samples had been played."""
        if not self._t:
            return None
        ts, ps = self._arrays()
        if played <= ps[0]:
            return ts[0] - (ps[0] - played) / RATE_HZ
        if played >= ps[-1]:
            return ts[-1] + (played - ps[-1]) / RATE_HZ
        j = int(np.searchsorted(ps, played, side="left"))
        p0, p1, t0, t1 = ps[j - 1], ps[j], ts[j - 1], ts[j]
        return t0 + (t1 - t0) * (played - p0) / (p1 - p0) if p1 > p0 else t1


class AnchorMap:
    """PPK2 sample index -> host time, from ppk_anchors.csv.

    Between neighbouring windows whose offsets differ by less than STEP_S the
    offset is interpolated (PPK2 crystal drift is a slow ramp: 6 ms per 10 s
    window covers 600 ppm). A larger difference is a step from lost samples
    and is kept as a step.
    """

    STEP_S = 0.006

    def __init__(self, n_start: np.ndarray, n_end: np.ndarray, offset: np.ndarray) -> None:
        order = np.argsort(n_start, kind="stable")
        self.n_start = np.asarray(n_start, dtype=np.int64)[order]
        self.n_mid = (self.n_start + np.asarray(n_end, dtype=np.int64)[order]) / 2.0
        self.offset = np.asarray(offset, dtype=np.float64)[order]

    @classmethod
    def load(cls, path: Path) -> Optional["AnchorMap"]:
        if not Path(path).exists():
            return None
        n_start, n_end, offset = [], [], []
        with open(path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                n_start.append(int(row["n_start"]))
                n_end.append(int(row["n_end"]))
                offset.append(float(row["offset_s"]))
        if not n_start:
            return None
        return cls(np.array(n_start), np.array(n_end), np.array(offset))

    def offset_at(self, n: int) -> float:
        last = len(self.n_start) - 1
        i = min(max(int(np.searchsorted(self.n_start, n, side="right")) - 1, 0), last)
        j = i - 1 if n < self.n_mid[i] else i + 1
        if 0 <= j <= last and abs(self.offset[j] - self.offset[i]) < self.STEP_S:
            a, b = (j, i) if j < i else (i, j)
            if self.n_mid[b] > self.n_mid[a]:
                f = (n - self.n_mid[a]) / (self.n_mid[b] - self.n_mid[a])
                return float(self.offset[a] + f * (self.offset[b] - self.offset[a]))
        return float(self.offset[i])

    def t_of(self, n: int) -> float:
        return n * PPK2_PERIOD_S + self.offset_at(n)


# ── STM32 run thread ───────────────────────────────────────────────────────

@dataclass
class SyncPoint:
    t: float          # host time (midpoint of the UPDATE round trip)
    played: int       # samples played since GO
    cursor: int       # next NPZ index to send
    ring: int         # samples still in the STM32 ring
    state: int
    flags: int
    rtt_ms: float


SYNC_HEADER = ["t_host", "t_run_s", "played", "cursor", "ring_samples",
               "npz_idx", "state", "flags", "rtt_ms"]


class RunMonitorThread(vimu.MonitorThread):
    """vimu's MonitorThread plus the ARM / GO / STOP steps of a timed run.

    Commands are tuples pushed with push_cmd():
      ("run_arm", {"start", "end", "sync_csv"})  STOP, CLEAR, prefill the ring
      ("run_go", None)                            START, the run clock begins
      ("run_stop", None)                          STOP, close the sync log
      ("run_resume", state)                       continue after a UART reconnect
    Signals go to the status queue as tuples: run_armed, run_go, sync,
    run_event, run_complete, run_stopped, run_error.
    """

    def __init__(self, port: str, baudrate: int, poll_interval: float,
                 status_q, log_q, refill_threshold_s: int = 7) -> None:
        super().__init__(port, baudrate, poll_interval, status_q, log_q)
        self.refill_threshold_s = refill_threshold_s
        self._run_cfg: Optional[dict] = None
        self._run_active = False
        self._t_go: Optional[float] = None
        self._sync_path: Optional[str] = None
        self._sync_file = None
        self._sync_writer = None
        self._sync_rows = 0
        self._last_sp: Optional[SyncPoint] = None
        self._was_running = True
        self._underrun = False
        self._stalled = False
        self._complete = False

    def run(self) -> None:
        try:
            super().run()
        finally:
            self._close_sync_file()

    # -- command dispatch --

    def _exec_cmd(self, io, cmd) -> None:
        if not isinstance(cmd, tuple):
            super()._exec_cmd(io, cmd)
            return
        name, arg = cmd
        if name == "run_stop":
            self._run_stop(io)
            return
        try:
            if name == "run_arm":
                self._run_arm(io, arg)
            elif name == "run_go":
                self._run_go(io)
            elif name == "run_resume":
                self._run_resume(arg)
        except Exception as exc:
            self._log(f"[ERROR] {name}: {exc}")
            self._sq.put(("run_error", f"{name}: {exc}"))

    def _ack_cmd(self, io, code: int, name: str, timeout: float = 1.5):
        pred = (lambda f, c=code: f.cmd == c and f.packet_type in (
            vimu.VIMU_UART_PKT_TYPE_ACK, vimu.VIMU_UART_PKT_TYPE_NACK))
        io.pop_pending_frames(pred)
        io.send_command(code)
        frame = io.wait_for_frame(pred, timeout=timeout, description=f"{name} ACK")
        if frame.packet_type == vimu.VIMU_UART_PKT_TYPE_NACK:
            err = frame.payload[0] if frame.payload else 0
            raise RuntimeError(f"{name} NACK (err=0x{err:02X})")
        return frame

    # -- run steps --

    def _run_arm(self, io, cfg: dict) -> None:
        if self._data_samples is None:
            raise RuntimeError("no NPZ data in the UART thread")
        self._close_sync_file()
        self._run_active = False
        self._run_cfg = None
        self.set_auto_refill(False)

        # STOP over UART only changes the STM32 state; the emulated CTRL_REG1
        # keeps ACTIVE, so the ESP32 ULP health check does not re-init.
        self._ack_cmd(io, vimu.VIMU_UART_CMD_STOP, "STOP")
        self._ack_cmd(io, vimu.VIMU_UART_CMD_CLEAR, "CLEAR")
        self._drain_events(io)

        start, end = int(cfg["start"]), int(cfg["end"])
        self._run_cfg = {"start": start, "end": end}
        # The STM32 keeps its last NEW_DATA sequence across CLEAR, so the
        # sequence counter keeps counting; only the cursor is reset.
        self._sample_cursor = start
        self._last_fall_id_sent = None
        prefill = min(vimu.RING_CAPACITY, end - start)
        self._log(f"[RUN] ARM: prefill {prefill} samples from NPZ index {start}")
        self._send_new_data_frames(io, prefill)

        status = self._poll_update(io)
        if status is None:
            raise RuntimeError("no UPDATE response after the prefill")
        if self._sample_cursor != start + prefill:
            raise RuntimeError(f"prefill stopped at {self._sample_cursor - start}/{prefill} samples")
        if status.state != vimu.VIMU_STATE_STOPPED:
            raise RuntimeError(f"STM32 is {status.state_name} after STOP; "
                               "the ESP32 probably re-initialised the sensor")
        if status.ring_samples is not None and status.ring_samples != prefill:
            raise RuntimeError(f"ring holds {status.ring_samples} samples, expected {prefill}")

        self._open_sync_file(cfg["sync_csv"], append=False)
        self._sq.put(("run_armed", {"prefill": prefill, "status": status.describe()}))

    def _run_go(self, io) -> None:
        if self._run_cfg is None:
            raise RuntimeError("GO without ARM")
        code = vimu.VIMU_UART_CMD_START
        pred = (lambda f, c=code: f.cmd == c and f.packet_type in (
            vimu.VIMU_UART_PKT_TYPE_ACK, vimu.VIMU_UART_PKT_TYPE_NACK))
        io.pop_pending_frames(pred)
        t0 = time.perf_counter()
        io.send_command(code)
        frame = io.wait_for_frame(pred, timeout=1.5, description="START ACK")
        t1 = time.perf_counter()
        if frame.packet_type == vimu.VIMU_UART_PKT_TYPE_NACK:
            raise RuntimeError("START NACK")

        # The first sample enters the FIFO on the next 50 Hz tick after this.
        t_go = (t0 + t1) / 2
        self._t_go = t_go
        self._run_active = True
        self._last_sp = None
        self._was_running = True
        self._underrun = False
        self._stalled = False
        self._complete = False
        self._sq.put(("run_go", {"t_go": t_go, "rtt_ms": (t1 - t0) * 1e3}))
        start = self._run_cfg["start"]
        self._record_sync(SyncPoint(t_go, 0, self._sample_cursor, self._sample_cursor - start,
                                    vimu.VIMU_STATE_RUNNING, 0, (t1 - t0) * 1e3), status=None)
        self.set_auto_refill(True, self.refill_threshold_s)

    def _run_stop(self, io) -> None:
        self._run_active = False
        self.set_auto_refill(False)
        try:
            self._ack_cmd(io, vimu.VIMU_UART_CMD_STOP, "STOP")
        except Exception as exc:
            self._log(f"[WARN] STOP at the end of the run failed: {exc}")
        finally:
            self._run_cfg = None
            self._close_sync_file()
            self._sq.put(("run_stopped", {"cursor": self._sample_cursor}))

    def export_run_state(self) -> Optional[dict]:
        """Run state for a new thread after the UART link dropped."""
        if self._run_cfg is None:
            return None
        return {"cfg": dict(self._run_cfg), "cursor": self._sample_cursor,
                "sequence": self._data_sequence, "sequenced": self._sequenced_new_data,
                "t_go": self._t_go, "active": self._run_active,
                "sync_path": self._sync_path, "last_sp": self._last_sp,
                "complete": self._complete}

    def _run_resume(self, st: dict) -> None:
        self._run_cfg = st["cfg"]
        self._sample_cursor = st["cursor"]
        self._data_sequence = st["sequence"]
        self._sequenced_new_data = st["sequenced"]
        self._t_go = st["t_go"]
        self._last_sp = st["last_sp"]
        self._complete = st["complete"]
        self._last_fall_id_sent = None
        if st["sync_path"]:
            self._open_sync_file(st["sync_path"], append=True)
        self._run_active = st["active"]
        if self._run_active:
            self.set_auto_refill(True, self.refill_threshold_s)
        self._event("uart_resumed", f"cursor={self._sample_cursor}")

    # -- overrides of the base thread --

    def _send_new_data_frames(self, io, total: int) -> None:
        if self._run_cfg is not None:
            total = min(total, self._run_cfg["end"] - self._sample_cursor)
            if total <= 0:
                return
        super()._send_new_data_frames(io, total)

    def _maybe_refill(self, io, status) -> None:
        if self._run_cfg is not None and self._sample_cursor >= self._run_cfg["end"]:
            return
        super()._maybe_refill(io, status)

    def _recover_link(self, io) -> None:
        if self._run_active:
            self._event("uart_resync", "UPDATE timed out 3 times, handshake again")
        super()._recover_link(io)

    def _poll_update(self, io):
        # Same as the base poll, with two changes: a late answer to an earlier
        # poll is dropped (it carries an old ring count), and a serial port
        # error ends the thread. The base version swallows that error, so an
        # unplugged USB-UART would poll forever and the GUI would never get
        # the "disconnected" it needs to reconnect and resume the run.
        rsp = (lambda f: f.packet_type == vimu.VIMU_UART_PKT_TYPE_RSP
               and f.cmd == vimu.VIMU_UART_CMD_UPDATE)
        io.pop_pending_frames(rsp)
        t0 = time.perf_counter()
        status = None
        try:
            io.send_command(vimu.VIMU_UART_CMD_UPDATE)
            frame = io.wait_for_frame(rsp, timeout=vimu.POLL_TIMEOUT_S, description="UPDATE RSP")
            if frame.crc_ok:
                status = vimu.decode_status_payload(frame.payload)
                self._sq.put(status)
                self._poll_failures = 0
        except vimu.serial.SerialException:
            raise
        except vimu.TestFailure as exc:
            self._log(f"[POLL] {exc}")
            self._poll_failures += 1
            if self._poll_failures >= 3:
                self._recover_link(io)
        except Exception as exc:
            self._log(f"[POLL] ERROR: {exc}")
            self._poll_failures += 1
        t1 = time.perf_counter()
        cfg = self._run_cfg
        if (status is not None and self._run_active and cfg is not None
                and status.ring_samples is not None):
            played = self._sample_cursor - cfg["start"] - status.ring_samples
            self._record_sync(SyncPoint((t0 + t1) / 2, played, self._sample_cursor,
                                        status.ring_samples, status.state, status.flags,
                                        (t1 - t0) * 1e3), status)
        return status

    # -- sync log and anomalies --

    def _event(self, kind: str, detail: str) -> None:
        self._log(f"[RUN] {kind}: {detail}")
        self._sq.put(("run_event", (time.perf_counter(), kind, detail)))

    def _record_sync(self, sp: SyncPoint, status) -> None:
        cfg = self._run_cfg
        if status is not None and cfg is not None:
            running = sp.state == vimu.VIMU_STATE_RUNNING
            if not running and self._was_running:
                self._event("stm32_not_running",
                            f"state={status.state_name} (ESP32 re-init or STM32 reset?)")
            elif running and not self._was_running:
                self._event("stm32_running", "playback resumed")
            self._was_running = running

            underrun = sp.ring == 0 and sp.cursor < cfg["end"]
            if underrun != self._underrun:
                self._event("ring_underrun" if underrun else "ring_refilled",
                            f"cursor={sp.cursor}")
            self._underrun = underrun

            last = self._last_sp
            if last is not None:
                dt = sp.t - last.t
                dp = sp.played - last.played
                expected = dt * RATE_HZ
                if dp > expected + 30:
                    self._event("played_jump", f"+{dp - expected:.0f} samples over {dt:.2f} s "
                                               "(ring content lost, STM32 reset?)")
                stalled = running and sp.ring > 0 and dp == 0 and dt > 0.3
                if stalled != self._stalled:
                    self._event("playback_stall" if stalled else "playback_moving",
                                "ring not draining, ESP32 not reading the FIFO?"
                                if stalled else f"played={sp.played}")
                self._stalled = stalled

            if not self._complete and sp.cursor >= cfg["end"] and sp.ring == 0:
                self._complete = True
                self._event("run_complete", f"all {cfg['end'] - cfg['start']} samples played")
                self._sq.put(("run_complete", {"t": sp.t, "played": sp.played}))

        self._last_sp = sp
        if self._sync_writer is not None and cfg is not None:
            t_run = sp.t - self._t_go if self._t_go is not None else 0.0
            self._sync_writer.writerow([
                f"{sp.t:.6f}", f"{t_run:.6f}", sp.played, sp.cursor, sp.ring,
                cfg["start"] + sp.played, sp.state, sp.flags, f"{sp.rtt_ms:.2f}"])
            self._sync_rows += 1
            if self._sync_rows % 20 == 0:
                self._sync_file.flush()
        self._sq.put(("sync", sp))

    def _open_sync_file(self, path: str, append: bool) -> None:
        self._close_sync_file()
        new = not append or not Path(path).exists()
        self._sync_path = path
        self._sync_file = open(path, "a" if append else "w", newline="", encoding="utf-8")
        self._sync_writer = csv.writer(self._sync_file)
        if new:
            self._sync_writer.writerow(SYNC_HEADER)

    def _close_sync_file(self) -> None:
        if self._sync_file is not None:
            try:
                self._sync_file.close()
            except Exception:
                pass
        self._sync_file = None
        self._sync_writer = None
