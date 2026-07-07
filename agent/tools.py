"""Part 2, Step 4: the five tools the agent harness routes to.

Every tool is a plain, deterministic Python function over the structured
log database (outputs/logs.db). All numbers, timestamps, charts and JSON
the agent ever outputs are computed HERE, by ordinary code -- the LLM (or
the fallback router) only decides which tool to call and phrases the
answer. Tools return JSON-serializable dicts; anything the tools cannot
provide comes back as an explicit {"error": ...} rather than a guess,
which is what lets the harness say "I can't answer that" instead of
hallucinating.
"""

from __future__ import annotations

import sys
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:  # allow `python agent/...` as well as `python -m agent...`
    sys.path.insert(0, str(ROOT))

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import pandas as pd

import diagnostics
from diagnostics import AXIS, GRID, INK, SEV_COLOR, SEV_MARKER

SUBSYSTEMS = {"BMS", "Battery", "Inverter", "Site", "Thermal", "UNKNOWN"}
SEVERITIES = {"INFO", "WARNING", "ERROR", "CRITICAL", "UNKNOWN"}
METRICS = {"temperature", "coolant_flow", "fan_speed", "pump_speed", "voltage",
           "cell_voltage", "cell_voltage_diff", "charge_current",
           "discharge_current", "soc", "grid_frequency", "ac_output", "power_limit"}
METRIC_COLORS = ["#2a78d6", "#1baf7a", "#eda100", "#4a3aa7"]  # fixed assignment order
MAX_ROWS = 100
MIN_POINTS_TO_PLOT = 2  # a line needs two points; sparser metrics are dropped + reported

# Per-issue-type knowledge used by the ticket tool. Everything episode-specific
# (title, metrics, actions) is looked up from the episode's own tags at runtime,
# so the tool works for ANY episode, not just the thermal incident.
TAG_PHRASES = {  # human-readable fragment per issue type, for ticket titles
    "coolant_flow_loss": "coolant-flow loss",
    "thermal_event": "overtemperature",
    "protective_shutdown": "protective shutdown",
    "cell_imbalance": "progressive cell imbalance",
    "voltage_drop": "voltage drop",
    "soc_mismatch": "SOC mismatch",
    "grid_sync_instability": "grid-sync instability",
}
TAG_METRICS = {  # dense, relevant evidence signals per issue type
    "coolant_flow_loss": ["coolant_flow", "temperature"],
    "thermal_event": ["temperature", "fan_speed"],
    "protective_shutdown": ["ac_output"],
    "cell_imbalance": ["cell_voltage_diff", "voltage"],
    "voltage_drop": ["voltage"],
    "soc_mismatch": ["soc"],
    "grid_sync_instability": ["grid_frequency", "ac_output"],
}
# Recommended action per fleet-health issue type, used by the ticket tool.
ACTIONS = {
    "coolant_flow_loss": "Inspect and service/replace the coolant pump; verify loop "
                         "pressure and check for leaks or blockages before restart.",
    "thermal_event": "Verify thermal sensors and fan operation; review temperature "
                     "trends after the coolant loop is serviced.",
    "protective_shutdown": "Run the safe-restart checklist and confirm all derates "
                           "cleared before returning the site to full dispatch.",
    "cell_imbalance": "Inspect the flagged battery module and run cell-balancing "
                      "diagnostics.",
    "voltage_drop": "Check interconnects and cell health on the affected module.",
    "soc_mismatch": "Recalibrate SOC estimation across modules.",
    "grid_sync_instability": "No action required: externally driven grid event that "
                             "self-resolved.",
}


@lru_cache(maxsize=1)
def _events() -> pd.DataFrame:
    return diagnostics.load_events()


def _bounds() -> tuple[pd.Timestamp, pd.Timestamp]:
    ts = _events()["timestamp"]
    return ts.min(), ts.max()


def _parse_when(text: str | None, name: str) -> pd.Timestamp | None:
    """Parse a user-supplied time; error clearly if outside the data."""
    if text is None:
        return None
    try:
        when = pd.Timestamp(text)
    except ValueError:
        raise ValueError(f"{name}={text!r} is not a valid timestamp")
    lo, hi = _bounds()
    if when < lo - pd.Timedelta(days=1) or when > hi + pd.Timedelta(days=1):
        raise ValueError(f"{name}={text!r} is outside the data range {lo} .. {hi}")
    return when


def query_events(subsystem: str | None = None, severity: str | None = None,
                 metric: str | None = None, start: str | None = None,
                 end: str | None = None, limit: int = 20) -> dict:
    """Filtered log events, newest limits first kept chronological."""
    for value, valid, label in ((subsystem, SUBSYSTEMS, "subsystem"),
                                (severity, SEVERITIES, "severity"),
                                (metric, METRICS, "metric")):
        if value is not None and value not in valid:
            return {"error": f"unknown {label} {value!r}; valid: {sorted(valid)}"}
    try:
        t0, t1 = _parse_when(start, "start"), _parse_when(end, "end")
    except ValueError as exc:
        return {"error": str(exc)}

    df = _events()
    if subsystem:
        df = df[df["subsystem"] == subsystem]
    if severity:
        df = df[df["severity"] == severity]
    if metric:
        df = df[df["metric"] == metric]
    if t0 is not None:
        df = df[df["timestamp"] >= t0]
    if t1 is not None:
        df = df[df["timestamp"] <= t1]

    limit = max(1, min(int(limit), MAX_ROWS))
    rows = df.head(limit)[["timestamp", "subsystem", "severity", "message",
                           "metric", "value", "unit", "module"]].copy()
    rows["timestamp"] = rows["timestamp"].astype(str)
    records = rows.astype(object).where(rows.notna(), None).to_dict("records")
    result = {"total_matching": int(len(df)), "returned": int(len(rows)),
              "events": records}
    if len(df) == 0:
        lo, hi = _bounds()
        result["note"] = f"no matching events; the log covers {lo} to {hi}"
    return result


def event_stats(group_by: list[str] | None = None, subsystem: str | None = None,
                severity: str | None = None, metric: str | None = None,
                date: str | None = None) -> dict:
    """Counts grouped by the given columns; value stats when metric is set."""
    group_by = group_by or ["subsystem", "severity"]
    valid_groups = {"subsystem", "severity", "date", "hour", "metric", "message_template"}
    bad = [g for g in group_by if g not in valid_groups]
    if bad:
        return {"error": f"cannot group by {bad}; valid: {sorted(valid_groups)}"}

    df = _events()
    for col, value in (("subsystem", subsystem), ("severity", severity),
                       ("metric", metric), ("date", date)):
        if value is not None:
            df = df[df[col] == value]
    if df.empty:
        return {"total": 0, "note": "no events match those filters",
                "counts": []}

    counts = (df.groupby(group_by).size().rename("n_events")
              .reset_index().to_dict("records"))
    result = {"total": int(len(df)), "counts": counts}
    if metric:
        values = df["value"].dropna()
        result["value_stats"] = {"metric": metric, "unit": df["unit"].dropna().iloc[0],
                                 "min": float(values.min()), "max": float(values.max()),
                                 "mean": round(float(values.mean()), 2),
                                 "n": int(len(values))}
    return result


def plot_signals(metrics: list[str], start: str | None = None, end: str | None = None,
                 highlight_alerts: bool = True, around_incident: bool = False) -> dict:
    """Chart the given metrics over time (one stacked panel per metric).

    around_incident=True centres the window on the most severe episode.
    Saves a PNG to outputs/ and returns its path.
    """
    metrics = list(dict.fromkeys(metrics))  # dedupe, keep order
    unknown = [m for m in metrics if m not in METRICS]
    if unknown or not metrics:
        return {"error": f"unknown metrics {unknown}; valid: {sorted(METRICS)}"}
    if len(metrics) > len(METRIC_COLORS):
        return {"error": f"at most {len(METRIC_COLORS)} metrics per chart"}

    events = _events()
    if around_incident:
        incident = diagnostics.reconstruct_incident(
            events, diagnostics.detect_episodes(events)[0])
        t0 = incident["episode"]["start"] - pd.Timedelta(minutes=45)
        recovered = incident["timings"]["recovered"]
        t1 = (recovered if pd.notna(recovered) else incident["episode"]["end"]) \
            + pd.Timedelta(minutes=20)
    else:
        try:
            t0, t1 = _parse_when(start, "start"), _parse_when(end, "end")
        except ValueError as exc:
            return {"error": str(exc)}
        lo, hi = _bounds()
        t0, t1 = t0 or lo, t1 or hi

    window = events[(events["timestamp"] >= t0) & (events["timestamp"] <= t1)]
    # A metric with <2 points in the window cannot form a line; allocating a
    # panel for it yields a blank, autoscaled axis. Filter first, report drops.
    counts = {m: int((window["metric"] == m).sum()) for m in metrics}
    plotted = [m for m in metrics if counts[m] >= MIN_POINTS_TO_PLOT]
    skipped = {m: counts[m] for m in metrics if counts[m] < MIN_POINTS_TO_PLOT}
    if not plotted:
        return {"error": f"no plottable metrics between {t0} and {t1} (a line "
                         f"needs >= {MIN_POINTS_TO_PLOT} points); point counts: {counts}"}

    fig, axes = plt.subplots(len(plotted), 1, figsize=(12, 3.2 * len(plotted)),
                             sharex=True, squeeze=False)
    for ax, metric, color in zip(axes.ravel(), plotted, METRIC_COLORS):
        series = window[window["metric"] == metric]
        ax.plot(series["timestamp"], series["value"], color=color, linewidth=2)
        if highlight_alerts:
            for sev in ("WARNING", "ERROR", "CRITICAL"):
                pts = series[series["severity"] == sev]
                if pts.empty:
                    continue
                ax.scatter(pts["timestamp"], pts["value"], s=90, zorder=3,
                           marker=SEV_MARKER[sev], color=SEV_COLOR[sev],
                           edgecolor="white", linewidth=1.5, label=sev.title())
        unit = series["unit"].dropna().iloc[0] if not series["value"].dropna().empty else ""
        ax.set_ylabel(f"{metric} ({unit})" if unit else metric, color=INK)
        ax.grid(color=GRID, linewidth=0.8)
        ax.tick_params(colors=INK)
        for spine in ax.spines.values():
            spine.set_color(AXIS)
    handles, labels = axes.ravel()[0].get_legend_handles_labels()
    if handles:
        axes.ravel()[0].legend(handles, labels, loc="best", fontsize=9)
    axes.ravel()[-1].xaxis.set_major_formatter(mdates.DateFormatter("%d %b %H:%M"))
    axes.ravel()[0].set_title(", ".join(plotted) + f"  ({t0:%Y-%m-%d %H:%M} to {t1:%H:%M})",
                              color=INK)
    fig.tight_layout()
    path = Path("outputs") / f"agent_plot_{'_'.join(plotted)}.png"
    path.parent.mkdir(exist_ok=True)
    fig.savefig(path, dpi=150)
    plt.close(fig)
    return {"chart_path": str(path), "metrics": plotted, "skipped_sparse": skipped,
            "n_points": counts, "window": [str(t0), str(t1)],
            "alerts_highlighted": bool(highlight_alerts)}


def _tags_in_causal_order(ep: dict) -> list[str]:
    """Episode tags ordered by when each issue type first appears in the
    alert sequence (ep["tags"] is alphabetical; titles read better causally)."""
    ordered: list[str] = []
    for message in ep["alerts"]["message"]:
        lowered = message.lower()
        for needle, tag in diagnostics.TAG_RULES:
            if needle in lowered and tag in ep["tags"] and tag not in ordered:
                ordered.append(tag)
    return ordered


def _episode_evidence_chart(ep: dict, timings: dict) -> dict:
    """Evidence chart for one episode: its tag-relevant signals over its own
    window (plus surrounding baseline), alerts highlighted. Shared by the
    ticket and summary tools so both attach identical evidence."""
    metrics: list[str] = []
    for tag in _tags_in_causal_order(ep):
        for metric in TAG_METRICS.get(tag, []):
            if metric not in metrics:
                metrics.append(metric)
    window_start = ep["start"] - pd.Timedelta(minutes=45)
    window_end = (timings["recovered"] if pd.notna(timings["recovered"])
                  else ep["end"]) + pd.Timedelta(minutes=20)
    return plot_signals(metrics[:len(METRIC_COLORS)], start=str(window_start),
                        end=str(window_end), highlight_alerts=True)


def _root_cause(ep: dict, events: pd.DataFrame) -> str:
    """Evidence-derived root-cause statement for any episode: the first fault
    signal (which is the causal origin in an escalation) plus classification."""
    first = ep["alerts"].iloc[0]
    label, rationale = diagnostics.classify_episode(ep, events)
    return (f"First fault signal: '{first['message']}' ({first['subsystem']}, "
            f"{first['timestamp']}). {label}: {rationale}.")


def _episode_json(ep: dict, events: pd.DataFrame) -> dict:
    label, rationale = diagnostics.classify_episode(ep, events)
    return {"rank": ep["rank"], "start": str(ep["start"]), "end": str(ep["end"]),
            # first alert -> last alert; NOT downtime (see summarize timings)
            "alert_span_min": round((ep["end"] - ep["start"]).total_seconds() / 60, 1),
            "alerts": {"warning": ep["n_warning"], "error": ep["n_error"],
                       "critical": ep["n_critical"]},
            "severity_score": ep["score"], "subsystems": ep["subsystems"],
            "issue_types": ep["tags"], "classification": label, "evidence": rationale}


def summarize_incident(date: str | None = None) -> dict:
    """Structured JSON summary of the most severe episode (optionally on a date)."""
    events = _events()
    episodes = diagnostics.detect_episodes(events)
    if date is not None:
        episodes = [ep for ep in episodes if str(ep["start"].date()) == date]
        if not episodes:
            days = sorted({str(ep["start"].date())
                           for ep in diagnostics.detect_episodes(events)})
            return {"error": f"no alert episodes on {date}; episodes occurred on {days}"}

    top = episodes[0]
    incident = diagnostics.reconstruct_incident(events, top)
    t = incident["timings"]
    timeline = [{"time": str(row.timestamp), "subsystem": row.subsystem,
                 "severity": row.severity, "message": row.message}
                for row in incident["timeline"].itertuples()]
    # Three distinct spans, named so they cannot be conflated: alert_span_min
    # (in each episode dict), downtime_min, first_warning_to_recovery_min.
    timings = {("first_warning_to_recovery_min" if k == "total_min" else k):
               (str(v) if isinstance(v, pd.Timestamp) else v)
               for k, v in t.items()}
    chart = _episode_evidence_chart(top, t)  # a summary ships with its evidence
    return {
        "episode": _episode_json(top, events),
        "all_episodes": [_episode_json(ep, events)
                         for ep in diagnostics.detect_episodes(events)],
        "timeline": timeline,
        "timings": timings,
        "root_cause": _root_cause(top, events),
        "evidence_chart": chart.get("chart_path"),
    }


def draft_service_ticket(rank: int = 1) -> dict:
    """Service ticket (JSON + rendered text) for the episode at the given
    severity rank (1 = most severe). Every field -- title, root cause,
    priority, evidence chart, affected modules -- is derived from the
    selected episode's own data; nothing is issue-specific.
    """
    events = _events()
    episodes = diagnostics.detect_episodes(events)
    try:
        rank = int(rank)
    except (TypeError, ValueError):
        return {"error": f"rank must be an integer, got {rank!r}"}
    if not 1 <= rank <= len(episodes):
        return {"error": f"rank {rank} out of range; {len(episodes)} episodes "
                         "detected (1 = most severe)"}
    ep = episodes[rank - 1]
    incident = diagnostics.reconstruct_incident(events, ep)
    t = incident["timings"]
    label, _ = diagnostics.classify_episode(ep, events)
    tags = _tags_in_causal_order(ep)

    evidence = [f"{row.timestamp:%H:%M:%S} [{row.subsystem}/{row.severity}] {row.message}"
                for row in incident["timeline"].itertuples()
                if row.severity in ("WARNING", "ERROR", "CRITICAL")]
    modules = sorted(ep["alerts"]["module"].dropna().astype(int).unique().tolist())

    chart = _episode_evidence_chart(ep, t)

    title = " -> ".join(TAG_PHRASES.get(tag, tag) for tag in tags)
    title = title[0].upper() + title[1:]  # capitalize() would lowercase "SOC"
    if modules:
        title += f" (Module {', '.join(map(str, modules))})"
    priority = "P1" if ep["n_critical"] else ("P2" if ep["n_error"] else "P3")
    when = lambda key, fallback: str(t[key]) if pd.notna(t[key]) else fallback

    ticket = {
        "ticket_id": f"MP-{ep['start']:%Y%m%d}-{rank:03d}",
        "severity_rank": rank,
        "title": title,
        "priority": priority,
        "status": "OPEN",
        "site": "Megapack site (MP_Logs)",
        "affected_subsystems": ep["subsystems"],
        "affected_modules": modules,
        "issue_types": ep["tags"],
        "classification": label,
        "root_cause": _root_cause(ep, events),
        "first_warning": when("first_warning", "n/a"),
        "trip": when("trip", "n/a (no protective trip)"),
        "recovered": when("recovered", "unresolved (no recovery marker)"),
        "warning_to_trip_min": t["warning_to_trip_min"],
        "downtime_min": t["downtime_min"],
        "evidence": evidence,
        "attachment_chart": chart.get("chart_path"),
        "recommended_actions": [ACTIONS[tag] for tag in ep["tags"] if tag in ACTIONS],
    }

    timeline = f"Timeline: first warning {ticket['first_warning']}"
    if pd.notna(t["trip"]):
        timeline += f" -> trip {ticket['trip']} ({t['warning_to_trip_min']:.1f} min)"
    timeline += f" -> recovered {ticket['recovered']}"
    if t["downtime_min"] is not None:
        timeline += f" (downtime {t['downtime_min']:.0f} min)"
    lines = [f"SERVICE TICKET {ticket['ticket_id']}  [{ticket['priority']}]",
             f"Title: {ticket['title']}",
             f"Affected subsystems: {', '.join(ticket['affected_subsystems'])}",
             f"Issue types: {', '.join(ticket['issue_types'])}",
             f"Root cause: {ticket['root_cause']}",
             timeline,
             "Recommended actions:"]
    lines += [f"  - {a}" for a in ticket["recommended_actions"]]
    if ticket["attachment_chart"]:
        lines.append(f"Attachment: {ticket['attachment_chart']} "
                     f"({', '.join(chart.get('metrics', []))} around the episode, "
                     "alerts highlighted)")
    ticket["rendered_text"] = "\n".join(lines)
    return ticket
