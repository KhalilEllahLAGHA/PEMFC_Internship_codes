"""
plot_experiments.py
--------------------
Plots each experiment file found in the same folder.
For every .txt file a figure is saved with three panels:
  1. Stack voltage (sum of all cells) [V]
  2. Individual cell voltages [mV]
  3. Current [mA]

Output PNG files are written next to this script.
"""

import re
import os
import glob

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

# ---------------------------------------------------------------------------
# Clean, report-quality plotting style
# ---------------------------------------------------------------------------
plt.rcParams.update({
    "figure.dpi"      : 200,
    "savefig.dpi"     : 200,
    "font.size"       : 12,
    "axes.titlesize"  : 13,
    "axes.labelsize"  : 12,
    "legend.fontsize" : 9,
    "xtick.labelsize" : 11,
    "ytick.labelsize" : 11,
    "axes.grid"       : True,
    "grid.alpha"      : 0.3,
    "axes.spines.top"   : False,
    "axes.spines.right" : False,
})

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILES  = sorted(glob.glob(os.path.join(SCRIPT_DIR, "experiment_*.txt")))

# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------
# Each line looks like:
#   Cell0[V]: 0 Cell1[V]: 1 ... time[s]: 42 data: mydata,v0,v1,...,v17,
# The numbers after CellN[V]: are column indices; the actual measurements
# are in the comma-separated data payload at those positions.

_HEADER_PAT = re.compile(
    r"time\[s\]:\s*(\d+)"
    r".*?"
    r"data:\s*mydata,([0-9,\-]+),",
    re.DOTALL,
)

_INDEX_PAT = re.compile(
    r"Cell(\d+)\[V\]:\s*(\d+)"
    r"|PressureS(\d+)\[kPa\]:\s*(\d+)"
    r"|CurrentS(\d+)\[mA\]:\s*(\d+)"
    r"|MassFlow(\d+)\[SCCM\]:\s*(\d+)"
    r"|U_Stack\[V\]:\s*(\d+)",
)

# Fixed column mapping deduced from the file header (indices are constant).
# Columns in the data payload:
#   0-9   : Cell0..9 [mV]
#   10,11,12 : Pressure [kPa raw]
#   13    : Current [mA]
#   14,15 : MassFlow [SCCM]
#   16    : U_Stack (seems to mirror sum; we compute from cells instead)
CELL_IDX    = list(range(10))      # data columns for cells
CURRENT_IDX = 13                   # data column for current

COLOURS = plt.cm.tab10.colors      # 10 distinct colours for 10 cells


def parse_file(filepath):
    """Return arrays: time_s, cell_mv (10xN), current_ma (N,)."""
    times, cells, currents = [], [], []
    with open(filepath, "r", errors="ignore") as fh:
        for line in fh:
            m = _HEADER_PAT.search(line)
            if not m:
                continue
            t = int(m.group(1))
            vals = [int(v) for v in m.group(2).split(",") if v.strip() != ""]
            if len(vals) < 14:
                continue
            times.append(t)
            cells.append([vals[i] for i in CELL_IDX])
            currents.append(vals[CURRENT_IDX])

    if not times:
        return None, None, None

    idx = np.argsort(times)
    t_arr  = np.array(times,   dtype=float)[idx]
    c_arr  = np.array(cells,   dtype=float)[idx]   # shape (N, 10)
    i_arr  = np.array(currents, dtype=float)[idx]
    return t_arr, c_arr, i_arr


# ---------------------------------------------------------------------------
# Per-file upper time cutoffs  (applied AFTER the pre-jump data is removed
# and time is re-zeroed to the jump point)
# ---------------------------------------------------------------------------
TIME_CUTOFFS = {
    "experiment_06_09_2026_1"              : 250,
    "experiment_06_09_2026_4_test_6_8_ohm": 175,
}

# Files where trimming starts at the current PEAK after the jump (not the jump itself)
TRIM_AT_PEAK = {
    "experiment_06_09_2026_4_test_6_8_ohm",
}
PEAK_SEARCH_WINDOW = 15   # samples to search for the peak after the jump

# Output CSV names keyed by source file stem
CSV_NAMES = {
    "experiment_06_09_2026_1"              : "experiment 3.3 ohm.csv",
    "experiment_06_09_2026_4_test_6_8_ohm" : "experiment 6.8 ohm.csv",
}

# ---------------------------------------------------------------------------
# Shared trimming helper
# ---------------------------------------------------------------------------

def trim_data(name, time_s, cell_mv, current_ma):
    """Remove pre-jump data, optionally start at peak, apply upper cutoff."""
    JUMP_THRESHOLD = 100   # mA
    jump_idx = None
    for i in range(1, len(current_ma)):
        if current_ma[i] - current_ma[i - 1] > JUMP_THRESHOLD:
            jump_idx = i
            break
    if jump_idx is not None:
        if name in TRIM_AT_PEAK:
            window_end = min(jump_idx + PEAK_SEARCH_WINDOW, len(current_ma))
            peak_offset = int(np.argmax(current_ma[jump_idx:window_end]))
            start_idx = jump_idx + peak_offset
            print(f"    Current jump at t={time_s[jump_idx]:.0f}s, "
                  f"peak at t={time_s[start_idx]:.0f}s "
                  f"({current_ma[start_idx]:.0f} mA) — trimming before peak.")
        else:
            start_idx = jump_idx
            print(f"    Current jump detected at t={time_s[jump_idx]:.0f}s "
                  f"({current_ma[jump_idx-1]:.0f} -> {current_ma[jump_idx]:.0f} mA) — "
                  f"trimming data before that point.")
        time_s     = time_s[start_idx:]
        cell_mv    = cell_mv[start_idx:]
        current_ma = current_ma[start_idx:]
        time_s = time_s - time_s[0]   # re-zero

    cutoff = TIME_CUTOFFS.get(name)
    if cutoff is not None:
        mask       = time_s <= cutoff
        time_s     = time_s[mask]
        cell_mv    = cell_mv[mask]
        current_ma = current_ma[mask]

    return time_s, cell_mv, current_ma

# ---------------------------------------------------------------------------
# Plotting
# ---------------------------------------------------------------------------

def plot_experiment(filepath):
    time_s, cell_mv, current_ma = parse_file(filepath)
    if time_s is None:
        print(f"  [SKIP] No valid data in {os.path.basename(filepath)}")
        return

    name = os.path.splitext(os.path.basename(filepath))[0]
    time_s, cell_mv, current_ma = trim_data(name, time_s, cell_mv, current_ma)

    # Stack voltage = sum of all cell voltages [mV -> V]
    stack_v = cell_mv.sum(axis=1) / 1000.0
    power_mw = stack_v * current_ma   # [V] * [mA] = [mW]

    out_png = os.path.join(SCRIPT_DIR, f"{name}_plot.png")

    # Friendly title from the resistor value encoded in the file name
    if "6_8" in name or "6.8" in name:
        title = "Load step, R = 6.8 Ω"
    elif "_1" in name or "3_3" in name or "3.3" in name:
        title = "Load step, R = 3.3 Ω"
    else:
        title = name.replace("_", " ")

    fig, axes = plt.subplots(4, 1, figsize=(9, 11), sharex=True)
    fig.suptitle(title, fontsize=14, fontweight="bold")

    # --- Panel 1: Stack voltage ---
    ax = axes[0]
    ax.plot(time_s, stack_v, color="black", lw=2.0)
    ax.set_ylabel("Stack voltage [V]")

    # --- Panel 2: Individual cell voltages ---
    ax = axes[1]
    for k in range(10):
        ax.plot(time_s, cell_mv[:, k], color=COLOURS[k], lw=1.2,
                label=f"Cell {k}")
    ax.set_ylabel("Cell voltage [mV]")
    ax.legend(ncol=5, loc="upper right", framealpha=0.9)

    # --- Panel 3: Current ---
    ax = axes[2]
    ax.plot(time_s, current_ma, color="tab:red", lw=2.0)
    ax.set_ylabel("Current [mA]")

    # --- Panel 4: Power ---
    ax = axes[3]
    ax.plot(time_s, power_mw, color="tab:purple", lw=2.0)
    ax.set_ylabel("Power [mW]")
    ax.set_xlabel("Time after connection [s]")

    plt.tight_layout()
    fig.savefig(out_png)
    plt.close(fig)
    print(f"  Saved: {os.path.basename(out_png)}")


# ---------------------------------------------------------------------------
# CSV export
# ---------------------------------------------------------------------------

def save_csv(filepath):
    time_s, cell_mv, current_ma = parse_file(filepath)
    if time_s is None:
        return

    name = os.path.splitext(os.path.basename(filepath))[0]
    csv_name = CSV_NAMES.get(name)
    if csv_name is None:
        return

    time_s, cell_mv, current_ma = trim_data(name, time_s, cell_mv, current_ma)
    stack_v = cell_mv.sum(axis=1) / 1000.0

    df = pd.DataFrame(
        {"time_s": time_s}
        | {f"Cell{k}_mV": cell_mv[:, k] for k in range(10)}
        | {"V_stack_V": stack_v, "Current_mA": current_ma}
    )

    out_csv = os.path.join(SCRIPT_DIR, csv_name)
    df.to_csv(out_csv, index=False)
    print(f"  Saved CSV: {csv_name}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    if not LOG_FILES:
        print("No experiment_*.txt files found in", SCRIPT_DIR)
    else:
        for fp in LOG_FILES:
            print(f"Processing: {os.path.basename(fp)}")
            plot_experiment(fp)
            save_csv(fp)
        print("Done.")
