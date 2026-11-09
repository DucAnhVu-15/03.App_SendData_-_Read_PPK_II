# VIMU × PPK2 HIL Run

One app that streams an NPZ recording to the STM32 VIMU and records the PPK2 at the same time, on one clock. Each D7 pulse (the ESP32 model's FALL verdict on GPIO6) is mapped back to the NPZ sample that was playing, with its activity label. Built for continuous 24 h runs.

## Files

| File | Role |
|---|---|
| `vimu_ppk2_run_app.py` | GUI and the START RUN sequence. This is the file to run. |
| `run_sync.py` | STM32 run control (`RunMonitorThread`), NPZ labels, and the host-time maps `PlayClock` and `AnchorMap`. |
| `ppk2_capture.py` | PPK2 capture in its own process: conversion, D7 pulses, anchors and the run files. |
| `ppk_raw.py` | The raw 100 kS/s files: format, writer, loader, and the `check` and `csv` commands. |
| `analyze_run.py` | Post-run analysis. The app runs it at the end, and it also runs on its own. |

## Folder layout

The app imports the UART protocol and `MonitorThread` from `vimu_uart_e2e_test.py` instead of copying them. That tool must sit next to this folder, in a folder named exactly `Tool_send_data_fall_numpy`:

```
01.FALL_VIMU/
├── 03.App_SendData_&_Read_PPK_II/   this repo (any folder name works)
└── Tool_send_data_fall_numpy/        VIMU_Python_Send_Data_Fall_Numpy
```

To set it up from scratch:

```bash
git clone https://github.com/DucAnhVu-15/03.App_SendData_-_Read_PPK_II.git
git clone https://github.com/DucAnhVu-15/VIMU_Python_Send_Data_Fall_Numpy.git Tool_send_data_fall_numpy
```

The firmware and the PPK2 wiring come from the matching repos: ULP_FALL (ESP32-S3), SMT32_VIMU_MMA8451_Seed (STM32) and [HIL_PPK_Fall_Dev](https://github.com/DucAnhVu-15/HIL_PPK_Fall_Dev).

## Requirements

- `py` (not `python`, see the PPK2 notes in `HIL_PPK_Fall_Dev`), with `numpy`, `pyserial` and `ppk2-api`:
  ```bash
  py -m pip install numpy pyserial ppk2-api
  ```
- Wiring as in `HIL_PPK_Fall_Dev`: XIAO GPIO6 (D5) to PPK2 D7, logic port VCC to the board 3V3, GND to GND. No extra sync wire is needed.

## Before a 24 h run

1. **ESP32 firmware:** set `DEBUG_FALL_TRIGGER_EVERY_WAKE` to `0` in `main/main.c`, then build and flash. At `1`, D7 pulses on every wake, not only on a FALL verdict.
2. **Windows power:**
   - Set Sleep to Never and turn Hibernate off.
   - Turn off USB selective suspend (Power Options > Advanced > USB settings).
   - Pause Windows Update for the day.
   - Keep a laptop on its charger.

   While a run is active the app stops idle sleep itself. It cannot prevent a lid close, a manual sleep or an update restart.
3. **Ports:** close the nRF Connect Power Profiler, which holds the PPK2 port (COM17).
4. **Disk:** a 24 h run takes about 0.4 GB, plus up to 1 GB of D7 snippets and about 5 GB of raw 100 kS/s NPZ files. The raw CSV twin needs about 11 GB per hour, so set `RAW_CSV = 0` before a long run (see [Raw 100 kS/s data](#raw-100-kss-data)).

## Run

```bash
py vimu_ppk2_run_app.py --stm32-port COM19 --ppk2-port COM17 --npz "..\Tool_send_data_fall_numpy\P01_S01_SA06_typical_day.npz"
```

1. Click **Connect** for the STM32, then **Connect** for the PPK2. The PPK2 starts measuring with DUT power OFF. You can pick a port from the list, which refreshes each time it opens, or type it: `19`, `com19` and `COM19` all mean COM19.
2. Click **DUT ON**, then wait until the ESP32 has booted. Once the ESP32 has configured the sensor, the STM32 card shows `RUNNING`.
3. Set **Start** and **Duration**. Each has three boxes, hours : minutes : seconds. Type the digits into a box, or use its arrows, the Up/Down keys or the mouse wheel. Loading a file fills Duration with the file's full length (24:00:00). The port and time boxes are locked while a run is active.
4. Click **▶ START RUN**. The run stops by itself after the last sample has played, plus a 5 s tail, and its files are kept and analysed.

**■ STOP RUN** stops a run early. It asks what to do with the files:
- **Stop & save** keeps the files recorded so far and analyses them.
- **Stop & discard** deletes the run's folder. For a run longer than 5 minutes the app asks once more before deleting.
- **Keep running** closes the dialog and the run continues.

Closing the window during a run asks the same question, then quits.

The middle of the window has two columns:
- **Saved files** lists every file this run has saved, newest first, with the time it was saved:
  - one `ppk_raw/raw_….npz` per minute, with the PPK2 time it holds and the size of the NPZ and of its CSV twin;
  - the auto-save checkpoints;
  - the final analysis.

  The title shows the run folder and the free disk space.
- **VIMU ring buffer** shows the STM32 ring as of the last `UPDATE` (every 0.5 s):
  - a fill bar, with the refill threshold as a dashed line;
  - the state and flags;
  - the samples sent and played;
  - the number of refills and how often they come;
  - the lowest ring level of the run, not counting the end of the data;
  - the number of underruns.

  Below it, each prefill or refill gets one row: the time, the ring level before, the samples sent, and the NPZ cursor. A refill that sent fewer samples than it asked for is shown in yellow.

On a small screen the app opens maximised. The stats panel on the right scrolls with the mouse wheel, and its scrollbar only shows when the panel is cut. The bar above the D7 table and log is a divider: drag it down to give the file list, the ring panel and the stats more room.

Do not switch DUT power on as part of the run. On boot the ESP32 writes `CTRL_REG1` standby and then active, which stops and restarts the STM32 in the middle of the run. If DUT power was switched on less than 5 s before START RUN, the app asks before starting.

## Auto-save

Tick **Auto-save every** in the run bar and set the interval (h : m : s, default 1:00:00, minimum 1 minute). Alternatively, start the app with `--autosave 1:00:00`. You can switch it on or off and change the interval during a run.

The measurement files are always written to disk as the run goes. Auto-save adds the analysis: at every interval of run time (1 h, 2 h, …) the app analyses the data recorded so far. If the PC or the app dies part-way through a 24 h run, the results up to the last checkpoint are already on disk. Each snapshot writes:
- `summary.txt`, `summary.json` and `d7_events.csv` in the run folder, overwritten with the latest snapshot;
- `checkpoints/<01h00m00s>_summary.txt` and `.json`, one pair per snapshot, kept.

The snapshot runs `analyze_run.py` in a separate process, so it does not slow the PPK2 or STM32 threads. It covers data up to about 10 s before the snapshot. The final analysis at the end of the run replaces the latest files. The `Auto-save` line in the RUN panel shows the time to the next snapshot and how many have been saved. **Stop & discard** deletes the checkpoints with the rest of the run.

## What START RUN does

| Step | Action |
|---|---|
| ARM | STM32 `STOP` and `CLEAR`, then 500 samples (10 s) are prefilled from the start index, and `UPDATE` checks `STOPPED` with ring = 500. A UART `STOP` leaves the emulated `CTRL_REG1` at ACTIVE, so the ESP32 ULP does not re-init. |
| REC | The PPK2 process starts writing the run files. |
| PRE-ROLL | 1 s of baseline current before any data plays. |
| GO | STM32 `START`. Its ACK time is `t_go`, and `t_run = 0` from there. |
| RUN | Auto refill below 7 s of data. Every 0.5 s `UPDATE` poll logs `played = cursor − ring_samples`. |
| STOP | STM32 `STOP` and `CLEAR`, PPK2 recording closed, `analyze_run.py` runs. The ring is emptied because any write that sets the ESP32's `CTRL_REG1` ACTIVE makes the STM32 RUNNING, and that happens at a DUT power-on or a re-init. Samples left in the ring would then play by themselves outside a run. After a run, an idle STM32 that shows `RUNNING · ring 0/500` therefore means the ESP32 is up and nothing is waiting to play. |

If nothing reaches GO within 60 s, the run is aborted with an error.

## How the clocks are tied together

Every timestamp is `time.perf_counter()` on this PC. It is system-wide on Windows, so the GUI and the PPK2 process share it.

- **STM32 → host.** When the STM32 answers `UPDATE`, it has moved `cursor − ring_samples` samples from its ring into the MMA8451 FIFO. That is when a real sensor would have produced them. These points are interpolated (`sync_log.csv`). So a STM32 that plays a little off 50 Hz, a ring underrun, or a FIFO stall does not shift the mapping.
- **PPK2 → host.** The app does not use `n × 10 µs`. Over 24 h the PPK2 crystal drifts by seconds, and every lost USB packet would shift all later samples. Instead, every read records (host time, samples so far). The smallest offset in each 10 s window is the estimate (`ppk_anchors.csv`). Windows split where the offset steps up for good, which is what lost samples look like.

The expected error is about ±20 ms, set by the 50 Hz tick. The ESP32 only wakes every 2 s, so this is small. In a simulated run with the following conditions, every pulse mapped to the exact NPZ index, with a time error under 1.5 ms:
- the STM32 playing 0.2 % fast;
- the PPK2 clock off by 300 ppm;
- 2000 PPK2 samples lost;
- a PPK2 USB drop and a STM32 UART drop.

## Output: `runs/<yyyymmdd_hhmmss>/`

| File | Content |
|---|---|
| `summary.txt`, `summary.json` | D7 counts (FP/TP/DUP/…), false alarms per hour, FP by activity, current per hour, sync and PPK2 health |
| `d7_events.csv` | One row per D7 pulse: run time, NPZ index, scenario time, category, phase, task, verdict, latency |
| `session.json` | Run settings, `t_go`, wall clock at GO and at stop |
| `sync_log.csv` | STM32 poll log: host time, played, cursor, ring, state, round trip |
| `run_events.csv` | Anomalies and steps: `ring_underrun`, `playback_stall`, `played_jump`, `stm32_not_running`, `uart_lost` / `uart_resumed`, `link_lost` / `link_ok`, `sample_loss`, … |
| `summary_1s.csv` | Per PPK2 second: mean, min and max µA, and charge |
| `current_1ms_HH.f32` | Mean µA of every 1 ms, float32 LE, one file per hour. Read it with `np.fromfile(path, "<f4")` |
| `d7_pulses.csv`, `ppk_anchors.csv`, `ppk_loss.csv`, `ppk_meta.json` | Raw PPK2-side data used by the analysis |
| `snippets/d7_NNNNN.npz` | Full 100 kS/s current and D0–D7 for ±1 s around each D7 pulse (max 1000 per run) |
| `ppk_raw/raw_HHhMMmSSs.npz` | Every PPK2 sample, Current(uA) and D0-D7, one file per minute. See [Raw 100 kS/s data](#raw-100-kss-data) |
| `ppk_raw/raw_HHhMMmSSs.csv` | The same rows as CSV text, while `RAW_CSV = 1` |
| `app_log.txt` | Full log, including every refill |

The average current and the charge are summed from every sample, not from the 1 ms means.

Verdicts:
- `TP`: the first pulse within [fall start, fall end + 3 s].
- `DUP`: later pulses for the same fall.
- `FP`: a pulse outside every fall.
- `WARMUP`: a pulse in the first 4 s after GO, while the 200-sample model window still holds older data.
- `PREROLL`: a pulse before GO.
- `GLITCH`: a pulse shorter than 200 µs.

`P01_S01_SA06_typical_day.npz` has no labelled falls, so on that file every pulse is an FP. The 24 h run measures false alarms per day and the average current.

## Raw 100 kS/s data

Every PPK2 sample of a run is saved in `ppk_raw/`, one file per minute of samples. Each file is named after the Timestamp of its first row: `raw_00h00m00s.npz`, `raw_00h01m00s.npz`, and so on. The files hold the three columns of the nRF Connect CSV export:

| Key | Type | Column |
|---|---|---|
| `current_uA` | float32, one per row | Current(uA) |
| `d0_d7` | uint8, one per row | D0-D7 as one byte, bit i = Di. D7 high is 128, and `(d0_d7 >> 7) & 1` is D7 |
| `t0_ms`, `dt_ms` | float64 | Timestamp(ms) of row i is `t0_ms + i × dt_ms`, where `dt_ms` is 0.01 |
| `n_rel_start`, `n_start` | int64 | Row 0 as `n_rel` and as `n` in `d7_pulses.csv` |

Timestamp 0 is the start of the PPK2 recording (REC). With the default pre-roll, that is 1 s before GO. Like the nRF Connect export, the Timestamp counts samples, so samples lost on the USB link (`ppk_loss.csv`) are not in it. The Timestamp of a D7 pulse is its `n_rel × 0.01` from `d7_pulses.csv`.

**Size.** The arrays are bzip2-compressed inside the `.npz`. On real captures that comes to about 0.6 byte per sample: 0.2 GB per hour, or 5 GB per 24 h. Uncompressed, the same data takes 43 GB, and as CSV about 270 GB. Timestamps are not stored per row, because a float64 column would double the size. Current is float32. Above 16 mA, its third decimal can differ by 1 to 3 nA from a float64 export, while one PPK2 step there is tens of µA.

**Read in Python.** `np.load()` reads the files directly. `ppk_raw.py` also builds the Timestamp column:

```python
import ppk_raw
ts_ms, cur_uA, d0_d7 = ppk_raw.load(r"runs\20261109_101500\ppk_raw\raw_00h10m00s.npz")
ts_ms, cur_uA, d0_d7 = ppk_raw.load_run(r"runs\20261109_101500", start_ms=600_000, end_ms=630_000)
```

`load_run` joins files. It needs about 80 MB of memory per minute of data, so load a window rather than a whole day.

**CSV twin (`RAW_CSV`).** While `RAW_CSV = 1` at the top of `vimu_ppk2_run_app.py`, each NPZ file gets a CSV with the same rows, for example `raw_00h10m00s.csv`. Its format is the one of `1.TestBasic.py`: `Timestamp(ms),Current(uA),D0-D7`, with D0 first, so a D7 pulse reads `00000001`. The CSV takes about 11 GB per hour. When less than 10 GB is free, the CSV stops by itself and the NPZ files continue. Once the NPZ is trusted, set `RAW_CSV = 0`. To change it for one session only, start the app with `--raw-csv 0` or `--raw-csv 1`.

To check every NPZ file against its twin:

```bash
py ppk_raw.py check runs\20261109_101500
```

The check compares every row: Timestamp to 0.01 ms, Current to 0.001 µA, and D0-D7 exactly.

**CSV from the NPZ later.** With the twin off, you can still write any time window as CSV, in the same format:

```bash
py ppk_raw.py csv runs\20261109_101500 --from 0:10:00 --to 0:10:30
```

The output is `ppk_00h10m00s-00h10m30s.csv` in the run folder. Use `-o` to choose another path. Without `--from` and `--to`, the command writes the whole run. It refuses to start if the file would not fit on the disk. Excel opens only the first 1,048,576 rows, about 10.5 s of data.

**After a crash.** The minute in progress is held in memory, so a crash loses at most that minute. Each file is written under a `.part` name and renamed when complete, so a file with its final name is always whole.

The `Raw 100 kS/s` line in the PPK2 stats shows the number of files, the GB written, and the CSV state.

## 10-minute sync check first

Build with `DEBUG_FALL_TRIGGER_EVERY_WAKE = 1` and run 10 minutes (Duration `00:10:00`). D7 then pulses on every ESP32 wake. In `summary.txt`, check the line `pulse spacing in NPZ samples`: the std should be a few samples at most. Spacing that drifts or jumps means the sync is broken. Then set the flag back to 0 for the real run.

## Re-analyse a run

```bash
py analyze_run.py runs\20261107_201500
py analyze_run.py runs\20261107_201500 --tolerance 5 --warmup 4
```

## Options

| Option | Default | Meaning |
|---|---|---|
| `--poll` | 0.5 | STM32 `UPDATE` period (s) |
| `--voltage` | 3300 | PPK2 source voltage (mV) |
| `--preroll` | 1.0 | Baseline recorded before GO (s) |
| `--tail` | 5.0 | Recording kept after the last sample (s) |
| `--tolerance` | 3.0 | Seconds after a fall still counted as TP |
| `--warmup` | 4.0 | Seconds after GO that are not scored |
| `--refill-threshold` | 7 | Refill the ring below this many seconds |
| `--snippet-pre` / `--snippet-post` | 1.0 / 1.0 | Full-rate data around each pulse (s) |
| `--max-snippets` | 1000 | Snippet files per run |
| `--min-pulse-us` | 200 | Shorter D7 pulses are glitches |
| `--raw-csv` | `RAW_CSV` (1) | 1 = CSV twin of every raw NPZ file, 0 = NPZ only |
| `--boot-wait` | 5.0 | Ask before START if DUT power is more recent than this (s) |
| `--runs-dir` | `runs` | Output folder |
| `--autosave` | off | Auto-save interval, e.g. `1:00:00` |

## Faults during a run

- **STM32 UART drops.** The app reconnects every 3 s and continues from the same cursor and NEW_DATA sequence. The ring holds 7–10 s, so a short drop does not starve the ESP32.
- **PPK2 USB drops.** The capture process reconnects every 2 s and restores the DUT power state. The gap is logged in `ppk_loss.csv` and as an anchor step.
- **ESP32 reboot or STM32 reset.** These show up as `stm32_not_running`, `played_jump` (ring content lost) or `playback_stall` (FIFO not read) in `run_events.csv`.
- **App crash.** Files are flushed every few seconds, so the hours before the crash are kept. The PPK2 process closes its files when the GUI process disappears.

## Known limits

- The STM32 still applies `SET_FALL_ID` when the command arrives, up to 10 s before those samples play. The `ground_truth_fall_id` that the ESP32 prints is therefore not reliable. Scoring here uses the NPZ labels on the PC instead.
- Live verdicts in the table use the sync data available at that moment. The final values are in `d7_events.csv`.
- Closing the app turns the DUT power OFF.
