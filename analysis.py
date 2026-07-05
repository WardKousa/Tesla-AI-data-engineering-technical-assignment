"""Part 1: BESS performance and revenue analysis.

Analyzes 15-minute operational/market data for a 200 MWh UK BESS and
recommends the 14-day maintenance-outage window in Q1 2024 with the
lowest revenue impact.

Revenue convention (stated explicitly, as the assignment requires):
    revenue_gbp = power_mw * 0.25 h * market_price_gbp_mwh
This is NET revenue: discharging (positive power) earns money, charging
(negative power) is a cost. Net revenue is the decision-relevant metric
for the outage question, because an offline site loses discharge income
but also avoids charging cost. Gross discharge value is reported
alongside for transparency.

Usage:
    python analysis.py [path/to/merged_bess_market_data.csv]

Outputs:
    outputs/bess.db                  SQLite database of the readings
    outputs/part1_outage_window.png  daily revenue chart with the
                                     recommended outage window shaded
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")  # headless: we only save the figure to disk
import matplotlib.pyplot as plt
import pandas as pd

DATA_PATH = Path("data/merged_bess_market_data.csv")
DB_PATH = Path("outputs/bess.db")
QUERIES_PATH = Path("queries.sql")
CHART_PATH = Path("outputs/part1_outage_window.png")

INTERVAL_HOURS = 0.25  # data is on a 15-minute grid
OUTAGE_DAYS = 14
QUARTER_START = pd.Timestamp("2024-01-01")
QUARTER_END = pd.Timestamp("2024-03-31")  # window must end on/before this day

EXPECTED_COLUMNS = [
    "Timestamp",
    "SOC (%)",
    "Power (MW)",
    "Frequency Regulation Signal (Hz)",
    "Operational Mode",
    "Market Price (GBP/MWh)",
]


def load_data(path: Path) -> pd.DataFrame:
    """Load and validate the raw CSV, and add derived columns.

    Validation fails fast with a clear message rather than producing
    silently wrong numbers if a different/corrupted file is supplied.
    """
    df = pd.read_csv(path, parse_dates=["Timestamp"])

    missing = [c for c in EXPECTED_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"{path}: missing expected columns {missing}")
    if df[EXPECTED_COLUMNS].isna().any().any():
        raise ValueError(f"{path}: contains null values; inspect before analysis")
    if df["Timestamp"].duplicated().any():
        raise ValueError(f"{path}: contains duplicate timestamps")
    if not df["Timestamp"].is_monotonic_increasing:
        raise ValueError(f"{path}: timestamps are not sorted ascending")
    steps = df["Timestamp"].diff().dropna().unique()
    if len(steps) != 1 or steps[0] != pd.Timedelta(minutes=15):
        raise ValueError(f"{path}: expected a gapless 15-minute grid, got steps {steps}")

    # Interval power (MW) -> interval energy (MWh); signed net revenue (GBP).
    df["energy_mwh"] = df["Power (MW)"] * INTERVAL_HOURS
    df["revenue_gbp"] = df["energy_mwh"] * df["Market Price (GBP/MWh)"]
    df["date"] = df["Timestamp"].dt.date
    return df


def energy_summary(df: pd.DataFrame) -> dict[str, float]:
    """Total energy discharged and charged over the full dataset (MWh)."""
    discharged = df.loc[df["energy_mwh"] > 0, "energy_mwh"].sum()
    charged = -df.loc[df["energy_mwh"] < 0, "energy_mwh"].sum()
    return {
        "discharged_mwh": discharged,
        "charged_mwh": charged,
        "ratio_discharged_over_charged": discharged / charged,
    }


def revenue_by_mode(df: pd.DataFrame) -> pd.DataFrame:
    """Average daily revenue per operational mode, net and gross.

    A mode's daily revenue is summed per calendar date, then averaged
    over the dates on which that mode was active (not all calendar
    days) — this answers "how much does a day of running this mode
    earn", which is the operationally meaningful rate.
    """
    gross = df["revenue_gbp"].clip(lower=0)  # discharge income only
    daily = (
        df.assign(gross_gbp=gross)
        .groupby(["date", "Operational Mode"])[["revenue_gbp", "gross_gbp"]]
        .sum()
        .groupby("Operational Mode")
        .mean()
        .rename(
            columns={
                "revenue_gbp": "avg_daily_net_gbp",
                "gross_gbp": "avg_daily_gross_gbp",
            }
        )
    )
    return daily.round(2)


def find_worst_window(df: pd.DataFrame, days: int = OUTAGE_DAYS) -> dict:
    """Lowest-net-revenue N-day window fully inside Q1 2024.

    Daily net revenue -> rolling N-day sum, sliding one day at a time.
    The last valid start date is QUARTER_END - (N - 1) days so the
    window never leaves the quarter.
    """
    q1 = df[(df["Timestamp"] >= QUARTER_START) & (df["Timestamp"] < QUARTER_END + pd.Timedelta(days=1))]
    daily = q1.groupby("date")["revenue_gbp"].sum()
    daily.index = pd.to_datetime(daily.index)

    rolling = daily.rolling(days).sum().dropna()  # indexed by window END date
    end = rolling.idxmin()
    start = end - pd.Timedelta(days=days - 1)
    return {
        "start": start,
        "end": end,
        "window_revenue_gbp": rolling.min(),
        "daily_revenue": daily,
        "rolling": rolling,
        "avg_window_gbp": rolling.mean(),
        "worst_case_window_gbp": rolling.max(),
    }


def run_sql(df: pd.DataFrame) -> list[tuple[str, list[tuple]]]:
    """Load readings into SQLite and run every statement in queries.sql.

    SQLite (stdlib) keeps the deliverable dependency-free and lets the
    reviewer re-run queries.sql against outputs/bess.db directly.
    """
    DB_PATH.parent.mkdir(exist_ok=True)
    DB_PATH.unlink(missing_ok=True)

    table = df.rename(
        columns={
            "Timestamp": "timestamp",
            "SOC (%)": "soc_pct",
            "Power (MW)": "power_mw",
            "Frequency Regulation Signal (Hz)": "freq_reg_signal_hz",
            "Operational Mode": "operational_mode",
            "Market Price (GBP/MWh)": "market_price_gbp_mwh",
        }
    )
    table["timestamp"] = table["timestamp"].astype(str)
    table["date"] = table["date"].astype(str)

    results = []
    with sqlite3.connect(DB_PATH) as conn:
        table.to_sql("bess_readings", conn, index=False)
        sql_text = QUERIES_PATH.read_text()
        part1_sql = sql_text.split("Part 2")[0]  # only Part 1 statements
        for statement in part1_sql.split(";"):
            # Drop comment lines; skip chunks with no actual SQL.
            lines = [l for l in statement.splitlines() if not l.strip().startswith("--")]
            statement = "\n".join(lines).strip()
            if not statement:
                continue
            cur = conn.execute(statement)
            header = [d[0] for d in cur.description]
            results.append((", ".join(header), cur.fetchall()))
    return results


def plot_outage_window(window: dict) -> None:
    """Chart: Q1 daily net revenue + 14-day rolling total, chosen window shaded."""
    daily = window["daily_revenue"]
    rolling = window["rolling"]

    fig, ax1 = plt.subplots(figsize=(12, 5))
    ax1.bar(daily.index, daily.values, width=0.8, color="#9ecae1", label="Daily net revenue")
    ax1.axvspan(
        window["start"],
        window["end"],
        color="#d62728",
        alpha=0.18,
        label=f"Recommended outage {window['start']:%d %b} – {window['end']:%d %b}",
    )
    ax1.set_ylabel("Daily net revenue (GBP)")
    ax1.set_xlabel("Date (Q1 2024)")

    ax2 = ax1.twinx()
    # Plot the rolling sum at the window START date so the line aligns
    # with where each candidate outage would begin.
    ax2.plot(rolling.index - pd.Timedelta(days=OUTAGE_DAYS - 1), rolling.values,
             color="#d62728", linewidth=2, label="14-day window total (at start date)")
    ax2.set_ylabel("14-day window net revenue (GBP)")

    lines1, labels1 = ax1.get_legend_handles_labels()
    lines2, labels2 = ax2.get_legend_handles_labels()
    ax1.legend(lines1 + lines2, labels1 + labels2, loc="upper left", fontsize=9)
    ax1.set_title("Q1 2024 daily net revenue and 14-day outage-window cost")
    fig.tight_layout()
    CHART_PATH.parent.mkdir(exist_ok=True)
    fig.savefig(CHART_PATH, dpi=150)
    plt.close(fig)


def main() -> None:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else DATA_PATH
    df = load_data(path)

    print("=" * 70)
    print("PART 1 - BESS PERFORMANCE AND REVENUE ANALYSIS")
    print(f"Data: {path}  ({len(df):,} rows, "
          f"{df['Timestamp'].min()} -> {df['Timestamp'].max()})")

    energy = energy_summary(df)
    print("\n-- Step 1a: Total energy throughput " + "-" * 33)
    print(f"Total discharged: {energy['discharged_mwh']:>12,.1f} MWh")
    print(f"Total charged:    {energy['charged_mwh']:>12,.1f} MWh")
    ratio = energy["ratio_discharged_over_charged"]
    print(f"Discharged/charged ratio: {ratio:.3f}")
    if ratio > 1:
        print("  NOTE: ratio > 1 is physically implausible for a real battery "
              "(round-trip losses mean energy out < energy in); flags this as "
              "synthetic/simulated data.")

    print("\n-- Step 1b: Average daily revenue per mode (GBP/day) " + "-" * 16)
    print("Net = discharge income minus charging cost (primary metric).")
    print("Gross = discharge income only.")
    print(revenue_by_mode(df).to_string())

    window = find_worst_window(df)
    print("\n-- Step 1c: Lowest-revenue 14-day window in Q1 2024 " + "-" * 17)
    print(f"Recommended outage: {window['start']:%Y-%m-%d} -> {window['end']:%Y-%m-%d}")
    print(f"Net revenue at stake:    GBP {window['window_revenue_gbp']:>12,.0f}")
    print(f"Average 14-day window:  GBP {window['avg_window_gbp']:>12,.0f}")
    print(f"Worst-case (max) window:GBP {window['worst_case_window_gbp']:>12,.0f}")
    saving = window["avg_window_gbp"] - window["window_revenue_gbp"]
    print(f"Saving vs average window: GBP {saving:,.0f}")

    print("\n-- Step 2: SQL insights (SQLite, queries.sql) " + "-" * 23)
    for header, rows in run_sql(df):
        print(f"\n  [{header}]")
        for row in rows:
            print("   ", row)

    plot_outage_window(window)
    print(f"\nChart saved to {CHART_PATH}")
    print(f"SQLite DB saved to {DB_PATH}")

    # Cross-check: pandas total must equal the sum of SQL monthly totals.
    with sqlite3.connect(DB_PATH) as conn:
        sql_total = conn.execute("SELECT SUM(revenue_gbp) FROM bess_readings").fetchone()[0]
    assert abs(sql_total - df["revenue_gbp"].sum()) < 1e-6, "pandas/SQL revenue mismatch"
    print("Cross-check passed: pandas total revenue == SQLite total revenue.")


if __name__ == "__main__":
    main()
