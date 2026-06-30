"""
tune_TR_experiments.py
======================
Tune the semi-empirical PEMFC model for the two TR-current experiments
(3.3 Ω and 6.8 Ω) whose trimmed data sit as CSV files in
  ..\\..\results experiment TR current\\

After tuning it:
  1. Saves per-experiment parameter CSV files to results/
  2. Applies the new tuned parameters on the Exp-19 loaded window
     and reports the RMSE per cell.
  3. Produces three figures in results/:
       - all_cells_overview_3.3ohm.png   (V-I fit quality, training data)
       - all_cells_overview_6.8ohm.png   (V-I fit quality, training data)
       - all_cells_exp19_new_params.png  (time-series overlay on Exp-19,
                                          one panel per cell, for each set
                                          of new params)
       - rmse_comparison.png             (bar chart: new params vs exp19 params
                                          evaluated on the Exp-19 dataset)
"""

import re
import os

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from scipy.optimize import differential_evolution, minimize

# =============================================================================
# Paths
# =============================================================================
SCRIPT_DIR   = os.path.dirname(os.path.abspath(__file__))
CSV_DIR      = os.path.normpath(
    os.path.join(SCRIPT_DIR, "..", "..", "results experiment TR current")
)
EXP19_LOG    = os.path.join(SCRIPT_DIR, "experiment_04_03_2026_19.txt")
RESULTS_DIR  = os.path.join(SCRIPT_DIR, "results")
os.makedirs(RESULTS_DIR, exist_ok=True)

CSV_FILES = {
    "3.3ohm": os.path.join(CSV_DIR, "experiment 3.3 ohm.csv"),
    "6.8ohm": os.path.join(CSV_DIR, "experiment 6.8 ohm.csv"),
}

# =============================================================================
# Physical / geometric parameters  (shared, fixed)
# =============================================================================
SHARED = {
    "T_K"      : 343.15,
    "P_O2_atm" : 0.21,
    "lambda_m" : 14.0,
    "A_fc_m2"  : 25e-4,
    "t_m_m"    : 178e-6,
    "i_max_A"  : 0.25,
}

# P_H2 is not recorded in the new CSV files; use atmospheric pressure as a
# fixed assumption (the same assumption used in tune_exp19 when pressure
# measurements were near-ambient).
P_H2_FIXED_ATM = 1.0

PARAM_NAMES  = ["xi1", "xi2", "xi3", "xi4", "B", "c"]
PARAM_BOUNDS = [
    (-8.00,  2.00),
    ( 0.00,  0.03),
    (-5e-3,  5e-3),
    (-0.01,  0.00),
    ( 0.00,  2.00),
    ( 0.00,  0.01),
]

COLOURS = plt.cm.tab10.colors

# =============================================================================
# 1.  Load new CSV files
# =============================================================================
def load_csv(filepath, skip_frac=0.30, n_points=80):
    """
    Read pre-trimmed experiment CSV.
    Columns: time_s, Cell0_mV .. Cell9_mV, V_stack_V, Current_mA

    Returns a calibration DataFrame with:
        I_A, P_H2_atm, Cell0_V .. Cell9_V
    and the full (un-downsampled) loaded DataFrame.
    """
    df = pd.read_csv(filepath)

    # Convert units
    for k in range(10):
        df[f"Cell{k}_V"] = df[f"Cell{k}_mV"] / 1000.0
    df["I_A"]      = df["Current_mA"] / 1000.0
    df["P_H2_atm"] = P_H2_FIXED_ATM

    # Discard the initial transient (first skip_frac of rows)
    n       = len(df)
    settled = df.iloc[int(skip_frac * n):].reset_index(drop=True)

    # Uniform downsample for calibration
    if len(settled) > n_points:
        idx     = np.round(np.linspace(0, len(settled) - 1, n_points)).astype(int)
        cal     = settled.iloc[idx].reset_index(drop=True)
    else:
        cal = settled.copy()

    cols = ["I_A", "P_H2_atm"] + [f"Cell{k}_V" for k in range(10)]
    return cal[cols], df


# =============================================================================
# 2.  Semi-empirical PEMFC voltage model  (identical to tune_exp19)
# =============================================================================
def pemfc_voltage(I_A, P_H2_atm, p):
    T, P_O2  = p["T_K"], max(p["P_O2_atm"], 1e-12)
    A_fc, t_m, i_max = p["A_fc_m2"], p["t_m_m"], p["i_max_A"]
    xi1, xi2, xi3, xi4 = p["xi1"], p["xi2"], p["xi3"], p["xi4"]
    B, c = p["B"], p["c"]

    I     = np.maximum(np.asarray(I_A,      float), 1e-6)
    P_H2  = np.maximum(np.asarray(P_H2_atm, float), 1e-9)
    I_raw = np.asarray(I_A, float)

    E     = 1.229 - 0.85e-3*(T - 298.15) + 4.3085e-5*T*(np.log(P_H2) + 0.5*np.log(P_O2))
    C_O2  = P_O2 / (5.1e6 * np.exp(-498.0 / T))
    v_act = xi1 + xi2*T + xi3*T*np.log(C_O2) + xi4*T*np.log(I)
    sigma = max(float((0.5139*p["lambda_m"] - 0.326)
                      * np.exp(1268*(1/303.15 - 1/T))), 1e-8)
    v_ohm = I_raw * (t_m / (A_fc * sigma) + c)
    v_con = -B * np.log(1.0 - np.clip(I_raw / i_max, 0.0, 0.999999))

    return E - v_act - v_ohm - v_con


# =============================================================================
# 3.  Optimiser  (identical to tune_exp19)
# =============================================================================
def fit_cell(I_data, P_H2_data, V_meas, seed=42):
    def objective(x):
        p = {**dict(zip(PARAM_NAMES, x)), **SHARED}
        return float(np.sqrt(np.mean((pemfc_voltage(I_data, P_H2_data, p) - V_meas)**2)))

    history = []
    de = differential_evolution(
        objective, PARAM_BOUNDS, seed=seed,
        maxiter=800, tol=1e-8, popsize=20,
        mutation=(0.5, 1.5), recombination=0.9,
        workers=1, polish=False,
        callback=lambda xk, conv: history.append(objective(xk)),
    )
    local = minimize(objective, de.x, method="L-BFGS-B", bounds=PARAM_BOUNDS,
                     options={"maxiter": 5000, "ftol": 1e-12, "gtol": 1e-9})

    best_x    = local.x if local.fun < de.fun else de.x
    best_rmse = min(local.fun, de.fun)
    history.append(best_rmse)
    return {**dict(zip(PARAM_NAMES, best_x)), **SHARED}, best_rmse, history


# =============================================================================
# 4.  Parse Exp-19 log  (reused from tune_exp19)
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
R_LOAD_OHM_EXP19 = 25.0

def parse_exp19(filepath):
    OCV_THRESHOLD_FRAC = 0.60
    rows = []
    with open(filepath, "r", errors="ignore") as fh:
        for line in fh:
            m = _ROW_PAT.search(line)
            if not m:
                continue
            g     = m.groups()
            cells = [int(g[k]) / 1000.0 for k in range(10)]
            v_stk = sum(cells)
            rows.append({
                **{f"Cell{k}_V": cells[k] for k in range(10)},
                "V_stk"    : v_stk,
                "P_H2_atm" : (int(g[12]) + 101.325) / 101.325,
                "time_s"   : int(g[17]),
            })

    df = pd.DataFrame(rows).sort_values("time_s").reset_index(drop=True)
    ocv_baseline = df["V_stk"].iloc[:50].median()
    threshold    = OCV_THRESHOLD_FRAC * ocv_baseline

    vstk = df["V_stk"].to_numpy()
    t_load_start_idx = None
    for i in range(len(vstk) - 2):
        if vstk[i] < threshold and vstk[i+1] < threshold and vstk[i+2] < threshold:
            t_load_start_idx = i
            break
    if t_load_start_idx is None:
        raise RuntimeError("Could not detect load start in Exp-19.")

    search_vstk = vstk[t_load_start_idx:]
    dv = np.diff(search_vstk)
    jump_rel = int(np.argmax(dv))
    t_load_end_idx = t_load_start_idx + jump_rel + 1

    t_start = int(df["time_s"].iloc[t_load_start_idx])
    t_end   = int(df["time_s"].iloc[t_load_end_idx])
    print(f"Exp-19 load window: {t_start} s → {t_end} s  "
          f"(OCV = {ocv_baseline*1000:.0f} mV)")

    df["loaded"]   = (df["time_s"] >= t_start) & (df["time_s"] < t_end)
    df["I_load_A"] = np.where(df["loaded"], df["V_stk"] / R_LOAD_OHM_EXP19, 0.0)
    return df, t_start, t_end


# =============================================================================
# 5.  Plots
# =============================================================================
def plot_vi_overview(all_results, df_cal, label):
    """2×5 grid: calibration scatter + fitted V-I curve."""
    I_data = df_cal["I_A"].to_numpy(float)
    P_mean = float(df_cal["P_H2_atm"].mean())
    I_fine = np.linspace(max(I_data.min(), 1e-4), I_data.max() * 1.05, 300)
    P_fine = np.full_like(I_fine, P_mean)

    fig, axes = plt.subplots(2, 5, figsize=(20, 9))
    for k, row in enumerate(all_results):
        ax  = axes.flat[k]
        p   = {**{n: row[n] for n in PARAM_NAMES}, **SHARED}
        Vm  = df_cal[f"Cell{k}_V"].to_numpy(float)

        ax.scatter(I_data * 1000, Vm * 1000,
                   s=20, color="steelblue", zorder=5, alpha=0.6, label="Measured")
        ax.plot(I_fine * 1000, pemfc_voltage(I_fine, P_fine, p) * 1000,
                color="tomato", lw=1.5, label="Model")
        ax.set_title(f"Cell{k}  RMSE={row['RMSE_mV']:.2f} mV", fontsize=9)
        ax.set_xlabel("I [mA]", fontsize=8)
        ax.set_ylabel("V [mV]",  fontsize=8)
        ax.legend(fontsize=7)
        ax.grid(True, alpha=0.3)

    plt.suptitle(f"Experiment {label} — per-cell V-I fits", fontsize=11)
    plt.tight_layout()
    path = os.path.join(RESULTS_DIR, f"all_cells_overview_{label}.png")
    plt.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"  V-I overview → {path}")


def plot_exp19_timeseries(tuned_sets, df19):
    """
    For each set of new parameters evaluate on Exp-19 loaded window and
    overlay measured vs simulated time series.  One figure per parameter set.
    """
    loaded  = df19[df19["loaded"]].copy()
    t       = loaded["time_s"].to_numpy(float)
    I_arr   = loaded["I_load_A"].to_numpy(float)
    P_arr   = loaded["P_H2_atm"].to_numpy(float)

    for label, all_results in tuned_sets.items():
        fig, axes = plt.subplots(2, 5, figsize=(20, 9), sharex=True)
        for k, row in enumerate(all_results):
            ax    = axes.flat[k]
            p     = {**{n: row[n] for n in PARAM_NAMES}, **SHARED}
            V_meas = loaded[f"Cell{k}_V"].to_numpy(float) * 1000
            V_sim  = pemfc_voltage(I_arr, P_arr, p) * 1000
            rmse   = float(np.sqrt(np.mean((V_sim - V_meas)**2)))

            ax.plot(t, V_meas, color="steelblue", lw=1.0, alpha=0.75, label="Exp-19 meas")
            ax.plot(t, V_sim,  color="tomato",    lw=1.5, alpha=0.90,
                    label=f"Model ({label})")
            ax.set_title(f"Cell{k}  RMSE={rmse:.2f} mV", fontsize=9)
            ax.set_xlabel("Time [s]", fontsize=8)
            ax.set_ylabel("Voltage [mV]", fontsize=8)
            ax.legend(fontsize=7)
            ax.grid(alpha=0.3)

        plt.suptitle(
            f"Exp-19 time-series — params tuned on {label}", fontsize=11
        )
        plt.tight_layout()
        path = os.path.join(RESULTS_DIR, f"all_cells_exp19_params_{label}.png")
        plt.savefig(path, dpi=150, bbox_inches="tight")
        plt.close(fig)
        print(f"  Exp-19 time-series ({label}) → {path}")


def plot_rmse_comparison(tuned_sets, df19, exp19_csv):
    """
    Bar chart comparing per-cell RMSE on Exp-19 data for:
      - params tuned on exp 3.3 ohm
      - params tuned on exp 6.8 ohm
      - original params tuned on exp19 itself
    """
    loaded  = df19[df19["loaded"]].copy()
    I_arr   = loaded["I_load_A"].to_numpy(float)
    P_arr   = loaded["P_H2_atm"].to_numpy(float)

    # RMSE from new tuned sets evaluated on exp19
    rmse_data = {}
    for label, all_results in tuned_sets.items():
        rmse_data[label] = []
        for k, row in enumerate(all_results):
            p      = {**{n: row[n] for n in PARAM_NAMES}, **SHARED}
            V_meas = loaded[f"Cell{k}_V"].to_numpy(float)
            V_sim  = pemfc_voltage(I_arr, P_arr, p)
            rmse_data[label].append(float(np.sqrt(np.mean((V_sim - V_meas)**2))) * 1000)

    # RMSE from original exp19 params
    df_orig = pd.read_csv(exp19_csv)
    rmse_orig = []
    for k in range(10):
        row    = df_orig[df_orig["cell"] == f"Cell{k}"].iloc[0]
        p      = {**{n: float(row[n]) for n in PARAM_NAMES}, **SHARED}
        V_meas = loaded[f"Cell{k}_V"].to_numpy(float)
        V_sim  = pemfc_voltage(I_arr, P_arr, p)
        rmse_orig.append(float(np.sqrt(np.mean((V_sim - V_meas)**2))) * 1000)
    rmse_data["exp19 (own)"] = rmse_orig

    cells = [f"C{k}" for k in range(10)]
    x     = np.arange(10)
    n_grp = len(rmse_data)
    width = 0.8 / n_grp
    bar_colours = ["steelblue", "darkorange", "seagreen"]

    fig, ax = plt.subplots(figsize=(14, 6))
    for i, (lbl, vals) in enumerate(rmse_data.items()):
        offset = (i - n_grp / 2 + 0.5) * width
        ax.bar(x + offset, vals, width=width * 0.9,
               color=bar_colours[i % len(bar_colours)],
               label=f"params: {lbl}", alpha=0.85)

    ax.set_xticks(x)
    ax.set_xticklabels(cells)
    ax.set_ylabel("RMSE on Exp-19 [mV]")
    ax.set_title("Per-cell RMSE on Exp-19 data — three parameter sets compared")
    ax.legend()
    ax.grid(axis="y", alpha=0.3)
    plt.tight_layout()
    path = os.path.join(RESULTS_DIR, "rmse_comparison.png")
    plt.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"  RMSE comparison → {path}")


# =============================================================================
# 6.  Main
# =============================================================================
def main():
    tuned_sets = {}   # label → list of result dicts (one per cell)

    for label, csv_path in CSV_FILES.items():
        print(f"\n{'='*60}")
        print(f"Tuning on experiment: {label}  ({os.path.basename(csv_path)})")
        print(f"{'='*60}")

        df_cal, df_full = load_csv(csv_path, skip_frac=0.30, n_points=80)
        I_data    = df_cal["I_A"].to_numpy(float)
        P_H2_data = df_cal["P_H2_atm"].to_numpy(float)

        print(f"Calibration points : {len(df_cal)}")
        print(f"  I range : {I_data.min()*1000:.1f} – {I_data.max()*1000:.1f} mA")

        # Identify dead cells (will still produce a row but with large RMSE note)
        zero_cells = [k for k in range(10)
                      if df_cal[f"Cell{k}_V"].mean() < 0.010]
        if zero_cells:
            print(f"  WARNING: Cell(s) {zero_cells} near-zero → skipped in fit,"
                  " params set to NaN.\n")

        all_results = []
        for k in range(10):
            name   = f"Cell{k}"
            V_meas = df_cal[f"Cell{k}_V"].to_numpy(float)

            if k in zero_cells:
                # Dead cell — store NaN params
                all_results.append({
                    "cell": name, "RMSE_mV": float("nan"),
                    **{n: float("nan") for n in PARAM_NAMES},
                })
                print(f"  {name}: skipped (dead cell)")
                continue

            params, rmse, _ = fit_cell(I_data, P_H2_data, V_meas)
            all_results.append({
                "cell": name,
                "RMSE_mV": round(rmse * 1000, 4),
                **{n: params[n] for n in PARAM_NAMES},
            })
            print(f"  {name}: RMSE = {rmse*1000:.3f} mV")

        # Save CSV
        out_csv = os.path.join(RESULTS_DIR, f"tuned_params_{label}.csv")
        pd.DataFrame(all_results).to_csv(out_csv, index=False)
        print(f"  Params saved → {out_csv}")

        # V-I overview plot
        plot_vi_overview(all_results, df_cal, label)

        tuned_sets[label] = all_results

    # ── Apply new params on Exp-19 ─────────────────────────────────────────
    print(f"\n{'='*60}")
    print("Loading Exp-19 for cross-validation …")
    print(f"{'='*60}")

    df19, _, _ = parse_exp19(EXP19_LOG)

    # Time-series overlay figures
    plot_exp19_timeseries(tuned_sets, df19)

    # RMSE comparison bar chart
    exp19_orig_csv = os.path.join(RESULTS_DIR, "tuned_params_exp19.csv")
    plot_rmse_comparison(tuned_sets, df19, exp19_orig_csv)

    print("\nAll done.")


if __name__ == "__main__":
    main()
