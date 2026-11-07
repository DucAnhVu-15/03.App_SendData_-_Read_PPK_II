"""Post-run analysis: maps every D7 pulse to the NPZ sample that was playing.

The GUI runs this when a run stops. It can also be run again on a finished
run directory, e.g. after changing the match tolerance:

    py analyze_run.py runs/20261107_201500
    py analyze_run.py runs/20261107_201500 --tolerance 5

Writes d7_events.csv, summary.json and summary.txt into the run directory.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import math
import shutil
from collections import Counter
from pathlib import Path
from typing import Callable, List, Optional

import numpy as np

import run_sync as rs

RATE = rs.RATE_HZ

EVENT_HEADER = [
    "rise_no", "ppk_n", "width_ms", "glitch", "t_host", "t_run_s", "played",
    "npz_idx", "scenario_time", "category", "phase", "task", "episode_id",
    "y_fall_id", "verdict", "fall_id", "latency_s", "snippet",
]


def _read_csv(path: Path) -> List[dict]:
    """Read a CSV that may still be written to (auto-save checkpoints run
    during a run): a last line without its newline is dropped, and so is any
    row with missing or extra fields."""
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8", errors="replace")
    if text and not text.endswith("\n"):
        text = text[:text.rfind("\n") + 1]
    rows = csv.DictReader(io.StringIO(text))
    return [r for r in rows if None not in r and None not in r.values()]


def _stats(values) -> dict:
    a = np.asarray(values, dtype=float)
    if a.size == 0:
        return {"n": 0}
    return {"n": int(a.size), "mean": float(a.mean()), "median": float(np.median(a)),
            "std": float(a.std()), "min": float(a.min()), "max": float(a.max()),
            "p5": float(np.percentile(a, 5)), "p95": float(np.percentile(a, 95))}


def analyze(run_dir, npz: Optional[rs.NpzRunData] = None,
            tolerance_s: Optional[float] = None, warmup_s: Optional[float] = None,
            log: Callable[[str], None] = print, checkpoint: str = "") -> dict:
    """checkpoint: a label such as '01h00m00s' when called during a run; the
    summary is then also copied to checkpoints/<label>_summary.*"""
    run_dir = Path(run_dir)
    session = json.loads((run_dir / "session.json").read_text(encoding="utf-8"))
    run = session["run"]
    opts = session.get("options", {})
    start, end = int(run["start_idx"]), int(run["end_idx"])
    run_len = end - start
    tol_s = float(opts.get("match_tolerance_s", 3.0) if tolerance_s is None else tolerance_s)
    warm_s = float(opts.get("warmup_s", 4.0) if warmup_s is None else warmup_s)
    tol, warm = int(round(tol_s * RATE)), int(round(warm_s * RATE))
    t_go = session.get("t_go")

    if npz is None:
        log(f"Loading {session['npz']['path']}")
        npz = rs.load_npz_run_data(session["npz"]["path"])

    # STM32 play clock
    sync_rows = _read_csv(run_dir / "sync_log.csv")
    clock = rs.PlayClock()
    rtts = []
    for r in sync_rows:
        clock.add(float(r["t_host"]), float(r["played"]))
        rtts.append(float(r["rtt_ms"]))

    anchors = rs.AnchorMap.load(run_dir / "ppk_anchors.csv")
    pulses = _read_csv(run_dir / "d7_pulses.csv")

    # D7 pulses -> NPZ samples
    detected: set = set()
    events = []
    for p in pulses:
        n = int(p["n"])
        glitch = p["glitch"] == "1"
        width = int(p["width_samples"]) if p["width_samples"] else None
        if anchors is not None:
            t = anchors.t_of(n)
        else:
            t = float(p["t_host_est"]) if p["t_host_est"] else None
        ev = {"rise_no": p["rise_no"], "ppk_n": n,
              "width_ms": "" if width is None else f"{width / 100:.2f}",
              "glitch": int(glitch), "t_host": "" if t is None else f"{t:.6f}",
              "t_run_s": "", "played": "", "npz_idx": "", "scenario_time": "",
              "category": "", "phase": "", "task": "", "episode_id": "",
              "y_fall_id": "", "verdict": "", "fall_id": "", "latency_s": "",
              "snippet": p.get("snippet", "")}
        if t is None or t_go is None or len(clock) == 0:
            ev["verdict"] = "UNMAPPED"
            events.append(ev)
            continue
        played = clock.played_at(t)
        ev["t_run_s"] = f"{t - t_go:.3f}"
        ev["played"] = f"{played:.2f}"
        if glitch:
            ev["verdict"] = "GLITCH"
        elif played < 0:
            ev["verdict"] = "PREROLL"
        else:
            idx = start + min(int(math.floor(played)), run_len - 1)
            lab = npz.labels_at(idx)
            verdict, fall_id = rs.classify_pulse(idx, start, npz.episodes, tol, warm, detected)
            ev.update({"npz_idx": idx, "scenario_time": rs.fmt_hms(idx / RATE, 2),
                       "category": lab["category"], "phase": lab["phase"],
                       "task": lab["task"], "episode_id": lab["episode_id"],
                       "y_fall_id": lab["fall_id"], "verdict": verdict,
                       "fall_id": fall_id or ""})
            if verdict == "TP":
                ep = next(e for e in npz.episodes if e.fall_id == fall_id)
                t_fall = clock.time_of(ep.start_sample - start)
                if t_fall is not None:
                    ev["latency_s"] = f"{t - t_fall:.3f}"
        events.append(ev)

    with open(run_dir / "d7_events.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=EVENT_HEADER)
        w.writeheader()
        w.writerows(events)

    verdicts = Counter(e["verdict"] for e in events)
    falls_in_run = [ep for ep in npz.episodes if start <= ep.start_sample < end]
    missed = [ep.fall_id for ep in falls_in_run if ep.fall_id not in detected]
    latencies = [float(e["latency_s"]) for e in events if e["latency_s"] != ""]

    # Consistency check: with DEBUG_FALL_TRIGGER_EVERY_WAKE=1 the pulses come
    # once per ESP32 wake, so their NPZ spacing must stay constant.
    mapped = [e for e in events if e["npz_idx"] != ""]
    idx_steps = np.diff([int(e["npz_idx"]) for e in mapped]) if len(mapped) > 1 else []
    t_steps = np.diff([float(e["t_host"]) for e in mapped]) if len(mapped) > 1 else []

    # Sync quality
    sync = {"points": len(clock), "rtt_ms": _stats(rtts)}
    last = clock.last()
    if last is not None and t_go is not None and last[0] > t_go:
        ts = np.array([float(r["t_host"]) for r in sync_rows])
        ps = np.array([float(r["played"]) for r in sync_rows])
        # Only the part before the last sample was played: after that the count
        # stays flat while the tail and STOP still add sync points.
        moving = ps < ps.max()
        if moving.sum() >= 2:
            rate = float(np.polyfit(ts[moving] - t_go, ps[moving], 1)[0])
        else:
            rate = float(ps.max() / (ts[-1] - t_go))
        drift = ps[moving] - (ts[moving] - t_go) * RATE if moving.any() else np.zeros(1)
        sync.update({
            "effective_rate_hz": rate,
            "drift_samples_final": float(drift[-1]),
            "drift_samples_max_abs": float(np.abs(drift).max()),
            "played_samples": int(ps.max()),
            "played_fraction": float(ps.max()) / run_len if run_len else 0.0,
        })
    run_events = Counter(r["kind"] for r in _read_csv(run_dir / "run_events.csv"))

    # Current
    seconds = _read_csv(run_dir / "summary_1s.csv")
    meta_path = run_dir / "ppk_meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
    current: dict = {}
    if seconds:
        mean = np.array([float(r["mean_uA"]) for r in seconds])
        count = np.array([int(r["samples"]) for r in seconds])
        hours = np.arange(len(seconds)) // 3600
        per_hour = []
        for h in np.unique(hours):
            m = hours == h
            per_hour.append({"hour": int(h),
                             "avg_uA": float(np.average(mean[m], weights=count[m])),
                             "seconds": int(m.sum())})
        current = {
            "avg_uA": float(np.average(mean, weights=count)),
            "charge_mAh": float(sum(float(r["charge_uC"]) for r in seconds) / 3.6e6),
            "seconds": len(seconds),
            "min_1s_avg_uA": float(mean.min()), "max_1s_avg_uA": float(mean.max()),
            "per_hour": per_hour,
        }
    ppk: dict = {k: meta.get(k) for k in ("samples", "loss_events", "lost_samples_counter",
                                         "counter_check", "snippets", "pulses", "glitches")}
    if anchors is not None and meta.get("samples"):
        host_span = anchors.t_of(meta["rec_end_n"]) - anchors.t_of(meta["rec_start_n"])
        ppk["host_seconds"] = host_span
        ppk["sample_seconds"] = meta["samples"] * rs.PPK2_PERIOD_S
        if host_span > 0:
            ppk["samples_vs_host_ppm"] = (ppk["sample_seconds"] / host_span - 1) * 1e6

    run_hours = (sync.get("played_samples", 0) / RATE) / 3600
    summary = {
        "run_dir": str(run_dir),
        "status": session.get("status", ""),
        "checkpoint": checkpoint,
        "npz": session["npz"]["path"],
        "start_idx": start, "end_idx": end,
        "scenario": f"{rs.fmt_hms(start / RATE)} - {rs.fmt_hms(end / RATE)}",
        "wall_start": session.get("wall_go", ""), "wall_stop": session.get("wall_stop", ""),
        "match_tolerance_s": tol_s, "warmup_s": warm_s,
        "d7": {
            "pulses": len(events), "verdicts": dict(verdicts),
            "falls_in_run": len(falls_in_run), "falls_detected": len(detected),
            "falls_missed": missed,
            "fp_per_hour": (verdicts.get("FP", 0) / run_hours) if run_hours > 0 else None,
            "fp_by_category": dict(Counter(e["category"] for e in events if e["verdict"] == "FP")),
            "fp_by_phase": dict(Counter(e["phase"] for e in events if e["verdict"] == "FP")),
            "latency_s": _stats(latencies),
            "interval_npz_samples": _stats(idx_steps),
            "interval_host_s": _stats(t_steps),
        },
        "sync": sync,
        "run_events": dict(run_events),
        "current": current,
        "ppk2": ppk,
    }
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    text = format_summary(summary)
    (run_dir / "summary.txt").write_text(text, encoding="utf-8")
    if checkpoint:
        ck = run_dir / "checkpoints"
        ck.mkdir(exist_ok=True)
        shutil.copyfile(run_dir / "summary.txt", ck / f"{checkpoint}_summary.txt")
        shutil.copyfile(run_dir / "summary.json", ck / f"{checkpoint}_summary.json")
    log(text)
    return summary


def format_summary(s: dict) -> str:
    d7, sync, cur, ppk = s["d7"], s["sync"], s["current"], s["ppk2"]
    v = d7["verdicts"]
    ck = f"  (checkpoint {s['checkpoint']}, run still going)" if s.get("checkpoint") else ""
    lines = [
        f"Run {s['run_dir']}  [{s['status']}]{ck}",
        f"NPZ {s['npz']}",
        f"Scenario {s['scenario']}   wall clock {s['wall_start']} -> {s['wall_stop']}",
        "",
        "D7 (model FALL pulses)",
        f"  pulses {d7['pulses']}: FP {v.get('FP', 0)}, TP {v.get('TP', 0)}, DUP {v.get('DUP', 0)}, "
        f"WARMUP {v.get('WARMUP', 0)}, PREROLL {v.get('PREROLL', 0)}, GLITCH {v.get('GLITCH', 0)}, "
        f"UNMAPPED {v.get('UNMAPPED', 0)}",
        f"  falls in run {d7['falls_in_run']}, detected {d7['falls_detected']}, "
        f"missed {d7['falls_missed']}",
    ]
    if d7["fp_per_hour"] is not None:
        lines.append(f"  false alarms {d7['fp_per_hour']:.2f}/h "
                     f"({d7['fp_per_hour'] * 24:.1f} per 24 h)")
    if d7["fp_by_category"]:
        lines.append("  FP by category: " + ", ".join(
            f"{k or '?'} {n}" for k, n in sorted(d7["fp_by_category"].items(), key=lambda x: -x[1])))
    if d7["latency_s"].get("n"):
        lat = d7["latency_s"]
        lines.append(f"  TP latency: median {lat['median']:.2f} s, max {lat['max']:.2f} s")
    iv = d7["interval_npz_samples"]
    if iv.get("n"):
        lines.append(f"  pulse spacing in NPZ samples: median {iv['median']:.1f}, "
                     f"std {iv['std']:.2f}, p5-p95 {iv['p5']:.0f}-{iv['p95']:.0f} "
                     "(constant spacing = sync is consistent)")
    lines += ["", "Sync (STM32 playback vs host clock)",
              f"  sync points {sync['points']}, UPDATE round trip median "
              f"{sync['rtt_ms'].get('median', float('nan')):.1f} ms"]
    if "effective_rate_hz" in sync:
        lines.append(f"  played {sync['played_samples']:,} samples "
                     f"({sync['played_fraction'] * 100:.1f}% of the run), "
                     f"effective rate {sync['effective_rate_hz']:.4f} Hz")
        lines.append(f"  drift vs ideal 50 Hz: final {sync['drift_samples_final']:+.0f} samples, "
                     f"max |{sync['drift_samples_max_abs']:.0f}| samples")
    if s["run_events"]:
        lines.append("  events: " + ", ".join(f"{k} {n}" for k, n in sorted(s["run_events"].items())))
    lines += ["", "Current (PPK2)"]
    if cur:
        lines.append(f"  average {cur['avg_uA']:.2f} uA over {cur['seconds']:,} s, "
                     f"charge {cur['charge_mAh']:.3f} mAh")
        lines.append(f"  1 s averages from {cur['min_1s_avg_uA']:.1f} to {cur['max_1s_avg_uA']:.1f} uA")
        if len(cur["per_hour"]) > 1:
            lines.append("  per hour: " + ", ".join(
                f"h{h['hour']:02d} {h['avg_uA']:.1f}" for h in cur["per_hour"]))
    if ppk.get("samples"):
        lines.append(f"  {ppk['samples']:,} samples, loss events {ppk.get('loss_events')}, "
                     f"counter check {ppk.get('counter_check')}")
        if "samples_vs_host_ppm" in ppk:
            lines.append(f"  sample count vs host clock: {ppk['samples_vs_host_ppm']:+.0f} ppm "
                         "(large negative = lost samples)")
    return "\n".join(lines) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser(description="Analyse a finished HIL run directory.")
    ap.add_argument("run_dir")
    ap.add_argument("--tolerance", type=float, help="seconds after a fall still counted as TP")
    ap.add_argument("--warmup", type=float, help="seconds at the start not scored")
    ap.add_argument("--checkpoint", default="",
                    help="label for a snapshot taken during a run (used by auto-save)")
    args = ap.parse_args()
    analyze(args.run_dir, tolerance_s=args.tolerance, warmup_s=args.warmup,
            checkpoint=args.checkpoint)


if __name__ == "__main__":
    main()
