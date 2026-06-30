import re, os
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from scipy.optimize import differential_evolution, minimize

# -- Paths --------------------------------------------------------------------
SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
LOG_FILE    = os.path.join(SCRIPT_DIR, "experiment_04_03_2026_15.txt")
RESULTS_DIR = os.path.join(SCRIPT_DIR, "results")
os.makedirs(RESULTS_DIR, exist_ok=True)

# -- Experiment constant ------------------------------------------------------
R_LOAD_OHM = 25.0   # external resistor wired across the stack [Ohm]

# -- Fixed physical/geometric parameters shared across all cells --------------
SHARED = {
    "T_K"      : 343.15,   # 70 degC
    "P_O2_atm" : 0.21,     # open-air cathode
    "lambda_m" : 14.0,     # membrane water content
    "A_fc_m2"  : 25e-4,    # active area [m^2]
    "t_m_m"    : 178e-6,   # membrane thickness [m]
    "i_max_A"  : 0.25,     # limiting current [A]
}

# -- Tunable parameters: names and search bounds ------------------------------
# xi4 <= 0 : activation loss must increase with current
# B   >= 0 : concentration loss is always a voltage drop
# c   >= 0 : ohmic/contact resistance is always positive
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
# 1. Log parser
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
                **{f"Cell{k}_V": int(g[k]) / 1000.0 for k in range(10)},
                "P_H2_atm": (int(g[12]) + 101.325) / 101.325,
                "I_load_A": v_stk / R_LOAD_OHM if valve > 0 else 0.0,
                "valve"   : valve,
                "time_s"  : int(g[17]),
            })
    return pd.DataFrame(rows).sort_values("time_s").reset_index(drop=True)


# =============================================================================
# 2. Build polarization curves
#    Average the settled second half of each valve plateau -> one calibration
#    point per plateau. Both up- and down-sweeps are kept, doubling the data.
# =============================================================================
def build_polarization_curves(df):
    df["block"] = (df["valve"] != df["valve"].shift()).cumsum()
    records = []
    for _, grp in df.groupby("block"):
        if grp["valve"].iloc[0] == 0:
            continue
        settled = grp.iloc[len(grp) // 2:]
        row = {"I_A": settled["I_load_A"].mean(), "P_H2_atm": settled["P_H2_atm"].mean()}
        for k in range(10):
            row[f"Cell{k}_V"] = settled[f"Cell{k}_V"].mean()
        records.append(row)
    return pd.DataFrame(records).sort_values("I_A").reset_index(drop=True)


# =============================================================================
# 3. Semi-empirical PEMFC voltage model
#    V = E(Nernst) - v_act(activation) - v_ohm(ohmic) - v_con(concentration)
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
    sigma = max(float((0.5139*p["lambda_m"] - 0.326) * np.exp(1268*(1/303.15 - 1/T))), 1e-8)
    v_ohm = I_raw * (t_m / (A_fc * sigma) + c)
    v_con = -B * np.log(1.0 - np.clip(I_raw / i_max, 0.0, 0.999999))

    return E - v_act - v_ohm - v_con


# =============================================================================
# 4. Optimiser: DE global search followed by L-BFGS-B local polish
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
# 5. Plots
# =============================================================================
def plot_vi_overview(all_results, df_iv, I_data, P_H2_data):
    """2x5 grid: measured vs. model V-I curve for every cell."""
    fig, axes = plt.subplots(2, 5, figsize=(18, 8))
    I_fine    = np.linspace(I_data.min(), I_data.max(), 300)
    P_H2_fine = np.interp(I_fine, I_data, P_H2_data)

    for k, row in enumerate(all_results):
        ax = axes.flat[k]
        p  = {**{n: row[n] for n in PARAM_NAMES}, **SHARED}
        Vm = df_iv[f"Cell{k}_V"].to_numpy(float)
        ax.scatter(I_data, Vm * 1000, s=30, color="steelblue", zorder=5, label="Measured")
        ax.plot(I_fine, pemfc_voltage(I_fine, P_H2_fine, p) * 1000,
                color="tomato", lw=1.5, label="Model")
        ax.set_title(f"Cell{k}  RMSE={row['RMSE_mV']:.2f} mV", fontsize=9)
        ax.set_xlabel("I [A]", fontsize=8); ax.set_ylabel("V [mV]", fontsize=8)
        ax.legend(fontsize=7); ax.grid(True, alpha=0.3)

    plt.suptitle("PEM stack - per-cell V-I model fits", fontsize=12)
    plt.tight_layout()
    path = os.path.join(RESULTS_DIR, "all_cells_overview.png")
    plt.savefig(path, dpi=150, bbox_inches="tight"); plt.close(fig)
    print(f"V-I overview -> {path}")


def plot_cost_evolution(histories):
    """Per-cell 2x5 grid and overlay plot of RMSE vs. DE generation (log scale)."""
    # Per-cell grid
    fig, axes = plt.subplots(2, 5, figsize=(18, 8))
    for k, (name, hist) in enumerate(histories.items()):
        ax = axes.flat[k]
        h  = np.array(hist) * 1000.0
        g  = np.arange(1, len(h) + 1)
        ax.semilogy(g[:-1], h[:-1], color=COLOURS[k], lw=1.5)
        ax.axhline(h[-1], color=COLOURS[k], lw=1, ls="--", alpha=0.7)
        ax.scatter(g[-1], h[-1], color=COLOURS[k], s=50, zorder=5,
                   label=f"Final={h[-1]:.2f} mV")
        ax.set_title(name, fontsize=9)
        ax.set_xlabel("Generation", fontsize=8); ax.set_ylabel("RMSE [mV]", fontsize=8)
        ax.legend(fontsize=7); ax.grid(True, which="both", alpha=0.3)
    plt.suptitle("Cost evolution - differential evolution per cell", fontsize=12)
    plt.tight_layout()
    path = os.path.join(RESULTS_DIR, "cost_evolution_grid.png")
    plt.savefig(path, dpi=150, bbox_inches="tight"); plt.close(fig)
    print(f"Cost grid -> {path}")

    # All-cells overlay
    fig, ax = plt.subplots(figsize=(10, 5))
    for k, (name, hist) in enumerate(histories.items()):
        h = np.array(hist) * 1000.0
        g = np.arange(1, len(h) + 1)
        ax.semilogy(g[:-1], h[:-1], color=COLOURS[k], lw=1.5, label=name)
        ax.scatter(g[-1], h[-1], color=COLOURS[k], s=40, marker="x", zorder=5)
    ax.set_xlabel("DE Generation", fontsize=10); ax.set_ylabel("Best RMSE [mV]", fontsize=10)
    ax.set_title("Cost evolution - all cells overlay", fontsize=11)
    ax.legend(fontsize=8, ncol=2); ax.grid(True, which="both", alpha=0.3)
    plt.tight_layout()
    path = os.path.join(RESULTS_DIR, "cost_evolution_overlay.png")
    plt.savefig(path, dpi=150); plt.close(fig)
    print(f"Cost overlay -> {path}")


# =============================================================================
# 6. Main
# =============================================================================
def main():
    # Load data and build calibration curves
    df_raw = parse_log(LOG_FILE)
    df_iv  = build_polarization_curves(df_raw)
    if df_iv.empty:
        raise RuntimeError("No loaded plateaus found - check the log file.")

    I_data    = df_iv["I_A"].to_numpy(float)
    P_H2_data = df_iv["P_H2_atm"].to_numpy(float)
    print(f"Data: {len(df_iv)} calibration points, "
          f"I={I_data.min():.3f}-{I_data.max():.3f} A, "
          f"P_H2={P_H2_data.min():.3f}-{P_H2_data.max():.3f} atm")

    # Fit each cell individually
    all_results, histories = [], {}
    for k in range(10):
        name   = f"Cell{k}"
        V_meas = df_iv[f"Cell{k}_V"].to_numpy(float)
        params, rmse, hist = fit_cell(I_data, P_H2_data, V_meas)
        histories[name] = hist
        all_results.append({"cell": name, "RMSE_mV": round(rmse*1000, 4),
                             **{n: params[n] for n in PARAM_NAMES}})
        print(f"  {name}: RMSE={rmse*1000:.3f} mV")

    # Save parameters to CSV
    df_out   = pd.DataFrame(all_results)
    csv_path = os.path.join(RESULTS_DIR, "tuned_params.csv")
    df_out.to_csv(csv_path, index=False, float_format="%.8g")
    print(f"\nParams saved -> {csv_path}")
    print(df_out.to_string(index=False))

    # Generate plots
    plot_vi_overview(all_results, df_iv, I_data, P_H2_data)
    plot_cost_evolution(histories)


if __name__ == "__main__":
    main()
