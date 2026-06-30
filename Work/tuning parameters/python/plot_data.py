# plot_data.py --- Plot raw cell voltages and input signals from all experiment files.
#
# Produces (in results/):
#   data_overview_<filename>.png  -- per-file: inputs + all cell voltages
#   cell_voltages_all.png         -- all experiments overlaid per cell (2x5 grid)

import re, os, glob
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

# -- Paths --------------------------------------------------------------------
SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
LOG_FILES   = sorted(glob.glob(os.path.join(SCRIPT_DIR, "experiment_*.txt")))
RESULTS_DIR = os.path.join(SCRIPT_DIR, "results")
os.makedirs(RESULTS_DIR, exist_ok=True)

R_LOAD_OHM = 25.0
COLOURS    = plt.cm.tab10.colors

# =============================================================================
# 1. Parser
# =============================================================================
_ROW_PAT = re.compile(
    r"Cell0\[V\]:\s*(-?\d+)\s+Cell1\[V\]:\s*(-?\d+)\s+Cell2\[V\]:\s*(-?\d+)\s+"
    r"Cell3\[V\]:\s*(-?\d+)\s+Cell4\[V\]:\s*(-?\d+)\s+Cell5\[V\]:\s*(-?\d+)\s+"
    r"Cell6\[V\]:\s*(-?\d+)\s+Cell7\[V\]:\s*(-?\d+)\s+Cell8\[V\]:\s*(-?\d+)\s+"
    r"Cell9\[V\]:\s*(-?\d+)\s+"
    r"PressureS10\[kPa\]:\s*(-?\d+)\s+PressureS11\[kPa\]:\s*(-?\d+)\s+"
    r"PressureS12\[kPa\]:\s*(-?\d+)\s+"
    r"CurrentS13\[mA\]:\s*(-?\d+)\s+"
    r"MassFlow14\[SCCM\]:\s*(-?\d+)\s+MassFlow15\[SCCM\]:\s*(-?\d+)\s+"
    r"U_Stack\[V\]:\s*(\d+)\s+time\[s\]:\s*(\d+)"
)

def parse_log(filepath):
    rows = []
    with open(filepath, "r", errors="ignore") as fh:
        for line in fh:
            m = _ROW_PAT.search(line)
            if not m:
                continue
            g     = m.groups()
            valve = int(g[16])
            v_stk = sum(int(g[k]) for k in range(10)) / 1000.0
            rows.append({
                **{f"Cell{k}_mV": int(g[k]) for k in range(10)},
                "V_stack_mV" : v_stk * 1000.0,
                "P_H2_kPa"   : int(g[12]),
                "Current_mA" : int(g[13]),
                "MassFlow_H2": int(g[14]),   # SCCM
                "MassFlow_Air": int(g[15]),  # SCCM
                "I_load_A"   : v_stk / R_LOAD_OHM if valve > 0 else 0.0,
                "valve"      : valve,
                "time_s"     : int(g[17]),
            })
    return pd.DataFrame(rows).sort_values("time_s").reset_index(drop=True)


# =============================================================================
# 2. Per-file overview plot  (inputs on top, all cell voltages on bottom)
# =============================================================================
def plot_file_overview(df, title, out_path):
    t = df["time_s"].to_numpy()

    fig, axes = plt.subplots(2, 1, figsize=(12, 8), sharex=True)

    # --- Panel 1: All 10 cell voltages on one axes ---
    ax = axes[0]
    for k in range(10):
        ax.plot(t, df[f"Cell{k}_mV"], color=COLOURS[k], lw=1.2, label=f"Cell{k}")
    ax.set_ylabel("Cell voltage [mV]")
    ax.set_title(title)
    ax.legend(fontsize=8, ncol=5, loc="lower right")
    ax.grid(True, alpha=0.3)

    # --- Panel 2: H2 pressure, valve opening, and load current ---
    ax = axes[1]
    ax.plot(t, df["P_H2_kPa"],        color="steelblue",  lw=1.5, label="P_H2 [kPa]")
    ax.plot(t, df["I_load_A"] * 1000, color="darkorange",  lw=1.5, label="I_load [mA]")
    ax2 = ax.twinx()
    ax2.plot(t, df["valve"] / 22.0 * 100.0, color="tomato", lw=1.0,
             linestyle="--", label="Valve [%]")
    ax.set_xlabel("Time [s]")
    ax.set_ylabel("Pressure [kPa] / Current [mA]")
    ax2.set_ylabel("Valve opening [%]", color="tomato")
    ax.grid(True, alpha=0.3)
    lines1, lab1 = ax.get_legend_handles_labels()
    lines2, lab2 = ax2.get_legend_handles_labels()
    ax.legend(lines1 + lines2, lab1 + lab2, fontsize=8, loc="upper left")

    plt.tight_layout()
    plt.savefig(out_path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"  Saved -> {out_path}")


# =============================================================================
# 3. Main
# =============================================================================
def main():
    if not LOG_FILES:
        raise RuntimeError("No experiment_*.txt files found.")

    print(f"Found {len(LOG_FILES)} experiment file(s).")

    for filepath in LOG_FILES:
        name = os.path.splitext(os.path.basename(filepath))[0]
        print(f"Plotting {name} ...")
        df  = parse_log(filepath)
        out = os.path.join(RESULTS_DIR, f"data_overview_{name}.png")
        plot_file_overview(df, title=name, out_path=out)

    print("Done.")


if __name__ == "__main__":
    main()


if __name__ == "__main__":
    main()
