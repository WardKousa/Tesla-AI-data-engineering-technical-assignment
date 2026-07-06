"""Part 2, Step 1: ETL for the Megapack site log (MP_Logs.txt).

Parses the semi-structured log (`timestamp | subsystem | severity | message`)
into a clean table with one row per event, extracting any numeric measurement
(value + unit), the module number where present, and a normalized message
template (numbers replaced by N) used to group "error types" in SQL.

Malformed-line policy (never silently drop; every input line is accounted for):
    - blank line                -> counted, not stored
    - missing severity field    -> salvaged, severity = "UNKNOWN"
    - empty subsystem field     -> salvaged, subsystem = "UNKNOWN"
    - timestamp without seconds -> salvaged, seconds assumed :00
    - unparseable line          -> quarantined to the rejected_lines table
Salvaged rows keep a `salvage_note` and their original line number, and the
stream is re-sorted by timestamp (the malformed lines are also out of order).

Usage:
    python etl.py [path/to/MP_Logs.txt]

Outputs:
    outputs/logs.db          SQLite tables `log_events` + `rejected_lines`
    outputs/log_events.csv   the same events table as CSV
"""

from __future__ import annotations

import re
import sqlite3
import sys
from pathlib import Path

import pandas as pd

LOG_PATH = Path("data/MP_Logs.txt")
DB_PATH = Path("outputs/logs.db")
CSV_PATH = Path("outputs/log_events.csv")

KNOWN_SUBSYSTEMS = {"BMS", "Battery", "Inverter", "Thermal", "Site"}
KNOWN_SEVERITIES = {"INFO", "WARNING", "ERROR", "CRITICAL"}
ALERT_SEVERITIES = ("WARNING", "ERROR", "CRITICAL")

# One entry per message shape that carries a measurement or a module number.
# Module-bearing messages put the module id BEFORE the value ("Overtemperature
# fault Module 7: 78.1°C"), so value and module are separate named groups --
# a naive "first number" parser would store 7 instead of 78.1.
NUM = r"(?P<value>\d+(?:\.\d+)?)"
MOD = r"Module (?P<module>\d+)"
MEASUREMENT_PATTERNS: list[tuple[re.Pattern, str | None, str | None]] = [
    (re.compile(rf"^Charge current: {NUM}A$"), "charge_current", "A"),
    (re.compile(rf"^Discharge current: {NUM}A$"), "discharge_current", "A"),
    (re.compile(rf"^SOC reading: {NUM}%$"), "soc", "%"),
    (re.compile(rf"^Voltage measured: {NUM}V$"), "voltage", "V"),
    (re.compile(rf"^Max cell voltage diff: {NUM}V$"), "cell_voltage_diff", "V"),
    (re.compile(rf"^Cell imbalance detected in {MOD}: {NUM}V$"), "cell_voltage_diff", "V"),
    (re.compile(rf"^Cell undervoltage {MOD}: {NUM} V/cell$"), "cell_voltage", "V/cell"),
    (re.compile(rf"^Critical voltage drop detected: {NUM}V \({MOD}\)$"), "voltage", "V"),
    (re.compile(rf"^Overtemperature fault {MOD}: {NUM}°C$"), "temperature", "°C"),
    (re.compile(rf"^{MOD} balancing active$"), None, None),
    (re.compile(rf"^{MOD} flagged for inspection$"), None, None),
    (re.compile(rf"^Temperature (?:reading|approaching safe threshold|exceeded safe threshold): {NUM}°C$"),
     "temperature", "°C"),
    (re.compile(rf"^Coolant (?:loop flow|loop flow low|flow below minimum threshold): {NUM} L/min$"),
     "coolant_flow", "L/min"),
    (re.compile(rf"^Coolant pump speed: {NUM}%$"), "pump_speed", "%"),
    (re.compile(rf"^Fan (?:speed|at maximum speed): {NUM} RPM$"), "fan_speed", "RPM"),
    (re.compile(rf"^Grid frequency(?: out of band)?: {NUM} Hz$"), "grid_frequency", "Hz"),
    (re.compile(rf"^AC output: {NUM} kW$"), "ac_output", "kW"),
    (re.compile(rf"^Power derated due to thermal limit: {NUM} kW$"), "power_limit", "kW"),
]


def _parse_timestamp(text: str) -> tuple[pd.Timestamp | None, str | None]:
    """Return (timestamp, salvage_note). Accepts a missing-seconds variant."""
    for fmt, note in (
        ("%Y-%m-%d %H:%M:%S", None),
        ("%Y-%m-%d %H:%M", "timestamp missing seconds; assumed :00"),
    ):
        try:
            return pd.Timestamp(pd.to_datetime(text, format=fmt)), note
        except ValueError:
            continue
    return None, None


def _extract_measurement(message: str) -> dict:
    """First matching pattern wins; unmatched messages carry no measurement."""
    for pattern, metric, unit in MEASUREMENT_PATTERNS:
        m = pattern.match(message)
        if m:
            groups = m.groupdict()
            value = float(groups["value"]) if groups.get("value") else None
            module = int(groups["module"]) if groups.get("module") else None
            return {"metric": metric, "value": value, "unit": unit, "module": module}
    return {"metric": None, "value": None, "unit": None, "module": None}


def _parse_line(raw: str, line_no: int) -> tuple[str, dict | None]:
    """Classify one raw line -> ("event"|"reject"|"blank", payload)."""
    if not raw.strip():
        return "blank", None

    parts = [p.strip() for p in raw.split("|")]
    notes: list[str] = []

    if len(parts) == 4:
        ts_text, subsystem, severity, message = parts
    elif len(parts) == 3 and parts[2] not in KNOWN_SEVERITIES:
        # Severity field missing entirely; third field is the message.
        ts_text, subsystem, message = parts
        severity = "UNKNOWN"
        notes.append("severity field missing; set to UNKNOWN")
    else:
        return "reject", {"raw_line_no": line_no, "raw_line": raw.rstrip("\n"),
                          "reason": "does not match 'timestamp | subsystem | severity | message'"}

    timestamp, ts_note = _parse_timestamp(ts_text)
    if timestamp is None:
        return "reject", {"raw_line_no": line_no, "raw_line": raw.rstrip("\n"),
                          "reason": f"unparseable timestamp {ts_text!r}"}
    if ts_note:
        notes.append(ts_note)

    if not subsystem:
        subsystem = "UNKNOWN"
        notes.append("subsystem field empty; set to UNKNOWN")
    elif subsystem not in KNOWN_SUBSYSTEMS:
        notes.append(f"unrecognized subsystem {subsystem!r}; kept as-is")
    if severity not in KNOWN_SEVERITIES | {"UNKNOWN"}:
        notes.append(f"unrecognized severity {severity!r}; kept as-is")

    return "event", {
        "raw_line_no": line_no,
        "timestamp": timestamp,
        "subsystem": subsystem,
        "severity": severity,
        "message": message,
        "message_template": re.sub(r"\d+(?:\.\d+)?", "N", message),
        **_extract_measurement(message),
        "salvage_note": "; ".join(notes) or None,
    }


def parse_log(path: Path) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    """Parse the raw log into (events, rejected_lines, accounting stats)."""
    events, rejects, blanks = [], [], 0
    lines = path.read_text(encoding="utf-8").splitlines()
    for line_no, raw in enumerate(lines, start=1):
        kind, payload = _parse_line(raw, line_no)
        if kind == "event":
            events.append(payload)
        elif kind == "reject":
            rejects.append(payload)
        else:
            blanks += 1

    events_df = pd.DataFrame(events).sort_values(["timestamp", "raw_line_no"]).reset_index(drop=True)
    events_df["date"] = events_df["timestamp"].dt.strftime("%Y-%m-%d")
    events_df["hour"] = events_df["timestamp"].dt.hour
    rejects_df = pd.DataFrame(rejects, columns=["raw_line_no", "raw_line", "reason"])
    stats = {"total_lines": len(lines), "events": len(events_df),
             "rejected": len(rejects_df), "blank": blanks}
    return events_df, rejects_df, stats


def validate(events: pd.DataFrame, rejects: pd.DataFrame, stats: dict) -> None:
    """Two tiers: hard asserts for internal consistency (code correctness),
    printed checks for ground-truth expectations about this particular file."""
    # Hard invariants -- if these fail, the parser itself is wrong.
    assert stats["events"] + stats["rejected"] + stats["blank"] == stats["total_lines"], \
        "line accounting does not sum to the input line count"
    assert events["timestamp"].is_monotonic_increasing, "events not sorted by timestamp"
    has_value = events["value"].notna()
    assert (events.loc[has_value, "unit"].notna()).all(), "value present without a unit"
    numeric_unparsed = events[events["message"].str.contains(r"\d", regex=True)
                              & events["metric"].isna() & events["module"].isna()]
    assert numeric_unparsed.empty, \
        f"messages with numbers escaped the pattern table:\n{numeric_unparsed['message'].unique()}"

    # Soft expectations -- true for the assignment file, warn-only elsewhere.
    def check(ok: bool, label: str) -> None:
        print(f"  {'OK ' if ok else 'WARN'} {label}")

    n_alerts = events["severity"].isin(ALERT_SEVERITIES).sum()
    n_salvaged = events["salvage_note"].notna().sum()
    check(n_alerts == 45, f"alert rows (WARNING/ERROR/CRITICAL) == 45 (got {n_alerts})")
    check(n_salvaged == 3, f"salvaged rows == 3 (got {n_salvaged})")
    check(stats["rejected"] == 1, f"rejected lines == 1 (got {stats['rejected']})")
    check(stats["blank"] == 1, f"blank lines == 1 (got {stats['blank']})")


def persist(events: pd.DataFrame, rejects: pd.DataFrame) -> None:
    """Write SQLite (log_events + rejected_lines) and CSV; rebuilt each run."""
    DB_PATH.parent.mkdir(exist_ok=True)
    DB_PATH.unlink(missing_ok=True)
    table = events.copy()
    table["timestamp"] = table["timestamp"].astype(str)
    with sqlite3.connect(DB_PATH) as conn:
        table.to_sql("log_events", conn, index=False)
        rejects.to_sql("rejected_lines", conn, index=False)
        n_db = conn.execute("SELECT COUNT(*) FROM log_events").fetchone()[0]
    assert n_db == len(events), "SQLite row count != DataFrame row count"
    events.to_csv(CSV_PATH, index=False)


def build(path: Path = LOG_PATH) -> pd.DataFrame:
    """Full pipeline (parse -> validate -> persist); returns the events table."""
    events, rejects, stats = parse_log(path)

    print("=" * 70)
    print("PART 2, STEP 1 - LOG ETL")
    print(f"Input: {path}  ({stats['total_lines']:,} lines, "
          f"{events['timestamp'].min()} -> {events['timestamp'].max()})")
    print(f"Parsed events: {stats['events']:,}   rejected: {stats['rejected']}   "
          f"blank: {stats['blank']}")

    salvaged = events[events["salvage_note"].notna()]
    print("\n-- Malformed-line handling " + "-" * 42)
    for row in salvaged.itertuples():
        print(f"  salvaged line {row.raw_line_no}: {row.salvage_note}")
    for row in rejects.itertuples():
        print(f"  rejected line {row.raw_line_no}: {row.reason}  [{row.raw_line}]")
    print(f"  (+ {events['salvage_note'].isna().sum():,} well-formed lines, "
          "1 blank line skipped)" if not salvaged.empty else "")

    print("\n-- Events by subsystem x severity " + "-" * 35)
    print(pd.crosstab(events["subsystem"], events["severity"]).to_string())

    n_measured = events["value"].notna().sum()
    print(f"\nMeasurements extracted: {n_measured:,} rows "
          f"({n_measured / len(events):.0%}) across "
          f"{events['metric'].nunique()} metrics; "
          f"{events['module'].notna().sum()} rows carry a module number.")

    print("\n-- Validation " + "-" * 55)
    validate(events, rejects, stats)
    persist(events, rejects)
    print(f"\nSQLite DB saved to {DB_PATH} (tables: log_events, rejected_lines)")
    print(f"CSV saved to {CSV_PATH}")
    return events


if __name__ == "__main__":
    build(Path(sys.argv[1]) if len(sys.argv) > 1 else LOG_PATH)
