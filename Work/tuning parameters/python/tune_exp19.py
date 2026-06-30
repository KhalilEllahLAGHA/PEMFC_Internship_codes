"""
tune_exp19.py  —  Parameter tuning for experiment_04_03_2026_19

Differences from tune_cells.py (experiment 15):
  - Experiment 19 uses a fixed external resistor (R_LOAD_OHM = 25 Ω).
    There is NO valve-driven current control.  The load is identified
    by the instant the stack voltage drops below 60 % of the pre-load OCV.
  - Current is computed as   I(t) = V_stack(t) / R_LOAD_OHM   for every
    loaded timestep.
  - The first 30 % of the loaded window is discarded (transient start-up).
    The remaining settled data are passed directly to the optimiser.
  - Results are saved to  tuned_params_exp19.csv  and  *_exp19.png  so that
    the experiment-15 outputs are not overwritten.
"""

import re, os
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from scipy.optimize import differential_evolution, minimize

# ---------------------------------------------------------------------------
# Clean, report-quality plotting style
# ---------------------------------------------------------------------------
plt.rcParams.update({
    "figure.dpi"      : 200,
    "savefig.dpi"     : 200,
    "font.size"       : 12,
    "axes.titlesize"  : 12,
    "axes.labelsize"  : 11,
    "legend.fontsize" : 9,
    "xtick.labelsize" : 10,
    "ytick.labelsize" : 10,
    "axes.grid"       : True,
    "grid.alpha"      : 0.3,
    "axes.spines.top"   : False,
    "axes.spines.right" : False,
})

# ── Paths ──────────────────────────────────────────────────────────────────────
SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
LOG_FILE    = os.path.join(SCRIPT_DIR, "experiment_04_03_2026_19.txt")
RESULTS_DIR = os.path.join(SCRIPT_DIR, "results")
os.makedirs(RESULTS_DIR, exist_ok=True)

# ── Experiment constant ────────────────────────────────────────────────────────
R_LOAD_OHM = 25.0    # fixed resistor [Ω]

# ── Fixed physical / geometric parameters shared across all cells ──────────────
SHARED = {
    "T_K"      : 343.15,
    "P_O2_atm" : 0.21,
    "lambda_m" : 14.0,
    "A_fc_m2"  : 25e-4,
    "t_m_m"    : 178e-6,
    "i_max_A"  : 0.25,
}

PARAM_NAMES  = ["xi1", "xi2", "xi3", "xi4", "B", "c"]
PARAM_BOUNDS = [
    (-8.00,  2.00),   # xi1
    ( 0.00,  0.03),   # xi2
    (-5e-3,  5e-3),   # xi3
    (-0.01,  0.00),   # xi4
    ( 0.00,  2.00),   # B
    ( 0.00,  0.01),   # c
]
COLOURS = plt.cm.tab10.colors


# =============================================================================
# 1.  Log parser
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
    """
    Read the log, compute V_stack = sum(Cell0..9) [V], and detect the load.

    Load-start rule
    ---------------
    The first 50 rows are assumed to be pre-load (OCV).
    Load start = first timestep where V_stack stays below
    OCV_THRESHOLD_FRAC * median_OCV for three consecutive samples.

    Current rule (loaded window only)
    ------------------------------------
        I(t)  =  V_stack(t) / R_LOAD_OHM   (0 outside the loaded window)

    Load-end rule
    -------------
    First timestep AFTER load start where V_stack rises back above the
    threshold for 3 consecutive samples (resistor was removed).
    """
    OCV_THRESHOLD_FRAC = 0.60

    rows = []
    with open(filepath, "r", errors="ignore") as fh:
        for line in fh:
            m = _ROW_PAT.search(line)
            if not m:
                continue
            g = m.groups()
            cells = [int(g[k]) / 1000.0 for k in range(10)]
            v_stk = sum(cells)
            rows.append({
                **{f"Cell{k}_V": cells[k] for k in range(10)},
                "V_stk"    : v_stk,
                "P_H2_atm" : (int(g[12]) + 101.325) / 101.325,
                "time_s"   : int(g[17]),
            })

    df = pd.DataFrame(rows).sort_values("time_s").reset_index(drop=True)

    # ── Detect load start ──────────────────────────────────────────────────────
    ocv_baseline = df["V_stk"].iloc[:50].median()
    threshold    = OCV_THRESHOLD_FRAC * ocv_baseline

    t_load_start_idx = None
    vstk = df["V_stk"].to_numpy()
    for i in range(len(vstk) - 2):
        if vstk[i] < threshold and vstk[i+1] < threshold and vstk[i+2] < threshold:
            t_load_start_idx = i
            break

    if t_load_start_idx is None:
        raise RuntimeError(
            "Could not find a sustained voltage drop — check the log file."
        )

    t_load_start = int(df["time_s"].iloc[t_load_start_idx])

    # ── Detect load end: largest positive voltage jump after load start ───────
    # The resistor removal produces the sharpest single-step voltage rise in the
    # entire post-load-start window.  That step index is the exact change point.
    search_vstk = vstk[t_load_start_idx:]
    dv           = np.diff(search_vstk)
    jump_rel     = int(np.argmax(dv))           # index of the largest positive jump
    t_load_end_idx = t_load_start_idx + jump_rel + 1  # first sample AFTER the jump

    t_load_end = int(df["time_s"].iloc[t_load_end_idx])

    print(f"OCV baseline: {ocv_baseline*1000:.0f} mV  "
          f"| threshold: {threshold*1000:.0f} mV  "
          f"| load start: t = {t_load_start} s  "
          f"| load end: t = {t_load_end} s")

    df["loaded"]   = (df["time_s"] >= t_load_start) & (df["time_s"] < t_load_end)
    df["I_load_A"] = np.where(df["loaded"], df["V_stk"] / R_LOAD_OHM, 0.0)

    return df, t_load_start, t_load_end, ocv_baseline


# =============================================================================
# 2.  Build calibration dataset from the settled loaded window
# =============================================================================
def build_calibration(df, skip_frac=0.30, n_points=80):
    """
    Take the loaded rows, discard the first `skip_frac` fraction (transient),
    then uniformly downsample to `n_points` calibration rows.

    Returns a DataFrame with columns: I_A, P_H2_atm, Cell0_V … Cell9_V.
    """
    loaded = df[df["loaded"] & (df["I_load_A"] > 1e-4)].copy()
    if loaded.empty:
        raise RuntimeError("No loaded data found after filtering.")

    n         = len(loaded)
    settled   = loaded.iloc[int(skip_frac * n) :].reset_index(drop=True)

    # Uniform downsample
    if len(settled) > n_points:
        idx     = np.round(np.linspace(0, len(settled) - 1, n_points)).astype(int)
        settled = settled.iloc[idx].reset_index(drop=True)

    df_cal = pd.DataFrame({
        "I_A"      : settled["I_load_A"].to_numpy(float),
        "P_H2_atm" : settled["P_H2_atm"].to_numpy(float),
        **{f"Cell{k}_V": settled[f"Cell{k}_V"].to_numpy(float) for k in range(10)},
    })
    return df_cal


# =============================================================================
# 3.  Semi-empirical PEMFC voltage model
# =============================================================================
def pemfc_voltage(I_A, P_H2_atm, p):
    T, P_O2  = p["T_K"], max(p["P_O2_atm"], 1e-12)
    A_fc, t_m, i_max = p["A_fc_m2"], p["t_m_m"], p["i_max_A"]
    xi1, xi2, xi3, xi4 = p["xi1"], p["xi2"], p["xi3"], p["xi4"]
    B, c = p["B"], p["c"]

    I     = np.maximum(np.asarray(I_A,      float), 1e-6)
    P_H2  = np.maximum(np.asarray(P_H2_atm, float), 1e-9)
    I_raw = np.asarray(I_A, float)

    E     = 1.229 - 0.85e-3*(T-298.15) + 4.3085e-5*T*(np.log(P_H2) + 0.5*np.log(P_O2))
    C_O2  = P_O2 / (5.1e6 * np.exp(-498.0 / T))
    v_act = xi1 + xi2*T + xi3*T*np.log(C_O2) + xi4*T*np.log(I)
    sigma = max(float((0.5139*p["lambda_m"] - 0.326)
                      * np.exp(1268*(1/303.15 - 1/T))), 1e-8)
    v_ohm = I_raw * (t_m / (A_fc * sigma) + c)
    v_con = -B * np.log(1.0 - np.clip(I_raw / i_max, 0.0, 0.999999))

    return E - v_act - v_ohm - v_con


# =============================================================================
# 4.  Optimiser: DE global search + L-BFGS-B polish
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
# 5.  Plots
# =============================================================================
def plot_vi_overview(all_results, df_cal, df_raw, t_load_start, ocv_baseline):
    """2×5 grid: measured calibration points vs fitted model curve."""
    I_data    = df_cal["I_A"].to_numpy(float)
    P_H2_data = df_cal["P_H2_atm"].to_numpy(float)
    I_fine    = np.linspace(max(I_data.min(), 1e-4), I_data.max() * 1.05, 300)

    fig, axes = plt.subplots(2, 5, figsize=(20, 8))
    for k, row in enumerate(all_results):
        ax = axes.flat[k]
        p  = {**{n: row[n] for n in PARAM_NAMES}, **SHARED}
        Vm = df_cal[f"Cell{k}_V"].to_numpy(float)
        degraded = Vm.mean() < 0.010

        # Use mean P_H2 for the model curve
        P_mean = P_H2_data.mean()
        P_fine = np.full_like(I_fine, P_mean)

        ax.scatter(I_data * 1000, Vm * 1000,
                   s=22, color="steelblue", zorder=5, alpha=0.65, label="Measured")
        ax.plot(I_fine * 1000, pemfc_voltage(I_fine, P_fine, p) * 1000,
                color="tomato", lw=2.0, label="Model fit")
        tag = "  (degraded)" if degraded else ""
        ax.set_title(f"Cell {k} — RMSE {row['RMSE_mV']:.2f} mV{tag}",
                     color=("crimson" if degraded else "black"))
        if k >= 5:
            ax.set_xlabel("Current [mA]")
        if k % 5 == 0:
            ax.set_ylabel("Cell voltage [mV]")
        ax.legend(loc="best")

    plt.suptitle(
        "Optimization Experiment — per-cell voltage–current calibration fits",
        fontsize=14, fontweight="bold")
    plt.tight_layout()
    path = os.path.join(RESULTS_DIR, "all_cells_overview_exp19.png")
    plt.savefig(path, bbox_inches="tight")
    plt.close(fig)
    print(f"V-I overview  → {path}")


def plot_time_series_fit(all_results, df_raw):
    """
    Overlay simulated vs measured cell voltage for the full loaded time window.
    """
    loaded  = df_raw[df_raw["loaded"]].copy()
    t       = loaded["time_s"].to_numpy(float)
    I_arr   = loaded["I_load_A"].to_numpy(float)
    P_arr   = loaded["P_H2_atm"].to_numpy(float)

    fig, axes = plt.subplots(2, 5, figsize=(20, 9), sharex=True)
    for k, row in enumerate(all_results):
        ax = axes.flat[k]
        p  = {**{n: row[n] for n in PARAM_NAMES}, **SHARED}
        V_meas = loaded[f"Cell{k}_V"].to_numpy(float) * 1000
        V_sim  = pemfc_voltage(I_arr, P_arr, p) * 1000

        ax.plot(t, V_meas, color="steelblue", lw=1.0, alpha=0.75, label="Measured")
        ax.plot(t, V_sim,  color="tomato",    lw=1.5, alpha=0.90, label="Simulated")
        ax.set_title(f"Cell{k}  RMSE={row['RMSE_mV']:.2f} mV", fontsize=9)
        ax.set_xlabel("Time [s]", fontsize=8)
        ax.set_ylabel("Voltage [mV]", fontsize=8)
        ax.legend(fontsize=7)
        ax.grid(alpha=0.3)

    plt.suptitle("Experiment 19 — time-series comparison (loaded window)", fontsize=11)
    plt.tight_layout()
    path = os.path.join(RESULTS_DIR, "time_series_fit_exp19.png")
    plt.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"Time-series fit → {path}")


def plot_stack_overview(df_raw, t_load_start, t_load_end, ocv_baseline):
    """Stack voltage and computed current over the full experiment."""
    t     = df_raw["time_s"].to_numpy(float)
    vstk  = df_raw["V_stk"].to_numpy(float)
    I_arr = df_raw["I_load_A"].to_numpy(float)

    fig, axes = plt.subplots(2, 1, figsize=(14, 7), sharex=True)

    ax = axes[0]
    ax.plot(t, vstk * 1000, color="steelblue", lw=1.5)
    ax.axvline(t_load_start, color="red",        lw=1, ls="--", label=f"Load start (t={t_load_start} s)")
    if t_load_end is not None:
        ax.axvline(t_load_end, color="darkorange", lw=1, ls="--", label=f"Load end   (t={t_load_end} s)")
    ax.axhline(ocv_baseline * 1000, color="gray", lw=1, ls=":",
               label=f"OCV baseline = {ocv_baseline*1000:.0f} mV")
    ax.set_ylabel("Stack voltage [mV]")
    ax.legend(fontsize=8)
    ax.grid(alpha=0.3)
    ax.set_title("Experiment 19 — stack overview")

    ax = axes[1]
    ax.plot(t, I_arr * 1000, color="darkgreen", lw=1.5)
    ax.axvline(t_load_start, color="red", lw=1, ls="--")
    if t_load_end is not None:
        ax.axvline(t_load_end, color="darkorange", lw=1, ls="--")
    ax.set_ylabel("Load current  [mA]")
    ax.set_xlabel("Time [s]")
    ax.grid(alpha=0.3)

    plt.tight_layout()
    path = os.path.join(RESULTS_DIR, "stack_overview_exp19.png")
    plt.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"Stack overview  → {path}")


def plot_cost_evolution(histories):
    fig, axes = plt.subplots(2, 5, figsize=(20, 8))
    for k, (name, hist) in enumerate(histories.items()):
        ax = axes.flat[k]
        h  = np.array(hist) * 1000.0
        g  = np.arange(1, len(h) + 1)
        ax.semilogy(g[:-1], h[:-1], color=COLOURS[k], lw=2.0)
        ax.axhline(h[-1], color=COLOURS[k], lw=1.2, ls="--", alpha=0.7)
        ax.scatter(g[-1], h[-1], color=COLOURS[k], s=55, zorder=5,
                   label=f"Final RMSE = {h[-1]:.2f} mV")
        ax.set_title(f"Cell {k}")
        if k >= 5:
            ax.set_xlabel("DE generation")
        if k % 5 == 0:
            ax.set_ylabel("RMSE [mV]")
        ax.legend(loc="best")
        ax.grid(True, which="both", alpha=0.3)
    plt.suptitle("Differential-evolution convergence per cell — Optimization Experiment",
                 fontsize=14, fontweight="bold")
    plt.tight_layout()
    path = os.path.join(RESULTS_DIR, "cost_evolution_exp19.png")
    plt.savefig(path, bbox_inches="tight")
    plt.close(fig)
    print(f"Cost evolution  → {path}")


# =============================================================================
# 6.  Main
# =============================================================================
def main():
    # ── Load and parse ─────────────────────────────────────────────────────────
    df_raw, t_load_start, t_load_end, ocv_baseline = parse_log(LOG_FILE)

    # ── Calibration dataset ────────────────────────────────────────────────────
    df_cal = build_calibration(df_raw, skip_frac=0.30, n_points=80)

    I_data    = df_cal["I_A"].to_numpy(float)
    P_H2_data = df_cal["P_H2_atm"].to_numpy(float)

    print(f"\nCalibration points: {len(df_cal)}")
    print(f"  I range  : {I_data.min()*1000:.2f} – {I_data.max()*1000:.2f} mA")
    print(f"  P_H2 range: {P_H2_data.min():.3f} – {P_H2_data.max():.3f} atm")

    # Warn about cells that read 0 V under load (cannot be fitted)
    zero_cells = []
    for k in range(10):
        if df_cal[f"Cell{k}_V"].mean() < 0.010:   # mean < 10 mV
            zero_cells.append(k)
    if zero_cells:
        print(f"\n  WARNING: Cell(s) {zero_cells} show near-zero voltage under load.")
        print("  These cells may have hardware issues; RMSE for them will be large.\n")

    # ── Fit each cell ──────────────────────────────────────────────────────────
    all_results, histories = [], {}
    for k in range(10):
        name   = f"Cell{k}"
        V_meas = df_cal[f"Cell{k}_V"].to_numpy(float)
        params, rmse, hist = fit_cell(I_data, P_H2_data, V_meas)
        histories[name] = hist
        all_results.append({
            "cell": name,
            "RMSE_mV": round(rmse * 1000, 4),
            **{n: params[n] for n in PARAM_NAMES},
        })
        print(f"  {name}: RMSE = {rmse*1000:.3f} mV")

    # ── Save parameters ────────────────────────────────────────────────────────
    df_out   = pd.DataFrame(all_results)
    csv_path = os.path.join(RESULTS_DIR, "tuned_params_exp19.csv")
    df_out.to_csv(csv_path, index=False, float_format="%.8g")
    print(f"\nParams saved → {csv_path}")
    print(df_out.to_string(index=False))

    # ── Generate plots ─────────────────────────────────────────────────────────
    plot_stack_overview(df_raw, t_load_start, t_load_end, ocv_baseline)
    plot_vi_overview(all_results, df_cal, df_raw, t_load_start, ocv_baseline)
    plot_time_series_fit(all_results, df_raw)
    plot_cost_evolution(histories)


if __name__ == "__main__":
    main()
