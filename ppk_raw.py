"""Full-rate (100 kS/s) PPK2 data of a run, kept as small .npz files.

The capture process writes them while a run records (RawWriter). Read them
back in Python with load() or load_run(), or from the command line:

    py ppk_raw.py check runs\\20261109_101500
        compare every raw NPZ with its CSV twin, row by row
    py ppk_raw.py csv runs\\20261109_101500 --from 0:10:00 --to 0:10:30
        write that window as one CSV, in the same format as the twins

run_dir/ppk_raw/raw_<HH>h<MM>m<SS>s.npz holds one minute of samples. The name
is the Timestamp of its first row. The keys mirror the CSV columns:

    current_uA   float32[k]  Current(uA)
    d0_d7        uint8[k]    D0-D7 as one byte, bit i = Di (D7 high = 128)
    t0_ms        float64     Timestamp(ms) of row 0
    dt_ms        float64     0.01, so Timestamp(ms) of row i = t0_ms + i * dt_ms
    n_rel_start  int64       row 0 as n_rel in d7_pulses.csv (samples since REC)
    n_start      int64       row 0 as n in d7_pulses.csv and ppk_anchors.csv

The arrays are bzip2-compressed inside the zip, and np.load() reads them like
any other .npz. On real PPK2 captures that is about 0.6 byte per sample, or
5 GB per 24 h, against 5 bytes uncompressed and about 30 bytes as CSV text.
Timestamps are not stored per row, because a float64 column doubles the size.
Like the nRF Connect export, Timestamp counts samples: samples lost on the USB
link (ppk_loss.csv) are not in the files, so later rows run behind real time.

The CSV twin raw_<...>.csv has the columns of the nRF Connect export and of
HIL_PPK_Fall_Dev/1.TestBasic.py: Timestamp(ms), Current(uA), D0-D7. D0-D7 is
8 characters with D0 first, so a D7 pulse reads 00000001.
"""

from __future__ import annotations

import argparse
import math
import os
import queue
import re
import shutil
import sys
import threading
import time
import zipfile
from pathlib import Path
from typing import Callable, Dict, Iterator, List, Optional, Tuple

import numpy as np

SAMPLE_RATE_HZ = 100_000
DT_MS = 1000.0 / SAMPLE_RATE_HZ
RAW_DIR = "ppk_raw"
FILE_S = 60                     # one file per minute of samples
CSV_HEADER = "Timestamp(ms),Current(uA),D0-D7\r\n"
NPZ_BYTES_PER_SAMPLE = 0.6      # measured on real captures, for estimates only
CSV_BYTES_PER_ROW = 31          # a row late in a long run, for estimates only
CSV_MIN_FREE_GB = 10.0          # the CSV twin stops below this much free disk
NPZ_MIN_FREE_GB = 1.0           # the NPZ files stop below this
CSV_MAX_BACKLOG_S = 120         # the CSV twin stops if it falls this far behind
CSV_BLOCK = 1_000_000           # rows formatted at a time when converting
D0D7_TEXT = [format(b, "08b")[::-1] for b in range(256)]   # D0 first
_NAME = re.compile(r"raw_(\d+)h(\d\d)m(\d\d)s$")


# ── Format ─────────────────────────────────────────────────────────────────

def file_stem(n_rel: int) -> str:
    """'raw_01h02m00s' for the file whose first row is n_rel samples into the run."""
    s = n_rel // SAMPLE_RATE_HZ
    return f"raw_{s // 3600:02d}h{s // 60 % 60:02d}m{s % 60:02d}s"


def _stem_seconds(path: Path) -> Optional[int]:
    m = _NAME.match(path.stem)
    return None if m is None else int(m[1]) * 3600 + int(m[2]) * 60 + int(m[3])


def _part(path: Path) -> Path:
    return path.with_name(path.name + ".part")


def save_npz(path: Path, arrays: Dict[str, np.ndarray]) -> int:
    """np.savez with bzip2 compression. The file is written as .part and then
    renamed, so a crash never leaves a broken file under the real name.
    Returns the size in bytes."""
    tmp = _part(path)
    with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_BZIP2,
                         compresslevel=9, allowZip64=True) as zf:
        for key, value in arrays.items():
            with zf.open(key + ".npy", "w", force_zip64=True) as f:
                np.lib.format.write_array(f, np.asanyarray(value), allow_pickle=False)
    os.replace(tmp, path)
    return path.stat().st_size


def csv_text(n_rel0: int, cur: np.ndarray, bits: np.ndarray) -> str:
    """CSV rows for samples n_rel0, n_rel0 + 1, ... (the 1.TestBasic.py text)."""
    ts = ((n_rel0 + np.arange(cur.size, dtype=np.int64)) * DT_MS).tolist()
    return "".join(f"{t:.2f},{c:.3f},{D0D7_TEXT[b]}\r\n"
                   for t, c, b in zip(ts, cur.tolist(), bits.tolist()))


# ── Writing ────────────────────────────────────────────────────────────────

class RawWriter:
    """Writes the raw files of one run while it records.

    feed() only queues the arrays. A thread fills the one-minute buffer,
    compresses it and writes the CSV text, so the capture loop never waits on
    the disk (bzip2 and file writes release the GIL). The minute in progress
    lives in memory, so a crash loses at most that minute.
    """

    def __init__(self, run_dir, n_start: int, csv_on: bool,
                 warn: Callable[[str], None] = print, file_s: float = FILE_S,
                 on_file: Optional[Callable[[dict], None]] = None) -> None:
        self.dir = Path(run_dir) / RAW_DIR
        self.dir.mkdir(parents=True, exist_ok=True)
        self.n_start = int(n_start)
        self.per_file = int(file_s * SAMPLE_RATE_HZ)
        self.csv_on = bool(csv_on)
        self.npz_on = True
        self._warn = warn
        self._on_file = on_file      # called from the thread after each saved file
        self.fed = 0             # samples queued
        self.done = 0            # samples taken off the queue by the thread
        self.saved = 0           # samples in finished NPZ files
        self.files = 0
        self.npz_bytes = 0
        self.csv_files = 0
        self.csv_bytes = 0
        self.error = ""
        self._cur = np.empty(self.per_file, dtype=np.float32)
        self._bits = np.empty(self.per_file, dtype=np.uint8)
        self._fill = 0
        self._file_n_rel = 0     # n_rel of row 0 of the file being filled
        self._csv = None
        self._csv_path: Optional[Path] = None
        self._q: queue.Queue = queue.Queue()
        self._thread = threading.Thread(target=self._loop, name="ppk-raw", daemon=True)
        self._thread.start()

    def feed(self, cur: np.ndarray, bits: np.ndarray) -> None:
        """Queue the next samples, in order. The arrays must not change afterwards."""
        cur = np.asarray(cur, dtype=np.float32)
        self._q.put((cur, np.asarray(bits, dtype=np.uint8)))
        self.fed += int(cur.size)

    def close(self, timeout: float = 12.0) -> dict:
        """Write the last, shorter file and wait for the thread."""
        self._q.put(None)
        self._thread.join(timeout)
        if self._thread.is_alive():
            self._warn(f"raw files: still writing after {timeout:.0f} s, "
                       "the last file finishes in the background")
        return self.stats()

    def stats(self) -> dict:
        return {"dir": RAW_DIR, "file_s": self.per_file / SAMPLE_RATE_HZ,
                "npz_on": self.npz_on, "files": self.files, "npz_bytes": self.npz_bytes,
                "samples": self.saved, "csv_on": self.csv_on, "csv_files": self.csv_files,
                "csv_bytes": self.csv_bytes,
                "backlog_s": max(self.fed - self.done, 0) / SAMPLE_RATE_HZ,
                "error": self.error}

    # -- thread --

    def _loop(self) -> None:
        while True:
            item = self._q.get()
            if item is None:
                break
            cur, bits = item
            if self.npz_on:
                try:
                    self._add(cur, bits)
                except Exception as exc:
                    self._fail(exc)
            self.done += int(cur.size)
        if self.npz_on:
            try:
                self._close_file()
            except Exception as exc:
                self._fail(exc)

    def _add(self, cur: np.ndarray, bits: np.ndarray) -> None:
        i = 0
        while i < cur.size:
            if self._fill == 0:
                self._open_file()
                if not self.npz_on:
                    return
            take = min(self.per_file - self._fill, cur.size - i)
            c, b = cur[i:i + take], bits[i:i + take]
            self._cur[self._fill:self._fill + take] = c
            self._bits[self._fill:self._fill + take] = b
            if self._csv is not None:
                self._csv.write(csv_text(self._file_n_rel + self._fill, c, b))
            self._fill += take
            i += take
            if self._fill == self.per_file:
                self._close_file()

    def _open_file(self) -> None:
        free_gb = shutil.disk_usage(self.dir).free / 1e9
        behind_s = (self.fed - self.done) / SAMPLE_RATE_HZ
        if self.csv_on and free_gb < CSV_MIN_FREE_GB:
            self.csv_on = False
            self._warn(f"raw CSV stopped: {free_gb:.1f} GB free on the disk "
                       f"(limit {CSV_MIN_FREE_GB:g} GB). The NPZ files continue.")
        elif self.csv_on and behind_s > CSV_MAX_BACKLOG_S:
            self.csv_on = False
            self._warn(f"raw CSV stopped: writing fell {behind_s:.0f} s behind. "
                       "The NPZ files continue.")
        if free_gb < NPZ_MIN_FREE_GB:
            self.npz_on = False
            self.csv_on = False
            self._warn(f"raw NPZ stopped: {free_gb:.1f} GB free on the disk")
            return
        if self.csv_on:
            self._csv_path = self.dir / (file_stem(self._file_n_rel) + ".csv")
            self._csv = open(_part(self._csv_path), "w", newline="", encoding="ascii")
            self._csv.write(CSV_HEADER)

    def _close_file(self) -> None:
        k = self._fill
        if k == 0:
            return
        n_rel = self._file_n_rel
        stem = file_stem(n_rel)
        npz_size = save_npz(self.dir / (stem + ".npz"), {
            "current_uA": self._cur[:k], "d0_d7": self._bits[:k],
            "t0_ms": np.float64(n_rel * DT_MS), "dt_ms": np.float64(DT_MS),
            "n_rel_start": np.int64(n_rel), "n_start": np.int64(self.n_start + n_rel)})
        self.npz_bytes += npz_size
        self.files += 1
        self.saved += k
        csv_size = 0
        if self._csv is not None:
            self._csv.close()
            self._csv = None
            os.replace(_part(self._csv_path), self._csv_path)
            csv_size = self._csv_path.stat().st_size
            self.csv_bytes += csv_size
            self.csv_files += 1
        self._file_n_rel += k
        self._fill = 0
        if self._on_file is not None:
            try:  # a failed notice must not stop the recording
                self._on_file({"name": stem, "rows": k, "t0_ms": n_rel * DT_MS,
                               "npz_bytes": npz_size, "csv_bytes": csv_size,
                               "t_wall": time.time()})
            except Exception:
                pass

    def _fail(self, exc: Exception) -> None:
        self.error = f"{type(exc).__name__}: {exc}"
        self.npz_on = False
        self.csv_on = False
        if self._csv is not None:
            try:
                self._csv.close()
            except Exception:
                pass
            self._csv = None
        self._warn(f"raw files stopped: {self.error}")


# ── Reading ────────────────────────────────────────────────────────────────

def read_file(path) -> dict:
    """Every key of one raw file, decompressed."""
    with np.load(path) as z:
        return {k: z[k] for k in z.files}


def timestamps_ms(f: dict) -> np.ndarray:
    """The Timestamp(ms) column of a file from read_file()."""
    k = f["current_uA"].size
    return (int(f["n_rel_start"]) + np.arange(k, dtype=np.int64)) * float(f["dt_ms"])


def load(path) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Timestamp(ms), Current(uA) and D0-D7 of one raw file, as the CSV columns.
    D0-D7 is a byte with bit i = Di, so (d0_d7 >> 7) & 1 is D7."""
    f = read_file(path)
    return timestamps_ms(f), f["current_uA"], f["d0_d7"]


def raw_files(target) -> List[Path]:
    """The raw .npz files of a run folder, of its ppk_raw folder, or one file,
    in time order."""
    p = Path(target)
    if p.is_file():
        return [p]
    if (p / RAW_DIR).is_dir():
        p = p / RAW_DIR
    return sorted((f for f in p.glob("raw_*.npz") if _stem_seconds(f) is not None),
                  key=_stem_seconds)


def _selected(files: List[Path], start_ms: Optional[float],
              end_ms: Optional[float]) -> Iterator[Tuple[Path, float]]:
    """(file, its span in ms) for the files that overlap [start_ms, end_ms).
    The span comes from the names; the last file is counted as a full one."""
    starts = [(_stem_seconds(f) or 0) * 1000.0 for f in files]
    for i, path in enumerate(files):
        a = starts[i]
        b = starts[i + 1] if i + 1 < len(files) else a + FILE_S * 1000.0
        if end_ms is not None and a >= end_ms:
            break
        if start_ms is not None and b <= start_ms:
            continue
        lo = a if start_ms is None else max(a, start_ms)
        hi = b if end_ms is None else min(b, end_ms)
        yield path, max(hi - lo, 0.0)


def _bounds(f: dict, start_ms: Optional[float], end_ms: Optional[float]) -> Tuple[int, int]:
    """Row range of a file inside [start_ms, end_ms)."""
    n0, k, dt = int(f["n_rel_start"]), f["current_uA"].size, float(f["dt_ms"])

    def row(ms: float) -> int:
        return min(max(math.ceil(round(ms / dt, 6)) - n0, 0), k)

    lo = 0 if start_ms is None else row(start_ms)
    hi = k if end_ms is None else row(end_ms)
    return lo, max(lo, hi)


def load_run(target, start_ms: Optional[float] = None, end_ms: Optional[float] = None
             ) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Timestamp(ms), Current(uA) and D0-D7 in [start_ms, end_ms), joined across
    files. Timestamp 0 is the start of the PPK2 recording. This needs about
    80 MB of memory per minute of data, so load a window, not a whole day."""
    parts = []
    for path, _span in _selected(raw_files(target), start_ms, end_ms):
        f = read_file(path)
        lo, hi = _bounds(f, start_ms, end_ms)
        parts.append((timestamps_ms(f)[lo:hi], f["current_uA"][lo:hi], f["d0_d7"][lo:hi]))
    if not parts:
        return np.empty(0), np.empty(0, np.float32), np.empty(0, np.uint8)
    ts, cur, bits = (np.concatenate(col) for col in zip(*parts))
    return ts, cur, bits


# ── Check and convert ──────────────────────────────────────────────────────

def check_twin(npz_path: Path, csv_path: Path) -> Tuple[bool, str]:
    """Compare one NPZ with its CSV twin. The CSV rounds Timestamp to 0.01 ms
    and Current to 0.001 uA; D0-D7 must match exactly."""
    f = read_file(npz_path)
    ts, cur, bits = timestamps_ms(f), f["current_uA"], f["d0_d7"]
    data = np.loadtxt(csv_path, delimiter=",", skiprows=1, dtype=np.float64, ndmin=2)
    if data.shape[0] != cur.size:
        return False, f"row count differs: NPZ {cur.size:,}, CSV {data.shape[0]:,}"
    if cur.size == 0:
        return True, "0 rows"
    digits = data[:, 2].astype(np.int64)        # '00000001' reads as 1
    csv_bits = np.zeros(digits.size, dtype=np.uint8)
    for i in range(8):                           # character i (from the left) is Di
        csv_bits |= ((digits // 10 ** (7 - i) % 10) << i).astype(np.uint8)
    d_t = float(np.abs(data[:, 0] - ts).max())
    d_i = float(np.abs(data[:, 1] - cur.astype(np.float64)).max())
    bad_bits = int(np.count_nonzero(csv_bits != bits))
    rises = int(np.count_nonzero(np.diff((bits >> 7).astype(np.int8)) == 1))
    ok = d_t < 0.001 and d_i <= 0.0005 + 1e-6 and bad_bits == 0
    return ok, (f"{cur.size:,} rows, max |dT| {d_t:.4f} ms, max |dI| {d_i:.4f} uA, "
                f"D0-D7 differences {bad_bits}, D7 rises {rises}")


def write_csv(files: List[Path], out: Path, start_ms: Optional[float] = None,
              end_ms: Optional[float] = None, log: Callable[[str], None] = print) -> int:
    """Write the rows in [start_ms, end_ms) of these files as one CSV."""
    chosen = list(_selected(files, start_ms, end_ms))
    if not chosen:
        raise SystemExit("No raw data in that time range.")
    est = sum(span for _p, span in chosen) / DT_MS * CSV_BYTES_PER_ROW
    out.parent.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(out.parent).free
    if est > free - 1e9:
        raise SystemExit(f"The CSV would take about {est / 1e9:.1f} GB, but only "
                         f"{free / 1e9:.1f} GB is free. Choose a shorter --from/--to window.")
    log(f"Writing {out} (about {est / 1e9:.2f} GB)")
    rows = 0
    tmp = _part(out)
    with open(tmp, "w", newline="", encoding="ascii") as fh:
        fh.write(CSV_HEADER)
        for path, _span in chosen:
            f = read_file(path)
            lo, hi = _bounds(f, start_ms, end_ms)
            n0, cur, bits = int(f["n_rel_start"]), f["current_uA"], f["d0_d7"]
            for a in range(lo, hi, CSV_BLOCK):
                b = min(a + CSV_BLOCK, hi)
                fh.write(csv_text(n0 + a, cur[a:b], bits[a:b]))
            rows += hi - lo
            log(f"  {path.name}: {hi - lo:,} rows")
    os.replace(tmp, out)
    return rows


def _parse_time_ms(text: str) -> float:
    """'1:02:03', '2:03' or '123.5' (seconds) -> milliseconds."""
    seconds = 0.0
    for part in text.strip().split(":"):
        seconds = seconds * 60 + float(part)
    return seconds * 1000.0


def _fmt_ms(ms: Optional[float], default: str) -> str:
    if ms is None:
        return default
    s = int(ms // 1000)
    return f"{s // 3600:02d}h{s // 60 % 60:02d}m{s % 60:02d}s"


def _run_folder(target: Path) -> Path:
    if target.is_file():
        target = target.parent
    return target.parent if target.name == RAW_DIR else target


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Raw 100 kS/s PPK2 files of a run (ppk_raw/*.npz)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check", help="compare every raw NPZ with its CSV twin")
    c.add_argument("run", help="run folder, its ppk_raw folder or one .npz")
    v = sub.add_parser("csv", help="write raw data as one CSV: Timestamp(ms),Current(uA),D0-D7")
    v.add_argument("run", help="run folder, its ppk_raw folder or one .npz")
    v.add_argument("--from", dest="start", type=_parse_time_ms,
                   help="first Timestamp, h:m:s or seconds (default: the first row)")
    v.add_argument("--to", dest="end", type=_parse_time_ms,
                   help="end Timestamp, h:m:s or seconds, not included (default: the last row)")
    v.add_argument("-o", "--out", help="output file (default: ppk_<from>-<to>.csv in the run folder)")
    a = ap.parse_args(argv)

    target = Path(a.run)
    files = raw_files(target)
    if not files:
        print(f"No raw_*.npz files in {target}")
        return 1

    if a.cmd == "check":
        checked = bad = 0
        for path in files:
            twin = path.with_suffix(".csv")
            if not twin.exists():
                continue
            ok, text = check_twin(path, twin)
            checked += 1
            bad += 0 if ok else 1
            print(f"{'OK  ' if ok else 'FAIL'}  {path.name}: {text}")
        if checked == 0:
            print(f"{len(files)} NPZ files, none has a CSV twin (the run had raw CSV off).")
            return 1
        print(f"\n{checked} of {len(files)} NPZ files have a CSV twin: "
              f"{checked - bad} match, {bad} differ.")
        return 1 if bad else 0

    out = Path(a.out) if a.out else _run_folder(target) / (
        f"ppk_{_fmt_ms(a.start, 'start')}-{_fmt_ms(a.end, 'end')}.csv")
    rows = write_csv(files, out, a.start, a.end)
    print(f"Wrote {rows:,} rows to {out} ({out.stat().st_size / 1e6:.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
