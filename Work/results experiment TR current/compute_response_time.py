"""
compute_response_time.py
========================
Computes response-time metrics for both TR-current experiments:
  - Stack current  I  [mA]
  - Stack voltage  V  [V]

The profile after resistor connection is a decaying step:
    y(t) = y_ss + Δy · exp(−t / τ)

Metrics computed (for each signal)
-----------------------------------
  y_ss      — steady-state value  (mean of last 20 % of samples)
  Δy        — step amplitude = y(0) − y_ss
  τ         — time constant from least-squares exponential fit
  T_5pct    — settling time  −τ·ln(0.05)
  T_2pct    — settling time  −τ·ln(0.02)
  T_fall    — fall time 90 % → 10 %  (from measured signal)

Outputs
-------
  response_time_results.csv          — all metrics (current + voltage)
  response_time_plot.png             — annotated current-response plot
  voltage_response_time_plot.png     — annotated voltage-response plot
"""

import os
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from scipy.optimize import curve_fit

# ---------------------------------------------------------------------------
# Clean, report-quality plotting style
# ---------------------------------------------------------------------------
plt.rcParams.update({
    "figure.dpi"      : 200,
    "savefig.dpi"     : 200,
    "font.size"       : 12,
    "axes.titlesize"  : 13,
    "axes.labelsize"  : 12,
    "legend.fontsize" : 10,
    "xtick.labelsize" : 11,
    "ytick.labelsize" : 11,
    "axes.grid"       : True,
    "grid.alpha"      : 0.3,
    "axes.spines.top"   : False,
    "axes.spines.right" : False,
    "lines.linewidth" : 1.8,
})

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

EXPERIMENTS = {
    "3.3 Ω": os.path.join(SCRIPT_DIR, "experiment 3.3 ohm.csv"),
    "6.8 Ω": os.path.join(SCRIPT_DIR, "experiment 6.8 ohm.csv"),
}

COLOURS = {"3.3 Ω": "steelblue", "6.8 Ω": "darkorange"}

# ---------------------------------------------------------------------------
# Exponential model
# ---------------------------------------------------------------------------
def exp_model(t, y_ss, delta_y, tau):
    return y_ss + delta_y * np.exp(-t / tau)


# ---------------------------------------------------------------------------
# Generic analysis function
# ---------------------------------------------------------------------------
def analyse(label, t, y, y_label, units):
    """Fit an exponential decay to signal y(t) and return metrics + fit curve."""
    # Steady-state: mean of last 20 %
    n_ss  = max(int(0.20 * len(y)), 5)
    y_ss  = float(y[-n_ss:].mean())
    delta = float(y[0] - y_ss)

    # Exponential fit
    try:
        lo = min(y_ss * 0.8, y_ss * 1.2)
        hi = max(y_ss * 0.8, y_ss * 1.2)
        # delta can be positive or negative — allow both directions
        d_lo = min(delta * 0.0, delta * 2.0)
        d_hi = max(delta * 0.0, delta * 2.0)
        if d_lo == d_hi:          # fallback when delta≈0
            d_lo, d_hi = -abs(delta)*3, abs(delta)*3
        bounds = ([lo, d_lo, 0.1], [hi, d_hi, 1000.0])
        popt, _ = curve_fit(exp_model, t, y, p0=[y_ss, delta, 30.0],
                            bounds=bounds, maxfev=20000)
        y_ss_fit, d_fit, tau = float(popt[0]), float(popt[1]), float(popt[2])
    except Exception:
        y_ss_fit, d_fit, tau = y_ss, delta, float("nan")

    # Settling times (analytical from fit)
    T_5pct = float(-tau * np.log(0.05)) if not np.isnan(tau) else float("nan")
    T_2pct = float(-tau * np.log(0.02)) if not np.isnan(tau) else float("nan")

    # Fall/rise time 90 % → 10 %
    sign  = np.sign(delta)
    lvl90 = y_ss + sign * 0.90 * abs(delta)
    lvl10 = y_ss + sign * 0.10 * abs(delta)
    t90 = t10 = float("nan")
    for i in range(len(y)):
        if sign * (y[i] - lvl90) <= 0:
            t90 = float(t[i]); break
    for i in range(len(y)):
        if sign * (y[i] - lvl10) <= 0:
            t10 = float(t[i]); break
    T_fall = (t10 - t90) if not (np.isnan(t90) or np.isnan(t10)) else float("nan")

    metrics = {
        "Experiment"        : label,
        "Signal"            : y_label,
        f"ss [{units}]"      : round(y_ss, 4),
        f"Δ [{units}]"       : round(delta, 4),
        "τ [s]"             : round(tau, 2)  if not np.isnan(tau)   else "N/A",
        "T_5pct [s]"        : round(T_5pct, 1) if not np.isnan(T_5pct) else "N/A",
        "T_2pct [s]"        : round(T_2pct, 1) if not np.isnan(T_2pct) else "N/A",
        "T_fall 90-10% [s]" : round(T_fall, 1) if not np.isnan(T_fall) else "N/A",
    }

    fit_curve = exp_model(t, y_ss_fit, d_fit, tau) if not np.isnan(tau) else None
    return t, y, y_ss, delta, tau, T_5pct, T_2pct, t90, t10, fit_curve, metrics, lvl90, lvl10


# ---------------------------------------------------------------------------
# Annotated plot (works for any signal)
# ---------------------------------------------------------------------------
def make_plot(results_dict, ylabel, title, outname):
    """Clean two-panel response-time figure.

    Only the elements needed to read the dynamics are shown: the measured
    signal, the exponential fit (with its time constant), the steady-state
    level, the +/-2 % settling band, and the 2 % settling-time marker. The
    crowded 5 % band and the 90 %/10 % construction lines are intentionally
    omitted to keep the figure legible at report size.
    """
    fig, axes = plt.subplots(1, 2, figsize=(13, 5))
    for ax, (label, res) in zip(axes, results_dict.items()):
        t, y, y_ss, delta, tau, T_5pct, T_2pct, t90, t10, fit_curve, _, lvl90, lvl10 = res
        colour = COLOURS[label]
        band2  = 0.02 * abs(delta)

        ax.plot(t, y, color=colour, lw=2.0, label="Measured")
        if fit_curve is not None:
            ax.plot(t, fit_curve, color="black", lw=2.0, ls="--",
                    label=fr"Exponential fit, $\tau$ = {tau:.1f} s")
        ax.axhline(y_ss, color="gray", lw=1.2, ls=":",
                   label=f"Steady state = {y_ss:.0f} mA")
        ax.axhspan(y_ss - band2, y_ss + band2, color="limegreen", alpha=0.25,
                   label="±2 % band")
        if not np.isnan(T_2pct):
            ax.axvline(T_2pct, color="seagreen", lw=1.6, ls="--",
                       label=fr"$T_{{s,2\%}}$ = {T_2pct:.0f} s")
        ax.set_title(f"Load step, R = {label}", fontweight="bold")
        ax.set_xlabel("Time after connection [s]")
        ax.set_ylabel(ylabel)
        ax.legend(loc="upper right", framealpha=0.9)

    plt.suptitle(title, fontsize=14, fontweight="bold")
    plt.tight_layout()
    out = os.path.join(SCRIPT_DIR, outname)
    plt.savefig(out)
    plt.close(fig)
    print(f"  Plot saved : {outname}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    all_rows = []
    I_results = {}
    V_results = {}

    for label, fpath in EXPERIMENTS.items():
        print(f"\nAnalysing: {label}")
        df = pd.read_csv(fpath)
        t  = df["time_s"].to_numpy(float)
        I  = df["Current_mA"].to_numpy(float)
        V  = df["V_stack_V"].to_numpy(float)

        # Current
        res_I = analyse(label, t, I, "Current", "mA")
        I_results[label] = res_I
        m = res_I[-3]   # metrics dict
        print(f"  [Current]  ss={m['ss [mA]']} mA  Δ={m['Δ [mA]']} mA  "
              f"τ={m['τ [s]']} s  Ts2%={m['T_2pct [s]']} s  Tfall={m['T_fall 90-10% [s]']} s")
        all_rows.append(m)

        # Voltage
        res_V = analyse(label, t, V, "Voltage", "V")
        V_results[label] = res_V
        m = res_V[-3]
        print(f"  [Voltage]  ss={m['ss [V]']} V  Δ={m['Δ [V]']} V  "
              f"τ={m['τ [s]']} s  Ts2%={m['T_2pct [s]']} s  Tfall={m['T_fall 90-10% [s]']} s")
        all_rows.append(m)

    # Save combined CSV
    csv_out = os.path.join(SCRIPT_DIR, "response_time_results.csv")
    pd.DataFrame(all_rows).to_csv(csv_out, index=False)
    print(f"\n  Results saved: response_time_results.csv")

    # Plot (current response only — the metric used in the report)
    make_plot(I_results, "Stack current [mA]",
              "Current step response to a resistive load",
              "response_time_plot.png")
    print("\nDone.")


