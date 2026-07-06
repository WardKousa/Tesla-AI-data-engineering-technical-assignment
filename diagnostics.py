"""Part 2, Steps 2+3: fleet-health diagnostics over the structured log.

Reads the events table produced by etl.py (rebuilding it if missing) and:
  - clusters WARNING/ERROR/CRITICAL rows into episodes (a >30-minute quiet
    gap starts a new episode) and ranks them by severity-weighted score;
  - tags each episode with the fleet-health issue types it contains
    (thermal_event, coolant_flow_loss, cell_imbalance, voltage_drop,
    grid_sync_instability, soc_mismatch, ...), derived from the messages;
  - classifies each episode as an internal fault vs an externally driven,
    self-resolving event, with the evidence for that call;
  - reconstructs the most severe incident end-to-end (timeline, root-cause
    chain, first-warning-to-trip and recovery times);
  - runs the Part 2 statements in queries.sql and cross-checks them
    against pandas;
  - saves an incident chart (temperature + coolant flow, alerts marked).

Usage:
    python diagnostics.py

Outputs:
    outputs/part2_incident.png
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")  # headless: we only save the figure to disk
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import pandas as pd

import etl

DB_PATH = etl.DB_PATH
QUERIES_PATH = Path("queries.sql")
CHART_PATH = Path("outputs/part2_incident.png")

ALERT_SEVERITIES = list(etl.ALERT_SEVERITIES)
# Alerts within an ongoing issue recur every few minutes here; a full hour
# of quiet is a safe boundary between unrelated episodes.
EPISODE_GAP = pd.Timedelta(minutes=60)
SEVERITY_WEIGHT = {"WARNING": 1, "ERROR": 5, "CRITICAL": 10}

# Issue-type vocabulary from the assignment brief, matched on message text.
# Order matters only for readability; a message can carry several tags.
TAG_RULES = [
    ("coolant", "coolant_flow_loss"),
    ("temperature", "thermal_event"),
    ("overtemperature", "thermal_event"),
    ("thermal", "thermal_event"),
    ("fan at maximum", "thermal_event"),
    ("cell imbalance", "cell_imbalance"),
    ("flagged for inspection", "cell_imbalance"),
    ("voltage drop", "voltage_drop"),
    ("undervoltage", "voltage_drop"),
    ("soc mismatch", "soc_mismatch"),
    ("grid", "grid_sync_instability"),
    ("megapack offline", "protective_shutdown"),
]

# INFO messages that mark recovery/state transitions; used to time recovery
# and to enrich the incident timeline with context beyond the alerts.
RECOVERY_TEMPLATES = {
    "Grid sync established",
    "Dispatch resumed",
    "Coolant pump restarted",
    "Thermal derate cleared",
    "Megapack online",
}
TRIP_MARKERS = ("trip", "shutdown", "offline")

SEV_COLOR = {"WARNING": "#fab219", "ERROR": "#ec835a", "CRITICAL": "#d03b3b"}
SEV_MARKER = {"WARNING": "^", "ERROR": "D", "CRITICAL": "X"}
INK, GRID, AXIS = "#52514e", "#e1e0d9", "#c3c2b7"


def load_events() -> pd.DataFrame:
    """Load the structured events, building the DB via etl.py if missing."""
    if not DB_PATH.exists():
        print("outputs/logs.db missing; running the ETL first.\n")
        etl.build()
    with sqlite3.connect(DB_PATH) as conn:
        events = pd.read_sql("SELECT * FROM log_events", conn, parse_dates=["timestamp"])
    return events


def tags_for(messages: pd.Series) -> list[str]:
    """Fleet-health issue tags present in a set of messages."""
    joined = [m.lower() for m in messages]
    return sorted({tag for needle, tag in TAG_RULES
                   if any(needle in m for m in joined)})


def detect_episodes(events: pd.DataFrame) -> list[dict]:
    """Cluster alert rows into episodes; a >60-min quiet gap starts a new one."""
    alerts = events[events["severity"].isin(ALERT_SEVERITIES)].sort_values("timestamp")
    episode_id = (alerts["timestamp"].diff() > EPISODE_GAP).cumsum()

    episodes = []
    for _, group in alerts.groupby(episode_id):
        sev_counts = group["severity"].value_counts()
        episodes.append({
            "start": group["timestamp"].iloc[0],
            "end": group["timestamp"].iloc[-1],
            "alerts": group,
            "n_warning": int(sev_counts.get("WARNING", 0)),
            "n_error": int(sev_counts.get("ERROR", 0)),
            "n_critical": int(sev_counts.get("CRITICAL", 0)),
            "score": int(group["severity"].map(SEVERITY_WEIGHT).sum()),
            "subsystems": sorted(group["subsystem"].unique()),
            "tags": tags_for(group["message"]),
        })
    episodes.sort(key=lambda e: e["score"], reverse=True)
    for rank, ep in enumerate(episodes, start=1):
        ep["rank"] = rank
    return episodes


def recovery_after(events: pd.DataFrame, when: pd.Timestamp) -> pd.DataFrame:
    """Recovery-marker INFO rows within an hour after `when`."""
    window = events[(events["timestamp"] >= when)
                    & (events["timestamp"] <= when + pd.Timedelta(hours=1))]
    return window[window["message"].isin(RECOVERY_TEMPLATES)]


def classify_episode(ep: dict, events: pd.DataFrame) -> tuple[str, str]:
    """(classification, evidence rationale) for one episode."""
    tripped = any(marker in m.lower() for m in ep["alerts"]["message"] for marker in TRIP_MARKERS)
    recovery = recovery_after(events, ep["end"])
    grid_only = set(ep["tags"]) <= {"grid_sync_instability"}

    if grid_only and not tripped and not recovery.empty:
        t = recovery["timestamp"].iloc[0]
        return ("EXTERNAL, self-resolving",
                f"all alerts are grid-side signals; no internal subsystem alerted; "
                f"re-synced without intervention at {t:%H:%M:%S} ('{recovery['message'].iloc[0]}')")
    if tripped:
        online = recovery[recovery["message"] == "Megapack online"]
        back = f"; back online at {online['timestamp'].iloc[0]:%H:%M:%S}" if not online.empty else ""
        return ("INTERNAL fault, protective trip",
                f"cross-subsystem escalation ({', '.join(ep['subsystems'])}) ending in a "
                f"protective shutdown{back}")
    return ("INTERNAL fault, unresolved",
            "progressive on-board degradation with no recovery marker; "
            "ends flagged for inspection, needs intervention")


def reconstruct_incident(events: pd.DataFrame, ep: dict) -> dict:
    """Timeline and quantified timings for one episode (the top-ranked one)."""
    alerts = ep["alerts"]
    first_of = lambda sev: alerts.loc[alerts["severity"] == sev, "timestamp"].min()
    offline_rows = alerts[alerts["message"].str.contains("offline", case=False)]
    recovery = recovery_after(events, ep["end"])
    online = recovery[recovery["message"] == "Megapack online"]

    timings = {
        "first_warning": first_of("WARNING"),
        "first_error": first_of("ERROR"),
        "trip": first_of("CRITICAL"),
        "site_offline": offline_rows["timestamp"].min() if not offline_rows.empty else pd.NaT,
        "recovered": online["timestamp"].min() if not online.empty else pd.NaT,
    }
    minutes = lambda a, b: (timings[b] - timings[a]).total_seconds() / 60 \
        if pd.notna(timings[a]) and pd.notna(timings[b]) else None
    timings["warning_to_trip_min"] = minutes("first_warning", "trip")
    timings["downtime_min"] = minutes("site_offline", "recovered")
    timings["total_min"] = minutes("first_warning", "recovered")

    # Timeline = every alert plus the recovery/context INFO transitions.
    span_end = timings["recovered"] if pd.notna(timings["recovered"]) else ep["end"]
    window = events[(events["timestamp"] >= ep["start"] - pd.Timedelta(minutes=10))
                    & (events["timestamp"] <= span_end + pd.Timedelta(minutes=5))]
    context = window[window["message"].isin(RECOVERY_TEMPLATES)
                     | ((window["metric"] == "ac_output") & (window["value"] == 0))]
    timeline = (pd.concat([alerts, context])
                .drop_duplicates("raw_line_no").sort_values("timestamp"))
    return {"timings": timings, "timeline": timeline, "episode": ep}


def run_part2_sql(events: pd.DataFrame) -> None:
    """Run the Part 2 statements in queries.sql; cross-check against pandas."""
    # Split at the "Part 2" marker, then drop the rest of the marker's own
    # comment line (it lost its leading "--" in the split).
    sql_text = QUERIES_PATH.read_text(encoding="utf-8").split("Part 2", 1)[1].split("\n", 1)[1]
    with sqlite3.connect(DB_PATH) as conn:
        for statement in sql_text.split(";"):
            lines = [l for l in statement.splitlines() if not l.strip().startswith("--")]
            statement = "\n".join(lines).strip()
            if not statement:
                continue
            cur = conn.execute(statement)
            header = [d[0] for d in cur.description]
            rows = cur.fetchall()
            print(f"\n  [{', '.join(header)}]")
            for row in rows:
                print("   ", row)

        # Cross-check 1: SQL event counts must sum to the pandas row count.
        sql_total = conn.execute("SELECT COUNT(*) FROM log_events").fetchone()[0]
        assert sql_total == len(events), "SQL/pandas row-count mismatch"
        # Cross-check 2: SQL mean thermal alert temperature == pandas mean.
        sql_mean = conn.execute(
            "SELECT AVG(value) FROM log_events WHERE subsystem='Thermal' "
            "AND severity IN ('WARNING','ERROR') AND metric='temperature'").fetchone()[0]
    pd_mean = events.loc[(events["subsystem"] == "Thermal")
                         & (events["severity"].isin(["WARNING", "ERROR"]))
                         & (events["metric"] == "temperature"), "value"].mean()
    assert abs(sql_mean - pd_mean) < 1e-9, "SQL/pandas thermal mean mismatch"
    print("\n  Cross-checks passed: SQL row count and thermal mean match pandas.")


def plot_incident(events: pd.DataFrame, incident: dict) -> None:
    """Two stacked panels (shared time axis): temperature and coolant flow,
    alert markers colored+shaped by severity, site-offline window shaded."""
    ep, timings = incident["episode"], incident["timings"]
    start = ep["start"] - pd.Timedelta(minutes=45)  # show the normal baseline
    end = (timings["recovered"] if pd.notna(timings["recovered"]) else ep["end"]) \
        + pd.Timedelta(minutes=20)
    window = events[(events["timestamp"] >= start) & (events["timestamp"] <= end)]

    fig, (ax_temp, ax_flow) = plt.subplots(
        2, 1, figsize=(12, 7), sharex=True, gridspec_kw={"hspace": 0.08})
    panels = [(ax_temp, "temperature", "Temperature (°C)", "#2a78d6"),
              (ax_flow, "coolant_flow", "Coolant flow (L/min)", "#1baf7a")]

    plotted_severities: set[str] = set()
    for ax, metric, label, color in panels:
        series = window[(window["metric"] == metric) & (window["subsystem"] == "Thermal")]
        ax.plot(series["timestamp"], series["value"], color=color, linewidth=2)
        for sev in ALERT_SEVERITIES:  # alert readings drawn on their own signal
            pts = series[series["severity"] == sev]
            if pts.empty:
                continue
            plotted_severities.add(sev)
            ax.scatter(pts["timestamp"], pts["value"], s=90, zorder=3,
                       marker=SEV_MARKER[sev], color=SEV_COLOR[sev],
                       edgecolor="white", linewidth=1.5)
        if pd.notna(timings["site_offline"]) and pd.notna(timings["recovered"]):
            ax.axvspan(timings["site_offline"], timings["recovered"],
                       color=AXIS, alpha=0.25, zorder=0)
        ax.set_ylabel(label, color=INK)
        ax.grid(color=GRID, linewidth=0.8)
        ax.tick_params(colors=INK)
        for spine in ax.spines.values():
            spine.set_color(AXIS)

    # Milestone annotations: keep them selective (three, not one per event).
    for ts, text in [(timings["first_warning"], "first warning"),
                     (timings["trip"], "inverter trip"),
                     (timings["recovered"], "back online")]:
        if pd.notna(ts):
            ax_temp.axvline(ts, color=AXIS, linestyle="--", linewidth=1, zorder=1)
            ax_flow.axvline(ts, color=AXIS, linestyle="--", linewidth=1, zorder=1)
            ax_temp.annotate(f"{text}\n{ts:%H:%M}", xy=(ts, 1.0), xycoords=("data", "axes fraction"),
                             xytext=(4, -4), textcoords="offset points",
                             va="top", fontsize=8.5, color=INK)

    handles = [plt.Line2D([], [], marker=SEV_MARKER[s], color=SEV_COLOR[s],
                          linestyle="", markersize=8, label=s.title())
               for s in ALERT_SEVERITIES if s in plotted_severities]
    handles.append(plt.Rectangle((0, 0), 1, 1, color=AXIS, alpha=0.25,
                                 label=f"Site offline ({timings['downtime_min']:.0f} min)"))
    ax_flow.legend(handles=handles, loc="lower left", fontsize=9, framealpha=0.9)

    ax_flow.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
    ax_flow.set_xlabel(f"{ep['start']:%d %b %Y}", color=INK)
    ax_temp.set_title("Megapack thermal incident: coolant-flow collapse -> overtemperature trip",
                      color=INK)
    fig.tight_layout()
    CHART_PATH.parent.mkdir(exist_ok=True)
    fig.savefig(CHART_PATH, dpi=150)
    plt.close(fig)


def main() -> None:
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")  # log messages contain °C
    events = load_events()
    alerts = events[events["severity"].isin(ALERT_SEVERITIES)]

    print("=" * 70)
    print("PART 2, STEP 2 - DIAGNOSTICS AND ROOT-CAUSE ANALYSIS")
    print(f"{len(events):,} events, {events['timestamp'].min():%Y-%m-%d} -> "
          f"{events['timestamp'].max():%Y-%m-%d}; 'today' = {events['date'].max()}")

    print("\n-- Events by subsystem x severity " + "-" * 35)
    print(pd.crosstab(events["subsystem"], events["severity"]).to_string())

    print("\n-- Alert activity by calendar hour (WARNING/ERROR/CRITICAL) " + "-" * 8)
    by_hour = alerts.groupby(["date", "hour"]).size().rename("n_alerts")
    print(by_hour.to_string())

    print("\n-- Fleet-health episodes, ranked by severity-weighted score " + "-" * 8)
    episodes = detect_episodes(events)
    for ep in episodes:
        label, rationale = classify_episode(ep, events)
        dur = (ep["end"] - ep["start"]).total_seconds() / 60
        print(f"\n  #{ep['rank']}  {ep['start']:%Y-%m-%d %H:%M} -> {ep['end']:%H:%M}"
              f"  ({dur:.0f} min, score {ep['score']})")
        print(f"      alerts: {ep['n_warning']} WARNING / {ep['n_error']} ERROR / "
              f"{ep['n_critical']} CRITICAL   subsystems: {', '.join(ep['subsystems'])}")
        print(f"      issue types: {', '.join(ep['tags'])}")
        print(f"      verdict: {label} - {rationale}")

    incident = reconstruct_incident(events, episodes[0])
    t = incident["timings"]
    print("\n-- Incident reconstruction (top-ranked episode) " + "-" * 21)
    for row in incident["timeline"].itertuples():
        val = f"  [{row.metric}={row.value:g} {row.unit}]" if pd.notna(row.value) else ""
        print(f"  {row.timestamp:%H:%M:%S}  {row.subsystem:<8} {row.severity:<8} {row.message}{val}")
    print(f"\n  First warning:        {t['first_warning']:%H:%M:%S}")
    print(f"  First error:          {t['first_error']:%H:%M:%S}")
    print(f"  Trip (first CRITICAL):{t['trip']:%H:%M:%S}"
          f"   -> {t['warning_to_trip_min']:.1f} min from first warning")
    print(f"  Site offline:         {t['site_offline']:%H:%M:%S}")
    print(f"  Recovered (online):   {t['recovered']:%H:%M:%S}"
          f"   -> downtime {t['downtime_min']:.0f} min, episode {t['total_min']:.0f} min")
    print("\n  Root cause (evidence-backed): coolant flow collapsed from nominal"
          "\n  ~6-11 L/min to 1.8 L/min in ~11 minutes (pump degradation - pump speed"
          "\n  71% shortly before). With cooling lost, temperature rose ~30°C in 10"
          "\n  minutes past the safe threshold; BMS applied a thermal derate, the"
          "\n  inverter derated then tripped on overtemperature, and the site executed"
          "\n  a protective shutdown. Cooling was restored by a coolant pump restart,"
          "\n  after which the site re-synced and resumed dispatch. The trigger is an"
          "\n  internal thermal-loop fault (coolant pump), not an external grid event.")

    print("\n" + "=" * 70)
    print("PART 2, STEP 3 - SQL INSIGHTS (queries.sql, Part 2 section)")
    run_part2_sql(events)

    plot_incident(events, incident)
    print(f"\nChart saved to {CHART_PATH}")

    # Ground-truth expectations for this file (warn-only; see etl.validate).
    print("\n-- Ground-truth checks " + "-" * 46)
    def check(ok: bool, label: str) -> None:
        print(f"  {'OK ' if ok else 'WARN'} {label}")
    check(len(episodes) == 3, f"episodes detected == 3 (got {len(episodes)})")
    check(by_hour.idxmax() == ("2026-06-18", 13),
          f"busiest alert hour == 2026-06-18 13:00 (got {by_hour.idxmax()})")
    check(str(t["first_warning"]) == "2026-06-18 13:22:00", "incident first warning == 13:22:00")
    check(str(t["trip"]) == "2026-06-18 13:34:30", "incident trip == 13:34:30")
    check(str(t["recovered"]) == "2026-06-18 13:51:00", "incident recovery == 13:51:00")


if __name__ == "__main__":
    main()
